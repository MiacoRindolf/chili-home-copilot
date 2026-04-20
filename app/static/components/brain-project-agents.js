(function () {
  var _analysisSnapshot = null;
  var _generatedPanelIds = [];

  var PERSPECTIVES = [
    { name: "product", label: "Product" },
    { name: "architecture", label: "Architecture" },
    { name: "backend", label: "Backend" },
    { name: "frontend", label: "Frontend" },
    { name: "qa", label: "QA" },
    { name: "security", label: "Security" },
    { name: "ops", label: "Ops" },
    { name: "ai", label: "AI" }
  ];

  function el(id) {
    return document.getElementById(id);
  }

  function escHtml(value) {
    var node = document.createElement("div");
    node.textContent = value == null ? "" : String(value);
    return node.innerHTML;
  }

  function plannerTaskId() {
    if (typeof window.brainHandoffPlannerTaskId === "function") {
      return window.brainHandoffPlannerTaskId();
    }
    return null;
  }

  function canonicalPerspective(name) {
    var raw = (name || "").toString().toLowerCase();
    var aliases = {
      product_owner: "product",
      architect: "architecture",
      project_manager: "ops",
      devops: "ops",
      ai_eng: "ai"
    };
    return aliases[raw] || raw || "product";
  }

  function _perspectiveListFromSnapshot() {
    var snapshot = _analysisSnapshot && _analysisSnapshot.snapshot ? _analysisSnapshot.snapshot : _analysisSnapshot;
    var keys = snapshot && snapshot.perspectives ? Object.keys(snapshot.perspectives) : [];
    if (!keys.length) {
      return PERSPECTIVES.slice();
    }
    return keys.map(function (key) {
      var item = snapshot.perspectives[key] || {};
      return { name: key, label: item.label || key };
    });
  }

  function _analysisUrl() {
    var url = "/api/brain/project/analysis/latest";
    var tid = plannerTaskId();
    if (tid) {
      url += "?planner_task_id=" + encodeURIComponent(String(tid));
    }
    return url;
  }

  function _runAnalysis(forceRefresh) {
    var tid = plannerTaskId();
    if (!forceRefresh) {
      return fetch(_analysisUrl(), { credentials: "same-origin" })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          _analysisSnapshot = data;
          return data;
        });
    }
    return fetch("/api/brain/project/analysis/run", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ planner_task_id: tid || null })
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        _analysisSnapshot = { snapshot: data.snapshot || null };
        return _analysisSnapshot;
      });
  }

  function _clearGeneratedPanels() {
    while (_generatedPanelIds.length) {
      var panel = el(_generatedPanelIds.pop());
      if (panel && panel.parentNode) {
        panel.parentNode.removeChild(panel);
      }
    }
  }

  function _ensureGeneratedPanels() {
    var container = el("proj-agent-panels");
    if (!container) return;
    _clearGeneratedPanels();
    _perspectiveListFromSnapshot().forEach(function (perspective) {
      var panel = document.createElement("div");
      panel.className = "proj-agent-panel";
      panel.id = "analysis-panel-" + perspective.name;
      panel.setAttribute("data-agent", perspective.name);
      panel.setAttribute("data-generated-analysis", "1");
      panel.innerHTML =
        '<div class="brain-section">' +
        '<div class="brain-section-title">' + escHtml(perspective.label) + "</div>" +
        '<div class="project-inline-muted">Loading perspective...</div>' +
        "</div>";
      container.appendChild(panel);
      _generatedPanelIds.push(panel.id);
    });
  }

  function _renderTabs() {
    var bar = el("proj-agent-bar");
    if (!bar) return;
    var current = canonicalPerspective(window._activeProjectAgent || window._activeAgent || "product");
    bar.innerHTML = _perspectiveListFromSnapshot().map(function (perspective) {
      var active = perspective.name === current ? " active" : "";
      return (
        '<button class="proj-agent-tab' + active + '" data-agent="' + escHtml(perspective.name) + '">' +
        escHtml(perspective.label) +
        "</button>"
      );
    }).join("");
    Array.prototype.forEach.call(bar.querySelectorAll("[data-agent]"), function (button) {
      button.addEventListener("click", function () {
        switchProjectAgent(button.getAttribute("data-agent"));
      });
    });
  }

  function _renderPerspectiveBody(name) {
    var snapshot = _analysisSnapshot && _analysisSnapshot.snapshot ? _analysisSnapshot.snapshot : _analysisSnapshot;
    var payload = snapshot && snapshot.perspectives ? snapshot.perspectives[name] : null;
    var panel = el("analysis-panel-" + name);
    if (!panel) return;
    if (!payload) {
      panel.innerHTML =
        '<div class="brain-section"><div class="brain-section-title">No data yet</div>' +
        '<div class="project-inline-muted">Run project analysis to generate this perspective.</div></div>';
      return;
    }
    var bullets = payload.bullets || [];
    var metrics = payload.metrics || {};
    var metricBits = [];
    if (metrics.total_files != null) metricBits.push(String(metrics.total_files) + " files");
    if (metrics.hotspot_count != null) metricBits.push(String(metrics.hotspot_count) + " hotspots");
    if (metrics.insight_count != null) metricBits.push(String(metrics.insight_count) + " insights");
    panel.innerHTML =
      '<div class="agent-status-card">' +
      '<div class="agent-stat"><div class="agent-stat-val">' + escHtml(payload.status || "idle") + '</div><div class="agent-stat-lbl">Status</div></div>' +
      '<div class="agent-stat"><div class="agent-stat-val">' + escHtml(metricBits.join(" | ") || "No metrics") + '</div><div class="agent-stat-lbl">Signals</div></div>' +
      "</div>" +
      '<div class="brain-section">' +
      '<div class="brain-section-title">' + escHtml(payload.label || name) + "</div>" +
      '<div style="font-size:12px;font-weight:600;margin-bottom:8px">' + escHtml(payload.headline || "") + "</div>" +
      (bullets.length
        ? '<div class="project-scroll-panel">' + bullets.map(function (item) {
            return '<div class="agt-finding-row"><div style="flex:1">' + escHtml(item) + "</div></div>";
          }).join("") + "</div>"
        : '<div class="project-inline-muted">No advisory notes yet.</div>') +
      "</div>";
  }

  function _applyPanelSelection(name) {
    Array.prototype.forEach.call(document.querySelectorAll(".proj-agent-tab"), function (node) {
      node.classList.toggle("active", node.getAttribute("data-agent") === name);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".proj-agent-panel"), function (node) {
      node.classList.toggle("active", node.getAttribute("data-agent") === name);
    });
  }

  function _loadAndRenderCurrent(forceRefresh) {
    return _runAnalysis(!!forceRefresh)
      .then(function () {
        _ensureGeneratedPanels();
        _renderTabs();
        var target = canonicalPerspective(window._activeProjectAgent || window._activeAgent || "product");
        _renderPerspectiveBody(target);
        _applyPanelSelection(target);
      })
      .catch(function () {
        var container = el("proj-agent-panels");
        if (container) {
          container.innerHTML =
            '<div class="brain-section"><div class="brain-section-title">Analysis unavailable</div>' +
            '<div class="project-inline-muted">Could not load the latest project analysis.</div></div>';
        }
      });
  }

  function initProjectAgentBar() {
    var current = canonicalPerspective(window._activeProjectAgent || window._activeAgent || "product");
    window._activeAgent = current;
    window._activeProjectAgent = current;
    _loadAndRenderCurrent(false);
  }

  function switchProjectAgent(agentName) {
    var current = canonicalPerspective(agentName);
    window._activeAgent = current;
    window._activeProjectAgent = current;
    if (!_analysisSnapshot) {
      _loadAndRenderCurrent(false);
      return;
    }
    _renderTabs();
    _renderPerspectiveBody(current);
    _applyPanelSelection(current);
  }

  function triggerAgentCycle(agentName) {
    var current = canonicalPerspective(agentName || window._activeProjectAgent || "product");
    window._activeAgent = current;
    window._activeProjectAgent = current;
    _loadAndRenderCurrent(true).then(function () {
      if (typeof window.loadAgentMessageFeed === "function") {
        window.loadAgentMessageFeed();
      }
      if (typeof window.brainProjectRefreshBootstrap === "function") {
        window.brainProjectRefreshBootstrap();
      }
    });
  }

  function _openPerspective(name) {
    switchProjectAgent(name);
  }

  function _refreshCurrentPerspective() {
    switchProjectAgent(window._activeProjectAgent || "product");
  }

  window.initProjectAgentBar = initProjectAgentBar;
  window.switchProjectAgent = switchProjectAgent;
  window._loadGenericDashboard = _openPerspective;
  window.loadPODashboard = _refreshCurrentPerspective;
  window.loadPMDashboard = _refreshCurrentPerspective;
  window.loadArchDashboard = _refreshCurrentPerspective;
  window.triggerAgentCycle = triggerAgentCycle;
})();
