"""
Shared client-side search box embedded on the live page (docs/index.html)
and every archive index page (docs/archive/index.html and each month's
docs/archive/<YYYY-MM>/index.html). There is no backend -- it fetches
docs/search-index.json (built by archive.py from every displayed article
across data/<date>/, rebuilt in full on every run) and filters it in the
browser with a plain substring match on title/topic label/source.

render_search_widget(root_prefix) returns the HTML+JS to embed. root_prefix
is the relative path BACK to the published site root (docs/) from wherever
the embedding page lives, matching the site layout:
  docs/index.html                     -> ""
  docs/archive/index.html             -> "../"
  docs/archive/<YYYY-MM>/index.html   -> "../../"
  docs/archive/<YYYY-MM>/<date>.html  -> "../../"
A search result's own href in the index ("h") is root-relative (e.g.
"archive/2026-09/2026-09-28.html#T01"), so root_prefix + h always resolves
correctly regardless of the embedding page's depth -- see archive.py's
build_search_index() for how "h" is constructed.

Results link into the matching topic SECTION of that day's archived page
(build_page()'s render_section gives every section id="<topic_id>"), not
out to the original article -- a search result stays anchored in its full
context (that day's BLUF, sibling articles, any reassignment note) instead
of dropping the reader straight onto an outside site.

Results are rendered with textContent, never innerHTML, so an article
title containing HTML-looking text can't be interpreted as markup -- the
search index stores raw (unescaped) text for exactly this reason; do not
html.escape() the fields that go into it.
"""
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


def render_search_widget(root_prefix, input_id="gsw-search"):
    placeholder = _escape("Search all briefings…")
    return f"""
<div class="search-wrap">
  <input id="{input_id}" type="search" placeholder="{placeholder}" autocomplete="off" aria-label="Search all briefings">
  <div id="{input_id}-results" class="search-results" hidden></div>
</div>
<script>
(function() {{
  var root = {root_prefix!r};
  var box = document.getElementById("{input_id}");
  var out = document.getElementById("{input_id}-results");
  var idx = null, timer = null;

  function ensureIndex() {{
    if (idx) return Promise.resolve(idx);
    return fetch(root + "search-index.json").then(function(r) {{ return r.json(); }})
      .then(function(d) {{ idx = d; return d; }})
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
