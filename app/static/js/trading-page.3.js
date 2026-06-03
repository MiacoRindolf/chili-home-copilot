
/* ── Stop Management ──────────────────────────── */

function loadStopPositions() {
  fetch('/api/trading/stops/positions').then(function(r){return r.json();}).then(function(d) {
    var list = document.getElementById('stop-positions-list');
    if (!d.ok || !d.positions || !d.positions.length) {
      list.innerHTML = '<div style="font-size:11px;color:var(--text-muted);padding:12px;text-align:center;">No open positions with stop data.</div>';
      return;
    }
    var html = '<div style="display:grid;gap:6px;">';
    d.positions.forEach(function(p) {
      var stateColor = {'initial':'#888','breakeven':'#22c55e','trailing':'#3b82f6','warn':'#f59e0b','triggered':'#ef4444'}[p.state] || '#888';
      var pnlColor = p.pnl_pct >= 0 ? '#22c55e' : '#ef4444';
      var pnlSign = p.pnl_pct >= 0 ? '+' : '';
      html += '<div style="background:var(--card-bg,#1a1a2e);border:1px solid var(--border);border-left:3px solid '+stateColor+';border-radius:8px;padding:8px 10px;font-size:10px;">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">';
      html += '<div><span style="font-weight:700;font-size:12px;">'+p.ticker+'</span> <span style="color:var(--text-muted);font-size:9px;">'+p.direction+' via '+(p.broker_source||'manual')+'</span></div>';
      html += '<span style="padding:1px 6px;border-radius:4px;font-size:9px;font-weight:700;background:'+stateColor+'22;color:'+stateColor+';text-transform:uppercase;">'+p.state+'</span>';
      html += '</div>';
      html += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px;margin-top:4px;">';
      html += '<div><div style="color:var(--text-muted);font-size:8px;">Entry</div><div>$'+(p.entry_price?p.entry_price.toFixed(2):'—')+'</div></div>';
      html += '<div><div style="color:var(--text-muted);font-size:8px;">Price</div><div>$'+(p.current_price?p.current_price.toFixed(2):'—')+'</div></div>';
      html += '<div><div style="color:var(--text-muted);font-size:8px;">Stop</div><div style="color:#ef4444;">$'+(p.stop_loss?p.stop_loss.toFixed(2):'—')+'</div></div>';
      html += '<div><div style="color:var(--text-muted);font-size:8px;">Target</div><div style="color:#22c55e;">$'+(p.take_profit?p.take_profit.toFixed(2):'—')+'</div></div>';
      html += '</div>';
      html += '<div style="display:flex;gap:12px;margin-top:5px;color:var(--text-muted);font-size:9px;">';
      html += '<span>P&L: <b style="color:'+pnlColor+'">'+pnlSign+p.pnl_pct+'%</b></span>';
      html += '<span>R: <b>'+p.current_r+'</b></span>';
      html += '<span>Stop dist: <b>'+p.stop_distance_pct+'%</b></span>';
      if(p.stop_model) html += '<span>Model: '+p.stop_model+'</span>';
      html += '</div>';
      html += '</div>';
    });
    html += '</div>';
    list.innerHTML = html;
  }).catch(function(e) {
    document.getElementById('stop-positions-list').innerHTML = '<div style="font-size:10px;color:#ef4444;padding:8px;">Error loading positions</div>';
  });
}

/* ── Pattern monitor (active setups) ─────────── */
window.__monitorPayload = null;

