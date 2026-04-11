(function () {
window._agentList = window._agentList || [];
window._activeAgent = window._activeAgent || 'product_owner';
window._activeProjectAgent = window._activeProjectAgent || 'product_owner';
window._agentPanelLoaded = window._agentPanelLoaded || {};

function initProjectAgentBar() {
  fetch('/api/brain/project/agents').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    window._agentList = d.agents || [];
    var bar = document.getElementById('proj-agent-bar');
    if (!bar) return;
    bar.innerHTML = '';

    var allAgentDefs = [
      {name:'product_owner',label:'Product Owner',icon:'\uD83D\uDC51'},
      {name:'project_manager',label:'PM',icon:'\uD83D\uDCCB'},
      {name:'architect',label:'Architect',icon:'\uD83C\uDFD7'},
      {name:'backend',label:'Backend',icon:'\u2699'},
      {name:'frontend',label:'Frontend',icon:'\uD83C\uDFA8'},
      {name:'ux',label:'UX',icon:'\uD83D\uDC41'},
      {name:'qa',label:'QA',icon:'\uD83E\uDDEA'},
      {name:'devops',label:'DevOps',icon:'\uD83D\uDE80'},
      {name:'security',label:'Security',icon:'\uD83D\uDD12'},
      {name:'ai_eng',label:'AI Eng',icon:'\uD83E\uDD16'},
    ];

    var activeNames = {};
    window._agentList.forEach(function(a) { if(a.active) activeNames[a.agent || a.name] = a; });

    allAgentDefs.forEach(function(def) {
      var tab = document.createElement('div');
      var isRegistered = !!activeNames[def.name];
      tab.className = 'proj-agent-tab' + (def.name === window._activeAgent ? ' active' : '');
      tab.dataset.agent = def.name;
      tab.innerHTML = '<span class="agt-dot"></span> ' + def.icon + ' ' + def.label;
      tab.onclick = function() { switchProjectAgent(def.name); };
      bar.appendChild(tab);
    });

    if (activeNames[window._activeAgent]) {
      switchProjectAgent(window._activeAgent);
    }
  });
}

function switchProjectAgent(agentName) {
  window._activeAgent = agentName;
  window._activeProjectAgent = agentName;
  document.querySelectorAll('.proj-agent-tab').forEach(function(el) {
    el.classList.toggle('active', el.dataset.agent === agentName);
  });
  document.querySelectorAll('.proj-agent-panel').forEach(function(el) {
    el.classList.toggle('active', el.dataset.agent === agentName);
  });

  var specialAgents = ['product_owner', 'project_manager', 'architect'];
  if (specialAgents.indexOf(agentName) === -1 && !window._agentPanelLoaded[agentName]) {
    _ensureGenericPanel(agentName);
  }

  if (agentName === 'product_owner' && !window._agentPanelLoaded['product_owner']) {
    window._agentPanelLoaded['product_owner'] = true;
    loadPODashboard();
  }
  if (agentName === 'project_manager' && !window._agentPanelLoaded['project_manager']) {
    window._agentPanelLoaded['project_manager'] = true;
    loadPMDashboard();
  }
  if (agentName === 'architect' && !window._agentPanelLoaded['architect']) {
    window._agentPanelLoaded['architect'] = true;
    loadArchDashboard();
  }
}

/* ── Generic Agent Panel (dynamic, shared template) ─── */
function _ensureGenericPanel(agentName) {
  var containerId = 'agent-panel-' + agentName;
  if (document.getElementById(containerId)) {
    window._agentPanelLoaded[agentName] = true;
    _loadGenericDashboard(agentName);
    return;
  }
  var container = document.getElementById('proj-agent-panels');
  if (!container) return;
  var panel = document.createElement('div');
  panel.className = 'proj-agent-panel active';
  panel.id = containerId;
  panel.dataset.agent = agentName;
  panel.innerHTML =
    '<div class="agent-status-card" id="' + agentName + '-status-card"></div>' +
    '<div class="brain-section">' +
      '<div class="brain-section-title"><span class="bs-icon">&#x1F4A1;</span> Findings & Recommendations</div>' +
      '<div id="' + agentName + '-findings" class="project-scroll-panel"><div class="project-inline-muted">Run a cycle to generate findings...</div></div>' +
    '</div>' +
    '<div class="brain-section">' +
      '<div class="brain-section-title"><span class="bs-icon">&#x1F50D;</span> Research</div>' +
      '<div id="' + agentName + '-research" class="project-scroll-panel"><div class="project-inline-muted">Research results will appear after a cycle...</div></div>' +
    '</div>' +
    '<div class="brain-section">' +
      '<div class="brain-section-title"><span class="bs-icon">&#x1F3AF;</span> Goals</div>' +
      '<div id="' + agentName + '-goals" class="project-scroll-panel short"><div class="project-inline-muted">Goals will appear...</div></div>' +
    '</div>' +
    '<div class="brain-section">' +
      '<div class="brain-section-title"><span class="bs-icon">&#x1F4C8;</span> Evolution Timeline</div>' +
      '<div id="' + agentName + '-evolution" class="project-scroll-panel medium"><div class="project-inline-muted">Evolution events will appear as the agent learns...</div></div>' +
    '</div>' +
    _getAgentSpecificSections(agentName) +
    '<button onclick="triggerAgentCycle(\'' + agentName + '\')" class="brain-act-btn project-mini-btn accent" style="margin-top:10px">&#x1F504; Run ' + agentName.replace('_', ' ') + ' Cycle</button>';
  container.appendChild(panel);
  window._agentPanelLoaded[agentName] = true;
  _loadGenericDashboard(agentName);
}

