/* â”€â”€ Deep Dive tab switching + lazy loading â”€â”€â”€â”€ */
var _bddLoaded = {};
var _bddActiveTab = null;

function switchDeepDiveTab(tab) {
  _bddActiveTab = tab;
  document.querySelectorAll('#bdd-tabs .bdd-tab').forEach(function(btn) {
    btn.classList.toggle('active', btn.getAttribute('data-bdd') === tab);
  });
  document.querySelectorAll('#bdd-content .bdd-panel').forEach(function(p) {
    p.style.display = p.id === ('bdd-' + tab) ? '' : 'none';
  });
  if (!_bddLoaded[tab]) {
    _bddLoaded[tab] = true;
    if (tab === 'patterns') {
      loadLearnedPatterns();
      loadNearTradeableCandidates();
      pollBackfillStatus();
    } else if (tab === 'cycles') {
      loadCycleAiReportsPage(true);
      loadBrainActivity();
      _loadCycleDigestOnly();
    } else if (tab === 'ops') {
      _wireOpsTab();
      _startOpsPolling();
    } else if (tab === 'research') {
      _loadResearchData();
    } else if (tab === 'analytics') {
      loadBrainThesis();
    } else if (tab === 'debug') {
      loadInspectHealthCard();
    }
  }
  if (tab === 'ops') _startOpsPolling();
  else _stopOpsPolling();
}

function _loadCycleDigestOnly() {
  fetch('/api/trading/brain/stats').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var cdEl = document.getElementById('brain-cycle-digest');
    if (!cdEl) return;
    var lcd = d.last_cycle_digest;
    if (lcd && typeof lcd === 'object') {
      var parts = [];
      parts.push('<div style="font-weight:700;color:var(--text);margin-bottom:4px;font-size:10px">Last learning cycle</div>');
      if (lcd.updated_at) parts.push('<div style="font-size:9px;color:var(--text-muted)">Updated ' + escHtml(String(lcd.updated_at)) + '</div>');
      var line = [];
      if (lcd.elapsed_s != null) line.push(lcd.elapsed_s + 's');
      if (lcd.data_provider) line.push(escHtml(String(lcd.data_provider)));
      if (lcd.interrupted) line.push('<span style="color:#f59e0b">interrupted</span>');
      if (lcd.error) line.push('<span style="color:#ef4444">error</span>');
      if (line.length) parts.push('<div>' + line.join(' &middot; ') + '</div>');
      var counts = [];
      function _cdn(k, lab) { if (lcd[k] != null) counts.push(lab + ' <strong>' + lcd[k] + '</strong>'); }
      _cdn('prescreen_candidates', 'pre-screen'); _cdn('tickers_scored', 'scored');
      _cdn('snapshots_taken', 'snapshots'); _cdn('patterns_discovered', 'patterns');
      _cdn('backtests_run', 'backtests'); _cdn('proposals_generated', 'proposals');
      _cdn('signal_events', 'signals');
      if (counts.length) parts.push('<div>' + counts.join(' &middot; ') + '</div>');
      cdEl.innerHTML = parts.join('');
    }
  }).catch(function(){});
}

