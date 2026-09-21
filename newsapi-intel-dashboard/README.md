# Global Situation Watch

A daily, automated geopolitical/military awareness briefing built on the
[NewsAPI](https://newsapi.org) free "Developer" tier, running unattended
on GitHub Actions and published as a static site via GitHub Pages.

This is a public, portfolio-scale adaptation of a private 14-topic
military awareness taxonomy -- built to demonstrate an end-to-end
AI/automation pipeline (scheduled ingestion, structured processing,
automated publishing) using only publicly available data and free-tier
services.

## What it does

Every day, a GitHub Actions workflow:

1. Queries NewsAPI for the two flagship flashpoints (Russia/Ukraine,
   Iran/Israel/Middle East) plus the full 14-topic taxonomy sweep
   (humanitarian crisis, nuclear activity, coups, cyberattacks,
   migration, etc. -- see `scripts/topics.py`).
2. Writes the raw results to `data/<date>/`.
3. Renders a static briefing page to `docs/index.html`.
4. Commits both back to the repo. GitHub Pages (configured to serve
   from `/docs`) picks up the change automatically.

No server, database, or paid infrastructure required.

## Scope

Both the two flagship topics and the full 14-topic taxonomy are fetched
and rendered daily by default (`TOPIC_SWEEP_ENABLED = True` in
`scripts/topics.py`). Since this is keyword-only retrieval (see "Known
limitations" below), expect the broader/vaguer topics -- things like
`T10 Military Modernization` or `T06 Mass Uprising` -- to carry more
noise than the tightly-scoped flagship queries. Set
`TOPIC_SWEEP_ENABLED = False` to scope back down to flagships-only
without touching `fetch_news.py` or `build_brief.py`.

## Request budget

The NewsAPI Developer plan allows **100 requests/day**, no burst
allowance. One full daily run uses:

| Query | Requests |
|---|---|
| 2 flagship topics via `/everything` | 2 |
| 2 flagship "US media lens" via `/top-headlines` | 2 |
| 14-topic taxonomy sweep via `/everything` | 14 |
| **Total per run** | **18** |

That leaves ~82 requests/day of headroom for manual testing, pagination
on high-volume days, or adding topics later. `scripts/fetch_news.py`
also has a hardcoded safety ceiling (`MAX_REQUESTS`) so a scheduling
mistake (e.g. an accidental duplicate run) can never silently blow
through the daily cap.

## Known limitations (v1, by design)

This first version deliberately uses **keyword search only** -- no
semantic filtering, no LLM relevance classification, no AI-written
narrative synthesis. The goal was to get a working, automated,
end-to-end pipeline live first, then layer in the following once v1 is
proven:

- **Noise from ambiguous keywords.** NewsAPI's `/everything` is a
  boolean keyword search over raw article text, not a semantic index.
  A query like `strike` will match both "Israeli airstrike" and "postal
  workers' strike." Some queries in `topics.py` use `NOT` clauses to
  cut the most obvious false positives, but this is not a full fix.
- **Planned fix:** a relevance-classification pass using an LLM (Claude,
  via API) between fetch and render -- send each topic's batch of
  headlines/descriptions to the model in one prompt, get back a
  relevant/not-relevant judgment per article, and only render the
  relevant ones. This is a language-understanding problem
  ("which sense of the word is this"), not a search-ranking problem, so
  a keyword tweak or an embeddings/semantic-similarity layer wouldn't
  fully solve it either -- it needs something that actually reads the
  sentence.
- **Also planned:** an AI-written narrative synthesis paragraph per
  topic (via the same Claude API call), so the page reads as a briefing
  rather than a headline list.
- Articles carry ~24h delay and the search window is capped at ~1 month
  on this plan -- this is a daily-digest product, not a live/real-time
  feed, which is intentional (matches a "brief you're handed" format
  rather than a dashboard you have to monitor).
- `/top-headlines` only supports `country=us` on this plan, so the "US
  media lens" section is deliberately scoped to US coverage only.

## Setup

1. **Rotate your NewsAPI key** before using it here if it's ever been
   shown on screen/shared -- get a fresh one at
   [newsapi.org/register](https://newsapi.org/register).
2. Create a new GitHub repo and push this project to it.
3. In the repo's **Settings > Secrets and variables > Actions**, add a
   repository secret named `NEWSAPI_KEY` with your key. Never commit the
   key directly to any file in this repo.
4. In **Settings > Pages**, set the source to "Deploy from a branch,"
   branch `main`, folder `/docs`.
5. The workflow in `.github/workflows/daily-brief.yml` runs on a daily
   cron schedule and can also be triggered manually from the Actions
   tab (`workflow_dispatch`).
6. To generate the first page before waiting for the schedule, run
   locally:
   ```
   export NEWSAPI_KEY=your_key_here
   pip install -r requirements.txt
   python scripts/fetch_news.py
   python scripts/build_brief.py
   ```
   then commit and push `data/` and `docs/`.

## Project layout

```
scripts/topics.py        topic taxonomy + NewsAPI query definitions (edit this to tune scope)
scripts/fetch_news.py     pulls raw article JSON from NewsAPI, writes to data/<date>/
scripts/build_brief.py    renders data/<date>/ into docs/index.html
.github/workflows/        daily scheduled run
docs/                     published site (GitHub Pages source)
data/                     raw daily snapshots (also serves as a running history/ledger)
```

## Background

Adapted from a private, structured intelligence-analysis pipeline built
on a commercial threat-intelligence API (Seerist), this version proves
the same architecture -- scheduled unattended ingestion, structured
topic taxonomy, automated static publishing -- using only free, public
data sources.