function _getAgentSpecificSections(agentName) {
  if (agentName === 'qa') {
    return '<div class="brain-section">' +
      '<div class="brain-section-title"><span class="bs-icon">&#x1F9EA;</span> Test Cases</div>' +
      '<div id="qa-test-cases" class="project-scroll-panel medium"><div class="project-inline-muted">Test cases will appear after QA cycle...</div></div>' +
    '</div>' +
    '<div class="brain-section">' +
      '<div class="brain-section-title"><span class="bs-icon">&#x1F41E;</span> Bug Reports</div>' +
      '<div id="qa-bug-reports" class="project-scroll-panel medium"><div class="project-inline-muted">Bug reports will appear after QA cycle...</div></div>' +
    '</div>' +
    '<div class="brain-section">' +
      '<div class="brain-section-title"><span class="bs-icon">&#x1F50D;</span> Code Reviews</div>' +
      '<div id="code-reviews" class="project-scroll-panel" style="max-height:400px;"><div class="project-inline-muted">Reviews of recent commits will appear after learning.</div></div>' +
    '</div>';
  }
  if (agentName === 'security') {
    return '<div class="brain-section">' +
      '<div class="brain-section-title"><span class="bs-icon">&#x26A0;</span> Risk Score</div>' +
      '<div id="security-risk" class="project-inline-muted">Security risk score will appear after a cycle...</div>' +
    '</div>' +
    '<div class="brain-section">' +
      '<div class="brain-section-title"><span class="bs-icon">&#x1F4E6;</span> Dependency Health</div>' +
      '<div id="sec-code-deps" class="project-scroll-panel"><div class="project-inline-muted">Dependency alerts will appear after learning.</div></div>' +
    '</div>';
  }
  if (agentName === 'devops') {
    return '<div class="brain-section">' +
      '<div class="brain-section-title"><span class="bs-icon">&#x1F4E6;</span> Dependency Health</div>' +
      '<div id="code-deps" class="project-scroll-panel"><div class="project-inline-muted">Dependency alerts will appear after learning.</div></div>' +
    '</div>' +
    '<div class="brain-section">' +
      '<div class="brain-section-title"><span class="bs-icon">&#x1F525;</span> Hotspots</div>' +
      '<div id="code-hotspots" class="project-scroll-panel"><div class="project-inline-muted">Loading hotspots...</div></div>' +
    '</div>' +
    '<div class="brain-section">' +
      '<div class="brain-section-title"><span class="bs-icon">&#x1F4CB;</span> Activity Timeline</div>' +
      '<div class="brain-timeline" id="code-activity"><div class="project-inline-muted">No activity yet. Add a repo and run indexing.</div></div>' +
    '</div>';
  }
  return '';
}

function _loadGenericDashboard(agentName) {
  _loadGenericMetrics(agentName);
  _loadGenericFindings(agentName);
  _loadGenericResearch(agentName);
  _loadGenericGoals(agentName);
  _loadGenericEvolution(agentName);
  if (agentName === 'qa') { _loadQATestCases(); _loadQABugReports(); loadCodeReviews(); }
  if (agentName === 'security') { _loadSecurityRisk(agentName); loadCodeDeps('sec-code-deps'); }
  if (agentName === 'devops') { loadCodeDeps(); loadCodeHotspots(); }
}

function _loadGenericMetrics(agentName) {
  fetch('/api/brain/project/agent/' + agentName + '/metrics').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById(agentName + '-status-card');
    if (!el) return;
    var kpis = [
      {val: ((d.confidence || 0) * 100).toFixed(0) + '%', lbl: 'Confidence'},
      {val: d.finding_count || 0, lbl: 'Findings'},
      {val: d.research_count || 0, lbl: 'Research'},
      {val: d.active_goals || 0, lbl: 'Goals'},
      {val: d.evolution_count || 0, lbl: 'Evolutions'},
      {val: d.unread_messages || 0, lbl: 'Inbox'},
    ];
    if (d.total_test_cases !== undefined) kpis.push({val: d.total_test_cases, lbl: 'Test Cases'});
    if (d.open_bugs !== undefined) kpis.push({val: d.open_bugs, lbl: 'Open Bugs'});
    el.innerHTML = kpis.map(function(k) {
      return '<div class="agent-stat"><div class="agent-stat-val">' + k.val + '</div><div class="agent-stat-lbl">' + k.lbl + '</div></div>';
    }).join('');
  });
}

function _loadGenericFindings(agentName) {
  fetch('/api/brain/project/agent/' + agentName + '/findings').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById(agentName + '-findings');
    if (!el) return;
    var items = d.findings || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No findings yet.</div>'; return; }
    el.innerHTML = items.map(function(f) {
      return '<div class="agt-finding-row">' +
        '<span class="agt-finding-sev ' + f.severity + '">' + f.severity + '</span>' +
        '<div style="flex:1"><strong>' + f.title + '</strong><div style="color:var(--text-muted);margin-top:2px">' + f.description + '</div></div>' +
        '<span style="font-size:9px;color:var(--text-muted);white-space:nowrap">' + f.category + '</span>' +
      '</div>';
    }).join('');
  });
}

function _loadGenericResearch(agentName) {
  fetch('/api/brain/project/agent/' + agentName + '/research').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById(agentName + '-research');
    if (!el) return;
    var items = d.research || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No research yet.</div>'; return; }
    el.innerHTML = items.map(function(r) {
      var sources = '';
      try {
        var s = JSON.parse(r.sources_json || '[]');
        if (s.length) {
          sources = '<div class="agt-research-sources">' + s.map(function(src) {
            return '<a href="' + (src.url || '#') + '" target="_blank">' + (src.title || src.url || 'source') + '</a>';
          }).join(' &middot; ') + '</div>';
        }
      } catch(e) {}
      return '<div class="agt-research-card">' +
        '<div class="agt-research-topic">' + r.topic + '</div>' +
        '<div class="agt-research-summary">' + r.summary + '</div>' +
        sources +
      '</div>';
    }).join('');
  });
}

function _loadGenericGoals(agentName) {
  fetch('/api/brain/project/agent/' + agentName + '/goals').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById(agentName + '-goals');
    if (!el) return;
    var items = d.goals || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No goals yet.</div>'; return; }
    el.innerHTML = items.map(function(g) {
      var statusColor = g.status === 'active' ? '#22c55e' : g.status === 'completed' ? '#a78bfa' : 'var(--text-muted)';
      var pct = Math.round(g.progress * 100);
      return '<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:11px">' +
        '<span style="color:' + statusColor + ';font-weight:600;text-transform:uppercase;font-size:9px;min-width:55px">' + g.status + '</span>' +
        '<span style="flex:1">' + g.description + '</span>' +
        '<span style="font-size:9px;color:var(--text-muted)">' + pct + '%</span>' +
        '<div style="width:50px;height:4px;background:var(--border);border-radius:2px;overflow:hidden"><div style="height:100%;width:' + pct + '%;background:#a78bfa;border-radius:2px"></div></div>' +
      '</div>';
    }).join('');
  });
}

