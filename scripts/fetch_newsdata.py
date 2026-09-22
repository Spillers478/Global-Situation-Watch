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
        print(f"  WARNING: newsdata.io returned {data.get('status')}: {data.get('results')}", file=sys.stderr)
    return data


def fetch_latest(query, search_in=None):
    params = {
        "language": "en",
        "excludedomain": _EXCLUDE_DOMAINS,
        "removeduplicate": 1,
        "size": 10,  # free-tier max articles per request
    }
    # newsdata uses a dedicated qInTitle param instead of a searchIn flag
    # alongside q -- see topics.py "search_in" comments (same per-topic
    # decision NewsAPI's searchIn="title" encodes).
    if search_in == "title":
        params["qInTitle"] = query
    else:
        params["q"] = query
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


def main():
    now = datetime.now(timezone.utc)
    out_dir = Path(__file__).resolve().parent.parent / "data" / now.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Raw newsdata payloads kept separately for audit/debugging -- the
    # merge above only writes the normalized articles into the shared
    # per-topic files the rest of the pipeline reads.
    raw_dir = out_dir / "newsdata"
    raw_dir.mkdir(parents=True, exist_ok=True)

    targets = [(t["id"], t["label"], t["query"], t.get("search_in")) for t in TIER1]
    if TOPIC_SWEEP_ENABLED:
        targets += [(t["id"], t["label"], t["query"], t.get("search_in")) for t in TOPICS]
    else:
        print("Topic sweep disabled (TOPIC_SWEEP_ENABLED=False in topics.py) -- flagships only.")

    total_added = 0
    for key, label, query, search_in in targets:
        print(f"Fetching (newsdata.io): {key} - {label}")
        raw = fetch_latest(query, search_in=search_in)
        with open(raw_dir / f"{key}.json", "w") as f:
            json.dump(raw, f, indent=2)

        normalized = [normalize(a) for a in (raw.get("results") or [])]
        added = merge_into(out_dir / f"{key}.json", normalized)
        total_added += added
        print(f"  +{added} new article(s) merged (of {len(normalized)} returned)")
        time.sleep(REQUEST_PAUSE_SECONDS)

    print(f"\nDone. {_request_count} requests used, {total_added} new articles merged from newsdata.io into {out_dir}/")


if __name__ == "__main__":
    main()
