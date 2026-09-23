"""
Second-source ingestion: pulls today's article set from newsdata.io's
`/latest` endpoint for the two Tier 1 flagship topics plus the full
14-topic taxonomy sweep (same queries defined once in topics.py, shared
with fetch_news.py), normalizes newsdata's article schema to match
NewsAPI's, and merges the result into the *same* data/<date>/<key>.json
files fetch_news.py writes -- deduped by URL. Downstream stages
(redteam.py, synthesize.py, build_brief.py) all read that shared file
shape, so nothing else in the pipeline needs to change: newsdata
articles just show up alongside NewsAPI articles in every section.

Run this after fetch_news.py (it merges into whatever fetch_news.py
already wrote) but it also works standalone -- if data/<date>/<key>.json
doesn't exist yet, it's created fresh.

Requires NEWSDATA_KEY in the environment. Never hardcode the key -- in
GitHub Actions it is supplied via repo secrets (see README).

newsdata.io free plan: 200 credits/day, 1 credit = 1 request = up to 10
articles, rate-limited to 30 credits/15 minutes. One full sweep here
(2 flagships + 14 topics, no separate "US lens" call -- see below) is
16 requests, comfortably under both caps even with the pacing delay
already used by fetch_news.py.

The `/latest` endpoint doesn't have a `/top-headlines`-style country
scoped variant the way NewsAPI does, so there's no newsdata equivalent
of the "-us-lens" files -- those stay NewsAPI-only.
"""
import os
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from topics import TIER1, TOPICS, TOPIC_SWEEP_ENABLED, EXCLUDE_DOMAINS

API_KEY = os.environ.get("NEWSDATA_KEY")
BASE_URL = "https://newsdata.io/api/1/latest"
REQUEST_TIMEOUT = 15
REQUEST_PAUSE_SECONDS = 1.5  # a bit more conservative than fetch_news.py's --
                              # newsdata's free tier caps at 30 credits/15min

# Same purpose as fetch_news.py's MAX_REQUESTS: a hard ceiling so a bug
# can't silently blow through the daily 200-credit cap.
MAX_REQUESTS = 30

# Every topic below sends a structurally identical request -- same
# endpoint, same optional params, only the query text differs. So a
# failure caused by something systemic (bad/expired key, a param this
# plan doesn't allow, rate limit hit, newsdata down) will fail on every
# single topic, spending a credit each time to learn the same thing.
# Bail out after this many *consecutive* errors: a systemic problem then
# costs 3 credits instead of 16, while a one-off bad query for a single
# topic still lets the remaining topics through.
MAX_CONSECUTIVE_ERRORS = 3

# newsdata's excludedomain param takes a comma-separated list like
# NewsAPI's excludeDomains -- reuse the same list from topics.py so both
# providers apply the same noise-control decisions.
_EXCLUDE_DOMAINS = EXCLUDE_DOMAINS

_request_count = 0


def _get(params):
    global _request_count
    if _request_count >= MAX_REQUESTS:
        raise RuntimeError(
            f"Refusing to exceed the safety ceiling of {MAX_REQUESTS} requests "
            f"in a single run ({_request_count} used)."
        )
    if not API_KEY:
        raise RuntimeError(
            "NEWSDATA_KEY is not set in the environment. Locally: export it "
            "before running. In GitHub Actions: add it as a repo secret."
        )
    params = {**params, "apikey": API_KEY}
    resp = requests.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
    _request_count += 1
    try:
        data = resp.json()
    except ValueError:
        print(f"  ERROR: non-JSON response from newsdata.io (HTTP {resp.status_code})", file=sys.stderr)
        return {"status": "error", "results": []}
    if data.get("status") != "success":
        # On error, newsdata puts a {"message": ..., "code": ...} dict in
        # "results" (not a list of articles) -- iterating that dict below
        # would hand normalize() a bare string key and blow up with
        # AttributeError. Sanitize the shape here so every caller can
        # always assume raw["results"] is a list, empty on failure.
        print(f"  WARNING: newsdata.io returned {data.get('status')}: {data.get('results')}", file=sys.stderr)
        return {"status": "error", "results": []}
    return data


