var _bwPollInterval = null;
var _bwCollapsed = false;
var _bwCycleHistory = [];
var _bwCycleHistoryMax = 40;
var _bwCycleChart = null;

function toggleBwDetails() {
  /* Legacy no-op; Ops tab always shows details */
}

function updateBrainWorkerUI(data) {
  var dot = document.getElementById('bw-status-dot');
  var statusEl = document.getElementById('bw-status');
  var startBtn = document.getElementById('bw-start-btn');
  var wakeBtn = document.getElementById('bw-wake-btn');
  var pauseBtn = document.getElementById('bw-pause-btn');
  var stopBtn = document.getElementById('bw-stop-btn');
  var currentEl = document.getElementById('bw-current');
  var stepEl = document.getElementById('bw-current-step');
  var progressEl = document.getElementById('bw-current-progress');
  var uptimeEl = document.getElementById('bw-uptime');
  var cyclesEl = document.getElementById('bw-cycles');
  
  var status = data.status || 'stopped';
  dot.className = 'bw-status-dot ' + status;
  statusEl.textContent = status.charAt(0).toUpperCase() + status.slice(1);
  
  if (status === 'running' || status === 'paused') {
    startBtn.style.display = 'none';
    pauseBtn.style.display = '';
    stopBtn.style.display = '';
    pauseBtn.textContent = status === 'paused' ? 'â–¶ Resume' : 'â¸ Pause';
    /* Wake applies as soon as the worker is between iterations (idle sleep). If clicked during
       a long reconcile pass, the wake file is kept until the next sleep â€” always show when running. */
    if (wakeBtn) {
      wakeBtn.style.display = (status === 'running') ? '' : 'none';
    }
    
    if (data.started_at) {
      var started = new Date(data.started_at + 'Z');  // Append Z to indicate UTC
      uptimeEl.textContent = timeSince(started);
    }
    
    if (data.current_step) {
      currentEl.style.display = '';
      stepEl.textContent = data.current_step;
      var progressText = data.current_progress || '';
      if (data.current_step === 'Idle' && progressText.indexOf('Queue empty') !== -1 && data.queue_pending_live > 0) {
        var nextMatch = progressText.match(/Next (?:cycle|iteration) in (\d+ \w+)/i);
        progressText = data.queue_pending_live + ' pending (next iteration in ' + (nextMatch ? nextMatch[1] : '?') + ')';
      }
      progressEl.textContent = progressText;
    } else {
      currentEl.style.display = 'none';
    }
  } else {
    startBtn.style.display = '';
    if (wakeBtn) wakeBtn.style.display = 'none';
    pauseBtn.style.display = 'none';
    stopBtn.style.display = 'none';
    currentEl.style.display = 'none';
    uptimeEl.textContent = '--';
  }
  
  var totals = data.totals || {};
  cyclesEl.textContent = totals.cycles_completed || 0;
  document.getElementById('bw-stat-scanned').textContent = totals.tickers_scanned || 0;
  document.getElementById('bw-stat-snapshots').textContent = totals.snapshots_taken || 0;
  document.getElementById('bw-stat-mined').textContent = totals.patterns_mined || 0;
  document.getElementById('bw-stat-tested').textContent = totals.patterns_tested || 0;
  document.getElementById('bw-stat-validated').textContent = totals.hypotheses_validated || 0;
  document.getElementById('bw-stat-challenged').textContent = totals.hypotheses_challenged || 0;
  document.getElementById('bw-stat-evolved').textContent = totals.patterns_evolved || 0;
  document.getElementById('bw-stat-spawned').textContent = totals.patterns_spawned || 0;
  var promEl = document.getElementById('bw-stat-promoted');
  if (promEl) promEl.textContent = totals.patterns_variant_promoted || 0;
  document.getElementById('bw-stat-pruned').textContent = totals.patterns_pruned || 0;
  document.getElementById('bw-stat-decayed').textContent = totals.insights_decayed || 0;

  var lastCycle = data.last_cycle || {};
  /* ES5-safe nullish: avoid ?? (ES2020) â€” breaks script parse on older engines. */
  var durationSec = (lastCycle.duration_seconds != null) ? lastCycle.duration_seconds : lastCycle.elapsed_s;
  var stepTimings = lastCycle.step_timings || {};
  var timingsLabel = document.getElementById('bw-last-cycle-timings');
  var stepsEl = document.getElementById('bw-step-timings');
  if (timingsLabel) {
    if (durationSec != null && durationSec > 0) {
      var m = Math.floor(durationSec / 60);
      var s = Math.round(durationSec % 60);
      timingsLabel.querySelector('.bw-timings-label').textContent = 'Last reconcile pass: ' + (m ? m + 'm ' : '') + s + 's';
    } else {
      timingsLabel.querySelector('.bw-timings-label').textContent = 'Last reconcile pass: --';
    }
  }
  if (stepsEl && typeof stepTimings === 'object' && Object.keys(stepTimings).length > 0) {
    var keys = ['backtest_queue', 'pattern_variant_evolution', 'mine', 'ml_train', 'backtest_insights', 'scan', 'evolve', 'cycle_ai_report', 'pattern_engine'];
    var parts = [];
    keys.forEach(function(k) {
      if (stepTimings[k] != null) parts.push(k.replace(/_/g, ' ') + ': ' + stepTimings[k] + 's');
    });
    stepsEl.textContent = parts.length ? ' | ' + parts.join(' Â· ') : '';
  } else if (stepsEl) {
    stepsEl.textContent = '';
  }

  if (lastCycle.completed && (durationSec != null && durationSec > 0)) {
    var tested = lastCycle.patterns_tested || 0;
    var existing = _bwCycleHistory.some(function(h) { return h.completed === lastCycle.completed; });
    if (!existing) {
      var t = new Date(lastCycle.completed).getTime() / 1000;
      _bwCycleHistory.push({
        completed: lastCycle.completed,
        time: t,
        tested: tested,
        duration: Math.round(parseFloat(durationSec)),
      });
      _bwCycleHistory.sort(function(a, b) { return a.time - b.time; });
      if (_bwCycleHistory.length > _bwCycleHistoryMax) _bwCycleHistory = _bwCycleHistory.slice(-_bwCycleHistoryMax);
      _updateBwCycleChart();
    }
  }
}

function _updateBwCycleChart() {
  var el = document.getElementById('bw-cycle-chart');
  if (!el || _bwCycleHistory.length === 0) {
    if (el) el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;font-size:10px;color:var(--text-muted)">Complete a reconcile pass to see throughput history</div>';
    if (_bwCycleChart) { _bwCycleChart.remove(); _bwCycleChart = null; }
    return;
  }
  var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  var testedData = _bwCycleHistory.map(function(h) { return { time: h.time, value: h.tested }; });
  var durationData = _bwCycleHistory.map(function(h) { return { time: h.time, value: h.duration }; });
  if (!_bwCycleChart) {
    el.innerHTML = '';
    _bwCycleChart = LightweightCharts.createChart(el, {
      width: el.clientWidth, height: el.clientHeight || 100,
      layout: { background: { type: 'solid', color: isDark ? '#0f172a' : '#f8fafc' }, textColor: isDark ? '#9ca3af' : '#6b7280' },
      grid: { vertLines: { visible: false }, horzLines: { color: isDark ? '#1f2937' : '#f3f4f6' } },
      rightPriceScale: { visible: true, borderVisible: false },
      leftPriceScale: { visible: true, borderVisible: false },
      timeScale: { borderVisible: false }, handleScroll: false, handleScale: false,
    });
    _bwCycleChart.addLineSeries({ color: '#8b5cf6', lineWidth: 2, priceScaleId: 'left', title: 'Tested' }).setData(testedData);
    _bwCycleChart.addLineSeries({ color: '#22c55e', lineWidth: 2, priceScaleId: 'right', title: 'Duration (s)' }).setData(durationData);
    _bwCycleChart.timeScale().fitContent();
    new ResizeObserver(function() { if (_bwCycleChart) _bwCycleChart.applyOptions({ width: el.clientWidth }); }).observe(el);
  } else {
    var testedSeries = _bwCycleChart.series()[0];
    var durationSeries = _bwCycleChart.series()[1];
    if (testedSeries) testedSeries.setData(testedData);
    if (durationSeries) durationSeries.setData(durationData);
    _bwCycleChart.timeScale().fitContent();
  }
}

function loadCpcvShadowFunnel() {
  var el = document.getElementById('bw-cpcv-shadow-body');
  if (!el) return;
  function fmtMetric(x) {
    if (x == null || x === '') return '-';
    var n = Number(x);
    if (isNaN(n)) return '-';
    return n.toFixed(4);
  }
  fetch('/api/brain/cpcv_shadow_funnel').then(function(r) { return r.json(); }).then(function(d) {
    if (!d.ok || !d.rows || d.rows.length === 0) {
      el.innerHTML = (d.view_available === false)
        ? 'Shadow log not available yet (run app migrations through <code>164_cpcv_shadow_eval_log</code>).'
        : 'No CPCV evaluations in the last 7 days — shadow data populates as the brain runs mining cycles.';
      return;
    }
    var lines = ['<table style="width:100%;border-collapse:collapse;font-size:10px">'];
    lines.push('<tr><th align="left">scanner</th><th>n</th><th>pass CPCV</th><th>prior gate</th><th>med DSR</th><th>med PBO</th><th>med paths</th></tr>');
    d.rows.forEach(function(row) {
      lines.push(
        '<tr><td>' + escHtml(String(row.scanner || '')) + '</td><td>' + escHtml(String(row.n_evaluated || 0)) +
        '</td><td>' + escHtml(String(row.n_would_pass_cpcv || 0)) + '</td><td>' +
        escHtml(String(row.n_actually_passed_existing_gate || 0)) + '</td><td>' +
        fmtMetric(row.median_dsr) + '</td><td>' + fmtMetric(row.median_pbo) + '</td><td>' +
        fmtMetric(row.median_cpcv_paths) + '</td></tr>'
      );
    });
    lines.push('</table>');
    el.innerHTML = lines.join('');
  }).catch(function() {
    el.textContent = 'Could not load CPCV shadow funnel.';
  });
}

function loadRegimeSharpeHeatmap() {
  var el = document.getElementById('bw-regime-heatmap-body');
  if (!el) return;
  function cellColor(sh, n) {
    if (sh == null || n < 10) return 'var(--text-muted)';
    if (sh >= 0.5) return '#22c55e';
    if (sh <= -0.5) return '#ef4444';
    return 'var(--text-secondary)';
  }
  fetch('/api/brain/regime_sharpe_heatmap').then(function(r) { return r.json(); }).then(function(d) {
    if (!d.ok) {
      if (d.reason === 'flag_off') {
        el.innerHTML = 'Regime classifier not yet enabled.';
        return;
      }
      el.innerHTML = 'Regime heatmap not available yet (run migration <code>165_regime_snapshot_and_tagging</code>).';
      return;
    }
    if (!d.model_version) {
      el.innerHTML = 'Regime data populates after first weekly fit.';
      return;
    }
    var regimes = d.regimes || [];
    var scanners = d.scanners || [];
    var sm = d.sharpe_matrix || [];
    var nm = d.n_trades_matrix || [];
    var lines = ['<div style="font-size:9px;color:var(--text-muted);margin-bottom:6px">model ' + escHtml(String(d.model_version || '')) + ' · as_of ' + escHtml(String(d.as_of || '')) + '</div>'];
    lines.push('<table style="width:100%;border-collapse:collapse;font-size:10px">');
    lines.push('<tr><th align="left">regime</th>');
    scanners.forEach(function(sc) {
      lines.push('<th align="right">' + escHtml(String(sc)) + '</th>');
    });
    lines.push('</tr>');
    for (var ri = 0; ri < regimes.length; ri++) {
      lines.push('<tr><td>' + escHtml(String(regimes[ri])) + '</td>');
      for (var si = 0; si < scanners.length; si++) {
        var sh = sm[ri] ? sm[ri][si] : null;
        var nn = nm[ri] ? nm[ri][si] : 0;
        var txt = (sh == null || nn < 10) ? '—' : Number(sh).toFixed(2);
        var col = cellColor(sh, nn);
        lines.push('<td align="right" style="color:' + col + '" title="n=' + String(nn) + '">' + txt + '</td>');
      }
      lines.push('</tr>');
    }
    lines.push('</table>');
    el.innerHTML = lines.join('');
  }).catch(function() {
    el.textContent = 'Could not load regime heatmap.';
  });
}

function loadBrainWorkerStatus() {
  return fetch('/api/trading/brain/worker/status').then(function(r){return r.json();}).then(function(d) {
    if (d.ok) {
      updateBrainWorkerUI(d);
      try { window._deskWorkerSnapshot = d; } catch (eDw) {}
    }
    if (typeof renderOperatorDeskTrustStrip === 'function') renderOperatorDeskTrustStrip();
    if (typeof renderOperatorDeskFromBoard === 'function' && window._oppBoardLastGoodPayload) {
      renderOperatorDeskFromBoard(window._oppBoardLastGoodPayload);
    }
    loadCpcvShadowFunnel();
    loadRegimeSharpeHeatmap();
  }).catch(function(){});
}

function loadBacktestQueueStatus() {
  return fetch('/api/trading/backtest-queue/status').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var badge = document.getElementById('bw-queue-badge');
    var pendingEl = document.getElementById('bw-queue-pending');
    var boostedEl = document.getElementById('bw-queue-boosted');
    var testedEl = document.getElementById('bw-queue-tested');
    
    pendingEl.textContent = d.pending || 0;
    boostedEl.textContent = d.boosted || 0;
    testedEl.textContent = d.recently_tested || 0;
    
    if (d.queue_empty) {
      badge.textContent = 'Empty';
      badge.className = 'bw-queue-badge empty';
    } else if (d.pending > 10) {
      badge.textContent = d.pending + ' pending';
      badge.className = 'bw-queue-badge busy';
    } else {
      badge.textContent = d.pending + ' pending';
      badge.className = 'bw-queue-badge';
    }
  }).catch(function(){});
}

function boostPattern(patternId, buttonEl) {
  buttonEl.disabled = true;
  buttonEl.textContent = 'Boosting...';
  fetch('/api/trading/patterns/' + patternId + '/boost', {method: 'POST'})
    .then(function(r){return r.json();})
    .then(function(d) {
      if (d.ok) {
        buttonEl.textContent = 'Boosted!';
        buttonEl.classList.add('boosted');
        loadBacktestQueueStatus();
        setTimeout(function() {
          buttonEl.textContent = 'Boost';
          buttonEl.disabled = false;
          buttonEl.classList.remove('boosted');
        }, 3000);
      } else {
        buttonEl.textContent = 'Failed';
        buttonEl.disabled = false;
      }
    })
    .catch(function() {
      buttonEl.textContent = 'Error';
      buttonEl.disabled = false;
    });
}


function loadBrainWorkerActivity() {
  return fetch('/api/trading/brain/worker/recent-activity?limit=15').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var container = document.getElementById('bw-activity');
    if (!d.activity || d.activity.length === 0) {
      container.innerHTML = '<div style="color:var(--text-muted);font-style:italic">No activity yet</div>';
      return;
    }
    var html = '';
    d.activity.forEach(function(a) {
      var time = a.created_at ? timeSince(new Date(a.created_at)) : '';
      html += '<div class="bw-activity-item">' +
        '<span class="bw-activity-type">' + (a.type || '').replace(/_/g, ' ') + '</span>' +
        '<span class="bw-activity-summary">' + escHtml((a.summary || '').substring(0, 100)) + '</span>' +
        '<span class="bw-activity-time">' + time + '</span>' +
      '</div>';
    });
    container.innerHTML = html;
  }).catch(function(){});
}

function startBrainWorker() {
  var btn = document.getElementById('bw-start-btn');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Starting...';
  fetch('/api/trading/brain/worker/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({interval: 30})})
    .then(function(r) {
      return r.json().then(function(d) {
        return { ok: r.ok, status: r.status, body: d };
      }).catch(function() {
        return { ok: false, status: r.status, body: {} };
      });
    })
    .then(function(result) {
      btn.disabled = false;
      btn.textContent = 'â–¶ Start';
      if (result.ok && result.body && result.body.ok) {
        setTimeout(loadBrainWorkerStatus, 1000);
      } else {
        var msg = (result.body && result.body.error) ? result.body.error : ('Server error (' + (result.status || '') + ')');
        alert('Failed to start: ' + msg);
      }
    })
    .catch(function(e) {
      btn.disabled = false;
      btn.textContent = 'â–¶ Start';
      alert('Error: ' + (e.message || e));
    });
}

/** POST to brain worker control endpoints; same-origin, tolerant of non-JSON errors. */
function _brainWorkerControlPost(url, onSuccess) {
  fetch(url, { method: 'POST', credentials: 'same-origin' })
    .then(function(r) {
      return r.text().then(function(t) {
        var j = {};
        try { j = t ? JSON.parse(t) : {}; } catch (e) { j = { ok: false, error: 'Invalid response (HTTP ' + r.status + ')' }; }
        return { httpOk: r.ok, status: r.status, body: j };
      });
    })
    .then(function(res) {
      if (res.httpOk && res.body && res.body.ok) {
        if (onSuccess) onSuccess();
      } else {
        var msg = (res.body && res.body.error) ? res.body.error : ('Request failed (HTTP ' + res.status + ')');
        alert(msg);
      }
    })
    .catch(function(err) {
      var m = (err && err.message) ? err.message : String(err);
      alert('Could not reach CHILI. Is the server running? ' + m);
    });
}

function stopBrainWorker() {
  brainConfirm({
    title: 'Stop Brain Worker',
    body: 'This will halt all learning cycles, pattern mining, and reconciliation. The worker can be restarted at any time.',
    confirmLabel: 'Stop Worker',
    variant: 'danger'
  }).then(function(ok) {
    if (!ok) return;
    _brainWorkerControlPost('/api/trading/brain/worker/stop', function() {
      brainToast({ type: 'warning', title: 'Worker stopped', message: 'Brain worker has been stopped.' });
      setTimeout(loadBrainWorkerStatus, 1000);
    });
  });
}

function pauseBrainWorker() {
  _brainWorkerControlPost('/api/trading/brain/worker/pause', function() {
    setTimeout(loadBrainWorkerStatus, 500);
  });
}

function bwOpenQueueDebug() {
  fetch('/api/trading/brain/worker/queue-debug?limit=50', { credentials: 'same-origin' })
    .then(function(r) {
      return r.json().then(function(d) {
        return { httpOk: r.ok, body: d };
      }).catch(function() {
        return { httpOk: r.ok, body: { ok: false, error: 'Invalid JSON' } };
      });
    })
    .then(function(res) {
      var d = res.body || {};
      if (!res.httpOk || !d.ok) {
        alert(d.error || ('Request failed (' + (res.httpOk ? 'bad response' : 'HTTP error') + ')'));
        return;
      }
      var ids = (d.eligible_pending_pattern_ids || []).join(', ');
      if (!ids) ids = '(none eligible)';
      alert((d.note || '') + '\n\nFirst ' + (d.limit || 50) + ' IDs:\n' + ids);
    })
    .catch(function(err) {
      alert('Could not reach CHILI. ' + (err.message || err));
    });
}

function runQueueBatchFromWeb() {
  if (!confirm('Run one backtest queue batch in the background on this web server?\n\nDoes not require the separate brain_worker process. Check server logs for [learning] Queue backtest.')) return;
  var btn = document.getElementById('bw-queue-batch-btn');
  if (btn) { btn.disabled = true; }
  fetch('/api/trading/brain/worker/run-queue-batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: '{}',
  })
    .then(function(r) {
      return r.json().then(function(d) {
        return { httpOk: r.ok, status: r.status, body: d };
      }).catch(function() {
        return { httpOk: r.ok, status: r.status, body: {} };
      });
    })
    .then(function(result) {
      if (btn) { btn.disabled = false; }
      if (result.httpOk && result.body && result.body.ok) {
        alert(result.body.message || 'Queue batch started.');
        setTimeout(loadBrainWorkerStatus, 1500);
        setTimeout(function() { loadBacktestQueueStatus().catch(function(){}); }, 2000);
      } else {
        var msg = (result.body && result.body.error) ? result.body.error : ('Server error (' + (result.status || '') + ')');
        alert(msg);
      }
    })
    .catch(function(e) {
      if (btn) { btn.disabled = false; }
      alert('Error: ' + (e.message || e));
    });
}

function wakeBrainWorker() {
  var btn = document.getElementById('bw-wake-btn');
  var prevLabel = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; }
  fetch('/api/trading/brain/worker/wake-cycle', { method: 'POST', credentials: 'same-origin' })
    .then(function(r) {
      return r.json().then(function(d) {
        return { httpOk: r.ok, status: r.status, body: d };
      }).catch(function() {
        return { httpOk: r.ok, status: r.status, body: { ok: false, error: 'Invalid JSON (HTTP ' + r.status + ')' } };
      });
    })
    .then(function(result) {
      if (btn) { btn.disabled = false; }
      if (result.httpOk && result.body && result.body.ok) {
        if (btn) {
          btn.textContent = 'Queuedâ€¦';
          setTimeout(function() { if (btn) btn.textContent = prevLabel; }, 2500);
        }
        var body = result.body;
        var notes = body.notes || [];
        var warnings = body.warnings || [];
        var msg = body.message || 'Wake queued.';
        var heartbeatFresh = body.worker_heartbeat_fresh === true;
        var parts = [];
        if (!heartbeatFresh) {
          parts.push(msg);
          if (warnings.length) parts.push(warnings.join('\n\n'));
          if (notes.length) parts.push(notes.join('\n'));
          parts.push('Wake only affects the separate brain_worker.py process using this database.');
          alert(parts.filter(Boolean).join('\n\n'));
        } else {
          if (notes.length) {
            var noteWarn = notes.some(function(n) {
              return /dead|Could not verify|Could not read|paused/i.test(String(n));
            });
            if (noteWarn || warnings.length) {
              alert([msg, warnings.join('\n\n'), notes.join('\n')].filter(Boolean).join('\n\n'));
            } else {
              console.info('[brain worker wake]', msg, notes, body.last_heartbeat_at || '');
            }
          } else if (warnings.length) {
            alert([msg, warnings.join('\n\n')].filter(Boolean).join('\n\n'));
          }
        }
        setTimeout(loadBrainWorkerStatus, 500);
      } else {
        var err = (result.body && result.body.error) ? result.body.error : ('Request failed (HTTP ' + (result.status || '') + ')');
        alert(err);
      }
    })
    .catch(function(err) {
      if (btn) { btn.disabled = false; }
      var m = (err && err.message) ? err.message : String(err);
      alert('Could not reach CHILI. Is the server running? ' + m);
    });
}

