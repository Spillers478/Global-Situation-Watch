"""
Two dependency-free client-side search widgets, both rendered as inline
HTML+CSS+JS with no backend.

render_search_widget() -- the SITE-WIDE search box, embedded on every
archive index page (docs/archive/index.html and each month's
docs/archive/<YYYY-MM>/index.html). It fetches docs/search-index.json
(built by archive.py from every displayed article across data/<date>/,
rebuilt in full on every run) and filters it in the browser with a plain
substring match on title/topic label/source, then links each result into
the matching topic SECTION of that day's archived page (build_page()'s
render_section gives every section id="<topic_id>") -- a search result
stays anchored in its full context (that day's BLUF, sibling articles, any
reassignment note) instead of dropping the reader straight onto an outside
site. root_prefix is the relative path BACK to the published site root
(docs/) from wherever the embedding page lives, matching the site layout:
  docs/archive/index.html             -> "../"
  docs/archive/<YYYY-MM>/index.html   -> "../../"
A search result's own href in the index ("h") is root-relative (e.g.
"archive/2026-09/2026-09-28.html#T01"), so root_prefix + h always resolves
correctly regardless of the embedding page's depth -- see archive.py's
build_search_index() for how "h" is constructed.

Pass scope_prefix (e.g. "2026-09") to restrict this box to search-index
entries whose date starts with it -- archive.py's render_month_index()
uses this so a month page's search box only searches that month, while
the top-level archive index has no scope and searches every briefing ever
published. scope_label is the human-readable form shown in the
placeholder/aria-label (e.g. "September 2026"); omit it for "all
briefings".

Results are rendered with textContent, never innerHTML, so an article
title containing HTML-looking text can't be interpreted as markup -- the
search index stores raw (unescaped) text for exactly this reason; do not
html.escape() the fields that go into it.

render_page_search_widget() -- the PAGE-LOCAL search box, embedded on
every single-day briefing page (the live docs/index.html and every frozen
docs/archive/<YYYY-MM>/<date>.html) by build_brief.py's build_page().
Unlike the site-wide box above, this one never fetches anything and never
leaves the page: it searches only the headlines, descriptions, "extra"
paragraphs, and AI narrative text already rendered in the DOM, highlights
every match in place with <mark class="gsw-hl">, opens any <details
class="more"> "show more" section that has a hidden match inside it, and
scrolls to the first hit. A day's own briefing already IS "one page" of
content, so a same-page find is the more useful and more honest tool
there; cross-day search stays on the archive index pages via the widget
above (reachable from every day page's header link).
"""
import json
from html import escape as _escape

SEARCH_CSS = """
  .search-wrap { position: relative; margin: 1.1rem 0 0; }
  .search-wrap input[type=search] {
    width: 100%; box-sizing: border-box; background: var(--panel); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 0.65rem 0.9rem;
    font-size: 0.9rem; font-family: inherit;
  }
  .search-wrap input[type=search]:focus { outline: none; border-color: var(--accent); }
  .search-wrap input[type=search]::-webkit-search-cancel-button { cursor: pointer; }
  .search-results {
    position: absolute; left: 0; right: 0; top: calc(100% + 0.35rem); z-index: 20;
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    max-height: 60vh; overflow-y: auto; box-shadow: 0 8px 24px rgba(0,0,0,0.35);
  }
  .sr-item { display: block; padding: 0.6rem 0.9rem; text-decoration: none;
             border-bottom: 1px solid var(--border); }
  .sr-item:last-child { border-bottom: none; }
  .sr-item:hover, .sr-item:focus { background: rgba(77,159,255,0.08); }
  .sr-title { display: block; color: var(--text); font-size: 0.85rem; font-weight: 600; }
  .sr-meta { display: block; color: var(--muted); font-size: 0.72rem; margin-top: 0.15rem; }
  .sr-empty { color: var(--muted); font-size: 0.82rem; padding: 0.7rem 0.9rem; margin: 0; }
"""

PAGE_SEARCH_CSS = """
  .page-search-wrap { position: relative; margin: 1.1rem 0 0; }
  .page-search-wrap input[type=search] {
    width: 100%; box-sizing: border-box; background: var(--panel); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 0.65rem 0.9rem;
    font-size: 0.9rem; font-family: inherit;
  }
  .page-search-wrap input[type=search]:focus { outline: none; border-color: var(--accent); }
  .page-search-wrap input[type=search]::-webkit-search-cancel-button { cursor: pointer; }
  .page-search-count {
    display: block; margin-top: 0.35rem; color: var(--muted); font-size: 0.75rem; min-height: 1em;
  }
  mark.gsw-hl {
    background: rgba(77,159,255,0.4); color: var(--text); border-radius: 2px; padding: 0 0.05em;
  }
"""


