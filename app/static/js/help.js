/* CHILI OS — keyboard shortcuts cheat-sheet. Opens on "?" (Shift+/) or the
   topbar help button; closes on Escape / scrim / ×. Vanilla, no deps. */
(function () {
  var scrim = document.getElementById('ws-help-scrim');
  if (!scrim) return;
  var btn = document.getElementById('ws-help-btn');
  var closeBtn = document.getElementById('ws-help-x');

  function open() { scrim.classList.add('open'); if (btn) btn.setAttribute('aria-expanded', 'true'); }
  function close() { scrim.classList.remove('open'); if (btn) btn.setAttribute('aria-expanded', 'false'); }
  function toggle() { scrim.classList.contains('open') ? close() : open(); }

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
