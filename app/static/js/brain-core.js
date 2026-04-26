function toggleTheme() {
  var html = document.documentElement;
  var current = html.getAttribute('data-theme');
  var next = current === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  localStorage.setItem('chili-theme', next);
}

function escHtml(s) {
  var d = document.createElement('div'); d.textContent = s; return d.innerHTML;
}

/** Phase 10: neural momentum desk strip on Trading â†’ Runtime (read-only API). */
function tbRefreshMomentumNeuralStrip() {
  var el = document.getElementById('tb-momentum-neural-strip');
  if (!el) return;
  fetch('/api/trading/brain/momentum/desk', { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d || !d.ok) {
        el.style.display = 'none';
        return;
      }
      el.style.display = 'block';
      var panel = d.momentum_panel || {};
      var pv = panel.paper_vs_live_30d || {};
      var pap = pv.paper || {};
      var liv = pv.live || {};
      var bd = d.badges || {};
      var parts = [];
      parts.push('<strong style="color:var(--text)">Momentum (neural)</strong> â€” ');
      parts.push(escHtml(panel.headline || 'Desk'));
      parts.push(' <span style="color:var(--text-muted)">|</span> ');
      parts.push('30d paper <strong>' + escHtml(String(pap.n || 0)) + '</strong>');
      if (pap.mean_return_bps != null) parts.push(' avg bps ' + escHtml(String(pap.mean_return_bps)));
      parts.push(' Â· live <strong>' + escHtml(String(liv.n || 0)) + '</strong>');
      if (liv.mean_return_bps != null) parts.push(' avg bps ' + escHtml(String(liv.mean_return_bps)));
      if (pv.live_sample_caution) {
        parts.push(' <span style="color:#f59e0b;font-weight:600">live sample low</span>');
      }
      parts.push('<br/><span style="font-size:9px;color:var(--text-muted)">');
      parts.push('mesh ' + (bd.neural_mesh_on ? 'on' : 'off'));
      parts.push(' Â· momentum ' + (bd.momentum_neural_on ? 'on' : 'off'));
      parts.push(' Â· feedback ' + (bd.feedback_enabled ? 'on' : 'off'));
      if (bd.governance_kill_switch) parts.push(' Â· <span style="color:#f87171">kill switch</span>');
      parts.push(' Â· <a href="/trading#momentum" style="color:var(--accent)">Trading momentum</a>');
      parts.push(' Â· <a href="/trading/autopilot" style="color:var(--accent)">Autopilot</a>');
      parts.push('</span>');
      el.innerHTML = parts.join('');
    })
    .catch(function() {
      el.style.display = 'none';
    });
}

function openBrainModal(title, bodyHtml, footerHtml, footerClass, showPineHeaderTools) {
  document.getElementById('brain-modal-title').textContent = title;
  document.getElementById('brain-modal-body').innerHTML = bodyHtml;
  var tools = document.getElementById('brain-modal-header-tools');
  if (tools) {
    if (showPineHeaderTools) {
      tools.classList.add('bm-tools-visible');
    } else {
      tools.classList.remove('bm-tools-visible');
    }
  }
  var footer = document.getElementById('brain-modal-footer');
  footer.className = 'brain-modal-footer' + (footerClass ? ' ' + footerClass : '');
  if (footerHtml) { footer.innerHTML = footerHtml; footer.style.display = ''; }
  else { footer.innerHTML = ''; footer.style.display = 'none'; }
  document.getElementById('brain-modal-overlay').classList.add('active');
}
var _evWrProgressChart = null;
function _disposeEvWrProgressChart() {
  if (_evWrProgressChart) {
    try { _evWrProgressChart.remove(); } catch (e) {}
    _evWrProgressChart = null;
  }
}

/** Read fetch Response as text; parse JSON only when body looks like JSON (avoids JSON.parse on HTML error pages). */
function parseFetchJson(r) {
  return r.text().then(function(t) {
    var trimmed = (t || '').trim();
    var first = trimmed.charAt(0);
    if (first === '{' || first === '[') {
      try {
        return JSON.parse(trimmed);
      } catch (e) {
        return Promise.reject(new Error('Invalid JSON in response (HTTP ' + r.status + '): ' + e.message));
      }
    }
    if (!r.ok) {
      var snippet = trimmed.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 120);
      return Promise.reject(new Error('HTTP ' + r.status + (snippet ? ': ' + snippet : '')));
    }
    return Promise.reject(new Error('Expected JSON; got: ' + (trimmed.slice(0, 80).replace(/\s+/g, ' ') || 'empty')));
  });
}

/** Pattern-card badge text for sector / ticker_specific scope (never throws on bad JSON). */
function _fmtScopeTickersBadge(p) {
  if (!p || !p.ticker_scope || p.ticker_scope === 'universal') return '';
  if (!p.scope_tickers) return escHtml(p.ticker_scope);
  try {
    var arr = JSON.parse(p.scope_tickers);
    if (!Array.isArray(arr)) return escHtml(p.ticker_scope);
    var parts = arr.map(function(t) { return escHtml(String(t)); });
    if (p.ticker_scope === 'sector') return 'sector: ' + parts.join(', ');
    if (p.ticker_scope === 'ticker_specific') return 'tickers: ' + parts.slice(0, 3).join(', ');
  } catch (e) {}
  return escHtml(p.ticker_scope);
}

