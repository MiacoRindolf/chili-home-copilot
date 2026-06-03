/* ── Smart price formatting ────────────────────── */
function smartPrice(v, isCrypto) {
  if (v == null || isNaN(v)) return '--';
  var abs = Math.abs(v);
  var d;
  if (isCrypto) {
    d = abs >= 100 ? 2 : abs >= 1 ? 6 : abs >= 0.01 ? 6 : abs >= 0.0001 ? 8 : 10;
  } else {
    d = abs >= 1 ? 2 : abs >= 0.01 ? 4 : abs >= 0.0001 ? 6 : 8;
  }
  var s = v.toFixed(d);
  if (isCrypto && d > 2) s = s.replace(/0+$/, '').replace(/\.$/, '.00');
  return s;
}
function smartMinMove(price) {
  if (!price || price >= 1) return 0.01;
  if (price >= 0.01) return 0.0001;
  if (price >= 0.0001) return 0.000001;
  return 0.00000001;
}

function _isCryptoTickerUi(t) {
  return (t || '').indexOf('-USD') !== -1;
}

/** When /api/trading/quote has no price, show last OHLCV close (same as chart). */
function _paintToolbarPriceFromOhlcvFallback() {
  var lbl = document.getElementById('price-label');
  if (!lbl || !_rawOhlcData || !_rawOhlcData.length) return false;
  if (_ohlcvBarsTicker !== currentTicker) return false;
  var last = _rawOhlcData[_rawOhlcData.length - 1];
  if (!last || last.close == null || isNaN(Number(last.close))) return false;
  var prevClose = _rawOhlcData.length >= 2 ? _rawOhlcData[_rawOhlcData.length - 2].close : last.close;
  var c = Number(last.close);
  var p = Number(prevClose);
  var _isCr = _isCryptoTickerUi(currentTicker);
  var chg = c - p;
  var pct = p ? (chg / p * 100) : 0;
  var cls = chg >= 0 ? 'up' : 'down';
  var sign = chg >= 0 ? '+' : '';
  var pStr = smartPrice(c, _isCr);
  lbl.innerHTML = '<strong>$' + pStr + '</strong> <span class="change ' + cls + '">' + sign + smartPrice(chg, _isCr) + ' (' + sign + (p ? pct.toFixed(2) : '0.00') + '%)</span>';
  if (isFinite(c)) _lastLiveDisplayedPrice = c;
  return true;
}

/* ── Mobile drawer management ──────────────────── */
function toggleSidebar() {
  var sidebar = document.querySelector('.t-sidebar');
  var backdrop = document.getElementById('drawer-backdrop');
  var isOpen = sidebar.classList.toggle('open');
  if (isOpen) {
    document.getElementById('ai-panel').classList.remove('open');
    if (backdrop) backdrop.classList.add('open');
  } else {
    if (backdrop) backdrop.classList.remove('open');
  }
}

function _positionAiPanelNearFab() {
  var panel = document.getElementById('ai-panel');
  var fab = document.getElementById('fab-ai');
  if (!panel || !fab) return;
  if (window.innerWidth <= 768) return;
  var rect = fab.getBoundingClientRect();
  // Use stable target size so we don't depend heavily on layout timing
  var panelWidth = Math.min(400, window.innerWidth - 40);
  var panelHeight = Math.min(520, window.innerHeight - 80);

  // Place panel horizontally centered on the FAB
  var left = rect.left + rect.width / 2 - panelWidth / 2;

  // Vertically, center the panel around the FAB so it \"sticks\" near it
  var top = rect.top + rect.height / 2 - panelHeight / 2;

  // Clamp to viewport
  if (left < 10) left = 10;
  if (left + panelWidth > window.innerWidth - 10) {
    left = window.innerWidth - panelWidth - 10;
  }
  if (top < 10) top = 10;
  if (top + panelHeight > window.innerHeight - 10) {
    top = window.innerHeight - panelHeight - 10;
  }
  if (top < 10) top = 10;
  panel.style.left = left + 'px';
  panel.style.top = top + 'px';
  panel.style.right = 'auto';
  panel.style.bottom = 'auto';
}

function toggleAiPanel() {
  var panel = document.getElementById('ai-panel');
  var backdrop = document.getElementById('drawer-backdrop');
  var fab = document.getElementById('fab-ai');
  var isOpen = panel.classList.toggle('open');
  if (isOpen) {
    document.querySelector('.t-sidebar').classList.remove('open');
    if (backdrop) backdrop.classList.add('open');
    _positionAiPanelNearFab();
    fab.style.display = 'none';
  } else {
    if (backdrop) backdrop.classList.remove('open');
    // When closing, move the FAB to the panel's last position so open/close feels seamless
    if (window.innerWidth > 768 && fab && panel) {
      var rect = panel.getBoundingClientRect();
      var right = Math.max(10, window.innerWidth - rect.right - 10);
      var bottom = Math.max(10, window.innerHeight - rect.bottom - 10);
      fab.style.right = right + 'px';
      fab.style.bottom = bottom + 'px';
    }
    fab.style.display = 'flex';
  }
  setTimeout(function(){ window.dispatchEvent(new Event('resize')); }, 300);
}

function closeAllDrawers() {
  document.querySelector('.t-sidebar').classList.remove('open');
  document.getElementById('ai-panel').classList.remove('open');
  var backdrop = document.getElementById('drawer-backdrop');
  if (backdrop) backdrop.classList.remove('open');
  document.getElementById('fab-ai').style.display = 'flex';
}

/* ── AI Panel resize drag ──────────────────────── */
(function() {
  var handle = document.getElementById('ai-resize-handle');
  var panel = document.getElementById('ai-panel');
  if (!handle || !panel) return;
  var dragging = false;
  handle.addEventListener('mousedown', function(e) {
    e.preventDefault();
    dragging = true;
    panel.style.transition = 'none';
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });
  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    var rect = panel.getBoundingClientRect();
    var newWidth = rect.right - e.clientX;
    if (newWidth < 280) newWidth = 280;
    if (newWidth > 700) newWidth = 700;
    panel.style.width = newWidth + 'px';
  });
  document.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false;
    panel.style.transition = '';
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    window.dispatchEvent(new Event('resize'));
  });
})();

/* ── AI Panel drag (free move on desktop) ───────── */
(function() {
  var panel = document.getElementById('ai-panel');
  var header = panel ? panel.querySelector('.t-ai-view-tabs') : null;
  if (!panel || !header) return;
  var dragging = false;
  var startX = 0, startY = 0;
  var startLeft = 0, startTop = 0;
  header.addEventListener('mousedown', function(e) {
    if (e.target.classList.contains('close-ai')) return;
    if (window.innerWidth <= 768) return; // keep mobile drawer behavior
    dragging = true;
    startX = e.clientX;
    startY = e.clientY;
    startLeft = panel.offsetLeft;
    startTop = panel.offsetTop;
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'move';
  });
  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    var dx = e.clientX - startX;
    var dy = e.clientY - startY;
    var newLeft = startLeft + dx;
    var newTop = startTop + dy;
    var margin = 10;
    var maxLeft = window.innerWidth - panel.offsetWidth - margin;
    var maxTop = window.innerHeight - panel.offsetHeight - margin;
    if (maxTop < margin) maxTop = margin;
    newLeft = Math.max(margin, Math.min(maxLeft, newLeft));
    newTop = Math.max(margin, Math.min(maxTop, newTop));
    panel.style.left = newLeft + 'px';
    panel.style.top = newTop + 'px';
    panel.style.right = 'auto';
    panel.style.bottom = 'auto';
  });
  document.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false;
    document.body.style.userSelect = '';
    document.body.style.cursor = '';
  });
})();

/* ── AI Chat autoscroll / follow ───────────────── */
var _aiAutoScroll = true;
function _aiScrollToBottom() {
  if (!_aiAutoScroll) return;
  var msgs = document.getElementById('ai-msgs');
  if (msgs) msgs.scrollTop = msgs.scrollHeight;
}
function _aiResumeFollow() {
  _aiAutoScroll = true;
  var msgs = document.getElementById('ai-msgs');
  if (msgs) msgs.scrollTop = msgs.scrollHeight;
  var btn = document.getElementById('ai-follow-btn');
  if (btn) btn.classList.remove('visible');
}
(function() {
  var msgs = document.getElementById('ai-msgs');
  var btn = document.getElementById('ai-follow-btn');
  if (!msgs || !btn) return;
  var threshold = 60;
  msgs.addEventListener('scroll', function() {
    var atBottom = msgs.scrollTop + msgs.clientHeight >= msgs.scrollHeight - threshold;
    if (atBottom) {
      _aiAutoScroll = true;
      btn.classList.remove('visible');
    } else {
      _aiAutoScroll = false;
      btn.classList.add('visible');
    }
  });
})();

/* ── Draggable AI FAB ───────────────────────────── */
(function() {
  var fab = document.getElementById('fab-ai');
  if (!fab) return;
  var dragging = false;
  var startX = 0, startY = 0;
  var startRight = 0, startBottom = 0;
   var moved = false;

  fab.addEventListener('mousedown', function(e) {
    dragging = true;
    moved = false;
    fab.classList.add('dragging');
    startX = e.clientX;
    startY = e.clientY;
    var rect = fab.getBoundingClientRect();
    startRight = window.innerWidth - rect.right;
    startBottom = window.innerHeight - rect.bottom;
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    var dx = e.clientX - startX;
    var dy = e.clientY - startY;
    if (!moved && (Math.abs(dx) > 3 || Math.abs(dy) > 3)) moved = true;
    var newRight = startRight - dx;
    var newBottom = startBottom - dy;
    if (newRight < 0) newRight = 0;
    if (newBottom < 0) newBottom = 0;
    if (newRight > window.innerWidth - 40) newRight = window.innerWidth - 40;
    if (newBottom > window.innerHeight - 40) newBottom = window.innerHeight - 40;
    fab.style.right = newRight + 'px';
    fab.style.bottom = newBottom + 'px';
  });

  document.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false;
    fab.classList.remove('dragging');
    document.body.style.userSelect = '';
  });

  // Distinguish between click vs drag: only open panel when there was no movement
  fab.addEventListener('click', function(e) {
    if (moved) {
      // this click came from a drag-release; swallow it
      e.preventDefault();
      e.stopPropagation();
      moved = false;
      return;
    }
    toggleAiPanel();
  });
})();

window.addEventListener('resize', function() {
  if (window.innerWidth > 768) {
    document.querySelector('.t-sidebar').classList.remove('open');
    var backdrop = document.getElementById('drawer-backdrop');
    if (backdrop) backdrop.classList.remove('open');
    var fab = document.getElementById('fab-ai');
    var panel = document.getElementById('ai-panel');
    if (fab) {
      fab.style.display = (panel && panel.classList.contains('open')) ? 'none' : 'flex';
    }
  }
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeAllDrawers();
});

/* ── State ──────────────────────────────────────── */
var currentTicker = '';  // set dynamically from watchlist/brain
var currentInterval = '1d';
/* Default chart range: 1y loads much faster than max (~20y daily + many provider pages). */
var currentPeriod = '1y';
var currentChartType = 'candles';
var chart = null;
var candleSeries = null;
var volumeSeries = null;
var indicatorSeries = {};
var activeIndicators = new Set();
var _chartAnnotations = [];
var _savedAnnotationSpecs = [];
var _lastBreakoutData = null;
var _activeBreakoutRow = null;
var _magnetEnabled = false;
var _drawUndoStack = [];
var _rawOhlcData = null;
/** Ticker that *_rawOhlcData* belongs to (avoids fallback label from wrong symbol). */
var _ohlcvBarsTicker = null;
var _lastCrosshairPrice = null;
var _ctxMenuPrice = null;
var CHART_TEMPLATE_STORAGE_KEY = 'chili_chart_template_v1';
var USER_PRICE_ALERTS_KEY = 'chili_user_price_alerts_v2';
var USER_ALERT_LINES_VISIBLE_KEY = 'chili_user_alert_lines_visible_v1';
var _userPriceAlerts = [];
var _userAlertLayoutRAF = null;
var _userAlertDragState = null;
var _chartAlertCtxTargetId = '';
var _chartAlertCtxDocBound = false;
var _compareSeries = {};  // ticker -> {series, data}
var _lastDaytradeData = null;
var _cachedScanTickers = [];
var _multiViewActive = false;
var _multiCharts = {};
var _multiTimeframes = ['1wk', '1d', '1h', '15m'];
var _multiPeriods = {'1wk':'1y', '1d':'1y', '1h':'3mo', '15m':'1mo'};

var INDICATORS = [
  {id:'ema_20',label:'EMA 20',color:'#8b5cf6'},
  {id:'sma_50',label:'SMA 50',color:'#06b6d4'},
  {id:'ema_100',label:'EMA 100',color:'#f43f5e'},
  {id:'ema_200',label:'EMA 200',color:'#a855f7'},
  {id:'rsi',label:'RSI',color:'#22c55e',pane:true},
  {id:'macd',label:'MACD',color:'#3b82f6',pane:true},
  {id:'adx',label:'ADX',color:'#a855f7',pane:true},
];

/* ── Init ───────────────────────────────────────── */
var btChart = null;
var btEquitySeries = null;

function _chiliTradingInit() {
  if (window._chiliTradingInitDone) return;
  window._chiliTradingInitDone = true;
  _restorePaneSlots();
  initTabOrder();
  initChart();
  _initDrawingListeners();
  _initChartContextMenu();
  _initChartAlertModal();
  _initChartAlertCtxMenu();
  _initPaneDrag();
  buildIndicatorPanel();
  _syncIntervalUi(currentInterval);
  loadStrategies().then(function() {
    var _urlParams = new URLSearchParams(window.location.search);
    if (_urlParams.get('tab') !== 'backtest') return;
    var _dlTicker = _urlParams.get('ticker');
    var _dlStrategy = _urlParams.get('strategy');
    var _dlPeriod = _urlParams.get('period');
    var _dlPattern = _urlParams.get('pattern_id');
    var _dlScanPattern = _urlParams.get('scan_pattern_id');
    setTimeout(function() {
      if (_dlTicker) selectTicker(_dlTicker);
      switchTabByName('backtest');
      setTimeout(function() {
        var sel = document.getElementById('bt-strategy');
        if (sel) {
          if (_dlScanPattern) {
            var targetSp = 'scan_pattern:' + _dlScanPattern;
            var foundSp = false;
            for (var si = 0; si < sel.options.length; si++) {
              if (sel.options[si].value === targetSp) { sel.value = targetSp; foundSp = true; break; }
            }
            if (!foundSp) {
              var og = sel.querySelector('optgroup[label="CHILI Brain"]');
              if (!og) {
                og = document.createElement('optgroup');
                og.label = 'CHILI Brain';
                sel.appendChild(og);
              }
              var optSp = document.createElement('option');
              optSp.value = targetSp;
              optSp.textContent = 'Scan pattern #' + _dlScanPattern;
              optSp.title = 'Opened from Brain tradeable list; resolves to ScanPattern id.';
              og.appendChild(optSp);
              sel.value = optSp.value;
            }
          } else if (_dlPattern) {
            var target = 'insight:' + _dlPattern;
            for (var i = 0; i < sel.options.length; i++) {
              if (sel.options[i].value === target) { sel.value = target; break; }
            }
          } else if (_dlStrategy) {
            for (var j = 0; j < sel.options.length; j++) {
              if (sel.options[j].value === _dlStrategy) { sel.value = _dlStrategy; break; }
            }
          }
        }
        if (_dlPeriod) {
          var per = document.getElementById('bt-period');
          if (per) {
            for (var k = 0; k < per.options.length; k++) {
              if (per.options[k].value === _dlPeriod) { per.value = _dlPeriod; break; }
            }
          }
        }
        renderBtStrategyParams();
        runBacktest();
      }, 600);
    }, 500);
  });
  loadScreenerPresets();
  loadPortfolio();
  loadTrades();
  loadJournal();
  loadStats();
  loadInsights();
  loadBrokerStatus();
  document.getElementById('ai-input').addEventListener('keydown', _onAiInputKey);
  _loadChatHistory();
  _refreshKnownTickers();
  document.getElementById('toolbar-search-input').addEventListener('keydown', _tsKeydown);
  document.getElementById('toolbar-search-input').addEventListener('blur', function() {
    setTimeout(closeToolbarSearch, 200);
  });
  document.getElementById('wl-input').addEventListener('keydown', _wlInputHandler);
  document.getElementById('wl-input').addEventListener('input', function() {
    clearTimeout(_wlAcDebounce);
    _wlAcDebounce = setTimeout(function() {
      _wlAcSearch(document.getElementById('wl-input').value.trim());
    }, 300);
  });
  document.getElementById('wl-input').addEventListener('blur', function() {
    setTimeout(function() { document.getElementById('wl-ac').classList.remove('open'); }, 200);
  });
  document.getElementById('j-input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') addJournal();
  });
  setInterval(refreshPrice, 60000);
  _connectLiveWS();
  setTimeout(prefetchWatchlistPrices, 2000);
  setTimeout(_autoLoadScreeners, 1500);

  // Load brain tickers -> resolve default ticker if watchlist empty
  _initFromBrain();

}

if (!window._chiliTradingDeferInit) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _chiliTradingInit);
  } else {
    _chiliTradingInit();
  }
}

function _initFromBrain() {
  var wlDone = false, brainDone = false, brainData = null;
  var _sessionStarted = false;

  function _maybeStartTradingSession() {
    if (_sessionStarted) return;
    if (!wlDone) return;
    /* Don't block the chart on /brain/tickers when the watchlist already picked a ticker. */
    if (!currentTicker && !brainDone) return;

    _sessionStarted = true;
    if (!currentTicker && brainData) {
      var stocks = brainData.stocks || [];
      if (stocks.length) currentTicker = stocks[0].ticker;
    }
    if (!currentTicker) currentTicker = 'AAPL';
    document.getElementById('ticker-label').textContent = currentTicker;
    _syncIntervalUi(currentInterval);
    loadChart();
    loadTickerDetail(currentTicker);
    loadTickerNews(currentTicker);
    loadSignals();
  }

  fetch('/api/trading/watchlist').then(function(r){return r.json();}).then(function(d) {
    if (d.ok && d.items && d.items.length && !currentTicker) {
      currentTicker = d.items[0].ticker;
    }
    wlDone = true;
    loadWatchlist();
    _maybeStartTradingSession();
  }).catch(function() { wlDone = true; _maybeStartTradingSession(); });

  fetch('/api/trading/brain/tickers').then(function(r){return r.json();}).then(function(d) {
    brainData = d.ok ? d : null;
    brainDone = true;
    _maybeStartTradingSession();
  }).catch(function() { brainDone = true; _maybeStartTradingSession(); });
}

