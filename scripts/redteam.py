"""
Reads today's raw NewsAPI/newsdata JSON from data/<date>/ (every TIER1
flagship and TOPICS taxonomy pull -- see topics.py) and, for every
retrieved article, has Claude decide:

1. Does it actually belong to the topic it was retrieved under, a
   DIFFERENT topic in the taxonomy, or none of them (discard)?
2. If it's kept, which combatant command's area of responsibility is it
   talking about (USEUCOM, USCENTCOM, USINDOPACOM, USAFRICOM, USSOUTHCOM,
   USNORTHCOM, or "Transregional" when it doesn't map to a single AOR)?

This is the "red team" pass: topics.py's query tightening (phrase-
anchoring, searchIn=title, NOT exclusions, excluded domains) cuts obvious
keyword-homonym noise at retrieval time, but it's still boolean keyword
search -- it can't tell a real military offensive from an NFL "offensive
line" with certainty, and it can't notice that an article retrieved under
T08 (VEO / Regional Insecurity) is actually a better fit for T01
(Military Conflict). This pass reads each article's actual title and
description and makes that call.

-- One call PER TOPIC, not one call for everything --

This used to be a single batched call across every topic in one prompt.
On a normal-to-busy day (17 topics x up to 25 articles = up to 425
articles, each needing a classification object like
{"T02#5": {"topic": "T02", "cocom": "USEUCOM"}} in the output) that
easily needed 10,000+ output tokens against an 8192-token ceiling -- the
response got cut off mid-JSON, json.loads failed on the truncated text,
and the ENTIRE call was treated as failed. Every topic silently fell
back to raw, unvetted retrieval at once, with no error visible anywhere
except a single stderr line in a CI log that expires. This is exactly
what happened in production: 286 articles in one call, a page full of
unfiltered noise (NFL/ad-copy/crime-blotter homonym matches the prompt
was already instructed to discard), and nothing on the page or in the
repo said why.

One call per topic bounds each call's output to at most
MAX_ARTICLES_PER_TOPIC classifications (~1KB of JSON, nowhere near any
ceiling) and, just as importantly, isolates failures: if one topic's
call fails for any reason, only that topic falls back to raw retrieval
-- every other topic's filtering still runs. The topic list and COCOM
definitions are still included in full in every call, so cross-topic
reassignment still works exactly as before; that's a small, fixed
per-call overhead (roughly +700 input tokens/call), not a per-article
one -- see README "Cost" for the actual delta this made.

-- Hardening against truncation / bad output (added 2026-09-25) --

The per-topic split contained failures but didn't prevent them: on
09-23 (T14) and 09-25 (flagship-ru-ua, T14) calls hit max_tokens having
emitted only ~500 tokens of visible text, so a topic was still lost whole.
Now: (1) output is one "id|topic|COCOM" line per article instead of JSON,
so a cut-off response still yields every complete line before the cut and
there is no JSON syntax to break; (2) topics are sent in chunks of
MAX_ARTICLES_PER_CALL; (3) only articles still without a decision are
retried, up to MAX_ATTEMPTS; (4) a topic counts as failed only if NOTHING
was classified -- a partial result is used and the leftovers stay in place
(reported as "unclassified" and listed under "incomplete" in _errors.json);
(5) every attempt logs stop_reason, output_tokens and content-block types
so a repeat failure identifies its cause from the repo.

-- Failure visibility --

A topic whose call fails writes nothing to data/<date>/redteam/, so
build_brief.py's existing per-topic fallback (already reads raw
data/<date>/<id>.json whenever the redteam file for that id doesn't
exist) kicks in for exactly that topic, and ONLY that topic --
build_brief.py also renders a small "unvetted" note on any section it
had to fall back on, so this is visible on the page itself, not just in
a log. Every failure is also recorded in
data/<date>/redteam/_errors.json (topic, article count, the exception,
and the model's stop_reason when available -- "max_tokens" there means
truncation specifically, distinct from a parse or network error) so the
cause is diagnosable from the repo after the fact, not only from a CI
run's console output.

Writes data/<date>/redteam/<topic_id>.json per successfully-classified
topic (flagship ids and T01-T15), each article carrying a "cocom" field
and an "_original_topic_id" field when reassigned. Also writes
data/<date>/redteam/_report.json (kept/reassigned/discarded counts) and,
when anything failed, data/<date>/redteam/_errors.json.

synthesize.py and build_brief.py both prefer data/<date>/redteam/ over
raw data/<date>/ per topic, falling back automatically per topic
otherwise -- so if this step is skipped entirely (no ANTHROPIC_API_KEY)
or a specific topic's call fails, the rest of the pipeline still runs,
just without relevance filtering/COCOM tags for whatever didn't
succeed.

Requires ANTHROPIC_API_KEY (same secret synthesize.py uses) and PAID API
calls using Sonnet, not Haiku -- see README "Cost". Sonnet over Haiku
here on purpose: cross-topic reassignment and COCOM geography are a
harder reasoning task than writing a narrative from already-clean data
(synthesize.py's job), and getting classification wrong defeats the
point of this step. If cost matters more than accuracy for your use
case, changing MODEL below to Haiku is a one-line change -- nothing else
needs to.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from topics import TIER1, TOPICS

ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-sonnet-5"

# Caps how many articles per topic are sent to the classifier (most-recent
# first, since fetch_news.py sorts by publishedAt). Bounds prompt size/
# cost/latency per call and keeps each call's output comfortably inside
# MAX_TOKENS_PER_CALL -- generous relative to build_brief.py's 8-article
# preview, but not unlimited. Raise it if a topic's real daily volume
# regularly exceeds it; MAX_TOKENS_PER_CALL scales with this (see below),
# so raising one without the other reintroduces the truncation risk this
# file's docstring describes.
MAX_ARTICLES_PER_TOPIC = 25

# Articles per API call. A topic with more than this is split into chunks,
# so no single call's output can grow with topic size.
MAX_ARTICLES_PER_CALL = 15

# Output ceiling per call. The required output is now one short line per
# article ("3|T02|USEUCOM", ~10 tokens), so 15 articles need ~150 tokens.
# The ceiling is deliberately ~50x that: on 2026-09-23 and 2026-09-25 calls
# for 17-25 articles hit stop_reason "max_tokens" at 4096 tokens while
# having emitted only ~500 tokens' worth of visible text, i.e. most of the
# budget was going somewhere other than the answer (extended thinking is
# the likely culprit; _errors.json now records block types and token usage
# so the next failure says for certain). Generous headroom makes that
# harmless whatever the cause.
MAX_TOKENS_PER_CALL = 8192

# A topic's call is retried this many times in total (counting the first),
# each retry sending ONLY the articles still without a classification.
MAX_ATTEMPTS = 3

# Small pacing delay between each call -- cheap insurance against
# bursting the Anthropic API's rate limits, same spirit as the pauses in
# fetch_news.py/fetch_newsdata.py. Retries wait RETRY_PAUSE_SECONDS times
# the attempt number.
REQUEST_PAUSE_SECONDS = 0.5
RETRY_PAUSE_SECONDS = 3.0

# The SDK's own built-in retry (rate limits, 5xx, overloaded) on top of the
# per-article retry loop above. Default is 2.
SDK_MAX_RETRIES = 4

VALID_COCOMS = {
    "USEUCOM", "USCENTCOM", "USINDOPACOM", "USAFRICOM", "USSOUTHCOM",
    "USNORTHCOM", "Transregional",
}


def load_articles(data_dir, key):
    path = data_dir / f"{key}.json"
    if not path.exists():
        return []
    with open(path) as f:
        payload = json.load(f)
    return payload.get("articles", []) or []


def dedupe(articles):
    seen = set()
    out = []
    for a in articles:
        url = a.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(a)
    return out


def all_topic_defs():
    """(topic_id, label) for every flagship + taxonomy topic, in a stable
    order -- this is also the valid-assignment list given to the model,
    the same for every per-topic call so cross-topic reassignment still
    has the full taxonomy to reassign into."""
    return [(t["id"], t["label"]) for t in TIER1] + [(t["id"], t["label"]) for t in TOPICS]


def collect_articles(data_dir):
    """Returns {topic_id: [article, ...]} for every topic, deduped and
    capped to MAX_ARTICLES_PER_TOPIC. Skips the "-us-lens" pulls --
    those are a media-comparison sidebar, not part of the taxonomy being
    red-teamed."""
    by_topic = {}
    for topic_id, _label in all_topic_defs():
        arts = dedupe(load_articles(data_dir, topic_id))[:MAX_ARTICLES_PER_TOPIC]
        if arts:
            by_topic[topic_id] = arts
    return by_topic


_COCOM_LIST = (
    "- USEUCOM: Europe, including Russia and Ukraine\n"
    "- USCENTCOM: Middle East, North Africa (Egypt/Libya), Central Asia, Afghanistan/Pakistan\n"
    "- USINDOPACOM: East/South/Southeast Asia, South Asia (India), Pacific\n"
    "- USAFRICOM: Africa (sub-Saharan; North Africa is USCENTCOM)\n"
    "- USSOUTHCOM: Central America, South America, Caribbean\n"
    "- USNORTHCOM: United States, Canada, Mexico (homeland-defense scope)\n"
    '- Transregional: genuinely spans multiple AORs, or is global in scope '
    "(e.g. a worldwide cyber threat, a UN-level diplomatic story with no single regional focus)"
)


def build_prompt(topic_id, indexed_arts, topic_defs):
    """One topic's articles (or one chunk of them) per call -- see module
    docstring for why. indexed_arts is [(index, article), ...] where index
    is the article's position in the topic's full list, so a retried
    subset keeps the same ids. topic_defs (the full taxonomy) is still
    passed in full so the model can reassign an article to any other
    topic, not just topic_id."""
    topic_list = "\n".join(f"- {tid}: {label}" for tid, label in topic_defs)

    lines = [f"### Retrieved under {topic_id}"]
    for i, a in indexed_arts:
        title = (a.get("title") or "").strip()
        desc = (a.get("description") or "").strip()
        lines.append(f'{i} :: {title} :: {desc}')
    first_id = indexed_arts[0][0] if indexed_arts else 0

    return f"""You are red-teaming the retrieval output of a keyword-search-based military/geopolitical news taxonomy, before it gets summarized and published. The retrieval is boolean keyword search, not semantic -- it makes real mistakes: false positives (a word like "offensive" matching an NFL recap), and articles that are genuinely military/geopolitical news but landed under the wrong topic in the taxonomy.