function _renderEvWrProgress(series) {
  _disposeEvWrProgressChart();
  var wrap = document.getElementById('ev-wr-progress-wrap');
  var el = document.getElementById('ev-wr-progress-chart');
  if (!wrap || !el) return;
  wrap.style.display = 'block';
  if (!series || series.length === 0) {
    el.innerHTML = '<div style="padding:12px;font-size:11px;color:var(--text-muted);text-align:center;border:1px dashed var(--border);border-radius:8px">No stored backtests for this insight yet (queue or run backtests for this card).</div>';
    return;
  }
  var chartData = series.map(function(pt) { return { time: pt.time, value: pt.win_rate }; });
  /* Lightweight-charts needs strictly increasing time; backend de-dupes; pad single point */
  if (chartData.length === 1) {
    chartData.push({ time: chartData[0].time + 1, value: chartData[0].value });
  }
  el.innerHTML = '';
  try {
    var isDark = document.documentElement.getAttribute('data-theme') === 'dark' || window.matchMedia('(prefers-color-scheme: dark)').matches;
    var bgColor = isDark ? '#1a1a2e' : '#ffffff';
    var gridColor = isDark ? 'rgba(255,255,255,.08)' : 'rgba(0,0,0,.06)';
    _evWrProgressChart = LightweightCharts.createChart(el, {
      width: el.clientWidth,
      height: 140,
      layout: { background: { type: 'solid', color: bgColor }, textColor: isDark ? '#a0a0b0' : '#666', fontSize: 10 },
      grid: { vertLines: { color: gridColor }, horzLines: { color: gridColor } },
      rightPriceScale: { borderColor: gridColor },
      timeScale: { borderColor: gridColor, timeVisible: true, secondsVisible: false },
    });
    var line = _evWrProgressChart.addLineSeries({ color: '#22c55e', lineWidth: 2 });
    line.setData(chartData);
    _evWrProgressChart.timeScale().fitContent();
    new ResizeObserver(function() {
      if (_evWrProgressChart && el) _evWrProgressChart.applyOptions({ width: el.clientWidth });
    }).observe(el);
  } catch (err) {
    console.error('Pattern evidence win-rate chart:', err);
    el.innerHTML = '<div style="padding:12px;font-size:11px;color:#ef4444;text-align:center">Could not draw chart (see browser console).</div>';
  }
}
function closeBrainModal() {
  _disposeEvWrProgressChart();
  var tools = document.getElementById('brain-modal-header-tools');
  if (tools) { tools.classList.remove('bm-tools-visible'); }
  document.getElementById('brain-modal-overlay').classList.remove('active');
}
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && document.getElementById('brain-modal-overlay').classList.contains('active')) closeBrainModal();
});

function goToTicker(ticker) { window.open('/trading?ticker=' + encodeURIComponent(ticker), '_blank'); }

/* â”€â”€ Ring SVG helper â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
function _makeRingSvg(pct, color) {
  var r = 26, c = Math.PI * 2 * r;
  var dash = c * (pct / 100);
  return '<svg width="64" height="64" viewBox="0 0 64 64">' +
    '<circle cx="32" cy="32" r="' + r + '" fill="none" stroke="var(--border)" stroke-width="5"/>' +
    '<circle cx="32" cy="32" r="' + r + '" fill="none" stroke="' + (color || '#8b5cf6') + '" stroke-width="5" ' +
    'stroke-dasharray="' + dash + ' ' + (c - dash) + '" stroke-linecap="round"/></svg>';
}

/* â”€â”€ Sidebar status polling â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
function setSidebarBannerFallback() {
  var banner = document.getElementById('hub-status-banner');
  if (banner) banner.textContent = 'Trading: --';
}

function updateSidebarStatus() {
  var banner = document.getElementById('hub-status-banner');
  return fetch('/api/brain/status').then(function(r) {
    if (!r.ok) { setSidebarBannerFallback(); return null; }
    return r.json();
  }).then(function(d) {
    if (!banner) return;
    if (!d || !d.ok) { setSidebarBannerFallback(); return; }
    var t = d.trading || {};
    var dot = document.getElementById('trading-status-dot');
    if (t.running) {
      if (dot) dot.className = 'b-domain-dot learning';
      banner.textContent = 'Trading: ' + (t.phase || 'learning') + '...';
    } else {
      if (dot) dot.className = 'b-domain-dot idle';
      var lastRun = t.last_run ? timeSince(new Date(t.last_run)) + ' ago' : 'never';
      banner.textContent = 'Trading: ' + lastRun;
    }
    var c = d.code || {};
    var projDot = document.getElementById('project-status-dot');
    if (projDot) {
      projDot.className = c.running ? 'b-domain-dot learning' : 'b-domain-dot idle';
    }
    if (c.running) {
      banner.textContent += ' | Project: ' + (c.phase || 'learning') + '...';
    } else if (c.last_run) {
      banner.textContent += ' | Project: ' + timeSince(new Date(c.last_run)) + ' ago';
    }
    var r = d.reasoning || {};
    var rDot = document.getElementById('reasoning-status-dot');
    if (rDot) {
      rDot.className = r.running ? 'b-domain-dot learning' : 'b-domain-dot idle';
    }
    if (r.running) {
      banner.textContent += ' | Reasoning: ' + (r.phase || 'learning') + '...';
    } else if (r.last_run) {
      banner.textContent += ' | Reasoning: ' + timeSince(new Date(r.last_run)) + ' ago';
    }
    var j = d.jobs || {};
    var jDot = document.getElementById('jobs-status-dot');
    if (jDot) {
      jDot.className = j.running ? 'b-domain-dot learning' : 'b-domain-dot idle';
    }
    if (j.running) {
      banner.textContent += ' | Jobs: ' + (j.phase || 'scheduler') + '...';
    } else if (j.last_run) {
      banner.textContent += ' | Jobs: ' + timeSince(new Date(j.last_run)) + ' ago';
    }
  }).catch(function(){ setSidebarBannerFallback(); });
}

function timeSince(date) {
  var seconds = Math.floor((new Date() - date) / 1000);
  if (seconds < 60) return seconds + 's';
  var minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes + 'm';
  var hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + 'h';
  return Math.floor(hours / 24) + 'd';
}

/* â”€â”€ Brain Worker Dashboard â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
var _activeDomain = (typeof window.__CHILI_BRAIN_INITIAL_DOMAIN__ === 'string')
  ? window.__CHILI_BRAIN_INITIAL_DOMAIN__
  : null;
var _projectDashboardLoaded = false;
var _reasoningDashboardLoaded = false;
var _tradingDashboardBootstrapped = false;
var _activeProjectLens = 'architect'; /* kept for compat */

function brainBootstrapTradingDesk() {
  if (_tradingDashboardBootstrapped) return;
  _tradingDashboardBootstrapped = true;
  loadBrainDashboard();
  initTradingBrainAssistant();
  startBwPolling();
  loadBrainStopDecisions();
}