function initChart() {
  var el = document.getElementById('main-chart');
  var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  chart = LightweightCharts.createChart(el, {
    width: el.clientWidth,
    height: el.clientHeight,
    layout: {
      background: { type: 'solid', color: isDark ? '#111827' : '#f9fafb' },
      textColor: isDark ? '#f3f4f6' : '#1f2937',
    },
    grid: {
      vertLines: { color: isDark ? '#1f293744' : '#e5e7eb44' },
      horzLines: { color: isDark ? '#1f293744' : '#e5e7eb44' },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { labelBackgroundColor: isDark ? '#6366f1' : '#4f46e5' },
      horzLine: { labelBackgroundColor: isDark ? '#6366f1' : '#4f46e5' },
    },
    rightPriceScale: {
      borderColor: isDark ? '#374151' : '#e5e7eb',
      scaleMargins: { top: 0.05, bottom: 0.2 },
    },
    timeScale: { borderColor: isDark ? '#374151' : '#e5e7eb', timeVisible: true },
    watermark: {
      visible: true,
      text: currentTicker || '',
      fontSize: 48, fontFamily: '-apple-system, BlinkMacSystemFont, sans-serif',
      color: isDark ? 'rgba(255,255,255,.04)' : 'rgba(0,0,0,.03)',
      vertDecorator: '',
    },
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: '#22c55e', downColor: '#ef4444',
    borderUpColor: '#16a34a', borderDownColor: '#dc2626',
    wickUpColor: '#22c55e', wickDownColor: '#ef4444',
  });
  volumeSeries = chart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: 'vol',
  });
  chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

  _initOhlcTooltip(el);

  new ResizeObserver(function() {
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    _scheduleUserAlertHandleLayout();
  }).observe(el);
  chart.timeScale().subscribeVisibleLogicalRangeChange(function() {
    _scheduleUserAlertHandleLayout();
  });
}

/* ── Indicator Sub-Pane Charts (RSI, MACD) ───────── */
var _rsiChart = null, _rsiSeries = null;
var _macdChart = null, _macdLineSeries = null, _macdSignalSeries = null, _macdHistSeries = null;
var _syncingTimeScale = false;

function _subPaneOpts(el) {
  var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  return {
    width: el.clientWidth,
    height: el.clientHeight,
    layout: {
      background: { type: 'solid', color: isDark ? '#111827' : '#f9fafb' },
      textColor: isDark ? '#9ca3af' : '#6b7280',
      fontSize: 10,
    },
    grid: {
      vertLines: { color: isDark ? '#1f293722' : '#e5e7eb22' },
      horzLines: { color: isDark ? '#1f293744' : '#e5e7eb44' },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { labelVisible: false },
      horzLine: { labelBackgroundColor: isDark ? '#6366f1' : '#4f46e5' },
    },
    rightPriceScale: {
      borderColor: isDark ? '#374151' : '#e5e7eb',
      scaleMargins: { top: 0.08, bottom: 0.08 },
    },
    timeScale: {
      visible: false,
      borderColor: isDark ? '#374151' : '#e5e7eb',
    },
    handleScroll: { vertTouchDrag: false },
  };
}

function _syncTimeScales() {
  if (!chart) return;
  var allCharts = [chart];
  if (_rsiChart) allCharts.push(_rsiChart);
  if (_macdChart) allCharts.push(_macdChart);
  if (allCharts.length < 2) return;

  allCharts.forEach(function(src) {
    src.timeScale().subscribeVisibleLogicalRangeChange(function(range) {
      if (_syncingTimeScale || !range) return;
      _syncingTimeScale = true;
      allCharts.forEach(function(dst) {
        if (dst !== src) {
          try { dst.timeScale().setVisibleLogicalRange(range); } catch(e) {}
        }
      });
      _syncingTimeScale = false;
    });
  });

  _setupCrosshairSync();
}

var _crosshairUnsubs = [];

function _setupCrosshairSync() {
  _crosshairUnsubs.forEach(function(fn) { try { fn(); } catch(e) {} });
  _crosshairUnsubs = [];

  var charts = [];
  if (chart && candleSeries) charts.push({ c: chart, s: candleSeries });
  if (_rsiChart && _rsiSeries) charts.push({ c: _rsiChart, s: _rsiSeries });
  if (_macdChart && _macdLineSeries) charts.push({ c: _macdChart, s: _macdLineSeries });
  if (charts.length < 2) return;

  charts.forEach(function(src) {
    var handler = function(param) {
      if (_syncingTimeScale) return;
      _syncingTimeScale = true;
      charts.forEach(function(dst) {
        if (dst.c === src.c) return;
        if (!param.time) { dst.c.clearCrosshairPosition(); return; }
        var dp = param.seriesData ? param.seriesData.get(src.s) : null;
        var val = 0;
        if (dp) val = dp.value != null ? dp.value : (dp.close != null ? dp.close : 0);
        try { dst.c.setCrosshairPosition(val, param.time, dst.s); } catch(e) {}
      });
      _syncingTimeScale = false;
    };
    src.c.subscribeCrosshairMove(handler);
  });
}

var _paneSlots = {};

function _placePaneInSlot(paneEl) {
  var paneId = paneEl.getAttribute('data-pane-id');
  var slot = _paneSlots[paneId] || paneEl.getAttribute('data-pane-default') || 'bottom';
  _paneSlots[paneId] = slot;
  var container = document.getElementById('ind-panes-' + slot);
  if (container && paneEl.parentElement !== container) {
    container.appendChild(paneEl);
  }
}

function _showRsiPane() {
  var pane = document.getElementById('rsi-pane');
  if (_rsiChart || !pane) return;
  _placePaneInSlot(pane);
  pane.style.display = 'block';
  var el = document.getElementById('rsi-chart');
  _rsiChart = LightweightCharts.createChart(el, _subPaneOpts(el));
  _rsiChart.subscribeCrosshairMove(function(param) {
    var valEl = document.getElementById('rsi-pane-val');
    if (!param.time || !param.seriesData || !_rsiSeries) { if (valEl) valEl.textContent = ''; return; }
    var d = param.seriesData.get(_rsiSeries);
    if (d && d.value != null) {
      var col = d.value >= 70 ? '#ef4444' : (d.value <= 30 ? '#22c55e' : '#9ca3af');
      valEl.innerHTML = '<span style="color:' + col + '">' + d.value.toFixed(1) + '</span>';
    } else { valEl.textContent = ''; }
  });
  new ResizeObserver(function() {
    if (_rsiChart) _rsiChart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
  }).observe(el);
  _syncTimeScales();
}

function _hideRsiPane() {
  var pane = document.getElementById('rsi-pane');
  if (_rsiChart) { _rsiChart.remove(); _rsiChart = null; _rsiSeries = null; }
  if (pane) pane.style.display = 'none';
  _syncTimeScales();
}

function _showMacdPane() {
  var pane = document.getElementById('macd-pane');
  if (_macdChart || !pane) return;
  _placePaneInSlot(pane);
  pane.style.display = 'block';
  var el = document.getElementById('macd-chart');
  _macdChart = LightweightCharts.createChart(el, _subPaneOpts(el));
  _macdChart.subscribeCrosshairMove(function(param) {
    var valEl = document.getElementById('macd-pane-val');
    if (!param.time || !param.seriesData) { if (valEl) valEl.textContent = ''; return; }
    var parts = [];
    if (_macdLineSeries) {
      var m = param.seriesData.get(_macdLineSeries);
      if (m && m.value != null) parts.push('<span style="color:#3b82f6">M:' + m.value.toFixed(4) + '</span>');
    }
    if (_macdSignalSeries) {
      var s = param.seriesData.get(_macdSignalSeries);
      if (s && s.value != null) parts.push('<span style="color:#f59e0b">S:' + s.value.toFixed(4) + '</span>');
    }
    if (_macdHistSeries) {
      var h = param.seriesData.get(_macdHistSeries);
      if (h && h.value != null) {
        var hcol = h.value >= 0 ? '#22c55e' : '#ef4444';
        parts.push('<span style="color:' + hcol + '">H:' + h.value.toFixed(4) + '</span>');
      }
    }
    valEl.innerHTML = parts.join(' &nbsp; ');
  });
  new ResizeObserver(function() {
    if (_macdChart) _macdChart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
  }).observe(el);
  _syncTimeScales();
}

function _hideMacdPane() {
  var pane = document.getElementById('macd-pane');
  if (_macdChart) { _macdChart.remove(); _macdChart = null; _macdLineSeries = null; _macdSignalSeries = null; _macdHistSeries = null; }
  if (pane) pane.style.display = 'none';
  _syncTimeScales();
}

/* ── Indicator pane drag-reorder ─────────────────── */
var _dragPane = null;

function _initPaneDrag() {
  document.querySelectorAll('.ind-sub-grip').forEach(function(grip) {
    grip.setAttribute('draggable', 'true');

    grip.addEventListener('dragstart', function(e) {
      var pane = grip.closest('.ind-sub-pane');
      if (!pane) return;
      _dragPane = pane;
      pane.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', pane.getAttribute('data-pane-id'));
    });

    grip.addEventListener('dragend', function() {
      if (_dragPane) _dragPane.classList.remove('dragging');
      _dragPane = null;
      document.querySelectorAll('.ind-panes-slot').forEach(function(s) { s.classList.remove('drag-over'); });
    });
  });

  ['ind-panes-top', 'ind-panes-bottom'].forEach(function(slotId) {
    var slot = document.getElementById(slotId);
    if (!slot) return;

    slot.addEventListener('dragover', function(e) {
      if (!_dragPane) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      slot.classList.add('drag-over');
    });

    slot.addEventListener('dragleave', function() {
      slot.classList.remove('drag-over');
    });

    slot.addEventListener('drop', function(e) {
      e.preventDefault();
      slot.classList.remove('drag-over');
      if (!_dragPane) return;

      var paneId = _dragPane.getAttribute('data-pane-id');
      var targetSlot = slotId.replace('ind-panes-', '');
      if (_paneSlots[paneId] === targetSlot) return;

      _paneSlots[paneId] = targetSlot;

      var otherPanes = slot.querySelectorAll('.ind-sub-pane');
      otherPanes.forEach(function(op) {
        var opId = op.getAttribute('data-pane-id');
        if (opId !== paneId) {
          var otherSlot = targetSlot === 'top' ? 'bottom' : 'top';
          _paneSlots[opId] = otherSlot;
          var otherContainer = document.getElementById('ind-panes-' + otherSlot);
          if (otherContainer) otherContainer.appendChild(op);
          _resizeSubChart(opId);
        }
      });

      slot.appendChild(_dragPane);
      _dragPane.classList.remove('dragging');

      _resizeSubChart(paneId);
      _dragPane = null;

      try { localStorage.setItem('chili_pane_slots', JSON.stringify(_paneSlots)); } catch(e) {}
    });
  });

  var mainChart = document.getElementById('main-chart');
  if (mainChart) {
    mainChart.addEventListener('dragover', function(e) {
      if (!_dragPane) return;
      e.preventDefault();
      var rect = mainChart.getBoundingClientRect();
      var midY = rect.top + rect.height / 2;
      var targetSlotId = e.clientY < midY ? 'ind-panes-top' : 'ind-panes-bottom';
      document.querySelectorAll('.ind-panes-slot').forEach(function(s) { s.classList.remove('drag-over'); });
      var target = document.getElementById(targetSlotId);
      if (target) target.classList.add('drag-over');
    });
    mainChart.addEventListener('dragleave', function() {
      document.querySelectorAll('.ind-panes-slot').forEach(function(s) { s.classList.remove('drag-over'); });
    });
    mainChart.addEventListener('drop', function(e) {
      e.preventDefault();
      document.querySelectorAll('.ind-panes-slot').forEach(function(s) { s.classList.remove('drag-over'); });
      if (!_dragPane) return;
      var rect = mainChart.getBoundingClientRect();
      var midY = rect.top + rect.height / 2;
      var targetSlotId = e.clientY < midY ? 'ind-panes-top' : 'ind-panes-bottom';
      var slot = document.getElementById(targetSlotId);
      if (slot) {
        var paneId = _dragPane.getAttribute('data-pane-id');
        var targetSlot = targetSlotId.replace('ind-panes-', '');
        _paneSlots[paneId] = targetSlot;

        var otherPanes = slot.querySelectorAll('.ind-sub-pane');
        otherPanes.forEach(function(op) {
          var opId = op.getAttribute('data-pane-id');
          if (opId !== paneId) {
            var otherSlot = targetSlot === 'top' ? 'bottom' : 'top';
            _paneSlots[opId] = otherSlot;
            var otherContainer = document.getElementById('ind-panes-' + otherSlot);
            if (otherContainer) otherContainer.appendChild(op);
            _resizeSubChart(opId);
          }
        });

        slot.appendChild(_dragPane);
        _resizeSubChart(paneId);
      }
      _dragPane.classList.remove('dragging');
      _dragPane = null;
      try { localStorage.setItem('chili_pane_slots', JSON.stringify(_paneSlots)); } catch(e) {}
    });
  }
}

function _resizeSubChart(paneId) {
  setTimeout(function() {
    if (paneId === 'rsi' && _rsiChart) {
      var el = document.getElementById('rsi-chart');
      if (el) _rsiChart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    }
    if (paneId === 'macd' && _macdChart) {
      var el2 = document.getElementById('macd-chart');
      if (el2) _macdChart.applyOptions({ width: el2.clientWidth, height: el2.clientHeight });
    }
  }, 50);
}

function _restorePaneSlots() {
  try {
    var saved = localStorage.getItem('chili_pane_slots');
    if (saved) {
      var parsed = JSON.parse(saved);
      if (parsed && typeof parsed === 'object') _paneSlots = parsed;
    }
  } catch(e) {}
}

/* ── Chart Type Switching ────────────────────────── */
function changeChartType(type) {
  currentChartType = type;
  if (_rawOhlcData) {
    _applyChartType(_rawOhlcData);
    _restoreAnnotations();
    _setupCrosshairSync();
    _redrawAll();
  }
}

function _toHeikinAshi(data) {
  var ha = [];
  for (var i = 0; i < data.length; i++) {
    var c = data[i];
    var prevHa = i > 0 ? ha[i-1] : null;
    var haClose = (c.open + c.high + c.low + c.close) / 4;
    var haOpen = prevHa ? (prevHa.open + prevHa.close) / 2 : (c.open + c.close) / 2;
    ha.push({time:c.time, open:haOpen, high:Math.max(c.high, haOpen, haClose), low:Math.min(c.low, haOpen, haClose), close:haClose});
  }
  return ha;
}

function _applyChartType(data) {
  if (!chart || !candleSeries) return;
  chart.removeSeries(candleSeries);
  var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  var mapped;

  if (currentChartType === 'line') {
    candleSeries = chart.addLineSeries({color: '#6366f1', lineWidth: 2});
    mapped = data.map(function(c) { return {time:c.time, value:c.close}; });
  } else if (currentChartType === 'area') {
    candleSeries = chart.addAreaSeries({
      topColor: 'rgba(99,102,241,.4)', bottomColor: 'rgba(99,102,241,.02)',
      lineColor: '#6366f1', lineWidth: 2,
    });
    mapped = data.map(function(c) { return {time:c.time, value:c.close}; });
  } else if (currentChartType === 'baseline') {
    var avgPrice = data.reduce(function(s,c){return s+c.close;},0) / data.length;
    candleSeries = chart.addBaselineSeries({
      baseValue: {type:'price', price:avgPrice},
      topLineColor: '#22c55e', topFillColor1: 'rgba(34,197,94,.2)', topFillColor2: 'rgba(34,197,94,.02)',
      bottomLineColor: '#ef4444', bottomFillColor1: 'rgba(239,68,68,.02)', bottomFillColor2: 'rgba(239,68,68,.2)',
    });
    mapped = data.map(function(c) { return {time:c.time, value:c.close}; });
  } else if (currentChartType === 'bars') {
    candleSeries = chart.addBarSeries({
      upColor: '#22c55e', downColor: '#ef4444',
    });
    mapped = data.map(function(c) { return {time:c.time, open:c.open, high:c.high, low:c.low, close:c.close}; });
  } else if (currentChartType === 'hollow') {
    candleSeries = chart.addCandlestickSeries({
      upColor: 'transparent', downColor: '#ef4444',
      borderUpColor: '#22c55e', borderDownColor: '#ef4444',
      wickUpColor: '#22c55e', wickDownColor: '#ef4444',
    });
    mapped = data.map(function(c) { return {time:c.time, open:c.open, high:c.high, low:c.low, close:c.close}; });
  } else if (currentChartType === 'heikin_ashi') {
    candleSeries = chart.addCandlestickSeries({
      upColor: '#22c55e', downColor: '#ef4444',
      borderUpColor: '#16a34a', borderDownColor: '#dc2626',
      wickUpColor: '#22c55e', wickDownColor: '#ef4444',
    });
    mapped = _toHeikinAshi(data);
  } else {
    candleSeries = chart.addCandlestickSeries({
      upColor: '#22c55e', downColor: '#ef4444',
      borderUpColor: '#16a34a', borderDownColor: '#dc2626',
      wickUpColor: '#22c55e', wickDownColor: '#ef4444',
    });
    mapped = data.map(function(c) { return {time:c.time, open:c.open, high:c.high, low:c.low, close:c.close}; });
  }
  candleSeries.setData(mapped);
}

/* ── Magnet / Snap to OHLC ──────────────────────── */
function _toggleMagnet() {
  _magnetEnabled = !_magnetEnabled;
  var btn = document.getElementById('btn-magnet');
  if (btn) btn.classList.toggle('active', _magnetEnabled);
}

function _snapToOHLC(chartPt) {
  if (!_magnetEnabled || !_rawOhlcData || !chartPt) return chartPt;
  var best = null, bestDist = Infinity;
  for (var i = 0; i < _rawOhlcData.length; i++) {
    var bar = _rawOhlcData[i];
    if (bar.time !== chartPt.time) continue;
    var prices = [bar.open, bar.high, bar.low, bar.close];
    for (var j = 0; j < prices.length; j++) {
      var d = Math.abs(prices[j] - chartPt.price);
      if (d < bestDist) { bestDist = d; best = {time: chartPt.time, price: prices[j]}; }
    }
    break;
  }
  return best || chartPt;
}

/* ── Undo / Redo ────────────────────────────────── */
function _undoDraw() {
  if (_userDrawings.length === 0) return;
  _drawUndoStack.push(_userDrawings.pop());
  _saveDrawings(); _redrawAll();
}

function _redoDraw() {
  if (_drawUndoStack.length === 0) return;
  _userDrawings.push(_drawUndoStack.pop());
  _saveDrawings(); _redrawAll();
}

