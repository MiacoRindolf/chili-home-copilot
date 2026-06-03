
function createTrade() {
  var price = parseFloat(document.getElementById('tf-price').value);
  if (!price) return;
  fetch('/api/trading/trades', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ ticker: currentTicker, direction: document.getElementById('tf-dir').value, entry_price: price, quantity: parseFloat(document.getElementById('tf-qty').value)||1 })
  }).then(function() { loadTrades(); loadStats(); document.getElementById('tf-price').value=''; });
}

function promptClose(id) {
  var price = prompt('Exit price:');
  if (!price) return;
  fetch('/api/trading/trades/'+id+'/close', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ exit_price: parseFloat(price) })
  }).then(function() { loadTrades(); loadStats(); loadInsights(); _refreshMonitorIfLoaded(); });
}

function deleteTrade(id, ticker) {
  if (!confirm('Remove trade ' + (ticker ? ticker + ' ' : '') + '(ID ' + id + ')? This cannot be undone.')) return;
  var opts = { method: 'DELETE', credentials: 'same-origin' };
  fetch('/api/trading/trades/' + id, opts).then(function(r) {
    return r.text().then(function(t) {
      var d = null;
      try { d = t ? JSON.parse(t) : {}; } catch(e) {}
      return { status: r.status, data: d };
    });
  }).then(function(res) {
    if (res.data && res.data.ok) { loadTrades(); loadStats(); _refreshMonitorIfLoaded(); return; }
    if (res.status === 403) { alert(res.data && res.data.error ? res.data.error : 'You don\'t have permission to delete this trade.'); return; }
    if (res.status === 404) {
      loadTrades();
      _refreshMonitorIfLoaded();
      alert(res.data && res.data.error ? res.data.error : 'Trade not found. It may have been deleted already.');
      return;
    }
    alert(res.data && res.data.error ? res.data.error : 'Could not delete trade');
  }).catch(function() { alert('Could not delete trade'); });
}

function openSellDialog(tradeId, ticker, maxQty, entryPrice, brokerSrc) {
  _sellTradeId = tradeId;
  document.getElementById('sell-dialog-title').textContent = 'Sell ' + ticker;
  document.getElementById('sell-qty').value = maxQty;
  document.getElementById('sell-qty').max = maxQty;
  document.getElementById('sell-limit').value = '';
  var info = 'Max qty: ' + maxQty + ' | Entry: $' + entryPrice.toFixed(2);
  if (brokerSrc === 'robinhood') info += ' | Routed to Robinhood';
  else info += ' | Simulated (manual trade)';
  document.getElementById('sell-dialog-info').textContent = info;
  document.getElementById('sell-dialog-overlay').classList.add('active');
}

function closeSellDialog() {
  _sellTradeId = null;
  document.getElementById('sell-dialog-overlay').classList.remove('active');
}

function confirmSell() {
  if (!_sellTradeId) return;
  var qty = parseFloat(document.getElementById('sell-qty').value);
  var limit = document.getElementById('sell-limit').value ? parseFloat(document.getElementById('sell-limit').value) : null;
  if (!qty || qty <= 0) { alert('Enter a valid quantity'); return; }
  var payload = { quantity: qty };
  if (limit) payload.limit_price = limit;

  var btn = document.querySelector('.sell-dialog .btn-confirm');
  btn.textContent = 'Selling...'; btn.disabled = true;

  fetch('/api/trading/trades/' + _sellTradeId + '/sell', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).then(function(r){ return r.json(); }).then(function(d) {
    btn.textContent = 'Place Sell'; btn.disabled = false;
    closeSellDialog();
    if (d.ok) {
      var msg = 'Sold ' + d.sold_qty + ' shares';
      if (d.rh_state) msg += ' (RH: ' + d.rh_state + ')';
      if (d.remaining_qty > 0) msg += ', ' + d.remaining_qty + ' remaining';
      appendAiMsg('assistant', msg);
      loadTrades(); loadStats(); loadBrokerPortfolio(); loadPortfolio(); _refreshMonitorIfLoaded();
    } else {
      alert(d.error || 'Sell failed');
    }
  }).catch(function() {
    btn.textContent = 'Place Sell'; btn.disabled = false;
    closeSellDialog();
  });
}

/* ── Journal ────────────────────────────────────── */
function loadJournal() {
  fetch('/api/trading/journal').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var list = document.getElementById('j-list');
    list.innerHTML = '';
    d.entries.forEach(function(e) {
      list.innerHTML += '<div class="j-entry"><span class="j-date">'+new Date(e.created_at).toLocaleDateString()+'</span> '+e.content+'</div>';
    });
  });
}

function addJournal() {
  var inp = document.getElementById('j-input');
  var text = inp.value.trim();
  if (!text) return;
  inp.value = '';
  fetch('/api/trading/journal', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({content:text}) })
    .then(function() { loadJournal(); });
}

/* ── Stats ──────────────────────────────────────── */
var _statsCalendarYear = new Date().getFullYear();
var _statsCalendarMonth = new Date().getMonth() + 1;

function loadStats() {
  fetch('/api/trading/journal/stats').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var bySource = d.by_source || {};
    var all = bySource.all || d;
    var real = bySource.real || { total_trades: 0 };
    var paper = bySource.paper || { total_trades: 0 };

    var overview = document.getElementById('stats-overview');
    var realEl = document.getElementById('stats-real');
    var paperEl = document.getElementById('stats-paper');
    var emptyEl = document.getElementById('stats-empty');

    if (!all.total_trades) {
      overview.style.display = 'none';
      realEl.style.display = 'none';
      paperEl.style.display = 'none';
      emptyEl.style.display = 'block';
      emptyEl.innerHTML = '<div class="stats-empty-msg">No closed trades yet. Close a trade to see your stats here.</div>';
      loadStatsCalendar();
      return;
    }
    emptyEl.style.display = 'none';
    overview.style.display = '';

    var pnlCls = (all.total_pnl || 0) >= 0 ? 'pnl-pos' : 'pnl-neg';
    overview.innerHTML =
      '<div class="stat-card stat-hero"><div class="stat-val">' + all.total_trades + '</div><div class="stat-lbl">Total Trades</div></div>' +
      '<div class="stat-card stat-hero"><div class="stat-val">' + (all.win_rate || 0) + '%</div><div class="stat-lbl">Win Rate</div></div>' +
      '<div class="stat-card stat-hero ' + pnlCls + '"><div class="stat-val">$' + (all.total_pnl || 0) + '</div><div class="stat-lbl">Total P&L</div></div>';

    if (real.total_trades > 0) {
      realEl.style.display = '';
      var rPnlCls = (real.total_pnl || 0) >= 0 ? 'pnl-pos' : 'pnl-neg';
      realEl.innerHTML =
        '<h4 class="stats-section-title">Real (Robinhood)</h4>' +
        '<div class="stats-grid">' +
        '<div class="stat-card"><div class="stat-val">' + real.total_trades + '</div><div class="stat-lbl">Trades</div></div>' +
        '<div class="stat-card"><div class="stat-val">' + (real.win_rate || 0) + '%</div><div class="stat-lbl">Win Rate</div></div>' +
        '<div class="stat-card ' + rPnlCls + '"><div class="stat-val">$' + (real.total_pnl || 0) + '</div><div class="stat-lbl">P&L</div></div>' +
        '<div class="stat-card pnl-pos"><div class="stat-val">$' + (real.best_trade || 0) + '</div><div class="stat-lbl">Best</div></div>' +
        '<div class="stat-card pnl-neg"><div class="stat-val">$' + (real.worst_trade || 0) + '</div><div class="stat-lbl">Worst</div></div>' +
        '<div class="stat-card"><div class="stat-val">$' + (real.max_drawdown || 0) + '</div><div class="stat-lbl">Max DD</div></div>' +
        '</div>';
    } else {
      realEl.style.display = 'none';
    }

    if (paper.total_trades > 0) {
      paperEl.style.display = '';
      var pPnlCls = (paper.total_pnl || 0) >= 0 ? 'pnl-pos' : 'pnl-neg';
      paperEl.innerHTML =
        '<h4 class="stats-section-title">Paper / Manual</h4>' +
        '<div class="stats-grid">' +
        '<div class="stat-card"><div class="stat-val">' + paper.total_trades + '</div><div class="stat-lbl">Trades</div></div>' +
        '<div class="stat-card"><div class="stat-val">' + (paper.win_rate || 0) + '%</div><div class="stat-lbl">Win Rate</div></div>' +
        '<div class="stat-card ' + pPnlCls + '"><div class="stat-val">$' + (paper.total_pnl || 0) + '</div><div class="stat-lbl">P&L</div></div>' +
        '<div class="stat-card pnl-pos"><div class="stat-val">$' + (paper.best_trade || 0) + '</div><div class="stat-lbl">Best</div></div>' +
        '<div class="stat-card pnl-neg"><div class="stat-val">$' + (paper.worst_trade || 0) + '</div><div class="stat-lbl">Worst</div></div>' +
        '<div class="stat-card"><div class="stat-val">$' + (paper.max_drawdown || 0) + '</div><div class="stat-lbl">Max DD</div></div>' +
        '</div>';
    } else {
      paperEl.style.display = 'none';
    }

    loadStatsCalendar();
  });
}

function loadStatsCalendar() {
  var url = '/api/trading/journal/calendar?year=' + _statsCalendarYear + '&month=' + _statsCalendarMonth;
  fetch(url).then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    renderStatsCalendar(d.days || [], d.year || _statsCalendarYear, d.month || _statsCalendarMonth);
  });
}

var _statsDayMap = {};

function renderStatsCalendar(days, year, month) {
  var header = document.getElementById('stats-cal-header');
  var grid = document.getElementById('stats-cal-grid');
  var dayDetail = document.getElementById('stats-day-detail');

  _statsDayMap = {};
  (days || []).forEach(function(day) { _statsDayMap[day.date] = day; });

  var monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  header.innerHTML =
    '<div class="stats-calendar-nav">' +
    '<button type="button" onclick="statsCalPrevMonth()">&#x276E;</button>' +
    '<span class="stats-calendar-title">' + monthNames[month - 1] + ' ' + year + '</span>' +
    '<button type="button" onclick="statsCalNextMonth()">&#x276F;</button></div>';

  var firstDay = new Date(year, month - 1, 1);
  var lastDay = new Date(year, month, 0);
  var startPad = firstDay.getDay();
  var numDays = lastDay.getDate();

  var html = '<div class="stats-calendar-dow">Sun</div><div class="stats-calendar-dow">Mon</div><div class="stats-calendar-dow">Tue</div><div class="stats-calendar-dow">Wed</div><div class="stats-calendar-dow">Thu</div><div class="stats-calendar-dow">Fri</div><div class="stats-calendar-dow">Sat</div>';
  for (var i = 0; i < startPad; i++) html += '<div class="stats-calendar-day other-month"></div>';
  for (var d = 1; d <= numDays; d++) {
    var dateStr = year + '-' + String(month).padStart(2,'0') + '-' + String(d).padStart(2,'0');
    var info = _statsDayMap[dateStr];
    var cls = 'stats-calendar-day';
    var content = '<span class="stats-cal-day-num">' + d + '</span>';
    var title = '';
    if (info && (info.trade_count > 0 || info.pnl !== 0)) {
      cls += ' has-trades ' + (info.pnl >= 0 ? 'pnl-pos' : 'pnl-neg');
      content += '<span class="stats-cal-day-pnl">' + (info.pnl >= 0 ? '+' : '') + '$' + info.pnl + '</span>';
      content += '<span class="stats-cal-day-count">' + info.trade_count + ' trade' + (info.trade_count !== 1 ? 's' : '') + '</span>';
      title = info.trade_count + ' trade(s), ' + (info.pnl >= 0 ? '+' : '') + '$' + info.pnl;
    }
    html += '<div class="' + cls + '" data-date="' + dateStr + '" title="' + title + '" onclick="showDayDetail(\'' + dateStr + '\')">' + content + '</div>';
  }
  grid.innerHTML = html;
  dayDetail.style.display = 'none';
  dayDetail.innerHTML = '';
}

function statsCalPrevMonth() {
  _statsCalendarMonth--;
  if (_statsCalendarMonth < 1) { _statsCalendarMonth = 12; _statsCalendarYear--; }
  loadStatsCalendar();
}

function statsCalNextMonth() {
  _statsCalendarMonth++;
  if (_statsCalendarMonth > 12) { _statsCalendarMonth = 1; _statsCalendarYear++; }
  loadStatsCalendar();
}

