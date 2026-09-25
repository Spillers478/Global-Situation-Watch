"""
Builds the browsable archive and the trend-ready dataset from everything
under data/<date>/. Run after build_brief.py (see the workflow).

Outputs
-------
docs/archive/<date>.html    One frozen briefing page per day. build_brief.py
                            writes today's; this script backfills any past
                            date that has data but no page yet (never
                            overwrites an existing one, so archived pages
                            stay exactly as they were published).
docs/archive/index.html     Dated list of every briefing with its BLUF and
                            article count.
data/history/topic_stats.csv
                            One row per (date, topic): retrieved / kept /
                            moved_out / discarded / displayed counts. This
                            is the "how loud was topic X this month" table.
data/history/articles-YYYY-MM.csv
                            One row per article shown on that day's page
                            (post red-team), with topic, source, COCOM and
                            reassignment info. Split by month so a daily
                            rewrite never touches more than one month's file.

Everything is rebuilt from data/<date>/ on each run rather than appended, so
it is idempotent, safe to re-run, and backfills history from day one. The
raw JSON in data/<date>/ remains the source of truth; the CSVs are derived
and can be regenerated any time with `python scripts/archive.py`.

Article counts here use build_brief.load_articles(), so they match what the
page actually displayed that day (red-team output preferred, raw fallback).
"""
import csv
import json
import re
import sys
from html import escape
from pathlib import Path

import build_brief
from topics import TIER1, TOPICS

ROOT = build_brief.ROOT
DATA = ROOT / "data"
ARCHIVE = ROOT / "docs" / "archive"
HISTORY = DATA / "history"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

STATS_FIELDS = ["date", "topic_id", "topic_label", "retrieved", "kept_here",
                "moved_out", "discarded", "displayed", "vetted"]
ARTICLE_FIELDS = ["date", "topic_id", "topic_label", "title", "source",
                  "published_at", "cocom", "reassigned_from", "url"]


def data_dates():
    return sorted(p.name for p in DATA.iterdir() if p.is_dir() and DATE_RE.match(p.name))


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
    """Returns (stat_rows, article_rows, summary) for one date."""
    data_dir = DATA / date
    report = load_json(data_dir / "redteam" / "_report.json") or {}
    report_topics = report.get("topics") or {}
    failed = set(report.get("failed_topics") or [])

    stat_rows, article_rows = [], []
    for topic_id, label in topic_list():
        shown = build_brief.dedupe(build_brief.load_articles(data_dir, topic_id))
        raw = load_json(data_dir / f"{topic_id}.json") or {}
        raw_count = len(raw.get("articles") or [])
        rt = report_topics.get(topic_id)
        if rt is None and raw_count == 0 and not shown:
            continue  # topic didn't exist / retrieved nothing that day
        vetted = rt is not None and topic_id not in failed
        stat_rows.append({
            "date": date, "topic_id": topic_id, "topic_label": label,
            "retrieved": rt["retrieved"] if rt else raw_count,
            "kept_here": rt["kept_here"] if rt else "",
            "moved_out": rt["moved_out"] if rt else "",
            "discarded": rt["discarded"] if rt else "",
            "displayed": len(shown),
            "vetted": int(vetted),
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


def render_index(summaries):
    items = []
    for s in sorted(summaries, key=lambda s: s["date"], reverse=True):
        bluf = s["bluf"]
        snippet = (bluf[:240].rsplit(" ", 1)[0] + "…") if len(bluf) > 240 else bluf
        items.append(
            f'<li><a class="d" href="{s["date"]}.html">{s["date"]}</a>'
            f'<span class="n">{s["displayed"]} articles</span>'
            f'<p>{escape(snippet)}</p></li>'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Global Situation Watch — Archive</title>
<style>
  :root {{ --bg:#0b0e14; --panel:#131722; --border:#232838; --text:#dbe1ee; --muted:#7c8797; --accent:#4d9fff; }}
  body {{ background:var(--bg); color:var(--text); margin:0; padding:0 0 4rem;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }}
  main {{ max-width:860px; margin:0 auto; padding:2.5rem 1.5rem 0; }}
  h1 {{ font-size:1.6rem; margin:0 0 .25rem; letter-spacing:.02em; }}
  .sub a {{ color:var(--accent); text-decoration:none; font-size:.9rem; }}
  ul {{ list-style:none; padding:0; margin:1.75rem 0 0; }}
  li {{ border-top:1px solid var(--border); padding:1rem 0; }}
  .d {{ color:var(--accent); font-weight:600; text-decoration:none; }}
  .n {{ color:var(--muted); font-size:.8rem; margin-left:.75rem; }}
  li p {{ color:var(--muted); font-size:.85rem; line-height:1.45; margin:.4rem 0 0; }}
</style>
</head>
<body>
<main>
  <h1>Global Situation Watch — Archive</h1>
  <div class="sub"><a href="../index.html">Latest briefing</a></div>
  <ul>
{chr(10).join(items)}
  </ul>
</main>
</body>
</html>
"""


def main():
    dates = data_dates()
    if not dates:
        print("No data/<date>/ directories found.", file=sys.stderr)
        sys.exit(1)

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    all_stats, articles_by_month, summaries = [], {}, []
    backfilled = 0

    for date in dates:
        stat_rows, article_rows, summary = collect(date)
        if not stat_rows:
            continue  # a data dir with nothing usable in it
        all_stats.extend(stat_rows)
        articles_by_month.setdefault(date[:7], []).extend(article_rows)
        summaries.append(summary)

        page = ARCHIVE / f"{date}.html"
        if not page.exists():
            page.write_text(build_brief.build_page(date, DATA / date, archive=True), encoding="utf-8")
            backfilled += 1

    write_csv(HISTORY / "topic_stats.csv", STATS_FIELDS, all_stats)
    for month, rows in articles_by_month.items():
        write_csv(HISTORY / f"articles-{month}.csv", ARTICLE_FIELDS, rows)
    (ARCHIVE / "index.html").write_text(render_index(summaries), encoding="utf-8")

    print(f"Archive: {len(summaries)} days indexed, {backfilled} pages backfilled, "
          f"{len(all_stats)} topic-stat rows, "
          f"{sum(len(r) for r in articles_by_month.values())} article rows.")


if __name__ == "__main__":
    main()
