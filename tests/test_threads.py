"""
Regression tests for scripts/threads.py, run against the archived days in
data/. They encode judgments made by reading real merged output on
2026-09-27 and 2026-09-29 -- "these headlines are the same event" and "these
must NOT be merged" -- so a change to the tokenizer, lexicon, or thresholds
that breaks a known-good thread (or glues unrelated stories together) fails
loudly instead of silently reshaping the page.

    python -m unittest discover -s tests -v          # from the repo root

They read data/<date>/ only (no network, no API keys). If a data folder has
been pruned the corresponding tests are skipped rather than failing.
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import changes as C  # noqa: E402
import source_tiers  # noqa: E402
import threads as T  # noqa: E402


def _build(date):
    d = ROOT / "data" / date
    if not d.exists():
        raise unittest.SkipTest(f"data/{date} not present")
    return T.build_threads(d)


def _thread_map(res):
    """title -> thread id for every article."""
    out = {}
    for t in res["threads"]:
        for a in t["articles"]:
            out[a["title"]] = t["id"]
    return out


def _titles(res, rx):
    r = re.compile(rx, re.I)
    return [a["title"] for t in res["threads"] for a in t["articles"] if r.search(a["title"] or "")]


class MustMerge(unittest.TestCase):
    """Each entry: (date, label, title regex, minimum articles that must
    share ONE thread). Minimums are set below the observed size on purpose,
    so small wording changes in a future re-run do not flake the test."""
    CASES = [
        ("2026-09-29", "Kyiv academy strike", r"National Academy of Sciences|science academy|scientific centre", 4),
        ("2026-09-29", "Myanmar Rakhine airstrike", r"Rakhine|Myanmar", 3),
        ("2026-09-29", "RAF Fairford (spans 3 sections)", r"Fairford|UK airbase|Air Force Base Released on Bail", 5),
        ("2026-09-29", "Citrix NetScaler zero-days", r"Citrix|NetScaler", 3),
        ("2026-09-29", "Florida dengue emergency", r"dengue|Florida (officials|on alert)", 3),
        ("2026-09-29", "OpenAI training pause", r"OpenAI (pauses|to Halt)", 3),
        ("2026-09-27", "Iran Hormuz reopening offer", r"Hormuz reopening|reopen Strait of Hormuz|7-Day Hormuz|seven days if US", 5),
        ("2026-09-27", "Zelensky urged Trump on China", r"Zelensk\w+ (S|s)ays (He )?urged Trump", 3),
        ("2026-09-27", "Ethiopia civil war resumes", r"Ethiopia (Returns|as civil)|Fighting rages in Ethiopia", 2),
    ]

    def test_cases(self):
        cache = {}
        for date, label, rx, minimum in self.CASES:
            if date not in cache:
                try:
                    cache[date] = _build(date)
                except unittest.SkipTest:
                    continue
            res = cache[date]
            tmap = _thread_map(res)
            titles = _titles(res, rx)
            self.assertGreaterEqual(len(titles), minimum, f"{label}: regex matched too few articles")
            biggest = max(sum(1 for t in titles if tmap[t] == tid) for tid in {tmap[t] for t in titles})
            self.assertGreaterEqual(biggest, minimum, f"{label}: only {biggest} of {len(titles)} matched articles share a thread")


class MustNotMerge(unittest.TestCase):
    """Pairs of regexes: no single thread may contain an article matching each."""
    CASES = [
        ("2026-09-29", "Kyiv strike vs Myanmar airstrike", r"National Academy of Sciences", r"Rakhine"),
        ("2026-09-29", "Korea DMZ mines vs POW row", r"North Korean mines", r"prisoner-of-war|POWs"),
        ("2026-09-29", "Ethiopia Afar vs Yemen front line", r"Afar region", r"Yemen.s front-line|escalating war in Yemen"),
        ("2026-09-29", "UK two-state statement vs UK pandemic statement", r"two-state solution", r"pandemic prevention"),
        ("2026-09-29", "Citrix zero-days vs FBI breach", r"Citrix", r"FBI"),
        ("2026-09-27", "Bitget crypto raid vs NK irregular warfare", r"Bitget", r"Irregular Warfare"),
    ]

    def test_cases(self):
        cache = {}
        for date, label, rx_a, rx_b in self.CASES:
            if date not in cache:
                try:
                    cache[date] = _build(date)
                except unittest.SkipTest:
                    continue
            res = cache[date]
            tmap = _thread_map(res)
            ta, tb = _titles(res, rx_a), _titles(res, rx_b)
            self.assertTrue(ta and tb, f"{label}: a regex matched nothing (data changed?)")
            self.assertFalse({tmap[t] for t in ta} & {tmap[t] for t in tb}, f"{label}: merged into one thread")


class Ranking(unittest.TestCase):
    def test_hormuz_thread_outranks_state_media_rail_story(self):
        """The motivating bug: a single-source RT piece on the Chabahar rail
        project must not sit above the Hormuz confrontation."""
        res = _build("2026-09-29")
        by_title = {}
        for t in res["threads"]:
            for a in t["articles"]:
                by_title[a["title"]] = t
        rail = next(t for ti, t in by_title.items() if re.search(r"Chabahar|rail link", ti or "", re.I))
        hormuz = next(t for ti, t in by_title.items() if re.search(r"Iran touts Hormuz|Trump rejects Iran proposal", ti or "", re.I))
        self.assertGreater(hormuz["score"], rail["score"] + 10)
        self.assertFalse(rail["bluf_eligible"] and rail["top_rank"], "state-media-only thread must not lead the page")

    def test_top_developments_are_distinct_and_capped(self):
        for date in ("2026-09-27", "2026-09-29"):
            res = _build(date)
            tops = [t for t in res["threads"] if t["top_rank"]]
            self.assertLessEqual(len(tops), T.TOP_DEVELOPMENTS)
            self.assertEqual(sorted(t["top_rank"] for t in tops), list(range(1, len(tops) + 1)))
            for a in tops:
                for b in tops:
                    if a is not b:
                        self.assertLess(T.cosine(a["_centroid"], b["_centroid"]), T.STORYLINE_SIMILARITY)

    def test_myanmar_not_hidden_by_kyiv(self):
        res = _build("2026-09-29")
        titles = {a["title"] for t in res["threads"] if t["top_rank"] for a in t["articles"]}
        self.assertTrue(any("Myanmar" in (x or "") or "Rakhine" in (x or "") for x in titles))

    def test_every_article_in_exactly_one_thread(self):
        res = _build("2026-09-29")
        urls = [a["url"] for t in res["threads"] for a in t["articles"]]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(len(urls), len(res["_records"]))

    def test_deterministic(self):
        a, b = _build("2026-09-29"), _build("2026-09-29")
        self.assertEqual([(t["id"], t["score"]) for t in a["threads"]], [(t["id"], t["score"]) for t in b["threads"]])


class Sources(unittest.TestCase):
    def test_domain_and_category(self):
        self.assertEqual(source_tiers.domain_of("https://www.bbc.co.uk/news/x"), "bbc.co.uk")
        self.assertEqual(source_tiers.domain_of("https://news.un.org/en/story/1"), "un.org")
        self.assertEqual(source_tiers.category_of("https://economictimes.indiatimes.com/a"), "established")
        self.assertEqual(source_tiers.category_of("https://news.un.org/en/story/1"), "wire_primary")
        self.assertEqual(source_tiers.category_of("https://www.rt.com/x"), "state_affiliated")
        self.assertEqual(source_tiers.category_of("https://www.globalsecurity.org/x"), "aggregator")
        self.assertEqual(source_tiers.category_of("https://example-unknown.net/x"), "unclassified")

    def test_wire_origin_collapses_syndication(self):
        self.assertEqual(source_tiers.wire_origin({"description": "KYIV, Ukraine (AP) -- Strikes killed two"}), "ap")
        self.assertEqual(source_tiers.wire_origin({"description": "By Luciana Magalhaes SAO PAULO, Sept 28 (Reuters) - From the cell"}), "reuters")
        self.assertIsNone(source_tiers.wire_origin({"description": "Russia's ap proach to talks"}))

    def test_aggregators_do_not_count_as_independent(self):
        d = ROOT / "data" / "2026-09-29"
        if not d.exists():
            self.skipTest("no data")
        res = T.build_threads(d)
        for t in res["threads"]:
            origins = {a["origin"] for a in t["articles"] if a["origin"]}
            self.assertEqual(t["n_independent"], len(origins))


class Escalation(unittest.TestCase):
    def test_casualties_and_language(self):
        hi = T.escalation_score({"title": "Airstrike kills 33 people at market", "description": "Dozens wounded"})
        lo = T.escalation_score({"title": "Minister to visit trade fair", "description": "Talks on tariffs continue"})
        self.assertGreater(hi, 0.7)
        self.assertLess(lo, 0.15)

    def test_year_is_not_a_casualty_count(self):
        a = T.escalation_score({"title": "Report on 2026 budget", "description": "At least 2026 pages long"})
        self.assertLess(a, 0.5)


class Changes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = ROOT / "data" / "2026-09-29"
        if not d.exists() or not (ROOT / "data" / "2026-09-28").exists():
            raise unittest.SkipTest("need data for 2026-09-28 and 2026-09-29")
        cls.cur = T.build_threads(d)
        cls.rep = C.compute("2026-09-29", cur=cls.cur)

    def _thread(self, rx):
        r = re.compile(rx, re.I)
        return next(t for t in self.cur["threads"] if any(r.search(a["title"] or "") for a in t["articles"]))

    def test_compares_to_previous_day(self):
        self.assertEqual(self.rep["compared_to"], "2026-09-28")
        self.assertEqual(self.rep["gap_days"], 1)
        self.assertGreaterEqual(self.rep["baseline_days"], 3)

    def test_multi_day_story_is_continuing(self):
        e = self.rep["threads"][self._thread(r"OpenAI pauses")["id"]]
        self.assertEqual(e["status"], "continuing")
        self.assertGreaterEqual(e["streak"], 3)

    def test_fairford_continuing_from_previous_day(self):
        e = self.rep["threads"][self._thread(r"Fairford")["id"]]
        self.assertEqual(e["status"], "continuing")

    def test_fresh_event_is_new(self):
        e = self.rep["threads"][self._thread(r"Rakhine")["id"]]
        self.assertEqual(e["status"], "new")

    def test_arrows_need_a_baseline_and_use_it(self):
        self.assertIn(self.rep["categories"]["T01"]["arrow"], {"up"})   # Myanmar/Ethiopia/Yemen day
        self.assertEqual(self.rep["categories"]["flagship-ru-ua"]["arrow"], "flat")
        early = C.compute("2026-09-26")            # only one earlier day on disk
        self.assertTrue(all(c["arrow"] is None for c in early["categories"].values()))

    def test_first_day_has_nothing_to_compare(self):
        rep = C.compute("2026-09-25")
        self.assertIsNone(rep["compared_to"])
        self.assertEqual(rep["threads"], {})

    def test_faded_are_multi_source(self):
        for f in self.rep["faded"]:
            self.assertGreaterEqual(f["n_independent"], C.FADE_MIN_SOURCES)


if __name__ == "__main__":
    unittest.main()