function _loadGenericEvolution(agentName) {
  fetch('/api/brain/project/agent/' + agentName + '/evolution').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById(agentName + '-evolution');
    if (!el) return;
    var items = d.evolution || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No evolution events yet.</div>'; return; }
    el.innerHTML = items.map(function(e) {
      var before = (e.confidence_before * 100).toFixed(0);
      var after = (e.confidence_after * 100).toFixed(0);
      var delta = after - before;
      var arrow = delta >= 0 ? '\u2191' : '\u2193';
      var color = delta >= 0 ? '#22c55e' : '#ef4444';
      return '<div class="agt-evo-row">' +
        '<span class="agt-evo-dim">' + e.dimension + '</span>' +
        '<div class="agt-evo-conf">' + before + '%<span style="color:' + color + ';font-weight:700">' + arrow + '</span>' + after + '%</div>' +
        '<span style="flex:1;color:var(--text-muted)">' + e.description.substring(0, 100) + '</span>' +
        '<span style="font-size:9px;color:var(--text-muted)">' + (e.created_at ? timeSince(new Date(e.created_at)) + ' ago' : '') + '</span>' +
      '</div>';
    }).join('');
  });
}

function _loadQATestCases() {
  fetch('/api/brain/project/agent/qa/test-cases').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('qa-test-cases');
    if (!el) return;
    var cases = d.test_cases || [];
    if (!cases.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No test cases yet. Run a QA cycle.</div>'; return; }
    el.innerHTML = cases.map(function(c) {
      return '<div class="qa-test-row">' +
        '<span class="qa-test-status ' + (c.status === 'active' ? 'pass' : 'fail') + '"></span>' +
        '<span style="flex:1;font-weight:600">' + c.name + '</span>' +
        '<span style="font-size:9px;color:var(--text-muted)">' + c.priority + '</span>' +
      '</div>';
    }).join('');
  });
}

function _loadQABugReports() {
  fetch('/api/brain/project/agent/qa/bug-reports').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('qa-bug-reports');
    if (!el) return;
    var bugs = d.bug_reports || [];
    if (!bugs.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No bugs detected yet.</div>'; return; }
    el.innerHTML = bugs.map(function(b) {
      return '<div class="qa-bug-card">' +
        '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">' +
          '<span class="qa-bug-sev ' + b.severity + '">' + b.severity + '</span>' +
          '<span class="qa-bug-title">' + b.title + '</span>' +
          '<span style="margin-left:auto;font-size:9px;padding:2px 6px;border-radius:6px;background:rgba(139,92,246,.1);color:#a78bfa">' + b.status + '</span>' +
        '</div>' +
        (b.description ? '<div style="font-size:10px;color:var(--text-muted)">' + b.description.substring(0, 150) + '</div>' : '') +
      '</div>';
    }).join('');
  });
}

function _loadSecurityRisk(agentName) {
  fetch('/api/brain/project/agent/' + agentName + '/metrics').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('security-risk');
    if (!el) return;
    var state = {};
    try { state = JSON.parse(d.state_json || '{}'); } catch(e) {}
    var risk = state.risk_score || 0;
    var level = risk < 3 ? 'low' : risk < 6 ? 'medium' : 'high';
    el.innerHTML = '<div style="display:flex;align-items:center;gap:12px">' +
      '<span class="sec-risk-badge ' + level + '">' + risk.toFixed(1) + ' / 10</span>' +
      '<span style="font-size:11px;color:var(--text-muted)">Overall Security Risk Score</span>' +
    '</div>';
  });
}

/* ── Agent message feed ───────────────────────── */
function loadPODashboard() {
  loadPOMetrics();
  loadPOQuestions();
  loadPORequirements();
  loadPOResearch();
  loadPOFindings();
  loadPOGoals();
  loadPOEvolution();
}

function loadPOMetrics() {
  fetch('/api/brain/project/agent/product_owner/metrics').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('po-status-card');
    if (!el) return;
    var kpis = [
      {val: (d.confidence * 100).toFixed(0) + '%', lbl: 'Confidence'},
      {val: d.pending_questions || 0, lbl: 'Pending Qs'},
      {val: d.answered_questions || 0, lbl: 'Answered'},
      {val: d.total_requirements || 0, lbl: 'Requirements'},
      {val: d.finding_count || 0, lbl: 'Findings'},
      {val: d.research_count || 0, lbl: 'Research'},
      {val: d.evolution_count || 0, lbl: 'Evolutions'},
    ];
    el.innerHTML = kpis.map(function(k) {
      return '<div class="agent-stat"><div class="agent-stat-val">' + k.val + '</div><div class="agent-stat-lbl">' + k.lbl + '</div></div>';
    }).join('');
  });
}

var _poSelectedOptions = {};

function loadPOQuestions() {
  fetch('/api/brain/project/agent/product_owner/questions?status=pending').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('po-questions');
    if (!el) return;
    var qs = d.questions || [];
    if (!qs.length) {
      el.innerHTML = '<div style="font-size:12px;color:var(--text-muted);padding:8px 0">No pending decisions. Run a PO cycle to generate new ones.</div>' +
        '<button onclick="triggerAgentCycle(\'product_owner\')" class="brain-act-btn" style="margin-top:6px;background:linear-gradient(135deg,#8b5cf6,#6d28d9)">&#x1F451; Run PO Cycle</button>';
      return;
    }

    var hasOptionless = qs.some(function(q) { return !q.options || !q.options.length; });
    _poSelectedOptions = {};
    var html = '';

    if (hasOptionless) {
      html += '<div class="po-q-refresh-banner">' +
        '<div style="font-size:12px;color:var(--text-muted)">Some questions need smart options generated from your repo.</div>' +
        '<button class="po-q-btn submit" onclick="refreshPOOptions()" id="btn-po-refresh">Generate Smart Options</button>' +
      '</div>';
    }

    html += qs.map(function(q) {
      var opts = q.options || [];
      var card = '<div class="po-q-card" id="po-card-' + q.id + '">';
      card += '<div class="po-q-header"><span class="po-q-badge pending">' + q.category + '</span></div>';
      card += '<div class="po-q-text">' + escHtml(q.question) + '</div>';
      if (q.context) card += '<div class="po-q-ctx">' + escHtml(q.context) + '</div>';

      if (opts.length) {
        card += '<div class="po-option-grid" id="po-opts-' + q.id + '">';
        opts.forEach(function(opt, idx) {
          card += '<div class="po-option-card" data-qid="' + q.id + '" data-idx="' + idx + '" onclick="selectPOOption(' + q.id + ',' + idx + ')">' +
            '<div class="po-option-radio"></div>' +
            '<div class="po-option-label">' + escHtml(opt) + '</div>' +
          '</div>';
        });
        card += '</div>';
        card += '<div class="po-q-custom-toggle" onclick="togglePOCustom(' + q.id + ')">&#x270F; Write a different answer</div>';
        card += '<div class="po-q-custom-row" id="po-custom-row-' + q.id + '" style="display:none">' +
          '<input type="text" class="po-q-input" id="po-custom-' + q.id + '" placeholder="Your own answer...">' +
        '</div>';
      } else {
        card += '<div class="po-q-custom-row" style="margin-top:8px">' +
          '<input type="text" class="po-q-input" id="po-custom-' + q.id + '" placeholder="Type your answer...">' +
        '</div>';
      }

      card += '<div class="po-q-action-row">' +
        '<button class="po-q-btn submit" onclick="submitPOAnswer(' + q.id + ')">Confirm</button>' +
        '<button class="po-q-btn skip" onclick="skipPOQuestion(' + q.id + ')">Skip</button>' +
      '</div>';
      card += '</div>';
      return card;
    }).join('');

    html += '<button onclick="triggerAgentCycle(\'product_owner\')" class="brain-act-btn" style="margin-top:10px;background:linear-gradient(135deg,#8b5cf6,#6d28d9)">&#x1F451; Run PO Cycle</button>';
    el.innerHTML = html;
  });

  _loadPOAnswered();
}

