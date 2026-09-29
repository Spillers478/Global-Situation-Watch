"""
Story THREADS and importance RANKING for one day's briefing.

Why this exists
---------------
The pipeline already groups same-event articles *within* a topic
(dedupe_stories.py -> "+N other sources"), and redteam.py already decides
which topic each article belongs to. Two things were still missing:

1. Nothing ranked stories by importance. Sections were ordered "trusted
   wire first, then feed order", and the BLUF was written from headlines in
   fixed topic order, so a single-source RT story about a rail project could
   lead the Middle East section over an active Hormuz confrontation.
2. Every topic was deduped in isolation, so ONE event (e.g. the RAF Fairford
   arrests) rendered in four sections with four different article lists, and
   each section's count treated it as four stories.

This module fixes both, deterministically -- no API calls, no new
dependencies, and the same input always gives the same output, so it can be
tested offline and replayed against archived days.

Pipeline
--------
load_day()          every displayed article across all topics (post red-team),
                    exactly what build_brief.load_articles() would show.
build_threads()     1. seed clusters from dedupe_stories.py groups (if present)
                    2. average-linkage agglomerative merge on TF-IDF cosine,
                       ACROSS topics, up to MERGE_THRESHOLD
                    3. score every resulting thread (see RANK_WEIGHTS)
                    4. pick each thread's HOME topic (most articles; ties go
                       to flagships, then higher severity)

A thread that spans topics renders once in full at its home section and as a
one-line cross-reference in the others, so tripwire sections still show
that they have activity without repeating the whole story. Only home
threads count toward a section's story count.

Scoring
-------
score = 100 * (w_corr*corroboration + w_qual*source_quality + w_rec*recency
               + w_kw*escalation + w_sev*topic_severity)

  corroboration   distinct INDEPENDENT origins, log-scaled, saturating at 6.
                  Aggregators/republishers don't count, and syndicated copies
                  collapse to their origin wire (ten outlets running one AP
                  story are one source). See source_tiers.py.
  source_quality  best tier in the thread (60%) + mean of top three (40%).
  recency         half-life of RECENCY_HALF_LIFE_H hours from the newest
                  article, measured against the newest article in the day's
                  data (not wall-clock), so replaying an old day is stable.
  escalation      casualty / violence / emergency vocabulary in the best
                  article's title+description, plus a bonus for stated
                  casualty figures.
  topic_severity  max `severity` (topics.py) among the thread's topics.

These are judgment calls, not measurements. Every weight and lexicon entry
is in this file's constants; change them here and nothing else.

BLUF eligibility: a thread is eligible for the "Top developments" block
only if its best source tier is at least `established` (quality >=
BLUF_MIN_QUALITY). A story carried only by state-affiliated, advocacy or
aggregator sites can still rank inside its section but does not lead the
page. This is about who is reporting, not what they say.

CLI
---
  python scripts/threads.py 2026-09-29                # ranked summary
  python scripts/threads.py 2026-09-29 --write        # data/<date>/threads.json
  python scripts/threads.py 2026-09-29 --unclassified # outlets missing from source_tiers.py
"""
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import source_tiers
from topics import TIER1, TOPICS

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
RANK_WEIGHTS = {
    "corroboration": 0.30,
    "quality": 0.20,
    "recency": 0.15,
    "escalation": 0.25,
    "severity": 0.10,
}
RECENCY_HALF_LIFE_H = 18.0
CORROBORATION_SATURATION = 6      # independent origins at which corroboration = 1.0
BLUF_MIN_QUALITY = 0.70           # see "BLUF eligibility" above
TOP_DEVELOPMENTS = 5