function escHtml(s) {
  if (s == null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function _monitorActionClass(act) {
  if (!act) return 'mon-badge-action-hold';
  var a = String(act).toLowerCase();
  if (a === 'tighten_stop') return 'mon-badge-action-tighten_stop';
  if (a === 'loosen_target') return 'mon-badge-action-loosen_target';
  if (a === 'exit_now') return 'mon-badge-action-exit_now';
  return 'mon-badge-action-hold';
}

function _monitorDecisionSourceLabel(src) {
  var s = String(src || '').toLowerCase();
  if (s === 'llm_unavailable') return 'rules-only';
  if (s === 'parse_failed') return 'rules-only';
  return String(src || '').replace(/_/g, ' ');
}

function _monitorExecutionClass(state) {
  var s = String(state || '').toLowerCase();
  if (s === 'exit_order_working') return 'mon-badge-exec-working';
  if (s === 'deferred') return 'mon-badge-exec-deferred';
  if (s === 'closed_weekend') return 'mon-badge-exec-closed';
  return 'mon-badge-exec-deferred';
}

function _monitorHealthPct(ld) {
  if (!ld) return null;
  if (ld.health_score_pct != null && ld.health_score_pct !== '') {
    var p = Number(ld.health_score_pct);
    if (isFinite(p)) return Math.max(0, Math.min(100, p));
  }
  if (ld.health_score == null) return null;
  var raw = Number(ld.health_score);
  if (!isFinite(raw)) return null;
  if (raw <= 1.5) return Math.max(0, Math.min(100, raw * 100));
  return Math.max(0, Math.min(100, raw));
}

function _monitorDeltaPts(ld) {
  if (!ld) return null;
  if (ld.health_delta_pts != null && ld.health_delta_pts !== '') {
    var pts = Number(ld.health_delta_pts);
    if (isFinite(pts)) return pts;
  }
  if (ld.health_delta == null) return null;
  var raw = Number(ld.health_delta);
  if (!isFinite(raw)) return null;
  if (Math.abs(raw) <= 1.5) return raw * 100;
  return raw;
}

function _monitorCardUrgencyClass(s) {
  var ld = s.latest_decision;
  if (ld && ld.action === 'exit_now') return 'mon-card-urgent';
  var h = _monitorHealthPct(ld);
  if (h != null && h < 40) return 'mon-card-urgent';
  if (h != null && h < 70) return 'mon-card-watch';
  return '';
}

function _monitorSortSetups(list, mode) {
  var arr = list.slice();
  if (mode === 'ticker') {
    arr.sort(function(a, b) { return String(a.ticker || '').localeCompare(String(b.ticker || '')); });
    return arr;
  }
  if (mode === 'pnl') {
    arr.sort(function(a, b) {
      var pa = a.pnl_pct != null ? Number(a.pnl_pct) : -9999;
      var pb = b.pnl_pct != null ? Number(b.pnl_pct) : -9999;
      return pb - pa;
    });
    return arr;
  }
  arr.sort(function(a, b) {
    var ha = _monitorHealthPct(a.latest_decision);
    var hb = _monitorHealthPct(b.latest_decision);
    if (ha == null) ha = 999;
    if (hb == null) hb = 999;
    return ha - hb;
  });
  return arr;
}

function applyMonitorSort() {
  if (!window.__monitorPayload || !window.__monitorPayload.setups) return;
  var mode = (document.getElementById('monitor-sort') || {}).value || 'health';
  var sorted = _monitorSortSetups(window.__monitorPayload.setups, mode);
  renderMonitorCards(sorted);
}

function toggleMonitorCardDetails(tradeId) {
  var el = document.getElementById('mon-details-' + tradeId);
  var btn = document.getElementById('mon-expand-' + tradeId);
  if (!el) return;
  var open = el.classList.toggle('open');
  if (btn) btn.textContent = open ? 'Hide details' : 'Details & history';
}

function renderMonitorCards(setups) {
  var grid = document.getElementById('monitor-cards');
  var empty = document.getElementById('monitor-empty');
  if (!grid) return;
  if (!setups || !setups.length) {
    grid.innerHTML = '';
    if (empty) empty.style.display = 'block';
    return;
  }
  if (empty) empty.style.display = 'none';
  var html = '';
  setups.forEach(function(s) {
    var ld = s.latest_decision;
    var health = _monitorHealthPct(ld);
    var hPct = health != null ? health : null;
    var deltaPts = _monitorDeltaPts(ld);
    var deltaStr = deltaPts == null ? '' : (deltaPts >= 0 ? '+' + deltaPts.toFixed(1) : deltaPts.toFixed(1));
    var deltaColor = deltaPts == null ? 'var(--text-muted)' : (deltaPts >= 0 ? '#22c55e' : '#ef4444');
    var act = ld ? ld.action : '—';
    var src = ld && ld.decision_source ? _monitorDecisionSourceLabel(ld.decision_source) : '—';
    var when = ld && ld.created_at ? ld.created_at : 'never';
    var healthLabel = ld && ld.health_label ? ld.health_label : 'Pattern health';
    var healthHint = ld && ld.health_hint ? ld.health_hint : 'Share of pattern conditions still satisfied';
    var healthScale = ld && ld.health_source && ld.health_source !== 'static_conditions' ? '0–100 (live setup)' : '0–100 (conditions met)';
    var pnlStr = s.pnl_pct != null ? (Number(s.pnl_pct) >= 0 ? '+' : '') + Number(s.pnl_pct).toFixed(2) + '%' : '—';
    var pnlColor = s.pnl_pct == null ? 'var(--text)' : (Number(s.pnl_pct) >= 0 ? '#22c55e' : '#ef4444');
    var planTitle = s.plan_label || s.pattern_name;
    var pat = planTitle ? escHtml(planTitle) : '—';
    var tf = s.timeframe ? '<span class="mon-tf-pill">' + escHtml(s.timeframe) + '</span>' : '';
    var snap = ld && ld.conditions_snapshot ? ld.conditions_snapshot : null;
    var snapStr = snap ? escHtml(JSON.stringify(snap, null, 2)) : '';
    var reason = ld && ld.llm_reasoning ? escHtml(ld.llm_reasoning) : '';
    var cardClass = 'mon-card ' + _monitorCardUrgencyClass(s);
    var selClass = (_selectedMonitorTradeId === s.trade_id) ? ' mon-card-selected' : '';
    html += '<div class="' + cardClass + selClass + '" id="mon-card-' + s.trade_id + '" onclick="_selectMonitorCard(' + s.trade_id + ')">';
    html += '<div class="mon-card-head">';
    html += '<div><span class="mon-card-ticker">' + escHtml(s.ticker) + '</span> <span class="mon-dir ' + (s.direction === 'short' ? 'short' : 'long') + '">' + escHtml(s.direction || 'long') + '</span></div>';
    html += '<div style="font-size:9px;color:var(--text-muted);">#' + s.trade_id + ' · <span style="color:var(--accent);font-size:8px;">click to chart</span></div>';
    html += '</div>';
    html += '<div class="mon-pattern-line">' + pat + tf + '</div>';
    html += '<div class="mon-health-label-row">';
    html += '<span class="mon-health-lbl">' + escHtml(healthLabel) + '</span>';
    html += '<span class="mon-health-scale-hint">' + escHtml(healthScale) + '</span>';
    html += '</div>';
    html += '<div class="mon-health-row">';
    html += '<div class="mon-health-bar-wrap" title="' + escHtml(healthHint) + '">';
    html += '<div class="mon-health-bar" style="width:' + (hPct != null ? hPct : 0) + '%;"></div></div>';
    html += '<div class="mon-health-score-wrap">';
    html += '<span class="mon-health-num" title="' + escHtml(healthLabel) + ' score (0–100)">' + (health != null ? Math.round(health) : '—') + '</span>';
    html += '<span class="mon-health-num-sub">/ 100</span>';
    html += '</div></div>';
    if (deltaPts != null) html += '<div style="font-size:10px;color:' + deltaColor + ';">Δ vs last check: <b>' + deltaStr + '</b> pts on this scale</div>';
    html += '<div class="mon-meta-row">';
    html += '<div>Entry<b>$' + (s.entry_price != null ? Number(s.entry_price).toFixed(4) : '—') + '</b></div>';
    html += '<div>Price<b>' + (s.current_price != null ? '$' + Number(s.current_price).toFixed(4) : '—') + '</b></div>';
    html += '<div>P&amp;L<b style="color:' + pnlColor + '">' + pnlStr + '</b></div>';
    html += '</div>';
    html += '<div class="mon-meta-row">';
    html += '<div>Stop<b style="color:#f87171;">' + (s.stop_loss != null ? '$' + Number(s.stop_loss).toFixed(4) : '—') + '</b></div>';
    html += '<div>Target<b style="color:#86efac;">' + (s.take_profit != null ? '$' + Number(s.take_profit).toFixed(4) : '—') + '</b></div>';
    html += '<div>Decisions<b>' + (s.decision_count != null ? s.decision_count : 0) + '</b></div>';
    html += '</div>';
    html += '<div class="mon-badges">';
    html += '<span class="mon-badge ' + _monitorActionClass(act) + '">' + escHtml(act) + '</span>';
    html += '<span class="mon-badge mon-badge-src">' + escHtml(src) + '</span>';
    if (s.execution_label) {
      html += '<span class="mon-badge ' + _monitorExecutionClass(s.execution_state) + '">' + escHtml(s.execution_label) + '</span>';
    }
    html += '<span style="font-size:9px;color:var(--text-muted);">' + escHtml(when) + '</span>';
    html += '</div>';
    if (s.execution_reason || s.next_eligible_session_at) {
      var execReason = s.execution_reason || '';
      if (s.next_eligible_session_at) {
        execReason += (execReason ? ' · ' : '') + 'next: ' + s.next_eligible_session_at;
      }
      html += '<div class="mon-exec-reason">' + escHtml(execReason) + '</div>';
    }
    var aiPlan = _getPlanByTradeId(s.trade_id) || _getPlanByTicker(s.ticker);
    if (aiPlan) {
      var assessClass = 'assess-' + (aiPlan.assessment || 'neutral').toLowerCase();
      var planActClass = 'action-' + ((aiPlan.action && aiPlan.action.primary) || 'hold').toLowerCase().replace(/ /g, '_');
      var cr = (s.ticker || '').indexOf('-USD') !== -1;
      var planSp = function(v) { return smartPrice(v, cr); };
      html += '<div class="mon-plan-inline">';
      html += '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">';
      html += '<span style="font-size:9px;font-weight:700;color:#f59e0b;">AI PLAN</span>';
      html += '<span class="mon-plan-assessment ' + assessClass + '">' + escHtml(aiPlan.assessment || '?') + '</span>';
      if (aiPlan.action && aiPlan.action.primary) html += '<span class="mon-plan-action ' + planActClass + '">' + escHtml(aiPlan.action.primary) + '</span>';
      if (aiPlan.action && aiPlan.action.urgency) html += '<span style="font-size:8px;color:var(--text-muted);">' + escHtml(aiPlan.action.urgency) + '</span>';
      if (aiPlan.confidence != null) html += '<span style="font-size:8px;color:var(--text-muted);">' + (aiPlan.confidence * 100).toFixed(0) + '%</span>';
      html += '</div>';
      if (aiPlan.one_liner) html += '<div style="font-size:9px;color:var(--text-secondary);font-style:italic;margin-top:2px;">' + escHtml(aiPlan.one_liner) + '</div>';
      if (aiPlan.risk_management || aiPlan.key_levels) {
        html += '<div class="mon-plan-levels" style="margin-top:4px;">';
        if (aiPlan.risk_management && aiPlan.risk_management.recommended_stop != null) html += '<div>AI Stop<b style="color:#f87171;">$' + planSp(aiPlan.risk_management.recommended_stop) + '</b></div>';
        if (aiPlan.risk_management && aiPlan.risk_management.risk_reward_ratio) html += '<div>R:R<b>' + escHtml(aiPlan.risk_management.risk_reward_ratio) + '</b></div>';
        if (aiPlan.key_levels && aiPlan.key_levels.support && aiPlan.key_levels.support[0] != null) html += '<div>Support<b style="color:#06b6d4;">$' + planSp(aiPlan.key_levels.support[0]) + '</b></div>';
        html += '</div>';
      }
      html += '</div>';
    }
    html += '<div class="mon-card-expand"><button type="button" class="mon-expand-btn" id="mon-expand-' + s.trade_id + '" onclick="event.stopPropagation(); toggleMonitorCardDetails(' + s.trade_id + ')">Details &amp; history</button></div>';
    html += '<div class="mon-card-details" id="mon-details-' + s.trade_id + '">';
    if (reason) html += '<div class="mon-reason"><b>Reasoning</b><br/>' + reason + '</div>';
    if (snapStr) html += '<div style="margin-bottom:6px;"><b>Conditions snapshot</b></div><pre style="margin:0;white-space:pre-wrap;word-break:break-all;font-size:9px;max-height:160px;overflow:auto;">' + snapStr + '</pre>';
    html += '<div style="margin:8px 0 4px;font-weight:700;">Recent</div><div class="mon-mini-timeline">';
    (s.recent_decisions || []).forEach(function(rd) {
      var hp = _monitorHealthPct(rd);
      html += '<div class="mon-mini-row">' + escHtml(rd.created_at || '') + ' · ' + escHtml(rd.action || '') + ' · h=' + (hp != null ? Math.round(hp) : '—') + '/100</div>';
    });
    html += '</div></div></div>';
  });
  grid.innerHTML = html;
}

function _monitorSetSummaryHealthClass(val) {
  var el = document.getElementById('mon-sum-health');
  if (!el || !el.parentElement) return;
  el.parentElement.className = 'mon-stat';
  if (val == null || val === '') return;
  var n = Number(val);
  if (n >= 70) el.parentElement.classList.add('health-ok');
  else if (n >= 40) el.parentElement.classList.add('health-warn');
  else el.parentElement.classList.add('health-bad');
}

/** Re-fetch monitor cards if the user has opened the Monitor tab at least once (avoids stale cards after close/sell/sync). */
function _refreshMonitorIfLoaded() {
  if (window.__monitorPayload != null) {
    loadMonitorData(false);
  }
}

function loadMonitorData(showRefreshFlash) {
  var runBtn = document.getElementById('monitor-run-btn');
  if (runBtn) {
    runBtn.disabled = !!CHILI_TRADING_IS_GUEST;
    if (CHILI_TRADING_IS_GUEST) runBtn.title = 'Sign in to run the pattern monitor';
    else runBtn.title = 'Evaluate all monitored positions now';
  }
  var evalBtn = document.getElementById('monitor-eval-btn');
  if (evalBtn) {
    evalBtn.disabled = !!CHILI_TRADING_IS_GUEST;
    if (CHILI_TRADING_IS_GUEST) evalBtn.title = 'Sign in to evaluate positions';
    else evalBtn.title = 'AI evaluates all open positions with custom plans';
  }
  var grid = document.getElementById('monitor-cards');
  if (grid && !window.__monitorPayload) {
    grid.innerHTML = '<div style="font-size:11px;color:var(--text-muted);padding:16px;text-align:center;">Loading…</div>';
  }
  fetch('/api/trading/active-setups').then(function(r) {
    if (!r.ok) {
      return r.text().then(function() {
        return { ok: false, _httpStatus: r.status };
      });
    }
    return r.json();
  }).then(function(d) {
    var emptyEl = document.getElementById('monitor-empty');
    if (!d || !d.ok) {
      if (emptyEl) emptyEl.style.display = 'none';
      var msg = 'Failed to load monitor data';
      if (d && d._httpStatus) msg += ' (HTTP ' + d._httpStatus + ')';
      if (grid) grid.innerHTML = '<div style="font-size:11px;color:#ef4444;padding:12px;">' + msg + '</div>';
      return;
    }
    window.__monitorPayload = d;
    var sum = d.summary || {};
    var cEl = document.getElementById('mon-sum-count');
    if (cEl) cEl.textContent = sum.active_count != null ? String(sum.active_count) : '0';
    var hEl = document.getElementById('mon-sum-health');
    if (hEl) {
      hEl.textContent = sum.avg_health != null ? Number(sum.avg_health).toFixed(1) : '—';
      _monitorSetSummaryHealthClass(sum.avg_health != null ? Number(sum.avg_health) : null);
    }
    var aEl = document.getElementById('mon-sum-actions');
    if (aEl) aEl.textContent = sum.actions_today != null ? String(sum.actions_today) : '0';
    var bEl = document.getElementById('mon-sum-benefit');
    if (bEl) bEl.textContent = sum.benefit_rate != null ? (Number(sum.benefit_rate) * 100).toFixed(0) + '%' : '—';
    var lEl = document.getElementById('mon-sum-last');
    if (lEl) lEl.textContent = sum.last_check ? sum.last_check : '—';
    var suppressedCount = Number(sum.suppressed_stale_count || 0);
    var monStatus = document.getElementById('monitor-status');
    if (suppressedCount > 0 && monStatus && !showRefreshFlash) {
      monStatus.style.display = 'block';
      monStatus.className = 'mon-status ok';
      monStatus.textContent = 'Synced: hidden ' + suppressedCount + ' closed broker position' + (suppressedCount === 1 ? '' : 's');
    } else if (suppressedCount <= 0 && monStatus && !showRefreshFlash && monStatus.textContent.indexOf('Synced: hidden ') === 0) {
      monStatus.style.display = 'none';
    }
    var mode = (document.getElementById('monitor-sort') || {}).value || 'health';
    renderMonitorCards(_monitorSortSetups(d.setups || [], mode));
    if (emptyEl) emptyEl.style.display = (d.setups && d.setups.length) ? 'none' : 'block';
    if (showRefreshFlash) {
      var st = document.getElementById('monitor-status');
      if (st) {
        st.style.display = 'block';
        st.className = 'mon-status ok';
        st.textContent = 'Refreshed';
        setTimeout(function() { st.style.display = 'none'; }, 2000);
      }
    }
    loadMonitorDecisionFeed();
    loadImminentAlerts();
    if (!window.__positionPlans) _loadCachedPositionPlans();
  }).catch(function() {
    var emptyEl2 = document.getElementById('monitor-empty');
    if (emptyEl2) emptyEl2.style.display = 'none';
    if (grid) grid.innerHTML = '<div style="font-size:11px;color:#ef4444;padding:12px;">Error loading monitor</div>';
  });
}

function runMonitorNow() {
  if (CHILI_TRADING_IS_GUEST) return;
  var st = document.getElementById('monitor-status');
  if (st) {
    st.style.display = 'block';
    st.className = 'mon-status ok';
    st.textContent = 'Running pattern monitor…';
  }
  fetch('/api/trading/active-setups/run', { method: 'POST' }).then(function(r) {
    if (!r.ok) {
      return r.text().then(function(body) {
        try { return JSON.parse(body); } catch (e) { return { ok: false, error: 'HTTP ' + r.status }; }
      });
    }
    return r.json();
  }).then(function(d) {
    if (!d.ok) {
      if (st) {
        st.className = 'mon-status err';
        st.textContent = d.error || 'Monitor run failed';
      }
      return;
    }
    if (st) {
      st.className = 'mon-status ok';
      st.textContent = 'Done: evaluated ' + (d.evaluated != null ? d.evaluated : 0) + ', actions ' + (d.actions != null ? d.actions : 0) + ' (' + (d.elapsed_s != null ? d.elapsed_s : '?') + 's)';
    }
    loadMonitorData(false);
  }).catch(function() {
    if (st) {
      st.className = 'mon-status err';
      st.textContent = 'Request failed';
    }
  });
}

function loadMonitorDecisionFeed() {
  var list = document.getElementById('monitor-feed-list');
  if (!list) return;
  var sel = document.getElementById('monitor-feed-action');
  var act = sel && sel.value ? '&action=' + encodeURIComponent(sel.value) : '';
  fetch('/api/trading/active-setups/decisions?limit=40' + act).then(function(r) {
    if (!r.ok) {
      return { ok: false };
    }
    return r.json();
  }).then(function(d) {
    if (!d || !d.ok) {
      list.innerHTML = '<div style="font-size:10px;color:#ef4444;padding:8px;">Could not load decision feed</div>';
      return;
    }
    if (!d.decisions || !d.decisions.length) {
      list.innerHTML = '<div style="font-size:10px;color:var(--text-muted);padding:10px;">No decisions yet.</div>';
      return;
    }
    var html = '';
    d.decisions.forEach(function(x) {
      var rowClass = 'mon-feed-row';
      if (x.action === 'exit_now') rowClass += ' mon-feed-exit';
      else if (x.action === 'tighten_stop') rowClass += ' mon-feed-tighten';
      else if (x.action === 'loosen_target') rowClass += ' mon-feed-loosen';
      var snippet = (x.llm_reasoning || '').slice(0, 220);
      if ((x.llm_reasoning || '').length > 220) snippet += '…';
      var stops = '';
      if (x.old_stop != null || x.new_stop != null) stops = ' stop ' + (x.old_stop != null ? x.old_stop : '—') + '→' + (x.new_stop != null ? x.new_stop : '—');
      html += '<div class="' + rowClass + '">';
      html += '<div><div class="mon-feed-ts">' + escHtml(x.created_at || '') + '</div><div class="mon-feed-tk">' + escHtml(x.ticker || '') + '</div></div>';
      html += '<div><span class="mon-badge ' + _monitorActionClass(x.action) + '">' + escHtml(x.action || '') + '</span></div>';
      var fhp = _monitorHealthPct(x);
      html += '<div class="mon-feed-body">health=' + (fhp != null ? Math.round(fhp) + '/100' : '—') + stops + (snippet ? '<br/>' + escHtml(snippet) : '') + '</div>';
      html += '</div>';
    });
    list.innerHTML = html;
  }).catch(function() {
    list.innerHTML = '<div style="font-size:10px;color:#ef4444;padding:8px;">Error loading feed</div>';
  });
}

function loadImminentAlerts() {
  var list = document.getElementById('monitor-imminent-list');
  var countEl = document.getElementById('mon-imminent-count');
  if (!list) return;
  fetch('/api/trading/monitor/imminent-alerts?hours=72').then(function(r) {
    if (!r.ok) return { ok: false };
    return r.json();
  }).then(function(d) {
    if (!d || !d.ok) {
      list.innerHTML = '<div class="mon-imm-empty">Could not load imminent alerts</div>';
      if (countEl) countEl.textContent = '';
      return;
    }
    var alerts = d.alerts || [];
    if (countEl) countEl.textContent = alerts.length ? String(alerts.length) : '';
    if (!alerts.length) {
      list.innerHTML = '<div class="mon-imm-empty">No imminent alerts pending action right now.</div>';
      return;
    }
    var summary = d.summary || {};
    var html = '';
    if (summary.total) {
      var bits = [
        ['Neg EV', summary.negative_expected_edge || 0, '#f87171'],
        ['Shadow +EV', summary.positive_edge_shadow_only || 0, '#fbbf24'],
        ['Recert +EV', summary.positive_edge_recert_debt || 0, '#fb923c'],
        ['Slippage', summary.missed_entry_slippage || 0, '#93c5fd'],
        ['Broker', summary.broker_execution_rejects || 0, '#fca5a5'],
        ['Live-ready', summary.live_eligible_candidates || 0, '#86efac']
      ];
      html += '<div class="mon-imm-card" style="cursor:default;border-color:rgba(148,163,184,0.28);">';
      html += '<div style="width:100%;">';
      html += '<div style="font-size:10px;font-weight:800;color:var(--text-secondary);margin-bottom:5px;">Edge funnel</div>';
      html += '<div style="display:flex;gap:6px;flex-wrap:wrap;">';
      bits.forEach(function(b) {
        html += '<span style="font-size:9px;color:' + b[2] + ';border:1px solid rgba(148,163,184,0.25);border-radius:999px;padding:2px 7px;">' + escHtml(b[0]) + ' <b>' + Number(b[1] || 0).toFixed(0) + '</b></span>';
      });
      html += '</div></div></div>';
    }
    alerts.forEach(function(a) {
      var scoreClass = 'mon-imm-score-low';
      if (a.score != null && a.score >= 75) scoreClass = 'mon-imm-score-high';
      else if (a.score != null && a.score >= 50) scoreClass = 'mon-imm-score-med';
      var cr = (a.ticker || '').indexOf('-USD') !== -1;
      var sp = function(v) { return smartPrice(v, cr); };
      var ago = '';
      if (a.alerted_at) {
        var ms = Date.now() - new Date(a.alerted_at).getTime();
        var hrs = Math.floor(ms / 3600000);
        if (hrs < 1) ago = Math.floor(ms / 60000) + 'm ago';
        else if (hrs < 24) ago = hrs + 'h ago';
        else ago = Math.floor(hrs / 24) + 'd ago';
      }
      html += '<div class="mon-imm-card" onclick="_chartImminentAlert(\'' + escHtml(a.ticker) + '\', \'' + escHtml(a.timeframe || '1d') + '\')">';
      html += '<div>';
      html += '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">';
      html += '<span class="mon-imm-ticker">' + escHtml(a.ticker) + '</span>';
      if (a.timeframe) html += '<span class="mon-tf-pill">' + escHtml(a.timeframe) + '</span>';
      if (a.lifecycle_stage) {
        var lifeColor = a.broker_eligible ? '#86efac' : '#fbbf24';
        var lifeLabel = a.lifecycle_stage === 'shadow_promoted' ? 'observation-only' : a.lifecycle_stage;
        html += '<span style="font-size:9px;font-weight:700;color:' + lifeColor + ';border:1px solid rgba(251,191,36,0.35);border-radius:999px;padding:1px 6px;">' + escHtml(lifeLabel) + '</span>';
      }
      if (a.pattern_name) html += '<span style="font-size:10px;color:var(--text-secondary);">' + escHtml(a.pattern_name) + '</span>';
      html += '</div>';
      html += '<div class="mon-imm-meta">';
      html += '<div>Price<b>$' + (a.price_at_alert != null ? sp(a.price_at_alert) : '—') + '</b></div>';
      if (a.entry_price != null) html += '<div>Entry<b>$' + sp(a.entry_price) + '</b></div>';
      if (a.stop_loss != null) html += '<div>Stop<b style="color:#f87171;">$' + sp(a.stop_loss) + '</b></div>';
      if (a.target_price != null) html += '<div>Target<b style="color:#86efac;">$' + sp(a.target_price) + '</b></div>';
      if (a.entry_edge_expected_net_pct != null) {
        var ev = Number(a.entry_edge_expected_net_pct);
        var evColor = ev > 0 ? '#86efac' : '#f87171';
        html += '<div>EV<b style="color:' + evColor + ';">' + ev.toFixed(2) + '%</b></div>';
      }
      if (a.calibrated_ev_pct != null) {
        var cev = Number(a.calibrated_ev_pct);
        html += '<div>Cal EV<b style="color:' + (cev > 0 ? '#86efac' : '#f87171') + ';">' + cev.toFixed(2) + '%</b></div>';
      }
      if (a.closed_evidence_count != null) {
        html += '<div>Closed<b>' + Number(a.closed_evidence_count || 0).toFixed(0) + '</b></div>';
      }
      html += '</div>';
      if (a.regime) html += '<div style="font-size:9px;color:var(--text-muted);margin-top:2px;">Regime: ' + escHtml(a.regime) + '</div>';
      if (a.autotrader_decision) {
        html += '<div style="font-size:9px;color:var(--text-muted);margin-top:2px;">AutoTrader: ' + escHtml(a.autotrader_decision) + (a.autotrader_reason ? ' / ' + escHtml(a.autotrader_reason) : '') + '</div>';
      }
      if (a.autotrader_blocker_category) {
        html += '<div style="font-size:9px;color:var(--text-muted);margin-top:2px;">' + escHtml(a.autotrader_blocker_category) + (a.autotrader_next_action ? ' -> ' + escHtml(a.autotrader_next_action) : '') + '</div>';
      }
      if (a.graduation_blocker || a.recommended_work_event) {
        html += '<div style="font-size:9px;color:var(--text-muted);margin-top:2px;">Reliability: ' + escHtml(a.graduation_blocker || 'pending') + (a.recommended_work_event ? ' -> ' + escHtml(a.recommended_work_event) : '') + '</div>';
      }
      html += '</div>';
      html += '<div style="text-align:right;">';
      html += '<div class="mon-imm-score ' + scoreClass + '">' + (a.score != null ? Number(a.score).toFixed(0) : '—') + '</div>';
      html += '<div class="mon-imm-time">' + ago + '</div>';
      html += '</div>';
      html += '</div>';
    });
    list.innerHTML = html;
  }).catch(function() {
    list.innerHTML = '<div class="mon-imm-empty">Error loading imminent alerts</div>';
    if (countEl) countEl.textContent = '';
  });
}

function _chartImminentAlert(ticker, timeframe) {
  var isCrypto = ticker.indexOf('-USD') !== -1;
  var targetInterval = timeframe || (isCrypto ? '15m' : '1d');
  currentTicker = ticker.toUpperCase();
  _activeBreakoutRow = null;
  clearAnnotations();
  _loadDrawings();
  document.querySelectorAll('.wl-item').forEach(function(el) { el.classList.toggle('active', el.dataset.ticker === currentTicker); });
  document.getElementById('ticker-label').textContent = currentTicker;
  if (_selectDebounce) clearTimeout(_selectDebounce);
  changeInterval(targetInterval);
  var chartArea = document.getElementById('chart-area');
  if (chartArea) {
    chartArea.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    chartArea.classList.remove('glow'); void chartArea.offsetWidth; chartArea.classList.add('glow');
  }
}

/* ── Monitor auto-refresh ─────────────────────── */
var _monitorRefreshTimer = null;
var _MONITOR_REFRESH_MS = 30000;

function _startMonitorAutoRefresh() {
  _stopMonitorAutoRefresh();
  _monitorRefreshTimer = setInterval(function() {
    if (window.__monitorPayload != null) {
      loadMonitorData(false);
    }
  }, _MONITOR_REFRESH_MS);
}

function _stopMonitorAutoRefresh() {
  if (_monitorRefreshTimer) {
    clearInterval(_monitorRefreshTimer);
    _monitorRefreshTimer = null;
  }
}

/* ── Monitor card → chart interaction ─────────── */
var _selectedMonitorTradeId = null;

function _selectMonitorCard(tradeId) {
  if (!window.__monitorPayload || !window.__monitorPayload.setups) return;
  var setup = null;
  window.__monitorPayload.setups.forEach(function(s) {
    if (s.trade_id === tradeId) setup = s;
  });
  if (!setup) return;

  _selectedMonitorTradeId = tradeId;
  document.querySelectorAll('.mon-card').forEach(function(el) { el.classList.remove('mon-card-selected'); });
  var card = document.getElementById('mon-card-' + tradeId);
  if (card) card.classList.add('mon-card-selected');

  var isCrypto = (setup.ticker || '').indexOf('-USD') !== -1;
  var targetInterval = setup.timeframe || (isCrypto ? '15m' : '1d');

  _pendingAnnotationFn = function(ohlcData) { drawMonitorAnnotations(setup, ohlcData); };
  currentTicker = setup.ticker.toUpperCase();
  _activeBreakoutRow = null;
  clearAnnotations();
  _loadDrawings();
  document.querySelectorAll('.wl-item').forEach(function(el) { el.classList.toggle('active', el.dataset.ticker === currentTicker); });
  document.getElementById('ticker-label').textContent = currentTicker;
  if (_selectDebounce) clearTimeout(_selectDebounce);
  changeInterval(targetInterval);

  var chartArea = document.getElementById('chart-area');
  if (chartArea) {
    chartArea.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    chartArea.classList.remove('glow'); void chartArea.offsetWidth; chartArea.classList.add('glow');
  }
}

function drawMonitorAnnotations(setup, ohlcData) {
  clearAnnotations();
  var cr = (setup.ticker || '').indexOf('-USD') !== -1;
  var sp = function(v) { return smartPrice(v, cr); };
  var legendItems = [];

  if (setup.entry_price != null && isFinite(Number(setup.entry_price))) {
    _addPriceLine(Number(setup.entry_price), '#3b82f6', 'Entry $' + sp(setup.entry_price), LightweightCharts.LineStyle.Solid, 2);
    legendItems.push({ color: '#3b82f6', label: 'Entry $' + sp(setup.entry_price) });
  }
  if (setup.stop_loss != null && isFinite(Number(setup.stop_loss))) {
    _addPriceLine(Number(setup.stop_loss), '#ef4444', 'Stop $' + sp(setup.stop_loss), LightweightCharts.LineStyle.Dotted, 2);
    legendItems.push({ color: '#ef4444', label: 'Stop $' + sp(setup.stop_loss), dashed: true });
  }
  if (setup.take_profit != null && isFinite(Number(setup.take_profit))) {
    _addPriceLine(Number(setup.take_profit), '#22c55e', 'Target $' + sp(setup.take_profit), LightweightCharts.LineStyle.Dashed, 2);
    legendItems.push({ color: '#22c55e', label: 'Target $' + sp(setup.take_profit), dashed: true });
  }

  var ld = setup.latest_decision;
  if (ld && ld.conditions_snapshot) {
    var snap = ld.conditions_snapshot;
    if (snap.nearest_support != null && isFinite(Number(snap.nearest_support))) {
      _addPriceLine(Number(snap.nearest_support), '#06b6d4', (snap.nearest_support_label || 'Support') + ' $' + sp(snap.nearest_support), LightweightCharts.LineStyle.Dashed, 1);
      legendItems.push({ color: '#06b6d4', label: (snap.nearest_support_label || 'Support') + ' $' + sp(snap.nearest_support), dashed: true });
    }
    if (snap.atr_snapshot != null && isFinite(Number(snap.atr_snapshot))) {
      legendItems.push({ color: '#a855f7', label: 'ATR ' + sp(snap.atr_snapshot) });
    }
  }

  if (ld && ld.new_stop != null && ld.old_stop != null && Number(ld.new_stop) !== Number(ld.old_stop) && isFinite(Number(ld.old_stop))) {
    _addPriceLine(Number(ld.old_stop), '#f87171', 'Prev Stop $' + sp(ld.old_stop), LightweightCharts.LineStyle.SparseDotted, 1);
    legendItems.push({ color: '#f87171', label: 'Prev Stop $' + sp(ld.old_stop), dashed: true });
  }
  if (ld && ld.new_target != null && ld.old_target != null && Number(ld.new_target) !== Number(ld.old_target) && isFinite(Number(ld.old_target))) {
    _addPriceLine(Number(ld.old_target), '#86efac', 'Prev Target $' + sp(ld.old_target), LightweightCharts.LineStyle.SparseDotted, 1);
    legendItems.push({ color: '#86efac', label: 'Prev Target $' + sp(ld.old_target), dashed: true });
  }

  var markers = [];
  var decisions = setup.recent_decisions || [];
  var actionColors = {
    hold: '#6b7280', tighten_stop: '#eab308', loosen_target: '#3b82f6', exit_now: '#ef4444'
  };
  if (decisions.length && ohlcData && ohlcData.length && _chartBarTimes && _chartBarTimes.length) {
    var actionShapes = {
      hold: 'circle', tighten_stop: 'arrowDown', loosen_target: 'arrowUp', exit_now: 'arrowDown'
    };
    decisions.slice().reverse().forEach(function(dec) {
      if (!dec.created_at) return;
      var ts = new Date(dec.created_at).getTime() / 1000;
      var barTime = _getNearestBarTime(ts);
      if (barTime == null) return;
      var act = (dec.action || 'hold').toLowerCase();
      var hp = _monitorHealthPct(dec);
      var hLabel = hp != null ? ' h=' + Math.round(hp) : '';
      markers.push({
        time: barTime,
        position: (act === 'exit_now' || act === 'tighten_stop') ? 'aboveBar' : 'belowBar',
        color: actionColors[act] || '#6b7280',
        shape: actionShapes[act] || 'circle',
        text: act.replace(/_/g, ' ') + hLabel,
      });
    });
  }
  if (markers.length) {
    markers.sort(function(a, b) { return a.time - b.time; });
    try { candleSeries.setMarkers(markers); } catch(e) {}
  }

  if (ld && ld.action) {
    var actLabel = ld.action.replace(/_/g, ' ');
    var srcLabel = ld.decision_source ? _monitorDecisionSourceLabel(ld.decision_source) : '';
    legendItems.push({ color: actionColors[ld.action] || '#6b7280', label: 'Latest: ' + actLabel + (srcLabel ? ' (' + srcLabel + ')' : '') });
  }

  if (legendItems.length) _showAnnotationLegend(legendItems);
}

/* ── AI Position Plans ─────────────────────────── */
window.__positionPlans = null;

function _getPlanByTicker(ticker) {
  if (!window.__positionPlans || !window.__positionPlans.position_plans) return null;
  var t = (ticker || '').toUpperCase();
  var plans = window.__positionPlans.position_plans;
  for (var i = 0; i < plans.length; i++) {
    if ((plans[i].ticker || '').toUpperCase() === t) return plans[i];
  }
  return null;
}

function _getPlanByTradeId(tradeId) {
  if (!window.__positionPlans || !window.__positionPlans.position_plans) return null;
  var plans = window.__positionPlans.position_plans;
  for (var i = 0; i < plans.length; i++) {
    if (plans[i].trade_id === tradeId) return plans[i];
  }
  return null;
}

function evaluateAllPositions() {
  if (CHILI_TRADING_IS_GUEST) return;
  var btn = document.getElementById('monitor-eval-btn');
  var st = document.getElementById('monitor-status');
  if (btn) { btn.disabled = true; btn.classList.add('loading'); btn.textContent = 'Evaluating…'; }
  if (st) { st.style.display = 'block'; st.className = 'mon-status ok'; st.textContent = 'AI is evaluating all open positions…'; }

  fetch('/api/trading/brain/evaluate-positions?force=true', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (btn) { btn.disabled = false; btn.classList.remove('loading'); btn.textContent = 'AI Evaluate All'; }
      if (!d.ok) {
        if (st) { st.className = 'mon-status err'; st.textContent = d.error || 'Evaluation failed'; }
        return;
      }
      if (st) {
        var n = (d.position_plans || []).length;
        st.className = 'mon-status ok';
        st.textContent = 'AI evaluated ' + n + ' position' + (n !== 1 ? 's' : '');
        setTimeout(function() { st.style.display = 'none'; }, 4000);
      }
      window.__positionPlans = d;
      _showPortfolioBanner(d);
      _reRenderMonitorWithPlans();
    }).catch(function() {
      if (btn) { btn.disabled = false; btn.classList.remove('loading'); btn.textContent = 'AI Evaluate All'; }
      if (st) { st.className = 'mon-status err'; st.textContent = 'Request failed'; }
    });
}

function _loadCachedPositionPlans() {
  fetch('/api/trading/brain/position-plans')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.ok && d.position_plans && d.position_plans.length) {
        window.__positionPlans = d;
        _showPortfolioBanner(d);
        _reRenderMonitorWithPlans();
      }
    }).catch(function() {});
}

