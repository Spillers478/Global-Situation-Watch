"""
Builds the browsable archive (grouped into monthly folders), the site-wide
search index, and the trend-ready dataset from everything under
data/<date>/. Run after build_brief.py (see the workflow).

Outputs
-------
docs/archive/<YYYY-MM>/<date>.html
                            One frozen briefing page per day, inside a
                            folder for its month (e.g. docs/archive/2026-09/).
                            build_brief.py writes today's directly; this
                            script backfills any past date that has data but
                            no page yet at that path (never overwrites an
                            existing one, so archived pages stay exactly as
                            they were published). Monthly folders keep the
                            archive from becoming one endless scroll as it
                            grows past a few weeks -- see render_month_index.
docs/archive/<YYYY-MM>/index.html
                            That month's briefings, newest first, each with
                            its BLUF snippet and article count.
docs/archive/index.html    Every month with data, newest first, linking to
                            its index.html above -- what a reader lands on
                            from "Archive" / "All months".
docs/search-index.json     One entry per displayed article across every
                            date: title, source, topic label, date, and a
                            root-relative link into that day's archived page
                            at the matching topic section. This file *is*
                            the search index -- there's no backend, so the
                            search box on the live page and every archive
                            index (search_widget.py) fetches and filters it
                            client-side. Rebuilt in full on every run, same
                            as the CSVs below.
data/history/topic_stats.csv
                            One row per (date, topic): retrieved / kept /
                            moved_out / discarded / unclassified / displayed
                            counts plus redteam_status (ok / incomplete /
                            failed / not_run) -- filter on ok when trending,
                            since failed/not_run rows show raw retrieval.
                            This is the "how loud was topic X" table.
data/history/articles-YYYY-MM.csv
                            One row per article shown on that day's page
                            (post red-team), with topic, source, COCOM and
                            reassignment info. Split by month so a daily
                            rewrite never touches more than one month's file.

Everything is rebuilt from data/<date>/ on each run rather than appended, so
it is idempotent, safe to re-run, and backfills history from day one. The
raw JSON in data/<date>/ remains the source of truth; the CSVs, the archive
pages, and search-index.json are all derived and can be regenerated any
time with `python scripts/archive.py`.

Article counts here use build_brief.load_articles(), so they match what the
page actually displayed that day (red-team output preferred, raw fallback).
"""
import calendar
import csv
import json
import re
import sys
from html import escape
from pathlib import Path

import build_brief
from search_widget import SEARCH_CSS, render_search_widget
from topics import TIER1, TOPICS

ROOT = build_brief.ROOT
DATA = ROOT / "data"
ARCHIVE = ROOT / "docs" / "archive"
HISTORY = DATA / "history"
SEARCH_INDEX_PATH = ROOT / "docs" / "search-index.json"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

STATS_FIELDS = ["date", "topic_id", "topic_label", "retrieved", "kept_here",
                "moved_out", "discarded", "unclassified", "displayed", "vetted",
                "redteam_status"]
ARTICLE_FIELDS = ["date", "topic_id", "topic_label", "title", "source",
                  "published_at", "cocom", "reassigned_from", "url"]


def data_dates():
    return sorted(p.name for p in DATA.iterdir() if p.is_dir() and DATE_RE.match(p.name))


def month_label(ym):
    """"2026-09" -> "September 2026", for the month folder's page title."""
    year, month = ym.split("-")
    return f"{calendar.month_name[int(month)]} {year}"


