# Global Situation Watch

A daily, automated geopolitical/military awareness briefing built on the
[NewsAPI](https://newsapi.org) free "Developer" tier and the
[Claude API](https://console.anthropic.com), running unattended on
GitHub Actions and published as a static site via GitHub Pages.

This is a public, portfolio-scale adaptation of a private 14-topic
military awareness taxonomy -- built to demonstrate an end-to-end
AI/automation pipeline (scheduled ingestion, AI-assisted processing,
automated publishing) using only publicly available data.

## What it does

Every day, a GitHub Actions workflow:

1. Queries NewsAPI for the two flagship flashpoints (Russia/Ukraine,
   Iran/Israel/Middle East) plus the full 14-topic taxonomy sweep
   (humanitarian crisis, nuclear activity, coups, cyberattacks,
   migration, etc. -- see `scripts/topics.py`).
2. Writes the raw results to `data/<date>/`.
3. Sends the day's headlines to Claude (Haiku) in one batched call to
   write a BLUF (Bottom-Line-Up-Front) overview plus a short narrative
   summary for each topic -- `scripts/synthesize.py`.
4. Renders a static briefing page to `docs/index.html`: a jump-to-topic
   nav bar (plain-language topic names, not internal codes), the BLUF up
   top, then each topic's AI summary with its supporting articles
   underneath -- source name, description, and (when NewsAPI's truncated
   `content` field adds anything beyond the description) a second
   paragraph of extra detail. Articles from recognized wire services and
   national broadcasters (Reuters, AP, BBC, Al Jazeera, etc.) are sorted
   to the front of each list and tagged "wire service" -- see
   `TRUSTED_SOURCES` in `build_brief.py`. Each section is capped to a
   preview per topic, with the rest behind a "show more" toggle -- see
   "Known limitations" for why noisier topics can still run long.
5. Commits everything back to the repo. GitHub Pages (configured to
   serve from `/docs`) picks up the change automatically.

No server or database required. NewsAPI is free at this volume; the
Claude API calls are paid but inexpensive -- see "Cost" below.

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

## Cost

NewsAPI is free at this volume. The Claude API is **not** free, but it's
cheap here: `synthesize.py` makes exactly **one** batched API call per
day (not one per topic) using Claude Haiku, capped at 12 articles/topic
to control prompt size. Rough estimate at this scope (16 topics,
~15K input tokens, ~1.5K output tokens per run):

| | Rate | Per run | Per month |
|---|---|---|---|
| Input | $1 / MTok | ~$0.015 | ~$0.45 |
| Output | $5 / MTok | ~$0.0075 | ~$0.23 |
| **Total** | | **~$0.02/day** | **~$0.70/month** |

That's an estimate, not a guarantee -- actual cost scales with how many
articles NewsAPI actually returns each day. If `ANTHROPIC_API_KEY` isn't
set (or the call fails for any reason), `synthesize.py` writes an empty
result instead of raising, and the page falls back to headline-only
display automatically -- the pipeline never breaks because of this step.

## Known limitations

- **Retrieval noise.** NewsAPI's `/everything` is a boolean keyword
  search over raw article text, not a semantic index. A bare word like
  `offensive` or `nuclear` will match an NFL "offensive line" or "nuclear
  DNA" in cell biology just as readily as a military offensive or a
  weapons story -- in an early live run this made `T01 Military Conflict`
  and `T04 Infectious Outbreak / Pandemic` almost entirely sports/
  metaphor noise. `topics.py` now fights this on three fronts per topic
  (see its "Noise-control tools" section for the full reasoning): phrase-
  anchoring instead of bare homonyms, restricting NewsAPI's `searchIn`
  param to the article title for the worst-offending topics (a big
  precision gain, some recall cost), and topic-specific `NOT` exclusions
  for observed noise categories. `fetch_news.py` also excludes a short
  list of domains (`EXCLUDE_DOMAINS` in `topics.py`) that turned out to
  be pure noise generators -- including `timesofindia.indiatimes.com`,
  whose very-high-volume local India city-desk wire was the single
  largest noise source across almost every topic; international/India-
  relevant stories are expected to still surface via other wire services,
  but that's a real recall trade-off worth knowing about and reverting if
  it doesn't hold for your use case. None of this makes it semantic
  understanding -- it's still keyword search, just with the worst-known
  false-positive patterns fenced off. `synthesize.py`'s prompt is the
  remaining backstop: it tells the model to say plainly when a topic's
  articles still look unrelated or too sparse rather than inventing a
  coherent narrative from noise.
- Articles carry ~24h delay and the search window is capped at ~1 month
  on this plan -- this is a daily-digest product, not a live/real-time
  feed, which is intentional (matches a "brief you're handed" format
  rather than a dashboard you have to monitor).
- `/top-headlines` only supports `country=us` on this plan, so the "US
  media lens" section is deliberately scoped to US coverage only.
- **Article detail is capped by the NewsAPI plan, not by this code.**
  The free/Developer tier's `content` field is truncated to roughly 200
  characters (with a "[+N chars]" marker for how much more exists but
  isn't returned) -- it is not full article text, and NewsAPI's paid
  tiers are what unlock that. `build_brief.py` shows this truncated
  content as a second paragraph when it adds anything beyond the
  `description` field, but "more detail than a couple of sentences"
  tops out there without either a paid NewsAPI plan or scraping each
  article's URL directly (not implemented -- scraping arbitrary news
  sites is fragile and has its own ToS/legal considerations per site).
- The Claude model ID in `synthesize.py` (`claude-haiku-4-5-20251001`)
  is current as of when this was built -- Anthropic's model lineup
  changes over time, so check
  [platform.claude.com/docs/en/models/overview](https://platform.claude.com/docs/en/models/overview)
  if the synthesis step ever starts failing with a model-not-found
  error.

## Roadmap idea: regional coverage / bias comparison

The "US Media Lens" subsection under each flagship (a `/top-headlines`
pull scoped to `country=us`) is a narrow version of a bigger idea: pulling
the same story from multiple regional press pools (e.g. Western, African,
Chinese, Middle Eastern outlets) side by side to make editorial framing
differences visible -- a real signal for spotting bias or propaganda, not
just "what happened." Not built yet. The main blocker is that NewsAPI
doesn't expose a source's country/region as queryable metadata, so this
would need a hand-maintained domain-to-region mapping (similar in spirit
to `TRUSTED_SOURCES` in `build_brief.py`) plus a region-scoped query per
flagship topic, generalizing the existing `us_lens_query` pattern in
`topics.py`. Worth prioritizing once the core taxonomy is stable.

## Setup

1. **Rotate your NewsAPI key** before using it here if it's ever been
   shown on screen/shared -- get a fresh one at
   [newsapi.org/register](https://newsapi.org/register).
2. Create a new GitHub repo and push this project to it.
3. In the repo's **Settings > Secrets and variables > Actions**, add a
   repository secret named `NEWSAPI_KEY` with your key. Never commit the
   key directly to any file in this repo.
4. Add a second repository secret named `ANTHROPIC_API_KEY` (same
   Settings > Secrets and variables > Actions page). Get a key from
   [console.anthropic.com](https://console.anthropic.com) -- note this
   is a paid API, so it requires a billing method on the account (see
   "Cost" above for how little this uses). If you'd rather not pay for
   this yet, you can skip this step: `synthesize.py` will fail
   gracefully and the page will fall back to headline-only display
   automatically.
5. In **Settings > Pages**, set the source to "Deploy from a branch,"
   branch `main`, folder `/docs`.
6. The workflow in `.github/workflows/daily-brief.yml` runs on a daily
   cron schedule and can also be triggered manually from the Actions
   tab (`workflow_dispatch`).
7. To generate the first page before waiting for the schedule, run
   locally:
   ```
   export NEWSAPI_KEY=your_key_here
   export ANTHROPIC_API_KEY=your_key_here
   pip install -r requirements.txt
   python scripts/fetch_news.py
   python scripts/synthesize.py
   python scripts/build_brief.py
   ```
   then commit and push `data/` and `docs/`.

## Project layout

```
scripts/topics.py        topic taxonomy + NewsAPI query definitions (edit this to tune scope)
scripts/fetch_news.py     pulls raw article JSON from NewsAPI, writes to data/<date>/
scripts/synthesize.py     sends the day's headlines to Claude for a BLUF + per-topic narrative, writes data/<date>/synthesis.json
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