function loadBrainStopDecisions() {
  fetch('/api/trading/stops/decisions?limit=20').then(function(r){return r.json();}).then(function(d) {
    var el = document.getElementById('brain-stop-decisions');
    if (!el) return;
    if (!d.ok || !d.decisions || !d.decisions.length) {
      el.innerHTML = '<div style="font-size:10px;color:var(--text-muted);padding:8px;text-align:center;">No stop decisions yet. The stop engine evaluates your open positions on a schedule.</div>';
      return;
    }
    var html = '';
    d.decisions.forEach(function(sd) {
      var tc = {'STOP_HIT':'#ef4444','TARGET_HIT':'#22c55e','STOP_APPROACHING':'#f59e0b','BREAKEVEN_REACHED':'#22c55e','STOP_TIGHTENED':'#3b82f6'}[sd.trigger] || '#888';
      var time = sd.as_of_ts ? new Date(sd.as_of_ts).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
      html += '<div style="display:flex;gap:6px;align-items:center;padding:4px 0;border-bottom:1px solid var(--border);font-size:10px;">';
      html += '<span style="padding:1px 5px;border-radius:4px;font-size:8px;font-weight:700;background:'+tc+'22;color:'+tc+';white-space:nowrap;">'+(sd.trigger||sd.state)+'</span>';
      html += '<span style="font-weight:600;">Trade #'+sd.trade_id+'</span>';
      if(sd.old_stop && sd.new_stop && sd.old_stop !== sd.new_stop) html += '<span>$'+sd.old_stop.toFixed(2)+' &rarr; $'+sd.new_stop.toFixed(2)+'</span>';
      html += '<span style="flex:1;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'+sd.reason+'</span>';
      html += '<span style="color:var(--text-muted);font-size:9px;white-space:nowrap;">'+time+'</span>';
      html += '</div>';
    });
    el.innerHTML = html;
  }).catch(function() {
    var el = document.getElementById('brain-stop-decisions');
    if (el) el.innerHTML = '<div style="font-size:10px;color:var(--text-muted);padding:8px;">Failed to load.</div>';
  });
}

function brainSetUrlDomain(which) {
  try {
    var u = new URL(window.location.href);
    if (which === 'hub' || which == null) {
      u.searchParams.delete('domain');
    } else {
      u.searchParams.set('domain', which);
    }
    var q = u.searchParams.toString();
    history.replaceState({}, '', u.pathname + (q ? '?' + q : '') + (u.hash || ''));
  } catch (e) {}
}

function brainReadUrlDomainString() {
  try {
    var rawParam = new URLSearchParams(window.location.search || '').get('domain');
    if (rawParam == null || rawParam === '') return null;
    var v = String(rawParam).toLowerCase();
    if (v === 'hub') return 'hub';
    if (v === 'code') return 'project';
    if (v === 'trading' || v === 'project' || v === 'reasoning' || v === 'jobs' || v === 'context') return v;
    return '__invalid__';
  } catch (e) {
    return null;
  }
}

function showBrainHub(opts) {
  opts = opts || {};
  _activeDomain = 'hub';
  var hub = document.getElementById('domain-hub');
  if (hub) hub.style.display = 'flex';
  var tr = document.getElementById('domain-trading');
  if (tr) tr.style.display = 'none';
  var pr = document.getElementById('domain-project');
  if (pr) pr.style.display = 'none';
  var rd = document.getElementById('domain-reasoning');
  if (rd) rd.style.display = 'none';
  var ctx = document.getElementById('domain-context');
  if (ctx) ctx.style.display = 'none';
  var back = document.getElementById('brain-nav-all-domains');
  if (back) back.style.display = 'none';
  if (!opts.skipUrl) brainSetUrlDomain('hub');
}

function renderBrainHubCards() {
  return fetch('/api/brain/domains').then(function(r) { return r.json(); }).then(function(d) {
    var grid = document.getElementById('domain-hub-grid');
    if (!grid) return;
    if (!d || !d.ok || !d.domains || !d.domains.length) {
      grid.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">Domains unavailable. Try again later.</div>';
      return;
    }
    grid.innerHTML = '';
    d.domains.forEach(function(dom) {
      var id = dom.id;
      var card = document.createElement('div');
      card.className = 'b-hub-card';
      card.setAttribute('role', 'button');
      card.tabIndex = 0;
      var icon = dom.icon || '\u2728';
      var label = dom.label || id;
      var desc = dom.description || '';
      var title = document.createElement('div');
      title.className = 'b-hub-card-title';
      title.appendChild(document.createTextNode(label));
      var dot = document.createElement('span');
      dot.className = (dom.status === 'learning') ? 'b-domain-dot learning' : 'b-domain-dot idle';
      dot.id = id + '-status-dot';
      title.appendChild(dot);
      var p = document.createElement('p');
      p.className = 'b-hub-card-desc';
      p.textContent = desc;
      var body = document.createElement('div');
      body.className = 'b-hub-card-body';
      body.appendChild(title);
      body.appendChild(p);
      var ic = document.createElement('span');
      ic.className = 'b-hub-card-icon';
      ic.textContent = icon;
      var inner = document.createElement('div');
      inner.className = 'b-hub-card-inner';
      inner.appendChild(ic);
      inner.appendChild(body);
      card.appendChild(inner);
      function go() {
        if (dom.navigate_url) {
          window.location.href = dom.navigate_url;
          return;
        }
        switchDomain(id);
      }
      card.onclick = go;
      card.onkeydown = function(e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          go();
        }
      };
      grid.appendChild(card);
    });
  }).catch(function() {
    var grid = document.getElementById('domain-hub-grid');
    if (grid) grid.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">Could not load domains.</div>';
  });
}

