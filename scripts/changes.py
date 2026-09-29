"""
"What changed" for one day's briefing, computed from the archive already in
data/ -- no new data, no API calls.

For a given date this module rebuilds the story threads (threads.py) for that
day and for up to LOOKBACK_DAYS earlier days present on disk, then:

  * matches each of today's threads to earlier days' threads by text
    similarity (only 12-31 article URLs repeat from one day to the next, so
    URL matching would miss almost everything; the same event is reported by
    different outlets each day, so matching has to be by content)
  * labels each thread  NEW / CONTINUING (day N) / RETURNING
  * flags CONTINUING threads that are GROWING (more independent outlets, or
    sharper escalation/casualty language than the last day they appeared)
  * lists yesterday's top threads that FADED (nothing like them today)
  * gives every category an arrow: today's ranked coverage versus its own
    average over the prior days

Read the arrows as COVERAGE trends, not ground truth. They measure what the
retrieval pipeline surfaced and the red-team pass kept, which is shaped by
news-cycle volume, NewsAPI's per-topic caps and the ~24h feed delay. A quiet
news day looks like de-escalation here; a busy one looks like escalation.
The arrows are suppressed until there are MIN_BASELINE_DAYS prior days, and
`fade` means "not in today's coverage", never "resolved".

Comparison uses the days that exist on disk, not calendar days, so a gap in
the archive is handled by comparing against the most recent earlier day and
saying so (`compared_to`, `gap_days`).

CLI:  python scripts/changes.py 2026-09-29
"""
import math
import re
import sys
from collections import defaultdict
from datetime import date as _date
from pathlib import Path

import threads as T

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

LOOKBACK_DAYS = 7
MIN_BASELINE_DAYS = 3

# Centroid cosine (unified TF-IDF across the whole window) at or above which
# a thread today and a thread on an earlier day count as the same story.
# Calibrated on 2026-09-25..29: stories that clearly ran across days
# (Hormuz talks, Kyiv strikes, the FBI breach, OpenAI's pause) score
# 0.30-0.80; unrelated threads sit under 0.15 at the 99th percentile.
MATCH_THRESHOLD = 0.30

# GROWING: independent outlets up by at least this many, or escalation
# component up by at least this much, since the last day the story appeared.
GROW_SOURCES = 2
GROW_ESCALATION = 0.25

# FADED: only consider yesterday's threads at least this high in the ranking
# AND carried by at least this many independent outlets.
FADE_TOP_N = 15
FADE_MIN_SOURCES = 2
# A candidate is only called faded if it ALSO has nothing even loosely similar
# among today's top FADE_STORYLINE_TOP threads (centroid cosine below
# FADE_STORYLINE_SIM). Without this, "16 dead as Ukraine, Russia trade
# strikes" was reported as gone on a day Russian strikes on Kyiv were the top
# story -- true of that one event, misleading about the storyline. Erring
# toward silence: a wrong "faded" is worse than a missed one.
FADE_STORYLINE_TOP = 30
FADE_STORYLINE_SIM = 0.15

# The changes block only lists GROWING stories ranked at least this high.
GROWING_MAX_RANK = 25

# Category arrows: weighted activity = sum of home-thread scores / 100.
ARROW_UP_RATIO, ARROW_UP_MIN_DELTA = 1.35, 1.0
ARROW_DOWN_RATIO, ARROW_DOWN_MIN_DELTA = 0.65, 1.0

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def prior_dates(date, data_root=DATA, n=LOOKBACK_DAYS):
    """Up to n earlier dates that have topic data on disk, most recent first."""
    root = Path(data_root)
    out = []
    for p in sorted(root.iterdir(), reverse=True):
        if p.is_dir() and _DATE_RE.match(p.name) and p.name < date and any(p.glob("T*.json")):
            out.append(p.name)
        if len(out) >= n:
            break
    return out