/** Row-level trade analytics (ScanPattern id). Same APIs as former Brain Worker panel. */
function _evTradeAnalyticsSpId() {
  return window._currentBacktestPatternId || null;
}
function evTradeAnalyticsLoad() {
  var out = document.getElementById('ev-trade-analytics-out');
  var spId = _evTradeAnalyticsSpId();
  if (!spId) {
    if (out) { out.style.display = 'block'; out.textContent = 'No ScanPattern linked to this insight yet. Try again after evidence loads, or ensure the pattern is linked.'; }
    return;
  }
  if (out) { out.style.display = 'block'; out.textContent = 'Loadingâ€¦'; }
  fetch('/api/trading/brain/pattern/' + spId + '/trade-analytics')
    .then(parseFetchJson)
    .then(function(d){ if (out) out.textContent = JSON.stringify(d, null, 2); })
    .catch(function(err){ if (out) out.textContent = 'Request failed: ' + (err && err.message ? err.message : 'unknown'); });
}
function evTradeAnalyticsPropose() {
  var out = document.getElementById('ev-trade-analytics-out');
  var spId = _evTradeAnalyticsSpId();
  if (!spId) {
    if (out) { out.style.display = 'block'; out.textContent = 'No ScanPattern linked to this insight yet.'; }
    return;
  }
  if (out) { out.style.display = 'block'; out.textContent = 'Proposingâ€¦'; }
  fetch('/api/trading/brain/pattern/' + spId + '/evidence/propose', {method: 'POST'})
    .then(parseFetchJson)
    .then(function(d){ if (out) out.textContent = JSON.stringify(d, null, 2); })
    .catch(function(err){ if (out) out.textContent = 'Request failed: ' + (err && err.message ? err.message : 'unknown'); });
}
function evTradeAnalyticsMl() {
  var out = document.getElementById('ev-trade-analytics-out');
  var spId = _evTradeAnalyticsSpId();
  if (!spId) {
    if (out) { out.style.display = 'block'; out.textContent = 'No ScanPattern linked to this insight yet.'; }
    return;
  }
  if (out) { out.style.display = 'block'; out.textContent = 'Trainingâ€¦'; }
  fetch('/api/trading/brain/pattern/' + spId + '/trade-ml')
    .then(parseFetchJson)
    .then(function(d){ if (out) out.textContent = JSON.stringify(d, null, 2); })
    .catch(function(err){ if (out) out.textContent = 'Request failed: ' + (err && err.message ? err.message : 'unknown'); });
}

/** Re-run every deduped stored backtest row for this insight (background job on server). */
function evRerunAllListedBacktests() {
  var iid = window._currentEvidencePatternId;
  var out = document.getElementById('ev-trade-analytics-out');
  if (!iid) {
    if (out) { out.style.display = 'block'; out.textContent = 'Evidence not loaded yet â€” wait for the modal to finish loading.'; }
    return;
  }
  var badge = document.getElementById('ev-badge-backtests');
  var n = badge ? String(badge.textContent || '').trim() : '';
  brainConfirm({
    title: 'Re-run All Backtests',
    body: 'Re-run every backtest row listed under the Backtests tab for this insight? ' +
      'Each row uses its saved period/OHLC window and current ScanPattern rules. ' +
      'Runs in the background; 100+ tickers can take many minutes.<br><br>' +
      '<strong>Backtests tab count: ' + (n || '?') + '</strong>',
    confirmLabel: 'Re-run All',
    variant: 'primary'
  }).then(function(ok) {
    if (!ok) return;
    if (out) { out.style.display = 'block'; out.textContent = 'Queuing reruns\u2026'; }
    fetch('/api/trading/learn/patterns/' + iid + '/rerun-stored-backtests', { method: 'POST' })
      .then(parseFetchJson)
      .then(function(d){
        if (!out) return;
        if (d.ok) {
          brainToast({ type: 'success', title: 'Backtests queued', message: 'Queued ' + (d.queued || 0) + ' reruns.' });
          out.textContent = 'Queued ' + (d.queued || 0) + ' reruns.\n' + (d.message || '');
        } else {
          out.textContent = 'Error: ' + (d.error || JSON.stringify(d));
        }
      })
      .catch(function(err){ if (out) out.textContent = 'Request failed: ' + (err && err.message ? err.message : 'unknown'); });
  });
}

var brainChart = null, brainConfSeries = null;
var _brainEventsRaw = [], _brainActiveFilter = 'all';
var brainPollInterval = null;
var _predShowAll = false, _PRED_INITIAL = 30;

function loadBrainDashboard() {
  /* Zone 1: status bar â€” worker status + regime chip */
  startBwPolling();
  _loadRegimeChip();

  /* Zone 2: Playbook + P&L + Predictions + Tradeable */
  loadPlaybook();
  loadPerfDashboard();
  loadOpportunityBoard();
  loadBrainPredictions();
  loadTradeablePatterns();
  loadResearchEdgePatterns();

  /* Polling: learning status (status bar only) */
  pollLearningStatus().catch(function(){});
  if (brainPollInterval) clearInterval(brainPollInterval);
  brainPollInterval = setInterval(function() {
    pollLearningStatus().catch(function(){});
  }, 3000);

  /* Opportunity board: soft auto-refresh while tab visible (no overlap with in-flight; see loadOpportunityBoard) */
  _schedOppBoardSoftAutoRefresh();

  /* Tradeable auto-refresh */
  if (window._tradeablePatternsInterval) clearInterval(window._tradeablePatternsInterval);
  window._tradeablePatternsInterval = setInterval(function() { loadTradeablePatterns(); }, 120000);

  /* Reset deep-dive tab state so tabs lazy-load fresh */
  _bddLoaded = {};
  _bddActiveTab = null;
}

function _loadRegimeChip() {
  fetch('/api/trading/brain/thesis').then(parseFetchJson).then(function(d) {
    if (!d.ok) {
      var chip0 = document.getElementById('bsb-regime');
      if (chip0) {
        chip0.innerHTML = '<span style="font-size:10px;color:var(--text-muted)">Thesis unavailable</span>';
        chip0.className = 'bsb-chip';
      }
      return;
    }
    window._brainThesisData = d;
    if (typeof renderOperatorDeskFromBoard === 'function' && window._oppBoardLastGoodPayload) {
      renderOperatorDeskFromBoard(window._oppBoardLastGoodPayload);
    }
    var chip = document.getElementById('bsb-regime');
    if (!chip) return;
    var icons = {bullish:'&#x1F7E2;', bearish:'&#x1F534;', neutral:'&#x1F7E1;'};
    var cls = {bullish:'bsb-chip-bull', bearish:'bsb-chip-bear'};
    chip.innerHTML = (icons[d.stance]||'&#x1F7E1;') + ' ' + (d.stance||'neutral').charAt(0).toUpperCase() + (d.stance||'neutral').slice(1);
    chip.className = 'bsb-chip ' + (cls[d.stance]||'');

    /* Also fill the full thesis card in Analytics tab */
    var stanceEl = document.getElementById('thesis-stance');
    var gaugeEl = document.getElementById('thesis-gauge');
    var textEl = document.getElementById('thesis-text');
    var metaEl = document.getElementById('thesis-meta');
    var card = document.getElementById('brain-thesis-card');
    if (card) {
      card.className = 'brain-thesis-card ' + d.stance;
      card.style.cursor = 'pointer';
      card.onclick = function() { showThesisDetail(); };
    }
    if (stanceEl) {
      stanceEl.className = 'thesis-stance ' + d.stance;
      var stanceLabels = {bullish:'&#x1F7E2; Bullish', bearish:'&#x1F534; Bearish', neutral:'&#x1F7E1; Neutral'};
      stanceEl.innerHTML = stanceLabels[d.stance] || 'Neutral';
    }
    if (gaugeEl) {
      var gp = d.stance === 'bullish' ? 75 : d.stance === 'bearish' ? 25 : 50;
      var gc = d.stance === 'bullish' ? '#22c55e' : d.stance === 'bearish' ? '#ef4444' : '#f59e0b';
      gaugeEl.style.width = gp + '%'; gaugeEl.style.background = gc;
    }
    if (textEl) {
      var truncated = d.thesis.length > 300 ? d.thesis.substring(0, 300) + '...' : d.thesis;
      if (typeof marked !== 'undefined') { try { textEl.innerHTML = marked.parse(truncated); } catch(e) { textEl.textContent = truncated; } }
      else textEl.innerHTML = truncated;
      textEl.style.color = 'var(--text)';
    }
    if (metaEl) {
      var mp = [];
      if (d.patterns_count) mp.push(d.patterns_count + ' patterns');
      if (d.total_predictions) mp.push(d.accuracy + '% accuracy (' + d.total_predictions + ')');
      metaEl.innerHTML = mp.join(' &middot; ');
    }
  }).catch(function(err) {
    var hint = (err && err.message) ? escHtml(String(err.message)) : 'Network or server error.';
    var chipE = document.getElementById('bsb-regime');
    if (chipE) {
      chipE.innerHTML = '<span style="font-size:10px;color:var(--text-muted)">Thesis failed</span><br/><span style="font-size:8px;opacity:.85">' + hint + '</span>';
      chipE.className = 'bsb-chip';
    }
  });
  fetch('/api/trading/brain/volatility').then(parseFetchJson).then(function(v) {
    if (!v.ok) return;
    window._brainVixData = v;
    var chip = document.getElementById('bsb-regime');
    if (chip) {
      var regimeColors = {low:'#22c55e', normal:'#f59e0b', elevated:'#f97316', extreme:'#ef4444', unknown:'#6b7280'};
      chip.innerHTML += ' <span style="font-size:8px;opacity:.8">VIX ' + (v.vix||'?') + '</span>';
    }
    var stanceEl = document.getElementById('thesis-stance');
    if (stanceEl) {
      var rc2 = {low:'#22c55e', normal:'#f59e0b', elevated:'#f97316', extreme:'#ef4444', unknown:'#6b7280'};
      stanceEl.innerHTML += ' <span style="font-size:9px;padding:1px 6px;border-radius:8px;background:' + (rc2[v.regime]||'#6b7280') + '20;color:' + (rc2[v.regime]||'#6b7280') + ';font-weight:700;vertical-align:middle">VIX ' + (v.vix||'?') + ' ' + (v.label||'') + '</span>';
    }
  }).catch(function(err) {
    var hint = (err && err.message) ? escHtml(String(err.message)) : 'Network or server error.';
    var chipV = document.getElementById('bsb-regime');
    if (chipV) {
      chipV.innerHTML += '<br/><span style="font-size:8px;opacity:.85;color:#b45309">Volatility failed: ' + hint + '</span>';
    }
  });
}

function loadPlaybook() {
  var el = document.getElementById('brain-playbook-content');
  if (!el) return;
  el.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:8px"><div class="brain-pulse"></div> Generating playbookâ€¦</div>';
  fetch('/api/trading/brain/playbook', {credentials:'same-origin'})
    .then(parseFetchJson)
    .then(function(d) {
      if (!d.ok) { el.innerHTML = '<div style="padding:12px;text-align:center">No playbook data.</div>'; return; }
      var html = '';
      var reg = d.regime || {};
      var regColor = reg.composite === 'risk_on' ? '#22c55e' : (reg.composite === 'risk_off' ? '#ef4444' : '#f59e0b');
      html += '<div style="background:var(--card-bg,rgba(30,30,40,.6));border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:8px">';
      html += '<div style="font-weight:700;font-size:12px;margin-bottom:4px"><span style="color:'+regColor+'">&#x25CF;</span> Regime: <span style="color:'+regColor+'">' + (reg.composite||'unknown').toUpperCase() + '</span></div>';
      html += '<div style="font-size:10px;color:var(--text-muted)">' + (reg.guidance || '') + '</div>';
      html += '</div>';

      var risk = d.risk || {};
      html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:6px;margin-bottom:8px">';
      html += _perfCard('Open Pos', String(risk.open_positions||0), 'var(--text)');
      html += _perfCard('Heat', (risk.total_heat_pct||0).toFixed(1)+'%', (risk.total_heat_pct||0)>4?'#ef4444':'#22c55e');
      html += _perfCard('Avail Heat', (risk.available_heat_pct||0).toFixed(1)+'%', '#60a5fa');
      html += _perfCard('Max New', String(risk.max_new_trades_today||0), (risk.max_new_trades_today||0)>0?'#22c55e':'#ef4444');
      html += '</div>';

      if (risk.breaker_tripped) {
        html += '<div style="background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.3);border-radius:6px;padding:6px 10px;font-size:10px;color:#ef4444;margin-bottom:8px">&#x26A0; Circuit breaker active: ' + (risk.breaker_reason||'') + '</div>';
      }

      var ideas = d.ideas || [];
      if (ideas.length) {
        html += '<div style="font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted);margin-bottom:4px">Top Trade Ideas</div>';
        html += '<table style="width:100%;font-size:10px;border-collapse:collapse">';
        html += '<tr style="color:var(--text-muted)"><th style="padding:3px;text-align:left">Pattern</th><th style="padding:3px">Score</th><th style="padding:3px">OOS WR</th><th style="padding:3px">Avg Ret</th><th style="padding:3px">TF</th></tr>';
        ideas.forEach(function(idea) {
          var sc = idea.idea_score >= 0.5 ? '#22c55e' : '#f59e0b';
          html += '<tr><td style="padding:3px">' + idea.pattern_name + '</td>';
          html += '<td style="padding:3px;text-align:center;color:'+sc+'">' + idea.idea_score.toFixed(3) + '</td>';
          html += '<td style="padding:3px;text-align:center">' + idea.oos_win_rate.toFixed(1) + '%</td>';
          html += '<td style="padding:3px;text-align:center">' + idea.avg_return_pct.toFixed(2) + '%</td>';
          html += '<td style="padding:3px;text-align:center">' + idea.timeframe + '</td></tr>';
        });
        html += '</table>';
      }

      var wl = d.watchlist || [];
      if (wl.length) {
        html += '<div style="font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted);margin:8px 0 4px">Near Promotion Watchlist</div>';
        wl.forEach(function(w) {
          html += '<div style="font-size:10px;padding:2px 0">' + w.name + ' â€” OOS WR ' + w.oos_win_rate.toFixed(1) + '%, ' + w.backtest_count + ' backtests</div>';
        });
      }

      html += '<div style="font-size:9px;color:var(--text-muted);margin-top:8px;text-align:right">Generated: ' + (d.generated_at || '') + '</div>';
      el.innerHTML = html;
    })
    .catch(function(err) {
      var hint = (err && err.message) ? escHtml(String(err.message)) : 'Network or server error.';
      el.innerHTML = '<div style="padding:12px;text-align:center;color:var(--text-muted)">Failed to generate playbook.<br/><span style="font-size:10px;opacity:.85">' + hint + '</span></div>';
    });
}

function loadPerfDashboard() {
  var el = document.getElementById('brain-perf-content');
  if (!el) return;
  el.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:8px"><div class="brain-pulse"></div> Loadingâ€¦</div>';
  fetch('/api/trading/brain/performance', {credentials:'same-origin'})
    .then(parseFetchJson)
    .then(function(d) {
      if (!d.ok) { el.innerHTML = '<div style="padding:12px;text-align:center">No performance data yet.</div>'; return; }
      var o = d.overall || {};
      var w = d.week || {};
      var m = d.month || {};
      var html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:12px">';
      html += _perfCard('Total P&L', '$' + (o.total_pnl||0).toFixed(2), (o.total_pnl||0)>=0?'#22c55e':'#ef4444');
      html += _perfCard('Win Rate', (o.win_rate||0).toFixed(1)+'%', (o.win_rate||0)>=50?'#22c55e':'#f59e0b');
      html += _perfCard('Trades', String(o.total_trades||0), 'var(--text)');
      html += _perfCard('7d P&L', '$' + (w.pnl||0).toFixed(2), (w.pnl||0)>=0?'#22c55e':'#ef4444');
      html += _perfCard('30d P&L', '$' + (m.pnl||0).toFixed(2), (m.pnl||0)>=0?'#22c55e':'#ef4444');
      html += _perfCard('30d WR', (m.win_rate||0).toFixed(1)+'%', (m.win_rate||0)>=50?'#22c55e':'#f59e0b');
      html += '</div>';

      if (d.daily_pnl && d.daily_pnl.length) {
        html += '<div style="margin-bottom:8px;font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted)">30-Day Equity Curve</div>';
        html += '<div style="display:flex;align-items:flex-end;gap:2px;height:60px;overflow-x:auto">';
        var maxAbs = 1;
        d.daily_pnl.forEach(function(dp) { maxAbs = Math.max(maxAbs, Math.abs(dp.pnl)); });
        d.daily_pnl.forEach(function(dp) {
          var h = Math.max(2, Math.abs(dp.pnl) / maxAbs * 50);
          var c = dp.pnl >= 0 ? '#22c55e' : '#ef4444';
          html += '<div title="' + dp.date + ': $' + dp.pnl.toFixed(2) + '" style="width:6px;height:'+h+'px;background:'+c+';border-radius:2px;flex-shrink:0"></div>';
        });
        html += '</div>';
      }

      if (d.attribution && d.attribution.length) {
        html += '<div style="margin-top:12px;font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted)">Pattern Attribution (Top 10)</div>';
        html += '<table style="width:100%;font-size:10px;border-collapse:collapse;margin-top:4px">';
        html += '<tr style="text-align:left;color:var(--text-muted)"><th style="padding:3px">Pattern</th><th style="padding:3px">Trades</th><th style="padding:3px">P&L</th><th style="padding:3px">WR</th></tr>';
        d.attribution.slice(0,10).forEach(function(a) {
          var pc = a.total_pnl >= 0 ? '#22c55e' : '#ef4444';
          html += '<tr><td style="padding:3px">' + (a.pattern_name||'â€”') + '</td>';
          html += '<td style="padding:3px">' + a.trades + '</td>';
          html += '<td style="padding:3px;color:'+pc+'">$' + a.total_pnl.toFixed(2) + '</td>';
          html += '<td style="padding:3px">' + a.win_rate.toFixed(1) + '%</td></tr>';
        });
        html += '</table>';
      }
      el.innerHTML = html;
    })
    .catch(function(err) {
      var hint = (err && err.message) ? escHtml(String(err.message)) : 'Network or server error.';
      el.innerHTML = '<div style="padding:12px;text-align:center;color:var(--text-muted)">Failed to load performance data.<br/><span style="font-size:10px;opacity:.85">' + hint + '</span></div>';
    });
}
function _perfCard(label, value, color) {
  return '<div style="background:var(--card-bg, rgba(30,30,40,.6));border:1px solid var(--border);border-radius:8px;padding:8px 10px;text-align:center">'
    + '<div style="font-size:9px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted)">' + label + '</div>'
    + '<div style="font-size:16px;font-weight:700;color:' + color + ';margin-top:2px">' + value + '</div>'
    + '</div>';
}

function _phase2ResearchLine(ov) {
  if (!ov || typeof ov !== 'object') return '';
  var ps = ov.parameter_stability;
  var sb = ov.selection_bias;
  var bits = [];
  if (ps && typeof ps === 'object') {
    var st = ps.stability_tier != null ? String(ps.stability_tier) : '';
    var ss = ps.stability_score != null ? String(ps.stability_score) : '';
    if (st || ss) {
      bits.push('stability ' + escHtml(st || 'â€”') + (ss ? ' (score ' + escHtml(ss) + ')' : ''));
    }
  }
  if (sb && typeof sb === 'object') {
    if (sb.skip_reason) {
      bits.push('slice burn skipped: ' + escHtml(String(sb.skip_reason)));
    } else {
      var bt = sb.burn_tier != null ? String(sb.burn_tier) : '';
      var uc = sb.usage_count != null ? String(sb.usage_count) : '';
      if (bt || uc) {
        bits.push('slice burn ' + escHtml(bt || 'â€”') + (uc ? ' (uses ' + escHtml(uc) + ')' : ''));
      }
    }
  }
  var hf = ov.research_hygiene_flags;
  if (hf && hf.length) {
    bits.push('hygiene: ' + escHtml(hf.join(', ')));
  }
  if (!bits.length) return '';
  return '<div style="font-size:8px;color:var(--text-muted);margin-top:4px;line-height:1.35" title="Phase 2 research hygiene (display only; not a promotion gate)">' + bits.join(' Â· ') + '</div>';
}

function _liveDriftLine(p) {
  var s = p.live_drift_summary;
  if (!s || typeof s !== 'object') return '';
  var tier = s.drift_tier != null ? String(s.drift_tier) : '';
  var parts = [];
  if (s.skip_reason) {
    parts.push('skipped: ' + escHtml(String(s.skip_reason)));
  } else {
    if (tier) parts.push('tier ' + escHtml(tier));
    if (s.drift_delta != null) parts.push('delta ' + escHtml(String(s.drift_delta)) + ' pp');
    if (s.sample_count != null) parts.push('n=' + escHtml(String(s.sample_count)));
    if (s.primary_runtime_source) parts.push(escHtml(String(s.primary_runtime_source)));
  }
  if (!parts.length) return '';
  return '<div style="font-size:8px;color:var(--text-muted);margin-top:4px;line-height:1.35" title="Phase 3 live/paper drift vs research (display only)">Live drift: ' + parts.join(' Â· ') + '</div>';
}

function _executionRobustnessLine(p) {
  var s = p.execution_robustness_summary;
  if (!s || typeof s !== 'object') return '';
  var parts = [];
  if (s.skip_reason) {
    parts.push('skipped: ' + escHtml(String(s.skip_reason).replace(/_/g, ' ')));
  } else {
    if (s.robustness_tier != null) parts.push('tier ' + escHtml(String(s.robustness_tier)));
    if (s.provider_truth_mode != null) parts.push('truth ' + escHtml(String(s.provider_truth_mode).replace(/_/g, ' ')));
    if (s.fill_rate != null) {
      var fr = Number(s.fill_rate);
      parts.push('fill ' + escHtml(String(isFinite(fr) ? Math.round(fr * 1000) / 10 : s.fill_rate)) + '%');
    }
    if (s.avg_realized_slippage_bps != null) parts.push('slip ' + escHtml(String(s.avg_realized_slippage_bps)) + ' bps');
    if (s.readiness_impact_flags && s.readiness_impact_flags.length) {
      parts.push('flags ' + escHtml(s.readiness_impact_flags.join(', ').replace(/_/g, ' ')));
    }
  }
  if (!parts.length) return '';
  return '<div style="font-size:8px;color:var(--text-muted);margin-top:4px;line-height:1.35" title="Phase 4 execution robustness from linked trades (display only)">Execution: ' + parts.join(' Â· ') + '</div>';
}