function switchDomain(domain, opts) {
  opts = opts || {};
  if (domain === 'code') domain = 'project';
  _activeDomain = domain;
  var hub = document.getElementById('domain-hub');
  if (hub) hub.style.display = 'none';
  var tr = document.getElementById('domain-trading');
  if (tr) tr.style.display = domain === 'trading' ? 'flex' : 'none';
  var pr = document.getElementById('domain-project');
  if (pr) pr.style.display = domain === 'project' ? '' : 'none';
  var rd = document.getElementById('domain-reasoning');
  if (rd) rd.style.display = domain === 'reasoning' ? '' : 'none';
  var ctx = document.getElementById('domain-context');
  if (ctx) ctx.style.display = domain === 'context' ? '' : 'none';
  var back = document.getElementById('brain-nav-all-domains');
  if (back) back.style.display = 'inline-block';

  if (!opts.skipUrl) brainSetUrlDomain(domain);

  if (domain === 'trading') {
    brainBootstrapTradingDesk();
  }
  if (domain === 'project' && !_projectDashboardLoaded) {
    _projectDashboardLoaded = true;
    initProjectAgentBar();
    loadCodeDashboard();
    loadAgentMessageFeed();
    loadProjectBrainStatus();
  }
  if (domain === 'project' && typeof window.brainProjectRefreshBootstrap === 'function') {
    window.brainProjectRefreshBootstrap();
  }
  if (domain === 'reasoning' && !_reasoningDashboardLoaded) {
    _reasoningDashboardLoaded = true;
    loadReasoningDashboard();
  }
  if (domain === 'context') {
    loadContextDashboard();
  }
}

/* ── Context Brain dashboard loader (Phase F) ──────────────────── */
function loadContextDashboard() {
  function safe(v, fb) { return (v == null || v === '') ? (fb || '--') : v; }
  function fmtPct(num, den) {
    if (!den || den <= 0) return '0%';
    return Math.round((num / den) * 100) + '%';
  }
  fetch('/api/brain/context/status').then(function(r){return r.json();}).then(function(d){
    if (!d) return;
    var rs = d.runtime_state || {};
    document.getElementById('ctx-mode').textContent = safe(rs.mode);
    document.getElementById('ctx-budget').textContent = safe(rs.token_budget_per_request) + ' tokens';
    var spent = safe(rs.spent_today_distillation_usd, '0');
    var cap = safe(rs.daily_distillation_usd_cap, '0');
    document.getElementById('ctx-cap').textContent = '$' + spent + ' / $' + cap;
    document.getElementById('ctx-version').textContent = 'v' + safe(rs.learned_strategy_version, '1');
    document.getElementById('ctx-learning').textContent = rs.learning_enabled ? 'enabled' : 'paused';
    document.getElementById('ctx-last-cycle').textContent = safe(rs.last_learning_cycle_at, 'never');
    var info = document.getElementById('context-status-info');
    if (info) {
      info.textContent = 'mode=' + safe(rs.mode) + ' · '
        + (d.learned_weights_count || 0) + ' learned weights · '
        + (d.distillation_cache_size || 0) + ' cache entries';
    }
    var dist = d.intent_distribution_24h || {};
    var keys = Object.keys(dist);
    var intentEl = document.getElementById('ctx-intents');
    if (intentEl) {
      if (!keys.length) {
        intentEl.innerHTML = '<div style="color:var(--text-muted)">No assemblies yet in the last 24h.</div>';
      } else {
        var rows = keys.sort(function(a,b){return dist[b]-dist[a];}).map(function(k){
          return '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">'
            + '<span>' + k + '</span><span style="color:var(--text-muted)">' + dist[k] + '</span></div>';
        });
        intentEl.innerHTML = rows.join('');
      }
    }
  }).catch(function(){});

  fetch('/api/brain/context/sources').then(function(r){return r.json();}).then(function(d){
    var el = document.getElementById('ctx-sources');
    if (!el) return;
    var items = (d && d.items) || [];
    if (!items.length) {
      el.innerHTML = '<div style="color:var(--text-muted)">No source data yet.</div>';
      return;
    }
    el.innerHTML = items.map(function(s){
      var rate = Math.round((s.selection_rate || 0) * 100);
      return '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">'
        + '<span><strong>' + s.source_id + '</strong></span>'
        + '<span style="color:var(--text-muted)">'
        +   s.total_selected + '/' + s.total_returned + ' (' + rate + '% selected)'
        + '</span></div>';
    }).join('');
  }).catch(function(){});

  fetch('/api/brain/context/assemblies?limit=15').then(function(r){return r.json();}).then(function(d){
    var el = document.getElementById('ctx-assemblies');
    if (!el) return;
    var items = (d && d.items) || [];
    if (!items.length) {
      el.innerHTML = '<div style="color:var(--text-muted)">No assemblies yet — try chatting to populate this.</div>';
      return;
    }
    el.innerHTML = items.map(function(a){
      var sources = a.sources_used || {};
      var srcKeys = Object.keys(sources);
      var when = (a.created_at || '').replace('T',' ').slice(0,19);
      return '<div style="padding:8px;margin:4px 0;background:var(--bg);border:1px solid var(--border);border-radius:6px">'
        + '<div style="display:flex;justify-content:space-between;font-size:12px">'
        +   '<span><strong>#' + a.id + '</strong> · ' + a.intent + ' (' + a.intent_confidence + ')</span>'
        +   '<span style="color:var(--text-muted)">' + when + '</span>'
        + '</div>'
        + '<div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
        +   a.total_tokens_input + '/' + a.budget_token_cap + ' tok · '
        +   a.elapsed_ms + 'ms · '
        +   srcKeys.join(', ')
        + '</div></div>';
    }).join('');
  }).catch(function(){});

  // F.4-F.6 Gateway learning loop visibility.
  loadGatewayLearningPanel();
}

