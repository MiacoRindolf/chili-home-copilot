function setTradingBrainSubtab(which) {
  if (which !== 'runtime' && which !== 'network') return;
  document.querySelectorAll('.tb-tabstrip .tb-tab').forEach(function(btn) {
    var on = btn.getAttribute('data-tb-tab') === which;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  var out = document.getElementById('trading-brain-pane-output');
  var net = document.getElementById('trading-brain-pane-network');
  var asst = document.getElementById('trading-brain-assistant-section');
  var isOut = which === 'runtime';
  if (out) {
    out.style.display = isOut ? 'flex' : 'none';
    out.setAttribute('aria-hidden', isOut ? 'false' : 'true');
  }
  if (net) {
    net.style.display = isOut ? 'none' : 'flex';
    net.setAttribute('aria-hidden', isOut ? 'true' : 'false');
  }
  if (asst) asst.style.display = isOut ? '' : 'none';
  if (!isOut) initTradingBrainNetworkGraph();
  if (isOut && typeof tbRefreshMomentumNeuralStrip === 'function') tbRefreshMomentumNeuralStrip();
}

var _tradingBrainNetworkInited = false;
var _tbnState = { tx: 0, ty: 0, scale: 0.52 };

function _tbnApplyStage() {
  var st = document.getElementById('trading-brain-network-stage');
  if (!st) return;
  st.style.transform = 'translate(' + _tbnState.tx + 'px,' + _tbnState.ty + 'px) scale(' + _tbnState.scale + ')';
}

function initTradingBrainNetworkGraph() {
  if (_tradingBrainNetworkInited) return;
  var vp = document.getElementById('trading-brain-network-viewport');
  var gEdges = document.getElementById('tbn-edges');
  var gNodes = document.getElementById('tbn-nodes');
  if (!vp || !gEdges || !gNodes) return;
  _tradingBrainNetworkInited = true;

  var _tbnLiveTimer = null;
  var _tbnEdgeDomByKey = {};
  var _tbnNeuralView = false;
  var _tbnDesk = window.__CHILI_TBN_DESK__ || {};

  function tbnApplyServerDeskConfig(d) {
    if (!d || d.ok === false) return;
    _tbnDesk = {
      mesh_enabled: true,
      effective_graph_mode: 'neural',
      desk_boot: d.desk_boot || 'api',
      recommended_graph_url: d.recommended_graph_url || '/api/trading/brain/graph',
    };
    window.__CHILI_TBN_DESK__ = _tbnDesk;
  }

  function tbnGraphFetchUrl() {
    var desk = _tbnDesk || {};
    return desk.recommended_graph_url || '/api/trading/brain/graph';
  }

  function tbnWantsNeuralStrict() {
    return true;
  }

  function tbnRestartLivePoll() {
    if (_tbnLiveTimer) {
      clearInterval(_tbnLiveTimer);
      _tbnLiveTimer = null;
    }
    var liveCb = document.getElementById('tbn-live-activations');
    var wrap = document.getElementById('tbn-graph-mode-wrap');
    var sel = document.getElementById('tbn-graph-mode');
    if (!liveCb || !liveCb.checked) return;
    if (!wrap || wrap.style.display === 'none' || !sel || sel.value !== 'neural') return;
    _tbnLiveTimer = setInterval(function() {
      var netPane = document.getElementById('trading-brain-pane-network');
      if (!netPane || netPane.style.display === 'none') return;
      fetch('/api/trading/brain/graph/live-overlay?activation_limit=80&time_window_sec=2', { credentials: 'same-origin' })
        .then(function(r) { return r.json(); })
        .then(function(d) {
          if (!d || !d.ok) return;
          var hot = d.hot_node_ids || [];
          gNodes.querySelectorAll('.tbn-neural-node').forEach(function(ng) {
            ng.classList.remove('tbn-neural-hot');
            var hw = ng.querySelector('.tbn-neural-halo-wrap');
            if (hw) hw.classList.remove('tbn-neural-halo-hot');
          });
          hot.forEach(function(nid) {
            if (!nid) return;
            var el = gNodes.querySelector('.tbn-neural-node[data-node-id="' + nid + '"]');
            if (el) {
              el.classList.add('tbn-neural-hot');
              var hw2 = el.querySelector('.tbn-neural-halo-wrap');
              if (hw2) hw2.classList.add('tbn-neural-halo-hot');
            }
          });
          (d.edge_pulse_keys || []).forEach(function(key) {
            var line = _tbnEdgeDomByKey[key];
            if (!line) return;
            line.classList.remove('tbn-edge-pulse');
            void line.offsetWidth;
            line.classList.add('tbn-edge-pulse');
          });
          var wind = document.getElementById('tbn-wave-indicator');
          var lw = d.last_wave;
          if (wind && lw) {
            var nc = (lw.source_node_ids && lw.source_node_ids.length) || 0;
            var ec = (d.edge_pulse_keys && d.edge_pulse_keys.length) || 0;
            wind.textContent = 'Last wave: ' + nc + ' nodes, ' + ec + ' edges pulsed';
          }
        })
        .catch(function() {});
    }, 3200);
  }

  function tbnInitGraphModeUiAfterConfig() {
    var wrap = document.getElementById('tbn-graph-mode-wrap');
    var sel = document.getElementById('tbn-graph-mode');
    var desk = _tbnDesk || {};
    if (!wrap || !sel) return;
    wrap.style.display = 'flex';
    sel.value = 'neural';
    sel.onchange = function() {
      try { localStorage.setItem('chili_tbn_graph_mode', sel.value); } catch (e2) {}
      tbnRefreshTradingBrainNetworkGraph();
      tbnRestartLivePoll();
    };
    var liveCb = document.getElementById('tbn-live-activations');
    if (liveCb) liveCb.onchange = function() { tbnRestartLivePoll(); };
  }

  var dragging = false;
  var px = 0;
  var py = 0;

  vp.addEventListener('mousedown', function(e) {
    if (e.button !== 0) return;
    if (e.target && e.target.closest && e.target.closest('.tbn-node')) return;
    tbnCloseNodeDetail();
    dragging = true;
    px = e.clientX;
    py = e.clientY;
    vp.classList.add('tbn-dragging');
  });
  window.addEventListener('mouseup', function() {
    dragging = false;
    vp.classList.remove('tbn-dragging');
  });
  window.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    var dx = e.clientX - px;
    var dy = e.clientY - py;
    px = e.clientX;
    py = e.clientY;
    _tbnState.tx += dx;
    _tbnState.ty += dy;
    _tbnApplyStage();
  });

  vp.addEventListener('wheel', function(e) {
    tbnCloseNodeDetail();
    e.preventDefault();
    var rect = vp.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var my = e.clientY - rect.top;
    var s = _tbnState.scale;
    var delta = e.deltaY > 0 ? 0.9 : 1.11;
    var ns = Math.max(0.32, Math.min(2.6, s * delta));
    var wx = (mx - _tbnState.tx) / s;
    var wy = (my - _tbnState.ty) / s;
    _tbnState.tx = mx - wx * ns;
    _tbnState.ty = my - wy * ns;
    _tbnState.scale = ns;
    _tbnApplyStage();
  }, { passive: false });

  function tbnShowGraphError() {
    var mpErr = document.getElementById('tbn-momentum-desk-panel');
    if (mpErr) {
      mpErr.style.display = 'none';
      mpErr.innerHTML = '';
    }
    gEdges.innerHTML = '';
    gNodes.innerHTML = '';
    var err = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    err.setAttribute('x', '800');
    err.setAttribute('y', '475');
    err.setAttribute('text-anchor', 'middle');
    err.setAttribute('class', 'tbn-graph-error');
    err.textContent = 'Could not load architecture graph. Reload the page or check the server log.';
    gNodes.appendChild(err);
  }

  function tbnShowNeuralGraphUnavailable(detail) {
    var mpNv = document.getElementById('tbn-momentum-desk-panel');
    if (mpNv) {
      mpNv.style.display = 'none';
      mpNv.innerHTML = '';
    }
    gEdges.innerHTML = '';
    gNodes.innerHTML = '';
    var g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    var y0 = 430;
    var lines = [
      'Neural graph unavailable',
      'The desk did not load a neural mesh projection.',
      'Check: API reachable, DB migrations applied, graph payload.',
    ];
    if (detail) lines.push(String(detail).slice(0, 120));
    lines.forEach(function(line, i) {
      var t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      t.setAttribute('x', '800');
      t.setAttribute('y', String(y0 + i * 22));
      t.setAttribute('text-anchor', 'middle');
      t.setAttribute('class', 'tbn-graph-error');
      t.setAttribute('font-size', i === 0 ? '15' : '11');
      t.textContent = line;
      g.appendChild(t);
    });
    gNodes.appendChild(g);
  }

  function tbnCloseNodeDetail() {
    var p = document.getElementById('tbn-node-detail');
    if (!p) return;
    p.classList.remove('tbn-node-detail-open');
    p.setAttribute('aria-hidden', 'true');
    p.style.left = '';
    p.style.top = '';
  }

  function tbnPositionNodeDetailPanel(p, anchorEl) {
    if (!p || !anchorEl) return;
    var gap = 10;
    var pad = 8;
    var r = anchorEl.getBoundingClientRect();
    var pr = p.getBoundingClientRect();
    var pw = pr.width || p.offsetWidth || 320;
    var ph = pr.height || p.offsetHeight || 120;
    var left = r.right + gap;
    var top = r.top + (r.height / 2) - (ph / 2);
    if (left + pw > window.innerWidth - pad) {
      left = r.left - pw - gap;
    }
    if (left < pad) left = pad;
    if (top + ph > window.innerHeight - pad) top = window.innerHeight - ph - pad;
    if (top < pad) top = pad;
    p.style.left = left + 'px';
    p.style.top = top + 'px';
  }

  function tbnOpenNodeDetail(n, anchorEl) {
    var p = document.getElementById('tbn-node-detail');
    var titleEl = document.getElementById('tbn-node-detail-title');
    var body = document.getElementById('tbn-node-detail-body');
    if (!p || !titleEl || !body) return;
    titleEl.textContent = n.label || n.id || 'Node';
    body.innerHTML = '';
    var sectionUid = 0;
    function addCollapsibleSection(title, innerExtraClass, fillInner) {
      var sid = 'tbn-sec-' + (++sectionUid);
      var dt = document.createElement('dt');
      dt.className = 'tbn-node-detail-sr-only';
      dt.id = sid;
      dt.textContent = title;
      var dd = document.createElement('dd');
      dd.className = 'tbn-node-detail-section-dd';
      var det = document.createElement('details');
      det.className = 'tbn-node-detail-collapsible';
      det.setAttribute('open', '');
      det.setAttribute('aria-labelledby', sid);
      var sum = document.createElement('summary');
      sum.textContent = title;
      var inner = document.createElement('div');
      inner.className = 'tbn-node-detail-collapsible-inner' + (innerExtraClass ? ' ' + innerExtraClass : '');
      fillInner(inner);
      det.appendChild(sum);
      det.appendChild(inner);
      dd.appendChild(det);
      body.appendChild(dt);
      body.appendChild(dd);
    }
    function fillBulletList(container, items) {
      if (!items || !items.length) {
        container.textContent = '\u2014';
        return;
      }
      var ul = document.createElement('ul');
      ul.className = 'tbn-node-detail-io';
      for (var bi = 0; bi < items.length; bi++) {
        var li = document.createElement('li');
        li.textContent = String(items[bi]);
        ul.appendChild(li);
      }
      container.appendChild(ul);
    }
    var descParts = [];
    if (n.description != null && String(n.description).trim() !== '') descParts.push(String(n.description).trim());
    if (n.remarks != null && String(n.remarks).trim() !== '') descParts.push(String(n.remarks).trim());
    var mergedDesc = descParts.join('\n\n').trim();
    if (mergedDesc) {
      addCollapsibleSection('Description', 'tbn-node-detail-collapsible-inner--prose', function(inner) {
        inner.textContent = mergedDesc;
      });
    }
    addCollapsibleSection('Inputs', '', function(inner) {
      fillBulletList(inner, n.inputs);
    });
    addCollapsibleSection('Outputs', '', function(inner) {
      fillBulletList(inner, n.outputs);
    });
    if (n.tier != null && n.tier !== '') {
      addCollapsibleSection('Tier', '', function(inner) {
        inner.textContent = String(n.tier);
      });
    }
    if (n.id != null && n.id !== '') {
      addCollapsibleSection('Id', '', function(inner) {
        inner.textContent = String(n.id);
      });
    }
    addCollapsibleSection('Code', '', function(inner) {
      var raw = (n.code_snippet != null && String(n.code_snippet).trim() !== '')
        ? String(n.code_snippet)
        : (n.code_ref != null && String(n.code_ref).trim() !== '' ? String(n.code_ref) : '');
      if (!raw.trim()) {
        inner.textContent = '\u2014';
        return;
      }
      var pre = document.createElement('pre');
      pre.className = 'tbn-node-detail-code-block';
      var codeEl = document.createElement('code');
      codeEl.textContent = raw;
      pre.appendChild(codeEl);
      inner.appendChild(pre);
    });
    if (n.phase != null && n.phase !== '') {
      addCollapsibleSection('Phase', '', function(inner) {
        inner.textContent = String(n.phase);
      });
    }
    p.classList.add('tbn-node-detail-open');
    p.setAttribute('aria-hidden', 'false');
    requestAnimationFrame(function() {
      requestAnimationFrame(function() {
        tbnPositionNodeDetailPanel(p, anchorEl);
      });
    });
  }

  var tbnCloseBtn = document.getElementById('tbn-node-detail-close');
  if (tbnCloseBtn) tbnCloseBtn.onclick = function() { tbnCloseNodeDetail(); };

  function tbnStepLabelLines(s, firstMax, secondMax) {
    if (!s) return [''];
    if (s.length <= firstMax) return [s];
    var sp = s.lastIndexOf(' ', firstMax + 8);
    if (sp < 8) sp = s.indexOf(' ');
    if (sp < 1) return [s.slice(0, firstMax - 1) + '\u2026'];
    var a = s.slice(0, sp);
    var b = s.slice(sp + 1).trim();
    if (b.length > secondMax) b = b.slice(0, secondMax - 1) + '\u2026';
    return [a, b];
  }

  /** Phase 10: compact momentum node detail from projection API (read-model). */
  function tbnMomentumDeskCardHtml(card) {
    if (!card || typeof card !== 'object' || card.error) return '';
    var role = card.role || '';
    var rows = [];
    rows.push('<div style="border:1px solid var(--border);border-radius:6px;padding:8px;margin-bottom:8px;background:var(--bg);max-width:100%">');
    rows.push('<div style="font-weight:700;color:var(--accent)">' + escHtml(card.title || 'Momentum') + '</div>');
    rows.push('<div style="font-size:9px;color:var(--text-muted);margin-bottom:6px">' + escHtml(card.subtitle || '') + '</div>');
    function line(k, v) {
      if (v == null || v === '') return;
      rows.push('<div style="font-size:10px"><strong>' + escHtml(k) + '</strong> ' + escHtml(String(v)) + '</div>');
    }
    if (role === 'momentum_crypto_intel') {
      line('Execution family', card.execution_family);
      line('Neural active', card.neural_active ? 'yes' : 'no');
      line('Last tick (hot)', card.last_tick_utc);
      line('Symbols evaluated', card.symbols_evaluated_count);
      line('Regime hint', card.regime_session_hint);
      line('Top family hint', card.top_family_hint);
      line('Corr (tail)', card.correlation_id_tail);
    } else if (role === 'momentum_viability_pool') {
      line('Hot last tick', card.hot_last_tick_utc);
      line('Hot rows (preview cap)', card.hot_row_count);
      line('DB viability rows', card.durable_row_count);
      line('Live-eligible', card.live_eligible_count);
      line('Paper-only', card.paper_only_count);
      line('Fresh last 24h', card.fresh_last_24h_count);
      if (card.top_durable_lines && card.top_durable_lines.length) {
        rows.push('<div style="margin-top:4px;font-size:9px"><strong>Top durable</strong><br/>' +
          card.top_durable_lines.map(function(l) { return escHtml(String(l)); }).join('<br/>') + '</div>');
      }
    } else if (role === 'momentum_evolution_trace') {
      line('Latest feedback (hot)', card.latest_feedback_at_utc);
      line('30d paper outcomes', card.paper_30d_n);
      line('30d live outcomes', card.live_30d_n);
      line('Paper mean bps (30d)', card.paper_mean_bps_30d);
      line('Live mean bps (30d)', card.live_mean_bps_30d);
      line('Feedback loop', card.feedback_loop_active ? 'active' : 'inactive');
      line('Trace tail length', card.feedback_trace_tail);
      if (card.live_sample_low) {
        rows.push('<div style="color:#f59e0b;font-size:9px;font-weight:600">Live sample low â€” treat live stats cautiously</div>');
      }
      if (card.mix_top && card.mix_top.length) {
        rows.push('<div style="margin-top:4px;font-size:9px"><strong>Outcome mix (30d)</strong> ' +
          card.mix_top.map(function(m) { return escHtml(String(m.outcome_class)) + ':' + escHtml(String(m.n)); }).join(' Â· ') +
          '</div>');
      }
      if (card.best_variant && card.best_variant.label) {
        var bv = card.best_variant;
        rows.push('<div style="font-size:9px"><strong>Best variant</strong> ' + escHtml(String(bv.label)) +
          (bv.avg_return_bps != null ? ' Â· avg bps ' + escHtml(String(bv.avg_return_bps)) : '') + '</div>');
      }
      if (card.weakest_variant && card.weakest_variant.label) {
        var wv = card.weakest_variant;
        rows.push('<div style="font-size:9px"><strong>Weakest variant</strong> ' + escHtml(String(wv.label)) +
          (wv.avg_return_bps != null ? ' Â· avg bps ' + escHtml(String(wv.avg_return_bps)) : '') + '</div>');
      }
    } else {
      rows.push('<div style="font-size:9px;color:var(--text-muted)">Momentum desk preview</div>');
    }
    rows.push('</div>');
    return rows.join('');
  }

  function tbnFillMomentumDeskPanel(data) {
    var panelEl = document.getElementById('tbn-momentum-desk-panel');
    if (!panelEl) return;
    var md = data && data.meta && data.meta.momentum_desk;
    if (!md) {
      panelEl.style.display = 'none';
      panelEl.innerHTML = '';
      return;
    }
    panelEl.style.display = 'block';
    var bd = md.badges || {};
    var pv = md.paper_vs_live_30d || {};
    var pap = pv.paper || {};
    var liv = pv.live || {};
    var links = md.links || {};
    var html = [];
    html.push('<div style="font-weight:600;color:var(--text);margin-bottom:4px">Momentum Â· neural desk</div>');
    html.push('<div style="color:var(--text-secondary);margin-bottom:6px;font-size:10px">' + escHtml(md.headline || '') + '</div>');
    html.push('<div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px;font-size:9px">');
    function pill(txt, ok, bad) {
      var col = bad ? '#f87171' : (ok ? '#22c55e' : 'var(--text-muted)');
      html.push('<span style="padding:2px 6px;border-radius:4px;border:1px solid var(--border);color:' + col + '">' + escHtml(txt) + '</span>');
    }
    pill('mesh ' + (bd.neural_mesh_on ? 'on' : 'off'), bd.neural_mesh_on);
    pill('momentum ' + (bd.momentum_neural_on ? 'on' : 'off'), bd.momentum_neural_on);
    pill('feedback ' + (bd.feedback_enabled ? 'on' : 'off'), bd.feedback_enabled);
    if (bd.governance_kill_switch) pill('kill switch', false, true);
    if (bd.live_sample_low) pill('live sample low', false, false);
    if (bd.outcomes_table_present === false) pill('outcomes table missing', false, false);
    html.push('</div>');
    html.push('<div style="font-size:9px;color:var(--text-muted)">');
    html.push('<strong>Paper 30d</strong> n=' + escHtml(String(pap.n != null ? pap.n : 0)));
    if (pap.mean_return_bps != null) html.push(' Â· mean bps ' + escHtml(String(pap.mean_return_bps)));
    html.push(' <span style="opacity:.5">|</span> ');
    html.push('<strong>Live 30d</strong> n=' + escHtml(String(liv.n != null ? liv.n : 0)));
    if (liv.mean_return_bps != null) html.push(' Â· mean bps ' + escHtml(String(liv.mean_return_bps)));
    if (pv.live_sample_caution) html.push(' <span style="color:#f59e0b;font-weight:600">(live sample caution)</span>');
    html.push('</div>');
    if (links.trading_momentum || links.automation) {
      html.push('<div style="margin-top:6px;font-size:9px">');
      if (links.trading_momentum) html.push('<a href="' + escHtml(links.trading_momentum) + '" style="color:var(--accent)">Trading momentum</a>');
      if (links.trading_momentum && links.automation) html.push(' Â· ');
      if (links.automation) html.push('<a href="' + escHtml(links.automation) + '" style="color:var(--accent)">Autopilot</a>');
      html.push('</div>');
    }
    html.push('<div style="margin-top:4px;font-size:8px;opacity:.75">Neural-native (not learning-cycle). Counts from durable tables; ticks from hot node state.</div>');
    panelEl.innerHTML = html.join('');
  }

  function tbnOpenNeuralNodeDetailFromApi(nodeId, anchorEl) {
    var p = document.getElementById('tbn-node-detail');
    var titleEl = document.getElementById('tbn-node-detail-title');
    var body = document.getElementById('tbn-node-detail-body');
    if (!p || !titleEl || !body) return;
    titleEl.textContent = 'Loading\u2026';
    body.innerHTML = '';
    p.classList.add('tbn-node-detail-open');
    p.setAttribute('aria-hidden', 'false');
    fetch('/api/trading/brain/graph/nodes/' + encodeURIComponent(nodeId), { credentials: 'same-origin' })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (!d || !d.ok || !d.node) throw new Error('bad');
        var nd = d.node;
        titleEl.textContent = nd.label || nd.id;
        var parts = [];
        var mcard = nd.momentum_desk_card;
        if (mcard && typeof mcard === 'object') {
          var mh = tbnMomentumDeskCardHtml(mcard);
          if (mh) {
            parts.push('<dt class="tbn-node-detail-sr-only">Momentum desk</dt><dd class="tbn-node-detail-section-dd">' + mh + '</dd>');
          }
        }
        parts.push('<dt class="tbn-node-detail-sr-only">Summary</dt><dd class="tbn-node-detail-section-dd">');
        parts.push('<div style="font-size:10px;line-height:1.45">');
        parts.push('<div><strong>type</strong> ' + escHtml(String(nd.node_type || '')) + ' &middot; <strong>layer</strong> ' + escHtml(String(nd.layer)) + ' ' + escHtml(String(nd.layer_label || '')) + '</div>');
        parts.push('<div><strong>activation</strong> ' + escHtml(String(nd.activation_score)) + ' &middot; <strong>confidence</strong> ' + escHtml(String(nd.confidence)) + '</div>');
        parts.push('<div><strong>threshold</strong> ' + escHtml(String(nd.fire_threshold)) + ' &middot; <strong>cooldown_s</strong> ' + escHtml(String(nd.cooldown_seconds)) + '</div>');
        parts.push('<div><strong>cooling</strong> ' + escHtml(String(nd.cooling)) + ' &middot; <strong>stale</strong> ' + escHtml(String(nd.stale)) + '</div>');
        parts.push('<div><strong>in last activation wave</strong> ' + escHtml(String(nd.in_last_activation_wave)) + '</div>');
        parts.push('<div><strong>activation wave id</strong> ' + escHtml(String(nd.activation_wave_id || '\u2014')) + '</div>');
        parts.push('<div><strong>wave correlation</strong> ' + escHtml(String(nd.activation_wave_correlation_id || '\u2014')) + '</div>');
        parts.push('<div><strong>last fire correlation</strong> ' + escHtml(String(nd.last_wave_correlation_id || '\u2014')) + '</div>');
        parts.push('<div><strong>last_fired</strong> ' + escHtml(String(nd.last_fired_at || '\u2014')) + '</div>');
        parts.push('<div><strong>last_activated_at</strong> ' + escHtml(String(nd.last_activated_at || '\u2014')) + '</div>');
        parts.push('<div style="margin-top:6px;font-size:9px;color:var(--text-muted)">Waves can include <strong>multiple nodes</strong> at once; match wave id to the Network tab Live indicator.</div>');
        var ibp = nd.edge_polarity_inbound || {};
        var obp = nd.edge_polarity_outbound || {};
        parts.push('<div><strong>polarity in</strong> ex=' + escHtml(String(ibp.excitatory)) + ' in=' + escHtml(String(ibp.inhibitory)) + ' &middot; <strong>out</strong> ex=' + escHtml(String(obp.excitatory)) + ' in=' + escHtml(String(obp.inhibitory)) + '</div>');
        parts.push('</div></dd>');
        var ib = nd.inbound_edges || [];
        var ob = nd.outbound_edges || [];
        parts.push('<dt class="tbn-node-detail-sr-only">Edges</dt><dd class="tbn-node-detail-section-dd"><pre style="margin:0;font-size:9px;white-space:pre-wrap">');
        parts.push('in: ' + ib.length + ' / out: ' + ob.length + '\n');
        ib.slice(0, 8).forEach(function(e) {
          parts.push('\u2190 ' + escHtml(e.from) + ' [' + escHtml(e.signal_type) + '/' + escHtml(e.polarity) + ']\n');
        });
        ob.slice(0, 8).forEach(function(e) {
          parts.push('\u2192 ' + escHtml(e.to) + ' [' + escHtml(e.signal_type) + '/' + escHtml(e.polarity) + ']\n');
        });
        parts.push('</pre></dd>');
        var rf = nd.recent_fires || [];
        parts.push('<dt class="tbn-node-detail-sr-only">Recent fires</dt><dd class="tbn-node-detail-section-dd"><pre style="margin:0;font-size:9px">');
        if (!rf.length) parts.push('\u2014');
        else rf.slice(0, 6).forEach(function(f) {
          parts.push(escHtml(String(f.fired_at)) + ' act=' + escHtml(String(f.activation_score)) + '\n');
        });
        parts.push('</pre></dd>');
        body.innerHTML = parts.join('');
        requestAnimationFrame(function() {
          requestAnimationFrame(function() {
            tbnPositionNodeDetailPanel(p, anchorEl);
          });
        });
      })
      .catch(function() {
        titleEl.textContent = 'Node';
        body.textContent = 'Could not load node detail.';
      });
  }

  var TBN_NEURAL_VW = 1600;
  var TBN_NEURAL_VH = 950;
  var TBN_HUB_IDS = {
    nm_event_bus: 1,
    nm_working_memory: 1,
    nm_regime: 1,
    nm_contradiction: 1,
    nm_momentum_crypto_intel: 1,
  };

  function tbnNeuralQuadPath(x1, y1, x2, y2, k) {
    var mx = (x1 + x2) * 0.5;
    var my = (y1 + y2) * 0.5;
    var dx = x2 - x1;
    var dy = y2 - y1;
    var len = Math.sqrt(dx * dx + dy * dy) || 1;
    var nx = -dy / len;
    var ny = dx / len;
    var off = Math.min(56, len * (k || 0.11));
    var cx = mx + nx * off;
    var cy = my + ny * off;
    return 'M' + x1 + ',' + y1 + ' Q ' + cx + ',' + cy + ' ' + x2 + ',' + y2;
  }

  function tbnNeuralFitCamera(vp, bounds) {
    var vw = vp.clientWidth || 900;
    var vh = vp.clientHeight || 500;
    var pad = 76;
    if (!bounds || bounds.min_x == null || bounds.max_x == null) {
      _tbnState.scale = Math.min(vw / TBN_NEURAL_VW, vh / TBN_NEURAL_VH, 0.68);
      _tbnState.tx = (vw - TBN_NEURAL_VW * _tbnState.scale) / 2;
      _tbnState.ty = (vh - TBN_NEURAL_VH * _tbnState.scale) / 2;
      _tbnApplyStage();
      return;
    }
    window.__TBN_LAST_NEURAL_BOUNDS__ = bounds;
    var bw = Math.max(bounds.max_x - bounds.min_x + 2 * pad, 180);
    var bh = Math.max(bounds.max_y - bounds.min_y + 2 * pad, 180);
    var s = Math.min(vw / bw, vh / bh, 0.82);
    var cx = (bounds.min_x + bounds.max_x) * 0.5;
    var cy = (bounds.min_y + bounds.max_y) * 0.5;
    _tbnState.scale = s;
    _tbnState.tx = vw * 0.5 - cx * s;
    _tbnState.ty = vh * 0.5 - cy * s;
    _tbnApplyStage();
  }

  function tbnRenderNeuralGraphData(data) {
    _tbnEdgeDomByKey = {};
    gEdges.innerHTML = '';
    gNodes.innerHTML = '';
    var gRings = document.getElementById('tbn-neural-rings');
    var gCues = document.getElementById('tbn-layer-cues');
    if (gRings) gRings.innerHTML = '';
    if (gCues) gCues.innerHTML = '';
    var vpMeta = (data.meta && data.meta.viewport) ? data.meta.viewport : { cx: 800, cy: 475 };
    var CX = typeof vpMeta.cx === 'number' ? vpMeta.cx : 800;
    var CY = typeof vpMeta.cy === 'number' ? vpMeta.cy : 475;
    if (gRings && data.meta && data.meta.ring_radii_draw && data.meta.ring_radii_draw.length) {
      data.meta.ring_radii_draw.forEach(function(rad) {
        var c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        c.setAttribute('cx', String(CX));
        c.setAttribute('cy', String(CY));
        c.setAttribute('r', String(rad));
        c.setAttribute('class', 'tbn-neural-ring');
        gRings.appendChild(c);
      });
    }
    if (gCues && data.meta && data.meta.layer_ring_cues && data.meta.layer_ring_cues.length) {
      data.meta.layer_ring_cues.forEach(function(cue, cidx) {
        var cueAngle = -2.2 + cidx * 0.11;
        var rr = cue.r + 14;
        var tx = CX + rr * Math.cos(cueAngle);
        var ty = CY + rr * Math.sin(cueAngle);
        var te = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        te.setAttribute('x', String(tx));
        te.setAttribute('y', String(ty));
        te.setAttribute('class', 'tbn-layer-cue');
        te.setAttribute('text-anchor', 'middle');
        te.textContent = cue.abbr || ('L' + cue.layer);
        gCues.appendChild(te);
      });
    }
    var nodeMap = {};
    data.nodes.forEach(function(n) { nodeMap[n.id] = n; });
    var obsEdges = [];
    var coreEdges = [];
    data.edges.forEach(function(edge) {
      var a = nodeMap[edge.from];
      var b = nodeMap[edge.to];
      if (!a || !b) return;
      if (a.is_observer || b.is_observer) obsEdges.push(edge);
      else coreEdges.push(edge);
    });
    function appendNeuralEdge(edge) {
      var a = nodeMap[edge.from];
      var b = nodeMap[edge.to];
      if (!a || !b) return;
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', tbnNeuralQuadPath(a.x, a.y, b.x, b.y, 0.11));
      var ec = 'tbn-edge tbn-edge-neural-excite';
      if (edge.polarity === 'inhibitory') ec = 'tbn-edge tbn-edge-neural-inhibit';
      if (a.is_observer || b.is_observer) ec += ' tbn-edge-neural-observer';
      if (edge.edge_type === 'causal_feedback') ec += ' tbn-edge-neural-causal';
      path.setAttribute('class', ec);
      path.setAttribute('data-edge-from', edge.from);
      path.setAttribute('data-edge-to', edge.to);
      _tbnEdgeDomByKey[edge.from + '->' + edge.to] = path;
      gEdges.appendChild(path);
    }
    obsEdges.forEach(appendNeuralEdge);
    coreEdges.forEach(appendNeuralEdge);
    var hintEl = document.getElementById('tbn-hint-text');
    if (hintEl && data.meta && data.meta.description) {
      hintEl.innerHTML = 'Neural mesh: rings = cognitive layer (see legend). Excitatory edges are green; inhibitory are red dashed. <span style="opacity:.85">' +
        escHtml(data.meta.description).slice(0, 140) + (data.meta.description.length > 140 ? '\u2026' : '') + '</span>';
    }
    var leg = document.getElementById('tbn-legend');
    if (leg && data.meta && data.meta.layer_labels) {
      var ll = data.meta.layer_labels;
      var partsL = ['<span style="color:var(--accent)">Layers</span>'];
      for (var lk in ll) {
        if (!Object.prototype.hasOwnProperty.call(ll, lk)) continue;
        partsL.push('<span>' + escHtml(lk) + ' ' + escHtml(ll[lk]) + '</span>');
      }
      partsL.push('<span style="border-left:1px solid var(--border);padding-left:8px"><span style="display:inline-block;width:14px;height:2px;background:rgba(34,197,94,.7);vertical-align:middle"></span> Excitatory</span>');
      partsL.push('<span><span style="display:inline-block;width:14px;height:2px;background:rgba(248,113,113,.7);vertical-align:middle;border-bottom:1px dashed"></span> Inhibitory</span>');
      partsL.push('<span>Observer: dashed ring</span><span>Hot + ripples: activation wave</span><span style="opacity:.85">Faint rings: layer guides</span>');
      partsL.push('<span><span style="display:inline-block;width:14px;height:2px;background:rgba(245,158,11,.65);vertical-align:middle;border-bottom:1px dashed"></span> Causal (interpretive)</span>');
      leg.innerHTML = partsL.join('');
    } else if (leg) {
      leg.innerHTML = '<span style="color:var(--accent)">Neural</span> '
        + '<span><span style="display:inline-block;width:14px;height:2px;background:rgba(34,197,94,.7);vertical-align:middle"></span> Excitatory</span>'
        + '<span><span style="display:inline-block;width:14px;height:2px;background:rgba(248,113,113,.7);vertical-align:middle;border-bottom:1px dashed"></span> Inhibitory</span>'
        + '<span style="border-left:1px solid var(--border);padding-left:8px">Observer: dashed ring</span>';
    }
    var labelEls = [];
    data.nodes.forEach(function(n) {
      var g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      var gcl = 'tbn-node tbn-neural-node';
      if (n.is_observer) gcl += ' tbn-neural-observer';
      if (n.stale) gcl += ' tbn-neural-stale';
      if (n.cooling) gcl += ' tbn-neural-cooling';
      if (n.enabled === false) gcl += ' tbn-neural-disabled';
      g.setAttribute('class', gcl);
      g.setAttribute('data-node-id', n.id);
      if (typeof n.lc_cluster_index === 'number' && n.lc_cluster_index >= 0) {
        g.setAttribute('data-lc-cluster-index', String(n.lc_cluster_index));
        g.setAttribute('data-lc-step-index', (typeof n.lc_step_index === 'number' && n.lc_step_index >= 0)
          ? String(n.lc_step_index) : '');
        g.setAttribute('data-lc-is-step', (typeof n.lc_step_index === 'number' && n.lc_step_index >= 0) ? '1' : '0');
      } else {
        g.setAttribute('data-lc-cluster-index', '');
        g.setAttribute('data-lc-step-index', '');
        g.setAttribute('data-lc-is-step', '0');
      }
      g.setAttribute('transform', 'translate(' + n.x + ',' + n.y + ')');
      var act = typeof n.activation_score === 'number' ? n.activation_score : parseFloat(n.activation_score) || 0;
      var conf = typeof n.confidence === 'number' ? n.confidence : parseFloat(n.confidence) || 0;
      var isHub = TBN_HUB_IDS[n.id] && Number(n.layer) === 3;
      var r = isHub
        ? (8 + Math.min(5, act * 7) + Math.min(3, conf * 2))
        : (7 + Math.min(14, act * 20) + Math.min(6, conf * 5));
      var haloWrap = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      haloWrap.setAttribute('class', 'tbn-neural-halo-wrap');
      haloWrap.setAttribute('pointer-events', 'none');
      var haloA = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      haloA.setAttribute('class', 'tbn-neural-halo tbn-neural-halo-a');
      haloA.setAttribute('cx', '0');
      haloA.setAttribute('cy', '0');
      haloA.setAttribute('r', String(r));
      var haloB = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      haloB.setAttribute('class', 'tbn-neural-halo tbn-neural-halo-b');
      haloB.setAttribute('cx', '0');
      haloB.setAttribute('cy', '0');
      haloB.setAttribute('r', String(r));
      haloWrap.appendChild(haloA);
      haloWrap.appendChild(haloB);
      g.appendChild(haloWrap);
      var circ = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circ.setAttribute('r', String(r));
      circ.setAttribute('class', 'tbn-node-circle');
      var glow = Math.min(0.95, 0.25 + act * 0.85);
      circ.setAttribute('fill', 'rgba(99,102,241,' + (0.25 + conf * 0.35) + ')');
      circ.setAttribute('stroke', 'rgba(167,139,250,' + glow + ')');
      circ.setAttribute('stroke-width', isHub ? '1.35' : '1.5');
      var t = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      var tipLines = [
        (n.label || n.id),
        (n.layer_label || ('L' + n.layer)) + ' act=' + act.toFixed(2),
      ];
      if (n.momentum_desk && n.momentum_desk.subtitle) tipLines.push(String(n.momentum_desk.subtitle));
      t.textContent = tipLines.join('\n');
      g.appendChild(t);
      g.appendChild(circ);
      g.addEventListener('mousedown', function(e) { e.stopPropagation(); });
      g.addEventListener('click', function(e) {
        e.stopPropagation();
        tbnOpenNeuralNodeDetailFromApi(n.id, g);
      });
      gNodes.appendChild(g);
      var ang = Math.atan2(n.y - CY, n.x - CX);
      var cr = Math.cos(ang);
      var sr = Math.sin(ang);
      var lp = r + 12;
      var lx = n.x + cr * lp;
      var ly = n.y + sr * lp;
      var text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('class', 'tbn-neural-label');
      text.setAttribute('x', String(lx));
      text.setAttribute('y', String(ly));
      text.setAttribute('text-anchor', cr >= 0 ? 'start' : 'end');
      if (Math.abs(sr) > 0.62) {
        text.setAttribute('dominant-baseline', sr > 0 ? 'hanging' : 'auto');
      } else {
        text.setAttribute('dominant-baseline', 'central');
      }
      var shown = (n.label_short != null && String(n.label_short).trim() !== '') ? String(n.label_short) : String(n.label || n.id);
      if (shown.length > 22) shown = shown.slice(0, 20) + '\u2026';
      text.textContent = shown;
      text.setAttribute('data-label-for', n.id);
      gNodes.appendChild(text);
      labelEls.push(text);
    });
    function tbnLabelRectsOverlap(a, b, pad) {
      return !(a.x + a.width + pad < b.x || b.x + b.width + pad < a.x ||
        a.y + a.height + pad < b.y || b.y + b.height + pad < a.y);
    }
    for (var pass = 0; pass < 2; pass++) {
      for (var li = 0; li < labelEls.length; li++) {
        for (var lj = li + 1; lj < labelEls.length; lj++) {
          try {
            var ra = labelEls[li].getBBox();
            var rb = labelEls[lj].getBBox();
            if (!tbnLabelRectsOverlap(ra, rb, 2)) continue;
            var ida = labelEls[li].getAttribute('data-label-for');
            var idb = labelEls[lj].getAttribute('data-label-for');
            var na = nodeMap[ida];
            var nb = nodeMap[idb];
            if (!na || !nb) continue;
            var xia = parseFloat(labelEls[li].getAttribute('x'));
            var yia = parseFloat(labelEls[li].getAttribute('y'));
            var xjb = parseFloat(labelEls[lj].getAttribute('x'));
            var yjb = parseFloat(labelEls[lj].getAttribute('y'));
            var tax = -(na.y - CY);
            var tay = (na.x - CX);
            var tlen = Math.sqrt(tax * tax + tay * tay) || 1;
            tax /= tlen;
            tay /= tlen;
            labelEls[li].setAttribute('x', String(xia + tax * 5));
            labelEls[li].setAttribute('y', String(yia + tay * 5));
            labelEls[lj].setAttribute('x', String(xjb - tax * 5));
            labelEls[lj].setAttribute('y', String(yjb - tay * 5));
          } catch (eLbl) {}
        }
      }
    }
    tbnNeuralFitCamera(vp, data.meta && data.meta.bounds ? data.meta.bounds : null);
    tbnFillMomentumDeskPanel(data);
    tbnRestartLivePoll();
  }

  function tbnRenderGraphData(data) {
    gEdges.innerHTML = '';
    gNodes.innerHTML = '';
    var gRingsClear = document.getElementById('tbn-neural-rings');
    var gCuesClear = document.getElementById('tbn-layer-cues');
    if (gRingsClear) gRingsClear.innerHTML = '';
    if (gCuesClear) gCuesClear.innerHTML = '';
    if (!data || !data.ok || !data.nodes || !data.edges) {
      tbnShowGraphError();
      return;
    }
    if (data.meta && data.meta.view === 'neural') {
      _tbnNeuralView = true;
      tbnRenderNeuralGraphData(data);
      return;
    }
    _tbnNeuralView = false;
    tbnFillMomentumDeskPanel({});
    window.__TBN_LAST_NEURAL_BOUNDS__ = null;
    var leg = document.getElementById('tbn-legend');
    if (leg) {
      leg.innerHTML = '<span><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#a78bfa;vertical-align:middle"></span> Root</span>'
        + '<span><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#60a5fa;vertical-align:middle"></span> Cluster</span>'
        + '<span><span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#94a3b8;vertical-align:middle"></span> Step</span>'
        + '<span style="border-left:2px dashed var(--accent);padding-left:6px">Pipeline</span>'
        + '<span><span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#22c55e;vertical-align:middle"></span> Running</span>'
        + '<span><span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#f59e0b;vertical-align:middle"></span> Pending</span>';
    }
    if (_tbnLiveTimer) {
      clearInterval(_tbnLiveTimer);
      _tbnLiveTimer = null;
    }
    var nodeMap = {};
    data.nodes.forEach(function(n) { nodeMap[n.id] = n; });

    var edgeList = data.edges.slice().sort(function(ea, eb) {
      var pa = (ea.kind === 'pipeline') ? 0 : 1;
      var pb = (eb.kind === 'pipeline') ? 0 : 1;
      return pa - pb;
    });
    edgeList.forEach(function(edge) {
      var a = nodeMap[edge.from];
      var b = nodeMap[edge.to];
      if (!a || !b) return;
      var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', String(a.x));
      line.setAttribute('y1', String(a.y));
      line.setAttribute('x2', String(b.x));
      line.setAttribute('y2', String(b.y));
      var ec = 'tbn-edge';
      if (edge.kind === 'pipeline') ec += ' tbn-edge-pipeline';
      else if (b.tier === 'step') ec += ' tbn-edge-step';
      line.setAttribute('class', ec);
      gEdges.appendChild(line);
    });

    var hintEl = document.getElementById('tbn-hint-text');
    if (hintEl && data.meta && data.meta.description) {
      hintEl.innerHTML = 'Pan by dragging; scroll to zoom. <span style="opacity:.85">' +
        escHtml(data.meta.description).slice(0, 220) + (data.meta.description.length > 220 ? '\u2026' : '') + '</span>';
    }

    data.nodes.forEach(function(n) {
      var g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      g.setAttribute('class', 'tbn-node tbn-tier-' + n.tier);
      g.setAttribute('data-node-id', n.id);
      var cidx = typeof n.cluster_index === 'number' ? n.cluster_index : -1;
      var sidx = typeof n.step_index === 'number' ? n.step_index : -1;
      g.setAttribute('data-cluster-index', String(cidx));
      g.setAttribute('data-step-index', String(sidx));
      var inLc = n.tier !== 'root' && n.in_learning_cycle !== false;
      g.setAttribute('data-in-learning-cycle', inLc ? '1' : '0');
      g.setAttribute('transform', 'translate(' + n.x + ',' + n.y + ')');
      var r = n.tier === 'root' ? 30 : (n.tier === 'cluster' ? 19 : 8);
      var circ = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circ.setAttribute('r', r);
      circ.setAttribute('class', 'tbn-node-circle');
      var t = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      var tip = n.label;
      if (n.code_ref) tip += '\n' + n.code_ref;
      if (n.phase) tip += '\nPhase: ' + n.phase;
      t.textContent = tip;
      g.appendChild(t);
      g.appendChild(circ);
      if (n.tier === 'step') {
        var lines = tbnStepLabelLines(n.label, 22, 26);
        var stext = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        stext.setAttribute('class', 'tbn-node-label');
        stext.setAttribute('text-anchor', 'middle');
        stext.setAttribute('y', r + 8);
        lines.forEach(function(line, li) {
          var tsp = document.createElementNS('http://www.w3.org/2000/svg', 'tspan');
          tsp.setAttribute('x', '0');
          if (li > 0) tsp.setAttribute('dy', '1.08em');
          tsp.textContent = line;
          stext.appendChild(tsp);
        });
        g.appendChild(stext);
      } else {
        var text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('class', 'tbn-node-label');
        text.setAttribute('y', r + 15);
        text.setAttribute('text-anchor', 'middle');
        var lab = n.label;
        if (lab.length > 30) lab = lab.slice(0, 28) + '\u2026';
        text.textContent = lab;
        g.appendChild(text);
      }
      g.addEventListener('mousedown', function(e) { e.stopPropagation(); });
      g.addEventListener('click', function(e) {
        e.stopPropagation();
        tbnOpenNodeDetail(n, g);
      });
      gNodes.appendChild(g);
    });

    var vw = vp.clientWidth || 800;
    var vh = vp.clientHeight || 480;
    var sc = _tbnState.scale;
    _tbnState.tx = (vw - 1600 * sc) / 2;
    _tbnState.ty = (vh - 950 * sc) / 2;
    _tbnApplyStage();
  }

  function tbnRefreshTradingBrainNetworkGraph() {
    tbnCloseNodeDetail();
    var btn = document.getElementById('tbn-refresh-graph');
    if (btn) btn.disabled = true;
    var wantNeural = tbnWantsNeuralStrict();
    fetch(tbnGraphFetchUrl(), { credentials: 'same-origin' })
      .then(function(r) {
        if (!r.ok) throw new Error('http ' + r.status);
        return r.json();
      })
      .then(function(d) {
        if (d && d.ok && d.nodes && d.edges) {
          if (wantNeural && !(d.meta && d.meta.view === 'neural')) {
            tbnShowNeuralGraphUnavailable('Payload was not neural (meta.view)');
            return;
          }
          tbnRenderGraphData(d);
          return;
        }
        if (wantNeural) tbnShowNeuralGraphUnavailable('Bad or empty graph payload');
        else tbnShowGraphError();
      })
      .catch(function(err) {
        if (wantNeural) tbnShowNeuralGraphUnavailable(err && err.message ? err.message : 'network error');
        else tbnShowGraphError();
      })
      .finally(function() {
        if (btn) btn.disabled = false;
      });
  }
  window.chiliRefreshTradingBrainNetworkGraph = tbnRefreshTradingBrainNetworkGraph;
  var tbnRefreshBtn = document.getElementById('tbn-refresh-graph');
  if (tbnRefreshBtn) tbnRefreshBtn.onclick = function() { tbnRefreshTradingBrainNetworkGraph(); };

  function tbnBootTradingBrainGraph() {
    fetch('/api/trading/brain/graph/config', { credentials: 'same-origin' })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d && d.ok) tbnApplyServerDeskConfig(d);
        tbnInitGraphModeUiAfterConfig();
        tbnRefreshTradingBrainNetworkGraph();
      })
      .catch(function() {
        tbnInitGraphModeUiAfterConfig();
        tbnRefreshTradingBrainNetworkGraph();
      });
  }
  tbnBootTradingBrainGraph();

  // Reset view button
  var resetBtn = document.getElementById('tbn-reset-view');
  if (resetBtn) {
    resetBtn.onclick = function() {
      if (_tbnNeuralView && window.__TBN_LAST_NEURAL_BOUNDS__) {
        tbnNeuralFitCamera(vp, window.__TBN_LAST_NEURAL_BOUNDS__);
      } else {
        var vw = vp.clientWidth || 900;
        var vh = vp.clientHeight || 500;
        _tbnState.scale = Math.min(vw / 1600, vh / 950, 0.65);
        _tbnState.tx = (vw - 1600 * _tbnState.scale) / 2;
        _tbnState.ty = (vh - 950 * _tbnState.scale) / 2;
        _tbnApplyStage();
      }
    };
  }

  // Search nodes
  var searchInput = document.getElementById('tbn-search');
  if (searchInput) {
    searchInput.addEventListener('input', function() {
      var q = this.value.trim().toLowerCase();
      var allNodes = gNodes.querySelectorAll('.tbn-node');
      allNodes.forEach(function(g) {
        var title = g.querySelector('title');
        var txt = (title ? title.textContent : '').toLowerCase();
        var label = g.querySelector('.tbn-node-label');
        var ltxt = (label ? label.textContent : '').toLowerCase();
        var match = !q || txt.indexOf(q) >= 0 || ltxt.indexOf(q) >= 0;
        g.style.opacity = match ? '1' : '0.15';
      });
    });
  }

  // ResizeObserver for viewport
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(function() {
      var svg = document.getElementById('trading-brain-network-svg');
      if (svg && vp.clientWidth > 0) {
        svg.setAttribute('width', String(Math.max(vp.clientWidth, 800)));
      }
    }).observe(vp);
  }

  // Reconcile-pass status overlay — highlights active cluster/step during full in-process reconcile (compatibility graph)
  function tbnUpdateNodeStatuses() {
    fetch('/api/trading/scan/status', { credentials: 'same-origin' })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (!d || !d.ok) return;
        // Graph overlay only: needs mesh/cluster/step indices and graph_node_id from full top-level `learning`.
        var lr = d.learning;
        if (!lr) return;
        var running = !!lr.running;
        var graphId = (lr.graph_node_id || '').toLowerCase();
        var meshStep = (lr.mesh_step_node_id || '').toLowerCase();
        var meshCluster = (lr.mesh_cluster_node_id || '').toLowerCase();
        var curCi = typeof lr.current_cluster_index === 'number' ? lr.current_cluster_index : -1;
        var curSi = typeof lr.current_step_index === 'number' ? lr.current_step_index : -1;
        var curClusterId = (lr.current_cluster_id || '').toLowerCase();
        var hintEl = document.getElementById('tbn-hint-text');
        if (hintEl && _tbnNeuralView && lr.secondary_miners_skipped) {
          hintEl.setAttribute('data-lc-secondary-skipped', '1');
        } else if (hintEl) {
          hintEl.removeAttribute('data-lc-secondary-skipped');
        }
        var allNodes = gNodes.querySelectorAll('.tbn-node');
        allNodes.forEach(function(g) {
          var circle = g.querySelector('.tbn-node-circle');
          if (!circle) return;
          g.classList.remove(
            'tbn-node--running', 'tbn-node--pending', 'tbn-cluster--active',
            'tbn-neural--lc-running', 'tbn-neural--lc-pending', 'tbn-neural--lc-cluster-active'
          );
          if (!running) return;
          var nid = (g.getAttribute('data-node-id') || '').toLowerCase();
          if (_tbnNeuralView && g.classList.contains('tbn-neural-node')) {
            if (meshStep && nid === meshStep) {
              g.classList.add('tbn-neural--lc-running');
              return;
            }
            var lcciRaw = g.getAttribute('data-lc-cluster-index');
            var lcsiRaw = g.getAttribute('data-lc-step-index');
            var isLcStep = g.getAttribute('data-lc-is-step') === '1';
            var lcci = parseInt(lcciRaw, 10);
            var lcsi = parseInt(lcsiRaw, 10);
            if (isNaN(lcci)) lcci = -1;
            if (isNaN(lcsi)) lcsi = -1;
            if (!isLcStep && meshCluster && nid === meshCluster) {
              g.classList.add('tbn-neural--lc-cluster-active');
              return;
            }
            if (isLcStep && curCi >= 0 && curSi >= 0 && lcci >= 0 && lcsi >= 0) {
              if (lcci > curCi || (lcci === curCi && lcsi > curSi)) {
                g.classList.add('tbn-neural--lc-pending');
              }
            }
            return;
          }
          var inLc = g.getAttribute('data-in-learning-cycle') !== '0';
          var cci = parseInt(g.getAttribute('data-cluster-index'), 10);
          var si = parseInt(g.getAttribute('data-step-index'), 10);
          if (isNaN(cci)) cci = -1;
          if (isNaN(si)) si = -1;
          if (g.classList.contains('tbn-tier-step') && graphId && nid === graphId) {
            g.classList.add('tbn-node--running');
            return;
          }
          if (g.classList.contains('tbn-tier-cluster') && curClusterId && nid === curClusterId) {
            g.classList.add('tbn-cluster--active');
            return;
          }
          if (g.classList.contains('tbn-tier-step') && inLc && curCi >= 0 && curSi >= 0 && cci >= 0 && si >= 0) {
            if (cci > curCi || (cci === curCi && si > curSi)) {
              g.classList.add('tbn-node--pending');
            }
          }
        });
      })
      .catch(function() {});
  }
  if (window._tbnStatusInterval) clearInterval(window._tbnStatusInterval);
  window._tbnStatusInterval = setInterval(tbnUpdateNodeStatuses, 3000);
  tbnUpdateNodeStatuses();
}

