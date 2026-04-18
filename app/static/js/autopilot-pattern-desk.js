(function() {
  'use strict';
  var AP = window.ChiliAutopilot;
  if (!AP || !AP.fetchJson) return;

  var esc = AP.esc;
  var fetchJson = AP.fetchJson;
  var showError = AP.showError;
  var showSuccess = AP.showSuccess;
  var badge = AP.badge;
  var IS_GUEST = AP.IS_GUEST;

  // Static per-position chart registry (ticker -> chart state). Each
  // card renders a one-shot 1d/1m candle chart without WebSocket streaming
  // (session cards own the live stream). Charts are destroyed + rebuilt
  // on every /api/trading/autotrader/desk refresh so the position list
  // stays consistent with the latest metrics.
  var _posCharts = {};
  var _posObserver = null;

  function el(id) { return document.getElementById(id); }

  function money(v) {
    var n = Number(v);
    if (!isFinite(n)) return 'n/a';
    var sign = n >= 0 ? '+' : '-';
    return sign + '$' + Math.abs(n).toFixed(2);
  }

  function pct(v) {
    var n = Number(v);
    if (!isFinite(n)) return 'n/a';
    var sign = n >= 0 ? '+' : '';
    return sign + n.toFixed(2) + '%';
  }

  function price(v, digits) {
    var n = Number(v);
    if (!isFinite(n)) return '—';
    var d = digits != null ? digits : (n >= 100 ? 2 : 4);
    return n.toFixed(d);
  }

  function dateShort(iso) {
    if (!iso) return 'n/a';
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return String(iso);
      return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
        + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    } catch (_e) {
      return String(iso);
    }
  }

  function quoteSourceLabel(src) {
    if (src === 'robinhood') return 'via Robinhood (live feed)';
    if (src === 'market_data') return 'via Massive/Polygon';
    return 'quote unavailable';
  }

  function renderBanner(data) {
    var b = el('ap-pattern-desk-banner');
    if (!b) return;
    var a = data.autotrader || {};
    var parts = [];
    if (IS_GUEST) {
      parts.push('Sign in (paired account) to view positions and desk controls.');
    } else {
      if (a.kill_switch_active) parts.push('<strong>Kill switch is ON</strong> — trading halted.');
      if (!a.env_enabled) parts.push('Autotrader scheduler: <strong>off</strong> (<code>CHILI_AUTOTRADER_ENABLED</code>). Desk controls still save; ticks need the flag on the server.');
      if (a.paused) parts.push('Desk: <strong>paused</strong> (no new autotrader entries).');
    }
    if (!parts.length) {
      b.style.display = 'none';
      b.innerHTML = '';
      return;
    }
    b.style.display = 'block';
    b.innerHTML = parts.join(' ');
  }

  function renderControls(data) {
    var c = el('ap-pattern-desk-controls');
    if (!c || IS_GUEST) {
      if (c) c.innerHTML = '';
      return;
    }
    var a = data.autotrader || {};
    var paused = !!a.paused;
    var liveEff = !!a.live_orders_effective;
    var deskOverride = !!a.desk_live_override;
    c.innerHTML = ''
      + '<button type="button" class="ap-btn ap-btn-primary" '
      +   'onclick="apPatternDeskAction(\'resume\')" ' + (paused ? '' : 'disabled ') + '>Run / Resume</button>'
      + '<button type="button" class="ap-btn" '
      +   'onclick="apPatternDeskAction(\'pause\')" ' + (!paused ? '' : 'disabled ') + '>Pause entries</button>'
      + '<label class="ap-pattern-desk-live">'
      + '  <input type="checkbox" id="ap-pattern-desk-live" ' + (liveEff ? 'checked ' : '') + '/>'
      + '  Robinhood live orders (effective)</label>'
      + (deskOverride ? ' <span class="ap-muted">(desk override)</span>' : ' <span class="ap-muted">(env default)</span>');
    var chk = el('ap-pattern-desk-live');
    if (chk) {
      chk.addEventListener('change', function() {
        var on = !!chk.checked;
        fetchJson('/api/trading/autotrader/desk', {
          method: 'PATCH',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ live_orders: on ? true : null })
        }).then(function() {
          showSuccess(on ? 'Live orders enabled (desk).' : 'Using env default for live (desk override cleared).');
          apPatternDeskRefresh();
        }).catch(function(err) {
          showError(err.message || 'Failed to update live mode');
          apPatternDeskRefresh();
        });
      });
    }
  }

  function rowBadges(r) {
    var out = [];
    out.push(badge(r.kind === 'trade' ? 'LIVE' : 'PAPER', r.kind === 'trade' ? 'live' : 'paper'));
    out.push(badge(r.asset_type === 'crypto' ? 'CRYPTO' : 'STOCK', r.asset_type === 'crypto' ? 'crypto' : 'stock'));
    if (r.auto_trader_v1) {
      out.push(badge('AUTO V1', 'compatible'));
    } else if (r.monitor_scope === 'plan_levels') {
      out.push(badge('PLAN', 'event'));
    } else {
      out.push(badge('LINKED', 'event'));
    }
    if (r.overrides && r.overrides.monitor_paused) out.push(badge('MONITOR PAUSED', 'paused'));
    if (r.overrides && r.overrides.synergy_excluded) out.push(badge('SYNERGY EXCLUDED', 'warn'));
    if (r.opened_today_et && (r.direction || 'long') === 'long') {
      out.push(badge('OPENED TODAY (PDT)', 'warn'));
    }
    if (r.scale_in_count) out.push(badge('SCALED x' + Number(r.scale_in_count), 'refined'));
    return out.join(' ');
  }

  function rowControls(r) {
    // No-adopt model: every CHILI-managed open position is managed by the
    // CHILI monitor by default. Pause is the per-position opt-out.
    var kind = r.kind;
    var id = r.id;
    var tkr = esc(r.ticker);
    var closeBtn = (r.close_supported !== false)
      ? '<button type="button" class="ap-btn ap-btn-live" '
        + 'onclick="apDeskPosCloseNow(\'' + kind + '\',' + id + ',\'' + tkr + '\')">Close now</button>'
      : '';
    var paused = r.overrides && r.overrides.monitor_paused;
    var excluded = r.overrides && r.overrides.synergy_excluded;
    var pauseBtn = paused
      ? '<button type="button" class="ap-btn ap-btn-primary" '
        + 'onclick="apDeskPosSetFlag(\'' + kind + '\',' + id + ',\'monitor_paused\',false)">Resume monitor</button>'
      : '<button type="button" class="ap-btn" '
        + 'onclick="apDeskPosSetFlag(\'' + kind + '\',' + id + ',\'monitor_paused\',true)">Pause monitor</button>';
    var synBtn = excluded
      ? '<button type="button" class="ap-btn" '
        + 'onclick="apDeskPosSetFlag(\'' + kind + '\',' + id + ',\'synergy_excluded\',false)">Allow synergy</button>'
      : '<button type="button" class="ap-btn" '
        + 'onclick="apDeskPosSetFlag(\'' + kind + '\',' + id + ',\'synergy_excluded\',true)">Exclude synergy</button>';
    return pauseBtn + ' ' + synBtn + ' ' + closeBtn;
  }

  function watchoutsFor(r) {
    var parts = [];
    var px = Number(r.current_price);
    var stop = Number(r.stop_loss != null ? r.stop_loss : r.stop_price);
    var tgt = Number(r.take_profit != null ? r.take_profit : r.target_price);
    if (isFinite(px) && isFinite(stop) && stop > 0 && px > 0) {
      var distStop = ((px - stop) / px) * 100.0;
      parts.push('Stop ' + price(stop) + ' (' + (distStop >= 0 ? '' : '') + distStop.toFixed(2) + '% away)');
    }
    if (isFinite(px) && isFinite(tgt) && tgt > 0 && px > 0) {
      var distTgt = ((tgt - px) / px) * 100.0;
      parts.push('Target ' + price(tgt) + ' (' + (distTgt >= 0 ? '+' : '') + distTgt.toFixed(2) + '% away)');
    }
    if (r.opened_today_et && (r.direction || 'long') === 'long' && r.kind === 'trade') {
      parts.push('Closing today may count as a day trade (PDT soft-warn).');
    }
    if (r.quote_source === 'unavailable') parts.push('No live quote available — metrics may be stale.');
    if (!parts.length) return '';
    return '<div class="ap-callout"><strong>Watchouts</strong>' + esc(parts.join(' · ')) + '</div>';
  }

  function monitorScopeLabel(r) {
    if (r.monitor_scope === 'plan_levels') return 'AI/manual position plan';
    if (r.auto_trader_v1) return 'AutoTrader v1';
    return 'Pattern-linked';
  }

  function patternCard(r) {
    var posId = r.kind + '-' + r.id;
    var pnlVal = Number(r.unrealized_pnl_usd);
    var pnlCls = isFinite(pnlVal) ? (pnlVal >= 0 ? 'positive' : 'negative') : '';
    var stop = r.stop_loss != null ? r.stop_loss : r.stop_price;
    var tgt = r.take_profit != null ? r.take_profit : r.target_price;
    var thesis = r.pattern_name
      ? ('CHILI-linked setup: ' + esc(r.pattern_name))
      : (r.monitor_scope === 'plan_levels'
        ? 'CHILI-managed AI/manual position plan'
        : (r.auto_trader_v1
          ? 'CHILI-managed AutoTrader v1 position'
          : ('CHILI-linked position (pattern #' + (r.scan_pattern_id || '?') + ')')));
    var linkageSummary = r.monitor_scope === 'plan_levels'
      ? 'Scope: AI/manual plan-level monitoring'
      : ('Pattern: ' + esc(r.pattern_name || ('#' + (r.scan_pattern_id || '?')))
        + '        &middot; Alert id: ' + esc(r.related_alert_id != null ? String(r.related_alert_id) : 'n/a'));
    return ''
      + '<article class="ap-session-card ap-pattern-card" data-position-id="' + esc(posId) + '" data-ticker="' + esc((r.ticker || '').toUpperCase()) + '">'
      + '  <div class="ap-session-card__header">'
      + '    <div>'
      + '      <div class="ap-symbol-lockup">'
      + '        <span class="ap-symbol">' + esc(r.ticker) + '</span>'
      + rowBadges(r)
      + '      </div>'
      + '      <div class="ap-subline" style="margin-top:8px;">'
      + '        ' + thesis
      + '        &middot; Qty ' + esc(String(r.quantity))
      + '        &middot; ' + ((r.direction || 'long').toLowerCase() === 'short' ? 'Short' : 'Long')
      + '        &middot; Opened ' + esc(dateShort(r.entry_date))
      + '      </div>'
      + '    </div>'
      + '    <div class="ap-actions">' + rowControls(r) + '</div>'
      + '  </div>'
      + '  <div class="ap-session-card__metrics">'
      + '    <div class="ap-metric"><label>Entry</label><b>' + esc(price(r.entry_price)) + '</b><small>Qty ' + esc(String(r.quantity)) + '</small></div>'
      + '    <div class="ap-metric"><label>Current</label><b>' + esc(price(r.current_price)) + '</b><small>' + esc(quoteSourceLabel(r.quote_source)) + '</small></div>'
      + '    <div class="ap-metric"><label>Unrealized P&amp;L</label><b class="' + pnlCls + '">'
      + (r.unrealized_pnl_usd != null ? esc(money(r.unrealized_pnl_usd)) : '—')
      + '</b><small>' + (r.unrealized_pnl_pct != null ? esc(pct(r.unrealized_pnl_pct)) : 'n/a') + '</small></div>'
      + '    <div class="ap-metric"><label>Levels</label><b>Stop ' + esc(price(stop)) + '</b><small>Target ' + esc(price(tgt)) + '</small></div>'
      + '  </div>'
      + '  <div class="ap-session-card__callouts">'
      + '    <div class="ap-callout"><strong>Thesis</strong>' + thesis + '</div>'
      + watchoutsFor(r)
      + '  </div>'
      + '  <div class="ap-tabs" role="tablist" aria-label="Position ' + esc(posId) + ' details">'
      + '    <button type="button" class="ap-tab-btn" role="tab" aria-selected="true" data-tab="chart" data-pos="' + esc(posId) + '">Chart</button>'
      + '    <button type="button" class="ap-tab-btn" role="tab" aria-selected="false" data-tab="details" data-pos="' + esc(posId) + '">Details</button>'
      + '  </div>'
      + '  <div class="ap-tab-panel active" data-panel="chart" data-pos="' + esc(posId) + '" role="tabpanel">'
      + '    <div class="ap-session-chart-wrap">'
      + '      <div class="ap-chart-head">'
      + '        <div class="ap-muted">1d / 1m replay with entry · stop · target overlays.</div>'
      + '      </div>'
      + '      <div id="ap-pos-chart-' + esc(posId) + '" class="ap-chart"><div class="ap-chart-empty">Waiting for chart data…</div></div>'
      + '    </div>'
      + '  </div>'
      + '  <div class="ap-tab-panel" data-panel="details" data-pos="' + esc(posId) + '" role="tabpanel">'
      + '    <div class="ap-detail-stack">'
      + '      <div class="ap-callout"><strong>Linkage</strong>'
      + '        ' + linkageSummary
      + '        &middot; Scale-ins: ' + esc(String(r.scale_in_count || 0))
      + '      </div>'
      + '      <div class="ap-callout"><strong>Broker / source</strong>'
      + '        Broker: ' + esc(r.broker_source || (r.kind === 'paper' ? 'paper' : 'n/a'))
      + '        &middot; Quote source: ' + esc(quoteSourceLabel(r.quote_source))
      + '        &middot; Monitor scope: ' + esc(monitorScopeLabel(r))
      + '        &middot; Auto v1: ' + (r.auto_trader_v1 ? 'yes' : 'no')
      + '      </div>'
      + '      <pre class="ap-pre">' + esc(JSON.stringify({
          entry_price: r.entry_price, current_price: r.current_price,
          unrealized_pnl_usd: r.unrealized_pnl_usd, unrealized_pnl_pct: r.unrealized_pnl_pct,
          stop: stop, target: tgt, direction: r.direction, quantity: r.quantity,
          overrides: r.overrides, opened_today_et: r.opened_today_et
        }, null, 2)) + '</pre>'
      + '    </div>'
      + '  </div>'
      + '</article>';
  }

  function bindPatternTabs() {
    document.querySelectorAll('#ap-pattern-desk-cards .ap-tab-btn[data-pos]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var pos = btn.getAttribute('data-pos');
        var tab = btn.getAttribute('data-tab');
        var card = btn.closest('.ap-pattern-card');
        if (!card) return;
        card.querySelectorAll('.ap-tab-btn[data-pos="' + pos + '"]').forEach(function(b) {
          b.setAttribute('aria-selected', 'false');
        });
        btn.setAttribute('aria-selected', 'true');
        card.querySelectorAll('.ap-tab-panel[data-pos="' + pos + '"]').forEach(function(p) {
          p.classList.toggle('active', p.getAttribute('data-panel') === tab);
        });
      });
    });
  }

  function _destroyPosCharts() {
    Object.keys(_posCharts).forEach(function(k) {
      try { _posCharts[k].chart.remove(); } catch (_e) {}
    });
    _posCharts = {};
  }

  function _observePosCards() {
    if (_posObserver) _posObserver.disconnect();
    if (typeof IntersectionObserver === 'undefined') return;
    _posObserver = new IntersectionObserver(function(entries) {
      entries.forEach(function(entry) {
        if (!entry.isIntersecting) return;
        var card = entry.target;
        var posId = card.getAttribute('data-position-id');
        var ticker = card.getAttribute('data-ticker');
        if (posId && ticker && !_posCharts[posId]) _loadPositionChart(posId, ticker, card);
      });
    }, { rootMargin: '200px 0px' });
    document.querySelectorAll('#ap-pattern-desk-cards .ap-pattern-card[data-position-id]').forEach(function(card) {
      _posObserver.observe(card);
    });
  }

  function _loadPositionChart(posId, ticker, card) {
    var chartEl = document.getElementById('ap-pos-chart-' + posId);
    if (!chartEl || typeof LightweightCharts === 'undefined') return;
    fetch('/api/trading/ohlcv?ticker=' + encodeURIComponent(ticker) + '&interval=1m&period=1d', { credentials: 'same-origin' })
      .then(function(resp) { return resp.json(); })
      .then(function(payload) {
        var points = (payload && payload.data ? payload.data : []).map(function(row) {
          var t = row.time || row.date || row.timestamp;
          var sec;
          if (typeof t === 'number') sec = t > 1e12 ? Math.floor(t / 1000) : Math.floor(t);
          else sec = Math.floor(Date.parse(t) / 1000);
          if (!isFinite(sec)) return null;
          return { time: sec, open: Number(row.open), high: Number(row.high), low: Number(row.low), close: Number(row.close) };
        }).filter(function(p) { return p && isFinite(p.close); });
        if (!points.length) {
          chartEl.innerHTML = '<div class="ap-chart-empty">No chart data returned for ' + ticker + '.</div>';
          return;
        }
        chartEl.innerHTML = '';
        var chart = LightweightCharts.createChart(chartEl, {
          layout: { background: { color: '#020617' }, textColor: '#cbd5e1' },
          grid: { vertLines: { color: 'rgba(148,163,184,0.08)' }, horzLines: { color: 'rgba(148,163,184,0.08)' } },
          rightPriceScale: { borderColor: 'rgba(148,163,184,0.14)' },
          timeScale: { borderColor: 'rgba(148,163,184,0.14)', timeVisible: true, secondsVisible: false },
          crosshair: { mode: 0 }
        });
        var series = chart.addCandlestickSeries({
          upColor: '#22c55e', downColor: '#ef4444',
          borderUpColor: '#22c55e', borderDownColor: '#ef4444',
          wickUpColor: '#22c55e', wickDownColor: '#ef4444'
        });
        series.setData(points);
        var meta = _posCardMeta(card);
        var t0 = points[0].time;
        var t1 = points[points.length - 1].time;
        [
          { price: meta.entry, color: '#facc15', style: LightweightCharts.LineStyle.Dashed, label: 'Entry' },
          { price: meta.stop,  color: '#ef4444', style: LightweightCharts.LineStyle.Dotted, label: 'Stop' },
          { price: meta.target, color: '#22c55e', style: LightweightCharts.LineStyle.Dotted, label: 'Target' }
        ].forEach(function(lv) {
          var px = Number(lv.price);
          if (!isFinite(px) || px <= 0) return;
          var ls = chart.addLineSeries({
            color: lv.color, lineWidth: 1, lineStyle: lv.style,
            priceLineVisible: false, lastValueVisible: false,
            crosshairMarkerVisible: false, pointMarkersVisible: false
          });
          ls.setData([{ time: t0, value: px }, { time: t1, value: px }]);
        });
        chart.timeScale().fitContent();
        _posCharts[posId] = { chart: chart, series: series };
      })
      .catch(function() {
        chartEl.innerHTML = '<div class="ap-chart-empty">Chart fetch failed for ' + ticker + '.</div>';
      });
  }

  function _posCardMeta(card) {
    var pre = card.querySelector('.ap-pre');
    if (!pre) return {};
    try { return JSON.parse(pre.textContent || '{}'); } catch (_e) { return {}; }
  }

  function renderCards(data) {
    var host = el('ap-pattern-desk-cards');
    var empty = el('ap-pattern-desk-empty');
    if (!host) return;
    var rows = (data.trades || []).concat(data.paper_trades || []);
    _destroyPosCharts();
    if (!rows.length) {
      host.innerHTML = '';
      if (empty) empty.style.display = 'block';
      return;
    }
    if (empty) empty.style.display = 'none';
    host.innerHTML = rows.map(patternCard).join('');
    bindPatternTabs();
    _observePosCards();
  }

  window.apPatternDeskRefresh = function() {
    if (IS_GUEST) {
      renderBanner({});
      renderCards({ trades: [], paper_trades: [] });
      return;
    }
    fetchJson('/api/trading/autotrader/desk').then(function(data) {
      renderBanner(data);
      renderControls(data);
      renderCards(data);
    }).catch(function(err) {
      showError(err.message || 'Failed to load pattern desk');
    });
  };

  window.apDeskPosSetFlag = function(kind, tradeId, field, value) {
    if (IS_GUEST) return;
    var body = { kind: kind };
    body[field] = !!value;
    fetchJson('/api/trading/autotrader/positions/' + encodeURIComponent(tradeId), {
      method: 'PATCH',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function() {
      var human = field === 'monitor_paused'
        ? (value ? 'Monitor paused for this position.' : 'Monitor resumed.')
        : (value ? 'Excluded from future synergy scale-ins.' : 'Synergy scale-ins allowed again.');
      showSuccess(human);
      apPatternDeskRefresh();
    }).catch(function(err) {
      showError(err.message || 'Per-position update failed');
    });
  };

  window.apDeskPosCloseNow = function(kind, tradeId, ticker) {
    if (IS_GUEST) return;
    var msg = 'Close ' + (ticker || ('#' + tradeId)) + ' now (' + kind + ')?'
      + '\n\nLive trades: this places an immediate market sell via Robinhood.'
      + '\nPaper trades: the row is closed at the current quote (with slippage).';
    if (!window.confirm(msg)) return;
    fetchJson('/api/trading/autotrader/positions/' + encodeURIComponent(tradeId) + '/close', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: kind, confirm: true })
    }).then(function(resp) {
      if (resp && resp.ok) {
        showSuccess('Closed ' + (ticker || '#' + tradeId) + ' at ' + resp.exit_price);
      } else {
        showError((resp && resp.error) ? String(resp.error) : 'Close failed');
      }
      apPatternDeskRefresh();
    }).catch(function(err) {
      showError(err.message || 'Close failed');
      apPatternDeskRefresh();
    });
  };

  window.apPatternDeskAction = function(action) {
    var paused = action === 'pause';
    fetchJson('/api/trading/autotrader/desk', {
      method: 'PATCH',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paused: paused })
    }).then(function() {
      showSuccess(paused ? 'New autotrader entries paused.' : 'Autotrader entries resumed.');
      apPatternDeskRefresh();
    }).catch(function(err) {
      showError(err.message || 'Desk update failed');
    });
  };

  document.addEventListener('DOMContentLoaded', function() {
    var btn = el('ap-pattern-desk-refresh');
    if (btn) btn.addEventListener('click', apPatternDeskRefresh);
    apPatternDeskRefresh();
  });
})();