Valid topics (a kept article must be assigned to exactly one of these):
{topic_list}

For EVERY article below, decide:
1. "topic": the topic id it actually belongs to -- this may be the topic it was retrieved under, a DIFFERENT id from the list above if it's a better fit, or null if it isn't genuinely relevant to ANY of these topics (e.g. sports, entertainment, unrelated local news, or metaphorical/homonym use of a keyword with no real military/security content).
2. "cocom": which combatant command's area of responsibility the story is contextually about. Only set this when "topic" is non-null. Valid values:
{_COCOM_LIST}

Articles (format: <id> :: title :: description):

{chr(10).join(lines)}

Be conservative about discarding: only set "topic" to null when the article clearly has no genuine military/security/geopolitical relevance, not merely because it's a slow day for that specific topic. When genuinely unsure between two adjacent topics, pick the closer one rather than discarding.

Answer with exactly one line per article and nothing else -- no reasoning, no commentary, no markdown, no JSON. Each line is the article's id, the topic id (or a single "-" to discard), and the COCOM, separated by "|":

<id>|<topic id or ->|<COCOM>

Examples (for illustration only):
{first_id}|{topic_id}|USEUCOM
{first_id + 1}|-
{first_id + 2}|T07|Transregional

A discarded article's line is just "<id>|-". Use each COCOM name exactly as written above.
"""


def parse_lines(text, valid_topics, wanted_ids, truncated=False):
    """Parses the one-line-per-article format into {index: {"topic", "cocom"}}.

    Line-oriented on purpose: unlike the JSON this replaced, a response cut
    off partway still yields every complete line before the cut, so a
    truncation costs one retry of the missing articles instead of the whole
    topic. When truncated is True the final line is dropped, since it may
    be cut mid-token ("12|T0" or "12|T02|USEU"). Lines that don't parse,
    name an unknown topic, or refer to an id that wasn't asked for are
    skipped -- those articles simply stay pending and get retried.

    valid_topics: {lowercase topic id: canonical topic id}."""
    cocoms = {c.lower(): c for c in VALID_COCOMS}
    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if truncated and raw_lines:
        raw_lines = raw_lines[:-1]

    out = {}
    for ln in raw_lines:
        ln = ln.strip("`").strip()
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        idx = int(parts[0])
        if idx not in wanted_ids:
            continue
        topic_tok = parts[1].strip("`\"' ")
        if topic_tok.lower() in ("-", "null", "none", ""):
            out[idx] = {"topic": None, "cocom": None}
            continue
        topic = valid_topics.get(topic_tok.lower())
        if topic is None:
            continue
        cocom = cocoms.get(parts[2].strip("`\"' ").lower()) if len(parts) > 2 else None
        out[idx] = {"topic": topic, "cocom": cocom}
    return out


def _response_diagnostics(response):
    """What we can tell about a response for _errors.json: how it stopped,
    how many tokens it used, and what kinds of content blocks it held.
    A response that stopped on max_tokens with only a little visible text
    and a "thinking" block is the signature of a token budget being spent
    on reasoning rather than the answer."""
    usage = getattr(response, "usage", None)
    return {
        "stop_reason": getattr(response, "stop_reason", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "block_types": [getattr(b, "type", "?") for b in (getattr(response, "content", None) or [])],
    }


def classify_topic(client, topic_id, arts, topic_defs):
    """Classifies one topic's articles, retrying only what's still missing.

    Returns (classifications, unresolved_indices, attempt_log):
      classifications: {"<topic_id>#<i>": {"topic": ..., "cocom": ...}}
      unresolved_indices: articles with no decision after MAX_ATTEMPTS
      attempt_log: one dict per API call (chunk, outcome, diagnostics) --
        written to _errors.json when anything was unresolved.
    A topic only counts as failed if NOTHING was classified; a partial
    result is used, with the leftovers kept in place and reported as
    unclassified (see apply_classifications)."""
    valid_topics = {tid.lower(): tid for tid, _ in topic_defs}
    results = {}
    log = []
    pending = list(range(len(arts)))

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if not pending:
            break
        if attempt > 1:
            time.sleep(RETRY_PAUSE_SECONDS * (attempt - 1))
        for start in range(0, len(pending), MAX_ARTICLES_PER_CALL):
            chunk = pending[start:start + MAX_ARTICLES_PER_CALL]
            prompt = build_prompt(topic_id, [(i, arts[i]) for i in chunk], topic_defs)
            entry = {"attempt": attempt, "requested": len(chunk)}
            response = None
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS_PER_CALL,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
                diag = _response_diagnostics(response)
                parsed = parse_lines(text, valid_topics, set(chunk),
                                     truncated=diag["stop_reason"] == "max_tokens")
                for i, decision in parsed.items():
                    results[f"{topic_id}#{i}"] = decision
                entry.update(diag)
                entry["parsed"] = len(parsed)
            except Exception as e:  # API/network error after the SDK's own retries
                entry["error"] = f"{type(e).__name__}: {e}"
                if response is not None:
                    entry.update(_response_diagnostics(response))
            log.append(entry)
            time.sleep(REQUEST_PAUSE_SECONDS)
        pending = [i for i in pending if f"{topic_id}#{i}" not in results]

    return results, pending, log


def apply_classifications(by_topic, classifications, successful_topics):
    """Returns (final_by_topic, report). final_by_topic: {topic_id: [article, ...]}
    with each article carrying "cocom" and, if reassigned, "_original_topic_id".

    A reassignment target that ISN'T in successful_topics (that topic's own
    call failed, or it was never sent -- e.g. a topic with zero retrieved
    articles) is redirected back to the article's original topic instead.
    Without this, a lone reassigned article could end up as the ONLY
    content in a failed topic's redteam output file, and build_brief.py
    would show just that one article instead of falling back to the
    topic's full raw retrieval -- silent data loss for that topic, worse
    than the failure it was trying to route around."""
    final_by_topic = {}
    report = {"topics": {}, "totals": {"kept": 0, "reassigned": 0, "discarded": 0, "unclassified": 0}}

    for topic_id, arts in by_topic.items():
        report["topics"][topic_id] = {"retrieved": len(arts), "kept_here": 0, "moved_out": 0,
                                      "discarded": 0, "unclassified": 0}

    for topic_id, arts in by_topic.items():
        if topic_id not in successful_topics:
            continue  # this topic's own call failed -- nothing to apply, falls back to raw entirely

        for i, article in enumerate(arts):
            article_id = f"{topic_id}#{i}"
            decision = classifications.get(article_id)

            if decision is None:
                # Model didn't return a decision for this one -- fail safe by
                # keeping it where it was rather than silently losing data.
                final_by_topic.setdefault(topic_id, []).append(article)
                report["topics"][topic_id]["unclassified"] += 1
                report["totals"]["unclassified"] += 1
                continue

            new_topic = decision.get("topic")
            cocom = decision.get("cocom")

            if new_topic is None:
                report["topics"][topic_id]["discarded"] += 1
                report["totals"]["discarded"] += 1
                continue

            if new_topic not in successful_topics:
                # Reassignment target's own call failed (or had nothing to
                # send) -- keep this article at its original, successfully-
                # processed topic instead of routing it into a topic whose
                # file won't otherwise exist.
                new_topic = topic_id

            if cocom not in VALID_COCOMS:
                cocom = None  # model returned something off-list; don't fabricate a tag

            out_article = dict(article)
            out_article["cocom"] = cocom
            if new_topic != topic_id:
                out_article["_original_topic_id"] = topic_id
                report["topics"][topic_id]["moved_out"] += 1
                report["totals"]["reassigned"] += 1
            else:
                report["topics"][topic_id]["kept_here"] += 1
                report["totals"]["kept"] += 1

            final_by_topic.setdefault(new_topic, []).append(out_article)

    return final_by_topic, report


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data_dir = ROOT / "data" / today
    out_dir = data_dir / "redteam"

    if not data_dir.exists():
        print(f"No data directory for {today} -- run fetch_news.py first.", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set -- skipping red team pass entirely; downstream "
              "scripts will fall back to raw (unvetted, untagged) retrieval for every topic.", file=sys.stderr)
        return

    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed -- skipping red team pass entirely.", file=sys.stderr)
        return

    topic_defs = all_topic_defs()
    by_topic = collect_articles(data_dir)
    if not by_topic:
        print("No articles retrieved today -- nothing to red-team.", file=sys.stderr)
        return

    client = anthropic.Anthropic(api_key=api_key, max_retries=SDK_MAX_RETRIES)

    all_classifications = {}
    successful_topics = set()
    failures = []       # topics where NOTHING could be classified
    incomplete = []     # topics that succeeded but left some articles unclassified

    total_articles = sum(len(v) for v in by_topic.values())
    print(f"Red-teaming {total_articles} articles across {len(by_topic)} topics with {MODEL}, "
          f"in chunks of {MAX_ARTICLES_PER_CALL}, up to {MAX_ATTEMPTS} attempts each...")

    for topic_id, arts in by_topic.items():
        print(f"  {topic_id}: {len(arts)} articles...")
        results, unresolved, log = classify_topic(client, topic_id, arts, topic_defs)
        all_classifications.update(results)

        if results:
            successful_topics.add(topic_id)
            if unresolved:
                print(f"    {len(unresolved)}/{len(arts)} left unclassified after "
                      f"{MAX_ATTEMPTS} attempts (kept in place).", file=sys.stderr)
                incomplete.append({"topic": topic_id, "articles": len(arts),
                                   "unclassified": len(unresolved), "attempts": log})
        else:
            print(f"    FAILED: no classifications after {MAX_ATTEMPTS} attempts "
                  f"-- see _errors.json", file=sys.stderr)
            failures.append({
                "topic": topic_id,
                "articles": len(arts),
                "error": "no classifications obtained after all attempts",
                "attempts": log,
            })

    final_by_topic, report = apply_classifications(by_topic, all_classifications, successful_topics)
    report["failed_topics"] = sorted(f["topic"] for f in failures)
    report["incomplete_topics"] = sorted(i["topic"] for i in incomplete)

    out_dir.mkdir(parents=True, exist_ok=True)
    for topic_id, arts in final_by_topic.items():
        with open(out_dir / f"{topic_id}.json", "w") as f:
            json.dump({"articles": arts}, f, indent=2)
    with open(out_dir / "_report.json", "w") as f:
        json.dump(report, f, indent=2)
    if failures or incomplete:
        with open(out_dir / "_errors.json", "w") as f:
            json.dump({"failures": failures, "incomplete": incomplete}, f, indent=2)

    t = report["totals"]
    print(
        f"\nRed team done: {t['kept']} kept in place, {t['reassigned']} reassigned, "
        f"{t['discarded']} discarded, {t['unclassified']} unclassified (kept as-is) "
        f"across {len(successful_topics)}/{len(by_topic)} topics. Wrote {out_dir}/"
    )
    if failures:
        print(
            f"{len(failures)} topic(s) failed and fell back to raw retrieval for that topic only: "
            f"{', '.join(f['topic'] for f in failures)}. See {out_dir}/_errors.json for details.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