function showDayDetail(dateStr) {
  var info = _statsDayMap[dateStr];
  var panel = document.getElementById('stats-day-detail');
  panel.style.display = 'block';
  if (!info || !info.trades || info.trades.length === 0) {
    panel.innerHTML = '<div class="stats-day-detail-placeholder">No trades on ' + dateStr + '</div>';
    return;
  }
  var rows = info.trades.map(function(t) {
    var pcls = (t.pnl || 0) >= 0 ? 'pnl-pos' : 'pnl-neg';
    var tickerEsc = (t.ticker || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    return '<tr class="stats-day-trade-row" data-ticker="' + (t.ticker || '').replace(/"/g, '&quot;') + '" onclick="selectTicker(\'' + tickerEsc + '\')"><td>' + (t.ticker || '') + '</td><td>' + t.direction + '</td><td class="' + pcls + '">$' + t.pnl + '</td></tr>';
  }).join('');
  panel.innerHTML =
    '<h5>Closed on ' + dateStr + '</h5>' +
    '<table class="stats-day-trades"><thead><tr><th>Ticker</th><th>Dir</th><th>P&L</th></tr></thead><tbody>' + rows + '</tbody></table>' +
    '<div class="stats-day-summary"><strong>Total:</strong> ' + info.trade_count + ' trade(s), <span class="' + ((info.pnl || 0) >= 0 ? 'pnl-pos' : 'pnl-neg') + '">$' + info.pnl + '</span></div>';
}

/* ── Brain (moved to /brain page) ────────────────── */
function loadInsights() { /* no-op: brain dashboard now lives at /brain */ }

/* ── AI Chat ────────────────────────────────────── */
var SMART_PICK_PATTERNS = /\b(what.*(buy|invest|trade|pick|recommend)|should\s+i\s+buy|best\s+stock|top\s+pick|give\s+me\s+a\s+stock|which\s+stock|what\s+to\s+buy|find\s+me|scan.*market|suggest.*stock)/i;
var PATTERN_SUGGEST_PATTERNS = /\b(add\s+(a\s+)?pattern|test\s+(this\s+)?pattern|seed\s+(a\s+)?pattern|try\s+(this\s+)?strategy|create\s+(a\s+)?pattern|new\s+pattern|suggest\s+(a\s+)?pattern|pattern\s*:)/i;
var PATTERN_INDICATOR_COMBO = /\b(rsi|ema|sma|bollinger|macd|adx|vwap|squeeze)\b.*\b(rsi|ema|sma|bollinger|macd|adx|vwap|squeeze|above|below|greater|less)\b/i;
var chatHistory = [];
var _streamAbort = null;
var _acDebounce = null;
var _acIdx = -1;

function _loadChatHistory() {
  try { var s = sessionStorage.getItem('chili_trade_chat'); if (s) chatHistory = JSON.parse(s); } catch(e) {}
  var msgs = document.getElementById('ai-msgs');
  if (msgs && chatHistory.length) {
    msgs.innerHTML = chatHistory.map(function(m) {
      return _buildMsgHtml(m.role, m.text);
    }).join('');
  }
}
function _saveChatHistory() {
  try { sessionStorage.setItem('chili_trade_chat', JSON.stringify(chatHistory.slice(-30))); } catch(e) {}
}
function _historyPayload() {
  return chatHistory.slice(-10).map(function(m) { return { role: m.role, content: m.text }; });
}

function clearChatHistory() {
  chatHistory = [];
  _saveChatHistory();
  var el = document.getElementById('ai-msgs');
  if (el) el.innerHTML = '';
}

function _timeStr() {
  var d = new Date();
  var h = d.getHours(), m = d.getMinutes();
  var ampm = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  return h + ':' + (m < 10 ? '0' : '') + m + ' ' + ampm;
}

function escHtml(s) { var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

/** Backtest win_rate may be fraction [0,1] (DB) or percent [0,100] (engine); return display percent or null. */
function tradingBacktestWinRateToPct(wr) {
  if (wr == null || (typeof wr === 'number' && isNaN(wr))) return null;
  var n = Number(wr);
  if (!isFinite(n)) return null;
  if (n <= 1 && n >= 0) return n * 100;
  return n;
}

/* Ticker tag regex: $AAPL, $BTC-USD or standalone known patterns */
var _TICKER_RE = /\$([A-Z]{1,5}(?:-USD)?)\b/g;
var _BARE_TICKER_RE = /\b([A-Z]{2,5}(?:-USD)?)\b/g;
var _KNOWN_TICKERS = new Set();

function _refreshKnownTickers() {
  fetch('/api/trading/watchlist').then(function(r){return r.json();}).then(function(d) {
    if (d.ok && d.items) d.items.forEach(function(i) { _KNOWN_TICKERS.add(i.ticker); });
  });
  fetch('/api/trading/brain/tickers').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    (d.crypto || []).forEach(function(t) { _KNOWN_TICKERS.add(t.ticker); });
    (d.stocks || []).forEach(function(t) { _KNOWN_TICKERS.add(t.ticker); });
  });
}

function linkifyTickers(html) {
  html = html.replace(_TICKER_RE, function(match, ticker) {
    return '<span class="ticker-tag" data-ticker="'+ticker+'" onclick="event.stopPropagation();selectTicker(\''+ticker+'\')" onmouseenter="_showTickerTooltip(this)" onmouseleave="_hideTickerTooltip(this)">$'+ticker+'</span>';
  });
  html = html.replace(_BARE_TICKER_RE, function(match, ticker) {
    if (_KNOWN_TICKERS.has(ticker) && html.indexOf('data-ticker="'+ticker+'"') === -1) {
      return '<span class="ticker-tag" data-ticker="'+ticker+'" onclick="event.stopPropagation();selectTicker(\''+ticker+'\')" onmouseenter="_showTickerTooltip(this)" onmouseleave="_hideTickerTooltip(this)">$'+ticker+'</span>';
    }
    return match;
  });
  return html;
}

/* Ticker tooltip on hover */
var _ttCache = {};
function _showTickerTooltip(el) {
  var ticker = el.dataset.ticker;
  var tip = document.createElement('div');
  tip.className = 'ticker-tooltip';
  tip.innerHTML = '<div class="tt-loading">Loading...</div>';
  el.appendChild(tip);

  if (_ttCache[ticker] && (Date.now() - _ttCache[ticker]._ts < 30000)) {
    tip.innerHTML = _ttCache[ticker].html;
    return;
  }
  fetch('/api/trading/quote?ticker='+encodeURIComponent(ticker)).then(function(r){return r.json();}).then(function(d) {
    if (!d.ok || d.price == null) { tip.innerHTML = '<span class="tt-ticker">'+ticker+'</span><br><span class="tt-loading">No data</span>'; return; }
    var cls = (d.change_pct||0)>=0 ? 'color:#22c55e' : 'color:#ef4444';
    var sign = (d.change_pct||0)>=0 ? '+' : '';
    var h = '<div class="tt-header"><span class="tt-ticker">'+ticker+'</span></div>' +
      '<div class="tt-price" style="margin:2px 0">$'+d.price+'</div>' +
      '<div class="tt-change" style="'+cls+'">'+sign+(d.change||0)+' ('+sign+(d.change_pct||0)+'%)</div>';
    if (d.volume) h += '<div style="font-size:10px;color:var(--text-muted);margin-top:2px">Vol: '+(d.volume/1e6).toFixed(1)+'M</div>';
    _ttCache[ticker] = { html: h, _ts: Date.now() };
    tip.innerHTML = h;
  }).catch(function() { tip.innerHTML = '<span class="tt-ticker">'+ticker+'</span>'; });
}

function _hideTickerTooltip(el) {
  var tip = el.querySelector('.ticker-tooltip');
  if (tip) tip.remove();
}

/* Inline ticker recommendation cards */
function renderTickerCards(html) {
  var cardRe = /\*\*(?:VERDICT|Signal|Recommendation)[:\s]*\s*(BUY|SELL|HOLD)\*\*/i;
  var tickerInContext = currentTicker;
  var match = html.match(cardRe);
  if (!match) return html;

  var tickers = [];
  var tagRe = /data-ticker="([A-Z]{1,5}(?:-USD)?)"/g;
  var tm;
  while ((tm = tagRe.exec(html)) !== null) { if (tickers.indexOf(tm[1]) === -1) tickers.push(tm[1]); }
  if (!tickers.length) tickers.push(tickerInContext);

  tickers.slice(0, 3).forEach(function(ticker) {
    var signal = match[1].toLowerCase();
    var sigCls = signal === 'buy' ? 'buy' : (signal === 'sell' ? 'sell' : 'hold');
    var card = '<div class="ticker-card" id="tc-'+ticker+'">' +
      '<div class="tc-header">' +
        '<span class="tc-ticker" onclick="selectTicker(\''+ticker+'\')">'+ticker+'</span>' +
        '<span class="tc-signal '+sigCls+'">'+signal.toUpperCase()+'</span>' +
        '<span class="tc-price" id="tc-price-'+ticker+'">...</span>' +
        '<span class="tc-change" id="tc-chg-'+ticker+'"></span>' +
      '</div>' +
      '<div class="tc-actions">' +
        '<button class="tc-btn-chart" onclick="selectTicker(\''+ticker+'\')">View Chart</button>' +
        '<button class="tc-btn-wl" onclick="addTickerToWatchlistQuick(\''+ticker+'\')">+ Watchlist</button>' +
      '</div>' +
    '</div>';
    html += card;
    _fetchCardPrice(ticker);
  });
  return html;
}

function _fetchCardPrice(ticker) {
  fetch('/api/trading/quote?ticker='+encodeURIComponent(ticker)).then(function(r){return r.json();}).then(function(d) {
    var priceEl = document.getElementById('tc-price-'+ticker);
    var chgEl = document.getElementById('tc-chg-'+ticker);
    if (!priceEl || !d.ok || d.price == null) return;
    priceEl.textContent = '$' + d.price;
    if (chgEl && d.change_pct != null) {
      var cls = d.change_pct >= 0 ? 'color:#22c55e' : 'color:#ef4444';
      var sign = d.change_pct >= 0 ? '+' : '';
      chgEl.innerHTML = '<span style="'+cls+'">'+sign+d.change_pct+'%</span>';
    }
  }).catch(function() {});
}

/* Generate follow-up chips */
function _generateFollowups(text, ticker) {
  var chips = [];
  if (/\bbuy\b/i.test(text)) {
    chips.push('Run backtest on ' + ticker);
    chips.push('What are the risks?');
    chips.push('Show similar stocks');
  } else if (/\bsell\b/i.test(text)) {
    chips.push('When exactly should I sell?');
    chips.push('What could change this outlook?');
  } else if (/\bhold\b/i.test(text)) {
    chips.push('What signals should I watch?');
    chips.push('Compare to alternatives');
  }
  if (/\brsi\b/i.test(text) || /\bmacd\b/i.test(text) || /\bbollinger\b/i.test(text)) {
    chips.push('Explain this in simpler terms');
  }
  if (/pattern/i.test(text) || /confidence/i.test(text) || /brain/i.test(text) || /learned/i.test(text)) {
    chips.push('How confident are you in these patterns?');
    chips.push('Show me a trading example');
    chips.push('What is your Brain still learning?');
  }
  if (/fundament/i.test(text) || /P\/E|earnings|revenue|margin/i.test(text)) {
    chips.push('How do fundamentals affect this trade?');
  }
  if (chips.length === 0) {
    chips.push('Tell me more');
    chips.push('Analyze ' + ticker);
  }
  chips.push('What should I buy?');
  return chips.slice(0, 4);
}

function _renderFollowups(chips) {
  if (!chips || !chips.length) return '';
  var html = '<div class="followup-chips">';
  chips.forEach(function(c) {
    html += '<button class="followup-chip" onclick="_clickFollowup(this)">'+escHtml(c)+'</button>';
  });
  html += '</div>';
  return html;
}

function _clickFollowup(el) {
  var text = el.textContent;
  document.getElementById('ai-input').value = text;
  sendAiChat();
}

/* Build message HTML with avatar, timestamp, copy, followups */
function _buildMsgHtml(role, content, followups) {
  var avatar = role === 'assistant' ? '&#x1F336;' : '&#x1F464;';
  var time = _timeStr();
  var actions = role === 'assistant' ? '<div class="msg-actions"><button onclick="_copyBubble(this)">Copy</button></div>' : '';
  var followHtml = role === 'assistant' && followups ? _renderFollowups(followups) : '';
  return '<div class="ai-msg '+role+'">' +
    '<div class="msg-avatar">'+avatar+'</div>' +
    '<div class="msg-body">' +
      '<div class="bubble">'+content+'</div>' +
      '<div class="msg-time">'+time+'</div>' +
      actions + followHtml +
    '</div></div>';
}

function _copyBubble(btn) {
  var bubble = btn.closest('.msg-body').querySelector('.bubble');
  if (!bubble) return;
  var text = bubble.innerText || bubble.textContent;
  navigator.clipboard.writeText(text).then(function() {
    btn.textContent = 'Copied!';
    setTimeout(function() { btn.textContent = 'Copy'; }, 1500);
  });
}

function appendAiMsg(role, text, opts) {
  opts = opts || {};
  var msgs = document.getElementById('ai-msgs');
  var content;
  if (role === 'assistant' && typeof marked !== 'undefined') {
    try { content = marked.parse(text); } catch(e) { content = escHtml(text); }
    content = linkifyTickers(content);
    if (!opts.skipCards) content = renderTickerCards(content);
  } else {
    content = escHtml(text);
  }

  // Detect and render trade proposals from AI responses
  var proposalHtml = '';
  if (role === 'assistant' && !opts.isThinking && !opts.skipProposals) {
    var proposals = _tryParseProposals(text);
    proposals.forEach(function(p) {
      proposalHtml += renderTradeProposal(p);
    });
  }

  var followups = (role === 'assistant' && !opts.isThinking) ? _generateFollowups(text, currentTicker) : null;
  msgs.innerHTML += _buildMsgHtml(role, content + proposalHtml, opts.isThinking ? null : followups);
  _aiScrollToBottom();

  if (!opts.isThinking) {
    chatHistory.push({ role: role, text: text, time: Date.now() });
    _saveChatHistory();
  }
}

function removeLastAiMsg() {
  var msgs = document.getElementById('ai-msgs');
  if (msgs.lastElementChild) msgs.removeChild(msgs.lastElementChild);
}

/* Thinking indicator */
function _showThinking(statusText) {
  var msgs = document.getElementById('ai-msgs');
  var html = _buildMsgHtml('assistant',
    '<div class="thinking-dots"><span></span><span></span><span></span></div>' +
    '<div class="thinking-status">' + escHtml(statusText || 'Thinking...') + '</div>',
    null
  );
  msgs.innerHTML += html;
  _aiScrollToBottom();
}

/* Stop button */
function _showStopBtn() {
  document.getElementById('stop-btn-container').innerHTML = '<button class="stop-btn" onclick="_stopStream()">&#x25A0; Stop generating</button>';
}
function _hideStopBtn() {
  document.getElementById('stop-btn-container').innerHTML = '';
}
function _stopStream() {
  if (_streamAbort) { _streamAbort.abort(); _streamAbort = null; }
  _hideStopBtn();
}

/* SSE streaming for analyze */
function _streamAnalyze(msg, isSmartPick) {
  var url = isSmartPick ? '/api/trading/smart-pick/stream' : '/api/trading/analyze/stream';
  if (isSmartPick) {
    _showThinking('Scanning the entire market...');
    _showStopBtn();

    var controller = new AbortController();
    _streamAbort = controller;
    var _streamStartTime = Date.now();
    var _slowTimer = null;
    var _timeoutTimer = null;

    fetch(url + '?risk_tolerance=medium', { signal: controller.signal })
      .then(function(response) {
        if (!response.ok || !response.body) {
          return response.json().then(function(d) {
            removeLastAiMsg(); _hideStopBtn();
            appendAiMsg('assistant', (d && d.reply) || 'Smart Pick unavailable.');
          });
        }
        removeLastAiMsg();

        var msgs = document.getElementById('ai-msgs');
        var msgEl = document.createElement('div');
        msgEl.innerHTML = _buildMsgHtml('assistant', '<span class="streaming-cursor"></span>', null);
        msgs.appendChild(msgEl.firstElementChild);
        var bubble = msgs.lastElementChild.querySelector('.bubble');
        var fullText = '';
        var renderTimer = null;
        var gotFirstToken = false;
        var scannedPrefix = '';

        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';

        _slowTimer = setTimeout(function() {
          if (!gotFirstToken && bubble) {
            bubble.innerHTML = '<span class="streaming-slow">Scanning the entire market — this can take a moment...</span><span class="streaming-cursor"></span>';
            _aiScrollToBottom();
          }
        }, 15000);

        _timeoutTimer = setTimeout(function() {
          if (!gotFirstToken) {
            controller.abort();
            clearInterval(renderTimer);
            _hideStopBtn();
            if (bubble) bubble.innerHTML = '<span class="streaming-slow">Smart Pick timed out. The AI model may be overloaded — please try again.</span>';
          }
        }, 60000);

        function _renderStream() {
          if (!bubble) return;
          var parsed = fullText;
          try { parsed = marked.parse(fullText); } catch(e) {}
          parsed = linkifyTickers(parsed);
          bubble.innerHTML = (scannedPrefix ? scannedPrefix + '\n\n' : '') + parsed + '<span class="streaming-cursor"></span>';
          _aiScrollToBottom();
        }

        function pump() {
          reader.read().then(function(result) {
            if (result.done) {
              clearInterval(renderTimer);
              clearTimeout(_slowTimer); clearTimeout(_timeoutTimer);
              _hideStopBtn();
              var parsed2 = fullText;
              try { parsed2 = marked.parse(fullText); } catch(e) {}
              parsed2 = linkifyTickers(parsed2);
              parsed2 = renderTickerCards(parsed2);
              var followups = _generateFollowups(fullText, currentTicker);
              bubble.innerHTML = (scannedPrefix ? scannedPrefix + '\n\n' : '') + parsed2;
              var body = bubble.closest('.msg-body');
              if (body) {
                body.querySelector('.msg-time').textContent = _timeStr();
                var actDiv = document.createElement('div');
                actDiv.className = 'msg-actions';
                actDiv.innerHTML = '<button onclick="_copyBubble(this)">Copy</button>';
                body.appendChild(actDiv);
                var chipDiv = document.createElement('div');
                chipDiv.innerHTML = _renderFollowups(followups);
                if (chipDiv.firstElementChild) body.appendChild(chipDiv.firstElementChild);
              }
              chatHistory.push({ role: 'assistant', text: (scannedPrefix ? scannedPrefix + '\n\n' : '') + fullText, time: Date.now() });
              _saveChatHistory();
              var elapsed = ((Date.now() - _streamStartTime) / 1000).toFixed(1);
              var ctxEl = document.getElementById('ai-ctx-info');
              ctxEl.textContent = elapsed + 's';
              ctxEl.title = 'Response time: ' + elapsed + 's | Smart Pick';
              ctxEl.classList.add('visible');
              return;
            }
            buffer += decoder.decode(result.value, { stream: true });
            var lines = buffer.split('\n');
            buffer = lines.pop();
            lines.forEach(function(line) {
              if (line.startsWith('data: ')) {
                var payload = line.substring(6);
                if (payload === '[DONE]') return;
                try {
                  var ev = JSON.parse(payload);
                  if (ev.meta) {
                    var scanned = ev.meta.scanned || 0;
                    var qualified = ev.meta.qualified || 0;
                    scannedPrefix = '*Scanned ' + scanned.toLocaleString() + ' tickers, ' + qualified + ' qualified*';
                  }
                  if (ev.token) {
                    if (!gotFirstToken) {
                      gotFirstToken = true;
                      clearTimeout(_slowTimer); clearTimeout(_timeoutTimer);
                    }
                    fullText += ev.token;
                  }
                } catch(e) {
                  if (!gotFirstToken) { gotFirstToken = true; clearTimeout(_slowTimer); clearTimeout(_timeoutTimer); }
                  fullText += payload;
                }
              }
            });
            pump();
          }).catch(function(e) {
            clearInterval(renderTimer);
            clearTimeout(_slowTimer); clearTimeout(_timeoutTimer);
            _hideStopBtn();
            if (e.name !== 'AbortError') {
              bubble.innerHTML = marked.parse(fullText || 'Smart Pick stream interrupted.');
            }
          });
        }

        renderTimer = setInterval(_renderStream, 80);
        pump();
      })
      .catch(function(e) {
        removeLastAiMsg(); _hideStopBtn();
        if (e.name !== 'AbortError') {
          appendAiMsg('assistant', 'Error reaching Smart Pick AI.');
        }
      });
    return;
  }

  _showThinking('Analyzing ' + currentTicker + '...');
  _showStopBtn();

  var params = new URLSearchParams({
    ticker: currentTicker, interval: currentInterval, message: msg,
    history: JSON.stringify(_historyPayload())
  });

  var controller2 = new AbortController();
  _streamAbort = controller2;
  var _streamStartTime = Date.now();
  var _slowTimer = null;
  var _timeoutTimer = null;

  fetch('/api/trading/analyze/stream?' + params.toString(), { signal: controller2.signal })
    .then(function(response) {
      if (!response.ok || !response.body) {
        clearTimeout(_slowTimer); clearTimeout(_timeoutTimer);
        return response.json().then(function(d) {
          removeLastAiMsg(); _hideStopBtn();
          var dispErr = _stripAiAssistantStructuredBlocks(d.reply || '');
          appendAiMsg('assistant', dispErr || 'Analysis unavailable.');
          var lvErr = d.annotations || _parseChartLevelsFallback(d.reply || '');
          if (lvErr) drawAiAnnotations(lvErr);
          if (d.trade_plan_levels) {
            var row = document.getElementById('ai-msgs').lastElementChild;
            var bod = row && row.querySelector('.msg-body');
            if (bod) _attachTradePlanApplyButton(bod, d.trade_plan_levels, currentTicker);
          }
          if (d.pattern_imminent_attach) {
            var rowP = document.getElementById('ai-msgs').lastElementChild;
            var bodP = rowP && rowP.querySelector('.msg-body');
            if (bodP) _attachPatternImminentButton(bodP, d.pattern_imminent_attach);
          }
        });
      }
      removeLastAiMsg();

      var msgs = document.getElementById('ai-msgs');
      var msgEl = document.createElement('div');
      msgEl.innerHTML = _buildMsgHtml('assistant', '<span class="streaming-cursor"></span>', null);
      msgs.appendChild(msgEl.firstElementChild);
      var bubble = msgs.lastElementChild.querySelector('.bubble');
      var fullText = '';
      var streamAnnotations = null;
      var streamTradePlanLevels = null;
      var streamPatternImminentAttach = null;
      var renderTimer = null;
      var gotFirstToken = false;

      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';

      _slowTimer = setTimeout(function() {
        if (!gotFirstToken && bubble) {
          bubble.innerHTML = '<span class="streaming-slow">Analysis is taking longer than expected...</span><span class="streaming-cursor"></span>';
          _aiScrollToBottom();
        }
      }, 15000);

      _timeoutTimer = setTimeout(function() {
        if (!gotFirstToken) {
          controller2.abort();
          clearInterval(renderTimer);
          _hideStopBtn();
          if (bubble) bubble.innerHTML = '<span class="streaming-slow">Analysis timed out. The AI model may be overloaded — please try again.</span>';
        }
      }, 45000);

      function _renderStream() {
        if (!bubble) return;
        var displayRaw = _stripAiAssistantStructuredBlocks(fullText);
        var parsed = displayRaw;
        try { parsed = marked.parse(displayRaw); } catch(e) {}
        parsed = linkifyTickers(parsed);
        bubble.innerHTML = parsed + '<span class="streaming-cursor"></span>';
        _aiScrollToBottom();
      }

      function pump() {
        reader.read().then(function(result) {
          if (result.done) {
            clearInterval(renderTimer);
            clearTimeout(_slowTimer); clearTimeout(_timeoutTimer);
            _hideStopBtn();
            var displayText = _stripAiAssistantStructuredBlocks(fullText);
            var parsed2 = displayText;
            try { parsed2 = marked.parse(displayText); } catch(e) {}
            parsed2 = linkifyTickers(parsed2);
            parsed2 = renderTickerCards(parsed2);
            var followups = _generateFollowups(displayText, currentTicker);
            bubble.innerHTML = parsed2;
            var body = bubble.closest('.msg-body');
            if (body) {
              body.querySelector('.msg-time').textContent = _timeStr();
              var actDiv = document.createElement('div');
              actDiv.className = 'msg-actions';
              actDiv.innerHTML = '<button onclick="_copyBubble(this)">Copy</button>';
              body.appendChild(actDiv);
              var chipDiv = document.createElement('div');
              chipDiv.innerHTML = _renderFollowups(followups);
              if (chipDiv.firstElementChild) body.appendChild(chipDiv.firstElementChild);
              var tlev = streamTradePlanLevels || _parseTradePlanLevelsFromText(fullText);
              if (tlev) {
                if (!tlev.ticker) tlev.ticker = currentTicker;
                _attachTradePlanApplyButton(body, tlev, currentTicker);
              }
              if (streamPatternImminentAttach) {
                _attachPatternImminentButton(body, streamPatternImminentAttach);
              }
            }
            chatHistory.push({ role: 'assistant', text: displayText, time: Date.now() });
            _saveChatHistory();
            var levelsDraw = streamAnnotations || _parseChartLevelsFallback(fullText);
            if (levelsDraw) drawAiAnnotations(levelsDraw);
            var elapsed = ((Date.now() - _streamStartTime) / 1000).toFixed(1);
            var ctxEl = document.getElementById('ai-ctx-info');
            ctxEl.textContent = elapsed + 's';
            ctxEl.title = 'Response time: ' + elapsed + 's | Ticker: ' + currentTicker + ' | History: ' + chatHistory.length + ' msgs';
            ctxEl.classList.add('visible');
            return;
          }
          buffer += decoder.decode(result.value, { stream: true });
          var lines = buffer.split('\n');
          buffer = lines.pop();
          lines.forEach(function(line) {
            if (line.startsWith('data: ')) {
              var payload = line.substring(6);
              if (payload === '[DONE]') return;
              try {
                var ev = JSON.parse(payload);
                if (ev.annotations && typeof ev.annotations === 'object') {
                  streamAnnotations = ev.annotations;
                }
                if (ev.trade_plan_levels && typeof ev.trade_plan_levels === 'object') {
                  streamTradePlanLevels = ev.trade_plan_levels;
                }
                if (ev.pattern_imminent_attach && typeof ev.pattern_imminent_attach === 'object') {
                  streamPatternImminentAttach = ev.pattern_imminent_attach;
                }
                if (ev.token) {
                  if (!gotFirstToken) {
                    gotFirstToken = true;
                    clearTimeout(_slowTimer); clearTimeout(_timeoutTimer);
                  }
                  fullText += ev.token;
                }
                if (ev.suggestions) { /* handled at end */ }
              } catch(e) {
                if (!gotFirstToken) { gotFirstToken = true; clearTimeout(_slowTimer); clearTimeout(_timeoutTimer); }
                fullText += payload;
              }
            }
          });
          pump();
        }).catch(function(e) {
          clearInterval(renderTimer);
          clearTimeout(_slowTimer); clearTimeout(_timeoutTimer);
          _hideStopBtn();
          if (e.name !== 'AbortError') {
            var dispInt = _stripAiAssistantStructuredBlocks(fullText || '');
            try {
              bubble.innerHTML = marked.parse(dispInt || 'Stream interrupted.');
            } catch (e2) {
              bubble.innerHTML = dispInt || 'Stream interrupted.';
            }
            var lvInt = streamAnnotations || _parseChartLevelsFallback(fullText || '');
            if (lvInt) drawAiAnnotations(lvInt);
            var tInt = streamTradePlanLevels || _parseTradePlanLevelsFromText(fullText || '');
            if (tInt && bubble) {
              var bodI = bubble.closest('.msg-body');
              if (bodI) {
                if (!tInt.ticker) tInt.ticker = currentTicker;
                _attachTradePlanApplyButton(bodI, tInt, currentTicker);
              }
            }
            if (streamPatternImminentAttach && bubble) {
              var bodPa = bubble.closest('.msg-body');
              if (bodPa) _attachPatternImminentButton(bodPa, streamPatternImminentAttach);
            }
          }
        });
      }

      renderTimer = setInterval(_renderStream, 80);
      pump();
    })
    .catch(function(e) {
      removeLastAiMsg(); _hideStopBtn();
      if (e.name !== 'AbortError') {
        appendAiMsg('assistant', 'Error reaching AI. Retrying with fallback...');
        fetch('/api/trading/analyze', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ ticker: currentTicker, interval: currentInterval, message: msg, history: _historyPayload() })
        }).then(function(r){return r.json();}).then(function(d) {
          removeLastAiMsg();
          var dispFb = _stripAiAssistantStructuredBlocks(d.reply || '');
          appendAiMsg('assistant', dispFb || 'No response.');
          var lvFb = d.annotations || _parseChartLevelsFallback(d.reply || '');
          if (lvFb) drawAiAnnotations(lvFb);
          if (d.trade_plan_levels) {
            var rowF = document.getElementById('ai-msgs').lastElementChild;
            var bodF = rowF && rowF.querySelector('.msg-body');
            if (bodF) _attachTradePlanApplyButton(bodF, d.trade_plan_levels, currentTicker);
          }
          if (d.pattern_imminent_attach) {
            var rowPa = document.getElementById('ai-msgs').lastElementChild;
            var bodPa = rowPa && rowPa.querySelector('.msg-body');
            if (bodPa) _attachPatternImminentButton(bodPa, d.pattern_imminent_attach);
          }
        }).catch(function() { removeLastAiMsg(); appendAiMsg('assistant', 'Error reaching AI.'); });
      }
    });
}