# Average-linkage cosine at or above which two clusters become one thread.
# Tuned on data/2026-09-25..29 (see tests/test_threads.py for the regression
# cases). Lower merges more aggressively; if you see unrelated stories glued
# together, raise it.
MERGE_THRESHOLD = 0.24
MAX_THREAD_ARTICLES = 30          # safety valve against runaway chaining
# Centroid cosine at or above which two THREADS count as the same storyline
# when filling the top-developments list. Measured on 2026-09-27: facets of
# the Hormuz standoff sit at 0.17-0.46; unrelated top threads are 0.09 at
# the 95th percentile and 0.20 at the 99th.
STORYLINE_SIMILARITY = 0.17
DEFAULT_SEVERITY = 8              # flagships carry no severity in topics.py

# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------
_STOP = set("""
a an the and or but of to in on at for from by with as is are was were be been being it its
this that these those he she they them his her their we you i not no nor so than then too
very can will just do does did has have had over after before into out up down about above
below between under again more most other some such only own same said says say also one two
three per via amid while when where which who whom whose why how what if against during
through until because could would should may might must new now still yet even much many
monday tuesday wednesday thursday friday saturday sunday sept september oct october jan
january news report reports reported according officials official told say saying read full
article here there their our your all any both each few get got getting make made ago last
next first second week weeks month months year years day days today yesterday tomorrow
statement statements ambassador deputy permanent representative remarks speech meeting meetings
""".split())

_PHRASES = [
    (re.compile(r"\bair\s+base\b"), "airbase"),
    (re.compile(r"\bwar\s*planes?\b"), "warplane"),
    (re.compile(r"\bu\.s\.?\b"), "us"),
    (re.compile(r"\bu\.k\.?\b"), "uk"),
    (re.compile(r"\bstrait of hormuz\b"), "hormuz"),
    (re.compile(r"\bnational academy of sciences\b"), "academy"),
]
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_KEEP_SHORT = {"us", "uk", "un", "eu", "ai", "ru", "ir"}


def _stem(t):
    if len(t) > 6 and t.endswith("ing"):
        return t[:-3]
    if len(t) > 5 and t.endswith("ed"):
        return t[:-2]
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def tokenize(text):
    s = (text or "").lower()
    for rx, rep in _PHRASES:
        s = rx.sub(rep, s)
    out = []
    for t in _TOKEN_RE.findall(s):
        if t in _STOP or t.isdigit():
            continue
        if len(t) < 3 and t not in _KEEP_SHORT:
            continue
        out.append(_stem(t))
    return out


# Generic "something violent happened" vocabulary. It is what the escalation
# score reads, but it is deliberately EXCLUDED from the similarity vectors:
# "strike", "kill", "attack", "dozens" appear in unrelated events on the same
# day, and measured on 2026-09-29 they made a Kyiv drone strike look related
# to a Myanmar airstrike. Event identity lives in the names and places
# (Kyiv, Rakhine, Fairford, Hormuz), which these words drown out.
_GENERIC_ACTION = {
    "kill", "dead", "death", "casualty", "wounded", "injur", "missing", "airstrike", "strike",
    "attack", "bomb", "explosion", "drone", "intercept", "shelling", "offensive", "warplane",
    "clash", "fight", "arrest", "terror", "emergency", "shot", "escalat", "displac", "dozen",
    "least", "people", "person", "hit", "hits", "target", "civilian", "injured", "leave",
    "left", "wound", "rescued", "rescue",
}


def doc_terms(article):
    """Title counted twice, description once, generic action words removed
    (see _GENERIC_ACTION). Content is skipped: on the free tiers it is a
    ~200-char stub that mostly repeats the description."""
    c = Counter()
    for t in tokenize(article.get("title")):
        if t not in _GENERIC_ACTION:
            c[t] += 2
    for t in tokenize(article.get("description")):
        if t not in _GENERIC_ACTION:
            c[t] += 1
    return c


def build_idf(term_counters):
    n = len(term_counters)
    df = Counter()
    for c in term_counters:
        df.update(c.keys())
    return {t: math.log((n + 1) / (d + 1)) + 1.0 for t, d in df.items()}, n


