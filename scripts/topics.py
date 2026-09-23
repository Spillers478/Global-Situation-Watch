"""
Topic taxonomy and NewsAPI query definitions for the intelligence dashboard.

Two layers:
- TIER1: flagship geographic flashpoints, queried directly on /everything,
  plus a lightweight "US media attention" pull via /top-headlines. These
  are the two topics that get top billing on the page.
- TOPICS: the full 14-topic taxonomy (adapted from the mil-awareness-brief
  taxonomy), each run once daily as a global (non-country-specific) sweep.

Everything here is a *keyword* boolean query against NewsAPI's /v2/everything
endpoint (see https://newsapi.org/docs/endpoints/everything for syntax).
No semantic/LLM filtering happens at retrieval time -- synthesize.py's BLUF
pass can flag a topic's results as noise, but it can't un-retrieve them.
This file is the actual noise-control lever; see "Noise-control tools"
below for the mechanics.

Editing this file is the primary way to tune the product: add/remove
topics, tighten/loosen a query, or change severity/tier labels. Nothing
else in the pipeline needs to change.

SCOPE: the two TIER1 flagships plus the full 14-topic TOPICS sweep are
both fetched/rendered daily. Set TOPIC_SWEEP_ENABLED to False to scope
back down to flagships-only (useful if noise/budget ever becomes a
problem) without touching fetch_news.py or build_brief.py.

-- Noise-control tools --

NewsAPI's /everything is boolean keyword search over article text, not a
semantic index, so any common English word in the query set will pick up
homonyms and metaphor: "offensive" matches an NFL "offensive line" as
readily as a "military offensive"; "nuclear" matches cell biology
("nuclear DNA") as readily as weapons; "displacement" matches a buoy's
water displacement or a bird pushed out of its nest as readily as a
refugee crisis; "epidemic"/"pandemic" get used loosely as adjectives
("loneliness epidemic," "COVID-era funding") far more often than they
describe an actual outbreak. This file leans on three levers to fight
that, applied per-topic where the noise was worst in practice (see the
comments on each topic below for what was actually observed):

1. Phrase-anchoring: prefer multi-word phrases ("military offensive")
   over bare words ("offensive") wherever the bare word is a common
   homonym. Cuts recall a little, cuts false positives a lot.
2. search_in="title": for topics that were dominated by body-text-only
   matches (the keyword never appeared in the actual headline), restrict
   matching to the title via NewsAPI's `searchIn` param. Trades recall
   for a big precision gain -- reserved for topics where that trade was
   clearly worth it based on real output.
3. NOT clauses: exclude specific noise categories seen in practice
   (sports scoring language, comic-book/video-game previews, TV listings,
   generic local-crime-blotter language) rather than guessing in advance.

EXCLUDE_DOMAINS below removes a small number of whole sources that turned
out to be pure noise generators for this taxonomy, applied to every
/everything call. Revert or trim this list any time by editing it --
nothing else needs to change.

-- newsdata_query: a second, shorter query per topic --

newsdata.io (see fetch_newsdata.py) enforces a hard 100-character cap on
its `q`/`qInTitle` parameter on this plan -- discovered by a live 400
error ("UnsupportedQueryLength"), not documented up front. Every `query`
string below is tuned for NewsAPI's much larger limit (~500 chars) and
routinely exceeds 100, so each topic also carries a `newsdata_query`:
a hand-shortened equivalent that keeps the strongest phrase-anchored
terms from `query` and drops most NOT-exclusion clauses (there wasn't
room) and secondary OR terms. This trades some of the precision tuning
described above for newsdata's results specifically -- redteam.py's
relevance pass and EXCLUDE_DOMAINS (via `excludedomain`) are the
remaining backstops for that source. fetch_newsdata.py falls back to
`query` if a topic has no `newsdata_query`, so this field is additive,
not required.
"""

TOPIC_SWEEP_ENABLED = True  # False = flagships only; True = flagships + full 14-topic sweep