/* -- Layer filtering -- */
var _tbnActiveLayerFilters = {};
function tbnFilterLayer(layer) {
  if (layer === 'all') {
    var allActive = document.querySelectorAll('.tbn-layer-btn.active').length ===
      document.querySelectorAll('.tbn-layer-btn').length;
    document.querySelectorAll('.tbn-layer-btn').forEach(function(btn) {
      if (allActive) { btn.classList.remove('active'); _tbnActiveLayerFilters = {}; }
      else { btn.classList.add('active'); _tbnActiveLayerFilters[btn.getAttribute('data-layer')] = true; }
    });
  } else {
    var btn = document.querySelector('.tbn-layer-btn[data-layer="' + layer + '"]');
    if (btn) {
      btn.classList.toggle('active');
      if (btn.classList.contains('active')) _tbnActiveLayerFilters[layer] = true;
      else delete _tbnActiveLayerFilters[layer];
    }
    var allBtn = document.querySelector('.tbn-layer-btn[data-layer="all"]');
    var total = document.querySelectorAll('.tbn-layer-btn:not([data-layer="all"])').length;
    var active = document.querySelectorAll('.tbn-layer-btn.active:not([data-layer="all"])').length;
    if (allBtn) allBtn.classList.toggle('active', active === total);
  }
  _tbnApplyLayerVisibility();
}