def tfidf_vec(counter, idf, default_idf):
    v = {t: (1.0 + math.log(c)) * idf.get(t, default_idf) for t, c in counter.items()}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {t: x / norm for t, x in v.items()}


def cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(t, 0.0) for t, w in a.items())


def centroid(vecs):
    acc = defaultdict(float)
    for v in vecs:
        for t, w in v.items():
            acc[t] += w
    norm = math.sqrt(sum(x * x for x in acc.values())) or 1.0
    return {t: x / norm for t, x in acc.items()}


# ---------------------------------------------------------------------------
# Escalation vocabulary. Matched against STEMMED tokens (see _stem) plus a few
# literal phrases. Weights are rough severity of language, not of event.
# ---------------------------------------------------------------------------
ESCALATION_TERMS = {
    # loss of life / physical harm
    "kill": 1.0, "dead": 1.0, "death": 1.0, "casualty": 1.0, "massacre": 1.0, "wounded": 0.8,
    "injur": 0.6, "missing": 0.5, "killed": 1.0,
    # kinetic action
    "airstrike": 1.0, "missile": 0.9, "explosion": 0.9, "invasion": 1.0, "shelling": 0.9,
    "bomb": 0.8, "intercept": 0.7, "drone": 0.5, "strike": 0.6, "attack": 0.7,
    "offensive": 0.7, "blockade": 0.8, "warplane": 0.5, "ambush": 0.8, "seiz": 0.5,
    "clash": 0.6, "fight": 0.5,
    # security / political shocks
    "coup": 1.0, "hostage": 1.0, "kidnap": 1.0, "terror": 0.8, "arrest": 0.4, "plot": 0.5,
    "assassin": 1.0, "martial": 0.8, "unrest": 0.6, "uprising": 0.6, "evacuat": 0.8,
    # emergencies
    "emergency": 0.7, "outbreak": 0.8, "ebola": 0.8, "flood": 0.5, "earthquake": 0.7,
    "wildfire": 0.5, "famine": 0.8, "displac": 0.5,
    # cyber
    "zeroday": 0.9, "exploit": 0.7, "breach": 0.7, "ransomware": 0.7, "botnet": 0.5,
    "blackout": 0.7,
    # WMD
    "nuclear": 0.6, "chemical": 0.6, "biolog": 0.6,
    # escalation framing
    "escalat": 0.6, "ceasefire": 0.4, "mobiliz": 0.6, "mobilis": 0.6, "shot": 0.4,
}
# Formats that are ABOUT events rather than reports of one: transcripts,
# roundups, recaps, opinion labels. Such an article is never chosen as a
# thread's lead if an ordinary report exists, and a thread led by one takes
# NON_EVENT_PENALTY off its score. Deliberately narrow (explicit format
# markers only) -- guessing "analysis" from tone would misfire on real news.
NON_EVENT_RE = re.compile(
    r"\btranscript\b|\bmorning rundown\b|\bnewswrap\b|\bweekly recap\b|\bthis week in\b|"
    r"\bnew book claims\b|^\s*(opinion|analysis|commentary|editorial|podcast|book review|explainer)\b",
    re.I,
)
NON_EVENT_PENALTY = 0.85
_ZERODAY_RE = re.compile(r"zero[\s-]?day", re.I)
_CASUALTY_RE = re.compile(
    r"\b(\d[\d,]{0,8})\s+(?:\w+\s+){0,3}?(?:killed|dead|died|wounded|injured|missing|displaced)\b"
    r"|\b(?:kill(?:ed|s|ing)|left|leav(?:e|es|ing))\s+(?:at\s+least\s+)?(\d[\d,]{0,8})\b"
    r"|\bat\s+least\s+(\d[\d,]{0,8})\b",
    re.I,
)