function loadGatewayLearningPanel() {
  // Top patterns
  fetch('/api/brain/context/gateway/patterns?min_confidence=0.4&limit=10').then(function(r){return r.json();}).then(function(d){
    var el = document.getElementById('ctx-patterns');
    if (!el) return;
    var items = (d && d.items) || [];
    if (!items.length) { el.innerHTML = '<div style="color:var(--text-muted)">No patterns yet — distiller needs gateway calls + outcomes.</div>'; return; }
    el.innerHTML = items.map(function(p){
      var q = (p.avg_quality === null) ? 'NA' : Number(p.avg_quality).toFixed(2);
      var sr = (p.success_rate === null) ? 'NA' : (Number(p.success_rate)*100).toFixed(0)+'%';
      return '<div style="padding:6px 8px;margin:3px 0;background:var(--bg);border:1px solid var(--border);border-radius:4px">'
        + '<div style="font-weight:600">' + p.purpose + ' · ' + p.pattern_kind + ' = ' + p.pattern_key + '</div>'
        + '<div style="font-size:11px;color:var(--text-muted)">n=' + p.sample_count + ' · q=' + q + ' · ok=' + sr + ' · conf=' + Number(p.confidence).toFixed(2) + '</div>'
        + '</div>';
    }).join('');
  }).catch(function(){});

  // Pending proposals
  fetch('/api/brain/context/gateway/proposals?status=pending&limit=15').then(function(r){return r.json();}).then(function(d){
    var el = document.getElementById('ctx-proposals');
    if (!el) return;
    var items = (d && d.items) || [];
    if (!items.length) { el.innerHTML = '<div style="color:var(--text-muted)">No proposals pending.</div>'; return; }
    el.innerHTML = items.map(function(p){
      return '<div style="padding:6px 8px;margin:3px 0;background:var(--bg);border:1px solid var(--border);border-radius:4px">'
        + '<div style="font-weight:600">' + p.purpose + ' · ' + p.field_name + ': ' + (p.current_value||'-') + ' → <strong>' + p.proposed_value + '</strong> [' + p.severity + ']</div>'
        + '<div style="font-size:11px;color:var(--text-muted);margin:2px 0">' + (p.justification||'') + '</div>'
        + '<div style="font-size:11px"><a href="#" data-act="approve" data-pid="'+p.id+'" style="color:#3aa37a">Approve</a> · '
        +   '<a href="#" data-act="reject" data-pid="'+p.id+'" style="color:#c0463a">Reject</a></div>'
        + '</div>';
    }).join('');
    // Wire approve/reject links
    el.querySelectorAll('a[data-act]').forEach(function(a){
      a.addEventListener('click', function(ev){
        ev.preventDefault();
        var act = a.getAttribute('data-act');
        var pid = a.getAttribute('data-pid');
        fetch('/api/brain/context/gateway/proposals/' + pid + '/decide', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({decision: act})
        }).then(function(r){return r.json();}).then(function(){ loadGatewayLearningPanel(); }).catch(function(){});
      });
    });
  }).catch(function(){});

  // Recent outcomes
  fetch('/api/brain/context/gateway/outcomes?limit=15').then(function(r){return r.json();}).then(function(d){
    var el = document.getElementById('ctx-outcomes');
    if (!el) return;
    var items = (d && d.items) || [];
    if (!items.length) { el.innerHTML = '<div style="color:var(--text-muted)">No outcomes recorded yet.</div>'; return; }
    el.innerHTML = items.map(function(o){
      var q = (o.quality_signal === null) ? 'NA' : Number(o.quality_signal).toFixed(2);
      var when = (o.measured_at || '').replace('T',' ').slice(0,19);
      return '<div style="padding:4px 8px;margin:2px 0;background:var(--bg);border:1px solid var(--border);border-radius:4px;font-size:11px">'
        + '<strong>' + (o.purpose||'?') + '</strong> · q=' + q
        + ' · src=' + (o.outcome_source||'?')
        + (o.thumbs_vote !== null ? ' · thumbs=' + (o.thumbs_vote>0?'+1':(o.thumbs_vote<0?'-1':'0')) : '')
        + ' · gw#' + (o.gateway_log_id||'?')
        + ' · <span style="color:var(--text-muted)">' + when + '</span>'
        + '</div>';
    }).join('');
  }).catch(function(){});

  // Learning runs
  fetch('/api/brain/context/gateway/learn/runs?limit=8').then(function(r){return r.json();}).then(function(d){
    var el = document.getElementById('ctx-learn-runs');
    if (!el) return;
    var items = (d && d.items) || [];
    if (!items.length) { el.innerHTML = '<div style="color:var(--text-muted)">No runs yet.</div>'; return; }
    el.innerHTML = items.map(function(r){
      var when = (r.started_at || '').replace('T',' ').slice(0,19);
      var ok = (r.success === true) ? '✓' : (r.success === false ? '✗' : '·');
      var summary = r.phase === 'distiller'
        ? ('patterns=' + r.patterns_touched)
        : ('proposals=' + r.proposals_created + ' (auto=' + r.proposals_auto_applied + ')');
      return '<div style="font-size:11px;padding:2px 0">'
        + ok + ' [' + r.phase + '] ' + when + ' — ' + summary
        + (r.error_message ? ' · err=' + r.error_message.slice(0,80) : '')
        + '</div>';
    }).join('');
  }).catch(function(){});

  // Wire the manual run button (idempotent — re-binds on each refresh).
  var btn = document.getElementById('ctx-learn-run-btn');
  if (btn && !btn.__wired) {
    btn.__wired = true;
    btn.addEventListener('click', function(){
      var status = document.getElementById('ctx-learn-run-status');
      if (status) status.textContent = 'running…';
      btn.disabled = true;
      fetch('/api/brain/context/gateway/learn/run?phase=both', {method:'POST'})
        .then(function(r){return r.json();})
        .then(function(d){
          if (status) status.textContent = 'distill: ' + (d.distill && d.distill.patterns_touched || 0)
            + ' · evolve: ' + (d.evolve && (d.evolve.proposals + ' (auto=' + d.evolve.auto_applied + ')') || 0);
          loadGatewayLearningPanel();
        })
        .catch(function(){ if (status) status.textContent = 'failed'; })
        .finally(function(){ btn.disabled = false; });
    });
  }
}

