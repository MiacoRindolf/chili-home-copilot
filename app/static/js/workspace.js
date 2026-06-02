/* CHILI Workspace shell — theme, command palette, rail.
   Vanilla, no deps. Dark-first: defaults to dark on first visit (no stored pref),
   but respects + persists the user's choice via the shared 'chili-theme' key. */
(function () {
  var root = document.documentElement;

  // Dark-first default for the workspace (visual only — don't persist unless toggled).
  if (!localStorage.getItem('chili-theme')) {
    root.setAttribute('data-theme', 'dark');
  }

  var toggle = document.getElementById('ws-theme-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      localStorage.setItem('chili-theme', next);
    });
  }

  // Command palette
  var scrim = document.getElementById('ws-scrim');
  var input = document.getElementById('ws-palette-in');
  var trigger = document.getElementById('ws-cmdk-trigger');
  function openPalette() {
    if (!scrim) return;
    scrim.classList.add('open');
    setTimeout(function () { if (input) { input.value = ''; input.focus(); filter(''); } }, 20);
  }
  function closePalette() { if (scrim) scrim.classList.remove('open'); }
  if (trigger) trigger.addEventListener('click', openPalette);
  if (scrim) scrim.addEventListener('click', function (e) { if (e.target === scrim) closePalette(); });

  // Filter options as the user types; keep arrow-key navigation simple.
  var opts = scrim ? Array.prototype.slice.call(scrim.querySelectorAll('.opt')) : [];
  function filter(q) {
    q = (q || '').toLowerCase().trim();
    var firstVisible = null;
    opts.forEach(function (o) {
      var hit = o.textContent.toLowerCase().indexOf(q) !== -1;
      o.style.display = hit ? '' : 'none';
      o.classList.remove('sel');
      if (hit && !firstVisible) firstVisible = o;
    });
    if (firstVisible) firstVisible.classList.add('sel');
  }
  if (input) {
    input.addEventListener('input', function () { filter(input.value); });
    input.addEventListener('keydown', function (e) {
      var visible = opts.filter(function (o) { return o.style.display !== 'none'; });
      var idx = visible.findIndex(function (o) { return o.classList.contains('sel'); });
      if (e.key === 'ArrowDown') { e.preventDefault(); move(visible, idx, 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(visible, idx, -1); }
      else if (e.key === 'Enter') {
        e.preventDefault();
        var sel = visible[idx] || visible[0];
        if (sel) sel.click();
      }
    });
  }
  function move(list, idx, dir) {
    if (!list.length) return;
    if (idx >= 0) list[idx].classList.remove('sel');
    var next = (idx + dir + list.length) % list.length;
    list[next].classList.add('sel');
    list[next].scrollIntoView({ block: 'nearest' });
  }

  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); }
    else if (e.key === 'Escape') closePalette();
  });
})();
