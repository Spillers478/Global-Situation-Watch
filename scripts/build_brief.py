"""
Reads today's article set and renders a static HTML briefing page to
docs/index.html (served via GitHub Pages, which is configured to publish
from /docs on the main branch -- see README).

Reads data/<date>/redteam/<topic_id>.json when redteam.py has produced
it (relevance-vetted, cross-topic-reassigned, COCOM-tagged articles),
falling back to the raw data/<date>/<topic_id>.json otherwise -- see
redteam.py and this file's load_articles(). If data/<date>/synthesis.json
also exists (written by synthesize.py), its BLUF overview is rendered at
the top of the page and each topic section gets a short AI-written
summary above its headlines. Either or both can be missing (e.g.
ANTHROPIC_API_KEY wasn't set) and this script still renders a full page,
just without those layers.

Retrieval-level noise control (tighter queries, title-only matching,
excluded domains) lives in topics.py / fetch_news.py -- see topics.py
"Noise-control tools". This script's job is layout and presentation:

- A jump-to-topic nav bar (the page can easily be 500+ articles across
  16 sections) using each topic's plain-language label, not its internal
  T01-style code -- those codes still exist as anchor ids in the HTML
  (view-source or the URL fragment shows them) for anyone maintaining
  topics.py, but a reader never sees them.
- Per-topic article lists capped to a preview count with the rest tucked
  behind a native <details> "show more" toggle.
- Each article renders both NewsAPI's `description` and, when it adds
  anything beyond that, its (free-tier-truncated) `content` field --
  see TRUSTED_SOURCES and _CONTENT_TRUNCATION_RE below for what that
  actually gives you and its limits.
- Articles are re-sorted (rank_articles) so recognized wire services and
  national broadcasters (Reuters, AP, BBC, Al Jazeera, etc. -- see
  TRUSTED_SOURCES) surface before blogs/aggregators covering the same
  story, without hiding anything.
- When redteam.py tagged an article with a combatant command (see that
  file's VALID_COCOMS) or reassigned it from a different topic, that
  shows as a small tag in the article's byline.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from html import escape

from topics import TIER1, TOPICS, TOPIC_SWEEP_ENABLED
from sources import SOURCE_ORIGIN

ROOT = Path(__file__).resolve().parent.parent

# Main topic sections show this many articles by default; the rest are
# tucked behind a native <details> "show more" toggle rather than either
# dumping the full list (unreadable on a noisy/high-volume topic) or
# silently dropping them (loses real signal on a quiet day when the
# reader does want to scan everything). Lowered from 12 now that each
# card can render a second paragraph (see render_article) -- keeps the
# above-the-fold view from getting too tall.
ARTICLE_PREVIEW_LIMIT = 8

# Source names (matched against NewsAPI's article.source.name field,
# verbatim) treated as major wire services / national broadcasters for
# ranking purposes -- see rank_articles(). This is a source-reputation
# heuristic, not a fact-check: it surfaces widely-recognized wire/
# broadcast reporting above blogs, aggregators, and single-topic sites
# for the same story, it doesn't verify any individual article. NewsAPI's
# own source attribution isn't always reliable either (an aggregator
# republishing wire copy sometimes gets credited as the source instead
# of the wire service itself), so treat the ordering as directional, not
# a guarantee. Edit this set freely -- it's the only place this list lives.
TRUSTED_SOURCES = {
    "Reuters", "Associated Press", "BBC News", "Al Jazeera English", "NPR",
    "The Guardian", "Agence France-Presse", "CBS News", "NBC News",
    "ABC News", "PBS", "CNN", "Bloomberg", "Financial Times",
    "The Washington Post", "The New York Times", "Wall Street Journal",
    "Deutsche Welle (DW)", "France 24", "CBC News", "Sky News", "Axios",
    "Politico",
}

# NewsAPI's `content` field on the free/Developer tier is truncated to
# roughly 200 characters with a "... [+1234 chars]" suffix marking how
# much more exists that this plan doesn't return. Strip that marker when
# displaying it -- it's not useful to a reader and the link to the full
# article is right there in the headline.
_CONTENT_TRUNCATION_RE = re.compile(r"\s*\[\+\d+ chars\]\s*$")


def rank_articles(articles):
    """Stable-sorts trusted wire/broadcast sources to the front of the
    list. Stable sort preserves the existing recency order (NewsAPI
    results are fetched with sortBy=publishedAt) within each tier, so
    this only changes trusted-vs-not ordering, not the within-tier order."""
    def source_name(a):
        return (a.get("source") or {}).get("name") or ""

    return sorted(articles, key=lambda a: 0 if source_name(a) in TRUSTED_SOURCES else 1)


def load_articles(data_dir, key):
    """Prefers the red-teamed article set for this topic (data_dir/redteam/
    <key>.json) when redteam.py has produced it, falling back to the raw
    retrieval (data_dir/<key>.json) otherwise -- see redteam.py."""
    redteam_path = data_dir / "redteam" / f"{key}.json"
    path = redteam_path if redteam_path.exists() else data_dir / f"{key}.json"
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
    source_name = (a.get("source") or {}).get("name") or "Unknown source"
    url = escape(a.get("url") or "#")
    published = escape(a.get("publishedAt") or "")
    desc = (a.get("description") or "").strip()
    content = _CONTENT_TRUNCATION_RE.sub("", (a.get("content") or "").strip()).strip()

    # NewsAPI's content field is often the same lead sentence as
    # description, just truncated differently -- only show it as a
    # second paragraph when it actually adds something new.
    extra_html = ""
    if content and content != desc and content[:60] not in desc:
        extra_html = f'<p class="item-extra">{escape(content)}</p>'

    trusted_html = (
        '<span class="trusted-tag">wire service</span>'
        if source_name in TRUSTED_SOURCES else ""
    )

    # Set by redteam.py when it ran: which combatant command's AOR the
    # article is contextually about, and -- if it moved the article here
    # from a different topic -- a small transparency note about that.
    # This is "what region is this story about," independent of who wrote
    # it -- see origin_html below for the separate "who wrote it" signal.
    cocom = a.get("cocom")
    cocom_html = f'<span class="cocom-tag">{escape(cocom)}</span>' if cocom else ""
    moved_from = a.get("_original_topic_id")
    moved_html = (
        f'<span class="moved-tag">reassigned from {escape(moved_from)}</span>' if moved_from else ""
    )

    # Hand-maintained metadata about the outlet itself (sources.py) --
    # where it's edited from, and separately, where its audience actually
    # sits when that's been looked up. Distinct from cocom_html above:
    # that tag is about the story, this one is about the outlet.
    origin = SOURCE_ORIGIN.get(source_name)
    origin_html = ""
    if origin:
        origin_label = origin["country"]
        if origin["cocom"] != "Global":
            origin_label += f" · {origin['cocom']}"
        origin_html = f'<span class="origin-tag" title="Where this outlet is edited from">{escape(origin_label)}</span>'
        if origin.get("audience"):
            origin_html += (
                f'<span class="audience-tag" title="Where this outlet\'s readership actually sits '
                f'(may differ from where it\'s published)">audience: {escape(origin["audience"])}</span>'
            )

    return f"""
    <article class="item">
      <a class="item-title" href="{url}" target="_blank" rel="noopener">{title}</a>
      <div class="item-meta">{escape(source_name)} {trusted_html}{origin_html}{cocom_html}{moved_html}&middot; {published}</div>
      <p class="item-desc">{escape(desc)}</p>
      {extra_html}
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
        arts = rank_articles(dedupe(load_articles(data_dir, flagship["id"])))
        sections.append(render_section(
            flagship["id"], flagship["label"], arts, badge="flagship",
            narrative=topic_narratives.get(flagship["id"]), preview_limit=ARTICLE_PREVIEW_LIMIT,
        ))
        nav_items.append((flagship["id"], flagship["label"], "flagship", len(arts)))
        us_lens = rank_articles(dedupe(load_articles(data_dir, f"{flagship['id']}-us-lens")))
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
            arts = rank_articles(dedupe(load_articles(data_dir, topic["id"])))
            badge = "tripwire" if topic["tier"] == "tripwire" else None
            sections.append(render_section(
                topic["id"], topic["label"], arts, badge=badge,
                narrative=topic_narratives.get(topic["id"]), preview_limit=ARTICLE_PREVIEW_LIMIT,
            ))
            nav_items.append((topic["id"], topic["label"], badge, len(arts)))

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
    --origin: #4ddb9e;
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
  .item-extra {{
    color: var(--muted);
    font-size: 0.85rem;
    margin: 0.45rem 0 0;
    padding-top: 0.45rem;
    border-top: 1px dashed var(--border);
    line-height: 1.45;
  }}
  .trusted-tag {{
    color: var(--accent);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    border: 1px solid rgba(77,159,255,0.35);
    border-radius: 3px;
    padding: 0.05rem 0.35rem;
    margin-right: 0.35rem;
  }}
  .cocom-tag {{
    color: var(--flagship);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    border: 1px solid rgba(255,181,69,0.35);
    border-radius: 3px;
    padding: 0.05rem 0.35rem;
    margin-right: 0.35rem;
  }}
  .moved-tag {{
    color: var(--muted);
    font-size: 0.65rem;
    font-style: italic;
    margin-right: 0.35rem;
  }}
  .origin-tag {{
    color: var(--origin);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    border: 1px solid rgba(77,219,158,0.35);
    border-radius: 3px;
    padding: 0.05rem 0.35rem;
    margin-right: 0.35rem;
    cursor: help;
  }}
  .audience-tag {{
    color: var(--muted);
    font-size: 0.65rem;
    font-style: italic;
    margin-right: 0.35rem;
    cursor: help;
  }}
  .legend {{
    max-width: 860px;
    margin: 0.9rem auto 0;
    padding: 0 1.5rem;
    font-size: 0.75rem;
    color: var(--muted);
    line-height: 1.6;
  }}
  .legend span {{ white-space: nowrap; margin-right: 0.9rem; }}
  .legend-example {{
    margin: 0.6rem 0 0;
    padding-top: 0.6rem;
    border-top: 1px dashed var(--border);
    line-height: 1.7;
  }}
  .legend-example em {{ color: var(--text); font-style: normal; font-weight: 600; }}
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
<div class="legend">
  <div>
    <span><span class="trusted-tag" style="margin-right:0.3rem">wire service</span>recognized wire/broadcast source</span>
    <span><span class="origin-tag" style="margin-right:0.3rem">country · AOR</span>where the outlet is edited from</span>
    <span><span class="cocom-tag" style="margin-right:0.3rem">AOR</span>what region the story is about</span>
    <span><span class="audience-tag" style="margin-right:0.3rem">audience: ...</span>where the outlet's readers actually are, when known</span>
  </div>
  <p class="legend-example"><em>Illustrative example, not a real data point:</em> a Turkish outlet (<span class="origin-tag">Turkey &middot; USEUCOM</span>) reporting on an
  Iran/Israel story (<span class="cocom-tag">USCENTCOM</span>) read mostly by an American audience
  (<span class="audience-tag">audience: ~70% US</span>) would show all three &mdash; three separate facts about one article, not one.</p>
</div>
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