def render_search_widget(root_prefix, input_id="gsw-search", scope_prefix=None, scope_label=None):
    label = scope_label or "all briefings"
    placeholder = _escape(f"Search {label}…")
    aria = _escape(f"Search {label}")
    scope_js = json.dumps(scope_prefix)
    return f"""
<div class="search-wrap">
  <input id="{input_id}" type="search" placeholder="{placeholder}" autocomplete="off" aria-label="{aria}">
  <div id="{input_id}-results" class="search-results" hidden></div>
</div>
<script>
(function() {{
  var root = {root_prefix!r};
  var scope = {scope_js};
  var box = document.getElementById("{input_id}");
  var out = document.getElementById("{input_id}-results");
  var idx = null, timer = null;

  function ensureIndex() {{
    if (idx) return Promise.resolve(idx);
    return fetch(root + "search-index.json").then(function(r) {{ return r.json(); }})
      .then(function(d) {{
        idx = scope ? d.filter(function(a) {{ return a.d.indexOf(scope) === 0; }}) : d;
        return idx;
      }})
      .catch(function() {{ idx = []; return idx; }});
  }}

  function clearResults() {{ out.innerHTML = ""; out.hidden = true; }}

  function renderResults(items, q) {{
    out.innerHTML = "";
    if (!items.length) {{
      var empty = document.createElement("p");
      empty.className = "sr-empty";
      empty.textContent = 'No matches for "' + q + '".';
      out.appendChild(empty);
      out.hidden = false;
      return;
    }}
    items.slice(0, 40).forEach(function(a) {{
      var link = document.createElement("a");
      link.className = "sr-item";
      link.href = root + a.h;
      var title = document.createElement("span");
      title.className = "sr-title";
      title.textContent = a.t;
      var meta = document.createElement("span");
      meta.className = "sr-meta";
      meta.textContent = a.d + " · " + a.l + " · " + a.s;
      link.appendChild(title);
      link.appendChild(meta);
      out.appendChild(link);
    }});
    out.hidden = false;
  }}

  box.addEventListener("input", function() {{
    clearTimeout(timer);
    var q = box.value.trim();
    if (q.length < 2) {{ clearResults(); return; }}
    timer = setTimeout(function() {{
      ensureIndex().then(function(items) {{
        var ql = q.toLowerCase();
        var hits = items.filter(function(a) {{
          return a.t.toLowerCase().indexOf(ql) !== -1 ||
                 a.l.toLowerCase().indexOf(ql) !== -1 ||
                 a.s.toLowerCase().indexOf(ql) !== -1;
        }});
        renderResults(hits, q);
      }});
    }}, 150);
  }});

  document.addEventListener("click", function(e) {{
    if (!e.target.closest(".search-wrap")) clearResults();
  }});
  box.addEventListener("keydown", function(e) {{
    if (e.key === "Escape") {{ clearResults(); box.blur(); }}
  }});
}})();
</script>
"""


def render_page_search_widget(input_id="gsw-page-search"):
    """Same-page find-and-highlight -- no fetch, no navigation. Walks every
    headline/description/extra-paragraph/narrative element already in the
    DOM, highlights matches in place, expands any collapsed "show more"
    section that holds a hidden match, and scrolls to the first hit."""
    placeholder = _escape("Search this page…")
    html = """
<div class="page-search-wrap">
  <input id="__ID__" type="search" placeholder="__PLACEHOLDER__" autocomplete="off" aria-label="Search this page">
  <span id="__ID__-count" class="page-search-count"></span>
</div>
<script>
(function() {
  var input = document.getElementById("__ID__");
  var counter = document.getElementById("__ID__-count");
  var timer = null;
  var SELECTOR = ".item-title, .item-desc, .item-extra, .narrative";

  function targets() {
    return Array.prototype.slice.call(document.querySelectorAll(SELECTOR));
  }

  function reset() {
    targets().forEach(function(el) {
      if (el.dataset.gswOrig !== undefined) el.textContent = el.dataset.gswOrig;
    });
    if (counter) counter.textContent = "";
  }

  function highlightOne(el, q) {
    if (el.dataset.gswOrig === undefined) el.dataset.gswOrig = el.textContent;
    var text = el.dataset.gswOrig;
    var lower = text.toLowerCase();
    var qlower = q.toLowerCase();
    var idx = lower.indexOf(qlower);
    if (idx === -1) {
      el.textContent = text;
      return 0;
    }
    el.textContent = "";
    var start = 0, count = 0, i;
    while ((i = lower.indexOf(qlower, start)) !== -1) {
      if (i > start) el.appendChild(document.createTextNode(text.slice(start, i)));
      var mark = document.createElement("mark");
      mark.className = "gsw-hl";
      mark.textContent = text.slice(i, i + q.length);
      el.appendChild(mark);
      start = i + q.length;
      count++;
    }
    if (start < text.length) el.appendChild(document.createTextNode(text.slice(start)));
    return count;
  }

  function highlight(q) {
    var total = 0, first = null;
    targets().forEach(function(el) {
      var n = highlightOne(el, q);
      total += n;
      if (n) {
        if (!first) first = el;
        var details = el.closest("details.more");
        if (details && !details.open) details.open = true;
      }
    });
    if (counter) {
      counter.textContent = total
        ? (total + " match" + (total !== 1 ? "es" : "") + " on this page")
        : 'No matches for "' + q + '".';
    }
    if (first) first.scrollIntoView({behavior: "smooth", block: "center"});
  }

  input.addEventListener("input", function() {
    clearTimeout(timer);
    var q = input.value.trim();
    timer = setTimeout(function() {
      if (q.length < 2) { reset(); return; }
      highlight(q);
    }, 150);
  });
  input.addEventListener("keydown", function(e) {
    if (e.key === "Escape") { input.value = ""; reset(); input.blur(); }
  });
})();
</script>
"""
    return html.replace("__ID__", input_id).replace("__PLACEHOLDER__", placeholder)
