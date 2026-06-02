/* CHILI OS — window manager. Opens CHILI surfaces as draggable/tiling/minimizable
   windows (iframes of the real routes) over the dashboard desktop. Vanilla, no deps.
   Deep-links the open app via the URL hash (#app=chat). Degrades to plain links
   when JS is off (rail items keep their href). */
(function () {
  var desktop = document.getElementById('os-desktop');
  var home = document.getElementById('os-home');
  var ghost = document.getElementById('os-ghost');
  if (!desktop) return;
  var Z = 20, wins = {}, order = [];

  // Taskbar for minimized windows (bottom of the desktop).
  var taskbar = document.createElement('div');
  taskbar.className = 'os-taskbar';
  desktop.appendChild(taskbar);
  function addChip(app, title, icon) {
    if (taskbar.querySelector('[data-chip="' + app + '"]')) return;
    var c = document.createElement('button');
    c.className = 'os-chip'; c.dataset.chip = app; c.title = 'Restore ' + title;
    c.innerHTML = '<span class="ci">' + (icon || '🗔') + '</span>' + title;
    c.addEventListener('click', function () {
      var w = wins[app]; if (w) { w.style.display = 'flex'; focusWin(app); }
      removeChip(app); syncHome();
    });
    taskbar.appendChild(c); taskbar.classList.add('show');
  }
  function removeChip(app) {
    var c = taskbar.querySelector('[data-chip="' + app + '"]');
    if (c) c.remove();
    if (!taskbar.children.length) taskbar.classList.remove('show');
  }

  function appsOpen() { return Object.keys(wins).filter(function (a) { return wins[a]; }).length; }
  function syncHome() { home && home.classList.toggle('dimmed', appsOpen() > 0); }
  function setHash() {
    var top = order[order.length - 1];
    try { history.replaceState(null, '', top ? '#app=' + top : location.pathname + location.search); } catch (e) {}
  }
  function focusWin(app) {
    Object.keys(wins).forEach(function (a) { if (wins[a]) wins[a].classList.remove('active'); });
    var el = wins[app]; if (!el) return;
    el.classList.add('active'); el.style.zIndex = ++Z;
    order = order.filter(function (a) { return a !== app; }); order.push(app);
    setHash();
  }
  function dock(app) { return document.querySelector('.ws-rb[data-app="' + app + '"]'); }

  function openApp(cfg, geom) {
    var app = cfg.app;
    if (wins[app]) { wins[app].style.display = 'flex'; focusWin(app); removeChip(app); syncHome(); saveLayout(); return; }
    var n = order.length;
    var el = document.createElement('div');
    el.className = 'os-win active';
    el.style.left = (40 + n * 34) + 'px'; el.style.top = (24 + n * 28) + 'px';
    el.style.width = 'min(640px,72vw)'; el.style.height = 'min(520px,72vh)';
    if (geom) { if (geom.left) el.style.left = geom.left; if (geom.top) el.style.top = geom.top; if (geom.width) el.style.width = geom.width; if (geom.height) el.style.height = geom.height; }
    var src = cfg.src + (cfg.src.indexOf('?') === -1 ? '?' : '&') + 'embed=1';
    el.innerHTML =
      '<div class="os-bar"><span class="wi">' + (cfg.icon || '🗔') + '</span><span class="wt">' + cfg.title + '</span>' +
      '<a class="pop" href="' + cfg.src + '" target="_blank" rel="noopener" title="Open in new tab"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M14 4h6v6M10 14L20 4M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/></svg></a>' +
      '<div class="os-ctrls">' +
      '<button class="min" title="Minimize"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M5 12h14"/></svg></button>' +
      '<button class="max" title="Tile / restore"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/></svg></button>' +
      '<button class="close" title="Close"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>' +
      '</div></div>' +
      '<div class="os-body"><div class="os-loading">Loading ' + cfg.title + '…</div>' +
      '<iframe src="' + src + '" title="' + cfg.title + '" loading="lazy"></iframe></div>' +
      '<div class="os-rs"></div>';
    desktop.appendChild(el); wins[app] = el; order.push(app);
    var d = dock(app); if (d) d.classList.add('os-open');
    var ifr = el.querySelector('iframe');
    ifr.addEventListener('load', function () { var l = el.querySelector('.os-loading'); if (l) l.remove(); });
    function minimize() { el.style.display = 'none'; order = order.filter(function (a) { return a !== app; }); addChip(app, cfg.title, cfg.icon); syncHome(); setHash(); saveLayout(); }
    el.querySelector('.close').addEventListener('click', function () { closeApp(app); });
    el.querySelector('.min').addEventListener('click', minimize);
    var restore = null;
    el.querySelector('.max').addEventListener('click', function () {
      if (restore) { el.style.left = restore.l; el.style.top = restore.t; el.style.width = restore.w; el.style.height = restore.h; restore = null; }
      else { restore = { l: el.style.left, t: el.style.top, w: el.style.width, h: el.style.height }; snap(el, 'max'); }
      saveLayout();
    });
    el.addEventListener('mousedown', function () { focusWin(app); });
    dragify(el, app); resizify(el);
    focusWin(app); syncHome();
    if (geom && geom.min) minimize();
    saveLayout();
  }
  function closeApp(app) {
    var el = wins[app]; if (!el) return;
    el.classList.add('closing');
    setTimeout(function () { el.remove(); }, 120);
    delete wins[app]; order = order.filter(function (a) { return a !== app; });
    var d = dock(app); if (d) d.classList.remove('os-open');
    removeChip(app); syncHome(); setHash(); saveLayout();
  }

  // ── Session restore: remember which windows are open, where, and how big,
  //    so reopening the workspace brings the desktop back as you left it. ──
  var LAYOUT_KEY = 'chili-os-layout', restoring = false;
  function saveLayout() {
    if (restoring) return;  // don't thrash storage while replaying a layout
    try {
      var apps = Object.keys(wins).map(function (app) {
        var el = wins[app]; if (!el) return null;
        return { app: app, left: el.style.left, top: el.style.top, width: el.style.width, height: el.style.height, min: el.style.display === 'none' };
      }).filter(Boolean);
      localStorage.setItem(LAYOUT_KEY, JSON.stringify({ apps: apps, order: order.slice() }));
    } catch (e) {}
  }
  function restoreLayout() {
    var raw; try { raw = localStorage.getItem(LAYOUT_KEY); } catch (e) { return false; }
    if (!raw) return false;
    var data; try { data = JSON.parse(raw); } catch (e) { return false; }
    if (!data || !data.apps || !data.apps.length) return false;
    restoring = true;
    // Open in saved focus order so the last-focused window ends up on top.
    var byApp = {}; data.apps.forEach(function (s) { byApp[s.app] = s; });
    var seq = (data.order && data.order.length ? data.order : data.apps.map(function (s) { return s.app; }));
    data.apps.forEach(function (s) { if (seq.indexOf(s.app) === -1) seq.push(s.app); });
    seq.forEach(function (app) {
      var s = byApp[app]; if (!s) return;
      var b = document.querySelector('.ws-rb[data-app="' + app + '"][data-src]');
      if (b) openApp(cfgFromEl(b), { left: s.left, top: s.top, width: s.width, height: s.height, min: s.min });
    });
    restoring = false; saveLayout();
    return Object.keys(wins).length > 0;
  }
  function snap(el, zone) {
    el.classList.add('snapping');
    var W = desktop.clientWidth, H = desktop.clientHeight;
    var r = { left: '0px', top: '0px', width: W + 'px', height: H + 'px' };
    if (zone === 'left') r = { left: '0px', top: '0px', width: (W / 2) + 'px', height: H + 'px' };
    if (zone === 'right') r = { left: (W / 2) + 'px', top: '0px', width: (W / 2) + 'px', height: H + 'px' };
    el.style.left = r.left; el.style.top = r.top; el.style.width = r.width; el.style.height = r.height;
    setTimeout(function () { el.classList.remove('snapping'); }, 160);
  }
  function dragify(el, app) {
    var bar = el.querySelector('.os-bar'), sx, sy, ol, ot, drag = false, zone = null;
    bar.addEventListener('mousedown', function (e) {
      if (e.target.closest('.os-ctrls') || e.target.closest('a.pop')) return;
      drag = true; sx = e.clientX; sy = e.clientY; ol = el.offsetLeft; ot = el.offsetTop; e.preventDefault();
    });
    window.addEventListener('mousemove', function (e) {
      if (!drag) return;
      el.style.left = (ol + e.clientX - sx) + 'px'; el.style.top = (ot + e.clientY - sy) + 'px';
      var rect = desktop.getBoundingClientRect(), W = desktop.clientWidth, H = desktop.clientHeight;
      var gx = e.clientX - rect.left, gy = e.clientY - rect.top; zone = null;
      if (gy < 16) { zone = 'max'; showGhost(0, 0, W, H); }
      else if (gx < 22) { zone = 'left'; showGhost(0, 0, W / 2, H); }
      else if (gx > W - 22) { zone = 'right'; showGhost(W / 2, 0, W / 2, H); }
      else hideGhost();
    });
    window.addEventListener('mouseup', function () { if (drag) { if (zone) snap(el, zone); saveLayout(); } drag = false; hideGhost(); });
  }
  function resizify(el) {
    var h = el.querySelector('.os-rs'), sx, sy, ow, oh, rz = false;
    h.addEventListener('mousedown', function (e) { rz = true; sx = e.clientX; sy = e.clientY; ow = el.offsetWidth; oh = el.offsetHeight; e.preventDefault(); e.stopPropagation(); });
    window.addEventListener('mousemove', function (e) { if (!rz) return; el.style.width = Math.max(340, ow + e.clientX - sx) + 'px'; el.style.height = Math.max(220, oh + e.clientY - sy) + 'px'; });
    window.addEventListener('mouseup', function () { if (rz) { rz = false; saveLayout(); } });
  }
  function showGhost(x, y, w, h) { if (!ghost) return; ghost.style.display = 'block'; ghost.style.left = x + 'px'; ghost.style.top = y + 'px'; ghost.style.width = w + 'px'; ghost.style.height = h + 'px'; }
  function hideGhost() { if (ghost) ghost.style.display = 'none'; }

  // Wire the dock: rail items with data-src open as windows (intercept the link).
  function cfgFromEl(b) { return { app: b.dataset.app, src: b.dataset.src, title: b.dataset.title || b.dataset.app, icon: b.dataset.icon || '🗔' }; }
  document.querySelectorAll('.ws-rb[data-app][data-src]').forEach(function (b) {
    b.addEventListener('click', function (e) { e.preventDefault(); openApp(cfgFromEl(b)); });
  });
  // Dashboard dock button: close/minimize all to show the desktop home.
  var dashBtn = document.querySelector('.ws-rb[data-app="dashboard"]');
  if (dashBtn) dashBtn.addEventListener('click', function (e) {
    e.preventDefault();
    Object.keys(wins).forEach(function (a) { if (wins[a]) wins[a].style.display = 'none'; });
    order = []; syncHome(); setHash(); saveLayout();
  });
  // (Command-palette result clicks are handled in workspace.js, which renders
  //  results dynamically and calls window.ChiliOS.open for app results.)

  // Any [data-os-open="app"] element (e.g. dashboard buttons/quick-actions)
  // opens that app as a window instead of navigating. href stays as a fallback.
  document.querySelectorAll('[data-os-open]').forEach(function (el) {
    el.addEventListener('click', function (e) {
      var b = document.querySelector('.ws-rb[data-app="' + el.dataset.osOpen + '"][data-src]');
      if (b) { e.preventDefault(); openApp(cfgFromEl(b)); }
    });
  });

  // On load: restore the saved window layout, then honor a deep-link hash
  // (#app=chat) by opening/focusing that app on top.
  restoreLayout();
  var m = (location.hash || '').match(/app=([a-z0-9_-]+)/i);
  if (m) { var b = document.querySelector('.ws-rb[data-app="' + m[1] + '"][data-src]'); if (b) openApp(cfgFromEl(b)); }

  // expose for other UI (e.g. quick actions, palette) to open windows.
  // Returns true if the app was opened as a window, false otherwise (caller can
  // then fall back to navigation — e.g. Dashboard, which is the desktop home).
  window.ChiliOS = { open: function (app) { var b = document.querySelector('.ws-rb[data-app="' + app + '"][data-src]'); if (b) { openApp(cfgFromEl(b)); return true; } return false; } };
})();