def _days_between(a, b):
    return (_date.fromisoformat(b) - _date.fromisoformat(a)).days


def _thread_vectors(day_result, idf, default_idf):
    """Centroid vector per thread of one day, under the shared IDF."""
    recs = day_result["_records"]
    vecs = [T.tfidf_vec(r["terms"], idf, default_idf) for r in recs]
    return {t["id"]: T.centroid([vecs[i] for i in t["_idxs"]]) for t in day_result["threads"]}


def activity(day_result):
    """{topic_id: (n_home_threads, weighted_activity)}"""
    out = defaultdict(lambda: [0, 0.0])
    for t in day_result["threads"]:
        out[t["home"]][0] += 1
        out[t["home"]][1] += t["score"] / 100.0
    return {k: (v[0], round(v[1], 2)) for k, v in out.items()}


def compute(date, data_root=DATA, cur=None, match_threshold=MATCH_THRESHOLD):
    """Change report for `date`. `cur` may pass an already-built
    threads.build_threads() result for that date to avoid rebuilding it."""
    data_root = Path(data_root)
    if cur is None:
        cur = T.build_threads(data_root / date)
    empty = {"date": date, "compared_to": None, "gap_days": None, "baseline_days": 0,
             "threads": {}, "categories": {}, "new": [], "growing": [], "faded": [],
             "returning": []}
    if not cur["threads"]:
        return empty
    priors = prior_dates(date, data_root)
    if not priors:
        return empty

    prior_results = {d: T.build_threads(data_root / d) for d in priors}

    # One IDF over the entire window so cross-day cosines are comparable.
    all_terms = [r["terms"] for r in cur["_records"]]
    for d in priors:
        all_terms.extend(r["terms"] for r in prior_results[d]["_records"])
    idf, n_docs = T.build_idf(all_terms)
    default_idf = math.log(n_docs + 1) + 1.0

    cur_vec = _thread_vectors(cur, idf, default_idf)
    prior_vec = {d: _thread_vectors(prior_results[d], idf, default_idf) for d in priors}
    prior_by_id = {d: {t["id"]: t for t in prior_results[d]["threads"]} for d in priors}

    yesterday = priors[0]
    per_thread = {}
    for t in cur["threads"]:
        matches = {}  # date -> (prior thread id, sim)
        for d in priors:
            best_id, best = None, 0.0
            for pid, pv in prior_vec[d].items():
                s = T.cosine(cur_vec[t["id"]], pv)
                if s > best:
                    best_id, best = pid, s
            if best_id is not None and best >= match_threshold:
                matches[d] = (best_id, round(best, 3))

        days_seen = sorted(matches)
        in_last = yesterday in matches
        # consecutive run ending at the most recent prior day
        streak = 0
        if in_last:
            for d in priors:
                if d in matches:
                    streak += 1
                else:
                    break
        status = "continuing" if in_last else ("returning" if matches else "new")
        entry = {"status": status, "days_seen": len(days_seen) + 1, "streak": streak + 1 if in_last else 1,
                 "first_seen": days_seen[0] if days_seen else date, "growing": False, "prev": None}
        if in_last:
            pid, sim = matches[yesterday]
            prev = prior_by_id[yesterday][pid]
            d_src = t["n_independent"] - prev["n_independent"]
            d_esc = t["components"]["escalation"] - prev["components"]["escalation"]
            entry["prev"] = {"date": yesterday, "id": pid, "sim": sim, "score": prev["score"],
                             "n_independent": prev["n_independent"]}
            entry["growing"] = d_src >= GROW_SOURCES or d_esc >= GROW_ESCALATION
            entry["delta_sources"], entry["delta_escalation"] = d_src, round(d_esc, 3)
        per_thread[t["id"]] = entry

    # FADED: yesterday's top threads with no counterpart today.
    faded = []
    for pt in prior_results[yesterday]["threads"][:FADE_TOP_N]:
        if pt["n_independent"] < FADE_MIN_SOURCES:
            continue  # a one-source story dropping out is noise, not a change
        pv = prior_vec[yesterday][pt["id"]]
        best = max((T.cosine(pv, cv) for cv in cur_vec.values()), default=0.0)
        near_top = max((T.cosine(pv, cur_vec[t["id"]]) for t in cur["threads"][:FADE_STORYLINE_TOP]), default=0.0)
        if best < match_threshold and near_top < FADE_STORYLINE_SIM:
            faded.append({"date": yesterday, "id": pt["id"], "title": pt["lead_title"], "url": pt["lead_url"],
                          "score": pt["score"], "home": pt["home"], "n_independent": pt["n_independent"]})

    # Category arrows vs. mean over the prior days present.
    cur_act = activity(cur)
    prior_acts = [activity(prior_results[d]) for d in priors]
    categories = {}
    for tid, _label, _sev, _fl in T.section_ids():
        n_today, w_today = cur_act.get(tid, (0, 0.0))
        base_ws = [a.get(tid, (0, 0.0))[1] for a in prior_acts]
        base = sum(base_ws) / len(base_ws)
        arrow = None
        if len(priors) >= MIN_BASELINE_DAYS:
            if w_today >= base * ARROW_UP_RATIO and w_today - base >= ARROW_UP_MIN_DELTA:
                arrow = "up"
            elif w_today <= base * ARROW_DOWN_RATIO and base - w_today >= ARROW_DOWN_MIN_DELTA:
                arrow = "down"
            else:
                arrow = "flat"
        categories[tid] = {"arrow": arrow, "today": w_today, "baseline": round(base, 2),
                           "threads_today": n_today, "baseline_days": len(priors)}

    ranked = [t["id"] for t in cur["threads"]]
    return {
        "date": date, "compared_to": yesterday, "gap_days": _days_between(yesterday, date),
        "baseline_days": len(priors), "match_threshold": match_threshold,
        "threads": per_thread, "categories": categories, "faded": faded,
        "new": [i for i in ranked if per_thread[i]["status"] == "new"],
        "returning": [i for i in ranked if per_thread[i]["status"] == "returning"],
        "growing": [t["id"] for t in cur["threads"]
                    if per_thread[t["id"]]["growing"] and t["rank"] <= GROWING_MAX_RANK],
    }