function loadTradeablePatterns() {
  var container = document.getElementById('brain-tradeable-patterns');
  if (!container) return;
  container.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:8px"><div class="brain-pulse"></div> Loading tradeable patternsâ€¦</div>';
  fetch('/api/trading/brain/tradeable-patterns').then(parseFetchJson).then(function(d) {
    if (!d.ok || !d.patterns) {
      container.innerHTML = '<div style="padding:8px;color:var(--text-muted)">Could not load tradeable patterns.</div>';
      return;
    }
    if (!d.patterns.length) {
      container.innerHTML = '<div style="padding:10px;color:var(--text-muted);line-height:1.5">No patterns match the current gates (promoted status, min OOS win rate, min trades). Run the brain worker to promote more patterns, or adjust <code style="font-size:9px">brain_tradeable_*</code> in config.</div>';
      return;
    }
    var html = '<div class="tp-tradeable-grid">';
    d.patterns.forEach(function(p) {
      var wr = p.display_win_rate_pct != null ? p.display_win_rate_pct + '%' : '--';
      var wrNote = p.display_wr_source === 'oos' ? 'OOS' : (p.display_wr_source === 'in_sample' ? 'IS' : '');
      var trades = p.trade_count_for_gate != null ? p.trade_count_for_gate : '--';
      var bench = '';
      if (p.bench_passes_gate === true) {
        bench = '<span style="font-size:8px;padding:2px 6px;border-radius:4px;background:rgba(34,197,94,.15);color:#22c55e">Bench pass</span>';
      } else if (p.bench_passes_gate === false) {
        bench = '<span style="font-size:8px;color:var(--text-muted)">Bench: no</span>';
      }
      if (p.bench_stress_passes_gate === true) {
        bench += '<span style="font-size:8px;padding:2px 6px;border-radius:4px;background:rgba(6,182,212,.15);color:#06b6d4;margin-left:4px">Stress pass</span>';
      } else if (p.bench_stress_passes_gate === false) {
        bench += '<span style="font-size:8px;color:var(--text-muted);margin-left:4px">Stress: no</span>';
      }
      if (p.oos_validation && p.oos_validation.bootstrap_mean_oos_wr_ci) {
        var ci = p.oos_validation.bootstrap_mean_oos_wr_ci;
        bench += '<span style="font-size:8px;color:var(--text-muted);margin-left:4px" title="Bootstrap CI on mean OOS WR (tickers)">CI ' + ci[0] + 'â€“' + ci[1] + '%</span>';
      }
      var rk = '';
      if (p.research_kpi_summary && p.research_kpi_summary.means) {
        var mm = p.research_kpi_summary.means;
        var rkBits = [];
        if (mm.sharpe_ratio != null) rkBits.push('Sh ' + mm.sharpe_ratio);
        if (mm.sortino_ratio != null) rkBits.push('So ' + mm.sortino_ratio);
        if (mm.information_ratio != null) rkBits.push('IR ' + mm.information_ratio);
        if (mm.calmar_ratio != null) rkBits.push('Ca ' + mm.calmar_ratio);
        if (rkBits.length) rk = '<div style="font-size:8px;color:var(--text-muted);margin-top:4px;line-height:1.4" title="Mean KPIs across stored backtests for this pattern">' + rkBits.join(' Â· ') + '</div>';
      }
      var rawName = (p.name || ('Pattern #' + p.id));
      var name = escHtml(rawName.length > 64 ? rawName.slice(0, 61) + 'â€¦' : rawName);
      var tf = escHtml(p.timeframe || 'â€”');
      var ac = escHtml(p.asset_class || 'â€”');
      var promo = escHtml((p.promotion_status || '').replace(/</g, ''));
      var edgeLine = '';
      if (p.edge_evidence && typeof p.edge_evidence === 'object') {
        var ee = p.edge_evidence;
        var isp = ee.in_sample_perm_p != null ? ee.in_sample_perm_p : '--';
        var wfp = ee.walk_forward_perm_p != null ? ee.walk_forward_perm_p : '--';
        var tier = ee.evidence_tier != null ? String(ee.evidence_tier) : '--';
        edgeLine = '<div style="font-size:8px;color:var(--text-muted);margin-top:4px;line-height:1.35" title="v1 weak-null permutation layer (display only; server enforces gates)">' +
          'Edge evidence (v1 weak-null): tier ' + escHtml(tier) + ', IS p ' + escHtml(String(isp)) + ', WF p ' + escHtml(String(wfp)) + '</div>';
      }
      var phase2Line = _phase2ResearchLine(p.oos_validation);
      var driftLine = _liveDriftLine(p);
      var execLine = _executionRobustnessLine(p);
      var link = '/trading?tab=backtest&scan_pattern_id=' + encodeURIComponent(p.id);
      html += '<div class="tp-tradeable-card">' +
        '<div style="font-size:11px;font-weight:700;color:var(--text);margin-bottom:4px;line-height:1.35">' + name + '</div>' +
        '<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:10px;color:var(--text-secondary)">' +
          '<span><strong style="color:var(--text)">' + wr + '</strong> WR' + (wrNote ? ' <span style="color:var(--text-muted)">(' + wrNote + ')</span>' : '') + '</span>' +
          '<span>' + trades + ' trades</span>' +
          '<span>' + tf + ' Â· ' + ac + '</span>' +
          '<span style="font-size:8px;text-transform:uppercase;letter-spacing:.04em;color:var(--accent)">' + promo + '</span>' +
          bench +
        '</div>' +
        rk +
        edgeLine +
        phase2Line +
        driftLine +
        execLine +
        '<a href="' + link + '" style="display:inline-block;margin-top:8px;font-size:10px;font-weight:600;color:var(--accent);text-decoration:none">Open in Trading â†’</a>' +
      '</div>';
    });
    html += '</div>';
    container.innerHTML = html;
  }).catch(function(err) {
    var hint = (err && err.message) ? escHtml(String(err.message)) : 'Network or server error.';
    container.innerHTML = '<div style="padding:8px;color:var(--text-muted)">Failed to load tradeable patterns.<br/><span style="font-size:9px;opacity:.85">' + hint + '</span></div>';
  });
}

function loadResearchEdgePatterns() {
  var container = document.getElementById('brain-research-edge-patterns');
  if (!container) return;
  container.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:8px"><div class="brain-pulse"></div> Loading edge research laneâ€¦</div>';
  fetch('/api/trading/brain/research-edge-patterns').then(parseFetchJson).then(function(d) {
    if (!d.ok || !d.patterns) {
      container.innerHTML = '<div style="padding:8px;color:var(--text-muted)">Could not load research-edge patterns.</div>';
      return;
    }
    if (!d.patterns.length) {
      container.innerHTML = '<div style="padding:10px;color:var(--text-muted);line-height:1.5">No patterns in <code style="font-size:9px">validated</code> or <code style="font-size:9px">challenged</code> lifecycle yet. Run hypothesis tests on OOS-gated (web/brain-discovered) patterns with <code style="font-size:9px">brain_edge_evidence_enabled</code>.</div>';
      return;
    }
    var html = '<div class="tp-tradeable-grid">';
    d.patterns.forEach(function(p) {
      var ee = p.edge_evidence;
      var rawName = (p.name || ('Pattern #' + p.id));
      var name = escHtml(rawName.length > 56 ? rawName.slice(0, 53) + 'â€¦' : rawName);
      var life = escHtml((p.lifecycle_stage || '').replace(/</g, ''));
      var promo = escHtml((p.promotion_status || '').replace(/</g, ''));
      var link = '/trading?tab=backtest&scan_pattern_id=' + encodeURIComponent(p.id);
      var body = '';
      if (ee && typeof ee === 'object') {
        var disc = ee.weak_null_disclaimer ? escHtml(String(ee.weak_null_disclaimer)) : '';
        var wfSrc = ee.walk_forward_evidence_source ? escHtml(String(ee.walk_forward_evidence_source)) : '';
        var blocks = (ee.promotion_block_codes && ee.promotion_block_codes.length) ? escHtml(ee.promotion_block_codes.join(', ')) : '';
        body =
          '<div style="font-size:9px;color:var(--text-secondary);line-height:1.45;margin-top:6px">' +
            '<div><strong>IS score</strong> ' + escHtml(String(ee.in_sample_score != null ? ee.in_sample_score : '--')) +
            ' Â· <strong>IS p</strong> ' + escHtml(String(ee.in_sample_perm_p != null ? ee.in_sample_perm_p : '--')) + '</div>' +
            '<div><strong>WF score</strong> ' + escHtml(String(ee.walk_forward_score != null ? ee.walk_forward_score : '--')) +
            ' Â· <strong>WF p</strong> ' + escHtml(String(ee.walk_forward_perm_p != null ? ee.walk_forward_perm_p : '--')) +
            (wfSrc ? ' <span style="color:var(--text-muted)">(' + wfSrc + ')</span>' : '') + '</div>' +
            '<div style="margin-top:4px"><strong>OOS mean WR %</strong> ' + escHtml(String(ee.oos_mean_wr_pct != null ? ee.oos_mean_wr_pct : '--')) +
            ' Â· <strong>OOS p</strong> ' + escHtml(String(ee.oos_perm_p != null ? ee.oos_perm_p : '--')) + '</div>' +
            '<div style="margin-top:4px"><strong>n_eff</strong> ' + escHtml(String(ee.effective_n != null ? ee.effective_n : '--')) +
            ' Â· <strong>OOS coverage</strong> ' + escHtml(String(ee.oos_coverage != null ? ee.oos_coverage : '--')) +
            ' Â· <strong>tier</strong> ' + escHtml(String(ee.evidence_tier != null ? ee.evidence_tier : '--')) + '</div>' +
            (ee.evidence_fresh_at ? '<div style="font-size:8px;color:var(--text-muted);margin-top:4px">Updated ' + escHtml(String(ee.evidence_fresh_at)) + '</div>' : '') +
            (blocks ? '<div style="font-size:8px;color:var(--text-muted);margin-top:4px">promotion_block_codes: ' + blocks + '</div>' : '') +
            (disc ? '<div style="font-size:8px;color:var(--text-muted);margin-top:6px;line-height:1.35">' + disc + '</div>' : '') +
          '</div>';
      } else {
        body = '<div style="font-size:9px;color:var(--text-muted);margin-top:6px">No edge_evidence JSON yet for this row.</div>';
      }
      var phase2Line = _phase2ResearchLine(p.oos_validation);
      var driftLine = _liveDriftLine(p);
      var execLineRe = _executionRobustnessLine(p);
      html += '<div class="tp-tradeable-card">' +
        '<div style="font-size:11px;font-weight:700;color:var(--text);margin-bottom:2px;line-height:1.35">' + name + '</div>' +
        '<div style="font-size:9px;color:var(--text-secondary)">lifecycle <strong style="color:var(--accent)">' + life + '</strong> Â· ' + promo + '</div>' +
        body +
        phase2Line +
        driftLine +
        execLineRe +
        '<a href="' + link + '" style="display:inline-block;margin-top:8px;font-size:10px;font-weight:600;color:var(--accent);text-decoration:none">Open in Trading â†’</a>' +
        '</div>';
    });
    html += '</div>';
    container.innerHTML = html;
  }).catch(function(err) {
    var hint = (err && err.message) ? escHtml(String(err.message)) : 'Network or server error.';
    container.innerHTML = '<div style="padding:8px;color:var(--text-muted)">Failed to load edge research lane.<br/><span style="font-size:9px;opacity:.85">' + hint + '</span></div>';
  });
}

function loadNearTradeableCandidates() {
  var el = document.getElementById('brain-near-tradeable-candidates');
  if (!el) return;
  el.innerHTML = '<span style="color:var(--text-muted)">Loadingâ€¦</span>';
  fetch('/api/trading/brain/tradeable-patterns?include_candidates=true&limit=12').then(parseFetchJson).then(function(d) {
    if (!d.ok || !d.patterns) {
      el.innerHTML = '<span style="color:var(--text-muted)">Could not load candidates.</span>';
      return;
    }
    var onlyCand = d.patterns.filter(function(p) { return (p.promotion_status || '') === 'candidate'; });
    if (!onlyCand.length) {
      el.innerHTML = '<span style="color:var(--text-muted)">No <code style="font-size:9px">candidate</code> patterns meet the same gates. Promoted patterns appear in the grid above.</span>';
      return;
    }
    var html = '<div class="tp-tradeable-grid">';
    onlyCand.forEach(function(p) {
      var wr = p.display_win_rate_pct != null ? p.display_win_rate_pct + '%' : '--';
      var trades = p.trade_count_for_gate != null ? p.trade_count_for_gate : '--';
      var rawName = (p.name || ('Pattern #' + p.id));
      var name = escHtml(rawName.length > 52 ? rawName.slice(0, 49) + 'â€¦' : rawName);
      var link = '/trading?tab=backtest&scan_pattern_id=' + encodeURIComponent(p.id);
      html += '<div class="tp-tradeable-card" style="opacity:.95">' +
        '<div style="font-size:10px;font-weight:700;color:var(--text);margin-bottom:4px">' + name + '</div>' +
        '<div style="font-size:9px;color:var(--text-secondary)">' + wr + ' WR Â· ' + trades + ' trades Â· <span style="color:var(--accent)">candidate</span></div>' +
        '<a href="' + link + '" style="display:inline-block;margin-top:6px;font-size:9px;font-weight:600;color:var(--accent);text-decoration:none">Open â†’</a>' +
        '</div>';
    });
    html += '</div>';
    el.innerHTML = html;
  }).catch(function(err) {
    var hint = (err && err.message) ? escHtml(String(err.message)) : 'Network or server error.';
    el.innerHTML = '<span style="color:var(--text-muted)">Failed to load candidates.<br/><span style="font-size:9px;opacity:.85">' + hint + '</span></span>';
  });
}

window._oppTierCaps = { A: null, B: null, C: null, D: null };
window._oppLastPayload = null;
window._oppBoardLastGoodPayload = null;
var OPP_BOARD_FETCH_TIMEOUT_MS = 75000;
var OPP_BOARD_SOFT_REFRESH_MS = 180000; /* 3 min â€” visible tab only */
window._oppBoardInFlight = false;
window._oppBoardFetchAbort = null;

function _schedOppBoardSoftAutoRefresh() {
  if (window._oppBoardSoftRefreshInterval) clearInterval(window._oppBoardSoftRefreshInterval);
  window._oppBoardSoftRefreshInterval = setInterval(function() {
    if (document.visibilityState !== 'visible') return;
    loadOpportunityBoard({ auto: true, silent: true });
  }, OPP_BOARD_SOFT_REFRESH_MS);
}

function hideOppRefreshFailedBanner() {
  var f = document.getElementById('opp-refresh-failed-banner');
  if (f) { f.style.display = 'none'; f.innerHTML = ''; }
}

function showOppRefreshFailedBanner(msg) {
  var f = document.getElementById('opp-refresh-failed-banner');
  if (!f) return;
  f.style.display = 'block';
  f.innerHTML = '<span style="vertical-align:middle">' + escHtml(msg) + '</span>' +
    ' <button type="button" class="bw-ctrl-btn" style="margin-left:8px;vertical-align:middle" onclick="loadOpportunityBoard()">Retry</button>';
}

function updateOppBoardTrustBanners(d) {
  var ban = document.getElementById('opp-stale-banner');
  if (!ban || !d) return;
  var parts = [];
  if (d.is_stale) {
    parts.push('Data age exceeds your stale threshold â€” verify feeds or use Inspect health before acting.');
  }
  if (d.freshness_degraded || !d.data_as_of) {
    parts.push('Freshness is unknown or incomplete â€” do not treat this board as fully current.');
  }
  if (d.board_truncated) {
    parts.push('Board sampled with limits â€” some patternÃ—ticker candidates may be omitted.');
  }
  if (parts.length) {
    ban.style.display = 'block';
    ban.innerHTML = parts.map(function(p) { return '<div style="margin-bottom:4px">' + escHtml(p) + '</div>'; }).join('');
  } else {
    ban.style.display = 'none';
    ban.innerHTML = '';
  }
}

function setOppStatusChips(actionable, watchSoon, watchToday, refreshLine, chipState) {
  var ca = document.getElementById('opp-chip-a');
  var cb = document.getElementById('opp-chip-b');
  var cc = document.getElementById('opp-chip-c');
  var cr = document.getElementById('opp-chip-refresh');
  if (ca) {
    ca.textContent = 'Actionable: ' + actionable;
    ca.classList.toggle('opp-chip-loading', chipState === 'loading');
    ca.classList.toggle('opp-chip-error', chipState === 'error');
  }
  if (cb) {
    cb.textContent = 'Watch soon: ' + watchSoon;
    cb.classList.toggle('opp-chip-loading', chipState === 'loading');
    cb.classList.toggle('opp-chip-error', chipState === 'error');
  }
  if (cc) {
    cc.textContent = 'Watch today: ' + watchToday;
    cc.classList.toggle('opp-chip-loading', chipState === 'loading');
    cc.classList.toggle('opp-chip-error', chipState === 'error');
  }
  if (cr) {
    cr.textContent = refreshLine || '';
    cr.classList.toggle('opp-chip-loading', chipState === 'loading');
    cr.classList.toggle('opp-chip-error', chipState === 'error');
  }
}

function loadInspectHealthCard() {
  var body = document.getElementById('bdd-inspect-health-body');
  if (!body) return;
  body.textContent = 'Loadingâ€¦';
  var ctrl = new AbortController();
  var tid = setTimeout(function() { try { ctrl.abort(); } catch (e0) {} }, 20000);
  fetch('/api/trading/inspect/health', { signal: ctrl.signal }).then(parseFetchJson).then(function(d) {
    clearTimeout(tid);
    if (!d || !d.ok) {
      body.textContent = 'Inspect health unavailable (auth or server).';
      return;
    }
    var parts = [];
    if (d.data_as_of) parts.push('data_as_of: ' + escHtml(String(d.data_as_of)));
    parts.push('scan rows in DB: ' + (d.scan_result_row_count != null ? d.scan_result_row_count : 'â€”'));
    if (d.predictions_cache_meta) {
      var pm = d.predictions_cache_meta;
      parts.push('pred cache count: ' + (pm.cached_result_count != null ? pm.cached_result_count : 'â€”'));
    }
    if (d.degraded && d.degraded.length) {
      parts.push('<span style="color:#b45309">degraded: ' + escHtml(d.degraded.join(', ')) + '</span>');
    }
    if (d.scheduler) parts.push('scheduler running: ' + (d.scheduler.running ? 'yes' : 'no'));
    body.innerHTML = parts.join('<br/>');
  }).catch(function(err) {
    clearTimeout(tid);
    body.textContent = 'Failed: ' + (err && err.message ? err.message : 'unknown');
  });
}

function oppExpandTier(t) {
  var prev = (window._oppLastPayload && window._oppLastPayload.applied_tier_caps) || {};
  var base = prev[t] || (t === 'A' ? 3 : t === 'B' ? 5 : t === 'C' ? 8 : 12);
  var cur = window._oppTierCaps[t];
  window._oppTierCaps[t] = cur ? Math.min(80, cur + Math.max(2, Math.floor(base))) : Math.min(80, base * 2);
  loadOpportunityBoard();
}

function _odhPillClass(eng) {
  if (eng === 'core_repeatable_edge') return 'opp-pill-core';
  return 'opp-pill-ctx';
}

function renderOperatorDeskTrustStrip() {
  var el = document.getElementById('odh-trust-panel');
  if (!el) return;
  var w = window._deskWorkerSnapshot || {};
  var lc = w.last_cycle || {};
  var parts = [];
  parts.push('<div><strong>Worker:</strong> ' + escHtml(w.status || 'unknown') + '</div>');
  if (lc.completed) parts.push('<div><strong>Last reconcile pass (worker):</strong> ' + escHtml(String(lc.completed)) + '</div>');
  var bd = window._oppBoardLastGoodPayload;
  if (bd) {
    parts.push('<div><strong>Opportunity board generated:</strong> ' + escHtml(String(bd.generated_at || 'â€”')) + '</div>');
    parts.push('<div><strong>Board data as-of:</strong> ' + escHtml(String(bd.data_as_of || 'unknown')) + '</div>');
  } else {
    parts.push('<div><strong>Opportunity board:</strong> not loaded yet</div>');
  }
  parts.push('<div><strong>Live predictions:</strong> refresh in section below (on-demand).</div>');
  parts.push('<div><strong>Promoted patterns list:</strong> refresh in Tradeable Patterns (on-demand).</div>');
  el.innerHTML = '<strong>Trust &amp; freshness</strong><br/>' + parts.join('');
}

function renderOperatorDeskHealthBadges(d, top3) {
  var health = 'healthy';
  if (d && (d.is_stale || d.freshness_degraded)) health = 'stale';
  var w = window._deskWorkerSnapshot || {};
  var ws = w.status || 'stopped';
  if (ws !== 'running') health = (health === 'healthy') ? 'partial' : health;
  var hasOpp = top3 && top3.length > 0;
  if (d && !hasOpp && health === 'healthy') health = 'empty';

  var healthEl = document.getElementById('odh-health-badge');
  if (healthEl) {
    healthEl.className = 'odh-badge health-' + health;
    var labels = { healthy: 'Healthy', partial: 'Partial', stale: 'Stale', offline: 'Offline', empty: 'Empty' };
    healthEl.textContent = labels[health] || health;
  }
  var wb = document.getElementById('odh-worker-badge');
  if (wb) {
    wb.textContent = 'Worker: ' + ws;
    wb.className = 'odh-badge' + (ws === 'running' ? ' health-healthy' : '');
  }
}

function renderSpeculativeMoversPanel(spec) {
  var meth = document.getElementById('spec-movers-methodology');
  var body = document.getElementById('spec-movers-body');
  if (!meth || !body) return;
  if (!spec) {
    body.textContent = 'No speculative payload.';
    return;
  }
  meth.textContent = (spec.methodology_note || '') + (spec.engine_version != null
    ? ' Â· Engine v' + escHtml(String(spec.engine_version)) + ' (' + escHtml(String(spec.methodology || '')) + ')'
    : '');
  if (!spec.ok && spec.error) {
    body.innerHTML = '<span style="color:#b45309">' + escHtml(String(spec.error)) + '</span>';
    return;
  }
  var items = spec.items || [];
  if (!items.length) {
    body.innerHTML = '<div style="font-style:italic;color:var(--text-muted)">No explosive-profile scanner rows matched the heuristic gate (scanner history may be quiet).</div>';
    return;
  }
  body.innerHTML = items.map(function(m) {
    var lines = (m.why_not_core_promoted || []).map(function(x) { return '<li>' + escHtml(String(x)) + '</li>'; }).join('');
    var clusterMeta = '';
    if (m.cluster_id || m.cluster_label) {
      clusterMeta = '<div class="sm-sub" style="font-size:9px;color:var(--text-muted)">Cluster: <code style="font-size:9px">' +
        escHtml(String(m.cluster_id || '')) + '</code> Â· ' + escHtml(String(m.cluster_label || m.move_type_label || '')) + '</div>';
    }
    var sigLine = '';
    var an = m.active_nodes || [];
    if (an.length) {
      var sigs = an.slice(0, 4).map(function(n) { return escHtml(String(n.node_id || '').replace(/^nm_sm_/, '')) + ' ' + (n.score != null ? '(' + n.score + ')' : ''); }).join(', ');
      sigLine = '<div class="sm-sub" style="font-size:9px;color:var(--text-secondary)">Signals: ' + sigs + (an.length > 4 ? 'â€¦' : '') + '</div>';
    }
    return '<div class="spec-mover-card"><div class="sm-head">' + escHtml(String(m.ticker || '')) + ' Â· ' + escHtml(String(m.move_type_label || 'Speculative')) + '</div>' +
      clusterMeta + sigLine +
      '<div class="sm-sub">' + escHtml(String(m.why_interesting || '')) + '</div>' +
      '<div class="sm-sub" style="color:#9a3412;font-weight:600">' + escHtml(String(m.operator_hint || '')) + '</div>' +
      '<div class="sm-sub"><strong>Why not promoted to core:</strong><ul style="margin:4px 0 0 16px;padding:0">' + lines + '</ul></div></div>';
  }).join('');
}