function _tbnApplyLayerVisibility() {
  var gNodes = document.getElementById('tbn-nodes');
  var gEdges = document.getElementById('tbn-edges');
  if (!gNodes) return;
  var hasFilter = Object.keys(_tbnActiveLayerFilters).length > 0 &&
    Object.keys(_tbnActiveLayerFilters).length < document.querySelectorAll('.tbn-layer-btn:not([data-layer="all"])').length;
  gNodes.querySelectorAll('[data-layer]').forEach(function(el) {
    if (!hasFilter) { el.style.opacity = ''; el.style.pointerEvents = ''; return; }
    var l = el.getAttribute('data-layer');
    var visible = _tbnActiveLayerFilters[l];
    el.style.opacity = visible ? '' : '0.08';
    el.style.pointerEvents = visible ? '' : 'none';
  });
  if (gEdges) {
    gEdges.querySelectorAll('[data-src-layer][data-tgt-layer]').forEach(function(el) {
      if (!hasFilter) { el.style.opacity = ''; return; }
      var vis = _tbnActiveLayerFilters[el.getAttribute('data-src-layer')] ||
                _tbnActiveLayerFilters[el.getAttribute('data-tgt-layer')];
      el.style.opacity = vis ? '' : '0.04';
    });
  }
}

/* -- Hover tooltips -- */
var _tbnTooltipEl = null;
function tbnShowTooltip(node, x, y) {
  if (!_tbnTooltipEl) {
    _tbnTooltipEl = document.createElement('div');
    _tbnTooltipEl.className = 'tbn-hover-tooltip';
    var vp = document.getElementById('trading-brain-network-viewport');
    if (vp) vp.appendChild(_tbnTooltipEl);
  }
  var label = node.getAttribute('data-label') || node.getAttribute('data-id') || 'Node';
  var layer = node.getAttribute('data-layer') || '';
  var type = node.getAttribute('data-type') || '';
  _tbnTooltipEl.innerHTML = '<strong>' + label + '</strong>' +
    (layer ? ' <span style="color:var(--text-muted)">&middot; L' + layer + '</span>' : '') +
    (type ? '<br><span style="color:var(--text-muted)">' + type + '</span>' : '');
  _tbnTooltipEl.style.left = (x + 12) + 'px';
  _tbnTooltipEl.style.top = (y - 10) + 'px';
  _tbnTooltipEl.classList.add('visible');
}
function tbnHideTooltip() {
  if (_tbnTooltipEl) _tbnTooltipEl.classList.remove('visible');
}

