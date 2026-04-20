/* Shared HTTP client for the /brain?domain=project cockpit.

   All three brain-project-*.js files should use this to fetch JSON so error
   handling is consistent: non-2xx becomes a rejected promise, network errors
   surface a non-blocking toast, and the caller decides whether to degrade
   the UI or retry.

   Intentionally exposed as window.BrainProjectClient so existing files can
   migrate site-by-site without bundler changes. */
(function () {
  if (window.BrainProjectClient) return;

  function ensureToastRoot() {
    var el = document.getElementById('brain-project-toast-root');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'brain-project-toast-root';
    el.style.cssText =
      'position:fixed;right:12px;bottom:12px;z-index:9999;display:flex;' +
      'flex-direction:column;gap:6px;pointer-events:none;';
    document.body && document.body.appendChild(el);
    return el;
  }

  function toast(label, message) {
    try {
      var root = ensureToastRoot();
      if (!root) return;
      var node = document.createElement('div');
      node.style.cssText =
        'background:var(--error-bg,#4a1414);color:var(--error,#ffb3b3);' +
        'border:1px solid var(--error,#ff6b6b);padding:8px 12px;border-radius:6px;' +
        'font-size:12px;max-width:320px;box-shadow:0 2px 8px rgba(0,0,0,0.4);' +
        'pointer-events:auto;';
      node.textContent = '\u26A0 ' + label + ': ' + (message || 'request failed');
      root.appendChild(node);
      setTimeout(function () {
        try { node.remove(); } catch (_) {}
      }, 6000);
    } catch (_) { /* last-resort UI — swallow */ }
  }

  function onErr(label) {
    return function (err) {
      var msg = (err && err.message) ? err.message : String(err || 'network error');
      try { console.warn('[brain-project-client] ' + label + ':', err); } catch (_) {}
      toast(label, msg);
    };
  }

  function fetchJson(url, opts, label) {
    return fetch(url, opts || {}).then(function (r) {
      if (!r.ok) {
        var err = new Error('HTTP ' + r.status);
        err.status = r.status;
        err.url = url;
        throw err;
      }
      return r.json();
    });
  }

  function get(url, label) {
    return fetchJson(url, undefined, label || url);
  }

  function post(url, body, label) {
    var opts = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    };
    if (body !== undefined && body !== null) opts.body = JSON.stringify(body);
    return fetchJson(url, opts, label || url);
  }

  /* withErrorUi: wraps a fetch promise with a default .catch that surfaces
     the error. Callers that want to also run recovery logic can chain an
     additional .catch AFTER this one (the toast will have already fired). */
  function withErrorUi(promise, label) {
    return promise.catch(function (err) {
      onErr(label)(err);
      throw err;  // re-raise so app-level .catch still fires
    });
  }

  window.BrainProjectClient = {
    fetchJson: fetchJson,
    get: get,
    post: post,
    onErr: onErr,
    toast: toast,
    withErrorUi: withErrorUi,
  };

  /* B4: single namespaced object for cross-file project-domain state.
     Existing files still set ``window._agentList`` etc. directly — we expose
     the same slots here so new code can migrate incrementally without a
     big-bang rename. Each property is a getter/setter that reads from or
     writes to the legacy window global underneath. */
  function _mirror(name, defaultValue) {
    if (typeof window[name] === 'undefined') window[name] = defaultValue;
    return {
      get: function () { return window[name]; },
      set: function (v) { window[name] = v; },
    };
  }

  if (!window.BrainProject) {
    window.BrainProject = {};
    Object.defineProperty(window.BrainProject, 'agentList', _mirror('_agentList', []));
    Object.defineProperty(window.BrainProject, 'activeAgent', _mirror('_activeAgent', 'product_owner'));
    Object.defineProperty(window.BrainProject, 'activeProjectAgent', _mirror('_activeProjectAgent', 'product_owner'));
    Object.defineProperty(window.BrainProject, 'agentPanelLoaded', _mirror('_agentPanelLoaded', {}));
    Object.defineProperty(window.BrainProject, 'activeDomain', _mirror('_activeDomain', 'hub'));
    /* Helpers live on the namespace directly (no mirror needed — callers set
       via window.brainHandoffPlannerTaskId today). */
    window.BrainProject.plannerTaskHint = function () {
      return (typeof window.brainHandoffPlannerTaskId === 'function')
        ? window.brainHandoffPlannerTaskId()
        : null;
    };
  }
})();