var LIQUIDATION_PATTERN = /sell.*?(all|everything|my).*?(coins?|tokens?|crypto).*?(transfer|send|move).*?coinbase|sell.*?metamask.*?(send|transfer|move|coinbase)|metamask.*?(sell|liquidat|send|move|transfer).*?coinbase|liquidat.*?metamask|move.*?metamask.*?coinbase|transfer.*?(everything|all).*?coinbase|swap.*?(all|everything).*?coinbase|send.*?(all|everything).*?coinbase/i;

function _handlePatternSuggest(msg) {
  _showThinking('Creating pattern from your description...');
  fetch('/api/trading/patterns/suggest', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({description: msg}),
  }).then(function(r){ return r.json(); }).then(function(d) {
    removeLastAiMsg();
    if (!d.ok) {
      appendAiMsg('assistant', 'Sorry, I could not create a pattern from that description: ' + (d.error || 'Unknown error'));
      return;
    }
    var p = d.pattern;
    var rules = [];
    try {
      var parsed = JSON.parse(p.rules_json || '{}');
      (parsed.conditions || []).forEach(function(c) {
        var line = c.indicator + ' ' + c.op + ' ' + (c.ref || c.value);
        if (c.params) line += ' (' + JSON.stringify(c.params) + ')';
        rules.push(line);
      });
    } catch(e) {}

    var html = '**Pattern Created: ' + escHtml(p.name) + '**\n\n';
    if (p.description) html += p.description + '\n\n';
    if (rules.length) {
      html += '**Rules:**\n';
      rules.forEach(function(r){ html += '- `' + r + '`\n'; });
      html += '\n';
    }
    html += 'Score boost: **+' + (p.score_boost || 0) + '** | ';
    html += d.hypothesis_id ? 'Hypothesis created for validation' : 'Will be tested in the next learning cycle';
    appendAiMsg('assistant', html);

    var msgs = document.getElementById('ai-msgs');
    var lastMsg = msgs.lastElementChild;
    if (lastMsg) {
      var chips = document.createElement('div');
      chips.className = 'ai-followup-chips';
      chips.innerHTML =
        '<button onclick="_backtestSuggestedPattern(' + p.id + ')">Backtest This Pattern</button>' +
        '<button onclick="askLearn(\'Explain the pattern: ' + escHtml(p.name).replace(/'/g, "\\'") + '\')">Explain Pattern</button>';
      lastMsg.appendChild(chips);
    }
  }).catch(function(e) {
    removeLastAiMsg();
    appendAiMsg('assistant', 'Error creating pattern: ' + (e.message || 'Network error'));
  });
}

function _backtestSuggestedPattern(patternId) {
  _showThinking('Running backtest on the pattern...');
  fetch('/api/trading/patterns/' + patternId + '/backtest', {method: 'POST'})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      removeLastAiMsg();
      if (!d.ok) {
        appendAiMsg('assistant', 'Backtest failed: ' + (d.error || 'Unknown error'));
        return;
      }
      var r = d.result || d;
      var html = '**Backtest Results**\n\n';
      html += '- Win Rate: **' + (function(){ var w = tradingBacktestWinRateToPct(r.win_rate); return (w != null ? w : 0).toFixed(1); })() + '%**\n';
      html += '- Return: **' + (r.return_pct || 0).toFixed(1) + '%**\n';
      html += '- Trades: **' + (r.trade_count || 0) + '**\n';
      html += '- Sharpe: **' + (r.sharpe || 0).toFixed(2) + '**\n';
      html += '- Max Drawdown: **' + (r.max_drawdown || 0).toFixed(1) + '%**\n';
      appendAiMsg('assistant', html);
    }).catch(function(e) {
      removeLastAiMsg();
      appendAiMsg('assistant', 'Backtest error: ' + (e.message || 'Network error'));
    });
}

function sendAiChat() {
  var inp = document.getElementById('ai-input');
  var msg = inp.value.trim();
  if (!msg) return;
  inp.value = '';
  _hideAutocomplete();
  appendAiMsg('user', msg);

  if (_liquidationActive && _w3._pendingLiquidation) {
    if (/^confirm$/i.test(msg.trim())) { _confirmLiquidation(); return; }
    if (/^cancel$/i.test(msg.trim())) { _cancelLiquidation(); return; }
  }

  if (LIQUIDATION_PATTERN.test(msg)) {
    _handleLiquidationIntent();
    return;
  }

  if (PATTERN_SUGGEST_PATTERNS.test(msg) || PATTERN_INDICATOR_COMBO.test(msg)) {
    _handlePatternSuggest(msg);
    return;
  }

  var isSmartPick = SMART_PICK_PATTERNS.test(msg);
  _streamAnalyze(msg, isSmartPick);
}

function askLearn(question) {
  appendAiMsg('user', question);
  _streamAnalyze(question, false);
}

function askSmartPick() {
  var msg = 'What are your top 10 stock picks I should buy RIGHT NOW? For each give me exact buy-in price, sell target, stop-loss, hold duration, position size, and confidence level. Rank by conviction.';
  appendAiMsg('user', 'What stocks should I buy right now?');
  _streamAnalyze(msg, true);
}

function runAnalysis() {
  var msg = 'Analyze ' + currentTicker + ' on the ' + currentInterval + ' timeframe. Give me a clear verdict: should I buy, sell, or hold?';
  if (_activeBreakoutRow && _activeBreakoutRow.ticker.toUpperCase() === currentTicker) {
    var bo = _activeBreakoutRow;
    msg += '\n\nIMPORTANT: This ticker was selected from the BREAKOUT screener. ' +
      'Status: ' + (bo.status || bo.signal || 'watch').toUpperCase() +
      ', Resistance: $' + bo.resistance +
      ', Breakout Entry: $' + bo.entry_price +
      ', Stop: $' + bo.stop_loss +
      ', Target: $' + bo.take_profit +
      (bo.dist_to_breakout != null ? ', Distance to breakout: ' + bo.dist_to_breakout + '%' : '') +
      (bo.bb_squeeze ? ', BB Squeeze: active' : '') +
      '. Evaluate this as a BREAKOUT setup — use the breakout entry/stop/target levels, not general swing levels.';
  }
  _streamAnalyze(msg, false);
}

function askBrainExplain() {
  var msg = 'Summarize everything your AI Brain has learned so far. For each pattern you have discovered:\n' +
    '1. Explain in plain English what the pattern means (no jargon)\n' +
    '2. How confident are you in it and how many times you have seen it\n' +
    '3. Give a real example of when this pattern would trigger\n' +
    '4. How should I use this pattern in my trading decisions?\n\n' +
    'Also tell me: What is your current market thesis? What are you most confident about right now?';
  appendAiMsg('user', 'What has your Brain learned? Explain it to me.');
  _streamAnalyze(msg, false);
}

function _promptPatternSuggest() {
  appendAiMsg('assistant',
    '**Describe a trading pattern** and I\'ll create it for Chili\'s brain to test and validate.\n\n' +
    'Examples:\n' +
    '- "RSI above 70 with price above EMA 20, 50, 100 and resistance retesting"\n' +
    '- "MACD bullish crossover with BB squeeze and volume spike"\n' +
    '- "Price near VWAP with ADX > 25 and narrow range"\n\n' +
    'Just describe the conditions naturally — I\'ll convert them into rules.'
  );
  document.getElementById('ai-input').focus();
}

function askPatternExplain(patternDesc) {
  var aiPanel = document.getElementById('ai-panel');
  if (aiPanel && !aiPanel.classList.contains('open')) {
    toggleAiPanel();
  }
  var msg = 'Explain this pattern your Brain discovered in simple terms I can understand:\n\n"' +
    patternDesc + '"\n\n' +
    'Tell me: What does this mean? When does it trigger? How should I trade when I see this? ' +
    'What are the risks? Give me a concrete example with a real stock or crypto.';
  appendAiMsg('user', 'Explain pattern: ' + patternDesc.substring(0, 80) + (patternDesc.length > 80 ? '...' : ''));
  _streamAnalyze(msg, false);
}

/* ── Ticker Autocomplete ──────────────────────────── */
function _hideAutocomplete() {
  document.getElementById('ticker-ac').classList.remove('open');
  document.getElementById('ticker-ac').innerHTML = '';
  _acIdx = -1;
}

function _showAutocomplete(results) {
  var ac = document.getElementById('ticker-ac');
  if (!results.length) { _hideAutocomplete(); return; }
  _acIdx = -1;
  var html = '';
  results.forEach(function(r, i) {
    html += '<div class="ticker-ac-item" data-idx="'+i+'" data-ticker="'+escHtml(r.ticker)+'" onclick="_selectAcItem(this)">' +
      '<span class="ac-sym">'+escHtml(r.ticker)+'</span>' +
      '<span class="ac-name">'+escHtml(r.name||'')+'</span>' +
      '<span class="ac-type">'+escHtml(r.type||'')+'</span>' +
    '</div>';
  });
  ac.innerHTML = html;
  ac.classList.add('open');
}

function _selectAcItem(el) {
  var ticker = el.dataset.ticker;
  var inp = document.getElementById('ai-input');
  var val = inp.value;
  var dollarIdx = val.lastIndexOf('$');
  if (dollarIdx >= 0) {
    inp.value = val.substring(0, dollarIdx) + '$' + ticker + ' ';
  } else {
    inp.value += '$' + ticker + ' ';
  }
  inp.focus();
  _hideAutocomplete();
}