function _loadPOAnswered() {
  fetch('/api/brain/project/agent/product_owner/questions?status=answered').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var qs = d.questions || [];
    if (!qs.length) return;
    var el = document.getElementById('po-questions');
    if (!el) return;
    var html = '<div style="margin-top:16px;border-top:1px solid var(--border);padding-top:12px">' +
      '<div style="font-size:10px;font-weight:700;text-transform:uppercase;color:var(--text-muted);letter-spacing:.5px;margin-bottom:8px">Previous Decisions (' + qs.length + ')</div>';
    qs.slice(0, 5).forEach(function(q) {
      html += '<div class="po-q-card" style="padding:12px 16px;margin-bottom:8px;opacity:.7">' +
        '<div class="po-q-header"><span class="po-q-badge answered">answered</span><span class="po-q-badge" style="background:rgba(139,92,246,.08);color:#a78bfa">' + q.category + '</span></div>' +
        '<div style="font-size:12px;font-weight:600;margin-bottom:4px">' + escHtml(q.question) + '</div>' +
        '<div class="po-q-answered"><strong>Decision: </strong>' + escHtml(q.answer) + '</div>' +
      '</div>';
    });
    html += '</div>';
    el.innerHTML += html;
  });
}

function refreshPOOptions() {
  var btn = document.getElementById('btn-po-refresh');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating...'; }
  fetch('/api/brain/project/agent/product_owner/refresh-options', {method:'POST'})
  .then(function(r){return r.json();}).then(function(d) {
    if (d.ok) loadPOQuestions();
    else if (btn) { btn.disabled = false; btn.textContent = 'Generate Smart Options'; }
  }).catch(function() {
    if (btn) { btn.disabled = false; btn.textContent = 'Generate Smart Options'; }
  });
}

function selectPOOption(qid, idx) {
  var grid = document.getElementById('po-opts-' + qid);
  if (!grid) return;
  grid.querySelectorAll('.po-option-card').forEach(function(c) {
    c.classList.toggle('selected', parseInt(c.dataset.idx) === idx);
  });
  _poSelectedOptions[qid] = idx;
  var customRow = document.getElementById('po-custom-row-' + qid);
  if (customRow) customRow.style.display = 'none';
}

function togglePOCustom(qid) {
  var customRow = document.getElementById('po-custom-row-' + qid);
  if (!customRow) return;
  var isVisible = customRow.style.display !== 'none';
  customRow.style.display = isVisible ? 'none' : 'flex';
  if (!isVisible) {
    delete _poSelectedOptions[qid];
    var grid = document.getElementById('po-opts-' + qid);
    if (grid) grid.querySelectorAll('.po-option-card').forEach(function(c) { c.classList.remove('selected'); });
    var input = document.getElementById('po-custom-' + qid);
    if (input) input.focus();
  }
}

function submitPOAnswer(questionId) {
  var answer = '';
  if (typeof _poSelectedOptions[questionId] !== 'undefined') {
    var grid = document.getElementById('po-opts-' + questionId);
    if (grid) {
      var sel = grid.querySelector('.po-option-card.selected .po-option-label');
      if (sel) answer = sel.textContent.trim();
    }
  }
  if (!answer) {
    var input = document.getElementById('po-custom-' + questionId);
    if (input) answer = input.value.trim();
  }
  if (!answer) return;

  var card = document.getElementById('po-card-' + questionId);
  if (card) card.style.opacity = '0.5';

  fetch('/api/brain/project/agent/product_owner/question/' + questionId + '/answer', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({answer: answer})
  }).then(function(r){return r.json();}).then(function(d) {
    if (d.ok) {
      delete _poSelectedOptions[questionId];
      loadPOQuestions();
      loadPOMetrics();
    } else if (card) { card.style.opacity = '1'; }
  });
}

function skipPOQuestion(questionId) {
  var card = document.getElementById('po-card-' + questionId);
  if (card) card.style.opacity = '0.5';
  fetch('/api/brain/project/agent/product_owner/question/' + questionId + '/skip', {method: 'POST'})
  .then(function(r){return r.json();}).then(function(d) {
    if (d.ok) loadPOQuestions();
    else if (card) card.style.opacity = '1';
  });
}

function loadPORequirements() {
  fetch('/api/brain/project/agent/product_owner/requirements').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('po-requirements');
    if (!el) return;
    var reqs = d.requirements || [];
    if (!reqs.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No requirements yet.</div>'; return; }
    el.innerHTML = reqs.map(function(r) {
      return '<div class="po-req-row">' +
        '<span class="po-req-pri ' + r.priority + '">' + r.priority + '</span>' +
        '<span class="po-req-title">' + r.title + '</span>' +
        '<span class="po-req-status">' + r.status + '</span>' +
        (r.status !== 'in_planner' && r.status !== 'done' ?
          '<button class="po-req-push" data-push-req="' + r.id + '" onclick="pushReqToPlanner(' + r.id + ')">&#x1F4CB; To Planner</button>' : '') +
      '</div>';
    }).join('');
  });
}

