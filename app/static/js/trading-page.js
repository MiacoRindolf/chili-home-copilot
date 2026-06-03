(function() {
  var version = '20260603a';
  var chunks = [
    '/static/js/trading-page.1.js?v=' + version,
    '/static/js/trading-page.2.js?v=' + version,
    '/static/js/trading-page.3.js?v=' + version,
    '/static/js/trading-page.4.js?v=' + version
  ];
  window._chiliTradingDeferInit = true;

  function loadNext(index) {
    if (index >= chunks.length) {
      window._chiliTradingDeferInit = false;
      if (typeof window._chiliTradingInit === 'function') {
        window._chiliTradingInit();
      }
      return;
    }
    var script = document.createElement('script');
    script.src = chunks[index];
    script.onload = function() { loadNext(index + 1); };
    script.onerror = function() {
      window._chiliTradingDeferInit = false;
      console.error('[CHILI trading] failed to load script chunk:', chunks[index]);
    };
    document.head.appendChild(script);
  }

  loadNext(0);
})();