function _onAiInputKey(e) {
  if (e.key === 'Enter') {
    if (document.getElementById('ticker-ac').classList.contains('open') && _acIdx >= 0) {
      var items = document.querySelectorAll('.ticker-ac-item');
      if (items[_acIdx]) { _selectAcItem(items[_acIdx]); e.preventDefault(); return; }
    }
    e.preventDefault();
    sendAiChat();
    return;
  }
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    var items2 = document.querySelectorAll('.ticker-ac-item');
    if (!items2.length) return;
    e.preventDefault();
    items2.forEach(function(it) { it.classList.remove('active'); });
    if (e.key === 'ArrowDown') _acIdx = Math.min(_acIdx + 1, items2.length - 1);
    else _acIdx = Math.max(_acIdx - 1, 0);
    items2[_acIdx].classList.add('active');
    return;
  }
  if (e.key === 'Escape') { _hideAutocomplete(); return; }

  clearTimeout(_acDebounce);
  _acDebounce = setTimeout(function() {
    var val = document.getElementById('ai-input').value;
    var dollarIdx = val.lastIndexOf('$');
    if (dollarIdx < 0 || dollarIdx === val.length - 1) { _hideAutocomplete(); return; }
    var query = val.substring(dollarIdx + 1).trim();
    if (query.length < 1) { _hideAutocomplete(); return; }
    fetch('/api/trading/search?q='+encodeURIComponent(query)).then(function(r){return r.json();}).then(function(d) {
      if (d.ok) _showAutocomplete(d.results || []);
    }).catch(function() {});
  }, 250);
}

/* ── Custom Screener ───────────────────────────── */
var _screenerPresets = {};

function loadScreenerPresets() {
  fetch('/api/trading/screener/presets').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var sel = document.getElementById('screen-preset');
    sel.innerHTML = '<option value="">-- Select a pattern --</option>';
    d.presets.forEach(function(p) {
      _screenerPresets[p.id] = p;
      if (p.scan_type && p.scan_type !== 'swing') return;
      var opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name + ' (' + p.conditions + ' cond' + (p.confirmations ? ', ' + p.confirmations + ' confirm' : '') + ')';
      sel.appendChild(opt);
    });
    sel.addEventListener('change', function() {
      var desc = document.getElementById('screen-desc');
      var p = _screenerPresets[this.value];
      if (p) { desc.textContent = p.description; desc.style.display = 'block'; }
      else { desc.style.display = 'none'; }
    });
  });
}

/* ── Screener sort/filter engine ─────────────────── */
var _swingResults = [], _dtResults = [], _boResults = [];
var _scrSort = {swing:{field:'score',dir:'desc'}, dt:{field:'score',dir:'desc'}, bo:{field:'score',dir:'desc'}};
var _scrFilters = {
  swing: {signal:'all', minScore:0, search:''},
  dt: {signal:'all', minScore:0, search:''},
  bo: {signal:'all', minScore:0, search:''},
};
var _activePatterns = {};

function _togglePattern(patId, btn) {
  if (_activePatterns[patId]) {
    delete _activePatterns[patId];
    btn.classList.remove('active');
  } else {
    _activePatterns[patId] = true;
    btn.classList.add('active');
  }
  _renderBoResults();
}

function _applyPatternFilters(results) {
  if (!Object.keys(_activePatterns).length) return results;
  return results.filter(function(r) {
    var ind = r.indicators || {};
    if (_activePatterns['rsi_ema_stack']) {
      var rsi = ind.rsi;
      var ema20 = ind.ema_20;
      var ema50 = ind.ema_50;
      var ema100 = ind.ema_100;
      if (rsi == null || rsi <= 50) return false;
      if (ema20 == null || ema50 == null || ema100 == null) return false;
      if (r.price <= ema20 || r.price <= ema50 || r.price <= ema100) return false;
    }
    return true;
  });
}

/* ── Pattern Engine UI ─────────────────────────────── */
function _loadPatterns() {
  fetch('/api/trading/patterns').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    _renderPatternsList(d.patterns || []);
  });
  _updateResearchStatus();
}

function _renderPatternsList(patterns) {
  var el = document.getElementById('patterns-list');
  if (!el) return;
  if (!patterns.length) {
    el.innerHTML = '<div class="scr-empty">No patterns yet. Click "Suggest Pattern" to create one.</div>';
    return;
  }
  var originColors = {builtin:'#06b6d4',user:'#8b5cf6',brain_discovered:'#22c55e',web_discovered:'#f59e0b'};
  el.innerHTML = patterns.map(function(p) {
    var rules = [];
    try { rules = JSON.parse(p.rules_json || '{}').conditions || []; } catch(e) {}
    var rulesStr = rules.map(function(c) {
      var ref = c.ref ? c.ref : c.value;
      return c.indicator + ' ' + c.op + ' ' + (Array.isArray(ref) ? ref.join('-') : ref);
    }).join(' & ');
    var confBar = Math.round((p.confidence || 0) * 100);
    var confColor = confBar >= 70 ? '#22c55e' : confBar >= 40 ? '#f59e0b' : '#ef4444';
    return '<div class="pat-card">' +
      '<div class="pat-header">' +
        '<span class="pat-origin" style="background:' + (originColors[p.origin] || '#6366f1') + '">' + p.origin + '</span>' +
        '<span class="pat-name">' + p.name + '</span>' +
        '<span class="pat-toggle">' +
          '<label class="pat-switch"><input type="checkbox" ' + (p.active ? 'checked' : '') +
            ' onchange="_togglePatternActive(' + p.id + ',this.checked)"><span class="pat-slider"></span></label>' +
        '</span>' +
      '</div>' +
      (p.description ? '<div class="pat-desc">' + p.description + '</div>' : '') +
      '<div class="pat-rules">' + (rulesStr || 'No conditions') + '</div>' +
      '<div class="pat-stats">' +
        '<span>Boost: <b>+' + (p.score_boost||0).toFixed(1) + '</b></span>' +
        '<span>Confidence: <span style="color:' + confColor + '">' + confBar + '%</span></span>' +
        (p.win_rate != null ? '<span>WR: ' + (function(){ var w = tradingBacktestWinRateToPct(p.win_rate); return w != null ? w.toFixed(0) : '--'; })() + '%</span>' : '') +
        (p.backtest_count ? '<span>Tests: ' + p.backtest_count + '</span>' : '') +
      '</div>' +
      '<div class="pat-actions">' +
        '<button onclick="_backtestPattern(' + p.id + ')" class="pat-btn">Backtest</button>' +
        (p.origin !== 'builtin' ? '<button onclick="_deletePattern(' + p.id + ')" class="pat-btn pat-btn-del">Delete</button>' : '') +
      '</div>' +
    '</div>';
  }).join('');
}

function _showSuggestPattern() {
  var form = document.getElementById('suggest-pattern-form');
  if (form) form.style.display = form.style.display === 'none' ? 'block' : 'none';
}

function _submitSuggestPattern() {
  var input = document.getElementById('suggest-pattern-input');
  var status = document.getElementById('suggest-pattern-status');
  if (!input || !input.value.trim()) return;
  if (status) status.textContent = 'Analyzing pattern with AI...';
  fetch('/api/trading/patterns/suggest', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({description: input.value.trim()})
  }).then(function(r){return r.json();}).then(function(d) {
    if (d.ok) {
      if (status) status.innerHTML = '<span style="color:#22c55e">Pattern created: ' + d.pattern.name + '</span>';
      input.value = '';
      _loadPatterns();
    } else {
      if (status) status.innerHTML = '<span style="color:#ef4444">' + (d.error || 'Failed') + '</span>';
    }
  }).catch(function() {
    if (status) status.innerHTML = '<span style="color:#ef4444">Network error</span>';
  });
}

function _togglePatternActive(id, active) {
  fetch('/api/trading/patterns/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({active: active})
  });
}

function _deletePattern(id) {
  if (!confirm('Delete this pattern?')) return;
  fetch('/api/trading/patterns/' + id, {method: 'DELETE'}).then(function(){_loadPatterns();});
}

function _backtestPattern(id) {
  var ticker = currentTicker || 'AAPL';
  var el = document.getElementById('patterns-list');
  var btn = event.target;
  btn.textContent = 'Running...'; btn.disabled = true;
  fetch('/api/trading/patterns/' + id + '/backtest?ticker=' + encodeURIComponent(ticker) + '&period=1y')
    .then(function(r){return r.json();}).then(function(d) {
      btn.textContent = 'Backtest'; btn.disabled = false;
      if (d.ok) {
        alert('Backtest on ' + ticker + ':\nReturn: ' + (d.return_pct||0).toFixed(1) + '%\nWin Rate: ' + (function(){ var w = tradingBacktestWinRateToPct(d.win_rate); return w != null ? w.toFixed(0) : '0'; })() + '%\nTrades: ' + (d.trade_count||0) + '\nSharpe: ' + (d.sharpe||0).toFixed(2));
        _loadPatterns();
      } else {
        alert(d.error || 'Backtest failed');
      }
    }).catch(function() { btn.textContent = 'Backtest'; btn.disabled = false; });
}

function _triggerWebResearch() {
  var btn = document.getElementById('btn-web-research');
  var status = document.getElementById('web-research-status');
  if (btn) { btn.disabled = true; btn.textContent = '\u{1F310} Researching...'; }
  if (status) status.textContent = 'Searching the web for new patterns...';
  fetch('/api/trading/patterns/research', {method: 'POST'})
    .then(function(r){return r.json();}).then(function(d) {
      if (btn) { btn.disabled = false; btn.innerHTML = '&#x1F310; Research Web'; }
      if (d.ok) {
        if (status) status.innerHTML = '<span style="color:#22c55e">Research started in background. Refresh in a minute.</span>';
        setTimeout(function() { _loadPatterns(); _updateResearchStatus(); }, 60000);
      } else {
        if (status) status.innerHTML = '<span style="color:#ef4444">' + (d.error || 'Failed') + '</span>';
      }
    }).catch(function() {
      if (btn) { btn.disabled = false; btn.innerHTML = '&#x1F310; Research Web'; }
      if (status) status.innerHTML = '<span style="color:#ef4444">Network error</span>';
    });
}

function _updateResearchStatus() {
  fetch('/api/trading/patterns/research/status').then(function(r){return r.json();}).then(function(d) {
    var status = document.getElementById('web-research-status');
    if (!status || !d.ok) return;
    var parts = [];
    if (d.last_research) {
      var ago = Math.round((Date.now()/1000) - new Date(d.last_research + 'Z').getTime()/1000);
      if (ago < 3600) parts.push('Last: ' + Math.round(ago/60) + 'm ago');
      else parts.push('Last: ' + Math.round(ago/3600) + 'h ago');
    }
    parts.push(d.queries_completed + '/' + d.total_queries + ' queries used');
    if (d.cooldown_remaining_s > 0) parts.push('Next in ' + Math.round(d.cooldown_remaining_s/60) + 'm');
    status.textContent = parts.join(' \u2022 ');
  });
}

function _setFilter(type, key, val, chipEl) {
  _scrFilters[type][key] = (key === 'minScore') ? parseFloat(val) || 0 : val;
  if (chipEl && key === 'signal') {
    var parent = chipEl.parentElement;
    parent.querySelectorAll('.scr-chip').forEach(function(c) { c.classList.remove('active'); });
    chipEl.classList.add('active');
  }
  if (type === 'swing') _renderSwingResults();
  else if (type === 'dt') _renderDtResults();
  else if (type === 'bo') _renderBoResults();
}

function _sortScreener(type, field) {
  var s = _scrSort[type];
  if (s.field === field) { s.dir = s.dir === 'desc' ? 'asc' : 'desc'; }
  else { s.field = field; s.dir = 'desc'; }
  if (type === 'swing') _renderSwingResults();
  else if (type === 'dt') _renderDtResults();
  else if (type === 'bo') _renderBoResults();
}

function _filterAndSort(raw, type, signalField) {
  var f = _scrFilters[type];
  var s = _scrSort[type];
  var arr = raw.filter(function(r) {
    if (f.signal !== 'all') {
      var val = r[signalField || 'signal'] || r.status || '';
      if (val.toLowerCase() !== f.signal.toLowerCase()) return false;
    }
    if (f.minScore && r.score < f.minScore) return false;
    if (f.search && r.ticker.toUpperCase().indexOf(f.search.toUpperCase()) === -1) return false;
    return true;
  });
  arr.sort(function(a, b) {
    var va = a[s.field], vb = b[s.field];
    if (typeof va === 'string') { va = va.toLowerCase(); vb = (vb||'').toLowerCase(); }
    if (va < vb) return s.dir === 'asc' ? -1 : 1;
    if (va > vb) return s.dir === 'asc' ? 1 : -1;
    return 0;
  });
  return arr;
}

function _sortHdr(type, field, label) {
  var s = _scrSort[type];
  var cls = s.field === field ? (s.dir === 'asc' ? 'sort-asc' : 'sort-desc') : '';
  return '<span data-sort="'+field+'" class="'+cls+'" onclick="_sortScreener(\''+type+'\',\''+field+'\')">'+label+'</span>';
}

function _scoreBar(score, max) {
  var pct = Math.min(100, (score / (max||10)) * 100);
  var col = score >= 7 ? '#22c55e' : (score >= 4 ? '#f59e0b' : '#6b7280');
  return '<span class="scr-score-bar" style="width:'+pct+'%;background:'+col+'"></span>';
}

function _renderSwingResults() {
  var container = document.getElementById('screen-results');
  var filtered = _filterAndSort(_swingResults, 'swing', 'signal');
  var cnt = document.getElementById('swing-count');
  if (cnt) cnt.textContent = filtered.length + '/' + _swingResults.length;
  if (!filtered.length) { container.innerHTML = '<div style="padding:12px 0;font-size:12px;color:var(--text-muted)">No results match filters.</div>'; return; }
  var topScore = filtered[0].score;
  var html = '<div class="scr-header">'+_sortHdr('swing','ticker','Ticker')+_sortHdr('swing','price','Price')+_sortHdr('swing','score','Score')+'<span>Signal</span>'+_sortHdr('swing','stop_loss','Stop')+_sortHdr('swing','take_profit','Target')+'<span>Conf</span></div>';
  filtered.forEach(function(r) {
    var isCr = r.ticker.indexOf('-USD') !== -1;
    var sigCls = r.signal === 'buy' ? 'color:#22c55e' : (r.signal === 'sell' ? 'color:#ef4444' : 'color:#eab308');
    var confPct = r.confirmations_total > 0 ? Math.round(r.confirmations_met / r.confirmations_total * 100) : 100;
    var confColor = confPct >= 75 ? '#22c55e' : (confPct >= 50 ? '#eab308' : '#ef4444');
    var isTop = r.score === topScore ? ' scr-top' : '';
    var swapBtn = isCr ? ' <button onclick="event.stopPropagation();prefillSwap(\''+r.ticker.replace('-USD','')+'\')" style="font-size:9px;padding:1px 5px;border:1px solid var(--accent);border-radius:4px;background:transparent;color:var(--accent);cursor:pointer;font-weight:700">Swap</button>' : '';
    html += '<div class="scr-row'+isTop+'" onclick="selectTicker(\''+r.ticker+'\')" title="'+escHtml((r.signals||[]).join(', '))+'">' +
      '<span class="tk">'+r.ticker+swapBtn+'</span>' +
      '<span class="price">$'+smartPrice(r.price,isCr)+'</span>' +
      '<span class="score" style="'+sigCls+'">'+r.score+_scoreBar(r.score,10)+'</span>' +
      '<span style="'+sigCls+';font-weight:600;text-transform:uppercase;font-size:10px">'+r.signal+'</span>' +
      '<span class="price">$'+smartPrice(r.stop_loss,isCr)+'</span><span class="price">$'+smartPrice(r.take_profit,isCr)+'</span>' +
      '<span>'+(r.confirmations_total > 0 ? '<div class="conf-bar" style="width:60px"><div class="conf-fill" style="width:'+confPct+'%;background:'+confColor+'"></div></div><span style="font-size:10px;color:var(--text-muted)">'+r.confirmations_met+'/'+r.confirmations_total+'</span>' : '<span style="font-size:10px;color:var(--text-muted)">&#x2705;</span>')+'</span></div>';
  });
  container.innerHTML = html;
}

