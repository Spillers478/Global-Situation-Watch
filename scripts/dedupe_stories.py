"""
Runs after redteam.py, before build_brief.py. Reads each topic's FINAL
article list -- the same one build_brief.py will actually display
(data/<date>/redteam/<topic_id>.json when the red team pass succeeded for
that topic, raw data/<date>/<topic_id>.json otherwise; see
build_brief.load_articles) -- and, in ONE call per topic, asks Claude to:

1. GROUP articles that report the same real-world story/event, including
   wire-service syndication where the wording differs but the underlying
   event is identical. So build_brief.py can collapse "5 outlets ran the
   same AP story about X" into one card with an expandable "+4 other
   sources" instead of 5 near-identical headlines back to back.

2. Within any group of 2+ sources, flag a CONTRADICTION only when sources
   genuinely disagree on one of three narrow, checkable things: a figure
   (casualty/damage counts), attribution (who is responsible), or
   outcome/status (what actually happened). Deliberately narrow --
   "do these two articles disagree" is a much easier question for a model
   to answer wrong (differently-worded, differently-emphasized coverage
   of the same true event reads as "contradictory" if you don't pin the
   question down) than "do they disagree on this specific figure/actor/
   outcome." False positives here would show a reader a manufactured
   disagreement between two accurate articles, which is worse than
   missing a real one, so the prompt is written to prefer silence when
   unsure -- see build_prompt().

Writes data/<date>/dedup/<topic_id>.json:
  {"groups": {"<article url>": "<group letter>", ...},
   "contradictions": [{"group": "<letter>", "claim_type": "figures" |
     "attribution" | "outcome", "summary": "...",
     "sources": [{"source": "...", "url": "...", "says": "..."}, ...]}]}
Keyed by URL (not list position) because build_brief.py re-sorts articles
(rank_articles -- trusted wire services first) before rendering, so a
positional index here wouldn't line up with render order there.

A topic with no data/<date>/dedup/<topic_id>.json (this step skipped
entirely, or that topic's call failed) just renders with no grouping/
contradiction UI -- build_brief.py degrades gracefully, the same pattern
as every other optional Claude-dependent layer in this pipeline
(synthesize.py's BLUF, redteam.py's classification).

One call per topic, not chunked like redteam.py, because the input here
is already small (at most MAX_ARTICLES_PER_TOPIC articles, title+
description only) and the output is compact structured JSON, not a
per-article line. A JSON-parse failure for one topic just means that
topic gets no grouping/contradiction data; it never blocks or breaks the
rest of the page.

Retried up to MAX_ATTEMPTS on a failed or truncated call, same pattern
as redteam.py's classify_topic -- a busy topic (near the
MAX_ARTICLES_PER_TOPIC cap: the most syndicated wire coverage, the most
groups, the most contradiction candidates to write out) produces the
largest response and is exactly the case most likely to hit a transient
failure or run past MAX_TOKENS_PER_CALL, so a single-shot call with no
retry silently drops precisely the topics this feature matters most for.
(First shipped version of this script had no retry and no size margin
for that reason, and both flagship topics -- capped at
MAX_ARTICLES_PER_TOPIC, the two busiest topics on the page -- came back
empty on the first real run. Fixed here.)

Writes data/<date>/dedup/_report.json: {"topics": [...succeeded],
"skipped": [...too few articles], "failed": [...no result after
MAX_ATTEMPTS]} so a silent per-topic failure like that is visible
without digging through GitHub Actions logs.

Requires ANTHROPIC_API_KEY (same secret as redteam.py/synthesize.py).
Uses Sonnet, not Haiku -- getting a contradiction flag wrong (or missing
a real one) is a judgment call worth the more careful model, same
reasoning as redteam.py's model choice.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import build_brief
from topics import TIER1, TOPICS

ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-sonnet-5"

# Same cap redteam.py uses for its per-topic input -- keeps this call's
# prompt and output size bounded regardless of how noisy a topic's day was.
MAX_ARTICLES_PER_TOPIC = 25
# Matches redteam.py's per-call budget. A busy, near-the-cap topic's
# response scales with how much duplication and how many contradiction
# candidates it actually has -- the two flagship topics (always at the
# cap) turned out to need more room than the original 4096 estimate gave
# them; see the module docstring.
MAX_TOKENS_PER_CALL = 8192

# Same retry shape as redteam.py's classify_topic: on a failed or
# truncated call, wait and try the whole topic again rather than giving
# up after one attempt -- see the module docstring for why the busiest
# topics are exactly the ones a single-shot call is most likely to lose.
MAX_ATTEMPTS = 3
REQUEST_PAUSE_SECONDS = 0.5
RETRY_PAUSE_SECONDS = 3.0

VALID_CLAIM_TYPES = {"figures", "attribution", "outcome"}
_GROUP_LETTER_RE = re.compile(r"^[a-z]$")


def all_topic_defs():
    """(topic_id, label) for every flagship + taxonomy topic -- skips the
    "-us-lens" pulls, same as redteam.py's collect_articles: that sidebar
    is a media-comparison view of the SAME flagship articles, not
    independent reporting worth grouping/comparing."""
    return [(t["id"], t["label"]) for t in TIER1] + [(t["id"], t["label"]) for t in TOPICS]


def build_prompt(label, articles):
    lines = []
    for i, a in enumerate(articles):
        title = (a.get("title") or "").strip()
        desc = (a.get("description") or "").strip()
        source = (a.get("source") or {}).get("name") or "Unknown source"
        lines.append(f"{i} :: [{source}] {title} :: {desc}")

    return f"""You are reviewing today's already-filtered article list for one topic ("{label}") of a geopolitical/military awareness brief. Every article below has already been judged relevant to this topic -- your job is not to re-filter them.