function brainApplyInitialView() {
  var server = (typeof window.__CHILI_BRAIN_INITIAL_DOMAIN__ === 'string')
    ? window.__CHILI_BRAIN_INITIAL_DOMAIN__
    : 'hub';
  /* Client-only: jobs bookmark without server redirect */
  if (brainReadUrlDomainString() === 'jobs') {
    window.location.replace('/app/jobs');
    return;
  }
  if (server === 'hub') {
    showBrainHub({ skipUrl: true });
    if (brainReadUrlDomainString() === '__invalid__') {
      brainSetUrlDomain('hub');
    }
    return;
  }
  if (server === 'trading' || server === 'project' || server === 'reasoning' || server === 'context') {
    switchDomain(server, { skipUrl: true });
    return;
  }
  showBrainHub({ skipUrl: true });
}

/* toggleCodeBrain removed â€” Code Brain merged into agent panels */

/* Reasoning Brain JavaScript */

function loadReasoningDashboard() {
  loadReasoningMetrics();
  loadReasoningInsightChat();
  loadReasoningGoals();
  loadReasoningHypotheses();
  loadReasoningConfidenceHistory();
  loadReasoningModel();
  loadReasoningInterests();
  loadReasoningResearch();
  loadReasoningAnticipations();
}

function loadReasoningMetrics() {
  fetch('/api/brain/reasoning/metrics').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('reasoning-status-info');
    if (!el) return;
    var st = d.status || {};
    var txt = [];
    if (st.running) {
      txt.push('Learning: ' + (st.phase || 'running'));
    } else if (st.last_run) {
      txt.push('Last run ' + timeSince(new Date(st.last_run)) + ' ago');
    } else {
      txt.push('Never run');
    }
    txt.push('Interests ' + (d.interest_count||0));
    txt.push('Research ' + (d.research_count||0));
    txt.push('Anticipations ' + (d.anticipation_count||0));
    el.textContent = txt.join(' â€¢ ');
  }).catch(function(){});
}

function loadReasoningInsightChat() {
  fetch('/api/brain/reasoning/insight-chat/opener').then(function(r){return r.json();}).then(function(d) {
    var thread = document.getElementById('reasoning-insight-thread');
    var goalEl = document.getElementById('reasoning-current-goal');
    if (!thread || !goalEl) return;
    if (!d.ok || !d.opener) {
      thread.innerHTML = '<div style="color:var(--text-muted)">Chili will start a conversation here once it has a new question for you.</div>';
      goalEl.textContent = 'No active goal yet';
      return;
    }
    var o = d.opener;
    goalEl.textContent = o.goal_description || o.goal_id || 'Reasoning';
    thread.innerHTML = '<div style="margin-bottom:6px"><strong style="color:var(--accent)">Chili</strong>: ' + escHtml(o.message) + '</div>';
    thread.dataset.goalId = o.goal_id;
  }).catch(function(){});
}

function sendReasoningInsightReply() {
  var input = document.getElementById('reasoning-insight-input');
  var thread = document.getElementById('reasoning-insight-thread');
  if (!input || !thread) return;
  var msg = input.value.trim();
  if (!msg) return;
  var goalId = parseInt(thread.dataset.goalId || '0', 10);
  if (!goalId) {
    alert('No active learning goal for this conversation yet.');
    return;
  }
  var html = thread.innerHTML + '<div style="margin-bottom:6px"><strong>You</strong>: ' + escHtml(msg) + '</div>';
  thread.innerHTML = html;
  input.value = '';
  fetch('/api/brain/reasoning/insight-chat/reply', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({message: msg, goal_id: goalId}),
  }).then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    loadReasoningGoals();
  }).catch(function(){});
}

function loadReasoningGoals() {
  fetch('/api/brain/reasoning/goals').then(function(r){return r.json();}).then(function(d) {
    var el = document.getElementById('reasoning-goals');
    if (!el) return;
    if (!d.ok || !d.goals) {
      el.textContent = 'No goals yet.';
      return;
    }
    if (!d.goals.length) {
      el.textContent = 'No goals yet.';
      return;
    }
    var html = '';
    d.goals.forEach(function(g){
      var pct = Math.min(100, (g.evidence_count || 0) * 20);
      html += '<div style="margin-bottom:6px">' +
        '<div style="font-size:11px;font-weight:600">' + escHtml(g.dimension) + '</div>' +
        '<div style="font-size:10px;color:var(--text-muted);margin-bottom:3px">' + escHtml(g.description) + '</div>' +
        '<div style="height:4px;background:var(--border);border-radius:2px;overflow:hidden"><div style="height:100%;width:' + pct + '%;background:linear-gradient(90deg,#8b5cf6,#6366f1)"></div></div>' +
        '<div style="font-size:9px;color:var(--text-muted);margin-top:2px">' + escHtml(g.status) + ' â€¢ evidence ' + (g.evidence_count||0) + '</div>' +
        '</div>';
    });
    el.innerHTML = html;
  }).catch(function(){});
}

function loadReasoningHypotheses() {
  fetch('/api/brain/reasoning/hypotheses').then(function(r){return r.json();}).then(function(d) {
    var el = document.getElementById('reasoning-hypotheses');
    if (!el) return;
    if (!d.ok || !d.hypotheses || !d.hypotheses.length) {
      el.textContent = 'No hypotheses yet.';
      return;
    }
    var html = '';
    d.hypotheses.forEach(function(h){
      var conf = (h.confidence || 0).toFixed(2);
      html += '<div class="pattern-card" style="margin-bottom:4px">' +
        '<div class="pc-header"><span class="pc-signal neutral">Hypothesis</span><span class="pc-desc" style="font-size:11px">' + escHtml(h.claim) + '</span></div>' +
        '<div class="pc-stats"><span><span>&#x1F4C8;</span>' + conf + '</span><span>for ' + (h.evidence_for||0) + '</span><span>against ' + (h.evidence_against||0) + '</span></div>' +
        '</div>';
    });
    el.innerHTML = html;
  }).catch(function(){});
}

