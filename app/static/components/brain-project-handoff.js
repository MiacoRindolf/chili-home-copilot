(function () {
var BRAIN_PLANNER_TASK_HINT = window.__CHILI_BRAIN_PLANNER_TASK_HINT__;
var BRAIN_PLANNER_PROJECT_HINT = window.__CHILI_BRAIN_PLANNER_PROJECT_HINT__;

function brainHandoffPlannerProjectId() {
  var t = BRAIN_PLANNER_PROJECT_HINT;
  if (typeof t === 'number' && t >= 1) return t;
  try {
    var q = new URLSearchParams(window.location.search || '').get('planner_project_id');
    if (q != null && q !== '') {
      var n = parseInt(q, 10);
      if (!isNaN(n) && n >= 1) return n;
    }
  } catch (e) {}
  return null;
}

function brainHandoffPlannerTaskId() {
  var t = BRAIN_PLANNER_TASK_HINT;
  if (typeof t === 'number' && t >= 1) return t;
  try {
    var q = new URLSearchParams(window.location.search || '').get('planner_task_id');
    if (q != null && q !== '') {
      var n = parseInt(q, 10);
      if (!isNaN(n) && n >= 1) return n;
    }
  } catch (e) {}
  return null;
}

function brainHandoffJsonToMarkdown(h) {
  if (!h) return '';
  var lines = [];
  lines.push('# Implementation handoff');
  lines.push('');
  if (h.task) {
    lines.push('## Task');
    lines.push('- id: ' + h.task.id);
    lines.push('- project_id: ' + (h.task.project_id != null ? h.task.project_id : ''));
    lines.push('- title: ' + (h.task.title || ''));
    lines.push('- readiness: ' + (h.task.coding_readiness_state || ''));
    lines.push('- workflow: ' + (h.task.coding_workflow_mode || ''));
    lines.push('');
  }
  if (h.readiness_context) {
    var rc = h.readiness_context;
    lines.push('## Readiness context (stored)');
    lines.push('- coding_readiness_state: ' + (rc.coding_readiness_state || ''));
    lines.push('- open_clarification_count: ' + (rc.open_clarification_count != null ? rc.open_clarification_count : ''));
    lines.push('- brief_approved_at: ' + (rc.brief_approved_at != null ? rc.brief_approved_at : '(null)'));
    lines.push('');
  }
  if (h.brief) {
    lines.push('## Brief (v' + h.brief.version + ')');
    lines.push(h.brief.body || '');
    lines.push('');
  }
  if (h.profile) {
    lines.push('## Repo profile');
    lines.push('- repo_index: ' + h.profile.repo_index);
    lines.push('- sub_path: ' + (h.profile.sub_path || ''));
    lines.push('');
  }
  if (h.ops_hints) {
    lines.push('## Ops hints');
    lines.push('- repos configured: ' + h.ops_hints.code_repos_configured_count);
    lines.push('- repo index ok: ' + h.ops_hints.repo_index_valid);
    lines.push('- cwd resolvable: ' + h.ops_hints.cwd_resolvable);
    lines.push('');
  }
  if (h.validation_latest) {
    var v = h.validation_latest;
    lines.push('## Latest validation run');
    lines.push('- id: ' + v.id + ' | status: ' + v.status + ' | exit: ' + v.exit_code + ' | timed_out: ' + v.timed_out);
    if (v.error_message) lines.push('- error: ' + v.error_message);
    lines.push('');
  }
  if (h.blockers && h.blockers.length) {
    lines.push('## Blockers');
    h.blockers.forEach(function(b) {
      lines.push('- [' + b.severity + '/' + b.category + '] ' + (b.summary || ''));
    });
    lines.push('');
  }
  if (h.clarifications && h.clarifications.length) {
    lines.push('## Clarifications');
    h.clarifications.forEach(function(c) {
      lines.push('### #' + c.id + ' (' + (c.status || '') + ', sort ' + (c.sort_order != null ? c.sort_order : '') + ')');
      lines.push('- Q: ' + (c.question || ''));
      lines.push('- A: ' + (c.answer != null ? c.answer : ''));
    });
    lines.push('');
  }
  if (h.artifact_previews && h.artifact_previews.length) {
    lines.push('## Artifact previews (latest run only)');
    h.artifact_previews.forEach(function(a) {
      lines.push('### ' + a.step_key + ' (' + a.kind + ')');
      lines.push('```');
      lines.push(a.content_preview || '');
      lines.push('```');
      lines.push('');
    });
  }
  return lines.join('\n');
}

/* Phase 15: in-memory handoff for prefill bridge only; set exclusively on successful explicit Load (never Copy / URL / storage). */
var _brainHandoffPrefillCache = null;

function brainSanitizeTaskTitleForPrefill(raw) {
  if (raw == null) return '';
  var s = String(raw).replace(/[\u0000-\u001F\u007F]/g, ' ').replace(/\s+/g, ' ').trim();
  if (s.length > 400) s = s.slice(0, 400);
  return s;
}

function brainUpdatePrefillBridgeButton() {
  var btn = document.getElementById('brain-handoff-prefill-code-search-btn');
  if (!btn) return;
  var h = _brainHandoffPrefillCache;
  var title = (h && h.task && h.task.title != null) ? brainSanitizeTaskTitleForPrefill(h.task.title) : '';
  btn.disabled = !title;
}

function brainPrefillCodeSearchFromHandoff() {
  var h = _brainHandoffPrefillCache;
  if (!h || !h.task || h.task.title == null) return;
  var q = brainSanitizeTaskTitleForPrefill(h.task.title);
  if (!q) return;
  var input = document.getElementById('code-search-input');
  if (!input) return;
  input.value = q;
  try {
    input.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (e) {
    try { input.scrollIntoView(true); } catch (e2) {}
  }
  try { input.focus(); } catch (e) {}
}

function brainLoadPlannerHandoff(tid) {
  var pre = document.getElementById('brain-handoff-preview');
  var wrap = document.getElementById('brain-handoff-preview-wrap');
  var st = document.getElementById('brain-handoff-status');
  var bindEl = document.getElementById('brain-handoff-project-bind');
  if (bindEl) bindEl.textContent = '';
  _brainHandoffPrefillCache = null;
  brainUpdatePrefillBridgeButton();
  fetch('/api/planner/tasks/' + tid + '/coding/handoff', { credentials: 'same-origin' })
    .then(function(r) { return r.json().then(function(d) { return { httpOk: r.ok, d: d }; }); })
    .then(function(x) {
      if (!x.httpOk || !x.d || !x.d.ok || !x.d.handoff) {
        _brainHandoffPrefillCache = null;
        brainUpdatePrefillBridgeButton();
        if (st) st.textContent = 'Handoff load failed.';
        alert((x.d && x.d.error) ? x.d.error : 'Handoff unavailable');
        return;
      }
      var handoff = x.d.handoff;
      _brainHandoffPrefillCache = handoff;
      brainUpdatePrefillBridgeButton();
      if (pre) pre.textContent = JSON.stringify(handoff, null, 2);
      if (wrap) wrap.style.display = 'block';
      if (st) st.textContent = 'Handoff preview loaded (read-only; not auto-updated).';

      /* Phase 14: optional mismatch hint (URL vs handoff.task.id); project GET only here, never on Copy or init */
      var urlTid = brainHandoffPlannerTaskId();
      var hTask = handoff.task || {};
      var handoffTaskId = hTask.id;
      var bindLines = [];
      if (urlTid != null && handoffTaskId != null && Number(urlTid) !== Number(handoffTaskId)) {
        bindLines.push('Note: URL planner_task_id (' + urlTid + ') differs from handoff task id (' + handoffTaskId + ').');
      }
      var projId = hTask.project_id;
      if (projId == null || projId === '') {
        if (bindEl) bindEl.textContent = bindLines.join(' ');
        return;
      }
      fetch('/api/planner/projects/' + encodeURIComponent(String(projId)), { credentials: 'same-origin' })
        .then(function(r) { return r.json().then(function(d) { return { httpOk: r.ok, d: d }; }); })
        .then(function(px) {
          if (px.httpOk && px.d && px.d.project) {
            var p = px.d.project;
            var nm = (p.name != null && String(p.name) !== '') ? String(p.name) : '';
            var ky = (p.key != null && String(p.key) !== '') ? String(p.key) : '';
            var label = 'Planner project: ';
            if (nm && ky) label += nm + ' · key ' + ky;
            else if (nm) label += nm;
            else if (ky) label += 'key ' + ky;
            else label += '(no name or key)';
            bindLines.unshift(label);
          } else {
            bindLines.unshift('Planner project metadata unavailable.');
          }
          if (bindEl) bindEl.textContent = bindLines.join(' ');
        })
        .catch(function() {
          bindLines.unshift('Planner project metadata request failed.');
          if (bindEl) bindEl.textContent = bindLines.join(' ');
        });
    })
    .catch(function() {
      _brainHandoffPrefillCache = null;
      brainUpdatePrefillBridgeButton();
      if (st) st.textContent = 'Handoff request failed.';
    });
}

function brainCopyHandoffJson(tid) {
  var st = document.getElementById('brain-handoff-status');
  fetch('/api/planner/tasks/' + tid + '/coding/handoff', { credentials: 'same-origin' })
    .then(function(r) { return r.json().then(function(d) { return { httpOk: r.ok, d: d }; }); })
    .then(function(x) {
      var d = x.d;
      if (!x.httpOk || !d || !d.ok || !d.handoff) {
        alert((d && d.error) ? d.error : 'Handoff failed');
        return;
      }
      var text = JSON.stringify(d.handoff, null, 2);
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function() {
          if (st) st.textContent = 'Handoff JSON copied.';
        }).catch(function() { alert('Clipboard failed'); });
      } else {
        alert('Clipboard not available');
      }
    });
}

function brainCopyHandoffMarkdown(tid) {
  var st = document.getElementById('brain-handoff-status');
  fetch('/api/planner/tasks/' + tid + '/coding/handoff', { credentials: 'same-origin' })
    .then(function(r) { return r.json().then(function(d) { return { httpOk: r.ok, d: d }; }); })
    .then(function(x) {
      var d = x.d;
      if (!x.httpOk || !d || !d.ok || !d.handoff) {
        alert((d && d.error) ? d.error : 'Handoff failed');
        return;
      }
      var text = brainHandoffJsonToMarkdown(d.handoff);
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function() {
          if (st) st.textContent = 'Handoff Markdown copied (from JSON).';
        }).catch(function() { alert('Clipboard failed'); });
      } else {
        alert('Clipboard not available');
      }
    });
}