function _fetchDeskGovernanceHint() {
  var el = document.getElementById('odh-governance-hint');
  if (!el || el._govLoaded) return;
  el._govLoaded = true;
  fetch('/api/trading/brain/governance').then(parseFetchJson).then(function(g) {
    if (!g || !g.kill_switch) { el.textContent = ''; return; }
    if (g.kill_switch.active) {
      el.innerHTML = '<span style="color:#dc2626;font-weight:700">Kill switch ON</span><br/><span style="font-size:9px">' + escHtml(String(g.kill_switch.reason || '')) + '</span>';
    } else {
      el.textContent = 'Governance: kill switch off.';
    }
  }).catch(function() { el.textContent = ''; });
}

function renderOperatorDeskFromBoard(d) {
  if (!d || d.ok === false) return;
  var T = d.tiers || {};
  var pool = [].concat(T.actionable_now || [], T.watch_soon || [], T.watch_today || []);
  var top3 = pool.slice(0, 3);

  var th = window._brainThesisData || {};
  var thesisLine = document.getElementById('odh-thesis-line');
  if (thesisLine) {
    if (th.thesis) {
      var plain = String(th.thesis).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
      thesisLine.textContent = plain.length > 240 ? plain.slice(0, 237) + 'â€¦' : plain;
    } else {
      thesisLine.textContent = 'Market thesis loads from the brain API â€” if this stays empty, check auth or server logs.';
    }
  }

  var nar = document.getElementById('odh-operator-narrative');
  if (nar) {
    var parts = [];
    var os = d.operator_summary || {};
    if (os.session_line) parts.push(String(os.session_line));
    if (th.stance) parts.push('Stance: ' + th.stance + '.');
    if (d.is_stale) parts.push('Feeds look stale versus your threshold â€” verify before acting.');
    else if (d.data_as_of) parts.push('Composite board freshness is bounded by data-as-of below.');
    if (!parts.length) parts.push('Use Tier Aâ€“C for repeatable edges; explosive movers are isolated in the panel below.');
    nar.textContent = parts.join(' ');
  }

  var fr = document.getElementById('odh-fresh-row');
  if (fr) {
    var bits = [];
    bits.push('Board: ' + (d.generated_at || 'â€”'));
    bits.push('Data as-of: ' + (d.data_as_of || 'unknown'));
    if (d.age_seconds != null) bits.push('Age ~' + Math.round(d.age_seconds) + 's');
    var sf = d.source_freshness || {};
    var sk = Object.keys(sf);
    for (var i = 0; i < Math.min(5, sk.length); i++) {
      var k = sk[i];
      var v = sf[k];
      if (v && typeof v === 'object' && v.timestamp) bits.push(k + ': ' + String(v.timestamp));
    }
    fr.innerHTML = bits.map(function(b) { return '<span>' + escHtml(b) + '</span>'; }).join('');
  }

  var t3 = document.getElementById('odh-top3');
  if (t3) {
    if (!top3.length) {
      t3.innerHTML = '<div class="odh-card"><div class="odh-eng">Core repeatable edge</div><div class="odh-tk">No tiered opportunities</div><div class="odh-act" style="color:var(--text-muted)">Run worker reconcile passes or relax tier caps â€” see board section.</div></div>';
    } else {
      t3.innerHTML = top3.map(function(it) {
        var eng = it.opportunity_engine || 'context_unknown';
        var engLabel = eng === 'core_repeatable_edge' ? 'Core repeatable edge engine' : (eng === 'prediction_context' ? 'Prediction context' : 'Auxiliary context');
        var pill = '<span class="opp-engine-pill ' + _odhPillClass(eng) + '">' + escHtml(String(it.setup_type_badge || 'Setup')) + '</span> ';
        var act = it.next_action_label || 'Watch';
        var pat = it.pattern_name ? escHtml(String(it.pattern_name)) : 'â€”';
        return '<div class="odh-card"><div class="odh-eng">' + pill + escHtml(engLabel) + '</div><div class="odh-tk">' + escHtml(String(it.ticker || '')) + '</div>' +
          '<div style="font-size:10px;color:var(--text-muted);margin-top:4px">' + pat + '</div>' +
          '<div class="odh-act">' + escHtml(String(act)) + '</div></div>';
      }).join('');
    }
  }

  var ev = document.getElementById('odh-evidence-strip');
  if (ev) {
    var c1 = th.thesis ? 'Thesis present â€” bias trades accordingly.' : 'Thesis not loaded yet.';
    var c2 = top3.length && top3[0].why_here ? String(top3[0].why_here).slice(0, 160) : 'No primary opportunity narrative.';
    var c3 = top3.length && top3[0].main_risk ? String(top3[0].main_risk).slice(0, 140) : (d.is_stale ? 'Stale data â€” invalidation is time-based.' : 'Use normal risk limits.');
    var engSrc = top3.length ? (top3[0].opportunity_engine || '') : '';
    var plane = engSrc === 'core_repeatable_edge' ? 'Core repeatable edge engine' : 'Auxiliary / context signal';
    ev.innerHTML = '<div><strong>Confirming context:</strong> ' + escHtml(c1) + '</div>' +
      '<div style="margin-top:4px"><strong>Strongest desk note:</strong> ' + escHtml(c2) + '</div>' +
      '<div style="margin-top:4px"><strong>Primary risk:</strong> ' + escHtml(c3) + '</div>' +
      '<div style="margin-top:4px"><strong>Plane:</strong> ' + escHtml(plane) + (d.is_stale ? ' Â· <span style="color:#b45309">Stale</span>' : '') + '</div>';
  }

  renderOperatorDeskTrustStrip();
  renderOperatorDeskHealthBadges(d, top3);
  renderSpeculativeMoversPanel(d.speculative_movers);
  _fetchDeskGovernanceHint();
}

function loadOpportunityBoardDebug() {
  var pre = document.getElementById('bdd-debug-opp-json');
  if (!pre) return;
  pre.textContent = 'Loadingâ€¦';
  var qs = ['debug=1', 'include_research=1'];
  ['a', 'b', 'c', 'd'].forEach(function(lc) {
    var T = lc.toUpperCase();
    var v = window._oppTierCaps[T];
    if (v) qs.push('max_tier_' + lc + '=' + encodeURIComponent(v));
  });
  fetch('/api/trading/opportunity-board?' + qs.join('&')).then(parseFetchJson).then(function(d) {
    pre.textContent = JSON.stringify(d, null, 2);
  }).catch(function(e) {
    pre.textContent = 'Error: ' + (e && e.message ? e.message : String(e));
  });
}

function renderOpportunityBoardFromPayload(d) {
  if (!d || d.ok === false) return;
  var os = d.operator_summary || {};
  var refShort = (os.last_refresh_utc || d.generated_at || 'â€”').toString();
  if (refShort.length > 24) refShort = refShort.slice(0, 22) + 'â€¦';
  setOppStatusChips(
    os.actionable_count != null ? os.actionable_count : '0',
    os.watch_soon_count != null ? os.watch_soon_count : '0',
    os.watch_today_count != null ? os.watch_today_count : '0',
    'Board: ' + refShort,
    ''
  );

  var sumEl = document.getElementById('opp-op-sum');
  if (sumEl) {
    var parts = [];
    parts.push('<strong>Actionable:</strong> ' + (os.actionable_count != null ? os.actionable_count : '0'));
    parts.push('<strong>Watch soon:</strong> ' + (os.watch_soon_count != null ? os.watch_soon_count : '0'));
    parts.push('<strong>Watch today:</strong> ' + (os.watch_today_count != null ? os.watch_today_count : '0'));
    parts.push('<strong>No-trade now:</strong> ' + (os.no_trade_now ? 'yes' : 'no'));
    parts.push('<strong>Generated (UTC):</strong> ' + escHtml(String(d.generated_at || 'â€”')));
    if (d.data_as_of) parts.push('<strong>Data as of (UTC):</strong> ' + escHtml(String(d.data_as_of)));
    else parts.push('<strong>Data as of (UTC):</strong> <span style="color:#b45309">unknown</span>');
    if (d.age_seconds != null) parts.push('<strong>Data age (s):</strong> ' + escHtml(String(d.age_seconds)));
    else parts.push('<strong>Data age (s):</strong> <span style="color:#b45309">unknown</span>');
    if (d.is_stale) parts.push('<span style="color:#b45309;font-weight:700">STALE</span>');
    if (d.freshness_degraded || !d.data_as_of) parts.push('<span style="color:#b45309;font-weight:700">FRESHNESS DEGRADED</span>');
    if (d.board_truncated) parts.push('<span style="color:#b45309;font-weight:700">SAMPLED / TRUNCATED</span>');
    if (os.session_line) parts.push('<strong>Session:</strong> ' + escHtml(String(os.session_line)));
    parts.push('<button type="button" class="bw-ctrl-btn" style="margin-left:6px" onclick="loadOpportunityBoard()" title="Reload from server">Manual refresh</button>');
    sumEl.innerHTML = parts.join(' <span style="color:var(--border)">|</span> ');
  }

  updateOppBoardTrustBanners(d);

  var nt = document.getElementById('opp-no-trade');
  if (nt) {
    if (os.no_trade_now && d.no_trade_summary_lines && d.no_trade_summary_lines.length) {
      nt.style.display = 'block';
      nt.innerHTML = '<div style="font-weight:700;margin-bottom:4px;color:var(--text)">Why nothing actionable (Tier A)</div><ul style="margin:0;padding-left:18px">' +
        d.no_trade_summary_lines.map(function(line) { return '<li>' + escHtml(String(line)) + '</li>'; }).join('') + '</ul>';
    } else {
      nt.style.display = 'none';
      nt.innerHTML = '';
    }
  }

  function cardHtml(it) {
    var eng = it.opportunity_engine || '';
    var pill = '';
    if (it.setup_type_badge) {
      pill = '<span class="opp-engine-pill ' + _odhPillClass(eng) + '" style="margin-bottom:4px">' + escHtml(String(it.setup_type_badge)) + '</span> ';
    }
    var head = '<div style="font-weight:700;font-size:11px;color:var(--text)">' + pill + escHtml(String(it.ticker || '')) +
      ' <span style="font-size:9px;color:var(--accent);font-weight:700">' + escHtml(String(it.next_action_label || '')) + '</span></div>';
    var sub = [];
    if (it.pattern_name) sub.push(escHtml(String(it.pattern_name)));
    if (it.opportunity_engine) sub.push('engine: ' + escHtml(String(it.opportunity_engine)));
    if (it.source_strength) sub.push('evidence: ' + escHtml(String(it.source_strength)));
    if (it.composite != null) sub.push('comp ' + it.composite);
    if (it.feature_coverage != null) sub.push('cov ' + it.feature_coverage);
    if (it.eta_label) sub.push(escHtml(String(it.eta_label)));
    var predBadge = '';
    if (it.also_in_live_predictions) {
      predBadge = ' <a href="#brain-predictions" style="font-size:9px;color:#818cf8;font-weight:600;text-decoration:none">In live predictions â†“</a>';
    }
    var why = '';
    if (it.why_here) why += '<div style="margin-top:6px;font-size:10px;color:var(--text-secondary);line-height:1.45">' + escHtml(String(it.why_here)) + '</div>';
    if (it.why_not_higher_tier) why += '<div style="margin-top:4px;font-size:9px;color:var(--text-muted)">Tier note: ' + escHtml(String(it.why_not_higher_tier)) + '</div>';
    if (it.main_risk) why += '<div style="margin-top:4px;font-size:9px;color:#b45309">Risk: ' + escHtml(String(it.main_risk)) + '</div>';
    return '<div style="border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:8px;background:var(--bg)">' + head +
      '<div style="font-size:10px;color:var(--text-muted)">' + sub.join(' Â· ') + predBadge + '</div>' + why + '</div>';
  }

  function fillTier(id, items, moreId, hmKey) {
    var el = document.getElementById(id);
    if (!el) return;
    if (!items || !items.length) {
      el.innerHTML = '<div style="font-size:10px;color:var(--text-muted);font-style:italic">None right now.</div>';
    } else {
      el.innerHTML = items.map(cardHtml).join('');
    }
    var moreBtn = document.getElementById(moreId);
    if (moreBtn) moreBtn.style.display = (d.has_more && d.has_more[hmKey]) ? 'inline-block' : 'none';
  }

  var T = d.tiers || {};
  fillTier('opp-tier-a', T.actionable_now, 'opp-more-a', 'A');
  fillTier('opp-tier-b', T.watch_soon, 'opp-more-b', 'B');
  fillTier('opp-tier-c', T.watch_today, 'opp-more-c', 'C');

  var dw = document.getElementById('opp-tier-d-wrap');
  var dd = document.getElementById('opp-tier-d');
  if (dw && dd) {
    var ro = T.research_only || [];
    if (ro.length) {
      dw.style.display = 'block';
      dd.innerHTML = ro.map(cardHtml).join('');
    } else {
      dw.style.display = 'none';
      dd.innerHTML = '';
    }
  }

  renderOperatorDeskFromBoard(d);
}

function _oppHandleBoardFetchFailure(msg, code) {
  var strip = document.getElementById('opp-operator-strip');
  function showBoardErrorFullClear(m, c) {
    hideOppRefreshFailedBanner();
    var ban = document.getElementById('opp-stale-banner');
    if (ban) { ban.style.display = 'none'; ban.innerHTML = ''; }
    setOppStatusChips('â€”', 'â€”', 'â€”', 'Board: error', 'error');
    if (strip) {
      strip.innerHTML = '<div id="opp-op-sum"><span style="color:#b45309">' + escHtml(m) + '</span>' +
        (c ? ' <code style="font-size:9px;color:var(--text-muted)">' + escHtml(c) + '</code>' : '') +
        ' <button type="button" class="bw-ctrl-btn" style="margin-left:8px" onclick="loadOpportunityBoard()">Retry</button></div>';
    }
    ['opp-tier-a', 'opp-tier-b', 'opp-tier-c'].forEach(function(tid) {
      var el = document.getElementById(tid);
      if (el) el.innerHTML = '<div style="font-size:10px;color:var(--text-muted)">Could not load â€” Retry above.</div>';
    });
    var dw = document.getElementById('opp-tier-d-wrap');
    var dd = document.getElementById('opp-tier-d');
    if (dw) dw.style.display = 'none';
    if (dd) dd.innerHTML = '';
    var nt = document.getElementById('opp-no-trade');
    if (nt) { nt.style.display = 'none'; nt.innerHTML = ''; }
  }

  if (window._oppBoardLastGoodPayload) {
    renderOpportunityBoardFromPayload(window._oppBoardLastGoodPayload);
    showOppRefreshFailedBanner('Refresh failed; showing last successful board. ' + msg + (code ? ' (' + code + ')' : ''));
    var os = window._oppBoardLastGoodPayload.operator_summary || {};
    var refShort = (os.last_refresh_utc || window._oppBoardLastGoodPayload.generated_at || 'â€”').toString();
    if (refShort.length > 24) refShort = refShort.slice(0, 22) + 'â€¦';
    setOppStatusChips(
      os.actionable_count != null ? os.actionable_count : '0',
      os.watch_soon_count != null ? os.watch_soon_count : '0',
      os.watch_today_count != null ? os.watch_today_count : '0',
      'Board: refresh failed â€” last OK ' + refShort,
      'error'
    );
  } else {
    showBoardErrorFullClear(msg, code);
  }
}

function loadOpportunityBoard(opts) {
  opts = opts || {};
  if (window._oppBoardInFlight && opts.auto) return;

  if (window._oppBoardFetchAbort) {
    try { window._oppBoardFetchAbort.abort(); } catch (eAbort0) {}
  }
  var ctrl = new AbortController();
  window._oppBoardFetchAbort = ctrl;
  window._oppBoardInFlight = true;

  var qs = [];
  ['a', 'b', 'c', 'd'].forEach(function(lc) {
    var T = lc.toUpperCase();
    var v = window._oppTierCaps[T];
    if (v) qs.push('max_tier_' + lc + '=' + encodeURIComponent(v));
  });
  var qstr = qs.length ? ('?' + qs.join('&')) : '';
  var strip = document.getElementById('opp-operator-strip');

  if (!opts.silent) {
    hideOppRefreshFailedBanner();
    setOppStatusChips('â€¦', 'â€¦', 'â€¦', 'Board: loadingâ€¦', 'loading');
    if (strip) strip.innerHTML = '<span id="opp-op-sum">Loading opportunity boardâ€¦</span>';
  }

  var to = setTimeout(function() { try { ctrl.abort(); } catch (e1) {} }, OPP_BOARD_FETCH_TIMEOUT_MS);

  fetch('/api/trading/opportunity-board' + qstr, { signal: ctrl.signal }).then(parseFetchJson).then(function(d) {
    clearTimeout(to);
    if (window._oppBoardFetchAbort !== ctrl) return;
    window._oppLastPayload = d;
    if (!d || d.ok === false) {
      _oppHandleBoardFetchFailure((d && d.message) ? String(d.message) : 'Board unavailable.', d && d.error ? String(d.error) : 'ok_false');
      return;
    }
    window._oppBoardLastGoodPayload = d;
    hideOppRefreshFailedBanner();
    renderOpportunityBoardFromPayload(d);
  }).catch(function(err) {
    clearTimeout(to);
    if (window._oppBoardFetchAbort !== ctrl) return;
    var msg = 'Failed to load opportunity board.';
    if (err && err.name === 'AbortError') msg = 'Board request timed out â€” server may be busy.';
    else if (err && err.message) msg = err.message;
    _oppHandleBoardFetchFailure(msg, 'fetch_error');
  }).finally(function() {
    if (window._oppBoardFetchAbort === ctrl) window._oppBoardInFlight = false;
  });
}

function loadBrainPredictions() {
  var container = document.getElementById('brain-predictions'); if (!container) return;
  container.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:12px"><div class="brain-pulse"></div> Computing predictions...</div>';
  fetch('/api/trading/brain/predictions').then(parseFetchJson).then(function(d) {
    if (!d.ok || !d.predictions || !d.predictions.length) { container.innerHTML = '<div style="padding:12px;text-align:center;color:var(--text-muted)">No predictions yet. Run a scan first.</div>'; return; }
    window._brainPredictions = d.predictions; _predShowAll = false;
    _renderPredCards(container, d.predictions);
    if (typeof renderOperatorDeskTrustStrip === 'function') renderOperatorDeskTrustStrip();
  }).catch(function(err) {
    var hint = (err && err.message) ? escHtml(String(err.message)) : 'Network or server error.';
    container.innerHTML = '<div style="padding:8px;color:var(--text-muted)">Failed to load predictions.<br/><span style="font-size:10px;opacity:.85">' + hint + '</span></div>';
  });
}

function _renderPredCards(container, preds) {
  var limit = _predShowAll ? preds.length : Math.min(_PRED_INITIAL, preds.length);
  var html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><span style="font-size:11px;color:var(--text-muted)">' + preds.length + ' predictions</span></div><div class="pred-grid">';
  for (var i = 0; i < limit; i++) {
    var p = preds[i]; var dirLabel = p.direction.replace('_',' ');
    var scoreNorm = ((p.score+10)/20)*100; var fillColor = p.score > 0 ? '#22c55e' : p.score < 0 ? '#ef4444' : '#f59e0b';
    var confColor = p.confidence >= 60 ? '#22c55e' : p.confidence >= 30 ? '#f59e0b' : '#6b7280';
    var riskLine = '';
    if (p.suggested_stop != null && p.suggested_target != null) {
      riskLine = '<div style="display:flex;gap:8px;font-size:9px;margin-top:3px;color:var(--text-muted)"><span style="color:#ef4444">SL $' + (p.suggested_stop||0).toFixed(2) + '</span><span style="color:#22c55e">TP $' + (p.suggested_target||0).toFixed(2) + '</span></div>';
    }
    var patChips = '';
    if (p.matched_patterns && p.matched_patterns.length) {
      patChips = '<div style="display:flex;flex-wrap:wrap;gap:3px;margin-top:3px">';
      p.matched_patterns.forEach(function(mp) {
        var info = '';
        if (mp.win_rate != null) info += mp.win_rate + '%';
        if (mp.avg_strength != null) info += (info ? ' ' : '') + Math.round(mp.avg_strength * 100) + '% str';
        if (mp.conditions_met != null && mp.conditions_total != null && mp.conditions_met < mp.conditions_total) info += (info ? ' ' : '') + mp.conditions_met + '/' + mp.conditions_total;
        if (info) info = ' ' + info;
        patChips += '<span style="display:inline-flex;align-items:center;gap:2px;padding:1px 6px;border-radius:8px;font-size:8px;font-weight:600;background:rgba(139,92,246,.12);color:#a78bfa;white-space:nowrap">&#x1F9EC; ' + escHtml(mp.name).substring(0,30) + info + '</span>';
      });
      patChips += '</div>';
    }
    html += '<div class="pred-card" onclick="showPredDetail(' + i + ')">' +
      '<div class="pred-top"><span class="pred-ticker">' + p.ticker.replace('-USD','') + '</span><span class="pred-dir ' + p.direction + '">' + dirLabel + '</span></div>' +
      '<div style="display:flex;align-items:center;gap:6px"><span class="pred-conf" style="color:' + confColor + '">' + p.confidence + '%</span>' +
        '<div class="pred-score-bar" style="flex:1"><div class="pred-score-mid"></div><div class="pred-score-fill" style="width:' + Math.max(2,scoreNorm) + '%;background:' + fillColor + '"></div></div>' +
        '<span style="font-size:10px;font-weight:700;color:' + fillColor + '">' + (p.score > 0 ? '+' : '') + p.score.toFixed(1) + '</span></div>' +
      patChips +
      '<div class="pred-signals">' + p.signals.join(' &middot; ') + '</div>' + riskLine + '</div>';
  }
  html += '</div>';
  if (!_predShowAll && preds.length > _PRED_INITIAL) html += '<div style="text-align:center;margin-top:8px"><button onclick="_predShowAll=true;_renderPredCards(document.getElementById(\'brain-predictions\'),window._brainPredictions)" style="font-size:11px;padding:4px 16px;border-radius:6px;background:rgba(99,102,241,.15);color:#818cf8;border:1px solid rgba(129,140,248,.2);cursor:pointer">Show All</button></div>';
  container.innerHTML = html;
}

