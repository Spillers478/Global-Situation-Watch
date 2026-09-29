"""
Source-quality tiers used for RANKING (threads.py) -- never for hiding.

Everything here is a heuristic about how an outlet is *built*, not a
judgment of any individual article's truth and not a judgment of politics.
The categories are functional and are applied the same way in every
direction:

  wire_primary     Wire services, public/national broadcasters and papers of
                   record, government and intergovernmental bodies, and
                   primary-source publishers (a vendor advisory, a CISA
                   bulletin, a UN press release).                    weight 1.0
  established      Established national/regional outlets, specialist trade
                   press, and tabloids with real newsrooms.          weight 0.7
  press_release    An organisation publishing its own statement. It is a
                   primary source for that organisation only; it is not
                   independent corroboration of anything else.        weight 0.4
  state_affiliated Outlets owned or directed by a government.        weight 0.3
  advocacy_opinion Sites whose primary model is commentary, activism or
                   explicit advocacy, on any side.                    weight 0.3
  aggregator       Republishers and link/syndication sites. They carry other
                   people's copy, so they add NO independent corroboration
                   and are excluded from source counts.               weight 0.15
  unclassified     Anything not listed here.                          weight 0.4

Edit DOMAIN_CATEGORY freely -- it is the only place this list lives. Keys are
registrable domains (see domain_of); a key also matches its subdomains, so
"news.un.org" only needs "un.org". Ranking degrades gracefully: an outlet
that is missing here is treated as "unclassified", never dropped.

Run `python scripts/threads.py --unclassified <date>` to list the domains on
a given day that still fall through to "unclassified".
"""
import re
from urllib.parse import urlparse

TIER_WEIGHT = {
    "wire_primary": 1.0,
    "established": 0.7,
    "press_release": 0.4,
    "state_affiliated": 0.3,
    "advocacy_opinion": 0.3,
    "aggregator": 0.15,
    "unclassified": 0.4,
}

# Categories that do not count toward "independent source" totals.
NOT_INDEPENDENT = {"aggregator"}

_WIRE = [
    "reuters.com", "apnews.com", "afp.com", "bbc.co.uk", "bbc.com", "aljazeera.com",
    "nbcnews.com", "cbsnews.com", "abcnews.com", "abc.net.au", "cbc.ca", "npr.org",
    "pbs.org", "dw.com", "cnn.com", "foxnews.com", "nytimes.com", "wsj.com",
    "washingtonpost.com", "ft.com", "bloomberg.com", "theguardian.com", "skynews.com",
    "news.sky.com", "politico.com", "politico.eu", "axios.com", "irishtimes.com",
    "rte.ie", "channelnewsasia.com", "yle.fi", "france24.com", "nzherald.co.nz",
    "cnbc.com",
    # government / intergovernmental / primary-source publishers
    "gov.uk", "un.org", "cisa.gov", "hrw.org", "microsoft.com", "tenable.com",
    "who.int", "iom.int", "unhcr.org", "nato.int", "state.gov", "defense.gov",
]

_ESTABLISHED = [
    "time.com", "theatlantic.com",
    "nypost.com", "dailymail.com", "dailymail.co.uk", "indiatimes.com", "thediplomat.com",
    "foreignpolicy.com", "military.com", "nextgov.com", "warontherocks.com",
    "theconversation.com", "hurriyetdailynews.com", "business-standard.com",
    "thehindubusinessline.com", "haaretz.com", "israelnationalnews.com", "rawstory.com",
    "mediaite.com", "thejournal.ie", "punchng.com", "fortune.com", "businessinsider.com",
    "thelocal.de", "thelocal.se", "techcrunch.com", "theverge.com", "bleepingcomputer.com",
    "helpnetsecurity.com", "krebsonsecurity.com", "thehackernews.com", "securityaffairs.com",
    "securityweek.com", "theregister.com", "malwarebytes.com", "infosecurity-magazine.com",
    "techradar.com", "gizmodo.com", "oilprice.com", "gcaptain.com", "semafor.com",
    "theintercept.com", "thenation.com", "motherjones.com", "huffpost.com", "financialpost.com",
    "rferl.org", "newsonjapan.com", "protothema.gr", "voxeurop.eu", "timeout.com",
    "nature.com", "technologyreview.com", "cryptobriefing.com", "thenextweb.com",
    "siliconangle.com", "itnews.com.au", "pcmag.com", "techspot.com", "ing.dk",
    "peoplesreview.com.np", "khabarhub.com", "antaranews.com", "thechronicle.com.gh",
    "nationalobserver.com", "kqed.org", "wsoctv.com", "democracynow.org", "ibtimes.com.au",
    "hoover.org", "opiniojuris.org", "fairobserver.com", "christiantoday.com",
    "ewtnnews.com", "archdaily.com", "hackaday.com", "techpinions.com", "plos.org",
    "nvidia.com", "online-tech-tips.com",
]