Two tasks:

1. GROUPING: assign every article a group letter (a, b, c, ...). Articles that report on the SAME real-world story or event -- including wire-service syndication where the wording differs but the underlying event is identical -- get the SAME letter. An article with no duplicate gets its own unique letter. Be conservative: only group articles that are clearly about the same specific event (same incident, same statement, same strike), not merely the same general topic, country, or ongoing situation.

2. CONTRADICTIONS: for any group with 2 or more articles, check ONLY these three things:
   - figures: do sources report DIFFERENT casualty counts, damage figures, or other specific numbers for the SAME event?
   - attribution: do sources DISAGREE on who is responsible or who carried out an action?
   - outcome: do sources DISAGREE on what actually happened, or the current status/result?
   Flag a contradiction ONLY when sources genuinely conflict on one of these three things. Do NOT flag it when they simply use different words, emphasize different aspects, or one mentions a detail the other omits without disputing it. If you are not sure it's a real disagreement, do not flag it -- a missed contradiction is a much smaller problem than inventing one between two articles that actually agree.

Articles (format: <id> :: [source] title :: description):

{chr(10).join(lines)}

Return JSON only, no markdown fences, no commentary, in exactly this shape:
{{"groups": {{"0": "a", "1": "a", "2": "b"}}, "contradictions": [{{"group": "a", "claim_type": "figures", "summary": "one sentence describing the disagreement", "sources": [{{"article_id": 0, "says": "short paraphrase of what this source specifically claims"}}, {{"article_id": 1, "says": "..."}}]}}]}}

