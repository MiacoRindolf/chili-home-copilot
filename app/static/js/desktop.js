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
    if (!lastActivityEl) return;
    lastActivityEl.textContent = 'Last trade · ' + relTime(lastActivityIso);
  }

  function tick() { tickClock(); renderActivity(); }
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
      html += '<div class="ws-stat"><span class="k tick ws-mono">' + esc(p.ticker) +
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
      html += '<div class="ws-stat"><span class="k tick ws-mono">' + esc(c.ticker) +
        '</span><span><span class="ws-tag">' + esc(c.pattern || '—') +
        '</span> <span class="ws-mono ' + cls + '">' + esc(c.pnl_fmt || '—') + '</span></span></div>';
    }
    el.innerHTML = html;
  }

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

    var k = d.kill_switch || {};
    if (!k.ok) setPill('ws-killswitch', 'unknown', 'Kill switch · —');
    else setPill('ws-killswitch', k.active ? 'bad' : 'ok', k.active ? 'Kill switch · ON' : 'Kill switch · off');

    var b = d.breaker || {};
    if (!b.ok) setPill('ws-breaker', 'unknown', 'Breaker · —');
    else setPill('ws-breaker', b.tripped ? 'bad' : 'ok', b.tripped ? 'Breaker · tripped' : 'Breaker · clear');

    lastActivityIso = (typeof d.last_trade_iso === 'string') ? d.last_trade_iso : null;
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
})();