function showPredDetail(idx) {
  var p = (window._brainPredictions||[])[idx]; if (!p) return;
  var fillColor = p.score > 0 ? '#22c55e' : p.score < 0 ? '#ef4444' : '#f59e0b';
  var html = '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px"><span style="font-size:20px;font-weight:800">' + p.ticker + '</span><span class="bm-tag" style="background:' + fillColor + '20;color:' + fillColor + '">' + p.direction.replace('_',' ') + '</span></div>';
  html += '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px">' +
    '<div style="padding:8px;background:rgba(139,92,246,.06);border-radius:8px;text-align:center"><div style="font-size:16px;font-weight:800;color:' + fillColor + '">' + (p.score > 0 ? '+' : '') + p.score.toFixed(1) + '</div><div style="font-size:9px;color:var(--text-muted)">Score</div></div>' +
    '<div style="padding:8px;background:rgba(139,92,246,.06);border-radius:8px;text-align:center"><div style="font-size:16px;font-weight:800">' + p.confidence + '%</div><div style="font-size:9px;color:var(--text-muted)">Confidence</div></div>' +
    '<div style="padding:8px;background:rgba(139,92,246,.06);border-radius:8px;text-align:center"><div style="font-size:16px;font-weight:800;color:#06b6d4">' + (p.meta_ml_probability != null ? Math.round(p.meta_ml_probability*100) + '%' : '--') + '</div><div style="font-size:9px;color:var(--text-muted)">Pattern ML</div></div></div>';
  if (p.matched_patterns && p.matched_patterns.length) {
    html += '<h4 style="margin-bottom:6px">Matched Patterns</h4><div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px">';
    p.matched_patterns.forEach(function(mp) {
      var parts = [];
      if (mp.win_rate != null) parts.push(mp.win_rate + '% WR');
      if (mp.conditions_met != null && mp.conditions_total != null && mp.conditions_met < mp.conditions_total) parts.push(mp.conditions_met + '/' + mp.conditions_total + ' conds');
      if (mp.avg_strength != null) parts.push(Math.round(mp.avg_strength * 100) + '% str');
      var sub = parts.length ? '<span style="margin-left:6px;color:var(--text-muted);font-size:10px">' + parts.join(' Â· ') + '</span>' : '';
      html += '<div style="padding:5px 10px;border-radius:8px;background:rgba(139,92,246,.1);border:1px solid rgba(139,92,246,.2);font-size:11px">' +
        '<span style="font-weight:700;color:#a78bfa">&#x1F9EC; ' + escHtml(mp.name) + '</span>' + sub +
      '</div>';
    });
    html += '</div>';
  }
  html += '<h4>Signals</h4><ul style="margin:4px 0;padding-left:18px">';
  (p.signals||[]).forEach(function(s) { html += '<li>' + s + '</li>'; });
  html += '</ul>';
  var footer = '<button onclick="goToTicker(\'' + p.ticker + '\');closeBrainModal()" style="background:linear-gradient(135deg,#8b5cf6,#6d28d9);color:#fff">&#x1F4CA; View in Trading</button>';
  openBrainModal(p.ticker + ' Prediction', html, footer);
}

function _eventIcon(type) {
  var icons = {discovery:'&#x1F4A1;',update:'&#x1F504;',demotion:'&#x1F4C9;',review:'&#x1F4DD;',journal:'&#x270F;',scan:'&#x1F50D;',error:'&#x26A0;',reflection:'&#x1F52C;'};
  return icons[type] || '&#x1F4CC;';
}

function loadBrainActivity() {
  fetch('/api/trading/brain/activity').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return; _brainEventsRaw = d.events || []; renderBrainTimeline(_brainEventsRaw);
  });
}