/* -- Minimap -- */
function tbnUpdateMinimap() {
  var canvas = document.getElementById('tbn-minimap-canvas');
  var vpEl = document.getElementById('tbn-minimap-viewport');
  var viewport = document.getElementById('trading-brain-network-viewport');
  if (!canvas || !vpEl || !viewport) return;
  var ctx = canvas.getContext('2d');
  var cw = canvas.width, ch = canvas.height;
  ctx.clearRect(0, 0, cw, ch);

  var svg = document.getElementById('trading-brain-network-svg');
  if (!svg) return;
  var vb = svg.getAttribute('viewBox');
  if (!vb) return;
  var parts = vb.split(/\s+/);
  var svgW = parseFloat(parts[2]) || 1600;
  var svgH = parseFloat(parts[3]) || 950;
  var scaleX = cw / svgW;
  var scaleY = ch / svgH;

  var nodes = svg.querySelectorAll('#tbn-nodes circle, #tbn-nodes ellipse');
  nodes.forEach(function(n) {
    var cx = parseFloat(n.getAttribute('cx')) || 0;
    var cy = parseFloat(n.getAttribute('cy')) || 0;
    var r = parseFloat(n.getAttribute('r')) || 2;
    var fill = getComputedStyle(n).fill || '#6366f1';
    ctx.beginPath();
    ctx.arc(cx * scaleX, cy * scaleY, Math.max(1, r * scaleX * 0.5), 0, Math.PI * 2);
    ctx.fillStyle = fill;
    ctx.globalAlpha = 0.7;
    ctx.fill();
  });
  ctx.globalAlpha = 1;

  var vpRect = viewport.getBoundingClientRect();
  var vpW = vpRect.width / _tbnState.scale;
  var vpH = vpRect.height / _tbnState.scale;
  var vpX = -_tbnState.tx / _tbnState.scale;
  var vpY = -_tbnState.ty / _tbnState.scale;
  vpEl.style.left = Math.round(vpX * scaleX) + 'px';
  vpEl.style.top = Math.round(vpY * scaleY) + 'px';
  vpEl.style.width = Math.round(vpW * scaleX) + 'px';
  vpEl.style.height = Math.round(vpH * scaleY) + 'px';
}

