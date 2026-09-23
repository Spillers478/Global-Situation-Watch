# Global Situation Watch

A daily, automated geopolitical/military awareness briefing built on the
[NewsAPI](https://newsapi.org) free "Developer" tier, [newsdata.io](https://newsdata.io)'s
free tier as a second retrieval source, and the
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
   migration, etc. -- see `scripts/topics.py`) -- `scripts/fetch_news.py`.
1b. Runs the same 16 queries against newsdata.io's `/latest` endpoint --
   `scripts/fetch_newsdata.py` -- normalizes its article schema to match
   NewsAPI's, and merges the result into the *same* per-topic files from
   step 1, deduped by article URL. This is a second retrieval source
   layered onto the same taxonomy, not a separate pipeline: everything
   downstream (red team, synthesis, page rendering) sees one merged
   article list per topic and doesn't know or care which provider a
   given article came from. Raw newsdata.io payloads are also kept at
   `data/<date>/newsdata/<topic_id>.json` for audit/debugging.
2. Writes the raw/merged results to `data/<date>/`.
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
   "wire service" -- see `TRUSTED_SOURCES` in `build_brief.py`. A page
   legend under the header explains what each badge means. Each
   section is capped to a preview per topic, with the rest behind a
   "show more" toggle -- see "Known limitations" for why noisier topics
   can still run long.
6. Commits everything back to the repo. GitHub Pages (configured to
   serve from `/docs`) picks up the change automatically.

No server or database required. NewsAPI is free at this volume; the
Claude API calls are paid but inexpensive -- see "Cost" below.