/* ── Compare Symbols Overlay ─────────────────────── */
function addCompareSymbol(ticker) {
  if (_compareSeries[ticker]) return;
  fetch('/api/trading/ohlcv?ticker=' + encodeURIComponent(ticker) + '&interval=' + currentInterval + '&period=' + currentPeriod)
    .then(function(r) { return r.json(); }).then(function(d) {
      if (!d.ok || !d.data || !d.data.length || !chart) return;
      var colors = ['#f59e0b','#06b6d4','#ec4899','#8b5cf6','#f43f5e'];
      var idx = Object.keys(_compareSeries).length % colors.length;
      var s = chart.addLineSeries({
        color: colors[idx], lineWidth: 1.5, priceScaleId: 'compare_' + ticker,
        title: ticker, lastValueVisible: true, priceLineVisible: false,
      });
      chart.priceScale('compare_' + ticker).applyOptions({
        scaleMargins: { top: 0.05, bottom: 0.2 },
      });
      s.setData(d.data.map(function(c) { return {time:c.time, value:c.close}; }));
      _compareSeries[ticker] = {series: s, color: colors[idx]};
    });
}

function removeCompareSymbol(ticker) {
  if (!_compareSeries[ticker]) return;
  chart.removeSeries(_compareSeries[ticker].series);
  delete _compareSeries[ticker];
}

/* ── On-Chart Price Alert (modal + live tick evaluation) ─ */
function _getUserAlertsStore() {
  try { return JSON.parse(sessionStorage.getItem(USER_PRICE_ALERTS_KEY) || '{}'); } catch (e) { return {}; }
}
function _setUserAlertsStore(obj) {
  try { sessionStorage.setItem(USER_PRICE_ALERTS_KEY, JSON.stringify(obj)); } catch (e) {}
}
function _userAlertSpecsForTicker(t) {
  var m = _getUserAlertsStore();
  return Array.isArray(m[t]) ? m[t] : [];
}
function _saveSpecsForTicker(t, specs) {
  var m = _getUserAlertsStore();
  m[t] = specs;
  _setUserAlertsStore(m);
}
function _serializeUserAlertsForTicker(t) {
  return _userPriceAlerts.filter(function(a) { return a.ticker === t; }).map(function(a) {
    return {
      id: a.id, price: a.price, condition: a.condition, triggerOnce: a.triggerOnce,
      expireAt: a.expireAt, message: a.message, toast: a.toast, sound: a.sound,
      paused: !!a.paused, color: a.color || '#f97316',
    };
  });
}
function _persistUserAlertsForTicker(t) {
  _saveSpecsForTicker(t, _serializeUserAlertsForTicker(t));
}

function _detachUserPriceAlertLines() {
  _userPriceAlerts.forEach(function(a) {
    if (a.lineHandle) {
      try { a.lineHandle.remove(); } catch (e) {}
      a.lineHandle = null;
    }
    if (a.handleEl) {
      try { a.handleEl.remove(); } catch (e2) {}
      a.handleEl = null;
    }
  });
  _userPriceAlerts = [];
  var layer = document.getElementById('user-alert-handles-layer');
  if (layer) layer.innerHTML = '';
}

function _chartAlertLineTitle(message, price) {
  var t = (message || '').trim();
  if (t.length > 36) t = t.slice(0, 34) + '…';
  if (!t) t = 'Alert ' + smartPrice(price, (currentTicker || '').indexOf('-USD') !== -1);
  return t;
}

function _createUserAlertLine(spec) {
  if (!candleSeries || spec.price == null || !isFinite(spec.price)) return null;
  var col = spec.color || '#f97316';
  return candleSeries.createPriceLine({
    price: spec.price,
    color: col,
    lineWidth: 2,
    lineStyle: LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: true,
    title: _chartAlertLineTitle(spec.message, spec.price),
  });
}

function _userAlertLinesGloballyVisible() {
  try { return sessionStorage.getItem(USER_ALERT_LINES_VISIBLE_KEY) !== '0'; } catch (e) { return true; }
}

function _setUserAlertLinesGloballyVisible(show) {
  try { sessionStorage.setItem(USER_ALERT_LINES_VISIBLE_KEY, show ? '1' : '0'); } catch (e2) {}
  _applyGlobalUserAlertLinesVisibility();
}

function _applyGlobalUserAlertLinesVisibility() {
  var show = _userAlertLinesGloballyVisible();
  if (!candleSeries) {
    _syncUserAlertHandlesDOM();
    return;
  }
  _userPriceAlerts.forEach(function(a) {
    if (a.ticker !== currentTicker) return;
    if (show) {
      if (!a.lineHandle && a.price != null && isFinite(a.price)) {
        a.lineHandle = _createUserAlertLine({
          price: a.price, message: a.message, color: a.color || '#f97316',
        });
      } else if (a.lineHandle) {
        try {
          a.lineHandle.applyOptions({
            price: a.price,
            color: a.color || '#f97316',
            title: _chartAlertLineTitle(a.message, a.price),
          });
        } catch (e) {}
      }
    } else if (a.lineHandle) {
      try { a.lineHandle.remove(); } catch (e2) {}
      a.lineHandle = null;
    }
  });
  _syncUserAlertHandlesDOM();
}

function _scheduleUserAlertHandleLayout() {
  if (_userAlertLayoutRAF) return;
  _userAlertLayoutRAF = requestAnimationFrame(function() {
    _userAlertLayoutRAF = null;
    _layoutUserAlertHandlesLayerBox();
    _syncUserAlertHandlesDOM();
  });
}

function _layoutUserAlertHandlesLayerBox() {
  var layer = document.getElementById('user-alert-handles-layer');
  var mc = document.getElementById('main-chart');
  var area = document.getElementById('chart-area');
  if (!layer || !mc || !area) return;
  var ar = area.getBoundingClientRect();
  var mr = mc.getBoundingClientRect();
  layer.style.left = (mr.left - ar.left + area.scrollLeft) + 'px';
  layer.style.top = (mr.top - ar.top + area.scrollTop) + 'px';
  layer.style.width = mr.width + 'px';
  layer.style.height = mr.height + 'px';
}

function _shortUserAlertHandleLabel(a) {
  var t = (a.message || '').trim();
  if (!t) t = _chartAlertLineTitle('', a.price);
  if (t.length > 42) return t.slice(0, 40) + '\u2026';
  return t;
}

function _syncUserAlertHandlesDOM() {
  var layer = document.getElementById('user-alert-handles-layer');
  if (!layer || !candleSeries) return;
  _layoutUserAlertHandlesLayerBox();
  if (!_userAlertLinesGloballyVisible()) {
    Array.prototype.slice.call(layer.querySelectorAll('.user-alert-handle')).forEach(function(n) { n.remove(); });
    _userPriceAlerts.forEach(function(a) { a.handleEl = null; });
    return;
  }
  var seen = {};
  _userPriceAlerts.forEach(function(a) {
    if (a.ticker !== currentTicker) return;
    seen[a.id] = true;
    var el = a.handleEl;
    if (!el || !el.parentNode) {
      el = document.createElement('div');
      el.className = 'user-alert-handle';
      el.dataset.alertId = a.id;
      el.innerHTML =
        '<span class="uah-text"></span>' +
        '<button type="button" class="uah-del" title="Delete alert" aria-label="Delete">&times;</button>';
      el.querySelector('.uah-del').addEventListener('click', function(ev) {
        ev.stopPropagation(); ev.preventDefault();
        _deleteUserPriceAlertById(a.id);
      });
      el.addEventListener('mousedown', _onUserAlertHandlePointerDown);
      el.addEventListener('contextmenu', _onUserAlertHandleContextMenu);
      layer.appendChild(el);
      a.handleEl = el;
    }
    var y = candleSeries.priceToCoordinate(a.price);
    if (y == null || !isFinite(y)) {
      el.style.display = 'none';
    } else {
      el.style.display = '';
      el.style.top = y + 'px';
      el.style.borderColor = a.color || '#f97316';
    }
    el.classList.toggle('paused', !!a.paused);
    var tx = el.querySelector('.uah-text');
    if (tx) tx.textContent = _shortUserAlertHandleLabel(a);
  });
  Array.prototype.slice.call(layer.children).forEach(function(ch) {
    var id = ch.dataset.alertId;
    if (!id || !seen[id]) {
      ch.remove();
      _userPriceAlerts.forEach(function(a) { if (a.handleEl === ch) a.handleEl = null; });
    }
  });
}

function _userPriceAlertIndexById(id) {
  for (var i = 0; i < _userPriceAlerts.length; i++) {
    if (_userPriceAlerts[i].id === id) return i;
  }
  return -1;
}

function _deleteUserPriceAlertById(id) {
  var i = _userPriceAlertIndexById(id);
  if (i >= 0) _removeUserPriceAlertAt(i, true);
  _closeChartAlertCtxMenu();
}

function _userPriceAlertHitTestPixel(py) {
  if (!candleSeries || !_userPriceAlerts.length) return -1;
  var tol = 10;
  for (var i = 0; i < _userPriceAlerts.length; i++) {
    var a = _userPriceAlerts[i];
    if (a.ticker !== currentTicker) continue;
    var y = candleSeries.priceToCoordinate(a.price);
    if (y == null || !isFinite(y)) continue;
    if (Math.abs(y - py) <= tol) return i;
  }
  return -1;
}

function _closeChartAlertCtxMenu() {
  var m = document.getElementById('chart-alert-ctx-menu');
  var fly = document.getElementById('ctx-uap-color-flyout');
  var tog = document.getElementById('ctx-uap-color-toggle');
  if (m) { m.classList.add('hidden'); m.setAttribute('aria-hidden', 'true'); }
  if (fly) fly.classList.add('hidden');
  if (tog) tog.setAttribute('aria-expanded', 'false');
  _chartAlertCtxTargetId = '';
  if (_chartAlertCtxDocBound) {
    document.removeEventListener('mousedown', _chartAlertCtxDocMouse, true);
    _chartAlertCtxDocBound = false;
  }
}

function _chartAlertCtxDocMouse(ev) {
  var m = document.getElementById('chart-alert-ctx-menu');
  if (!m || m.classList.contains('hidden')) return;
  if (m.contains(ev.target)) return;
  _closeChartAlertCtxMenu();
}

function _openChartAlertCtxMenuAtEvent(ev, alertId) {
  _closeChartCtxMenu();
  var m = document.getElementById('chart-alert-ctx-menu');
  if (!m) return;
  _chartAlertCtxTargetId = alertId;
  var idx = _userPriceAlertIndexById(alertId);
  var a = idx >= 0 ? _userPriceAlerts[idx] : null;
  var pauseLab = document.getElementById('ctx-uap-pause-label');
  if (pauseLab) pauseLab.textContent = (a && a.paused) ? 'Resume' : 'Pause';
  var chk = document.getElementById('ctx-uap-lines-check');
  if (chk) chk.classList.toggle('hidden', !_userAlertLinesGloballyVisible());
  m.classList.remove('hidden');
  m.setAttribute('aria-hidden', 'false');
  var w = m.offsetWidth || 220;
  var h = m.offsetHeight || 200;
  var x = ev.clientX;
  var y = ev.clientY;
  if (x + w > window.innerWidth - 8) x = window.innerWidth - w - 8;
  if (y + h > window.innerHeight - 8) y = window.innerHeight - h - 8;
  m.style.left = x + 'px';
  m.style.top = y + 'px';
  if (!_chartAlertCtxDocBound) {
    document.addEventListener('mousedown', _chartAlertCtxDocMouse, true);
    _chartAlertCtxDocBound = true;
  }
}

function _onUserAlertHandleContextMenu(ev) {
  ev.preventDefault();
  ev.stopPropagation();
  var id = ev.currentTarget && ev.currentTarget.dataset ? ev.currentTarget.dataset.alertId : '';
  if (!id) return;
  _openChartAlertCtxMenuAtEvent(ev, id);
}

function _onUserAlertHandlePointerDown(ev) {
  if (ev.button !== 0) return;
  if (ev.target.closest && ev.target.closest('.uah-del')) return;
  var id = ev.currentTarget && ev.currentTarget.dataset ? ev.currentTarget.dataset.alertId : '';
  if (!id) return;
  ev.preventDefault();
  ev.stopPropagation();
  var idx = _userPriceAlertIndexById(id);
  if (idx < 0) return;
  var el = ev.currentTarget;
  el.classList.add('dragging');
  _userAlertDragState = { id: id, startY: ev.clientY };
  document.addEventListener('mousemove', _onUserAlertDragMove, true);
  document.addEventListener('mouseup', _onUserAlertDragUp, true);
}

function _onUserAlertDragMove(ev) {
  if (!_userAlertDragState || !candleSeries) return;
  var mc = document.getElementById('main-chart');
  if (!mc) return;
  var r = mc.getBoundingClientRect();
  var py = ev.clientY - r.top;
  var price = candleSeries.coordinateToPrice(py);
  if (price == null || !isFinite(price)) return;
  var mm = smartMinMove(price);
  if (mm && mm > 0) price = Math.round(price / mm) * mm;
  var idx = _userPriceAlertIndexById(_userAlertDragState.id);
  if (idx < 0) return;
  var a = _userPriceAlerts[idx];
  a.price = price;
  if (a.lineHandle) {
    try {
      a.lineHandle.applyOptions({
        price: price,
        title: _chartAlertLineTitle(a.message, price),
        color: a.color || '#f97316',
      });
    } catch (e) {}
  }
  if (a.handleEl) {
    var y = candleSeries.priceToCoordinate(price);
    if (y != null && isFinite(y)) a.handleEl.style.top = y + 'px';
  }
}

function _onUserAlertDragUp() {
  document.removeEventListener('mousemove', _onUserAlertDragMove, true);
  document.removeEventListener('mouseup', _onUserAlertDragUp, true);
  if (_userAlertDragState) {
    var idx = _userPriceAlertIndexById(_userAlertDragState.id);
    if (idx >= 0) _persistUserAlertsForTicker(currentTicker);
    var a = idx >= 0 ? _userPriceAlerts[idx] : null;
    if (a && a.handleEl) a.handleEl.classList.remove('dragging');
  }
  _userAlertDragState = null;
  _scheduleUserAlertHandleLayout();
}

function _initChartAlertCtxMenu() {
  var m = document.getElementById('chart-alert-ctx-menu');
  if (!m) return;
  m.addEventListener('click', function(ev) { ev.stopPropagation(); });
  var p = document.getElementById('ctx-uap-pause');
  if (p) p.onclick = function() {
    var idx = _userPriceAlertIndexById(_chartAlertCtxTargetId);
    if (idx >= 0) {
      _userPriceAlerts[idx].paused = !_userPriceAlerts[idx].paused;
      _persistUserAlertsForTicker(currentTicker);
      _syncUserAlertHandlesDOM();
    }
    _closeChartAlertCtxMenu();
  };
  var ed = document.getElementById('ctx-uap-edit');
  if (ed) ed.onclick = function() {
    var idx = _userPriceAlertIndexById(_chartAlertCtxTargetId);
    if (idx >= 0) _openChartAlertModalForEdit(_userPriceAlerts[idx]);
    _closeChartAlertCtxMenu();
  };
  var del = document.getElementById('ctx-uap-delete');
  if (del) del.onclick = function() {
    _deleteUserPriceAlertById(_chartAlertCtxTargetId);
  };
  var lines = document.getElementById('ctx-uap-lines-toggle');
  if (lines) lines.onclick = function() {
    _setUserAlertLinesGloballyVisible(!_userAlertLinesGloballyVisible());
    _closeChartAlertCtxMenu();
  };
  var ct = document.getElementById('ctx-uap-color-toggle');
  var cf = document.getElementById('ctx-uap-color-flyout');
  if (ct && cf) {
    ct.onclick = function(e) {
      e.stopPropagation();
      var was = cf.classList.contains('hidden');
      cf.classList.toggle('hidden', !was);
      ct.setAttribute('aria-expanded', was ? 'true' : 'false');
    };
    var swatches = cf.querySelectorAll('[data-uap-color]');
    for (var si = 0; si < swatches.length; si++) {
      (function(btn) {
        btn.onclick = function(e) {
          e.stopPropagation();
          var col = btn.getAttribute('data-uap-color');
          var idx = _userPriceAlertIndexById(_chartAlertCtxTargetId);
          if (idx >= 0 && col) {
            _userPriceAlerts[idx].color = col;
            var aa = _userPriceAlerts[idx];
            if (aa.lineHandle) {
              try {
                aa.lineHandle.applyOptions({
                  color: col,
                  title: _chartAlertLineTitle(aa.message, aa.price),
                });
              } catch (e2) {}
            }
            _persistUserAlertsForTicker(currentTicker);
            _syncUserAlertHandlesDOM();
          }
          cf.classList.add('hidden');
          ct.setAttribute('aria-expanded', 'false');
          _closeChartAlertCtxMenu();
        };
      })(swatches[si]);
    }
  }
}

function _hydrateUserPriceAlertsAfterLoad() {
  _detachUserPriceAlertLines();
  if (!candleSeries || !currentTicker) return;
  var specs = _userAlertSpecsForTicker(currentTicker);
  var lastClose = null;
  if (_rawOhlcData && _rawOhlcData.length) {
    lastClose = _rawOhlcData[_rawOhlcData.length - 1].close;
  }
  var now = Date.now();
  var keep = [];
  specs.forEach(function(s) {
    if (!s || s.price == null || !isFinite(s.price)) return;
    if (s.expireAt && now > s.expireAt) return;
    var specForLine = {
      price: Number(s.price), message: s.message || '', color: s.color || '#f97316',
    };
    var line = _userAlertLinesGloballyVisible() ? _createUserAlertLine(specForLine) : null;
    keep.push({
      id: s.id || String(Date.now()) + '_' + Math.random().toString(36).slice(2, 7),
      ticker: currentTicker,
      price: Number(s.price),
      condition: s.condition || 'crossing',
      triggerOnce: s.triggerOnce !== false,
      expireAt: s.expireAt || null,
      message: s.message || '',
      toast: s.toast !== false,
      sound: s.sound !== false,
      paused: !!s.paused,
      color: s.color || '#f97316',
      lineHandle: line,
      handleEl: null,
      prevRefPrice: lastClose != null && isFinite(lastClose) ? lastClose : null,
      fired: false,
    });
  });
  _userPriceAlerts = keep;
  _scheduleUserAlertHandleLayout();
}

function _removeUserPriceAlertAt(i, persist) {
  var a = _userPriceAlerts[i];
  if (!a) return;
  if (a.lineHandle) {
    try { a.lineHandle.remove(); } catch (e) {}
  }
  if (a.handleEl) {
    try { a.handleEl.remove(); } catch (e2) {}
    a.handleEl = null;
  }
  _userPriceAlerts.splice(i, 1);
  if (persist) _persistUserAlertsForTicker(currentTicker);
}