function _showPortfolioBanner(data) {
  var banner = document.getElementById('plans-portfolio-banner');
  if (!banner) return;
  var pSum = data.portfolio_summary || {};
  if (!pSum.overall_assessment) { banner.style.display = 'none'; return; }
  banner.style.display = 'block';
  var heatClass = 'plan-heat-' + (pSum.portfolio_heat || 'low').toLowerCase();
  var genAt = data.generated_at ? new Date(data.generated_at).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
  var html = '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">';
  html += '<span class="plan-heat ' + heatClass + '">' + escHtml(pSum.portfolio_heat || 'low') + '</span>';
  html += '<span style="font-weight:700;font-size:11px;">' + (pSum.total_positions || 0) + ' positions</span>';
  html += '<span style="color:var(--text-muted);font-size:10px;">Regime: ' + escHtml(pSum.regime || 'unknown') + '</span>';
  html += '<span style="flex:1;"></span>';
  if (genAt) html += '<span style="font-size:9px;color:var(--text-muted);">' + genAt + (data.stale ? ' (stale)' : '') + '</span>';
  html += '</div>';
  html += '<div style="font-size:10px;margin-top:4px;color:var(--text-secondary);">' + escHtml(pSum.overall_assessment) + '</div>';
  if (pSum.concentration_warnings && pSum.concentration_warnings.length) {
    html += '<div style="margin-top:3px;color:#f59e0b;font-size:9px;">⚠ ' + pSum.concentration_warnings.map(escHtml).join(' · ') + '</div>';
  }
  banner.innerHTML = html;
}

