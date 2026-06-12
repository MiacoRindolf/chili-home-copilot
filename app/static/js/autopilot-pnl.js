(function() {
  'use strict';
  // P&L command band + per-symbol ledger (2026-06-12 money-first redesign).
  // Single source of truth: GET /api/trading/momentum/automation/pnl-rollup —
  // server-computed, uncapped, archived included. This file only renders.
  var AP = window.ChiliAutopilot;
  var esc = AP.esc;
  var fetchJson = AP.fetchJson;
  var IS_GUEST = AP.IS_GUEST;

  var _payload = null;
  var _fetchedAtMs = 0;
  var _bootMs = Date.now();
  var _fetchFailed = false;

  function signedMoney(v) {
    var n = Number(v);
    if (!isFinite(n)) return '—';
    return (n >= 0 ? '+' : '−') + '$' + Math.abs(n).toFixed(2);
  }
  function pnlCls(v) {
    var n = Number(v);
    if (!isFinite(n) || n === 0) return '';
    return n > 0 ? 'positive' : 'negative';
  }
  function qtyFmt(q) {
    var n = Number(q);
    if (!isFinite(n)) return '';
    return n >= 100 ? String(Math.round(n)) : String(Math.round(n * 10000) / 10000);
  }
  function pxFmt(p) {
    var n = Number(p);
    if (!isFinite(n)) return '—';
    return n < 1 ? n.toPrecision(4) : n.toFixed(2);
  }
  function wl(d) {
    if (!d.trades) return '';
    return d.trades + ' trades (' + d.wins + 'W–' + d.losses + 'L)';
  }

  function renderBand(p) {
    var live = p.buckets.live, paper = p.buckets.paper, alpaca = p.buckets.alpaca;
    var heroTotal = document.getElementById('ap-pnl-hero-total');
    var heroSub = document.getElementById('ap-pnl-hero-sub');
    if (heroTotal) {
      heroTotal.textContent = signedMoney(live.total_usd);
      heroTotal.className = 'ap-pnl-band__big ' + pnlCls(live.total_usd);
    }
    if (heroSub) {
      heroSub.textContent = 'Realized ' + signedMoney(live.realized_usd)
        + ' · Floating ' + signedMoney(live.floating_usd)
        + ' · ' + (wl(live) || '0 trades')
        + ' · 7d ' + signedMoney(live.realized_7d_usd);
    }
    var riskLine = document.getElementById('ap-pnl-risk-line');
    var riskSub = document.getElementById('ap-pnl-risk-sub');
    if (riskLine) {
      var atRisk = (live.at_risk_unknown_stops > 0 ? '≥ ' : '') + '$' + Number(live.at_risk_usd).toFixed(0);
      riskLine.textContent = 'OPEN ' + live.open_count + ' · AT-RISK ' + atRisk;
    }
    if (riskSub) {
      riskSub.textContent = live.at_risk_unknown_stops > 0
        ? ('⚠ ' + live.at_risk_unknown_stops + ' stop' + (live.at_risk_unknown_stops > 1 ? 's' : '') + ' unknown')
        : (live.armed_count + ' armed');
    }
    var pv = document.getElementById('ap-pnl-paper-val');
    var ps = document.getElementById('ap-pnl-paper-sub');
    if (pv) { pv.textContent = signedMoney(paper.total_usd); pv.className = 'ap-pnl-chip__val ' + pnlCls(paper.total_usd); }
    if (ps) ps.textContent = paper.open_count + ' open · ' + (wl(paper) || '0 trades') + ' · 7d ' + signedMoney(paper.realized_7d_usd);
    var av = document.getElementById('ap-pnl-alpaca-val');
    var as_ = document.getElementById('ap-pnl-alpaca-sub');
    if (av) { av.textContent = signedMoney(alpaca.total_usd); av.className = 'ap-pnl-chip__val ' + pnlCls(alpaca.total_usd); }
    if (as_) {
      var n = alpaca.open_count + alpaca.armed_count + alpaca.trades;
      as_.textContent = n > 0
        ? (alpaca.open_count + ' open · ' + (wl(alpaca) || '0 trades') + ' · 7d ' + signedMoney(alpaca.realized_7d_usd))
        : 'soak idle';
    }
  }

  var LANES = [
    { key: 'live', label: 'LIVE', cls: '' },
    { key: 'paper', label: 'PAPER', cls: 'ap-lane--paper' },
    { key: 'alpaca', label: 'ALPACA SOAK', cls: 'ap-lane--alpaca' }
  ];

  function laneEmpty(key, d) {
    if (key === 'live') {
      return 'No live fills today (ET)' + (d.armed_count ? ' · ' + d.armed_count + ' armed' : '');
    }
    if (key === 'alpaca') return 'Soak running · no twin fills yet';
    return 'No paper trades today';
  }

  function markCell(row) {
    if (row.state !== 'OPEN') return '—';
    var s = qtyFmt(row.qty) + ' @ ' + pxFmt(row.avg_price);
    if (row.mark != null) s += ' → ' + pxFmt(row.mark);
    if (row.mark_age_s != null) {
      s += row.mark_age_s > 30
        ? ' <span class="ap-badge warn">mark ' + row.mark_age_s + 's</span>'
        : ' <span class="ap-pnl-muted">' + row.mark_age_s + 's</span>';
    }
    return s;
  }

  function flattenCell(laneKey, row) {
    if (laneKey !== 'live' || row.state !== 'OPEN' || !row.session_id) return '';
    var ids = (row.open_session_ids && row.open_session_ids.length)
      ? row.open_session_ids : [row.session_id];
    return ids.map(function(sid, i) {
      var label = row.symbol + (ids.length > 1 ? ' #' + sid : '');
      return '<button type="button" class="ap-btn ap-btn-live" '
        + 'onclick="apSessionAction(' + Number(sid) + ',\'flatten\',\'' + esc(label) + '\')">'
        + 'Flatten ' + esc(label) + '</button>';
    }).join('');
  }

  function anchorCell(row) {
    if (!row.session_id) return '';
    return '<a class="ap-pnl-jump" href="#ap-sess-' + Number(row.session_id) + '" title="Jump to session card">→</a>';
  }

  function laneTable(lane, d) {
    var head = '<div class="ap-lane-head ' + lane.cls + '">'
      + '<span class="ap-lane-name">' + lane.label + '</span>'
      + '<span class="ap-lane-total ' + pnlCls(d.total_usd) + '">' + signedMoney(d.total_usd) + '</span>'
      + '<span class="ap-pnl-muted">Realized ' + signedMoney(d.realized_usd)
      + ' · Floating ' + signedMoney(d.floating_usd)
      + ' · ' + d.open_count + ' open · ' + (wl(d) || '0 trades')
      + ' · 7d ' + signedMoney(d.realized_7d_usd) + '</span>'
      + '</div>';
    var older = d.older_7d_symbols
      ? '<div class="ap-pnl-empty">+' + d.older_7d_symbols + ' more symbol' + (d.older_7d_symbols > 1 ? 's' : '') + ' traded earlier this week (in the 7d totals above)</div>'
      : '';
    if (!d.symbols.length) {
      return head + '<div class="ap-pnl-empty">' + esc(laneEmpty(lane.key, d)) + '</div>' + older;
    }
    var rows = d.symbols.map(function(r) {
      var stateCls = r.state === 'OPEN' ? 'open' : (r.state === 'ARMED' ? 'paper' : 'closed');
      return '<tr>'
        + '<td><span class="ap-pnl-sym">' + esc(r.symbol) + '</span>'
        + (r.asset_class === 'crypto' ? ' <span class="ap-pnl-muted">24/7</span>' : '') + '</td>'
        + '<td><span class="ap-badge ' + stateCls + '">' + esc(r.state) + '</span></td>'
        + '<td>' + markCell(r) + '</td>'
        + '<td class="ap-pnl-num ' + pnlCls(r.floating_usd) + '">' + (r.state === 'OPEN' ? signedMoney(r.floating_usd) : '—') + '</td>'
        + '<td class="ap-pnl-num ' + pnlCls(r.realized_usd) + '">' + signedMoney(r.realized_usd) + '</td>'
        + '<td class="ap-pnl-num ' + pnlCls(r.total_usd) + '"><b>' + signedMoney(r.total_usd) + '</b></td>'
        + '<td class="ap-pnl-num ' + pnlCls(r.realized_7d_usd) + '">' + signedMoney(r.realized_7d_usd) + '</td>'
        + '<td>' + esc(wl(r) || '0') + '</td>'
        + '<td class="ap-pnl-actions">' + flattenCell(lane.key, r) + anchorCell(r) + '</td>'
        + '</tr>';
    }).join('');
    return head
      + '<div class="ap-pattern-desk-table"><table class="ap-data-table ap-pnl-table">'
      + '<thead><tr><th>Symbol</th><th>State</th><th>Position</th><th>Floating</th><th>Realized</th><th>Total</th><th>7d</th><th>Trades</th><th></th></tr></thead>'
      + '<tbody>' + rows + '</tbody></table></div>' + older;
  }

  function renderLedger(p) {
    var host = document.getElementById('ap-pnl-ledger');
    if (!host) return;
    host.innerHTML = LANES.map(function(lane) {
      return '<div class="ap-lane ' + lane.cls + '">' + laneTable(lane, p.buckets[lane.key]) + '</div>';
    }).join('');
  }

  function renderStaleness() {
    var el = document.getElementById('ap-pnl-asof');
    var band = document.getElementById('ap-pnl-band');
    if (!el || !band) return;
    if (!_payload) {
      var waited = Math.round((Date.now() - _bootMs) / 1000);
      if (_fetchFailed || waited > 50) {
        band.classList.add('ap-pnl-stale');
        el.innerHTML = '<span class="ap-badge blocked">NO DATA</span>';
      } else {
        el.textContent = '—';
      }
      return;
    }
    var age = Math.round((Date.now() - _fetchedAtMs) / 1000);
    band.classList.toggle('ap-pnl-stale', age > 50);
    band.classList.toggle('ap-pnl-aging', age > 30 && age <= 50);
    el.innerHTML = age > 50
      ? '<span class="ap-badge blocked">STALE ' + age + 's</span>'
      : 'as of ' + esc(_payload.as_of_et) + ' ET';
  }

  window.apRefreshPnl = function() {
    if (IS_GUEST) return Promise.resolve();
    return fetchJson('/api/trading/momentum/automation/pnl-rollup', { credentials: 'same-origin' })
      .then(function(p) {
        _payload = p;
        _fetchedAtMs = Date.now();
        _fetchFailed = false;
        renderBand(p);
        renderLedger(p);
        renderStaleness();
      })
      .catch(function() { _fetchFailed = true; renderStaleness(); });
  };

  // The page header is itself sticky — the band must stack BELOW it, and
  // anchor jumps must clear both. Height is derived, not hardcoded.
  (function() {
    var hdr = document.querySelector('.ap-header');
    var band = document.getElementById('ap-pnl-band');
    function setOffsets() {
      var h = hdr ? hdr.offsetHeight : 64;
      document.documentElement.style.setProperty('--ap-header-h', h + 'px');
      document.documentElement.style.setProperty(
        '--ap-top-obstruction', (h + (band ? band.offsetHeight : 100) + 12) + 'px');
    }
    setOffsets();
    window.addEventListener('resize', setOffsets);
  })();

  window.setInterval(renderStaleness, 5000);
  document.addEventListener('visibilitychange', function() {
    if (!document.hidden) window.apRefreshPnl();
  });
  if (IS_GUEST) {
    var gSub = document.getElementById('ap-pnl-hero-sub');
    if (gSub) gSub.textContent = 'Sign in to view P&L';
    var gLedger = document.getElementById('ap-pnl-ledger');
    if (gLedger) gLedger.innerHTML = '<div class="ap-pnl-empty">Sign in (paired device) to view the P&L ledger.</div>';
  } else {
    window.apRefreshPnl();
  }
})();