function _fireUserPriceAlert(a, price) {
  if (a.toast) {
    _showAlertToast({
      type: 'alert',
      ticker: a.ticker || currentTicker,
      alert_type: 'Price alert',
      price: price,
      message: a.message || (a.ticker + ' @ ' + smartPrice(a.price, (a.ticker || '').indexOf('-USD') !== -1)),
    }, !a.sound);
  }
  if (!a.toast && a.sound && _alertSoundEnabled) {
    try {
      var ac = new (window.AudioContext || window.webkitAudioContext)();
      var osc = ac.createOscillator();
      var gain = ac.createGain();
      osc.connect(gain); gain.connect(ac.destination);
      osc.type = 'sine'; osc.frequency.value = 880;
      gain.gain.value = 0.1;
      osc.start(); osc.stop(ac.currentTime + 0.12);
    } catch (e) {}
  }
}

function _evaluateUserPriceAlertsOnTick(price) {
  if (!price || !isFinite(price) || !_userPriceAlerts.length) return;
  var now = Date.now();
  for (var i = _userPriceAlerts.length - 1; i >= 0; i--) {
    var a = _userPriceAlerts[i];
    if (a.ticker !== currentTicker) continue;
    if (a.paused) continue;
    if (a.expireAt && now > a.expireAt) {
      _removeUserPriceAlertAt(i, true);
      continue;
    }
    var L = a.price;
    var prev = a.prevRefPrice;
    if (prev == null || !isFinite(prev)) {
      a.prevRefPrice = price;
      continue;
    }
    var isCross = a.condition === 'crossing' || a.condition === 'crossing_up' || a.condition === 'crossing_down';
    var fire = false;
    if (a.condition === 'crossing') fire = (prev < L && price >= L) || (prev > L && price <= L);
    else if (a.condition === 'crossing_up') fire = prev < L && price >= L;
    else if (a.condition === 'crossing_down') fire = prev > L && price <= L;
    else if (a.condition === 'gte') fire = price >= L;
    else if (a.condition === 'lte') fire = price <= L;

    if (!fire) {
      a.prevRefPrice = price;
      continue;
    }

    if (isCross && !a.triggerOnce) {
      _fireUserPriceAlert(a, price);
      a.prevRefPrice = price;
      continue;
    }

    if (isCross && a.triggerOnce) {
      _fireUserPriceAlert(a, price);
      _removeUserPriceAlertAt(i, true);
      continue;
    }

    if (!isCross) {
      if (a.fired && a.triggerOnce) {
        a.prevRefPrice = price;
        continue;
      }
      _fireUserPriceAlert(a, price);
      if (a.triggerOnce) _removeUserPriceAlertAt(i, true);
      else a.prevRefPrice = price;
    }
  }
}

function _defaultCamMessage(ticker, condition, priceNum) {
  var t = ticker || '';
  var isCr = t.indexOf('-USD') !== -1;
  var p = smartPrice(priceNum, isCr);
  var map = { crossing: 'Crossing', crossing_up: 'Crossing up', crossing_down: 'Crossing down', gte: 'At or above', lte: 'At or below' };
  var lab = map[condition] || 'Crossing';
  return t + ' ' + lab + ' ' + p;
}

function _syncCamMessageFromForm() {
  var msg = document.getElementById('cam-message');
  var cond = document.getElementById('cam-condition');
  var val = document.getElementById('cam-value');
  if (!msg || !cond || !val) return;
  var p = parseFloat(val.value);
  if (isNaN(p) || !isFinite(p)) return;
  if (msg.dataset.userEdited === '1') return;
  msg.value = _defaultCamMessage(currentTicker, cond.value, p);
}

function _onCamConditionChange() {
  var cond = document.getElementById('cam-condition');
  var trig = document.getElementById('cam-trigger');
  if (!cond || !trig) return;
  var isCross = cond.value.indexOf('crossing') === 0;
  trig.querySelector('option[value="repeat"]').disabled = !isCross;
  if (!isCross && trig.value === 'repeat') trig.value = 'once';
  _syncCamMessageFromForm();
}

function _openChartAlertModal(prefillPrice) {
  var eid = document.getElementById('cam-edit-id');
  if (eid) eid.value = '';
  var sub = document.getElementById('cam-submit-btn');
  if (sub) sub.textContent = 'Create alert';
  var ov = document.getElementById('chart-alert-modal');
  if (!ov) return;
  var tickEl = document.getElementById('cam-ticker');
  if (tickEl) tickEl.textContent = currentTicker || '—';
  var valIn = document.getElementById('cam-value');
  var cond = document.getElementById('cam-condition');
  var trig = document.getElementById('cam-trigger');
  var exp = document.getElementById('cam-expires');
  var msg = document.getElementById('cam-message');
  if (valIn) {
    if (prefillPrice != null && isFinite(prefillPrice)) valIn.value = String(prefillPrice);
    else if (_lastCrosshairPrice != null && isFinite(_lastCrosshairPrice)) valIn.value = String(_lastCrosshairPrice);
    else valIn.value = '';
  }
  if (cond) cond.value = 'crossing';
  if (trig) { trig.value = 'once'; _onCamConditionChange(); }
  if (exp) {
    var d = new Date();
    d.setDate(d.getDate() + 30);
    exp.value = d.toISOString().slice(0, 16);
  }
  if (msg) {
    msg.dataset.userEdited = '0';
    msg.value = '';
    var p = parseFloat(valIn && valIn.value);
    if (!isNaN(p) && isFinite(p)) msg.value = _defaultCamMessage(currentTicker, cond ? cond.value : 'crossing', p);
  }
  ov.classList.add('active');
  ov.setAttribute('aria-hidden', 'false');
  if (valIn) setTimeout(function() { valIn.focus(); valIn.select && valIn.select(); }, 50);
}

function _openChartAlertModalForEdit(a) {
  if (!a) return;
  var eid = document.getElementById('cam-edit-id');
  if (eid) eid.value = a.id;
  var sub = document.getElementById('cam-submit-btn');
  if (sub) sub.textContent = 'Save changes';
  var ov = document.getElementById('chart-alert-modal');
  if (!ov) return;
  var tickEl = document.getElementById('cam-ticker');
  if (tickEl) tickEl.textContent = a.ticker || currentTicker || '—';
  var valIn = document.getElementById('cam-value');
  var cond = document.getElementById('cam-condition');
  var trig = document.getElementById('cam-trigger');
  var exp = document.getElementById('cam-expires');
  var msg = document.getElementById('cam-message');
  var toastCb = document.getElementById('cam-toast');
  var soundCb = document.getElementById('cam-sound');
  if (valIn) valIn.value = String(a.price);
  if (cond) cond.value = a.condition || 'crossing';
  if (trig) {
    trig.value = a.triggerOnce === false ? 'repeat' : 'once';
    _onCamConditionChange();
  }
  if (exp) {
    if (a.expireAt) {
      exp.value = new Date(a.expireAt).toISOString().slice(0, 16);
    } else {
      exp.value = '';
    }
  }
  if (msg) {
    msg.dataset.userEdited = '1';
    msg.value = a.message || '';
  }
  if (toastCb) toastCb.checked = a.toast !== false;
  if (soundCb) soundCb.checked = a.sound !== false;
  ov.classList.add('active');
  ov.setAttribute('aria-hidden', 'false');
  if (valIn) setTimeout(function() { valIn.focus(); valIn.select && valIn.select(); }, 50);
}

function _closeChartAlertModal() {
  var ov = document.getElementById('chart-alert-modal');
  if (ov) { ov.classList.remove('active'); ov.setAttribute('aria-hidden', 'true'); }
  var eid = document.getElementById('cam-edit-id');
  if (eid) eid.value = '';
  var sub = document.getElementById('cam-submit-btn');
  if (sub) sub.textContent = 'Create alert';
}

function _submitChartAlertModal() {
  var valIn = document.getElementById('cam-value');
  var cond = document.getElementById('cam-condition');
  var trig = document.getElementById('cam-trigger');
  var exp = document.getElementById('cam-expires');
  var msg = document.getElementById('cam-message');
  var toastCb = document.getElementById('cam-toast');
  var soundCb = document.getElementById('cam-sound');
  var editEl = document.getElementById('cam-edit-id');
  var editId = editEl && editEl.value ? editEl.value : '';
  if (!candleSeries || !currentTicker) { _closeChartAlertModal(); return; }
  var price = valIn ? parseFloat(valIn.value) : NaN;
  if (isNaN(price) || !isFinite(price)) {
    alert('Enter a valid price.');
    return;
  }
  var condition = cond ? cond.value : 'crossing';
  var triggerOnce = !trig || trig.value !== 'repeat';
  var expireAt = null;
  if (exp && exp.value) {
    var ts = new Date(exp.value).getTime();
    if (!isNaN(ts)) expireAt = ts;
  }
  var message = msg ? msg.value.trim() : '';
  if (!message) message = _defaultCamMessage(currentTicker, condition, price);
  var toastOk = toastCb ? toastCb.checked : true;
  var soundOk = soundCb ? soundCb.checked : true;

  if (editId) {
    var ei = _userPriceAlertIndexById(editId);
    if (ei < 0) { _closeChartAlertModal(); return; }
    var a = _userPriceAlerts[ei];
    a.price = price;
    a.condition = condition;
    a.triggerOnce = triggerOnce;
    a.expireAt = expireAt;
    a.message = message;
    a.toast = toastOk;
    a.sound = soundOk;
    if (a.lineHandle) {
      try {
        a.lineHandle.applyOptions({
          price: price,
          title: _chartAlertLineTitle(message, price),
          color: a.color || '#f97316',
        });
      } catch (e) {}
    }
    _persistUserAlertsForTicker(currentTicker);
    _closeChartAlertModal();
    _syncUserAlertHandlesDOM();
    _scheduleUserAlertHandleLayout();
    return;
  }

  var id = String(Date.now()) + '_' + Math.random().toString(36).slice(2, 9);
  var spec = {
    id: id, ticker: currentTicker, price: price, condition: condition, triggerOnce: triggerOnce,
    expireAt: expireAt, message: message, toast: toastOk, sound: soundOk, color: '#f97316',
  };
  var line = _userAlertLinesGloballyVisible() ? _createUserAlertLine(spec) : null;
  var lastClose = null;
  if (_rawOhlcData && _rawOhlcData.length) lastClose = _rawOhlcData[_rawOhlcData.length - 1].close;
  _userPriceAlerts.push({
    id: id, ticker: currentTicker, price: price, condition: condition, triggerOnce: triggerOnce,
    expireAt: expireAt, message: message, toast: toastOk, sound: soundOk,
    paused: false, color: '#f97316',
    lineHandle: line, handleEl: null,
    prevRefPrice: lastClose != null && isFinite(lastClose) ? lastClose : null, fired: false,
  });
  _persistUserAlertsForTicker(currentTicker);
  _closeChartAlertModal();
  _syncUserAlertHandlesDOM();
  _scheduleUserAlertHandleLayout();
}

function addChartAlert(price) {
  _openChartAlertModal(price);
}

function _promptChartAlert() {
  _openChartAlertModal(null);
}

function _initChartAlertModal() {
  var msg = document.getElementById('cam-message');
  var valIn = document.getElementById('cam-value');
  if (msg) {
    msg.addEventListener('input', function() { msg.dataset.userEdited = '1'; });
  }
  if (valIn) valIn.addEventListener('input', function() { _syncCamMessageFromForm(); });
}

function toggleComparePanel() {
  var panel = document.getElementById('compare-panel');
  if (panel) { panel.remove(); return; }
  panel = document.createElement('div');
  panel.id = 'compare-panel';
  panel.className = 'compare-panel';
  panel.innerHTML = '<div style="font-size:11px;font-weight:600;margin-bottom:6px;color:var(--text-muted)">Compare Symbols</div>' +
    '<div style="display:flex;gap:4px"><input type="text" id="compare-input" placeholder="Ticker..." style="flex:1;padding:4px 8px;font-size:11px;background:var(--bg-primary);border:1px solid var(--border);color:var(--text);border-radius:4px;"/>' +
    '<button onclick="_doCompare()" style="padding:4px 10px;font-size:11px;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer;">Add</button></div>' +
    '<div id="compare-list" style="margin-top:6px;"></div>';
  var chartArea = document.querySelector('.t-chart-area');
  if (chartArea) { chartArea.style.position = 'relative'; chartArea.appendChild(panel); }
  var inp = document.getElementById('compare-input');
  if (inp) { inp.focus(); inp.addEventListener('keydown', function(e){ if(e.key==='Enter') _doCompare(); }); }
}

function _doCompare() {
  var inp = document.getElementById('compare-input');
  if (!inp) return;
  var ticker = inp.value.trim().toUpperCase();
  if (!ticker) return;
  inp.value = '';
  addCompareSymbol(ticker);
  _renderCompareList();
}

function _renderCompareList() {
  var el = document.getElementById('compare-list');
  if (!el) return;
  var tickers = Object.keys(_compareSeries);
  el.innerHTML = tickers.map(function(t) {
    var c = _compareSeries[t].color;
    return '<div style="display:flex;align-items:center;gap:4px;font-size:11px;margin-top:3px;">' +
      '<span style="width:8px;height:8px;border-radius:50%;background:' + c + ';flex-shrink:0;"></span>' +
      '<span style="flex:1;color:var(--text)">' + t + '</span>' +
      '<button onclick="removeCompareSymbol(\'' + t + '\');_renderCompareList();" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:10px;">✕</button>' +
      '</div>';
  }).join('');
}

function _drawVolProfile() {
  var entry = indicatorSeries['vol_profile'] || indicatorSeries['volume_profile'];
  if (!entry || !entry.volProfile || !_drawCtx || !_drawCanvas || !chart || !candleSeries) return;
  var data = entry.volProfile;
  var ctx = _drawCtx;
  var w = _drawCanvas.width;
  var maxBarWidth = w * 0.15;
  data.forEach(function(bin) {
    var yPt = candleSeries.priceToCoordinate(bin.price);
    if (yPt == null) return;
    var barW = bin.pct * maxBarWidth;
    var barH = 4;
    var alpha = 0.15 + bin.pct * 0.35;
    ctx.fillStyle = 'rgba(100,116,139,' + alpha + ')';
    ctx.fillRect(w - barW - 2, yPt - barH/2, barW, barH);
  });
}

function _updateWatermark() {
  if (!chart) return;
  var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  chart.applyOptions({
    watermark: {
      visible: true, text: currentTicker || '',
      fontSize: 48, fontFamily: '-apple-system, BlinkMacSystemFont, sans-serif',
      color: isDark ? 'rgba(255,255,255,.04)' : 'rgba(0,0,0,.03)',
    }
  });
}

function _initOhlcTooltip(chartEl) {
  var tip = document.createElement('div');
  tip.id = 'ohlc-tooltip';
  tip.style.cssText = 'position:absolute;top:6px;left:8px;font-size:11px;font-family:monospace;display:flex;gap:10px;pointer-events:none;color:var(--text-secondary);background:var(--bg-header);padding:2px 8px;border-radius:6px;border:1px solid var(--border);opacity:0;transition:opacity .15s;white-space:nowrap;';
  chartEl.appendChild(tip);

  chart.subscribeCrosshairMove(function(param) {
    if (!param.time || !param.seriesData || !param.seriesData.size) {
      tip.style.opacity = '0';
      return;
    }
    var d = param.seriesData.get(candleSeries);
    if (!d) { tip.style.opacity = '0'; return; }
    var _isCr = (currentTicker||'').indexOf('-USD') !== -1;
    tip.style.opacity = '1';
    if (d.open != null && d.close != null) {
    _lastCrosshairPrice = d.close;
    var chg = d.close - d.open;
    var pct = d.open !== 0 ? ((chg / d.open) * 100).toFixed(2) : '0.00';
    var clr = chg >= 0 ? '#22c55e' : '#ef4444';
    var sign = chg >= 0 ? '+' : '';
    tip.innerHTML =
      '<span>O <b>' + smartPrice(d.open, _isCr) + '</b></span>' +
      '<span>H <b>' + smartPrice(d.high, _isCr) + '</b></span>' +
      '<span>L <b>' + smartPrice(d.low, _isCr) + '</b></span>' +
      '<span>C <b style="color:' + clr + '">' + smartPrice(d.close, _isCr) + '</b></span>' +
      '<span style="color:' + clr + '">' + sign + smartPrice(chg, _isCr) + ' (' + sign + pct + '%)</span>';
    } else if (d.value != null) {
      _lastCrosshairPrice = d.value;
      tip.innerHTML = '<span>Price <b>' + smartPrice(d.value, _isCr) + '</b></span>';
    }
  });
}

/* ── Chart data ─────────────────────────────────── */
function _showChartOverlay(msg) {
  var ov = document.getElementById('chart-overlay');
  if (ov) { ov.classList.remove('hidden'); ov.querySelector('.chart-loading-text').textContent = msg || 'Loading chart data...'; }
}
function _hideChartOverlay() {
  var ov = document.getElementById('chart-overlay');
  if (ov) ov.classList.add('hidden');
}

var _pendingAnnotationFn = null;

function loadChart() {
  var reqTicker = currentTicker;
  document.getElementById('ticker-label').textContent = currentTicker;
  _updateWatermark();
  _showChartOverlay('Loading ' + currentTicker + '...');
  _ohlcvBarsTicker = null;
  refreshPrice();
  fetch('/api/trading/ohlcv?ticker=' + encodeURIComponent(currentTicker) + '&interval=' + currentInterval + '&period=' + currentPeriod)
    .then(function(r) { return r.json(); })
    .then(function(d) {
      _hideChartOverlay();
      if (currentTicker !== reqTicker) return;
      if (!d.ok || !d.data || !d.data.length) {
        document.getElementById('price-label').innerHTML = '<em style="font-size:11px;color:#ef4444">No data for ' + currentTicker + '</em>';
        candleSeries.setData([]);
        volumeSeries.setData([]);
        _rawOhlcData = [];
        _ohlcvBarsTicker = null;
        _hydrateUserPriceAlertsAfterLoad();
        _pendingAnnotationFn = null;
        return;
      }
      clearAnnotations(true);
      _rawOhlcData = d.data;
      _ohlcvBarsTicker = reqTicker;
      _chartBarTimes = d.data.map(function(c) { return c.time; });
      _lastBarTime = _chartBarTimes.length ? _chartBarTimes[_chartBarTimes.length - 1] : null;
      _applyChartType(d.data);
      volumeSeries.setData(d.data.map(function(c) { return {time:c.time, value:c.volume, color: c.close>=c.open ? 'rgba(34,197,94,.3)' : 'rgba(239,68,68,.3)'}; }));
      var lastPrice = d.data[d.data.length - 1].close;
      candleSeries.applyOptions({ priceFormat: { type: 'price', minMove: smartMinMove(lastPrice) } });
      chart.timeScale().fitContent();
      _reconnectLiveForTicker();
      loadActiveIndicators();
      _restoreAnnotations();
      _resizeDrawCanvas();
      if (_pendingAnnotationFn) {
        try { _pendingAnnotationFn(d.data); } catch(e) { console.error('[loadChart] annotation callback error:', e); }
        _pendingAnnotationFn = null;
      }
      _hydrateUserPriceAlertsAfterLoad();
      _paintToolbarPriceFromOhlcvFallback();
      refreshPrice();
    }).catch(function() {
      _hideChartOverlay();
      _pendingAnnotationFn = null;
      _detachUserPriceAlertLines();
      _ohlcvBarsTicker = null;
      if (currentTicker === reqTicker) {
        document.getElementById('price-label').innerHTML = '<em style="font-size:11px;color:#ef4444">Error loading chart</em>';
      }
    });
}