function _loadResearchData() {
  fetch('/api/trading/brain/stats').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    window._brainStats = d;
    var pipeline = document.getElementById('brain-pipeline');
    if (pipeline) {
      var snapDot = d.total_snapshots > 0 ? 'active' : 'empty';
      var pending = d.pending_predictions || 0;
      var evaluated = d.evaluated_snapshots || 0;
      var pendDot = pending > 0 ? 'pending' : 'empty';
      var verDot = evaluated > 0 ? 'active' : (d.early_predictions > 0 ? 'pending' : 'empty');
      var verLabel = evaluated > 0 ? evaluated.toLocaleString() : (d.early_predictions > 0 ? d.early_predictions.toLocaleString() + ' (3d)' : '0');
      pipeline.innerHTML =
        '<span class="bp-step"><span class="bp-dot ' + snapDot + '"></span> Snapshots: <span class="bp-num">' + (d.total_snapshots||0).toLocaleString() + '</span></span>' +
        '<span class="bp-arrow">&rarr;</span>' +
        '<span class="bp-step"><span class="bp-dot ' + pendDot + '"></span> Pending: <span class="bp-num">' + pending.toLocaleString() + '</span></span>' +
        '<span class="bp-arrow">&rarr;</span>' +
        '<span class="bp-step"><span class="bp-dot ' + verDot + '"></span> Verified: <span class="bp-num">' + verLabel + '</span></span>';
    }
    _fillResearchFunnel(d);
    _fillProposalSkips(d);
    _fillResearchKpiBenchmarks(d);
    _fillPipelineNear(d);
    _fillKpis(d);
  }).catch(function(){});
  fetch('/api/trading/brain/confidence-history').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok || !d.data || !d.data.length) return;
    var el = document.getElementById('brain-chart');
    if (!el) return;
    el.innerHTML = '';
    var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    brainChart = LightweightCharts.createChart(el, {
      width: el.clientWidth, height: el.clientHeight || 180,
      layout: { background: {type:'solid', color: isDark ? '#0f172a' : '#f8fafc'}, textColor: isDark ? '#9ca3af' : '#6b7280' },
      grid: { vertLines:{visible:false}, horzLines:{color: isDark ? '#1f2937' : '#f3f4f6'} },
      rightPriceScale: { borderVisible:false }, timeScale: { borderVisible:false }, handleScroll:false, handleScale:false,
    });
    brainConfSeries = brainChart.addAreaSeries({ lineColor: '#8b5cf6', topColor: 'rgba(139,92,246,.3)', bottomColor: 'rgba(139,92,246,.02)', lineWidth: 2 });
    brainConfSeries.setData(d.data);
    brainChart.timeScale().fitContent();
    new ResizeObserver(function() { if (brainChart) brainChart.applyOptions({width:el.clientWidth}); }).observe(el);
  }).catch(function(){});
}

function applyConfigProfile() {
  var sel = document.getElementById('ops-profile-select');
  var st = document.getElementById('ops-profile-status');
  var name = sel ? sel.value : '';
  if (!name) { if (st) st.textContent = 'Select a profile first.'; return; }
  if (st) st.textContent = 'Applying...';
  fetch('/api/trading/brain/config/apply-profile', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({profile: name})
  }).then(function(r){ return r.json(); }).then(function(d) {
    if (d.ok) {
      if (st) st.innerHTML = '<span style="color:#22c55e">Applied ' + escHtml(name) + ' (' + Object.keys(d.applied||{}).length + ' settings)</span>';
    } else {
      if (st) st.innerHTML = '<span style="color:#ef4444">' + escHtml(d.error || 'Failed') + '</span>';
    }
  }).catch(function(e) {
    if (st) st.innerHTML = '<span style="color:#ef4444">Error: ' + escHtml(e.message||'') + '</span>';
  });
}

function _wireOpsTab() {
  var qLink = document.getElementById('bw-queue-debug-link');
  if (qLink && !qLink._bwWired) {
    qLink._bwWired = true;
    qLink.addEventListener('click', function(ev) {
      ev.preventDefault();
      bwOpenQueueDebug();
    });
  }
}

var _opsPollingInterval = null;
function _startOpsPolling() {
  if (_opsPollingInterval) return;
  function refreshOps() {
    return loadBrainWorkerStatus()
      .catch(function(){})
      .then(function() { return loadBrainWorkerActivity().catch(function(){}); })
      .then(function() { return loadBacktestQueueStatus().catch(function(){}); });
  }
  refreshOps();
  _opsPollingInterval = setInterval(refreshOps, 10000);
}
function _stopOpsPolling() {
  if (_opsPollingInterval) { clearInterval(_opsPollingInterval); _opsPollingInterval = null; }
}

function startBwPolling() {
  loadBrainWorkerStatus().catch(function(){});
}

