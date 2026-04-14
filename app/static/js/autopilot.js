(function() {
  'use strict';
  var cfg = window.CHILI_AP_CONFIG || {};
  var IS_GUEST = cfg.IS_GUEST;

  var state = {
    opportunityMode: 'paper',
    assetFilter: 'all',
    opportunitySearch: '',
    opportunities: [],
    discovered: [],
    opportunityMeta: {},
    sessions: [],
    _prevSessions: [],
    detailCache: {},
    visibleSessionIds: {},
    charts: {},
    observer: null,
    liveArmPending: null,
    pollHandle: null
  };

  function esc(value) {
    if (value == null) return '';
    var div = document.createElement('div');
    div.textContent = String(value);
    return div.innerHTML;
  }

  function safeNum(value, fallback) {
    var num = Number(value);
    return isFinite(num) ? num : fallback;
  }

  function pct(value) {
    var num = Number(value);
    if (!isFinite(num)) return 'n/a';
    if (num <= 1) num = num * 100;
    return num.toFixed(num >= 10 ? 0 : 1) + '%';
  }

  function money(value) {
    var num = Number(value);
    if (!isFinite(num)) return 'n/a';
    return (num < 0 ? '-$' : '$') + Math.abs(num).toFixed(2);
  }

  function runtimeLabel(runtime) {
    var sec = runtime && runtime.seconds;
    if (sec !== 0 && !sec) return 'n/a';
    sec = Number(sec);
    if (!isFinite(sec)) return 'n/a';
    if (sec < 60) return Math.round(sec) + 's';
    if (sec < 3600) return Math.floor(sec / 60) + 'm';
    return Math.floor(sec / 3600) + 'h ' + Math.floor((sec % 3600) / 60) + 'm';
  }

  function dateLabel(value) {
    if (!value) return 'n/a';
    try {
      return new Date(value).toLocaleString();
    } catch (_err) {
      return String(value);
    }
  }

  function etaLabel(seconds) {
    var num = Number(seconds);
    if (!isFinite(num)) return 'n/a';
    if (num < 60) return num + 's';
    return Math.ceil(num / 60) + 'm';
  }

  function badge(text, cls) {
    return '<span class="ap-badge ' + esc(cls || '') + '">' + esc(text) + '</span>';
  }

  function searchMatchesOpportunity(row) {
    if (!state.opportunitySearch) return true;
    var hay = [
      row.symbol,
      row.asset_class,
      row.scan_context && row.scan_context.label,
      row.top_variant && row.top_variant.label,
      row.top_variant && row.top_variant.family
    ].join(' ').toLowerCase();
    return hay.indexOf(state.opportunitySearch) >= 0;
  }

  function jsonPreview(value) {
    try {
      return JSON.stringify(value || {}, null, 2);
    } catch (_err) {
      return String(value || '');
    }
  }

  function runnerStateText(health) {
    if (!health) return 'n/a';
    if (health.blocked_reason) return 'Blocked';
    if (!health.enabled) return 'Off';
    if (!health.scheduler_enabled) return 'Manual only';
    return 'Ready';
  }

  function runnerStateBadge(health) {
    if (!health) return badge('runner unknown', 'warn');
    if (health.blocked_reason) return badge('runner blocked', 'blocked');
    if (!health.enabled) return badge('runner off', 'warn');
    if (!health.scheduler_enabled) return badge('manual runner', 'warn');
    return badge('runner ready', 'compatible');
  }

  function explainBlockedReasons(reasons) {
    var list = Array.isArray(reasons) ? reasons.filter(Boolean) : [];
    if (!list.length) return 'No active blockers.';
    return list.join(', ').replace(/_/g, ' ');
  }

  function refinementBadge(info) {
    if (info && info.is_refined) return badge('Brain refined', 'refined');
    return badge('Seeded', 'seeded');
  }

  function pipelineStateText(pipeline) {
    if (!pipeline) return 'Unknown';
    if (pipeline.viability_pipeline_stale) return 'Stale';
    if (Number(pipeline.pending_refresh_count || 0) > 0) return 'Warming up';
    if (pipeline.last_viability_tick_utc) return 'Healthy';
    return 'Idle';
  }

  function opportunityMetaBlurb(meta) {
    var bits = [];
    if (!meta) return '';
    if (Number(meta.hidden_scan_only_count || 0) > 0) {
      bits.push(String(meta.hidden_scan_only_count) + ' scan-only symbol' + (Number(meta.hidden_scan_only_count) === 1 ? ' is' : 's are') + ' hidden until fresh symbol viability lands');
    }
    if (Number(meta.pending_refresh_count || 0) > 0) {
      bits.push(String(meta.pending_refresh_count) + ' momentum refresh event' + (Number(meta.pending_refresh_count) === 1 ? '' : 's') + ' pending');
    }
    if (meta.last_viability_tick_utc) {
      bits.push('last viability tick ' + dateLabel(meta.last_viability_tick_utc));
    }
    return bits.join(' | ');
  }

  function opportunityEmptyMessage(meta) {
    meta = meta || {};
    if (meta.viability_pipeline_stale) {
      return 'Symbol viability is stale. Pending momentum refresh work has backed up, so scan-only symbols stay hidden until the backlog clears.';
    }
    if (Number(meta.hidden_scan_only_count || 0) > 0) {
      return String(meta.hidden_scan_only_count) + ' scanned symbol' + (Number(meta.hidden_scan_only_count) === 1 ? ' is' : 's are') + ' warming up. Fresh per-symbol viability has not landed yet, so the board is staying actionable-only.';
    }
    if (Number(meta.market_closed_hidden_count || 0) > 0) {
      return 'Fresh symbol viability exists, but the stock market is closed for the current filter. Switch to crypto or come back during market hours.';
    }
    return 'No actionable opportunities matched the current filters.';
  }

  function renderFilterState() {
    var modeButtons = document.querySelectorAll('#ap-mode-filters [data-mode]');
    modeButtons.forEach(function(btn) {
      var sel = btn.getAttribute('data-mode') === state.opportunityMode;
      btn.setAttribute('aria-selected', String(sel));
      btn.classList.toggle('active', sel);
    });
    var assetButtons = document.querySelectorAll('#ap-asset-filters [data-asset]');
    assetButtons.forEach(function(btn) {
      var sel = btn.getAttribute('data-asset') === state.assetFilter;
      btn.setAttribute('aria-selected', String(sel));
      btn.classList.toggle('active', sel);
    });
  }

  function renderSummary(summary) {
    var strip = document.getElementById('ap-summary-strip');
    var blockers = document.getElementById('ap-blockers');
    if (!strip || !blockers) return;
    var lanes = summary && summary.lanes || {};
    var paperRunner = summary && summary.paper_runner_health || {};
    var liveRunner = summary && summary.live_runner_health || {};
    var pipeline = summary && summary.viability_pipeline || {};
    function sc(label, value, detail, valueCls) {
      return '<div class="ap-stat-card">'
        + '<span class="ap-stat-card__label">' + esc(label) + '</span>'
        + '<span class="ap-stat-card__value' + (valueCls ? ' ' + valueCls : '') + '">' + esc(value) + '</span>'
        + '<span class="ap-stat-card__detail">' + detail + '</span>'
        + '</div>';
    }
    strip.innerHTML = ''
      + sc('Sessions', summary.total_sessions || 0,
          'Sim ' + esc(lanes.simulation || 0) + ' &middot; Armed ' + esc(lanes['live-armed'] || 0) + ' &middot; Live ' + esc(lanes.live || 0))
      + sc('Paper Runner', runnerStateText(paperRunner),
          'Tick: ' + esc(dateLabel(paperRunner.last_tick_utc)) + '<br>ETA: ' + esc(etaLabel(paperRunner.next_tick_eta_seconds)))
      + sc('Live Runner', runnerStateText(liveRunner),
          'Tick: ' + esc(dateLabel(liveRunner.last_tick_utc)) + '<br>ETA: ' + esc(etaLabel(liveRunner.next_tick_eta_seconds)))
      + sc('Pending', summary.pending_paper_drafts || 0,
          'Queued ' + esc(summary.paper_runner_queued || 0) + ' &middot; Armed ' + esc(summary.armed_awaiting_runner || 0))
      + sc('Pipeline', pipelineStateText(pipeline),
          'Tick: ' + esc(dateLabel(pipeline.last_viability_tick_utc)) + '<br>Pending: ' + esc(pipeline.pending_refresh_count || 0))
      + sc('Governance',
          summary.governance && summary.governance.kill_switch_active ? 'Kill switch' : 'Normal',
          'Event: ' + esc(dateLabel(summary.last_event_ts)) + '<br>' + esc(summary.limitations_note || ''));

    var cards = [];
    if (!paperRunner.enabled && !liveRunner.enabled) {
      cards.push(
        '<div class="ap-blocker" style="border-color:rgba(239,68,68,0.5);background:rgba(239,68,68,0.12)">'
        + '<strong>Autopilot runners disabled</strong> '
        + 'Both paper and live runners are OFF. Sessions will not advance automatically. '
        + 'Set CHILI_MOMENTUM_PAPER_RUNNER_ENABLED=1 and CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=1 in your environment '
        + '(or docker-compose.yml) and restart the scheduler-worker service.'
        + '</div>'
      );
    } else {
      if (!paperRunner.enabled) {
        cards.push(
          '<div class="ap-blocker" style="border-color:rgba(245,158,11,0.5);background:rgba(245,158,11,0.10)">'
          + '<strong>Paper runner disabled</strong> '
          + 'Paper sessions will not tick. Set CHILI_MOMENTUM_PAPER_RUNNER_ENABLED=1 to enable.'
          + '</div>'
        );
      }
      if (!liveRunner.enabled) {
        cards.push(
          '<div class="ap-blocker" style="border-color:rgba(245,158,11,0.5);background:rgba(245,158,11,0.10)">'
          + '<strong>Live runner disabled</strong> '
          + 'Live sessions will not tick. Set CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=1 to enable.'
          + '</div>'
        );
      }
    }
    if (paperRunner.blocked_reason) {
      cards.push(
        '<div class="ap-blocker"><strong>Paper runner blocked</strong>'
        + 'Sessions can be created, but they will not advance until the paper runner and scheduler path are healthy.'
        + '<br>Reason: ' + esc(String(paperRunner.blocked_reason).replace(/_/g, ' '))
        + (paperRunner.scheduler_heartbeat_utc ? '<br>Scheduler heartbeat: ' + esc(dateLabel(paperRunner.scheduler_heartbeat_utc)) : '')
        + '</div>'
      );
    }
    if (liveRunner.blocked_reason) {
      cards.push(
        '<div class="ap-blocker"><strong>Live runner gated</strong>'
        + 'Live control paths remain visible, but actual live execution is blocked until runtime readiness is healthy.'
        + '<br>Reason: ' + esc(String(liveRunner.blocked_reason).replace(/_/g, ' '))
        + '</div>'
      );
    }
    if (pipeline.viability_pipeline_stale) {
      cards.push(
        '<div class="ap-blocker"><strong>Viability pipeline stale</strong>'
        + 'Autopilot is hiding scan-only candidates because pending symbol refresh work is older than the healthy threshold.'
        + '<br>Pending refresh: ' + esc(pipeline.pending_refresh_count || 0)
        + (pipeline.oldest_pending_refresh_utc ? '<br>Oldest pending: ' + esc(dateLabel(pipeline.oldest_pending_refresh_utc)) : '')
        + '</div>'
      );
    } else if (Number(pipeline.pending_refresh_count || 0) > 0) {
      cards.push(
        '<div class="ap-blocker"><strong>Viability warming up</strong>'
        + 'Recent symbol refreshes are still queued. Scan-only symbols stay hidden until fresh per-symbol viability is written.'
        + '<br>Pending refresh: ' + esc(pipeline.pending_refresh_count || 0)
        + '</div>'
      );
    }
    blockers.innerHTML = cards.join('');
  }

  function opportunityActionButtons(row) {
    var top = row.top_variant || {};
    var variantId = top.variant_id;
    var paperAction = row.paper_action || {};
    var liveAction = row.live_action || {};
    var paperDisabled = !paperAction.enabled || !variantId;
    var liveDisabled = !liveAction.enabled || !variantId;
    return ''
      + '<button type="button" class="ap-btn ap-btn-primary" '
      + (paperDisabled ? 'disabled ' : '')
      + 'onclick="apRunPaper(\'' + esc(row.symbol) + '\',' + Number(variantId || 0) + ')">' + esc(paperAction.label || 'Create draft') + '</button>'
      + '<button type="button" class="ap-btn ap-btn-live" '
      + (liveDisabled ? 'disabled ' : '')
      + 'onclick="apArmLive(\'' + esc(row.symbol) + '\',' + Number(variantId || 0) + ')">' + esc(liveAction.label || 'Arm live') + '</button>'
      + '<button type="button" class="ap-btn" onclick="apNeuralRefresh(\'' + esc(row.symbol) + '\')">Brain refresh</button>';
  }

  function opportunityReason(row) {
    var action = state.opportunityMode === 'live' ? (row.live_action || {}) : (row.paper_action || {});
    if (row.compatible_now) {
      if (action.detail) {
        return action.detail;
      }
      return 'Compatible now for ' + state.opportunityMode + ' mode.';
    }
    return explainBlockedReasons(row.blocked_reasons);
  }

  function renderOpportunities() {
    var list = document.getElementById('ap-opportunities');
    var metaEl = document.getElementById('ap-opportunities-meta');
    var empty = document.getElementById('ap-opportunities-empty');
    if (!list || !empty) return;
    var meta = state.opportunityMeta || {};
    if (metaEl) {
      var blurb = opportunityMetaBlurb(meta);
      metaEl.innerHTML = esc(blurb);
      metaEl.style.display = blurb ? 'block' : 'none';
    }
    var rows = (state.opportunities || []).filter(searchMatchesOpportunity);
    var hasRows = rows.length > 0;
    if (!hasRows) {
      list.innerHTML = '';
      empty.innerHTML = ''
        + '<div class="ap-empty-state">'
        + '  <div class="ap-empty-state__icon">&#x1F50D;</div>'
        + '  <div class="ap-empty-state__title">No matching opportunities</div>'
        + '  <div class="ap-empty-state__desc">' + esc(opportunityEmptyMessage(meta)) + '</div>'
        + '</div>';
      empty.style.display = 'block';
    } else {
      empty.style.display = 'none';
    }
    renderDiscovered();
    if (!hasRows) return;
    empty.style.display = 'none';
    list.innerHTML = rows.map(function(row) {
      var scan = row.scan_context || {};
      var top = row.top_variant || {};
      var params = top.strategy_params_summary || {};
      var liveAction = row.live_action || {};
      var compatBadge = row.compatible_now ? badge('Compatible now', 'compatible') : badge('Blocked', 'blocked');
      var marketBadge = row.asset_class === 'crypto'
        ? badge('24x7', 'open')
        : badge(row.market_open_now ? 'Market open' : 'Market closed', row.market_open_now ? 'open' : 'closed');
      var scanScore = safeNum(scan.score, 0);
      var viability = safeNum(top.viability_score, 0);
      return ''
        + '<div class="ap-opp-card" aria-expanded="false" data-symbol="' + esc(row.symbol) + '" data-asset="' + esc(row.asset_class) + '">'
        + '  <div class="ap-opp-header" onclick="this.parentElement.setAttribute(\'aria-expanded\', this.parentElement.getAttribute(\'aria-expanded\')===\'true\'?\'false\':\'true\')" role="button" tabindex="0">'
        + '    <div class="ap-opp-header__left">'
        + '      <span class="ap-opp-header__symbol">' + esc(row.symbol) + '</span>'
        +        badge(row.asset_class || 'unknown', row.asset_class || '')
        +        marketBadge
        +        compatBadge
        +        refinementBadge(top.refinement_info)
        + '      <span class="ap-opp-header__signal">Score <b>' + esc(scanScore.toFixed(1)) + '</b> &middot; Viability <b>' + esc(viability.toFixed(2)) + '</b></span>'
        + '    </div>'
        + '    <div class="ap-opp-header__actions">'
        +        opportunityActionButtons(row)
        + '      <span class="ap-opp-expand-icon" aria-hidden="true">&#x25B6;</span>'
        + '    </div>'
        + '  </div>'
        + '  <div class="ap-opp-details">'
        + '    <div class="ap-subline">'
        + '      Strategy: <strong style="color:var(--text)">' + esc(top.label || 'No fresh strategy variant') + '</strong>'
        + '      &middot; Freshness: ' + esc(dateLabel(row.freshness_ts))
        + '    </div>'
        + '    <div class="ap-badges">'
        +        badge(row.paper_ready ? 'Paper ready' : 'Paper blocked', row.paper_ready ? 'compatible' : 'warn')
        +        badge(row.live_ready ? 'Live ready' : 'Live blocked', row.live_ready ? 'live' : 'blocked')
        +        (scan.signal ? badge('Signal ' + String(scan.signal), 'paper') : badge('No scan signal', 'warn'))
        +        ((!row.can_run_paper && row.can_create_paper_draft) ? badge('Draft only', 'warn') : '')
        +        (liveAction.armed_only ? badge('Armed only', 'warn') : '')
        + '    </div>'
        + '    <div class="ap-callout ' + (row.compatible_now ? 'good' : 'warn') + '">'
        + '      <strong>Compatibility</strong>' + esc(opportunityReason(row))
        + '    </div>'
        + '    <div class="ap-row-grid">'
        + '      <div class="ap-metric"><label>Scan score</label><b>' + esc(scanScore.toFixed(3)) + '</b><small>' + esc(scan.source || 'no scan source') + '</small></div>'
        + '      <div class="ap-metric"><label>Viability</label><b>' + esc(viability.toFixed(3)) + '</b><small>' + esc(top.family || 'n/a') + '</small></div>'
        + '      <div class="ap-metric"><label>Entry floor</label><b>' + esc(params.entry_viability_min != null ? params.entry_viability_min : 'n/a') + '</b><small>Revalidate ' + esc(params.entry_revalidate_floor != null ? params.entry_revalidate_floor : 'n/a') + '</small></div>'
        + '      <div class="ap-metric"><label>Scalp hold cap</label><b>' + esc(params.max_hold_seconds != null ? params.max_hold_seconds + 's' : 'n/a') + '</b><small>Trail ' + esc(params.trail_activate_return_bps != null ? params.trail_activate_return_bps + ' bps' : 'n/a') + '</small></div>'
        + '    </div>'
        + '    <div class="ap-inline-list">'
        + '      <span class="ap-inline-chip">Blocked: ' + esc(explainBlockedReasons(row.blocked_reasons)) + '</span>'
        + '      <span class="ap-inline-chip">Signal: ' + esc(scan.label || 'n/a') + '</span>'
        + '      <span class="ap-inline-chip">Variant: ' + esc(top.version != null ? 'v' + top.version : 'n/a') + '</span>'
        + '    </div>'
        + '  </div>'
        + '</div>';
    }).join('');
  }

  function renderDiscovered() {
    var section = document.getElementById('ap-discovered-section');
    var listEl = document.getElementById('ap-discovered-list');
    if (!section || !listEl) return;
    var disc = (state.discovered || []).filter(function(d) {
      if (!state.opportunitySearch) return true;
      return (d.symbol || '').toLowerCase().indexOf(state.opportunitySearch) >= 0;
    });
    if (!disc.length) {
      section.style.display = 'none';
      listEl.innerHTML = '';
      return;
    }
    section.style.display = 'block';
    listEl.innerHTML = disc.map(function(d) {
      var scan = d.scan_context || {};
      var marketBadge = d.asset_class === 'crypto'
        ? badge('24x7', 'open')
        : badge(d.market_open_now ? 'Market open' : 'Market closed', d.market_open_now ? 'open' : 'closed');
      return ''
        + '<div class="ap-opportunity-row" style="opacity:0.7;">'
        + '  <div class="ap-op-top">'
        + '    <div>'
        + '      <div class="ap-symbol-lockup">'
        + '        <span class="ap-symbol">' + esc(d.symbol) + '</span>'
        +          badge(d.asset_class || 'unknown', d.asset_class || '')
        +          marketBadge
        +          badge('Assessing...', 'warn')
        + '      </div>'
        + '      <div class="ap-subline" style="margin-top:6px;">'
        + '        Scanner: <strong style="color:var(--text)">' + esc(scan.source || 'scanner') + '</strong>'
        + '        | Score: ' + esc(safeNum(scan.score, 0).toFixed(1))
        + (scan.signal ? ' | Signal: ' + esc(String(scan.signal)) : '')
        + '      </div>'
        + '    </div>'
        + '  </div>'
        + '</div>';
    }).join('');
  }

  function fetchJson(url, options) {
    return fetch(url, options || { credentials: 'same-origin' }).then(function(resp) {
      return resp.json().then(function(data) {
        if (!resp.ok) {
          var detail = data && data.detail;
          var message = typeof detail === 'string' ? detail : JSON.stringify(detail || data || {});
          throw new Error(message || ('Request failed: ' + resp.status));
        }
        return data;
      });
    });
  }

  function showToast(message, type) {
    var container = document.getElementById('ap-toast-container');
    if (!container) { window.alert(message); return; }
    var toast = document.createElement('div');
    toast.className = 'ap-toast' + (type ? ' ' + type : '');
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(function() {
      toast.style.opacity = '0';
      toast.style.transition = 'opacity 0.3s';
      setTimeout(function() { toast.remove(); }, 300);
    }, 4000);
  }

  function showError(message) {
    showToast(message, 'error');
  }

  function showSuccess(message) {
    showToast(message, 'success');
  }

  function trackEvent(name, data) {
    try {
      console.log('[autopilot:telemetry]', name, data || {});
    } catch (_err) {}
  }

  function renderDecisionStrip(recent, abstentions, deployment) {
    var el = document.getElementById('ap-decision-strip');
    if (!el) return;
    if (!recent || !recent.length) {
      el.textContent = '';
      return;
    }
    var last = recent[0];
    var dep0 = (deployment && deployment[0]) || {};
    var abstLast = (abstentions && abstentions[0]) || null;
    var parts = [];
    parts.push('Decision ledger: last #' + (last.id || '?') + ' ' + (last.decision_type || '') + ' ' + (last.execution_mode || ''));
    if (last.expected_edge_net != null) parts.push('net edge ' + Number(last.expected_edge_net).toFixed(4));
    if (last.deployment_stage) parts.push('stage ' + last.deployment_stage);
    if (last.capacity_blocked) parts.push('capacity signal');
    if (abstLast && abstLast.abstain_reason_code) {
      parts.push('last abstain: ' + abstLast.abstain_reason_code);
    }
    if (dep0.current_stage) parts.push('deploy ' + dep0.scope_key + '=' + dep0.current_stage);
    el.textContent = parts.join(' · ');
  }

  function openLiveModal(payload) {
    state.liveArmPending = payload;
    var modal = document.getElementById('ap-live-modal');
    var body = document.getElementById('ap-live-modal-body');
    var checkbox = document.getElementById('ap-live-confirm-check');
    var confirmBtn = document.getElementById('ap-live-modal-confirm');
    if (!modal || !body) return;
    var lines = [];
    var confirmation = payload.confirmation || {};
    var evaluation = payload.risk_evaluation || {};
    lines.push('<div class="ap-callout block"><strong>Live execution warning</strong>This action can place real orders on your configured execution venue. Review the details below carefully.</div>');
    lines.push('<div class="ap-callout"><strong>Symbol</strong>' + esc(confirmation.symbol || payload.symbol || 'n/a') + '</div>');
    lines.push('<div class="ap-callout"><strong>Variant</strong>#' + esc(confirmation.variant_id || payload.variant_id || 'n/a') + ' &middot; Viability ' + esc(confirmation.viability_score != null ? confirmation.viability_score : 'n/a') + '</div>');
    lines.push('<div class="ap-callout"><strong>Risk evaluation</strong>Severity ' + esc(evaluation.severity || 'n/a') + '<br>Allowed ' + esc(String(evaluation.allowed)) + '</div>');
    if (Array.isArray(confirmation.warnings) && confirmation.warnings.length) {
      lines.push('<div class="ap-callout warn"><strong>Warnings</strong>' + esc(confirmation.warnings.join(' | ')) + '</div>');
    }
    if (confirmation.disclaimer) {
      lines.push('<div class="ap-callout"><strong>Disclaimer</strong>' + esc(confirmation.disclaimer) + '</div>');
    }
    body.innerHTML = lines.join('');
    if (checkbox) {
      checkbox.checked = false;
      checkbox.onchange = function() {
        if (confirmBtn) confirmBtn.disabled = !checkbox.checked;
      };
    }
    if (confirmBtn) confirmBtn.disabled = true;
    modal.classList.add('open');
    if (checkbox) checkbox.focus();
    modal._trapFocus = function(e) {
      if (e.key === 'Escape') { apCloseLiveModal(); return; }
      if (e.key !== 'Tab') return;
      var focusable = modal.querySelectorAll('button:not([disabled]), input, [tabindex]');
      if (!focusable.length) return;
      var first = focusable[0], last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', modal._trapFocus);
  }

  window.apRefreshDecisionLedger = function() {
    if (IS_GUEST) return Promise.resolve();
    return Promise.all([
      fetchJson('/api/trading/momentum/automation/decisions/recent?limit=8', { credentials: 'same-origin' }),
      fetchJson('/api/trading/momentum/automation/decisions/abstentions/recent?limit=5', { credentials: 'same-origin' }),
      fetchJson('/api/trading/momentum/automation/deployment/summary', { credentials: 'same-origin' })
    ]).then(function(results) {
      renderDecisionStrip(results[0].packets || [], results[1].packets || [], results[2].deployment || []);
    }).catch(function() {
      var el = document.getElementById('ap-decision-strip');
      if (el) el.textContent = '';
    });
  };

  window.apRefreshSummary = function() {
    if (IS_GUEST) return Promise.resolve();
    return fetchJson('/api/trading/momentum/automation/summary', { credentials: 'same-origin' })
      .then(renderSummary)
      .then(function() { return apRefreshDecisionLedger(); })
      .catch(function(err) {
        showError(err.message || 'Failed to load summary.');
      });
  };

  window.apRefreshOpportunities = function() {
    if (IS_GUEST) return Promise.resolve();
    var list = document.getElementById('ap-opportunities');
    if (list && !state.opportunities.length) {
      list.innerHTML = '<div class="ap-skeleton-row"><div class="ap-skeleton"></div><div class="ap-skeleton"></div><div class="ap-skeleton"></div></div>';
    }
    var url = '/api/trading/momentum/opportunities?mode=' + encodeURIComponent(state.opportunityMode)
      + '&asset_class=' + encodeURIComponent(state.assetFilter)
      + '&limit=80';
    return fetchJson(url, { credentials: 'same-origin' })
      .then(function(data) {
        state.opportunities = data.opportunities || [];
        state.discovered = data.discovered || [];
        state.opportunityMeta = data.metadata || {};
        renderOpportunities();
      })
      .catch(function(err) {
        showError(err.message || 'Failed to load opportunities.');
        trackEvent('ErrorOccurred', { action: 'RefreshOpportunities', error: err.message });
      });
  };

  window.apToggleEligible = function() {
    var body = document.getElementById('ap-eligible-body');
    var btn = document.getElementById('ap-eligible-toggle');
    if (!body || !btn) return;
    var collapsed = body.classList.toggle('ap-collapsed');
    btn.textContent = collapsed ? 'Expand' : 'Collapse';
    btn.setAttribute('aria-expanded', String(!collapsed));
  };

  window.apNeuralRefresh = function(symbol) {
    if (IS_GUEST) return;
    trackEvent('NeuralRefresh', { symbol: symbol });
    fetchJson('/api/trading/momentum/refresh', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: symbol, execution_family: 'coinbase_spot' })
    }).then(function(payload) {
      showSuccess(payload.accepted ? ('Refresh queued for ' + symbol) : ('Refresh not queued: ' + (payload.reason || 'blocked')));
      apRefreshOpportunities();
    }).catch(function(err) {
      showError(err.message || 'Failed to queue neural refresh.');
      trackEvent('ErrorOccurred', { action: 'NeuralRefresh', error: err.message });
    });
  };

  window.apRunPaper = function(symbol, variantId) {
    if (!variantId) return;
    trackEvent('SessionCreated', { symbol: symbol, variant_id: variantId, mode: 'paper' });
    fetchJson('/api/trading/momentum/run-paper', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: symbol, variant_id: Number(variantId), execution_family: 'coinbase_spot' })
    }).then(function(payload) {
      showSuccess(payload.message || ('Paper workflow started for ' + symbol));
      apRefreshAll();
    }).catch(function(err) {
      showError(err.message || 'Failed to start paper workflow.');
      trackEvent('ErrorOccurred', { action: 'RunPaper', error: err.message });
    });
  };

  window.apCloseLiveModal = function() {
    state.liveArmPending = null;
    var modal = document.getElementById('ap-live-modal');
    if (modal) {
      modal.classList.remove('open');
      if (modal._trapFocus) {
        document.removeEventListener('keydown', modal._trapFocus);
        modal._trapFocus = null;
      }
    }
  };

  window.apArmLive = function(symbol, variantId) {
    if (!variantId) return;
    fetchJson('/api/trading/momentum/arm-live', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: symbol, variant_id: Number(variantId), execution_family: 'coinbase_spot' })
    }).then(function(payload) {
      if (payload.arm_token && payload.confirmation) {
        payload.symbol = symbol;
        payload.variant_id = variantId;
        openLiveModal(payload);
        return;
      }
      window.alert(payload.message || ('Live arm request processed for ' + symbol));
      apRefreshAll();
    }).catch(function(err) {
      showError(err.message || 'Failed to arm live workflow.');
    });
  };

  window.apConfirmLiveArm = function() {
    if (!state.liveArmPending || !state.liveArmPending.arm_token) {
      apCloseLiveModal();
      return;
    }
    trackEvent('LiveArmConfirmed', { symbol: state.liveArmPending.symbol, variant_id: state.liveArmPending.variant_id });
    fetchJson('/api/trading/momentum/confirm-live-arm', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ arm_token: state.liveArmPending.arm_token, confirm: true })
    }).then(function(payload) {
      apCloseLiveModal();
      showSuccess(payload.message || ('Live arm confirmed for ' + (payload.symbol || 'session')));
      apRefreshAll();
    }).catch(function(err) {
      apCloseLiveModal();
      showError(err.message || 'Failed to confirm live arm.');
      trackEvent('ErrorOccurred', { action: 'ConfirmLiveArm', error: err.message });
    });
  };

  window.ChiliAutopilot = {
    state: state,
    esc: esc,
    safeNum: safeNum,
    pct: pct,
    money: money,
    runtimeLabel: runtimeLabel,
    dateLabel: dateLabel,
    etaLabel: etaLabel,
    badge: badge,
    jsonPreview: jsonPreview,
    runnerStateText: runnerStateText,
    runnerStateBadge: runnerStateBadge,
    explainBlockedReasons: explainBlockedReasons,
    refinementBadge: refinementBadge,
    fetchJson: fetchJson,
    showError: showError,
    showSuccess: showSuccess,
    showToast: showToast,
    trackEvent: trackEvent,
    renderFilterState: renderFilterState,
    renderOpportunities: renderOpportunities,
    IS_GUEST: IS_GUEST
  };
})();