var _lastLiveDisplayedPrice = null;
var _priceLabelFlashTimer = null;

function _pulsePriceLabel(direction) {
  var lbl = document.getElementById('price-label');
  if (!lbl) return;
  lbl.classList.remove('price-flash-up', 'price-flash-down');
  void lbl.offsetWidth;
  lbl.classList.add(direction === 'up' ? 'price-flash-up' : 'price-flash-down');
  if (_priceLabelFlashTimer) clearTimeout(_priceLabelFlashTimer);
  _priceLabelFlashTimer = setTimeout(function() {
    lbl.classList.remove('price-flash-up', 'price-flash-down');
    _priceLabelFlashTimer = null;
  }, 820);
}

function refreshPrice() {
  var t = (currentTicker && String(currentTicker).trim()) || '';
  if (!t) return;
  fetch('/api/trading/quote?ticker=' + encodeURIComponent(t))
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var lbl = document.getElementById('price-label');
      if (t !== currentTicker) return;
      if (!d.ok || d.price == null) {
        if (d && d.broker_held) {
          lbl.innerHTML = '<strong>--</strong> <span class="change down">broker quote unavailable</span>';
          _lastLiveDisplayedPrice = null;
          _priceCache[currentTicker] = d;
          return;
        }
        if (!_paintToolbarPriceFromOhlcvFallback()) {
          lbl.innerHTML = '<strong>--</strong>';
          _lastLiveDisplayedPrice = null;
        }
        return;
      }
      var prev = _lastLiveDisplayedPrice;
      var p = parseFloat(d.price);
      _priceCache[currentTicker] = d;
      var cls = (d.change_pct||0) >= 0 ? 'up' : 'down';
      var sign = (d.change_pct||0) >= 0 ? '+' : '';
      lbl.innerHTML = '<strong>$' + d.price + '</strong> <span class="change ' + cls + '">' + sign + (d.change||0) + ' (' + sign + (d.change_pct||0) + '%)</span>';
      if (prev != null && isFinite(prev) && isFinite(p) && p !== prev) {
        if (p > prev) _pulsePriceLabel('up');
        else if (p < prev) _pulsePriceLabel('down');
      }
      if (isFinite(p)) _lastLiveDisplayedPrice = p;
    }).catch(function() {
      var lbl = document.getElementById('price-label');
      if (t !== currentTicker) return;
      if (!_paintToolbarPriceFromOhlcvFallback()) {
        lbl.innerHTML = '<strong>--</strong>';
        _lastLiveDisplayedPrice = null;
      }
    });
}

/* ── Real-time WebSocket: live chart ticks + alert push ──────────── */

var _liveWS = null;
var _liveWSReconnectDelay = 1000;
var _liveWSReconnectTimer = null;
var _liveWSEnabled = true;
var _liveCurrentBar = null;
var _liveBarVolume = 0;
var _liveWSTicker = '';

function _bucketTime(epochSec, intervalStr) {
  var secs = {'1m':60,'2m':120,'5m':300,'15m':900,'30m':1800,
              '1h':3600,'4h':14400,'1d':86400,'1wk':604800,'1mo':2592000}[intervalStr] || 60;
  return Math.floor(epochSec / secs) * secs;
}

function _connectLiveWS() {
  if (!_liveWSEnabled) return;
  var targetTicker = (currentTicker && String(currentTicker).trim()) || '';
  if (!targetTicker) return;
  if (_liveWS && _liveWSTicker === targetTicker &&
      (_liveWS.readyState === WebSocket.OPEN || _liveWS.readyState === WebSocket.CONNECTING)) {
    return;
  }
  if (_liveWSReconnectTimer) { clearTimeout(_liveWSReconnectTimer); _liveWSReconnectTimer = null; }
  if (_liveWS) { try { _liveWS.close(); } catch(e) {} _liveWS = null; }
  _liveWSTicker = targetTicker;
  _liveCurrentBar = null;
  _liveBarVolume = 0;

  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  var url = proto + '//' + location.host + '/ws/trading/live?ticker=' + encodeURIComponent(targetTicker);
  _liveWS = new WebSocket(url);

  _liveWS.onopen = function() {
    console.log('[live-ws] Connected for', targetTicker);
    _liveWSReconnectDelay = 1000;
    var dot = document.getElementById('live-dot');
    if (dot) dot.className = 'live-dot connected';
  };

  _liveWS.onmessage = function(evt) {
    try {
      var msg = JSON.parse(evt.data);
    } catch(e) { return; }

    if (msg.type === 'tick') {
      _handleLiveTick(msg);
    } else if (msg.type === 'alert') {
      _showAlertToast(msg);
    }
  };

  _liveWS.onclose = function() {
    console.log('[live-ws] Disconnected');
    var dot = document.getElementById('live-dot');
    if (dot) dot.className = 'live-dot disconnected';
    if (_liveWSEnabled && _liveWSTicker === targetTicker) {
      _liveWSReconnectTimer = setTimeout(function() {
        _liveWSTicker = '';
        _connectLiveWS();
      }, _liveWSReconnectDelay);
      _liveWSReconnectDelay = Math.min(_liveWSReconnectDelay * 2, 30000);
    }
  };

  _liveWS.onerror = function() {};
}

function _handleLiveTick(msg) {
  var price = msg.price;
  var size = msg.size || 0;
  if (!price || !candleSeries) return;

  var now = msg.time || (Date.now() / 1000);
  var bucket = _bucketTime(now, currentInterval);

  if (_lastBarTime && bucket <= _lastBarTime) {
    bucket = _lastBarTime;
  }

  if (!_liveCurrentBar || _liveCurrentBar.time !== bucket) {
    if (_liveCurrentBar && bucket > _liveCurrentBar.time) {
      _lastBarTime = bucket;
      _chartBarTimes.push(bucket);
      _rawOhlcData.push({
        time: bucket, open: price, high: price, low: price, close: price, volume: size
      });
    } else if (!_liveCurrentBar && _rawOhlcData.length) {
      var lastRaw = _rawOhlcData[_rawOhlcData.length - 1];
      if (bucket === lastRaw.time) {
        _liveCurrentBar = { time: bucket, open: lastRaw.open, high: Math.max(lastRaw.high, price), low: Math.min(lastRaw.low, price), close: price };
        _liveBarVolume = lastRaw.volume + size;
      }
    }
    if (!_liveCurrentBar || _liveCurrentBar.time !== bucket) {
      _liveCurrentBar = { time: bucket, open: price, high: price, low: price, close: price };
      _liveBarVolume = size;
    }
  } else {
    _liveCurrentBar.high = Math.max(_liveCurrentBar.high, price);
    _liveCurrentBar.low = Math.min(_liveCurrentBar.low, price);
    _liveCurrentBar.close = price;
    _liveBarVolume += size;
    if (_rawOhlcData.length) {
      var last = _rawOhlcData[_rawOhlcData.length - 1];
      if (last.time === bucket) {
        last.high = _liveCurrentBar.high;
        last.low = _liveCurrentBar.low;
        last.close = price;
        last.volume = _liveBarVolume;
      }
    }
  }

  try {
    if (currentChartType === 'candles' || currentChartType === 'hollow' || currentChartType === 'bars' || currentChartType === 'heikin_ashi') {
      candleSeries.update(_liveCurrentBar);
    } else {
      candleSeries.update({ time: _liveCurrentBar.time, value: price });
    }
    volumeSeries.update({
      time: _liveCurrentBar.time,
      value: _liveBarVolume,
      color: _liveCurrentBar.close >= _liveCurrentBar.open ? 'rgba(34,197,94,.3)' : 'rgba(239,68,68,.3)'
    });
  } catch(e) {
    console.warn('[live-ws] Chart update error:', e.message, 'bar:', _liveCurrentBar);
  }

  var lbl = document.getElementById('price-label');
  if (lbl) {
    var prevTick = _lastLiveDisplayedPrice;
    var prevData = _priceCache[currentTicker];
    var prevClose = prevData ? (prevData.previous_close || prevData.price) : null;
    if (prevClose) {
      var chg = price - prevClose;
      var chgPct = ((chg / prevClose) * 100).toFixed(2);
      var cls = chg >= 0 ? 'up' : 'down';
      var sign = chg >= 0 ? '+' : '';
      lbl.innerHTML = '<strong>$' + price.toFixed(2) + '</strong> <span class="change ' + cls + '">' + sign + chg.toFixed(2) + ' (' + sign + chgPct + '%)</span>';
    } else {
      lbl.innerHTML = '<strong>$' + price.toFixed(2) + '</strong>';
    }
    if (prevTick != null && isFinite(prevTick) && isFinite(price)) {
      if (price > prevTick) _pulsePriceLabel('up');
      else if (price < prevTick) _pulsePriceLabel('down');
    }
    _lastLiveDisplayedPrice = price;
  }
  _evaluateUserPriceAlertsOnTick(price);
}

function _disconnectLiveWS() {
  _liveWSEnabled = false;
  if (_liveWSReconnectTimer) { clearTimeout(_liveWSReconnectTimer); _liveWSReconnectTimer = null; }
  if (_liveWS) { try { _liveWS.close(); } catch(e) {} _liveWS = null; }
  var dot = document.getElementById('live-dot');
  if (dot) dot.className = 'live-dot disconnected';
}

function _reconnectLiveForTicker() {
  _liveWSEnabled = true;
  _liveWSReconnectDelay = 1000;
  _connectLiveWS();
}

var _alertSoundEnabled = true;
var _toastAutoDismissMs = 10000;

function _showAlertToast(msg, skipSound) {
  var container = document.getElementById('alert-toast-container');
  if (!container) return;

  var toast = document.createElement('div');
  toast.className = 'alert-toast';

  var ticker = msg.ticker || '???';
  var tier = msg.alert_type || msg.tier || '';
  var price = msg.price != null ? '$' + Number(msg.price).toFixed(2) : '';
  var body = msg.message || '';

  toast.innerHTML =
    '<span class="at-ticker">' + ticker + '</span>' +
    '<div class="at-body">' +
      (tier ? '<div class="at-tier">' + tier + '</div>' : '') +
      (price ? '<div class="at-price">' + price + '</div>' : '') +
      (body ? '<div class="at-msg">' + body.substring(0, 120) + '</div>' : '') +
    '</div>' +
    '<button class="at-close" onclick="event.stopPropagation(); this.parentElement.remove();">&times;</button>';

  toast.addEventListener('click', function() {
    currentTicker = ticker;
    loadChart();
    toast.remove();
  });

  container.appendChild(toast);

  if (_alertSoundEnabled && !skipSound) {
    try {
      var ac = new (window.AudioContext || window.webkitAudioContext)();
      var osc = ac.createOscillator();
      var gain = ac.createGain();
      osc.connect(gain); gain.connect(ac.destination);
      osc.type = 'sine'; osc.frequency.value = 880;
      gain.gain.value = 0.1;
      osc.start(); osc.stop(ac.currentTime + 0.12);
    } catch(e) {}
  }

  setTimeout(function() {
    toast.classList.add('dismissing');
    setTimeout(function() { toast.remove(); }, 300);
  }, _toastAutoDismissMs);

  while (container.children.length > 5) {
    container.removeChild(container.firstChild);
  }
}

function _syncIntervalUi(v) {
  document.querySelectorAll('.interval-pop-item').forEach(function(b) {
    var on = b.getAttribute('data-interval') === v;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
}
function changeInterval(v) {
  currentInterval = v;
  _syncIntervalUi(v);
  loadChart();
  if (_multiViewActive && currentTicker) loadMultiCharts(currentTicker);
}



var _selectDebounce = null;
function selectTicker(t) {
  _detachUserPriceAlertLines();
  currentTicker = t.toUpperCase();
  _activeBreakoutRow = null;
  clearAnnotations();
  _loadDrawings();
  document.querySelectorAll('.wl-item').forEach(function(el) { el.classList.toggle('active', el.dataset.ticker === currentTicker); });
  var tickerLabel = document.getElementById('ticker-label');
  tickerLabel.innerHTML = currentTicker + '<span class="ticker-spinner"></span>';
  var chartArea = document.getElementById('chart-area');
  if (chartArea) { chartArea.classList.remove('glow'); void chartArea.offsetWidth; chartArea.classList.add('glow'); }
  var pl = document.getElementById('price-label');
  if (pl) pl.classList.remove('price-flash-up', 'price-flash-down');
  if (_priceLabelFlashTimer) { clearTimeout(_priceLabelFlashTimer); _priceLabelFlashTimer = null; }
  var cached = _priceCache[currentTicker];
  if (cached && cached.price != null) {
    var cls = (cached.change_pct||0) >= 0 ? 'up' : 'down';
    var sign = (cached.change_pct||0) >= 0 ? '+' : '';
    document.getElementById('price-label').innerHTML = '<strong>$' + cached.price + '</strong> <span class="change ' + cls + '">' + sign + (cached.change||0) + ' (' + sign + (cached.change_pct||0) + '%)</span>';
    var cp = parseFloat(cached.price);
    _lastLiveDisplayedPrice = isFinite(cp) ? cp : null;
  } else if (cached && cached.broker_held) {
    document.getElementById('price-label').innerHTML = '<strong>--</strong> <span class="change down">broker quote unavailable</span>';
    _lastLiveDisplayedPrice = null;
  } else {
    _lastLiveDisplayedPrice = null;
  }
  if (_selectDebounce) clearTimeout(_selectDebounce);
  _selectDebounce = setTimeout(function() {
    loadChart();
    if (_multiViewActive) loadMultiCharts(currentTicker);
    loadTickerDetail(currentTicker);
    loadTickerNews(currentTicker);
  }, 200);
  if (window.innerWidth <= 768) closeAllDrawers();
}

function loadTickerDetail(ticker) {
  var section = document.getElementById('ticker-detail-section');
  var bar = document.getElementById('ticker-detail-bar');
  if (!section || !bar) return;
  section.style.display = 'flex';
  bar.innerHTML = '<div class="td-row"><span class="td-ticker">' + escHtml(ticker) + '</span>' +
    '<span class="td-skeleton"><span class="td-bone" style="width:60px"></span><span class="td-bone" style="width:45px"></span></span></div>';
  fetch('/api/trading/ticker-info?ticker=' + encodeURIComponent(ticker))
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d.ok || !d.info) {
        bar.innerHTML = '<div class="td-row"><span class="td-ticker">' + escHtml(ticker) + '</span></div>';
        return;
      }
      var i = d.info;
      var row1 = '<div class="td-row"><span class="td-ticker">' + escHtml(ticker) + '</span>';
      row1 += '<span class="td-company">' + escHtml(i.name || ticker) + '</span>';
      if (i.sector_or_type && i.sector_or_type !== '\u2014') row1 += '<span class="td-sector">' + escHtml(i.sector_or_type) + '</span>';
      row1 += '</div>';
      var row2 = '<div class="td-row">';
      if (i.market_cap_fmt) row2 += '<span class="td-tag">MCap <b>' + escHtml(i.market_cap_fmt) + '</b></span>';
      if (i.pe != null && i.pe > 0) row2 += '<span class="td-tag">P/E <b>' + i.pe.toFixed(1) + '</b></span>';
      row2 += '</div>';
      bar.innerHTML = row1 + row2;
    })
    .catch(function() {
      bar.innerHTML = '<div class="td-row"><span class="td-ticker">' + escHtml(ticker) + '</span></div>';
    });
}

function openNewsReader(url, title, source, pubDate) {
  if (!url || url === '#') return;
  var overlay = document.getElementById('news-reader-overlay');
  var titleEl = document.getElementById('nr-title');
  var sourceEl = document.getElementById('nr-source');
  var openEl = document.getElementById('nr-open');
  var primaryOpen = document.getElementById('nr-primary-open');
  var previewTitle = document.getElementById('nr-preview-title');
  var previewMeta = document.getElementById('nr-preview-meta');
  if (!overlay) return;
  if (titleEl) titleEl.textContent = title || '';
  if (sourceEl) sourceEl.textContent = source || '';
  if (openEl) openEl.href = url;
  if (primaryOpen) primaryOpen.href = url;
  if (previewTitle) previewTitle.textContent = title || '';
  if (previewMeta) {
    var metaParts = [];
    if (source) metaParts.push(source);
    if (pubDate) metaParts.push(pubDate);
    previewMeta.textContent = metaParts.join(' · ');
  }
  overlay.classList.add('active');
}

function closeNewsReader() {
  var overlay = document.getElementById('news-reader-overlay');
  if (!overlay) return;
  overlay.classList.remove('active');
}