function pushReqToPlanner(reqId) {
  fetch('/api/brain/project/planner-projects')
  .then(function(r){return r.json();}).then(function(d) {
    var projects = (d.projects || []).filter(function(p){ return p.status !== 'archived'; });
    if (!projects.length) {
      alert('No planner projects found. Create one on the Planner page first.');
      return;
    }
    if (projects.length === 1) {
      _doPushReq(reqId, projects[0].id);
      return;
    }
    var existing = document.getElementById('po-project-picker-' + reqId);
    if (existing) { existing.remove(); return; }
    var btn = document.querySelector('[data-push-req="' + reqId + '"]');
    if (!btn) { _doPushReq(reqId, projects[0].id); return; }
    var picker = document.createElement('div');
    picker.id = 'po-project-picker-' + reqId;
    picker.className = 'po-project-picker';
    picker.innerHTML = '<div class="po-picker-title">Select project:</div>' +
      projects.map(function(p) {
        return '<button class="po-picker-opt" onclick="event.stopPropagation();_doPushReq(' + reqId + ',' + p.id + ')">' +
          '<span class="po-picker-dot" style="background:' + (p.color || '#6366f1') + '"></span>' +
          p.name + '</button>';
      }).join('');
    btn.parentElement.appendChild(picker);
    setTimeout(function(){ document.addEventListener('click', function _close(e) {
      if (!picker.contains(e.target)) { picker.remove(); document.removeEventListener('click', _close); }
    }); }, 0);
  });
}

function _doPushReq(reqId, projectId) {
  var picker = document.getElementById('po-project-picker-' + reqId);
  if (picker) picker.remove();
  fetch('/api/brain/project/agent/product_owner/requirement/' + reqId + '/to-task', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project_id: projectId})
  })
  .then(function(r){return r.json();}).then(function(d) {
    if (d.ok) { loadPORequirements(); }
    else { alert(d.error || d.message || 'Could not push to planner.'); }
  });
}

function loadPOResearch() {
  fetch('/api/brain/project/agent/product_owner/research').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('po-research');
    if (!el) return;
    var items = d.research || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No research yet.</div>'; return; }
    el.innerHTML = items.map(function(r) {
      var sources = '';
      try {
        var s = JSON.parse(r.sources_json || '[]');
        if (s.length) {
          sources = '<div class="agt-research-sources">' + s.map(function(src) {
            return '<a href="' + (src.url || '#') + '" target="_blank">' + (src.title || src.url || 'source') + '</a>';
          }).join(' &middot; ') + '</div>';
        }
      } catch(e) {}
      return '<div class="agt-research-card">' +
        '<div class="agt-research-topic">' + r.topic + '</div>' +
        '<div class="agt-research-summary">' + r.summary + '</div>' +
        sources +
      '</div>';
    }).join('');
  });
}

function loadPOFindings() {
  fetch('/api/brain/project/agent/product_owner/findings').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('po-findings');
    if (!el) return;
    var items = d.findings || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No findings yet.</div>'; return; }
    el.innerHTML = items.map(function(f) {
      return '<div class="agt-finding-row">' +
        '<span class="agt-finding-sev ' + f.severity + '">' + f.severity + '</span>' +
        '<div style="flex:1"><strong>' + f.title + '</strong><div style="color:var(--text-muted);margin-top:2px">' + f.description + '</div></div>' +
        '<span style="font-size:9px;color:var(--text-muted);white-space:nowrap">' + f.category + '</span>' +
      '</div>';
    }).join('');
  });
}

function loadPOGoals() {
  fetch('/api/brain/project/agent/product_owner/goals').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('po-goals');
    if (!el) return;
    var items = d.goals || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No goals yet.</div>'; return; }
    el.innerHTML = items.map(function(g) {
      var statusColor = g.status === 'active' ? '#22c55e' : g.status === 'completed' ? '#a78bfa' : 'var(--text-muted)';
      var pct = Math.round(g.progress * 100);
      return '<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:11px">' +
        '<span style="color:' + statusColor + ';font-weight:600;text-transform:uppercase;font-size:9px;min-width:55px">' + g.status + '</span>' +
        '<span style="flex:1">' + g.description + '</span>' +
        '<span style="font-size:9px;color:var(--text-muted)">' + pct + '%</span>' +
        '<div style="width:50px;height:4px;background:var(--border);border-radius:2px;overflow:hidden"><div style="height:100%;width:' + pct + '%;background:#a78bfa;border-radius:2px"></div></div>' +
      '</div>';
    }).join('');
  });
}

function loadPOEvolution() {
  fetch('/api/brain/project/agent/product_owner/evolution').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('po-evolution');
    if (!el) return;
    var items = d.evolution || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No evolution events yet.</div>'; return; }
    el.innerHTML = items.map(function(e) {
      var before = (e.confidence_before * 100).toFixed(0);
      var after = (e.confidence_after * 100).toFixed(0);
      var delta = after - before;
      var arrow = delta >= 0 ? '\u2191' : '\u2193';
      var color = delta >= 0 ? '#22c55e' : '#ef4444';
      return '<div class="agt-evo-row">' +
        '<span class="agt-evo-dim">' + e.dimension + '</span>' +
        '<div class="agt-evo-conf">' + before + '%<span style="color:' + color + ';font-weight:700">' + arrow + '</span>' + after + '%</div>' +
        '<span style="flex:1;color:var(--text-muted)">' + e.description.substring(0, 100) + '</span>' +
        '<span style="font-size:9px;color:var(--text-muted)">' + (e.created_at ? timeSince(new Date(e.created_at)) + ' ago' : '') + '</span>' +
      '</div>';
    }).join('');
  });
}

/* ── Project Manager Panel ────────────────────── */

function loadPMDashboard() {
  loadPMMetrics();
  loadPMVelocity();
  loadPMBreakdown();
  loadPMFindings();
  loadPMResearch();
  loadPMGoals();
  loadPMEvolution();
}