function _renderDtResults() {
  var container = document.getElementById('daytrade-results');
  var filtered = _filterAndSort(_dtResults, 'dt', 'signal');
  var cnt = document.getElementById('dt-count');
  if (cnt) cnt.textContent = filtered.length + '/' + _dtResults.length;
  if (!filtered.length) { container.innerHTML = '<div style="padding:12px 0;font-size:12px;color:var(--text-muted)">No results match filters.</div>'; return; }
  var topScore = filtered[0].score;
  var html = '<div class="scr-dt-header">'+_sortHdr('dt','ticker','Ticker')+_sortHdr('dt','price','Price')+_sortHdr('dt','score','Score')+'<span>Signal</span>'+_sortHdr('dt','vwap_pct','VWAP%')+_sortHdr('dt','vol_ratio','Vol')+_sortHdr('dt','gap_pct','Gap')+'<span>Signals</span></div>';
  filtered.forEach(function(r, idx) {
    var isCr = r.ticker.indexOf('-USD') !== -1;
    var sigCls = r.signal === 'long' ? 'color:#22c55e' : (r.signal === 'short' ? 'color:#ef4444' : 'color:#eab308');
    var vwapCls = r.vwap_pct > 0 ? 'color:#22c55e' : 'color:#ef4444';
    var volCls = r.vol_ratio >= 2 ? 'color:#f59e0b;font-weight:700' : 'color:var(--text)';
    var isTop = r.score === topScore ? ' scr-top' : '';
    var origIdx = _dtResults.indexOf(r);
    var swapBtn = isCr ? ' <button onclick="event.stopPropagation();prefillSwap(\''+r.ticker.replace('-USD','')+'\')" style="font-size:9px;padding:1px 5px;border:1px solid var(--accent);border-radius:4px;background:transparent;color:var(--accent);cursor:pointer;font-weight:700">Swap</button>' : '';
    html += '<div class="scr-dt-row'+isTop+'" onclick="_selectDaytrade('+origIdx+')" title="'+escHtml((r.signals||[]).join(', '))+'">' +
      '<span class="tk">'+r.ticker+swapBtn+'</span>' +
      '<span class="price" style="font-family:monospace">$'+smartPrice(r.price,isCr)+'</span>' +
      '<span style="font-weight:700;'+sigCls+'">'+r.score+_scoreBar(r.score,10)+'</span>' +
      '<span style="'+sigCls+';font-weight:600;text-transform:uppercase;font-size:10px">'+r.signal+'</span>' +
      '<span style="'+vwapCls+';font-size:10px">'+(r.vwap_pct > 0 ? '+' : '')+r.vwap_pct+'%</span>' +
      '<span style="'+volCls+';font-size:10px">'+r.vol_ratio+'x</span>' +
      '<span style="font-size:10px">'+(r.gap_pct ? (r.gap_pct > 0 ? '+':'')+r.gap_pct+'%' : '-')+'</span>' +
      '<span style="font-size:10px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+escHtml((r.signals||[]).slice(0,2).join('; '))+'</span></div>';
  });
  container.innerHTML = html;
}

function _renderBoResults() {
  var container = document.getElementById('breakout-results');
  var patternFiltered = _applyPatternFilters(_boResults);
  var filtered = _filterAndSort(patternFiltered, 'bo', 'status');
  var cnt = document.getElementById('bo-count');
  var patCount = Object.keys(_activePatterns).length;
  if (cnt) cnt.textContent = filtered.length + '/' + _boResults.length + (patCount ? ' (pattern applied)' : '');
  if (!filtered.length) { container.innerHTML = '<div style="padding:12px 0;font-size:12px;color:var(--text-muted)">No results match filters.</div>'; return; }
  var topScore = filtered[0].score;
  var html = '<div class="scr-bo-header">'+_sortHdr('bo','ticker','Ticker')+_sortHdr('bo','price','Price')+_sortHdr('bo','score','Score')+'<span>Status</span>'+_sortHdr('bo','dist_to_breakout','Dist%')+_sortHdr('bo','resistance','Resist')+'<span>Sqz</span><span>Signals</span></div>';
  filtered.forEach(function(r) {
    var isCr = r.ticker.indexOf('-USD') !== -1;
    var statusCls = r.status === 'breaking_out' ? 'breaking' : r.status;
    var statusLabel = r.status === 'breaking_out' ? 'BREAKOUT!' : (r.status||'').toUpperCase();
    var distCls = r.dist_to_breakout <= 1 ? 'color:#22c55e;font-weight:700' : (r.dist_to_breakout <= 3 ? 'color:#f59e0b' : 'color:var(--text-muted)');
    var isTop = r.score === topScore ? ' scr-top' : '';
    var origIdx = _boResults.indexOf(r);
    var swapBtn = isCr ? ' <button onclick="event.stopPropagation();prefillSwap(\''+r.ticker.replace('-USD','')+'\')" style="font-size:9px;padding:1px 5px;border:1px solid var(--accent);border-radius:4px;background:transparent;color:var(--accent);cursor:pointer;font-weight:700">Swap</button>' : '';
    html += '<div class="scr-bo-row signal-'+statusCls+isTop+'" onclick="_selectBreakout('+origIdx+')" title="'+escHtml((r.signals||[]).join(', '))+'">' +
      '<span class="tk">'+r.ticker+swapBtn+'</span>' +
      '<span class="price" style="font-family:monospace">$'+smartPrice(r.price,isCr)+'</span>' +
      '<span style="font-weight:700;color:'+(r.score>=7?'#22c55e':(r.score>=5?'#f59e0b':'#6b7280'))+'">'+r.score+_scoreBar(r.score,10)+'</span>' +
      '<span><span class="status-badge '+statusCls+'">'+statusLabel+'</span></span>' +
      '<span style="'+distCls+';font-size:10px">'+r.dist_to_breakout+'%</span>' +
      '<span style="font-family:monospace;font-size:10px">$'+smartPrice(r.resistance,isCr)+'</span>' +
      '<span style="'+(r.bb_squeeze?'color:#f59e0b;font-weight:700':'color:var(--text-muted)')+';font-size:10px">'+(r.bb_squeeze?'YES':'no')+'</span>' +
      '<span style="font-size:10px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+escHtml((r.signals||[]).slice(0,2).join('; '))+'</span></div>';
  });
  container.innerHTML = html;
}

function _brainTag(brain) {
  if (!brain) return '';
  var parts = [];
  if (brain.brain_adjusted_weights > 0) parts.push(brain.brain_adjusted_weights + ' weights tuned');
  var thr = brain.immaculate_thresholds;
  if (thr) parts.push('imm\u2265' + thr.min_score + '/' + thr.min_vol + 'x/' + thr.min_rr + 'R');
  return parts.length ? '<span class="scr-brain-tag" title="CHILI Brain: ' + escHtml(parts.join(', ')) + '">&#x1F9E0; Brain</span>' : '';
}
function _candidateLabel(d) {
  var scanned = d.candidates_scanned || 0;
  var total = d.total_sourced || 0;
  if (total && total > scanned) return scanned.toLocaleString() + ' of ' + total.toLocaleString() + ' sourced';
  return scanned ? scanned.toLocaleString() + ' sourced' : 'cached';
}

function runScreener() {
  var sid = document.getElementById('screen-preset').value;
  if (!sid) { alert('Select a screening pattern first'); return; }
  var btn = document.getElementById('btn-screen');
  var status = document.getElementById('screen-status');
  var results = document.getElementById('screen-results');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  status.textContent = 'Scanning thousands of stocks + crypto...';
  _injectSkeletons(results, 5, '80px 60px 45px 50px 60px 60px 1fr');

  fetch('/api/trading/screener/run', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ screen_id: sid })
  }).then(function(r){return r.json();}).then(function(d) {
    btn.disabled = false;
    btn.textContent = 'Scan Market';
    if (!d.ok) { results.innerHTML = '<em style="color:#ef4444">'+escHtml(d.error||'Screener error')+'</em>'; status.textContent = ''; return; }
    var brainTag = _brainTag(d.brain);
    status.innerHTML = escHtml(d.screen_name + ': ' + d.matches + ' matches out of ' + d.total_scanned.toLocaleString() + ' scanned') + brainTag;
    _cachedScanTickers = (d.results || []).map(function(r) { return r.ticker; });
    _swingResults = d.results || [];
    document.getElementById('swing-filter-bar').style.display = _swingResults.length ? 'flex' : 'none';
    _renderSwingResults();
    _attachScoreTooltips();
  }).catch(function() {
    btn.disabled = false;
    btn.textContent = 'Scan Market';
    status.textContent = 'Error running screener';
    results.innerHTML = '<em style="color:#ef4444">Network error</em>';
  });
}

var _screenerLoaded = { swing: false, daytrade: false, breakouts: false, momentum: false, patterns: false };

function _autoLoadScreeners() {
  runDaytradeScan();
  _screenerLoaded.daytrade = true;
  _screenerLoaded.breakouts = true;
  setTimeout(function() { runMomentumScan(); _screenerLoaded.momentum = true; }, 400);
}

function switchScreenType(btn, stype) {
  document.querySelectorAll('.screen-type-btn').forEach(function(b){ b.classList.remove('active'); });
  btn.classList.add('active');
  document.querySelectorAll('.screen-panel').forEach(function(p){ p.style.display = 'none'; });
  var panel = document.getElementById('screen-panel-' + stype);
  if (panel) panel.style.display = 'block';
  if (stype === 'daytrade' && !_screenerLoaded.daytrade) { runDaytradeScan(); _screenerLoaded.daytrade = true; }
  if (stype === 'breakouts' && !_screenerLoaded.breakouts) { runBreakoutScan(); _screenerLoaded.breakouts = true; }
  if (stype === 'patterns' && !_screenerLoaded.patterns) { _loadPatterns(); _screenerLoaded.patterns = true; }
  if (stype === 'momentum' && !_screenerLoaded.momentum) { runMomentumScan(); _screenerLoaded.momentum = true; }
}

var _scanProgressInterval = null;
function _startScanProgressPoll(statusEl) {
  _stopScanProgressPoll();
  _scanProgressInterval = setInterval(function() {
    fetch('/api/trading/scan/progress').then(function(r){return r.json();}).then(function(p) {
      if (!p.running) return;
      var total = p.passed_filter || p.total_sourced || 0;
      var done = p.scored_so_far || 0;
      var elapsed = p.elapsed_s || 0;
      var pct = total ? Math.round(done / total * 100) : 0;
      statusEl.textContent = 'Scoring ' + done + ' / ' + total + ' (' + pct + '%)  \u2014 ' + elapsed + 's';
    }).catch(function(){});
  }, 600);
}
function _stopScanProgressPoll() {
  if (_scanProgressInterval) { clearInterval(_scanProgressInterval); _scanProgressInterval = null; }
}

var _dtScanRetries = 0;
function runDaytradeScan() {
  if (typeof _cachedScanTickers === 'undefined') _cachedScanTickers = [];
  var btn = document.getElementById('btn-daytrade');
  var status = document.getElementById('daytrade-status');
  var results = document.getElementById('daytrade-results');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  status.textContent = 'Scanning full market for momentum plays...';
  _injectSkeletons(results, 5, '70px 58px 42px 50px 55px 55px 50px 1fr');
  _stopScanProgressPoll();
  _startScanProgressPoll(status);

  fetch('/api/trading/scan/daytrade', {method:'POST'})
    .then(function(r){return r.json();}).then(function(d) {
    _dtScanRetries = 0;
    if (d.warming_up) {
      status.textContent = 'Brain is computing day-trade data — refreshing in 5s...';
      results.innerHTML = '';
      setTimeout(function(){ runDaytradeScan(); }, 5000);
      return;
    }
    _stopScanProgressPoll();
    btn.disabled = false;
    btn.textContent = 'Scan Day Trades';
    if (!d.ok) { results.innerHTML = '<em style="color:#ef4444">'+(d.error||'Scan error')+'</em>'; status.textContent = ''; return; }
    var brainTag = _brainTag(d.brain);
    var elapsed = d.elapsed_s ? ' (' + d.elapsed_s + 's)' : (d.cached ? ' (cached)' : '');
    status.innerHTML = escHtml(d.matches + ' setups from ' + _candidateLabel(d) + elapsed) + brainTag;
    var dtTickers = (d.results || []).map(function(r) { return r.ticker; });
    _cachedScanTickers = _cachedScanTickers.concat(dtTickers.filter(function(t) { return _cachedScanTickers.indexOf(t) === -1; }));
    _dtResults = d.results || [];
    _lastDaytradeData = _dtResults;
    document.getElementById('dt-filter-bar').style.display = _dtResults.length ? 'flex' : 'none';
    _renderDtResults();
    _attachScoreTooltips();
  }).catch(function(err) {
    console.error('[runDaytradeScan] error:', err);
    _stopScanProgressPoll();
    if (_dtScanRetries < 2 && (err.message || '').indexOf('NetworkError') !== -1) {
      _dtScanRetries++;
      status.textContent = 'Network error, retrying in 2s...';
      setTimeout(function(){ runDaytradeScan(); }, 2000);
      return;
    }
    _dtScanRetries = 0;
    btn.disabled = false;
    btn.textContent = 'Scan Day Trades';
    status.textContent = '';
    results.innerHTML = '<em style="color:#ef4444">' + escHtml(err && err.message ? err.message : String(err)) + '</em>';
  });
}

var _boScanRetries = 0;
function runBreakoutScan() {
  if (typeof _cachedScanTickers === 'undefined') _cachedScanTickers = [];
  var btn = document.getElementById('btn-breakout');
  var status = document.getElementById('breakout-status');
  var results = document.getElementById('breakout-results');
  _boScanRetries = 0;
  _stopScanProgressPoll();
  btn.disabled = false;
  btn.textContent = 'Open Monitor';
  status.textContent = 'Breakout candidates now flow through Pattern Monitor.';
  document.getElementById('bo-filter-bar').style.display = 'none';
  document.getElementById('bo-pattern-bar').style.display = 'none';
  results.innerHTML =
    '<div class="scr-empty">' +
    '<button class="sig-btn sig-btn-primary" onclick="document.querySelector(&quot;.t-tab[data-tab=monitor]&quot;).click()">Open Pattern Monitor</button>' +
    '</div>';
  return;
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  status.textContent = 'Finding consolidation and squeeze patterns...';
  _injectSkeletons(results, 5, '70px 58px 42px 60px 55px 55px 50px 1fr');
  _stopScanProgressPoll();
  _startScanProgressPoll(status);

  fetch('/api/trading/scan/breakouts', {method:'POST'})
    .then(function(r){return r.json();}).then(function(d) {
    _boScanRetries = 0;
    if (d.warming_up) {
      status.textContent = 'Brain is computing breakout data — refreshing in 5s...';
      results.innerHTML = '';
      setTimeout(function(){ runBreakoutScan(); }, 5000);
      return;
    }
    _stopScanProgressPoll();
    btn.disabled = false;
    btn.textContent = 'Scan Breakouts';
    if (!d.ok) { results.innerHTML = '<em style="color:#ef4444">'+(d.error||'Scan error')+'</em>'; status.textContent = ''; return; }
    var brainTag = _brainTag(d.brain);
    var elapsed = d.elapsed_s ? ' (' + d.elapsed_s + 's)' : (d.cached ? ' (cached)' : '');
    status.innerHTML = escHtml(d.matches + ' setups from ' + _candidateLabel(d) + elapsed) + brainTag;
    var boTickers = (d.results || []).map(function(r) { return r.ticker; });
    _cachedScanTickers = _cachedScanTickers.concat(boTickers.filter(function(t) { return _cachedScanTickers.indexOf(t) === -1; }));
    _boResults = d.results || [];
    _lastBreakoutData = _boResults;
    document.getElementById('bo-filter-bar').style.display = _boResults.length ? 'flex' : 'none';
    document.getElementById('bo-pattern-bar').style.display = _boResults.length ? 'flex' : 'none';
    _renderBoResults();
    _attachScoreTooltips();

    var hasCrypto = _boResults.some(function(r) { return r.ticker.indexOf('-USD') !== -1; });
    if (!hasCrypto && _boResults.length) {
      status.innerHTML += ' <span style="color:#f59e0b;font-size:10px">(crypto scan in progress...)</span>';
      setTimeout(function() {
        fetch('/api/trading/scan/breakouts', {method:'POST'})
          .then(function(r2){return r2.json();}).then(function(d2) {
            if (!d2.ok || !d2.results) return;
            _boResults = d2.results || [];
            _lastBreakoutData = _boResults;
            _renderBoResults();
            var newHasCrypto = _boResults.some(function(r) { return r.ticker.indexOf('-USD') !== -1; });
            var tag2 = _brainTag(d2.brain);
            status.innerHTML = escHtml(d2.matches + ' setups from ' + _candidateLabel(d2)) + tag2;
            if (!newHasCrypto) {
              status.innerHTML += ' <span style="color:#f59e0b;font-size:10px">(crypto still loading...)</span>';
            }
          });
      }, 8000);
    }
  }).catch(function(err) {
    console.error('[runBreakoutScan] error:', err);
    _stopScanProgressPoll();
    if (_boScanRetries < 2 && (err.message || '').indexOf('NetworkError') !== -1) {
      _boScanRetries++;
      status.textContent = 'Network error, retrying in 2s...';
      setTimeout(function(){ runBreakoutScan(); }, 2000);
      return;
    }
    _boScanRetries = 0;
    btn.disabled = false;
    btn.textContent = 'Scan Breakouts';
    status.textContent = '';
    results.innerHTML = '<em style="color:#ef4444">' + escHtml(err && err.message ? err.message : String(err)) + '</em>';
  });
}

/* ── Momentum Scanner ──────────────────────────── */
function runMomentumScan() {
  var btn = document.getElementById('btn-momentum');
  var status = document.getElementById('momentum-status');
  var results = document.getElementById('momentum-results');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  status.textContent = 'Searching for A+ momentum setups...';
  _injectSkeletons(results, 3, '70px 58px 42px 60px 55px 1fr');
  _startScanProgressPoll(status);

  fetch('/api/trading/scan/momentum')
    .then(function(r){return r.json();}).then(function(d) {
    _stopScanProgressPoll();
    btn.disabled = false;
    btn.textContent = 'Scan Momentum';
    if (!d.ok) { results.innerHTML = '<em style="color:#ef4444">'+(d.error||'Scan error')+'</em>'; status.textContent = ''; return; }
    var ic = d.immaculate_count || 0;
    var brainTag = _brainTag(d.brain);
    var txt = d.matches + ' setups' + (ic ? ' (' + ic + ' immaculate)' : '') + ' from ' + _candidateLabel(d) + (d.elapsed_s ? ' (' + d.elapsed_s + 's)' : '') + (d.cached ? ' [cached]' : '');
    status.innerHTML = escHtml(txt) + brainTag;
    _renderMomentumResults(d.results || []);
  }).catch(function() {
    _stopScanProgressPoll();
    btn.disabled = false;
    btn.textContent = 'Scan Momentum';
    status.textContent = 'Error';
    results.innerHTML = '<em style="color:#ef4444">Network error</em>';
  });
}

