/* Shared browser helpers for the exported archive.
 *
 * Loaded by index.html and every folder page. No dependencies, no network:
 * the archive has to work from a file:// URL with no server.
 *
 * Relative timestamps are computed in the browser rather than baked into the
 * HTML, because "3 months ago" is only true on the day it is written. The
 * absolute date stays in the `title` attribute so nothing is lost.
 */
(function (global) {
  'use strict';

  var MINUTE = 60,
      HOUR = 3600,
      DAY = 86400,
      WEEK = 604800,
      MONTH = 2629746,   // average Gregorian month
      YEAR = 31556952;   // average Gregorian year

  function plural(n, unit) {
    return n + ' ' + unit + (n === 1 ? '' : 's') + ' ago';
  }

  /* Microseconds since the epoch (Quip's unit) -> "3 months ago". */
  function relativeTime(usec, nowMs) {
    var ms = Number(usec) / 1000;
    if (!isFinite(ms) || ms <= 0) { return ''; }
    var seconds = ((nowMs === undefined ? Date.now() : nowMs) - ms) / 1000;

    if (seconds < 0) { return 'just now'; }        // clock skew, not the future
    if (seconds < 45) { return 'just now'; }
    if (seconds < 90) { return '1 minute ago'; }
    if (seconds < HOUR) { return plural(Math.round(seconds / MINUTE), 'minute'); }
    if (seconds < DAY) { return plural(Math.round(seconds / HOUR), 'hour'); }
    if (seconds < WEEK) { return plural(Math.round(seconds / DAY), 'day'); }
    if (seconds < MONTH) { return plural(Math.round(seconds / WEEK), 'week'); }
    if (seconds < YEAR) { return plural(Math.round(seconds / MONTH), 'month'); }
    return plural(Math.round(seconds / YEAR), 'year');
  }

  /* "3 days ago" while it is still useful, then a plain date.
     Past about a month "5 months ago" is worse than "Jan 17, 2023": it is
     harder to compare two of them, and it decays as the archive ages. */
  function shortDate(usec) {
    var ms = Number(usec) / 1000;
    if (!isFinite(ms) || ms <= 0) { return ''; }
    var d = new Date(ms);
    try {
      return d.toLocaleDateString(undefined,
        { year: 'numeric', month: 'short', day: 'numeric' });
    } catch (e) {
      return d.toDateString();
    }
  }

  function recentTime(usec, nowMs) {
    var ms = Number(usec) / 1000;
    if (!isFinite(ms) || ms <= 0) { return ''; }
    var seconds = ((nowMs === undefined ? Date.now() : nowMs) - ms) / 1000;
    return seconds < MONTH ? relativeTime(usec, nowMs) : shortDate(usec);
  }

  /* Absolute date for the tooltip, so the relative form never loses detail. */
  function absoluteDate(usec) {
    var ms = Number(usec) / 1000;
    if (!isFinite(ms) || ms <= 0) { return ''; }
    var d = new Date(ms);
    try {
      return d.toLocaleDateString(undefined,
        { year: 'numeric', month: 'long', day: 'numeric' });
    } catch (e) {
      return d.toDateString();
    }
  }

  var KINDS = ['folder', 'document', 'spreadsheet', 'slides', 'chat'];

  /* Icons are inlined once per page as a <symbol> sprite -- they carry no
     xmlns, so loading them through <img src> would render nothing. Rows just
     reference the sprite. */
  function iconHtml(kind, color) {
    var key = KINDS.indexOf(String(kind || '').toLowerCase()) >= 0
      ? String(kind).toLowerCase() : 'document';
    /* Folder colour, as Quip names it. Mirrors templates.icon_use: an unknown
       name simply adds no class, leaving the manila default. */
    var cls = 'quip-icon';
    if (key === 'folder') {
      /* Always a colour class: Quip's system roots report no colour, and
         without one the icon inherits the link colour. A name the stylesheet
         does not know still matches the [class*=] default rule. */
      cls += /^[a-z_]+$/.test(String(color || ''))
        ? ' quip-folder-' + color : ' quip-folder-manila';
    }
    return '<svg class="' + cls + '" aria-hidden="true" focusable="false">' +
           '<use href="#quip-icon-' + key + '"></use></svg>';
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* Recents filter: hide documents created before a chosen date.

     Filtering on *creation* rather than modification is the point. Some
     accounts have documents whose `updated_usec` moved for reasons that were
     not a person editing them -- one real account had 165 pre-2020 documents
     all change within a single week -- and those swamp a list ordered by
     modification. A creation date does not move. */
  function initRecentFilter(root) {
    var scope = root || document;
    if (!scope || typeof scope.querySelector !== 'function') { return; }
    var input = scope.querySelector('#quip-created-after');
    if (!input) { return; }   /* every page but recents */
    var clear = scope.querySelector('#quip-created-clear');
    var status = scope.querySelector('#quip-filter-status');
    var items = scope.querySelectorAll('.quip-recent-item');
    var groups = scope.querySelectorAll('.quip-day-group');

    function apply() {
      /* Parse as UTC midnight: `new Date("2020-01-01")` is already UTC, but
         being explicit keeps it away from the browser's local timezone. */
      var cutoff = input.value ? Date.parse(input.value + 'T00:00:00Z') : null;
      var hidden = 0;
      for (var i = 0; i < items.length; i++) {
        var created = Number(items[i].getAttribute('data-quip-created')) / 1000;
        var hide = cutoff !== null && isFinite(created) && created < cutoff;
        items[i].hidden = hide;
        if (hide) { hidden++; }
      }
      /* A day heading with nothing under it is noise. */
      for (var g = 0; g < groups.length; g++) {
        var live = groups[g].querySelectorAll('.quip-recent-item:not([hidden])');
        groups[g].hidden = live.length === 0;
      }
      if (status) {
        status.textContent = hidden
          ? hidden + ' of ' + items.length + ' hidden'
          : (input.value ? 'nothing hidden' : '');
      }
    }

    input.addEventListener('change', apply);
    input.addEventListener('input', apply);
    if (clear) {
      clear.addEventListener('click', function () {
        input.value = '';
        apply();
      });
    }
    apply();
  }

  /* Site-wide search, on every page.

     The index is `window.QUIP_INDEX`, written once to quip-search-index.js and
     shared by every page -- inlining ~1 MB into 1,592 documents is not a thing
     to put on disk. Entry fields are short because the file is large:
       t title, p path, f folder, k kind, c folder colour,
       u updated_usec, cr created_usec, m member count, s sharing bucket
  */
  function initSiteSearch(root) {
    var scope = root || document;
    if (!scope || typeof scope.querySelector !== 'function') { return; }
    var box = scope.querySelector('#quip-q');
    var out = scope.querySelector('#quip-results');
    if (!box || !out) { return; }

    var f = {
      type: scope.querySelector('#quip-f-type'),
      share: scope.querySelector('#quip-f-share'),
      members: scope.querySelector('#quip-f-members'),
      created: scope.querySelector('#quip-f-created'),
      modified: scope.querySelector('#quip-f-modified')
    };
    var clear = scope.querySelector('#quip-f-clear');
    /* The index page shows a folder tree; hide it while results are up so the
       two lists are never on screen at once. */
    var tree = scope.querySelector('#tree');

    function stamp(input) {
      return input && input.value ? Date.parse(input.value + 'T00:00:00Z') : null;
    }

    function active() {
      return (box.value.trim() !== '') ||
        (f.type && f.type.value) || (f.share && f.share.value) ||
        (f.members && f.members.value !== '') ||
        (f.created && f.created.value) || (f.modified && f.modified.value);
    }

    function matches(e, term, createdAfter, modifiedAfter, minMembers) {
      if (term && !((e.t || '').toLowerCase().indexOf(term) >= 0 ||
                    (e.f || '').toLowerCase().indexOf(term) >= 0)) { return false; }
      if (f.type && f.type.value && e.k !== f.type.value) { return false; }
      if (f.share && f.share.value && (e.s || '') !== f.share.value) { return false; }
      if (minMembers !== null && !(Number(e.m || 0) >= minMembers)) { return false; }
      /* Folders carry no creation date; a date filter is about documents, so
         they drop out rather than pretending to satisfy it. */
      if (createdAfter !== null && !(Number(e.cr || 0) / 1000 >= createdAfter)) {
        return false;
      }
      if (modifiedAfter !== null && !(Number(e.u || 0) / 1000 >= modifiedAfter)) {
        return false;
      }
      return true;
    }

    /* Index paths are relative to the export root; a page three folders deep
       has to climb back out before following one. The nav's first link already
       encodes that climb, so it is read back off the DOM rather than threaded
       through every call site. */
    var prefix = (function () {
      var link = scope.querySelector('.quip-nav a');
      var href = link ? link.getAttribute('href') : '';
      var cut = href.lastIndexOf('index.html');
      return cut > 0 ? href.slice(0, cut) : '';
    })();

    function apply() {
      var index = global.QUIP_INDEX || [];
      if (!active()) {
        out.hidden = true;
        out.innerHTML = '';
        if (tree) { tree.style.display = ''; }
        return;
      }
      var term = box.value.trim().toLowerCase();
      var createdAfter = stamp(f.created), modifiedAfter = stamp(f.modified);
      var minMembers = (f.members && f.members.value !== '')
        ? Number(f.members.value) : null;

      var hits = [];
      for (var i = 0; i < index.length; i++) {
        if (matches(index[i], term, createdAfter, modifiedAfter, minMembers)) {
          hits.push(index[i]);
        }
      }
      if (tree) { tree.style.display = 'none'; }
      out.hidden = false;
      if (!hits.length) { out.innerHTML = '<p class="quip-no-hits">No matches.</p>'; return; }
      var shown = hits.slice(0, 300);
      var html = '<p class="quip-hit-count">' + hits.length + ' match' +
        (hits.length === 1 ? '' : 'es') +
        (hits.length > shown.length ? ', showing the first ' + shown.length : '') +
        '</p><ul class="quip-listing">';
      for (var j = 0; j < shown.length; j++) {
        html += '<li>' + entryHtml(shown[j], prefix) + '</li>';
      }
      out.innerHTML = html + '</ul>';
      applyRelativeTimes(out);
    }

    [box, f.type, f.share, f.members, f.created, f.modified].forEach(function (el) {
      if (!el) { return; }
      el.addEventListener('input', apply);
      el.addEventListener('change', apply);
    });
    if (clear) {
      clear.addEventListener('click', function () {
        [f.type, f.share, f.members, f.created, f.modified].forEach(function (el) {
          if (el) { el.value = ''; }
        });
        apply();
      });
    }
    apply();
  }

  /* Fill in every [data-quip-updated] element under `root`. */
  function applyRelativeTimes(root) {
    var nodes = (root || document).querySelectorAll('[data-quip-updated]');
    var now = Date.now();
    for (var i = 0; i < nodes.length; i++) {
      var usec = nodes[i].getAttribute('data-quip-updated');
      /* The recents list switches to an exact date once "N months ago" stops
         being the more readable of the two. */
      var rel = nodes[i].getAttribute('data-quip-when') === 'recent'
        ? recentTime(usec, now) : relativeTime(usec, now);
      if (!rel) { continue; }
      nodes[i].textContent = rel;
      if (!nodes[i].getAttribute('title')) {
        nodes[i].setAttribute('title', 'Last modified ' + absoluteDate(usec));
      }
    }
  }

  /* One row: icon, title, relative date. Used by the search results so they
     match the static tree exactly. */
  function entryHtml(entry, prefix) {
    /* Mirrors entry_row() in reports.py, folder class included, so a search
       result and the same row in the tree look identical.

       `prefix` climbs back to the export root: index paths are stored relative
       to it, so a result shown on a page three folders deep needs "../../../"
       in front or the link resolves against the wrong directory. */
    var cls = 'quip-entry' +
      (String(entry.k || '').toLowerCase() === 'folder' ? ' quip-folder' : '');
    return '<a class="' + cls + '" href="' + (prefix || '') + encodeURI(entry.p) + '">' +
      iconHtml(entry.k, entry.c) +
      '<span class="quip-entry-title">' + escapeHtml(entry.t) + '</span>' +
      (entry.u ? '<span class="quip-when" data-quip-updated="' + entry.u + '"></span>' : '') +
      (entry.f ? '<span class="quip-entry-folder">' + escapeHtml(entry.f) + '</span>' : '') +
      '</a>';
  }

  /* Folder collapse. One delegated listener, so it also covers any rows added
     later. The toggle is a <button> beside the link rather than part of it, so
     collapsing never navigates. */
  function onToggleClick(event) {
    var button = event.target.closest ? event.target.closest('.quip-toggle') : null;
    if (!button) { return; }
    event.preventDefault();
    event.stopPropagation();

    var li = button.parentNode && button.parentNode.parentNode;   // .quip-row -> li
    var list = li && li.querySelector(':scope > ul.quip-tree');
    if (!list) { return; }

    var expanded = button.getAttribute('aria-expanded') !== 'false';
    button.setAttribute('aria-expanded', expanded ? 'false' : 'true');
    button.setAttribute('aria-label', expanded ? 'Expand folder' : 'Collapse folder');
    list.hidden = expanded;
  }

  function initTree(root) {
    (root || document).addEventListener('click', onToggleClick);
  }

  /* ---- syntax highlighting -------------------------------------------
   *
   * Only blocks Quip itself marked as code (`pre.prettyprint`). Of the 108
   * <pre> elements in the test account, 106 are live-app YAML dumps inside
   * <details> -- machine internals, one of them 1.26 MB -- and highlighting
   * those would be both wrong and slow enough to freeze the tab.
   *
   * Runs in the browser, never at export time: the stored HTML stays exactly
   * as Quip sent it, so `verify`'s text comparison is unaffected and a reader
   * with JavaScript off still gets a perfectly good plain code block.
   */

  /* The languages Quip itself offers for code blocks, so a block written in
     Quip can be recognised as what it was. Restricting the candidate set also
     detects better than offering every grammar: `console.log("hey")` is picked
     as C++ against all 39 and JavaScript against this list. */
  var LANGUAGE_LABELS = [
    ['auto', 'Auto'],
    ['plaintext', 'Plain text'],
    ['c', 'C'], ['cpp', 'C++'], ['coffeescript', 'CoffeeScript'],
    ['csharp', 'C#'], ['css', 'CSS'], ['go', 'Go'], ['xml', 'HTML'],
    ['java', 'Java'], ['javascript', 'JavaScript'], ['json', 'JSON'],
    ['kotlin', 'Kotlin'], ['lisp', 'LISP'], ['perl', 'Perl'],
    ['protobuf', 'Protocol Buffer'], ['python', 'Python'], ['ruby', 'Ruby'],
    ['rust', 'Rust'], ['bash', 'Shell Script'], ['sql', 'SQL'],
    ['swift', 'Swift'], ['typescript', 'TypeScript'], ['yaml', 'YAML']
  ];
  /* `auto` and `plaintext` are menu choices, not grammars to detect against. */
  var CODE_LANGUAGES = LANGUAGE_LABELS
    .map(function (p) { return p[0]; })
    .filter(function (id) { return id !== 'plaintext' && id !== 'auto'; });
  var MAX_HIGHLIGHT_BYTES = 100000;
  /* Measured on this corpus: correct guesses scored 6 (JS), 5 (Python) and
     3 (SQL); prose scored 1. Two is the floor that keeps the real code and
     drops the noise. */
  var MIN_RELEVANCE = 2;

  /* Quip stores newlines inside a code block as <br>, which textContent drops
     -- taking it directly would run the whole program onto one line. */
  function codeText(pre) {
    var out = '';
    (function walk(node) {
      for (var i = 0; i < node.childNodes.length; i++) {
        var n = node.childNodes[i];
        if (n.nodeType === 3) { out += n.nodeValue; }
        else if (n.nodeName === 'BR') { out += '\n'; }
        else if (n.nodeType === 1) { walk(n); }
      }
    })(pre);
    return out;
  }

  /* Put the line breaks back the way Quip stores them.
   *
   * highlight.js returns literal newlines; Quip's own HTML uses <br> and never
   * emits a newline at all. Converting back means the rendered line structure
   * does not depend on the stylesheet -- a `white-space` value that collapses
   * newlines once ran an entire program onto a single line, and line breaks in
   * imported code are not something to leave resting on a CSS property.
   *
   * Safe as a string replace: highlight.js escapes the source text, and the
   * tags it emits never contain a newline.
   */
  function asLines(html) {
    return html.replace(/\n/g, '<br>');
  }

  function highlightCode(root) {
    if (typeof hljs === 'undefined') { return 0; }
    var blocks = (root || document).querySelectorAll('pre.prettyprint');
    var done = 0;
    for (var i = 0; i < blocks.length; i++) {
      var pre = blocks[i];
      /* Anything richer than text and <br> -- a link, an image, or an element
         carrying an id a comment may anchor to -- is left alone, because
         rewriting the block would destroy it. */
      if (pre.querySelector('a, img, [id]')) { continue; }
      var code = codeText(pre);
      if (!code.trim() || code.length > MAX_HIGHLIGHT_BYTES) { continue; }

      var result;
      try { result = hljs.highlightAuto(code, CODE_LANGUAGES); }
      catch (e) { continue; }

      /* Recorded whether or not it is used, so the guess is inspectable rather
         than silently asserted. */
      pre.setAttribute('data-quip-language', result.language || 'unknown');
      pre.setAttribute('data-quip-relevance', String(result.relevance));
      if (!result.language || result.relevance < MIN_RELEVANCE) { continue; }

      pre.innerHTML = asLines(result.value);
      pre.className += ' hljs quip-highlighted';
      addLanguageSwitcher(pre, code, result.language);
      done++;
    }
    return done;
  }

  /* Let a reader correct the guess.
   *
   * highlight.js ships no UI for this, but re-rendering in a chosen language is
   * a single call, so the switcher is a small addition -- and detection is
   * genuinely unreliable on short snippets: `interface P { name: string }` is
   * detected as CSS, because it looks like one. Without a way to override, a
   * wrong guess would be permanent.
   */
  function addLanguageSwitcher(pre, code, detected) {
    if (!pre.parentNode || pre.parentNode.className === 'quip-code-block') { return; }
    var wrap = document.createElement('div');
    wrap.className = 'quip-code-block';
    pre.parentNode.insertBefore(wrap, pre);
    wrap.appendChild(pre);

    /* The control sits in its own bar *above* the block. Floating it over the
       top-right corner covered the first line of code, which is usually the
       line you most want to read. */
    var bar = document.createElement('div');
    bar.className = 'quip-code-bar';
    bar.innerHTML = '<svg class="quip-code-icon" aria-hidden="true" ' +
                    'focusable="false"><use href="#quip-icon-code"></use></svg>';

    var select = document.createElement('select');
    select.className = 'quip-code-lang';
    select.setAttribute('aria-label', 'Syntax highlighting language');
    for (var i = 0; i < LANGUAGE_LABELS.length; i++) {
      var option = document.createElement('option');
      option.value = LANGUAGE_LABELS[i][0];
      option.textContent = LANGUAGE_LABELS[i][1];
      select.appendChild(option);
    }
    /* Auto is the default and what is actually rendered; the detected language
       is named in the tooltip so the guess stays visible without a second
       control. */
    select.value = 'auto';
    select.title = 'Detected: ' + detected;

    function render(language) {
      if (language === 'auto') {
        var auto = hljs.highlightAuto(code, CODE_LANGUAGES);
        pre.setAttribute('data-quip-language', auto.language || 'unknown');
        pre.setAttribute('data-quip-relevance', String(auto.relevance));
        select.title = 'Detected: ' + (auto.language || 'unknown');
        pre.innerHTML = asLines(auto.value);
        if (pre.className.indexOf('hljs') < 0) { pre.className += ' hljs'; }
        return;
      }
      pre.setAttribute('data-quip-language', language);
      pre.setAttribute('data-quip-relevance', 'chosen');
      if (language === 'plaintext') {
        // The original text, always kept -- and its breaks restored as <br>
        // so plain text is no more CSS-dependent than the highlighted form.
        pre.innerHTML = asLines(escapeHtml(code));
        pre.className = pre.className.replace(/\bhljs\b/, '');
        return;
      }
      try {
        pre.innerHTML = asLines(hljs.highlight(
          code, { language: language, ignoreIllegals: true }).value);
        if (pre.className.indexOf('hljs') < 0) { pre.className += ' hljs'; }
      } catch (e) { /* unknown grammar: leave the block as it is */ }
    }

    select.addEventListener('change', function () { render(select.value); });
    bar.appendChild(select);
    wrap.insertBefore(bar, pre);
  }

  global.QuipUtils = {
    relativeTime: relativeTime,
    recentTime: recentTime,
    initRecentFilter: initRecentFilter,
    initSiteSearch: initSiteSearch,
    shortDate: shortDate,
    absoluteDate: absoluteDate,
    iconHtml: iconHtml,
    escapeHtml: escapeHtml,
    applyRelativeTimes: applyRelativeTimes,
    entryHtml: entryHtml,
    initTree: initTree,
    codeText: codeText,
    asLines: asLines,
    highlightCode: highlightCode,
    CODE_LANGUAGES: CODE_LANGUAGES,
    LANGUAGE_LABELS: LANGUAGE_LABELS,
    MIN_RELEVANCE: MIN_RELEVANCE
  };

  function start() {
    applyRelativeTimes();
    initTree();
    highlightCode();
    initRecentFilter();
    initSiteSearch();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})(window);