/* â”€â”€ Brain Dashboard (reuses /api/trading/brain/* endpoints) â”€â”€â”€ */
/* â”€â”€ Research sub-tab data helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
function _fillResearchFunnel(d) {
  var rfEl = document.getElementById('brain-research-funnel');
  if (rfEl && d.research_funnel) {
    var qf = d.research_funnel.queue || {};
    var pa = d.research_funnel.promotion_status_active || {};
    var pin = d.research_funnel.promotion_status_inactive || {};
    var qt = d.research_funnel.queue_tier_active || {};
    var cov = d.attribution_coverage || {};
    var bud = d.last_cycle_budget || {};
    var rej = (pa.rejected_oos||0) + (pa.rejected_bench||0) + (pa.rejected_bench_stress||0) + (pa.rejected_prescreen||0);
    rfEl.innerHTML =
      '<div style="font-weight:700;color:var(--text);margin-bottom:4px;font-size:10px">Research funnel</div>' +
      '<div>Backtest queue pending: <strong>' + (qf.pending!=null?qf.pending:'â€”') + '</strong> Â· ScanPatterns total: <strong>' + (d.research_funnel.scan_patterns_total||0) + '</strong></div>' +
      '<div>Active by promo: promoted <strong>' + (pa.promoted||0) + '</strong>, pending OOS <strong>' + (pa.pending_oos||0) + '</strong>, candidates <strong>' + (pa.candidate||0) + '</strong>, legacy <strong>' + (pa.legacy||0) + '</strong>, rejected sum <strong>' + rej + '</strong></div>' +
      '<div>Inactive: rejected OOS <strong>' + (pin.rejected_oos||0) + '</strong>, degraded live <strong>' + (pin.degraded_live||0) + '</strong>, rejected prescreen <strong>' + (pin.rejected_prescreen||0) + '</strong></div>' +
      (Object.keys(qt).length ? '<div>Queue tier (active): ' + Object.keys(qt).map(function(k){ return k + ' <strong>' + qt[k] + '</strong>'; }).join(' Â· ') + '</div>' : '') +
      (cov.coverage_pct!=null ? '<div>Live attribution: <strong>' + cov.coverage_pct + '%</strong> (' + (cov.closed_with_scan_pattern_id||0) + '/' + (cov.closed_trades||0) + ')</div>' : '') +
      (bud.ohlcv_used!=null ? '<div>Budget: OHLCV ' + bud.ohlcv_used + '/' + (bud.ohlcv_cap||'âˆž') + ', rows ' + (bud.miner_rows_used||0) + '/' + (bud.miner_rows_cap||'âˆž') + '</div>' : '');
  } else if (rfEl) { rfEl.innerHTML = ''; }
}

function _fillProposalSkips(d) {
  var psEl = document.getElementById('brain-proposal-skips');
  if (!psEl) return;
  var lps = d.last_proposal_skips;
  if (lps && typeof lps === 'object' && (lps.picks_total != null || lps.skips)) {
    var sk = lps.skips || {};
    var skeys = Object.keys(sk).filter(function(k){ return sk[k] > 0; }).sort();
    var skipRows = skeys.map(function(k){
      return '<div style="display:flex;justify-content:space-between;max-width:28rem;border-bottom:1px solid var(--border);padding:2px 0"><span style="font-size:9px">' + escHtml(k) + '</span><strong style="font-size:9px">' + sk[k] + '</strong></div>';
    }).join('');
    psEl.innerHTML =
      '<div style="font-weight:700;color:var(--text);margin-bottom:4px;font-size:10px">Proposal generation (last run)</div>' +
      '<div style="font-size:9px;color:var(--text-muted)">Picks ' + (lps.picks_total != null ? lps.picks_total : 'â€”') + ' Â· created <strong>' + (lps.created != null ? lps.created : 0) + '</strong>' + (lps.updated_at ? ' Â· ' + escHtml(String(lps.updated_at)) : '') + '</div>' +
      (skipRows ? '<div style="margin-top:6px">' + skipRows + '</div>' : '');
  } else {
    psEl.innerHTML = '<div style="font-weight:700;color:var(--text);margin-bottom:4px;font-size:10px">Proposal generation</div><div style="font-size:9px;color:var(--text-muted)">No skip rollup yet.</div>';
  }
}

function _fillResearchKpiBenchmarks(d) {
  var rkEl = document.getElementById('brain-research-kpi-benchmarks');
  if (rkEl && d.research_kpi_benchmarks && d.research_kpi_benchmarks.sample_count > 0 && d.research_kpi_benchmarks.means) {
    var m = d.research_kpi_benchmarks.means;
    var parts = [];
    function _rk(k, label) { if (m[k] != null) parts.push('<strong>' + label + '</strong> ' + m[k]); }
    _rk('sharpe_ratio', 'Sharpe'); _rk('sortino_ratio', 'Sortino'); _rk('information_ratio', 'IR');
    _rk('calmar_ratio', 'Calmar'); _rk('max_drawdown_pct', 'MaxDD%'); _rk('jensen_alpha_pct', 'Jensen Î±%'); _rk('win_loss_payoff_ratio', 'W/L payoff');
    rkEl.innerHTML = parts.length
      ? '<div style="font-weight:700;color:var(--text);margin-bottom:4px;font-size:10px">Research KPIs (mean, n=' + d.research_kpi_benchmarks.sample_count + ')</div><div>' + parts.join(' Â· ') + '</div>'
      : '';
  } else if (rkEl) { rkEl.innerHTML = ''; }
}

function _fillPipelineNear(d) {
  var pipeNear = document.getElementById('brain-pipeline-near');
  if (!pipeNear) return;
  var pn = d.pattern_pipeline_near;
  if (pn && pn.length) {
    var rows = pn.map(function(x) {
      var wr = x.oos_win_rate != null ? x.oos_win_rate + '% OOS' : 'OOS pending';
      var tc = x.oos_trade_count != null ? x.oos_trade_count + ' trades' : '';
      var rawName = x.name || ('#' + x.id);
      var nm = escHtml(rawName.length > 56 ? rawName.slice(0, 53) + 'â€¦' : rawName);
      var link = '/trading?tab=backtest&scan_pattern_id=' + encodeURIComponent(x.id);
      return '<div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)">' +
        '<a href="' + link + '" style="font-weight:600;color:var(--accent);text-decoration:none;max-width:240px">' + nm + '</a>' +
        '<span style="font-size:9px">' + wr + (tc ? ' Â· ' + tc : '') + '</span></div>';
    }).join('');
    pipeNear.innerHTML = '<div style="font-weight:700;color:var(--text);margin-bottom:4px;font-size:10px">Near promotion</div>' + rows;
  } else {
    pipeNear.innerHTML = '<div style="font-weight:700;color:var(--text);margin-bottom:4px;font-size:10px">Near promotion</div><div style="font-size:9px;color:var(--text-muted)">No active pending_oos or candidate patterns.</div>';
  }
}

function _fillKpis(d) {
  var kpis = document.getElementById('brain-kpis');
  if (!kpis) return;
  var hasData = d.total_predictions > 0;
  var pending = d.pending_predictions || 0;
  var ps = d.pipeline_status || 'no_data';
  function _accDisp(acc, count) { return count > 0 ? acc + '%' : '--'; }
  function _accCls(acc, count) { if (count === 0) return ''; return acc >= 55 ? 'good' : acc >= 40 ? 'warn' : 'danger'; }
  function _accColor(acc, count) { if (count === 0) return 'var(--text-muted)'; return acc >= 55 ? '#22c55e' : acc >= 40 ? '#f59e0b' : '#ef4444'; }
  function _noDataCls(count) { return count > 0 ? '' : ' no-data'; }
  var daysLeft = d.days_until_first_result;
  function _accSub(count) {
    if (count > 0) return '<div class="bk-trend flat">' + count + ' predictions</div>';
    if (ps === 'pending_verification') {
      var eta = (daysLeft != null && daysLeft > 0) ? '<br><span style="font-size:9px;color:var(--text-muted)">~' + daysLeft + 'd</span>' : '';
      return '<div class="bk-trend flat" style="color:#f59e0b">' + pending.toLocaleString() + ' awaiting verification' + eta + '</div>';
    }
    if (ps === 'collecting') return '<div class="bk-trend flat">Collecting snapshots&hellip;</div>';
    return '<div class="bk-trend flat">Run scans to start</div>';
  }
  var confCls = d.avg_confidence >= 60 ? 'good' : d.avg_confidence >= 40 ? 'warn' : 'danger';
  var weekTrend = d.patterns_this_week > 0
    ? '<div class="bk-trend up">&#9650; +' + d.patterns_this_week + ' this week</div>'
    : '<div class="bk-trend flat">&mdash; no new this week</div>';
  var strongSub = (d.strong_predictions||0) > 0
    ? '<div class="bk-trend flat">' + d.strong_predictions + ' high-conviction</div>'
    : _accSub(0);
  var earlyAcc = d.early_accuracy || 0;
  var earlyPred = d.early_predictions || 0;
  var hasEarly = !hasData && earlyPred > 0;
  var accRingPct = hasData ? d.prediction_accuracy : (hasEarly ? earlyAcc : 0);
  var strRingPct = (d.strong_predictions||0) > 0 ? (d.strong_accuracy||0) : 0;
  var accKpiCls = hasData ? _noDataCls(d.total_predictions) : (hasEarly ? '' : _noDataCls(0));
  var accDispVal = hasData ? _accDisp(d.prediction_accuracy, d.total_predictions) : (hasEarly ? earlyAcc + '%' : '--');
  var accClsVal = hasData ? _accCls(d.prediction_accuracy, d.total_predictions) : (hasEarly ? _accCls(earlyAcc, earlyPred) : '');
  var accColorVal = hasData ? _accColor(d.prediction_accuracy, d.total_predictions) : (hasEarly ? _accColor(earlyAcc, earlyPred) : 'var(--text-muted)');
  kpis.innerHTML =
    '<div class="brain-kpi clickable' + accKpiCls + '" onclick="showAccuracyDetail(\'all\')">' +
      '<div class="brain-ring">' + _makeRingSvg(accRingPct, accColorVal) + '<div class="ring-label ' + accClsVal + '">' + accDispVal + '</div></div>' +
      '<div class="bk-lbl">Overall Accuracy</div>' + _accSub(hasData ? d.total_predictions : earlyPred) + '</div>' +
    '<div class="brain-kpi clickable' + _noDataCls(d.strong_predictions||0) + '" onclick="showAccuracyDetail(\'strong\')">' +
      '<div class="brain-ring">' + _makeRingSvg(strRingPct, _accColor(d.strong_accuracy||0, d.strong_predictions||0)) + '<div class="ring-label ' + _accCls(d.strong_accuracy||0, d.strong_predictions||0) + '">' + _accDisp(d.strong_accuracy||0, d.strong_predictions||0) + '</div></div>' +
      '<div class="bk-lbl">Strong Signal</div>' + strongSub + '</div>' +
    '<div class="brain-kpi clickable" onclick="showKpiInfo(\'patterns\')">' +
      '<div class="bk-val" style="color:#8b5cf6">' + (d.total_scan_patterns || d.total_patterns) + '</div>' +
      '<div class="bk-lbl">Discovered Patterns</div>' + weekTrend + '</div>' +
    '<div class="brain-kpi clickable" onclick="showKpiInfo(\'confidence\')">' +
      '<div class="bk-val ' + confCls + '">' + d.avg_confidence + '%</div>' +
      '<div class="bk-lbl">Avg Confidence</div>' +
      (d.confidence_trend > 0 ? '<div class="bk-trend up">&#9650; +' + d.confidence_trend + '%</div>' : d.confidence_trend < 0 ? '<div class="bk-trend down">&#9660; ' + d.confidence_trend + '%</div>' : '<div class="bk-trend flat">&mdash; stable</div>') + '</div>' +
    '<div class="brain-kpi clickable' + _noDataCls(d.stock_predictions||0) + '" onclick="showAccuracyDetail(\'stock\')">' +
      '<div class="bk-val ' + _accCls(d.stock_accuracy||0, d.stock_predictions||0) + '">' + _accDisp(d.stock_accuracy||0, d.stock_predictions||0) + '</div>' +
      '<div class="bk-lbl">Stock Accuracy</div>' + _accSub(d.stock_predictions||0) + '</div>' +
    '<div class="brain-kpi clickable' + _noDataCls(d.crypto_predictions||0) + '" onclick="showAccuracyDetail(\'crypto\')">' +
      '<div class="bk-val ' + _accCls(d.crypto_accuracy||0, d.crypto_predictions||0) + '">' + _accDisp(d.crypto_accuracy||0, d.crypto_predictions||0) + '</div>' +
      '<div class="bk-lbl">Crypto Accuracy</div>' + _accSub(d.crypto_predictions||0) + '</div>' +
    '<div class="brain-kpi clickable" onclick="showMLDetail()">' +
      (d.ml_ready
        ? '<div class="bk-val" style="color:#06b6d4">' + (d.ml_accuracy||0) + '%</div><div class="bk-lbl">Pattern ML</div><div class="bk-trend flat">' + (d.ml_samples||0) + ' samples</div>'
        : '<div class="bk-val" style="color:var(--text-muted)">--</div><div class="bk-lbl">Pattern ML</div><div class="bk-trend flat">Not trained</div>') + '</div>' +
    '<div class="brain-kpi clickable" onclick="showKpiInfo(\'snapshots\')">' +
      '<div class="bk-val">' + d.total_snapshots.toLocaleString() + '</div>' +
      '<div class="bk-lbl">Snapshots</div>' +
      '<div class="bk-trend flat">' + (d.universe_total||0).toLocaleString() + ' tickers</div></div>';
}

function showAccuracyDetail(type) {
  var labels = {all:'Overall',strong:'Strong Signal',stock:'Stock',crypto:'Crypto'};
  openBrainModal(labels[type]+' Accuracy Detail', '<div style="text-align:center;padding:20px"><div class="brain-pulse"></div> Loading...</div>');
  fetch('/api/trading/brain/accuracy-detail?type=' + type + '&limit=20').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok || !d.rows || !d.rows.length) {
      document.getElementById('brain-modal-body').innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-muted)">No verified predictions yet for this category. Keep scanning to collect data.</div>';
      return;
    }
    var hits = d.rows.filter(function(r){return r.hit;}).length;
    var html = '<div style="margin-bottom:10px;font-size:11px"><strong>Accuracy:</strong> ' + Math.round(hits/d.rows.length*100) + '% (' + hits + '/' + d.rows.length + ')</div>' +
      '<table><thead><tr><th>Ticker</th><th>Date</th><th>Predicted</th><th>Actual 5d</th><th>Result</th></tr></thead><tbody>';
    d.rows.forEach(function(r) {
      var predColor = r.predicted_direction === 'bullish' ? '#22c55e' : '#ef4444';
      var retColor = r.actual_return_5d > 0 ? '#22c55e' : '#ef4444';
      html += '<tr><td><strong style="cursor:pointer;color:var(--accent)" onclick="goToTicker(\'' + r.ticker + '\');closeBrainModal()">' + r.ticker + '</strong></td>' +
        '<td style="font-size:10px;color:var(--text-muted)">' + (r.date ? new Date(r.date).toLocaleDateString() : '-') + '</td>' +
        '<td><span class="bm-tag" style="background:' + predColor + '20;color:' + predColor + '">' + r.predicted_direction + ' (' + r.predicted_score + ')</span></td>' +
        '<td style="color:' + retColor + ';font-weight:600">' + (r.actual_return_5d > 0 ? '+' : '') + r.actual_return_5d + '%</td>' +
        '<td><span class="bm-tag ' + (r.hit ? 'bm-hit' : 'bm-miss') + '">' + (r.hit ? '&#x2705; Hit' : '&#x274C; Miss') + '</span></td></tr>';
    });
    html += '</tbody></table>';
    document.getElementById('brain-modal-body').innerHTML = html;
  });
}

function showMLDetail() {
  var d = window._brainStats || {};
  if (!d.ml_ready) { openBrainModal('Pattern ML', '<div style="text-align:center;padding:20px;color:var(--text-muted)">Pattern meta-learner not trained yet. Click <strong>Retrain ML</strong> when you have enough snapshot data and active patterns.</div>'); return; }
  var html = '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px">' +
    '<div style="padding:8px;background:rgba(6,182,212,.06);border-radius:8px;text-align:center"><div style="font-size:18px;font-weight:800;color:#06b6d4">' + (d.ml_accuracy||0) + '%</div><div style="font-size:10px;color:var(--text-muted)">CV Accuracy</div></div>' +
    '<div style="padding:8px;background:rgba(139,92,246,.06);border-radius:8px;text-align:center"><div style="font-size:18px;font-weight:800;color:#8b5cf6">' + (d.ml_samples||0) + '</div><div style="font-size:10px;color:var(--text-muted)">Training Samples</div></div>' +
    '<div style="padding:8px;background:rgba(34,197,94,.06);border-radius:8px;text-align:center"><div style="font-size:18px;font-weight:800;color:#22c55e">' + (d.ml_active_patterns||0) + '</div><div style="font-size:10px;color:var(--text-muted)">Active Patterns</div></div></div>';
  var imps = d.ml_feature_importances || {};
  var entries = Object.entries(imps).sort(function(a,b){return b[1]-a[1];}).slice(0, 20);
  if (entries.length) {
    html += '<h4 style="margin-bottom:6px">Top Pattern Feature Importances</h4>';
    var maxVal = entries[0][1] || 1;
    entries.forEach(function(e) {
      var pct = Math.round(e[1] / maxVal * 100);
      var label = e[0].replace(/^pat_\d+_/, function(m) { return 'P' + m.split('_')[1] + ' '; });
      html += '<div style="display:flex;align-items:center;gap:6px;margin:3px 0"><span style="width:120px;font-size:10px;font-weight:600;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + e[0] + '">' + label + '</span><div style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden"><div style="height:100%;width:' + pct + '%;background:linear-gradient(90deg,#06b6d4,#8b5cf6);border-radius:3px"></div></div><span style="font-size:10px;color:var(--text-muted);width:40px">' + (e[1]*100).toFixed(1) + '%</span></div>';
    });
  }
  openBrainModal('Pattern ML Details', html);
}

function showKpiInfo(type) {
  var d = window._brainStats || {};
  var info = {
    patterns: { title: 'Discovered Patterns', html: '<p>CHILI discovers scan patterns by analyzing historical indicator snapshots and market data.</p><div style="margin:10px 0;padding:10px;background:rgba(139,92,246,.06);border-radius:8px;text-align:center"><div style="font-size:20px;font-weight:800;color:#8b5cf6">' + (d.total_scan_patterns || d.total_patterns) + '</div><div style="font-size:10px;color:var(--text-muted)">active scan patterns</div></div>' + (d.promoted_patterns ? '<div style="font-size:11px;text-align:center;color:var(--text-muted)">' + d.promoted_patterns + ' promoted to live</div>' : '') },
    confidence: { title: 'Average Confidence', html: '<p>Average confidence across all active patterns.</p><div style="margin:10px 0;padding:10px;background:rgba(139,92,246,.06);border-radius:8px;text-align:center"><div style="font-size:20px;font-weight:800">' + d.avg_confidence + '%</div></div>' },
    snapshots: { title: 'Market Snapshots', html: '<p>Snapshots record indicator values at a point in time. After ~5 days, CHILI checks actual price movement.</p><div style="margin:10px 0;padding:10px;background:rgba(139,92,246,.06);border-radius:8px;text-align:center"><div style="font-size:20px;font-weight:800">' + (d.total_snapshots||0).toLocaleString() + '</div><div style="font-size:10px;color:var(--text-muted)">total snapshots</div></div>' },
  };
  var entry = info[type] || {title:'Info',html:'<p>No additional details.</p>'};
  openBrainModal(entry.title, entry.html);
}

function loadBrainThesis() {
  _loadRegimeChip();
}

function showThesisDetail() {
  var d = window._brainThesisData; if (!d) return;
  var v = window._brainVixData || {};
  var sc = {bullish:'#22c55e', bearish:'#ef4444', neutral:'#f59e0b'}[d.stance] || '#f59e0b';
  var html = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px"><span class="bm-tag" style="background:' + sc + '20;color:' + sc + ';font-size:12px;padding:3px 12px">' + (d.stance||'neutral') + '</span>';
  if (v.regime) { var rc = {low:'#22c55e',normal:'#f59e0b',elevated:'#f97316',extreme:'#ef4444'}[v.regime]||'#6b7280'; html += '<span class="bm-tag" style="background:' + rc + '20;color:' + rc + '">VIX ' + (v.vix||'?') + '</span>'; }
  html += '</div>';
  if (typeof marked !== 'undefined') { try { html += '<div style="line-height:1.7">' + marked.parse(d.thesis||'') + '</div>'; } catch(e) { html += '<pre>' + escHtml(d.thesis) + '</pre>'; } }
  else html += '<pre>' + escHtml(d.thesis) + '</pre>';
  openBrainModal('Market Thesis', html, '<button onclick="closeBrainModal();loadBrainThesis()" style="background:linear-gradient(135deg,#8b5cf6,#6d28d9);color:#fff">&#x1F504; Regenerate</button>');
}
