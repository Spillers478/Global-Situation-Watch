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
No semantic filtering or LLM classification is applied in this version --
see README "Known limitations" for the planned next iteration (a relevance
pass to cut noise like "strike" matching labor disputes as well as
airstrikes).

Editing this file is the primary way to tune the product: add/remove
topics, tighten/loosen a query, or change severity/tier labels. Nothing
else in the pipeline needs to change.

SCOPE: the two TIER1 flagships plus the full 14-topic TOPICS sweep are
both fetched/rendered daily. Set TOPIC_SWEEP_ENABLED to False to scope
back down to flagships-only (useful if noise/budget ever becomes a
problem) without touching fetch_news.py or build_brief.py.
"""

TOPIC_SWEEP_ENABLED = True  # False = flagships only; True = flagships + full 14-topic sweep

TIER1 = [
    {
        "id": "flagship-ru-ua",
        "label": "Russia / Ukraine",
        "query": 'Ukraine AND Russia AND (strike OR offensive OR missile OR drone OR ceasefire OR '
                  'negotiation OR invasion OR shelling OR "front line" OR Kursk OR Donbas)',
        "us_lens_query": "Ukraine",
    },
    {
        "id": "flagship-ir-il",
        "label": "Iran / Israel / Middle East",
        "query": 'Iran AND (Israel OR strike OR nuclear OR missile OR "Middle East" OR Hezbollah '
                  'OR "Red Sea" OR Houthi)',
        "us_lens_query": "Iran",
    },
]

# tier: "tripwire" | "slow" | "event"  (mirrors the mil-awareness-brief taxonomy)
# severity: 1-10, kept from the original taxonomy for later use in ranking/sorting
#
# Query design note: each query below is widened with synonyms/related terms
# to improve recall ("enough data"), while keeping the most obvious false-
# positive sources out with NOT clauses where that was a known problem
# (e.g. "strike" matching labor disputes, "outbreak" matching unrelated
# sports/entertainment usage). This is still keyword search, not semantic
# understanding -- wider recall means MORE noise to review, not less; see
# README "Known limitations." All queries are kept under NewsAPI's 500-char
# limit for the q parameter.
TOPICS = [
    {
        "id": "T01", "label": "Military Conflict", "tier": "event", "severity": 7,
        "query": '(war OR invasion OR offensive OR "armed conflict" OR "military conflict" OR '
                 '"military operation" OR clashes OR airstrike OR bombardment OR "front line" OR '
                 'shelling) NOT ("video game" OR movie OR film OR esports)',
    },
    {
        "id": "T02", "label": "Humanitarian Crisis", "tier": "slow", "severity": 8,
        "query": '"humanitarian crisis" OR "humanitarian emergency" OR famine OR "mass starvation" '
                 'OR "food insecurity" OR malnutrition OR "aid blocked" OR "internally displaced" '
                 'OR "humanitarian access"',
    },
    {
        "id": "T03", "label": "Bio/Chemical Attack", "tier": "tripwire", "severity": 10,
        "query": '"chemical attack" OR "chemical weapons" OR "biological attack" OR bioweapon OR '
                 '"nerve agent" OR sarin OR anthrax OR "toxic gas attack" OR "chemical weapons use"',
    },
    {
        "id": "T04", "label": "Infectious Outbreak / Pandemic", "tier": "slow", "severity": 7,
        "query": '"disease outbreak" OR epidemic OR pandemic OR "public health emergency" OR '
                 'quarantine OR "novel virus" OR "mystery illness" OR contagion',
    },
    {
        "id": "T05", "label": "Political Instability / Coup Risk", "tier": "tripwire", "severity": 8,
        "query": 'coup OR "coup attempt" OR "coup d\'etat" OR "government collapse" OR '
                 '"military takeover" OR "state of emergency" OR "regime change" OR "power vacuum"',
    },
    {
        "id": "T06", "label": "Mass Uprising / Regime-Threatening Unrest", "tier": "slow", "severity": 6,
        "query": '(uprising OR insurrection OR "mass protests" OR "anti-government protests" OR '
                 '"civil unrest" OR "nationwide protests" OR riots) NOT (labor OR union OR sports '
                 'OR strikers)',
    },
    {
        "id": "T07", "label": "Nuclear Weaponry / Attack", "tier": "tripwire", "severity": 10,
        "query": 'nuclear AND (weapon OR missile OR warhead OR enrichment OR test OR strike OR '
                 '"nuclear facility" OR "nuclear program" OR proliferation)',
    },
    {
        "id": "T08", "label": "VEO / Regional Insecurity", "tier": "event", "severity": 7,
        "query": '(terrorist OR "extremist group" OR insurgency OR militant OR jihadist OR '
                 '"armed group") AND (attack OR offensive OR ambush OR bombing)',
    },
    {
        "id": "T09", "label": "American Hostage / Kidnapping", "tier": "tripwire", "severity": 9,
        "query": '"American hostage" OR "US citizen kidnapped" OR "American detained" OR '
                 '"US citizen held" OR "American captured" OR "US national kidnapped"',
    },
    {
        "id": "T10", "label": "Military Modernization", "tier": "slow", "severity": 5,
        "query": '"military modernization" OR "defense budget" OR "new weapons system" OR '
                 'rearmament OR "arms buildup" OR "weapons procurement" OR "defense spending"',
    },
    {
        "id": "T11", "label": "Military Exercise", "tier": "event", "severity": 4,
        "query": '"military exercise" OR "joint exercise" OR "war games" OR "military drill" OR '
                 'maneuvers OR "naval exercise" OR "joint military drill"',
    },
    {
        "id": "T12", "label": "Large-Scale Cyber Attack / Internet Blackout", "tier": "tripwire", "severity": 8,
        "query": '(cyberattack OR "cyber attack" OR "internet blackout" OR ransomware OR '
                 '"infrastructure hack" OR "grid hack" OR "state-sponsored hack") AND '
                 '(government OR infrastructure OR critical)',
    },
    {
        "id": "T13", "label": "Migration / Refugee Crisis", "tier": "event", "severity": 6,
        "query": '"refugee crisis" OR "migrant crisis" OR "mass migration" OR displacement OR '
                 'exodus OR "asylum seekers surge" OR "border crossing surge"',
    },
    {
        "id": "T14", "label": "Threats to US Embassies / Diplomats", "tier": "tripwire", "severity": 9,
        "query": '("US embassy" OR "American consulate" OR diplomat OR "diplomatic mission") AND '
                 '(threat OR attack OR evacuation OR breach OR stormed)',
    },
]