function _reRenderMonitorWithPlans() {
  if (!window.__monitorPayload || !window.__monitorPayload.setups) return;
  var mode = (document.getElementById('monitor-sort') || {}).value || 'health';
  renderMonitorCards(_monitorSortSetups(window.__monitorPayload.setups, mode));
}

function loadStopDecisions() {
  fetch('/api/trading/stops/decisions?limit=30').then(function(r){return r.json();}).then(function(d) {
    var list = document.getElementById('stop-decisions-list');
    if (!d.ok || !d.decisions || !d.decisions.length) {
      list.innerHTML = '<div style="font-size:10px;color:var(--text-muted);padding:8px;text-align:center;">No stop decisions yet.</div>';
      return;
    }
    var html = '';
    d.decisions.forEach(function(sd) {
      var tc = {'STOP_HIT':'#ef4444','TARGET_HIT':'#22c55e','STOP_APPROACHING':'#f59e0b','BREAKEVEN_REACHED':'#22c55e','STOP_TIGHTENED':'#3b82f6'}[sd.trigger] || '#888';
      var time = sd.as_of_ts ? new Date(sd.as_of_ts).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
      html += '<div style="display:flex;gap:6px;align-items:center;padding:3px 0;border-bottom:1px solid var(--border);font-size:10px;">';
      html += '<span style="padding:1px 5px;border-radius:4px;font-size:8px;font-weight:700;background:'+tc+'22;color:'+tc+';">'+(sd.trigger||sd.state)+'</span>';
      html += '<span style="font-weight:600;">Trade #'+sd.trade_id+'</span>';
      if(sd.old_stop && sd.new_stop && sd.old_stop !== sd.new_stop) html += '<span>$'+sd.old_stop.toFixed(2)+' → $'+sd.new_stop.toFixed(2)+'</span>';
      html += '<span style="flex:1;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'+escHtml(sd.reason||'')+'</span>';
      html += '<span style="color:var(--text-muted);font-size:9px;">'+time+'</span>';
      html += '</div>';
    });
    list.innerHTML = html;
  }).catch(function() {});
}

function runStopEvaluate() {
  var result = document.getElementById('stop-evaluate-result');
  result.style.display = 'block';
  result.textContent = 'Running stop check...';
  result.style.color = 'var(--text-muted)';
  fetch('/api/trading/stops/evaluate', {method:'POST'}).then(function(r){return r.json();}).then(function(d) {
    if (d.ok) {
      var parts = [];
      parts.push('Checked: '+d.total_checked);
      if (d.stops_hit) parts.push('Stops hit: '+d.stops_hit);
      if (d.targets_hit) parts.push('Targets hit: '+d.targets_hit);
      if (d.stops_tightened) parts.push('Tightened: '+d.stops_tightened);
      if (d.breakevens) parts.push('Break-even: '+d.breakevens);
      if (d.warnings) parts.push('Warnings: '+d.warnings);
      result.textContent = parts.join(' | ');
      result.style.color = d.stops_hit ? '#ef4444' : '#22c55e';
      loadStopPositions();
      loadStopDecisions();
    } else {
      result.textContent = 'Failed';
      result.style.color = '#ef4444';
    }
  }).catch(function() { result.textContent = 'Error'; result.style.color = '#ef4444'; });
}

/* ── Portfolio ─────────────────────────────────── */
function loadPortfolio() {
  fetch('/api/trading/portfolio').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var grid = document.getElementById('portfolio-grid');
    var totalCls = d.total_pnl >= 0 ? 'pos' : 'neg';
    var unrealCls = d.unrealized_pnl >= 0 ? 'pos' : 'neg';
    var realCls = d.realized_pnl >= 0 ? 'pos' : 'neg';
    grid.innerHTML =
      '<div class="p-card"><div class="p-val '+totalCls+'">$'+d.total_pnl+'</div><div class="p-lbl">Total P&L</div></div>' +
      '<div class="p-card"><div class="p-val '+realCls+'">$'+d.realized_pnl+'</div><div class="p-lbl">Realized</div></div>' +
      '<div class="p-card"><div class="p-val '+unrealCls+'">$'+d.unrealized_pnl+'</div><div class="p-lbl">Unrealized</div></div>' +
      '<div class="p-card"><div class="p-val">'+d.position_count+'</div><div class="p-lbl">Open Positions</div></div>' +
      '<div class="p-card"><div class="p-val">$'+d.total_invested+'</div><div class="p-lbl">Invested</div></div>' +
      '<div class="p-card"><div class="p-val">$'+d.total_current+'</div><div class="p-lbl">Current Value</div></div>';

    var posEl = document.getElementById('portfolio-positions');
    if (d.positions && d.positions.length) {
      var html = '<table class="pos-table"><thead><tr><th>Ticker</th><th>Dir</th><th>Entry</th><th>Current</th><th>Qty</th><th>P&L</th><th>%</th></tr></thead><tbody>';
      d.positions.forEach(function(p) {
        var cls = p.unrealized_pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
        html += '<tr><td>'+p.ticker+'</td><td>'+p.direction+'</td><td>$'+p.entry_price+'</td><td>$'+p.current_price+'</td><td>'+p.quantity+'</td><td class="'+cls+'">$'+p.unrealized_pnl+'</td><td class="'+cls+'">'+p.unrealized_pct+'%</td></tr>';
      });
      html += '</tbody></table>';
      posEl.innerHTML = html;
    } else {
      posEl.innerHTML = '<div style="color:var(--text-muted);font-size:12px;margin-top:8px">No open positions</div>';
    }
  });
}

/* ── Backtest ──────────────────────────────────── */
window._btStrategyCatalog = { builtins: [], brain: [] };

function loadStrategies() {
  return Promise.all([
    fetch('/api/trading/backtest/strategies').then(function(r) { return r.json(); }),
    fetch('/api/trading/learn/patterns').then(function(r) { return r.json(); }).catch(function() { return { ok: false }; }),
  ]).then(function(pair) {
    var d = pair[0];
    var lp = pair[1];
    var sel = document.getElementById('bt-strategy');
    if (!sel) return Promise.resolve();
    sel.innerHTML = '';
    window._btStrategyCatalog.builtins = d.ok ? d.strategies : [];
    if (d.ok) {
      var ogB = document.createElement('optgroup');
      ogB.label = 'Built-in';
      d.strategies.forEach(function(s) {
        var opt = document.createElement('option');
        opt.value = s.id;
        opt.textContent = s.name;
        opt.title = s.description || '';
        ogB.appendChild(opt);
      });
      sel.appendChild(ogB);
    }
    window._btStrategyCatalog.brain = [];
    if (lp && lp.ok && lp.active && lp.active.length) {
      var ogC = document.createElement('optgroup');
      ogC.label = 'CHILI Brain';
      lp.active.forEach(function(p) {
        var opt = document.createElement('option');
        opt.value = 'insight:' + p.id;
        var label = (p.pattern_display || p.pattern || '').replace(/\s+/g, ' ').trim();
        if (label.length > 72) label = label.slice(0, 69) + '...';
        opt.textContent = label || ('Pattern #' + p.id);
        opt.title = (p.pattern || '') + ' — backtest uses insight id; server resolves pattern.';
        ogC.appendChild(opt);
        window._btStrategyCatalog.brain.push(p);
      });
      sel.appendChild(ogC);
    }
    sel.removeEventListener('change', _onBtStrategyChange);
    sel.addEventListener('change', _onBtStrategyChange);
    renderBtStrategyParams();
  });
}

function _onBtStrategyChange() {
  renderBtStrategyParams();
  var _v = document.getElementById('bt-strategy').value;
  if (_v.indexOf('insight:') === 0 || _v.indexOf('scan_pattern:') === 0) {
    var rows = document.getElementById('bt-conditions-rows');
    if (rows && rows.children.length === 0) btAddConditionRow();
  }
}

