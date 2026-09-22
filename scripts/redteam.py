"""
Reads today's raw NewsAPI JSON from data/<date>/ (every TIER1 flagship and
TOPICS taxonomy pull -- see topics.py) and runs one batched Claude call
that, for every retrieved article, decides:

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

Writes the result to data/<date>/redteam/<topic_id>.json -- one file per
topic (flagship ids and T01-T14), containing only the articles that
survived review and were assigned there (whether originally retrieved
there or reassigned from elsewhere), each with a "cocom" field added and
an "_original_topic_id" field when that differs from where it ended up.
Also writes data/<date>/redteam/_report.json, a plain audit summary
(kept/reassigned/discarded counts per topic) so you can sanity-check what
the model actually did without digging through every file.

synthesize.py and build_brief.py both prefer data/<date>/redteam/ over
the raw data/<date>/ files when it exists, and fall back to raw data
automatically otherwise -- so if this step is skipped, fails, or you
haven't set ANTHROPIC_API_KEY, the rest of the pipeline runs exactly as
it did before this existed (just without relevance filtering or COCOM
tags).

Requires ANTHROPIC_API_KEY (same secret synthesize.py uses) and a PAID
API call using Sonnet, not Haiku -- see README "Cost". Sonnet over Haiku
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
from datetime import datetime, timezone
from pathlib import Path

from topics import TIER1, TOPICS

ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-sonnet-5"

# Caps how many articles per topic are sent to the classifier (most-recent
# first, since fetch_news.py sorts by publishedAt). Bounds prompt size/
# cost/latency and keeps the single batched call reliable -- generous
# relative to build_brief.py's 8-article preview, but not unlimited.
# Raise it if a topic's real daily volume regularly exceeds it.
MAX_ARTICLES_PER_TOPIC = 25

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
    order -- this is also the valid-assignment list given to the model."""
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


def build_prompt(by_topic, topic_defs):
    topic_list = "\n".join(f"- {tid}: {label}" for tid, label in topic_defs)
    cocom_list = (
        "- USEUCOM: Europe, including Russia and Ukraine\n"
        "- USCENTCOM: Middle East, North Africa (Egypt/Libya), Central Asia, Afghanistan/Pakistan\n"
        "- USINDOPACOM: East/South/Southeast Asia, South Asia (India), Pacific\n"
        "- USAFRICOM: Africa (sub-Saharan; North Africa is USCENTCOM)\n"
        "- USSOUTHCOM: Central America, South America, Caribbean\n"
        "- USNORTHCOM: United States, Canada, Mexico (homeland-defense scope)\n"
        '- Transregional: genuinely spans multiple AORs, or is global in scope '
        "(e.g. a worldwide cyber threat, a UN-level diplomatic story with no single regional focus)"
    )

    blocks = []
    for topic_id, arts in by_topic.items():
        lines = [f"### Retrieved under {topic_id}"]
        for i, a in enumerate(arts):
            title = (a.get("title") or "").strip()
            desc = (a.get("description") or "").strip()
            lines.append(f'{topic_id}#{i} :: {title} :: {desc}')
        blocks.append("\n".join(lines))

    return f"""You are red-teaming the retrieval output of a keyword-search-based military/geopolitical news taxonomy, before it gets summarized and published. The retrieval is boolean keyword search, not semantic -- it makes real mistakes: false positives (a word like "offensive" matching an NFL recap), and articles that are genuinely military/geopolitical news but landed under the wrong topic in the taxonomy.

Valid topics (a kept article must be assigned to exactly one of these):
{topic_list}

For EVERY article below, decide:
1. "topic": the topic id it actually belongs to -- this may be the topic it was retrieved under, a DIFFERENT id from the list above if it's a better fit, or null if it isn't genuinely relevant to ANY of these topics (e.g. sports, entertainment, unrelated local news, or metaphorical/homonym use of a keyword with no real military/security content).
2. "cocom": which combatant command's area of responsibility the story is contextually about. Only set this when "topic" is non-null. Valid values:
{cocom_list}

Articles (format: <id> :: title :: description):

{chr(10).join(blocks)}

Be conservative about discarding: only set "topic" to null when the article clearly has no genuine military/security/geopolitical relevance, not merely because it's a slow day for that specific topic. When genuinely unsure between two adjacent topics, pick the closer one rather than discarding.

Return ONLY a JSON object, no markdown fences, no commentary, in exactly this shape (one entry per article id, using the ids as given, e.g. "T01#0"):
{{"classifications": {{"T01#0": {{"topic": "T01", "cocom": "USEUCOM"}}, "T08#3": {{"topic": null, "cocom": null}}}}}}
"""


def parse_json_response(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def apply_classifications(by_topic, classifications):
    """Returns (final_by_topic, report). final_by_topic: {topic_id: [article, ...]}
    with each article carrying "cocom" and, if reassigned, "_original_topic_id".
    report: per-topic before/after counts plus overall kept/reassigned/discarded
    totals, for _report.json."""
    final_by_topic = {}
    report = {"topics": {}, "totals": {"kept": 0, "reassigned": 0, "discarded": 0, "unclassified": 0}}

    for topic_id, arts in by_topic.items():
        report["topics"][topic_id] = {"retrieved": len(arts), "kept_here": 0, "moved_out": 0, "discarded": 0}

    for topic_id, arts in by_topic.items():
        for i, article in enumerate(arts):
            article_id = f"{topic_id}#{i}"
            decision = classifications.get(article_id)

            if decision is None:
                # Model didn't return a decision for this one -- fail safe by
                # keeping it where it was rather than silently losing data.
                final_by_topic.setdefault(topic_id, []).append(article)
                report["totals"]["unclassified"] += 1
                continue

            new_topic = decision.get("topic")
            cocom = decision.get("cocom")

            if new_topic is None:
                report["topics"][topic_id]["discarded"] += 1
                report["totals"]["discarded"] += 1
                continue

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
        print("ANTHROPIC_API_KEY not set -- skipping red team pass; downstream "
              "scripts will fall back to raw (unvetted, untagged) retrieval.", file=sys.stderr)
        return

    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed -- skipping red team pass.", file=sys.stderr)
        return

    topic_defs = all_topic_defs()
    by_topic = collect_articles(data_dir)
    if not by_topic:
        print("No articles retrieved today -- nothing to red-team.", file=sys.stderr)
        return

    prompt = build_prompt(by_topic, topic_defs)
    total_articles = sum(len(v) for v in by_topic.values())
    print(f"Red-teaming {total_articles} articles across {len(by_topic)} topics with {MODEL}...")

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        result = parse_json_response(text)
        classifications = result.get("classifications") or {}
    except Exception as e:
        print(f"Red team pass failed: {e} -- downstream scripts will fall back to raw retrieval.", file=sys.stderr)
        return

    final_by_topic, report = apply_classifications(by_topic, classifications)

    out_dir.mkdir(parents=True, exist_ok=True)
    for topic_id, arts in final_by_topic.items():
        with open(out_dir / f"{topic_id}.json", "w") as f:
            json.dump({"articles": arts}, f, indent=2)
    with open(out_dir / "_report.json", "w") as f:
        json.dump(report, f, indent=2)

    t = report["totals"]
    print(
        f"Red team done: {t['kept']} kept in place, {t['reassigned']} reassigned, "
        f"{t['discarded']} discarded, {t['unclassified']} unclassified (kept as-is). "
        f"Wrote {out_dir}/"
    )


if __name__ == "__main__":
    main()