def fetch_latest(query, search_in=None, timeframe=None):
    params = {
        "language": "en",
        "excludedomain": _EXCLUDE_DOMAINS,
        "removeduplicate": 1,
        "size": 10,  # free-tier max articles per request
    }
    # newsdata uses a dedicated qInTitle param instead of a searchIn flag
    # alongside q -- see topics.py "search_in" comments (same per-topic
    # decision NewsAPI's searchIn="title" encodes). Anything other than
    # exactly "title" (e.g. NewsAPI's "title,description" combo, which
    # newsdata has no equivalent narrower param for) falls back to a plain
    # q search across all fields -- the closest available approximation.
    if search_in == "title":
        params["qInTitle"] = query
    else:
        params["q"] = query
    # Optional per-topic override of newsdata's lookback window (free tier
    # defaults to 48h). Time-perishable topics (see topics.py's T15
    # "newsdata_timeframe" comment) can narrow this; most topics omit it
    # and get the plan default.
    if timeframe:
        params["timeframe"] = timeframe
    return _get(params)


def normalize(article):
    """Reshapes a newsdata.io article into the NewsAPI-shaped dict every
    downstream script (redteam.py, synthesize.py, build_brief.py)
    already expects: title/url/description/content/publishedAt/source.name.
    pubdate arrives as "YYYY-MM-DD HH:MM:SS" (UTC, space-separated, no
    offset); reformat to ISO 8601 so it sorts/displays consistently next
    to NewsAPI's publishedAt values."""
    published = article.get("pubdate") or ""
    if published:
        try:
            published = (
                datetime.strptime(published, "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        except ValueError:
            pass  # leave as-is if the format ever changes

    return {
        "title": article.get("title"),
        "url": article.get("link"),
        "description": article.get("description"),
        # newsdata's free tier doesn't return full_content, and its
        # `content` field on this plan is typically null/truncated same
        # spirit as NewsAPI's -- pass through whatever's there.
        "content": article.get("content"),
        "publishedAt": published,
        "source": {"name": article.get("source_name") or article.get("source_id") or "Unknown source"},
        "_provider": "newsdata.io",
    }


def merge_into(path, new_articles):
    """Loads the existing {"status": ..., "articles": [...]} file this
    key already has (written by fetch_news.py, or by a previous
    newsdata run), appends any article whose URL isn't already present,
    and writes it back. Creates the file fresh if it doesn't exist."""
    if path.exists():
        with open(path) as f:
            payload = json.load(f)
    else:
        payload = {"status": "ok", "articles": []}

    existing = payload.setdefault("articles", [])
    seen = {a.get("url") for a in existing if a.get("url")}
    added = 0
    for a in new_articles:
        if a.get("url") and a["url"] not in seen:
            existing.append(a)
            seen.add(a["url"])
            added += 1

    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return added


USAGE = """Usage: python scripts/fetch_newsdata.py [--dry-run] [--only TOPIC_ID]

  (no flags)        Full sweep: every topic, merged into data/<date>/. ~16 credits.
  --dry-run         Print the exact request each topic would send and stop.
                    Costs ZERO credits -- use it to sanity-check queries,
                    query lengths, and params after editing topics.py.
  --only TOPIC_ID   Fetch a single topic (e.g. --only T03) and pretty-print
                    the raw newsdata.io response. Costs ONE credit -- use it
                    to verify the live API contract without spending a full
                    sweep (or a CI run) to find out something is wrong.
"""


def _parse_args(argv):
    dry_run = "--dry-run" in argv
    only = None
    if "--only" in argv:
        i = argv.index("--only")
        if i + 1 >= len(argv):
            print(USAGE, file=sys.stderr)
            sys.exit(2)
        only = argv[i + 1]
    unknown = [a for a in argv if a.startswith("--") and a not in ("--dry-run", "--only")]
    if unknown:
        print(f"Unknown option(s): {', '.join(unknown)}\n\n{USAGE}", file=sys.stderr)
        sys.exit(2)
    return dry_run, only


def main(argv=None):
    dry_run, only = _parse_args(argv if argv is not None else sys.argv[1:])

    now = datetime.now(timezone.utc)
    out_dir = Path(__file__).resolve().parent.parent / "data" / now.strftime("%Y-%m-%d")
    raw_dir = out_dir / "newsdata"
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        # Raw newsdata payloads kept separately for audit/debugging -- the
        # merge above only writes the normalized articles into the shared
        # per-topic files the rest of the pipeline reads.
        raw_dir.mkdir(parents=True, exist_ok=True)

    # Prefer each topic's newsdata_query (hand-shortened to fit newsdata's
    # 100-char q/qInTitle cap -- see topics.py "newsdata_query" section);
    # fall back to the full NewsAPI-tuned query for any topic that hasn't
    # gotten a shortened version yet.
    def _target(t):
        return (t["id"], t["label"], t.get("newsdata_query", t["query"]), t.get("search_in"),
                t.get("newsdata_timeframe"))

    targets = [_target(t) for t in TIER1]
    if TOPIC_SWEEP_ENABLED:
        targets += [_target(t) for t in TOPICS]
    else:
        print("Topic sweep disabled (TOPIC_SWEEP_ENABLED=False in topics.py) -- flagships only.")

    if only:
        targets = [t for t in targets if t[0] == only]
        if not targets:
            print(f"No topic with id {only!r}. Known ids: "
                  f"{', '.join(t['id'] for t in TIER1 + TOPICS)}", file=sys.stderr)
            sys.exit(2)

    if dry_run:
        print("DRY RUN -- no requests sent, no credits spent.\n")
        for key, label, query, search_in, timeframe in targets:
            field = "qInTitle" if search_in == "title" else "q"
            status = "OK" if len(query) <= 100 else "TOO LONG (cap 100)"
            tf_note = f", timeframe={timeframe}h" if timeframe else ""
            print(f"{key} - {label}")
            print(f"  {field} ({len(query)} chars, {status}{tf_note}): {query}")
        print(f"\n{len(targets)} topic(s) would be fetched = {len(targets)} credit(s).")
        return

    total_added = 0
    consecutive_errors = 0
    for idx, (key, label, query, search_in, timeframe) in enumerate(targets):
        print(f"Fetching (newsdata.io): {key} - {label}")
        if len(query) > 100:
            # Safety net, not the primary control -- topics.py's
            # newsdata_query values are hand-checked to stay under
            # newsdata's 100-char cap. If one ever creeps over (an edit
            # to topics.py, a topic missing its override), skip that one
            # topic rather than spend a credit on a request newsdata will
            # reject anyway.
            print(f"  WARNING: query is {len(query)} chars (newsdata's cap is 100) -- skipping {key}. "
                  f"Add/shorten its newsdata_query in topics.py.", file=sys.stderr)
            continue
        try:
            raw = fetch_latest(query, search_in=search_in, timeframe=timeframe)
        except RuntimeError:
            raise  # the MAX_REQUESTS / missing-key ceilings are meant to stop the whole run
        except Exception as e:
            # Any other failure (network blip, unexpected response shape)
            # shouldn't take down NewsAPI's already-fetched data for every
            # other topic -- log it and move on, same graceful-degradation
            # spirit as redteam.py/synthesize.py.
            print(f"  ERROR fetching {key} from newsdata.io: {e}", file=sys.stderr)
            raw = {"status": "error", "results": []}

        if raw.get("status") != "success":
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(
                    f"\nABORTING: {consecutive_errors} consecutive newsdata.io errors -- this looks "
                    f"systemic (key, plan limits, or the API itself), not one bad query. Stopping "
                    f"here so the remaining {len(targets) - idx - 1} "
                    f"topic(s) don't each spend a credit to fail the same way. "
                    f"{_request_count} credit(s) used. Diagnose with: "
                    f"python scripts/fetch_newsdata.py --only {key}",
                    file=sys.stderr,
                )
                break
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue
        consecutive_errors = 0

        with open(raw_dir / f"{key}.json", "w") as f:
            json.dump(raw, f, indent=2)

        results = raw.get("results") or []
        if only:
            # Single-topic diagnostic mode: show what the API actually
            # returned, since that's the whole point of spending the credit.
            print(json.dumps(raw, indent=2)[:4000])

        normalized = [normalize(a) for a in results]
        added = merge_into(out_dir / f"{key}.json", normalized)
        total_added += added
        print(f"  +{added} new article(s) merged (of {len(normalized)} returned)")
        time.sleep(REQUEST_PAUSE_SECONDS)

    print(f"\nDone. {_request_count} credit(s) used, {total_added} new articles merged from newsdata.io into {out_dir}/")


if __name__ == "__main__":
    main()