var _btConditionIndicators = [
  'rsi_14', 'ema_9', 'ema_20', 'ema_50', 'ema_100', 'sma_20', 'sma_50', 'sma_100',
  'bb_squeeze', 'bb_upper', 'bb_lower', 'macd_hist', 'adx', 'price', 'rel_vol', 'gap_pct'
];
var _btConditionOps = ['<', '<=', '==', '>=', '>', '!='];

function btAddConditionRow() {
  var container = document.getElementById('bt-conditions-rows');
  if (!container) return;
  var idx = container.children.length;
  var row = document.createElement('div');
  row.className = 'bt-condition-row';
  row.style.cssText = 'display:flex;align-items:center;gap:6px;margin-bottom:6px;flex-wrap:wrap';
  var selInd = document.createElement('select');
  selInd.setAttribute('data-bt-cond', 'indicator');
  selInd.style.fontSize = '11px';
  _btConditionIndicators.forEach(function(o) {
    var opt = document.createElement('option');
    opt.value = o;
    opt.textContent = o;
    selInd.appendChild(opt);
  });
  var selOp = document.createElement('select');
  selOp.setAttribute('data-bt-cond', 'op');
  selOp.style.fontSize = '11px';
  _btConditionOps.forEach(function(o) {
    var opt = document.createElement('option');
    opt.value = o;
    opt.textContent = o;
    selOp.appendChild(opt);
  });
  var inpVal = document.createElement('input');
  inpVal.type = 'text';
  inpVal.setAttribute('data-bt-cond', 'value');
  inpVal.placeholder = 'e.g. 35 or 70';
  inpVal.style.cssText = 'width:80px;font-size:11px;padding:2px 4px';
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = 'Remove';
  btn.style.cssText = 'font-size:10px;padding:2px 6px;cursor:pointer';
  btn.onclick = function() { row.remove(); };
  row.appendChild(selInd);
  row.appendChild(selOp);
  row.appendChild(inpVal);
  row.appendChild(btn);
  container.appendChild(row);
}

function _collectBtAppendConditionsFromForm() {
  var rows = document.getElementById('bt-conditions-rows');
  if (!rows || !rows.children.length) return null;
  var list = [];
  for (var i = 0; i < rows.children.length; i++) {
    var r = rows.children[i];
    var ind = r.querySelector('[data-bt-cond="indicator"]');
    var op = r.querySelector('[data-bt-cond="op"]');
    var val = r.querySelector('[data-bt-cond="value"]');
    if (!ind || !op || !val) continue;
    var v = val.value.trim();
    if (!v) continue;
    var num = parseFloat(v);
    var value = isNaN(num) ? v : (num === parseInt(v, 10) ? parseInt(v, 10) : num);
    list.push({ indicator: ind.value, op: op.value, value: value });
  }
  return list.length ? list : null;
}

function _collectBtExitConfigFromForm() {
  var atr = document.getElementById('bt-exit-atr');
  var bars = document.getElementById('bt-exit-max-bars');
  var bos = document.getElementById('bt-exit-use-bos');
  if (!atr && !bars && !bos) return null;
  var cfg = {};
  if (atr && atr.value.trim() !== '') { var n = parseFloat(atr.value); if (!isNaN(n)) cfg.atr_mult = n; }
  if (bars && bars.value.trim() !== '') { var n = parseInt(bars.value, 10); if (!isNaN(n)) cfg.max_bars = n; }
  if (bos) cfg.use_bos = bos.checked;
  return Object.keys(cfg).length ? cfg : null;
}

function renderBtStrategyParams() {
  var sel = document.getElementById('bt-strategy');
  var container = document.getElementById('bt-strategy-params');
  var adv = document.getElementById('bt-advanced');
  if (!sel || !container) return;
  var val = sel.value || '';
  container.innerHTML = '';
  if (val.indexOf('insight:') === 0) {
    if (adv) adv.style.display = 'block';
    var condRows = document.getElementById('bt-conditions-rows');
    if (condRows && condRows.children.length === 0) btAddConditionRow();
    return;
  }
  if (adv) adv.style.display = 'none';
  var meta = (window._btStrategyCatalog.builtins || []).find(function(s) { return s.id === val; });
  if (!meta || !meta.tunables || !meta.tunables.length) return;
  var row = document.createElement('div');
  row.className = 'bt-param-row';
  row.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;align-items:center;';
  meta.tunables.forEach(function(t) {
    var lab = document.createElement('label');
    lab.style.cssText = 'font-size:11px;color:var(--text-secondary);display:inline-flex;align-items:center;gap:4px;';
    lab.textContent = t.name + ': ';
    var inp = document.createElement('input');
    inp.type = 'number';
    inp.setAttribute('data-param', t.name);
    inp.step = t.step != null ? t.step : 1;
    if (t.min != null) inp.min = t.min;
    if (t.max != null) inp.max = t.max;
    inp.value = t.default != null ? t.default : '';
    inp.style.width = '76px';
    lab.appendChild(inp);
    row.appendChild(lab);
  });
  container.appendChild(row);
}

function _collectBuiltinStrategyParams() {
  var params = {};
  var container = document.getElementById('bt-strategy-params');
  if (!container) return params;
  container.querySelectorAll('input[data-param]').forEach(function(inp) {
    var k = inp.getAttribute('data-param');
    if (!k) return;
    var v = parseFloat(inp.value);
    if (!isNaN(v)) params[k] = v;
  });
  return params;
}

var _btIndicatorChart = null;
var _btIndicatorSeries = [];

var _indicatorColors = [
  '#f59e0b', '#3b82f6', '#22c55e', '#ef4444', '#a855f7', '#ec4899', '#14b8a6', '#f97316'
];

function runBacktest() {
  var strategyEl = document.getElementById('bt-strategy');
  var strategy = strategyEl ? strategyEl.value : 'sma_cross';
  var period = document.getElementById('bt-period').value;
  var cash = parseFloat(document.getElementById('bt-cash').value) || 10000;
  var results = document.getElementById('bt-results');
  results.innerHTML = '<em>Running backtest on ' + currentTicker + '...</em>';
  document.getElementById('bt-chart-area').innerHTML = '';
  document.getElementById('bt-indicator-pane').innerHTML = '';
  document.getElementById('bt-legend').innerHTML = '';
  document.getElementById('bt-trade-log').innerHTML = '';

  function _applyResult(d) {
    if (!d.ok) { results.innerHTML = '<em style="color:#ef4444">'+escHtml(d.error||'Backtest failed')+'</em>'; return; }
    var retCls = d.return_pct >= 0 ? 'pnl-pos' : 'pnl-neg';
    var bhCls = d.buy_hold_pct >= 0 ? 'pnl-pos' : 'pnl-neg';
    var stratLine = d.strategy + ' on ' + d.ticker + ' (' + d.period + ')';
    if (d.mapped_strategy) stratLine += ' <span style="color:var(--text-muted);font-weight:400">[' + d.mapped_strategy + ']</span>';
    results.innerHTML =
      '<div style="font-size:13px;font-weight:600;margin-bottom:6px">' + stratLine + '</div>' +
      '<div class="bt-stats">' +
        '<div class="bt-stat"><div class="bt-val '+retCls+'">'+d.return_pct+'%</div><div class="bt-lbl">Strategy Return</div></div>' +
        '<div class="bt-stat"><div class="bt-val '+bhCls+'">'+d.buy_hold_pct+'%</div><div class="bt-lbl">Buy & Hold</div></div>' +
        '<div class="bt-stat"><div class="bt-val">'+(function(){ var w = tradingBacktestWinRateToPct(d.win_rate); return w != null ? w.toFixed(1) : (d.win_rate != null ? String(d.win_rate) : '--'); })()+'%</div><div class="bt-lbl">Win Rate</div></div>' +
        '<div class="bt-stat"><div class="bt-val">'+(d.sharpe||'-')+'</div><div class="bt-lbl">Sharpe Ratio</div></div>' +
        '<div class="bt-stat"><div class="bt-val" style="color:#ef4444">'+d.max_drawdown+'%</div><div class="bt-lbl">Max Drawdown</div></div>' +
        '<div class="bt-stat"><div class="bt-val">'+d.trade_count+'</div><div class="bt-lbl">Total Trades</div></div>' +
        '<div class="bt-stat"><div class="bt-val">$'+d.final_equity+'</div><div class="bt-lbl">Final Equity</div></div>' +
        (d.profit_factor ? '<div class="bt-stat"><div class="bt-val">'+d.profit_factor+'</div><div class="bt-lbl">Profit Factor</div></div>' : '') +
      '</div>';
    drawBacktestChart(d);
    renderTradeLog(d.trades || []);
    switchTab(document.querySelector('[data-tab="backtest"]'));
  }

  if (strategy.indexOf('insight:') === 0 || strategy.indexOf('scan_pattern:') === 0) {
    var pid = strategy.indexOf('insight:') === 0 ? strategy.slice('insight:'.length) : strategy.slice('scan_pattern:'.length);
    var body = { ticker: currentTicker, period: period, interval: '1d', cash: cash, commission: 0.001 };
    var appendFromForm = _collectBtAppendConditionsFromForm();
    if (appendFromForm) {
      body.append_conditions = appendFromForm;
    } else {
      var appendEl = document.getElementById('bt-append-conditions');
      if (appendEl && appendEl.value.trim()) {
        try { body.append_conditions = JSON.parse(appendEl.value.trim()); } catch (err) {
          results.innerHTML = '<em style="color:#ef4444">Append conditions JSON error: ' + escHtml(String(err.message || err)) + '</em>';
          return;
        }
      }
    }
    var exitFromForm = _collectBtExitConfigFromForm();
    if (exitFromForm) {
      body.exit_config = exitFromForm;
    }
    var exitEl = document.getElementById('bt-exit-config');
    if (exitEl && exitEl.value.trim()) {
      try {
        var exitJson = JSON.parse(exitEl.value.trim());
        body.exit_config = body.exit_config ? Object.assign({}, body.exit_config, exitJson) : exitJson;
      } catch (err) {
        results.innerHTML = '<em style="color:#ef4444">Exit config JSON error: ' + escHtml(String(err.message || err)) + '</em>';
        return;
      }
    }
    var rulesEl = document.getElementById('bt-rules-override');
    if (rulesEl && rulesEl.value.trim()) body.rules_json_override = rulesEl.value.trim();
    fetch('/api/trading/patterns/' + encodeURIComponent(pid) + '/backtest?ticker=' + encodeURIComponent(currentTicker) + '&period=' + encodeURIComponent(period) + '&interval=1d', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function(r) { return r.json(); }).then(_applyResult).catch(function() { results.innerHTML = '<em style="color:#ef4444">Error running backtest</em>'; });
    return;
  }

  var sp = _collectBuiltinStrategyParams();
  var payload = {
    ticker: currentTicker,
    strategy: strategy,
    period: period,
    interval: '1d',
    cash: cash,
    commission: 0.001,
    strategy_params: Object.keys(sp).length ? sp : null,
  };
  fetch('/api/trading/backtest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(function(r) { return r.json(); }).then(_applyResult).catch(function() { results.innerHTML = '<em style="color:#ef4444">Error running backtest</em>'; });
}

function drawBacktestChart(d) {
  var el = document.getElementById('bt-chart-area');
  var indPane = document.getElementById('bt-indicator-pane');
  var legendEl = document.getElementById('bt-legend');
  el.innerHTML = '';
  indPane.innerHTML = '';
  legendEl.innerHTML = '';
  el.style.height = '360px';
  var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  var bgColor = isDark ? '#111827' : '#f9fafb';
  var textColor = isDark ? '#9ca3af' : '#6b7280';
  var gridColor = isDark ? '#1f2937' : '#f3f4f6';

  if (btChart) { try { btChart.remove(); } catch(e){} btChart = null; }
  if (_btIndicatorChart) { try { _btIndicatorChart.remove(); } catch(e){} _btIndicatorChart = null; }

  var ohlc = d.ohlc || [];
  var trades = d.trades || [];
  var indicators = d.indicators || {};
  var indNames = Object.keys(indicators);

  function _isRsiName(n) { return n.indexOf('RSI ') === 0; }
  var rsiKey = indNames.find(_isRsiName);
  var hasSeparatePane = indNames.some(function(n) {
    return _isRsiName(n) || n === 'MACD' || n === 'Signal' || n === 'Histogram';
  });
  var overlayIndicators = {};
  var paneIndicators = {};
  indNames.forEach(function(name) {
    if (_isRsiName(name) || name === 'MACD' || name === 'Signal' || name === 'Histogram') {
      paneIndicators[name] = indicators[name];
    } else {
      overlayIndicators[name] = indicators[name];
    }
  });

  if (!hasSeparatePane) { indPane.style.display = 'none'; }
  else { indPane.style.display = 'block'; indPane.style.height = '120px'; }

  // Main candlestick chart
  btChart = LightweightCharts.createChart(el, {
    width: el.clientWidth, height: 360,
    layout: { background: {type:'solid', color: bgColor}, textColor: textColor },
    grid: { vertLines: {visible:false}, horzLines: {color: gridColor} },
    rightPriceScale: { borderVisible: false },
    timeScale: { borderVisible: false, timeVisible: true },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });

  var btCandleSeries = null;
  if (ohlc.length > 0) {
    btCandleSeries = btChart.addCandlestickSeries({
      upColor: '#22c55e', downColor: '#ef4444',
      borderUpColor: '#22c55e', borderDownColor: '#ef4444',
      wickUpColor: '#22c55e88', wickDownColor: '#ef444488',
    });
    btCandleSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.15, bottom: 0.1 },
    });
    btCandleSeries.setData(ohlc);

    // Trade markers - set immediately after candle data
    if (trades.length > 0) {
      var ohlcTimeSet = {};
      ohlc.forEach(function(bar) { ohlcTimeSet[bar.time] = true; });

      var markers = [];
      trades.forEach(function(t, idx) {
        if (t.entry_time != null && ohlcTimeSet[t.entry_time]) {
          markers.push({
            time: t.entry_time,
            position: 'belowBar',
            color: '#2563eb',
            shape: 'arrowUp',
            text: 'BUY $' + t.entry_price,
            size: 2,
          });
        }
        if (t.exit_time != null && ohlcTimeSet[t.exit_time]) {
          var isWin = t.pnl >= 0;
          markers.push({
            time: t.exit_time,
            position: 'aboveBar',
            color: isWin ? '#16a34a' : '#dc2626',
            shape: 'arrowDown',
            text: 'SELL ' + (isWin ? '+' : '') + '$' + t.pnl.toFixed(0) + ' (' + (isWin ? '+' : '') + t.return_pct.toFixed(1) + '%)',
            size: 2,
          });
        }
      });
      markers.sort(function(a, b) { return a.time - b.time; });
      console.log('[BT] markers:', markers.length, '(buys:', markers.filter(function(m){return m.shape==='arrowUp';}).length, ', sells:', markers.filter(function(m){return m.shape==='arrowDown';}).length + ')', markers);
      if (markers.length > 0) {
        btCandleSeries.setMarkers(markers);
        var verify = btCandleSeries.markers();
        console.log('[BT] markers stored on series:', verify ? verify.length : 'markers() unavailable');
      }
    }
  } else if (d.equity_curve && d.equity_curve.length > 1) {
  btEquitySeries = btChart.addAreaSeries({
    lineColor: '#3b82f6', topColor: 'rgba(59,130,246,.3)', bottomColor: 'rgba(59,130,246,.02)',
    lineWidth: 2,
  });
    btEquitySeries.setData(d.equity_curve);
  }

  // Overlay indicator lines on the price chart
  var legendHtml = '';
  var colorIdx = 0;
  var overlayNames = Object.keys(overlayIndicators);
  overlayNames.forEach(function(name) {
    var color = _indicatorColors[colorIdx % _indicatorColors.length];
    colorIdx++;
    var lineSeries = btChart.addLineSeries({
      color: color, lineWidth: 1, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false,
    });
    lineSeries.setData(overlayIndicators[name]);
    legendHtml += '<div class="bt-legend-item"><span class="bt-legend-swatch" style="background:'+color+'"></span>'+name+'</div>';
  });

  btChart.timeScale().fitContent();
  new ResizeObserver(function() { btChart.applyOptions({width: el.clientWidth}); }).observe(el);

  // Separate indicator pane (RSI / MACD)
  if (hasSeparatePane) {
    _btIndicatorChart = LightweightCharts.createChart(indPane, {
      width: indPane.clientWidth, height: 120,
      layout: { background: {type:'solid', color: bgColor}, textColor: textColor },
      grid: { vertLines: {visible:false}, horzLines: {color: gridColor} },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: false },
    });

    var paneNames = Object.keys(paneIndicators);
    paneNames.forEach(function(name) {
      var color = _indicatorColors[colorIdx % _indicatorColors.length];
      colorIdx++;
      if (name === 'Histogram') {
        var histSeries = _btIndicatorChart.addHistogramSeries({
          color: '#3b82f680', priceLineVisible: false, lastValueVisible: false,
        });
        var histData = paneIndicators[name].map(function(p) {
          return { time: p.time, value: p.value, color: p.value >= 0 ? '#22c55e80' : '#ef444480' };
        });
        histSeries.setData(histData);
        legendHtml += '<div class="bt-legend-item"><span class="bt-legend-swatch" style="background:#3b82f680"></span>'+name+'</div>';
      } else {
        var ls = _btIndicatorChart.addLineSeries({
          color: color, lineWidth: 1, priceLineVisible: false,
          lastValueVisible: false,
        });
        ls.setData(paneIndicators[name]);
        legendHtml += '<div class="bt-legend-item"><span class="bt-legend-swatch" style="background:'+color+'"></span>'+name+'</div>';
      }
    });

    // RSI reference lines (any period)
    var _rsiSeriesKey = rsiKey && paneIndicators[rsiKey] ? rsiKey : indNames.find(_isRsiName);
    if (_rsiSeriesKey && paneIndicators[_rsiSeriesKey]) {
      var rsiData = paneIndicators[_rsiSeriesKey];
      if (rsiData.length > 0) {
        var ref70 = rsiData.map(function(p){ return {time:p.time, value:70}; });
        var ref30 = rsiData.map(function(p){ return {time:p.time, value:30}; });
        _btIndicatorChart.addLineSeries({
          color: '#ef444466', lineWidth: 1, lineStyle: 2,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        }).setData(ref70);
        _btIndicatorChart.addLineSeries({
          color: '#22c55e66', lineWidth: 1, lineStyle: 2,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        }).setData(ref30);
      }
    }

    // Zero line for MACD
    if (paneIndicators['MACD']) {
      var macdData = paneIndicators['MACD'];
      if (macdData.length > 0) {
        var zeroLine = macdData.map(function(p){ return {time:p.time, value:0}; });
        _btIndicatorChart.addLineSeries({
          color: '#6b728044', lineWidth: 1, lineStyle: 2,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        }).setData(zeroLine);
      }
    }

    _btIndicatorChart.timeScale().fitContent();
    // Sync scrolling between main chart and indicator pane
    btChart.timeScale().subscribeVisibleLogicalRangeChange(function(range) {
      if (range && _btIndicatorChart) _btIndicatorChart.timeScale().setVisibleLogicalRange(range);
    });
    _btIndicatorChart.timeScale().subscribeVisibleLogicalRangeChange(function(range) {
      if (range && btChart) btChart.timeScale().setVisibleLogicalRange(range);
    });

    new ResizeObserver(function() {
      if (_btIndicatorChart) _btIndicatorChart.applyOptions({width: indPane.clientWidth});
    }).observe(indPane);
  }

  legendEl.innerHTML = '<div class="bt-legend">' + legendHtml + '</div>';
}