/* Phase 16: last successful agent-suggest payload in memory for this page (taskId + six-field payload only). */
var brainLastAgentSuggestOk = null;
var brainApplySuggestionId = null;

function brainUpdateApplyControls(tid) {
  var btnApply = document.getElementById('brain-handoff-apply-snapshot-btn');
  var btnDry = document.getElementById('brain-handoff-dry-run-snapshot-btn');
  var curTid = brainHandoffPlannerTaskId();
  var ok = brainApplySuggestionId != null && curTid != null && Number(curTid) === Number(tid);
  if (btnApply) {
    btnApply.disabled = !ok;
    btnApply.onclick = ok ? function() { brainConfirmApplySnapshot(tid, brainApplySuggestionId); } : null;
  }
  if (btnDry) {
    btnDry.disabled = !ok;
    btnDry.onclick = ok ? function() { brainConfirmDryRunSnapshot(tid, brainApplySuggestionId); } : null;
  }
}

function brainConfirmDryRunSnapshot(tid, sid) {
  if (!confirm('Dry run only: run git apply --check for snapshot #' + sid + ' at the registered repo root? No files will be modified. This is not a real apply.')) return;
  var ar = document.getElementById('brain-handoff-apply-result');
  if (ar) { ar.style.display = 'block'; ar.style.color = 'var(--text-muted)'; ar.textContent = 'Dry run (git apply --check) for snapshot #' + sid + '…'; }
  fetch('/api/planner/tasks/' + encodeURIComponent(String(tid)) + '/coding/agent-suggestions/' + encodeURIComponent(String(sid)) + '/apply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ dry_run: true })
  }).then(function(r) { return r.json().then(function(d) { return { httpOk: r.ok, d: d, code: r.status }; }); })
  .then(function(x) {
    if (!ar) return;
    ar.style.color = 'var(--text)';
    if (!x.httpOk || !x.d || !x.d.ok) {
      ar.textContent = 'Dry run failed: ' + ((x.d && x.d.message) ? x.d.message : JSON.stringify(x.d));
      brainLoadApplyAttempts(tid, sid);
      return;
    }
    ar.textContent = 'Dry run OK: ' + JSON.stringify(x.d, null, 2);
    brainLoadApplyAttempts(tid, sid);
  }).catch(function() {
    if (ar) ar.textContent = 'Dry run network error.';
  });
}

