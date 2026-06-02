/* CHILI OS notifications — surfaces meaningful changes in the trading brain as
   toasts, by diffing successive polls of /api/workspace/desktop. Frontend-only:
   reuses the existing cockpit endpoint (no new backend). The first poll seeds a
   baseline (no toasts); subsequent polls toast on safety flips, market
   open/close, and newly-closed trades. Pauses while the tab is hidden. */
(function () {
  var container = document.getElementById('ws-toasts');
  if (!container) return;

  var prev = null, seq = 0, reduce = false;
  try { reduce = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (e) {}

  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

  function toast(o) {
    var t = document.createElement('div');
    t.className = 'ws-toast ' + (o.kind || 'info');
    t.setAttribute('role', 'status');
    t.innerHTML =
      '<span class="wt-ic">' + esc(o.icon || '•') + '</span>' +
      '<div class="wt-body"><div class="wt-title">' + esc(o.title) + '</div>' +
      (o.body ? '<div class="wt-sub">' + esc(o.body) + '</div>' : '') + '</div>' +
      '<button class="wt-x" aria-label="Dismiss">×</button>';
    container.appendChild(t);
    while (container.children.length > 4) container.removeChild(container.firstChild);  // cap the stack
    var killed = false;
    var dismiss = function () {
      if (killed) return; killed = true;
      t.classList.add('out');
      setTimeout(function () { if (t.parentNode) t.remove(); }, reduce ? 0 : 220);
    };
    t.querySelector('.wt-x').addEventListener('click', dismiss);
    setTimeout(dismiss, 6500);
  }

  function diff(cur) {
    if (!cur || !cur.ok) return;
    if (prev) {
      var pk = prev.kill_switch || {}, ck = cur.kill_switch || {};
      if (ck.ok && pk.ok && ck.active !== pk.active) {
        toast(ck.active
          ? { kind: 'bad', icon: '⛔', title: 'Kill switch engaged', body: 'Automated trading halted' }
          : { kind: 'ok', icon: '✓', title: 'Kill switch cleared' });
      }
      var pb = prev.breaker || {}, cb = cur.breaker || {};
      if (cb.ok && pb.ok && cb.tripped !== pb.tripped) {
        toast(cb.tripped
          ? { kind: 'bad', icon: '⛔', title: 'Drawdown breaker tripped', body: cb.reason || 'Trading blocked until reset' }
          : { kind: 'ok', icon: '✓', title: 'Drawdown breaker reset' });
      }
      var pm = prev.market || {}, cm = cur.market || {};
      if (cm.ok && pm.ok && cm.equities_open != null && pm.equities_open != null && cm.equities_open !== pm.equities_open) {
        toast(cm.equities_open
          ? { kind: 'ok', icon: '●', title: 'Market open', body: 'US equities regular session' }
          : { kind: 'info', icon: '○', title: 'Market closed', body: 'US equities' });
      }
      var pc = (typeof prev.closes_today === 'number') ? prev.closes_today : null;
      var cc = (typeof cur.closes_today === 'number') ? cur.closes_today : null;
      if (pc != null && cc != null && cc > pc) {
        var k = cc - pc, rep = (cur.closes || [])[0] || null;
        toast({
          kind: rep ? (rep.pnl_up ? 'ok' : 'bad') : 'info',
          icon: '📈',
          title: k + ' trade' + (k === 1 ? '' : 's') + ' closed',
          body: rep ? (rep.ticker + (rep.pnl_fmt ? ' · ' + rep.pnl_fmt : '')) : ''
        });
      }
    }
    prev = cur;
  }

  function poll() {
    if (document.hidden) return;
    var my = ++seq;
    fetch('/api/workspace/desktop', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (my === seq) diff(d); })
      .catch(function () {});
  }
  poll();
  setInterval(poll, 20000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
})();
