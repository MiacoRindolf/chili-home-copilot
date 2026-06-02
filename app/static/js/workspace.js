/* CHILI Workspace shell — theme, command palette, rail.
   Vanilla, no deps. Dark-first: defaults to dark on first visit (no stored pref),
   but respects + persists the user's choice via the shared 'chili-theme' key. */
(function () {
  var root = document.documentElement;

  // Dark-first: on first visit, default to dark AND persist it via the shared
  // 'chili-theme' key so windowed apps (same-origin iframes) inherit the dark
  // theme too — keeping the OS and its windows visually consistent.
  if (!localStorage.getItem('chili-theme')) {
    localStorage.setItem('chili-theme', 'dark');
    root.setAttribute('data-theme', 'dark');
  }

  var toggle = document.getElementById('ws-theme-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      localStorage.setItem('chili-theme', next);
    });
  }

  // ── Accent picker: override the --ws-accent tokens across the OS chrome and
  //    persist the choice ('chili-accent'). Applies to the top document (rail,
  //    topbar, windows chrome, palette, cockpit); iframe app interiors keep the
  //    default accent. ──
  var ACCENTS = {
    blue:   { a: '#5b8cff', a2: '#7aa2ff', bg: 'rgba(91,140,255,.13)' },
    violet: { a: '#a78bfa', a2: '#bda6ff', bg: 'rgba(167,139,250,.15)' },
    green:  { a: '#3fdd9a', a2: '#6fe9b6', bg: 'rgba(63,221,154,.14)' },
    chili:  { a: '#ff6b4a', a2: '#ff8a6e', bg: 'rgba(255,107,74,.14)' },
    amber:  { a: '#f2c14e', a2: '#f6d27d', bg: 'rgba(242,193,78,.16)' },
    cyan:   { a: '#22c5d6', a2: '#5bd9e6', bg: 'rgba(34,197,214,.14)' }
  };
  function applyAccent(name) {
    var c = ACCENTS[name]; if (!c) return;
    root.style.setProperty('--ws-accent', c.a);
    root.style.setProperty('--ws-accent-2', c.a2);
    root.style.setProperty('--ws-accent-bg', c.bg);
  }
  var savedAccent = null; try { savedAccent = localStorage.getItem('chili-accent'); } catch (e) {}
  if (savedAccent && ACCENTS[savedAccent]) applyAccent(savedAccent);

  var accWrap = document.getElementById('ws-accent');
  var accBtn = document.getElementById('ws-accent-btn');
  var accMenu = document.getElementById('ws-accent-menu');
  function markActive() {
    if (!accMenu) return;
    var cur = (savedAccent && ACCENTS[savedAccent]) ? savedAccent : 'blue';
    Array.prototype.forEach.call(accMenu.querySelectorAll('.ws-swatch'), function (s) {
      s.classList.toggle('active', s.getAttribute('data-accent') === cur);
    });
  }
  if (accBtn) accBtn.addEventListener('click', function (e) {
    e.stopPropagation();
    var open = accWrap.classList.toggle('open');
    accBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) markActive();
  });
  if (accMenu) accMenu.addEventListener('click', function (e) {
    e.stopPropagation();
    var s = e.target.closest('.ws-swatch'); if (!s) return;
    var name = s.getAttribute('data-accent');
    applyAccent(name); savedAccent = name;
    try { localStorage.setItem('chili-accent', name); } catch (x) {}
    markActive();
  });
  document.addEventListener('click', function (e) {
    if (accWrap && accWrap.classList.contains('open') && !accWrap.contains(e.target)) { accWrap.classList.remove('open'); if (accBtn) accBtn.setAttribute('aria-expanded', 'false'); }
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && accWrap && accWrap.classList.contains('open')) { accWrap.classList.remove('open'); if (accBtn) accBtn.setAttribute('aria-expanded', 'false'); } });

  // ── Command palette: live search across destinations, patterns, tickers ──
  var scrim = document.getElementById('ws-scrim');
  var input = document.getElementById('ws-palette-in');
  var resultsEl = document.getElementById('ws-palette-results');
  var trigger = document.getElementById('ws-cmdk-trigger');
  var debounce, reqSeq = 0, lastPaletteFocus = null;

  function openPalette() {
    if (!scrim) return;
    lastPaletteFocus = document.activeElement;  // restore focus here on close (a11y)
    scrim.classList.add('open');
    setTimeout(function () { if (input) { input.value = ''; input.focus(); runSearch(''); } }, 20);
  }
  function closePalette() {
    if (!scrim || !scrim.classList.contains('open')) return;
    scrim.classList.remove('open');
    if (lastPaletteFocus && lastPaletteFocus.focus) { try { lastPaletteFocus.focus(); } catch (e) {} }
    lastPaletteFocus = null;
  }
  if (trigger) trigger.addEventListener('click', openPalette);
  if (scrim) scrim.addEventListener('click', function (e) { if (e.target === scrim) closePalette(); });

  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

  function render(results) {
    if (!resultsEl) return;
    if (!results.length) { resultsEl.innerHTML = '<div class="ws-empty" style="padding:18px">No matches.</div>'; return; }
    resultsEl.innerHTML = results.map(function (r, i) {
      return '<div class="opt' + (i === 0 ? ' sel' : '') + '" role="option" id="ws-opt-' + i + '" aria-selected="' + (i === 0 ? 'true' : 'false') + '"' +
        (r.app ? ' data-app="' + esc(r.app) + '"' : '') +
        (r.space ? ' data-space="' + esc(r.space) + '"' : '') +
        (r.cmd ? ' data-cmd="' + esc(r.cmd) + '"' : '') +
        ' data-url="' + esc(r.url || '') + '"' + (r.blank ? ' data-blank="1"' : '') + '>' +
        '<span class="pi">' + esc(r.icon || '•') + '</span>' +
        '<span>' + esc(r.label) + '</span>' +
        '<span class="pk">' + esc(r.sub || '') + '</span></div>';
    }).join('');
    syncActiveDescendant();  // point the combobox at the selected option (a11y)
  }
  // Reflect the selected result to assistive tech via aria-activedescendant.
  function syncActiveDescendant() {
    if (!input) return;
    var sel = resultsEl && resultsEl.querySelector('.opt.sel');
    if (sel && sel.id) input.setAttribute('aria-activedescendant', sel.id);
    else input.removeAttribute('aria-activedescendant');
  }

  // Client-side Spaces (localStorage, not the DB) shown first in the palette so
  // ⌘K can switch to a saved arrangement, not just open apps.
  function spaceResults(q) {
    var api = window.ChiliOS && window.ChiliOS.spaces; if (!api) return [];
    var ql = (q || '').toLowerCase();
    return api.list().filter(function (s) { return !ql || s.name.toLowerCase().indexOf(ql) !== -1; })
      .map(function (s) { return { type: 'space', space: s.name, label: s.name, icon: '🗂', sub: 'Space · ' + s.count + ' window' + (s.count === 1 ? '' : 's') }; });
  }

  // Recently-opened palette items (localStorage) — a "jump back in" list shown
  // first on the empty query so re-opening a ticker/pattern/space is one keystroke.
  var RECENTS_KEY = 'chili-os-recents';
  function loadRecents() { try { var a = JSON.parse(localStorage.getItem(RECENTS_KEY) || '[]'); return Array.isArray(a) ? a : []; } catch (e) { return []; } }
  function recentResults() {
    return loadRecents().map(function (r) {
      return { type: 'recent', label: r.label, icon: r.icon || '↩', sub: r.sub || 'Recent', app: r.app, space: r.space, url: r.url, blank: r.blank };
    });
  }
  function rkey(x) { return (x.space || '') + '|' + (x.app || '') + '|' + (x.url || '') + '|' + (x.label || ''); }
  function recordRecent(d) {
    if (!d || !d.label) return;
    var k = rkey(d), list = loadRecents().filter(function (r) { return rkey(r) !== k; });
    list.unshift({ label: d.label, icon: d.icon, sub: d.sub, app: d.app, space: d.space, url: d.url, blank: d.blank });
    if (list.length > 8) list.length = 8;
    try { localStorage.setItem(RECENTS_KEY, JSON.stringify(list)); } catch (e) {}
  }
  function optData(opt) {
    return {
      label: (opt.children[1] && opt.children[1].textContent) || '',
      icon: (opt.querySelector('.pi') && opt.querySelector('.pi').textContent) || '',
      sub: (opt.querySelector('.pk') && opt.querySelector('.pk').textContent) || '',
      app: opt.getAttribute('data-app') || undefined,
      space: opt.getAttribute('data-space') || undefined,
      url: opt.getAttribute('data-url') || undefined,
      blank: opt.getAttribute('data-blank') ? true : undefined
    };
  }
  function dedup(list) {
    var seen = {}, out = [];
    list.forEach(function (r) { var k = rkey(r); if (!seen[k]) { seen[k] = 1; out.push(r); } });
    return out;
  }

  // ⌘K commands — run an action instead of navigating (theme, accent, help).
  function commandResults(q) {
    var ql = (q || '').trim().toLowerCase(); if (!ql) return [];
    var cmds = [
      { cmd: 'theme', label: 'Toggle light / dark theme', icon: '🌓' },
      { cmd: 'help', label: 'Show keyboard shortcuts', icon: '⌨️' },
      { cmd: 'accent:blue', label: 'Accent: Blue', icon: '🔵' },
      { cmd: 'accent:violet', label: 'Accent: Violet', icon: '🟣' },
      { cmd: 'accent:green', label: 'Accent: Green', icon: '🟢' },
      { cmd: 'accent:chili', label: 'Accent: Chili', icon: '🔴' },
      { cmd: 'accent:amber', label: 'Accent: Amber', icon: '🟡' },
      { cmd: 'accent:cyan', label: 'Accent: Cyan', icon: '🟦' }
    ];
    return cmds.filter(function (c) { return c.label.toLowerCase().indexOf(ql) !== -1; })
      .map(function (c) { return { type: 'command', cmd: c.cmd, label: c.label, icon: c.icon, sub: 'Command' }; });
  }
  function runCommand(id) {
    if (id === 'theme') {
      var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('chili-theme', next); } catch (e) {}
    } else if (id.indexOf('accent:') === 0) {
      var name = id.slice(7);
      applyAccent(name); savedAccent = name;
      try { localStorage.setItem('chili-accent', name); } catch (e) {}
      markActive();
    } else if (id === 'help') {
      var hb = document.getElementById('ws-help-btn'); if (hb) hb.click();
    }
  }

  function runSearch(q) {
    var seq = ++reqSeq;
    // recents only on the empty query; spaces always (filtered by q); commands match by label
    var pre = (q ? [] : recentResults()).concat(spaceResults(q));
    fetch('/api/workspace/search?q=' + encodeURIComponent(q), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (seq === reqSeq) render(dedup(pre.concat((d && d.results) || []).concat(commandResults(q)))); })
      .catch(function () { if (seq === reqSeq) render(dedup(pre.concat(commandResults(q)))); });
  }

  function openResult(opt) {
    if (!opt) return;
    var cmd = opt.getAttribute('data-cmd');
    if (cmd) { closePalette(); runCommand(cmd); return; }  // commands run, not recorded
    recordRecent(optData(opt));  // remember what was opened (for the recents list)
    var space = opt.getAttribute('data-space');
    if (space && window.ChiliOS && window.ChiliOS.spaces) { closePalette(); window.ChiliOS.spaces.open(space); return; }
    var app = opt.getAttribute('data-app'), url = opt.getAttribute('data-url'), blank = opt.getAttribute('data-blank');
    closePalette();
    // A result URL carrying query params (e.g. /trading?ticker=NVDA) is a
    // deep-link: open the app window pointed at it. Plain destinations open
    // their default surface.
    var deep = (url && url.indexOf('?') !== -1) ? url : null;
    if (app && window.ChiliOS && window.ChiliOS.open && window.ChiliOS.open(app, deep)) return;
    if (blank) { window.open(url, '_blank', 'noopener'); return; }
    if (url) window.location.href = url;
  }

  function curOpts() { return resultsEl ? Array.prototype.slice.call(resultsEl.querySelectorAll('.opt')) : []; }
  function move(dir) {
    var list = curOpts(); if (!list.length) return;
    var idx = list.findIndex(function (o) { return o.classList.contains('sel'); });
    if (idx >= 0) { list[idx].classList.remove('sel'); list[idx].setAttribute('aria-selected', 'false'); }
    var next = (idx + dir + list.length) % list.length;
    list[next].classList.add('sel'); list[next].setAttribute('aria-selected', 'true');
    list[next].scrollIntoView({ block: 'nearest' });
    syncActiveDescendant();
  }

  if (resultsEl) resultsEl.addEventListener('click', function (e) {
    var o = e.target.closest('.opt'); if (o) openResult(o);
  });
  if (input) {
    input.addEventListener('input', function () {
      clearTimeout(debounce);
      var q = input.value;
      debounce = setTimeout(function () { runSearch(q); }, 130);
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
      else if (e.key === 'Enter') { e.preventDefault(); openResult(curOpts().find(function (o) { return o.classList.contains('sel'); }) || curOpts()[0]); }
      else if (e.key === 'Tab') { e.preventDefault(); move(e.shiftKey ? -1 : 1); }  // trap focus; Tab also moves selection
    });
  }
  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); }
    else if (e.key === 'Escape') closePalette();
  });

  // ── Spaces menu: snapshot the current window arrangement under a name and
  //    switch between saved arrangements. Backed by window.ChiliOS.spaces. ──
  var spacesWrap = document.getElementById('ws-spaces');
  var spacesBtn = document.getElementById('ws-spaces-btn');
  var spacesMenu = document.getElementById('ws-spaces-menu');
  function spacesApi() { return window.ChiliOS && window.ChiliOS.spaces; }
  function renderSpaces() {
    var api = spacesApi(); if (!spacesMenu || !api) return;
    var list = api.list();
    var rows = list.length
      ? list.map(function (s) {
          return '<div class="ws-space-row" role="menuitem" tabindex="0" draggable="true" data-space="' + esc(s.name) + '">' +
            '<span class="ws-space-grip" aria-hidden="true" title="Drag to reorder">⠿</span>' +
            '<span class="ws-space-name">' + esc(s.name) + '</span>' +
            '<span class="ws-space-count" title="' + s.count + ' window(s)">' + s.count + '</span>' +
            '<button class="ws-space-edit" data-edit="' + esc(s.name) + '" title="Rename space" aria-label="Rename ' + esc(s.name) + '">✎</button>' +
            '<button class="ws-space-del" data-del="' + esc(s.name) + '" title="Delete space" aria-label="Delete ' + esc(s.name) + '">×</button>' +
          '</div>';
        }).join('')
      : '<div class="ws-space-empty">No saved spaces yet.</div>';
    spacesMenu.innerHTML = rows +
      '<div class="ws-space-save">' +
        '<input id="ws-space-new" type="text" placeholder="Save current as…" autocomplete="off" maxlength="40">' +
        '<button id="ws-space-save-btn" type="button">Save</button>' +
      '</div>';
  }
  function openSpacesMenu() { if (!spacesWrap) return; renderSpaces(); spacesWrap.classList.add('open'); if (spacesBtn) spacesBtn.setAttribute('aria-expanded', 'true'); }
  function closeSpacesMenu() { if (!spacesWrap) return; spacesWrap.classList.remove('open'); if (spacesBtn) spacesBtn.setAttribute('aria-expanded', 'false'); }
  function toggleSpacesMenu() { (spacesWrap && spacesWrap.classList.contains('open')) ? closeSpacesMenu() : openSpacesMenu(); }
  function saveCurrentSpace() {
    var api = spacesApi(); if (!api) return;
    var inp = document.getElementById('ws-space-new');
    var name = inp && inp.value.trim();
    if (!name) { if (inp) inp.focus(); return; }
    api.save(name); renderSpaces();
    var again = document.getElementById('ws-space-new'); if (again) again.focus();
  }
  // Inline rename: swap the name label for an input; commit on Enter/blur.
  function enterRenameMode(row, name) {
    var nameSpan = row.querySelector('.ws-space-name'); if (!nameSpan) return;
    row.setAttribute('draggable', 'false');
    var inp = document.createElement('input');
    inp.className = 'ws-space-rename'; inp.type = 'text'; inp.value = name; inp.maxLength = 40;
    nameSpan.replaceWith(inp); inp.focus(); inp.select();
    var committed = false;
    var done = function (commit) {
      if (committed) return; committed = true;
      var api = spacesApi();
      if (commit && api) { var nv = inp.value.trim(); if (nv && nv !== name) api.rename(name, nv); }
      renderSpaces();
    };
    inp.addEventListener('click', function (e) { e.stopPropagation(); });
    inp.addEventListener('keydown', function (e) {
      e.stopPropagation();
      if (e.key === 'Enter') { e.preventDefault(); done(true); }
      else if (e.key === 'Escape') { e.preventDefault(); done(false); }
    });
    inp.addEventListener('blur', function () { done(true); });
  }

  if (spacesBtn) spacesBtn.addEventListener('click', function (e) { e.stopPropagation(); toggleSpacesMenu(); });
  if (spacesMenu) spacesMenu.addEventListener('click', function (e) {
    e.stopPropagation();
    var api = spacesApi(); if (!api) return;
    var del = e.target.closest('.ws-space-del');
    if (del) { api.remove(del.getAttribute('data-del')); renderSpaces(); return; }
    var edit = e.target.closest('.ws-space-edit');
    if (edit) { var er = edit.closest('.ws-space-row'); if (er) enterRenameMode(er, edit.getAttribute('data-edit')); return; }
    if (e.target.closest('#ws-space-save-btn')) { saveCurrentSpace(); return; }
    var row = e.target.closest('.ws-space-row');
    if (row) { api.open(row.getAttribute('data-space')); closeSpacesMenu(); }
  });

  // Drag-reorder rows; persist the new order via ChiliOS.spaces.reorder.
  var dragName = null;
  function clearDropTargets() {
    if (spacesMenu) Array.prototype.forEach.call(spacesMenu.querySelectorAll('.ws-space-row.drop-target'),
      function (r) { r.classList.remove('drop-target'); });
  }
  if (spacesMenu) {
    spacesMenu.addEventListener('dragstart', function (e) {
      var row = e.target.closest('.ws-space-row'); if (!row) return;
      dragName = row.getAttribute('data-space'); row.classList.add('dragging');
      try { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', dragName); } catch (x) {}
    });
    spacesMenu.addEventListener('dragend', function (e) {
      var row = e.target.closest('.ws-space-row'); if (row) row.classList.remove('dragging');
      clearDropTargets(); dragName = null;
    });
    spacesMenu.addEventListener('dragover', function (e) {
      var row = e.target.closest('.ws-space-row'); if (!row || !dragName) return;
      e.preventDefault(); clearDropTargets(); row.classList.add('drop-target');
    });
    spacesMenu.addEventListener('drop', function (e) {
      var row = e.target.closest('.ws-space-row'); if (!row || !dragName) return;
      e.preventDefault();
      var target = row.getAttribute('data-space'), api = spacesApi();
      if (target === dragName || !api) { dragName = null; clearDropTargets(); return; }
      var names = Array.prototype.map.call(spacesMenu.querySelectorAll('.ws-space-row'),
        function (r) { return r.getAttribute('data-space'); });
      names.splice(names.indexOf(dragName), 1);
      names.splice(names.indexOf(target), 0, dragName);  // drop before the target row
      api.reorder(names); dragName = null; renderSpaces();
    });
  }
  if (spacesMenu) spacesMenu.addEventListener('keydown', function (e) {
    if (e.target.id === 'ws-space-new' && e.key === 'Enter') { e.preventDefault(); saveCurrentSpace(); }
    else if (e.target.classList.contains('ws-space-row') && e.key === 'Enter') { e.preventDefault(); var api = spacesApi(); if (api) { api.open(e.target.getAttribute('data-space')); closeSpacesMenu(); } }
  });
  document.addEventListener('click', function (e) {
    if (spacesWrap && spacesWrap.classList.contains('open') && !spacesWrap.contains(e.target)) closeSpacesMenu();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeSpacesMenu(); });
})();