function _renderMomentumResults(data) {
  var container = document.getElementById('momentum-results');
  if (!data.length) {
    container.innerHTML = '<div class="scr-empty">No A+ setups found right now. Check again during market hours (9:30-11 AM ET).</div>';
    return;
  }
  var html = '<div style="display:grid;grid-template-columns:70px 58px 50px 60px 55px 55px 1fr;gap:4px;padding:4px 6px;font-size:10px;color:var(--text-dim);font-weight:600">' +
    '<span>Ticker</span><span>Price</span><span>Score</span><span>Vol</span><span>R:R</span><span>Signal</span><span>Key Signals</span></div>';
  data.forEach(function(r, i) {
    var badge = r.immaculate ? '<span class="momentum-badge immaculate">IMMACULATE</span>' : '<span class="momentum-badge good">GOOD</span>';
    var scoreColor = r.score >= 8.5 ? '#ef4444' : r.score >= 7 ? '#f59e0b' : '#10b981';
    var signals = (r.signals || []).slice(0, 3).join(' · ');
    html += '<div class="scr-dt-row" onclick="selectTicker(\'' + escHtml(r.ticker) + '\')" title="' + escHtml((r.signals||[]).join(', ')) + '" style="border-left:3px solid ' + scoreColor + '">' +
      '<span style="font-weight:700">' + escHtml(r.ticker) + badge + '</span>' +
      '<span>$' + r.price + '</span>' +
      '<span class="score" style="color:' + scoreColor + ';font-weight:700">' + r.score + '</span>' +
      '<span>' + (r.vol_ratio||0).toFixed(1) + 'x</span>' +
      '<span>' + (r.risk_reward||0).toFixed(1) + ':1</span>' +
      '<span style="color:' + (r.signal==='long'?'#10b981':'#ef4444') + '">' + (r.signal||'').toUpperCase() + '</span>' +
      '<span style="font-size:10px;color:var(--text-dim)">' + escHtml(signals) + '</span>' +
      '</div>';
  });
  container.innerHTML = html;
  _attachScoreTooltips();
}

/* ── Score tooltip on hover ─────────────────────── */
var _activeScoreTip = null;
function _attachScoreTooltips() {
  document.querySelectorAll('.scr-row[title], .scr-dt-row[title], .scr-bo-row[title]').forEach(function(row) {
    var scoreEl = row.querySelector('.score') || row.children[2];
    if (!scoreEl || scoreEl.dataset.tipBound) return;
    scoreEl.dataset.tipBound = '1';
    scoreEl.style.position = 'relative';
    scoreEl.style.cursor = 'help';
    scoreEl.addEventListener('mouseenter', function() {
      var signals = (row.getAttribute('title') || '').split(', ').filter(Boolean);
      if (!signals.length) return;
      var tip = document.createElement('div');
      tip.className = 'score-tip';
      tip.innerHTML = signals.map(function(s){ return '<div class="st-row"><span>'+escHtml(s)+'</span></div>'; }).join('');
      scoreEl.appendChild(tip);
      _activeScoreTip = tip;
    });
    scoreEl.addEventListener('mouseleave', function() {
      if (_activeScoreTip) { _activeScoreTip.remove(); _activeScoreTip = null; }
    });
  });
}

function _selectBreakout(idx) {
  if (!_lastBreakoutData || !_lastBreakoutData[idx]) return;
  var r = _lastBreakoutData[idx];
  var isCrypto = r.ticker.indexOf('-USD') !== -1;
  var targetInterval = isCrypto ? '15m' : '1d';
  _pendingAnnotationFn = function() { drawBreakoutAnnotations(r); };
  currentTicker = r.ticker.toUpperCase();
  _activeBreakoutRow = r;
  clearAnnotations();
  _loadDrawings();
  document.querySelectorAll('.wl-item').forEach(function(el) { el.classList.toggle('active', el.dataset.ticker === currentTicker); });
  document.getElementById('ticker-label').textContent = currentTicker;
  if (_selectDebounce) clearTimeout(_selectDebounce);
  changeInterval(targetInterval);
}

function _selectDaytrade(idx) {
  if (!_lastDaytradeData || !_lastDaytradeData[idx]) return;
  var r = _lastDaytradeData[idx];
  _pendingAnnotationFn = function() { drawDaytradeAnnotations(r); };
  currentTicker = r.ticker.toUpperCase();
  _activeBreakoutRow = null;
  clearAnnotations();
  _loadDrawings();
  document.querySelectorAll('.wl-item').forEach(function(el) { el.classList.toggle('active', el.dataset.ticker === currentTicker); });
  document.getElementById('ticker-label').textContent = currentTicker;
  if (_selectDebounce) clearTimeout(_selectDebounce);
  changeInterval('15m');
}

/* ── Tab switching and draggable order ──────────── */
var _tabDragging = false;
var _tabDragSrc = null;

function handleTabClick(el) {
  if (_tabDragging) return;
  switchTab(el);
}

function tabDragStart(ev) {
  _tabDragging = true;
  _tabDragSrc = ev.target;
  ev.dataTransfer.effectAllowed = 'move';
  ev.dataTransfer.setData('text/plain', ev.target.dataset.tab);
  ev.target.classList.add('t-tab-dragging');
}

function tabDragOver(ev) {
  ev.preventDefault();
  ev.dataTransfer.dropEffect = 'move';
  if (ev.target.classList.contains('t-tab') && ev.target !== _tabDragSrc) {
    ev.target.classList.add('t-tab-drop-target');
  }
}

function tabDrop(ev) {
  ev.preventDefault();
  document.querySelectorAll('.t-tab').forEach(function(t){
    t.classList.remove('t-tab-drop-target');
  });
  var tabName = ev.dataTransfer.getData('text/plain');
  var target = ev.target.closest('.t-tab');
  if (!target || !tabName) return;
  var tabs = document.getElementById('t-tabs');
  var src = tabs.querySelector('.t-tab[data-tab="'+tabName+'"]');
  if (!src || src === target) return;
  if (target.nextSibling) {
    tabs.insertBefore(src, target);
  } else {
    tabs.appendChild(src);
  }
  _saveTabOrder();
}

function tabDragEnd(ev) {
  _tabDragging = false;
  _tabDragSrc = null;
  ev.target.classList.remove('t-tab-dragging');
  document.querySelectorAll('.t-tab').forEach(function(t){
    t.classList.remove('t-tab-drop-target');
  });
}

function _saveTabOrder() {
  var tabs = document.querySelectorAll('#t-tabs .t-tab');
  var order = Array.prototype.map.call(tabs, function(t){ return t.dataset.tab; });
  localStorage.setItem('chili_tab_order', JSON.stringify(order));
}

function initTabOrder() {
  var saved = localStorage.getItem('chili_tab_order');
  if (!saved) return;
  try {
    var order = JSON.parse(saved);
    var tabs = document.getElementById('t-tabs');
    if (!tabs || !order.length) return;
    var byTab = {};
    Array.prototype.forEach.call(tabs.querySelectorAll('.t-tab'), function(t){
      byTab[t.dataset.tab] = t;
    });
    order.forEach(function(name){
      if (byTab[name]) tabs.appendChild(byTab[name]);
    });
  } catch (e) { /**/ }
}

function switchTab(el) {
  var tabName = el.dataset.tab;
  document.querySelectorAll('.t-tab').forEach(function(t){t.classList.remove('active');});
  document.querySelectorAll('.t-tab-content').forEach(function(t){t.classList.remove('active');});
  el.classList.add('active');
  var content = document.getElementById('tab-'+tabName);
  if (content) content.classList.add('active');
  if (tabName === 'screener' && !document.getElementById('screen-preset').options.length > 1) {
    loadScreenerPresets();
  }
  if (tabName === 'alerts') {
    loadStopPositions();
  }
  if (tabName === 'monitor') {
    loadMonitorData(false);
    _startMonitorAutoRefresh();
  } else {
    _stopMonitorAutoRefresh();
  }
}

function switchAiView(view) {
  document.querySelectorAll('.ai-view-btn').forEach(function(b) { b.classList.toggle('active', b.dataset.view === view); });
  document.querySelectorAll('.ai-view').forEach(function(v) { v.classList.remove('active'); });
  var target = document.getElementById('ai-view-' + view);
  if (target) target.classList.add('active');
}

function switchTabByName(name) {
  var tab = document.querySelector('.t-tab[data-tab="'+name+'"]');
  if (tab) switchTab(tab);
}

window.handleTabClick = handleTabClick;
window.tabDragStart = tabDragStart;
window.tabDragOver = tabDragOver;
window.tabDrop = tabDrop;
window.tabDragEnd = tabDragEnd;
window.switchTab = switchTab;
window.switchTabByName = switchTabByName;

/* ── Resize handle for bottom panel ────────────── */
(function initResizeHandle() {
  var handle = document.getElementById('resize-handle');
  if (!handle) return;
  var bottom = document.querySelector('.t-bottom');
  var mainEl = document.querySelector('.t-main');
  if (!bottom || !mainEl) return;

  var savedH = localStorage.getItem('chili_bottom_h');
  if (savedH) {
    var h = parseInt(savedH, 10);
    if (h >= 140 && h <= 600) bottom.style.height = h + 'px';
  }

  var dragging = false;
  var startY = 0;
  var startH = 0;

  handle.addEventListener('mousedown', function(e) {
    e.preventDefault();
    dragging = true;
    startY = e.clientY;
    startH = bottom.offsetHeight;
    handle.classList.add('dragging');
    document.body.style.cursor = 'row-resize';
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    var delta = startY - e.clientY;
    var maxH = Math.floor(mainEl.offsetHeight * 0.7);
    var newH = Math.max(140, Math.min(maxH, startH + delta));
    bottom.style.height = newH + 'px';
  });

  document.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    localStorage.setItem('chili_bottom_h', bottom.offsetHeight);
    if (typeof chart !== 'undefined' && chart) { var el = document.getElementById('main-chart'); chart.applyOptions({width: el.clientWidth, height: el.clientHeight}); }
    window.dispatchEvent(new Event('resize'));
  });

  handle.addEventListener('touchstart', function(e) {
    var touch = e.touches[0];
    startY = touch.clientY;
    startH = bottom.offsetHeight;
    dragging = true;
    handle.classList.add('dragging');
  }, {passive: true});

  document.addEventListener('touchmove', function(e) {
    if (!dragging) return;
    var touch = e.touches[0];
    var delta = startY - touch.clientY;
    var maxH = Math.floor(mainEl.offsetHeight * 0.7);
    var newH = Math.max(140, Math.min(maxH, startH + delta));
    bottom.style.height = newH + 'px';
  }, {passive: true});

  document.addEventListener('touchend', function() {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('dragging');
    localStorage.setItem('chili_bottom_h', bottom.offsetHeight);
    window.dispatchEvent(new Event('resize'));
  });
})();

/* ── Signals (Beginner Dashboard) ──────────────── */
function _renderPickCard(p, compact) {
  var isCr = (p.ticker || '').indexOf('-USD') !== -1;
  var badgeCls = p.signal === 'buy' ? 'buy' : (p.signal === 'sell' ? 'sell' : 'hold');
  var riskCls = p.risk_level || 'medium';
  var profPct = p.projected_profit_pct ? (p.projected_profit_pct > 0 ? '+' : '') + p.projected_profit_pct + '%' : '--';
  var profCls = (p.projected_profit_pct || 0) >= 0 ? 'pos' : 'neg';
  var confPct = p.brain_confidence || Math.round((p.combined_score || p.score || 5) * 10);
  var confColor = confPct >= 70 ? '#22c55e' : (confPct >= 50 ? '#f59e0b' : '#ef4444');
  var rrText = p.risk_reward ? p.risk_reward.toFixed(1) + ':1' : '--';
  var swapBtn = isCr ? ' <button onclick="event.stopPropagation();prefillSwap(\''+p.ticker.replace('-USD','')+'\')" style="font-size:9px;padding:1px 5px;border:1px solid var(--accent);border-radius:4px;background:transparent;color:var(--accent);cursor:pointer;font-weight:700">Swap</button>' : '';
  var brainTag = p.brain_confidence ? '<span class="pc-brain-tag">AI Brain</span>' : '';

  var html = '<div class="pick-card" onclick="selectTicker(\''+p.ticker+'\')">';
  if (p.rank) html += '<div class="pc-rank">#'+p.rank+'</div>';
  html += '<div class="pc-header"><span class="pc-ticker">'+p.ticker+swapBtn+'</span><span class="pc-badge '+badgeCls+'">'+p.signal.toUpperCase()+'</span></div>';
  html += '<div class="pc-price">$'+smartPrice(p.price, isCr)+'</div>';
  html += '<div class="pc-levels">' +
    '<div class="pc-lv"><span class="pc-lbl">Entry</span><span class="pc-val entry">$'+smartPrice(p.entry_price, isCr)+'</span></div>' +
    '<div class="pc-lv"><span class="pc-lbl">Stop Loss</span><span class="pc-val stop">$'+smartPrice(p.stop_loss, isCr)+'</span></div>' +
    '<div class="pc-lv"><span class="pc-lbl">Target</span><span class="pc-val target">$'+smartPrice(p.take_profit, isCr)+'</span></div></div>';

  if (!compact) {
    html += '<div class="pc-metrics">' +
      '<div class="pc-metric"><span style="color:var(--text-muted)">Profit</span><span class="pc-mv '+profCls+'">'+profPct+'</span></div>' +
      '<div class="pc-metric"><span style="color:var(--text-muted)">R:R</span><span class="pc-mv" style="color:#3b82f6">'+rrText+'</span></div>' +
      '<div class="pc-metric"><span style="color:var(--text-muted)">Score</span><span class="pc-mv">'+(p.combined_score || p.score)+'/10</span></div></div>';

    if (p.thesis) html += '<div class="pc-thesis">'+escHtml(p.thesis)+'</div>';
  }

  html += '<div class="pc-footer">' +
    '<div style="display:flex;align-items:center;gap:4px">' +
      '<div class="pc-conf-bar"><div class="pc-conf-fill" style="width:'+confPct+'%;background:'+confColor+'"></div></div>' +
      '<span style="font-size:9px;color:var(--text-muted)">'+confPct+'%</span></div>' +
    '<span class="pc-risk '+riskCls+'">'+riskCls.toUpperCase()+'</span>' +
    brainTag + '</div>';

  if (!compact && p.timeframe) {
    html += '<div class="pc-timeframe">&#x23F1; '+p.timeframe+(p.best_strategy ? ' &middot; Best: '+p.best_strategy : '')+'</div>';
  }
  if (!compact && p.scanned_at) {
    var scanDate = new Date(p.scanned_at);
    var scanAgo = _formatRelativeTime(scanDate);
    html += '<div class="pc-scan-meta" style="font-size:9px;color:var(--text-muted);margin-top:2px">from scan: '+scanAgo+'</div>';
  }
  if (!compact) {
    var entryPrice = (p.entry_price || p.price || 0);
    html += '<button class="pc-recheck-btn" onclick="event.stopPropagation();recheckPick(\''+escAttr(p.ticker)+'\','+entryPrice+',this)" title="Revalidate with live price">&#x21BB; Recheck</button>';
  }
  html += '</div>';
  return html;
}
function _formatRelativeTime(d) {
  var sec = Math.floor((Date.now() - d) / 1000);
  if (sec < 60) return 'just now';
  if (sec < 3600) return Math.floor(sec/60)+' min ago';
  if (sec < 86400) return Math.floor(sec/3600)+'h ago';
  return Math.floor(sec/86400)+'d ago';
}
function escAttr(s){ return String(s||'').replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'&quot;'); }
function recheckPick(ticker, entryPrice, btnEl) {
  if (!btnEl || !ticker || entryPrice == null) return;
  btnEl.disabled = true;
  btnEl.textContent = '…';
  fetch('/api/trading/top-picks/recheck', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ticker:ticker,entry_price:entryPrice})})
    .then(function(r){return r.json();})
    .then(function(d) {
      btnEl.disabled = false;
      btnEl.textContent = 'Recheck';
      if (d.ok && d.status) {
        var msg = d.status === 'valid' ? 'Valid' : (d.status === 'moved_but_ok' ? 'Moved '+(d.drift_pct||0)+'%' : 'Invalidated');
        btnEl.title = d.live_price ? 'Now $'+d.live_price+' ('+msg+')' : (d.message || msg);
      }
    })
    .catch(function(){ btnEl.disabled = false; btnEl.textContent = 'Recheck'; });
}

function _fetchJsonWithTimeout(url, fallback, timeoutMs) {
  var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
  var timer = setTimeout(function() {
    if (controller) controller.abort();
  }, timeoutMs || 5000);
  var opts = controller ? { signal: controller.signal } : {};
  return fetch(url, opts)
    .then(function(r) { return r.json(); })
    .catch(function() { return fallback; })
    .then(function(d) {
      clearTimeout(timer);
      return d;
    });
}

