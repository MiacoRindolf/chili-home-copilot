/*
 * Brain Component Library – JS behaviors
 * Depends on brain-components.css.
 */

/* ── ConfirmModal ── */
var _brainConfirmResolve = null;

function brainConfirm(opts) {
  opts = opts || {};
  var overlay = document.getElementById('brain-confirm-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'brain-confirm-overlay';
    overlay.className = 'confirm-modal-overlay';
    overlay.innerHTML =
      '<div class="confirm-modal">' +
        '<div class="confirm-modal-title" id="brain-confirm-title"></div>' +
        '<div class="confirm-modal-body" id="brain-confirm-body"></div>' +
        '<div class="confirm-modal-actions">' +
          '<button class="confirm-modal-btn cancel" id="brain-confirm-cancel">Cancel</button>' +
          '<button class="confirm-modal-btn" id="brain-confirm-ok">Confirm</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(overlay);
    overlay.addEventListener('click', function(e) { if (e.target === overlay) _brainConfirmClose(false); });
    document.getElementById('brain-confirm-cancel').onclick = function() { _brainConfirmClose(false); };
    document.getElementById('brain-confirm-ok').onclick = function() { _brainConfirmClose(true); };
  }
  document.getElementById('brain-confirm-title').textContent = opts.title || 'Confirm';
  document.getElementById('brain-confirm-body').innerHTML = opts.body || 'Are you sure?';
  var okBtn = document.getElementById('brain-confirm-ok');
  okBtn.textContent = opts.confirmLabel || 'Confirm';
  okBtn.className = 'confirm-modal-btn ' + (opts.variant || 'primary');
  overlay.classList.add('visible');

  return new Promise(function(resolve) { _brainConfirmResolve = resolve; });
}

function _brainConfirmClose(result) {
  var overlay = document.getElementById('brain-confirm-overlay');
  if (overlay) overlay.classList.remove('visible');
  if (_brainConfirmResolve) {
    _brainConfirmResolve(result);
    _brainConfirmResolve = null;
  }
}

/* ── Toast ── */
var _toastContainer = null;

function brainToast(opts) {
  opts = opts || {};
  if (!_toastContainer) {
    _toastContainer = document.createElement('div');
    _toastContainer.className = 'toast-container';
    document.body.appendChild(_toastContainer);
  }
  var icons = { success: '\u2705', error: '\u274C', warning: '\u26A0\uFE0F', info: '\u2139\uFE0F' };
  var type = opts.type || 'info';
  var toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.innerHTML =
    '<span class="toast-icon">' + (icons[type] || '') + '</span>' +
    '<div class="toast-body">' +
      (opts.title ? '<div class="toast-title">' + opts.title + '</div>' : '') +
      (opts.message ? '<div class="toast-message">' + opts.message + '</div>' : '') +
    '</div>' +
    '<button class="toast-close">&times;</button>';
  toast.querySelector('.toast-close').onclick = function() { _dismissToast(toast); };
  _toastContainer.appendChild(toast);

  var duration = opts.duration != null ? opts.duration : 4000;
  if (duration > 0) {
    setTimeout(function() { _dismissToast(toast); }, duration);
  }
  return toast;
}

