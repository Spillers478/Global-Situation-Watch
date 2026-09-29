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
summary above its headlines. If data/<date>/dedup/<topic_id>.json also
exists (written by dedupe_stories.py), same-event articles are collapsed
into one story with a "+N other sources" toggle, and any flagged
contradiction (disagreement on figures, attribution, or outcome only --
see dedupe_stories.py) renders as a bordered callout under that story.
Any or all of these three layers can be missing (e.g. ANTHROPIC_API_KEY
wasn't set, or dedupe_stories.py hasn't been added to the workflow yet)
and this script still renders a full page, just without them.

Story threads, ranking and change detection (threads.py, changes.py) are
computed here, in-process and deterministically, from the same article
files -- no extra pipeline step or API call is required. Sections list
STORIES ranked by importance rather than raw articles in feed order; a story
reported under several topics renders in full once (at its "home" topic) and
as a one-line cross-reference elsewhere; the top of the page gets a
"Top developments" block and a "Changes since <prior day>" block. If that
computation ever fails, build_page() logs a warning and falls back to the
previous flat, per-topic layout (render_section) so the daily publish never
breaks on a ranking bug.

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
  story, without hiding anything. This ordering is silent -- no "wire
  service" badge is shown; it just changes which article a reader sees
  first within a topic.
- redteam.py's COCOM tag, and the source-origin/audience metadata in
  sources.py, are still computed and still drive filtering and cross-
  topic reassignment (see redteam.py) -- they're just not surfaced as
  visible badges on the page. Readers said the topic itself and the
  underlying story matter more than that categorization; when a redteam
  pass reassigns an article from a different topic, that's still noted
  in the byline (moved_html below), since it's directly relevant to
  why the article appears where it does.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from html import escape

import changes as C
import source_tiers
import threads as T
from search_widget import PAGE_SEARCH_CSS, render_page_search_widget
from topics import TIER1, TOPICS, TOPIC_SWEEP_ENABLED

ROOT = Path(__file__).resolve().parent.parent

# Maps internal topic ids (T03, flagship-ru-ua, ...) to their reader-facing
# label -- used only by render_article()'s "reassigned from" byline note, so
# a moved article names the topic in plain language instead of leaking the
# internal code to a reader (see that function for why this matters).
TOPIC_LABELS = {t["id"]: t["label"] for t in TIER1 + TOPICS}

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


def _redteam_vetted_to_empty(data_dir, key):
    """redteam.py writes no <topic>.json when every retrieved article was
    discarded or moved to another topic, so "file missing" alone can't tell
    "red team ran and kept nothing here" from "red team never ran / this
    topic's call failed". _report.json can: a topic listed there and not in
    failed_topics was vetted successfully. Without this check, a fully
    filtered topic fell back to its raw, unvetted retrieval."""
    report_path = data_dir / "redteam" / "_report.json"
    if not report_path.exists():
        return False
    try:
        with open(report_path) as f:
            report = json.load(f)
    except (OSError, ValueError):
        return False
    return key in (report.get("topics") or {}) and key not in (report.get("failed_topics") or [])


def load_articles(data_dir, key):
    """Prefers the red-teamed article set for this topic (data_dir/redteam/
    <key>.json) when redteam.py has produced it, falling back to the raw
    retrieval (data_dir/<key>.json) otherwise -- see redteam.py."""
    redteam_path = data_dir / "redteam" / f"{key}.json"
    if not redteam_path.exists() and _redteam_vetted_to_empty(data_dir, key):
        return []
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


def load_dedup(data_dir, topic_id):
    """Loads data/<date>/dedup/<topic_id>.json (written by
    dedupe_stories.py), if present. Missing file -- that pipeline stage
    hasn't been added to the workflow yet, ANTHROPIC_API_KEY wasn't set,
    or this topic's call failed -- returns None, and callers must treat
    None as "no grouping data available" (render the flat, ungrouped
    list) rather than "zero duplicates found" (a real, different answer
    this file can also contain, as an empty "groups" mapping)."""
    path = data_dir / "dedup" / f"{topic_id}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def cluster_articles(articles, dedup):
    """Groups articles into (primary, others) pairs using dedup's url ->
    group-letter map, one pair per distinct story. Cluster order follows
    the first appearance of each group within `articles`, so the existing
    rank_articles ordering (trusted wire services first) still decides
    which source leads each story. An article missing from the map --
    normally shouldn't happen, since dedupe_stories.py maps every
    url-bearing article it saw, but a topic can gain new articles between
    that run and a later solo re-run of this script -- gets its own
    private singleton group rather than being dropped or mis-clustered.
    With dedup=None (no grouping data for this topic at all) every
    article is its own singleton, in original order -- callers see the
    same flat list as before this feature existed."""
    if not dedup:
        return [(a, []) for a in articles]

    groups_map = dedup.get("groups") or {}
    order = []
    by_letter = {}
    for a in articles:
        letter = groups_map.get(a.get("url")) or f"_ungrouped_{id(a)}"
        if letter not in by_letter:
            by_letter[letter] = []
            order.append(letter)
        by_letter[letter].append(a)

    return [(members[0], members[1:]) for letter in order for members in [by_letter[letter]]]


