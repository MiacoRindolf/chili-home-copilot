/**
 * Reusable modal component for CHILI.
 * Usage:
 *   var m = ChiliModal.create({ title: 'My Modal', onClose: fn });
 *   m.setBody(htmlString);
 *   m.open();
 *   m.close();
 */
var ChiliModal = (function() {
  'use strict';

  function create(opts) {
    opts = opts || {};
    var overlay = document.createElement('div');
    overlay.className = 'chili-modal-overlay';
    overlay.style.cssText = 'display:none;position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.55);align-items:center;justify-content:center;';

    var dialog = document.createElement('div');
    dialog.className = 'chili-modal-dialog';
    dialog.style.cssText = 'background:var(--bg-header);color:var(--text);border-radius:12px;padding:0;max-width:' + (opts.maxWidth || '480px') + ';width:90%;box-shadow:0 8px 32px rgba(0,0,0,.3);max-height:85vh;display:flex;flex-direction:column;';

    var header = document.createElement('div');
    header.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--border);flex-shrink:0;';

    var titleEl = document.createElement('h3');
    titleEl.style.cssText = 'margin:0;font-size:1.05rem;';
    titleEl.textContent = opts.title || '';

    var closeBtn = document.createElement('button');
    closeBtn.style.cssText = 'background:none;border:none;font-size:1.4rem;cursor:pointer;color:var(--text-secondary);padding:0 4px;';
    closeBtn.innerHTML = '&times;';
    closeBtn.onclick = function() { close(); };

    header.appendChild(titleEl);
    header.appendChild(closeBtn);

    var body = document.createElement('div');
    body.className = 'chili-modal-body';
    body.style.cssText = 'padding:16px 20px;overflow-y:auto;flex:1;';

    dialog.appendChild(header);
    dialog.appendChild(body);
    overlay.appendChild(dialog);

    overlay.addEventListener('click', function(e) {
      if (e.target === overlay) close();
    });

    document.body.appendChild(overlay);

    function open() {
      overlay.style.display = 'flex';
    }

    function close() {
      overlay.style.display = 'none';
      if (opts.onClose) opts.onClose();
    }

    function setTitle(t) { titleEl.textContent = t; }
    function setBody(html) { body.innerHTML = html; }
    function getBody() { return body; }
    function destroy() { overlay.remove(); }

    return { open: open, close: close, setTitle: setTitle, setBody: setBody, getBody: getBody, destroy: destroy, el: overlay };
  }

  return { create: create };
})();
