"""
Writes the ranking and change-detection results for a day to disk, for audit:

  data/<date>/threads.json   every story thread with its score, the five score
                             components, sources, and which topics it spans
  data/<date>/changes.json   new / continuing / returning / growing / faded
                             markers and the per-category coverage arrows

build_brief.py and synthesize.py do NOT read these files -- both recompute the
same result in-process (it is deterministic and takes about half a second), so
a stale or missing file can never change what the page shows. These files
exist so "why is this story #1?" and "why does this category have an arrow?"
can be answered from the repo without re-running anything, and so the daily
history can be analysed later.

Run after dedupe_stories.py (its groups seed the story clustering) and before
synthesize.py:

    python scripts/rank_day.py            # today (UTC)
    python scripts/rank_day.py 2026-09-29 # a specific archived day
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import changes as C
import threads as T

ROOT = Path(__file__).resolve().parent.parent


def main(argv):
    date = argv[0] if argv else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data_dir = ROOT / "data" / date
    if not data_dir.exists():
        print(f"No data directory for {date} -- run fetch_news.py first.", file=sys.stderr)
        return 1

    res = T.build_threads(data_dir)
    with open(data_dir / "threads.json", "w") as f:
        json.dump(T.public_view(res), f, indent=1)

    rep = C.compute(date, data_root=ROOT / "data", cur=res)
    with open(data_dir / "changes.json", "w") as f:
        json.dump(rep, f, indent=1)

    tops = [t for t in res["threads"] if t.get("top_rank")]
    print(f"{date}: {len(res['_records'])} articles -> {len(res['threads'])} threads "
          f"({sum(1 for t in res['threads'] if len(t['topics']) > 1)} span topics); "
          f"top developments: {len(tops)}; compared to {rep.get('compared_to') or 'nothing (no earlier day)'}")
    print(f"Wrote {data_dir / 'threads.json'} and {data_dir / 'changes.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
