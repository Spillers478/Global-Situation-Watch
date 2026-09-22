"""
Reads today's raw NewsAPI JSON from data/<date>/ and renders a static
HTML briefing page to docs/index.html (served via GitHub Pages, which is
configured to publish from /docs on the main branch -- see README).

If data/<date>/synthesis.json exists (written by synthesize.py), its BLUF
overview is rendered at the top of the page and each topic section gets a
short AI-written summary above its headlines. If that file is missing or
empty (e.g. ANTHROPIC_API_KEY wasn't set), the page falls back to
headline-only display -- this script never depends on synthesis having
run.

Retrieval-level noise control (tighter queries, title-only matching,
excluded domains) lives in topics.py / fetch_news.py -- see topics.py
"Noise-control tools". This script's job is layout only: a jump-to-topic
nav bar (the page can easily be 500+ articles across 16 sections), and
per-topic article lists capped to a preview count with the rest tucked
behind a native <details> "show more" toggle, so the page reads as a
dashboard you scan rather than a feed you scroll through top to bottom.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from html import escape

from topics import TIER1, TOPICS, TOPIC_SWEEP_ENABLED

ROOT = Path(__file__).resolve().parent.parent

# Main topic sections show this many articles by default; the rest are
# tucked behind a native <details> "show more" toggle rather than either
# dumping the full list (unreadable on a noisy/high-volume topic) or
# silently dropping them (loses real signal on a quiet day when the
# reader does want to scan everything).
ARTICLE_PREVIEW_LIMIT = 12


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


def render_section(section_id, label, articles, badge=None, limit=None, narrative=None, preview_limit=None):
    shown_all = articles[:limit] if limit else articles

    if not shown_all:
        body = '<p class="empty">No matching articles in this window.</p>'
    elif preview_limit and len(shown_all) > preview_limit:
        visible, rest = shown_all[:preview_limit], shown_all[preview_limit:]
        visible_html = "\n".join(render_article(a) for a in visible)
        rest_html = "\n".join(render_article(a) for a in rest)
        body = f"""{visible_html}
    <details class="more">
      <summary>Show {len(rest)} more article{"s" if len(rest) != 1 else ""}</summary>
      {rest_html}
    </details>"""
    else:
        body = "\n".join(render_article(a) for a in shown_all)

    badge_html = f'<span class="badge badge-{badge}">{escape(badge)}</span>' if badge else ""
    narrative_html = f'<p class="narrative">{escape(narrative)}</p>' if narrative else ""
    return f"""
  <section class="topic" id="{escape(section_id)}">
    <h2>{escape(label)} {badge_html}<span class="count">{len(articles)}</span></h2>
    {narrative_html}
    {body}
  </section>"""


def render_nav(nav_items):
    """A jump-to-topic bar so a 16-section page is a dashboard you scan,
    not a wall you scroll. nav_items: list of (anchor_id, label, badge, count)."""
    links = []
    for anchor_id, label, badge, count in nav_items:
        badge_class = f" nav-{badge}" if badge else ""
        links.append(
            f'<a class="nav-link{badge_class}" href="#{escape(anchor_id)}">'
            f'{escape(label)} <span class="nav-count">{count}</span></a>'
        )
    return f'<nav class="topic-nav">{"".join(links)}</nav>'


def load_synthesis(data_dir):
    path = data_dir / "synthesis.json"
    if not path.exists():
        return {"bluf": None, "topics": {}}
    with open(path) as f:
        return json.load(f)


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data_dir = ROOT / "data" / today
    if not data_dir.exists():
        print(f"No data directory for {today} -- run fetch_news.py first.", file=sys.stderr)
        sys.exit(1)

    synthesis = load_synthesis(data_dir)
    topic_narratives = synthesis.get("topics") or {}
    bluf = synthesis.get("bluf")

    sections = []
    nav_items = []  # (anchor_id, label, badge, count) -- drives the jump-to-topic bar

    # Tier 1 flagships get top billing
    for flagship in TIER1:
        arts = dedupe(load_articles(data_dir, flagship["id"]))
        sections.append(render_section(
            flagship["id"], flagship["label"], arts, badge="flagship",
            narrative=topic_narratives.get(flagship["id"]), preview_limit=ARTICLE_PREVIEW_LIMIT,
        ))
        nav_items.append((flagship["id"], flagship["label"], "flagship", len(arts)))
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
            sections.append(render_section(
                topic["id"], label, arts, badge=badge,
                narrative=topic_narratives.get(topic["id"]), preview_limit=ARTICLE_PREVIEW_LIMIT,
            ))
            nav_items.append((topic["id"], topic["id"], badge, len(arts)))

    nav_html = render_nav(nav_items)

    bluf_html = ""
    if bluf:
        bluf_html = f"""
  <section class="bluf">
    <h2>BLUF</h2>
    <p>{escape(bluf)}</p>
  </section>"""

    html = TEMPLATE.format(date=today, nav=nav_html, bluf=bluf_html, sections="\n".join(sections))

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
  section.bluf {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: 6px;
    padding: 1.25rem 1.5rem;
    margin: 1.75rem 0;
  }}
  section.bluf h2 {{
    margin: 0 0 0.6rem;
    font-size: 0.75rem;
    letter-spacing: 0.08em;
    color: var(--accent);
    text-transform: uppercase;
  }}
  section.bluf p {{
    margin: 0;
    font-size: 0.95rem;
    line-height: 1.55;
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
  .narrative {{
    font-size: 0.9rem;
    line-height: 1.5;
    color: var(--text);
    background: var(--panel);
    border-left: 2px solid var(--accent);
    padding: 0.6rem 0.9rem;
    margin: 0 0 1.1rem;
    border-radius: 0 4px 4px 0;
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
  .topic-nav {{
    position: sticky;
    top: 0;
    z-index: 10;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    padding: 0.6rem 1.5rem;
    max-width: 860px;
    margin: 0 auto;
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }}
  .nav-link {{
    color: var(--muted);
    text-decoration: none;
    font-size: 0.75rem;
    padding: 0.25rem 0.55rem;
    border: 1px solid var(--border);
    border-radius: 12px;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
  }}
  .nav-link:hover {{ color: var(--text); border-color: var(--accent); }}
  .nav-link.nav-tripwire {{ border-color: rgba(255,90,90,0.35); color: var(--tripwire); }}
  .nav-link.nav-flagship {{ border-color: rgba(255,181,69,0.35); color: var(--flagship); }}
  .nav-count {{ color: var(--muted); font-size: 0.7rem; }}
  details.more {{ margin-top: 0.5rem; }}
  details.more summary {{
    cursor: pointer;
    color: var(--accent);
    font-size: 0.85rem;
    padding: 0.4rem 0;
    list-style: none;
  }}
  details.more summary::-webkit-details-marker {{ display: none; }}
  details.more summary:before {{ content: "+ "; }}
  details.more[open] summary:before {{ content: "− "; }}
  details.more[open] summary {{ margin-bottom: 0.5rem; }}
</style>
</head>
<body>
<header>
  <h1>Global Situation Watch</h1>
  <p>{date} &middot; keyword-retrieval build &middot; NewsAPI Developer tier &middot; refreshed daily via GitHub Actions</p>
</header>
{nav}
<main>
{bluf}
{sections}
</main>
</body>
</html>
"""

if __name__ == "__main__":
    main()