function loadPMMetrics() {
  fetch('/api/brain/project/agent/project_manager/metrics').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('pm-status-card');
    if (!el) return;
    var healthColor = d.health === 'healthy' ? '#22c55e' : d.health === 'at_risk' ? '#f59e0b' : d.health === 'critical' ? '#ef4444' : 'var(--text-muted)';
    var kpis = [
      {val: (d.confidence * 100).toFixed(0) + '%', lbl: 'Confidence'},
      {val: '<span style="color:' + healthColor + '">' + (d.health || 'unknown') + '</span>', lbl: 'Health'},
      {val: d.done_tasks || 0, lbl: 'Done'},
      {val: d.in_progress_tasks || 0, lbl: 'In Progress'},
      {val: d.blocked_tasks || 0, lbl: 'Blocked'},
      {val: d.overdue_tasks || 0, lbl: 'Overdue'},
      {val: (d.completion_pct || 0) + '%', lbl: 'Completion'},
      {val: d.finding_count || 0, lbl: 'Findings'},
    ];
    el.innerHTML = kpis.map(function(k) {
      return '<div class="agent-stat"><div class="agent-stat-val">' + k.val + '</div><div class="agent-stat-lbl">' + k.lbl + '</div></div>';
    }).join('');
  });
}

function loadPMVelocity() {
  fetch('/api/brain/project/agent/project_manager/velocity').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('pm-velocity');
    if (!el) return;
    var healthColor = d.health === 'healthy' ? '#22c55e' : d.health === 'at_risk' ? '#f59e0b' : '#ef4444';
    var pct = d.completion_pct || 0;
    el.innerHTML =
      '<div style="display:flex;align-items:center;gap:14px;margin-bottom:10px">' +
        '<div style="flex:1">' +
          '<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">Overall Completion</div>' +
          '<div style="height:10px;background:var(--border);border-radius:5px;overflow:hidden">' +
            '<div style="height:100%;width:' + pct + '%;background:' + healthColor + ';border-radius:5px;transition:width .5s"></div>' +
          '</div>' +
          '<div style="font-size:10px;color:var(--text-muted);margin-top:2px">' + pct + '% complete</div>' +
        '</div>' +
        '<div style="text-align:center;padding:8px 16px;background:var(--bg-header);border:1px solid var(--border);border-radius:8px">' +
          '<div style="font-size:20px;font-weight:700;color:' + healthColor + '">' + (d.health || 'N/A').toUpperCase() + '</div>' +
          '<div style="font-size:9px;color:var(--text-muted)">Project Health</div>' +
        '</div>' +
      '</div>' +
      '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px">' +
        '<div class="agent-stat"><div class="agent-stat-val" style="color:#22c55e">' + (d.done || 0) + '</div><div class="agent-stat-lbl">Done</div></div>' +
        '<div class="agent-stat"><div class="agent-stat-val" style="color:#06b6d4">' + (d.in_progress || 0) + '</div><div class="agent-stat-lbl">In Progress</div></div>' +
        '<div class="agent-stat"><div class="agent-stat-val" style="color:#f59e0b">' + (d.blocked || 0) + '</div><div class="agent-stat-lbl">Blocked</div></div>' +
        '<div class="agent-stat"><div class="agent-stat-val" style="color:#ef4444">' + (d.overdue || 0) + '</div><div class="agent-stat-lbl">Overdue</div></div>' +
      '</div>';
  });
}

function loadPMBreakdown() {
  fetch('/api/brain/project/agent/project_manager/breakdown').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('pm-breakdown');
    if (!el) return;
    var projects = d.projects || [];
    if (!projects.length) {
      el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No projects in Planner yet. Run a PM cycle to auto-create one.</div>' +
        '<button onclick="triggerAgentCycle(\'project_manager\')" class="brain-act-btn" style="margin-top:8px;background:linear-gradient(135deg,#06b6d4,#0891b2)">\uD83D\uDCCB Run PM Cycle</button>';
      return;
    }
    el.innerHTML = projects.map(function(p) {
      var statusBars = '';
      var statuses = ['done', 'in_progress', 'todo', 'blocked'];
      var statusColors = {done: '#22c55e', in_progress: '#06b6d4', todo: 'var(--text-muted)', blocked: '#f59e0b'};
      var total = p.total || 1;
      statuses.forEach(function(s) {
        var count = (p.by_status || {})[s] || 0;
        if (count > 0) {
          var pct = (count / total * 100).toFixed(1);
          statusBars += '<div style="display:flex;align-items:center;gap:6px;font-size:10px;margin-top:2px">' +
            '<span style="width:65px;color:var(--text-muted)">' + s.replace('_', ' ') + '</span>' +
            '<div style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden">' +
              '<div style="height:100%;width:' + pct + '%;background:' + (statusColors[s] || 'var(--text-muted)') + ';border-radius:3px"></div>' +
            '</div>' +
            '<span style="min-width:30px;text-align:right;color:var(--text-muted)">' + count + '</span>' +
          '</div>';
        }
      });
      var priBadges = '';
      var priorities = ['critical', 'high', 'medium', 'low'];
      var priColors = {critical: '#ef4444', high: '#f59e0b', medium: '#22c55e', low: '#94a3b8'};
      priorities.forEach(function(pr) {
        var count = (p.by_priority || {})[pr] || 0;
        if (count > 0) {
          priBadges += '<span style="font-size:9px;padding:2px 6px;border-radius:8px;background:rgba(0,0,0,.1);color:' + (priColors[pr] || 'inherit') + ';font-weight:600">' + pr + ': ' + count + '</span> ';
        }
      });
      return '<div style="background:var(--bg-header);border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:8px">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">' +
          '<strong style="font-size:12px">' + p.project_name + '</strong>' +
          '<span style="font-size:10px;color:var(--text-muted)">' + p.total + ' tasks</span>' +
        '</div>' +
        statusBars +
        '<div style="margin-top:6px">' + priBadges + '</div>' +
      '</div>';
    }).join('');
    el.innerHTML += '<button onclick="triggerAgentCycle(\'project_manager\')" class="brain-act-btn" style="margin-top:8px;background:linear-gradient(135deg,#06b6d4,#0891b2)">\uD83D\uDCCB Run PM Cycle</button>';
  });
}

function loadPMFindings() {
  fetch('/api/brain/project/agent/project_manager/findings').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('pm-findings');
    if (!el) return;
    var items = d.findings || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No findings yet.</div>'; return; }
    el.innerHTML = items.map(function(f) {
      return '<div class="agt-finding-row">' +
        '<span class="agt-finding-sev ' + f.severity + '">' + f.severity + '</span>' +
        '<div style="flex:1"><strong>' + f.title + '</strong><div style="color:var(--text-muted);margin-top:2px">' + f.description + '</div></div>' +
        '<span style="font-size:9px;color:var(--text-muted);white-space:nowrap">' + f.category + '</span>' +
      '</div>';
    }).join('');
  });
}

