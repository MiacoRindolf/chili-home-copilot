/* CHILI OS notifications — surfaces meaningful changes in the trading brain as
   toasts (by diffing successive polls of /api/workspace/desktop) AND keeps a
   history reachable from the topbar bell, with an unread badge. Frontend-only:
   reuses the existing cockpit endpoint (no new backend). The first poll seeds a
   baseline (no toasts); later polls toast on safety flips, market open/close,
   and newly-closed trades. Pauses while the tab is hidden. */
(function () {
  var container = document.getElementById('ws-toasts');
  if (!container) return;

  // Notification center (topbar bell) — optional; toasts work without it.
  var wrap = document.getElementById('ws-notif');
  var bell = document.getElementById('ws-notif-btn');
  var panel = document.getElementById('ws-notif-panel');
  var badge = document.getElementById('ws-notif-badge');

  var prev = null, seq = 0, reduce = false;
  var history = [], unread = 0, HISTORY_CAP = 40;
  try { reduce = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (e) {}

  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  function timeStr() { try { return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); } catch (e) { return ''; } }

  function updateBadge() {
    if (!badge) return;
    if (unread > 0) { badge.textContent = unread > 9 ? '9+' : String(unread); badge.hidden = false; }
    else { badge.hidden = true; }
  }

  // Record a notification in the history feed (newest first) and bump unread.
  function record(o) {
    history.unshift({ kind: o.kind || 'info', icon: o.icon || '•', title: o.title, body: o.body || '', t: timeStr() });
    if (history.length > HISTORY_CAP) history.length = HISTORY_CAP;
    if (!(wrap && wrap.classList.contains('open'))) { unread++; updateBadge(); }
    else renderPanel();
  }

  // ── Desktop (browser) alerts — opt-in; fired only for critical events so a
  //    trader is alerted even with the tab backgrounded. ──
  var ALERTS_KEY = 'chili-desktop-alerts', alertsOn = false;
  try { alertsOn = localStorage.getItem(ALERTS_KEY) === '1'; } catch (e) {}
  function persistAlerts() { try { localStorage.setItem(ALERTS_KEY, alertsOn ? '1' : '0'); } catch (e) {} }

  // ── Per-category preferences — mute the noisy categories; safety (kill switch
  //    / breaker) is always on and can't be muted. ──
  var PREFS_KEY = 'chili-notif-prefs', prefs = { market: true, trades: true };
  try { var sp = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}'); if (sp && typeof sp === 'object') { if (sp.market === false) prefs.market = false; if (sp.trades === false) prefs.trades = false; } } catch (e) {}
  function persistPrefs() { try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) {} }
  function desktopReady() { return alertsOn && typeof Notification !== 'undefined' && Notification.permission === 'granted'; }
  function fireDesktop(o) {
    if (!desktopReady()) return;
    try { new Notification('CHILI · ' + o.title, { body: o.body || '', tag: 'chili-' + o.title }); } catch (e) {}
  }
  function setAlerts(on) {
    if (!on || typeof Notification === 'undefined') { alertsOn = false; persistAlerts(); renderPanel(); return; }
    if (Notification.permission === 'granted') { alertsOn = true; persistAlerts(); renderPanel(); return; }
    if (Notification.permission === 'denied') {
      alertsOn = false; persistAlerts(); renderPanel();
      toast({ kind: 'info', icon: '🔕', title: 'Desktop alerts blocked', body: 'Allow notifications in your browser settings' });
      return;
    }
    Notification.requestPermission().then(function (p) {
      alertsOn = (p === 'granted'); persistAlerts(); renderPanel();
      if (!alertsOn) toast({ kind: 'info', icon: '🔕', title: 'Desktop alerts not enabled', body: 'Browser permission was not granted' });
    });
  }

  function toast(o) {
    record(o);
    if (o.kind === 'bad') fireDesktop(o);  // push critical events to the desktop
    var t = document.createElement('div');
    t.className = 'ws-toast ' + (o.kind || 'info');
    t.setAttribute('role', 'status');
    t.innerHTML =
      '<span class="wt-ic">' + esc(o.icon || '•') + '</span>' +
      '<div class="wt-body"><div class="wt-title">' + esc(o.title) + '</div>' +
      (o.body ? '<div class="wt-sub">' + esc(o.body) + '</div>' : '') + '</div>' +
      '<button class="wt-x" aria-label="Dismiss">×</button>';
    container.appendChild(t);
    while (container.children.length > 4) container.removeChild(container.firstChild);  // cap the stack
    var killed = false;
    var dismiss = function () {
      if (killed) return; killed = true;
      t.classList.add('out');
      setTimeout(function () { if (t.parentNode) t.remove(); }, reduce ? 0 : 220);
    };
    t.querySelector('.wt-x').addEventListener('click', dismiss);
    setTimeout(dismiss, 6500);
  }

  // ── Notification center panel ──
  function renderPanel() {
    if (!panel) return;
    var rows = history.length
      ? history.map(function (n) {
          return '<div class="ws-notif-row ' + n.kind + '"><span class="nr-dot"></span>' +
            '<div class="nr-body"><div class="nr-title">' + esc(n.title) + '</div>' +
            (n.body ? '<div class="nr-sub">' + esc(n.body) + '</div>' : '') + '</div>' +
            '<span class="nr-time">' + esc(n.t) + '</span></div>';
        }).join('')
      : '<div class="ws-notif-empty">No notifications yet.</div>';
    panel.innerHTML = rows + (history.length ? '<button class="ws-notif-clear" id="ws-notif-clear" type="button">Clear all</button>' : '') +
      '<div class="ws-notif-prefs">' +
        '<label class="ws-notif-alerts"><input type="checkbox" id="ws-notif-alerts-cb"' + (alertsOn ? ' checked' : '') + '>' +
          '<span class="na-txt">Desktop alerts</span><span class="na-sub">critical events</span></label>' +
        '<label class="ws-notif-alerts"><input type="checkbox" id="ws-notif-pref-market"' + (prefs.market ? ' checked' : '') + '>' +
          '<span class="na-txt">Market open / close</span></label>' +
        '<label class="ws-notif-alerts"><input type="checkbox" id="ws-notif-pref-trades"' + (prefs.trades ? ' checked' : '') + '>' +
          '<span class="na-txt">Trade closes</span></label>' +
        '<div class="ws-notif-prefs-note">Safety alerts (kill switch · breaker) are always on.</div>' +
      '</div>';
  }
  function openPanel() {
    if (!wrap) return;
    renderPanel(); wrap.classList.add('open');
    unread = 0; updateBadge();
    if (bell) bell.setAttribute('aria-expanded', 'true');
  }
  function closePanel() { if (!wrap) return; wrap.classList.remove('open'); if (bell) bell.setAttribute('aria-expanded', 'false'); }
  if (bell) bell.addEventListener('click', function (e) {
    e.stopPropagation();
    wrap.classList.contains('open') ? closePanel() : openPanel();
  });
  if (panel) panel.addEventListener('click', function (e) {
    e.stopPropagation();
    if (e.target.closest('#ws-notif-clear')) { history = []; renderPanel(); }
  });
  if (panel) panel.addEventListener('change', function (e) {
    var t = e.target; if (!t) return;
    if (t.id === 'ws-notif-alerts-cb') setAlerts(t.checked);
    else if (t.id === 'ws-notif-pref-market') { prefs.market = t.checked; persistPrefs(); }
    else if (t.id === 'ws-notif-pref-trades') { prefs.trades = t.checked; persistPrefs(); }
  });
  document.addEventListener('click', function (e) {
    if (wrap && wrap.classList.contains('open') && !wrap.contains(e.target)) closePanel();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closePanel(); });

  // ── Diff successive cockpit snapshots into notifications ──
  function diff(cur) {
    if (!cur || !cur.ok) return;
    if (prev) {
      var pk = prev.kill_switch || {}, ck = cur.kill_switch || {};
      if (ck.ok && pk.ok && ck.active !== pk.active) {
        toast(ck.active
          ? { kind: 'bad', icon: '⛔', title: 'Kill switch engaged', body: 'Automated trading halted' }
          : { kind: 'ok', icon: '✓', title: 'Kill switch cleared' });
      }
      var pb = prev.breaker || {}, cb = cur.breaker || {};
      if (cb.ok && pb.ok && cb.tripped !== pb.tripped) {
        toast(cb.tripped
          ? { kind: 'bad', icon: '⛔', title: 'Drawdown breaker tripped', body: cb.reason || 'Trading blocked until reset' }
          : { kind: 'ok', icon: '✓', title: 'Drawdown breaker reset' });
      }
      var pm = prev.market || {}, cm = cur.market || {};
      if (prefs.market && cm.ok && pm.ok && cm.equities_open != null && pm.equities_open != null && cm.equities_open !== pm.equities_open) {
        toast(cm.equities_open
          ? { kind: 'ok', icon: '●', title: 'Market open', body: 'US equities regular session' }
          : { kind: 'info', icon: '○', title: 'Market closed', body: 'US equities' });
      }
      var pc = (typeof prev.closes_today === 'number') ? prev.closes_today : null;
      var cc = (typeof cur.closes_today === 'number') ? cur.closes_today : null;
      if (prefs.trades && pc != null && cc != null && cc > pc) {
        var k = cc - pc, rep = (cur.closes || [])[0] || null;
        toast({
          kind: rep ? (rep.pnl_up ? 'ok' : 'bad') : 'info',
          icon: '📈',
          title: k + ' trade' + (k === 1 ? '' : 's') + ' closed',
          body: rep ? (rep.ticker + (rep.pnl_fmt ? ' · ' + rep.pnl_fmt : '')) : ''
        });
      }
    }
    prev = cur;
  }

  function poll() {
    if (document.hidden) return;
    var my = ++seq;
    fetch('/api/workspace/desktop', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (my === seq) diff(d); })
      .catch(function () {});
  }

  // Prefer the cockpit's single poll: desktop.js dispatches 'chili:desktop' with
  // each fresh snapshot, so we diff off that and avoid a duplicate GET. We only
  // start our own poll as a fallback if that event never arrives (e.g. the page
  // has no cockpit / desktop.js).
  var fbTimer = null;
  var fb = setTimeout(function () { poll(); fbTimer = setInterval(poll, 20000); }, 24000);
  document.addEventListener('chili:desktop', function (e) {
    clearTimeout(fb);
    if (fbTimer) { clearInterval(fbTimer); fbTimer = null; }
    diff(e.detail);
  });
  // In fallback mode, re-poll when the tab regains focus (the cockpit-driven
  // path gets this for free via desktop.js's own visibility handler).
  document.addEventListener('visibilitychange', function () { if (!document.hidden && fbTimer) poll(); });
})();