function renderTradeLog(trades) {
  var el = document.getElementById('bt-trade-log');
  if (!trades || trades.length === 0) { el.innerHTML = ''; return; }

  var html = '<table class="bt-trade-log"><thead><tr>' +
    '<th>#</th><th>Entry Date</th><th>Entry $</th><th>Exit Date</th><th>Exit $</th><th>Size</th><th>P&L</th><th>Return</th>' +
    '</tr></thead><tbody>';

  trades.forEach(function(t, i) {
    var isWin = t.pnl >= 0;
    var cls = isWin ? 'tl-win' : 'tl-loss';
    var entryDate = t.entry_time ? new Date(t.entry_time * 1000).toLocaleDateString() : '-';
    var exitDate = t.exit_time ? new Date(t.exit_time * 1000).toLocaleDateString() : '-';
    html += '<tr>' +
      '<td>' + (i + 1) + '</td>' +
      '<td>' + entryDate + '</td>' +
      '<td>$' + t.entry_price + '</td>' +
      '<td>' + exitDate + '</td>' +
      '<td>$' + t.exit_price + '</td>' +
      '<td>' + t.size + '</td>' +
      '<td class="' + cls + '">' + (isWin ? '+' : '') + '$' + t.pnl.toFixed(2) + '</td>' +
      '<td class="' + cls + '">' + (isWin ? '+' : '') + t.return_pct.toFixed(2) + '%</td>' +
      '</tr>';
  });

  html += '</tbody></table>';
  el.innerHTML = html;
}

/* ── Broker (Robinhood) ────────────────────────────── */
var _brokerConnected = false;

function signOut() {
  fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' })
    .then(function() { window.location.href = '/trading'; })
    .catch(function() { window.location.href = '/trading'; });
}

function loadBrokerStatus() {
  fetch('/api/trading/broker/status', { credentials: 'same-origin' }).then(function(r){return r.json();}).then(function(d) {
    if (!d.ok || !d.brokers) return;
    var anyConnected = false;

    var rh = d.brokers.robinhood || {};
    var dotRh = document.getElementById('broker-dot-rh');
    var subRh = document.getElementById('broker-sub-rh');
    if (rh.connected) {
      anyConnected = true;
      if (dotRh) dotRh.className = 'broker-dot connected';
      if (subRh) subRh.textContent = rh.username || 'Connected';
    } else if (rh.has_credentials || rh.configured) {
      if (dotRh) dotRh.className = 'broker-dot disconnected';
      if (subRh) subRh.textContent = 'Click to connect';
    } else {
      if (dotRh) dotRh.className = 'broker-dot disconnected';
      if (subRh) subRh.textContent = 'Click to set up';
    }

    var cb = d.brokers.coinbase || {};
    var dotCb = document.getElementById('broker-dot-cb');
    var subCb = document.getElementById('broker-sub-cb');
    if (cb.connected) {
      anyConnected = true;
      if (dotCb) dotCb.className = 'broker-dot connected';
      if (subCb) subCb.textContent = 'Connected';
    } else if (cb.has_credentials || cb.configured) {
      if (dotCb) dotCb.className = 'broker-dot disconnected';
      if (subCb) subCb.textContent = 'Click to connect';
    } else {
      if (dotCb) dotCb.className = 'broker-dot disconnected';
      if (subCb) subCb.textContent = 'Click to set up';
    }

    _brokerConnected = anyConnected;
    var syncAll = document.getElementById('broker-sync-all');
    var tradesTabSyncBtn = document.getElementById('trades-tab-sync-btn');
    if (syncAll) syncAll.style.display = anyConnected ? 'inline-block' : 'none';
    if (tradesTabSyncBtn) tradesTabSyncBtn.style.display = anyConnected ? 'inline-block' : 'none';
    if (anyConnected) loadBrokerPortfolio();
  }).catch(function() {});
}

var _approvalPollTimer = null;

var _setupBroker = '';

function connectBrokerByName(broker) {
  var suffix = broker === 'robinhood' ? 'rh' : (broker === 'coinbase' ? 'cb' : 'mm');
  var subEl = document.getElementById('broker-sub-' + suffix);
  if (subEl) subEl.textContent = 'Connecting...';
  fetch('/api/trading/broker/connect', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    credentials: 'same-origin',
    body: JSON.stringify({ broker: broker })
  }).then(function(r){return r.json();}).then(function(d) {
      if (d.ok) {
        loadBrokerStatus();
        var label = broker === 'coinbase' ? 'Coinbase Advanced' : 'Robinhood';
        appendAiMsg('assistant', label + ' connected successfully! I can now see your portfolio and execute trades through it.');
      } else if (d.status === 'needs_credentials') {
        if (subEl) subEl.textContent = 'Click to set up';
        openBrokerSetup(broker);
      } else if (broker === 'robinhood' && d.status === 'app_approval') {
        if (subEl) subEl.textContent = 'Check Robinhood app';
        openApprovalDialog();
      } else if (broker === 'robinhood' && d.status === 'sms_sent') {
        if (subEl) subEl.textContent = 'Check your phone';
        openSmsDialog('sms');
      } else {
        var shortMsg = d.status === 'error' ? 'Setup required' : 'Connection failed';
        if (subEl) subEl.textContent = shortMsg;
        var msg = d.message || 'Could not connect to ' + broker + '.';
        appendAiMsg('assistant', msg);
      }
    }).catch(function() {
      if (subEl) subEl.textContent = 'Connection failed';
    });
}