function loadPMResearch() {
  fetch('/api/brain/project/agent/project_manager/research').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('pm-research');
    if (!el) return;
    var items = d.research || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No research yet.</div>'; return; }
    el.innerHTML = items.map(function(r) {
      var sources = '';
      try {
        var s = JSON.parse(r.sources_json || '[]');
        if (s.length) {
          sources = '<div class="agt-research-sources">' + s.map(function(src) {
            return '<a href="' + (src.url || '#') + '" target="_blank">' + (src.title || src.url || 'source') + '</a>';
          }).join(' &middot; ') + '</div>';
        }
      } catch(e) {}
      return '<div class="agt-research-card">' +
        '<div class="agt-research-topic">' + r.topic + '</div>' +
        '<div class="agt-research-summary">' + r.summary + '</div>' +
        sources +
      '</div>';
    }).join('');
  });
}

function loadPMGoals() {
  fetch('/api/brain/project/agent/project_manager/goals').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('pm-goals');
    if (!el) return;
    var items = d.goals || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No goals yet.</div>'; return; }
    el.innerHTML = items.map(function(g) {
      var statusColor = g.status === 'active' ? '#22c55e' : g.status === 'completed' ? '#a78bfa' : 'var(--text-muted)';
      var pct = Math.round(g.progress * 100);
      return '<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:11px">' +
        '<span style="color:' + statusColor + ';font-weight:600;text-transform:uppercase;font-size:9px;min-width:55px">' + g.status + '</span>' +
        '<span style="flex:1">' + g.description + '</span>' +
        '<span style="font-size:9px;color:var(--text-muted)">' + pct + '%</span>' +
        '<div style="width:50px;height:4px;background:var(--border);border-radius:2px;overflow:hidden"><div style="height:100%;width:' + pct + '%;background:#06b6d4;border-radius:2px"></div></div>' +
      '</div>';
    }).join('');
  });
}

function loadPMEvolution() {
  fetch('/api/brain/project/agent/project_manager/evolution').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('pm-evolution');
    if (!el) return;
    var items = d.evolution || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No evolution events yet.</div>'; return; }
    el.innerHTML = items.map(function(e) {
      var before = (e.confidence_before * 100).toFixed(0);
      var after = (e.confidence_after * 100).toFixed(0);
      var delta = after - before;
      var arrow = delta >= 0 ? '\u2191' : '\u2193';
      var color = delta >= 0 ? '#22c55e' : '#ef4444';
      return '<div class="agt-evo-row">' +
        '<span class="agt-evo-dim">' + e.dimension + '</span>' +
        '<div class="agt-evo-conf">' + before + '%<span style="color:' + color + ';font-weight:700">' + arrow + '</span>' + after + '%</div>' +
        '<span style="flex:1;color:var(--text-muted)">' + e.description.substring(0, 100) + '</span>' +
        '<span style="font-size:9px;color:var(--text-muted)">' + (e.created_at ? timeSince(new Date(e.created_at)) + ' ago' : '') + '</span>' +
      '</div>';
    }).join('');
  });
}

/* ── Architect Panel ──────────────────────────── */

function loadArchDashboard() {
  loadArchMetrics();
  loadArchHealth();
  loadArchFindings();
  loadArchResearch();
  loadArchEvolution();
  loadCodeRepos();
  loadCodeMetrics();
  loadCodeGraph();
  loadCodeInsights();
  loadCodeTrends();
}

function loadArchMetrics() {
  fetch('/api/brain/project/agent/architect/metrics').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('arch-status-card');
    if (!el) return;
    var hs = d.health_score || 0;
    var hsColor = hs > 0.7 ? '#22c55e' : hs > 0.4 ? '#f59e0b' : '#ef4444';
    var kpis = [
      {val: (d.confidence * 100).toFixed(0) + '%', lbl: 'Confidence'},
      {val: '<span style="color:' + hsColor + '">' + (hs * 100).toFixed(0) + '%</span>', lbl: 'Health Score'},
      {val: d.circular_deps || 0, lbl: 'Circular Deps'},
      {val: d.critical_hotspots || 0, lbl: 'Critical Hotspots'},
      {val: d.high_coupling_pairs || 0, lbl: 'High Coupling'},
      {val: d.trend_direction || 'N/A', lbl: 'Trend'},
      {val: d.finding_count || 0, lbl: 'Findings'},
      {val: d.research_count || 0, lbl: 'Research'},
    ];
    el.innerHTML = kpis.map(function(k) {
      return '<div class="agent-stat"><div class="agent-stat-val">' + k.val + '</div><div class="agent-stat-lbl">' + k.lbl + '</div></div>';
    }).join('');
  });
}