_PRESS_RELEASE = ["prnewswire.com", "businesswire.com", "globenewswire.com"]

_STATE = [
    "rt.com", "sputnikglobe.com", "sputniknews.com", "tass.com", "xinhuanet.com",
    "globaltimes.cn", "cgtn.com", "presstv.ir", "irna.ir", "tasnimnews.com", "aa.com.tr",
]

_ADVOCACY = [
    "breitbart.com", "dailycaller.com", "dailysignal.com", "wnd.com", "thegatewaypundit.com",
    "naturalnews.com", "lewrockwell.com", "wattsupwiththat.com", "order-order.com",
    "nakedcapitalism.com", "commondreams.org", "truthout.org", "juancole.com", "mondoweiss.net",
    "lawyersgunsmoneyblog.com", "crooksandliars.com", "globalresearch.ca", "activistpost.com",
    "dianeravitch.net", "politicalwire.com",
]

_AGGREGATOR = [
    "globalsecurity.org", "biztoc.com", "yahoo.com", "slashdot.org", "newser.com",
    "dailyhunt.in", "medianewsgroup.com", "isegoria.net", "kitploit.com", "freerepublic.com",
    "intomobile.com", "gossiplankanews.com", "snopes.com", "dailyutahchronicle.com",
]

DOMAIN_CATEGORY = {}
for _cat, _domains in (
    ("wire_primary", _WIRE), ("established", _ESTABLISHED), ("press_release", _PRESS_RELEASE),
    ("state_affiliated", _STATE), ("advocacy_opinion", _ADVOCACY), ("aggregator", _AGGREGATOR),
):
    for _d in _domains:
        DOMAIN_CATEGORY[_d] = _cat

# Two-label public suffixes we care about when reducing a host to its
# registrable domain. Not a full public-suffix list -- just what shows up.
_TWO_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "gov.uk", "com.au", "net.au", "org.au", "co.nz", "co.in", "com.np",
    "com.gh", "com.br", "co.jp", "co.za", "com.tr", "com.pk",
}


def domain_of(url_or_host):
    """Registrable domain for a URL or host: 'https://news.un.org/x' -> 'un.org',
    'https://www.bbc.co.uk/x' -> 'bbc.co.uk'. Returns '' if unparseable."""
    s = (url_or_host or "").strip().lower()
    if "//" in s:
        s = urlparse(s).netloc
    s = s.split("@")[-1].split(":")[0].strip(".")
    if s.startswith("www."):
        s = s[4:]
    parts = [p for p in s.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if ".".join(parts[-2:]) in _TWO_LEVEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def category_of(url_or_host):
    """Tier category for an article URL. Tries the full registrable domain
    first, then progressively shorter suffixes of the raw host so entries
    like 'indiatimes.com' match 'economictimes.indiatimes.com'."""
    host = (urlparse(url_or_host).netloc if "//" in (url_or_host or "") else (url_or_host or "")).lower()
    host = host.split(":")[0]
    labels = [p for p in host.split(".") if p]
    for i in range(len(labels) - 1):
        if ".".join(labels[i:]) in DOMAIN_CATEGORY:
            return DOMAIN_CATEGORY[".".join(labels[i:])]
    return "unclassified"


def tier_weight(category):
    return TIER_WEIGHT.get(category, TIER_WEIGHT["unclassified"])


# Wire attribution found in the first few hundred characters of an article's
# description/content: "(AP)", "(Reuters)", "AP -", "By ... (AFP)". Used to
# collapse syndicated copies to their origin wire so ten outlets running one
# AP story count as ONE source, not ten.
_WIRE_TAG_RE = re.compile(
    r"\((AP|Reuters|AFP|Associated Press|Agence France-Presse)\)|"
    r"\b(AP|Reuters|AFP)\s+[-—–]\s|"
    r"^\s*(AP|Reuters|AFP)\b",
    re.IGNORECASE,
)
_WIRE_CANON = {"ap": "ap", "associated press": "ap", "reuters": "reuters",
               "afp": "afp", "agence france-presse": "afp"}


def wire_origin(article):
    """'ap' / 'reuters' / 'afp' if the article's own text credits that wire in
    its lead, else None."""
    text = " ".join([
        (article.get("description") or "")[:300],
        (article.get("content") or "")[:300],
    ])
    m = _WIRE_TAG_RE.search(text)
    if not m:
        return None
    tag = next(g for g in m.groups() if g)
    return _WIRE_CANON.get(tag.lower())