def render_contradiction_box(entries):
    """entries: contradiction dicts sharing one story's group letter (see
    dedupe_stories.py's output shape). Usually 0 or 1 given how narrowly
    contradictions are scoped there, but nothing here assumes exactly one."""
    if not entries:
        return ""
    claim_labels = {"figures": "Figures", "attribution": "Attribution", "outcome": "Outcome"}
    boxes = []
    for c in entries:
        claim_label = claim_labels.get(c.get("claim_type"), "Disagreement")
        rows = "\n".join(
            f'<li><a href="{escape(s.get("url") or "#")}" target="_blank" rel="noopener">'
            f'{escape(s.get("source") or "Unknown source")}</a>: {escape(s.get("says") or "")}</li>'
            for s in c.get("sources", [])
        )
        boxes.append(f"""
      <div class="contradiction-box">
        <div class="contradiction-label">Sources disagree &middot; {escape(claim_label)}</div>
        <p class="contradiction-summary">{escape(c.get("summary") or "")}</p>
        <ul class="contradiction-sources">{rows}</ul>
      </div>""")
    return "\n".join(boxes)


def render_cluster(primary, others, contradictions_by_group, group_letter):
    """One story: the lead article, an expandable list of any other sources
    reporting the same event, and any flagged contradiction between them."""
    primary_html = render_article(primary)

    others_html = ""
    if others:
        others_body = "\n".join(render_article(a) for a in others)
        others_html = f"""
    <details class="other-sources">
      <summary>+{len(others)} other source{"s" if len(others) != 1 else ""} on this story</summary>
      {others_body}
    </details>"""

    contradiction_html = render_contradiction_box(contradictions_by_group.get(group_letter, []))

    return f"""
    <div class="story-cluster">
      {primary_html}
      {others_html}
      {contradiction_html}
    </div>"""


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

    # redteam.py may have moved this article here from a different topic --
    # that's still worth a small transparency note in the byline, since it
    # directly explains why the article shows up under this topic. The
    # COCOM tag, wire-service tag, and source-origin/audience metadata are
    # still computed upstream (they drive filtering and ranking) but aren't
    # surfaced as reader-facing badges -- see the module docstring. Shown in
    # plain language (TOPIC_LABELS), never the internal T03/flagship-ru-ua
    # style id -- a reader is never meant to see those, same as everywhere
    # else on the page.
    moved_from = a.get("_original_topic_id")
    moved_label = TOPIC_LABELS.get(moved_from, moved_from)
    moved_html = (
        f'<span class="moved-tag">reassigned from {escape(moved_label)}</span>' if moved_from else ""
    )

    return f"""
    <article class="item">
      <a class="item-title" href="{url}" target="_blank" rel="noopener">{title}</a>
      <div class="item-meta">{escape(source_name)} {moved_html}&middot; {published}</div>
      <p class="item-desc">{escape(desc)}</p>
      {extra_html}
    </article>"""