def topic_list():
    """(id, label) for every topic that gets a page section, plus US-lens
    sections so their counts are recorded too."""
    out = []
    for fl in TIER1:
        out.append((fl["id"], fl["label"]))
        out.append((f"{fl['id']}-us-lens", f"{fl['label']} — US Media Lens"))
    for t in TOPICS:
        out.append((t["id"], t["label"]))
    return out


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def collect(date):
    """Returns (stat_rows, article_rows, summary) for one date. article_rows
    carries topic_id (needed to anchor a search result at the right section)
    alongside the fields written to the CSV."""
    data_dir = DATA / date
    report = load_json(data_dir / "redteam" / "_report.json") or {}
    report_topics = report.get("topics") or {}
    failed = set(report.get("failed_topics") or [])
    incomplete = set(report.get("incomplete_topics") or [])

    stat_rows, article_rows = [], []
    for topic_id, label in topic_list():
        shown = build_brief.dedupe(build_brief.load_articles(data_dir, topic_id))
        raw = load_json(data_dir / f"{topic_id}.json") or {}
        raw_count = len(raw.get("articles") or [])
        rt = report_topics.get(topic_id)
        if rt is None and raw_count == 0 and not shown:
            continue  # topic didn't exist / retrieved nothing that day
        vetted = rt is not None and topic_id not in failed
        # ok = fully classified; incomplete = some articles left unclassified
        # (kept in place); failed = nothing classified, page showed raw
        # retrieval; not_run = no red-team pass that day (or no data for it).
        if rt is None:
            status = "not_run"
        elif topic_id in failed:
            status = "failed"
        elif topic_id in incomplete or rt.get("unclassified"):
            status = "incomplete"
        else:
            status = "ok"
        stat_rows.append({
            "date": date, "topic_id": topic_id, "topic_label": label,
            "retrieved": rt["retrieved"] if rt else raw_count,
            "kept_here": rt["kept_here"] if rt else "",
            "moved_out": rt["moved_out"] if rt else "",
            "discarded": rt["discarded"] if rt else "",
            "unclassified": rt.get("unclassified", 0) if rt else "",
            "displayed": len(shown),
            "vetted": int(vetted),
            "redteam_status": status,
        })
        for a in shown:
            article_rows.append({
                "date": date, "topic_id": topic_id, "topic_label": label,
                "title": (a.get("title") or "").replace("\n", " ").strip(),
                "source": (a.get("source") or {}).get("name") or "",
                "published_at": a.get("publishedAt") or "",
                "cocom": a.get("cocom") or "",
                "reassigned_from": a.get("_original_topic_id") or "",
                "url": a.get("url") or "",
            })

    synthesis = load_json(data_dir / "synthesis.json") or {}
    summary = {
        "date": date,
        "bluf": synthesis.get("bluf") or "",
        "displayed": sum(r["displayed"] for r in stat_rows
                         if not r["topic_id"].endswith("-us-lens")),
    }
    return stat_rows, article_rows, summary


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def build_search_index(article_rows):
    """One entry per displayed article, keyed short (t/s/l/d/h) since this
    file ships to every visitor's browser on first search. "h" is root-
    relative (from docs/) so search_widget.py's root_prefix + h resolves
    from any page depth -- see that module's docstring. Skips -us-lens
    rows: that sidebar is a media-comparison view of the SAME flagship
    articles, not distinct content worth a second search hit."""
    seen, out = set(), []
    for r in article_rows:
        if r["topic_id"].endswith("-us-lens") or not r["title"]:
            continue
        key = (r["date"], r["url"] or r["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "t": r["title"], "s": r["source"], "l": r["topic_label"], "d": r["date"],
            "h": f"archive/{r['date'][:7]}/{r['date']}.html#{r['topic_id']}",
        })
    out.sort(key=lambda a: a["d"], reverse=True)
    return out