function loadTickerNews(ticker) {
  var container = document.getElementById('ticker-news-cards');
  if (!container) return;
  container.innerHTML = '<div class="news-card" style="min-height:40px"><span class="td-bone" style="width:140px;height:8px;display:block;margin-bottom:6px"></span><span class="td-bone" style="width:70px;height:6px;display:block"></span></div>' +
    '<div class="news-card" style="min-height:40px"><span class="td-bone" style="width:120px;height:8px;display:block;margin-bottom:6px"></span><span class="td-bone" style="width:60px;height:6px;display:block"></span></div>' +
    '<div class="news-card" style="min-height:40px"><span class="td-bone" style="width:150px;height:8px;display:block;margin-bottom:6px"></span><span class="td-bone" style="width:50px;height:6px;display:block"></span></div>';
  fetch('/api/trading/news?ticker=' + encodeURIComponent(ticker) + '&limit=8')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d.ok || !d.news || !d.news.length) { container.innerHTML = ''; return; }
      var html = '';
      d.news.forEach(function(n) {
        var rawUrl = (n.url || '').trim();
        var escJsAttr = function(s) {
          return String(s || '')
            .replace(/\\/g, '\\\\')
            .replace(/\r?\n/g, ' ')
            .replace(/'/g, "\\'")
            .replace(/"/g, '&quot;');
        };
        var url = rawUrl ? escJsAttr(rawUrl) : '#';
        var rawTitle = (n.title || '').substring(0, 100);
        var title = escHtml(rawTitle);
        var src = n.publisher ? escHtml(n.publisher) : '';
        var safeTitle = escJsAttr(rawTitle);
        var safeSrc = escJsAttr(n.publisher || '');
        var safeDate = escJsAttr(n.date || '');
        var sent = n.sentiment || 'neutral';
        var badge = sent !== 'neutral' ? '<span class="nc-badge ' + sent + '">' + sent + '</span>' : '';
        html += '<div class="news-card" onclick="openNewsReader(\'' + url + '\',\'' + safeTitle + '\',\'' + safeSrc + '\',\'' + safeDate + '\')" style="cursor:pointer">';
        html += badge;
        html += '<div class="nc-title">' + title + '</div>';
        if (src) html += '<div class="nc-meta">' + src + '</div>';
        html += '</div>';
      });
      container.innerHTML = html;
    })
    .catch(function() { container.innerHTML = ''; });
}

var _priceCache = {};
function prefetchWatchlistPrices() {
  var items = document.querySelectorAll('.wl-item');
  var tickers = [];
  items.forEach(function(el) {
    var t = el.dataset.ticker;
    if (t && !_priceCache[t]) tickers.push(t);
  });
  if (!tickers.length) return;
  fetch('/api/trading/quotes/batch?tickers=' + encodeURIComponent(tickers.join(',')))
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d.ok || !d.quotes) return;
      Object.keys(d.quotes).forEach(function(ticker) {
        var q = d.quotes[ticker];
        _priceCache[ticker] = q;
        var el = document.querySelector('.wl-item[data-ticker="'+ticker+'"]');
        if (el) {
          var priceEl = el.querySelector('.wl-price');
          if (priceEl) {
            if (q.price == null) {
              priceEl.innerHTML = '<span class="wl-price-muted">--</span>';
            } else {
              var cls = (q.change_pct||0) >= 0 ? 'up' : 'down';
              var sign = (q.change_pct||0) >= 0 ? '+' : '';
              priceEl.innerHTML = '$' + q.price + ' <small class="' + cls + '">' + sign + (q.change_pct||0) + '%</small>';
            }
          }
        }
      });
    }).catch(function() {});
}

function clearAllActiveIndicators() {
  var ids = Array.from(activeIndicators);
  ids.forEach(function(id) {
    activeIndicators.delete(id);
    removeIndicatorSeries(id);
  });
  buildIndicatorPanel();
}

function _chartCtxResetView() {
  if (chart) chart.timeScale().fitContent();
}

function _closeChartCtxMenu() {
  var m = document.getElementById('chart-context-menu');
  if (!m) return;
  m.classList.add('hidden');
  m.setAttribute('aria-hidden', 'true');
  var fly = document.getElementById('ctx-template-flyout');
  var tog = document.getElementById('ctx-template-toggle');
  if (fly) fly.classList.add('hidden');
  if (tog) tog.setAttribute('aria-expanded', 'false');
  document.removeEventListener('mousedown', _chartCtxDocMouse, true);
  document.removeEventListener('keydown', _chartCtxEsc, true);
}

function _chartCtxDocMouse(ev) {
  var m = document.getElementById('chart-context-menu');
  if (!m || m.classList.contains('hidden')) return;
  if (m.contains(ev.target)) return;
  _closeChartCtxMenu();
}

function _chartCtxEsc(ev) {
  if (ev.key !== 'Escape') return;
  _closeChartCtxMenu();
  _closeChartAlertCtxMenu();
  _closeChartTableModal();
  _closeChartObjModal();
  _closeChartSettingsModal();
  _closeChartAlertModal();
}

function _positionChartCtxMenu(clientX, clientY) {
  var m = document.getElementById('chart-context-menu');
  if (!m) return;
  m.classList.remove('hidden');
  m.setAttribute('aria-hidden', 'false');
  var w = m.offsetWidth || 260;
  var h = m.offsetHeight || 200;
  var x = clientX;
  var y = clientY;
  if (x + w > window.innerWidth - 8) x = window.innerWidth - w - 8;
  if (y + h > window.innerHeight - 8) y = window.innerHeight - h - 8;
  if (x < 8) x = 8;
  if (y < 8) y = 8;
  m.style.left = x + 'px';
  m.style.top = y + 'px';
  document.removeEventListener('mousedown', _chartCtxDocMouse, true);
  document.addEventListener('mousedown', _chartCtxDocMouse, true);
  document.removeEventListener('keydown', _chartCtxEsc, true);
  document.addEventListener('keydown', _chartCtxEsc, true);
}

function _openChartCtxMenuAtEvent(e, numericPrice, priceDisplay) {
  _closeChartAlertCtxMenu();
  _ctxMenuPrice = numericPrice;
  var t = currentTicker || '';
  var copyLab = document.getElementById('ctx-copy-label');
  var alertLab = document.getElementById('ctx-alert-label');
  var remLab = document.getElementById('ctx-remove-ind-label');
  var _isCr = t.indexOf('-USD') !== -1;
  if (priceDisplay && copyLab) copyLab.textContent = 'Copy price ' + priceDisplay;
  else if (copyLab) copyLab.textContent = 'Copy price';
  if (alertLab) {
    alertLab.textContent = (numericPrice != null && isFinite(numericPrice))
      ? ('Add alert on ' + t + ' at ' + (priceDisplay || smartPrice(numericPrice, _isCr)))
      : ('Add alert on ' + t + '…');
  }
  var n = activeIndicators.size;
  if (remLab) remLab.textContent = n ? ('Remove ' + n + ' indicator' + (n !== 1 ? 's' : '')) : 'Remove indicators';
  var btnCopy = document.getElementById('ctx-copy-price');
  var btnAlert = document.getElementById('ctx-add-alert');
  if (btnCopy) btnCopy.disabled = !(numericPrice != null && isFinite(numericPrice));
  if (btnAlert) btnAlert.disabled = !(numericPrice != null && isFinite(numericPrice));
  var btnRem = document.getElementById('ctx-remove-indicators');
  if (btnRem) btnRem.disabled = n === 0;
  _positionChartCtxMenu(e.clientX, e.clientY);
}

function _chartCtxCopyPrice() {
  if (_ctxMenuPrice == null || !isFinite(_ctxMenuPrice)) return;
  var t = (currentTicker || '').indexOf('-USD') !== -1;
  var s = String(smartPrice(_ctxMenuPrice, t));
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(s).catch(function() { window.prompt('Copy:', s); });
  } else {
    window.prompt('Copy:', s);
  }
  _closeChartCtxMenu();
}

function _chartCtxPastePrice() {
  if (!navigator.clipboard || !navigator.clipboard.readText) {
    var v = window.prompt('Paste price value:');
    if (v) _applyPastedPriceAlert(v);
    _closeChartCtxMenu();
    return;
  }
  navigator.clipboard.readText().then(function(txt) {
    _applyPastedPriceAlert(txt);
    _closeChartCtxMenu();
  }).catch(function() { _closeChartCtxMenu(); });
}

function _applyPastedPriceAlert(txt) {
  if (!txt) return;
  var cleaned = String(txt).replace(/[$\s,]/g, '').replace(/,/g, '');
  var p = parseFloat(cleaned);
  if (!isNaN(p) && isFinite(p)) addChartAlert(p);
}

function _chartCtxAddAlertFromMenu() {
  if (_ctxMenuPrice != null && isFinite(_ctxMenuPrice)) addChartAlert(_ctxMenuPrice);
  _closeChartCtxMenu();
}

function _chartCtxAddAlertShortcut() {
  if (_lastCrosshairPrice != null && isFinite(_lastCrosshairPrice)) addChartAlert(_lastCrosshairPrice);
  else _promptChartAlert();
}

function _saveChartTemplate() {
  try {
    localStorage.setItem(CHART_TEMPLATE_STORAGE_KEY, JSON.stringify({
      interval: currentInterval,
      period: currentPeriod,
      indicators: Array.from(activeIndicators),
      theme: document.documentElement.getAttribute('data-theme') || 'dark',
    }));
  } catch (err) {}
  _closeChartCtxMenu();
}

function _loadChartTemplate() {
  try {
    var s = localStorage.getItem(CHART_TEMPLATE_STORAGE_KEY);
    if (!s) return;
    var p = JSON.parse(s);
    activeIndicators.clear();
    (p.indicators || []).forEach(function(id) { if (typeof id === 'string') activeIndicators.add(id); });
    buildIndicatorPanel();
    var legacyInt = document.getElementById('interval-select');
    if (legacyInt && typeof changeInterval === 'function' && p.interval) {
      changeInterval(p.interval);
      if (p.period && typeof changePeriod === 'function') changePeriod(p.period);
    } else {
      if (p.interval) {
        currentInterval = p.interval;
        if (typeof _syncIntervalUi === 'function') _syncIntervalUi(currentInterval);
      }
      if (p.period) currentPeriod = p.period;
      loadChart();
    }
    if (p.theme) document.documentElement.setAttribute('data-theme', p.theme);
    if (_multiViewActive && currentTicker) loadMultiCharts(currentTicker);
  } catch (err) {}
  _closeChartCtxMenu();
}

function _resetChartTemplateDefault() {
  try { localStorage.removeItem(CHART_TEMPLATE_STORAGE_KEY); } catch (e) {}
  activeIndicators.clear();
  buildIndicatorPanel();
  currentInterval = '1d';
  currentPeriod = '1y';
  var legacyInt = document.getElementById('interval-select');
  if (legacyInt && typeof changeInterval === 'function') {
    changeInterval('1d');
    if (typeof changePeriod === 'function') changePeriod('1y');
  } else {
    if (typeof _syncIntervalUi === 'function') _syncIntervalUi('1d');
    loadChart();
  }
  if (_multiViewActive && currentTicker) loadMultiCharts(currentTicker);
  _closeChartCtxMenu();
}

function _openChartTableModal() {
  _closeChartCtxMenu();
  var overlay = document.getElementById('chart-table-modal');
  var wrap = document.getElementById('chart-table-wrap');
  var lab = document.getElementById('chart-table-ticker');
  if (!overlay || !wrap) return;
  if (lab) lab.textContent = currentTicker || '—';
  if (!_rawOhlcData || !_rawOhlcData.length) {
    wrap.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">No chart data loaded.</p>';
  } else {
    var _isCr = (currentTicker || '').indexOf('-USD') !== -1;
    var rows = _rawOhlcData.map(function(c) {
      var ts = typeof c.time === 'number' ? c.time : parseInt(c.time, 10);
      var d = new Date(ts * 1000);
      var ds = isNaN(d.getTime()) ? String(c.time) : d.toLocaleString();
      return '<tr><td>' + escHtml(ds) + '</td><td>' + escHtml(String(smartPrice(c.open, _isCr))) + '</td><td>' + escHtml(String(smartPrice(c.high, _isCr))) + '</td><td>' + escHtml(String(smartPrice(c.low, _isCr))) + '</td><td>' + escHtml(String(smartPrice(c.close, _isCr))) + '</td><td>' + escHtml(String(c.volume != null ? c.volume : '')) + '</td></tr>';
    }).join('');
    wrap.innerHTML = '<table><thead><tr><th>Time</th><th>O</th><th>H</th><th>L</th><th>C</th><th>Vol</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  overlay.classList.add('active');
  overlay.setAttribute('aria-hidden', 'false');
}

function _closeChartTableModal() {
  var overlay = document.getElementById('chart-table-modal');
  if (overlay) { overlay.classList.remove('active'); overlay.setAttribute('aria-hidden', 'true'); }
}

function _openChartObjModal() {
  _closeChartCtxMenu();
  var overlay = document.getElementById('chart-obj-modal');
  var body = document.getElementById('chart-obj-modal-body');
  if (!overlay || !body) return;
  var indLines = [];
  activeIndicators.forEach(function(id) {
    var meta = INDICATORS.find(function(i) { return i.id === id; });
    indLines.push(meta ? meta.label : id);
  });
  var indHtml = indLines.length ? indLines.map(function(l) { return '• ' + escHtml(l); }).join('<br/>') : 'No indicators active.';
  var dCount = (_userDrawings && _userDrawings.length) ? _userDrawings.length : 0;
  body.innerHTML =
    '<div class="chart-ctx-obj-list"><strong>Indicators</strong><br/>' + indHtml +
    '<br/><br/><strong>Drawings</strong><br/>' + dCount + ' object(s) on chart (session)</div>' +
    '<div class="chart-ctx-obj-actions">' +
    '<button type="button" onclick="clearAllActiveIndicators();_refreshObjModalIfOpen()">Remove all indicators</button>' +
    '<button type="button" onclick="_clearAllDrawings();_refreshObjModalIfOpen()">Clear drawings</button>' +
    '</div>';
  overlay.classList.add('active');
  overlay.setAttribute('aria-hidden', 'false');
}

function _refreshObjModalIfOpen() {
  var o = document.getElementById('chart-obj-modal');
  if (o && o.classList.contains('active')) _openChartObjModal();
}

function _closeChartObjModal() {
  var overlay = document.getElementById('chart-obj-modal');
  if (overlay) { overlay.classList.remove('active'); overlay.setAttribute('aria-hidden', 'true'); }
}

function _openChartSettingsModal() {
  _closeChartCtxMenu();
  var overlay = document.getElementById('chart-settings-modal');
  var el = document.getElementById('chart-settings-shortcuts');
  if (el) {
    el.textContent = 'L Trend line | Y Ray | H H-line | V V-line | C Channel | R Rectangle | F Fib | A Arrow | N Callout | G Measure | T Text | S Magnet | E Eraser | X Clear drawings | Ctrl+Z Undo';
  }
  if (overlay) { overlay.classList.add('active'); overlay.setAttribute('aria-hidden', 'false'); }
}

function _closeChartSettingsModal() {
  var overlay = document.getElementById('chart-settings-modal');
  if (overlay) { overlay.classList.remove('active'); overlay.setAttribute('aria-hidden', 'true'); }
}

function _scrollToIndPanel() {
  var p = document.getElementById('ind-panel');
  if (p) p.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  _closeChartSettingsModal();
}

function _initChartContextMenu() {
  var area = document.getElementById('chart-area');
  if (!area) return;
  area.addEventListener('contextmenu', function(ev) {
    var grid = document.getElementById('multi-chart-grid');
    if (grid && !grid.classList.contains('hidden')) return;
    var dc = document.getElementById('draw-canvas');
    if (dc && ev.target === dc && dc.classList.contains('drawing') && _drawTool) return;
    var mc = document.getElementById('main-chart');
    if (!mc || !chart || !candleSeries) return;
    var r = mc.getBoundingClientRect();
    if (ev.clientX < r.left || ev.clientX > r.right || ev.clientY < r.top || ev.clientY > r.bottom) return;
    var py = ev.clientY - r.top;
    var hitAlert = _userPriceAlertHitTestPixel(py);
    if (hitAlert >= 0) {
      ev.preventDefault();
      ev.stopPropagation();
      _openChartAlertCtxMenuAtEvent(ev, _userPriceAlerts[hitAlert].id);
      return;
    }
    ev.preventDefault();
    ev.stopPropagation();
    var price = candleSeries.coordinateToPrice(py);
    var t = (currentTicker || '').indexOf('-USD') !== -1;
    var priceStr = null;
    var num = null;
    if (price != null && isFinite(price)) {
      num = price;
      priceStr = smartPrice(price, t);
    }
    if (num == null && _lastCrosshairPrice != null && isFinite(_lastCrosshairPrice)) {
      num = _lastCrosshairPrice;
      priceStr = smartPrice(num, t);
    }
    _openChartCtxMenuAtEvent(ev, num, priceStr);
  }, true);

  var menu = document.getElementById('chart-context-menu');
  if (!menu) return;
  menu.addEventListener('click', function(ev) { ev.stopPropagation(); });
  var elReset = document.getElementById('ctx-reset-view');
  if (elReset) elReset.onclick = function() { _chartCtxResetView(); _closeChartCtxMenu(); };
  var elCopy = document.getElementById('ctx-copy-price');
  if (elCopy) elCopy.onclick = function() { _chartCtxCopyPrice(); };
  var elPaste = document.getElementById('ctx-paste-price');
  if (elPaste) elPaste.onclick = function() { _chartCtxPastePrice(); };
  var elAlert = document.getElementById('ctx-add-alert');
  if (elAlert) elAlert.onclick = function() { _chartCtxAddAlertFromMenu(); };
  var elTbl = document.getElementById('ctx-table-view');
  if (elTbl) elTbl.onclick = function() { _openChartTableModal(); };
  var elObj = document.getElementById('ctx-object-tree');
  if (elObj) elObj.onclick = function() { _openChartObjModal(); };
  var elRem = document.getElementById('ctx-remove-indicators');
  if (elRem) elRem.onclick = function() {
    if (activeIndicators.size) clearAllActiveIndicators();
    _closeChartCtxMenu();
  };
  var elSet = document.getElementById('ctx-chart-settings');
  if (elSet) elSet.onclick = function() { _openChartSettingsModal(); };
  var tplTog = document.getElementById('ctx-template-toggle');
  var tplFly = document.getElementById('ctx-template-flyout');
  if (tplTog && tplFly) {
    tplTog.onclick = function(e) {
      e.stopPropagation();
      var wasHidden = tplFly.classList.contains('hidden');
      tplFly.classList.toggle('hidden', !wasHidden);
      tplTog.setAttribute('aria-expanded', wasHidden ? 'true' : 'false');
    };
    var ts = document.getElementById('ctx-tpl-save');
    var tl = document.getElementById('ctx-tpl-load');
    var tr = document.getElementById('ctx-tpl-reset');
    if (ts) ts.onclick = function(e) { e.stopPropagation(); _saveChartTemplate(); };
    if (tl) tl.onclick = function(e) { e.stopPropagation(); _loadChartTemplate(); };
    if (tr) tr.onclick = function(e) { e.stopPropagation(); _resetChartTemplateDefault(); };
  }
  window.addEventListener('scroll', function() { _closeChartCtxMenu(); _closeChartAlertCtxMenu(); }, true);
  ['chart-table-modal', 'chart-obj-modal', 'chart-settings-modal'].forEach(function(id) {
    var o = document.getElementById(id);
    if (!o) return;
    o.addEventListener('click', function(ev) {
      if (ev.target !== o) return;
      if (id === 'chart-table-modal') _closeChartTableModal();
      else if (id === 'chart-obj-modal') _closeChartObjModal();
      else _closeChartSettingsModal();
    });
  });
}

/* ── Indicators ─────────────────────────────────── */
function buildIndicatorPanel() {
  var panel = document.getElementById('ind-panel');
  panel.innerHTML = '';
  INDICATORS.forEach(function(ind) {
    var chip = document.createElement('div');
    chip.className = 'ind-chip' + (activeIndicators.has(ind.id) ? ' on' : '');
    chip.textContent = ind.label;
    chip.dataset.id = ind.id;
    chip.onclick = function() { toggleIndicator(ind.id, chip); };
    panel.appendChild(chip);
  });
}

function toggleIndicator(id, chip) {
  if (activeIndicators.has(id)) {
    activeIndicators.delete(id);
    chip.classList.remove('on');
    removeIndicatorSeries(id);
  } else {
    activeIndicators.add(id);
    chip.classList.add('on');
    fetchAndDrawIndicator(id);
  }
}

function loadActiveIndicators() {
  Object.keys(indicatorSeries).forEach(function(k) { removeIndicatorSeries(k); });
  if (activeIndicators.size === 0) return;
  var indStr = Array.from(activeIndicators).join(',');
  fetch('/api/trading/indicators?ticker=' + encodeURIComponent(currentTicker) + '&interval=' + currentInterval + '&period=' + currentPeriod + '&indicators=' + indStr)
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d.ok) return;
      var indicators = d.indicators;
      for (var key in indicators) {
        drawIndicator(key, indicators[key]);
      }
    });
}