function renderBrainTimeline(events) {
  var el = document.getElementById('brain-activity');
  if (!events.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted);padding:8px 0">No learning events yet.</div>'; return; }
  var filtered = _brainActiveFilter === 'all' ? events : events.filter(function(e) { return e.event_type === _brainActiveFilter; });
  var groups = {};
  filtered.forEach(function(e, idx) { e._idx = idx; var dk = new Date(e.created_at).toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'}); if (!groups[dk]) groups[dk]=[]; groups[dk].push(e); });
  var html = '';
  Object.keys(groups).forEach(function(dk) {
    html += '<div class="bt-date-group"><div class="bt-date-label">' + dk + '</div>';
    groups[dk].forEach(function(e) {
      var desc = e.description || ''; var short = desc.length > 80 ? escHtml(desc.substring(0,80)) + '...' : escHtml(desc);
      html += '<div class="brain-event" style="cursor:pointer"><div class="be-icon">' + _eventIcon(e.event_type) + '</div><div class="be-body"><div class="be-desc">' + short + '</div><div class="be-time">' + new Date(e.created_at).toLocaleTimeString() + '</div></div></div>';
    });
    html += '</div>';
  });
  el.innerHTML = html;
}

function filterBrainEvents(filter, btn) {
  _brainActiveFilter = filter;
  document.querySelectorAll('.brain-filter-btn').forEach(function(b) { b.classList.remove('active'); });
  if (btn) btn.classList.add('active');
  renderBrainTimeline(_brainEventsRaw);
}

function loadLearnedPatterns() {
  fetch('/api/trading/learn/patterns').then(parseFetchJson).then(function(d) {
    if (!d.ok) return;
    window._brainPatterns = {active: d.active, demoted: d.demoted};
    renderPatterns();
  }).catch(function(){});
}

function togglePatChip(el) {
  el.classList.toggle('on');
  renderPatterns();
}

function _getPatFilters() {
  var sort = document.getElementById('pat-sort').value;
  var search = (document.getElementById('pat-search').value || '').toLowerCase().trim();
  var chips = document.querySelectorAll('#pat-toolbar .pat-chip');
  var signals = [], sectors = [], backtested = false, showDemoted = false, hideVariants = false;
  chips.forEach(function(c) {
    if (!c.classList.contains('on')) return;
    var sec = c.getAttribute('data-sector');
    if (sec) { sectors.push(sec); return; }
    var s = c.getAttribute('data-sig');
    if (s === 'backtested') backtested = true;
    else if (s === 'demoted') showDemoted = true;
    else if (s === 'hideVariants') hideVariants = true;
    else signals.push(s);
  });
  return {sort:sort, search:search, signals:signals, backtested:backtested, showDemoted:showDemoted, hideVariants:hideVariants, sectors:sectors};
}

function renderPatterns() {
  var data = window._brainPatterns;
  if (!data) return;
  var el = document.getElementById('brain-patterns');
  var countEl = document.getElementById('pat-count');

  var all = (data.active || []).map(function(p){ p._demoted = false; return p; });
  var f = _getPatFilters();
  if (f.showDemoted) {
    (data.demoted || []).forEach(function(p){ var c = Object.assign({}, p); c._demoted = true; all.push(c); });
  }
  var totalAll = all.length;

  if (!all.length) {
    el.innerHTML = '<div style="font-size:11px;color:var(--text-muted);padding:12px;text-align:center;background:var(--bg-header);border:1px dashed var(--border);border-radius:8px"><div style="font-size:24px;margin-bottom:6px">&#x1F50D;</div>No patterns discovered yet.<br><strong style="color:#8b5cf6">Click "Deep Study" to start.</strong></div>';
    countEl.textContent = '';
    return;
  }

  if (f.hideVariants) all = all.filter(function(p){ return !p.variant; });
  if (f.search) all = all.filter(function(p){ return (p.pattern||'').toLowerCase().indexOf(f.search) !== -1; });
  if (f.signals.length) all = all.filter(function(p){ return f.signals.indexOf(p.signal_type||'neutral') !== -1; });
  if (f.backtested) all = all.filter(function(p){ return ((p.win_count||0) + (p.loss_count||0)) > 0; });
  if (f.sectors.length) all = all.filter(function(p) {
    var ps = p.sectors || [];
    return f.sectors.some(function(s){ return ps.indexOf(s) !== -1; });
  });

  var sortKey = f.sort;
  all.sort(function(a,b) {
    var d = 0;
    if (sortKey === 'win_rate') {
      var nA = (a.win_count||0)+(a.loss_count||0);
      var nB = (b.win_count||0)+(b.loss_count||0);
      if (nA > 0 && nB === 0) return -1;
      if (nA === 0 && nB > 0) return 1;
      if (nA === 0 && nB === 0) return (b.confidence||0) - (a.confidence||0);
      d = (b.win_rate||0) - (a.win_rate||0);
      if (d === 0) d = nB - nA;
      if (d === 0) d = (b.confidence||0) - (a.confidence||0);
    } else if (sortKey === 'evidence') {
      d = (b.evidence_count||0) - (a.evidence_count||0);
      if (d === 0) d = (b.confidence||0) - (a.confidence||0);
    } else if (sortKey === 'newest') {
      var ca = a.created_at||'', cb = b.created_at||'';
      d = ca < cb ? 1 : ca > cb ? -1 : 0;
    } else if (sortKey === 'recent') {
      var la = a.last_seen||'', lb = b.last_seen||'';
      d = la < lb ? 1 : la > lb ? -1 : 0;
    } else {
      d = (b.confidence||0) - (a.confidence||0);
    }
    return d;
  });

  // Group variants directly after their parent card
  var grouped = [];
  var parentIdx = {};
  all.forEach(function(p, i) {
    if (!p.variant) {
      parentIdx[p.scan_pattern_id] = grouped.length;
      grouped.push(p);
    }
  });
  all.forEach(function(p) {
    if (p.variant) {
      var pSpId = p.parent_scan_pattern_id;
      if (pSpId != null && parentIdx[pSpId] != null) {
        var insertAt = parentIdx[pSpId] + 1;
        while (insertAt < grouped.length && grouped[insertAt].variant && grouped[insertAt].parent_scan_pattern_id === pSpId) {
          insertAt++;
        }
        grouped.splice(insertAt, 0, p);
        Object.keys(parentIdx).forEach(function(k) {
          if (parentIdx[k] >= insertAt) parentIdx[k]++;
        });
      } else {
        grouped.push(p);
      }
    }
  });
  all = grouped;

  countEl.textContent = 'Showing ' + all.length + ' of ' + totalAll + ' patterns';

  var html = '';
  all.forEach(function(p) {
    var sig = p.signal_type || 'neutral';
    var isDemoted = p._demoted;

    var heroValue, heroLabel, heroColor;
    if (sortKey === 'win_rate') {
      if (p.win_rate != null && ((p.win_count||0)+(p.loss_count||0)) > 0) {
        heroValue = p.win_rate + '%';
        heroLabel = 'win rate';
        heroColor = p.win_rate >= 60 ? '#22c55e' : p.win_rate >= 45 ? '#f59e0b' : '#ef4444';
      } else {
        heroValue = 'â€”';
        heroLabel = 'no BT WR yet';
        heroColor = 'var(--text-muted)';
      }
    } else if (sortKey === 'evidence') {
      heroValue = p.evidence_count || 0;
      heroLabel = 'evidence';
      heroColor = '#8b5cf6';
    } else {
      heroValue = p.confidence + '%';
      heroLabel = 'confidence';
      heroColor = p.confidence >= 70 ? '#22c55e' : p.confidence >= 50 ? '#f59e0b' : '#ef4444';
    }

    var wrHtml = '';
    if (p.win_rate != null && ((p.win_count||0)+(p.loss_count||0)) > 0) {
      var wrClass = p.win_rate >= 60 ? 'wr-good' : p.win_rate >= 45 ? 'wr-mid' : 'wr-bad';
      wrHtml = '<span class="pc-pill ' + wrClass + '">' + p.win_rate + '% win</span>' +
               '<span class="pc-pill">' + (p.win_count||0) + 'W / ' + (p.loss_count||0) + 'L</span>';
    } else {
      wrHtml = '<span class="pc-pill" style="opacity:.85;border-style:dashed" title="No saved per-ticker backtests with trades yet">no BT WR</span>';
    }
    if (sortKey !== 'confidence') {
      wrHtml += '<span class="pc-pill" style="opacity:.7">' + p.confidence + '% conf</span>';
    }
    if (p.promotion_status && p.promotion_status !== 'legacy') {
      var ps = String(p.promotion_status).replace(/_/g, ' ');
      wrHtml += '<span class="pc-pill" style="opacity:.85;font-size:9px" title="Promotion gate">' + escHtml(ps) + '</span>';
    }
    if (p.backtest_spread_used != null || p.backtest_commission_used != null) {
      var fr = [];
      if (p.backtest_commission_used != null) fr.push('fee ' + (p.backtest_commission_used * 100).toFixed(2) + '%');
      if (p.backtest_spread_used != null) fr.push('spr ' + (p.backtest_spread_used * 10000).toFixed(1) + 'bp');
      wrHtml += '<span class="pc-pill" style="opacity:.65;font-size:9px" title="Backtest friction assumptions">' + fr.join(' Â· ') + '</span>';
    }

    var retHtml = p.avg_return != null ? '<span class="pc-pill">' + (p.avg_return > 0 ? '+' : '') + p.avg_return + '% avg</span>' : '';

    var btTrades = p.bt_total_trades;
    var hasSimTrades = btTrades != null && btTrades > 0;
    var ddVal = p.bt_worst_max_drawdown;
    var ddStr = 'â€”';
    if (ddVal != null && ddVal !== undefined && hasSimTrades) {
      var ddn = typeof ddVal === 'number' ? ddVal : parseFloat(ddVal);
      if (!isNaN(ddn)) ddStr = ddn.toFixed(1) + '%';
    }
    var oosWrVal = p.oos_win_rate;
    var oosWrStr = (oosWrVal != null && oosWrVal !== undefined) ? (oosWrVal + '%') : 'â€”';
    var oosPillClass = 'pc-pill';
    if (oosWrVal != null && oosWrVal !== undefined) {
      oosPillClass += oosWrVal >= 50 ? ' wr-good' : oosWrVal >= 40 ? ' wr-mid' : ' wr-bad';
    } else {
      oosPillClass += ' pc-pill-muted';
    }
    var oosTitle = 'Pattern-level OOS win rate (held-out bars).';
    if (oosWrVal == null || oosWrVal === undefined) oosTitle += ' Not evaluated yet.';
    if (p.oos_trade_count != null && p.oos_trade_count > 0) oosTitle += ' OOS trades: ' + p.oos_trade_count + '.';
    var tradesStr = hasSimTrades ? String(btTrades) : 'â€”';
    var evidenceStrip = '<div class="pc-evidence-strip">' +
      '<span class="pc-pill' + (hasSimTrades ? '' : ' pc-pill-muted') + '" title="Simulated trades across stored backtests for this insight (one row per ticker/strategy).">' +
        'Trades <b>' + tradesStr + '</b></span>' +
      '<span class="pc-pill' + (ddStr !== 'â€”' ? '' : ' pc-pill-muted') + '" title="Worst max drawdown among those backtests (most negative %).">' +
        'Max DD <b>' + ddStr + '</b></span>' +
      '<span class="' + oosPillClass + '" title="' + escHtml(oosTitle) + '">' +
        'OOS WR <b>' + oosWrStr + '</b></span>' +
      '</div>';

    var benchStrip = '';
    if (p.bench_fold_summary) {
      var bGate = p.bench_passes_gate;
      var bPillClass = 'pc-pill';
      if (bGate === true) bPillClass += ' wr-good';
      else if (bGate === false) bPillClass += ' wr-bad';
      else bPillClass += ' pc-pill-muted';
      var bTip = 'Benchmark walk-forward (config tickers, contiguous windows). Positive folds / total. ';
      if (p.bench_evaluated_at) bTip += 'Evaluated: ' + p.bench_evaluated_at + '. ';
      if (bGate === true) bTip += 'Passes fold-ratio gate.';
      else if (bGate === false) bTip += 'Does not pass fold-ratio gate.';
      benchStrip = '<div class="pc-evidence-strip" style="margin-top:2px">' +
        '<span class="' + bPillClass + '" style="font-size:8px" title="' + escHtml(bTip) + '">Bench <b>' +
        escHtml(p.bench_fold_summary) + '</b></span></div>';
    }

    var tickersHtml = '';
    var sectors = p.sectors || [];
    var tagsHtml = '';
    sectors.forEach(function(sec) {
      if (sec === 'crypto') {
        tagsHtml += '<span class="pc-pill" style="background:rgba(245,158,11,.1);color:#d97706">crypto</span>';
      } else {
        tagsHtml += '<span class="pc-pill" style="background:rgba(59,130,246,.1);color:#3b82f6">' + sec + '</span>';
      }
    });
    var displayTickers = (p.example_tickers && p.example_tickers.length) ? p.example_tickers : (p.bt_tickers || []).slice(0, 4);
    if (displayTickers.length || tagsHtml) {
      tickersHtml = '<div class="pc-tickers">';
      if (tagsHtml) tickersHtml += tagsHtml;
      displayTickers.forEach(function(t){ tickersHtml += '<span class="pc-ticker">' + t + '</span>'; });
      tickersHtml += '</div>';
    }

    var lastSeen = p.last_seen ? timeSince(new Date(p.last_seen)) + ' ago' : '';

    var variantBadge = '';
    var varTypeCls = '';
    if (p.variant) {
      var vo = (p.variant.origin || '').toLowerCase();
      var vbCls = 'vb-exit', vbLabel = 'exit';
      if (vo.indexOf('entry') !== -1) { vbCls = 'vb-entry'; vbLabel = 'entry'; varTypeCls = ' var-entry'; }
      else if (vo.indexOf('combo') !== -1 || vo.indexOf('cross') !== -1) { vbCls = 'vb-combo'; vbLabel = 'combo'; varTypeCls = ' var-combo'; }
      else { varTypeCls = ' var-exit'; }
      variantBadge = '<span class="pc-var-badge ' + vbCls + '">' + vbLabel + '</span>';
      if (p.variant.label) {
        variantBadge += '<span class="pc-signal" style="background:rgba(128,128,128,.08);color:var(--text-muted);font-size:8px">' + escHtml(p.variant.label) + ' g' + (p.variant.generation||0) + '</span>';
      }
    }
    var bestExitBadge = '';
    if (p.best_exit && !p.variant) {
      bestExitBadge = '<span class="pc-signal" style="background:rgba(34,197,94,.12);color:#16a34a;font-size:8px">best: ' + escHtml(p.best_exit) + '</span>';
    }

    var parentCrumb = '';
    if (p.variant && p.variant.parent_name) {
      parentCrumb = '<div class="pc-parent-crumb">&#x2514; ' + escHtml(p.variant.parent_name) + '</div>';
    }

    html += '<div class="pattern-card sig-' + sig + (isDemoted ? ' demoted' : '') + (p.variant ? ' is-variant' + varTypeCls : '') + '" style="cursor:pointer" onclick="showPatternDetail(' + p.id + ')"' +
      (p.parent_scan_pattern_id ? ' data-parent-sp="' + p.parent_scan_pattern_id + '"' : '') +
      (p.scan_pattern_id ? ' data-sp="' + p.scan_pattern_id + '"' : '') +
    '>' +
      '<div class="pc-header">' +
        '<span class="pc-signal ' + sig + '">' + sig + '</span>' +
        (isDemoted ? '<span class="pc-signal demoted-badge">demoted</span>' : '') +
        variantBadge + bestExitBadge +
        (p.ticker_scope && p.ticker_scope !== 'universal' ?
          '<span class="evo-origin-badge" style="background:rgba(234,179,8,.12);color:#eab308;font-size:7px">' +
          _fmtScopeTickersBadge(p) + '</span>' : '') +
        '<span class="pc-conf" style="color:' + heroColor + '">' + heroValue + '<span style="display:block;font-size:8px;font-weight:400;opacity:.6">' + heroLabel + '</span></span>' +
      '</div>' +
      '<div class="pc-desc" title="' + escHtml(p.pattern_display || p.pattern) + '">' + escHtml(p.pattern_display || p.pattern) + '</div>' +
      parentCrumb +
      evidenceStrip +
      benchStrip +
      '<div class="pc-stats">' +
        '<span class="pc-pill">' + p.evidence_count + ' evidence files</span>' +
        wrHtml + retHtml +
      '</div>' +
      tickersHtml +
      '<div class="pc-footer">' +
        (lastSeen ? '<span>Last seen ' + lastSeen + '</span>' : '') +
        (p.scan_pattern_id ? '<button class="bw-boost-btn" onclick="event.stopPropagation();boostPattern(' + p.scan_pattern_id + ',this)" title="Boost to front of backtest queue">&#x26A1; Boost</button>' : '') +
      '</div>' +
    '</div>';
  });
  el.innerHTML = html;
}

function _syncEvPineStrategyUi() {
  /* Pine export is keyed by TradingInsight id â€” server resolves ScanPattern (avoids wrong id=1). */
  var insightId = window._currentEvidencePatternId;
  var fetchBtn = document.getElementById('ev-pine-fetch-btn');
  if (fetchBtn) fetchBtn.disabled = !insightId;
}

function evPineStrategyFetch() {
  var insightId = window._currentEvidencePatternId;
  if (!insightId) return;
  var ta = document.getElementById('ev-pine-text');
  var warnEl = document.getElementById('ev-pine-warnings');
  var copyBtn = document.getElementById('ev-pine-copy-btn');
  if (copyBtn) copyBtn.disabled = true;
  if (ta) { ta.style.display = 'block'; ta.value = 'Loadingâ€¦'; }
  if (warnEl) { warnEl.style.display = 'none'; warnEl.textContent = ''; }
  fetch('/api/trading/learn/patterns/' + encodeURIComponent(insightId) + '/export/pine?kind=strategy').then(parseFetchJson).then(function(d) {
    if (!d.ok) {
      if (ta) ta.value = '';
      if (warnEl) {
        warnEl.style.display = 'block';
        warnEl.textContent = d.error || 'Export failed';
      }
      return;
    }
    if (ta) ta.value = d.pine || '';
    if (copyBtn) copyBtn.disabled = !(d.pine);
    if (warnEl) {
      if (d.warnings && d.warnings.length) {
        warnEl.style.display = 'block';
        warnEl.textContent = d.warnings.join(' ');
      } else {
        warnEl.style.display = 'none';
        warnEl.textContent = '';
      }
    }
  }).catch(function(err) {
    if (ta) ta.value = '';
    if (warnEl) {
      warnEl.style.display = 'block';
      warnEl.textContent = (err && err.message) ? err.message : 'Network error';
    }
  });
}

function evPineStrategyCopy() {
  var ta = document.getElementById('ev-pine-text');
  if (!ta || !ta.value) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(ta.value).catch(function() {
      ta.select();
      try { document.execCommand('copy'); } catch (e) {}
    });
  } else {
    ta.select();
    try { document.execCommand('copy'); } catch (e) {}
  }
}

function showPatternByScanPatternId(spId) {
  var all = ((window._brainPatterns || {}).active || []).concat((window._brainPatterns || {}).demoted || []);
  var p = all.find(function(x){ return x.scan_pattern_id === spId; });
  if (p) {
    showPatternDetail(p.id);
  } else {
    fetch('/api/trading/learn/patterns').then(parseFetchJson).then(function(d) {
      if (!d.ok) return;
      window._brainPatterns = {active: d.active, demoted: d.demoted};
      var refreshed = (d.active || []).concat(d.demoted || []);
      var found = refreshed.find(function(x){ return x.scan_pattern_id === spId; });
      if (found) showPatternDetail(found.id);
    }).catch(function(){});
  }
}

function showPatternDetail(patternId) {
  _disposeEvWrProgressChart();
  var all = ((window._brainPatterns || {}).active || []).concat((window._brainPatterns || {}).demoted || []);
  var p = all.find(function(x){ return x.id === patternId; });
  if (!p) return;
  var barColor = p.confidence >= 70 ? '#22c55e' : p.confidence >= 50 ? '#f59e0b' : '#ef4444';

  var patLine = p.pattern_display || p.pattern;
  var html = '<div id="ev-pattern-desc" style="padding:10px;background:var(--bg);border:1px solid var(--border);border-radius:8px;margin-bottom:10px;font-size:12px;line-height:1.5">' + escHtml(patLine) + '</div>';
  html += '<div id="ev-pine-strategy-row" style="margin-bottom:10px;padding:10px 12px;border-radius:8px;border:1px solid rgba(139,92,246,.35);background:rgba(139,92,246,.06);box-sizing:border-box">' +
    '<div style="font-size:11px;font-weight:700;color:var(--text);margin-bottom:6px">TradingView Pine strategy</div>' +
    '<div style="font-size:9px;color:var(--text-muted);margin-bottom:8px;line-height:1.35">Use <strong>Load</strong> / <strong>Copy</strong> in the <strong>modal title bar</strong> (top row, next to the close button). Exports <strong>ScanPattern</strong> as a Pine <strong>strategy</strong>. Exits are signal on/off only.</div>' +
    '<div id="ev-pine-warnings" style="display:none;margin-top:8px;font-size:9px;color:#f59e0b;line-height:1.45"></div>' +
    '<textarea id="ev-pine-text" readonly spellcheck="false" style="display:none;margin-top:8px;width:100%;box-sizing:border-box;min-height:100px;font-family:ui-monospace,Consolas,monospace;font-size:10px;line-height:1.35;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);resize:vertical"></textarea>' +
    '</div>';
  html += '<div id="ev-stat-cards" style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">' +
    '<div style="padding:6px;background:rgba(139,92,246,.06);border-radius:6px;text-align:center"><div style="font-size:14px;font-weight:700">' + p.evidence_count + '</div><div style="font-size:9px;color:var(--text-muted)">Evidence files</div></div>' +
    '<div style="padding:6px;background:rgba(139,92,246,.06);border-radius:6px;text-align:center"><div style="font-size:14px;font-weight:700" id="ev-wr-val">' + (p.win_rate != null && ((p.win_count||0)+(p.loss_count||0)) > 0 ? '<span style="color:' + (p.win_rate >= 50 ? '#22c55e' : '#ef4444') + '">' + p.win_rate + '%</span>' : '<span style="color:var(--text-muted)">--</span>') + '</div><div style="font-size:9px;color:var(--text-muted)" id="ev-wr-label">Win Rate' + (p.win_rate != null && ((p.win_count||0)+(p.loss_count||0)) > 0 ? ' (' + p.win_count + 'W/' + (p.loss_count||0) + 'L)' : ' <span style="font-size:8px">(saved backtests)</span>') + '</div></div>' +
    '<div style="padding:6px;background:rgba(139,92,246,.06);border-radius:6px;text-align:center"><div style="font-size:14px;font-weight:700;color:' + barColor + '">' + p.confidence + '%</div><div style="font-size:9px;color:var(--text-muted)">Confidence</div></div></div>';
  html += '<div id="ev-computed-row" style="display:none;margin-top:8px"></div>';
  html += '<div id="ev-trade-analytics-row" style="margin-top:10px;padding:8px;border-radius:6px;border:1px solid var(--border);background:var(--card)">' +
    '<div style="font-size:10px;font-weight:600;color:var(--text-muted);margin-bottom:4px">Trade analytics (row-level)</div>' +
    '<div style="font-size:9px;color:var(--text-muted);margin-bottom:8px;line-height:1.35">Uses your linked <strong>ScanPattern</strong> (same id as Boost). Output appears below when you run an action.</div>' +
    '<div style="display:flex;flex-wrap:wrap;gap:6px">' +
    '<button type="button" class="btn btn-sm" style="font-size:10px" onclick="evTradeAnalyticsLoad()">Load analytics</button>' +
    '<button type="button" class="btn btn-sm" style="font-size:10px" onclick="evTradeAnalyticsPropose()">Propose hypotheses</button>' +
    '<button type="button" class="btn btn-sm" style="font-size:10px" onclick="evTradeAnalyticsMl()">Train ML (rows)</button>' +
    '<button type="button" class="btn btn-sm" style="font-size:10px;font-weight:600" onclick="evRerunAllListedBacktests()" title="Re-run each row in the Backtests tab with current rules and each row\'s saved window">Rerun all listed BTs</button>' +
    '</div>' +
    '<pre id="ev-trade-analytics-out" style="display:none;margin-top:8px;max-height:220px;overflow:auto;white-space:pre-wrap;font-size:10px;color:var(--text-muted);border:1px solid var(--border);border-radius:4px;padding:6px;background:var(--bg)"></pre>' +
    '</div>';
  html += '<div id="ev-wr-progress-wrap" style="display:none;margin-top:10px">' +
    '<div style="font-size:10px;color:var(--text-muted);margin-bottom:4px"><strong>Win rate over time</strong> â€” stored backtests for <strong>this insight</strong> (latest per ticker/strategy in the recent window). Aggregate WR is <strong>trade-weighted</strong> (matches per-row win rate columns), not â€œ% of backtests with positive return.â€ Re-runs replace the row for that ticker/strategy. Last point matches the header.</div>' +
    '<div id="ev-wr-progress-chart" style="height:140px;width:100%"></div></div>';
  if (p.example_tickers && p.example_tickers.length) {
    html += '<div style="margin-top:8px;display:flex;gap:4px;flex-wrap:wrap">';
    p.example_tickers.forEach(function(t) { html += '<span class="pc-ticker" onclick="goToTicker(\'' + t + '\');closeBrainModal()" style="cursor:pointer">' + t + '</span>'; });
    html += '</div>';
  }

  html += '<div class="ev-tabs" id="ev-tabs">' +
    '<button class="ev-tab active" data-evtab="timeline" onclick="_switchEvTab(\'timeline\')">Timeline <span class="ev-tab-badge" id="ev-badge-timeline">-</span></button>' +
    '<button class="ev-tab" data-evtab="hypotheses" onclick="_switchEvTab(\'hypotheses\')">Hypotheses <span class="ev-tab-badge" id="ev-badge-hypotheses">-</span></button>' +
    '<button class="ev-tab" data-evtab="trades" onclick="_switchEvTab(\'trades\')">Journal <span class="ev-tab-badge" id="ev-badge-trades">-</span></button>' +
    '<button class="ev-tab" data-evtab="backtests" onclick="_switchEvTab(\'backtests\')">Backtests <span class="ev-tab-badge" id="ev-badge-backtests">-</span></button>' +
    '<button class="ev-tab" data-evtab="evolution" onclick="_switchEvTab(\'evolution\')">Evolution <span class="ev-tab-badge" id="ev-badge-evolution">-</span></button>' +
  '</div>';
  html += '<div class="ev-pane active" id="ev-pane-timeline"><div class="ev-empty" style="padding:16px"><span style="display:inline-block;width:16px;height:16px;border:2px solid #8b5cf6;border-top-color:transparent;border-radius:50%;animation:spin .6s linear infinite"></span> Loading evidence...</div></div>';
  html += '<div class="ev-pane" id="ev-pane-hypotheses"></div>';
  html += '<div class="ev-pane" id="ev-pane-trades"></div>';
  html += '<div class="ev-pane" id="ev-pane-backtests"></div>';
  html += '<div class="ev-pane" id="ev-pane-evolution"><div class="ev-empty" style="padding:16px">Click to load evolution tree.</div></div>';

  var footer = '';
  if (p.scan_pattern_id) {
    footer += '<button onclick="boostPattern(' + p.scan_pattern_id + ',this)" style="background:linear-gradient(135deg,#f97316,#ea580c);color:#fff">&#x26A1; Boost to Queue</button>';
  }
  if (p.active) {
    footer += '<button onclick="demotePattern(' + p.id + ')" style="background:#ef4444;color:#fff">Demote</button>';
  }
  openBrainModal('Pattern Evidence', html, footer, undefined, true);
  window._currentEvidencePatternId = p.id;
  /* ScanPattern id for /api/trading/brain/pattern/{id}/* and backtest deep links â€” never use TradingInsight id here */
  window._currentBacktestPatternId = p.scan_pattern_id || null;
  window._evoTabLoaded = false;
  _syncEvPineStrategyUi();

  fetch('/api/trading/learn/patterns/' + p.id + '/evidence').then(parseFetchJson).then(function(d) {
    if (!d.ok) {
      var tl = document.getElementById('ev-pane-timeline');
      if (tl) tl.innerHTML = '<div class="ev-empty">Could not load evidence.</div>';
      _syncEvPineStrategyUi();
      return;
    }
    if (d.resolved_scan_pattern_id) {
      window._currentBacktestPatternId = d.resolved_scan_pattern_id;
    }
    _syncEvPineStrategyUi();
    var pd = d.pattern_display || (d.insight && d.insight.pattern_display);
    if (pd) {
      var descEl = document.getElementById('ev-pattern-desc');
      if (descEl) descEl.textContent = pd;
    }
    _renderEvTimeline(d.timeline || []);
    _renderEvHypotheses(d.hypotheses || []);
    _renderEvTrades(d.trades || []);
    _renderEvBacktests(d.backtests || []);
    if (d.insight) {
      var wrEl = document.getElementById('ev-wr-val');
      var wrLbl = document.getElementById('ev-wr-label');
      var wc = d.insight.win_count != null ? d.insight.win_count : 0;
      var lc = d.insight.loss_count != null ? d.insight.loss_count : 0;
      var hasBt = (wc + lc) > 0 && d.insight.win_rate != null;
      if (wrEl) {
        if (hasBt) {
          var wrColor = d.insight.win_rate >= 50 ? '#22c55e' : '#ef4444';
          wrEl.innerHTML = '<span style="color:' + wrColor + '">' + d.insight.win_rate + '%</span>';
        } else {
          wrEl.innerHTML = '<span style="color:var(--text-muted)">--</span>';
        }
      }
      if (wrLbl) {
        wrLbl.innerHTML = hasBt
          ? 'Win Rate (' + wc + 'W/' + lc + 'L)'
          : 'Win Rate <span style="font-size:8px">(no saved backtests)</span>';
      }
    }
    var b = function(id,n){ var el=document.getElementById('ev-badge-'+id); if(el) el.textContent=n; };
    b('timeline', (d.timeline||[]).length);
    b('hypotheses', (d.hypotheses||[]).length);
    b('trades', (d.trades||[]).length);
    b('backtests', (d.backtests||[]).length);
    if (d.computed_stats) _renderComputedStats(d.computed_stats);
    _renderEvWrProgress(d.win_rate_progress || []);
  }).catch(function(err) {
    var msg = (err && err.message) ? escHtml(err.message) : 'Network error loading evidence.';
    var el = document.getElementById('ev-pane-timeline');
    if (el) el.innerHTML = '<div class="ev-empty">Could not load evidence: ' + msg + '</div>';
  });
}

function _switchEvTab(tab) {
  document.querySelectorAll('.ev-tab').forEach(function(t){ t.classList.toggle('active', t.getAttribute('data-evtab')===tab); });
  document.querySelectorAll('.ev-pane').forEach(function(p){ p.classList.toggle('active', p.id==='ev-pane-'+tab); });
  if (tab === 'evolution' && !window._evoTabLoaded && window._currentEvidencePatternId) {
    window._evoTabLoaded = true;
    var evoPane = document.getElementById('ev-pane-evolution');
    if (evoPane) evoPane.innerHTML = '<div class="ev-empty" style="padding:16px"><span style="display:inline-block;width:16px;height:16px;border:2px solid #8b5cf6;border-top-color:transparent;border-radius:50%;animation:spin .6s linear infinite"></span> Loading evolution tree...</div>';
    fetch('/api/trading/learn/patterns/' + window._currentEvidencePatternId + '/evolution').then(parseFetchJson).then(function(d) {
      if (d.ok) {
        _renderEvEvolution(d.root, d.current_scan_pattern_id);
        var badge = document.getElementById('ev-badge-evolution');
        if (badge) {
          if (d.root && d.root.children && d.root.children.length > 0) {
            var countNodes = function(n) { var c = 1; (n.children||[]).forEach(function(ch){ c += countNodes(ch); }); return c; };
            badge.textContent = countNodes(d.root);
          } else {
            badge.textContent = '0';
          }
        }
      } else {
        _renderEvEvolution(null, null);
        var badge = document.getElementById('ev-badge-evolution');
        if (badge) badge.textContent = '0';
      }
    }).catch(function(){
      _renderEvEvolution(null, null);
      var badge = document.getElementById('ev-badge-evolution');
      if (badge) badge.textContent = '0';
    });
  }
}

function _renderComputedStats(cs) {
  var row = document.getElementById('ev-computed-row');
  if (!row) return;
  var cards = [];

  if (cs.backtest_avg_win_rate != null) {
    var bwrColor = cs.backtest_avg_win_rate >= 50 ? '#22c55e' : '#ef4444';
    var runLabel = (cs.backtest_count || 0) + ' deduped runs w/ trades';
    if (cs.backtest_total_displayed != null) {
      runLabel += ' Â· ' + (cs.backtest_total_displayed || 0) + ' rows listed';
    }
    if (cs.backtest_simulated_trades != null && cs.backtest_simulated_trades > 0) {
      runLabel += ' Â· ' + cs.backtest_simulated_trades + ' sim trades (W/L in header)';
    }
    cards.push('<div style="padding:5px 8px;background:rgba(59,130,246,.06);border:1px solid rgba(59,130,246,.12);border-radius:6px;text-align:center;flex:1">' +
      '<div style="font-size:13px;font-weight:700;color:' + bwrColor + '">' + cs.backtest_avg_win_rate + '%</div>' +
      '<div style="font-size:8px;color:var(--text-muted)">Backtest WR (' + runLabel + ')</div></div>');
  }
  if (cs.backtest_avg_return != null) {
    var brColor = cs.backtest_avg_return >= 0 ? '#22c55e' : '#ef4444';
    cards.push('<div style="padding:5px 8px;background:rgba(59,130,246,.06);border:1px solid rgba(59,130,246,.12);border-radius:6px;text-align:center;flex:1">' +
      '<div style="font-size:13px;font-weight:700;color:' + brColor + '">' + cs.backtest_avg_return + '%</div>' +
      '<div style="font-size:8px;color:var(--text-muted)">Avg Backtest Return</div></div>');
  }
  if (cs.trade_win_rate != null) {
    var twrColor = cs.trade_win_rate >= 50 ? '#22c55e' : '#ef4444';
    cards.push('<div style="padding:5px 8px;background:rgba(34,197,94,.06);border:1px solid rgba(34,197,94,.12);border-radius:6px;text-align:center;flex:1">' +
      '<div style="font-size:13px;font-weight:700;color:' + twrColor + '">' + cs.trade_win_rate + '%</div>' +
      '<div style="font-size:8px;color:var(--text-muted)">Real Trade WR (' + (cs.trade_count||0) + ' trades)</div></div>');
  }
  if (cs.hypothesis_confirm_rate != null) {
    var hcColor = cs.hypothesis_confirm_rate >= 60 ? '#22c55e' : cs.hypothesis_confirm_rate >= 40 ? '#f59e0b' : '#ef4444';
    cards.push('<div style="padding:5px 8px;background:rgba(139,92,246,.06);border:1px solid rgba(139,92,246,.12);border-radius:6px;text-align:center;flex:1">' +
      '<div style="font-size:13px;font-weight:700;color:' + hcColor + '">' + cs.hypothesis_confirm_rate + '%</div>' +
      '<div style="font-size:8px;color:var(--text-muted)">Hypothesis Confirm (' + (cs.hypotheses_tested||0) + ')</div></div>');
  }
  if (cs.confirmations != null || cs.challenges != null) {
    cards.push('<div style="padding:5px 8px;background:rgba(139,92,246,.06);border:1px solid rgba(139,92,246,.12);border-radius:6px;text-align:center;flex:1">' +
      '<div style="font-size:13px;font-weight:700"><span style="color:#22c55e">' + (cs.confirmations||0) + '</span> / <span style="color:#ef4444">' + (cs.challenges||0) + '</span></div>' +
      '<div style="font-size:8px;color:var(--text-muted)">Validated / Challenged</div></div>');
  }

  if (cards.length) {
    row.innerHTML = '<div style="font-size:9px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px">Live Evidence Stats</div>' +
      '<div style="display:flex;gap:6px;flex-wrap:wrap">' + cards.join('') + '</div>';
    row.style.display = 'block';
  }
}

function _renderEvTimeline(events) {
  var el = document.getElementById('ev-pane-timeline');
  if (!events.length) { el.innerHTML = '<div class="ev-empty">No learning events recorded yet for this pattern.</div>'; return; }
  var html = '';
  events.forEach(function(e) {
    var dotClass = e.event_type || 'update';
    var confHtml = '';
    if (e.confidence_before != null && e.confidence_after != null) {
      confHtml = '<span class="ev-tl-conf"><span class="before">' + (e.confidence_before*100).toFixed(0) + '%</span> &rarr; <span class="after">' + (e.confidence_after*100).toFixed(0) + '%</span></span>';
    }
    var dateStr = e.created_at ? new Date(e.created_at).toLocaleString() : '';
    html += '<div class="ev-tl-item">' +
      '<div class="ev-tl-dot ' + dotClass + '"></div>' +
      '<div class="ev-tl-body">' +
        '<div class="ev-tl-desc">' + escHtml(e.description) + '</div>' +
        '<div class="ev-tl-meta"><span>' + dateStr + '</span>' + confHtml +
          '<span style="color:var(--text-muted);font-style:italic">' + escHtml(e.event_type||'') + '</span>' +
        '</div>' +
      '</div></div>';
  });
  el.innerHTML = html;
}

function _renderEvHypotheses(hyps) {
  var el = document.getElementById('ev-pane-hypotheses');
  if (!hyps.length) { el.innerHTML = '<div class="ev-empty">No related hypotheses found.</div>'; return; }
  var html = '';
  hyps.forEach(function(h) {
    var statusClass = h.status || 'pending';
    var confirmColor = h.confirm_rate >= 70 ? '#22c55e' : h.confirm_rate >= 40 ? '#f59e0b' : '#ef4444';
    html += '<div class="ev-hyp-card">' +
      '<div class="ev-hyp-header">' +
        '<span class="ev-hyp-status ' + statusClass + '">' + statusClass + '</span>' +
        (h.origin ? '<span style="font-size:9px;color:var(--text-muted)">' + h.origin + '</span>' : '') +
        (h.last_tested_at ? '<span style="font-size:9px;color:var(--text-muted);margin-left:auto">' + new Date(h.last_tested_at).toLocaleDateString() + '</span>' : '') +
      '</div>' +
      '<div class="ev-hyp-desc">' + escHtml(h.description) + '</div>' +
      '<div class="ev-hyp-stats">' +
        '<div class="ev-hyp-stat"><b>' + h.times_tested + '</b>Tested</div>' +
        '<div class="ev-hyp-stat"><b style="color:#22c55e">' + h.times_confirmed + '</b>Confirmed</div>' +
        '<div class="ev-hyp-stat"><b style="color:#ef4444">' + h.times_rejected + '</b>Rejected</div>' +
      '</div>' +
      '<div class="ev-confirm-bar"><div class="ev-confirm-fill" style="width:' + h.confirm_rate + '%;background:' + confirmColor + '"></div></div>' +
      '<div style="text-align:center;font-size:9px;color:var(--text-muted);margin-top:2px">' + h.confirm_rate + '% confirmation rate</div>';

    if (h.last_result) {
      var lr = h.last_result;
      html += '<div class="ev-hyp-ab">' +
        '<div class="ev-hyp-ab-col col-a"><div class="ab-label">Group A</div>' +
          '<div>Avg: <b>' + (lr.group_a_avg != null ? lr.group_a_avg.toFixed(2) + '%' : '--') + '</b></div>' +
          '<div>WR: <b>' + (lr.group_a_wr != null ? lr.group_a_wr.toFixed(0) + '%' : '--') + '</b></div>' +
          '<div>N: ' + (lr.group_a_n || 0) + '</div>' +
        '</div>' +
        '<div class="ev-hyp-ab-col col-b"><div class="ab-label">Group B</div>' +
          '<div>Avg: <b>' + (lr.group_b_avg != null ? lr.group_b_avg.toFixed(2) + '%' : '--') + '</b></div>' +
          '<div>WR: <b>' + (lr.group_b_wr != null ? lr.group_b_wr.toFixed(0) + '%' : '--') + '</b></div>' +
          '<div>N: ' + (lr.group_b_n || 0) + '</div>' +
        '</div></div>';
    }

    if (h.condition_a || h.condition_b) {
      html += '<div class="ev-hyp-conds">A: <code>' + escHtml(h.condition_a||'') + '</code> &nbsp; B: <code>' + escHtml(h.condition_b||'') + '</code></div>';
    }
    html += '</div>';
  });
  el.innerHTML = html;
}

function _renderEvTrades(trades) {
  var el = document.getElementById('ev-pane-trades');
  if (!trades.length) { el.innerHTML = '<div class="ev-empty">No journal trades tagged for this pattern (simulated backtests are under Backtests).</div>'; return; }
  var html = '<table class="ev-table"><thead><tr><th>Ticker</th><th>Dir</th><th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Status</th><th>Date</th></tr></thead><tbody>';
  trades.forEach(function(t) {
    var pnlClass = t.pnl > 0 ? 'ev-pnl-pos' : (t.pnl < 0 ? 'ev-pnl-neg' : '');
    var pnlStr = t.pnl != null ? '$' + t.pnl.toFixed(2) : '--';
    var statusClass = t.pnl > 0 ? 'win' : (t.pnl < 0 ? 'loss' : 'open');
    var dateStr = t.entry_date ? new Date(t.entry_date).toLocaleDateString() : '--';
    html += '<tr>' +
      '<td><b>' + escHtml(t.ticker) + '</b></td>' +
      '<td>' + (t.direction||'--') + '</td>' +
      '<td>' + (t.entry_price != null ? '$' + t.entry_price.toFixed(2) : '--') + '</td>' +
      '<td>' + (t.exit_price != null ? '$' + t.exit_price.toFixed(2) : '--') + '</td>' +
      '<td class="' + pnlClass + '">' + pnlStr + '</td>' +
      '<td><span class="ev-status-badge ' + statusClass + '">' + (t.status||'--') + '</span></td>' +
      '<td>' + dateStr + '</td>' +
    '</tr>';
  });
  html += '</tbody></table>';
  el.innerHTML = html;
}

/** DB may store win_rate as fraction [0,1] or API may send percent; normalize for display. */
function backtestWinRateToPct(wr) {
  if (wr == null || (typeof wr === 'number' && isNaN(wr))) return null;
  var n = Number(wr);
  if (n <= 1 && n >= 0) return n * 100;
  return n;
}

/** Period + interval for /patterns/{id}/backtest (never pass full period_display as period). */
function _btPeriodIntervalForApi(b) {
  var period = '1y', interval = '1d';
  var hasP = false, hasI = false;
  try {
    var p = b.params ? (typeof b.params === 'string' ? JSON.parse(b.params) : b.params) : {};
    if (p && p.period) { period = String(p.period); hasP = true; }
    if (p && p.interval) { interval = String(p.interval); hasI = true; }
  } catch (e) {}
  var disp = (b.period_display && b.period_display !== '--') ? String(b.period_display) : '';
  if (disp && disp.indexOf(' Â· ') >= 0 && (!hasP || !hasI)) {
    var parts = disp.split(' Â· ');
    var seg0 = parts[0] ? parts[0].trim() : '';
    var seg1 = parts[1] ? parts[1].trim() : '';
    if (!hasP && seg0 && !/bar/i.test(seg0) && seg0.length <= 12) period = seg0;
    if (!hasI && seg1 && !/bar/i.test(seg1) && !/[â€“-].*[â€“-]/.test(seg1) && seg1.length <= 8) interval = seg1;
  }
  return { period: period, interval: interval };
}

function _renderEvBacktests(bts) {
  var el = document.getElementById('ev-pane-backtests');
  if (!bts.length) { el.innerHTML = '<div class="ev-empty">No backtest results found for this pattern.</div>'; return; }
  var html = '<table class="ev-table"><thead><tr><th>Ticker</th><th>Strategy</th><th>Return</th><th>Win Rate</th><th>Sharpe</th><th>Max DD</th><th>Trades</th><th>Period</th><th>Ran</th></tr></thead><tbody>';
  bts.forEach(function(b, idx) {
    var retClass = b.return_pct > 0 ? 'ev-pnl-pos' : (b.return_pct < 0 ? 'ev-pnl-neg' : '');
    var noTrades = (b.trade_count || 0) === 0;
    var rowStyle = noTrades ? ' style="opacity:.5"' : '';
    var dateStr = b.ran_at ? new Date(b.ran_at).toLocaleDateString() : '--';
    var periodStr = '--';
    var strategyId = '';
    try {
      if (b.period_display && b.period_display !== '--') {
        periodStr = b.period_display;
      } else if (b.params) {
        var p = typeof b.params === 'string' ? JSON.parse(b.params) : b.params;
        if (p && p.period) periodStr = p.period;
        if (p && p.strategy_id) strategyId = p.strategy_id;
      }
    } catch(e) {}
    var win = _btPeriodIntervalForApi(b);
    var btIdVal = (b.id != null && b.id !== '') ? b.id : null;
    var btIdArg = btIdVal != null ? btIdVal : 'null';
    var clickAttr = noTrades ? rowStyle : ' class="bt-clickable" onclick="toggleBtChart(this,' + idx + ',\'' + escHtml(b.ticker) + '\',\'' + escHtml(strategyId || b.strategy_name || '') + '\',\'' + escHtml(win.period) + '\',\'' + escHtml(win.interval) + '\',' + btIdArg + ')" title="Click to view chart"';
    html += '<tr' + (noTrades ? rowStyle : '') + (noTrades ? '' : clickAttr) + '>' +
      '<td><b>' + escHtml(b.ticker) + '</b></td>' +
      '<td>' + escHtml(b.strategy_name||'--') + '</td>' +
      '<td class="' + retClass + '">' + (noTrades ? '<span style="color:var(--text-muted);font-size:10px">no signal</span>' : (b.return_pct != null ? b.return_pct.toFixed(1) + '%' : '--')) + '</td>' +
      '<td>' + (noTrades ? '--' : (function(){ var wrp = backtestWinRateToPct(b.win_rate); return wrp != null ? wrp.toFixed(0) + '%' : '--'; })()) + '</td>' +
      '<td>' + (noTrades ? '--' : (b.sharpe != null ? b.sharpe.toFixed(2) : '--')) + '</td>' +
      '<td>' + (noTrades ? '--' : (b.max_drawdown != null ? b.max_drawdown.toFixed(1) + '%' : '--')) + '</td>' +
      '<td>' + (b.trade_count||0) + '</td>' +
      '<td>' + periodStr + '</td>' +
      '<td>' + dateStr + '</td>' +
    '</tr>' +
    '<tr class="bt-chart-row" id="bt-chart-row-' + idx + '" style="display:none"><td colspan="9"></td></tr>';
  });
  html += '</tbody></table>';
  el.innerHTML = html;
}

var _btChartInstances = {};
/** Evidence mini-chart + table: requested period/interval, actual bar count, OHLC date span (UTC). */
function _fmtBtEvidenceWindow(d) {
  if (!d) return '';
  var parts = [];
  if (d.period) parts.push(String(d.period));
  if (d.interval) parts.push(String(d.interval));
  if (d.ohlc_bars != null && d.ohlc_bars !== '') parts.push(d.ohlc_bars + ' bars');
  if (d.chart_time_from && d.chart_time_to) {
    var a = new Date(d.chart_time_from * 1000);
    var b = new Date(d.chart_time_to * 1000);
    var opt = { month: 'short', year: 'numeric' };
    parts.push(a.toLocaleDateString(undefined, opt) + ' â€“ ' + b.toLocaleDateString(undefined, opt));
  }
  return parts.length ? parts.join(' Â· ') : '';
}

function toggleBtChart(row, idx, ticker, strategy, btPeriod, btInterval, btId) {
  var chartRow = document.getElementById('bt-chart-row-' + idx);
  if (!chartRow) return;
  var isOpen = chartRow.style.display !== 'none';
  if (isOpen) { chartRow.style.display = 'none'; return; }
  chartRow.style.display = '';
  var cell = chartRow.querySelector('td');
  if (cell.querySelector('.bt-chart-wrap')) return;

  var deepLink = (strategy === 'dynamic_pattern' && window._currentBacktestPatternId)
    ? '/trading?tab=backtest&ticker=' + encodeURIComponent(ticker) + '&pattern_id=' + window._currentBacktestPatternId + '&period=' + encodeURIComponent(btPeriod || '1y') + '&interval=' + encodeURIComponent(btInterval || '1d')
    : '/trading?tab=backtest&ticker=' + encodeURIComponent(ticker) + '&strategy=' + encodeURIComponent(strategy) + '&period=' + encodeURIComponent(btPeriod || '1y');
  var loadMsg = btId ? 'Re-running backtest (aligned period/interval)â€¦' : 'Running backtest for ' + ticker + '...';
  cell.innerHTML = '<div class="bt-chart-wrap">' +
    '<div style="display:flex;align-items:center;gap:8px;padding:4px 0 6px"><div class="brain-pulse"></div><span style="font-size:10px;color:var(--text-muted)">' + loadMsg + '</span></div>' +
    '<div class="bt-chart-container" id="bt-chart-' + idx + '"></div>' +
    '<div class="bt-chart-toolbar">' +
      '<div class="bt-chart-window" id="bt-chart-window-' + idx + '" style="font-size:10px;color:var(--text-muted);line-height:1.35;padding:0 0 4px"></div>' +
      '<div class="bt-chart-stats" id="bt-chart-stats-' + idx + '"></div>' +
      '<a class="bt-chart-link" href="' + deepLink + '" target="_blank">Open Full Chart &#x2197;</a>' +
    '</div></div>';

  function patchBacktestEvidenceRow(headerRow, res) {
    if (!headerRow || !headerRow.querySelector('td')) return;
    var tc = res.trade_count || 0;
    var noTrades = tc === 0;
    var ret = res.return_pct;
    var retClass = ret > 0 ? 'ev-pnl-pos' : (ret < 0 ? 'ev-pnl-neg' : '');
    var cells = headerRow.querySelectorAll('td');
    if (cells[2]) {
      cells[2].className = retClass;
      cells[2].innerHTML = noTrades ? '<span style="color:var(--text-muted);font-size:10px">no signal</span>' : (ret != null ? ret.toFixed(1) + '%' : '--');
    }
    if (cells[3]) {
      var _wrp = backtestWinRateToPct(res.win_rate);
      cells[3].textContent = noTrades ? '--' : (_wrp != null ? _wrp.toFixed(0) + '%' : '--');
    }
    if (cells[4]) cells[4].textContent = noTrades ? '--' : (res.sharpe != null ? res.sharpe.toFixed(2) : '--');
    if (cells[5]) cells[5].textContent = noTrades ? '--' : (res.max_drawdown != null ? res.max_drawdown.toFixed(1) + '%' : '--');
    if (cells[6]) cells[6].textContent = tc;
    if (cells[7]) {
      if (res.period_display && res.period_display !== '--') {
        cells[7].textContent = res.period_display;
        cells[7].title = 'Backtest window: requested period, interval, bar count, OHLC date span (UTC).';
      } else {
        var pw = _fmtBtEvidenceWindow(res);
        cells[7].textContent = pw || (res.period != null ? String(res.period) : '--');
        cells[7].title = pw ? ('Backtest window (requested period + actual OHLC bars): ' + pw) : '';
      }
    }
    if (cells[8]) cells[8].textContent = res.ran_at ? new Date(res.ran_at).toLocaleDateString() : '--';
  }

  function onChartData(d, opts) {
    opts = opts || {};
    if (!d.ok) { cell.querySelector('.bt-chart-wrap').innerHTML = '<div style="padding:12px;color:var(--text-muted);font-size:11px">Backtest failed: ' + escHtml(d.error||'unknown') + '</div>'; return; }
    _drawBtMiniChart(idx, d);
    /* POST /rerun already persists + returns stats; skip duplicate /refresh. Legacy POST pattern/backtest still uses /refresh to sync DB. */
    if (opts.skipRefresh) {
      patchBacktestEvidenceRow(chartRow.previousElementSibling, d);
      return;
    }
    if (btId) {
      fetch('/api/trading/learn/backtest/' + btId + '/refresh', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          return_pct: d.return_pct, win_rate: d.win_rate, sharpe: d.sharpe,
          max_drawdown: d.max_drawdown, trade_count: d.trade_count || 0,
          equity_curve: d.equity_curve || [],
          period: d.period, interval: d.interval,
          ohlc_bars: d.ohlc_bars, chart_time_from: d.chart_time_from, chart_time_to: d.chart_time_to,
          strategy_id: d.strategy_id,
        }),
      }).then(parseFetchJson).then(function(refreshRes) {
        if (refreshRes.ok) patchBacktestEvidenceRow(chartRow.previousElementSibling, refreshRes);
      }).catch(function(){});
    }
  }
  function onChartError(e) {
    cell.querySelector('.bt-chart-wrap').innerHTML = '<div style="padding:12px;color:var(--text-muted);font-size:11px">Error: ' + escHtml(e.message) + '</div>';
  }

  /* 404 "Backtest not found": stale BacktestResult id in this table â€” reload Pattern Evidence or hard-refresh Brain. */
  if (btId) {
    fetch('/api/trading/learn/backtest/' + btId + '/rerun', { method: 'POST' })
      .then(parseFetchJson)
      .then(function(d){ onChartData(d, { skipRefresh: true }); })
      .catch(onChartError);
  } else {
    runFreshBacktestOnly();
  }

  function runFreshBacktestOnly() {
    if (cell.querySelector('.bt-chart-wrap')) {
      var loaderSpan = cell.querySelector('.bt-chart-wrap span');
      if (loaderSpan) loaderSpan.textContent = 'Running backtest for ' + escHtml(ticker) + '...';
    }
    var fetchPromise;
    if (window._currentBacktestPatternId) {
      fetchPromise = fetch(
        '/api/trading/patterns/' + window._currentBacktestPatternId + '/backtest?ticker=' + encodeURIComponent(ticker)
        + '&period=' + encodeURIComponent(btPeriod || '1y') + '&interval=' + encodeURIComponent(btInterval || '1d'),
        { method: 'POST' }
      );
    } else {
      fetchPromise = fetch('/api/trading/backtest', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ticker: ticker, strategy: strategy || 'sma_cross', period: btPeriod || '1y', interval: btInterval || '1d', cash: 10000})
      });
    }
    fetchPromise.then(parseFetchJson).then(function(d){ onChartData(d, {}); }).catch(onChartError);
  }
}