# Domains excluded from every /everything call (see fetch_news.py). Picked
# from actual noise observed in a live run, not guessed in advance:
#   - lifesciencesworld.com: a generic animal/science-trivia content mill;
#     its articles on bird/animal behavior were matching "displacement"
#     and "exodus" under T13 (Migration/Refugee Crisis) via literal bird-
#     migration and nest-displacement content.
#   - rlsbb.cc: a torrent/release-listing site, not a news source; its
#     TV-episode listings were matching "American hostage" (T09) because
#     that's also a TV show's literal title.
#   - timesofindia.indiatimes.com: a very high-volume wire aggregator whose
#     hyper-local India city-desk content (crime blotter, civic-works
#     notices, festival planning) was the single largest source of noise
#     across nearly every topic below, via bare words like "attack,"
#     "conflict," "crisis," and "offensive" appearing in unrelated local
#     stories. International/India-relevant conflict or security stories
#     are almost always also carried by wire services (Reuters, AP, Al
#     Jazeera, BBC) that remain in scope, so the recall loss here is
#     expected to be small relative to the noise cut. Remove this entry
#     if that trade-off turns out to be wrong for your use case.
EXCLUDE_DOMAINS = "lifesciencesworld.com,rlsbb.cc,timesofindia.indiatimes.com"

# Cheap, reusable NOT-suffix for topics that were getting flooded by NFL/
# sports-scoring language and entertainment previews sharing vocabulary
# with conflict/security terms ("offensive," "strike," "attack," "war").
_SPORTS_ENT_EXCLUSION = (
    ' NOT (NFL OR NBA OR NHL OR MLB OR quarterback OR touchdown OR "box office" OR '
    'playoff OR esports OR "video game" OR movie OR film OR TVLine OR Hulu OR "season finale")'
)

TIER1 = [
    {
        "id": "flagship-ru-ua",
        "label": "Russia / Ukraine",
        "query": 'Ukraine AND Russia AND (strike OR offensive OR missile OR drone OR ceasefire OR '
                  'negotiation OR invasion OR shelling OR "front line" OR Kursk OR Donbas) NOT '
                  '(TVLine OR Hulu OR "season finale" OR "TV series")',
        "newsdata_query": 'Ukraine AND Russia AND (strike OR missile OR ceasefire OR invasion OR Kursk OR Donbas)',
        "us_lens_query": "Ukraine",
    },
    {
        "id": "flagship-ir-il",
        "label": "Iran / Israel / Middle East",
        "query": 'Iran AND (Israel OR strike OR nuclear OR missile OR "Middle East" OR Hezbollah '
                  'OR "Red Sea" OR Houthi) NOT (TVLine OR Hulu OR "season finale" OR "TV series")',
        "newsdata_query": 'Iran AND (Israel OR strike OR nuclear OR missile OR Hezbollah OR Houthi)',
        "us_lens_query": "Iran",
    },
    {
        # Two-gate design (hazard AND access/infrastructure effect) instead
        # of a flat keyword-OR list like the other flagships: a bare
        # "earthquake" or "flood" query would be almost entirely routine
        # weather/disaster reporting. Gating on an infrastructure-access
        # term (airport/port closure, grid/pipeline/bridge disruption,
        # evacuation) narrows this to disasters that actually deny access
        # or degrade infrastructure -- the operationally relevant subset,
        # not disaster news in general.
        "id": "T15",
        "label": "Disaster / Access Denial",
        "severity": 7,
        "query": '(earthquake OR quake OR volcano OR eruption OR typhoon OR cyclone OR hurricane OR '
                  'tsunami OR flood OR flooding OR landslide OR mudslide OR wildfire) AND '
                  '("airport closed" OR "airspace closed" OR "port closed" OR "port suspended" OR '
                  'runway OR airfield OR seaport OR harbor OR strait OR "shipping lane" OR '
                  '"power grid" OR refinery OR pipeline OR "bridge collapse" OR "rail line" OR '
                  '"flights suspended" OR "road severed" OR evacuation)',
        "search_in": "title,description",
        # Hand-shortened for newsdata's 100-char q cap (see topics.py's
        # "newsdata_query" docs above) -- keeps the strongest hazard/access
        # terms from each gate rather than the full list.
        "newsdata_query": '(earthquake OR hurricane OR tsunami) AND (runway OR "power grid" OR evacuation)',
        # Disaster-access stories are far more time-perishable than most
        # other topics here (an airport reopens or a grid comes back in
        # hours, not days) -- narrows newsdata's lookback window from its
        # 48h free-tier default to 24h. NewsAPI has no equivalent per-query
        # knob; its own ~24h article delay already limits how fresh a
        # /everything result can be regardless.
        "newsdata_timeframe": 24,
    },
]