function fetchAndDrawIndicator(id) {
  fetch('/api/trading/indicators?ticker=' + encodeURIComponent(currentTicker) + '&interval=' + currentInterval + '&period=' + currentPeriod + '&indicators=' + id)
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d.ok) return;
      var indicators = d.indicators;
      for (var key in indicators) { drawIndicator(key, indicators[key]); }
    });
}

function drawIndicator(id, data) {
  if (!data || !data.length) return;
  var meta = INDICATORS.find(function(i) { return i.id === id; }) || { color: '#888' };
  removeIndicatorSeries(id);

  if (id === 'rsi') {
    _showRsiPane();
    if (!_rsiChart) return;
    _rsiSeries = _rsiChart.addLineSeries({
      color: '#22c55e', lineWidth: 1.5, title: 'RSI',
      priceFormat: { type: 'price', precision: 1, minMove: 0.1 },
    });
    _rsiSeries.setData(data.map(function(d) { return { time: d.time, value: d.value }; }));
    var ob70 = _rsiChart.addLineSeries({
      color: 'rgba(239,68,68,.3)', lineWidth: 1, lineStyle: 2, title: '',
      priceFormat: { type: 'price', precision: 0, minMove: 1 },
      crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
    });
    var os30 = _rsiChart.addLineSeries({
      color: 'rgba(34,197,94,.3)', lineWidth: 1, lineStyle: 2, title: '',
      priceFormat: { type: 'price', precision: 0, minMove: 1 },
      crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
    });
    var mid50 = _rsiChart.addLineSeries({
      color: 'rgba(156,163,175,.2)', lineWidth: 1, lineStyle: 2, title: '',
      priceFormat: { type: 'price', precision: 0, minMove: 1 },
      crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
    });
    var refData = data.map(function(d) { return { time: d.time }; });
    ob70.setData(refData.map(function(d) { return { time: d.time, value: 70 }; }));
    os30.setData(refData.map(function(d) { return { time: d.time, value: 30 }; }));
    mid50.setData(refData.map(function(d) { return { time: d.time, value: 50 }; }));
    _rsiChart.priceScale('right').applyOptions({ scaleMargins: { top: 0.05, bottom: 0.05 } });
    _rsiChart.timeScale().fitContent();
    indicatorSeries[id] = { pane: 'rsi' };
    _setupCrosshairSync();
    return;
  }

  if (id === 'macd') {
    _showMacdPane();
    if (!_macdChart) return;
    _macdLineSeries = _macdChart.addLineSeries({ color: '#3b82f6', lineWidth: 1.5, title: 'MACD' });
    _macdSignalSeries = _macdChart.addLineSeries({ color: '#f59e0b', lineWidth: 1.5, title: 'Signal' });
    _macdHistSeries = _macdChart.addHistogramSeries({ title: 'Hist' });
    _macdLineSeries.setData(data.filter(function(d){return d.macd!=null;}).map(function(d){return{time:d.time,value:d.macd};}));
    _macdSignalSeries.setData(data.filter(function(d){return d.signal!=null;}).map(function(d){return{time:d.time,value:d.signal};}));
    _macdHistSeries.setData(data.filter(function(d){return d.histogram!=null;}).map(function(d){return{time:d.time,value:d.histogram,color:d.histogram>=0?'rgba(34,197,94,.6)':'rgba(239,68,68,.6)'};}));
    _macdChart.timeScale().fitContent();
    indicatorSeries[id] = { pane: 'macd' };
    _setupCrosshairSync();
    return;
  }

  if (id === 'bbands' || id === 'bb' || id === 'bollinger') {
    var upper = chart.addLineSeries({ color: 'rgba(236,72,153,.5)', lineWidth: 1, title: 'BB Upper' });
    var middle = chart.addLineSeries({ color: 'rgba(236,72,153,.8)', lineWidth: 1, title: 'BB Mid' });
    var lower = chart.addLineSeries({ color: 'rgba(236,72,153,.5)', lineWidth: 1, title: 'BB Lower' });
    upper.setData(data.filter(function(d){return d.upper!=null;}).map(function(d){return{time:d.time,value:d.upper};}));
    middle.setData(data.filter(function(d){return d.middle!=null;}).map(function(d){return{time:d.time,value:d.middle};}));
    lower.setData(data.filter(function(d){return d.lower!=null;}).map(function(d){return{time:d.time,value:d.lower};}));
    indicatorSeries[id] = [upper, middle, lower];
    return;
  }

  if (id === 'stoch' || id === 'stochastic') {
    var kLine = chart.addLineSeries({ color: '#f97316', lineWidth: 1, priceScaleId: 'stoch', title: '%K' });
    var dLine = chart.addLineSeries({ color: '#3b82f6', lineWidth: 1, priceScaleId: 'stoch', title: '%D' });
    kLine.setData(data.filter(function(d){return d.k!=null;}).map(function(d){return{time:d.time,value:d.k};}));
    dLine.setData(data.filter(function(d){return d.d!=null;}).map(function(d){return{time:d.time,value:d.d};}));
    chart.priceScale('stoch').applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } });
    indicatorSeries[id] = [kLine, dLine];
    return;
  }

  if (id === 'psar' || id === 'sar') {
    var longS = chart.addLineSeries({ color: '#22c55e', lineWidth: 0, pointMarkersVisible: true, pointMarkersRadius: 2, title: 'SAR Long' });
    var shortS = chart.addLineSeries({ color: '#ef4444', lineWidth: 0, pointMarkersVisible: true, pointMarkersRadius: 2, title: 'SAR Short' });
    longS.setData(data.filter(function(d){return d.long!=null;}).map(function(d){return{time:d.time,value:d.long};}));
    shortS.setData(data.filter(function(d){return d.short!=null;}).map(function(d){return{time:d.time,value:d.short};}));
    indicatorSeries[id] = [longS, shortS];
    return;
  }

  if (id === 'ichimoku') {
    var tenkan = chart.addLineSeries({ color: '#2563eb', lineWidth: 1, title: 'Tenkan' });
    var kijun = chart.addLineSeries({ color: '#dc2626', lineWidth: 1, title: 'Kijun' });
    var spanA = chart.addLineSeries({ color: 'rgba(34,197,94,.6)', lineWidth: 1, title: 'Span A' });
    var spanB = chart.addLineSeries({ color: 'rgba(239,68,68,.6)', lineWidth: 1, title: 'Span B' });
    var chikou = chart.addLineSeries({ color: '#a855f7', lineWidth: 1, lineStyle: 2, title: 'Chikou' });
    tenkan.setData(data.filter(function(d){return d.tenkan!=null;}).map(function(d){return{time:d.time,value:d.tenkan};}));
    kijun.setData(data.filter(function(d){return d.kijun!=null;}).map(function(d){return{time:d.time,value:d.kijun};}));
    spanA.setData(data.filter(function(d){return d.senkou_a!=null;}).map(function(d){return{time:d.time,value:d.senkou_a};}));
    spanB.setData(data.filter(function(d){return d.senkou_b!=null;}).map(function(d){return{time:d.time,value:d.senkou_b};}));
    chikou.setData(data.filter(function(d){return d.chikou!=null;}).map(function(d){return{time:d.time,value:d.chikou};}));
    indicatorSeries[id] = [tenkan, kijun, spanA, spanB, chikou];
    return;
  }

  if (id === 'supertrend') {
    var upData = data.filter(function(d){return d.trend===1;}).map(function(d){return{time:d.time,value:d.value};});
    var dnData = data.filter(function(d){return d.trend===-1;}).map(function(d){return{time:d.time,value:d.value};});
    var upS = chart.addLineSeries({ color: '#22c55e', lineWidth: 2, title: 'ST Up' });
    var dnS = chart.addLineSeries({ color: '#ef4444', lineWidth: 2, title: 'ST Down' });
    upS.setData(upData); dnS.setData(dnData);
    indicatorSeries[id] = [upS, dnS];
    return;
  }

  if (id === 'pivot' || id === 'pivots') {
    var colors = {pivot:'#f59e0b',r1:'#ef4444',r2:'#dc2626',r3:'#b91c1c',s1:'#22c55e',s2:'#16a34a',s3:'#15803d'};
    var series = [];
    ['pivot','r1','r2','r3','s1','s2','s3'].forEach(function(key) {
      var s = chart.addLineSeries({
        color: colors[key], lineWidth: 1, lineStyle: 2, title: key.toUpperCase(),
        lastValueVisible: true, priceLineVisible: false,
      });
      s.setData(data.filter(function(d){return d[key]!=null;}).map(function(d){return{time:d.time,value:d[key]};}));
      series.push(s);
    });
    indicatorSeries[id] = series;
    return;
  }

  if (id === 'vol_profile' || id === 'volume_profile') {
    indicatorSeries[id] = { volProfile: data };
    _drawVolProfile();
    return;
  }

  if (id === 'adx') {
    var adxLine = chart.addLineSeries({ color: '#a855f7', lineWidth: 1.5, priceScaleId: 'adx', title: 'ADX' });
    var diP = chart.addLineSeries({ color: '#22c55e', lineWidth: 1, priceScaleId: 'adx', title: '+DI' });
    var diN = chart.addLineSeries({ color: '#ef4444', lineWidth: 1, priceScaleId: 'adx', title: '-DI' });
    adxLine.setData(data.filter(function(d){return d.adx!=null;}).map(function(d){return{time:d.time,value:d.adx};}));
    diP.setData(data.filter(function(d){return d.dmp!=null;}).map(function(d){return{time:d.time,value:d.dmp};}));
    diN.setData(data.filter(function(d){return d.dmn!=null;}).map(function(d){return{time:d.time,value:d.dmn};}));
    chart.priceScale('adx').applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } });
    indicatorSeries[id] = [adxLine, diP, diN];
    return;
  }

  // Single-value indicators (ATR, OBV, MFI, CCI, VWAP, Williams, SMA, EMA)
  var isPaneInd = meta.pane;
  var scaleId = isPaneInd ? id : undefined;
  var series = chart.addLineSeries({ color: meta.color, lineWidth: 1.5, priceScaleId: scaleId, title: meta.label });
  series.setData(data.map(function(d) { return { time: d.time, value: d.value }; }));
  if (isPaneInd) {
    chart.priceScale(id).applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } });
  }
  indicatorSeries[id] = [series];
}

function removeIndicatorSeries(id) {
  var entry = indicatorSeries[id];
  if (!entry) return;
  if (entry && entry.pane === 'rsi') {
    _hideRsiPane();
    delete indicatorSeries[id];
    return;
  }
  if (entry && entry.pane === 'macd') {
    _hideMacdPane();
    delete indicatorSeries[id];
    return;
  }
  if (entry && entry.volProfile) {
    delete indicatorSeries[id];
    _redrawAll();
    return;
  }
  if (Array.isArray(entry)) {
    entry.forEach(function(s) { try { chart.removeSeries(s); } catch(e) {} });
  }
  delete indicatorSeries[id];
}

/* ── Watchlist (groupable, localStorage) ─────────── */
var WL_GROUPS_KEY = 'chili_trading_wl_groups_v1';

function _loadWlGroupState() {
  try {
    var raw = localStorage.getItem(WL_GROUPS_KEY);
    if (!raw) return { groups: [], tickerToGroupId: {}, collapsed: {} };
    var o = JSON.parse(raw);
    if (!o || typeof o !== 'object') return { groups: [], tickerToGroupId: {}, collapsed: {} };
    return {
      groups: Array.isArray(o.groups) ? o.groups : [],
      tickerToGroupId: o.tickerToGroupId && typeof o.tickerToGroupId === 'object' ? o.tickerToGroupId : {},
      collapsed: o.collapsed && typeof o.collapsed === 'object' ? o.collapsed : {},
    };
  } catch (e) { return { groups: [], tickerToGroupId: {}, collapsed: {} }; }
}

function _saveWlGroupState(st) {
  try { localStorage.setItem(WL_GROUPS_KEY, JSON.stringify(st)); } catch (e) {}
}

function toggleWatchlistGroupCollapse(gid) {
  var st = _loadWlGroupState();
  st.collapsed[gid] = !st.collapsed[gid];
  _saveWlGroupState(st);
  loadWatchlist();
}

function _wlPruneEmptyGroups(st) {
  var used = {};
  for (var t in st.tickerToGroupId) {
    if (st.tickerToGroupId[t]) used[st.tickerToGroupId[t]] = true;
  }
  st.groups = (st.groups || []).filter(function(g) { return used[g.id]; });
  var valid = {};
  st.groups.forEach(function(g) { valid[g.id] = true; });
  for (var t2 in st.tickerToGroupId) {
    var g2 = st.tickerToGroupId[t2];
    if (g2 && !valid[g2]) delete st.tickerToGroupId[t2];
  }
}

function _wlDropOnGroupId(srcTicker, groupId) {
  var st = _loadWlGroupState();
  if (!srcTicker) return;
  if (!groupId) {
    delete st.tickerToGroupId[srcTicker];
  } else {
    st.tickerToGroupId[srcTicker] = groupId;
  }
  _wlPruneEmptyGroups(st);
  _saveWlGroupState(st);
  loadWatchlist();
}

function _wlDropOnTicker(srcTicker, targetTicker) {
  if (!srcTicker || !targetTicker || srcTicker === targetTicker) return;
  var st = _loadWlGroupState();
  var tg = st.tickerToGroupId[targetTicker];
  var sg = st.tickerToGroupId[srcTicker];
  if (tg) {
    st.tickerToGroupId[srcTicker] = tg;
  } else if (sg) {
    st.tickerToGroupId[targetTicker] = sg;
  } else {
    var newId = 'g_' + Date.now();
    var n = 1;
    for (var gi = 0; gi < st.groups.length; gi++) {
      if ((st.groups[gi].name || '').indexOf('Group ') === 0) {
        var m = /^Group (\d+)$/.exec(st.groups[gi].name);
        if (m) n = Math.max(n, parseInt(m[1], 10) + 1);
      }
    }
    st.groups.push({ id: newId, name: 'Group ' + n });
    st.tickerToGroupId[srcTicker] = newId;
    st.tickerToGroupId[targetTicker] = newId;
  }
  _wlPruneEmptyGroups(st);
  _saveWlGroupState(st);
  loadWatchlist();
}

var _wlDnDInit = false;
var _wlDropHoverEl = null;

function _wlClearDropHover() {
  if (_wlDropHoverEl) {
    _wlDropHoverEl.classList.remove('wl-drop-hover');
    _wlDropHoverEl = null;
  }
}

function _wlSetDropHover(el) {
  if (_wlDropHoverEl === el) return;
  _wlClearDropHover();
  if (el) {
    el.classList.add('wl-drop-hover');
    _wlDropHoverEl = el;
  }
}

function _ensureWatchlistDnD() {
  if (_wlDnDInit) return;
  _wlDnDInit = true;
  var list = document.getElementById('wl-list');
  if (!list) return;

  list.addEventListener('dragover', function(e) {
    var row = e.target.closest('.wl-item');
    var body = e.target.closest('.wl-group-body');
    if (row || body) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
    }
    var h = row || body;
    if (h) _wlSetDropHover(h);
    else _wlClearDropHover();
  });

  list.addEventListener('drop', function(e) {
    e.preventDefault();
    _wlClearDropHover();
    var src = (e.dataTransfer.getData('text/plain') || '').trim().toUpperCase();
    if (!src) return;

    var row = e.target.closest('.wl-item');
    if (row && row.dataset.ticker && row.dataset.ticker !== src) {
      _wlDropOnTicker(src, row.dataset.ticker);
      return;
    }

    var body = e.target.closest('.wl-group-body');
    if (body && body.dataset.wlGroupId !== undefined) {
      _wlDropOnGroupId(src, body.dataset.wlGroupId || '');
      return;
    }

    var head = e.target.closest('.wl-group-head');
    if (head) {
      var sec = head.closest('.wl-group');
      if (sec && sec.dataset.wlGroupId !== undefined) {
        _wlDropOnGroupId(src, sec.dataset.wlGroupId || '');
      }
    }
  });
}