function brainConfirmApplySnapshot(tid, sid) {
  if (!confirm('Apply snapshot #' + sid + ' to the registered repo root? All-or-nothing git apply. Use git checkout to undo if needed.')) return;
  var ar = document.getElementById('brain-handoff-apply-result');
  if (ar) { ar.style.display = 'block'; ar.style.color = 'var(--text-muted)'; ar.textContent = 'Applying snapshot #' + sid + '…'; }
  fetch('/api/planner/tasks/' + encodeURIComponent(String(tid)) + '/coding/agent-suggestions/' + encodeURIComponent(String(sid)) + '/apply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ dry_run: false })
  }).then(function(r) { return r.json().then(function(d) { return { httpOk: r.ok, d: d, code: r.status }; }); })
  .then(function(x) {
    if (!ar) return;
    ar.style.color = 'var(--text)';
    if (!x.httpOk || !x.d || !x.d.ok) {
      ar.textContent = 'Apply failed: ' + ((x.d && x.d.message) ? x.d.message : JSON.stringify(x.d));
      brainLoadApplyAttempts(tid, sid);
      return;
    }
    ar.textContent = 'OK: ' + JSON.stringify(x.d, null, 2);
    brainLoadApplyAttempts(tid, sid);
  }).catch(function() {
    if (ar) ar.textContent = 'Apply network error.';
  });
}