Every article id from 0 to {len(articles) - 1} must appear as a key in "groups", as a string. Omit "contradictions" (empty list) if there are none -- do not invent one just to have something to report."""


def parse_json_response(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def call_with_retry(client, topic_id, label, articles):
    """One topic's call, retried up to MAX_ATTEMPTS on failure (bad JSON,
    truncation, or an API/network error the SDK's own retries didn't
    absorb) -- see the module docstring. Returns (result, last_error):
    result is None only if every attempt failed."""
    prompt = build_prompt(label, articles)
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(RETRY_PAUSE_SECONDS * (attempt - 1))
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS_PER_CALL,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
            raw = parse_json_response(text)
            return validate_and_resolve(raw, articles), None
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            print(f"  {topic_id}: attempt {attempt}/{MAX_ATTEMPTS} failed -- {last_error}", file=sys.stderr)
    return None, last_error


def validate_and_resolve(raw, articles):
    """Turns the model's index-keyed response into the URL-keyed shape this
    module writes to disk, dropping anything malformed rather than trusting
    it -- a bad group id, an out-of-range article_id, or an off-list
    claim_type just gets that one entry silently discarded instead of
    corrupting the file or crashing the run."""
    n = len(articles)
    urls = [a.get("url") for a in articles]

    raw_groups = raw.get("groups") if isinstance(raw, dict) else None
    groups_by_index = {}
    if isinstance(raw_groups, dict):
        for k, v in raw_groups.items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < n and isinstance(v, str) and _GROUP_LETTER_RE.match(v.strip().lower()):
                groups_by_index[idx] = v.strip().lower()

    # Any article the model didn't return a group for still needs one --
    # give it a unique letter of its own rather than dropping it from the
    # map (an ungrouped article must still render normally).
    used_letters = set(groups_by_index.values())
    next_ord = ord("a")

    def fresh_letter():
        nonlocal next_ord
        while chr(next_ord) in used_letters:
            next_ord += 1
        letter = chr(next_ord)
        used_letters.add(letter)
        return letter

    groups_by_url = {}
    for i in range(n):
        if not urls[i]:
            continue
        letter = groups_by_index.get(i) or fresh_letter()
        groups_by_url[urls[i]] = letter

    contradictions = []
    for entry in (raw.get("contradictions") or []) if isinstance(raw, dict) else []:
        if not isinstance(entry, dict):
            continue
        group = entry.get("group")
        claim_type = entry.get("claim_type")
        summary = entry.get("summary")
        sources_in = entry.get("sources")
        if (not isinstance(group, str) or not _GROUP_LETTER_RE.match(group.strip().lower())
                or claim_type not in VALID_CLAIM_TYPES
                or not isinstance(summary, str) or not summary.strip()
                or not isinstance(sources_in, list) or len(sources_in) < 2):
            continue
        resolved_sources = []
        for s in sources_in:
            if not isinstance(s, dict):
                continue
            try:
                aid = int(s.get("article_id"))
            except (TypeError, ValueError):
                continue
            says = s.get("says")
            if not (0 <= aid < n) or not urls[aid] or not isinstance(says, str) or not says.strip():
                continue
            article = articles[aid]
            resolved_sources.append({
                "source": (article.get("source") or {}).get("name") or "Unknown source",
                "url": urls[aid],
                "says": says.strip(),
            })
        if len(resolved_sources) >= 2:
            contradictions.append({
                "group": group.strip().lower(),
                "claim_type": claim_type,
                "summary": summary.strip(),
                "sources": resolved_sources,
            })

    return {"groups": groups_by_url, "contradictions": contradictions}


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data_dir = ROOT / "data" / today
    out_dir = data_dir / "dedup"

    if not data_dir.exists():
        print(f"No data directory for {today} -- run fetch_news.py first.", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set -- skipping duplicate/contradiction detection; "
              "pages render without story grouping.", file=sys.stderr)
        return

    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed -- skipping duplicate/contradiction detection.", file=sys.stderr)
        return

    client = anthropic.Anthropic(api_key=api_key)
    out_dir.mkdir(parents=True, exist_ok=True)

    succeeded, skipped, failed = [], [], []
    for topic_id, label in all_topic_defs():
        articles = build_brief.dedupe(build_brief.load_articles(data_dir, topic_id))[:MAX_ARTICLES_PER_TOPIC]
        articles = [a for a in articles if a.get("url")]  # can't key a group without a stable id
        if len(articles) < 2:
            skipped.append(topic_id)
            continue  # nothing to group or compare

        result, error = call_with_retry(client, topic_id, label, articles)
        if result is None:
            print(f"  {topic_id}: FAILED after {MAX_ATTEMPTS} attempts -- {error}", file=sys.stderr)
            failed.append({"topic": topic_id, "error": error})
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue

        with open(out_dir / f"{topic_id}.json", "w") as f:
            json.dump(result, f, indent=2)
        n_groups = len(set(result["groups"].values()))
        n_dupes = len(articles) - n_groups
        print(f"  {topic_id}: {len(articles)} articles -> {n_groups} stories "
              f"({n_dupes} duplicate{'s' if n_dupes != 1 else ''}), "
              f"{len(result['contradictions'])} contradiction(s) flagged")
        succeeded.append(topic_id)
        time.sleep(REQUEST_PAUSE_SECONDS)

    with open(out_dir / "_report.json", "w") as f:
        json.dump({"topics": succeeded, "skipped": skipped, "failed": failed}, f, indent=2)

    print(f"\nDuplicate/contradiction detection done: {len(succeeded)} topic(s) written, "
          f"{len(skipped)} skipped (fewer than 2 articles), {len(failed)} failed. Wrote {out_dir}/")
    if failed:
        print(f"Failed topics (see {out_dir / '_report.json'} for details): "
              f"{', '.join(f['topic'] for f in failed)}", file=sys.stderr)


if __name__ == "__main__":
    main()