# tier: "tripwire" | "slow" | "event"  (mirrors the mil-awareness-brief taxonomy)
# severity: 1-10, kept from the original taxonomy for later use in ranking/sorting
# search_in: None (default -- title+description+content) or "title" -- see
#   "Noise-control tools" above. Only set on topics where body-only matches
#   were the dominant noise source in a real run.
TOPICS = [
    {
        # Was the single worst offender in practice: bare "war," "offensive,"
        # and "clashes" matched NFL/NBA game recaps almost exclusively
        # (~95% of results were sports, not conflict). Fixed by dropping the
        # bare homonyms in favor of phrases, restricting to title matches,
        # and excluding sports/entertainment vocabulary.
        "id": "T01", "label": "Military Conflict", "tier": "event", "severity": 7,
        "search_in": "title",
        "query": '"armed conflict" OR "military conflict" OR "military offensive" OR '
                 '"ground offensive" OR invasion OR airstrike OR "air strikes" OR bombardment OR '
                 '"front line" OR shelling OR "armed clashes"' + _SPORTS_ENT_EXCLUSION,
        "newsdata_query": '"armed conflict" OR "military offensive" OR airstrike OR bombardment OR shelling',
    },
    {
        "id": "T02", "label": "Humanitarian Crisis", "tier": "slow", "severity": 8,
        "query": '"humanitarian crisis" OR "humanitarian emergency" OR famine OR "mass starvation" '
                 'OR "food insecurity" OR malnutrition OR "aid blocked" OR "internally displaced" '
                 'OR "humanitarian access" NOT (concert OR "world tour" OR "box office")',
        "newsdata_query": '"humanitarian crisis" OR famine OR "mass starvation" OR "food insecurity" OR malnutrition',
    },
    {
        "id": "T03", "label": "Bio/Chemical Attack", "tier": "tripwire", "severity": 10,
        "query": '"chemical attack" OR "chemical weapons" OR "biological attack" OR bioweapon OR '
                 '"nerve agent" OR sarin OR anthrax OR "toxic gas attack" OR "chemical weapons use"',
        "newsdata_query": '"chemical attack" OR "chemical weapons" OR "biological attack" OR bioweapon OR sarin OR anthrax',
    },
    {
        # Bare "epidemic"/"pandemic"/"contagion" are used loosely as
        # adjectives far more often than they describe a real outbreak
        # ("loneliness epidemic," "COVID-era funding windfall," "market
        # contagion") -- in practice this was near-100% noise. Restricting
        # to title matches cuts almost all of it, since that loose usage
        # showed up in body text, not headlines.
        "id": "T04", "label": "Infectious Outbreak / Pandemic", "tier": "slow", "severity": 7,
        "search_in": "title",
        "query": '"disease outbreak" OR epidemic OR pandemic OR "public health emergency" OR '
                 'quarantine OR "novel virus" OR "mystery illness" OR contagion NOT '
                 '(funding OR stimulus OR relief OR economy OR market OR stock)',
        "newsdata_query": '"disease outbreak" OR "public health emergency" OR quarantine OR "novel virus"',
    },
    {
        "id": "T05", "label": "Political Instability / Coup Risk", "tier": "tripwire", "severity": 8,
        "query": 'coup OR "coup attempt" OR "coup d\'etat" OR "government collapse" OR '
                  '"military takeover" OR "state of emergency" OR "regime change" OR "power vacuum" '
                  'NOT ("board game" OR "card game" OR boardgame)',
        "newsdata_query": 'coup OR "coup attempt" OR "government collapse" OR "military takeover" OR "regime change"',
    },
    {
        # "uprising"/"insurrection" were matching comic-book and video-game
        # preview coverage (rebellion/regime-change plot points share the
        # vocabulary). Title-only + a comics/games exclusion cut this.
        "id": "T06", "label": "Mass Uprising / Regime-Threatening Unrest", "tier": "slow", "severity": 6,
        "search_in": "title",
        "query": '(uprising OR insurrection OR "mass protests" OR "anti-government protests" OR '
                  '"civil unrest" OR "nationwide protests" OR riots) NOT (labor OR union OR sports '
                  'OR strikers OR comic OR Marvel OR DC OR superhero OR "video game")',
        "newsdata_query": 'uprising OR "mass protests" OR "anti-government protests" OR "civil unrest" OR riots',
    },
    {
        # "nuclear" alone is a homonym minefield: cell biology ("nuclear
        # DNA"), astrophysics, and "nuclear family" all matched here
        # alongside real weapons/proliferation stories. Added an explicit
        # exclusion for the biology/astronomy senses rather than
        # restricting to title (title-only was cutting too many real NK/
        # Iran missile headlines that don't literally say "nuclear").
        "id": "T07", "label": "Nuclear Weaponry / Attack", "tier": "tripwire", "severity": 10,
        "query": 'nuclear AND (weapon OR missile OR warhead OR enrichment OR test OR strike OR '
                  '"nuclear facility" OR "nuclear program" OR proliferation) NOT (biology OR genetic '
                  'OR DNA OR cell OR "nuclear family" OR astronomy OR telescope OR planet)',
        "newsdata_query": 'nuclear AND (weapon OR warhead OR enrichment OR "nuclear facility" OR proliferation)',
    },
    {
        # Bare "attack" and "offensive" were pulling in generic local-crime
        # items (dog/bee/pepper-spray "attacks," unrelated "offensive"
        # usage) alongside real terrorism/insurgency coverage.
        "id": "T08", "label": "VEO / Regional Insecurity", "tier": "event", "severity": 7,
        "search_in": "title",
        "query": '(terrorist OR "extremist group" OR insurgency OR militant OR jihadist OR '
                  '"armed group") AND (attack OR offensive OR ambush OR bombing) NOT (pitbull OR dog '
                  'OR bee OR sting OR "heart attack" OR "cardiac arrest" OR pepper OR robbery)',
        "newsdata_query": '(terrorist OR insurgency OR militant OR jihadist) AND (attack OR ambush OR bombing)',
    },
    {
        # The literal phrase "American hostage" is also a TV show's title;
        # torrent-listing and TV-recap sites were the entire result set.
        # EXCLUDE_DOMAINS drops the torrent site; this NOT clause drops the
        # TV-recap language.
        "id": "T09", "label": "American Hostage / Kidnapping", "tier": "tripwire", "severity": 9,
        "query": '"American hostage" OR "US citizen kidnapped" OR "American detained" OR '
                  '"US citizen held" OR "American captured" OR "US national kidnapped" NOT '
                  '(season OR episode OR "S01E" OR TVLine OR streaming OR premiere OR renewed)',
        "newsdata_query": '"American hostage" OR "US citizen kidnapped" OR "American detained" OR "US citizen held"',
    },
    {
        "id": "T10", "label": "Military Modernization", "tier": "slow", "severity": 5,
        "query": '"military modernization" OR "defense budget" OR "new weapons system" OR '
                  'rearmament OR "arms buildup" OR "weapons procurement" OR "defense spending"',
        "newsdata_query": '"military modernization" OR "defense budget" OR rearmament OR "arms buildup"',
    },
    {
        # Bare "maneuvers"/"drill" picked up a dog-behavior article and a
        # video-game character comparison.
        "id": "T11", "label": "Military Exercise", "tier": "event", "severity": 4,
        "search_in": "title",
        "query": '"military exercise" OR "joint exercise" OR "war games" OR "military drill" OR '
                  'maneuvers OR "naval exercise" OR "joint military drill" NOT (dog OR puppy OR pet '
                  'OR Kratos OR "video game")',
        "newsdata_query": '"military exercise" OR "joint exercise" OR "war games" OR "naval exercise"',
    },
    {
        "id": "T12", "label": "Large-Scale Cyber Attack / Internet Blackout", "tier": "tripwire", "severity": 8,
        "query": '(cyberattack OR "cyber attack" OR "internet blackout" OR ransomware OR '
                  '"infrastructure hack" OR "grid hack" OR "state-sponsored hack") AND '
                  '(government OR infrastructure OR critical)',
        "newsdata_query": '(cyberattack OR ransomware OR "infrastructure hack") AND (government OR infrastructure)',
    },
    {
        # Bare "displacement" and "exodus" were matching animal-behavior
        # trivia (a bird "displaced" from its nest, a titanium buoy's water
        # displacement) almost entirely via lifesciencesworld.com, which is
        # now in EXCLUDE_DOMAINS. Also tightened the phrasing itself.
        "id": "T13", "label": "Migration / Refugee Crisis", "tier": "event", "severity": 6,
        "query": '"refugee crisis" OR "migrant crisis" OR "mass migration" OR "population '
                  'displacement" OR "displaced families" OR "displaced persons" OR "mass exodus" OR '
                  '"asylum seekers surge" OR "border crossing surge" NOT (bird OR wildlife OR animal '
                  'OR species OR nest OR buoy)',
        "newsdata_query": '"refugee crisis" OR "migrant crisis" OR "displaced persons" OR "mass exodus"',
    },
    {
        "id": "T14", "label": "Threats to US Embassies / Diplomats", "tier": "tripwire", "severity": 9,
        "query": '("US embassy" OR "American consulate" OR diplomat OR "diplomatic mission") AND '
                  '(threat OR attack OR evacuation OR breach OR stormed)',
        "newsdata_query": '("US embassy" OR "American consulate" OR diplomat) AND (threat OR attack OR evacuation)',
    },
]
