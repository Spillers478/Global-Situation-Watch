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
3. Sends every retrieved article to Claude (Sonnet) in one batched
   "red team" call that checks it actually belongs to the topic it was
   retrieved under, reassigns it to a better-fitting topic when it
   doesn't, discards it if it isn't genuinely relevant to the taxonomy
   at all (sports/entertainment/homonym noise that slipped past the
   query-level filtering), and tags whichever it keeps with the
   geographic combatant command (USEUCOM, USCENTCOM, USINDOPACOM,
   USAFRICOM, USSOUTHCOM, USNORTHCOM, or "Transregional") the story is
   contextually about -- `scripts/redteam.py`. Writes the vetted,
   tagged result to `data/<date>/redteam/`, plus a `_report.json` audit
   summary of what got kept/reassigned/discarded per topic.
4. Sends the day's (now vetted) headlines to Claude (Haiku) in one
   batched call to write a BLUF (Bottom-Line-Up-Front) overview plus a
   short narrative summary for each topic -- `scripts/synthesize.py`.
5. Renders a static briefing page to `docs/index.html`: a jump-to-topic
   nav bar (plain-language topic names, not internal codes), the BLUF up
   top, then each topic's AI summary with its supporting articles
   underneath -- source name, description, a combatant-command tag, a
   "reassigned from <topic>" note when the red team pass moved it, and
   (when NewsAPI's truncated `content` field adds anything beyond the
   description) a second paragraph of extra detail. Articles from
   recognized wire services and national broadcasters (Reuters, AP, BBC,
   Al Jazeera, etc.) are sorted to the front of each list and tagged
   "wire service" -- see `TRUSTED_SOURCES` in `build_brief.py`. Each
   section is capped to a preview per topic, with the rest behind a
   "show more" toggle -- see "Known limitations" for why noisier topics
   can still run long.
6. Commits everything back to the repo. GitHub Pages (configured to
   serve from `/docs`) picks up the change automatically.

No server or database required. NewsAPI is free at this volume; the
Claude API calls are paid but inexpensive -- see "Cost" below.

Both the red team pass and the synthesis pass degrade gracefully:
if `ANTHROPIC_API_KEY` isn't set, either call fails, or the response
can't be parsed, that stage is skipped rather than breaking the
pipeline -- `synthesize.py` falls back to headline-only display, and
`build_brief.py` falls back to the raw (unvetted, untagged) retrieval
whenever `data/<date>/redteam/` doesn't exist. See `redteam.py`'s
module docstring for the full reasoning on why this step uses Sonnet
rather than Haiku.

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

NewsAPI is free at this volume. The Claude API is **not** free -- there
are now two paid calls per day, and they're not the same size. Both are
estimates, not guarantees: actual cost scales with how many articles
NewsAPI actually returns each day, and this project's own live testing
has seen topic counts range from zero to dozens depending on the news
cycle.

**Synthesis** (`synthesize.py`, Claude Haiku) makes exactly **one**
batched call per day, capped at 12 articles/topic. Rough estimate at
this scope (16 topics, ~15K input tokens, ~1.5K output tokens per run):

| | Rate | Per run | Per month |
|---|---|---|---|
| Input | $1 / MTok | ~$0.015 | ~$0.45 |
| Output | $5 / MTok | ~$0.0075 | ~$0.23 |
| **Total** | | **~$0.02/day** | **~$0.70/month** |

**Red team classification** (`redteam.py`, Claude Sonnet) also makes
**one** batched call per day, but it's a bigger one -- it sends every
retrieved article (not a summary), capped at 25/topic, and Sonnet costs
more per token than Haiku. Two scenarios:

| | Typical day (~100-150 articles total) | Worst case (all 16 topics maxed, 400 articles) |
|---|---|---|
| Input tokens | ~6-8K | ~20K |
| Output tokens | ~2-3K | ~7-8K |
| Input cost ($2/MTok) | ~$0.01-0.02 | ~$0.04 |
| Output cost ($10/MTok) | ~$0.02-0.03 | ~$0.07-0.08 |
| **Total** | **~$0.04/day (~$1.10/month)** | **~$0.11/day (~$3.40/month)** |

Realistically, expect something between those two rows most days --
the noise-reduction work in `topics.py` means most topics return far
fewer than 25 articles/day in practice, so the typical-day column is
the more likely one, but a genuinely high-volume news day (e.g. an
active flagship crisis) can push several topics toward the cap at
once, which is what the worst-case column models. The worst case also
sits close to `redteam.py`'s `max_tokens=8192` output ceiling; if real
usage regularly approaches 400 articles/day, that ceiling may need
raising (the JSON response would otherwise truncate and fail to
parse -- which is handled gracefully, see below, but would mean losing
that day's classification).

**Combined**, that's roughly **$0.06/day (~$1.80/month) typical**, up
to **~$0.13/day (~$4.10/month) worst case** for the full pipeline. If
`ANTHROPIC_API_KEY` isn't set, or either call fails or returns
something that can't be parsed, that stage is skipped rather than
raising -- `redteam.py` leaves the raw retrieval untouched and
`synthesize.py` writes an empty result, so the page still builds, just
without relevance filtering/COCOM tags and/or without the BLUF and
narrative summaries.

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
- The Claude model IDs in `synthesize.py` (`claude-haiku-4-5-20251001`)
  and `redteam.py` (`claude-sonnet-5`) are current as of when this was
  built -- Anthropic's model lineup changes over time, so check
  [platform.claude.com/docs/en/models/overview](https://platform.claude.com/docs/en/models/overview)
  if either step ever starts failing with a model-not-found error.
- **Red team classification is model judgment, not verified ground
  truth.** `redteam.py` reads each article's title and description
  (not the full article -- see the article-detail limitation above) and
  makes a relevance/topic/COCOM call from that alone. It will
  occasionally discard something that was actually relevant, reassign
  something to a less-ideal topic, or tag the wrong COCOM -- particularly
  for genuinely ambiguous or Transregional stories. The `_report.json`
  audit file in `data/<date>/redteam/` records every kept/reassigned/
  discarded decision per topic if you want to spot-check its judgment.
  It's a meaningful precision improvement over raw keyword retrieval,
  not a guarantee.

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
   ~~~
   export NEWSAPI_KEY=your_key_here
   export ANTHROPIC_API_KEY=your_key_here
   pip install -r requirements.txt
   python scripts/fetch_news.py
   python scripts/redteam.py
   python scripts/synthesize.py
   python scripts/build_brief.py
   ~~~
   then commit and push `data/` and `docs/`.

## Project layout

~~~
scripts/topics.py        topic taxonomy + NewsAPI query definitions (edit this to tune scope)
scripts/fetch_news.py     pulls raw article JSON from NewsAPI, writes to data/<date>/
scripts/redteam.py        Claude red-team pass: verifies relevance, reassigns topics, tags COCOM, writes data/<date>/redteam/
scripts/synthesize.py     sends the day's (vetted) headlines to Claude for a BLUF + per-topic narrative, writes data/<date>/synthesis.json
scripts/build_brief.py    renders data/<date>/ (preferring data/<date>/redteam/) into docs/index.html
.github/workflows/        daily scheduled run
docs/                     published site (GitHub Pages source)
data/                     raw daily snapshots (also serves as a running history/ledger)
~~~

## Background

Adapted from a private, structured intelligence-analysis pipeline built
on a commercial threat-intelligence API (Seerist), this version proves
the same architecture -- scheduled unattended ingestion, structured
topic taxonomy, automated static publishing -- using only free, public
data sources.
