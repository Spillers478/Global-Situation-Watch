"""
Source metadata for the briefing page: where an outlet is edited/published
from, independent of what any given story is about.

This is a *different* signal from redteam.py's `cocom` field on each
article. That field says "this story's content concerns USCENTCOM" (an
Iran/Israel story gets tagged USCENTCOM no matter who wrote it). This
file says "this outlet is published out of Qatar" -- a fact about the
source, not the story. Both get rendered, side by side, so a reader can
see both "what region is this about" and "whose lens is this coming
through."

-- Two separate axes on purpose: origin vs. audience reach --

SOURCE_ORIGIN's `country`/`cocom` fields answer "where is this outlet
edited from." That is NOT the same question as "who actually reads it,"
and conflating the two produces wrong conclusions -- e.g. TechRadar is
edited in the UK (Future plc) but SimilarWeb shows a majority-American
audience; tagging it "United States" because of its readership would
misattribute a British editorial voice to an American one. Where the
audience actually sits is its own useful signal (it's the "is this
outlet shaping opinion somewhere its own newsroom isn't" question --
relevant to influence/reach analysis), so it gets its own field,
`audience`, kept deliberately separate from `country`/`cocom`.

There's no free API for audience geography (SimilarWeb doesn't offer
one), so `audience` is populated by hand, opportunistically, from
whatever you've looked up yourself (SimilarWeb, comparable tools) --
most entries will have it empty/omitted, and that's fine. It only
renders when present; nothing waits on it being filled in for every
source.

-- Maintaining this list --

Keyed by the exact string NewsAPI's/newsdata's article.source.name
field returns (same key space TRUSTED_SOURCES in build_brief.py uses --
check a real article's source name before adding an entry; outlets are
sometimes credited under a slightly different name than you'd expect).
A source with no entry here just renders with no origin badge -- this
is additive, not a gate on anything.

`cocom` is one of redteam.py's VALID_COCOMS (USEUCOM, USCENTCOM,
USINDOPACOM, USAFRICOM, USSOUTHCOM, USNORTHCOM, Transregional), or the
literal string "Global" for wire services whose whole purpose is
worldwide reporting -- assigning a national AOR to Reuters' or AP's
*coverage* would be misleading even though the wire itself has an HQ
country; "Global" keeps that distinction visible instead of forcing a
false precision.

First-pass list (~40 outlets): the global wires already in
TRUSTED_SOURCES, plus the most common bylines actually observed in a
real day's data across both providers (spot-checked from
data/<date>/*.json, not guessed) -- see thepaperboy.com or
world-newspapers.com (browseable by country) as a reference for looking
up an unfamiliar outlet's home country. Extend freely; there's nothing
else to touch when adding a source.
"""

SOURCE_ORIGIN = {
    # -- Global wire services (HQ country shown, but AOR is "Global" --
    # their whole business is worldwide coverage, so tagging them with
    # their HQ's regional AOR would misleadingly suggest a regional lens
    # they don't have) --
    "Reuters": {"country": "United Kingdom", "cocom": "Global"},
    "Associated Press": {"country": "United States", "cocom": "Global"},
    "Agence France-Presse": {"country": "France", "cocom": "Global"},
    "Bloomberg": {"country": "United States", "cocom": "Global"},

    # -- United Kingdom (USEUCOM) --
    "BBC News": {"country": "United Kingdom", "cocom": "USEUCOM"},
    "Sky News": {"country": "United Kingdom", "cocom": "USEUCOM"},
    "The Guardian": {"country": "United Kingdom", "cocom": "USEUCOM"},
    "Financial Times": {"country": "United Kingdom", "cocom": "USEUCOM"},

    # -- Other Europe (USEUCOM) --
    "France 24": {"country": "France", "cocom": "USEUCOM"},
    "Deutsche Welle (DW)": {"country": "Germany", "cocom": "USEUCOM"},
    "The Local Germany": {"country": "Germany", "cocom": "USEUCOM"},
    "The Irish Times": {"country": "Ireland", "cocom": "USEUCOM"},
    "TheJournal.ie": {"country": "Ireland", "cocom": "USEUCOM"},
    "RTE": {"country": "Ireland", "cocom": "USEUCOM"},
    "Protothema.gr": {"country": "Greece", "cocom": "USEUCOM"},
    "Hurriyet Daily News": {"country": "Turkey", "cocom": "USEUCOM"},
    "RT": {"country": "Russia", "cocom": "USEUCOM"},
    "Sputnikglobe.com": {"country": "Russia", "cocom": "USEUCOM"},
    # Formally headquartered in Prague, Czech Republic; US government
    # funded (BBG/USAGM), editorially independent international
    # broadcaster -- flagged as such rather than filed as a plain
    # domestic Czech outlet.
    "Radio Free Europe/ Radio Liberty": {
        "country": "Czech Republic", "cocom": "USEUCOM",
        "note": "US-government-funded international broadcaster, HQ Prague",
    },

    # -- Middle East (USCENTCOM) --
    "Al Jazeera English": {"country": "Qatar", "cocom": "USCENTCOM"},
    "Israelnationalnews.com": {"country": "Israel", "cocom": "USCENTCOM"},

    # -- South/East Asia, Pacific (USINDOPACOM) --
    "The Times of India": {"country": "India", "cocom": "USINDOPACOM"},
    "Business Standard": {"country": "India", "cocom": "USINDOPACOM"},
    "BusinessLine": {"country": "India", "cocom": "USINDOPACOM"},
    "ABC News (AU)": {"country": "Australia", "cocom": "USINDOPACOM"},
    "New Zealand Herald": {"country": "New Zealand", "cocom": "USINDOPACOM"},
    "Newsonjapan.com": {"country": "Japan", "cocom": "USINDOPACOM"},

    # -- Africa (USAFRICOM) --
    "The Punch": {"country": "Nigeria", "cocom": "USAFRICOM"},
    "The Conversation Africa": {"country": "South Africa", "cocom": "USAFRICOM"},

    # -- North America (USNORTHCOM) --
    "CBC News": {"country": "Canada", "cocom": "USNORTHCOM"},
    "NPR": {"country": "United States", "cocom": "USNORTHCOM"},
    "PBS": {"country": "United States", "cocom": "USNORTHCOM"},
    "CBS News": {"country": "United States", "cocom": "USNORTHCOM"},
    "NBC News": {"country": "United States", "cocom": "USNORTHCOM"},
    "ABC News": {"country": "United States", "cocom": "USNORTHCOM"},
    "CNN": {"country": "United States", "cocom": "USNORTHCOM"},
    "Fox News": {"country": "United States", "cocom": "USNORTHCOM"},
    "The New York Times": {"country": "United States", "cocom": "USNORTHCOM"},
    "The Washington Post": {"country": "United States", "cocom": "USNORTHCOM"},
    "Wall Street Journal": {"country": "United States", "cocom": "USNORTHCOM"},
    "Politico": {"country": "United States", "cocom": "USNORTHCOM"},
    "Axios": {"country": "United States", "cocom": "USNORTHCOM"},

    # -- Example of the `audience` field: filled in by hand from a real
    # SimilarWeb lookup (see the source-tagging discussion this table
    # came from). Most entries won't have this -- it's opportunistic,
    # not required. TechRadar isn't a wire/broadcast source so it isn't
    # in TRUSTED_SOURCES, but it can still carry an origin+audience tag.
    "TechRadar": {
        "country": "United Kingdom", "cocom": "USEUCOM",
        "audience": "~33% US traffic (SimilarWeb, Aug 2026)",
    },
}
