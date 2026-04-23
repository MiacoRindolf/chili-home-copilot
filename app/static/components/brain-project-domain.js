(function () {
  var _projectBootstrap = null;
  var _selectedCodeRepoId = null;
  var _codePollTimer = null;

  function el(id) {
    return document.getElementById(id);
  }

  function escHtml(value) {
    var node = document.createElement("div");
    node.textContent = value == null ? "" : String(value);
    return node.innerHTML;
  }

  function _bootstrapSelectedRepoId() {
    var selected = _projectBootstrap && _projectBootstrap.workspace && _projectBootstrap.workspace.selected_repo;
    var repoId = selected && selected.id ? parseInt(selected.id, 10) : NaN;
    return Number.isFinite(repoId) && repoId > 0 ? repoId : null;
  }

  function _currentCodeRepoId() {
    var repoSel = document.querySelector("#code-repos .code-repo-card.active");
    var repoId = repoSel ? parseInt(repoSel.getAttribute("data-repo-id") || "", 10) : NaN;
    if (Number.isFinite(repoId) && repoId > 0) return repoId;
    if (Number.isFinite(_selectedCodeRepoId) && _selectedCodeRepoId > 0) return _selectedCodeRepoId;
    return _bootstrapSelectedRepoId();
  }

  function summaryCardHtml(label, value, note) {
    return (
      '<div class="project-summary-card">' +
      '<div class="project-summary-label">' +
      escHtml(label) +
      "</div>" +
      '<div class="project-summary-value">' +
      escHtml(value) +
      "</div>" +
      '<div class="project-summary-note">' +
      escHtml(note || "") +
      "</div>" +
      "</div>"
    );
  }

  function plannerTaskId() {
    if (typeof window.brainHandoffPlannerTaskId === "function") {
      return window.brainHandoffPlannerTaskId();
    }
    try {
      var raw = new URLSearchParams(window.location.search || "").get("planner_task_id");
      if (raw == null || raw === "") return null;
      var parsed = parseInt(raw, 10);
      return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
    } catch (err) {
      return null;
    }
  }

  function setPane(target) {
    var panes = document.querySelectorAll("[data-project-pane]");
    panes.forEach(function (pane) {
      pane.style.display = pane.getAttribute("data-project-pane") === target ? "block" : "none";
    });
    var buttons = document.querySelectorAll("[data-project-pane-target]");
    buttons.forEach(function (button) {
      var active = button.getAttribute("data-project-pane-target") === target;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function setButtonState(id, capability, fallbackReason) {
    var button = el(id);
    if (!button) return;
    var enabled = !!(capability && capability.enabled);
    button.disabled = !enabled;
    var reason = enabled ? "" : ((capability && capability.reason) || fallbackReason || "");
    if (reason) {
      button.title = reason;
    }
  }

  function renderChecklist(items) {
    var list = el("project-workspace-checklist");
    if (!list) return;
    list.innerHTML = "";
    (items || []).forEach(function (item) {
      var li = document.createElement("li");
      li.className = "project-checklist-item";
      li.innerHTML =
        '<span class="project-checklist-state ' +
        (item.done ? "done" : "todo") +
        '">' +
        (item.done ? "OK" : "TODO") +
        "</span>" +
        escHtml(item.label || item.key || "");
      list.appendChild(li);
    });
    if (!list.innerHTML) {
      list.innerHTML = '<li class="project-inline-muted">No setup checklist yet.</li>';
    }
  }

  function renderWorkspaceSummary(data) {
    var summary = el("project-workspace-summary");
    var empty = el("project-workspace-empty");
    var taskSummary = el("project-workspace-task-summary");
    if (!summary || !empty || !taskSummary) return;

    var workspace = (data && data.workspace) || {};
    var handoff = (data && data.planner_handoff) || {};
    var capabilities = (data && data.capabilities) || {};
    var profile = (handoff.summary && handoff.summary.profile) || {};
    var ops = (handoff.summary && handoff.summary.ops_hints) || {};
    var selectedRepo = workspace.selected_repo || {};

    var taskCardValue = "No task";
    var taskCardNote = "Open a planner task in Brain to unlock task-scoped suggest, apply, and validation.";
    if (handoff.task) {
      taskCardValue = profile.workspace_bound ? "Bound" : "Needs binding";
      taskCardNote = handoff.task.title || ("Task #" + handoff.task.id);
    } else if (plannerTaskId()) {
      taskCardValue = "Unavailable";
      taskCardNote = "Planner task handoff is not available for this device or project membership.";
    }

    summary.innerHTML =
      '<div class="project-summary-grid">' +
      summaryCardHtml(
        "Registered Repos",
        workspace.repo_count || 0,
        workspace.repo_count ? "Canonical workspaces are ready to bind." : "Add a repo to start the cockpit."
      ) +
      summaryCardHtml(
        "Indexed Repos",
        workspace.indexed_repo_count || 0,
        workspace.indexed_repo_count ? "Code search and agent context are available." : "Run indexing after registration."
      ) +
      summaryCardHtml(
        "Project Brain",
        (data.project_status && data.project_status.running) ? "Running" : "Idle",
        "Code Brain: " + ((data.code_status && data.code_status.running) ? "Indexing" : "Idle")
      ) +
      summaryCardHtml("Task Workspace", taskCardValue, taskCardNote) +
      "</div>";

    if (workspace.empty_state) {
      empty.style.display = "block";
      empty.innerHTML =
        "<strong>No workspace is registered yet.</strong><br>" +
        "Start by adding the repo you want this cockpit to operate on, then run indexing so search and task handoff share the same canonical record.";
    } else {
      empty.style.display = "none";
      empty.innerHTML = "";
    }

    var taskBits = [];
    if (profile.repo_name) {
      taskBits.push({ label: "Bound repo", value: profile.repo_name });
    }
    if (profile.repo_path) {
      taskBits.push({ label: "Path", value: profile.repo_path });
    }
    if (profile.sub_path) {
      taskBits.push({ label: "Focus path", value: profile.sub_path });
    }
    if (ops.workspace_reason) {
      taskBits.push({ label: "Status", value: ops.workspace_reason });
    }
    if (selectedRepo.name) {
      taskBits.push({ label: "Selected repo", value: selectedRepo.name });
    }
    if (selectedRepo.reason) {
      taskBits.push({ label: "Repo choice", value: selectedRepo.reason });
    }
    if (capabilities.suggest && !capabilities.suggest.enabled) {
      taskBits.push({ label: "Suggest", value: capabilities.suggest.reason || "Unavailable" });
    }
    taskSummary.innerHTML = taskBits.length
      ? taskBits
          .map(function (bit) {
            return (
              '<div class="project-summary-note"><strong>' +
              escHtml(bit.label) +
              ":</strong> " +
              escHtml(bit.value) +
              "</div>"
            );
          })
          .join("")
      : '<div class="project-summary-note">Task-scoped workspace details will appear here after you open a planner handoff.</div>';
  }

  function applyBootstrap(data) {
    _projectBootstrap = data || null;
    _selectedCodeRepoId = _bootstrapSelectedRepoId();
    renderChecklist(data && data.workspace ? data.workspace.setup_checklist : []);
    renderWorkspaceSummary(data || {});

    var loadEnabled = !!plannerTaskId() && !(data && data.is_guest);
    setButtonState(
      "brain-handoff-load-btn",
      { enabled: loadEnabled, reason: loadEnabled ? null : "Select and pair into a planner task first." }
    );
    setButtonState("btn-project-add-repo", data && data.capabilities && data.capabilities.register_repo);
    setButtonState(
      "btn-code-learn",
      {
        enabled: !!(data && !data.is_guest && data.workspace && data.workspace.repo_count > 0),
        reason: "Register a repo before indexing."
      }
    );
    setButtonState(
      "btn-all-agents",
      { enabled: !(data && data.is_guest), reason: "Pair this device to run Project Brain agents." }
    );
    setButtonState("brain-handoff-agent-suggest-btn", data && data.capabilities && data.capabilities.suggest);
    setButtonState("brain-handoff-run-validation-btn", data && data.capabilities && data.capabilities.validate);

    var hint = el("brain-planner-handoff-hint");
    if (hint) {
      if (!plannerTaskId()) {
        hint.textContent = "Select a planner task to load the implementation handoff and unlock workspace-scoped actions.";
      } else if (data && data.capabilities && data.capabilities.suggest && !data.capabilities.suggest.enabled) {
        hint.textContent = data.capabilities.suggest.reason || "Task handoff is not ready yet.";
      } else {
        hint.textContent = "Task handoff stays explicit and read-only until you click Load handoff.";
      }
    }
  }

  function refreshBootstrap() {
    var url = "/api/brain/project/bootstrap";
    var tid = plannerTaskId();
    if (tid) {
      url += "?planner_task_id=" + encodeURIComponent(String(tid));
    }
    return fetch(url, { credentials: "same-origin" })
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.ok) {
          throw new Error("bootstrap_unavailable");
        }
        applyBootstrap(data);
        return data;
      })
      .catch(function () {
        var summary = el("project-workspace-summary");
        if (summary) {
          summary.textContent = "Could not load the project cockpit bootstrap.";
        }
        return null;
      });
  }

  function loadCodeDashboard() {
    pollCodeLearningStatus();
  }

  function loadAgentMessageFeed() {
    var feed = el("agent-msg-feed");
    if (!feed) return;
    if (window._activeDomain !== "project") {
      feed.innerHTML =
        '<div class="project-inline-muted" data-agent-feed-off-project="1">Switch to the Project domain to see the operator timeline...</div>';
      return;
    }
    feed.innerHTML = '<div class="project-inline-muted">Loading operator timeline...</div>';
    fetch("/api/brain/project/messages", { credentials: "same-origin" })
      .then(function (response) {
        return response.json().then(
          function (data) {
            return { ok: response.ok, data: data };
          },
          function () {
            return { ok: false, data: null };
          }
        );
      })
      .then(function (result) {
        if (!feed || window._activeDomain !== "project") return;
        if (!result.ok || !result.data || result.data.ok !== true) {
          feed.innerHTML =
            '<div style="font-size:11px;color:var(--danger,#dc2626)">Could not load the operator timeline. Try Refresh.</div>';
          return;
        }
        var messages = result.data.messages || [];
        if (!messages.length) {
          feed.innerHTML =
            '<div style="font-size:11px;color:var(--text-muted)">No operator timeline events yet. Run analysis.</div>';
          return;
        }
        feed.innerHTML = messages
          .slice(0, 30)
          .map(function (message) {
            var ts = message.created_at ? window.timeSince(new Date(message.created_at)) + " ago" : "";
            var ack = message.acknowledged ? "" : ' style="background:rgba(139,92,246,.04)"';
            return (
              '<div class="msg-feed-row"' +
              ack +
              ">" +
              '<span class="msg-feed-agents">' +
              escHtml(message.summary || message.type || "Event") +
              "</span>" +
              '<span class="msg-feed-type">' +
              escHtml(message.status || message.type) +
              "</span>" +
              '<span class="msg-feed-time">' +
              escHtml(ts) +
              "</span>" +
              "</div>"
            );
          })
          .join("");
      })
      .catch(function () {
        if (!feed || window._activeDomain !== "project") return;
        feed.innerHTML =
          '<div style="font-size:11px;color:var(--danger,#dc2626)">Could not load the operator timeline (network error). Try Refresh.</div>';
      });
  }

  function triggerAllAgentsCycle() {
    fetch("/api/brain/project/cycle", { method: "POST" })
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        var status = el("project-brain-status-text");
        if (data && data.ok) {
          if (status) status.textContent = "All-agent cycle started...";
          window.setTimeout(function () {
            loadProjectBrainStatus();
          }, 3000);
          window.setTimeout(function () {
            if (typeof window._agentPanelLoaded === "object" && window._agentPanelLoaded) {
              Object.keys(window._agentPanelLoaded).forEach(function (key) {
                delete window._agentPanelLoaded[key];
              });
            }
            if (typeof window.switchProjectAgent === "function") {
              window.switchProjectAgent(window._activeAgent || "product_owner");
            }
            loadAgentMessageFeed();
            refreshBootstrap();
          }, 15000);
        } else if (status) {
          status.textContent = (data && (data.error || data.message)) || "Failed";
        }
      })
      .catch(function () {
        var status = el("project-brain-status-text");
        if (status) {
          status.textContent = "Failed";
        }
      });
  }

  function loadProjectBrainStatus() {
    var status = el("project-brain-status-text");
    if (!status) return;
    fetch("/api/brain/project/status")
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.ok) return;
        if (data.running) {
          var pct = Math.round((data.progress || 0) * 100);
          status.textContent = (data.step || "running") + " (" + pct + "%)";
          window.setTimeout(loadProjectBrainStatus, 3000);
        } else {
          status.textContent = data.last_run
            ? "Last run: " + window.timeSince(new Date(data.last_run)) + " ago"
            : "Idle";
        }
      })
      .catch(function () {});
  }

  function loadCodeRepos() {
    var container = el("code-repos");
    if (!container) return;
    fetch("/api/brain/code/repos")
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.ok) return;
        var repos = data.repos || [];
        var preferredRepoId = _currentCodeRepoId();
        var hasPreferredRepo = !!preferredRepoId && repos.some(function (repo) {
          return parseInt(repo.id, 10) === preferredRepoId;
        });
        if (!repos.length) {
          _selectedCodeRepoId = null;
          container.innerHTML =
            '<div style="text-align:center;padding:20px;color:var(--text-muted)">' +
            '<div style="font-size:32px;margin-bottom:8px">&#x1F4C1;</div>' +
            '<div style="font-size:12px;font-weight:600;margin-bottom:4px">No repositories registered</div>' +
            '<div style="font-size:11px">Click <strong>+ Add Repo</strong> above to get started</div></div>';
          return;
        }
        if (!hasPreferredRepo) {
          _selectedCodeRepoId = parseInt(repos[0].id, 10);
        }
        var html = "";
        repos.forEach(function (repo, index) {
          var langs = Object.entries(repo.language_stats || {})
            .sort(function (a, b) {
              return b[1] - a[1];
            })
            .slice(0, 6);
          var langTags = langs
            .map(function (lang) {
              return '<span class="code-repo-lang-tag">' + escHtml(lang[0]) + " (" + lang[1] + ")</span>";
            })
            .join("");
          var fwTags = (repo.framework_tags || [])
            .map(function (framework) {
              return '<span class="code-repo-fw-tag">' + escHtml(framework) + "</span>";
            })
            .join("");
          html +=
            '<div class="code-repo-card' +
            ((hasPreferredRepo ? preferredRepoId === parseInt(repo.id, 10) : index === 0) ? " active" : "") +
            '" data-repo-id="' +
            escHtml(repo.id) +
            '">' +
            '<button class="code-repo-remove" data-remove-repo-id="' +
            escHtml(repo.id) +
            '" title="Remove">&times;</button>' +
            '<div class="code-repo-name">&#x1F4C2; ' +
            escHtml(repo.name) +
            "</div>" +
            '<div class="code-repo-path">' +
            escHtml(repo.path) +
            "</div>" +
            '<div class="code-repo-stats">' +
            "<span>&#x1F4C4; " +
            (repo.file_count || 0).toLocaleString() +
            " files</span>" +
            "<span>&#x1F4DD; " +
            (repo.total_lines || 0).toLocaleString() +
            " lines</span>" +
            "</div>" +
            '<div class="code-repo-langs">' +
            langTags +
            fwTags +
            "</div>" +
            '<div class="code-repo-meta">' +
            (repo.last_indexed
              ? "Last indexed: " + window.timeSince(new Date(repo.last_indexed)) + " ago"
              : "Not yet indexed") +
            "</div>" +
            "</div>";
        });
        container.innerHTML = html;

        Array.prototype.forEach.call(container.querySelectorAll(".code-repo-card"), function (card) {
          card.addEventListener("click", function (event) {
            if (event.target && event.target.closest && event.target.closest(".code-repo-remove")) {
              return;
            }
            Array.prototype.forEach.call(container.querySelectorAll(".code-repo-card"), function (node) {
              node.classList.toggle("active", node === card);
            });
            _selectedCodeRepoId = parseInt(card.getAttribute("data-repo-id") || "", 10);
            loadCodeGraph();
            loadCodeTrends();
          });
        });

        Array.prototype.forEach.call(container.querySelectorAll("[data-remove-repo-id]"), function (button) {
          button.addEventListener("click", function (event) {
            event.preventDefault();
            event.stopPropagation();
            var repoId = parseInt(button.getAttribute("data-remove-repo-id") || "", 10);
            if (Number.isFinite(repoId) && repoId > 0) {
              removeCodeRepo(repoId);
            }
          });
        });
      })
      .catch(function () {});
  }

  function showAddRepoModal() {
    if (typeof window.openBrainModal !== "function") return;
    var bodyHtml =
      '<div style="margin-bottom:12px">' +
      '<label style="font-size:11px;font-weight:600;display:block;margin-bottom:4px">Local Path</label>' +
      '<input type="text" id="add-repo-path" placeholder="C:\\dev\\my-project" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font-size:12px">' +
      "</div>" +
      "<div>" +
      '<label style="font-size:11px;font-weight:600;display:block;margin-bottom:4px">Name (optional)</label>' +
      '<input type="text" id="add-repo-name" placeholder="my-project" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font-size:12px">' +
      "</div>";
    var footerHtml =
      '<button onclick="doAddRepo()" style="background:var(--accent);color:#fff;border-radius:6px;padding:6px 20px">Add Repository</button>';
    window.openBrainModal("Add Repository", bodyHtml, footerHtml);
  }

  function doAddRepo() {
    var pathInput = el("add-repo-path");
    var nameInput = el("add-repo-name");
    var path = pathInput ? pathInput.value.trim() : "";
    var name = nameInput ? nameInput.value.trim() : "";
    if (!path) return;
    fetch("/api/brain/code/repos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: path, name: name || null }),
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (typeof window.closeBrainModal === "function") {
          window.closeBrainModal();
        }
        if (data && data.ok) {
          loadCodeRepos();
          refreshBootstrap();
          return;
        }
        window.alert((data && (data.message || data.error)) || "Failed to add repo");
      })
      .catch(function (error) {
        window.alert("Error: " + error.message);
      });
  }

  function removeCodeRepo(repoId) {
    if (!window.confirm("Remove this repository from Code Brain?")) return;
    fetch("/api/brain/code/repos/" + repoId, { method: "DELETE" })
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (data && data.ok) {
          loadCodeRepos();
          refreshBootstrap();
        }
      })
      .catch(function () {});
  }

  function loadCodeMetrics() {
    var kpis = el("code-kpis");
    if (!kpis) return;
    fetch("/api/brain/code/metrics")
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.ok) return;
        var langList = Object.entries(data.languages || {})
          .sort(function (a, b) {
            return b[1] - a[1];
          })
          .slice(0, 3);
        var topLangs = langList
          .map(function (lang) {
            return lang[0];
          })
          .join(", ") || "N/A";

        kpis.innerHTML =
          '<div class="brain-kpi">' +
          '<div class="bk-val" style="color:#8b5cf6">' +
          (data.total_files || 0).toLocaleString() +
          "</div>" +
          '<div class="bk-lbl">Total Files</div>' +
          '<div class="bk-trend flat">' +
          (data.repos || 0) +
          " repo(s)</div></div>" +
          '<div class="brain-kpi">' +
          '<div class="bk-val" style="color:#06b6d4">' +
          (data.total_lines || 0).toLocaleString() +
          "</div>" +
          '<div class="bk-lbl">Lines of Code</div>' +
          '<div class="bk-trend flat">' +
          escHtml(topLangs) +
          "</div></div>" +
          '<div class="brain-kpi">' +
          '<div class="bk-val ' +
          (data.avg_complexity > 50 ? "danger" : data.avg_complexity > 30 ? "warn" : "good") +
          '">' +
          (data.avg_complexity || 0).toFixed(1) +
          "</div>" +
          '<div class="bk-lbl">Avg Complexity</div>' +
          '<div class="bk-trend flat">' +
          (data.hotspot_count || 0) +
          " hotspots</div></div>" +
          '<div class="brain-kpi">' +
          '<div class="bk-val" style="color:#22c55e">' +
          (data.insight_count || 0) +
          "</div>" +
          '<div class="bk-lbl">Insights Discovered</div>' +
          '<div class="bk-trend flat">patterns &amp; conventions</div></div>';

        var events = data.recent_events || [];
        var activity = el("code-activity");
        if (activity && events.length > 0) {
          var timelineHtml = "";
          events.forEach(function (event) {
            var icon =
              event.type === "error"
                ? "&#x274C;"
                : event.type === "insight"
                  ? "&#x1F4A1;"
                  : event.type === "index"
                    ? "&#x1F4C1;"
                    : "&#x1F504;";
            timelineHtml +=
              '<div class="brain-event">' +
              '<div class="be-icon">' +
              icon +
              "</div>" +
              '<div class="be-body">' +
              '<div class="be-desc">' +
              escHtml(event.description) +
              "</div>" +
              '<div class="be-time">' +
              (event.created_at ? window.timeSince(new Date(event.created_at)) + " ago" : "") +
              "</div>" +
              "</div></div>";
          });
          activity.innerHTML = timelineHtml;
        }
      })
      .catch(function () {});
  }

  function loadCodeHotspots() {
    var container = el("code-hotspots");
    if (!container) return;
    fetch("/api/brain/code/hotspots")
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.ok) return;
        if (!data.hotspots || data.hotspots.length === 0) {
          container.innerHTML =
            '<div style="font-size:11px;color:var(--text-muted);padding:8px">No hotspots yet. Run indexing to analyze your code.</div>';
          return;
        }
        var maxCombined = data.hotspots[0].combined || 0.01;
        var html = "";
        data.hotspots.slice(0, 20).forEach(function (hotspot) {
          var pct = Math.round((hotspot.combined / maxCombined) * 100);
          var color = pct > 70 ? "#ef4444" : pct > 40 ? "#f59e0b" : "#22c55e";
          html +=
            '<div class="code-hotspot-row">' +
            '<div class="code-hotspot-file" title="' +
            escHtml(hotspot.file) +
            '">' +
            escHtml(hotspot.file) +
            "</div>" +
            '<div class="code-hotspot-bar"><div class="code-hotspot-fill" style="width:' +
            pct +
            "%;background:" +
            color +
            '"></div></div>' +
            '<div class="code-hotspot-score" style="color:' +
            color +
            '">' +
            (hotspot.combined * 100).toFixed(0) +
            "</div>" +
            '<div class="code-hotspot-commits">' +
            hotspot.commits +
            " commits</div>" +
            "</div>";
        });
        container.innerHTML = html;
      })
      .catch(function () {});
  }

  function loadCodeInsights() {
    var container = el("code-insights");
    if (!container) return;
    fetch("/api/brain/code/insights")
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.ok) return;
        if (!data.insights || data.insights.length === 0) {
          container.innerHTML =
            '<div style="font-size:11px;color:var(--text-muted);padding:8px">No insights yet. Run indexing to discover patterns.</div>';
          return;
        }
        var html = "";
        data.insights.forEach(function (insight) {
          html +=
            '<div class="code-insight-card">' +
            '<div class="code-insight-header">' +
            '<span class="code-insight-cat ' +
            escHtml(insight.category) +
            '">' +
            escHtml(insight.category) +
            "</span>" +
            '<span style="font-size:10px;color:var(--text-muted)">' +
            Math.round((insight.confidence || 0) * 100) +
            "% confidence</span>" +
            "</div>" +
            '<div class="code-insight-desc">' +
            escHtml(insight.description) +
            "</div>" +
            '<div class="code-insight-meta">' +
            "<span>Evidence: " +
            (insight.evidence_count || 0) +
            " files</span>" +
            "<span>" +
            (insight.last_seen ? window.timeSince(new Date(insight.last_seen)) + " ago" : "") +
            "</span>" +
            "</div></div>";
        });
        container.innerHTML = html;
      })
      .catch(function () {});
  }

  function loadCodeGraph() {
    var repoId = _currentCodeRepoId();
    if (!repoId) {
      return;
    }
    _fetchGraph(repoId);
  }

  function _fetchGraph(repoId) {
    var container = el("code-graph");
    if (!container) return;
    fetch("/api/brain/code/graph?repo_id=" + encodeURIComponent(repoId))
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.ok || !data.stats) {
          container.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No graph data yet.</div>';
          return;
        }
        var stats = data.stats;
        var html = '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px">';
        html +=
          '<div class="brain-kpi"><div class="bk-val">' +
          (stats.total_nodes || 0) +
          '</div><div class="bk-label">Files</div></div>';
        html +=
          '<div class="brain-kpi"><div class="bk-val">' +
          (stats.total_edges || 0) +
          '</div><div class="bk-label">Edges</div></div>';
        html +=
          '<div class="brain-kpi"><div class="bk-val" style="color:' +
          (stats.circular_edges ? "#ef4444" : "#22c55e") +
          '">' +
          (stats.circular_edges || 0) +
          '</div><div class="bk-label">Circular</div></div>';
        html += "</div>";
        if (stats.circular_edges > 0) {
          html +=
            '<div style="background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);border-radius:8px;padding:8px 12px;font-size:11px;color:#ef4444;margin-bottom:8px">';
          html +=
            "&#x26A0; " +
            stats.circular_edges +
            " circular dependency edge(s) detected. Consider breaking these cycles.</div>";
        }
        if (stats.most_depended_on && stats.most_depended_on.length) {
          html += '<div style="font-size:11px;font-weight:600;margin:8px 0 4px">Most Depended-On Files</div>';
          html += '<div style="display:flex;flex-direction:column;gap:3px">';
          stats.most_depended_on.forEach(function (file) {
            html +=
              '<div style="display:flex;justify-content:space-between;font-size:11px;padding:3px 8px;background:var(--card);border-radius:4px">';
            html +=
              '<span style="color:var(--text);word-break:break-all">' +
              escHtml(file.file) +
              "</span>";
            html +=
              '<span style="color:var(--accent);font-weight:600;white-space:nowrap;margin-left:8px">' +
              file.count +
              " imports</span></div>";
          });
          html += "</div>";
        }
        if (stats.coupling && stats.coupling.length) {
          html += '<div style="font-size:11px;font-weight:600;margin:10px 0 4px">Top Module Coupling</div>';
          html += '<div style="display:flex;flex-direction:column;gap:3px">';
          stats.coupling.slice(0, 8).forEach(function (coupling) {
            html +=
              '<div style="display:flex;justify-content:space-between;font-size:10px;padding:3px 8px;background:var(--card);border-radius:4px">';
            html +=
              "<span>" +
              escHtml(coupling.source_dir) +
              " &#8594; " +
              escHtml(coupling.target_dir) +
              "</span>";
            html += '<span style="font-weight:600">' + coupling.edge_count + "</span></div>";
          });
          html += "</div>";
        }
        container.innerHTML = html;
      })
      .catch(function () {});
  }

  function loadCodeTrends() {
    var repoId = _currentCodeRepoId();
    if (!repoId) {
      return;
    }
    _fetchTrends(repoId);
  }

  function _fetchTrends(repoId) {
    var container = el("code-trends");
    if (!container) return;
    fetch("/api/brain/code/trends?repo_id=" + encodeURIComponent(repoId))
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.ok || !data.series || !data.series.length) {
          container.innerHTML =
            '<div style="font-size:11px;color:var(--text-muted)">No trend data yet. Requires multiple learning cycles.</div>';
          return;
        }
        var html = "";
        var deltas = data.deltas && data.deltas.available ? data.deltas : null;
        if (deltas && deltas.alerts && deltas.alerts.length) {
          html +=
            '<div style="background:rgba(234,179,8,0.08);border:1px solid rgba(234,179,8,0.2);border-radius:8px;padding:8px 12px;font-size:11px;color:#eab308;margin-bottom:8px">';
          deltas.alerts.forEach(function (alert) {
            html +=
              "&#x26A0; <b>" +
              escHtml(alert.metric) +
              "</b> changed " +
              (alert.change > 0 ? "+" : "") +
              alert.change +
              "%<br>";
          });
          html += "</div>";
        }
        html += '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px">';
        html +=
          '<canvas id="trend-cx-canvas" width="260" height="80" style="background:var(--card);border-radius:8px;padding:4px"></canvas>';
        html +=
          '<canvas id="trend-files-canvas" width="260" height="80" style="background:var(--card);border-radius:8px;padding:4px"></canvas>';
        html += "</div>";
        if (deltas && deltas.deltas) {
          html += '<div style="display:flex;gap:12px;flex-wrap:wrap">';
          var dd = deltas.deltas;
          if (dd.complexity_delta_pct !== null) html += _trendBadge("Complexity", dd.complexity_delta_pct, true);
          if (dd.files_delta_pct !== null) html += _trendBadge("Files", dd.files_delta_pct, false);
          if (dd.test_ratio_delta_pct !== null) html += _trendBadge("Test Ratio", dd.test_ratio_delta_pct, false);
          if (dd.hotspot_delta_pct !== null) html += _trendBadge("Hotspots", dd.hotspot_delta_pct, true);
          html += "</div>";
        }
        container.innerHTML = html;
        _drawTrendSparkline(
          "trend-cx-canvas",
          data.series.map(function (seriesPoint) {
            return seriesPoint.avg_complexity;
          }),
          "Avg Complexity",
          "#06b6d4"
        );
        _drawTrendSparkline(
          "trend-files-canvas",
          data.series.map(function (seriesPoint) {
            return seriesPoint.total_files;
          }),
          "Total Files",
          "#8b5cf6"
        );
      })
      .catch(function () {});
  }

  function _trendBadge(label, pct, invertColor) {
    var isUp = pct > 0;
    var color = invertColor ? (isUp ? "#ef4444" : "#22c55e") : isUp ? "#22c55e" : "#ef4444";
    return (
      '<div style="font-size:10px;padding:3px 10px;border-radius:12px;background:' +
      color +
      '15;color:' +
      color +
      '">' +
      escHtml(label) +
      " <b>" +
      (isUp ? "+" : "") +
      pct +
      "%</b></div>"
    );
  }

  function _drawTrendSparkline(canvasId, values, label, color) {
    var cv = el(canvasId);
    if (!cv || !values.length) return;
    var ctx = cv.getContext("2d");
    var w = cv.width;
    var h = cv.height;
    ctx.clearRect(0, 0, w, h);
    var mn = Math.min.apply(null, values);
    var mx = Math.max.apply(null, values);
    var range = mx - mn || 1;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    values.forEach(function (value, index) {
      var x = (index / Math.max(values.length - 1, 1)) * (w - 20) + 10;
      var y = h - 16 - ((value - mn) / range) * (h - 24);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.font = "9px system-ui";
    ctx.fillStyle = "#888";
    ctx.fillText(label, 4, 10);
  }

  function loadCodeReviews() {
    var container = el("code-reviews");
    if (!container) return;
    fetch("/api/brain/code/reviews")
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.ok || !data.reviews || !data.reviews.length) {
          container.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No reviews yet.</div>';
          return;
        }
        var html = "";
        data.reviews.forEach(function (review) {
          var scoreColor = review.overall_score >= 7 ? "#22c55e" : review.overall_score >= 4 ? "#eab308" : "#ef4444";
          html += '<div style="background:var(--card);border-radius:8px;padding:10px 12px;margin-bottom:6px">';
          html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">';
          html +=
            '<span style="font-size:11px;font-weight:600;font-family:monospace">' +
            escHtml((review.commit_hash || "").slice(0, 8)) +
            "</span>";
          html += '<span style="font-size:10px;color:var(--text-muted)">' + escHtml(review.author || "") + "</span>";
          html +=
            '<span style="font-size:11px;font-weight:700;color:' +
            scoreColor +
            '">' +
            review.overall_score +
            "/10</span>";
          html += "</div>";
          if (review.summary) {
            html += '<div style="font-size:11px;color:var(--text);margin-bottom:4px">' + escHtml(review.summary) + "</div>";
          }
          if (review.findings && review.findings.length) {
            review.findings.slice(0, 5).forEach(function (finding) {
              var findingColor =
                finding.severity === "critical" ? "#ef4444" : finding.severity === "warn" ? "#eab308" : "#06b6d4";
              html +=
                '<div style="font-size:10px;padding:2px 8px;border-left:2px solid ' +
                findingColor +
                ';margin:2px 0;color:var(--text)">';
              html +=
                '<span style="color:' +
                findingColor +
                ';font-weight:600;text-transform:uppercase">' +
                escHtml(finding.severity || "info") +
                "</span> ";
              html += escHtml(finding.message || "") + "</div>";
            });
          }
          html +=
            '<div style="font-size:9px;color:var(--text-muted);margin-top:4px">' +
            (review.reviewed_at ? window.timeSince(new Date(review.reviewed_at)) + " ago" : "") +
            "</div>";
          html += "</div>";
        });
        container.innerHTML = html;
      })
      .catch(function () {});
  }

  function loadCodeDeps(targetId) {
    var container = el(targetId || "code-deps");
    if (!container) return;
    fetch("/api/brain/code/deps")
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.ok || data.total === 0) {
          container.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No dependency alerts.</div>';
          return;
        }
        var html = '<div style="display:flex;gap:12px;margin-bottom:10px">';
        html +=
          '<div class="brain-kpi"><div class="bk-val" style="color:#ef4444">' +
          (data.critical || 0) +
          '</div><div class="bk-label">Critical</div></div>';
        html +=
          '<div class="brain-kpi"><div class="bk-val" style="color:#eab308">' +
          (data.warn || 0) +
          '</div><div class="bk-label">Warning</div></div>';
        html +=
          '<div class="brain-kpi"><div class="bk-val" style="color:#06b6d4">' +
          (data.info || 0) +
          '</div><div class="bk-label">Info</div></div>';
        html += "</div>";
        html += '<table style="width:100%;font-size:11px;border-collapse:collapse">';
        html += '<tr style="text-align:left;border-bottom:1px solid var(--border)">';
        html +=
          '<th style="padding:4px 8px">Package</th><th style="padding:4px 8px">Current</th><th style="padding:4px 8px">Latest</th><th style="padding:4px 8px">Severity</th><th style="padding:4px 8px">Eco</th></tr>';
        var allAlerts = [].concat(data.alerts.critical || [], data.alerts.warn || [], data.alerts.info || []);
        allAlerts.slice(0, 30).forEach(function (alert) {
          var sevColor =
            alert.severity === "critical" ? "#ef4444" : alert.severity === "warn" ? "#eab308" : "#06b6d4";
          html += '<tr style="border-bottom:1px solid var(--border)">';
          html += '<td style="padding:4px 8px;font-weight:600">' + escHtml(alert.package) + "</td>";
          html += '<td style="padding:4px 8px;font-family:monospace">' + escHtml(alert.current || "?") + "</td>";
          html +=
            '<td style="padding:4px 8px;font-family:monospace;color:#22c55e">' +
            escHtml(alert.latest || "?") +
            "</td>";
          html +=
            '<td style="padding:4px 8px"><span style="color:' +
            sevColor +
            ';font-weight:600;text-transform:uppercase">' +
            escHtml(alert.severity || "") +
            "</span></td>";
          html += '<td style="padding:4px 8px">' + escHtml(alert.ecosystem || "") + "</td>";
          html += "</tr>";
        });
        html += "</table>";
        container.innerHTML = html;
      })
      .catch(function () {});
  }

  function runCodeSearch(useLlm) {
    var input = el("code-search-input");
    var query = input ? input.value.trim() : "";
    if (!query) return;
    var container = el("code-search-results");
    if (!container) return;
    container.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">Searching...</div>';
    var repoId = _currentCodeRepoId();
    fetch("/api/brain/code/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: query, repo_id: repoId, use_llm: !!useLlm }),
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.ok) {
          container.innerHTML = '<div style="font-size:11px;color:#ef4444">Search failed.</div>';
          return;
        }
        var html = "";
        if (data.answer) {
          html +=
            '<div style="background:rgba(139,92,246,0.06);border:1px solid rgba(139,92,246,0.15);border-radius:8px;padding:10px 12px;font-size:11px;color:var(--text);margin-bottom:8px;white-space:pre-wrap">' +
            escHtml(data.answer) +
            "</div>";
        }
        if (data.results && data.results.length) {
          data.results.forEach(function (result) {
            html += '<div style="background:var(--card);border-radius:6px;padding:6px 10px;margin-bottom:4px;font-size:11px">';
            html += '<div style="display:flex;justify-content:space-between;align-items:center">';
            html += '<span style="font-weight:600;color:var(--accent)">' + escHtml(result.symbol) + "</span>";
            html +=
              '<span style="font-size:9px;padding:1px 6px;background:var(--border);border-radius:4px">' +
              escHtml(result.type) +
              "</span>";
            html += "</div>";
            html +=
              '<div style="font-size:10px;color:var(--text-muted);font-family:monospace">' +
              escHtml(result.file) +
              ":" +
              escHtml(result.line) +
              "</div>";
            if (result.signature) {
              html +=
                '<div style="font-size:10px;color:var(--text);font-family:monospace;margin-top:2px">' +
                escHtml(result.signature) +
                "</div>";
            }
            if (result.docstring) {
              html +=
                '<div style="font-size:10px;color:var(--text-muted);font-style:italic;margin-top:2px">' +
                escHtml(result.docstring) +
                "</div>";
            }
            html += "</div>";
          });
        } else if (!data.answer) {
          html += '<div style="font-size:11px;color:var(--text-muted)">No results found.</div>';
        }
        container.innerHTML = html;
      })
      .catch(function () {
        container.innerHTML = '<div style="font-size:11px;color:#ef4444">Search error.</div>';
      });
  }

  function triggerCodeLearn() {
    var btn = el("btn-code-learn");
    if (!btn) return;
    btn.disabled = true;
    btn.innerHTML = "&#x1F4BB; Starting...";
    fetch("/api/brain/code/learn", { method: "POST" })
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (data && data.ok) {
          btn.innerHTML = "&#x1F4BB; Learning...";
          pollCodeLearningStatus();
        } else {
          btn.innerHTML = (data && (data.message || data.error)) || "Already running";
          window.setTimeout(function () {
            btn.disabled = false;
            btn.innerHTML = "&#x1F4BB; Reindex Code";
          }, 3000);
        }
      })
      .catch(function () {
        btn.disabled = false;
        btn.innerHTML = "&#x1F4BB; Reindex Code";
      });
  }

  function pollCodeLearningStatus() {
    var section = el("code-pipeline-section");
    var btn = el("btn-code-learn");
    if (!section || !btn) return;
    fetch("/api/brain/status")
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.ok) return;
        var code = data.code || {};
        var dot = el("project-status-dot");
        var wasRunning = !!section._wasRunning;

        if (code.running) {
          section.style.display = "";
          if (dot) dot.className = "b-domain-dot learning";
          if (el("code-learning-step")) el("code-learning-step").textContent = code.current_step || "";
          if (el("code-learning-phase")) el("code-learning-phase").textContent = code.phase || "";
          var pct =
            code.repos_processed && code.total_steps
              ? Math.round((code.repos_processed / code.total_steps) * 100)
              : 0;
          if (el("code-progress-bar")) el("code-progress-bar").style.width = pct + "%";
          btn.disabled = true;
          btn.innerHTML = "&#x1F4BB; Learning...";
          if (!_codePollTimer) {
            _codePollTimer = window.setInterval(pollCodeLearningStatus, 3000);
          }
        } else {
          section.style.display = "none";
          if (dot) dot.className = "b-domain-dot idle";
          btn.disabled = false;
          btn.innerHTML = "&#x1F4BB; Reindex Code";
          if (_codePollTimer) {
            window.clearInterval(_codePollTimer);
            _codePollTimer = null;
          }
          if (wasRunning) {
            refreshBootstrap();
            if (window._activeProjectAgent === "architect" && typeof window.loadArchDashboard === "function") {
              window.loadArchDashboard();
            } else if (window._activeProjectAgent === "devops" && typeof window._loadGenericDashboard === "function") {
              window._loadGenericDashboard("devops");
            } else if (window._activeProjectAgent === "qa" && typeof window._loadGenericDashboard === "function") {
              window._loadGenericDashboard("qa");
            } else if (window._activeProjectAgent === "security" && typeof window._loadGenericDashboard === "function") {
              window._loadGenericDashboard("security");
            }
          }
        }
        section._wasRunning = !!code.running;
      })
      .catch(function () {});
  }

  function codeAgentOpen() {
    var section = el("code-agent-section");
    if (!section) return;
    section.style.display = section.style.display === "none" ? "" : "none";
    if (section.style.display !== "none" && el("code-agent-prompt")) {
      el("code-agent-prompt").focus();
    }
  }

  function runCodeAgent() {
    var promptEl = el("code-agent-prompt");
    var prompt = promptEl ? promptEl.value.trim() : "";
    if (!prompt) return;
    var btn = el("btn-code-agent");
    var resultDiv = el("code-agent-result");
    if (!btn || !resultDiv) return;
    btn.disabled = true;
    btn.innerHTML = "&#x1F9E0; Thinking...";
    resultDiv.style.display = "";
    resultDiv.innerHTML = '<div class="project-loading-note">Chili is analyzing your request...</div>';

    fetch("/api/brain/code/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: prompt }),
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        btn.disabled = false;
        btn.innerHTML = "&#x1F680; Run Agent";
        if (!data || !data.ok) {
          resultDiv.innerHTML =
            '<div class="project-error-note">' +
            escHtml((data && (data.message || data.error)) || "Error") +
            "</div>";
          return;
        }
        var html = '<div class="code-agent-result-box">';
        if (typeof window.marked !== "undefined") {
          try {
            html += window.marked.parse(data.response);
          } catch (err) {
            html += "<pre>" + escHtml(data.response) + "</pre>";
          }
        } else {
          html += "<pre>" + escHtml(data.response) + "</pre>";
        }
        html += "</div>";

        var ctx = data.context_used || {};
        html +=
          '<div class="code-agent-context-bar">' +
          "<span>Model: " +
          escHtml(data.model || "unknown") +
          "</span>" +
          "<span>Repos: " +
          (ctx.repos || 0) +
          "</span>" +
          "<span>Insights: " +
          (ctx.insights || 0) +
          "</span>" +
          "<span>Files read: " +
          (ctx.file_contents_included || 0) +
          "</span>" +
          "</div>";

        if (data.files_changed && data.files_changed.length > 0) {
          html +=
            '<div class="project-agent-files">Files referenced: ' +
            data.files_changed
              .map(function (file) {
                return "<code>" + escHtml(file) + "</code>";
              })
              .join(", ") +
            "</div>";
        }

        resultDiv.innerHTML = html;
      })
      .catch(function (error) {
        btn.disabled = false;
        btn.innerHTML = "&#x1F680; Run Agent";
        resultDiv.innerHTML = '<div class="project-error-note">' + escHtml(error.message) + "</div>";
      });
  }

  function bindNav() {
    var buttons = document.querySelectorAll("[data-project-pane-target]");
    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        setPane(button.getAttribute("data-project-pane-target") || "workspace");
      });
    });
  }

  function bindSearchInput() {
    var searchInput = el("code-search-input");
    if (!searchInput || searchInput.dataset.boundEnter === "1") return;
    searchInput.dataset.boundEnter = "1";
    searchInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        runCodeSearch();
      }
    });
  }

  function init() {
    bindNav();
    bindSearchInput();
    setPane("workspace");
    refreshBootstrap();
  }

  window.brainProjectDomainInit = init;
  window.brainProjectRefreshBootstrap = refreshBootstrap;
  window.loadCodeDashboard = loadCodeDashboard;
  window.loadAgentMessageFeed = loadAgentMessageFeed;
  window.triggerAllAgentsCycle = triggerAllAgentsCycle;
  window.loadProjectBrainStatus = loadProjectBrainStatus;
  window.loadCodeRepos = loadCodeRepos;
  window.showAddRepoModal = showAddRepoModal;
  window.doAddRepo = doAddRepo;
  window.removeCodeRepo = removeCodeRepo;
  window.loadCodeMetrics = loadCodeMetrics;
  window.loadCodeHotspots = loadCodeHotspots;
  window.loadCodeInsights = loadCodeInsights;
  window.loadCodeGraph = loadCodeGraph;
  window.loadCodeTrends = loadCodeTrends;
  window.loadCodeReviews = loadCodeReviews;
  window.loadCodeDeps = loadCodeDeps;
  window.runCodeSearch = runCodeSearch;
  window.triggerCodeLearn = triggerCodeLearn;
  window.pollCodeLearningStatus = pollCodeLearningStatus;
  window.codeAgentOpen = codeAgentOpen;
  window.runCodeAgent = runCodeAgent;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
