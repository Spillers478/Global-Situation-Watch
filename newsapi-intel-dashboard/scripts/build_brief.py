"""
Reads today's raw NewsAPI JSON from data/<date>/ and renders a static
HTML briefing page to docs/index.html (served via GitHub Pages, which is
configured to publish from /docs on the main branch -- see README).

Keyword-retrieval only in this version -- no LLM relevance filtering or
narrative synthesis is applied yet. Expect some noise (e.g. "strike"
pulling in labor-action articles alongside airstrikes). See README
"Known limitations" for the planned next iteration.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from html import escape

from topics import TIER1, TOPICS, TOPIC_SWEEP_ENABLED

ROOT = Path(__file__).resolve().parent.parent


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


def render_article(a):
    title = escape(a.get("title") or "(untitled)")
    source = escape((a.get("source") or {}).get("name") or "Unknown source")
    url = escape(a.get("url") or "#")
    published = escape(a.get("publishedAt") or "")
    desc = escape(a.get("description") or "")
    return f"""
    <article class="item">
      <a class="item-title" href="{url}" target="_blank" rel="noopener">{title}</a>
      <div class="item-meta">{source} &middot; {published}</div>
      <p class="item-desc">{desc}</p>
    </article>"""


def render_section(section_id, label, articles, badge=None, limit=None):
    shown = articles[:limit] if limit else articles
    if not shown:
        body = '<p class="empty">No matching articles in this window.</p>'
    else:
        body = "\n".join(render_article(a) for a in shown)
    badge_html = f'<span class="badge badge-{badge}">{escape(badge)}</span>' if badge else ""
    return f"""
  <section class="topic" id="{escape(section_id)}">
    <h2>{escape(label)} {badge_html}<span class="count">{len(articles)}</span></h2>
    {body}
  </section>"""


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data_dir = ROOT / "data" / today
    if not data_dir.exists():
        print(f"No data directory for {today} -- run fetch_news.py first.", file=sys.stderr)
        sys.exit(1)

    sections = []

    # Tier 1 flagships get top billing
    for flagship in TIER1:
        arts = dedupe(load_articles(data_dir, flagship["id"]))
        sections.append(render_section(flagship["id"], flagship["label"], arts, badge="flagship"))
        us_lens = dedupe(load_articles(data_dir, f"{flagship['id']}-us-lens"))
        if us_lens:
            sections.append(render_section(
                f"{flagship['id']}-us-lens",
                f"{flagship['label']} — US Media Lens",
                us_lens,
                limit=8,
            ))

    # Full 14-topic taxonomy sweep -- off by default in v1 (see topics.py)
    if TOPIC_SWEEP_ENABLED:
        ordered = sorted(TOPICS, key=lambda t: (t["tier"] != "tripwire", t["id"]))
        for topic in ordered:
            arts = dedupe(load_articles(data_dir, topic["id"]))
            badge = "tripwire" if topic["tier"] == "tripwire" else None
            label = f"{topic['id']} — {topic['label']}"
            sections.append(render_section(topic["id"], label, arts, badge=badge))

    html = TEMPLATE.format(date=today, sections="\n".join(sections))

    docs_dir = ROOT / "docs"
    docs_dir.mkdir(exist_ok=True)
    with open(docs_dir / "index.html", "w") as f:
        f.write(html)

    print(f"Wrote {docs_dir / 'index.html'} for {today}")


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Global Situation Watch — {date}</title>
<style>
  :root {{
    --bg: #0b0e14;
    --panel: #131722;
    --border: #232838;
    --text: #dbe1ee;
    --muted: #7c8797;
    --accent: #4d9fff;
    --tripwire: #ff5a5a;
    --flagship: #ffb545;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    margin: 0;
    padding: 0 0 4rem;
  }}
  header {{
    padding: 2.5rem 1.5rem 1.5rem;
    border-bottom: 1px solid var(--border);
    max-width: 860px;
    margin: 0 auto;
  }}
  header h1 {{
    margin: 0 0 0.25rem;
    font-size: 1.6rem;
    letter-spacing: 0.02em;
  }}
  header p {{
    color: var(--muted);
    margin: 0;
    font-size: 0.9rem;
  }}
  main {{
    max-width: 860px;
    margin: 0 auto;
    padding: 0 1.5rem;
  }}
  section.topic {{
    border-top: 1px solid var(--border);
    padding: 1.75rem 0;
  }}
  section.topic h2 {{
    font-size: 1.05rem;
    margin: 0 0 1rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }}
  .count {{
    margin-left: auto;
    color: var(--muted);
    font-weight: 400;
    font-size: 0.85rem;
  }}
  .badge {{
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    font-weight: 600;
  }}
  .badge-tripwire {{ background: rgba(255,90,90,0.15); color: var(--tripwire); }}
  .badge-flagship {{ background: rgba(255,181,69,0.15); color: var(--flagship); }}
  .item {{ margin-bottom: 1.1rem; }}
  .item-title {{
    color: var(--text);
    text-decoration: none;
    font-weight: 600;
    font-size: 0.95rem;
  }}
  .item-title:hover {{ color: var(--accent); }}
  .item-meta {{
    color: var(--muted);
    font-size: 0.75rem;
    margin: 0.2rem 0 0.35rem;
  }}
  .item-desc {{
    color: var(--muted);
    font-size: 0.85rem;
    margin: 0;
    line-height: 1.4;
  }}
  .empty {{ color: var(--muted); font-size: 0.85rem; font-style: italic; }}
</style>
</head>
<body>
<header>
  <h1>Global Situation Watch</h1>
  <p>{date} &middot; keyword-retrieval build &middot; NewsAPI Developer tier &middot; refreshed daily via GitHub Actions</p>
</header>
<main>
{sections}
</main>
</body>
</html>
"""

if __name__ == "__main__":
    main()