function _drawBtMiniChart(idx, data) {
  var el = document.getElementById('bt-chart-' + idx);
  if (!el) return;
  el.innerHTML = '';

  var isDark = document.documentElement.getAttribute('data-theme') === 'dark' || window.matchMedia('(prefers-color-scheme: dark)').matches;
  var bgColor = isDark ? '#1a1a2e' : '#ffffff';
  var textColor = isDark ? '#a0a0b0' : '#666';
  var gridColor = isDark ? 'rgba(255,255,255,.05)' : 'rgba(0,0,0,.05)';

  var chart = LightweightCharts.createChart(el, {
    width: el.clientWidth,
    height: 280,
    layout: { background: { type: 'solid', color: bgColor }, textColor: textColor, fontSize: 10 },
    grid: { vertLines: { color: gridColor }, horzLines: { color: gridColor } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: gridColor },
    timeScale: { borderColor: gridColor, timeVisible: true },
  });

  var winEl = document.getElementById('bt-chart-window-' + idx);
  if (winEl) {
    var wtxt = _fmtBtEvidenceWindow(data);
    winEl.textContent = wtxt ? ('Window: ' + wtxt) : '';
    winEl.title = wtxt ? ('Matches candle data below. Requested period vs bars returned by market data.') : '';
  }

  if (data.ohlc && data.ohlc.length) {
    var candleSeries = chart.addCandlestickSeries({
      upColor: '#22c55e', downColor: '#ef4444',
      borderUpColor: '#22c55e', borderDownColor: '#ef4444',
      wickUpColor: '#22c55e88', wickDownColor: '#ef444488',
    });
    candleSeries.setData(data.ohlc);

    if (data.trades && data.trades.length) {
      var markers = [];
      data.trades.forEach(function(t) {
        if (t.entry_time) markers.push({ time: t.entry_time, position: 'belowBar', color: '#22c55e', shape: 'arrowUp', text: 'BUY' });
        if (t.exit_time) markers.push({ time: t.exit_time, position: 'aboveBar', color: t.pnl >= 0 ? '#22c55e' : '#ef4444', shape: 'arrowDown', text: (t.return_pct != null ? (t.return_pct > 0 ? '+' : '') + t.return_pct.toFixed(1) + '%' : 'SELL') });
      });
      markers.sort(function(a,b){ return a.time - b.time; });
      candleSeries.setMarkers(markers);
    }
  } else if (data.equity_curve && data.equity_curve.length) {
    var areaSeries = chart.addAreaSeries({
      lineColor: '#818cf8', topColor: 'rgba(129,140,248,.3)', bottomColor: 'rgba(129,140,248,.02)', lineWidth: 2,
    });
    areaSeries.setData(data.equity_curve);
  } else if (!(data.ohlc && data.ohlc.length)) {
    el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:11px">Chart data not stored for this run</div>';
    if (_btChartInstances[idx]) delete _btChartInstances[idx];
    var statsEl = document.getElementById('bt-chart-stats-' + idx);
    if (statsEl) {
      var retColor = (data.return_pct||0) > 0 ? '#22c55e' : (data.return_pct||0) < 0 ? '#ef4444' : 'var(--text-muted)';
      statsEl.innerHTML =
        '<span>Return: <b style="color:' + retColor + '">' + (data.return_pct != null ? (data.return_pct > 0 ? '+' : '') + data.return_pct.toFixed(1) + '%' : '--') + '</b></span>' +
        '<span>Trades: <b>' + (data.trade_count||0) + '</b></span>' +
        '<span>Win Rate: <b>' + (function(){ var w = backtestWinRateToPct(data.win_rate); return w != null ? w.toFixed(0) + '%' : '--'; })() + '</b></span>' +
        '<span>Sharpe: <b>' + (data.sharpe != null ? data.sharpe.toFixed(2) : '--') + '</b></span>';
    }
    var wrap = el.closest('.bt-chart-wrap');
    if (wrap) { var loader = wrap.querySelector('.brain-pulse'); if (loader) loader.parentElement.style.display = 'none'; }
    return;
  }

  chart.timeScale().fitContent();
  _btChartInstances[idx] = chart;

  var statsEl = document.getElementById('bt-chart-stats-' + idx);
  if (statsEl) {
    var retColor = (data.return_pct||0) > 0 ? '#22c55e' : (data.return_pct||0) < 0 ? '#ef4444' : 'var(--text-muted)';
    statsEl.innerHTML =
      '<span>Return: <b style="color:' + retColor + '">' + (data.return_pct != null ? (data.return_pct > 0 ? '+' : '') + data.return_pct.toFixed(1) + '%' : '--') + '</b></span>' +
      '<span>Trades: <b>' + (data.trade_count||0) + '</b></span>' +
      '<span>Win Rate: <b>' + (function(){ var w = backtestWinRateToPct(data.win_rate); return w != null ? w.toFixed(0) + '%' : '--'; })() + '</b></span>' +
      '<span>Sharpe: <b>' + (data.sharpe != null ? data.sharpe.toFixed(2) : '--') + '</b></span>';
  }

  var wrap = el.closest('.bt-chart-wrap');
  if (wrap) { var loader = wrap.querySelector('.brain-pulse'); if (loader) loader.parentElement.style.display = 'none'; }

  new ResizeObserver(function() { chart.applyOptions({ width: el.clientWidth }); }).observe(el);
}

function _renderEvEvolution(root, currentSpId) {
  var el = document.getElementById('ev-pane-evolution');
  if (!el) return;

  if (!root) {
    el.innerHTML = '<div class="evo-empty-tree">' +
      '<div style="font-size:24px;margin-bottom:8px;opacity:.5">&#x1F331;</div>' +
      '<div style="font-weight:600;margin-bottom:4px">No evolutionary variants yet</div>' +
      '<div>Chili\'s brain will fork entry conditions, exit strategies, and cross-breed patterns to evolve them over time.<br>Check back after the next reconcile pass or pattern refresh.</div>' +
    '</div>';
    return;
  }

  function wrColor(wr) {
    if (wr == null) return 'var(--text-muted)';
    if (wr >= 60) return '#22c55e';
    if (wr >= 40) return '#f59e0b';
    return '#ef4444';
  }

  function wrFillColor(wr) {
    if (wr == null) return 'rgba(128,128,128,.2)';
    if (wr >= 60) return '#22c55e';
    if (wr >= 40) return '#f59e0b';
    return '#ef4444';
  }

  function originLabel(o) {
    if (o === 'exit_variant') return 'exit';
    if (o === 'entry_variant') return 'entry';
    if (o === 'combo_variant') return 'combo';
    if (o === 'tf_variant') return 'timeframe';
    if (o === 'scope_variant') return 'scope';
    if (o === 'brain_discovered') return 'brain';
    return o || 'root';
  }

  function mutBadgeClass(mt) {
    if (mt === 'entry') return 'evo-mut-entry';
    if (mt === 'combo') return 'evo-mut-combo';
    if (mt === 'exit') return 'evo-mut-exit';
    if (mt === 'timeframe') return 'evo-mut-timeframe';
    if (mt === 'scope') return 'evo-mut-scope';
    return 'evo-mut-root';
  }

  function genClass(g) {
    if (g <= 0) return 'g0';
    if (g === 1) return 'g1';
    if (g === 2) return 'g2';
    return 'g3';
  }

  var _hypCounter = 0;

  function buildNode(node) {
    var isCurrent = node.is_current;
    var isInactive = !node.active;
    var cls = 'evo-node-card';
    if (isCurrent) cls += ' is-current';
    if (isInactive) cls += ' is-inactive';

    var gen = node.generation || 0;
    var wr = node.win_rate;
    var wrPct = wr != null ? Math.max(0, Math.min(100, wr)) : 0;
    var displayName = node.variant_label || node.name || 'Pattern #' + node.id;
    var truncName = displayName.length > 22 ? displayName.substring(0, 20) + '...' : displayName;

    var mt = node.mutation_type || 'root';
    var h = '<div class="' + cls + '" style="cursor:pointer" onclick="event.stopPropagation();showPatternByScanPatternId(' + node.id + ')">';
    h += '<div><span class="evo-node-gen ' + genClass(gen) + '">g' + gen + '</span>';
    h += '<span class="evo-origin-badge ' + mutBadgeClass(mt) + '">' + escHtml(originLabel(node.origin)) + '</span>';
    if (isCurrent) h += '<span style="float:right;font-size:8px;color:#8b5cf6;font-weight:700">YOU</span>';
    h += '</div>';

    if (node.variant_label) {
      h += '<div class="evo-node-label">' + escHtml(node.variant_label) + '</div>';
    }
    h += '<div class="evo-node-name" title="' + escHtml(displayName) + '">' + escHtml(truncName) + '</div>';

    h += '<div class="evo-node-stats">';
    h += '<span style="font-size:9px;font-weight:700;color:' + wrColor(wr) + ';min-width:32px">' + (wr != null ? wr.toFixed(0) + '%' : '--') + '</span>';
    h += '<div class="evo-wr-bar"><div class="evo-wr-fill" style="width:' + wrPct + '%;background:' + wrFillColor(wr) + '"></div></div>';
    h += '</div>';

    h += '<div class="evo-node-metrics">';
    var avgRet = node.avg_return_pct;
    var retColor = avgRet != null ? (avgRet >= 0 ? '#22c55e' : '#ef4444') : 'var(--text-muted)';
    h += '<span style="color:' + retColor + '">' + (avgRet != null ? (avgRet >= 0 ? '+' : '') + avgRet.toFixed(1) + '%' : '--') + '</span>';
    h += '<span>' + (node.backtest_count || 0) + ' BTs</span>';
    h += '<span>' + (node.wins || 0) + 'W/' + (node.losses || 0) + 'L</span>';
    h += '</div>';

    if (node.exit_config) {
      var ec = node.exit_config;
      var ecParts = [];
      if (ec.atr_mult != null) ecParts.push('ATR:' + ec.atr_mult.toFixed(1));
      if (ec.max_bars != null) ecParts.push('Bars:' + ec.max_bars);
      if (ec.use_bos === true) ecParts.push('BOS');
      if (ec.use_bos === false) ecParts.push('No-BOS');
      if (ecParts.length) {
        h += '<div style="margin-top:3px;font-size:8px;color:var(--text-muted);opacity:.7">' + ecParts.join(' | ') + '</div>';
      }
    }

    if (node.hypotheses && node.hypotheses.length > 0) {
      h += '<div class="evo-hyp-pills">';
      node.hypotheses.forEach(function(hyp) {
        var hid = 'evo-hyp-' + (++_hypCounter);
        var statusCls = hyp.status === 'confirmed' ? 'confirmed' : (hyp.status === 'rejected' ? 'rejected' : 'pending');
        h += '<span class="evo-hyp-pill ' + statusCls + '" onclick="var d=document.getElementById(\'' + hid + '\');d.classList.toggle(\'open\')" title="' + escHtml(hyp.description||'').substring(0,80) + '">';
        h += 'H' + hyp.id + ' ' + hyp.confirm_rate + '%';
        h += '</span>';
        h += '<div class="evo-hyp-detail" id="' + hid + '">' + escHtml(hyp.description||'') + '<br><span style="color:#8b5cf6">Tested: ' + hyp.times_tested + ' | ' + hyp.status + '</span></div>';
      });
      h += '</div>';
    }

    if (isInactive) {
      h += '<div style="margin-top:4px;font-size:8px;color:#ef4444;font-weight:600">DEACTIVATED</div>';
    }

    h += '</div>';
    return h;
  }

  function buildTree(node) {
    var h = '<div class="evo-branch">';
    h += buildNode(node);

    if (node.children && node.children.length > 0) {
      h += '<div class="evo-children">';
      node.children.forEach(function(child) {
        h += '<div class="evo-child-wrap">';
        h += buildTree(child);
        h += '</div>';
      });
      h += '</div>';
    }

    h += '</div>';
    return h;
  }

  var html = '<div class="evo-tree">' + buildTree(root) + '</div>';
  el.innerHTML = html;
  setTimeout(function() {
    var cur = el.querySelector('.evo-node-card.is-current');
    if (cur) cur.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
  }, 50);
}

