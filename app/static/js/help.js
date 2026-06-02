/* CHILI OS — keyboard shortcuts cheat-sheet. Opens on "?" (Shift+/) or the
   topbar help button; closes on Escape / scrim / ×. Vanilla, no deps. */
(function () {
  var scrim = document.getElementById('ws-help-scrim');
  if (!scrim) return;
  var btn = document.getElementById('ws-help-btn');
  var closeBtn = document.getElementById('ws-help-x');

  var lastFocus = null;
  function open() {
    lastFocus = document.activeElement;
    scrim.classList.add('open');
    if (btn) btn.setAttribute('aria-expanded', 'true');
    if (closeBtn) try { closeBtn.focus(); } catch (e) {}  // move focus into the dialog
  }
  function close() {
    if (!scrim.classList.contains('open')) return;
    scrim.classList.remove('open');
    if (btn) btn.setAttribute('aria-expanded', 'false');
    if (lastFocus && lastFocus.focus) { try { lastFocus.focus(); } catch (e) {} }
    lastFocus = null;
  }
  function toggle() { scrim.classList.contains('open') ? close() : open(); }
  // Trap focus inside the dialog (the close button is the only focusable).
  scrim.addEventListener('keydown', function (e) {
    if (e.key === 'Tab' && scrim.classList.contains('open')) { e.preventDefault(); if (closeBtn) closeBtn.focus(); }
  });

  if (btn) btn.addEventListener('click', function (e) { e.stopPropagation(); toggle(); });
  if (closeBtn) closeBtn.addEventListener('click', close);
  scrim.addEventListener('click', function (e) { if (e.target === scrim) close(); });

  // "?" opens the sheet, unless the user is typing into a field.
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && scrim.classList.contains('open')) { e.preventDefault(); close(); return; }
    if (e.key !== '?') return;
    var t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    e.preventDefault(); toggle();
  });
})();