function brainUpdateSaveSuggestionControls(tid) {
  var btn = document.getElementById('brain-handoff-save-suggestion-btn');
  if (!btn) return;
  var ok = brainLastAgentSuggestOk && Number(brainLastAgentSuggestOk.taskId) === Number(tid);
  btn.disabled = !ok;
  btn.onclick = ok ? function() { brainSaveSuggestionSnapshot(tid); } : null;
}

function brainRefreshSuggestionList(tid) {
  var wrap = document.getElementById('brain-handoff-suggestions-wrap');
  var ul = document.getElementById('brain-handoff-suggestions-list');
  if (!tid || !wrap || !ul) return;
  wrap.style.display = 'block';
  ul.textContent = 'Loading…';
  fetch('/api/planner/tasks/' + encodeURIComponent(String(tid)) + '/coding/agent-suggestions?limit=20', {
    credentials: 'same-origin'
  }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
  .then(function(x) {
    ul.innerHTML = '';
    if (!x.ok || !x.d || !x.d.ok || !x.d.suggestions) {
      ul.textContent = (x.d && x.d.error) ? String(x.d.error) : 'Could not load list.';
      return;
    }
    var rows = x.d.suggestions;
    if (rows.length === 0) {
      ul.textContent = 'No saved snapshots yet.';
      return;
    }
    rows.forEach(function(s) {
      var li = document.createElement('li');
      li.style.marginBottom = '4px';
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'brain-act-btn';
      b.style.cssText = 'padding:3px 8px;font-size:9px;background:#334155;';
      b.textContent = '#' + s.id + ' · ' + (s.created_at || '') + ' · ' + (s.model || '') + ' · diffs:' + (s.diffs_count != null ? s.diffs_count : 0) + ' files:' + (s.files_changed_count != null ? s.files_changed_count : 0);
      b.onclick = function() { brainLoadSuggestionDetail(tid, s.id); };
      li.appendChild(b);
      ul.appendChild(li);
    });
  }).catch(function() {
    ul.textContent = 'Network error.';
  });
}

function brainClearValidationBridgeUi() {
  var wrap = document.getElementById('brain-handoff-validation-bridge-wrap');
  var ul = document.getElementById('brain-handoff-validation-runs-list');
  var rb = document.getElementById('brain-handoff-validation-runs-refresh-btn');
  var runBtn = document.getElementById('brain-handoff-run-validation-btn');
  var pre = document.getElementById('brain-handoff-validation-run-result');
  if (wrap) wrap.style.display = 'none';
  if (ul) ul.textContent = '';
  if (rb) rb.onclick = null;
  if (runBtn) runBtn.onclick = null;
  if (pre) { pre.style.display = 'none'; pre.textContent = ''; }
}

function brainSetupValidationBridge(tid) {
  var wrap = document.getElementById('brain-handoff-validation-bridge-wrap');
  var ul = document.getElementById('brain-handoff-validation-runs-list');
  var rb = document.getElementById('brain-handoff-validation-runs-refresh-btn');
  var runBtn = document.getElementById('brain-handoff-run-validation-btn');
  if (!wrap || !tid) return;
  wrap.style.display = 'block';
  if (ul) ul.textContent = 'Click Refresh runs list to load validation metadata (no auto-fetch).';
  if (rb) rb.onclick = function() { brainLoadValidationRunsMeta(tid); };
  if (runBtn) runBtn.onclick = function() { brainConfirmRunWorkspaceValidation(tid); };
}

function brainLoadValidationRunsMeta(tid) {
  var ul = document.getElementById('brain-handoff-validation-runs-list');
  if (!ul || !tid) return;
  ul.textContent = 'Loading…';
  fetch('/api/planner/tasks/' + encodeURIComponent(String(tid)) + '/coding/validation/runs', {
    credentials: 'same-origin'
  }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
  .then(function(x) {
    ul.innerHTML = '';
    if (!x.ok || !x.d || !x.d.ok || !Array.isArray(x.d.runs)) {
      ul.textContent = (x.d && x.d.error) ? String(x.d.error) : 'Could not load validation runs.';
      return;
    }
    var rows = x.d.runs;
    if (rows.length === 0) {
      ul.textContent = 'No validation runs yet.';
      return;
    }
    rows.forEach(function(rn) {
      var li = document.createElement('li');
      li.style.marginBottom = '3px';
      var em = rn.error_message ? String(rn.error_message).slice(0, 100) : '';
      li.textContent = '#' + rn.id + ' · ' + (rn.trigger_source || '') + ' · ' + (rn.status || '') + ' · exit ' + (rn.exit_code != null ? rn.exit_code : '—') + ' · timeout:' + rn.timed_out + ' · ' + (rn.started_at || '') + (em ? ' · ' + em : '');
      ul.appendChild(li);
    });
  }).catch(function() {
    ul.textContent = 'Network error.';
  });
}

function brainConfirmRunWorkspaceValidation(tid) {
  if (!confirm('Phase 20: Run workspace validation for this task now? Uses existing non-destructive tools. Explicit only; not automatic after apply or load.')) return;
  var pre = document.getElementById('brain-handoff-validation-run-result');
  if (pre) {
    pre.style.display = 'block';
    pre.style.color = 'var(--text-muted)';
    pre.textContent = 'Running validation…';
  }
  fetch('/api/planner/tasks/' + encodeURIComponent(String(tid)) + '/coding/validation/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ trigger_source: 'post_apply' })
  }).then(function(r) { return r.json().then(function(d) { return { httpOk: r.ok, d: d, code: r.status }; }); })
  .then(function(x) {
    if (!pre) return;
    pre.style.color = 'var(--text)';
    if (!x.httpOk || !x.d || !x.d.ok) {
      pre.textContent = 'Validation run failed: ' + ((x.d && x.d.error) ? x.d.error : JSON.stringify(x.d));
      brainLoadValidationRunsMeta(tid);
      return;
    }
    pre.textContent = 'Validation OK: ' + JSON.stringify(x.d, null, 2);
    brainLoadValidationRunsMeta(tid);
  }).catch(function() {
    if (pre) pre.textContent = 'Validation network error.';
  });
}

function brainClearApplyAttemptsUi() {
  var wrap = document.getElementById('brain-handoff-apply-attempts-wrap');
  var ul = document.getElementById('brain-handoff-apply-attempts-list');
  var rb = document.getElementById('brain-handoff-apply-attempts-refresh-btn');
  if (wrap) wrap.style.display = 'none';
  if (ul) ul.textContent = '';
  if (rb) rb.onclick = null;
}

function brainLoadApplyAttempts(tid, sid) {
  var wrap = document.getElementById('brain-handoff-apply-attempts-wrap');
  var ul = document.getElementById('brain-handoff-apply-attempts-list');
  var rb = document.getElementById('brain-handoff-apply-attempts-refresh-btn');
  if (!wrap || !ul || !tid || !sid) return;
  wrap.style.display = 'block';
  ul.textContent = 'Loading…';
  if (rb) rb.onclick = function() { brainLoadApplyAttempts(tid, sid); };
  fetch('/api/planner/tasks/' + encodeURIComponent(String(tid)) + '/coding/agent-suggestions/' + encodeURIComponent(String(sid)) + '/apply-attempts', {
    credentials: 'same-origin'
  }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
  .then(function(x) {
    ul.innerHTML = '';
    if (!x.ok || !x.d || !x.d.ok || !x.d.apply_attempts) {
      ul.textContent = (x.d && x.d.error) ? String(x.d.error) : 'Could not load apply attempts.';
      return;
    }
    var rows = x.d.apply_attempts;
    if (rows.length === 0) {
      ul.textContent = 'No apply attempts yet.';
      return;
    }
    rows.forEach(function(a) {
      var li = document.createElement('li');
      li.style.marginBottom = '3px';
      li.textContent = '#' + a.id + ' · ' + (a.created_at || '') + ' · user ' + a.user_id + ' · dry_run:' + a.dry_run + ' · ' + (a.status || '') + ' · ' + (a.message_preview || '');
      ul.appendChild(li);
    });
  }).catch(function() {
    ul.textContent = 'Network error.';
  });
}

function brainLoadSuggestionDetail(tid, sid) {
  var pre = document.getElementById('brain-handoff-suggestion-detail');
  if (!tid || !pre) return;
  brainClearApplyAttemptsUi();
  brainApplySuggestionId = null;
  brainUpdateApplyControls(tid);
  pre.style.display = 'block';
  pre.style.color = 'var(--text-muted)';
  pre.textContent = 'Loading snapshot #' + sid + '…';
  fetch('/api/planner/tasks/' + encodeURIComponent(String(tid)) + '/coding/agent-suggestions/' + encodeURIComponent(String(sid)), {
    credentials: 'same-origin'
  }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
  .then(function(x) {
    if (!x.ok || !x.d || !x.d.ok || !x.d.suggestion) {
      pre.textContent = 'Error: ' + ((x.d && x.d.error) ? x.d.error : 'Not found');
      brainApplySuggestionId = null;
      brainUpdateApplyControls(tid);
      brainClearApplyAttemptsUi();
      return;
    }
    pre.style.color = 'var(--text)';
    try {
      pre.textContent = JSON.stringify(x.d.suggestion, null, 2);
    } catch (e) {
      pre.textContent = String(x.d.suggestion);
    }
    brainApplySuggestionId = sid;
    brainUpdateApplyControls(tid);
    brainLoadApplyAttempts(tid, sid);
    try { pre.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch (e2) { try { pre.scrollIntoView(true); } catch (e3) {} }
  }).catch(function() {
    pre.textContent = 'Network error.';
    brainApplySuggestionId = null;
    brainUpdateApplyControls(tid);
    brainClearApplyAttemptsUi();
  });
}

function brainSaveSuggestionSnapshot(tid) {
  if (!brainLastAgentSuggestOk || Number(brainLastAgentSuggestOk.taskId) !== Number(tid)) return;
  var btn = document.getElementById('brain-handoff-save-suggestion-btn');
  if (btn) btn.disabled = true;
  fetch('/api/planner/tasks/' + encodeURIComponent(String(tid)) + '/coding/agent-suggestions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(brainLastAgentSuggestOk.payload)
  }).then(function(r) { return r.json().then(function(d) { return { httpOk: r.ok, d: d, code: r.status }; }); })
  .then(function(x) {
    if (btn) btn.disabled = false;
    brainUpdateSaveSuggestionControls(tid);
    var st = document.getElementById('brain-handoff-status');
    if (!x.httpOk || !x.d || !x.d.ok) {
      var msg = (x.d && x.d.message) ? x.d.message : (x.d && x.d.error) ? x.d.error : 'Save failed';
      if (st) st.textContent = 'Snapshot save failed: ' + msg;
      return;
    }
    if (st) st.textContent = 'Saved snapshot id ' + x.d.id + ' (append-only).';
    brainRefreshSuggestionList(tid);
  }).catch(function() {
    if (btn) btn.disabled = false;
    brainUpdateSaveSuggestionControls(tid);
    var st = document.getElementById('brain-handoff-status');
    if (st) st.textContent = 'Snapshot save network error.';
  });
}

function brainRunAgentSuggestFromPlannerTask(tid) {
  var out = document.getElementById('brain-handoff-agent-suggest-result');
  var btn = document.getElementById('brain-handoff-agent-suggest-btn');
  if (!tid || !out) return;
  brainLastAgentSuggestOk = null;
  brainApplySuggestionId = null;
  brainUpdateSaveSuggestionControls(tid);
  brainUpdateApplyControls(tid);
  if (btn) btn.disabled = true;
  out.style.display = 'block';
  out.style.color = 'var(--text-muted)';
  out.textContent = 'Running Code Agent (review only; no auto-apply or validation)…';
  fetch('/api/planner/tasks/' + encodeURIComponent(String(tid)) + '/coding/agent-suggest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({})
  }).then(function(r) {
    return r.json().then(function(d) { return { httpOk: r.ok, d: d }; });
  }).then(function(x) {
    if (btn) btn.disabled = false;
    if (!x.httpOk || !x.d || !x.d.ok) {
      var msg = (x.d && x.d.message) ? x.d.message : (x.d && x.d.error) ? x.d.error : 'Request failed';
      brainLastAgentSuggestOk = null;
      brainApplySuggestionId = null;
      brainUpdateSaveSuggestionControls(tid);
      brainUpdateApplyControls(tid);
      out.textContent = 'Error: ' + msg;
      return;
    }
    var d = x.d;
    brainLastAgentSuggestOk = {
      taskId: Number(tid),
      payload: {
        response: d.response != null ? String(d.response) : '',
        model: d.model != null ? String(d.model) : '',
        diffs: Array.isArray(d.diffs) ? d.diffs.map(function(x) { return String(x); }) : [],
        files_changed: Array.isArray(d.files_changed) ? d.files_changed.map(function(x) { return String(x); }) : [],
        validation: Array.isArray(d.validation) ? d.validation : [],
        context_used: d.context_used && typeof d.context_used === 'object' ? d.context_used : {}
      }
    };
    brainUpdateSaveSuggestionControls(tid);
    brainUpdateApplyControls(tid);
    var parts = [];
    parts.push('model: ' + (d.model || ''));
    parts.push('files_changed: ' + JSON.stringify(d.files_changed || []));
    parts.push('--- response (review only) ---');
    parts.push(d.response || '');
    out.textContent = parts.join('\n\n');
    out.style.color = 'var(--text)';
    try { out.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch (e) { try { out.scrollIntoView(true); } catch (e2) {} }
  }).catch(function() {
    if (btn) btn.disabled = false;
    brainLastAgentSuggestOk = null;
    brainApplySuggestionId = null;
    brainUpdateSaveSuggestionControls(tid);
    brainUpdateApplyControls(tid);
    out.textContent = 'Network error.';
  });
}

function initBrainPlannerHandoffBridge() {
  var tid = brainHandoffPlannerTaskId();
  var pid = brainHandoffPlannerProjectId();
  var hintEl = document.getElementById('brain-planner-handoff-hint');
  var loadBtn = document.getElementById('brain-handoff-load-btn');
  var cj = document.getElementById('brain-handoff-copy-json-btn');
  var cm = document.getElementById('brain-handoff-copy-md-btn');
  var prefillBtn = document.getElementById('brain-handoff-prefill-code-search-btn');
  var agentSuggestBtn = document.getElementById('brain-handoff-agent-suggest-btn');
  var agentSuggestOut = document.getElementById('brain-handoff-agent-suggest-result');
  var plannerA = document.getElementById('brain-open-planner-link');
  if (!hintEl || !loadBtn) return;

  if (tid == null) {
    hintEl.textContent = 'No planner_task_id in URL. Open a task in the planner and use “Open in Brain”, or add ?planner_task_id=… to this page.';
    loadBtn.disabled = true;
    if (cj) cj.disabled = true;
    if (cm) cm.disabled = true;
    if (prefillBtn) { prefillBtn.disabled = true; prefillBtn.onclick = null; }
    if (agentSuggestBtn) { agentSuggestBtn.disabled = true; agentSuggestBtn.onclick = null; }
    if (agentSuggestOut) { agentSuggestOut.style.display = 'none'; agentSuggestOut.textContent = ''; }
    var saveSnap = document.getElementById('brain-handoff-save-suggestion-btn');
    if (saveSnap) { saveSnap.disabled = true; saveSnap.onclick = null; }
    var drySnap = document.getElementById('brain-handoff-dry-run-snapshot-btn');
    if (drySnap) { drySnap.disabled = true; drySnap.onclick = null; }
    var applySnap = document.getElementById('brain-handoff-apply-snapshot-btn');
    if (applySnap) { applySnap.disabled = true; applySnap.onclick = null; }
    var applyRes = document.getElementById('brain-handoff-apply-result');
    if (applyRes) { applyRes.style.display = 'none'; applyRes.textContent = ''; }
    var sugWrap = document.getElementById('brain-handoff-suggestions-wrap');
    var sugDet = document.getElementById('brain-handoff-suggestion-detail');
    if (sugWrap) sugWrap.style.display = 'none';
    if (sugDet) { sugDet.style.display = 'none'; sugDet.textContent = ''; }
    brainClearApplyAttemptsUi();
    brainClearValidationBridgeUi();
    brainLastAgentSuggestOk = null;
    brainApplySuggestionId = null;
    if (plannerA) plannerA.style.display = 'none';
    return;
  }
  var hintLine = 'Planner task id from link (hint only): ' + tid + '.';
  if (pid != null) hintLine += ' Project id: ' + pid + ' (for Open in Planner).';
  hintLine += ' Handoff is not loaded until you click Load handoff.';
  hintEl.textContent = hintLine;
  loadBtn.disabled = false;
  if (cj) cj.disabled = false;
  if (cm) cm.disabled = false;
  loadBtn.onclick = function() { brainLoadPlannerHandoff(tid); };
  if (cj) cj.onclick = function() { brainCopyHandoffJson(tid); };
  if (cm) cm.onclick = function() { brainCopyHandoffMarkdown(tid); };
  if (prefillBtn) {
    prefillBtn.onclick = function() { brainPrefillCodeSearchFromHandoff(); };
    brainUpdatePrefillBridgeButton();
  }
  if (agentSuggestBtn) {
    agentSuggestBtn.disabled = false;
    agentSuggestBtn.onclick = function() { brainRunAgentSuggestFromPlannerTask(tid); };
  }
  brainLastAgentSuggestOk = null;
  brainApplySuggestionId = null;
  brainUpdateSaveSuggestionControls(tid);
  brainUpdateApplyControls(tid);
  brainRefreshSuggestionList(tid);
  brainSetupValidationBridge(tid);
  if (plannerA && pid != null) {
    plannerA.href = '/planner?project_id=' + encodeURIComponent(String(pid)) + '&task_id=' + encodeURIComponent(String(tid));
    plannerA.style.display = 'inline';
  } else if (plannerA) {
    plannerA.style.display = 'none';
  }
}


function brainReadChiliHandoffLaunchParam() {
  try {
    return new URLSearchParams(window.location.search || '').get('chili_handoff_launch');
  } catch (e) {
    return null;
  }
}

/* Planner Parity v2: scroll, outline flash, URL cleanup only — no fetch or handoff automation */
function brainApplyHandoffLaunchParamOnce() {
  var step = brainReadChiliHandoffLaunchParam();
  if (!step) return;
  var map = { suggest: 'brain-handoff-agent-suggest-btn', implement: 'brain-handoff-save-suggestion-btn', validate: 'brain-handoff-validation-bridge-wrap' };
  if (!map[step]) return;
  var el = document.getElementById(map[step]);
  function stripLaunchFromUrl() {
    try {
      var u = new URL(window.location.href);
      if (!u.searchParams.has('chili_handoff_launch')) return;
      u.searchParams.delete('chili_handoff_launch');
      var q = u.searchParams.toString();
      history.replaceState({}, '', u.pathname + (q ? '?' + q : '') + (u.hash || ''));
    } catch (e2) {}
  }
  function run() {
    if (el) {
      try { el.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch (e0) { try { el.scrollIntoView(true); } catch (e1) {} }
      try {
        el.classList.add('chili-handoff-launch-flash');
        setTimeout(function() { try { el.classList.remove('chili-handoff-launch-flash'); } catch (e3) {} }, 1800);
      } catch (e4) {}
    }
    stripLaunchFromUrl();
  }
  setTimeout(run, 0);
}

window.brainHandoffPlannerProjectId = brainHandoffPlannerProjectId;
window.brainHandoffPlannerTaskId = brainHandoffPlannerTaskId;
window.brainHandoffJsonToMarkdown = brainHandoffJsonToMarkdown;
window.initBrainPlannerHandoffBridge = initBrainPlannerHandoffBridge;
window.brainApplyHandoffLaunchParamOnce = brainApplyHandoffLaunchParamOnce;
})();