function loadArchHealth() {
  fetch('/api/brain/project/agent/architect/health').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var healthEl = document.getElementById('arch-health');
    var circEl = document.getElementById('arch-circular');
    var coupEl = document.getElementById('arch-coupling');
    var hotEl = document.getElementById('arch-hotspots');

    var snap = d.snapshot || {};
    if (healthEl) {
      healthEl.innerHTML =
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:8px">' +
          '<div class="agent-stat"><div class="agent-stat-val">' + (snap.repo_count || 0) + '</div><div class="agent-stat-lbl">Repos</div></div>' +
          '<div class="agent-stat"><div class="agent-stat-val">' + (snap.total_deps || 0) + '</div><div class="agent-stat-lbl">Dependencies</div></div>' +
          '<div class="agent-stat"><div class="agent-stat-val">' + (snap.total_hotspots || 0) + '</div><div class="agent-stat-lbl">Hotspots</div></div>' +
        '</div>' +
        '<button onclick="triggerAgentCycle(\'architect\')" class="brain-act-btn" style="margin-top:10px;background:linear-gradient(135deg,#f59e0b,#d97706)">\uD83C\uDFD7 Run Architect Cycle</button>';
    }

    var circ = d.circular || {};
    if (circEl) {
      if (!circ.total) {
        circEl.innerHTML = '<div style="font-size:11px;color:#22c55e;font-weight:600">No circular dependencies detected.</div>';
      } else {
        var cycleHtml = (circ.top_cycles || []).map(function(c) {
          return '<div style="padding:4px 0;border-bottom:1px solid var(--border);font-size:10px;font-family:monospace;color:#f59e0b">' + c + '</div>';
        }).join('');
        circEl.innerHTML =
          '<div style="font-size:12px;font-weight:700;color:#ef4444;margin-bottom:6px">' + circ.total + ' circular edge(s) in ' + circ.unique_cycles + ' unique cycle(s)</div>' +
          cycleHtml;
      }
    }

    var coup = d.coupling || {};
    if (coupEl) {
      var pairs = coup.top_coupling || [];
      if (!pairs.length) {
        coupEl.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No cross-module coupling data yet.</div>';
      } else {
        coupEl.innerHTML = pairs.map(function(p) {
          var barW = Math.min(100, (p.count / (pairs[0].count || 1)) * 100);
          var color = p.count > 20 ? '#ef4444' : p.count > 10 ? '#f59e0b' : '#22c55e';
          return '<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:10px">' +
            '<span style="min-width:180px;font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + p.source + ' → ' + p.target + '">' + p.source + ' → ' + p.target + '</span>' +
            '<div style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden"><div style="height:100%;width:' + barW + '%;background:' + color + ';border-radius:3px"></div></div>' +
            '<span style="min-width:25px;text-align:right;color:var(--text-muted)">' + p.count + '</span>' +
          '</div>';
        }).join('');
      }
    }

    var hots = d.hotspots || {};
    if (hotEl) {
      var files = hots.top_files || [];
      if (!files.length) {
        hotEl.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No hotspot data yet.</div>';
      } else {
        hotEl.innerHTML = '<div style="margin-bottom:6px;font-size:11px"><span style="color:#ef4444;font-weight:600">' + (hots.critical_count || 0) + '</span> critical, <span style="color:#f59e0b;font-weight:600">' + (hots.moderate_count || 0) + '</span> moderate</div>' +
          files.map(function(h) {
            var barW = Math.min(100, h.score * 100);
            var color = h.score > 0.7 ? '#ef4444' : h.score > 0.4 ? '#f59e0b' : '#22c55e';
            return '<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:10px">' +
              '<span style="min-width:200px;font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + h.file + '">' + h.file + '</span>' +
              '<div style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden"><div style="height:100%;width:' + barW + '%;background:' + color + ';border-radius:3px"></div></div>' +
              '<span style="min-width:40px;text-align:right;color:var(--text-muted)">' + h.score + '</span>' +
              '<span style="min-width:50px;text-align:right;font-size:9px;color:var(--text-muted)">' + h.commits + ' commits</span>' +
            '</div>';
          }).join('');
      }
    }
  });
}

function loadArchFindings() {
  fetch('/api/brain/project/agent/architect/findings').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('arch-findings');
    if (!el) return;
    var items = d.findings || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No findings yet.</div>'; return; }
    el.innerHTML = items.map(function(f) {
      return '<div class="agt-finding-row">' +
        '<span class="agt-finding-sev ' + f.severity + '">' + f.severity + '</span>' +
        '<div style="flex:1"><strong>' + f.title + '</strong><div style="color:var(--text-muted);margin-top:2px">' + f.description + '</div></div>' +
        '<span style="font-size:9px;color:var(--text-muted);white-space:nowrap">' + f.category + '</span>' +
      '</div>';
    }).join('');
  });
}

function loadArchResearch() {
  fetch('/api/brain/project/agent/architect/research').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('arch-research');
    if (!el) return;
    var items = d.research || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No research yet.</div>'; return; }
    el.innerHTML = items.map(function(r) {
      var sources = '';
      try {
        var s = JSON.parse(r.sources_json || '[]');
        if (s.length) {
          sources = '<div class="agt-research-sources">' + s.map(function(src) {
            return '<a href="' + (src.url || '#') + '" target="_blank">' + (src.title || src.url || 'source') + '</a>';
          }).join(' &middot; ') + '</div>';
        }
      } catch(e) {}
      return '<div class="agt-research-card">' +
        '<div class="agt-research-topic">' + r.topic + '</div>' +
        '<div class="agt-research-summary">' + r.summary + '</div>' +
        sources +
      '</div>';
    }).join('');
  });
}

function loadArchEvolution() {
  fetch('/api/brain/project/agent/architect/evolution').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var el = document.getElementById('arch-evolution');
    if (!el) return;
    var items = d.evolution || [];
    if (!items.length) { el.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No evolution events yet.</div>'; return; }
    el.innerHTML = items.map(function(e) {
      var before = (e.confidence_before * 100).toFixed(0);
      var after = (e.confidence_after * 100).toFixed(0);
      var delta = after - before;
      var arrow = delta >= 0 ? '\u2191' : '\u2193';
      var color = delta >= 0 ? '#22c55e' : '#ef4444';
      return '<div class="agt-evo-row">' +
        '<span class="agt-evo-dim">' + e.dimension + '</span>' +
        '<div class="agt-evo-conf">' + before + '%<span style="color:' + color + ';font-weight:700">' + arrow + '</span>' + after + '%</div>' +
        '<span style="flex:1;color:var(--text-muted)">' + e.description.substring(0, 100) + '</span>' +
        '<span style="font-size:9px;color:var(--text-muted)">' + (e.created_at ? timeSince(new Date(e.created_at)) + ' ago' : '') + '</span>' +
      '</div>';
    }).join('');
  });
}

function triggerAgentCycle(agentName) {
  fetch('/api/brain/project/agent/' + agentName + '/cycle', {method: 'POST'})
  .then(function(r){return r.json();}).then(function(d) {
    if (d.ok) {
      var el = document.getElementById('project-brain-status-text');
      if (el) el.textContent = agentName + ' cycle started...';
      setTimeout(function() {
        if (agentName === 'product_owner') loadPODashboard();
        else if (agentName === 'project_manager') loadPMDashboard();
        else if (agentName === 'architect') loadArchDashboard();
        else _loadGenericDashboard(agentName);
        loadAgentMessageFeed();
      }, 5000);
    } else {
      alert(d.message || 'Could not start cycle');
    }
  });
}

/* Lens functions removed — Code Brain merged into agent panels */

window.initProjectAgentBar = initProjectAgentBar;
window.switchProjectAgent = switchProjectAgent;
window._loadGenericDashboard = _loadGenericDashboard;
window.loadPODashboard = loadPODashboard;
window.loadPMDashboard = loadPMDashboard;
window.loadArchDashboard = loadArchDashboard;
window.triggerAgentCycle = triggerAgentCycle;
})();