def _cli(argv):
    if not argv:
        print(__doc__.split("CLI:")[-1])
        return 2
    date = argv[0]
    cur = T.build_threads(DATA / date)
    rep = compute(date, cur=cur)
    if not rep["compared_to"]:
        print("no earlier days on disk -- nothing to compare")
        return 0
    by_id = {t["id"]: t for t in cur["threads"]}
    print(f"{date} vs {rep['compared_to']} (gap {rep['gap_days']}d, {rep['baseline_days']} baseline days)")
    print(f"new={len(rep['new'])} returning={len(rep['returning'])} growing={len(rep['growing'])} faded={len(rep['faded'])}")
    print("\nTop 15 threads:")
    for t in cur["threads"][:15]:
        e = rep["threads"][t["id"]]
        tag = e["status"].upper() + (f" d{e['streak']}" if e["status"] == "continuing" else "")
        tag += " GROWING" if e["growing"] else ""
        sim = f" sim={e['prev']['sim']}" if e["prev"] else ""
        print(f"  {t['rank']:2d} {t['score']:5.1f} {tag:22s}{sim:11s} {t['lead_title'][:70]}")
    print("\nFaded from yesterday's top:")
    for f in rep["faded"]:
        print(f"  {f['score']:5.1f} [{f['home']}] {f['title'][:80]}")
    print("\nCategory arrows:")
    labels = cur["labels"]
    for tid, c in rep["categories"].items():
        print(f"  {c['arrow'] or '-':5s} {labels[tid][:36]:37s} today={c['today']:4.1f} avg={c['baseline']:4.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