function _dismissToast(toast) {
  if (!toast || toast.classList.contains('exiting')) return;
  toast.classList.add('exiting');
  setTimeout(function() { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 200);
}

/* ── SectionPanel toggle ── */
function toggleSectionPanel(header) {
  var panel = header.closest('.section-panel');
  if (panel && panel.classList.contains('collapsible')) {
    panel.classList.toggle('collapsed');
  }
}

/* ── LoadingSkeleton helpers ── */
function brainSkeletonCards(container, count) {
  count = count || 3;
  var html = '';
  for (var i = 0; i < count; i++) {
    html += '<div class="skeleton skeleton-card" style="margin-bottom:8px"></div>';
  }
  if (typeof container === 'string') container = document.getElementById(container);
  if (container) container.innerHTML = html;
}

function brainSkeletonText(container, lines) {
  lines = lines || 4;
  var html = '';
  for (var i = 0; i < lines; i++) {
    html += '<div class="skeleton skeleton-text"></div>';
  }
  if (typeof container === 'string') container = document.getElementById(container);
  if (container) container.innerHTML = html;
}

/* ── EmptyState helper ── */
function brainEmptyState(container, opts) {
  opts = opts || {};
  var html =
    '<div class="empty-state">' +
      '<div class="empty-state-icon">' + (opts.icon || '\uD83D\uDCED') + '</div>' +
      '<div class="empty-state-title">' + (opts.title || 'No data') + '</div>' +
      '<div class="empty-state-desc">' + (opts.desc || '') + '</div>' +
      (opts.action ? '<div class="empty-state-action">' + opts.action + '</div>' : '') +
    '</div>';
  if (typeof container === 'string') container = document.getElementById(container);
  if (container) container.innerHTML = html;
}

/* ── Badge helper ── */
function brainBadge(text, variant) {
  variant = variant || 'muted';
  return '<span class="badge badge-' + variant + '">' + text + '</span>';
}

function brainBadgeDot(text, variant) {
  variant = variant || 'muted';
  return '<span class="badge badge-' + variant + '"><span class="badge-dot"></span> ' + text + '</span>';
}

/* ── Global fetch wrapper with error handling ── */
function brainFetch(url, opts) {
  opts = opts || {};
  return fetch(url, Object.assign({ credentials: 'same-origin' }, opts))
    .then(function(r) {
      if (!r.ok) {
        return r.text().then(function(txt) {
          var msg = 'HTTP ' + r.status;
          try { var j = JSON.parse(txt); msg = j.detail || j.error || msg; } catch(e) {}
          throw new Error(msg);
        });
      }
      return r.json();
    })
    .catch(function(err) {
      if (opts.silent) throw err;
      brainToast({
        type: 'error',
        title: opts.errorTitle || 'Request failed',
        message: err.message || 'Network error'
      });
      throw err;
    });
}

/* ── Loading state helpers ── */
/* ── Keyboard navigation for section nav ── */
document.addEventListener('keydown', function(e) {
  var nav = document.getElementById('brain-section-nav');
  if (!nav || !nav.contains(document.activeElement)) return;
  var btns = Array.prototype.slice.call(nav.querySelectorAll('.brain-nav-btn'));
  var idx = btns.indexOf(document.activeElement);
  if (idx < 0) return;
  var next = -1;
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (idx + 1) % btns.length;
  if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (idx - 1 + btns.length) % btns.length;
  if (e.key === 'Home') next = 0;
  if (e.key === 'End') next = btns.length - 1;
  if (next >= 0) {
    e.preventDefault();
    btns[next].focus();
    btns[next].click();
  }
});

/* ── Focus trap for confirm modal ── */
document.addEventListener('keydown', function(e) {
  var overlay = document.getElementById('brain-confirm-overlay');
  if (!overlay || !overlay.classList.contains('visible')) return;
  if (e.key === 'Escape') { _brainConfirmClose(false); return; }
  if (e.key !== 'Tab') return;
  var focusable = overlay.querySelectorAll('button, [tabindex]:not([tabindex="-1"])');
  if (!focusable.length) return;
  var first = focusable[0];
  var last = focusable[focusable.length - 1];
  if (e.shiftKey) {
    if (document.activeElement === first) { e.preventDefault(); last.focus(); }
  } else {
    if (document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});

function brainSetLoading(containerId, loading) {
  var el = typeof containerId === 'string' ? document.getElementById(containerId) : containerId;
  if (!el) return;
  if (loading) {
    if (!el.dataset.originalContent) el.dataset.originalContent = el.innerHTML;
    brainSkeletonText(el, 3);
  } else if (el.dataset.originalContent) {
    el.innerHTML = el.dataset.originalContent;
    delete el.dataset.originalContent;
  }
}