**Fault isolation.** Only the NewsAPI fetch and the page build can fail
the workflow. The newsdata.io fetch and both Claude passes run with
`continue-on-error: true`, and the commit step runs with `if: always()`.
That combination means a bug in any secondary stage can't throw away the
API calls the earlier stages already paid for: whatever was fetched
still gets committed and the brief still publishes, just with that layer
missing. (Before this was added, a crash in the newsdata step discarded
the ~18 NewsAPI requests that run had already spent, because the commit
step never ran -- fetched data lives only in the runner's ephemeral
workspace until it's committed.)

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

**newsdata.io** (`scripts/fetch_newsdata.py`) runs the same 16 queries
(2 flagships + 14-topic sweep, no separate "US lens" equivalent -- see
below) against the `/latest` endpoint:

| Query | Requests (credits) |
|---|---|
| 2 flagship topics | 2 |
| 14-topic taxonomy sweep | 14 |
| **Total per run** | **16** |

newsdata's free plan allows **200 credits/day** and **30 credits per 15
minutes**, so one run at 16 credits (paced ~1.5s apart, well under the
15-minute window) uses a small fraction of both caps -- plenty of
headroom for manual re-runs. `fetch_newsdata.py` has its own
`MAX_REQUESTS` safety ceiling, same rationale as NewsAPI's. Each
newsdata request returns up to 10 articles on the free tier (vs.
NewsAPI's 100/page), so it contributes a modest, complementary slice of
coverage per topic rather than a full second haul -- its value is
catching stories NewsAPI's index missed, not duplicating volume.

### Testing without spending a sweep

`fetch_newsdata.py` has two flags so you never have to burn a full
16-credit sweep (or a CI run) to find out something is misconfigured:

~~~
python scripts/fetch_newsdata.py --dry-run       # 0 credits
python scripts/fetch_newsdata.py --only T03      # 1 credit
~~~

`--dry-run` prints the exact query each topic would send plus its
character count against newsdata's 100-char cap, and sends nothing.
`--only <topic_id>` fetches one topic and pretty-prints the raw
response, which is the fastest way to see what the live API actually
returns (field names, whether an optional param is rejected on this
plan) without guessing.

The script also aborts the sweep after 3 consecutive API errors
(`MAX_CONSECUTIVE_ERRORS`). Every topic sends a structurally identical
request, so a systemic problem -- bad key, a param this plan doesn't
allow, rate limit, API outage -- would otherwise fail 16 times and spend
16 credits to learn the same thing once.

### Re-running a failed workflow

GitHub Actions re-runs at the **job** level, and a re-run replays the
**same commit** the original run was created from -- it does not pick up
newer commits on `main`. So re-running a failed run will never test a
fix you just pushed; use **Actions > Daily Briefing > Run workflow**
(`workflow_dispatch`) to start a fresh run against latest `main`.

Note also that both fetch steps re-run from scratch on any re-run, since
Actions can't resume mid-job from a failed step.

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

- **newsdata.io's free tier is narrower than NewsAPI's on a few axes.**
  `/latest` on the free plan only looks back ~48 hours (no ~1-month
  lookback like NewsAPI's `/everything`) and returns at most 10
  articles/request (vs. NewsAPI's 100/page) with `content` frequently
  null since full-text requires a paid plan. It's included as a
  complementary source to catch stories NewsAPI's index doesn't carry,
  not as a like-for-like doubling of volume -- see "Request budget"
  above for the credit math. Articles from both providers are merged
  and deduped by URL, so build_brief.py, redteam.py, and synthesize.py
  never need to know which source an article came from.

## Source origin and audience-reach tagging

Every article carries a `cocom` tag already (see above) for what region
the *story* is about. `scripts/sources.py` adds a second, independent
tag for what country the *outlet* is published out of -- e.g. an
Iran/Israel story (content COCOM: USCENTCOM) reported by Al Jazeera
(origin: Qatar, also USCENTCOM) reads differently than the same story
picked up by RT (origin: Russia, USEUCOM). Neither NewsAPI nor
newsdata.io exposes a source's home country as queryable metadata, so
`SOURCE_ORIGIN` is a hand-maintained `{source name: {country, cocom}}`
table, same pattern and same key space as `TRUSTED_SOURCES` in
`build_brief.py` (both keyed on the exact `source.name` string the
article carries). It ships with ~40 entries: the global wires already in
`TRUSTED_SOURCES`, plus the most common bylines actually observed in a
real day's data across both providers. A source with no entry just shows
no origin badge -- adding one is additive, never a gate. See
`sources.py`'s module docstring for the full reasoning, including why
global wires (Reuters, AP, AFP, Bloomberg) get tagged `"Global"` rather
than their HQ country's regional AOR.

A third field, `audience`, is separate again and answers a different
question: not where an outlet is edited, but where its *readers*
actually are -- useful for spotting an outlet with outsized reach into a
region its own newsroom isn't based in. There's no free API for this
(SimilarWeb doesn't offer one), so it's populated by hand, opportunistically,
from whatever's been looked up (SimilarWeb or similar), and renders as a
distinct "audience: ..." badge only when present -- most entries won't
have it, and nothing blocks on filling it in. `sources.py`'s `TechRadar`
entry is a worked example, and thepaperboy.com or world-newspapers.com
(both browseable by country) are useful references for looking up an
unfamiliar outlet's home country when adding new entries.

## Roadmap idea: COCOM-first page layout

The page is currently organized topic-first (Military Conflict, then
Humanitarian Crisis, etc.), each section mixing stories from every
region. A COCOM-first layout -- one BLUF per COCOM with its supporting
articles underneath, using the per-article `cocom` tag redteam.py
already computes -- would read more like a regional watch than a topic
index. Not built yet; it's a real restructuring of `build_brief.py`'s
page generation (and probably `synthesize.py`'s narrative pass, to write
a per-COCOM BLUF instead of a per-topic one), deliberately deferred until
source-origin tagging has been seen on a real page.

## Setup

1. **Rotate your NewsAPI key** before using it here if it's ever been
   shown on screen/shared -- get a fresh one at
   [newsapi.org/register](https://newsapi.org/register). The same rule
   applies to the newsdata.io key in step 4 -- rotate it at
   [newsdata.io/register](https://newsdata.io/register) if it's ever
   been shown on screen or shared anywhere, including in a chat.
2. Create a new GitHub repo and push this project to it.
3. In the repo's **Settings > Secrets and variables > Actions**, add a
   repository secret named `NEWSAPI_KEY` with your key. Never commit the
   key directly to any file in this repo.
4. Add a repository secret named `NEWSDATA_KEY` with your newsdata.io
   key (same Settings > Secrets and variables > Actions page). Get a
   free key at [newsdata.io/register](https://newsdata.io/register).
   This step has to be done by hand in the GitHub UI (or via `gh secret
   set NEWSDATA_KEY --repo <owner>/<repo>` from a machine you trust with
   the key) -- GitHub's Actions-secrets API requires client-side sealed-
   box encryption of the value before it's sent, which isn't something
   an automated assistant should be doing on your behalf with a live
   key. If you'd rather not use this source yet, skip this step and
   don't add the `fetch_newsdata.py` step to the workflow (or just leave
   the secret unset -- `fetch_newsdata.py` will raise a clear error if
   the workflow step runs without it, unlike the graceful degradation
   `redteam.py`/`synthesize.py` have for a missing `ANTHROPIC_API_KEY`).
5. Add a third repository secret named `ANTHROPIC_API_KEY` (same
   Settings > Secrets and variables > Actions page). Get a key from
   [console.anthropic.com](https://console.anthropic.com) -- note this
   is a paid API, so it requires a billing method on the account (see
   "Cost" above for how little this uses). If you'd rather not pay for
   this yet, you can skip this step: `synthesize.py` will fail
   gracefully and the page will fall back to headline-only display
   automatically.
6. In **Settings > Pages**, set the source to "Deploy from a branch,"
   branch `main`, folder `/docs`.
7. The workflow in `.github/workflows/daily-brief.yml` runs on a daily
   cron schedule and can also be triggered manually from the Actions
   tab (`workflow_dispatch`).
8. To generate the first page before waiting for the schedule, run
   locally:
   ~~~
   export NEWSAPI_KEY=your_key_here
   export NEWSDATA_KEY=your_key_here
   export ANTHROPIC_API_KEY=your_key_here
   pip install -r requirements.txt
   python scripts/fetch_news.py
   python scripts/fetch_newsdata.py
   python scripts/redteam.py
   python scripts/synthesize.py
   python scripts/build_brief.py
   ~~~
   then commit and push `data/` and `docs/`.

## Project layout

~~~
scripts/topics.py        topic taxonomy + query definitions, shared by both providers (edit this to tune scope)
scripts/fetch_news.py     pulls raw article JSON from NewsAPI, writes to data/<date>/
scripts/fetch_newsdata.py pulls raw article JSON from newsdata.io, normalizes + merges into data/<date>/ (dedup by URL); raw payloads also kept in data/<date>/newsdata/
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