function openBrokerSetup(broker) {
  _setupBroker = broker;
  var dialog = document.getElementById('broker-setup-dialog');
  var fields = document.getElementById('setup-fields');
  var title = document.getElementById('setup-title');
  var desc = document.getElementById('setup-desc');
  var icon = document.getElementById('setup-icon');
  document.getElementById('setup-error').textContent = '';

  if (broker === 'robinhood') {
    icon.innerHTML = '&#x1F4B9;';
    title.textContent = 'Connect Robinhood';
    desc.textContent = 'Enter your Robinhood login credentials. These are encrypted and stored securely.';
    fields.innerHTML =
      '<div class="setup-field"><label>Email</label><input type="email" id="setup-rh-user" placeholder="your@email.com" autocomplete="username" /></div>' +
      '<div class="setup-field"><label>Password</label><input type="password" id="setup-rh-pass" placeholder="Password" autocomplete="current-password" /></div>' +
      '<div class="setup-field"><label>TOTP Secret <span style="font-weight:400;text-transform:none">(optional)</span></label><input type="text" id="setup-rh-totp" placeholder="Base32 secret for auto-login" /><div class="setup-hint">If set, Chili can auto-connect without SMS verification each time.</div></div>';
  } else if (broker === 'coinbase') {
    icon.innerHTML = '&#x1F4B0;';
    title.textContent = 'Connect Coinbase Advanced';
    desc.innerHTML = 'Enter your API credentials from <a href="https://www.coinbase.com/settings/api" target="_blank" style="color:var(--accent)">coinbase.com/settings/api</a>.';
    fields.innerHTML =
      '<div class="setup-field"><label>API Key</label><input type="text" id="setup-cb-key" placeholder="organizations/xxxx/apiKeys/yyyy" /></div>' +
      '<div class="setup-field"><label>API Secret</label><textarea id="setup-cb-secret" placeholder="-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----"></textarea></div>';
  }

  dialog.classList.add('visible');
  var firstInput = fields.querySelector('input,textarea');
  if (firstInput) setTimeout(function() { firstInput.focus(); }, 100);
}

function closeBrokerSetup() {
  document.getElementById('broker-setup-dialog').classList.remove('visible');
  _setupBroker = '';
  loadBrokerStatus();
}

function saveBrokerCredentials() {
  var btn = document.getElementById('setup-save-btn');
  var error = document.getElementById('setup-error');
  error.textContent = '';
  var body = { broker: _setupBroker };

  if (_setupBroker === 'robinhood') {
    body.username = (document.getElementById('setup-rh-user') || {}).value || '';
    body.password = (document.getElementById('setup-rh-pass') || {}).value || '';
    body.totp_secret = (document.getElementById('setup-rh-totp') || {}).value || '';
    if (!body.username || !body.password) {
      error.textContent = 'Email and password are required.';
      return;
    }
  } else if (_setupBroker === 'coinbase') {
    body.api_key = (document.getElementById('setup-cb-key') || {}).value || '';
    body.api_secret = (document.getElementById('setup-cb-secret') || {}).value || '';
    if (!body.api_key || !body.api_secret) {
      error.textContent = 'API Key and Secret are required.';
      return;
    }
  }

  btn.disabled = true;
  btn.textContent = 'Connecting...';

  fetch('/api/trading/broker/credentials', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    credentials: 'same-origin',
    body: JSON.stringify(body)
  }).then(function(r) { return r.json(); }).then(function(d) {
    btn.disabled = false;
    btn.textContent = 'Save & Connect';
    if (d.ok) {
      closeBrokerSetup();
      loadBrokerStatus();
      var label = _setupBroker === 'coinbase' ? 'Coinbase Advanced' : 'Robinhood';
      appendAiMsg('assistant', label + ' connected successfully! Your credentials have been saved securely.');
    } else if (_setupBroker === 'robinhood' && d.status === 'app_approval') {
      closeBrokerSetup();
      openApprovalDialog();
    } else if (_setupBroker === 'robinhood' && d.status === 'sms_sent') {
      closeBrokerSetup();
      openSmsDialog('sms');
    } else {
      error.textContent = d.message || 'Connection failed. Check your credentials.';
    }
  }).catch(function() {
    btn.disabled = false;
    btn.textContent = 'Save & Connect';
    error.textContent = 'Network error. Please try again.';
    });
}

function openApprovalDialog() {
  var dialog = document.getElementById('sms-dialog');
  var card = dialog.querySelector('.broker-sms-card');
  card.querySelector('.sms-icon').innerHTML = '&#x1F4F1;';
  card.querySelector('h3').textContent = 'Approve in Robinhood App';
  card.querySelector('p').textContent = 'A login request was sent to your Robinhood app. Open the app and tap "Approve" to continue.';
  document.getElementById('sms-code-input').style.display = 'none';
  document.getElementById('sms-verify-btn').style.display = 'none';
  document.getElementById('sms-error').textContent = '';

  dialog.classList.add('visible');
  _startApprovalPolling();
}

function _startApprovalPolling() {
  if (_approvalPollTimer) clearInterval(_approvalPollTimer);
  var attempts = 0;
  _approvalPollTimer = setInterval(function() {
    attempts++;
    if (attempts > 24) { // ~2 minutes
      clearInterval(_approvalPollTimer);
      _approvalPollTimer = null;
      document.getElementById('sms-error').textContent = 'Timed out waiting for approval. Try again.';
      return;
    }
    fetch('/api/trading/broker/poll', { credentials: 'same-origin' }).then(function(r){return r.json();}).then(function(d) {
      if (d.ok) {
        clearInterval(_approvalPollTimer);
        _approvalPollTimer = null;
        closeSmsDialog();
        loadBrokerStatus();
        appendAiMsg('assistant', 'Robinhood connected successfully! I can now see your real portfolio and give you personalized advice based on your actual holdings.');
      } else if (d.status === 'error') {
        clearInterval(_approvalPollTimer);
        _approvalPollTimer = null;
        document.getElementById('sms-error').textContent = d.message || 'Approval failed.';
      }
    }).catch(function() {});
  }, 5000);
}

function openSmsDialog(mode) {
  var dialog = document.getElementById('sms-dialog');
  var card = dialog.querySelector('.broker-sms-card');
  card.querySelector('.sms-icon').innerHTML = '&#x1F4F1;';
  card.querySelector('h3').textContent = 'Enter Verification Code';
  card.querySelector('p').textContent = 'A verification code was sent to your device. Enter it below.';
  document.getElementById('sms-code-input').style.display = '';
  document.getElementById('sms-code-input').value = '';
  document.getElementById('sms-verify-btn').style.display = '';
  document.getElementById('sms-error').textContent = '';

  dialog.classList.add('visible');
  setTimeout(function() { document.getElementById('sms-code-input').focus(); }, 100);

  document.getElementById('sms-code-input').onkeydown = function(e) {
    if (e.key === 'Enter') verifySmsCode();
    if (e.key === 'Escape') closeSmsDialog();
  };
}

function closeSmsDialog() {
  if (_approvalPollTimer) { clearInterval(_approvalPollTimer); _approvalPollTimer = null; }
  document.getElementById('sms-dialog').classList.remove('visible');
  document.getElementById('sms-code-input').value = '';
  document.getElementById('sms-error').textContent = '';
  loadBrokerStatus();
}

function verifySmsCode() {
  var input = document.getElementById('sms-code-input');
  var btn = document.getElementById('sms-verify-btn');
  var error = document.getElementById('sms-error');
  var code = input.value.trim();

  if (!code || code.length < 4) {
    error.textContent = 'Please enter the 6-digit code.';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Verifying...';
  error.textContent = '';

  fetch('/api/trading/broker/verify', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    credentials: 'same-origin',
    body: JSON.stringify({ code: code })
  }).then(function(r){return r.json();}).then(function(d) {
    btn.disabled = false;
    btn.textContent = 'Verify';
    if (d.ok) {
      closeSmsDialog();
      loadBrokerStatus();
      appendAiMsg('assistant', 'Robinhood connected successfully! I can now see your real portfolio and give you personalized advice based on your actual holdings.');
    } else {
      error.textContent = d.message || 'Invalid code. Please try again.';
      input.value = '';
      input.focus();
    }
  }).catch(function() {
    btn.disabled = false;
    btn.textContent = 'Verify';
    error.textContent = 'Network error. Please try again.';
  });
}

function syncAllBrokers() {
  var syncBtn = document.getElementById('broker-sync-all');
  var tradesTabSyncBtn = document.getElementById('trades-tab-sync-btn');
  function setSyncing(v) {
    if (syncBtn) { syncBtn.textContent = v ? 'Syncing...' : 'Sync All'; syncBtn.disabled = v; }
    if (tradesTabSyncBtn) { tradesTabSyncBtn.textContent = v ? 'Syncing...' : 'Sync Brokers'; tradesTabSyncBtn.disabled = v; }
  }
  setSyncing(true);
  fetch('/api/trading/broker/sync', { method: 'POST', credentials: 'same-origin' })
    .then(function(r){return r.json();})
    .then(function(d) {
      setSyncing(false);
      if (d.ok) {
        loadBrokerPortfolio();
        loadPortfolio();
        loadTrades();
        _refreshMonitorIfLoaded();
        var parts = [];
        var rho = d.robinhood_orders;
        if (rho && (rho.synced || rho.filled || rho.cancelled)) parts.push('RH Orders: ' + (rho.filled || 0) + ' filled, ' + (rho.cancelled || 0) + ' cancelled');
        var rhp = d.robinhood_positions;
        if (rhp && (rhp.created || rhp.updated || rhp.closed)) parts.push('RH Positions: ' + (rhp.created || 0) + ' new, ' + (rhp.updated || 0) + ' updated');
        var cbo = d.coinbase_orders;
        if (cbo && (cbo.synced || cbo.filled || cbo.cancelled)) parts.push('CB Orders: ' + (cbo.filled || 0) + ' filled, ' + (cbo.cancelled || 0) + ' cancelled');
        var cbp = d.coinbase_positions;
        if (cbp && (cbp.created || cbp.updated || cbp.closed)) parts.push('CB Positions: ' + (cbp.created || 0) + ' new, ' + (cbp.updated || 0) + ' updated');
        if (parts.length) appendAiMsg('assistant', 'Synced all brokers. ' + parts.join('; ') + '.');
        else appendAiMsg('assistant', 'All brokers synced. Everything is up to date.');
      }
    }).catch(function() {
      setSyncing(false);
    });
}

function loadBrokerPortfolio() {
  if (!_brokerConnected) return;

  var section = document.getElementById('broker-portfolio-section');
  var summaryEl = document.getElementById('broker-portfolio-summary');
  var listEl = document.getElementById('broker-positions-list');

  Promise.all([
    fetch('/api/trading/broker/portfolio', { credentials: 'same-origin' }).then(function(r){return r.json();}),
    fetch('/api/trading/broker/positions', { credentials: 'same-origin' }).then(function(r){return r.json();})
  ]).then(function(results) {
    var portData = results[0];
    var posData = results[1];

    if (!portData.ok && !posData.ok) {
      section.style.display = 'none';
      return;
    }

    section.style.display = 'block';

    if (portData.ok && portData.portfolio) {
      var p = portData.portfolio;
      summaryEl.innerHTML =
        '<div class="portfolio-grid" style="margin-bottom:8px">' +
        '<div class="p-card"><div class="p-val">$' + (p.total_equity || 0).toLocaleString(undefined, {minimumFractionDigits:2}) + '</div><div class="p-lbl">Total Equity</div></div>' +
        '<div class="p-card"><div class="p-val">$' + (p.total_buying_power || 0).toLocaleString(undefined, {minimumFractionDigits:2}) + '</div><div class="p-lbl">Buying Power</div></div>' +
        '<div class="p-card"><div class="p-val">$' + (p.total_cash || 0).toLocaleString(undefined, {minimumFractionDigits:2}) + '</div><div class="p-lbl">Cash</div></div>' +
        '</div>';
    }

    if (posData.ok && posData.positions && posData.positions.length) {
      var html = '<table class="rh-positions-table"><thead><tr><th>Ticker</th><th>Broker</th><th>Qty</th><th>Avg Cost</th><th>Current</th><th>%</th></tr></thead><tbody>';
      posData.positions.forEach(function(p) {
        var pct = p.percent_change || 0;
        var cls = pct >= 0 ? 'rh-pnl-pos' : 'rh-pnl-neg';
        var pctStr = (pct >= 0 ? '+' : '') + parseFloat(pct).toFixed(2) + '%';
        var src = p.broker_source || 'manual';
        var badgeCls = src === 'robinhood' ? 'rh' : (src === 'coinbase' ? 'cb' : 'manual');
        var badgeLbl = src === 'robinhood' ? 'RH' : (src === 'coinbase' ? 'CB' : 'MAN');
        html += '<tr onclick="selectTicker(\'' + p.ticker + '\')" style="cursor:pointer">' +
          '<td><strong>' + p.ticker + '</strong></td>' +
          '<td><span class="broker-badge ' + badgeCls + '">' + badgeLbl + '</span></td>' +
          '<td>' + p.quantity + '</td>' +
          '<td>$' + smartPrice(p.average_buy_price || 0) + '</td>' +
          '<td>$' + smartPrice(p.current_price || 0) + '</td>' +
          '<td class="' + cls + '">' + pctStr + '</td></tr>';
      });
      html += '</tbody></table>';
      listEl.innerHTML = html;
    } else {
      listEl.innerHTML = '<div style="color:var(--text-muted);font-size:11px;margin-top:4px">No open positions</div>';
    }
  }).catch(function() {
    section.style.display = 'none';
  });
}


