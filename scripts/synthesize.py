"""
Reads today's raw NewsAPI JSON from data/<date>/ and generates a BLUF
(Bottom Line Up Front) overview plus a short narrative summary per topic,
using Claude Haiku. Writes the result to data/<date>/synthesis.json.

Requires ANTHROPIC_API_KEY in the environment -- separate from
NEWSAPI_KEY, and a PAID API (see README "Cost"). If this step fails or
the key is missing, it writes an empty synthesis result rather than
raising, so build_brief.py falls back to headline-only display instead
of breaking the whole pipeline.

Design note: this makes ONE batched API call covering every topic (BLUF
+ all per-topic summaries together), rather than one call per topic --
keeps cost and latency down, and lets the BLUF be informed by everything
else in a single pass. See README for the cost estimate at this volume.

Honesty note: this still summarizes keyword-retrieved articles, which
can include false positives (see topics.py). The prompt instructs the
model to say so plainly when a topic's articles look unrelated or too
sparse to summarize, rather than inventing a coherent narrative -- this
doesn't fix the underlying retrieval noise, it just avoids compounding
it with a confident-sounding fabricated summary.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from topics import TIER1, TOPICS

ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-haiku-4-5-20251001"
MAX_ARTICLES_PER_TOPIC = 12  # caps prompt size; NewsAPI results are already sorted by publishedAt


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


def build_topic_block(topic_id, label, articles, extra=""):
    lines = [f"### {topic_id} — {label}{extra}"]
    if not articles:
        lines.append("(no articles retrieved for this topic today)")
        return "\n".join(lines)
    for a in articles[:MAX_ARTICLES_PER_TOPIC]:
        title = (a.get("title") or "").strip()
        desc = (a.get("description") or "").strip()
        lines.append(f"- {title} :: {desc}")
    return "\n".join(lines)


def build_prompt(data_dir):
    blocks = []

    for flagship in TIER1:
        arts = dedupe(load_articles(data_dir, flagship["id"]))
        blocks.append(build_topic_block(flagship["id"], flagship["label"], arts, extra=" [FLAGSHIP]"))

    for topic in TOPICS:
        arts = dedupe(load_articles(data_dir, topic["id"]))
        extra = " [TRIPWIRE]" if topic["tier"] == "tripwire" else ""
        blocks.append(build_topic_block(topic["id"], topic["label"], arts, extra=extra))

    topic_ids = [t["id"] for t in TIER1] + [t["id"] for t in TOPICS]

    return f"""You are drafting the written portion of a daily open-source geopolitical/military awareness brief, built from keyword-searched news headlines (not a curated intelligence feed -- treat the input as noisy raw search results, not verified fact).

Below is today's retrieved data, grouped by topic. FLAGSHIP topics are the two priority geographic flashpoints. TRIPWIRE topics are the highest-severity categories (nuclear, coup, bio/chem, hostage, cyber, embassy threats).

{chr(10).join(blocks)}

Write two things and return them as JSON, nothing else (no markdown fences, no commentary before or after):

1. "bluf": A Bottom-Line-Up-Front paragraph (4-6 sentences). Lead with the single most significant development across all topics in the first sentence. Prioritize the FLAGSHIP topics and any TRIPWIRE topic with real activity. Be factual and grounded only in what's in the headlines/descriptions above -- do not speculate or add outside knowledge.

2. "topics": an object mapping each topic id to a 2-3 sentence factual summary of what that topic's headlines describe. If a topic's articles look unrelated to the topic itself (keyword noise -- e.g. a labor "strike" article under a military topic) or too sparse/generic to summarize meaningfully, say so plainly in one sentence rather than inventing a coherent narrative. Cover every one of these topic ids: {", ".join(topic_ids)}

Return exactly this JSON shape and nothing else:
{{"bluf": "...", "topics": {{"flagship-ru-ua": "...", "...": "..."}}}}
"""


def parse_json_response(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data_dir = ROOT / "data" / today
    out_path = data_dir / "synthesis.json"

    if not data_dir.exists():
        print(f"No data directory for {today} -- run fetch_news.py first.", file=sys.stderr)
        sys.exit(1)

    empty_result = {"bluf": None, "topics": {}}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set -- skipping synthesis, page will show headlines only.", file=sys.stderr)
        with open(out_path, "w") as f:
            json.dump(empty_result, f)
        return

    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed -- skipping synthesis.", file=sys.stderr)
        with open(out_path, "w") as f:
            json.dump(empty_result, f)
        return

    client = anthropic.Anthropic(api_key=api_key)
    prompt = build_prompt(data_dir)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        result = parse_json_response(text)
    except Exception as e:
        print(f"Synthesis failed: {e}", file=sys.stderr)
        result = empty_result

    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
