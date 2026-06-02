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

  // ── Command palette: live search across destinations, patterns, tickers ──
  var scrim = document.getElementById('ws-scrim');
  var input = document.getElementById('ws-palette-in');
  var resultsEl = document.getElementById('ws-palette-results');
  var trigger = document.getElementById('ws-cmdk-trigger');
  var debounce, reqSeq = 0;

  function openPalette() {
    if (!scrim) return;
    scrim.classList.add('open');
    setTimeout(function () { if (input) { input.value = ''; input.focus(); runSearch(''); } }, 20);
  }
  function closePalette() { if (scrim) scrim.classList.remove('open'); }
  if (trigger) trigger.addEventListener('click', openPalette);
  if (scrim) scrim.addEventListener('click', function (e) { if (e.target === scrim) closePalette(); });

  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

  function render(results) {
    if (!resultsEl) return;
    if (!results.length) { resultsEl.innerHTML = '<div class="ws-empty" style="padding:18px">No matches.</div>'; return; }
    resultsEl.innerHTML = results.map(function (r, i) {
      return '<div class="opt' + (i === 0 ? ' sel' : '') + '" role="option"' +
        (r.app ? ' data-app="' + esc(r.app) + '"' : '') +
        ' data-url="' + esc(r.url) + '"' + (r.blank ? ' data-blank="1"' : '') + '>' +
        '<span class="pi">' + esc(r.icon || '•') + '</span>' +
        '<span>' + esc(r.label) + '</span>' +
        '<span class="pk">' + esc(r.sub || '') + '</span></div>';
    }).join('');
  }

  function runSearch(q) {
    var seq = ++reqSeq;
    fetch('/api/workspace/search?q=' + encodeURIComponent(q), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (seq === reqSeq) render((d && d.results) || []); })
      .catch(function () { if (seq === reqSeq) render([]); });
  }

  function openResult(opt) {
    if (!opt) return;
    var app = opt.getAttribute('data-app'), url = opt.getAttribute('data-url'), blank = opt.getAttribute('data-blank');
    closePalette();
    if (app && window.ChiliOS && window.ChiliOS.open && window.ChiliOS.open(app)) return;
    if (blank) { window.open(url, '_blank', 'noopener'); return; }
    if (url) window.location.href = url;
  }

  function curOpts() { return resultsEl ? Array.prototype.slice.call(resultsEl.querySelectorAll('.opt')) : []; }
  function move(dir) {
    var list = curOpts(); if (!list.length) return;
    var idx = list.findIndex(function (o) { return o.classList.contains('sel'); });
    if (idx >= 0) list[idx].classList.remove('sel');
    var next = (idx + dir + list.length) % list.length;
    list[next].classList.add('sel'); list[next].scrollIntoView({ block: 'nearest' });
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
          return '<div class="ws-space-row" role="menuitem" tabindex="0" data-space="' + esc(s.name) + '">' +
            '<span class="ws-space-name">' + esc(s.name) + '</span>' +
            '<span class="ws-space-count" title="' + s.count + ' window(s)">' + s.count + '</span>' +
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
  if (spacesBtn) spacesBtn.addEventListener('click', function (e) { e.stopPropagation(); toggleSpacesMenu(); });
  if (spacesMenu) spacesMenu.addEventListener('click', function (e) {
    e.stopPropagation();
    var api = spacesApi(); if (!api) return;
    var del = e.target.closest('.ws-space-del');
    if (del) { api.remove(del.getAttribute('data-del')); renderSpaces(); return; }
    if (e.target.closest('#ws-space-save-btn')) { saveCurrentSpace(); return; }
    var row = e.target.closest('.ws-space-row');
    if (row) { api.open(row.getAttribute('data-space')); closeSpacesMenu(); }
  });
  if (spacesMenu) spacesMenu.addEventListener('keydown', function (e) {
    if (e.target.id === 'ws-space-new' && e.key === 'Enter') { e.preventDefault(); saveCurrentSpace(); }
    else if (e.target.classList.contains('ws-space-row') && e.key === 'Enter') { e.preventDefault(); var api = spacesApi(); if (api) { api.open(e.target.getAttribute('data-space')); closeSpacesMenu(); } }
  });
  document.addEventListener('click', function (e) {
    if (spacesWrap && spacesWrap.classList.contains('open') && !spacesWrap.contains(e.target)) closeSpacesMenu();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeSpacesMenu(); });
})();
