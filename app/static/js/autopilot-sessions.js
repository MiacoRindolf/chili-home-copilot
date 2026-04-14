(function() {
  'use strict';
  var AP = window.ChiliAutopilot;
  var state = AP.state;
  var esc = AP.esc;
  var safeNum = AP.safeNum;
  var pct = AP.pct;
  var money = AP.money;
  var runtimeLabel = AP.runtimeLabel;
  var dateLabel = AP.dateLabel;
  var etaLabel = AP.etaLabel;
  var badge = AP.badge;
  var jsonPreview = AP.jsonPreview;
  var runnerStateText = AP.runnerStateText;
  var runnerStateBadge = AP.runnerStateBadge;
  var refinementBadge = AP.refinementBadge;
  var fetchJson = AP.fetchJson;
  var showError = AP.showError;
  var IS_GUEST = AP.IS_GUEST;

  function renderSessionWarningCallout(row, readiness, runner) {
    var parts = [];
    if (readiness && readiness.blocked_reason) {
      parts.push('Execution blocked: ' + String(readiness.blocked_reason).replace(/_/g, ' '));
    }
    if (row.blocked_reason) {
      parts.push('Operator blocked: ' + String(row.blocked_reason).replace(/_/g, ' '));
    }
    if (runner && runner.blocked_reason) {
      parts.push('Runner blocked: ' + String(runner.blocked_reason).replace(/_/g, ' '));
    }
    if (row.next_action_required) {
      parts.push('Next action: ' + row.next_action_required);
    }
    if (!parts.length) {
      return '<div class="ap-callout good"><strong>Status</strong>Session is clear to continue based on current runtime state.</div>';
    }
    return '<div class="ap-callout block"><strong>Watchouts</strong>' + esc(parts.join(' | ')) + '</div>';
  }

  function renderRepeatableEdgeReadiness(row) {
    var re = row.repeatable_edge_readiness;
    if (!re || typeof re !== 'object') return '';
    var er = re.execution_robustness;
    var parts = [];
    if (re.live_not_recommended) {
      parts.push(
        'Live not recommended'
        + (re.live_not_recommended_reason
          ? ' (' + String(re.live_not_recommended_reason).replace(/_/g, ' ') + ')'
          : '')
      );
    }
    if (er && typeof er === 'object') {
      if (er.skip_reason) {
        parts.push('Execution robustness skipped: ' + String(er.skip_reason).replace(/_/g, ' '));
      } else {
        if (er.robustness_tier != null) parts.push('Robustness: ' + String(er.robustness_tier));
        if (er.provider_truth_mode != null) parts.push('Truth: ' + String(er.provider_truth_mode).replace(/_/g, ' '));
        if (er.fill_rate != null) {
          var fr = Number(er.fill_rate);
          parts.push('Fill ' + (isFinite(fr) ? (Math.round(fr * 1000) / 10 + '%') : String(er.fill_rate)));
        }
        if (er.avg_realized_slippage_bps != null) parts.push('Slippage ' + String(er.avg_realized_slippage_bps) + ' bps');
        if (er.readiness_impact_flags && er.readiness_impact_flags.length) {
          parts.push('Flags: ' + er.readiness_impact_flags.map(function(f) { return String(f).replace(/_/g, ' '); }).join(', '));
        }
      }
    }
    if (!parts.length) return '';
    return '<div class="ap-callout" style="border-color:rgba(251,191,36,.35);background:rgba(251,191,36,.06)"><strong>Linked repeatable-edge pattern</strong>'
      + esc(parts.join(' | '))
      + '</div>';
  }

  function renderSessionControlButton(sessionId, action, meta) {
    meta = meta || {};
    var cls = 'ap-btn';
    if (action === 'run' || action === 'resume') cls += ' ap-btn-primary';
    if (action === 'stop') cls += ' ap-btn-live';
    if (action === 'delete') cls += ' ap-btn-danger';
    return '<button type="button" class="' + cls + '" '
      + (meta.enabled ? '' : 'disabled ')
      + 'onclick="apSessionAction(' + Number(sessionId) + ',\'' + esc(action) + '\')">'
      + esc(meta.label || action)
      + '</button>';
  }

  function sessionSummaryCard(row) {
    var controls = row.controls || {};
    var readiness = row.execution_readiness || {};
    var refinement = row.refinement_info || {};
    var params = row.strategy_params_summary || {};
    var runner = row.runner_health || {};
    var sid = esc(row.id);
    var badges = ''
      + badge(row.mode || 'paper', row.mode || 'paper')
      + badge(row.state || 'unknown', row.is_paused ? 'paused' : (row.market_open_now ? 'open' : 'closed'))
      + badge(row.asset_class || 'unknown', row.asset_class || '')
      + badge(row.market_open_now ? (row.asset_class === 'crypto' ? '24x7' : 'Market open') : 'Market closed', row.market_open_now ? 'open' : 'closed')
      + refinementBadge(refinement)
      + runnerStateBadge(runner);
    if (row.is_paused) badges += badge('Paused', 'paused');

    var pnlVal = Number(row.simulated_pnl);
    var pnlCls = isFinite(pnlVal) ? (pnlVal >= 0 ? 'positive' : 'negative') : '';

    return ''
      + '<article class="ap-session-card" data-session-id="' + sid + '">'
      + '  <div class="ap-session-card__header">'
      + '    <div>'
      + '      <div class="ap-symbol-lockup">'
      + '        <span class="ap-symbol">' + esc(row.symbol) + '</span>'
      + badges
      + '      </div>'
      + '      <div class="ap-subline" style="margin-top:8px;">'
      + '        ' + esc((row.variant && row.variant.label) || row.strategy_family || 'Strategy')
      + '        &middot; Position ' + esc(row.current_position_state || 'flat')
      + '        &middot; Last action ' + esc(row.last_action || row.state || 'n/a')
      + '      </div>'
      + '    </div>'
      + '    <div class="ap-actions">'
      + renderSessionControlButton(row.id, 'run', controls.run)
      + renderSessionControlButton(row.id, 'pause', controls.pause)
      + renderSessionControlButton(row.id, 'resume', controls.resume)
      + renderSessionControlButton(row.id, 'stop', controls.stop)
      + renderSessionControlButton(row.id, 'delete', controls.delete)
      + '    </div>'
      + '  </div>'
      + '  <div class="ap-session-card__metrics">'
      + '    <div class="ap-metric"><label>Runtime</label><b>' + esc(runtimeLabel(row.runtime || {})) + '</b><small>Lane ' + esc(row.lane || 'simulation') + '</small></div>'
      + '    <div class="ap-metric"><label>Confidence</label><b>' + esc(pct(row.confidence)) + '</b><small>Conviction ' + esc(pct(row.conviction)) + '</small></div>'
      + '    <div class="ap-metric"><label>Sim P&amp;L</label><b class="' + pnlCls + '">' + esc(money(row.simulated_pnl)) + '</b><small>Trades ' + esc(row.trade_count || 0) + '</small></div>'
      + '    <div class="ap-metric"><label>Runner</label><b>' + esc(runnerStateText(runner)) + '</b><small>Last tick ' + esc(dateLabel(runner.last_tick_utc)) + '</small></div>'
      + '  </div>'
      + '  <div class="ap-session-card__callouts">'
      + '    <div class="ap-callout"><strong>Thesis</strong>' + esc(row.thesis || 'Awaiting next bounded decision update.') + '</div>'
      + renderSessionWarningCallout(row, readiness, runner)
      + renderRepeatableEdgeReadiness(row)
      + '  </div>'
      + '  <div class="ap-tabs" role="tablist" aria-label="Session ' + sid + ' details">'
      + '    <button type="button" class="ap-tab-btn" role="tab" aria-selected="true" data-tab="chart" data-sid="' + sid + '">Chart</button>'
      + '    <button type="button" class="ap-tab-btn" role="tab" aria-selected="false" data-tab="fills" data-sid="' + sid + '">Fills</button>'
      + '    <button type="button" class="ap-tab-btn" role="tab" aria-selected="false" data-tab="events" data-sid="' + sid + '">Events</button>'
      + '    <button type="button" class="ap-tab-btn" role="tab" aria-selected="false" data-tab="details" data-sid="' + sid + '">Details</button>'
      + '  </div>'
      + '  <div class="ap-tab-panel active" data-panel="chart" data-sid="' + sid + '" role="tabpanel">'
      + '    <div class="ap-session-chart-wrap">'
      + '      <div class="ap-chart-head">'
      + '        <div class="ap-muted">Candlestick replay with buy/sell markers.</div>'
      + '        <div class="ap-badges">' + badge('Provider ' + (((row.data_binding || {}).source_of_truth_provider) || ((row.data_binding || {}).chart_provider) || 'n/a'), 'event') + '</div>'
      + '      </div>'
      + '      <div id="ap-chart-' + sid + '" class="ap-chart"><div class="ap-chart-empty">Waiting for chart data...</div></div>'
      + '    </div>'
      + '  </div>'
      + '  <div class="ap-tab-panel" data-panel="fills" data-sid="' + sid + '" role="tabpanel">'
      + '    <div id="ap-fills-' + sid + '" class="ap-muted">Scroll into view to load fills.</div>'
      + '  </div>'
      + '  <div class="ap-tab-panel" data-panel="events" data-sid="' + sid + '" role="tabpanel">'
      + '    <div id="ap-events-' + sid + '" class="ap-event-list"><div class="ap-muted">Scroll into view to load events.</div></div>'
      + '  </div>'
      + '  <div class="ap-tab-panel" data-panel="details" data-sid="' + sid + '" role="tabpanel">'
      + '    <div class="ap-detail-stack">'
      + '      <div class="ap-callout"><strong>Strategy Params</strong>'
      + '        Entry ' + esc(params.entry_viability_min != null ? params.entry_viability_min : 'n/a')
      + '        &middot; Revalidate ' + esc(params.entry_revalidate_floor != null ? params.entry_revalidate_floor : 'n/a')
      + '        &middot; Bailout ' + esc(params.bailout_viability_floor != null ? params.bailout_viability_floor : 'n/a')
      + '        <br>Stop ATR ' + esc(params.stop_atr_mult != null ? params.stop_atr_mult : 'n/a')
      + '        &middot; Target ATR ' + esc(params.target_atr_mult != null ? params.target_atr_mult : 'n/a')
      + '        &middot; Hold cap ' + esc(params.max_hold_seconds != null ? params.max_hold_seconds + 's' : 'n/a')
      + '      </div>'
      + '      <div class="ap-callout"><strong>Refinement</strong>'
      + (refinement.is_refined
        ? 'Brain refined from variant #' + esc(refinement.parent_variant_id || 'n/a')
        : 'Seeded family baseline.')
      + '      </div>'
      + '      <div class="ap-callout"><strong>Runner Health</strong>'
      + '        Blocked: ' + esc((runner.blocked_reason || 'none').replace(/_/g, ' '))
      + '        <br>ETA: ' + esc(etaLabel(runner.next_tick_eta_seconds))
      + '        <br>Heartbeat: ' + esc(dateLabel(runner.scheduler_heartbeat_utc))
      + '      </div>'
      + '      <pre id="ap-runtime-' + sid + '" class="ap-pre">'
      + esc(jsonPreview({
          execution_readiness: row.execution_readiness || {},
          chart_levels: row.chart_levels || {},
          pause_info: row.pause_info || {},
          data_binding: row.data_binding || {}
        }))
      + '</pre>'
      + '    </div>'
      + '  </div>'
      + '</article>';
  }

  function bindSessionTabs() {
    document.querySelectorAll('.ap-tab-btn[data-tab]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var sid = btn.getAttribute('data-sid');
        var tab = btn.getAttribute('data-tab');
        var card = btn.closest('.ap-session-card');
        if (!card) return;
        card.querySelectorAll('.ap-tab-btn[data-sid="' + sid + '"]').forEach(function(b) {
          b.setAttribute('aria-selected', 'false');
        });
        btn.setAttribute('aria-selected', 'true');
        card.querySelectorAll('.ap-tab-panel[data-sid="' + sid + '"]').forEach(function(p) {
          p.classList.toggle('active', p.getAttribute('data-panel') === tab);
        });
      });
    });
  }

  function _saveTabState() {
    var saved = {};
    document.querySelectorAll('.ap-tab-btn[aria-selected="true"][data-sid]').forEach(function(btn) {
      saved[btn.getAttribute('data-sid')] = btn.getAttribute('data-tab');
    });
    return saved;
  }

  function _restoreTabState(saved) {
    Object.keys(saved).forEach(function(sid) {
      var tab = saved[sid];
      if (!tab || tab === 'chart') return;
      var card = document.querySelector('.ap-session-card[data-session-id="' + sid + '"]');
      if (!card) return;
      card.querySelectorAll('.ap-tab-btn[data-sid="' + sid + '"]').forEach(function(b) {
        var isSel = b.getAttribute('data-tab') === tab;
        b.setAttribute('aria-selected', String(isSel));
      });
      card.querySelectorAll('.ap-tab-panel[data-sid="' + sid + '"]').forEach(function(p) {
        p.classList.toggle('active', p.getAttribute('data-panel') === tab);
      });
    });
  }

  function _sessionIdSet(sessions) {
    return (sessions || []).map(function(s) { return String(s.id); }).sort().join(',');
  }

  function _updateMetricsInPlace(sessions) {
    sessions.forEach(function(row) {
      var card = document.querySelector('.ap-session-card[data-session-id="' + row.id + '"]');
      if (!card) return;
      var runner = row.runner_health || {};
      var pnlVal = Number(row.simulated_pnl);
      var pnlCls = isFinite(pnlVal) ? (pnlVal >= 0 ? 'positive' : 'negative') : '';
      var metrics = card.querySelectorAll('.ap-metric');
      if (metrics[0]) metrics[0].querySelector('b').textContent = runtimeLabel(row.runtime || {});
      if (metrics[1]) metrics[1].querySelector('b').textContent = pct(row.confidence);
      if (metrics[2]) {
        var pnlB = metrics[2].querySelector('b');
        pnlB.textContent = money(row.simulated_pnl);
        pnlB.className = pnlCls;
        var pnlSmall = metrics[2].querySelector('small');
        if (pnlSmall) pnlSmall.textContent = 'Trades ' + (row.trade_count || 0);
      }
      if (metrics[3]) {
        metrics[3].querySelector('b').textContent = runnerStateText(runner);
        var runSmall = metrics[3].querySelector('small');
        if (runSmall) runSmall.textContent = 'Last tick ' + dateLabel(runner.last_tick_utc);
      }
    });
  }

  function renderSessions() {
    var list = document.getElementById('ap-sessions');
    var empty = document.getElementById('ap-sessions-empty');
    if (!list || !empty) return;

    if (!state.sessions.length) {
      destroyAllCharts();
      list.innerHTML = ''
        + '<div class="ap-empty-state">'
        + '  <div class="ap-empty-state__icon">&#x1F4CA;</div>'
        + '  <div class="ap-empty-state__title">No sessions yet</div>'
        + '  <div class="ap-empty-state__desc">Start from an eligible symbol above to create a paper or live workflow.</div>'
        + '</div>';
      empty.style.display = 'none';
      observeSessionCards();
      return;
    }

    var prevIds = _sessionIdSet(state._prevSessions);
    var currIds = _sessionIdSet(state.sessions);

    if (prevIds === currIds && list.querySelector('.ap-session-card')) {
      _updateMetricsInPlace(state.sessions);
      state._prevSessions = state.sessions;
      return;
    }

    var tabState = _saveTabState();
    destroyAllCharts();

    empty.style.display = 'none';
    list.innerHTML = state.sessions.map(sessionSummaryCard).join('');
    bindSessionTabs();
    _restoreTabState(tabState);
    observeSessionCards();
    state._prevSessions = state.sessions;
  }

  function observeSessionCards() {
    if (state.observer) state.observer.disconnect();
    state.visibleSessionIds = {};
    state.observer = new IntersectionObserver(function(entries) {
      entries.forEach(function(entry) {
        var id = Number(entry.target.getAttribute('data-session-id') || 0);
        if (!id) return;
        if (entry.isIntersecting) {
          state.visibleSessionIds[id] = true;
          apLoadSessionDetail(id, false);
        } else {
          delete state.visibleSessionIds[id];
          destroyChart(id);
        }
      });
    }, { rootMargin: '240px 0px' });
    document.querySelectorAll('.ap-session-card[data-session-id]').forEach(function(card) {
      state.observer.observe(card);
    });
  }

  function renderFillTable(rows) {
    if (!rows || !rows.length) {
      return '<div class="ap-muted">No fills recorded yet.</div>';
    }
    return '<table class="ap-fill-table"><thead><tr><th>Time</th><th>Action</th><th>Price</th><th>Qty</th><th>P&amp;L</th><th>Reason</th></tr></thead><tbody>'
      + rows.map(function(fill) {
        return '<tr>'
          + '<td>' + esc(dateLabel(fill.ts)) + '</td>'
          + '<td><strong>' + esc(fill.action || '') + '</strong></td>'
          + '<td>' + esc(fill.price != null ? Number(fill.price).toFixed(4) : 'n/a') + '</td>'
          + '<td>' + esc(fill.quantity != null ? fill.quantity : 'n/a') + '</td>'
          + '<td>' + esc(fill.pnl_usd == null ? 'n/a' : money(fill.pnl_usd)) + '</td>'
          + '<td>' + esc(fill.reason || '') + '</td>'
          + '</tr>';
      }).join('')
      + '</tbody></table>';
  }

  function renderEventRows(rows) {
    if (!rows || !rows.length) {
      return '<div class="ap-muted">No events yet.</div>';
    }
    return rows.map(function(ev) {
      return '<div class="ap-event">'
        + '<b>' + esc(ev.event_type || 'event') + '</b>'
        + '<small>' + esc(dateLabel(ev.ts)) + '</small>'
        + '<div class="ap-pre">' + esc(jsonPreview(ev.payload_summary || {})) + '</div>'
        + '</div>';
    }).join('');
  }

  function _createChart(chartEl) {
    return LightweightCharts.createChart(chartEl, {
      layout: { background: { color: '#020617' }, textColor: '#cbd5e1' },
      grid: {
        vertLines: { color: 'rgba(148,163,184,0.08)' },
        horzLines: { color: 'rgba(148,163,184,0.08)' }
      },
      rightPriceScale: { borderColor: 'rgba(148,163,184,0.14)' },
      timeScale: { borderColor: 'rgba(148,163,184,0.14)', timeVisible: true, secondsVisible: true },
      crosshair: { mode: 0 }
    });
  }

  function _parseOhlcvPoints(data) {
    return (data || []).map(function(row) {
      var timeVal = row.time || row.date || row.timestamp;
      var ts;
      if (typeof timeVal === 'number') {
        ts = timeVal > 1e12 ? timeVal : timeVal * 1000;
      } else {
        ts = Date.parse(timeVal);
      }
      return {
        time: Math.floor(ts / 1000),
        open: safeNum(row.open, NaN), high: safeNum(row.high, NaN),
        low: safeNum(row.low, NaN), close: safeNum(row.close, NaN)
      };
    }).filter(function(row) {
      return isFinite(row.time) && isFinite(row.open) && isFinite(row.high) && isFinite(row.low) && isFinite(row.close);
    }).sort(function(a, b) { return a.time - b.time; });
  }

  function _fmtPrice(p) {
    var n = Number(p);
    if (!isFinite(n)) return '';
    return n < 1 ? n.toPrecision(4) : n.toFixed(2);
  }

  function _friendlyLabel(fill) {
    var isEnter = String(fill.action || '').indexOf('enter') === 0;
    var px = _fmtPrice(fill.price);
    if (isEnter) return 'BUY ' + px;
    var reason = String(fill.reason || '').replace(/_/g, ' ');
    var pnl = '';
    if (fill.pnl_usd != null && isFinite(Number(fill.pnl_usd))) {
      pnl = ' | ' + (Number(fill.pnl_usd) >= 0 ? '+' : '') + Number(fill.pnl_usd).toFixed(2);
    }
    return 'SELL ' + px + (reason ? ' (' + reason + ')' : '') + pnl;
  }

  function _buildFillMarkers(fills) {
    return (fills || []).map(function(fill) {
      var timeVal = Date.parse(fill.ts || '');
      if (!isFinite(timeVal) || !isFinite(Number(fill.price))) return null;
      var isEnter = String(fill.action || '').indexOf('enter') === 0;
      return {
        time: Math.floor(timeVal / 1000),
        position: isEnter ? 'belowBar' : 'aboveBar',
        color: isEnter ? '#facc15' : '#f97316',
        shape: isEnter ? 'arrowUp' : 'arrowDown',
        text: _friendlyLabel(fill)
      };
    }).filter(Boolean).sort(function(a, b) { return a.time - b.time; });
  }

  function _addLevelSegments(chartState, fills) {
    _clearLevelSeries(chartState);
    var pairs = _pairFills(fills);
    pairs.forEach(function(pair) {
      var mj = pair.entry.marker_json || {};
      var t0 = Math.floor(Date.parse(pair.entry.ts || '') / 1000);
      var t1 = pair.exit ? Math.floor(Date.parse(pair.exit.ts || '') / 1000) : null;
      if (!isFinite(t0)) return;
      if (!t1 || !isFinite(t1)) {
        var lb = chartState.lastBar;
        t1 = lb ? lb.time : t0 + 3600;
      }
      if (t1 <= t0) t1 = t0 + 60;
      var levels = [
        { price: mj.entry, color: '#facc15', style: LightweightCharts.LineStyle.Dashed },
        { price: mj.stop,  color: '#ef4444', style: LightweightCharts.LineStyle.Dotted },
        { price: mj.target, color: '#22c55e', style: LightweightCharts.LineStyle.Dotted }
      ];
      levels.forEach(function(lv) {
        var px = Number(lv.price);
        if (!isFinite(px)) return;
        var ls = chartState.chart.addLineSeries({
          color: lv.color, lineWidth: 1, lineStyle: lv.style,
          priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false, pointMarkersVisible: false
        });
        ls.setData([{ time: t0, value: px }, { time: t1, value: px }]);
        chartState.levelSeries.push(ls);
      });
    });
  }

  function _clearLevelSeries(chartState) {
    (chartState.levelSeries || []).forEach(function(ls) {
      try { chartState.chart.removeSeries(ls); } catch (_e) {}
    });
    chartState.levelSeries = [];
  }

  function _pairFills(fills) {
    var pairs = [];
    var pending = null;
    (fills || []).forEach(function(f) {
      if (String(f.action || '').indexOf('enter') === 0) {
        if (pending) pairs.push(pending);
        pending = { entry: f, exit: null };
      } else if (pending && String(f.action || '').indexOf('exit') === 0) {
        pending.exit = f;
        pairs.push(pending);
        pending = null;
      }
    });
    if (pending) pairs.push(pending);
    return pairs;
  }

  function _updateOverlays(chartState, fills) {
    var markers = _buildFillMarkers(fills);
    if (typeof chartState.series.setMarkers === 'function') chartState.series.setMarkers(markers);
    _addLevelSegments(chartState, fills);
  }

  // ── WebSocket streaming for real-time chart updates ──────────────
  var _apWs = null;
  var _apWsSymbols = new Set();

  function _ensureAutopilotWs() {
    if (_apWs && (_apWs.readyState === WebSocket.OPEN || _apWs.readyState === WebSocket.CONNECTING)) return;
    var syms = Array.from(_apWsSymbols);
    if (!syms.length) return;

    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/autopilot/live?symbols=' + encodeURIComponent(syms.join(','));
    _apWs = new WebSocket(url);

    _apWs.onmessage = function(evt) {
      try {
        var msg = JSON.parse(evt.data);
        if (msg.type === 'tick') {
          _handleStreamTick(msg);
        } else if (msg.type === 'candle') {
          _handleStreamCandle(msg);
        } else if (msg.type === 'fill') {
          _handleStreamFill(msg);
        }
      } catch (_e) {}
    };

    _apWs.onclose = function() {
      _apWs = null;
      setTimeout(function() { _ensureAutopilotWs(); }, 5000);
    };

    _apWs.onerror = function() { /* onclose will fire */ };
  }

  function _handleStreamTick(msg) {
    var sym = msg.symbol;
    if (!sym) return;
    Object.keys(state.charts).forEach(function(sid) {
      var c = state.charts[sid];
      if (c && c.symbol === sym && c.series && msg.mid > 0) {
        var rawSec = msg.time || (Date.now() / 1000);
        var now = Math.floor(rawSec / 60) * 60;
        var lastBar = c.lastBar || {};
        if (lastBar.time === now) {
          lastBar.high = Math.max(lastBar.high, msg.mid);
          lastBar.low = Math.min(lastBar.low, msg.mid);
          lastBar.close = msg.mid;
          c.series.update({
            time: now,
            open: lastBar.open,
            high: lastBar.high,
            low: lastBar.low,
            close: msg.mid
          });
        } else {
          c.lastBar = { time: now, open: msg.mid, high: msg.mid, low: msg.mid, close: msg.mid };
          c.series.update({ time: now, open: msg.mid, high: msg.mid, low: msg.mid, close: msg.mid });
        }
      }
    });
  }

  function _handleStreamCandle(msg) {
    var sym = msg.symbol;
    if (!sym) return;
    var candleTime = Math.floor(msg.t || 0);
    if (!candleTime) return;
    Object.keys(state.charts).forEach(function(sid) {
      var c = state.charts[sid];
      if (c && c.symbol === sym && c.series) {
        c.series.update({
          time: candleTime, open: msg.o, high: msg.h, low: msg.l, close: msg.c
        });
        c.lastBar = { time: candleTime, open: msg.o, high: msg.h, low: msg.l, close: msg.c };
      }
    });
  }

  function _handleStreamFill(msg) {
    var sid = msg.session_id;
    if (sid) apLoadSessionDetail(Number(sid), true);
  }

  function _closeAutopilotWs() {
    if (_apWs) {
      try { _apWs.close(); } catch (_e) {}
      _apWs = null;
    }
    _apWsSymbols.clear();
  }

  function loadChartForSession(sessionId, symbol, fills) {
    var chartEl = document.getElementById('ap-chart-' + sessionId);
    if (!chartEl || !state.visibleSessionIds[sessionId] || !symbol || typeof LightweightCharts === 'undefined') return;

    _apWsSymbols.add(symbol.toUpperCase());
    _ensureAutopilotWs();

    var existing = state.charts[sessionId];
    if (existing && existing.chart && existing.symbol === symbol.toUpperCase()) {
      _updateOverlays(existing, fills);
      return;
    }

    fetch('/api/trading/ohlcv?ticker=' + encodeURIComponent(symbol) + '&interval=1m&period=1d', { credentials: 'same-origin' })
      .then(function(resp) { return resp.json(); })
      .then(function(payload) {
        if (!state.visibleSessionIds[sessionId]) return;
        var points = _parseOhlcvPoints(payload.data);
        if (!points.length) {
          chartEl.innerHTML = '<div class="ap-chart-empty">No chart data returned for this symbol.</div>';
          return;
        }
        if (existing && existing.chart) {
          try { existing.chart.remove(); } catch (_err) {}
        }
        chartEl.innerHTML = '';
        var chart = _createChart(chartEl);
        var series = chart.addCandlestickSeries({
          upColor: '#22c55e', downColor: '#ef4444',
          wickUpColor: '#22c55e', wickDownColor: '#ef4444', borderVisible: false
        });
        series.setData(points);
        var markers = _buildFillMarkers(fills);
        if (typeof series.setMarkers === 'function') series.setMarkers(markers);
        chart.timeScale().fitContent();
        var lastPt = points[points.length - 1];
        var chartState = {
          chart: chart, series: series, symbol: symbol.toUpperCase(),
          lastBar: lastPt ? { time: lastPt.time, open: lastPt.open, high: lastPt.high, low: lastPt.low, close: lastPt.close } : null,
          levelSeries: []
        };
        state.charts[sessionId] = chartState;
        _addLevelSegments(chartState, fills);
      })
      .catch(function() {
        if (chartEl) chartEl.innerHTML = '<div class="ap-chart-empty">Chart failed to load.</div>';
      });
  }

  function destroyChart(sessionId) {
    var existing = state.charts[sessionId];
    if (!existing || !existing.chart) return;
    _clearLevelSeries(existing);
    try {
      existing.chart.remove();
    } catch (_err) {}
    delete state.charts[sessionId];
  }

  function destroyAllCharts() {
    Object.keys(state.charts).forEach(function(key) {
      destroyChart(Number(key));
    });
    _closeAutopilotWs();
  }

  function applySessionDetail(detail) {
    var session = detail && detail.session || {};
    var sessionId = Number(session.id || 0);
    if (!sessionId) return;
    var fills = detail.simulated_fills || [];
    var fillsEl = document.getElementById('ap-fills-' + sessionId);
    var eventsEl = document.getElementById('ap-events-' + sessionId);
    var runtimeEl = document.getElementById('ap-runtime-' + sessionId);
    if (fillsEl) fillsEl.innerHTML = renderFillTable(fills);
    if (eventsEl) eventsEl.innerHTML = renderEventRows(detail.events || []);
    if (runtimeEl) {
      runtimeEl.textContent = jsonPreview({
        session: {
          blocked_reason: session.blocked_reason,
          canonical_operator_state: session.canonical_operator_state,
          next_action_required: session.next_action_required,
          runner_health: session.runner_health || {},
          viability_snapshot: detail.viability_snapshot || {},
          momentum_feedback: session.momentum_feedback || {}
        },
        execution_readiness: session.execution_readiness || {},
        paper_execution: session.paper_execution || {},
        live_execution: session.live_execution || {}
      });
    }
    loadChartForSession(sessionId, session.symbol, fills);
  }

  window.apRefreshSessions = function() {
    if (IS_GUEST) return Promise.resolve();
    var includeArchived = document.getElementById('ap-show-archived');
    var url = '/api/trading/momentum/automation/sessions?limit=100';
    if (includeArchived && includeArchived.checked) url += '&include_archived=true';
    return fetchJson(url, { credentials: 'same-origin' })
      .then(function(data) {
        state.sessions = data.sessions || [];
        renderSessions();
      })
      .catch(function(err) {
        showError(err.message || 'Failed to load sessions.');
      });
  };

  window.apLoadSessionDetail = function(sessionId, force) {
    if (IS_GUEST) return Promise.resolve();
    var cached = state.detailCache[sessionId];
    if (!force && cached && (Date.now() - cached.loadedAt < 10000)) {
      applySessionDetail(cached.payload);
      return Promise.resolve(cached.payload);
    }
    return fetchJson('/api/trading/momentum/automation/sessions/' + encodeURIComponent(sessionId), { credentials: 'same-origin' })
      .then(function(detail) {
        state.detailCache[sessionId] = { payload: detail, loadedAt: Date.now() };
        applySessionDetail(detail);
        return detail;
      })
      .catch(function(err) {
        var fillsEl = document.getElementById('ap-fills-' + sessionId);
        var eventsEl = document.getElementById('ap-events-' + sessionId);
        if (fillsEl) fillsEl.innerHTML = '<div class="ap-muted">Detail failed to load.</div>';
        if (eventsEl) eventsEl.innerHTML = '<div class="ap-muted">Detail failed to load.</div>';
        console.error(err);
      });
  };

  window.apSessionAction = function(sessionId, action) {
    if (!sessionId || !action) return;
    if ((action === 'delete' || action === 'stop') && !window.confirm('Are you sure you want to ' + action + ' this session?')) {
      return;
    }
    AP.trackEvent('SessionAction', { session_id: sessionId, action: action });
    fetchJson('/api/trading/momentum/automation/sessions/' + encodeURIComponent(sessionId) + '/' + encodeURIComponent(action), {
      method: 'POST',
      credentials: 'same-origin'
    }).then(function(payload) {
      AP.showSuccess(payload.message || ('Session ' + action + ' complete.'));
      delete state.detailCache[sessionId];
      apRefreshAll();
    }).catch(function(err) {
      showError(err.message || ('Failed to ' + action + ' session.'));
      AP.trackEvent('ErrorOccurred', { action: 'SessionAction', error: err.message });
    });
  };

  window.apRefreshAll = function() {
    return Promise.all([
      apRefreshSummary(),
      apRefreshOpportunities(),
      apRefreshSessions()
    ]);
  };

  function kickPoll() {
    if (state.pollHandle) window.clearInterval(state.pollHandle);
    state.pollHandle = window.setInterval(function() {
      if (IS_GUEST) return;
      apRefreshSummary();
      apRefreshOpportunities();
      apRefreshSessions().then(function() {
        Object.keys(state.visibleSessionIds).forEach(function(id) {
          apLoadSessionDetail(Number(id), true);
        });
      });
    }, 20000);
  }

  function bindUi() {
    document.querySelectorAll('#ap-mode-filters [data-mode]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        state.opportunityMode = btn.getAttribute('data-mode') || 'paper';
        AP.renderFilterState();
        AP.trackEvent('FilterChanged', { filter: 'mode', value: state.opportunityMode });
        apRefreshOpportunities();
      });
    });
    document.querySelectorAll('#ap-asset-filters [data-asset]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        state.assetFilter = btn.getAttribute('data-asset') || 'all';
        AP.renderFilterState();
        AP.trackEvent('FilterChanged', { filter: 'asset', value: state.assetFilter });
        apRefreshOpportunities();
      });
    });
    var search = document.getElementById('ap-opportunity-search');
    var searchWrap = document.getElementById('ap-search-wrap');
    var searchClear = document.getElementById('ap-search-clear');
    if (search) {
      search.addEventListener('input', function() {
        state.opportunitySearch = String(search.value || '').trim().toLowerCase();
        if (searchWrap) searchWrap.classList.toggle('has-value', search.value.length > 0);
        AP.renderOpportunities();
      });
    }
    if (searchClear && search) {
      searchClear.addEventListener('click', function() {
        search.value = '';
        state.opportunitySearch = '';
        if (searchWrap) searchWrap.classList.remove('has-value');
        AP.renderOpportunities();
        search.focus();
      });
    }
    document.addEventListener('keydown', function(e) {
      if (e.target.closest && e.target.closest('.ap-opp-header') && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault();
        e.target.click();
      }
    });
    var archived = document.getElementById('ap-show-archived');
    if (archived) {
      archived.addEventListener('change', function() {
        apRefreshSessions();
      });
    }
  }

  function boot() {
    AP.renderFilterState();
    bindUi();
    AP.trackEvent('PageViewed', { guest: IS_GUEST });
    if (IS_GUEST) {
      var guest = document.getElementById('ap-guest');
      if (guest) guest.style.display = 'block';
      return;
    }
    apRefreshAll();
    kickPoll();
  }

  boot();
})();