function loadBestIdeas() {
  var status = document.getElementById('picks-status');
  if (status) status.textContent = 'Loading…';
  Promise.all([
    _fetchJsonWithTimeout('/api/trading/top-picks', {ok: true, picks: [], is_stale: true, timed_out: true}, 5000),
    _fetchJsonWithTimeout('/api/trading/proposals', {ok: true, proposals: []}, 5000)
  ]).then(function(results) {
    if (status) status.textContent = '';
    var picksData = results[0];
    var propsData = results[1];
    var picks = (picksData.ok && picksData.picks) ? picksData.picks : [];
    var proposals = (propsData.ok && propsData.proposals) ? propsData.proposals : [];

    var activeProposals = {};
    proposals.forEach(function(p) {
      if (['pending','approved','working'].indexOf(p.status) !== -1) {
        if (!activeProposals[p.ticker] || (p.id > activeProposals[p.ticker].id)) {
          activeProposals[p.ticker] = p;
        }
      }
    });

    var merged = [];
    var seen = {};
    picks.forEach(function(pk) {
      var t = pk.ticker;
      seen[t] = true;
      if (activeProposals[t]) {
        merged.push({type:'proposal', proposal: activeProposals[t], pick: pk});
      } else {
        merged.push({type:'pick', pick: pk});
      }
    });
    Object.keys(activeProposals).forEach(function(t) {
      if (!seen[t]) merged.push({type:'proposal', proposal: activeProposals[t], pick: null});
    });

    merged.sort(function(a, b) {
      var sa = a.proposal ? (a.proposal.confidence || 0) : (a.pick ? ((a.pick.brain_confidence || (a.pick.combined_score || 0) * 10)) : 0);
      var sb = b.proposal ? (b.proposal.confidence || 0) : (b.pick ? ((b.pick.brain_confidence || (b.pick.combined_score || 0) * 10)) : 0);
      return sb - sa;
    });

    var listEl = document.getElementById('best-ideas-list');
    var header = document.getElementById('best-ideas-header');
    var countEl = document.getElementById('best-ideas-count');
    var freshness = document.getElementById('best-ideas-freshness');

    if (!merged.length) {
      if (header) header.style.display = 'none';
      listEl.innerHTML = '<div class="picks-empty">' +
        '<div class="pe-icon">&#x1F50D;</div>' +
        '<div class="pe-title">No Ideas Yet</div>' +
        '<div class="pe-sub">Run a Full Market Scan to generate AI-driven trade ideas with entry, exit, risk, and projected returns.</div></div>';
    } else {
      if (header) header.style.display = 'flex';
      if (countEl) countEl.textContent = merged.length + ' ideas';
      if (freshness && picksData.age_seconds != null) {
        var ageSec = picksData.age_seconds || 0;
        var ageMin = Math.floor(ageSec / 60);
        var ageStr = ageMin < 1 ? 'just now' : (ageMin === 1 ? '1 min ago' : ageMin + ' min ago');
        var asOf = picksData.as_of ? new Date(picksData.as_of).toLocaleTimeString(undefined, {hour:'2-digit',minute:'2-digit'}) : '';
        freshness.textContent = asOf ? 'As of ' + asOf + ' (' + ageStr + ')' : ageStr;
        freshness.className = 'picks-freshness' + (picksData.is_stale ? ' picks-stale' : '');
      }
      listEl.innerHTML = '';
      merged.forEach(function(item) { listEl.innerHTML += _renderBestIdeaRow(item); });
    }

    loadAlertHistory();
    loadAlertSettings();
    loadStopPositions();
  }).catch(function() {
    if (status) status.textContent = '';
    var listEl = document.getElementById('best-ideas-list');
    if (listEl) listEl.innerHTML = '<div class="picks-empty">' +
      '<div class="pe-icon">&#x26A0;</div>' +
      '<div class="pe-title">Could not load ideas</div>' +
      '<div class="pe-sub">The server may still be computing predictions. Try refreshing in a moment.</div></div>';
  });
}

function _renderEvidencePills(signals, indicators) {
  var pills = [];
  var ind = indicators || {};
  var sigs = signals || [];

  if (ind.rsi != null) {
    var rsiV = Math.round(ind.rsi);
    var cls = rsiV < 35 ? 'bullish' : (rsiV > 70 ? 'bearish' : 'neutral');
    var arrow = rsiV < 35 ? '&#x2197;' : (rsiV > 70 ? '&#x2198;' : '');
    pills.push('<span class="bi-pill ' + cls + '">RSI ' + rsiV + ' ' + arrow + '</span>');
  }
  if (ind.macd_hist != null) {
    var mCls = ind.macd_hist > 0 ? 'bullish' : 'bearish';
    var mLabel = ind.macd_hist > 0 ? 'MACD Bullish' : 'MACD Bearish';
    pills.push('<span class="bi-pill ' + mCls + '">' + mLabel + '</span>');
  }
  if (ind.vol_ratio != null && ind.vol_ratio > 1.3) {
    pills.push('<span class="bi-pill bullish">Vol ' + ind.vol_ratio.toFixed(1) + 'x</span>');
  }
  if (ind.adx != null && ind.adx > 20) {
    pills.push('<span class="bi-pill neutral">ADX ' + Math.round(ind.adx) + '</span>');
  }
  if (ind.bb_pct != null) {
    if (ind.bb_pct < 0.2) pills.push('<span class="bi-pill bullish">BB Low</span>');
    else if (ind.bb_pct > 0.8) pills.push('<span class="bi-pill bearish">BB High</span>');
  }
  if (ind.stoch_k != null) {
    if (ind.stoch_k < 20) pills.push('<span class="bi-pill bullish">Stoch Oversold</span>');
    else if (ind.stoch_k > 80) pills.push('<span class="bi-pill bearish">Stoch Overbought</span>');
  }

  var seenLabels = {};
  sigs.forEach(function(s) {
    var sl = s.toLowerCase();
    if (seenLabels[sl]) return;
    seenLabels[sl] = true;
    var cls = 'neutral';
    if (/bullish|oversold|golden|above vwap|gap up|breakout|volume surge/i.test(sl)) cls = 'bullish';
    else if (/bearish|overbought|death|below vwap/i.test(sl)) cls = 'bearish';
    var short = s.length > 24 ? s.substring(0, 22) + '...' : s;
    pills.push('<span class="bi-pill ' + cls + '">' + escHtml(short) + '</span>');
  });

  return pills.slice(0, 8).join('');
}

function _renderBestIdeaRow(item) {
  var isProposal = item.type === 'proposal';
  var p = isProposal ? item.proposal : null;
  var pk = item.pick || {};
  var ticker = isProposal ? p.ticker : pk.ticker;
  var isCr = (ticker || '').indexOf('-USD') !== -1;

  var price = isProposal ? p.entry_price : (pk.price || pk.entry_price || 0);
  var stop = isProposal ? p.stop_loss : (pk.stop_loss || pk.brain_stop || 0);
  var target = isProposal ? p.take_profit : (pk.take_profit || pk.brain_target || 0);
  var rr = isProposal ? p.risk_reward_ratio : (pk.risk_reward || 0);
  var conf = isProposal ? (p.confidence || 0) : (pk.brain_confidence || Math.round((pk.combined_score || pk.score || 5) * 10));
  var confColor = conf >= 70 ? '#22c55e' : (conf >= 50 ? '#f59e0b' : '#ef4444');
  var profitPct = isProposal ? p.projected_profit_pct : pk.projected_profit_pct;
  var lossPct = isProposal ? p.projected_loss_pct : null;
  var rrText = rr ? rr.toFixed(1) + ':1' : '--';
  var profText = profitPct ? ((profitPct > 0 ? '+' : '') + profitPct.toFixed(1) + '%') : '--';
  var lossText = lossPct ? ('-' + Math.abs(lossPct).toFixed(1) + '%') : '--';
  var direction = isProposal ? (p.direction || 'long') : (pk.signal || 'buy');
  var signals = isProposal ? (p.signals || []) : (pk.signals || []);
  var indicators = isProposal ? (p.indicators || {}) : (pk.indicators || {});
  var btStrategy = pk.best_strategy || null;
  var btReturn = pk.backtest_return;
  var btWinRate = pk.backtest_win_rate;

  var html = '<div class="best-idea-row ' + (isProposal ? 'is-proposal' : 'is-pick') + '" onclick="selectTicker(\'' + ticker + '\')">';

  /* ── Header row ── */
  html += '<div class="bi-top">';
  html += '<div style="display:flex;align-items:center;gap:8px">';
  html += '<span class="bi-ticker">' + ticker + '</span>';
  html += '<span style="font-size:10px;color:var(--text-muted)">' + direction.toUpperCase() + '</span>';
  if (isProposal) {
    html += '<span class="bi-type-badge proposal">Proposal</span>';
    html += '<span class="prop-status ' + (p.status || 'pending') + '">' + (p.status || 'pending') + '</span>';
  } else {
    html += '<span class="bi-type-badge pick">Pick</span>';
  }
  if (isCr) html += ' <button onclick="event.stopPropagation();prefillSwap(\'' + ticker.replace('-USD','') + '\')" style="font-size:9px;padding:1px 5px;border:1px solid var(--accent);border-radius:4px;background:transparent;color:var(--accent);cursor:pointer;font-weight:700">Swap</button>';
  html += '</div>';
  html += '<div class="bi-conf"><span style="font-size:9px;color:var(--text-muted)">Confidence</span>';
  html += '<div class="bi-conf-bar"><div class="bi-conf-fill" style="width:' + Math.round(conf) + '%;background:' + confColor + '"></div></div>';
  html += '<span style="font-size:11px;font-weight:700;color:' + confColor + '">' + Math.round(conf) + '%</span></div>';
  html += '</div>';

  /* ── Row 1: Chili's Take + Levels + Metrics (3-column) ── */
  var thesis = isProposal ? p.thesis : pk.thesis;
  var explanation = pk.explanation || null;

  html += '<div class="bi-data-grid">';

  // Column 1: Chili's Take + plain-English explanation
    html += '<div class="bi-take collapsed" onclick="event.stopPropagation();this.classList.toggle(\'collapsed\')">';
  if (thesis) {
    html += '<span class="bi-take-label">Chili\'s Take</span><br>';
    html += escHtml(thesis);
  }
  if (explanation) {
    html += '<div class="bi-explain">';
    html += '<span class="bi-take-label">In Plain English</span><br>';
    html += escHtml(explanation);
    html += '</div>';
  }
  if (!thesis && !explanation) {
    html += '<span class="bi-take-label">Analysis</span><br>';
    html += '<span style="color:var(--text-muted);font-size:10px">Run a scan to generate insight</span>';
  }
  html += '</div>';

  // Column 2: Entry / Stop / Target levels
  html += '<div class="bi-levels">';
  html += '<div class="bi-lv"><span class="bi-lbl">Entry</span><span class="bi-val entry">$' + smartPrice(price, isCr) + '</span></div>';
  html += '<div class="bi-lv"><span class="bi-lbl">Stop Loss</span><span class="bi-val stop">$' + smartPrice(stop, isCr) + '</span></div>';
  html += '<div class="bi-lv"><span class="bi-lbl">Target</span><span class="bi-val target">$' + smartPrice(target, isCr) + '</span></div>';
  html += '</div>';

  // Column 3: Metrics
  html += '<div class="bi-metrics-col">';
  html += '<div class="bi-metric-row"><span class="bi-metric-lbl">Profit</span><span class="bi-metric-val profit">' + profText + '</span></div>';
  if (isProposal) html += '<div class="bi-metric-row"><span class="bi-metric-lbl">Max Loss</span><span class="bi-metric-val loss">' + lossText + '</span></div>';
  html += '<div class="bi-metric-row"><span class="bi-metric-lbl">R:R</span><span class="bi-metric-val rr">' + rrText + '</span></div>';
  if (isProposal && p.quantity) html += '<div class="bi-metric-row"><span class="bi-metric-lbl">Qty</span><span class="bi-metric-val">' + p.quantity + '</span></div>';
  if (isProposal && p.position_size_pct) html += '<div class="bi-metric-row"><span class="bi-metric-lbl">Position</span><span class="bi-metric-val">' + p.position_size_pct + '%</span></div>';
  if (!isProposal && pk.combined_score) html += '<div class="bi-metric-row"><span class="bi-metric-lbl">Score</span><span class="bi-metric-val">' + (pk.combined_score || pk.score) + '/10</span></div>';
  html += '</div>';

  html += '</div>';

  /* ── Row 2: Backtest strategy cards ── */
  html += '<div class="bi-backtest-row" id="bt-cards-' + ticker + '">';
  if (btStrategy) {
    var retCls = (btReturn != null && btReturn >= 0) ? 'pos' : 'neg';
    html += '<div class="bi-bt-card">';
    html += '<div class="bi-bt-card-name">' + escHtml(btStrategy) + '</div>';
    if (btReturn != null) html += '<div class="bi-bt-card-row"><span class="bi-bt-card-lbl">Return</span><span class="bi-bt-card-val bi-bt-val ' + retCls + '">' + (btReturn >= 0 ? '+' : '') + btReturn.toFixed(1) + '%</span></div>';
    if (btWinRate != null) {
      var _wrDisp = tradingBacktestWinRateToPct(btWinRate);
      if (_wrDisp != null) html += '<div class="bi-bt-card-row"><span class="bi-bt-card-lbl">Win Rate</span><span class="bi-bt-card-val">' + _wrDisp.toFixed(0) + '%</span></div>';
    }
    html += '</div>';
  }
  html += '<div class="bi-bt-card" style="display:flex;align-items:center;justify-content:center;cursor:pointer;border-style:dashed" onclick="event.stopPropagation();runAllBacktests(\'' + escAttr(ticker) + '\',this)">';
  html += '<span style="font-size:11px;color:var(--text-muted)">&#x25B6; Test All Strategies</span>';
  html += '</div>';
  html += '<div class="bi-bt-expanded" id="bt-expand-' + ticker + '" style="display:none"></div>';
  html += '</div>';

  /* ── Evidence pills ── */
  var pillsHtml = _renderEvidencePills(signals, indicators);
  if (pillsHtml) {
    html += '<div class="bi-evidence">' + pillsHtml + '</div>';
  }

  /* ── Footer: actions + meta ── */
  html += '<div class="bi-footer">';

  html += '<div class="bi-actions" onclick="event.stopPropagation()">';
  if (isProposal && p.status === 'pending') {
    html += '<button class="prop-btn-approve" onclick="approveProposal(' + p.id + ', this)">Approve & Execute</button>';
    var _isCrypto = ticker.indexOf('-USD') !== -1;
    if (_isCrypto) html += '<span class="broker-badge cb" title="Will execute via Coinbase or best available">CB</span>';
    else html += '<span class="broker-badge rh" title="Will execute via Robinhood or best available">RH</span>';
    html += '<button class="prop-btn-reject" onclick="rejectProposal(' + p.id + ', this)">Reject</button>';
    html += '<button class="prop-btn-recheck" onclick="recheckProposal(' + p.id + ', this)" title="Revalidate with live price">&#x21BB; Recheck</button>';
  } else if (!isProposal) {
    var entryVal = pk.entry_price || pk.price || 0;
    var stopVal = pk.stop_loss || pk.brain_stop || 0;
    var targetVal = pk.take_profit || pk.brain_target || 0;
    html += '<button class="prop-btn-create-proposal" onclick="createProposalFromPick(\'' + escAttr(ticker) + '\', this, ' + entryVal + ', ' + stopVal + ', ' + targetVal + ')">Create Proposal</button>';
    html += '<button class="prop-btn-recheck" onclick="recheckPick(\'' + escAttr(ticker) + '\',' + entryVal + ',this)" title="Revalidate with live price">&#x21BB; Recheck</button>';
  }
  if (isProposal && p.status === 'executed' && p.broker_order_id) {
    html += '<span style="font-size:10px;color:#22c55e">Order ID: ' + p.broker_order_id + '</span>';
  }
  html += '</div>';

  html += '<div class="bi-meta">';
  if (isProposal) {
    if (p.age_seconds != null) {
      var asec = p.age_seconds;
      var proposedAgo = asec < 60 ? 'just now' : (asec < 3600 ? Math.floor(asec/60) + ' min ago' : Math.floor(asec/3600) + 'h ago');
      html += '<span>Proposed ' + proposedAgo + '</span>';
    }
    if (p.expires_in_seconds != null) {
      var tl = '';
      if (p.expires_in_seconds <= 0) tl = 'Expired';
      else {
        var m = Math.floor(p.expires_in_seconds / 60);
        var h = Math.floor(m / 60);
        m = m % 60;
        tl = h ? h + 'h ' + m + 'm left' : m + 'm left';
      }
      html += '<span class="prop-expiry">' + tl + '</span>';
    }
    if (p.timeframe) html += '<span>' + p.timeframe + '</span>';
    if (p.brain_score) html += '<span>Brain ' + (p.brain_score > 0 ? '+' : '') + p.brain_score.toFixed(1) + '</span>';
    if (p.ml_probability) html += '<span>ML ' + (p.ml_probability * 100).toFixed(0) + '%</span>';
  } else {
    if (pk.timeframe) html += '<span>' + pk.timeframe + '</span>';
    if (pk.risk_level) html += '<span class="pc-risk ' + pk.risk_level + '">' + pk.risk_level.toUpperCase() + '</span>';
    if (pk.scanned_at) html += '<span>Scan: ' + _formatRelativeTime(new Date(pk.scanned_at)) + '</span>';
  }
  html += '</div>';

  html += '</div>';

  html += '</div>';
  return html;
}

