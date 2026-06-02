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

  function openApp(cfg) {
    var app = cfg.app;
    if (wins[app]) { wins[app].style.display = 'flex'; focusWin(app); return; }
    var n = order.length;
    var el = document.createElement('div');
    el.className = 'os-win active';
    el.style.left = (40 + n * 34) + 'px'; el.style.top = (24 + n * 28) + 'px';
    el.style.width = 'min(640px,72vw)'; el.style.height = 'min(520px,72vh)';
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
    el.querySelector('.close').addEventListener('click', function () { closeApp(app); });
    el.querySelector('.min').addEventListener('click', function () { el.style.display = 'none'; order = order.filter(function (a) { return a !== app; }); syncHome(); setHash(); });
    var restore = null;
    el.querySelector('.max').addEventListener('click', function () {
      if (restore) { el.style.left = restore.l; el.style.top = restore.t; el.style.width = restore.w; el.style.height = restore.h; restore = null; }
      else { restore = { l: el.style.left, t: el.style.top, w: el.style.width, h: el.style.height }; snap(el, 'max'); }
    });
    el.addEventListener('mousedown', function () { focusWin(app); });
    dragify(el, app); resizify(el);
    focusWin(app); syncHome();
  }
  function closeApp(app) {
    var el = wins[app]; if (!el) return;
    el.classList.add('closing');
    setTimeout(function () { el.remove(); }, 120);
    delete wins[app]; order = order.filter(function (a) { return a !== app; });
    var d = dock(app); if (d) d.classList.remove('os-open');
    syncHome(); setHash();
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
    window.addEventListener('mouseup', function () { if (drag && zone) snap(el, zone); drag = false; hideGhost(); });
  }
  function resizify(el) {
    var h = el.querySelector('.os-rs'), sx, sy, ow, oh, rz = false;
    h.addEventListener('mousedown', function (e) { rz = true; sx = e.clientX; sy = e.clientY; ow = el.offsetWidth; oh = el.offsetHeight; e.preventDefault(); e.stopPropagation(); });
    window.addEventListener('mousemove', function (e) { if (!rz) return; el.style.width = Math.max(340, ow + e.clientX - sx) + 'px'; el.style.height = Math.max(220, oh + e.clientY - sy) + 'px'; });
    window.addEventListener('mouseup', function () { rz = false; });
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
    order = []; syncHome(); setHash();
  });
  // Command-palette items with data-app open windows too.
  document.querySelectorAll('#ws-scrim .opt[data-app][data-src]').forEach(function (o) {
    o.addEventListener('click', function (e) {
      e.preventDefault(); openApp(cfgFromEl(o));
      var scrim = document.getElementById('ws-scrim'); if (scrim) scrim.classList.remove('open');
    });
  });

  // Restore deep-linked app from the hash on load (#app=chat).
  var m = (location.hash || '').match(/app=([a-z0-9_-]+)/i);
  if (m) { var b = document.querySelector('.ws-rb[data-app="' + m[1] + '"][data-src]'); if (b) openApp(cfgFromEl(b)); }

  // expose for other UI (e.g. quick actions) to open windows
  window.ChiliOS = { open: function (app) { var b = document.querySelector('.ws-rb[data-app="' + app + '"][data-src]'); if (b) openApp(cfgFromEl(b)); } };
})();