def escalation_score(article):
    """0..1. Language severity in title (full) + description (half), plus a
    bonus for a stated casualty figure."""
    title = article.get("title") or ""
    desc = article.get("description") or ""
    seen_t = set(tokenize(title))
    seen_d = set(tokenize(desc))
    if _ZERODAY_RE.search(title + " " + desc):
        seen_t.add("zeroday")
    s = 0.0
    for term, w in ESCALATION_TERMS.items():
        if term in seen_t:
            s += w
        elif term in seen_d:
            s += 0.5 * w
    base = 1.0 - math.exp(-s / 2.0)
    bonus = 0.0
    for m in _CASUALTY_RE.finditer(title + " . " + desc):
        raw = next((g for g in m.groups() if g), None)
        if not raw:
            continue
        try:
            n = int(raw.replace(",", ""))
        except ValueError:
            continue
        if 0 < n < 5_000_000:  # ignore years-as-numbers and money-sized figures
            bonus = max(bonus, min(0.4, math.log10(n + 1) / 3.0))
    return min(1.0, base + bonus)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def section_ids():
    """(topic_id, label, severity) for every section that renders threads --
    flagships + taxonomy, excluding the '-us-lens' sidebars."""
    out = []
    for t in TIER1:
        out.append((t["id"], t["label"], t.get("severity", DEFAULT_SEVERITY), True))
    for t in TOPICS:
        out.append((t["id"], t["label"], t.get("severity", DEFAULT_SEVERITY), False))
    return out


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_day(data_dir):
    """Every displayed article across all topic sections, as records.
    Reuses build_brief.load_articles so the corpus is identical to what the
    page shows (red-team output preferred, raw fallback, fully-filtered topic
    = empty). build_brief is imported lazily: it imports this module."""
    import build_brief
    records, seen_urls = [], set()
    for tid, _label, _sev, _fl in section_ids():
        for a in build_brief.dedupe(build_brief.load_articles(data_dir, tid)):
            url = a.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            records.append(_make_record(len(records), tid, a))
    return records


