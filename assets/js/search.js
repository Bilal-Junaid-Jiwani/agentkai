/* agentkai docs search — vanilla JS, zero dependencies.
 * Opens #search-modal on Ctrl/Cmd+K or click of #search-open.
 * Filters assets/js/search-index.json client-side with weighted scoring. */
(function () {
  'use strict';

  var MAX_RESULTS = 8;
  var SNIPPET_LEN = 100;
  var INDEX_URL = 'assets/js/search-index.json';

  var modal = null;
  var input = null;
  var resultsEl = null;
  var indexData = null;
  var indexError = false;
  var fetching = false;
  var results = [];
  var activeIdx = -1;

  function esc(str) {
    return String(str).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function ensureIndex(cb) {
    if (indexData || indexError) {
      cb();
      return;
    }
    if (fetching) {
      var poll = setInterval(function () {
        if (!fetching) {
          clearInterval(poll);
          cb();
        }
      }, 50);
      return;
    }
    fetching = true;
    fetch(INDEX_URL)
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        indexData = Array.isArray(data) ? data : [];
      })
      .catch(function () {
        indexError = true;
      })
      .then(function () {
        fetching = false;
        cb();
      });
  }

  /* docs.js owns modal open/close and dispatches 'docs:search-opened' /
     'docs:search-closed' — this file only renders results. */
  function onSearchOpened() {
    if (!modal) return;
    ensureIndex(function () {
      if (indexError) {
        resultsEl.innerHTML = '<p class="search-status">Search index unavailable</p>';
        results = [];
        activeIdx = -1;
      } else {
        renderResults(input.value);
      }
      input.focus();
      input.select();
    });
  }

  function onSearchClosed() {
    activeIdx = -1;
    if (input) {
      input.removeAttribute('aria-activedescendant');
      input.setAttribute('aria-expanded', 'false');
    }
  }

  function scoreEntry(entry, q) {
    var query = q.toLowerCase();
    var score = 0;
    var foundIn = null;
    var matchText = '';

    if (entry.title && entry.title.toLowerCase().indexOf(query) !== -1) {
      score += 3;
      foundIn = 'title';
      matchText = entry.title;
    }
    var headings = Array.isArray(entry.headings) ? entry.headings : [];
    var hi;
    for (hi = 0; hi < headings.length; hi++) {
      var hText = headings[hi] && headings[hi].text ? headings[hi].text : headings[hi];
      if (hText && hText.toLowerCase().indexOf(query) !== -1) {
        score += 2;
        if (!foundIn) {
          foundIn = 'heading';
          matchText = hText;
        }
        break;
      }
    }
    var body = [entry.eyebrow, entry.description, entry.text]
      .filter(Boolean)
      .join(' ');
    if (body.toLowerCase().indexOf(query) !== -1) {
      score += 1;
      if (!foundIn) {
        foundIn = 'text';
        matchText = body;
      }
    }
    return score > 0 ? { score: score, snippet: makeSnippet(matchText, query) } : null;
  }

  function makeSnippet(text, query) {
    if (!text) return '';
    var lower = text.toLowerCase();
    var pos = lower.indexOf(query);
    if (pos === -1) {
      return esc(text.slice(0, SNIPPET_LEN));
    }
    var half = Math.floor(SNIPPET_LEN / 2);
    var start = Math.max(0, pos - half);
    var end = Math.min(text.length, start + SNIPPET_LEN);
    if (end - start < SNIPPET_LEN) start = Math.max(0, end - SNIPPET_LEN);
    var slice = text.slice(start, end);
    var localPos = pos - start;
    var before = esc(slice.slice(0, localPos));
    var match = esc(slice.slice(localPos, localPos + query.length));
    var after = esc(slice.slice(localPos + query.length));
    return (start > 0 ? '…' : '') + before + '<mark>' + match + '</mark>' + after + (end < text.length ? '…' : '');
  }

  function setExpanded(open) {
    if (input) input.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function renderResults(query) {
    results = [];
    activeIdx = -1;
    var q = query.trim();
    if (!q) {
      resultsEl.innerHTML = '<p class="search-status">Type to search the docs</p>';
      setExpanded(false);
      if (input) input.removeAttribute('aria-activedescendant');
      return;
    }
    if (indexError) {
      resultsEl.innerHTML = '<p class="search-status">Search index unavailable</p>';
      setExpanded(false);
      if (input) input.removeAttribute('aria-activedescendant');
      return;
    }
    if (!indexData || !indexData.length) {
      resultsEl.innerHTML = '<p class="search-status">Search index unavailable</p>';
      setExpanded(false);
      if (input) input.removeAttribute('aria-activedescendant');
      return;
    }
    var scored = [];
    indexData.forEach(function (entry) {
      var hit = scoreEntry(entry, q);
      if (hit) scored.push({ entry: entry, score: hit.score, snippet: hit.snippet });
    });
    scored.sort(function (a, b) { return b.score - a.score; });
    results = scored.slice(0, MAX_RESULTS);

    if (!results.length) {
      resultsEl.innerHTML = '<p class="search-status">No results for "' + esc(q) + '"</p>';
      setExpanded(false);
      if (input) input.removeAttribute('aria-activedescendant');
      return;
    }
    // The outer #search-results container carries role="listbox", so the
    // list itself is role="presentation": the options are owned directly by
    // the listbox. Each option gets an id wired to the combobox input via
    // aria-activedescendant so screen readers announce the active result.
    var html = '<ul class="search-list" role="presentation">';
    results.forEach(function (r, i) {
      var e = r.entry;
      html += '<li role="option" id="search-option-' + i + '" aria-selected="' + (i === 0) + '" data-idx="' + i + '">'
        + '<a href="' + esc(e.url) + '" data-idx="' + i + '" tabindex="-1">'
        + '<span class="search-eyebrow">' + esc(e.eyebrow || '') + '</span>'
        + '<span class="search-title">' + esc(e.title || '') + '</span>'
        + '<span class="search-snippet">' + r.snippet + '</span>'
        + '</a></li>';
    });
    html += '</ul>';
    resultsEl.innerHTML = html;
    activeIdx = 0;
    setExpanded(true);
    paintActive();
  }

  function paintActive() {
    var items = resultsEl.querySelectorAll('.search-list li');
    items.forEach(function (li, i) {
      var selected = i === activeIdx;
      li.classList.toggle('is-active', selected);
      li.setAttribute('aria-selected', selected ? 'true' : 'false');
      if (selected && input) {
        input.setAttribute('aria-activedescendant', li.id);
      }
    });
    if (activeIdx < 0 && input) {
      input.removeAttribute('aria-activedescendant');
    }
  }

  function openActive() {
    if (activeIdx >= 0 && results[activeIdx]) {
      window.location.href = results[activeIdx].entry.url;
    }
  }

  function init() {
    modal = document.getElementById('search-modal');
    input = document.getElementById('search-input');
    resultsEl = document.getElementById('search-results');
    if (!modal || !input || !resultsEl) return;

    modal.addEventListener('click', function (e) {
      var link = e.target.closest ? e.target.closest('a[data-idx]') : null;
      if (link) {
        e.preventDefault();
        activeIdx = parseInt(link.getAttribute('data-idx'), 10);
        openActive();
      }
    });

    document.addEventListener('docs:search-opened', onSearchOpened);
    document.addEventListener('docs:search-closed', onSearchClosed);

    input.addEventListener('input', function () {
      ensureIndex(function () { renderResults(input.value); });
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (results.length) {
          activeIdx = (activeIdx + 1) % results.length;
          paintActive();
        }
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (results.length) {
          activeIdx = (activeIdx - 1 + results.length) % results.length;
          paintActive();
        }
      } else if (e.key === 'Enter') {
        e.preventDefault();
        openActive();
      }
    });

  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