function runAllBacktests(ticker, btnEl) {
  if (!ticker) return;
  var container = document.getElementById('bt-cards-' + ticker);
  if (!container) return;
  if (btnEl) { btnEl.innerHTML = '<span style="font-size:11px;color:var(--text-muted)">Running\u2026</span>'; btnEl.style.pointerEvents = 'none'; }

  fetch('/api/trading/backtest/all?ticker=' + encodeURIComponent(ticker))
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d.ok || !d.results) {
        if (btnEl) { btnEl.innerHTML = '<span style="font-size:11px;color:#ef4444">Failed</span>'; }
        return;
      }
      container.innerHTML = '';
      d.results.forEach(function(r) {
        var retCls = r.return_pct >= 0 ? 'pos' : 'neg';
        var card = '<div class="bi-bt-card">';
        card += '<div class="bi-bt-card-name">' + escHtml(r.strategy) + '</div>';
        card += '<div class="bi-bt-card-row"><span class="bi-bt-card-lbl">Return</span><span class="bi-bt-card-val bi-bt-val ' + retCls + '">' + (r.return_pct >= 0 ? '+' : '') + r.return_pct.toFixed(1) + '%</span></div>';
        card += '<div class="bi-bt-card-row"><span class="bi-bt-card-lbl">Win Rate</span><span class="bi-bt-card-val">' + (function(){ var w = tradingBacktestWinRateToPct(r.win_rate); return w != null ? w.toFixed(0) : '--'; })() + '%</span></div>';
        card += '<div class="bi-bt-card-row"><span class="bi-bt-card-lbl">Trades</span><span class="bi-bt-card-val">' + r.trade_count + '</span></div>';
        if (r.sharpe != null) card += '<div class="bi-bt-card-row"><span class="bi-bt-card-lbl">Sharpe</span><span class="bi-bt-card-val">' + r.sharpe.toFixed(2) + '</span></div>';
        card += '</div>';
        container.innerHTML += card;
      });
      var linkCard = '<div class="bi-bt-card" style="display:flex;align-items:center;justify-content:center;cursor:pointer" onclick="event.stopPropagation();_openFullBacktest(\'' + escAttr(ticker) + '\')">';
      linkCard += '<span style="font-size:10px;color:var(--accent);font-weight:600">Full Chart &#x2192;</span>';
      linkCard += '</div>';
      container.innerHTML += linkCard;
    })
    .catch(function() {
      if (btnEl) { btnEl.innerHTML = '<span style="font-size:11px;color:#ef4444">Error</span>'; }
    });
}

function createProposalFromPick(ticker, btn, entryPrice, stopLoss, takeProfit) {
  if (!btn || !ticker) return;
  btn.disabled = true;
  btn.textContent = 'Creating…';
  var body = { ticker: ticker };
  if (entryPrice != null && entryPrice > 0) body.entry_price = entryPrice;
  if (stopLoss != null && stopLoss > 0) body.stop_loss = stopLoss;
  if (takeProfit != null && takeProfit > 0) body.take_profit = takeProfit;
  fetch('/api/trading/proposals/from-pick', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.ok) {
        btn.textContent = 'Created!';
        btn.style.background = '#22c55e';
        setTimeout(function() { loadBestIdeas(); }, 800);
      } else {
        btn.disabled = false;
        btn.textContent = 'Create Proposal';
        alert(d.error || 'Could not create proposal');
      }
    })
    .catch(function() { btn.disabled = false; btn.textContent = 'Create Proposal'; });
}

function runQuickBacktest(ticker, btnEl) {
  if (!btnEl || !ticker) return;
  btnEl.disabled = true;
  btnEl.textContent = 'Running\u2026';
  var expandEl = document.getElementById('bt-expand-' + ticker);
  fetch('/api/trading/backtest/quick?ticker=' + encodeURIComponent(ticker))
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(d) {
      btnEl.disabled = false;
      if (!d.ok) {
        btnEl.textContent = 'Failed';
        setTimeout(function() { btnEl.textContent = '\u25B6 Run Backtest'; }, 2000);
        return;
      }
      btnEl.textContent = '\u2713 Done';
      setTimeout(function() { btnEl.textContent = '\u25B6 Run Again'; }, 1500);
      if (!expandEl) return;
      expandEl.style.display = 'block';
      var tradesList = d.trades || [];
      var wins = tradesList.filter(function(t){ return t.pnl >= 0; }).length;
      var losses = tradesList.length - wins;

      var statsHtml = '<div class="bi-bt-stats">';
      statsHtml += '<div>Strategy: <span>' + escHtml(d.strategy_name || '--') + '</span></div>';
      statsHtml += '<div>Return: <span style="color:' + (d.return_pct >= 0 ? '#22c55e' : '#ef4444') + '">' + (d.return_pct >= 0 ? '+' : '') + (d.return_pct || 0).toFixed(1) + '%</span></div>';
      statsHtml += '<div>Win Rate: <span>' + (function(){ var w = tradingBacktestWinRateToPct(d.win_rate); return (w != null ? w : 0).toFixed(0); })() + '%</span></div>';
      statsHtml += '<div>Trades: <span>' + (d.trade_count || 0) + '</span>';
      if (tradesList.length > 0) statsHtml += ' <span style="color:#22c55e">' + wins + 'W</span>/<span style="color:#ef4444">' + losses + 'L</span>';
      statsHtml += '</div>';
      if (d.sharpe != null) statsHtml += '<div>Sharpe: <span>' + d.sharpe.toFixed(2) + '</span></div>';
      if (d.max_drawdown != null) statsHtml += '<div>Max DD: <span style="color:#ef4444">' + d.max_drawdown.toFixed(1) + '%</span></div>';
      statsHtml += '</div>';

      var canvasHtml = '';
      if (d.equity_curve && d.equity_curve.length > 2) {
        canvasHtml = '<canvas id="bt-spark-' + ticker + '"></canvas>';
      }

      var tradeLogHtml = '';
      if (tradesList.length > 0) {
        tradeLogHtml = '<div style="margin-top:4px;font-size:9px;color:var(--text-muted);display:flex;flex-wrap:wrap;gap:3px 8px">';
        tradesList.forEach(function(t, i) {
          var w = t.pnl >= 0;
          var clr = w ? '#22c55e' : '#ef4444';
          var sign = w ? '+' : '';
          tradeLogHtml += '<span style="color:' + clr + '">#' + (i+1) + ' ' + sign + t.return_pct.toFixed(1) + '%</span>';
        });
        tradeLogHtml += '</div>';
      }

      var linkHtml = '<div style="margin-top:6px;text-align:right"><a href="#" style="font-size:10px;color:var(--accent);text-decoration:none;font-weight:600" onclick="event.stopPropagation();event.preventDefault();_openFullBacktest(\'' + escAttr(ticker) + '\')">Open Full Backtest &#x2192;</a></div>';
      expandEl.innerHTML = canvasHtml + statsHtml + tradeLogHtml + linkHtml;
      if (d.equity_curve && d.equity_curve.length > 2) {
        _drawSparkline('bt-spark-' + ticker, d.equity_curve, tradesList);
      }
    })
    .catch(function(err) {
      btnEl.disabled = false;
      btnEl.textContent = 'Error';
      console.error('Quick backtest failed for ' + ticker, err);
      setTimeout(function() { btnEl.textContent = '\u25B6 Run Backtest'; }, 2000);
    });
}

function _drawSparkline(canvasId, curve, trades) {
  var canvas = document.getElementById(canvasId);
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var w = canvas.parentElement.clientWidth - 16;
  var h = 80;
  canvas.width = w * (window.devicePixelRatio || 1);
  canvas.height = h * (window.devicePixelRatio || 1);
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';
  ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);
  var vals = curve.map(function(pt) { return pt.value; });
  var times = curve.map(function(pt) { return pt.time; });
  var mn = Math.min.apply(null, vals);
  var mx = Math.max.apply(null, vals);
  var range = mx - mn || 1;
  var padTop = 14, padBot = 4;
  var plotH = h - padTop - padBot;
  var step = w / (vals.length - 1);
  var startVal = vals[0];
  var endVal = vals[vals.length - 1];
  var color = endVal >= startVal ? '#22c55e' : '#ef4444';

  function timeToX(t) {
    var tMin = times[0], tMax = times[times.length - 1];
    if (tMax === tMin) return w / 2;
    return ((t - tMin) / (tMax - tMin)) * w;
  }
  function valToY(v) {
    return h - padBot - ((v - mn) / range) * plotH;
  }

  ctx.beginPath();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  for (var i = 0; i < vals.length; i++) {
    var x = i * step;
    var y = valToY(vals[i]);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  var cr = parseInt(color.slice(1,3),16), cg = parseInt(color.slice(3,5),16), cb = parseInt(color.slice(5,7),16);
  ctx.fillStyle = 'rgba(' + cr + ',' + cg + ',' + cb + ',0.08)';
  ctx.fill();

  if (!trades || trades.length === 0) return;

  trades.forEach(function(t) {
    var isWin = t.pnl >= 0;

    if (t.entry_time != null) {
      var bx = timeToX(t.entry_time);
      var eqIdx = _findClosestIdx(times, t.entry_time);
      var by = eqIdx >= 0 ? valToY(vals[eqIdx]) : h / 2;
      // Green up-triangle for BUY
      ctx.beginPath();
      ctx.moveTo(bx, by + 2);
      ctx.lineTo(bx - 4, by + 9);
      ctx.lineTo(bx + 4, by + 9);
      ctx.closePath();
      ctx.fillStyle = '#2563eb';
      ctx.fill();
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 0.5;
      ctx.stroke();
    }

    if (t.exit_time != null) {
      var sx = timeToX(t.exit_time);
      var seqIdx = _findClosestIdx(times, t.exit_time);
      var sy = seqIdx >= 0 ? valToY(vals[seqIdx]) : h / 2;
      // Down-triangle for SELL (green=win, red=loss)
      ctx.beginPath();
      ctx.moveTo(sx, sy - 2);
      ctx.lineTo(sx - 4, sy - 9);
      ctx.lineTo(sx + 4, sy - 9);
      ctx.closePath();
      ctx.fillStyle = isWin ? '#16a34a' : '#dc2626';
      ctx.fill();
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 0.5;
      ctx.stroke();

      // P&L label above
      var label = (isWin ? '+' : '') + t.return_pct.toFixed(0) + '%';
      ctx.font = 'bold 8px sans-serif';
      ctx.fillStyle = isWin ? '#22c55e' : '#ef4444';
      ctx.textAlign = 'center';
      ctx.fillText(label, sx, sy - 11);
    }
  });
}

function _findClosestIdx(arr, val) {
  var best = -1, bestDist = Infinity;
  for (var i = 0; i < arr.length; i++) {
    var d = Math.abs(arr[i] - val);
    if (d < bestDist) { bestDist = d; best = i; }
    if (d === 0) break;
  }
  return best;
}

function _openFullBacktest(ticker) {
  selectTicker(ticker);
  var tabs = document.querySelectorAll('.t-tab');
  tabs.forEach(function(t) {
    if (t.getAttribute('data-tab') === 'backtest') t.click();
  });
  setTimeout(function() { runBacktest(); }, 400);
}

function loadSignals() { loadBestIdeas(); }
function loadSignalsBanner() { /* banner removed; use Best Ideas tab */ }
function loadTopPicks() { loadBestIdeas(); }
function loadProposals() { /* now handled by loadBestIdeas */ }

function runFullScan() {
  var btn = event.target;
  btn.textContent = 'Scanning...';
  btn.disabled = true;
  fetch('/api/trading/scan', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})})
    .then(function(r){return r.json();}).then(function(d) {
      btn.textContent = '\uD83D\uDD0D Run Full Scan';
      btn.disabled = false;
      if (d.ok) {
        loadTopPicks();
      }
    }).catch(function() { btn.textContent = '\uD83D\uDD0D Run Full Scan'; btn.disabled = false; });
}

/* ── Strategy Proposals (actions) ─────────────────── */

function approveProposal(id, btn, broker) {
  btn.disabled = true;
  btn.textContent = 'Executing...';
  var opts = {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({broker: broker || null})};
  fetch('/api/trading/proposals/' + id + '/approve', opts)
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.ok) {
        var exec = d.execution || {};
        var st = exec.status || 'unknown';
        if (st === 'executed') {
          btn.textContent = 'Executed!';
          btn.style.background = '#22c55e';
        } else if (st === 'recorded') {
          btn.textContent = 'Recorded';
          btn.style.background = '#3b82f6';
        } else if (st === 'failed') {
          btn.textContent = 'Order Failed';
          btn.style.background = '#ef4444';
          alert('Order failed: ' + (exec.error || 'Unknown error'));
        } else {
          btn.textContent = 'Approved';
        }
        if (exec.reason) {
          var row = btn.closest('.proposal-card') || btn.parentElement;
          var note = document.createElement('div');
          note.style.cssText = 'color:#94a3b8;font-size:11px;margin-top:4px;';
          note.textContent = exec.reason;
          row.appendChild(note);
        }
        setTimeout(function() { loadBestIdeas(); }, 1500);
      } else {
        btn.disabled = false;
        btn.textContent = 'Approve & Execute';
        alert(d.error || 'Failed to approve proposal');
      }
    })
    .catch(function(e) {
      btn.disabled = false;
      btn.textContent = 'Approve & Execute';
      alert('Network error: ' + (e.message || 'Could not reach server'));
    });
}

function rejectProposal(id, btn) {
  btn.disabled = true;
  btn.textContent = 'Rejecting...';
  fetch('/api/trading/proposals/' + id + '/reject', {method: 'POST'})
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.ok) loadBestIdeas();
      else { btn.disabled = false; btn.textContent = 'Reject'; }
    })
    .catch(function() { btn.disabled = false; btn.textContent = 'Reject'; });
}

function recheckProposal(id, btnEl) {
  if (!btnEl || !id) return;
  btnEl.disabled = true;
  btnEl.textContent = '\u2026';
  fetch('/api/trading/proposals/' + id + '/recheck', {method: 'POST'})
    .then(function(r) { return r.json(); })
    .then(function(d) {
      btnEl.disabled = false;
      btnEl.textContent = '\u21BB Recheck';
      if (d.ok && d.expired) loadBestIdeas();
      else if (d.ok && d.status) {
        btnEl.title = d.live_price ? 'Now $' + d.live_price + ' (' + (d.drift_pct || 0) + '% drift) — ' + d.status : d.message || d.status;
      }
    })
    .catch(function() { btnEl.disabled = false; btnEl.textContent = '\u21BB Recheck'; });
}

/* ── Alert Settings & History ──────────────────── */

function toggleAlertSettings() {
  var panel = document.getElementById('alert-settings-panel');
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
  if (panel.style.display === 'block') loadAlertSettings();
}

function loadAlertSettings() {
  fetch('/api/trading/alerts/settings').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var indicator = document.getElementById('sms-status-indicator');
    if (d.configured) {
      indicator.className = 'sms-status ok';
      indicator.textContent = 'Connected (' + (d.provider === 'twilio' ? 'Twilio' : 'Email Gateway') + ') - ***' + d.phone;
    } else {
      indicator.className = 'sms-status off';
      indicator.textContent = 'Not configured - set SMS_PHONE in .env';
    }
  }).catch(function() {});
}

function sendTestAlert() {
  var result = document.getElementById('test-alert-result');
  result.textContent = 'Sending...';
  result.style.color = 'var(--text-muted)';
  fetch('/api/trading/alerts/test', {method: 'POST'})
    .then(function(r){return r.json();}).then(function(d) {
      if (d.ok) {
        result.textContent = 'Test SMS sent!';
        result.style.color = '#22c55e';
      } else {
        result.textContent = d.error || 'Failed to send';
        result.style.color = '#ef4444';
      }
    })
    .catch(function() { result.textContent = 'Error'; result.style.color = '#ef4444'; });
}

function loadAlertHistory() {
  fetch('/api/trading/alerts/history?limit=30').then(function(r){return r.json();}).then(function(d) {
    if (!d.ok) return;
    var list = document.getElementById('alert-history-list');
    if (!d.alerts || !d.alerts.length) {
      list.innerHTML = '<div style="font-size:11px;color:var(--text-muted);padding:8px">No alerts yet.</div>';
      return;
    }
    var html = '';
    d.alerts.forEach(function(a) {
      var typeCls = (a.alert_type || 'test').replace(/ /g, '_');
      var time = a.created_at ? new Date(a.created_at).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
      html += '<div class="alert-row">';
      html += '<span class="alert-type-badge ' + typeCls + '">' + typeCls.replace(/_/g, ' ') + '</span>';
      if (a.trade_type) {
        var ttCls = 'tt-' + a.trade_type;
        html += '<span class="alert-trade-type ' + ttCls + '">' + a.trade_type.replace(/_/g, ' ') + '</span>';
      }
      html += '<span class="alert-ticker" style="cursor:pointer;text-decoration:underline" onclick="selectTicker(\'' + (a.ticker || '') + '\')">' + (a.ticker || '') + '</span>';
      html += '<span class="alert-msg" title="' + escHtml(a.message) + '">' + escHtml(a.message) + '</span>';
      if (a.duration_estimate) {
        html += '<span class="alert-eta" title="Estimated time to target">' + a.duration_estimate + '</span>';
      }
      html += '<span class="alert-time">' + time + '</span>';
      html += '</div>';
    });
    list.innerHTML = html;
  }).catch(function() {});
}