function loadWatchlist() {
  _ensureWatchlistDnD();
  fetch('/api/trading/watchlist').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var st = _loadWlGroupState();
    var tickersPresent = {};
    (d.items || []).forEach(function(item) { tickersPresent[item.ticker] = true; });
    for (var tk in st.tickerToGroupId) {
      if (!tickersPresent[tk]) delete st.tickerToGroupId[tk];
    }
    _wlPruneEmptyGroups(st);
    _saveWlGroupState(st);

    var list = document.getElementById('wl-list');
    list.innerHTML = '';
    if (!d.items.length) {
      list.innerHTML = '<div style="padding:12px;color:var(--text-muted);font-size:12px">Add tickers above</div>';
      return;
    }

    var ungrouped = [];
    var byGroup = {};
    var brokerBuckets = {};
    var _brokerLabels = { robinhood: 'Robinhood', coinbase: 'Coinbase' };
    st.groups.forEach(function(g) { byGroup[g.id] = []; });
    d.items.forEach(function(item) {
      var gid = st.tickerToGroupId[item.ticker];
      if (gid && Object.prototype.hasOwnProperty.call(byGroup, gid)) {
        byGroup[gid].push(item);
      } else if (item.source && item.source !== 'manual') {
        var bk = item.source;
        if (!brokerBuckets[bk]) brokerBuckets[bk] = [];
        brokerBuckets[bk].push(item);
      } else {
        ungrouped.push(item);
      }
    });

    function appendItem(parentEl, item) {
      var isBroker = item.source && item.source !== 'manual';
      var div = document.createElement('div');
      div.className = 'wl-item' + (item.ticker === currentTicker ? ' active' : '');
      div.dataset.ticker = item.ticker;

      var handle = document.createElement('span');
      handle.className = 'wl-drag-handle';
      handle.innerHTML = '&#x2630;';
      handle.title = 'Drag to group';
      handle.draggable = true;
      handle.addEventListener('dragstart', function(e) {
        e.stopPropagation();
        div.classList.add('wl-dragging');
        e.dataTransfer.setData('text/plain', item.ticker);
        e.dataTransfer.effectAllowed = 'move';
      });
      handle.addEventListener('dragend', function() {
        div.classList.remove('wl-dragging');
        _wlClearDropHover();
      });

      var tEl = document.createElement('span');
      tEl.className = 'wl-ticker';
      tEl.textContent = item.ticker;

      if (isBroker) {
        var badge = document.createElement('span');
        badge.className = 'wl-broker-badge';
        badge.textContent = item.source === 'coinbase' ? 'CB' : 'RH';
        badge.title = 'Open position (' + item.source + ')';
        tEl.appendChild(badge);
      }

      var pEl = document.createElement('span');
      pEl.className = 'wl-price';
      pEl.id = 'wl-p-' + item.ticker;

      var spacer = document.createElement('span');
      spacer.style.flex = '1';

      div.appendChild(handle);
      div.appendChild(tEl);
      div.appendChild(pEl);
      div.appendChild(spacer);

      if (!isBroker) {
        var rm = document.createElement('button');
        rm.type = 'button';
        rm.className = 'wl-rm';
        rm.innerHTML = '&times;';
        rm.title = 'Remove';
        rm.onclick = function(ev) {
          ev.stopPropagation();
          removeWatchlist(item.ticker);
        };
        div.appendChild(rm);
      }

      div.onclick = function(ev) {
        if (ev.target.closest && (ev.target.closest('.wl-drag-handle') || ev.target.closest('.wl-rm'))) return;
        selectTicker(item.ticker);
      };

      parentEl.appendChild(div);
      fetchWlPrice(item.ticker);
    }

    function appendSection(title, groupKey, items, collapsible) {
      if (groupKey !== '' && !items.length) return;

      var sec = document.createElement('div');
      sec.className = 'wl-group';
      sec.dataset.wlGroupId = groupKey;

      var head = document.createElement('div');
      head.className = 'wl-group-head';
      var body = document.createElement('div');
      body.className = 'wl-group-body';
      body.dataset.wlGroupId = groupKey;

      if (collapsible && groupKey && st.collapsed[groupKey]) body.classList.add('wl-collapsed');

      var count = items.length;
      if (collapsible && groupKey) {
        head.innerHTML = '<span class="wl-group-chev">' + (st.collapsed[groupKey] ? '&#9654;' : '&#9660;') + '</span><span>' + escHtml(title) + '</span><span style="opacity:.5;margin-left:auto;font-weight:600">' + count + '</span>';
        head.onclick = function() { toggleWatchlistGroupCollapse(groupKey); };
      } else {
        head.innerHTML = '<span>' + escHtml(title) + '</span><span style="opacity:.5;margin-left:auto;font-weight:600">' + count + '</span>';
      }

      sec.appendChild(head);
      sec.appendChild(body);

      if (!items.length && groupKey === '') {
        var hint = document.createElement('div');
        hint.className = 'wl-ungrp-hint';
        hint.textContent = 'Drop tickers here to ungroup';
        body.appendChild(hint);
      } else {
        items.forEach(function(item) { appendItem(body, item); });
      }

      list.appendChild(sec);
    }

    Object.keys(brokerBuckets).forEach(function(bk) {
      var label = _brokerLabels[bk] || bk;
      appendSection(label, '_broker_' + bk, brokerBuckets[bk], true);
    });
    if (ungrouped.length) appendSection('Watchlist', '', ungrouped, false);
    st.groups.forEach(function(g) {
      appendSection(g.name, g.id, byGroup[g.id] || [], true);
    });
  });
}

function fetchWlPrice(ticker) {
  fetch('/api/trading/quote?ticker='+encodeURIComponent(ticker)).then(function(r){return r.json();}).then(function(d) {
    var el = document.getElementById('wl-p-'+ticker);
    if (!el || !d.ok) return;
    var cls = (d.change_pct||0) >= 0 ? 'up' : 'down';
    el.className = 'wl-price wl-change ' + cls;
    el.textContent = '$' + (d.price||'--');
  });
}

function addToWatchlist() {
  var inp = document.getElementById('wl-input');
  var ticker = inp.value.trim().toUpperCase();
  if (!ticker) return;
  inp.value = '';
  fetch('/api/trading/watchlist', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ticker:ticker}) })
    .then(function() { loadWatchlist(); selectTicker(ticker); });
}

function addTickerToWatchlistQuick(ticker) {
  fetch('/api/trading/watchlist', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ticker: ticker }) })
    .then(function() { loadWatchlist(); selectTicker(ticker); });
}

function removeWatchlist(ticker) {
  fetch('/api/trading/watchlist?ticker='+encodeURIComponent(ticker), {method:'DELETE'}).then(function() { loadWatchlist(); });
}

/* ── Toolbar Ticker Search ─────────────────────── */
var _tsDebounce = null;
var _tsIdx = -1;

function openToolbarSearch() {
  var label = document.getElementById('ticker-label');
  var box = document.getElementById('toolbar-search');
  var inp = document.getElementById('toolbar-search-input');
  label.style.display = 'none';
  box.classList.add('open');
  inp.value = '';
  inp.focus();
}

function closeToolbarSearch() {
  var label = document.getElementById('ticker-label');
  var box = document.getElementById('toolbar-search');
  var results = document.getElementById('toolbar-search-results');
  box.classList.remove('open');
  results.classList.remove('open');
  results.innerHTML = '';
  label.style.display = '';
  _tsIdx = -1;
}

function _tsSearch(query) {
  if (!query || query.length < 1) {
    document.getElementById('toolbar-search-results').classList.remove('open');
    document.getElementById('toolbar-search-results').innerHTML = '';
    return;
  }
  fetch('/api/trading/search?q=' + encodeURIComponent(query) + '&limit=8')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d.ok) return;
      _tsRender(d.results || []);
    }).catch(function() {});
}

function _tsRender(results) {
  var el = document.getElementById('toolbar-search-results');
  _tsIdx = -1;
  if (!results.length) {
    el.innerHTML = '<div class="tsr-empty">No results found</div>';
    el.classList.add('open');
    return;
  }
  var html = '';
  results.forEach(function(r, i) {
    html += '<div class="tsr-item" data-idx="' + i + '" data-ticker="' + escHtml(r.ticker) + '" onclick="_tsSelect(this)">' +
      '<span class="tsr-sym">' + escHtml(r.ticker) + '</span>' +
      '<span class="tsr-name">' + escHtml(r.name || '') + '</span>' +
      '<span class="tsr-type">' + escHtml(r.type || '') + '</span>' +
      '<button class="tsr-add" onclick="event.stopPropagation();_tsAddWl(this)" title="Add to watchlist">+</button>' +
      '</div>';
  });
  html += '<div class="tsr-hint">Click to view chart &middot; + to add to watchlist</div>';
  el.innerHTML = html;
  el.classList.add('open');
}

function _tsSelect(el) {
  var ticker = el.dataset.ticker;
  closeToolbarSearch();
  selectTicker(ticker);
}

function _tsAddWl(btn) {
  var item = btn.closest('.tsr-item');
  var ticker = item.dataset.ticker;
  btn.textContent = '\u2713';
  btn.style.color = '#22c55e';
  btn.style.borderColor = '#22c55e';
  fetch('/api/trading/watchlist', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ticker: ticker }) })
    .then(function() { loadWatchlist(); });
}

function _tsKeydown(e) {
  var items = document.querySelectorAll('#toolbar-search-results .tsr-item');
  if (e.key === 'Escape') { closeToolbarSearch(); e.preventDefault(); return; }
  if (e.key === 'ArrowDown') { _tsIdx = Math.min(_tsIdx + 1, items.length - 1); _tsHighlight(items); e.preventDefault(); return; }
  if (e.key === 'ArrowUp') { _tsIdx = Math.max(_tsIdx - 1, 0); _tsHighlight(items); e.preventDefault(); return; }
  if (e.key === 'Enter') {
    if (_tsIdx >= 0 && items[_tsIdx]) {
      _tsSelect(items[_tsIdx]);
    } else if (items.length === 1) {
      _tsSelect(items[0]);
    } else {
      var val = document.getElementById('toolbar-search-input').value.trim().toUpperCase();
      if (val) { closeToolbarSearch(); selectTicker(val); }
    }
    e.preventDefault();
    return;
  }
  clearTimeout(_tsDebounce);
  _tsDebounce = setTimeout(function() {
    _tsSearch(document.getElementById('toolbar-search-input').value.trim());
  }, 250);
}

function _tsHighlight(items) {
  items.forEach(function(el, i) { el.classList.toggle('active', i === _tsIdx); });
  if (items[_tsIdx]) items[_tsIdx].scrollIntoView({ block: 'nearest' });
}

/* ── Sidebar Watchlist Autocomplete ────────────── */
var _wlAcDebounce = null;

function _wlAcSearch(query) {
  var ac = document.getElementById('wl-ac');
  if (!query || query.length < 1) { ac.classList.remove('open'); ac.innerHTML = ''; return; }
  fetch('/api/trading/search?q=' + encodeURIComponent(query) + '&limit=5')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d.ok || !d.results || !d.results.length) { ac.classList.remove('open'); ac.innerHTML = ''; return; }
      var html = '';
      d.results.forEach(function(r) {
        html += '<div class="wl-ac-item" data-ticker="' + escHtml(r.ticker) + '" onclick="_wlAcSelect(this)">' +
          '<span class="wac-sym">' + escHtml(r.ticker) + '</span>' +
          '<span class="wac-name">' + escHtml(r.name || '') + '</span></div>';
      });
      ac.innerHTML = html;
      ac.classList.add('open');
    }).catch(function() {});
}

function _wlAcSelect(el) {
  var ticker = el.dataset.ticker;
  var inp = document.getElementById('wl-input');
  inp.value = ticker;
  document.getElementById('wl-ac').classList.remove('open');
  addToWatchlist();
}

function _wlInputHandler(e) {
  if (e.key === 'Enter') { document.getElementById('wl-ac').classList.remove('open'); addToWatchlist(); return; }
  if (e.key === 'Escape') { document.getElementById('wl-ac').classList.remove('open'); return; }
}

/* ── Trades ─────────────────────────────────────── */
var _allTrades = [];
var _sellTradeId = null;
var _assignTradeId = null;
var _assignTicker = '';
var _patternIdToName = {};
var _patternsListCache = null;

function loadTradePatternNames(done) {
  fetch('/api/trading/patterns').then(function(r) { return r.json(); }).then(function(d) {
    if (d && d.ok && d.patterns) {
      _patternsListCache = d.patterns;
      _patternIdToName = {};
      d.patterns.forEach(function(p) { _patternIdToName[p.id] = p.name; });
    }
    if (typeof done === 'function') done();
  }).catch(function() { if (typeof done === 'function') done(); });
}

function loadTrades() {
  fetch('/api/trading/trades').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    _allTrades = d.trades;
    loadTradePatternNames(applyTradesFilter);
  });
}

function applyTradesFilter() {
  var statusFilter = document.getElementById('trades-filter-status').value;
  var rhOnly = document.getElementById('trades-filter-rh').checked;
  var body = document.getElementById('trades-body');
  body.innerHTML = '';

  var filtered = _allTrades.filter(function(t) {
    if (statusFilter !== 'all' && t.status !== statusFilter) return false;
    if (rhOnly && t.broker_source !== 'robinhood') return false;
    return true;
  });

  if (!filtered.length) {
    body.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--text-secondary);padding:16px">No trades match the current filter.</td></tr>';
    return;
  }

  filtered.forEach(function(t) {
    var pnlCls = t.pnl > 0 ? 'pnl-pos' : (t.pnl < 0 ? 'pnl-neg' : '');
    var pnlStr = t.pnl != null ? '$' + t.pnl.toFixed(2) : '-';

    var srcBadge = t.broker_source === 'robinhood'
      ? '<span class="trade-badge rh">RH</span>'
      : '<span class="trade-badge manual">Manual</span>';

    var statusCls = t.status === 'open' ? 'open' : (t.status === 'working' ? 'working' : (t.status === 'closed' ? 'closed' : 'cancelled'));
    var statusLabel = t.status;
    if (t.status === 'working' && t.broker_status) statusLabel = 'Working (' + t.broker_status + ')';
    var statusBadge = '<span class="trade-status-badge ' + statusCls + '">' + statusLabel + '</span>';

    var entryStr = t.entry_price < 1 ? '$' + t.entry_price.toFixed(6) : '$' + t.entry_price.toFixed(2);
    var exitStr = t.exit_price ? (t.exit_price < 1 ? '$' + t.exit_price.toFixed(6) : '$' + t.exit_price.toFixed(2)) : '-';

    var actions = '<div class="trade-actions">';
    if (t.status === 'open') {
      actions += '<button class="btn-sell" onclick="openSellDialog(' + t.id + ',\'' + t.ticker + '\',' + t.quantity + ',' + t.entry_price + ',\'' + (t.broker_source || '') + '\')">Sell</button>'
        + '<button class="btn-close-all" onclick="promptClose(' + t.id + ')">Close</button>';
    }
    actions += '<button class="btn-delete" onclick="deleteTrade(' + t.id + ',\'' + (t.ticker || '').replace(/'/g, "\\'") + '\')" title="Remove this trade">Delete</button></div>';

    var patCell = '';
    if (t.status === 'open') {
      var pid = t.scan_pattern_id;
      var pLabel = pid ? (_patternIdToName[pid] || ('#' + pid)) : '—';
      patCell = '<span style="font-size:11px;color:var(--text-secondary);max-width:120px;display:inline-block;vertical-align:middle" title="Monitor strategy">' + (pid ? escapeHtml(pLabel) : '—') + '</span> '
        + '<button type="button" class="broker-sync-btn" style="margin-left:4px" onclick="openAssignPatternDialog(' + t.id + ',\'' + (t.ticker || '').replace(/'/g, "\\'") + '\',' + (pid ? pid : 'null') + ')">Assign</button>';
    } else {
      patCell = t.scan_pattern_id ? escapeHtml(_patternIdToName[t.scan_pattern_id] || ('#' + t.scan_pattern_id)) : '—';
    }

    body.innerHTML += '<tr>'
      + '<td style="font-weight:600">' + t.ticker + '</td>'
      + '<td>' + srcBadge + '</td>'
      + '<td>' + t.direction + '</td>'
      + '<td>' + entryStr + '</td>'
      + '<td>' + exitStr + '</td>'
      + '<td>' + t.quantity + '</td>'
      + '<td class="' + pnlCls + '">' + pnlStr + '</td>'
      + '<td>' + statusBadge + '</td>'
      + '<td style="white-space:nowrap">' + patCell + '</td>'
      + '<td>' + actions + '</td>'
      + '</tr>';
  });
}

function escapeHtml(s) {
  if (!s) return '';
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function openAssignPatternDialog(tradeId, ticker, currentPatternId) {
  if (CHILI_TRADING_IS_GUEST) { alert('Sign in to assign a strategy'); return; }
  _assignTradeId = tradeId;
  _assignTicker = ticker || '';
  var overlay = document.getElementById('assign-pattern-overlay');
  var title = document.getElementById('assign-pattern-title');
  if (title) title.textContent = 'Assign strategy — ' + _assignTicker;
  function fill() {
    _rebuildAssignPatternSelect(_assignTicker, currentPatternId);
    if (overlay) overlay.classList.add('active');
  }
  if (_patternsListCache && _patternsListCache.length) {
    fill();
  } else {
    loadTradePatternNames(fill);
  }
}

function closeAssignPatternDialog() {
  _assignTradeId = null;
  _assignTicker = '';
  var overlay = document.getElementById('assign-pattern-overlay');
  if (overlay) overlay.classList.remove('active');
}

function _rebuildAssignPatternSelect(ticker, selectedId) {
  var sel = document.getElementById('assign-pattern-select');
  if (!sel) return;
  var crypto = (ticker || '').toUpperCase().indexOf('-USD') !== -1;
  sel.innerHTML = '';
  var opt0 = document.createElement('option');
  opt0.value = '';
  opt0.textContent = '(None — clear assignment)';
  sel.appendChild(opt0);
  var list = _patternsListCache || [];
  list.forEach(function(p) {
    var ac = (p.asset_class || 'all').toLowerCase();
    if (ac === 'crypto' && !crypto) return;
    if (ac === 'stock' && crypto) return;
    var rj = p.rules_json;
    var conds = (rj && typeof rj === 'object' && rj.conditions) ? rj.conditions : null;
    if (!conds || !conds.length) return;
    var o = document.createElement('option');
    o.value = String(p.id);
    o.textContent = p.name + ' (#' + p.id + ', ' + (p.asset_class || 'all') + ')';
    sel.appendChild(o);
  });
  if (selectedId) {
    sel.value = String(selectedId);
    if (sel.value !== String(selectedId)) {
      var o2 = document.createElement('option');
      o2.value = String(selectedId);
      o2.textContent = 'Current #' + selectedId;
      sel.appendChild(o2);
      sel.value = String(selectedId);
    }
  } else {
    sel.value = '';
  }
}

function confirmAssignPattern(isClear) {
  if (!_assignTradeId) return;
  var sel = document.getElementById('assign-pattern-select');
  var raw = isClear ? '' : (sel ? sel.value : '');
  var pid = raw === '' ? null : parseInt(raw, 10);
  if (raw !== '' && !isFinite(pid)) { alert('Choose a pattern or clear'); return; }
  var payload = { scan_pattern_id: pid };
  fetch('/api/trading/trades/' + _assignTradeId + '/assign-pattern', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(payload)
  }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); }).then(function(res) {
    if (!res.d || !res.d.ok) {
      alert(res.d && res.d.error ? res.d.error : 'Could not update assignment');
      return;
    }
    closeAssignPatternDialog();
    loadTrades();
    loadStats();
    _refreshMonitorIfLoaded();
    appendAiMsg('assistant', pid ? ('Strategy assigned for monitor (pattern #' + pid + ').') : 'Strategy assignment cleared.');
  }).catch(function() { alert('Request failed'); });
}