def _make_record(idx, topic_id, article):
    url = article.get("url") or ""
    category = source_tiers.category_of(url)
    wire = source_tiers.wire_origin(article)
    if wire:
        # Text credits a wire: the story's provenance IS that wire, whichever
        # site republished it. Count it as one wire source at full weight.
        origin, eff_cat = f"wire:{wire}", "wire_primary"
    elif category in source_tiers.NOT_INDEPENDENT:
        origin, eff_cat = None, category
    else:
        origin, eff_cat = source_tiers.domain_of(url), category
    return {
        "idx": idx, "topic": topic_id, "article": article, "terms": doc_terms(article),
        "domain": source_tiers.domain_of(url), "category": eff_cat, "origin": origin,
        "ts": parse_ts(article.get("publishedAt")),
        "esc": escalation_score(article),
        "non_event": bool(NON_EVENT_RE.search(article.get("title") or "")),
    }


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------
def _seed_groups(records, data_dir):
    """Union-find seeds from dedupe_stories.py output (same-topic groups the
    LLM already judged to be one event) plus identical-title duplicates."""
    parent = list(range(len(records)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_url = {r["article"].get("url"): r for r in records}
    group_key = {}  # record idx -> (topic, letter)
    dedup_dir = data_dir / "dedup"
    if dedup_dir.exists():
        first_in_group = {}
        for tid, _l, _s, _f in section_ids():
            p = dedup_dir / f"{tid}.json"
            if not p.exists():
                continue
            try:
                with open(p) as fh:
                    groups = (json.load(fh).get("groups")) or {}
            except (OSError, ValueError):
                continue
            for url, letter in groups.items():
                r = by_url.get(url)
                if r is None or r["topic"] != tid:
                    continue
                group_key[r["idx"]] = (tid, letter)
                k = (tid, letter)
                if k in first_in_group:
                    union(first_in_group[k], r["idx"])
                else:
                    first_in_group[k] = r["idx"]

    first_title = {}
    for r in records:
        t = re.sub(r"\W+", " ", (r["article"].get("title") or "").lower()).strip()
        if len(t) < 25:
            continue
        if t in first_title:
            union(first_title[t], r["idx"])
        else:
            first_title[t] = r["idx"]

    clusters = defaultdict(list)
    for r in records:
        clusters[find(r["idx"])].append(r["idx"])
    return list(clusters.values()), group_key


def cluster(records, vecs, seeds, threshold=MERGE_THRESHOLD, max_size=MAX_THREAD_ARTICLES):
    """Average-linkage agglomerative merge. Average (not single) linkage on
    purpose: single linkage chains -- one article that mentions both Iran
    and Ukraine would glue two unrelated stories together."""
    n_c = len(seeds)
    members = {i: list(m) for i, m in enumerate(seeds)}
    # pairwise sums of similarity between clusters
    sim_sum = defaultdict(dict)
    idx_lists = list(members.items())
    for a in range(len(idx_lists)):
        ca, ma = idx_lists[a]
        for b in range(a + 1, len(idx_lists)):
            cb, mb = idx_lists[b]
            s = 0.0
            for i in ma:
                vi = vecs[i]
                for j in mb:
                    s += cosine(vi, vecs[j])
            sim_sum[ca][cb] = s
            sim_sum[cb][ca] = s

    active = set(members)
    while True:
        best, best_pair = threshold, None
        for a in active:
            na = len(members[a])
            for b, s in sim_sum[a].items():
                if b <= a or b not in active:
                    continue
                if na + len(members[b]) > max_size:
                    continue
                avg = s / (na * len(members[b]))
                if avg > best or (avg == best and best_pair and (a, b) < best_pair):
                    best, best_pair = avg, (a, b)
        if best_pair is None:
            break
        a, b = best_pair
        members[a].extend(members[b])
        for k in list(sim_sum[b].keys()):
            if k in (a, b):
                continue
            sim_sum[a][k] = sim_sum[a].get(k, 0.0) + sim_sum[b][k]
            sim_sum[k][a] = sim_sum[a][k]
            del sim_sum[k][b]
        sim_sum[a].pop(b, None)
        del sim_sum[b]
        del members[b]
        active.discard(b)
    return list(members.values())


# ---------------------------------------------------------------------------
# Thread construction + scoring
# ---------------------------------------------------------------------------
def _article_public(r):
    a = r["article"]
    return {
        "topic": r["topic"], "url": a.get("url"), "title": a.get("title"),
        "description": (a.get("description") or "")[:300],
        "source": (a.get("source") or {}).get("name"),
        "publishedAt": a.get("publishedAt"), "domain": r["domain"],
        "category": r["category"], "origin": r["origin"],
    }


def score_thread(recs, ref_ts, severity_by_topic, lead=None):
    origins = {}
    for r in recs:
        if r["origin"]:
            w = source_tiers.tier_weight(r["category"])
            origins[r["origin"]] = max(origins.get(r["origin"], 0.0), w)
    n_ind = len(origins)
    corr = min(1.0, math.log2(1 + n_ind) / math.log2(1 + CORROBORATION_SATURATION)) if n_ind else 0.0

    weights = sorted(origins.values(), reverse=True) or [source_tiers.tier_weight("aggregator")]
    best = weights[0]
    top3 = sum(weights[:3]) / min(3, len(weights))
    quality = 0.6 * best + 0.4 * top3

    latest = max((r["ts"] for r in recs if r["ts"]), default=None)
    if latest is not None and ref_ts is not None:
        age_h = max(0.0, (ref_ts - latest).total_seconds() / 3600.0)
        recency = 0.5 ** (age_h / RECENCY_HALF_LIFE_H)
    else:
        recency = 0.5

    esc = max((r["esc"] for r in recs), default=0.0)
    sev = max(severity_by_topic.get(r["topic"], DEFAULT_SEVERITY) for r in recs) / 10.0

    w = RANK_WEIGHTS
    score = 100.0 * (w["corroboration"] * corr + w["quality"] * quality + w["recency"] * recency
                     + w["escalation"] * esc + w["severity"] * sev)
    if lead is not None and lead["non_event"]:
        score *= NON_EVENT_PENALTY
    return {
        "score": round(score, 1),
        "n_independent": n_ind, "best_tier_weight": round(best, 2),
        "components": {"corroboration": round(corr, 3), "quality": round(quality, 3),
                       "recency": round(recency, 3), "escalation": round(esc, 3),
                       "severity": round(sev, 3)},
        "latest": latest.isoformat() if latest else None,
    }


def _pick_home(topic_counts, flagship_ids, severity_by_topic):
    return sorted(
        topic_counts,
        key=lambda t: (-topic_counts[t], t not in flagship_ids, -severity_by_topic.get(t, 0), t),
    )[0]


def _pick_lead(recs):
    return sorted(
        recs,
        key=lambda r: (r["non_event"], -source_tiers.tier_weight(r["category"]),
                       -(r["ts"].timestamp() if r["ts"] else 0), r["idx"]),
    )[0]


def build_threads(data_dir, records=None, threshold=MERGE_THRESHOLD):
    """Returns {"date", "threads": [...ranked...], "sections": {topic_id: {...}}}.
    Threads are sorted by score desc (deterministic tie-breaks)."""
    data_dir = Path(data_dir)
    if records is None:
        records = load_day(data_dir)
    secs = section_ids()
    severity = {tid: sev for tid, _l, sev, _f in secs}
    flagships = {tid for tid, _l, _s, fl in secs if fl}
    labels = {tid: lab for tid, lab, _s, _f in secs}

    if not records:
        return {"date": data_dir.name, "threads": [], "sections": {}, "labels": labels}

    idf, n_docs = build_idf([r["terms"] for r in records])
    default_idf = math.log(n_docs + 1) + 1.0
    vecs = [tfidf_vec(r["terms"], idf, default_idf) for r in records]

    seeds, group_key = _seed_groups(records, data_dir)
    merged = cluster(records, vecs, seeds, threshold=threshold)

    ref_ts = max((r["ts"] for r in records if r["ts"]), default=None)

    contradictions = _load_contradictions(data_dir)
    threads = []
    for idxs in merged:
        recs = [records[i] for i in idxs]
        topic_counts = Counter(r["topic"] for r in recs)
        home = _pick_home(topic_counts, flagships, severity)
        lead = _pick_lead(recs)
        sc = score_thread(recs, ref_ts, severity, lead=lead)
        sig = hashlib.sha1(min(r["article"]["url"] for r in recs).encode()).hexdigest()[:8]
        ordered = sorted(
            recs, key=lambda r: (r["article"]["url"] != lead["article"]["url"], r["non_event"],
                                 -source_tiers.tier_weight(r["category"]),
                                 -(r["ts"].timestamp() if r["ts"] else 0), r["idx"]))
        member_groups = {group_key[i] for i in idxs if i in group_key}
        th_contra = [c for (tid, letter) in member_groups
                     for c in contradictions.get((tid, letter), [])]
        threads.append({
            "id": f"th-{sig}",
            "home": home,
            "topics": dict(topic_counts),
            "n_articles": len(recs),
            "lead_url": lead["article"]["url"],
            "lead_title": lead["article"].get("title"),
            "lead_source": (lead["article"].get("source") or {}).get("name"),
            "bluf_eligible": sc["best_tier_weight"] >= BLUF_MIN_QUALITY,
            "articles": [_article_public(r) for r in ordered],
            "contradictions": th_contra,
            "_idxs": idxs,
            "_ordered": [r["idx"] for r in ordered],
            **sc,
        })

    threads.sort(key=lambda t: (-t["score"], -t["n_independent"], t["lead_url"]))
    for rank, t in enumerate(threads, 1):
        t["rank"] = rank
    select_top_developments(threads, vecs)

    sections = {}
    for tid, _l, _s, _f in secs:
        homed = [t["id"] for t in threads if t["home"] == tid]
        linked = [t["id"] for t in threads if t["home"] != tid and tid in t["topics"]]
        sections[tid] = {"home": homed, "linked": linked}

    return {"date": data_dir.name, "threads": threads, "sections": sections, "labels": labels,
            "_records": records, "_vecs": vecs, "_idf": idf}


def select_top_developments(threads, vecs, k=TOP_DEVELOPMENTS, sim=STORYLINE_SIMILARITY):
    """Marks up to k BLUF-eligible threads with t["top_rank"] (1..k), highest
    score first, skipping any candidate whose centroid is too similar to a
    thread already chosen. One big storyline (say, the Hormuz standoff)
    usually splits into several high-scoring threads -- talks, casualties,
    oil prices -- and without this rule they would fill every slot. Skipped
    threads are recorded on the chosen one as t["related"] so they are
    still visible ("+3 related threads"), not lost.

    Every thread gets `top_rank` (None if not chosen) and `related` (list of
    thread ids; empty unless chosen)."""
    for t in threads:
        t["top_rank"], t["related"] = None, []
        t["_centroid"] = centroid([vecs[i] for i in t["_idxs"]])
    chosen = []
    for t in threads:
        if not t["bluf_eligible"]:
            continue
        twin = next((c for c in chosen if cosine(t["_centroid"], c["_centroid"]) >= sim), None)
        if twin is not None:
            twin["related"].append(t["id"])
            continue
        if len(chosen) < k:
            chosen.append(t)
            t["top_rank"] = len(chosen)
    return chosen


def _load_contradictions(data_dir):
    out = defaultdict(list)
    d = data_dir / "dedup"
    if not d.exists():
        return out
    for tid, _l, _s, _f in section_ids():
        p = d / f"{tid}.json"
        if not p.exists():
            continue
        try:
            with open(p) as fh:
                contradictions = json.load(fh).get("contradictions") or []
        except (OSError, ValueError):
            continue
        for c in contradictions:
            out[(tid, c.get("group"))].append(c)
    return out


def public_view(result):
    """JSON-safe copy (drops in-memory vectors and record refs)."""
    return {
        "date": result["date"],
        "labels": result["labels"],
        "sections": result["sections"],
        "threads": [{k: v for k, v in t.items() if not k.startswith("_")} for t in result["threads"]],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__.split("CLI")[-1])
        return 2
    date = argv[0]
    data_dir = DATA / date
    if not data_dir.exists():
        print(f"no data for {date}", file=sys.stderr)
        return 1
    res = build_threads(data_dir)
    if "--unclassified" in argv:
        c = Counter(r["domain"] for r in res["_records"] if r["category"] == "unclassified")
        for dom, n in c.most_common():
            print(f"{n:3d}  {dom}")
        return 0
    if "--write" in argv:
        out = data_dir / "threads.json"
        with open(out, "w") as f:
            json.dump(public_view(res), f, indent=1)
        print(f"wrote {out}")
    ts = res["threads"]
    multi = sum(1 for t in ts if len(t["topics"]) > 1)
    print(f"{date}: {len(res['_records'])} articles -> {len(ts)} threads "
          f"({multi} span multiple topics)")
    for t in ts[:25]:
        tp = "+".join(f"{k}:{v}" for k, v in sorted(t["topics"].items()))
        flag = "*" if t["top_rank"] else " "
        print(f"{flag}{t['rank']:3d} {t['score']:5.1f} n={t['n_articles']:2d} ind={t['n_independent']} "
              f"[{t['home']}] ({tp}) {t['lead_title'][:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
