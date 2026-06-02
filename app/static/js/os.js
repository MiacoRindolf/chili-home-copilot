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
      var w = wins[app]; if (w) { w.style.display = 'flex'; animIn(w); focusWin(app); }
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

  // Respect the user's reduced-motion preference for window animations.
  var _reduceMotion = false;
  try { _reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (e) {}
  // Re-trigger the entrance animation (open / restore-from-taskbar).
  function animIn(el) { if (_reduceMotion || !el) return; el.classList.remove('os-in'); void el.offsetWidth; el.classList.add('os-in'); }

  // Append the embed flag that strips an app's chrome inside an OS window.
  function withEmbed(src) { return src + (src.indexOf('?') === -1 ? '?' : '&') + 'embed=1'; }

  function openApp(cfg, geom) {
    var app = cfg.app;
    if (wins[app]) {
      var ex = wins[app];
      // A deep-link (e.g. ⌘K "NVDA") re-points an already-open window's iframe.
      if (cfg.deep && cfg.src) {
        var ifr0 = ex.querySelector('iframe'), want = withEmbed(cfg.src);
        if (ifr0 && ifr0.getAttribute('src') !== want) ifr0.setAttribute('src', want);
      }
      var wasHidden = ex.style.display === 'none';
      ex.style.display = 'flex'; if (wasHidden) animIn(ex);
      focusWin(app); removeChip(app); syncHome(); saveLayout(); return;
    }
    var n = order.length;
    var el = document.createElement('div');
    el.className = 'os-win active';
    el.dataset.title = cfg.title; el.dataset.icon = cfg.icon || '🗔';
    el.style.left = (40 + n * 34) + 'px'; el.style.top = (24 + n * 28) + 'px';
    el.style.width = 'min(640px,72vw)'; el.style.height = 'min(520px,72vh)';
    if (geom) { if (geom.left) el.style.left = geom.left; if (geom.top) el.style.top = geom.top; if (geom.width) el.style.width = geom.width; if (geom.height) el.style.height = geom.height; }
    var src = withEmbed(cfg.src);
    el.innerHTML =
      '<div class="os-bar"><span class="wi">' + (cfg.icon || '🗔') + '</span><span class="wt">' + cfg.title + '</span>' +
      '<a class="pop" href="' + cfg.src + '" target="_blank" rel="noopener" title="Open in new tab"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M14 4h6v6M10 14L20 4M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/></svg></a>' +
      '<div class="os-ctrls">' +
      '<button class="min" title="Minimize (Ctrl/⌘+Alt+↓)"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M5 12h14"/></svg></button>' +
      '<button class="max" title="Tile / restore (Ctrl/⌘+Alt+↑/←/→)"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/></svg></button>' +
      '<button class="close" title="Close (Ctrl/⌘+Alt+W)"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>' +
      '</div></div>' +
      '<div class="os-body"><div class="os-loading">Loading ' + cfg.title + '…</div>' +
      '<iframe src="' + src + '" title="' + cfg.title + '" loading="lazy"></iframe></div>' +
      '<div class="os-rs"></div>';
    desktop.appendChild(el); wins[app] = el; order.push(app); animIn(el);
    var d = dock(app); if (d) d.classList.add('os-open');
    var ifr = el.querySelector('iframe');
    ifr.addEventListener('load', function () { var l = el.querySelector('.os-loading'); if (l) l.remove(); });
    el.querySelector('.close').addEventListener('click', function () { closeApp(app); });
    el.querySelector('.min').addEventListener('click', function () { minimizeApp(app); });
    var restore = null;
    el.querySelector('.max').addEventListener('click', function () {
      if (restore) { el.style.left = restore.l; el.style.top = restore.t; el.style.width = restore.w; el.style.height = restore.h; restore = null; }
      else { restore = { l: el.style.left, t: el.style.top, w: el.style.width, h: el.style.height }; snap(el, 'max'); }
      saveLayout();
    });
    el.addEventListener('mousedown', function () { focusWin(app); });
    dragify(el, app); resizify(el);
    focusWin(app); syncHome();
    if (geom && geom.min) minimizeApp(app);
    saveLayout();
  }
  // Minimize a window to a taskbar chip (top-level so keyboard shortcuts can drive it).
  function minimizeApp(app) {
    var el = wins[app]; if (!el) return;
    order = order.filter(function (a) { return a !== app; });
    addChip(app, el.dataset.title, el.dataset.icon);
    var finish = function () { el.classList.remove('os-out'); el.style.display = 'none'; syncHome(); setHash(); saveLayout(); };
    // Skip the shrink while replaying a saved layout (restoring) or under
    // reduced-motion — hide immediately so there's no flash.
    if (_reduceMotion || restoring) { finish(); return; }
    el.classList.add('os-out');
    setTimeout(finish, 150);
  }
  function closeApp(app) {
    var el = wins[app]; if (!el) return;
    el.classList.add('closing');
    setTimeout(function () { el.remove(); }, 120);
    delete wins[app]; order = order.filter(function (a) { return a !== app; });
    var d = dock(app); if (d) d.classList.remove('os-open');
    removeChip(app); syncHome(); setHash(); saveLayout();
  }

  // ── Session restore + named Spaces: a layout is the set of open windows with
  //    their geometry/min-state/focus order. The current layout auto-restores on
  //    reload; named Spaces let you snapshot and switch between arrangements. ──
  var LAYOUT_KEY = 'chili-os-layout', SPACES_KEY = 'chili-os-spaces', restoring = false;

  // Serialize the current desktop into a plain layout object.
  function captureLayout() {
    var apps = Object.keys(wins).map(function (app) {
      var el = wins[app]; if (!el) return null;
      return { app: app, left: el.style.left, top: el.style.top, width: el.style.width, height: el.style.height, min: el.style.display === 'none' };
    }).filter(Boolean);
    return { apps: apps, order: order.slice() };
  }
  function saveLayout() {
    if (restoring) return;  // don't thrash storage while replaying a layout
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(captureLayout())); } catch (e) {}
  }
  // Replay a layout object onto the desktop (in saved focus order, last on top).
  function applyLayout(data) {
    if (!data || !data.apps || !data.apps.length) return false;
    restoring = true;
    var byApp = {}; data.apps.forEach(function (s) { byApp[s.app] = s; });
    var seq = (data.order && data.order.length ? data.order.slice() : data.apps.map(function (s) { return s.app; }));
    data.apps.forEach(function (s) { if (seq.indexOf(s.app) === -1) seq.push(s.app); });
    seq.forEach(function (app) {
      var s = byApp[app]; if (!s) return;
      var b = document.querySelector('.ws-rb[data-app="' + app + '"][data-src]');
      if (b) openApp(cfgFromEl(b), { left: s.left, top: s.top, width: s.width, height: s.height, min: s.min });
    });
    restoring = false; saveLayout();
    return Object.keys(wins).length > 0;
  }
  function restoreLayout() {
    var raw; try { raw = localStorage.getItem(LAYOUT_KEY); } catch (e) { return false; }
    if (!raw) return false;
    var data; try { data = JSON.parse(raw); } catch (e) { return false; }
    return applyLayout(data);
  }
  // Immediately tear down every open window (used when switching Spaces).
  function closeAllNow() {
    restoring = true;
    Object.keys(wins).forEach(function (app) {
      var el = wins[app]; if (el) el.remove();
      var d = dock(app); if (d) d.classList.remove('os-open');
      removeChip(app);
    });
    wins = {}; order = [];
    restoring = false; syncHome(); setHash();
  }

  // Named Spaces store (array preserves the user's ordering).
  function loadSpaces() {
    try { var d = JSON.parse(localStorage.getItem(SPACES_KEY) || '{}'); return Array.isArray(d.spaces) ? d.spaces : []; }
    catch (e) { return []; }
  }
  function persistSpaces(arr) { try { localStorage.setItem(SPACES_KEY, JSON.stringify({ spaces: arr })); } catch (e) {} }
  function saveSpace(name) {
    name = (name || '').trim(); if (!name) return false;
    var arr = loadSpaces(), snap = captureLayout(), entry = { name: name, apps: snap.apps, order: snap.order };
    var i = arr.map(function (s) { return s.name; }).indexOf(name);
    if (i >= 0) arr[i] = entry; else arr.push(entry);
    persistSpaces(arr); return true;
  }
  function openSpace(name) {
    var sp = loadSpaces().filter(function (s) { return s.name === name; })[0];
    if (!sp) return false;
    closeAllNow();
    applyLayout({ apps: sp.apps, order: sp.order });
    return true;
  }
  function removeSpace(name) { persistSpaces(loadSpaces().filter(function (s) { return s.name !== name; })); }
  function renameSpace(oldName, newName) {
    newName = (newName || '').trim(); if (!newName || newName === oldName) return false;
    var arr = loadSpaces(), i = arr.map(function (s) { return s.name; }).indexOf(oldName);
    if (i < 0) return false;
    if (arr.some(function (s, j) { return j !== i && s.name === newName; })) return false;  // no duplicate names
    arr[i].name = newName; persistSpaces(arr); return true;
  }
  function reorderSpaces(names) {
    var arr = loadSpaces(), byName = {}; arr.forEach(function (s) { byName[s.name] = s; });
    var next = names.map(function (n) { return byName[n]; }).filter(Boolean);
    arr.forEach(function (s) { if (names.indexOf(s.name) === -1) next.push(s); });  // keep any not listed
    persistSpaces(next); return true;
  }
  function snap(el, zone) {
    el.classList.add('snapping');
    var W = desktop.clientWidth, H = desktop.clientHeight, hw = (W / 2) + 'px', hh = (H / 2) + 'px';
    var r = { left: '0px', top: '0px', width: W + 'px', height: H + 'px' };  // max
    if (zone === 'left') r = { left: '0px', top: '0px', width: hw, height: H + 'px' };
    else if (zone === 'right') r = { left: hw, top: '0px', width: hw, height: H + 'px' };
    else if (zone === 'tl') r = { left: '0px', top: '0px', width: hw, height: hh };
    else if (zone === 'tr') r = { left: hw, top: '0px', width: hw, height: hh };
    else if (zone === 'bl') r = { left: '0px', top: hh, width: hw, height: hh };
    else if (zone === 'br') r = { left: hw, top: hh, width: hw, height: hh };
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
      // Corners (quarter tiles) take priority over the edge halves; then the
      // top strip maximizes. CX/CY are the corner hot-zone sizes.
      var CX = 130, CY = 110;
      if (gx < CX && gy < CY) { zone = 'tl'; showGhost(0, 0, W / 2, H / 2); }
      else if (gx > W - CX && gy < CY) { zone = 'tr'; showGhost(W / 2, 0, W / 2, H / 2); }
      else if (gx < CX && gy > H - CY) { zone = 'bl'; showGhost(0, H / 2, W / 2, H / 2); }
      else if (gx > W - CX && gy > H - CY) { zone = 'br'; showGhost(W / 2, H / 2, W / 2, H / 2); }
      else if (gy < 16) { zone = 'max'; showGhost(0, 0, W, H); }
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

  // ── Keyboard window management — drive the focused (top) window without the
  //    mouse. Combos are chosen to avoid clashing with browser/OS bindings:
  //      Ctrl/⌘ + `            cycle focus to the next window
  //      Ctrl/⌘ + Alt + ←/→/↑  tile left / right / maximize
  //      Ctrl/⌘ + Alt + 1/2/3/4 tile top-left / top-right / bottom-left / bottom-right quarter
  //      Ctrl/⌘ + Alt + ↓      minimize
  //      Ctrl/⌘ + Alt + W      close
  //    (Only fire while the OS chrome has focus — keydown inside an app iframe
  //     stays with that iframe, which is the expected browser behavior.) ──
  document.addEventListener('keydown', function (e) {
    if (!(e.ctrlKey || e.metaKey)) return;
    if (e.key === '`') { if (order.length > 1) { e.preventDefault(); focusWin(order[0]); } return; }
    if (!e.altKey) return;
    var top = order[order.length - 1], el = top && wins[top];
    if (!el) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); snap(el, 'left'); saveLayout(); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); snap(el, 'right'); saveLayout(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); snap(el, 'max'); saveLayout(); }
    else if (e.key === '1') { e.preventDefault(); snap(el, 'tl'); saveLayout(); }
    else if (e.key === '2') { e.preventDefault(); snap(el, 'tr'); saveLayout(); }
    else if (e.key === '3') { e.preventDefault(); snap(el, 'bl'); saveLayout(); }
    else if (e.key === '4') { e.preventDefault(); snap(el, 'br'); saveLayout(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); minimizeApp(top); }
    else if (e.key.toLowerCase() === 'w') { e.preventDefault(); closeApp(top); }
  });

  // expose for other UI (quick actions, palette, the Spaces menu) to drive the OS.
  // open() returns true if the app opened as a window, false otherwise (caller can
  // then fall back to navigation — e.g. Dashboard, which is the desktop home).
  window.ChiliOS = {
    // open(app) opens/focuses the app's default window; open(app, srcOverride)
    // opens it pointed at a deep-link URL (re-navigating it if already open).
    open: function (app, srcOverride) {
      var b = document.querySelector('.ws-rb[data-app="' + app + '"][data-src]');
      if (!b) return false;
      var cfg = cfgFromEl(b);
      if (srcOverride) { cfg.src = srcOverride; cfg.deep = true; }
      openApp(cfg); return true;
    },
    // Named Spaces — snapshot/restore window arrangements by name.
    spaces: {
      list: function () { return loadSpaces().map(function (s) { return { name: s.name, count: (s.apps || []).length }; }); },
      save: saveSpace,
      open: openSpace,
      remove: removeSpace,
      rename: renameSpace,
      reorder: reorderSpaces
    }
  };
})();