function loadReasoningConfidenceHistory() {
  fetch('/api/brain/reasoning/confidence-history').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok || !d.data || !d.data.length) return;
    var el = document.getElementById('reasoning-chart');
    if (!el) return;
    el.innerHTML = '';
    var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    var chart = LightweightCharts.createChart(el, {
      width: el.clientWidth, height: el.clientHeight || 160,
      layout: { background: {type:'solid', color: isDark ? '#020617' : '#f8fafc'}, textColor: isDark ? '#9ca3af' : '#6b7280' },
      grid: { vertLines:{visible:false}, horzLines:{color: isDark ? '#111827' : '#e5e7eb'} },
      rightPriceScale: { borderVisible:false }, timeScale: { borderVisible:false }, handleScroll:false, handleScale:false,
    });
    var series = chart.addAreaSeries({ lineColor: '#22c55e', topColor: 'rgba(34,197,94,.3)', bottomColor: 'rgba(34,197,94,.02)', lineWidth: 2 });
    var pts = d.data.map(function(p){ return {time:p.time, value:p.value}; });
    series.setData(pts);
    chart.timeScale().fitContent();
    new ResizeObserver(function() { if (chart) chart.applyOptions({width:el.clientWidth}); }).observe(el);
  }).catch(function(){});
}

function triggerReasoningLearn() {
  var btn = document.getElementById('btn-reasoning-learn');
  if (btn) { btn.disabled = true; btn.textContent = 'Running...'; }
  fetch('/api/brain/reasoning/learn', {method:'POST'}).then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) { alert(d.message || 'Failed to start reasoning cycle'); }
    setTimeout(function(){ if (btn) { btn.disabled = false; btn.textContent = 'Refresh Model'; } loadReasoningMetrics(); }, 2000);
  }).catch(function(e){
    if (btn) { btn.disabled = false; btn.textContent = 'Refresh Model'; }
  });
}

function triggerReasoningResearch() {
  // Shortcut: reasoning cycle already runs research; just call learn
  triggerReasoningLearn();
}

function loadReasoningModel() {
  fetch('/api/brain/reasoning/model').then(function(r){return r.json();}).then(function(d) {
    var el = document.getElementById('reasoning-user-model-content');
    if (!el) return;
    if (!d.ok || !d.model) {
      el.textContent = 'No user model yet. Run a reasoning cycle to let Chili study your behaviour.';
      return;
    }
    var m = d.model;
    var html = '';
    html += '<div style="font-size:11px;margin-bottom:4px"><strong>Decision style:</strong> ' + escHtml(m.decision_style || 'unknown') + '</div>';
    html += '<div style="font-size:11px;margin-bottom:4px"><strong>Risk tolerance:</strong> ' + escHtml(m.risk_tolerance || 'unknown') + '</div>';
    if (m.communication_prefs) {
      try {
        var cp = JSON.parse(m.communication_prefs);
        html += '<div style="font-size:11px;margin-bottom:4px"><strong>Communication:</strong> ' +
          escHtml((cp.detail_level || 'normal') + ' detail, ' + (cp.tone || 'friendly') + ' tone') + '</div>';
      } catch(e) {}
    }
    if (m.active_goals) {
      try {
        var goals = JSON.parse(m.active_goals) || [];
        if (goals.length) {
          html += '<div style="font-size:11px;margin-top:4px"><strong>Active goals:</strong></div><ul style="margin:4px 0 0 16px;font-size:11px">';
          goals.slice(0,5).forEach(function(g){
            html += '<li>[' + escHtml(g.area || 'general') + '] ' + escHtml(g.goal || '') + ' (' + escHtml(g.horizon || '?') + ')</li>';
          });
          html += '</ul>';
        }
      } catch(e) {}
    }
    el.innerHTML = html;
  }).catch(function(){});
}

function loadReasoningInterests() {
  fetch('/api/brain/reasoning/interests').then(function(r){return r.json();}).then(function(d) {
    var el = document.getElementById('reasoning-interests');
    if (!el) return;
    if (!d.ok || !d.interests || !d.interests.length) {
      el.textContent = 'No interests tracked yet.';
      return;
    }
    var html = '<table style="width:100%;border-collapse:collapse"><tbody>';
    d.interests.forEach(function(it){
      html += '<tr>' +
        '<td style="padding:3px 4px;font-weight:600">' + escHtml(it.topic) + '</td>' +
        '<td style="padding:3px 4px;font-size:10px;color:var(--text-muted)">' + escHtml(it.category) + '</td>' +
        '<td style="padding:3px 4px;font-size:10px;text-align:right;color:var(--text-secondary)">' + it.weight.toFixed(2) + '</td>' +
      '</tr>';
    });
    html += '</tbody></table>';
    el.innerHTML = html;
  }).catch(function(){});
}

function loadReasoningResearch() {
  fetch('/api/brain/reasoning/research').then(function(r){return r.json();}).then(function(d) {
    var el = document.getElementById('reasoning-research');
    if (!el) return;
    if (!d.ok || !d.research || !d.research.length) {
      el.textContent = 'No background research yet. Chili will start researching once interests are known.';
      return;
    }
    var html = '';
    d.research.forEach(function(r){
      html += '<div class="pattern-card" style="margin-bottom:6px">' +
        '<div class="pc-header"><span class="pc-signal neutral">Topic</span><span style="font-weight:600;font-size:11px">' + escHtml(r.topic) + '</span></div>' +
        '<div class="pc-desc" style="font-size:11px">' + escHtml(r.summary) + '</div>';
      try {
        var sources = JSON.parse(r.sources || '[]');
        if (sources && sources.length) {
          html += '<div style="margin-top:4px;font-size:10px;color:var(--text-muted)">';
          sources.slice(0,3).forEach(function(s, idx){
            if (s.url) {
              html += (idx>0?' â€¢ ':'') + '<a href="' + s.url + '" target="_blank" style="color:var(--accent);text-decoration:none">' + escHtml(s.title || s.url) + '</a>';
            }
          });
          html += '</div>';
        }
      } catch(e) {}
      html += '</div>';
    });
    el.innerHTML = html;
  }).catch(function(){});
}

