/*
 * Runtime tab redesign 2026-05-23 (Phase B): diagnostics drawer controller.
 *
 * Owns the slide-in panel on the right side of the runtime tab. It does NOT
 * fetch data itself — brainRuntimeGates (the IIFE in
 * _trading_runtime_gates.html) populates the tbody ids on its own polling
 * cadence. The drawer just controls visibility and which sub-view (stuck
 * patterns / verdict diff / queue) is active when opened.
 *
 * Wired up to:
 *   - #bx-open-diagnostics-btn   → openBxDiagnosticsDrawer()
 *   - #bx-queue-pill             → openBxDiagnosticsDrawer('queue')
 *   - #bx-pattern-gate-summary   → openBxDiagnosticsDrawer('patterns')
 *   - #bx-queue-tile-*           → openBxDiagnosticsDrawer('queue')
 *   - #bx-diagnostics-drawer-close → closeBxDiagnosticsDrawer()
 *   - Escape / backdrop click    → closeBxDiagnosticsDrawer()
 */
(function () {
  if (window.__bxDiagnosticsDrawerInited) return;
  window.__bxDiagnosticsDrawerInited = true;

  function _root() { return document.getElementById('bx-diagnostics-drawer'); }
  function _backdrop() { return document.getElementById('bx-diagnostics-drawer-backdrop'); }

  function isOpen() {
    var root = _root();
    return !!(root && root.classList.contains('open'));
  }

  function open(focusKind) {
    var root = _root();
    var backdrop = _backdrop();
    if (!root || !backdrop) return;
    root.classList.add('open');
    root.setAttribute('aria-hidden', 'false');
    backdrop.classList.add('open');
    backdrop.setAttribute('aria-hidden', 'false');
    document.body.classList.add('bx-drawer-open');

    if (focusKind === 'patterns' && window.brainRuntimeGates) {
      window.brainRuntimeGates.refreshPatterns();
      window.brainRuntimeGates.switchView('stuck');
    } else if (focusKind === 'queue' && window.brainRuntimeGates) {
      window.brainRuntimeGates.refreshQueueDepth();
    } else if (window.brainRuntimeGates) {
      window.brainRuntimeGates.refreshPatterns();
      window.brainRuntimeGates.refreshQueueDepth();
    }

    var closeBtn = document.getElementById('bx-diagnostics-drawer-close');
    if (closeBtn) try { closeBtn.focus(); } catch (e) {}
  }

  function close() {
    var root = _root();
    var backdrop = _backdrop();
    if (root) {
      root.classList.remove('open');
      root.setAttribute('aria-hidden', 'true');
    }
    if (backdrop) {
      backdrop.classList.remove('open');
      backdrop.setAttribute('aria-hidden', 'true');
    }
    document.body.classList.remove('bx-drawer-open');
  }

  function _onKeydown(e) {
    if (e.key === 'Escape' && isOpen()) {
      e.preventDefault();
      close();
    }
  }

  function _wire() {
    var backdrop = _backdrop();
    if (backdrop && !backdrop._bxWired) {
      backdrop._bxWired = true;
      backdrop.addEventListener('click', function () { close(); });
    }
    var pill = document.getElementById('bx-queue-pill');
    if (pill && !pill._bxWired) {
      pill._bxWired = true;
      pill.addEventListener('click', function () { open('queue'); });
    }
    document.addEventListener('keydown', _onKeydown);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _wire);
  } else {
    _wire();
  }

  window.openBxDiagnosticsDrawer = open;
  window.closeBxDiagnosticsDrawer = close;
})();