def render_section(section_id, label, articles, badge=None, limit=None, narrative=None,
                    preview_limit=None, dedup=None):
    shown_all = articles[:limit] if limit else articles

    if not shown_all:
        body = '<p class="empty">No matching articles in this window.</p>'
    else:
        # clusters: (primary, others) per distinct story -- see
        # cluster_articles(). With no dedup data this is just one singleton
        # cluster per article, in the original order, so a topic without
        # grouping data renders exactly as it did before this feature.
        clusters = cluster_articles(shown_all, dedup)
        groups_map = (dedup or {}).get("groups") or {}
        contradictions_by_group = {}
        for c in (dedup or {}).get("contradictions") or []:
            contradictions_by_group.setdefault(c.get("group"), []).append(c)

        def cluster_html(primary, others):
            letter = groups_map.get(primary.get("url"))
            return render_cluster(primary, others, contradictions_by_group, letter)

        # The preview/"show more" split now counts STORIES, not raw
        # articles, so a topic full of 15 outlets covering 3 real events
        # doesn't trip the toggle just because it has many articles.
        if preview_limit and len(clusters) > preview_limit:
            visible, rest = clusters[:preview_limit], clusters[preview_limit:]
            visible_html = "\n".join(cluster_html(p, o) for p, o in visible)
            rest_html = "\n".join(cluster_html(p, o) for p, o in rest)
            body = f"""{visible_html}
    <details class="more">
      <summary>Show {len(rest)} more stor{"y" if len(rest) == 1 else "ies"}</summary>
      {rest_html}
    </details>"""
        else:
            body = "\n".join(cluster_html(p, o) for p, o in clusters)

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
    not a wall you scroll. nav_items: list of (anchor_id, label, badge, count)
    or (anchor_id, label, badge, count, trend_html) -- the optional fifth item
    is a small coverage-trend arrow (see trend_html)."""
    links = []
    for item in nav_items:
        anchor_id, label, badge, count = item[:4]
        trend = item[4] if len(item) > 4 else ""
        badge_class = f" nav-{badge}" if badge else ""
        links.append(
            f'<a class="nav-link{badge_class}" href="#{escape(anchor_id)}">'
            f'{escape(label)}{trend} <span class="nav-count">{count}</span></a>'
        )
    return f'<nav class="topic-nav">{"".join(links)}</nav>'


def load_synthesis(data_dir):
    path = data_dir / "synthesis.json"
    if not path.exists():
        return {"bluf": None, "topics": {}}
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Thread-based rendering (see threads.py / changes.py)
# ---------------------------------------------------------------------------
_DAY_CACHE = {}


def get_day(data_dir):
    """(threads_result, changes_report) for this day, or None if ranking
    failed. Memoized per process: build_page runs twice per build (live +
    archive copy). Failures are logged and swallowed on purpose -- a ranking
    bug must degrade the page to the old flat layout, never stop it
    publishing. Change detection failing alone keeps the ranked threads."""
    key = str(data_dir)
    if key not in _DAY_CACHE:
        try:
            res = T.build_threads(data_dir)
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: thread ranking failed for {data_dir.name}: {type(e).__name__}: {e} "
                  f"-- rendering flat per-topic lists instead.", file=sys.stderr)
            _DAY_CACHE[key] = None
            return None
        try:
            rep = C.compute(data_dir.name, data_root=data_dir.parent, cur=res)
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: change detection failed for {data_dir.name}: {type(e).__name__}: {e} "
                  f"-- rendering without trend/new/growing markers.", file=sys.stderr)
            rep = None
        _DAY_CACHE[key] = (res, rep)
    return _DAY_CACHE[key]


def _thread_originals(res, thread):
    """The raw article dicts of a thread, lead first (display order)."""
    return [res["_records"][i]["article"] for i in thread["_ordered"]]


def trend_html(cat):
    """A small arrow for a category that is clearly above/below its own
    recent average. 'flat' and 'not enough history' render nothing."""
    if not cat or cat.get("arrow") not in ("up", "down"):
        return ""
    sym, word = ("▲", "up") if cat["arrow"] == "up" else ("▼", "down")
    tip = (f"Coverage {word}: {cat['today']:.1f} vs {cat['baseline']:.1f} average over "
           f"{cat['baseline_days']} prior days (a coverage trend, not a measure of events)")
    return f'<span class="trend trend-{cat["arrow"]}" title="{escape(tip)}" aria-label="{escape(tip)}">{sym}</span>'


def flags_html(thread, entry, labels, show_also=True):
    flags = []
    if entry:
        st = entry["status"]
        if st == "new":
            flags.append('<span class="flag flag-new">New</span>')
        elif st == "continuing":
            flags.append(f'<span class="flag">Day {entry["streak"]}</span>')
        elif st == "returning":
            flags.append('<span class="flag">Returning</span>')
        if entry.get("growing"):
            flags.append('<span class="flag flag-grow">▲ Growing</span>')
    others = [labels[t] for t in thread["topics"] if t != thread["home"] and t in labels]
    if others and show_also:
        flags.append(f'<span class="flag-also">also under: {escape(" · ".join(others))}</span>')
    return "".join(flags)


def render_thread(res, thread, entry, labels):
    """One story in full: flags, lead article, other sources, contradictions."""
    arts = _thread_originals(res, thread)
    others_html = ""
    if len(arts) > 1:
        body = "\n".join(render_article(a) for a in arts[1:])
        n = len(arts) - 1
        others_html = f"""
    <details class="other-sources">
      <summary>+{n} other source{"s" if n != 1 else ""} on this story</summary>
      {body}
    </details>"""
    flags = flags_html(thread, entry, labels)
    flags_block = f'<div class="thread-flags">{flags}</div>' if flags else ""
    contradiction_html = render_contradiction_box(thread.get("contradictions") or [])
    return f"""
    <div class="story-cluster" id="{escape(thread["id"])}" data-score="{thread["score"]}">
      {flags_block}
      {render_article(arts[0])}
      {others_html}
      {contradiction_html}
    </div>"""


def render_ref(res, thread, section_id, labels):
    """One-line pointer for a story whose full card lives under another topic.
    Shows THIS topic's own best article so the section still says what it
    saw, and links to the full story."""
    mine = [a for a in thread["articles"] if a["topic"] == section_id]
    a = mine[0] if mine else thread["articles"][0]
    home_label = labels.get(thread["home"], thread["home"])
    return f"""
    <div class="story-ref">
      <a class="item-title" href="{escape(a.get("url") or "#")}" target="_blank" rel="noopener">{escape(a.get("title") or "(untitled)")}</a>
      <div class="item-meta">{escape(a.get("source") or "Unknown source")} &middot; part of a larger story under
        <a class="ref-link" href="#{escape(thread["id"])}">{escape(home_label)}</a>: {escape(thread.get("lead_title") or "")}</div>
    </div>"""


def no_signal_detail(data_dir, topic_id):
    """Why a section is empty, from the red-team audit report: what was
    retrieved and what happened to it. '' when there is nothing to say."""
    path = data_dir / "redteam" / "_report.json"
    try:
        with open(path) as f:
            t = (json.load(f).get("topics") or {}).get(topic_id)
    except (OSError, ValueError):
        return ""
    if not t or not t.get("retrieved"):
        return ""
    bits = [f'{t["retrieved"]} article{"s" if t["retrieved"] != 1 else ""} retrieved']
    if t.get("discarded"):
        bits.append(f'{t["discarded"]} filtered out as not relevant')
    if t.get("moved_out"):
        bits.append(f'{t["moved_out"]} moved to a better-fitting topic')
    return "; ".join(bits) + "."


def render_thread_section(section_id, label, res, rep, data_dir, narrative=None, badge=None,
                          preview_limit=None):
    labels = res["labels"]
    by_id = {t["id"]: t for t in res["threads"]}
    sec = res["sections"].get(section_id) or {"home": [], "linked": []}
    home = [by_id[i] for i in sec["home"]]
    linked = [by_id[i] for i in sec["linked"]]
    per_thread = (rep or {}).get("threads") or {}
    cat = ((rep or {}).get("categories") or {}).get(section_id)

    if not home and not linked:
        # Quiet on purpose: no headline that matches a TV series, and no AI
        # narrative describing noise -- just what was searched and filtered.
        detail = no_signal_detail(data_dir, section_id)
        detail_html = f' <span class="empty-detail">{escape(detail)}</span>' if detail else ""
        body = f'<p class="empty">No signal today.{detail_html}</p>'
        narrative = None
    else:
        def cards(items):
            return "\n".join(render_thread(res, t, per_thread.get(t["id"]), labels) for t in items)

        if preview_limit and len(home) > preview_limit:
            visible, rest = home[:preview_limit], home[preview_limit:]
            body = f"""{cards(visible)}
    <details class="more">
      <summary>Show {len(rest)} more stor{"y" if len(rest) == 1 else "ies"}</summary>
      {cards(rest)}
    </details>"""
        else:
            body = cards(home)
        if linked:
            refs = "\n".join(render_ref(res, t, section_id, labels) for t in linked)
            body += f"""
    <div class="refs"><div class="refs-label">Also reported here &mdash; full story elsewhere</div>{refs}</div>"""

    badge_html = f'<span class="badge badge-{badge}">{escape(badge)}</span>' if badge else ""
    narrative_html = f'<p class="narrative">{escape(narrative)}</p>' if narrative else ""
    n = len(home)
    count = f'{n} stor{"y" if n == 1 else "ies"}' + (f' <span class="count-linked">+{len(linked)} linked</span>' if linked else "")
    return f"""
  <section class="topic" id="{escape(section_id)}">
    <h2>{escape(label)} {badge_html}{trend_html(cat)}<span class="count">{count}</span></h2>
    {narrative_html}
    {body}
  </section>"""


def render_top_developments(res, rep):
    labels = res["labels"]
    tops = sorted((t for t in res["threads"] if t.get("top_rank")), key=lambda t: t["top_rank"])
    if not tops:
        return ""
    by_id = {t["id"]: t for t in res["threads"]}
    per_thread = (rep or {}).get("threads") or {}
    items = []
    for t in tops:
        lead = res["_records"][t["_ordered"][0]]["article"]
        desc = (lead.get("description") or "").strip()
        if len(desc) > 200:
            desc = desc[:197].rsplit(" ", 1)[0] + "…"
        others = [labels[k] for k in t["topics"] if k != t["home"] and k in labels]
        meta = [f'{t["n_independent"]} independent source{"s" if t["n_independent"] != 1 else ""}',
                escape(labels.get(t["home"], t["home"]))]
        if others:
            meta.append("also " + escape(", ".join(others)))
        if t.get("related"):
            r = len(t["related"])
            meta.append(f"+{r} related thread{'s' if r != 1 else ''}")
        rel_titles = "; ".join(by_id[i]["lead_title"] or "" for i in t["related"] if i in by_id)
        rel_attr = f' title="{escape(rel_titles)}"' if rel_titles else ""
        items.append(f"""
      <li>
        <div class="td-head">{flags_html(t, per_thread.get(t["id"]), labels, show_also=False)}
          <a class="item-title" href="{escape(lead.get("url") or "#")}" target="_blank" rel="noopener">{escape(lead.get("title") or "(untitled)")}</a></div>
        <p class="item-desc">{escape(desc)}</p>
        <div class="item-meta"{rel_attr}>{" &middot; ".join(meta)} &middot; <a class="ref-link" href="#{escape(t["id"])}">full story</a></div>
      </li>""")
    return f"""
  <section class="top-dev">
    <h2>Top developments</h2>
    <ol>{"".join(items)}
    </ol>
  </section>"""


def render_changes(res, rep):
    if not rep or not rep.get("compared_to"):
        return ""
    labels = res["labels"]
    by_id = {t["id"]: t for t in res["threads"]}

    def link(t):
        lead = res["_records"][t["_ordered"][0]]["article"]
        return (f'<li><a class="item-title" href="#{escape(t["id"])}">{escape(lead.get("title") or "")}</a>'
                f' <span class="item-meta">{escape(labels.get(t["home"], t["home"]))}</span></li>')

    new_top = [by_id[i] for i in rep["new"] if by_id[i]["rank"] <= 15][:5]
    growing = [by_id[i] for i in rep["growing"]][:5]
    faded = rep["faded"][:3]
    ups = [labels[k] for k, c in rep["categories"].items() if c["arrow"] == "up"]
    downs = [labels[k] for k, c in rep["categories"].items() if c["arrow"] == "down"]

    groups = []
    if new_top:
        groups.append('<div class="chg-group"><h3>New in today\'s top 15</h3><ul>' + "".join(link(t) for t in new_top) + "</ul></div>")
    if growing:
        groups.append('<div class="chg-group"><h3>Growing</h3><ul>' + "".join(link(t) for t in growing) + "</ul></div>")
    if faded:
        rows = "".join(
            f'<li><a class="item-title" href="{escape(f["url"] or "#")}" target="_blank" rel="noopener">{escape(f["title"] or "")}</a>'
            f' <span class="item-meta">{escape(labels.get(f["home"], f["home"]))}</span></li>' for f in faded)
        groups.append('<div class="chg-group"><h3>In yesterday\'s top, not in today\'s coverage</h3><ul>' + rows + "</ul></div>")
    shifts = []
    if ups:
        shifts.append("▲ " + escape(", ".join(ups)))
    if downs:
        shifts.append("▼ " + escape(", ".join(downs)))
    if shifts:
        groups.append('<div class="chg-group"><h3>Coverage shifts vs. recent average</h3><p class="chg-shifts">' + "<br>".join(shifts) + "</p></div>")
    if not groups:
        return ""
    gap = rep.get("gap_days") or 1
    when = rep["compared_to"] + ("" if gap == 1 else f" &mdash; latest earlier day on record, {gap} days before")
    return f"""
  <section class="changes">
    <h2>Changes since {when}</h2>
    <div class="chg-grid">{"".join(groups)}</div>
    <p class="chg-note">Compared on story text against the {rep["baseline_days"]} earlier day{"s" if rep["baseline_days"] != 1 else ""} on record.
       &ldquo;New&rdquo; means no close match in those days &mdash; a fresh development in a long-running conflict can still read as new.
       &ldquo;Not in today&rsquo;s coverage&rdquo; is not the same as resolved.</p>
  </section>"""


def render_method():
    w = T.RANK_WEIGHTS
    cats = "".join(
        f"<li><b>{escape(k.replace('_', ' '))}</b> &middot; weight {v:g}</li>"
        for k, v in source_tiers.TIER_WEIGHT.items())
    return f"""
  <details class="method">
    <summary>How stories are ranked and compared</summary>
    <p>Articles about the same event are grouped into one <b>story</b> by text similarity, across topics.
       Each story is scored 0&ndash;100 from five parts: independent-source corroboration ({w["corroboration"]:.0%}),
       source quality ({w["quality"]:.0%}), recency ({w["recency"]:.0%}), escalation and casualty language ({w["escalation"]:.0%}),
       and the topic&rsquo;s severity ({w["severity"]:.0%}). Syndicated copies and republisher sites count as one source, not many.
       Only stories whose best source is an established outlet or better can appear in Top developments.
       Stories are never hidden for their source &mdash; they are ranked lower.</p>
    <p>Source categories are functional, not political: <ul class="method-list">{cats}</ul></p>
    <p>This is an automated heuristic. Grouping is approximate (occasionally two events are merged or one is split),
       scores measure how much and how credibly something is being reported, not how true or important it ultimately is,
       and trend arrows reflect news coverage volume, not events on the ground.</p>
  </details>"""


EXTRA_CSS = """
  section.top-dev, section.changes {
    background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
    padding: 1.1rem 1.5rem; margin: 1.25rem 0;
  }
  section.top-dev { border-left: 3px solid var(--accent); }
  section.top-dev h2, section.changes h2 {
    margin: 0 0 0.7rem; font-size: 0.75rem; letter-spacing: 0.08em;
    color: var(--accent); text-transform: uppercase;
  }
  section.top-dev ol { margin: 0; padding-left: 1.2rem; }
  section.top-dev li { margin-bottom: 0.95rem; }
  section.top-dev li:last-child { margin-bottom: 0; }
  .td-head { line-height: 1.5; }
  .chg-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 1rem 1.5rem; }
  .chg-group h3 { margin: 0 0 0.35rem; font-size: 0.75rem; color: var(--muted); font-weight: 600; }
  .chg-group ul { margin: 0; padding-left: 1.1rem; font-size: 0.85rem; line-height: 1.45; }
  .chg-group li { margin-bottom: 0.3rem; }
  .chg-group a { color: var(--text); text-decoration: none; font-size: 0.85rem; font-weight: 500; }
  .chg-group a:hover { color: var(--accent); }
  .chg-shifts { margin: 0; font-size: 0.85rem; line-height: 1.6; }
  .chg-note { margin: 0.9rem 0 0; font-size: 0.72rem; color: var(--muted); line-height: 1.45; }
  .thread-flags { display: flex; flex-wrap: wrap; align-items: center; gap: 0.35rem; margin: 0 0 0.3rem; }
  .td-head .flag, .td-head .flag-also { margin-right: 0.35rem; }
  .flag {
    font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600;
    padding: 0.1rem 0.42rem; border-radius: 3px; border: 1px solid var(--border); color: var(--muted);
  }
  .flag-new { color: var(--accent); border-color: rgba(77,159,255,0.45); }
  .flag-grow { color: #ffb454; border-color: rgba(255,180,84,0.45); }
  .flag-also { font-size: 0.68rem; color: var(--muted); font-style: italic; }
  .trend { font-size: 0.7rem; margin-left: 0.2rem; cursor: help; }
  .trend-up { color: #ffb454; }
  .trend-down { color: var(--muted); }
  .count-linked { color: var(--muted); font-size: 0.72rem; margin-left: 0.3rem; }
  .refs { margin-top: 0.4rem; padding-top: 0.7rem; border-top: 1px dashed var(--border); }
  .refs-label { font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 0.5rem; }
  .story-ref { margin-bottom: 0.75rem; }
  .story-ref .item-title { font-size: 0.88rem; font-weight: 500; }
  .ref-link { color: var(--accent); text-decoration: none; }
  .ref-link:hover { text-decoration: underline; }
  .empty-detail { color: var(--muted); font-style: normal; }
  details.method { margin: 2.5rem 0 0; border-top: 1px solid var(--border); padding-top: 1rem; }
  details.method summary { cursor: pointer; color: var(--muted); font-size: 0.8rem; }
  details.method p, details.method li { font-size: 0.8rem; color: var(--muted); line-height: 1.5; }
  .method-list { columns: 2; margin: 0.3rem 0 0; padding-left: 1.1rem; }
"""

# Story cards inside a collapsed "Show N more" <details>, or the "+N other
# sources" toggle, don't open themselves when a link jumps to them; this
# opens every closed ancestor <details> of the jump target.
EXTRA_JS = """
(function() {
  function openAncestors(id) {
    var el = id ? document.getElementById(id) : null;
    for (; el; el = el.parentElement) { if (el.tagName === "DETAILS") el.open = true; }
    var t = id ? document.getElementById(id) : null;
    if (t) t.scrollIntoView({block: "start"});
  }
  function onHash() { if (location.hash.length > 1) openAncestors(decodeURIComponent(location.hash.slice(1))); }
  window.addEventListener("hashchange", onHash);
  onHash();
})();
"""


def build_page(date_str, data_dir, archive=False):
    """Renders the full briefing page for data_dir as an HTML string.
    archive=True adjusts the header links and search-widget path for a page
    living two directories down, at docs/archive/<YYYY-MM>/<date>.html --
    archive.py builds the month folder around it and this function never
    needs to know the month string itself, only that it's two levels deep."""
    synthesis = load_synthesis(data_dir)
    topic_narratives = synthesis.get("topics") or {}
    bluf = synthesis.get("bluf")

    sections = []
    nav_items = []  # (anchor_id, label, badge, count[, trend]) -- drives the jump-to-topic bar

    # Ranked stories + change markers (threads.py / changes.py). None means
    # ranking failed and this page falls back to the flat per-topic layout.
    day = get_day(data_dir)
    res, rep = day if day else (None, None)
    categories = (rep or {}).get("categories") or {}

    def thread_section(tid, label, badge=None):
        n_home = len((res["sections"].get(tid) or {}).get("home") or [])
        sections.append(render_thread_section(
            tid, label, res, rep, data_dir, narrative=topic_narratives.get(tid), badge=badge,
            preview_limit=ARTICLE_PREVIEW_LIMIT,
        ))
        nav_items.append((tid, label, badge, n_home, trend_html(categories.get(tid))))

    # Tier 1 flagships get top billing -- no badge/color on these anymore
    # (Matt: drop the "FLAGSHIP" label entirely; position at the top of the
    # page plus going first in the nav bar already signals priority, and a
    # badge here was visually competing with TRIPWIRE's red for meaning
    # "important" when they're actually two different things -- WHERE
    # (this is a priority theater) vs HOW SEVERE (this category is
    # high-severity). Leaving TRIPWIRE as the only colored badge/pill makes
    # that distinction unambiguous instead of both looking like one tier.)
    for flagship in TIER1:
        if res:
            thread_section(flagship["id"], flagship["label"])
        else:
            arts = rank_articles(dedupe(load_articles(data_dir, flagship["id"])))
            sections.append(render_section(
                flagship["id"], flagship["label"], arts,
                narrative=topic_narratives.get(flagship["id"]), preview_limit=ARTICLE_PREVIEW_LIMIT,
                dedup=load_dedup(data_dir, flagship["id"]),
            ))
            nav_items.append((flagship["id"], flagship["label"], None, len(arts)))
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
            badge = "tripwire" if topic["tier"] == "tripwire" else None
            if res:
                thread_section(topic["id"], topic["label"], badge=badge)
            else:
                arts = rank_articles(dedupe(load_articles(data_dir, topic["id"])))
                sections.append(render_section(
                    topic["id"], topic["label"], arts, badge=badge,
                    narrative=topic_narratives.get(topic["id"]), preview_limit=ARTICLE_PREVIEW_LIMIT,
                    dedup=load_dedup(data_dir, topic["id"]),
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

    if archive:
        # This page lives at docs/archive/<YYYY-MM>/<date>.html: "index.html"
        # is this month's own index (sibling file), "../index.html" is the
        # top-level archive (list of months), "../../index.html" is the
        # live page. (The site-wide, cross-day search box lives on those
        # archive index pages -- see search_widget.py's docstring -- this
        # page itself only gets the page-local find-and-highlight box
        # below, so there's no root_prefix to thread through here anymore.)
        links = ('<a href="index.html">This month</a> &middot; '
                 '<a href="../index.html">All months</a> &middot; '
                 '<a href="../../index.html">Latest</a>')
    else:
        links = '<a href="archive/index.html">Archive</a>'

    topdev_html = render_top_developments(res, rep) if res else ""
    changes_html = render_changes(res, rep) if res else ""
    method_html = render_method() if res else ""

    return TEMPLATE.format(date=date_str, nav=nav_html, bluf=bluf_html,
                           topdev=topdev_html, changes=changes_html, method=method_html,
                           sections="\n".join(sections), links=links,
                           search_css=PAGE_SEARCH_CSS, search=render_page_search_widget(),
                           extra_css=EXTRA_CSS, extra_js=EXTRA_JS)


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data_dir = ROOT / "data" / today
    if not data_dir.exists():
        print(f"No data directory for {today} -- run fetch_news.py first.", file=sys.stderr)
        sys.exit(1)

    docs_dir = ROOT / "docs"
    month_dir = docs_dir / "archive" / today[:7]
    month_dir.mkdir(parents=True, exist_ok=True)

    with open(docs_dir / "index.html", "w") as f:
        f.write(build_page(today, data_dir))
    # Same page frozen under its date, inside that month's archive folder.
    # Rewritten on every run of the same UTC day (so a manual re-run
    # replaces it) and never touched on later days.
    with open(month_dir / f"{today}.html", "w") as f:
        f.write(build_page(today, data_dir, archive=True))

    print(f"Wrote {docs_dir / 'index.html'} and archive/{today[:7]}/{today}.html for {today}")


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
    /* Fallback height for the sticky topic-nav bar, used by section.topic's
       scroll-margin-top below so a nav-link jump doesn't land a section's
       heading/narrative underneath the sticky bar. Overwritten with the
       bar's real measured height by the inline script right before
       </body> -- see there for why a fixed guess isn't enough on its own
       (the bar wraps to a different number of rows at different widths). */
    --nav-h: 160px;
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
  header p a {{ color: var(--accent); text-decoration: none; }}
  header p a:hover {{ text-decoration: underline; }}
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
    /* Without this, jumping here via a nav-link (or a search-widget/jump-
       list result) lands the heading and narrative box right underneath
       the sticky topic-nav bar, hidden from view -- the visible scroll
       position ends up mid-article-list instead. See --nav-h above. */
    scroll-margin-top: calc(var(--nav-h) + 0.75rem);
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
  /* Each story (one lead article, optionally more sources + a
     contradiction box tucked inside) is the spacing unit now, not the
     bare .item -- see render_cluster(). Un-clustered topics (no dedup
     data) still get one story-cluster per article, so this replaces
     .item's old margin-bottom one-for-one rather than adding to it. */
  .story-cluster {{ margin-bottom: 1.1rem; }}
  .item {{ margin-bottom: 0; }}
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
  .moved-tag {{
    color: var(--muted);
    font-size: 0.65rem;
    font-style: italic;
    margin-right: 0.35rem;
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
  .nav-count {{ color: var(--muted); font-size: 0.7rem; }}
  details.more, details.other-sources {{ margin-top: 0.5rem; }}
  details.more summary, details.other-sources summary {{
    cursor: pointer;
    color: var(--accent);
    font-size: 0.85rem;
    padding: 0.4rem 0;
    list-style: none;
  }}
  details.more summary::-webkit-details-marker, details.other-sources summary::-webkit-details-marker {{ display: none; }}
  details.more summary:before, details.other-sources summary:before {{ content: "+ "; }}
  details.more[open] summary:before, details.other-sources[open] summary:before {{ content: "− "; }}
  details.more[open] summary, details.other-sources[open] summary {{ margin-bottom: 0.5rem; }}
  /* "+N other sources" reads as a lighter-weight aside than the main
     "show more stories" toggle, so it's dimmer and doesn't compete with it. */
  details.other-sources summary {{ color: var(--muted); font-size: 0.78rem; }}
  details.other-sources summary:hover {{ color: var(--text); }}
  .contradiction-box {{
    margin: 0.6rem 0 0;
    padding: 0.6rem 0.85rem;
    border: 1px solid rgba(255,90,90,0.35);
    border-left: 3px solid var(--tripwire);
    border-radius: 0 4px 4px 0;
    background: rgba(255,90,90,0.06);
  }}
  .contradiction-label {{
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--tripwire);
    font-weight: 600;
    margin-bottom: 0.3rem;
  }}
  .contradiction-summary {{
    font-size: 0.82rem;
    color: var(--text);
    margin: 0 0 0.4rem;
    line-height: 1.4;
  }}
  .contradiction-sources {{
    margin: 0;
    padding-left: 1.1rem;
    font-size: 0.78rem;
    color: var(--muted);
    line-height: 1.5;
  }}
  .contradiction-sources a {{ color: var(--accent); text-decoration: none; }}
  .contradiction-sources a:hover {{ text-decoration: underline; }}
  #gsw-top-btn {{
    position: fixed;
    right: 1.25rem;
    bottom: 1.25rem;
    z-index: 30;
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0.55rem 1rem;
    font-size: 0.8rem;
    font-family: inherit;
    cursor: pointer;
    box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    opacity: 0;
    transform: translateY(0.5rem);
    pointer-events: none;
    transition: opacity 0.2s ease, transform 0.2s ease, border-color 0.2s ease, color 0.2s ease;
  }}
  #gsw-top-btn.visible {{ opacity: 1; transform: translateY(0); pointer-events: auto; }}
  #gsw-top-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
{search_css}
{extra_css}
</style>
</head>
<body>
<header>
  <h1>Global Situation Watch</h1>
  <p>{date} &middot; {links}</p>
  {search}
</header>
{nav}
<main>
{bluf}
{topdev}
{changes}
{sections}
{method}
</main>
<button id="gsw-top-btn" type="button" aria-label="Back to top">&uarr; Top</button>
<script>
(function() {{
  // Keeps --nav-h (used by section.topic's scroll-margin-top, see :root
  // above) matched to the sticky topic-nav bar's REAL height -- it wraps to
  // a different number of rows depending on viewport width, so a single
  // fixed guess can't stay correct at every width. Re-measures on resize
  // since a width change can change the wrap count.
  var nav = document.querySelector(".topic-nav");
  function syncNavHeight() {{
    if (nav) document.documentElement.style.setProperty("--nav-h", nav.offsetHeight + "px");
  }}
  syncNavHeight();
  window.addEventListener("resize", syncNavHeight);

  var topBtn = document.getElementById("gsw-top-btn");
  function toggleTopBtn() {{
    if (window.scrollY > 500) topBtn.classList.add("visible");
    else topBtn.classList.remove("visible");
  }}
  window.addEventListener("scroll", toggleTopBtn, {{passive: true}});
  topBtn.addEventListener("click", function() {{
    window.scrollTo({{top: 0, behavior: "smooth"}});
  }});
  toggleTopBtn();
}})();
{extra_js}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