function loadReasoningAnticipations() {
  fetch('/api/brain/reasoning/anticipations').then(function(r){return r.json();}).then(function(d) {
    var el = document.getElementById('reasoning-anticipations');
    if (!el) return;
    if (!d.ok || !d.anticipations || !d.anticipations.length) {
      el.textContent = 'No active anticipations. Chili will start predicting once enough data is gathered.';
      return;
    }
    var html = '';
    d.anticipations.forEach(function(a){
      var conf = (a.confidence || 0).toFixed(2);
      html += '<div class="pred-card" style="margin-bottom:6px">' +
        '<div class="pred-top"><span class="pred-ticker">' + escHtml(a.domain || 'general') + '</span>' +
        '<span class="pred-dir neutral">' + conf + '</span></div>' +
        '<div class="pred-signals">' + escHtml(a.description) + '</div>' +
        '<div style="margin-top:6px;display:flex;gap:6px;justify-content:flex-end">' +
        '<button onclick="dismissAnticipation(' + a.id + ')" style="padding:3px 8px;font-size:10px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text-muted);cursor:pointer">Dismiss</button>' +
        '</div>' +
        '</div>';
    });
    el.innerHTML = html;
  }).catch(function(){});
}

function dismissAnticipation(id) {
  fetch('/api/brain/reasoning/anticipations/' + id + '/dismiss', {method:'POST'}).then(function(r){return r.json();}).then(function(d) {
    if (d.ok) {
      loadReasoningAnticipations();
      loadReasoningMetrics();
    }
  }).catch(function(){});
}

/* â”€â”€ Repos â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
/* -- Section panel switching -- */
function switchBrainPanel(panelId) {
  document.querySelectorAll('.brain-content-panel').forEach(function(p) {
    p.classList.remove('active');
  });
  document.querySelectorAll('.brain-nav-btn').forEach(function(btn) {
    var on = btn.getAttribute('data-panel') === panelId;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  var target = document.getElementById('brain-panel-' + panelId);
  if (target) target.classList.add('active');
}

/* -- Summary bar sync (called from desk polling) -- */
function updateBrainSummaryBar(data) {
  var dot = document.getElementById('bsb-summary-dot');
  var worker = document.getElementById('bsb-summary-worker');
  var regime = document.getElementById('bsb-summary-regime');
  var actionable = document.getElementById('bsb-summary-actionable');
  var watch = document.getElementById('bsb-summary-watch');
  var cycle = document.getElementById('bsb-summary-cycle');
  if (!data) return;
  if (dot && data.workerStatus) {
    dot.className = 'bsb-dot ' + (data.workerStatus === 'running' ? 'running' : data.workerStatus === 'idle' ? 'idle' : 'stopped');
  }
  if (worker && data.workerLabel) worker.textContent = data.workerLabel;
  if (regime && data.regime) regime.textContent = data.regime;
  if (actionable && data.actionable != null) actionable.textContent = data.actionable;
  if (watch && data.watch != null) watch.textContent = data.watch;
  if (cycle && data.lastCycle) cycle.textContent = data.lastCycle;
}

/* -- Telemetry events -- */
function brainTrackEvent(action, label, data) {
  if (typeof window.gtag === 'function') {
    window.gtag('event', action, { event_category: 'brain', event_label: label });
  }
  if (typeof window._chiliTelemetry === 'object' && typeof window._chiliTelemetry.push === 'function') {
    window._chiliTelemetry.push({ action: action, label: label, data: data, ts: Date.now() });
  }
}

/* -- Lazy loading for panels -- */
var _brainPanelLoaded = {};
function brainEnsurePanelLoaded(panelId) {
  if (_brainPanelLoaded[panelId]) return;
  _brainPanelLoaded[panelId] = true;
  brainTrackEvent('panel_view', panelId);
  switch (panelId) {
    case 'overview':
      if (typeof loadBrainDashboard === 'function') loadBrainDashboard();
      if (typeof loadPlaybook === 'function') loadPlaybook();
      if (typeof loadPerfDashboard === 'function') loadPerfDashboard();
      break;
    case 'opportunities':
      if (typeof loadOpportunityBoard === 'function') loadOpportunityBoard();
      if (typeof loadBrainPredictions === 'function') loadBrainPredictions();
      if (typeof loadTradeablePatterns === 'function') loadTradeablePatterns();
      if (typeof loadResearchEdgePatterns === 'function') loadResearchEdgePatterns();
      break;
    case 'deep-dive':
      if (typeof switchDeepDiveTab === 'function') switchDeepDiveTab('patterns');
      break;
  }
}

/* -- Global keyboard shortcuts -- */
document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
  if (e.target.isContentEditable) return;

  if (e.key === '1' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    switchBrainPanel('overview');
    brainEnsurePanelLoaded('overview');
    brainTrackEvent('shortcut', 'panel_overview');
  }
  if (e.key === '2' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    switchBrainPanel('opportunities');
    brainEnsurePanelLoaded('opportunities');
    brainTrackEvent('shortcut', 'panel_opportunities');
  }
  if (e.key === '3' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    switchBrainPanel('deep-dive');
    brainEnsurePanelLoaded('deep-dive');
    brainTrackEvent('shortcut', 'panel_deep_dive');
  }
  if (e.key === 'n' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    setTradingBrainSubtab('network');
    brainTrackEvent('shortcut', 'network_tab');
  }
  if (e.key === 'r' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    setTradingBrainSubtab('runtime');
    brainTrackEvent('shortcut', 'runtime_tab');
  }
  if (e.key === '?' && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    _showKeyboardShortcutsHelp();
  }
});

function _showKeyboardShortcutsHelp() {
  var body =
    '<table class="data-table" style="width:100%">' +
    '<thead><tr><th>Key</th><th>Action</th></tr></thead><tbody>' +
    '<tr><td><kbd>1</kbd></td><td>Switch to Overview panel</td></tr>' +
    '<tr><td><kbd>2</kbd></td><td>Switch to Opportunities panel</td></tr>' +
    '<tr><td><kbd>3</kbd></td><td>Switch to Deep Dive panel</td></tr>' +
    '<tr><td><kbd>n</kbd></td><td>Switch to Network graph</td></tr>' +
    '<tr><td><kbd>r</kbd></td><td>Switch to Runtime view</td></tr>' +
    '<tr><td><kbd>?</kbd></td><td>Show this help</td></tr>' +
    '</tbody></table>';
  openBrainModal('Keyboard Shortcuts', body);
}
