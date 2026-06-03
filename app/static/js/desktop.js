/* CHILI OS desktop cockpit — a live market clock plus auto-refreshing P/L and
   trading-safety widgets on the workspace home. Polls /api/workspace/desktop
   every 20s (and on load / when the tab regains focus); pauses while hidden.
   Vanilla, no deps. Degrades silently if the cockpit bar isn't on the page. */
(function () {
  var cockpit = document.getElementById('ws-cockpit');
  if (!cockpit) return;

  // ── Market clock: Eastern wall-clock, ticking client-side every second. ──
  var clockEl = document.getElementById('ws-clock');
  function tickClock() {
    if (!clockEl) return;
    try {
      clockEl.textContent = new Intl.DateTimeFormat('en-US', {
        timeZone: 'America/New_York', hour12: false,
        hour: '2-digit', minute: '2-digit', second: '2-digit'
      }).format(new Date());
    } catch (e) { /* Intl/timezone unavailable — leave the placeholder */ }
  }

  // ── Last-activity indicator: "Last trade · 12m ago", relative to now. ──
  // The server sends an ISO-8601 UTC string (or null); the relative phrase is
  // computed client-side and re-rendered on each clock tick so it advances
  // between the 20s polls. Null/unparseable → em dash.
  var lastActivityEl = document.getElementById('ws-last-activity');
  var lastActivityIso = null;  // stashed from the latest poll
  // Data-freshness indicator: "Data · 8s ago" — when market data was last
  // ingested (the brain's pipeline heartbeat, distinct from last trade).
  var dataFreshEl = document.getElementById('ws-data-fresh');
  var dataFreshIso = null;  // stashed from the latest poll
  function relTime(iso) {
    if (!iso) return '—';
    var t = Date.parse(iso);
    if (isNaN(t)) return '—';
    var secs = Math.floor((Date.now() - t) / 1000);
    if (secs < 0) secs = 0;  // clock skew — clamp to "just now"
    if (secs < 45) return 'just now';
    var mins = Math.floor(secs / 60);
    if (mins < 60) return mins + 'm ago';
    var hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + 'h ago';
    var days = Math.floor(hrs / 24);
    if (days === 1) return 'yesterday';
    if (days < 7) return days + 'd ago';
    try {
      return new Date(t).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    } catch (e) { return days + 'd ago'; }
  }
  function renderActivity() {
    if (lastActivityEl) lastActivityEl.textContent = 'Last trade · ' + relTime(lastActivityIso);
    if (dataFreshEl) dataFreshEl.textContent = 'Data · ' + relTime(dataFreshIso);
  }

  // ── Market-session countdown: "Opens · 1h 30m" / "Closes · 2h 14m" next to the
  //    market pill, ticking client-side off the ET clock. The open/closed STATE
  //    comes from the backend poll (mktOpen); the countdown targets the naive US
  //    equities regular session (9:30–16:00 ET, Mon–Fri). Holiday-safe: if the
  //    backend reports closed *during* regular hours it's a holiday/half-day, so
  //    we show "Market closed" instead of a misleading countdown. ──
  var mktOpen = null;  // latest equities_open from the poll (true / false / null=unknown)
  var SESSION_OPEN = 9 * 60 + 30, SESSION_CLOSE = 16 * 60;
  function etNow() {
    try {
      var o = {};
      new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour12: false, weekday: 'short', hour: '2-digit', minute: '2-digit' })
        .formatToParts(new Date()).forEach(function (p) { o[p.type] = p.value; });
      var dow = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }[o.weekday];
      if (dow == null) return null;
      return { dow: dow, h: parseInt(o.hour, 10) % 24, m: parseInt(o.minute, 10) };
    } catch (e) { return null; }
  }
  function fmtDur(mins) {
    if (mins < 1) return '<1m';
    var d = Math.floor(mins / 1440), h = Math.floor((mins % 1440) / 60), m = mins % 60;
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + (m < 10 ? '0' : '') + m + 'm';
    return m + 'm';
  }
  function minsToNextOpen(t) {
    var now = t.h * 60 + t.m;
    for (var d = 0; d < 8; d++) {
      var dow = (t.dow + d) % 7;
      if (dow >= 1 && dow <= 5) { var at = d * 1440 + SESSION_OPEN; if (at > now) return at - now; }
    }
    return 0;
  }
  // Pure: backend open-flag + ET parts → the session-countdown label.
  function sessionLabel(open, t) {
    if (!t || open == null) return 'Session · —';
    var now = t.h * 60 + t.m, weekday = t.dow >= 1 && t.dow <= 5;
    if (open) return 'Closes · ' + fmtDur(Math.max(0, SESSION_CLOSE - now));
    if (weekday && now >= SESSION_OPEN && now < SESSION_CLOSE) return 'Market closed';  // holiday / halt
    return 'Opens · ' + fmtDur(minsToNextOpen(t));
  }
  // Fraction of the regular session elapsed (0..1), or null when not mid-session.
  function sessionProgress(open, t) {
    if (open !== true || !t) return null;
    var now = t.h * 60 + t.m;
    return Math.max(0, Math.min(1, (now - SESSION_OPEN) / (SESSION_CLOSE - SESSION_OPEN)));
  }
  function renderSession() {
    var el = document.getElementById('ws-mkt-countdown'); if (!el) return;
    var t = etNow();
    el.textContent = sessionLabel(mktOpen, t);
    // Progress underline: a 2px accent bar filling left→right as the session elapses.
    var p = sessionProgress(mktOpen, t);
    if (p == null) { el.style.backgroundImage = ''; el.classList.remove('in-session'); return; }
    var pct = (p * 100).toFixed(1) + '%';
    el.style.backgroundImage = 'linear-gradient(90deg,var(--ws-accent) ' + pct + ',transparent ' + pct + ')';
    el.style.backgroundRepeat = 'no-repeat';
    el.style.backgroundPosition = 'left bottom';
    el.style.backgroundSize = '100% 2px';
    el.classList.add('in-session');
  }

  function tick() { tickClock(); renderActivity(); renderSession(); }
  tick();
  setInterval(tick, 1000);

  // ── Status pills (market / kill switch / breaker) ──
  // state ∈ {ok, warn, bad, unknown} drives the colour; label is the text.
  function setPill(id, state, label) {
    var el = document.getElementById(id); if (!el) return;
    el.classList.remove('ok', 'warn', 'bad', 'unknown');
    el.classList.add(state);
    var lbl = el.querySelector('.lbl'); if (lbl) lbl.textContent = label;
  }

  // ── KPI tiles — update in place, flashing the value when it changes. ──
  function setKpi(key, value, cls) {
    var v = document.querySelector('.ws-kpi[data-kpi="' + key + '"] .val');
    if (!v) return;
    if (v.textContent !== String(value)) {
      v.textContent = value;
      v.classList.remove('ws-flash'); void v.offsetWidth; v.classList.add('ws-flash');
    }
    if (cls !== undefined) { v.classList.remove('ws-up', 'ws-down'); if (cls) v.classList.add(cls); }
  }

  function setStatus(s) {
    var el = document.getElementById('ws-cockpit-status'); if (!el) return;
    el.classList.remove('live', 'offline');
    el.classList.add(s);
    var node = el.childNodes[el.childNodes.length - 1];
    if (node && node.nodeType === 3) node.nodeValue = s; else el.appendChild(document.createTextNode(s));
  }

  // ── Live list widgets (open positions / recent closes) ──
  // Tickers and patterns are data → HTML-escape before injecting.
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function renderPositions(list) {
    var el = document.getElementById('ws-live-positions'); if (!el) return;
    if (!list || !list.length) { el.innerHTML = '<div class="ws-empty">No open positions.</div>'; return; }
    var html = '';
    for (var i = 0; i < list.length; i++) {
      var p = list[i] || {};
      html += '<div class="ws-stat ws-stat-link" data-ticker="' + esc(p.ticker) + '" title="Open ' + esc(p.ticker) + ' on the desk"><span class="k tick ws-mono">' + esc(p.ticker) +
        '</span><span class="ws-tag">' + esc(p.side) + '</span></div>';
    }
    el.innerHTML = html;
  }

  function renderCloses(list) {
    var el = document.getElementById('ws-live-closes'); if (!el) return;
    if (!list || !list.length) { el.innerHTML = '<div class="ws-empty">No closes in the last 24h.</div>'; return; }
    var html = '';
    for (var i = 0; i < list.length; i++) {
      var c = list[i] || {};
      var cls = c.pnl_up ? 'ws-up' : 'ws-down';
      html += '<div class="ws-stat ws-stat-link" data-ticker="' + esc(c.ticker) + '" title="Open ' + esc(c.ticker) + ' on the desk"><span class="k tick ws-mono">' + esc(c.ticker) +
        '</span><span><span class="ws-tag">' + esc(c.pattern || '—') +
        '</span> <span class="ws-mono ' + cls + '">' + esc(c.pnl_fmt || '—') + '</span></span></div>';
    }
    el.innerHTML = html;
  }

  // Click a live position / recent close → open the Trading Desk on that ticker
  // (uses the deep-link plumbing). Delegated, so it covers re-rendered rows.
  ['ws-live-positions', 'ws-live-closes'].forEach(function (id) {
    var box = document.getElementById(id); if (!box) return;
    box.addEventListener('click', function (e) {
      var row = e.target.closest('.ws-stat-link'); if (!row) return;
      var t = row.getAttribute('data-ticker'); if (!t) return;
      if (window.ChiliOS && window.ChiliOS.open) window.ChiliOS.open('trading', '/trading?ticker=' + encodeURIComponent(t));
    });
  });

  function apply(d) {
    if (!d || !d.ok) { setStatus('offline'); return; }
    setKpi('net_pnl', d.net_pnl_fmt, d.net_pnl_up ? 'ws-up' : 'ws-down');
    setKpi('win_rate', d.win_rate_fmt);
    setKpi('open', d.open_positions);
    setKpi('patterns', d.top_patterns);

    renderPositions(d.positions);
    renderCloses(d.closes);

    var tp = document.getElementById('ws-topbar-pnl');
    if (tp && d.net_pnl_fmt) {
      tp.textContent = (d.net_pnl_up ? '▲ ' : '▼ ') + d.net_pnl_fmt + ' today';
      tp.classList.remove('ws-up', 'ws-down'); tp.classList.add(d.net_pnl_up ? 'ws-up' : 'ws-down');
    }

    var m = d.market || {};
    if (m.ok === false || m.equities_open == null) setPill('ws-mkt', 'unknown', 'Market · —');
    else setPill('ws-mkt', m.equities_open ? 'ok' : 'warn', m.equities_open ? 'Market · open' : 'Market · closed');
    mktOpen = (m.ok === false || m.equities_open == null) ? null : !!m.equities_open;
    renderSession();

    var k = d.kill_switch || {};
    if (!k.ok) setPill('ws-killswitch', 'unknown', 'Kill switch · —');
    else setPill('ws-killswitch', k.active ? 'bad' : 'ok', k.active ? 'Kill switch · ON' : 'Kill switch · off');

    var b = d.breaker || {};
    if (!b.ok) setPill('ws-breaker', 'unknown', 'Breaker · —');
    else setPill('ws-breaker', b.tripped ? 'bad' : 'ok', b.tripped ? 'Breaker · tripped' : 'Breaker · clear');

    lastActivityIso = (typeof d.last_trade_iso === 'string') ? d.last_trade_iso : null;
    dataFreshIso = (typeof d.data_fresh_iso === 'string') ? d.data_fresh_iso : null;
    renderActivity();

    setStatus('live');
  }

  var seq = 0;
  function poll() {
    if (document.hidden) return;
    var my = ++seq;
    fetch('/api/workspace/desktop', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (my !== seq) return;
        apply(d);
        // Share the snapshot so other widgets (notifications) don't re-poll.
        try { document.dispatchEvent(new CustomEvent('chili:desktop', { detail: d })); } catch (e) {}
      })
      .catch(function () { if (my === seq) setStatus('offline'); });
  }
  poll();
  setInterval(poll, 20000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });

  // Expose the pure session-label helper (other widgets / tests can compute the
  // same "Opens/Closes in …" string for any open-flag + ET time).
  window.ChiliDesktop = { sessionLabel: sessionLabel, sessionProgress: sessionProgress, etNow: etNow };
})();