function demotePattern(patternId) {
  brainConfirm({
    title: 'Demote Pattern',
    body: 'This will lower the pattern lifecycle stage. Demoted patterns require re-validation before they can be promoted again.',
    confirmLabel: 'Demote',
    variant: 'danger'
  }).then(function(ok) {
    if (!ok) return;
    fetch('/api/trading/learn/patterns/' + patternId + '/demote', {method:'POST'}).then(function(r){return r.json();}).then(function(d) {
      if (d.ok) {
        brainToast({ type: 'success', title: 'Pattern demoted', message: 'Pattern has been demoted successfully.' });
        closeBrainModal(); loadLearnedPatterns(); loadBrainActivity();
      }
    }).catch(function() {
      brainToast({ type: 'error', title: 'Demote failed', message: 'Could not demote pattern. Check the console for details.' });
    });
  });
}

/* â”€â”€ Cycle AI reports (paginated, flip card) â”€â”€â”€â”€â”€ */
var _cycleReportOffset = 0;
var _cycleReportTotal = 0;

function toggleCycleReportFlip(ev) {
  if (ev && ev.target && ev.target.closest && ev.target.closest('a')) return;
  var inner = document.getElementById('cycle-report-flip-inner');
  if (inner) inner.classList.toggle('is-flipped');
}

function cycleReportNav(delta) {
  var next = _cycleReportOffset + delta;
  if (next < 0) return;
  if (_cycleReportTotal > 0 && next >= _cycleReportTotal) return;
  _cycleReportOffset = next;
  loadCycleAiReportsPage(false);
}

function _cycleMetricsLine(m) {
  if (!m || typeof m !== 'object') return '';
  var parts = [];
  if (m.tickers_scored != null) parts.push('scored ' + m.tickers_scored);
  if (m.patterns_discovered != null) parts.push('patterns ' + m.patterns_discovered);
  if (m.backtests_run != null) parts.push('backtests ' + m.backtests_run);
  if (m.elapsed_s != null) parts.push(Math.round(m.elapsed_s) + 's');
  if (m.data_provider) parts.push(String(m.data_provider));
  return parts.length ? parts.join(' Â· ') : '';
}

/** Quant triad for cycle card: cycle-level counts where available; OOS/DD/trades point to pattern cards. */
function _cycleRobustnessLine(m) {
  if (!m || typeof m !== 'object') return '';
  var parts = [];
  if (m.backtests_run != null) {
    parts.push('<span title="Backtest jobs finished this cycle (insights + queue)"><b>BT runs</b> ' + m.backtests_run + '</span>');
  } else {
    parts.push('<span class="pc-pill-muted" style="display:inline-block;padding:1px 6px;border-radius:6px;font-size:8px"><b>BT runs</b> â€”</span>');
  }
  if (m.patterns_tested != null) {
    parts.push('<span title="Pattern-engine tests this cycle"><b>Engine tests</b> ' + m.patterns_tested + '</span>');
  } else {
    parts.push('<span title="Engine test count is recorded on newer cycles"><b>Engine tests</b> â€”</span>');
  }
  var evo = m.evolution && typeof m.evolution === 'object' ? m.evolution : {};
  if (evo.promoted != null && evo.promoted > 0) {
    parts.push('<span title="Variants promoted after compare"><b>Promoted</b> ' + evo.promoted + '</span>');
  }
  parts.push('<span style="opacity:.7" title="Held-out win rate lives on each ScanPattern; see cards below"><b>OOS WR</b> â€”</span>');
  parts.push('<span style="opacity:.7" title="Drawdown is per backtest; open a pattern for the equity table"><b>Max DD</b> â€”</span>');
  parts.push('<span style="opacity:.7" title="Simulated trade totals are summed on each pattern card"><b>Trades</b> â€”</span>');
  return parts.join(' <span style="opacity:.25">|</span> ');
}

function loadCycleAiReportsPage(resetOffset) {
  if (resetOffset) _cycleReportOffset = 0;
  var emptyEl = document.getElementById('cycle-report-empty');
  var uiEl = document.getElementById('cycle-report-ui');
  if (!emptyEl || !uiEl) return;
  fetch('/api/trading/brain/cycle-reports?limit=1&offset=' + encodeURIComponent(_cycleReportOffset))
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d.ok) {
        emptyEl.style.display = 'block';
        uiEl.style.display = 'none';
        return;
      }
      _cycleReportTotal = d.total || 0;
      if (!d.items || !d.items.length) {
        emptyEl.style.display = 'block';
        uiEl.style.display = 'none';
        return;
      }
      emptyEl.style.display = 'none';
      uiEl.style.display = 'block';
      var it = d.items[0];
      var prevBtn = document.getElementById('cycle-report-prev');
      var nextBtn = document.getElementById('cycle-report-next');
      if (prevBtn) prevBtn.disabled = _cycleReportOffset <= 0;
      if (nextBtn) nextBtn.disabled = _cycleReportTotal <= 0 || (_cycleReportOffset + 1 >= _cycleReportTotal);
      var posEl = document.getElementById('cycle-report-pos');
      if (posEl) posEl.textContent = _cycleReportTotal ? (_cycleReportOffset + 1) + ' / ' + _cycleReportTotal : '';
      var flip = document.getElementById('cycle-report-flip-inner');
      if (flip) flip.classList.remove('is-flipped');
      var when = it.created_at ? new Date(it.created_at) : null;
      var dateEl = document.getElementById('cycle-report-front-date');
      if (dateEl) {
        dateEl.textContent = (when && !isNaN(when.getTime())) ? when.toLocaleString() : (it.created_at || '');
      }
      var metEl = document.getElementById('cycle-report-front-metrics');
      if (metEl) metEl.textContent = _cycleMetricsLine(it.metrics);
      var robEl = document.getElementById('cycle-report-front-robustness');
      if (robEl) robEl.innerHTML = _cycleRobustnessLine(it.metrics || {});
      var prevEl = document.getElementById('cycle-report-front-preview');
      if (prevEl) prevEl.textContent = it.preview || '';
      var backMd = document.getElementById('cycle-report-back-md');
      if (backMd) backMd.innerHTML = '<div style="color:var(--text-muted)">Loadingâ€¦</div>';
      fetch('/api/trading/brain/cycle-reports/' + encodeURIComponent(it.id))
        .then(function(r) { return r.json(); })
        .then(function(det) {
          if (!backMd) return;
          if (!det.ok || det.content == null || det.content === '') {
            backMd.innerHTML = '<pre style="white-space:pre-wrap">' + escHtml(it.preview || '') + '</pre>';
            return;
          }
          if (typeof marked !== 'undefined') {
            try { backMd.innerHTML = '<div class="cycle-md">' + marked.parse(det.content) + '</div>'; }
            catch (e2) { backMd.innerHTML = '<pre style="white-space:pre-wrap">' + escHtml(det.content) + '</pre>'; }
          } else {
            backMd.innerHTML = '<pre style="white-space:pre-wrap">' + escHtml(det.content) + '</pre>';
          }
        })
        .catch(function() {
          if (backMd) backMd.innerHTML = '<pre style="white-space:pre-wrap">' + escHtml(it.preview || '') + '</pre>';
        });
    })
    .catch(function() {
      emptyEl.style.display = 'block';
      uiEl.style.display = 'none';
    });
}

/* â”€â”€ Learning pipeline status polling â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
var _PIPELINE_STEPS = [
  {id:'pre-filter',label:'Pre-Filter',icon:'&#x26A1;'},{id:'scan',label:'Deep Score',icon:'&#x1F50D;'},{id:'snapshot',label:'Snapshots',icon:'&#x1F4F8;'},
  {id:'backfill',label:'Backfill',icon:'&#x1F4CA;'},{id:'mine',label:'Patterns',icon:'&#x1F4A1;'},{id:'backtest',label:'Validate',icon:'&#x1F9EA;'},
  {id:'journal',label:'Journal',icon:'&#x1F4DD;'},{id:'signal',label:'Signals',icon:'&#x1F4E1;'},{id:'ml_train',label:'ML Train',icon:'&#x1F9E0;'},{id:'final',label:'Done',icon:'&#x2705;'}
];

/**
 * Resolve primary fields from ``/api/trading/scan/status``.
 * In-repo Brain desk uses ``brain_runtime`` only when the aggregate is present (happy path).
 * Top-level ``work_ledger`` / ``release`` / ``scheduler`` / ``scan`` are read only on
 * ``encode_error`` or other legacy edge cases (absent on happy path — see docs).
 * ``release`` is always empty. ``learning_summary`` + ``activity_signals`` drive operator strip;
 * ``pollLearningStatus`` merges ``learning_summary`` over top-level ``learning`` for those fields.
 * Neural graph overlay (``tbnUpdateNodeStatuses``) reads top-level ``learning`` only (mesh/step indices).
 * ``activity_signals``: ledger outcome refresh uses ``outcome_head_id`` when
 * ``window.__CHILI_LEDGER_OUTCOME_REFRESH === true`` (default off).
 */
if (typeof window.__CHILI_LEDGER_OUTCOME_REFRESH === 'undefined') { window.__CHILI_LEDGER_OUTCOME_REFRESH = false; }
var _chiliLedgerOutcomeHeadPrev = null;
var _chiliLedgerRefreshTimer = null;
function _debouncedLedgerOutcomeRefresh(headId) {
  if (!window.__CHILI_LEDGER_OUTCOME_REFRESH) return;
  if (headId == null) return;
  if (headId === _chiliLedgerOutcomeHeadPrev) return;
  var changed = _chiliLedgerOutcomeHeadPrev !== null;
  _chiliLedgerOutcomeHeadPrev = headId;
  if (!changed) return;
  if (_chiliLedgerRefreshTimer) clearTimeout(_chiliLedgerRefreshTimer);
  _chiliLedgerRefreshTimer = setTimeout(function() {
    loadPlaybook();
    loadPerfDashboard();
    loadTradeablePatterns();
    loadResearchEdgePatterns();
    if (typeof _bddActiveTab !== 'undefined' && _bddActiveTab === 'research') {
      if (typeof _bddLoaded !== 'undefined') _bddLoaded['research'] = false;
      if (typeof _loadResearchData === 'function') _loadResearchData();
    }
    _chiliLedgerRefreshTimer = null;
  }, 4000);
}

function scanStatusBrainRuntime(d) {
  var br = d && d.brain_runtime && typeof d.brain_runtime === 'object' ? d.brain_runtime : null;
  var encErr = !!(d && d.encode_error);
  var wlBr = br && br.work_ledger;
  var useAggregate = !encErr && wlBr != null && typeof wlBr === 'object';
  if (useAggregate) {
    return {
      work_ledger: wlBr,
      release: (br.release && typeof br.release === 'object') ? br.release : {},
      scheduler: (br.scheduler && typeof br.scheduler === 'object') ? br.scheduler : { running: false, jobs: [] },
      scan: (br.scan && typeof br.scan === 'object') ? br.scan : {},
      learning_summary: (br.learning_summary && typeof br.learning_summary === 'object') ? br.learning_summary : null,
      activity_signals: (br.activity_signals && typeof br.activity_signals === 'object') ? br.activity_signals : null
    };
  }
  return {
    work_ledger: d.work_ledger || {},
    release: d.release || {},
    scheduler: d.scheduler || { running: false, jobs: [] },
    scan: d.scan || {},
    learning_summary: null,
    activity_signals: null
  };
}

function pollLearningStatus() {
  return fetch('/api/trading/scan/status').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var rt = scanStatusBrainRuntime(d);
    var learningFull = d.learning || {};
    var lsOp = rt.learning_summary;
    var learning = (lsOp && typeof lsOp === 'object')
      ? Object.assign({}, learningFull, lsOp)
      : learningFull;
    var asLedger = rt.activity_signals;
    if (asLedger && asLedger.outcome_head_id != null) {
      _debouncedLedgerOutcomeRefresh(asLedger.outcome_head_id);
    }
    var scan = rt.scan;
    var scheduler = rt.scheduler;
    var wl = rt.work_ledger;
    var wlStrip = document.getElementById('brain-work-ledger-strip');
    var wlText = document.getElementById('brain-work-ledger-text');
    if (wlStrip && wlText && wl && wl.enabled !== false) {
      wlStrip.style.display = '';
      var pend = typeof wl.pending_work === 'number' ? wl.pending_work : 0;
      var rw = typeof wl.retry_wait === 'number' ? wl.retry_wait : 0;
      var dead = typeof wl.dead_last_24h === 'number' ? wl.dead_last_24h : 0;
      var pbt = wl.pending_by_type && typeof wl.pending_by_type === 'object' ? wl.pending_by_type : {};
      var pendTypes = Object.keys(pbt).map(function(k){ return k + ':' + pbt[k]; }).join(', ');
      var proc = (wl.processing && wl.processing.length) ? wl.processing : [];
      var procStr = proc.length ? proc.map(function(p){ return p.event_type + (p.lease_holder ? '@' + String(p.lease_holder).slice(0, 12) : ''); }).join(', ') : '—';
      var rc = (wl.recent_meaningful_outcomes && wl.recent_meaningful_outcomes.length) ? wl.recent_meaningful_outcomes : ((wl.recent_completions && wl.recent_completions.length) ? wl.recent_completions : []);
      var last = rc[0];
      var lastStr = last ? last.event_type : 'none';
      var rel = rt.release || {};
      var sha = rel.git_commit ? String(rel.git_commit).slice(0, 7) : '';
      var pulse = wl.execution_pulse;
      var eo24 = wl.execution_outcomes_24h && typeof wl.execution_outcomes_24h === 'object' ? wl.execution_outcomes_24h : {};
      var lead = [];
      if (pulse && pulse.event_type) {
        var evLab = pulse.event_type === 'broker_fill_closed' ? 'Broker' : 'Live';
        var tk = pulse.ticker || '—';
        var pid = pulse.scan_pattern_id != null ? ' p#' + pulse.scan_pattern_id : '';
        var pnlStr = (typeof pulse.pnl === 'number') ? (' pnl ' + pulse.pnl) : '';
        var src = pulse.source ? (' · ' + String(pulse.source).slice(0, 24)) : '';
        lead.push(evLab + ' ' + tk + pid + pnlStr + src);
      }
      var e24k = Object.keys(eo24);
      if (e24k.length) {
        var e24lbl = { live_trade_closed: 'live', broker_fill_closed: 'broker', paper_trade_closed: 'paper' };
        lead.push('24h ' + e24k.map(function(k){ return (e24lbl[k] || k.slice(0, 10)) + ':' + eo24[k]; }).join(' '));
      }
      if (sha) lead.push('build ' + sha);
      var queue = 'Q ' + pend + (rw ? ' (retry ' + rw + ')' : '') + ' · dead24h ' + dead + ' · ' + (pendTypes || '—') + ' · proc ' + procStr;
      wlText.textContent = lead.length ? (lead.join(' · ') + ' — ' + queue) : (queue + ' · last ' + lastStr);
    } else if (wlStrip) {
      wlStrip.style.display = 'none';
    }
    var pipelineSection = document.getElementById('brain-pipeline-section');
    var pipelineDetails = document.getElementById('brain-reconcile-pipeline-details');

    if (learning.running) {
      if (pipelineDetails) {
        pipelineDetails.style.display = '';
        pipelineDetails.open = true;
      }
      if (pipelineSection) pipelineSection.style.display = '';
      var stepEl = document.getElementById('brain-learning-step');
      var progEl = document.getElementById('brain-learning-progress');
      var barEl = document.getElementById('brain-progress-bar');
      if (stepEl) stepEl.textContent = learning.current_step || '';
      var pct = learning.total_nodes ? Math.round(learning.nodes_completed / learning.total_nodes * 100) : 0;
      var clusterText = 'Clusters ' + (learning.clusters_completed || 0) + '/' + (learning.total_clusters || 0);
      var nodeText = 'Nodes ' + (learning.nodes_completed || 0) + '/' + (learning.total_nodes || 0);
      var extra = (learning.tickers_processed > 0 ? ' (' + learning.tickers_processed + ' scored)' : '') + (learning.elapsed_s ? ' ' + Math.round(learning.elapsed_s) + 's' : '');
      if (progEl) progEl.textContent = clusterText + '  ' + nodeText + extra;
      if (barEl) barEl.style.width = Math.min(100, pct) + '%';
    } else {
      // Secondary UX: keep <details> visible but collapsed when idle (not display:none).
      if (pipelineDetails) {
        pipelineDetails.style.display = '';
        pipelineDetails.open = false;
      }
      if (pipelineSection) pipelineSection.style.display = 'none';
    }

    var infoEl = document.getElementById('brain-scheduler-info');
    var parts = [];
    if (scan.last_run) { parts.push('Last: ' + timeSince(new Date(scan.last_run)) + ' ago'); if (scan.tickers_scored) parts.push(scan.tickers_scored.toLocaleString() + ' scored'); }
    else parts.push('No scans yet');
    if (scheduler.jobs && scheduler.jobs.length) {
      var nextJob = scheduler.jobs.find(function(j){return j.id==='learning_cycle';});
      if (nextJob && nextJob.next_run) parts.push('Next: ' + new Date(nextJob.next_run).toLocaleTimeString());
    }
    if (infoEl) infoEl.textContent = parts.join(' Â· ');

    if (!learning.running && pipelineSection && pipelineSection._wasRunning) {
      loadPlaybook();
      loadPerfDashboard();
      loadTradeablePatterns();
      loadResearchEdgePatterns();
      if (_bddActiveTab === 'research') { _bddLoaded['research'] = false; _loadResearchData(); }
    }
    if (pipelineSection) pipelineSection._wasRunning = learning.running;
  }).catch(function(){}).then(function() { return updateSidebarStatus(); });
}

/* â”€â”€ Backfill progress polling â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
var _bfWasRunning = false;
function pollBackfillStatus() {
  return fetch('/api/trading/learn/backfill-status').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var banner = document.getElementById('backfill-banner');
    if (d.running && d.total > 0) {
      banner.style.display = 'block';
      var pct = Math.round(d.done / d.total * 100);
      document.getElementById('bf-progress-text').textContent = d.done + ' / ' + d.total + ' patterns (' + d.filled + ' updated)';
      document.getElementById('bf-progress-bar').style.width = pct + '%';
      _bfWasRunning = true;
    } else {
      banner.style.display = 'none';
      if (_bfWasRunning) { _bfWasRunning = false; loadLearnedPatterns(); }
    }
  }).catch(function(){});
}

/* â”€â”€ Deep Study trigger â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
function triggerDeepStudy() {
  var btn = document.getElementById('btn-deep-study');
  brainConfirm({
    title: 'Trigger Deep Study',
    body: 'This will run an intensive deep study cycle analyzing all patterns. It may take several minutes and will use significant API resources.',
    confirmLabel: 'Start Deep Study',
    variant: 'primary'
  }).then(function(ok) {
    if (!ok) return;
    btn.disabled = true;
    btn.innerHTML = '\u{1F52C} Studying...';
    fetch('/api/trading/learn/deep-study', {method:'POST'})
    .then(function(r) {
      return r.json().then(function(d) {
        return { ok: r.ok, body: d };
      }).catch(function() {
        return { ok: false, body: { error: 'Server returned non-JSON (status ' + r.status + ')' } };
      });
    })
    .then(function(result) {
      btn.disabled = false;
      btn.innerHTML = '\u{1F52C} Deep Study';
      var d = result.body;
      if (result.ok && d && d.ok) {
        var panel = document.getElementById('brain-reflection');
        var content = document.getElementById('brain-reflection-content');
        panel.style.display = 'block';
        if (typeof marked !== 'undefined') {
          try { content.innerHTML = marked.parse(d.reflection); }
          catch(e) { content.textContent = d.reflection; }
        } else {
          content.textContent = d.reflection;
        }
        loadBrainDashboard();
      } else {
        var msg = (d && d.error) ? d.error : 'Deep study failed';
        var panel = document.getElementById('brain-reflection');
        var content = document.getElementById('brain-reflection-content');
        if (panel && content) {
          panel.style.display = 'block';
          content.innerHTML = '<p style="color:var(--danger,#c00)">' + escHtml(msg) + '</p>';
        } else {
          alert(msg);
        }
      }
    })
    .catch(function(e) {
      btn.disabled = false;
      btn.innerHTML = '\u{1F52C} Deep Study';
      console.error('Deep study error:', e);
      brainToast({ type: 'error', title: 'Deep study error', message: e.message || String(e) });
    });
  });
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Code Brain JavaScript
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */