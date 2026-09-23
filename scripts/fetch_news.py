"""
Fetches today's article set from NewsAPI for the two Tier 1 flagship
topics plus the full 14-topic taxonomy sweep, and writes raw JSON to
data/<date>/.

Requires NEWSAPI_KEY in the environment. Never hardcode the key -- in
GitHub Actions it is supplied via repo secrets (see README).

Request budget: ~18 requests/run (2 flagship /everything + 2 flagship
US-lens top-headlines + 14 topic sweeps), well under the Developer
plan's 100/day cap. Set TOPIC_SWEEP_ENABLED to False in topics.py to
scope back down to flagships-only (4 requests/run) if needed.

Noise control: every /everything call excludes the domains in
EXCLUDE_DOMAINS (topics.py), and any topic that sets "search_in" in its
dict restricts NewsAPI's keyword matching to the article title only
(via the `searchIn` param) instead of title+description+content. Both
are configured per-topic in topics.py, not here -- see that file's
"Noise-control tools" section for what each one is for and why.
"""
import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from topics import TIER1, TOPICS, TOPIC_SWEEP_ENABLED, EXCLUDE_DOMAINS

API_KEY = os.environ.get("NEWSAPI_KEY")
BASE_URL = "https://newsapi.org/v2"
PAGE_SIZE = 100
REQUEST_TIMEOUT = 15
REQUEST_PAUSE_SECONDS = 1.0

# Hard safety ceiling so a bug (e.g. an accidental extra scheduled run)
# can never silently blow through the daily 100-request cap. Raise this
# deliberately in topics.py-driven changes, not by accident.
MAX_REQUESTS = 40

_request_count = 0


def _get(endpoint, params):
    global _request_count
    if _request_count >= MAX_REQUESTS:
        raise RuntimeError(
            f"Refusing to exceed the safety ceiling of {MAX_REQUESTS} requests "
            f"in a single run ({_request_count} used). Check topics.py / cadence "
            f"before raising MAX_REQUESTS."
        )
    if not API_KEY:
        raise RuntimeError(
            "NEWSAPI_KEY is not set in the environment. Locally: export it "
            "before running. In GitHub Actions: add it as a repo secret."
        )
    params = {**params, "apiKey": API_KEY}
    resp = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=REQUEST_TIMEOUT)
    _request_count += 1
    try:
        data = resp.json()
    except ValueError:
        print(f"  ERROR: non-JSON response from {endpoint} (HTTP {resp.status_code})", file=sys.stderr)
        return {"status": "error", "articles": []}
    if data.get("status") != "ok":
        print(f"  WARNING: {endpoint} returned {data.get('code')}: {data.get('message')}", file=sys.stderr)
    return data


def fetch_everything(query, from_date, to_date, search_in=None):
    params = {
        "q": query,
        "from": from_date,
        "to": to_date,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": PAGE_SIZE,
        "excludeDomains": EXCLUDE_DOMAINS,
    }
    if search_in:
        # Restricts q-matching to the given field(s) (e.g. "title") instead
        # of title+description+content -- see topics.py "Noise-control
        # tools" for why specific topics opt into this.
        params["searchIn"] = search_in
    return _get("everything", params)


def fetch_us_lens(query):
    return _get("top-headlines", {
        "country": "us",
        "q": query,
        "pageSize": PAGE_SIZE,
    })


def main():
    now = datetime.now(timezone.utc)
    # Articles carry roughly a 24h delay on the Developer plan, so anchor
    # the search window a couple of days back rather than "today" -- an
    # exact "today" window is often thin or empty.
    to_date = now.strftime("%Y-%m-%d")
    from_date = (now - timedelta(days=2)).strftime("%Y-%m-%d")

    out_dir = Path(__file__).resolve().parent.parent / "data" / now.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)

    results = {}

    for flagship in TIER1:
        print(f"Fetching flagship: {flagship['label']}")
        results[flagship["id"]] = fetch_everything(
            flagship["query"], from_date, to_date, search_in=flagship.get("search_in")
        )
        time.sleep(REQUEST_PAUSE_SECONDS)
        # us_lens_query is optional -- not every flagship has a meaningful
        # "US media attention" angle (T15 Disaster/Access Denial doesn't;
        # it's not a foreign-policy story the way Russia/Ukraine or
        # Iran/Israel are). Skip the -us-lens fetch entirely when absent
        # rather than requiring every flagship to define one.
        if flagship.get("us_lens_query"):
            results[f"{flagship['id']}-us-lens"] = fetch_us_lens(flagship["us_lens_query"])
            time.sleep(REQUEST_PAUSE_SECONDS)

    if TOPIC_SWEEP_ENABLED:
        for topic in TOPICS:
            print(f"Fetching topic: {topic['id']} - {topic['label']}")
            results[topic["id"]] = fetch_everything(
                topic["query"], from_date, to_date, search_in=topic.get("search_in")
            )
            time.sleep(REQUEST_PAUSE_SECONDS)
    else:
        print("Topic sweep disabled (TOPIC_SWEEP_ENABLED=False in topics.py) -- flagships only.")

    for key, payload in results.items():
        with open(out_dir / f"{key}.json", "w") as f:
            json.dump(payload, f, indent=2)

    print(f"\nDone. {_request_count} requests used. Raw data written to {out_dir}/")


if __name__ == "__main__":
    main()
