/* docs.js — agentkai docs site chrome behaviour (vanilla JS, no dependencies).
 *
 * Responsibilities of THIS file:
 *   - mobile sidebar drawer open/close (focus trap, Esc, scrim, return focus)
 *   - "On this page" TOC scroll-spy (IntersectionObserver)
 *   - copy buttons injected into every pre.codeblock (clipboard API + fallback)
 *   - #copy-page button (copies page title + URL + plain text)
 *   - Ctrl/⌘+K toggles the search modal; Esc closes it
 *
 * NOT this file's job (see assets/js/search.js, owned by the search worker):
 *   - rendering the search results list or handling #search-input keystrokes.
 *   docs.js only toggles modal visibility and dispatches the
 *   'docs:search-opened' / 'docs:search-closed' CustomEvents so search.js
 *   can hook in without either file fighting over the same elements.
 *
 * Active tab / sidebar highlighting is baked statically by build.py via
 * data-active attributes — docs.js does not touch nav highlighting.
 */
(function () {
  'use strict';

  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $all(sel, ctx) {
    return Array.prototype.slice.call((ctx || document).querySelectorAll(sel));
  }

  // Tabbable elements inside a container, in DOM order. Excludes disabled
  // controls and anything deliberately removed from the tab order
  // (tabindex="-1", e.g. search result links, which are arrow-navigated).
  function tabbables(container) {
    return $all(
      'a[href], button, input, select, textarea, [tabindex]',
      container
    ).filter(function (el) {
      return !el.disabled && el.tabIndex >= 0 && el.offsetParent !== null;
    });
  }

  /* ------------------------------------------------------------------ */
  /* Clipboard helper: navigator.clipboard with a textarea fallback.     */
  /* ------------------------------------------------------------------ */
  function copyText(text, done) {
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      done(ok);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { done(true); },
        function () { fallback(); }
      );
    } else {
      fallback();
    }
  }

  /* ------------------------------------------------------------------ */
  /* Mobile sidebar drawer (<800px).                                     */
  /* ------------------------------------------------------------------ */
  var drawerOpenBtn = $('#drawer-open');
  var drawerCloseBtn = $('#drawer-close');
  var sidebar = $('#sidebar');
  var drawerScrim = $('#drawer-scrim');
  var lastFocus = null;

  function drawerIsOpen() {
    return sidebar && sidebar.classList.contains('open');
  }

  function openDrawer() {
    if (!sidebar || !drawerScrim) return;
    lastFocus = document.activeElement;
    sidebar.classList.add('open');
    drawerScrim.hidden = false;
    document.body.classList.add('drawer-open');
    if (drawerOpenBtn) drawerOpenBtn.setAttribute('aria-expanded', 'true');
    document.addEventListener('keydown', onDrawerKey, true);
    if (drawerCloseBtn) drawerCloseBtn.focus();
  }

  function closeDrawer() {
    if (!drawerIsOpen()) return;
    sidebar.classList.remove('open');
    drawerScrim.hidden = true;
    document.body.classList.remove('drawer-open');
    if (drawerOpenBtn) drawerOpenBtn.setAttribute('aria-expanded', 'false');
    document.removeEventListener('keydown', onDrawerKey, true);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  // Focus trap: keep Tab cycling inside the open drawer.
  function onDrawerKey(e) {
    if (e.key === 'Escape') { closeDrawer(); return; }
    if (e.key !== 'Tab' || !sidebar) return;
    var focusables = tabbables(sidebar);
    if (!focusables.length) return;
    var first = focusables[0];
    var last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  }

  if (drawerOpenBtn) drawerOpenBtn.addEventListener('click', openDrawer);
  if (drawerCloseBtn) drawerCloseBtn.addEventListener('click', closeDrawer);
  if (drawerScrim) drawerScrim.addEventListener('click', closeDrawer);
  // A nav tap in the drawer means the user picked a destination: close it.
  if (sidebar) {
    $all('a', sidebar).forEach(function (a) {
      a.addEventListener('click', closeDrawer);
    });
  }

  /* ------------------------------------------------------------------ */
  /* TOC scroll-spy: highlight the heading currently in view.            */
  /* ------------------------------------------------------------------ */
  var tocLinks = $all('.toc a[href^="#"]');
  if (tocLinks.length && 'IntersectionObserver' in window) {
    var byId = {};
    tocLinks.forEach(function (a) {
      byId[a.getAttribute('href').slice(1)] = a;
    });
    var spy = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        $all('.toc a[data-active]').forEach(function (a) {
          a.removeAttribute('data-active');
        });
        var link = byId[entry.target.id];
        if (link) link.setAttribute('data-active', 'true');
      });
    }, { rootMargin: '-15% 0px -70% 0px' });
    Object.keys(byId).forEach(function (id) {
      var el = document.getElementById(id);
      if (el) spy.observe(el);
    });
  }

  /* ------------------------------------------------------------------ */
  /* Copy buttons on every pre.codeblock.                                */
  /* ------------------------------------------------------------------ */
  $all('pre.codeblock').forEach(function (pre) {
    // Don't double-inject if the page is ever re-processed.
    if (pre.querySelector('.code-copy')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'code-copy';
    btn.setAttribute('aria-label', 'Copy code to clipboard');
    btn.innerHTML =
      '<svg width="13" height="13" viewBox="0 0 14 14" aria-hidden="true">' +
      '<rect x="4.5" y="4.5" width="8" height="8" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.25"/>' +
      '<path d="M9.5 4.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2" fill="none" stroke="currentColor" stroke-width="1.25"/>' +
      '</svg><span class="code-copy-label">Copy</span>';
    btn.addEventListener('click', function () {
      var codeEl = pre.querySelector('code');
      var text = codeEl ? codeEl.innerText : pre.innerText;
      var label = btn.querySelector('.code-copy-label');
      copyText(text, function (ok) {
        btn.classList.add('copied');
        if (label) label.textContent = ok ? 'Copied' : 'Failed';
        setTimeout(function () {
          btn.classList.remove('copied');
          if (label) label.textContent = 'Copy';
        }, 2000);
      });
    });
    pre.appendChild(btn);
  });

  /* ------------------------------------------------------------------ */
  /* #copy-page: title + URL + plain text of the article.                */
  /* ------------------------------------------------------------------ */
  var copyPageBtn = $('#copy-page');
  if (copyPageBtn) {
    copyPageBtn.addEventListener('click', function () {
      var label = copyPageBtn.querySelector('span');
      var prose = $('.prose');
      var text = prose ? prose.innerText : document.body.innerText;
      var payload = document.title + '\n' +
        window.location.href.split('#')[0] + '\n\n' + text;
      copyText(payload, function (ok) {
        copyPageBtn.classList.add('copied');
        if (label) label.textContent = ok ? 'Copied' : 'Failed';
        setTimeout(function () {
          copyPageBtn.classList.remove('copied');
          if (label) label.textContent = 'Copy page';
        }, 2000);
      });
    });
  }

  /* ------------------------------------------------------------------ */
  /* Search modal: docs.js owns OPEN/CLOSE only. Rendering of the input  */
  /* results list lives in search.js, which should listen for the       */
  /* 'docs:search-opened' / 'docs:search-closed' events below.          */
  /* ------------------------------------------------------------------ */
  var searchModal = $('#search-modal');
  var searchScrim = $('#search-scrim');
  var searchInput = $('#search-input');
  var searchOpenBtn = $('#search-open');
  var searchCloseBtn = $('#search-close');

  function searchIsOpen() {
    return searchModal && !searchModal.hidden;
  }

  function openSearch() {
    if (!searchModal || !searchModal.hidden) return;
    searchModal.hidden = false;
    searchModal.classList.add('open');
    if (searchScrim) { searchScrim.hidden = false; searchScrim.classList.add('open'); }
    document.body.classList.add('search-open');
    document.addEventListener('keydown', onSearchKey, true);
    document.dispatchEvent(new CustomEvent('docs:search-opened'));
    // Focus after the modal paints so screen readers catch it.
    setTimeout(function () { if (searchInput) searchInput.focus(); }, 0);
  }

  function closeSearch() {
    if (!searchModal || searchModal.hidden) return;
    searchModal.hidden = true;
    searchModal.classList.remove('open');
    if (searchScrim) { searchScrim.hidden = true; searchScrim.classList.remove('open'); }
    document.body.classList.remove('search-open');
    document.removeEventListener('keydown', onSearchKey, true);
    document.dispatchEvent(new CustomEvent('docs:search-closed'));
    // Return focus to the trigger that opened the dialog.
    if (searchOpenBtn) searchOpenBtn.focus();
  }

  // Focus trap for the search modal: keep Tab cycling inside it while open.
  function onSearchKey(e) {
    if (e.key === 'Escape') { closeSearch(); return; }
    if (e.key !== 'Tab' || !searchModal) return;
    var focusables = tabbables(searchModal);
    if (!focusables.length) return;
    var first = focusables[0];
    var last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  }

  if (searchOpenBtn) searchOpenBtn.addEventListener('click', openSearch);
  if (searchCloseBtn) searchCloseBtn.addEventListener('click', closeSearch);
  if (searchScrim) searchScrim.addEventListener('click', closeSearch);

  document.addEventListener('keydown', function (e) {
    var isK = e.key === 'k' || e.key === 'K';
    if ((e.ctrlKey || e.metaKey) && isK) {
      e.preventDefault();
      if (searchIsOpen()) closeSearch();
      else openSearch();
    } else if (e.key === 'Escape') {
      closeSearch();
      closeDrawer();
    }
  });
})();