/* -- Zoom to cluster -- */
function tbnZoomToCluster(clusterId) {
  var gNodes = document.getElementById('tbn-nodes');
  if (!gNodes) return;
  var nodes = gNodes.querySelectorAll('[data-cluster="' + clusterId + '"]');
  if (!nodes.length) return;
  var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  nodes.forEach(function(n) {
    var cx = parseFloat(n.getAttribute('cx') || n.getAttribute('x')) || 0;
    var cy = parseFloat(n.getAttribute('cy') || n.getAttribute('y')) || 0;
    if (cx < minX) minX = cx;
    if (cy < minY) minY = cy;
    if (cx > maxX) maxX = cx;
    if (cy > maxY) maxY = cy;
  });
  var vp = document.getElementById('trading-brain-network-viewport');
  if (!vp) return;
  var vpW = vp.clientWidth;
  var vpH = vp.clientHeight;
  var cw = maxX - minX + 100;
  var ch = maxY - minY + 100;
  var scale = Math.min(vpW / cw, vpH / ch, 2);
  var cx = (minX + maxX) / 2;
  var cy = (minY + maxY) / 2;
  _tbnState.scale = scale;
  _tbnState.tx = vpW / 2 - cx * scale;
  _tbnState.ty = vpH / 2 - cy * scale;
  _tbnApplyStage();
  tbnUpdateMinimap();
}