/* ── Trade Proposal Cards ──────────────────────────── */
function renderTradeProposal(data) {
  var ticker = data.ticker || '???';
  var direction = (data.direction || 'buy').toLowerCase();
  var dirClass = direction === 'sell' ? 'sell' : 'buy';
  var dirLabel = direction === 'sell' ? 'SELL' : 'BUY';
  var confidence = data.confidence || 0;

  var html = '<div class="trade-proposal" data-ticker="' + ticker + '">';
  html += '<div class="tp-header">';
  html += '<span class="tp-ticker">' + ticker + '</span>';
  html += '<span class="tp-direction ' + dirClass + '">' + dirLabel + '</span>';
  if (data.timeframe) html += '<span style="font-size:10px;color:var(--text-secondary)">' + data.timeframe + '</span>';
  html += '</div>';

  html += '<div class="tp-grid">';
  if (data.entry_price) html += '<div class="tp-item"><div class="tp-label">Entry</div><div class="tp-value">$' + data.entry_price + '</div></div>';
  if (data.target_price) html += '<div class="tp-item"><div class="tp-label">Target</div><div class="tp-value" style="color:#22c55e">$' + data.target_price + '</div></div>';
  if (data.stop_loss) html += '<div class="tp-item"><div class="tp-label">Stop Loss</div><div class="tp-value" style="color:#ef4444">$' + data.stop_loss + '</div></div>';
  if (data.quantity) html += '<div class="tp-item"><div class="tp-label">Quantity</div><div class="tp-value">' + data.quantity + '</div></div>';
  html += '</div>';

  if (confidence) {
    html += '<div class="tp-confidence">';
    html += '<span>Confidence: ' + confidence + '%</span>';
    html += '<div class="tp-conf-bar"><div class="tp-conf-fill" style="width:' + confidence + '%"></div></div>';
    html += '</div>';
  }

  if (data.rationale) {
    html += '<div style="font-size:10px;color:var(--text-secondary);margin-bottom:8px">' + data.rationale + '</div>';
  }

  var proposalId = 'tp_' + Date.now() + '_' + Math.random().toString(36).substring(2,7);
  html += '<div class="tp-actions">';
  html += '<button class="tp-btn-confirm" onclick="confirmProposal(\'' + proposalId + '\',\'' + ticker + '\',\'' + direction + '\',' + (data.entry_price||0) + ',' + (data.quantity||1) + ')">Confirm & Track</button>';
  html += '<button class="tp-btn-reject" onclick="dismissTradeProposal(this)">Dismiss</button>';
  var _tpIsCrypto = ticker.indexOf('-USD') !== -1;
  if (_tpIsCrypto) {
    html += '<button class="tp-btn-rh" onclick="openInCoinbase(\'' + ticker + '\')" title="Open in Coinbase" style="background:#0052ff">CB</button>';
  }
  html += '<button class="tp-btn-rh" onclick="openInRobinhood(\'' + ticker + '\')" title="Open in Robinhood">RH</button>';
  html += '</div>';
  html += '</div>';

  return html;
}

function confirmProposal(proposalId, ticker, direction, entryPrice, quantity) {
  var btn = event.target;
  btn.textContent = 'Tracking...';
  btn.disabled = true;

  fetch('/api/trading/trades', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      ticker: ticker,
      direction: direction || 'long',
      entry_price: entryPrice,
      quantity: quantity || 1,
      tags: 'ai-proposal',
      notes: 'AI trade proposal confirmed by user (proposal: ' + proposalId + ')'
    })
  }).then(function(r){return r.json();}).then(function(d) {
    if (d.ok) {
      btn.textContent = 'Tracked!';
      btn.style.background = '#22c55e';
      loadTrades();
      loadPortfolio();
      appendAiMsg('assistant', 'Trade confirmed! I\'m now tracking your ' + direction.toUpperCase() + ' position on **' + ticker + '** at $' + entryPrice + '. I\'ll monitor it and alert you when targets or stop-loss levels are hit.');
    } else {
      btn.textContent = 'Error';
      btn.disabled = false;
    }
  }).catch(function() {
    btn.textContent = 'Error';
    btn.disabled = false;
  });
}

function dismissTradeProposal(btn) {
  var card = btn.closest('.trade-proposal');
  if (card) {
    card.style.opacity = '0.4';
    card.style.pointerEvents = 'none';
    card.querySelector('.tp-actions').innerHTML = '<span style="font-size:11px;color:var(--text-secondary)">Dismissed</span>';
  }
}

function openInRobinhood(ticker) {
  var cleanTicker = ticker.replace('-USD', '');
  var isCrypto = ticker.includes('-USD') || ticker.includes('-');
  var url;
  if (isCrypto) {
    url = 'https://robinhood.com/crypto/' + cleanTicker;
  } else {
    url = 'https://robinhood.com/stocks/' + cleanTicker;
  }
  window.open(url, '_blank');
}

function openInCoinbase(ticker) {
  var pair = ticker.toUpperCase();
  if (!pair.includes('-')) pair = pair + '-USD';
  window.open('https://www.coinbase.com/advanced-trade/' + pair, '_blank');
}

function _tryParseProposals(text) {
  var proposals = [];
  var buyPattern = /\*\*(?:BUY|SELL)\*\*[^]*?\*\*([A-Z]{1,5}(?:-USD)?)\*\*/gi;
  var verdictPattern = /VERDICT:\s*(BUY|SELL)/i;
  var entryPattern = /Entry\s*(?:zone|price)?[:\s]*\$?([\d,.]+)/i;
  var targetPattern = /Target\s*(?:1)?[:\s]*\$?([\d,.]+)/i;
  var stopPattern = /Stop[- ]?loss[:\s]*\$?([\d,.]+)/i;
  var confPattern = /Confidence[:\s]*([\d]+)%/i;

  var verdict = text.match(verdictPattern);
  var entry = text.match(entryPattern);
  var target = text.match(targetPattern);
  var stop = text.match(stopPattern);
  var conf = text.match(confPattern);

  var tickers = [];
  var tickerPat = /\$([A-Z]{1,6}(?:-USD)?)/g;
  var m;
  while ((m = tickerPat.exec(text)) !== null) {
    if (tickers.indexOf(m[1]) === -1) tickers.push(m[1]);
  }

  if (verdict && tickers.length > 0 && (entry || target || stop)) {
    proposals.push({
      ticker: tickers[0],
      direction: verdict[1].toLowerCase(),
      entry_price: entry ? parseFloat(entry[1].replace(',','')) : null,
      target_price: target ? parseFloat(target[1].replace(',','')) : null,
      stop_loss: stop ? parseFloat(stop[1].replace(',','')) : null,
      confidence: conf ? parseInt(conf[1]) : null,
    });
  }

  return proposals;
}


/* ── Web3 / MetaMask Wallet ────────────────────── */
var CHAINS = {
  1:     {name:'Ethereum', rpc:'https://eth.llamarpc.com',         symbol:'ETH',  explorer:'https://etherscan.io',    hexId:'0x1'},
  137:   {name:'Polygon',  rpc:'https://polygon-rpc.com',          symbol:'POL',  explorer:'https://polygonscan.com', hexId:'0x89'},
  56:    {name:'BSC',      rpc:'https://bsc-dataseed.binance.org', symbol:'BNB',  explorer:'https://bscscan.com',     hexId:'0x38'},
  42161: {name:'Arbitrum', rpc:'https://arb1.arbitrum.io/rpc',     symbol:'ETH',  explorer:'https://arbiscan.io',     hexId:'0xa4b1'},
  8453:  {name:'Base',     rpc:'https://mainnet.base.org',         symbol:'ETH',  explorer:'https://basescan.org',    hexId:'0x2105'},
};

var _w3 = {
  provider: null, signer: null, address: '', chainId: 137, balance: '0',
  sellToken: null, buyToken: null, slippageBps: 50, quoteTimer: null,
  tokenModalSide: 'sell', tokenList: [],
  pendingTx: null, lastQuote: null, connected: false,
};

function _hasMetaMask() { return typeof window.ethereum !== 'undefined'; }

async function _connectWallet() {
  if (!_hasMetaMask()) { alert('MetaMask is not installed. Please install it from metamask.io'); return; }
  try {
    var accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
    if (!accounts || !accounts.length) return;
    _w3.provider = new ethers.BrowserProvider(window.ethereum);
    _w3.signer = await _w3.provider.getSigner();
    _w3.address = accounts[0];
    var network = await _w3.provider.getNetwork();
    _w3.chainId = Number(network.chainId);
    _w3.connected = true;
    _updateWalletUI();
    _loadTokenDefaults();
    _updateSwapBalances();

    window.ethereum.on('accountsChanged', function(accs) {
      if (!accs.length) { _disconnectWallet(); return; }
      _w3.address = accs[0];
      _updateWalletUI();
      _updateSwapBalances();
    });
    window.ethereum.on('chainChanged', function(cid) {
      _w3.chainId = parseInt(cid, 16);
      _w3.provider = new ethers.BrowserProvider(window.ethereum);
      _w3.provider.getSigner().then(function(s){ _w3.signer = s; });
      var cs = document.getElementById('chain-select');
      if (cs) cs.value = String(_w3.chainId);
      _updateWalletUI();
      _loadTokenDefaults();
      _updateSwapBalances();
    });
  } catch(e) {
    console.error('Wallet connect failed:', e);
  }
}

function _disconnectWallet() {
  _w3.provider = null; _w3.signer = null; _w3.address = '';
  _w3.balance = '0'; _w3.connected = false;
  _w3.sellToken = null; _w3.buyToken = null; _w3.lastQuote = null;
  var btn = document.getElementById('wallet-btn');
  if (btn) btn.classList.remove('connected');
  var wl = document.getElementById('wallet-label');
  if (wl) wl.textContent = 'Connect Wallet';
  document.getElementById('swap-connect-state').style.display = '';
  document.getElementById('swap-main').style.display = 'none';
}

async function _updateWalletUI() {
  var btn = document.getElementById('wallet-btn');
  var label = document.getElementById('wallet-label');
  var mmDot = document.getElementById('broker-dot-mm');
  var mmSub = document.getElementById('broker-sub-mm');
  if (!_w3.connected) {
    if (btn) btn.classList.remove('connected');
    if (label) label.textContent = 'Connect Wallet';
    if (mmDot) mmDot.className = 'broker-dot disconnected';
    if (mmSub) mmSub.textContent = 'Connect wallet';
    return;
  }
  if (btn) btn.classList.add('connected');
  if (mmDot) mmDot.className = 'broker-dot connected';
  var chain = CHAINS[_w3.chainId];
  var addr = _w3.address.slice(0,6) + '...' + _w3.address.slice(-4);
  if (mmSub) mmSub.textContent = addr;
  if (label) {
    try {
      var bal = await _w3.provider.getBalance(_w3.address);
      _w3.balance = ethers.formatEther(bal);
      var dispBal = parseFloat(_w3.balance).toFixed(4);
      label.innerHTML = '<span class="wallet-addr">' + addr + '</span> <span class="wallet-balance">' + dispBal + ' ' + (chain ? chain.symbol : '') + '</span>';
    } catch(e) {
      label.innerHTML = '<span class="wallet-addr">' + addr + '</span>';
    }
  }
  var sel = document.getElementById('chain-select');
  if (sel && String(sel.value) !== String(_w3.chainId) && CHAINS[_w3.chainId]) sel.value = String(_w3.chainId);
  document.getElementById('swap-connect-state').style.display = 'none';
  document.getElementById('swap-main').style.display = '';
}

async function _switchChain(chainId) {
  if (!_hasMetaMask()) return;
  var chain = CHAINS[chainId];
  if (!chain) return;
  try {
    await window.ethereum.request({
      method: 'wallet_switchEthereumChain',
      params: [{ chainId: chain.hexId }]
    });
  } catch(e) {
    if (e.code === 4902) {
      try {
        await window.ethereum.request({
          method: 'wallet_addEthereumChain',
          params: [{
            chainId: chain.hexId,
            chainName: chain.name,
            nativeCurrency: { name: chain.symbol, symbol: chain.symbol, decimals: 18 },
            rpcUrls: [chain.rpc],
            blockExplorerUrls: [chain.explorer],
          }]
        });
      } catch(e2) { console.error('Add chain failed:', e2); }
    }
  }
}

/* ── Token management ─────────────────────────────── */
function _loadTokenDefaults() {
  fetch('/api/trading/web3/tokens?chain_id=' + _w3.chainId)
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (!d.ok) return;
      _w3.tokenList = d.tokens;
      if (d.tokens.length >= 2) {
        _w3.sellToken = d.tokens[0];
        _w3.buyToken = d.tokens[1];
        _updateTokenButtons();
      }
    }).catch(function(){});
}

function _updateTokenButtons() {
  var sellBtn = document.getElementById('swap-sell-token-btn');
  var buyBtn = document.getElementById('swap-buy-token-btn');
  if (_w3.sellToken) sellBtn.innerHTML = (_w3.sellToken.symbol || '?') + ' &#x25BC;';
  if (_w3.buyToken) buyBtn.innerHTML = (_w3.buyToken.symbol || '?') + ' &#x25BC;';
}

function _openTokenModal(side) {
  _w3.tokenModalSide = side;
  var modal = document.getElementById('token-modal-bg');
  modal.classList.add('open');
  var input = document.getElementById('token-search-input');
  input.value = '';
  input.focus();
  _renderTokenList(_w3.tokenList);
}