ARCHIVE_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  :root {{ --bg:#0b0e14; --panel:#131722; --border:#232838; --text:#dbe1ee; --muted:#7c8797; --accent:#4d9fff; }}
  body {{ background:var(--bg); color:var(--text); margin:0; padding:0 0 4rem;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }}
  main {{ max-width:860px; margin:0 auto; padding:2.5rem 1.5rem 0; }}
  h1 {{ font-size:1.6rem; margin:0 0 .25rem; letter-spacing:.02em; }}
  .sub a {{ color:var(--accent); text-decoration:none; font-size:.9rem; }}
  .sub a:hover {{ text-decoration: underline; }}
  ul {{ list-style:none; padding:0; margin:1.75rem 0 0; }}
  li {{ border-top:1px solid var(--border); padding:1rem 0; }}
  .d {{ color:var(--accent); font-weight:600; text-decoration:none; }}
  .n {{ color:var(--muted); font-size:.8rem; margin-left:.75rem; }}
  li p {{ color:var(--muted); font-size:.85rem; line-height:1.45; margin:.4rem 0 0; }}
{search_css}
</style>
</head>
<body>
<main>
  <h1>{heading}</h1>
  <div class="sub">{subnav}</div>
  {search}
  <ul>
{items}
  </ul>
</main>
</body>
</html>
"""


def _day_items_html(summaries, href=lambda s: f"{s['date']}.html"):
    items = []
    for s in sorted(summaries, key=lambda s: s["date"], reverse=True):
        bluf = s["bluf"]
        snippet = (bluf[:240].rsplit(" ", 1)[0] + "…") if len(bluf) > 240 else bluf
        items.append(
            f'<li><a class="d" href="{href(s)}">{s["date"]}</a>'
            f'<span class="n">{s["displayed"]} articles</span>'
            f'<p>{escape(snippet)}</p></li>'
        )
    return "\n".join(items)


def render_month_index(ym, month_summaries):
    label = month_label(ym)
    return ARCHIVE_PAGE_TEMPLATE.format(
        title=f"Global Situation Watch — {label}",
        heading=label,
        subnav='<a href="../index.html">All months</a> &middot; <a href="../../index.html">Latest briefing</a>',
        # Scoped to this month: search-index.json entries outside ym are
        # filtered out client-side before matching -- see search_widget.py.
        # The top-level archive index (render_top_index) stays unscoped.
        search=render_search_widget("../../", scope_prefix=ym, scope_label=label),
        search_css=SEARCH_CSS, items=_day_items_html(month_summaries),
    )


def render_top_index(months):
    """months: [(ym, label, day_count), ...]."""
    items = "\n".join(
        f'<li><a class="d" href="{ym}/index.html">{escape(label)}</a>'
        f'<span class="n">{n} day{"s" if n != 1 else ""}</span></li>'
        for ym, label, n in sorted(months, key=lambda m: m[0], reverse=True)
    )
    return ARCHIVE_PAGE_TEMPLATE.format(
        title="Global Situation Watch — Archive",
        heading="Archive",
        subnav='<a href="../index.html">Latest briefing</a>',
        search=render_search_widget("../"),
        search_css=SEARCH_CSS, items=items,
    )


def main():
    dates = data_dates()
    if not dates:
        print("No data/<date>/ directories found.", file=sys.stderr)
        sys.exit(1)

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    all_stats, all_articles, articles_by_month = [], [], {}
    summaries_by_month = {}
    backfilled = 0

    for date in dates:
        stat_rows, article_rows, summary = collect(date)
        if not stat_rows:
            continue  # a data dir with nothing usable in it
        ym = date[:7]
        all_stats.extend(stat_rows)
        all_articles.extend(article_rows)
        articles_by_month.setdefault(ym, []).extend(article_rows)
        summaries_by_month.setdefault(ym, []).append(summary)

        month_dir = ARCHIVE / ym
        month_dir.mkdir(parents=True, exist_ok=True)
        page = month_dir / f"{date}.html"
        if not page.exists():
            page.write_text(build_brief.build_page(date, DATA / date, archive=True), encoding="utf-8")
            backfilled += 1

    write_csv(HISTORY / "topic_stats.csv", STATS_FIELDS, all_stats)
    for month, rows in articles_by_month.items():
        write_csv(HISTORY / f"articles-{month}.csv", ARTICLE_FIELDS, rows)

    for ym, month_summaries in summaries_by_month.items():
        (ARCHIVE / ym / "index.html").write_text(
            render_month_index(ym, month_summaries), encoding="utf-8")
    months = [(ym, month_label(ym), len(s)) for ym, s in summaries_by_month.items()]
    (ARCHIVE / "index.html").write_text(render_top_index(months), encoding="utf-8")

    search_index = build_search_index(all_articles)
    SEARCH_INDEX_PATH.write_text(json.dumps(search_index, ensure_ascii=False, separators=(",", ":")),
                                 encoding="utf-8")

    print(f"Archive: {sum(len(s) for s in summaries_by_month.values())} days across "
          f"{len(months)} month(s), {backfilled} pages backfilled, "
          f"{len(all_stats)} topic-stat rows, {len(all_articles)} article rows, "
          f"{len(search_index)} search-index entries.")


if __name__ == "__main__":
    main()
