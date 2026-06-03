function _closeTokenModal() {
  document.getElementById('token-modal-bg').classList.remove('open');
}

function _searchTokens(q) {
  var filtered = _w3.tokenList.filter(function(t){
    var lq = q.toLowerCase();
    return t.symbol.toLowerCase().indexOf(lq) !== -1 || t.name.toLowerCase().indexOf(lq) !== -1;
  });
  _renderTokenList(filtered);
}

function _renderTokenList(tokens) {
  var list = document.getElementById('token-modal-list');
  list.innerHTML = '';
  tokens.forEach(function(t) {
    var item = document.createElement('div');
    item.className = 'token-modal-item';
    var addrShort = t.address.length > 10 ? (t.address.slice(0,6) + '...' + t.address.slice(-4)) : t.address;
    item.innerHTML = '<span class="tmi-symbol">' + t.symbol + '</span><span class="tmi-name">' + t.name + '</span><span class="tmi-addr">' + addrShort + '</span>';
    item.onclick = function() {
      if (_w3.tokenModalSide === 'sell') { _w3.sellToken = t; }
      else { _w3.buyToken = t; }
      _updateTokenButtons();
      _closeTokenModal();
      _updateSwapBalances();
      _onSwapAmountChange();
    };
    list.appendChild(item);
  });
}

function _flipTokens() {
  var tmp = _w3.sellToken;
  _w3.sellToken = _w3.buyToken;
  _w3.buyToken = tmp;
  _updateTokenButtons();
  var sellInput = document.getElementById('swap-sell-amount');
  var buyInput = document.getElementById('swap-buy-amount');
  sellInput.value = buyInput.value;
  buyInput.value = '';
  _updateSwapBalances();
  _onSwapAmountChange();
}

function _setSlippage(bps, btn) {
  _w3.slippageBps = bps;
  document.querySelectorAll('.slip-btn').forEach(function(b){ b.classList.remove('active'); });
  btn.classList.add('active');
}

/* ── Swap balance display ─────────────────────────── */
var NATIVE_ADDR = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE';

async function _getTokenBalance(token) {
  if (!_w3.provider || !_w3.address || !token) return '0';
  try {
    if (token.address === NATIVE_ADDR || token.address.toLowerCase() === '0x0000000000000000000000000000000000000000') {
      var bal = await _w3.provider.getBalance(_w3.address);
      return ethers.formatUnits(bal, 18);
    }
    var erc20 = new ethers.Contract(token.address, ['function balanceOf(address) view returns (uint256)'], _w3.provider);
    var raw = await erc20.balanceOf(_w3.address);
    return ethers.formatUnits(raw, parseInt(token.decimals || '18'));
  } catch(e) { return '0'; }
}

async function _updateSwapBalances() {
  if (!_w3.connected) return;
  var sellEl = document.getElementById('swap-sell-balance');
  var buyEl = document.getElementById('swap-buy-balance');
  if (_w3.sellToken) {
    var sb = await _getTokenBalance(_w3.sellToken);
    sellEl.innerHTML = 'Balance: ' + parseFloat(sb).toFixed(6) + ' <span class="max-btn" onclick="_setMaxSellAmount(\'' + sb + '\')">MAX</span>';
  }
  if (_w3.buyToken) {
    var bb = await _getTokenBalance(_w3.buyToken);
    buyEl.innerHTML = 'Balance: ' + parseFloat(bb).toFixed(6);
  }
}

function _setMaxSellAmount(bal) {
  document.getElementById('swap-sell-amount').value = parseFloat(bal).toFixed(8);
  _onSwapAmountChange();
}

/* ── Swap quote (debounced) ───────────────────────── */
function _onSwapAmountChange() {
  clearTimeout(_w3.quoteTimer);
  var val = document.getElementById('swap-sell-amount').value.trim();
  var btn = document.getElementById('swap-exec-btn');
  if (!val || isNaN(parseFloat(val)) || parseFloat(val) <= 0) {
    document.getElementById('swap-buy-amount').value = '';
    document.getElementById('swap-details').style.display = 'none';
    btn.disabled = true;
    btn.textContent = 'Enter an amount';
    _w3.lastQuote = null;
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Fetching quote...';
  _w3.quoteTimer = setTimeout(function(){ _fetchSwapPrice(val); }, 500);
}

async function _fetchSwapPrice(val) {
  if (!_w3.sellToken || !_w3.buyToken) return;
  var decimals = parseInt(_w3.sellToken.decimals || '18');
  var sellAmountWei;
  try { sellAmountWei = ethers.parseUnits(val, decimals).toString(); } catch(e) { return; }
  try {
    var url = '/api/trading/web3/price?chain_id=' + _w3.chainId
      + '&sell=' + encodeURIComponent(_w3.sellToken.address)
      + '&buy=' + encodeURIComponent(_w3.buyToken.address)
      + '&amount=' + sellAmountWei
      + '&taker=' + encodeURIComponent(_w3.address);
    var resp = await fetch(url);
    var data = await resp.json();
    var btn = document.getElementById('swap-exec-btn');
    if (!data.ok) {
      document.getElementById('swap-buy-amount').value = '';
      document.getElementById('swap-details').style.display = 'none';
      btn.textContent = data.error || 'Quote failed';
      btn.disabled = true;
      return;
    }
    var buyDec = parseInt(_w3.buyToken.decimals || '18');
    var buyAmt = ethers.formatUnits(data.buyAmount, buyDec);
    document.getElementById('swap-buy-amount').value = smartPrice(parseFloat(buyAmt));

    var rate = parseFloat(buyAmt) / parseFloat(val);
    document.getElementById('swap-rate').textContent = '1 ' + _w3.sellToken.symbol + ' = ' + smartPrice(rate) + ' ' + _w3.buyToken.symbol;
    document.getElementById('swap-impact').textContent = '<0.1%';
    var gasGwei = data.gasPrice ? (parseInt(data.gasPrice) / 1e9).toFixed(1) + ' gwei' : '--';
    document.getElementById('swap-gas').textContent = gasGwei;
    var minBuy = parseFloat(buyAmt) * (1 - _w3.slippageBps / 10000);
    document.getElementById('swap-min').textContent = smartPrice(minBuy) + ' ' + _w3.buyToken.symbol;
    document.getElementById('swap-details').style.display = '';

    btn.textContent = 'Review Swap';
    btn.disabled = false;
    btn.classList.remove('approve');
    _w3.lastQuote = { sellAmountWei: sellAmountWei };
  } catch(e) {
    console.error('Price fetch error:', e);
  }
}

/* ── Swap execution ───────────────────────────────── */
async function _executeSwap() {
  if (!_w3.connected || !_w3.sellToken || !_w3.buyToken) return;
  var btn = document.getElementById('swap-exec-btn');
  var sellAmt = document.getElementById('swap-sell-amount').value.trim();
  if (!sellAmt || !_w3.lastQuote) return;

  btn.disabled = true;
  btn.textContent = 'Getting quote...';

  var sellAmountWei = _w3.lastQuote.sellAmountWei;
  try {
    // Check ERC-20 allowance for non-native tokens
    var isNative = (_w3.sellToken.address === NATIVE_ADDR);
    if (!isNative) {
      btn.textContent = 'Checking allowance...';
      var permit2Addr = '0x000000000022D473030F116dDEE9F6B43aC78BA3';
      var erc20 = new ethers.Contract(_w3.sellToken.address, [
        'function allowance(address,address) view returns (uint256)',
        'function approve(address,uint256) returns (bool)'
      ], _w3.signer);
      var allowance = await erc20.allowance(_w3.address, permit2Addr);
      if (allowance < BigInt(sellAmountWei)) {
        btn.textContent = 'Approve token spend...';
        btn.classList.add('approve');
        var approveTx = await erc20.approve(permit2Addr, ethers.MaxUint256);
        _showTxToast('Approving ' + _w3.sellToken.symbol + '...', approveTx.hash);
        await approveTx.wait();
        _updateTxToast(approveTx.hash, 'confirmed');
      }
    }

    // Fetch full quote with calldata
    btn.textContent = 'Sign transaction...';
    btn.classList.remove('approve');
    var quoteResp = await fetch('/api/trading/web3/quote', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        chain_id: _w3.chainId,
        sell_token: _w3.sellToken.address,
        buy_token: _w3.buyToken.address,
        sell_amount: sellAmountWei,
        taker_address: _w3.address,
        slippage_bps: _w3.slippageBps
      })
    });
    var quote = await quoteResp.json();
    if (!quote.ok) { btn.textContent = quote.error || 'Quote failed'; btn.disabled = false; return; }

    // Build and send tx via MetaMask
    var txObj = quote.transaction || { to: quote.to, data: quote.data, value: quote.value };
    if (!txObj.to) txObj = { to: quote.to, data: quote.data, value: quote.value };
    if (txObj.value && typeof txObj.value === 'string' && !txObj.value.startsWith('0x')) {
      txObj.value = '0x' + BigInt(txObj.value).toString(16);
    }
    if (txObj.gas) { txObj.gasLimit = txObj.gas; delete txObj.gas; }

    var tx = await _w3.signer.sendTransaction(txObj);
    btn.textContent = 'Transaction pending...';
    _showTxToast('Swap pending...', tx.hash);

    var receipt = await tx.wait();
    if (receipt.status === 1) {
      _updateTxToast(tx.hash, 'confirmed');
      btn.textContent = 'Swap successful!';
    } else {
      _updateTxToast(tx.hash, 'failed');
      btn.textContent = 'Swap failed';
    }
    setTimeout(function(){
      btn.textContent = 'Review Swap';
      btn.disabled = false;
      _updateSwapBalances();
      _updateWalletUI();
    }, 3000);
  } catch(e) {
    console.error('Swap error:', e);
    btn.textContent = e.code === 'ACTION_REJECTED' ? 'Transaction rejected' : 'Swap failed';
    btn.disabled = false;
    setTimeout(function(){ btn.textContent = 'Review Swap'; }, 3000);
  }
}

function _showTxToast(msg, txHash) {
  var area = document.getElementById('swap-tx-area');
  var chain = CHAINS[_w3.chainId] || {};
  var link = chain.explorer ? chain.explorer + '/tx/' + txHash : '#';
  var toast = document.createElement('div');
  toast.className = 'swap-tx-toast';
  toast.id = 'tx-' + txHash;
  toast.innerHTML = '<div class="tx-spinner"></div> ' + msg + ' <a href="' + link + '" target="_blank" rel="noopener">' + txHash.slice(0,10) + '...</a>';
  area.prepend(toast);
}

function _updateTxToast(txHash, status) {
  var el = document.getElementById('tx-' + txHash);
  if (!el) return;
  el.classList.add(status);
  var spinner = el.querySelector('.tx-spinner');
  if (spinner) spinner.style.display = 'none';
  var icon = status === 'confirmed' ? '&#x2705; ' : '&#x274C; ';
  el.innerHTML = el.innerHTML.replace(/<div class="tx-spinner"[^>]*><\/div>\s*/, icon);
}

/* Pre-fill swap panel from screener (called by quick-swap buttons) */
function prefillSwap(symbol) {
  switchTabByName('swap');
  if (!_w3.connected) { _connectWallet(); return; }
  var matchedToken = _w3.tokenList.find(function(t){ return t.symbol.toUpperCase() === symbol.toUpperCase(); });
  if (matchedToken) {
    _w3.buyToken = matchedToken;
    _updateTokenButtons();
    _updateSwapBalances();
  }
}


/* ── MetaMask Liquidation → Coinbase ──────────────── */

var USDC_ADDRESSES = {
  1:     '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
  137:   '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359',
  56:    '0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d',
  42161: '0xaf88d065e77c8cC2239327C5EDb3A432268e5831',
  8453:  '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
};

var COMMON_TOKENS = {
  1: [
    {symbol:'USDT',address:'0xdAC17F958D2ee523a2206206994597C13D831ec7',decimals:6},
    {symbol:'USDC',address:'0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',decimals:6},
    {symbol:'DAI',address:'0x6B175474E89094C44Da98b954EedeAC495271d0F',decimals:18},
    {symbol:'WETH',address:'0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2',decimals:18},
    {symbol:'WBTC',address:'0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599',decimals:8},
    {symbol:'LINK',address:'0x514910771AF9Ca656af840dff83E8264EcF986CA',decimals:18},
    {symbol:'UNI',address:'0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984',decimals:18},
    {symbol:'PEPE',address:'0x6982508145454Ce325dDbE47a25d4ec3d2311933',decimals:18},
    {symbol:'SHIB',address:'0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE',decimals:18},
  ],
  137: [
    {symbol:'USDT',address:'0xc2132D05D31c914a87C6611C10748AEb04B58e8F',decimals:6},
    {symbol:'USDC',address:'0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359',decimals:6},
    {symbol:'WETH',address:'0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619',decimals:18},
    {symbol:'WMATIC',address:'0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270',decimals:18},
    {symbol:'DAI',address:'0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063',decimals:18},
  ],
  56: [
    {symbol:'USDT',address:'0x55d398326f99059fF775485246999027B3197955',decimals:18},
    {symbol:'USDC',address:'0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d',decimals:18},
    {symbol:'WBNB',address:'0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c',decimals:18},
    {symbol:'DAI',address:'0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3',decimals:18},
  ],
  42161: [
    {symbol:'USDT',address:'0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9',decimals:6},
    {symbol:'USDC',address:'0xaf88d065e77c8cC2239327C5EDb3A432268e5831',decimals:6},
    {symbol:'WETH',address:'0x82aF49447D8a07e3bd95BD0d56f35241523fBab1',decimals:18},
    {symbol:'DAI',address:'0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1',decimals:18},
  ],
  8453: [
    {symbol:'USDC',address:'0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',decimals:6},
    {symbol:'WETH',address:'0x4200000000000000000000000000000000000006',decimals:18},
    {symbol:'DAI',address:'0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb',decimals:18},
  ]
};

var CHAIN_RPC = {
  1:     'https://eth.llamarpc.com',
  137:   'https://polygon-rpc.com',
  56:    'https://bsc-dataseed1.binance.org',
  42161: 'https://arb1.arbitrum.io/rpc',
  8453:  'https://mainnet.base.org'
};
var CHAIN_NAMES = {1:'Ethereum',137:'Polygon',56:'BNB Chain',42161:'Arbitrum',8453:'Base'};
var CHAIN_NATIVE = {1:{sym:'ETH',dec:18},137:{sym:'MATIC',dec:18},56:{sym:'BNB',dec:18},42161:{sym:'ETH',dec:18},8453:{sym:'ETH',dec:18}};

async function _rpcBalanceOf(rpcUrl, tokenAddr, wallet, decimals) {
  var calldata = '0x70a08231' + wallet.slice(2).toLowerCase().padStart(64, '0');
  try {
    var resp = await fetch(rpcUrl, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({jsonrpc:'2.0',id:1,method:'eth_call',params:[{to:tokenAddr,data:calldata},'latest']})
    });
    var json = await resp.json();
    if (json.result && json.result !== '0x' && json.result !== '0x0' && json.result !== '0x0000000000000000000000000000000000000000000000000000000000000000') {
      return parseFloat(ethers.formatUnits(BigInt(json.result), decimals));
    }
  } catch(e) { console.warn('[rpc] error:', e); }
  return 0;
}

async function _rpcNativeBalance(rpcUrl, wallet) {
  try {
    var resp = await fetch(rpcUrl, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({jsonrpc:'2.0',id:1,method:'eth_getBalance',params:[wallet,'latest']})
    });
    var json = await resp.json();
    if (json.result) return parseFloat(ethers.formatUnits(BigInt(json.result), 18));
  } catch(e) {}
  return 0;
}

async function _scanChainViaRPC(chainId, wallet) {
  var rpc = CHAIN_RPC[chainId];
  if (!rpc) return [];
  var found = [];
  var native = CHAIN_NATIVE[chainId];
  var nativeBal = await _rpcNativeBalance(rpc, wallet);
  if (nativeBal > 0.000001) {
    found.push({symbol: native.sym, address: NATIVE_ADDR, balance: nativeBal, decimals: native.dec, chainId: chainId});
  }
  var tokens = COMMON_TOKENS[chainId] || [];
  var checks = tokens.map(function(tk) {
    return _rpcBalanceOf(rpc, tk.address, wallet, tk.decimals).then(function(bal) {
      if (bal > 0.000001) found.push({symbol: tk.symbol, address: tk.address, balance: bal, decimals: tk.decimals, chainId: chainId});
    });
  });
  await Promise.all(checks);
  return found;
}

async function _scanAllTokenBalances() {
  if (!_w3.connected) return [];

  if (!_w3.provider) {
    try {
      _w3.provider = new ethers.BrowserProvider(window.ethereum);
      _w3.signer = await _w3.provider.getSigner();
      var net = await _w3.provider.getNetwork();
      _w3.chainId = Number(net.chainId);
    } catch(e) { return []; }
  }

  var wallet = _w3.address;
  var connectedChain = _w3.chainId;

  appendAiMsg('assistant', 'Scanning all chains for wallet `' + wallet.slice(0,6) + '...' + wallet.slice(-4) + '`...');

  var allChains = [1, 137, 56, 42161, 8453];
  var allResults = [];
  var chainPromises = allChains.map(function(cid) {
    return _scanChainViaRPC(cid, wallet).then(function(found) {
      found.forEach(function(f) { allResults.push(f); });
    });
  });
  await Promise.all(chainPromises);

  if (allResults.length === 0) return [];

  var chainGroups = {};
  allResults.forEach(function(r) {
    if (!chainGroups[r.chainId]) chainGroups[r.chainId] = [];
    chainGroups[r.chainId].push(r);
  });

  var bestChain = connectedChain;
  var bestValue = 0;
  Object.keys(chainGroups).forEach(function(cid) {
    var total = 0;
    chainGroups[cid].forEach(function(t) { total += t.balance; });
    if (total > bestValue) { bestValue = total; bestChain = parseInt(cid); }
  });

  if (bestChain !== connectedChain) {
    var chainName = CHAIN_NAMES[bestChain] || ('chain ' + bestChain);
    appendAiMsg('assistant', 'Your tokens are on **' + chainName + '**. Switching MetaMask now...');
    try {
      var hexId = '0x' + bestChain.toString(16);
      await window.ethereum.request({ method: 'wallet_switchEthereumChain', params: [{chainId: hexId}] });
      _w3.provider = new ethers.BrowserProvider(window.ethereum);
      _w3.signer = await _w3.provider.getSigner();
      _w3.chainId = bestChain;
      var sel = document.getElementById('chain-select');
      if (sel) sel.value = bestChain;
    } catch(e) {
      appendAiMsg('assistant', 'Please switch MetaMask to **' + chainName + '** manually and try again.');
      return [];
    }
  }

  var results = [];
  var group = chainGroups[bestChain] || [];
  group.forEach(function(r) {
    results.push({
      symbol: r.symbol, address: r.address, balance: r.balance,
      balanceRaw: ethers.parseUnits(r.balance.toFixed(r.decimals > 8 ? 8 : r.decimals), r.decimals).toString(),
      decimals: r.decimals,
      token: {symbol: r.symbol, address: r.address, decimals: String(r.decimals)}
    });
  });
  return results;
}

var _liquidationActive = false;

var STABLECOIN_SYMBOLS = ['USDT','USDC','DAI','BUSD','TUSD','FRAX'];

function _isStablecoin(symbol) {
  return STABLECOIN_SYMBOLS.indexOf(symbol.toUpperCase()) !== -1;
}

async function _executeLiquidationFlow(depositAddress, preScannedTokens) {
  if (_liquidationActive) { appendAiMsg('assistant', 'A liquidation is already in progress.'); return; }
  if (!_w3.connected) { appendAiMsg('assistant', 'Please connect MetaMask first.'); return; }
  _liquidationActive = true;

  var chainId = _w3.chainId;
  var nativeInfo = CHAIN_NATIVE[chainId] || {sym:'ETH',dec:18};
  var target = _pickTargetToken(preScannedTokens || [], chainId);
  var usdcAddr = USDC_ADDRESSES[chainId];

  var tokens = preScannedTokens;
  if (!tokens || !tokens.length) {
    appendAiMsg('assistant', 'Scanning your wallet for tokens...');
    tokens = await _scanAllTokenBalances();
  }
  if (!tokens.length) {
    appendAiMsg('assistant', 'No tokens with meaningful balances found in your wallet.');
    _liquidationActive = false;
    return;
  }

  var directTransfer = [];
  var toSwap = [];
  var nativeTk = null;

  tokens.forEach(function(t) {
    if (t.address === NATIVE_ADDR || t.address === '0x0000000000000000000000000000000000000000') {
      nativeTk = t;
    } else if (_isStablecoin(t.symbol)) {
      directTransfer.push(t);
    } else {
      toSwap.push(t);
    }
  });

  var sendNative = (target.type === 'native' && nativeTk && nativeTk.balance > 0);
  var hasWork = directTransfer.length > 0 || toSwap.length > 0 || sendNative;
  if (!hasWork) {
    appendAiMsg('assistant', 'No transferable tokens found.');
    _liquidationActive = false;
    return;
  }

  var summary = '**Plan:**\n';
  if (toSwap.length > 0) {
    toSwap.forEach(function(t) {
      summary += '- Swap **' + t.balance.toFixed(6) + ' ' + t.symbol + '** → ' + nativeInfo.sym + '\n';
    });
  }
  if (directTransfer.length > 0) {
    if (target.type === 'native') {
      directTransfer.forEach(function(t) {
        summary += '- Swap **' + t.balance.toFixed(2) + ' ' + t.symbol + '** → ' + nativeInfo.sym + '\n';
      });
    } else {
      directTransfer.forEach(function(t) {
        summary += '- Send **' + t.balance.toFixed(2) + ' ' + t.symbol + '** directly to Coinbase\n';
      });
    }
  }
  if (sendNative) {
    summary += '- Send **' + nativeInfo.sym + '** (including swapped amounts) to Coinbase\n';
  }
  summary += '\nEach step requires MetaMask approval.\nType **confirm** to go or **cancel** to stop.';
  appendAiMsg('assistant', summary);

  _w3._pendingLiquidation = {
    toSwap: toSwap, directTransfer: directTransfer,
    usdcAddr: usdcAddr, depositAddress: depositAddress,
    nativeTk: nativeTk, targetType: target.type, targetSymbol: target.symbol
  };
}

async function _confirmLiquidation() {
  var pending = _w3._pendingLiquidation;
  if (!pending) { _liquidationActive = false; return; }
  var toSwap = pending.toSwap || [];
  var directTransfer = pending.directTransfer || [];
  var depositAddr = pending.depositAddress;
  var isNativeTarget = pending.targetType === 'native';
  var nativeInfo = CHAIN_NATIVE[_w3.chainId] || {sym:'ETH',dec:18};

  var permit2Addr = '0x000000000022D473030F116dDEE9F6B43aC78BA3';
  var swapped = 0;
  var failed = 0;
  var transferred = 0;

  var allToSwap = toSwap.slice();
  if (isNativeTarget && directTransfer.length > 0) {
    allToSwap = allToSwap.concat(directTransfer);
  }

  var nativeTarget = isNativeTarget
    ? '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE'
    : (pending.usdcAddr || USDC_ADDRESSES[_w3.chainId]);

  var nativeTargetLabel = isNativeTarget ? nativeInfo.sym : 'USDC';

  if (allToSwap.length > 0) {
    appendAiMsg('assistant', 'Starting swaps to **' + nativeTargetLabel + '**...');
    for (var i = 0; i < allToSwap.length; i++) {
      var tk = allToSwap[i];
      appendAiMsg('assistant', '**[' + (i+1) + '/' + allToSwap.length + ']** Swapping ' + tk.balance.toFixed(6) + ' ' + tk.symbol + ' → ' + nativeTargetLabel + '...');
      try {
        var sellAmountWei = ethers.parseUnits(tk.balance.toString(), tk.decimals).toString();

        var erc20 = new ethers.Contract(tk.address, [
          'function allowance(address,address) view returns (uint256)',
          'function approve(address,uint256) returns (bool)'
        ], _w3.signer);
        var allowance = await erc20.allowance(_w3.address, permit2Addr);
        if (allowance < BigInt(sellAmountWei)) {
          appendAiMsg('assistant', 'Approving ' + tk.symbol + ' for swap...');
          var approveTx = await erc20.approve(permit2Addr, ethers.MaxUint256);
          await approveTx.wait();
        }

        var buyToken = isNativeTarget
          ? '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE'
          : nativeTarget;

        var quoteResp = await fetch('/api/trading/web3/quote', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            chain_id: _w3.chainId,
            sell_token: tk.address,
            buy_token: buyToken,
            sell_amount: sellAmountWei,
            taker_address: _w3.address,
            slippage_bps: _w3.slippageBps || 100
          })
        });
        var quote = await quoteResp.json();
        if (!quote.ok) { appendAiMsg('assistant', 'Quote failed for ' + tk.symbol + ': ' + (quote.error || 'unknown')); failed++; continue; }

        var txObj = quote.transaction || { to: quote.to, data: quote.data, value: quote.value };
        if (!txObj.to) txObj = { to: quote.to, data: quote.data, value: quote.value };
        if (txObj.value && typeof txObj.value === 'string' && !txObj.value.startsWith('0x')) {
          txObj.value = '0x' + BigInt(txObj.value).toString(16);
        }
        if (txObj.gas) { txObj.gasLimit = txObj.gas; delete txObj.gas; }

        var tx = await _w3.signer.sendTransaction(txObj);
        _showTxToast('Swapping ' + tk.symbol + '...', tx.hash);
        var receipt = await tx.wait();
        if (receipt.status === 1) {
          _updateTxToast(tx.hash, 'confirmed');
          appendAiMsg('assistant', tk.symbol + ' swapped to ' + nativeTargetLabel + ' successfully.');
          swapped++;
        } else {
          _updateTxToast(tx.hash, 'failed');
          appendAiMsg('assistant', tk.symbol + ' swap failed on-chain.');
          failed++;
        }
        await new Promise(function(r){ setTimeout(r, 1500); });
      } catch(e) {
        var reason = e.code === 'ACTION_REJECTED' ? 'Rejected by user' : (e.message || String(e));
        appendAiMsg('assistant', 'Failed to swap ' + tk.symbol + ': ' + reason);
        failed++;
        if (e.code === 'ACTION_REJECTED') {
          appendAiMsg('assistant', 'Liquidation aborted by user.');
          _liquidationActive = false;
          _w3._pendingLiquidation = null;
          return;
        }
      }
    }
    if (allToSwap.length > 0) {
      appendAiMsg('assistant', '**Swaps done.** ' + swapped + ' succeeded, ' + failed + ' failed.');
    }
  }

  if (isNativeTarget) {
    try {
      var nativeBal = await _w3.provider.getBalance(_w3.address);
      var gasReserve = ethers.parseUnits('0.002', 18);
      var sendableNative = nativeBal - gasReserve;
      if (sendableNative <= 0n) {
        appendAiMsg('assistant', nativeInfo.sym + ' balance too low after reserving gas. Nothing to send.');
      } else {
        var sendAmt = parseFloat(ethers.formatUnits(sendableNative, 18));
        appendAiMsg('assistant', 'Sending **' + sendAmt.toFixed(6) + ' ' + nativeInfo.sym + '** to Coinbase...\n`' + depositAddr + '`');
        var nativeTx = await _w3.signer.sendTransaction({ to: depositAddr, value: sendableNative });
        _showTxToast('Sending ' + nativeInfo.sym + ' to Coinbase...', nativeTx.hash);
        var nativeReceipt = await nativeTx.wait();
        if (nativeReceipt.status === 1) {
          _updateTxToast(nativeTx.hash, 'confirmed');
          appendAiMsg('assistant', '**' + sendAmt.toFixed(6) + ' ' + nativeInfo.sym + ' sent!** It should appear in Coinbase within a few minutes.');
          transferred++;
        } else {
          _updateTxToast(nativeTx.hash, 'failed');
          appendAiMsg('assistant', nativeInfo.sym + ' transfer failed on-chain.');
        }
      }
    } catch(e) {
      var reason = e.code === 'ACTION_REJECTED' ? 'Rejected by user' : (e.message || String(e));
      appendAiMsg('assistant', nativeInfo.sym + ' transfer failed: ' + reason);
    }
  } else {
    var tokensToSend = directTransfer.slice();
    var usdcAddr = pending.usdcAddr;
    if (usdcAddr) {
      try {
        var usdcContract = new ethers.Contract(usdcAddr, ['function balanceOf(address) view returns (uint256)'], _w3.provider);
        var usdcBal = await usdcContract.balanceOf(_w3.address);
        var usdcAmt = parseFloat(ethers.formatUnits(usdcBal, 6));
        if (usdcAmt > 0.01) {
          var alreadyHas = tokensToSend.find(function(t){ return t.address.toLowerCase() === usdcAddr.toLowerCase(); });
          if (!alreadyHas) {
            tokensToSend.push({ symbol: 'USDC', address: usdcAddr, balance: usdcAmt, balanceRaw: usdcBal.toString(), decimals: 6 });
          }
        }
      } catch(e) {}
    }

    for (var j = 0; j < tokensToSend.length; j++) {
      var stk = tokensToSend[j];
      try {
        var contract = new ethers.Contract(stk.address, [
          'function balanceOf(address) view returns (uint256)',
          'function transfer(address,uint256) returns (bool)'
        ], _w3.signer);
        var bal = await contract.balanceOf(_w3.address);
        var balFmt = parseFloat(ethers.formatUnits(bal, stk.decimals));
        if (balFmt < 0.01) {
          appendAiMsg('assistant', stk.symbol + ' balance too low to transfer. Skipping.');
          continue;
        }
        appendAiMsg('assistant', 'Sending **' + balFmt.toFixed(2) + ' ' + stk.symbol + '** to Coinbase...\n`' + depositAddr + '`');
        var sendTx = await contract.transfer(depositAddr, bal);
        _showTxToast('Sending ' + stk.symbol + ' to Coinbase...', sendTx.hash);
        var sendReceipt = await sendTx.wait();
        if (sendReceipt.status === 1) {
          _updateTxToast(sendTx.hash, 'confirmed');
          appendAiMsg('assistant', '**' + balFmt.toFixed(2) + ' ' + stk.symbol + ' sent!** It should appear in Coinbase within a few minutes.');
          transferred++;
        } else {
          _updateTxToast(sendTx.hash, 'failed');
          appendAiMsg('assistant', stk.symbol + ' transfer failed on-chain.');
        }
      } catch(e) {
        var reason = e.code === 'ACTION_REJECTED' ? 'Rejected by user' : (e.message || String(e));
        appendAiMsg('assistant', stk.symbol + ' transfer failed: ' + reason);
        if (e.code === 'ACTION_REJECTED') {
          appendAiMsg('assistant', 'Transfer aborted by user.');
          _liquidationActive = false;
          _w3._pendingLiquidation = null;
          return;
        }
      }
    }
  }

  if (transferred > 0) {
    appendAiMsg('assistant', '**All done!** ' + transferred + ' transfer(s) sent to Coinbase successfully.');
  } else if (swapped > 0 && transferred === 0) {
    appendAiMsg('assistant', 'Swaps completed but transfer step had issues. Check your wallet.');
  } else {
    appendAiMsg('assistant', 'No transfers completed. Your tokens are still in your wallet.');
  }

  _liquidationActive = false;
  _w3._pendingLiquidation = null;
  _updateWalletUI();
}

function _cancelLiquidation() {
  _liquidationActive = false;
  _w3._pendingLiquidation = null;
  appendAiMsg('assistant', 'Liquidation cancelled.');
}

var _pendingDepositNetwork = '';
var _pendingDepositToken = '';
var _scannedTokens = null;

function _pickTargetToken(tokens, chainId) {
  var nativeInfo = CHAIN_NATIVE[chainId] || {sym:'ETH',dec:18};
  var nativeTk = tokens.find(function(t) {
    return t.address === NATIVE_ADDR || t.address === '0x0000000000000000000000000000000000000000';
  });
  var stables = tokens.filter(function(t) { return _isStablecoin(t.symbol) && t.address !== NATIVE_ADDR; });
  var erc20s = tokens.filter(function(t) {
    return !_isStablecoin(t.symbol) && t.address !== NATIVE_ADDR && t.address !== '0x0000000000000000000000000000000000000000';
  });

  var bestStable = stables.length > 0 ? stables.reduce(function(a,b){ return (b.usdValue||0) > (a.usdValue||0) ? b : a; }) : null;
  var nativeVal = nativeTk ? (nativeTk.usdValue || nativeTk.balance * 1) : 0;
  var stableVal = bestStable ? (bestStable.usdValue || bestStable.balance * 1) : 0;

  if (erc20s.length > 0 || (stables.length > 0 && nativeVal > stableVal)) {
    return { symbol: nativeInfo.sym, type: 'native' };
  }
  if (bestStable && stableVal > 0) {
    return { symbol: bestStable.symbol, type: 'stable' };
  }
  if (nativeTk && nativeVal > 0) {
    return { symbol: nativeInfo.sym, type: 'native' };
  }
  return { symbol: nativeInfo.sym, type: 'native' };
}

async function _handleLiquidationIntent() {
  if (!_w3.connected) {
    appendAiMsg('assistant', 'Please connect MetaMask first, then try again.');
    return;
  }
  appendAiMsg('assistant', 'Got it. Let me scan your wallet first to see what we\'re working with...');

  var tokens = await _scanAllTokenBalances();
  if (!tokens.length) {
    appendAiMsg('assistant', 'No tokens with meaningful balances found in your wallet.');
    return;
  }

  _scannedTokens = tokens;
  var chainId = _w3.chainId;
  var networkName = CHAIN_NAMES[chainId] || ('chain-' + chainId);
  var networkKey = networkName.toLowerCase().replace(/\s+/g, '_');
  var target = _pickTargetToken(tokens, chainId);

  var summary = 'Found on **' + networkName + '**:\n';
  tokens.forEach(function(t) { summary += '- **' + t.symbol + '**: ' + t.balance.toFixed(6) + '\n'; });
  summary += '\nI\'ll send everything to your Coinbase as **' + target.symbol + '** on **' + networkName + '**.';
  appendAiMsg('assistant', summary);

  _pendingDepositNetwork = networkKey;
  _pendingDepositToken = target.symbol;

  try {
    var addrKey = networkKey + '_' + target.symbol.toLowerCase();
    var resp = await fetch('/api/trading/broker/deposit-address?broker=coinbase&network=' + encodeURIComponent(addrKey), { credentials: 'same-origin' });
    var data = await resp.json();
    if (data.ok && data.address) {
      _executeLiquidationFlow(data.address, _scannedTokens);
    } else {
      appendAiMsg('assistant', 'I need your Coinbase **' + target.symbol + '** deposit address on **' + networkName + '**. You only need to do this once per token/network.');
      openDepositAddrDialog(networkName, target.symbol);
    }
  } catch(e) {
    appendAiMsg('assistant', 'I need your Coinbase **' + target.symbol + '** deposit address on **' + networkName + '**.');
    openDepositAddrDialog(networkName, target.symbol);
  }
}

function openDepositAddrDialog(networkName, tokenSymbol) {
  networkName = networkName || 'this network';
  tokenSymbol = tokenSymbol || 'USDT';
  document.getElementById('deposit-dialog-title').textContent = 'Coinbase ' + tokenSymbol + ' Deposit Address';
  document.getElementById('deposit-dialog-desc').innerHTML = 'Open <b>Coinbase</b> &rarr; <b>' + tokenSymbol + '</b> &rarr; <b>Receive</b> &rarr; select <b>' + networkName + '</b> network &rarr; copy the address.';
  document.getElementById('deposit-dialog-warning').textContent = 'Make sure you select "' + networkName + '" as the network in Coinbase! Using the wrong network will send funds to the wrong address.';
  document.getElementById('deposit-dialog-label').textContent = tokenSymbol + ' Deposit Address (' + networkName + ')';
  document.getElementById('deposit-addr-error').textContent = '';
  document.getElementById('deposit-addr-input').value = '';
  document.getElementById('deposit-addr-dialog').classList.add('visible');
}

function closeDepositAddrDialog() {
  document.getElementById('deposit-addr-dialog').classList.remove('visible');
  appendAiMsg('assistant', 'Transfer cancelled — no deposit address provided.');
}

async function saveDepositAddress() {
  var addr = document.getElementById('deposit-addr-input').value.trim();
  var errEl = document.getElementById('deposit-addr-error');
  if (!addr || !/^0x[a-fA-F0-9]{40}$/.test(addr)) {
    errEl.textContent = 'Please enter a valid address (0x..., 42 characters).';
    return;
  }
  errEl.textContent = '';
  var btn = document.getElementById('deposit-addr-save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  var networkKey = _pendingDepositNetwork || 'ethereum';
  var tokenSym = (_pendingDepositToken || 'ETH').toLowerCase();
  var addrKey = networkKey + '_' + tokenSym;
  try {
    var resp = await fetch('/api/trading/broker/deposit-address', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify({ broker: 'coinbase', network: addrKey, address: addr })
    });
    var data = await resp.json();
    if (data.ok) {
      document.getElementById('deposit-addr-dialog').classList.remove('visible');
      appendAiMsg('assistant', 'Coinbase deposit address saved for **' + (CHAIN_NAMES[_w3.chainId] || networkKey) + '**. Starting liquidation...');
      _executeLiquidationFlow(addr);
    } else {
      errEl.textContent = data.message || 'Failed to save address.';
    }
  } catch(e) {
    errEl.textContent = 'Network error. Please try again.';
  }
  btn.disabled = false;
  btn.textContent = 'Save & Continue';
}

/* ── Chart Annotations ──────────────────────────── */
var _lastBarTime = null;
var _chartBarTimes = [];

function _getLastCandleTime() {
  return _lastBarTime;
}

function _getNearestBarTime(targetTime) {
  if (!_chartBarTimes.length) return _lastBarTime;
  var best = _chartBarTimes[0];
  for (var i = 1; i < _chartBarTimes.length; i++) {
    if (Math.abs(_chartBarTimes[i] - targetTime) < Math.abs(best - targetTime)) best = _chartBarTimes[i];
  }
  return best;
}

function clearAnnotations(keepSpecs) {
  _chartAnnotations.forEach(function(line) {
    try { candleSeries.removePriceLine(line); } catch(e) {}
  });
  _chartAnnotations = [];
  if (!keepSpecs) { _savedAnnotationSpecs = []; }
  try { candleSeries.setMarkers([]); } catch(e) {}
  var legend = document.getElementById('annotations-legend');
  if (legend) {
    legend.innerHTML = '';
    legend.classList.remove('visible');
    legend.classList.remove('dragging');
  }
}

function _restoreAnnotations() {
  if (!_savedAnnotationSpecs.length || !candleSeries) return;
  _savedAnnotationSpecs.forEach(function(spec) {
    _addPriceLine(spec.price, spec.color, spec.title, spec.style, spec.width, true);
  });
  if (_savedAnnotationSpecs.length) {
    _showAnnotationLegend(_savedAnnotationSpecs.map(function(s) {
      return {label: s.title + (s.price ? ' $' + s.price : ''), color: s.color, dashed: s.style === LightweightCharts.LineStyle.Dashed};
    }));
  }
}

function _addPriceLine(price, color, title, style, width, skipSave) {
  if (!price || !candleSeries) return null;
  var lineStyle = style || LightweightCharts.LineStyle.Solid;
  var line = candleSeries.createPriceLine({
    price: price, color: color, lineWidth: width || 1,
    lineStyle: lineStyle,
    axisLabelVisible: true, title: title,
  });
  _chartAnnotations.push(line);
  if (!skipSave) {
    _savedAnnotationSpecs.push({price: price, color: color, title: title, style: lineStyle, width: width || 1});
  }
  return line;
}

function _showAnnotationLegend(items) {
  var legend = document.getElementById('annotations-legend');
  if (!legend) return;
  var html = '<div class="cal-header" id="cal-drag-handle">' +
    '<div class="cal-drag-dots"><span></span><span></span></div>' +
    '<span class="cal-title">Annotations</span>' +
    '<button class="cal-close" onclick="clearAnnotations()" title="Clear annotations (Esc)">&times;</button>' +
    '</div><div class="cal-body">';
  items.forEach(function(it) {
    var parts = it.label.match(/^(.+?)(\$[\d,.]+)?$/);
    var labelText = parts ? parts[1].trim() : it.label;
    var priceText = parts && parts[2] ? parts[2] : '';
    html += '<div class="cal-item">' +
      '<span class="cal-swatch' + (it.dashed ? ' dashed' : '') + '" style="' +
      (it.dashed ? 'border-color:'+it.color : 'background:'+it.color) + '"></span>' +
      '<span class="cal-label">' + labelText + '</span>' +
      (priceText ? '<span class="cal-price">' + priceText + '</span>' : '') +
      '</div>';
  });
  html += '</div>';
  legend.innerHTML = html;
  legend.classList.add('visible');
  _initLegendDrag();
}

/* ── Draggable legend ──────────────────────────────── */
var _legendDragState = { dragging: false, startX: 0, startY: 0, origX: 0, origY: 0 };

function _initLegendDrag() {
  var legend = document.getElementById('annotations-legend');
  var handle = document.getElementById('cal-drag-handle');
  if (!legend || !handle) return;

  var saved = sessionStorage.getItem('cal_pos');
  if (saved) {
    try {
      var pos = JSON.parse(saved);
      legend.style.right = 'auto';
      legend.style.left = pos.x + 'px';
      legend.style.top = pos.y + 'px';
    } catch(e) {}
  }

  handle.onmousedown = function(e) {
    if (e.target.closest('.cal-close')) return;
    e.preventDefault();
    _legendDragState.dragging = true;
    _legendDragState.startX = e.clientX;
    _legendDragState.startY = e.clientY;
    var rect = legend.getBoundingClientRect();
    _legendDragState.origX = rect.left;
    _legendDragState.origY = rect.top;
    legend.classList.add('dragging');
    document.body.style.cursor = 'grabbing';
    document.body.style.userSelect = 'none';
  };

  handle.ontouchstart = function(e) {
    if (e.target.closest('.cal-close')) return;
    var t = e.touches[0];
    _legendDragState.dragging = true;
    _legendDragState.startX = t.clientX;
    _legendDragState.startY = t.clientY;
    var rect = legend.getBoundingClientRect();
    _legendDragState.origX = rect.left;
    _legendDragState.origY = rect.top;
    legend.classList.add('dragging');
  };
}

document.addEventListener('mousemove', function(e) {
  if (!_legendDragState.dragging) return;
  var legend = document.getElementById('annotations-legend');
  if (!legend) return;
  var parent = legend.parentElement.getBoundingClientRect();
  var dx = e.clientX - _legendDragState.startX;
  var dy = e.clientY - _legendDragState.startY;
  var newX = Math.max(0, Math.min(parent.width - legend.offsetWidth, _legendDragState.origX - parent.left + dx));
  var newY = Math.max(0, Math.min(parent.height - legend.offsetHeight, _legendDragState.origY - parent.top + dy));
  legend.style.right = 'auto';
  legend.style.left = newX + 'px';
  legend.style.top = newY + 'px';
});

document.addEventListener('touchmove', function(e) {
  if (!_legendDragState.dragging) return;
  var legend = document.getElementById('annotations-legend');
  if (!legend) return;
  var t = e.touches[0];
  var parent = legend.parentElement.getBoundingClientRect();
  var dx = t.clientX - _legendDragState.startX;
  var dy = t.clientY - _legendDragState.startY;
  var newX = Math.max(0, Math.min(parent.width - legend.offsetWidth, _legendDragState.origX - parent.left + dx));
  var newY = Math.max(0, Math.min(parent.height - legend.offsetHeight, _legendDragState.origY - parent.top + dy));
  legend.style.right = 'auto';
  legend.style.left = newX + 'px';
  legend.style.top = newY + 'px';
}, {passive: true});

function _finishLegendDrag() {
  if (!_legendDragState.dragging) return;
  _legendDragState.dragging = false;
  var legend = document.getElementById('annotations-legend');
  if (legend) {
    legend.classList.remove('dragging');
    sessionStorage.setItem('cal_pos', JSON.stringify({ x: parseInt(legend.style.left) || 10, y: parseInt(legend.style.top) || 10 }));
  }
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
}
document.addEventListener('mouseup', _finishLegendDrag);
document.addEventListener('touchend', _finishLegendDrag);

/* ── Drawing Tools Engine ─────────────────────────────────── */
var _drawTool = null;
var _userDrawings = [];
var _drawPending = [];
var _drawCanvas = null;
var _drawCtx = null;

function _initDrawCanvas() {
  _drawCanvas = document.getElementById('draw-canvas');
  if (!_drawCanvas) return;
  _drawCtx = _drawCanvas.getContext('2d');
  _resizeDrawCanvas();
}

function _resizeDrawCanvas() {
  if (!_drawCanvas) return;
  var mc = document.getElementById('main-chart');
  if (!mc || mc.clientWidth === 0 || mc.clientHeight === 0) return;
  _drawCanvas.width = mc.clientWidth;
  _drawCanvas.height = mc.clientHeight;
  _drawCanvas.style.left = mc.offsetLeft + 'px';
  _drawCanvas.style.top = mc.offsetTop + 'px';
  _drawCanvas.style.width = mc.clientWidth + 'px';
  _drawCanvas.style.height = mc.clientHeight + 'px';
  _redrawAll();
}

function _setDrawTool(tool) {
  if (_drawTool === tool) { _drawTool = null; } else { _drawTool = tool; }
  _drawPending = [];
  document.querySelectorAll('.draw-btn[data-tool]').forEach(function(b) {
    b.classList.toggle('active', b.dataset.tool === _drawTool);
  });
  var canvas = document.getElementById('draw-canvas');
  if (canvas) {
    canvas.classList.toggle('drawing', !!_drawTool);
    canvas.style.cursor = _drawTool === 'eraser' ? 'pointer' : (_drawTool ? 'crosshair' : 'default');
  }
}

document.getElementById('draw-toolbar').addEventListener('click', function(e) {
  var btn = e.target.closest('.draw-btn');
  if (!btn) return;
  if (btn.id === 'draw-clear-btn') { _clearAllDrawings(); return; }
  var tool = btn.dataset.tool;
  if (tool) _setDrawTool(tool);
});

function _pixelToChart(px, py) {
  if (!chart || !candleSeries) return null;
  var t = chart.timeScale().coordinateToTime(px);
  var p = candleSeries.coordinateToPrice(py);
  if (t == null || p == null) return null;
  return {time: t, price: p};
}

function _chartToPixel(time, price) {
  if (!chart || !candleSeries) return null;
  var x = chart.timeScale().timeToCoordinate(time);
  var y = candleSeries.priceToCoordinate(price);
  if (x == null || y == null) return null;
  return {x: x, y: y};
}

function _drawLineOnCanvas(ctx, x1, y1, x2, y2, color, width, dashed) {
  ctx.beginPath();
  ctx.strokeStyle = color || '#818cf8';
  ctx.lineWidth = width || 1.5;
  if (dashed) ctx.setLineDash([6, 3]); else ctx.setLineDash([]);
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
  ctx.setLineDash([]);
}

function _drawRectOnCanvas(ctx, x1, y1, x2, y2, color) {
  ctx.fillStyle = (color || 'rgba(99,102,241,0.12)');
  ctx.fillRect(Math.min(x1,x2), Math.min(y1,y2), Math.abs(x2-x1), Math.abs(y2-y1));
  ctx.strokeStyle = color ? color.replace('0.12','0.5') : 'rgba(99,102,241,0.5)';
  ctx.lineWidth = 1;
  ctx.strokeRect(Math.min(x1,x2), Math.min(y1,y2), Math.abs(x2-x1), Math.abs(y2-y1));
}

function _drawTextOnCanvas(ctx, x, y, text, color) {
  ctx.font = '12px Inter, sans-serif';
  ctx.fillStyle = color || '#e2e8f0';
  ctx.fillText(text, x, y);
}

var _FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
var _FIB_COLORS = ['#ef4444','#f59e0b','#eab308','#22c55e','#06b6d4','#3b82f6','#8b5cf6'];

function _drawFibOnCanvas(ctx, high, low, w) {
  var range = high.price - low.price;
  for (var i = 0; i < _FIB_LEVELS.length; i++) {
    var price = low.price + range * _FIB_LEVELS[i];
    var pt = _chartToPixel(low.time, price);
    if (!pt) continue;
    _drawLineOnCanvas(ctx, 0, pt.y, w, pt.y, _FIB_COLORS[i], 1, true);
    ctx.font = '10px Inter, sans-serif';
    ctx.fillStyle = _FIB_COLORS[i];
    ctx.fillText((_FIB_LEVELS[i]*100).toFixed(1) + '% $' + smartPrice(price), 4, pt.y - 3);
  }
}

function _redrawAll() {
  if (!_drawCtx || !_drawCanvas) return;
  _drawCtx.clearRect(0, 0, _drawCanvas.width, _drawCanvas.height);
  var w = _drawCanvas.width;
  _userDrawings.forEach(function(d) { _renderDrawing(_drawCtx, d, w); });
  if (_drawPending.length > 0 && _drawTool) {
    _renderPendingPreview(_drawCtx, w);
  }
  _drawVolProfile();
}

function _renderDrawing(ctx, d, w) {
  if (d.type === 'trendline') {
    var a = _chartToPixel(d.points[0].time, d.points[0].price);
    var b = _chartToPixel(d.points[1].time, d.points[1].price);
    if (a && b) _drawLineOnCanvas(ctx, a.x, a.y, b.x, b.y, d.color, 2);
  } else if (d.type === 'ray') {
    var a = _chartToPixel(d.points[0].time, d.points[0].price);
    var b = _chartToPixel(d.points[1].time, d.points[1].price);
    if (a && b) {
      var dx = b.x - a.x, dy = b.y - a.y;
      var len = Math.sqrt(dx*dx + dy*dy);
      if (len > 0) { var ext = 5000/len; _drawLineOnCanvas(ctx, a.x, a.y, a.x+dx*ext, a.y+dy*ext, d.color, 1.5); }
    }
  } else if (d.type === 'hline') {
    var pt = _chartToPixel(d.points[0].time, d.points[0].price);
    if (pt) {
      _drawLineOnCanvas(ctx, 0, pt.y, w, pt.y, d.color, 1.5, true);
      ctx.font = '10px Inter, sans-serif';
      ctx.fillStyle = d.color || '#818cf8';
      ctx.fillText('$' + smartPrice(d.points[0].price), 4, pt.y - 3);
    }
  } else if (d.type === 'vline') {
    var pt = _chartToPixel(d.points[0].time, d.points[0].price);
    if (pt) {
      _drawLineOnCanvas(ctx, pt.x, 0, pt.x, ctx.canvas.height, d.color, 1.5, true);
    }
  } else if (d.type === 'channel') {
    var a = _chartToPixel(d.points[0].time, d.points[0].price);
    var b = _chartToPixel(d.points[1].time, d.points[1].price);
    if (a && b) {
      var offset = d.channelWidth || 30;
      var dx = b.x - a.x, dy = b.y - a.y;
      var len = Math.sqrt(dx*dx + dy*dy);
      if (len > 0) {
        var nx = -dy/len*offset, ny = dx/len*offset;
        _drawLineOnCanvas(ctx, a.x, a.y, b.x, b.y, d.color, 1.5);
        _drawLineOnCanvas(ctx, a.x+nx, a.y+ny, b.x+nx, b.y+ny, d.color, 1.5);
        ctx.fillStyle = (d.color || '#818cf8').replace(')', ',0.06)').replace('rgb', 'rgba');
        ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y); ctx.lineTo(b.x+nx,b.y+ny); ctx.lineTo(a.x+nx,a.y+ny); ctx.closePath(); ctx.fill();
      }
    }
  } else if (d.type === 'rect') {
    var a = _chartToPixel(d.points[0].time, d.points[0].price);
    var b = _chartToPixel(d.points[1].time, d.points[1].price);
    if (a && b) _drawRectOnCanvas(ctx, a.x, a.y, b.x, b.y, d.color);
  } else if (d.type === 'fib') {
    var high = d.points[0].price > d.points[1].price ? d.points[0] : d.points[1];
    var low = d.points[0].price <= d.points[1].price ? d.points[0] : d.points[1];
    _drawFibOnCanvas(ctx, high, low, w);
  } else if (d.type === 'arrow') {
    var a = _chartToPixel(d.points[0].time, d.points[0].price);
    var b = _chartToPixel(d.points[1].time, d.points[1].price);
    if (a && b) {
      _drawLineOnCanvas(ctx, a.x, a.y, b.x, b.y, d.color, 2);
      var angle = Math.atan2(b.y-a.y, b.x-a.x);
      var hl = 12;
      ctx.beginPath(); ctx.fillStyle = d.color || '#818cf8';
      ctx.moveTo(b.x, b.y);
      ctx.lineTo(b.x - hl*Math.cos(angle-0.4), b.y - hl*Math.sin(angle-0.4));
      ctx.lineTo(b.x - hl*Math.cos(angle+0.4), b.y - hl*Math.sin(angle+0.4));
      ctx.closePath(); ctx.fill();
    }
  } else if (d.type === 'callout') {
    var pt = _chartToPixel(d.points[0].time, d.points[0].price);
    if (pt && d.text) {
      ctx.font = '11px Inter, sans-serif';
      var tw = ctx.measureText(d.text).width;
      var pad = 6, bw = tw + pad*2, bh = 20;
      ctx.fillStyle = 'rgba(99,102,241,.15)';
      ctx.strokeStyle = d.color || '#818cf8';
      ctx.lineWidth = 1;
      ctx.beginPath();
      if (ctx.roundRect) { ctx.roundRect(pt.x, pt.y - bh - 6, bw, bh, 4); }
      else { ctx.rect(pt.x, pt.y - bh - 6, bw, bh); }
      ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(pt.x+10,pt.y-6); ctx.lineTo(pt.x+15,pt.y); ctx.lineTo(pt.x+20,pt.y-6); ctx.fill();
      ctx.fillStyle = d.color || '#e2e8f0';
      ctx.fillText(d.text, pt.x + pad, pt.y - bh + 8);
    }
  } else if (d.type === 'measure') {
    var a = _chartToPixel(d.points[0].time, d.points[0].price);
    var b = _chartToPixel(d.points[1].time, d.points[1].price);
    if (a && b) {
      _drawLineOnCanvas(ctx, a.x, a.y, b.x, b.y, '#f59e0b', 1, true);
      _drawLineOnCanvas(ctx, a.x, a.y, a.x, b.y, '#f59e0b88', 1, true);
      _drawLineOnCanvas(ctx, a.x, b.y, b.x, b.y, '#f59e0b88', 1, true);
      var priceDiff = d.points[1].price - d.points[0].price;
      var pctChange = d.points[0].price !== 0 ? ((priceDiff / d.points[0].price) * 100).toFixed(2) : '0';
      var bars = Math.abs(_chartBarTimes.indexOf(d.points[1].time) - _chartBarTimes.indexOf(d.points[0].time));
      var sign = priceDiff >= 0 ? '+' : '';
      var label = sign + '$' + smartPrice(Math.abs(priceDiff)) + ' (' + sign + pctChange + '%) ' + bars + ' bars';
      var midX = (a.x + b.x) / 2, midY = (a.y + b.y) / 2 - 8;
      ctx.font = '10px Inter, sans-serif';
      var tw = ctx.measureText(label).width;
      ctx.fillStyle = 'rgba(0,0,0,.6)';
      ctx.fillRect(midX - tw/2 - 4, midY - 10, tw + 8, 16);
      ctx.fillStyle = '#f59e0b';
      ctx.fillText(label, midX - tw/2, midY);
    }
  } else if (d.type === 'text') {
    var pt = _chartToPixel(d.points[0].time, d.points[0].price);
    if (pt) _drawTextOnCanvas(ctx, pt.x, pt.y, d.text, d.color);
  }
}

function _renderPendingPreview(ctx, w) {
  if (_drawPending.length === 0) return;
  var twoPointTools = ['trendline','ray','rect','fib','channel','arrow','measure'];
  if (twoPointTools.indexOf(_drawTool) !== -1 && _drawPending.length === 1 && _drawHover) {
    var a = _chartToPixel(_drawPending[0].time, _drawPending[0].price);
    var b = _drawHover;
    if (a && b) {
      if (_drawTool === 'trendline' || _drawTool === 'arrow') _drawLineOnCanvas(ctx, a.x, a.y, b.x, b.y, 'rgba(129,140,248,.5)', 1.5);
      else if (_drawTool === 'ray') {
        var dx = b.x-a.x, dy = b.y-a.y, len=Math.sqrt(dx*dx+dy*dy);
        if(len>0){var ext=5000/len; _drawLineOnCanvas(ctx,a.x,a.y,a.x+dx*ext,a.y+dy*ext,'rgba(129,140,248,.5)',1.5);}
      }
      else if (_drawTool === 'rect') _drawRectOnCanvas(ctx, a.x, a.y, b.x, b.y);
      else if (_drawTool === 'channel') {
        _drawLineOnCanvas(ctx, a.x, a.y, b.x, b.y, 'rgba(129,140,248,.5)', 1.5);
        var dx=b.x-a.x,dy=b.y-a.y,len=Math.sqrt(dx*dx+dy*dy);
        if(len>0){var nx=-dy/len*30,ny=dx/len*30;_drawLineOnCanvas(ctx,a.x+nx,a.y+ny,b.x+nx,b.y+ny,'rgba(129,140,248,.3)',1.5);}
      }
      else if (_drawTool === 'measure') {
        _drawLineOnCanvas(ctx, a.x, a.y, b.x, b.y, 'rgba(245,158,11,.5)', 1, true);
        _drawLineOnCanvas(ctx, a.x, a.y, a.x, b.y, 'rgba(245,158,11,.3)', 1, true);
        _drawLineOnCanvas(ctx, a.x, b.y, b.x, b.y, 'rgba(245,158,11,.3)', 1, true);
      }
      else if (_drawTool === 'fib') {
        var priceA = _drawPending[0].price;
        var priceB = candleSeries.coordinateToPrice(b.y);
        if (priceB != null) {
          var high = {price: Math.max(priceA, priceB), time: _drawPending[0].time};
          var low = {price: Math.min(priceA, priceB), time: _drawPending[0].time};
          _drawFibOnCanvas(ctx, high, low, w);
        }
      }
    }
  }
}

var _drawHover = null;

function _handleDrawCanvasClick(e) {
  if (!_drawTool || !chart || !candleSeries) return;
  var rect = _drawCanvas.getBoundingClientRect();
  var px = e.clientX - rect.left;
  var py = e.clientY - rect.top;

  if (_drawTool === 'eraser') {
    _eraseAt(px, py);
    return;
  }

  var pt = _pixelToChart(px, py);
  if (!pt) return;
  pt = _snapToOHLC(pt);

  if (_drawTool === 'text') {
    var txt = prompt('Enter label text:');
    if (txt) {
      _drawUndoStack = [];
      _userDrawings.push({type:'text', points:[pt], text:txt, color:'#e2e8f0'});
      _saveDrawings();
      _redrawAll();
    }
    return;
  }

  if (_drawTool === 'callout') {
    var txt = prompt('Enter callout text:');
    if (txt) {
      _drawUndoStack = [];
      _userDrawings.push({type:'callout', points:[pt], text:txt, color:'#818cf8'});
      _saveDrawings();
      _redrawAll();
    }
    return;
  }

  if (_drawTool === 'hline') {
    _drawUndoStack = [];
    _userDrawings.push({type:'hline', points:[pt], color:'#818cf8'});
    _saveDrawings();
    _redrawAll();
    return;
  }

  if (_drawTool === 'vline') {
    _drawUndoStack = [];
    _userDrawings.push({type:'vline', points:[pt], color:'#818cf8'});
    _saveDrawings();
    _redrawAll();
    return;
  }

  _drawPending.push(pt);
  if (_drawPending.length >= 2) {
    _drawUndoStack = [];
    var color = '#818cf8';
    if (_drawTool === 'rect') color = 'rgba(99,102,241,0.12)';
    else if (_drawTool === 'measure') color = '#f59e0b';
    else if (_drawTool === 'arrow') color = '#818cf8';
    _userDrawings.push({type:_drawTool, points:[_drawPending[0], _drawPending[1]], color: color});
    _drawPending = [];
    _drawHover = null;
    _saveDrawings();
    _redrawAll();
  }
}

function _handleDrawCanvasMove(e) {
  if (!_drawTool || _drawPending.length === 0) { _drawHover = null; return; }
  var rect = _drawCanvas.getBoundingClientRect();
  _drawHover = {x: e.clientX - rect.left, y: e.clientY - rect.top};
  _redrawAll();
}

function _eraseAt(px, py) {
  var threshold = 12;
  for (var i = _userDrawings.length - 1; i >= 0; i--) {
    var d = _userDrawings[i];
    if (d.type === 'hline') {
      var pt = _chartToPixel(d.points[0].time, d.points[0].price);
      if (pt && Math.abs(pt.y - py) < threshold) { _drawUndoStack = []; _userDrawings.splice(i,1); break; }
    } else if (d.type === 'vline') {
      var pt = _chartToPixel(d.points[0].time, d.points[0].price);
      if (pt && Math.abs(pt.x - px) < threshold) { _drawUndoStack = []; _userDrawings.splice(i,1); break; }
    } else if (d.type === 'text' || d.type === 'callout') {
      var pt = _chartToPixel(d.points[0].time, d.points[0].price);
      if (pt && Math.abs(pt.x - px) < 40 && Math.abs(pt.y - py) < 25) { _drawUndoStack = []; _userDrawings.splice(i,1); break; }
    } else if (d.points && d.points.length === 2) {
      var a = _chartToPixel(d.points[0].time, d.points[0].price);
      var b = _chartToPixel(d.points[1].time, d.points[1].price);
      if (a && b) {
        if (d.type === 'rect') {
          var minX = Math.min(a.x, b.x), maxX = Math.max(a.x, b.x);
          var minY = Math.min(a.y, b.y), maxY = Math.max(a.y, b.y);
          if (px >= minX - threshold && px <= maxX + threshold && py >= minY - threshold && py <= maxY + threshold) {
            _drawUndoStack = []; _userDrawings.splice(i,1); break;
          }
        } else {
          var dist = _pointToLineDistance(px, py, a.x, a.y, b.x, b.y);
          if (dist < threshold) { _drawUndoStack = []; _userDrawings.splice(i,1); break; }
        }
      }
    }
  }
  _saveDrawings();
  _redrawAll();
}

function _pointToLineDistance(px, py, x1, y1, x2, y2) {
  var A = px - x1, B = py - y1, C = x2 - x1, D = y2 - y1;
  var dot = A*C + B*D, len2 = C*C + D*D;
  var t = len2 !== 0 ? Math.max(0, Math.min(1, dot / len2)) : 0;
  var projX = x1 + t*C, projY = y1 + t*D;
  return Math.sqrt((px-projX)*(px-projX) + (py-projY)*(py-projY));
}

function _clearAllDrawings() {
  _userDrawings = [];
  _drawPending = [];
  _drawHover = null;
  _drawUndoStack = [];
  _saveDrawings();
  _redrawAll();
  _setDrawTool(null);
}

function _saveDrawings() {
  if (!currentTicker) return;
  try { sessionStorage.setItem('drawings_' + currentTicker, JSON.stringify(_userDrawings)); } catch(e) {}
}

function _loadDrawings() {
  _userDrawings = [];
  if (!currentTicker) return;
  try {
    var s = sessionStorage.getItem('drawings_' + currentTicker);
    if (s) _userDrawings = JSON.parse(s);
  } catch(e) {}
  _redrawAll();
}

document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) return;
  var k = e.key.toUpperCase();
  if (k === 'R' && e.altKey && !e.ctrlKey && !e.metaKey) { _chartCtxResetView(); e.preventDefault(); return; }
  if (k === 'A' && e.altKey && !e.ctrlKey && !e.metaKey) { _chartCtxAddAlertShortcut(); e.preventDefault(); return; }
  if (k === 'L') { _setDrawTool('trendline'); e.preventDefault(); }
  else if (k === 'Y' && !e.ctrlKey && !e.metaKey && !e.altKey) { _setDrawTool('ray'); e.preventDefault(); }
  else if (k === 'H' && !e.ctrlKey && !e.metaKey && !e.altKey) { _setDrawTool('hline'); e.preventDefault(); }
  else if (k === 'V' && !e.ctrlKey && !e.metaKey && !e.altKey) { _setDrawTool('vline'); e.preventDefault(); }
  else if (k === 'C' && !e.ctrlKey && !e.metaKey && !e.altKey) { _setDrawTool('channel'); e.preventDefault(); }
  else if (k === 'R' && !e.ctrlKey && !e.metaKey && !e.altKey) { _setDrawTool('rect'); e.preventDefault(); }
  else if (k === 'F' && !e.ctrlKey && !e.metaKey && !e.altKey) { _setDrawTool('fib'); e.preventDefault(); }
  else if (k === 'A' && !e.ctrlKey && !e.metaKey && !e.altKey) { _setDrawTool('arrow'); e.preventDefault(); }
  else if (k === 'N' && !e.ctrlKey && !e.metaKey && !e.altKey) { _setDrawTool('callout'); e.preventDefault(); }
  else if (k === 'G' && !e.ctrlKey && !e.metaKey && !e.altKey) { _setDrawTool('measure'); e.preventDefault(); }
  else if (k === 'T' && !e.ctrlKey && !e.metaKey && !e.altKey) { _setDrawTool('text'); e.preventDefault(); }
  else if (k === 'S' && !e.ctrlKey && !e.metaKey && !e.altKey) { _toggleMagnet(); e.preventDefault(); }
  else if (k === 'E') { _setDrawTool('eraser'); e.preventDefault(); }
  else if (k === 'X') { _clearAllDrawings(); e.preventDefault(); }
  else if (k === 'ESCAPE') {
    _setDrawTool(null);
    _closeChartCtxMenu();
    _closeChartTableModal();
    _closeChartObjModal();
    _closeChartSettingsModal();
  }
  else if (k === 'Z' && (e.ctrlKey || e.metaKey) && !e.shiftKey) { _undoDraw(); e.preventDefault(); }
  else if ((k === 'Y' && (e.ctrlKey || e.metaKey)) || (k === 'Z' && (e.ctrlKey || e.metaKey) && e.shiftKey)) { _redoDraw(); e.preventDefault(); }
  else if (k === 'DELETE' || k === 'BACKSPACE') { if (_drawTool === 'eraser') e.preventDefault(); }
});

function _initDrawingListeners() {
  _initDrawCanvas();
  var canvas = document.getElementById('draw-canvas');
  if (!canvas) return;
  canvas.addEventListener('click', _handleDrawCanvasClick);
  canvas.addEventListener('mousemove', _handleDrawCanvasMove);
  canvas.addEventListener('contextmenu', function(e) {
    e.preventDefault();
    _setDrawTool(null);
  });
  if (chart) {
    chart.timeScale().subscribeVisibleTimeRangeChange(function() { _redrawAll(); });
  }
  new ResizeObserver(function() { _resizeDrawCanvas(); }).observe(document.getElementById('main-chart'));
}

/** Strip AI ```json:chart_levels ... ``` fence from chat display (safe while streaming). */
function _stripChartLevelsBlockDisplay(text) {
  if (!text) return '';
  var fence = '```json:chart_levels';
  var start = text.indexOf(fence);
  if (start === -1) return text;
  var afterFence = text.slice(start + fence.length);
  var endMark = afterFence.search(/\n```/);
  if (endMark !== -1) {
    return (text.slice(0, start) + afterFence.slice(endMark + 4)).replace(/\s+$/, '').trim();
  }
  return text.slice(0, start).replace(/\s+$/, '').trim();
}

/** Strip AI ```json:trade_plan_levels ... ``` fence (saved via Apply button / API). */
function _stripTradePlanLevelsBlockDisplay(text) {
  if (!text) return '';
  var fence = '```json:trade_plan_levels';
  var start = text.indexOf(fence);
  if (start === -1) return text;
  var afterFence = text.slice(start + fence.length);
  var endMark = afterFence.search(/\n```/);
  if (endMark !== -1) {
    return (text.slice(0, start) + afterFence.slice(endMark + 4)).replace(/\s+$/, '').trim();
  }
  return text.slice(0, start).replace(/\s+$/, '').trim();
}

function _stripAiAssistantStructuredBlocks(text) {
  return _stripTradePlanLevelsBlockDisplay(_stripChartLevelsBlockDisplay(text || ''));
}

function _parseTradePlanLevelsFromText(text) {
  var m = (text || '').match(/```json:trade_plan_levels\s*\n(\{[\s\S]*?\})\s*```/);
  if (!m) return null;
  try {
    var o = JSON.parse(m[1]);
    if (!o || typeof o !== 'object') return null;
    if (o.stop_loss == null && o.take_profit == null) return null;
    return o;
  } catch (e) { return null; }
}

function _findOpenTradeIdForTicker(ticker) {
  if (!_allTrades || !ticker) return null;
  var u = String(ticker).toUpperCase().replace(/^\$/, '');
  for (var i = 0; i < _allTrades.length; i++) {
    var t = _allTrades[i];
    if (t.status === 'open' && t.ticker && String(t.ticker).toUpperCase().replace(/^\$/, '') === u) return t.id;
  }
  return null;
}

function _getOpenTradeCurrentPrice(tradeId) {
  if (!_allTrades || !tradeId) return null;
  for (var i = 0; i < _allTrades.length; i++) {
    if (_allTrades[i].id === tradeId && _allTrades[i].current_price != null) {
      return Number(_allTrades[i].current_price);
    }
  }
  return null;
}

function _attachPatternImminentButton(bodyEl, payload) {
  if (!bodyEl || !payload || CHILI_TRADING_IS_GUEST) return;
  var prevP = bodyEl.querySelector('.pattern-imminent-attach-wrap');
  if (prevP) prevP.remove();
  var wrap = document.createElement('div');
  wrap.className = 'pattern-imminent-attach-wrap';
  wrap.style.cssText = 'margin-top:10px;padding:8px;border:1px solid var(--border);border-radius:8px;background:var(--bg);';
  if (payload.already_linked) {
    wrap.innerHTML = '<span style="font-size:11px;color:var(--text-muted);">Pattern alert is already linked to your open position — Monitor can track health.</span>';
    bodyEl.appendChild(wrap);
    return;
  }
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn-confirm';
  btn.style.cssText = 'font-size:11px;padding:6px 12px;margin-right:8px;';
  btn.textContent = 'Apply pattern to trade & sync monitor';
  btn.onclick = function() { attachPatternImminentToTrade(payload); };
  var hint = document.createElement('span');
  hint.style.cssText = 'font-size:10px;color:var(--text-muted);';
  var pn = payload.pattern_name ? String(payload.pattern_name) : 'this pattern';
  hint.textContent = 'Links CHILI imminent alert (“' + pn + '”) to your open lot for Monitor scoring.';
  wrap.appendChild(btn);
  wrap.appendChild(hint);
  bodyEl.appendChild(wrap);
}

function attachPatternImminentToTrade(payload) {
  if (CHILI_TRADING_IS_GUEST) { alert('Sign in to link pattern'); return; }
  if (!payload || !payload.alert_id) return;
  fetch('/api/trading/breakout-alerts/' + payload.alert_id + '/attach-to-open-trade', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: '{}'
  }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); }).then(function(res) {
    if (!res.d || !res.d.ok) {
      alert(res.d && res.d.error ? res.d.error : 'Could not link alert to trade');
      return;
    }
    loadTrades();
    _refreshMonitorIfLoaded();
    appendAiMsg('assistant', 'Linked pattern alert to trade #' + res.d.id + '. Check the Monitor tab for pattern health.');
  }).catch(function() { alert('Request failed'); });
}

function _attachTradePlanApplyButton(bodyEl, payload, tickerFallback) {
  if (!bodyEl || !payload || CHILI_TRADING_IS_GUEST) return;
  if (payload.stop_loss == null && payload.take_profit == null) return;
  var prev = bodyEl.querySelector('.trade-plan-apply-wrap');
  if (prev) prev.remove();
  var wrap = document.createElement('div');
  wrap.className = 'trade-plan-apply-wrap';
  wrap.style.cssText = 'margin-top:10px;padding:8px;border:1px solid var(--border);border-radius:8px;background:var(--bg);';
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn-confirm';
  btn.style.cssText = 'font-size:11px;padding:6px 12px;margin-right:8px;';
  btn.textContent = 'Apply stop/target to open position (Monitor)';
  btn.onclick = function() {
    var p = Object.assign({}, payload, { ticker: payload.ticker || tickerFallback });
    applyAiTradePlanLevels(p);
  };
  var hint = document.createElement('span');
  hint.style.cssText = 'font-size:10px;color:var(--text-muted);';
  hint.textContent = 'Saves levels on your trade row; Monitor shows Stop/Target. Pattern health needs a linked scan pattern or monitor run.';
  wrap.appendChild(btn);
  wrap.appendChild(hint);
  bodyEl.appendChild(wrap);
}

function applyAiTradePlanLevels(payload) {
  if (CHILI_TRADING_IS_GUEST) { alert('Sign in to apply levels'); return; }
  var tid = payload.trade_id;
  if (!tid) tid = _findOpenTradeIdForTicker(payload.ticker || currentTicker);
  if (!tid) {
    alert('No open trade found for this ticker. Open the Trades tab or sync your broker.');
    return;
  }
  var body = {};
  if (payload.stop_loss != null && isFinite(Number(payload.stop_loss))) body.stop_loss = Number(payload.stop_loss);
  if (payload.take_profit != null && isFinite(Number(payload.take_profit))) body.take_profit = Number(payload.take_profit);
  if (payload.take_profit_trim != null && isFinite(Number(payload.take_profit_trim))) body.take_profit_trim = Number(payload.take_profit_trim);
  if (payload.label) body.note = String(payload.label);
  if (payload.verdict) body.verdict = String(payload.verdict);
  if (payload.confidence != null && isFinite(Number(payload.confidence))) body.confidence = Number(payload.confidence);
  var tradePrice = _getOpenTradeCurrentPrice(tid);
  if (tradePrice != null) body.price_at_decision = tradePrice;
  if (!body.stop_loss && !body.take_profit) {
    alert('Nothing to apply (need stop and/or take profit).');
    return;
  }
  fetch('/api/trading/trades/' + tid + '/apply-levels', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(body)
  }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); }).then(function(res) {
    if (!res.d || !res.d.ok) {
      alert(res.d && res.d.error ? res.d.error : 'Could not apply levels');
      return;
    }
    loadTrades();
    _refreshMonitorIfLoaded();
    appendAiMsg('assistant', 'Saved stop/target on trade #' + tid + '. Check the Monitor tab for updated Stop and Target.');
  }).catch(function() { alert('Request failed'); });
}

/** Best-effort parse of entry/stop/targets from prose when no structured block. */
function _parseChartLevelsFallback(text) {
  if (!text) return null;
  var out = {};
  function grab(re) {
    var m = text.match(re);
    if (!m || m[1] == null) return null;
    var v = parseFloat(String(m[1]).replace(/,/g, ''));
    return isFinite(v) ? v : null;
  }
  var entry = grab(/(?:Entry zone|Entry)\s*(?:\([^)]*\))?\s*:\s*\$?\s*([\d,.]+)/i);
  if (entry == null) {
    var ez = text.match(/Entry zone\s*:\s*\$?\s*([\d,.]+)\s*[-–]\s*\$?\s*([\d,.]+)/i);
    if (ez) {
      var a = parseFloat(ez[1].replace(/,/g, ''));
      var b = parseFloat(ez[2].replace(/,/g, ''));
      if (isFinite(a) && isFinite(b)) entry = (a + b) / 2;
    }
  }
  if (entry != null) out.entry = entry;
  var stop = grab(/(?:Stop-loss|Stop loss|Stop)\s*:\s*\$?\s*([\d,.]+)/i);
  if (stop != null) out.stop = stop;
  var targets = [];
  var reT = /Target\s*(?:\d+|one|two|three|1|2|3)?\s*:\s*\$?\s*([\d,.]+)/gi;
  var mt;
  while ((mt = reT.exec(text)) !== null) {
    var tv = parseFloat(mt[1].replace(/,/g, ''));
    if (isFinite(tv)) targets.push(tv);
  }
  if (targets.length) out.targets = targets;
  var sup = [];
  var reS = /(?:Support|S\s*\d)\s*:\s*\$?\s*([\d,.]+)/gi;
  while ((mt = reS.exec(text)) !== null) {
    var sv = parseFloat(mt[1].replace(/,/g, ''));
    if (isFinite(sv)) sup.push(sv);
  }
  var res = [];
  var reR = /(?:Resistance|R\s*\d)\s*:\s*\$?\s*([\d,.]+)/gi;
  while ((mt = reR.exec(text)) !== null) {
    var rv = parseFloat(mt[1].replace(/,/g, ''));
    if (isFinite(rv)) res.push(rv);
  }
  if (sup.length) out.support = sup;
  if (res.length) out.resistance = res;
  var sma20 = grab(/SMA\s*20\s*(?:\([^)]*\))?\s*[:\s]+\$?\s*([\d,.]+)/i);
  if (sma20 != null) out.sma_20 = sma20;
  var sma50 = grab(/SMA\s*50\s*(?:\([^)]*\))?\s*[:\s]+\$?\s*([\d,.]+)/i);
  if (sma50 != null) out.sma_50 = sma50;
  var sma200 = grab(/SMA\s*200\s*(?:\([^)]*\))?\s*[:\s]+\$?\s*([\d,.]+)/i);
  if (sma200 != null) out.sma_200 = sma200;
  var vwap = grab(/VWAP\s*:\s*\$?\s*([\d,.]+)/i);
  if (vwap != null) out.vwap = vwap;
  return Object.keys(out).length ? out : null;
}

/** Draw horizontal levels from AI Analyze structured output or fallback parse. */
function drawAiAnnotations(levels) {
  if (!levels || !candleSeries) return;
  clearAnnotations();
  var cr = (currentTicker || '').indexOf('-USD') !== -1;
  var sp = function(v) { return smartPrice(v, cr); };
  var legendItems = [];

  if (levels.entry != null && isFinite(Number(levels.entry))) {
    _addPriceLine(Number(levels.entry), '#3b82f6', 'AI Entry $' + sp(levels.entry), LightweightCharts.LineStyle.Solid, 2);
    legendItems.push({ color: '#3b82f6', label: 'AI Entry $' + sp(levels.entry) });
  }
  if (levels.stop != null && isFinite(Number(levels.stop))) {
    _addPriceLine(Number(levels.stop), '#ef4444', 'AI Stop $' + sp(levels.stop), LightweightCharts.LineStyle.Dotted, 2);
    legendItems.push({ color: '#ef4444', label: 'AI Stop $' + sp(levels.stop), dashed: true });
  }
  (levels.targets || []).forEach(function(t, i) {
    if (t == null || !isFinite(Number(t))) return;
    _addPriceLine(Number(t), '#22c55e', 'AI T' + (i + 1) + ' $' + sp(t), LightweightCharts.LineStyle.Dashed, 1);
    legendItems.push({ color: '#22c55e', label: 'AI Target ' + (i + 1) + ' $' + sp(t), dashed: true });
  });
  (levels.support || []).forEach(function(s, i) {
    if (s == null || !isFinite(Number(s))) return;
    _addPriceLine(Number(s), '#06b6d4', 'AI S' + (i + 1) + ' $' + sp(s), LightweightCharts.LineStyle.Dashed, 1);
    legendItems.push({ color: '#06b6d4', label: 'AI Support ' + (i + 1) + ' $' + sp(s), dashed: true });
  });
  (levels.resistance || []).forEach(function(r, i) {
    if (r == null || !isFinite(Number(r))) return;
    _addPriceLine(Number(r), '#f59e0b', 'AI R' + (i + 1) + ' $' + sp(r), LightweightCharts.LineStyle.Dashed, 1);
    legendItems.push({ color: '#f59e0b', label: 'AI Resist ' + (i + 1) + ' $' + sp(r), dashed: true });
  });
  if (levels.sma_20 != null && isFinite(Number(levels.sma_20))) {
    _addPriceLine(Number(levels.sma_20), '#a855f7', 'AI SMA 20', LightweightCharts.LineStyle.Solid, 1);
    legendItems.push({ color: '#a855f7', label: 'AI SMA 20' });
  }
  if (levels.sma_50 != null && isFinite(Number(levels.sma_50))) {
    _addPriceLine(Number(levels.sma_50), '#ec4899', 'AI SMA 50', LightweightCharts.LineStyle.Solid, 1);
    legendItems.push({ color: '#ec4899', label: 'AI SMA 50' });
  }
  if (levels.sma_200 != null && isFinite(Number(levels.sma_200))) {
    _addPriceLine(Number(levels.sma_200), '#8b5cf6', 'AI SMA 200', LightweightCharts.LineStyle.Solid, 1);
    legendItems.push({ color: '#8b5cf6', label: 'AI SMA 200' });
  }
  if (levels.vwap != null && isFinite(Number(levels.vwap))) {
    _addPriceLine(Number(levels.vwap), '#e11d48', 'AI VWAP', LightweightCharts.LineStyle.LargeDashed, 2);
    legendItems.push({ color: '#e11d48', label: 'AI VWAP', dashed: true });
  }
  if (legendItems.length) _showAnnotationLegend(legendItems);
}

function drawBreakoutAnnotations(r) {
  clearAnnotations();
  var cr = (r.ticker||currentTicker||'').indexOf('-USD') !== -1;
  var sp = function(v){ return smartPrice(v, cr); };
  _addPriceLine(r.resistance, '#22c55e', 'Resistance $'+sp(r.resistance), LightweightCharts.LineStyle.Dashed, 2);
  _addPriceLine(r.entry_price, '#3b82f6', 'Entry $'+sp(r.entry_price), LightweightCharts.LineStyle.Solid, 2);
  _addPriceLine(r.stop_loss, '#ef4444', 'Stop $'+sp(r.stop_loss), LightweightCharts.LineStyle.Dotted, 1);
  _addPriceLine(r.take_profit, '#22c55e', 'Target $'+sp(r.take_profit), LightweightCharts.LineStyle.Dotted, 1);
  if (r.indicators && r.indicators.ema_20) _addPriceLine(r.indicators.ema_20, '#f59e0b', 'EMA20', LightweightCharts.LineStyle.Solid, 1);
  if (r.indicators && r.indicators.ema_50) _addPriceLine(r.indicators.ema_50, '#06b6d4', 'EMA50', LightweightCharts.LineStyle.Solid, 1);

  var legendItems = [
    {color:'#22c55e', label:'Resistance $'+sp(r.resistance), dashed:true},
    {color:'#3b82f6', label:'Entry $'+sp(r.entry_price)},
    {color:'#ef4444', label:'Stop $'+sp(r.stop_loss)},
    {color:'#22c55e', label:'Target $'+sp(r.take_profit)},
  ];

  if (r.bb_squeeze) {
    var lastBarTime = _getLastCandleTime();
    if (lastBarTime) {
    candleSeries.setMarkers([{
        time: lastBarTime,
      position: 'aboveBar', color: '#f59e0b', shape: 'arrowDown',
      text: 'BB Squeeze'
    }]);
  }
    legendItems.push({color:'#f59e0b', label:'BB Squeeze active'});
  }

  if (r.hold_estimate) {
    legendItems.push({color:'#a78bfa', label:'Hold: ' + r.hold_estimate});
  }

  _showAnnotationLegend(legendItems);
}

function drawDaytradeAnnotations(r) {
  clearAnnotations();
  var cr = (r.ticker||currentTicker||'').indexOf('-USD') !== -1;
  var sp = function(v){ return smartPrice(v, cr); };
  _addPriceLine(r.entry_price, '#3b82f6', 'Entry $'+sp(r.entry_price), LightweightCharts.LineStyle.Solid, 2);
  _addPriceLine(r.stop_loss, '#ef4444', 'Stop $'+sp(r.stop_loss), LightweightCharts.LineStyle.Dotted, 1);
  _addPriceLine(r.take_profit, '#22c55e', 'Target $'+sp(r.take_profit), LightweightCharts.LineStyle.Dotted, 1);
  if (r.vwap) _addPriceLine(r.vwap, '#e11d48', 'VWAP', LightweightCharts.LineStyle.LargeDashed, 2);

  var legendItems = [
    {color:'#3b82f6', label:'Entry $'+sp(r.entry_price)},
    {color:'#ef4444', label:'Stop $'+sp(r.stop_loss)},
    {color:'#22c55e', label:'Target $'+sp(r.take_profit)},
  ];
  if (r.vwap) legendItems.push({color:'#e11d48', label:'VWAP $'+sp(r.vwap), dashed:true});
  if (r.hold_estimate) legendItems.push({color:'#a78bfa', label:'Hold: ' + r.hold_estimate});
  _showAnnotationLegend(legendItems);
}

/* ── Multi-Timeframe View ──────────────────────── */
function _createMiniChart(elId) {
  var el = document.getElementById(elId);
  if (!el) return null;
  var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  var c = LightweightCharts.createChart(el, {
    width: el.clientWidth, height: el.clientHeight,
    layout: { background: { type: 'solid', color: isDark ? '#111827' : '#f9fafb' }, textColor: isDark ? '#d1d5db' : '#6b7280' },
    grid: { vertLines: { color: isDark ? '#1f293766' : '#f3f4f666' }, horzLines: { color: isDark ? '#1f293766' : '#f3f4f666' } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.1, bottom: 0.1 } },
    timeScale: { borderVisible: false, timeVisible: true },
    handleScroll: false, handleScale: false,
  });
  var cs = c.addCandlestickSeries({
    upColor: '#22c55e', downColor: '#ef4444',
    borderUpColor: '#22c55e', borderDownColor: '#ef4444',
    wickUpColor: '#22c55e', wickDownColor: '#ef4444',
  });
  new ResizeObserver(function() { c.applyOptions({width: el.clientWidth, height: el.clientHeight}); }).observe(el);
  return {chart: c, series: cs};
}

function _initMultiCharts() {
  _multiTimeframes.forEach(function(tf) {
    var key = 'mc-' + tf;
    if (!_multiCharts[tf]) {
      _multiCharts[tf] = _createMiniChart(key);
    }
  });
}

function _setupCrosshairSync() {
  _multiTimeframes.forEach(function(srcTf) {
    var src = _multiCharts[srcTf];
    if (!src) return;
    src.chart.subscribeCrosshairMove(function(param) {
      _multiTimeframes.forEach(function(dstTf) {
        if (dstTf === srcTf) return;
        var dst = _multiCharts[dstTf];
        if (!dst) return;
        if (!param || !param.time) { dst.chart.clearCrosshairPosition(); return; }
        try {
          dst.chart.setCrosshairPosition(param.seriesData.get(src.series) || 0, param.time, dst.series);
        } catch(e) { /* time not available in dst chart */ }
      });
    });
  });
}

function loadMultiCharts(ticker) {
  _initMultiCharts();
  var fetches = _multiTimeframes.map(function(tf) {
    var period = _multiPeriods[tf] || '6mo';
    return fetch('/api/trading/ohlcv?ticker='+encodeURIComponent(ticker)+'&interval='+tf+'&period='+period)
      .then(function(r){return r.json();}).then(function(d) { return {tf:tf, data:d}; })
      .catch(function(){return {tf:tf, data:{ok:false}};});
  });
  Promise.all(fetches).then(function(results) {
    results.forEach(function(r) {
      var mc = _multiCharts[r.tf];
      if (!mc) return;
      if (r.data.ok && r.data.data && r.data.data.length) {
        mc.series.setData(r.data.data.map(function(c){return {time:c.time,open:c.open,high:c.high,low:c.low,close:c.close};}));
        mc.chart.timeScale().fitContent();
      } else { mc.series.setData([]); }
    });
    _setupCrosshairSync();
  });
}

function toggleMultiView() {
  _multiViewActive = !_multiViewActive;
  var mainChart = document.getElementById('main-chart');
  var grid = document.getElementById('multi-chart-grid');
  var btn = document.getElementById('btn-multiview');
  var drawToolbar = document.getElementById('draw-toolbar');
  var drawCanvas = document.getElementById('draw-canvas');
  if (_multiViewActive) {
    mainChart.style.display = 'none';
    grid.classList.remove('hidden');
    btn.classList.add('active');
    if (drawToolbar) drawToolbar.style.display = 'none';
    if (drawCanvas) drawCanvas.style.display = 'none';
    loadMultiCharts(currentTicker);
  } else {
    mainChart.style.display = '';
    grid.classList.add('hidden');
    btn.classList.remove('active');
    if (drawToolbar) drawToolbar.style.display = '';
    if (drawCanvas) drawCanvas.style.display = '';
    if (chart) { chart.applyOptions({width: mainChart.clientWidth, height: mainChart.clientHeight}); }
    _resizeDrawCanvas();
  }
}

function exitMultiView(interval) {
  _multiViewActive = false;
  document.getElementById('main-chart').style.display = '';
  document.getElementById('multi-chart-grid').classList.add('hidden');
  document.getElementById('btn-multiview').classList.remove('active');
  var drawToolbar = document.getElementById('draw-toolbar');
  var drawCanvas = document.getElementById('draw-canvas');
  if (drawToolbar) drawToolbar.style.display = '';
  if (drawCanvas) drawCanvas.style.display = '';
  changeInterval(interval);
  _resizeDrawCanvas();
}

/* ── Skeleton Rows ─────────────────────────────── */
function _injectSkeletons(container, count, cols) {
  var html = '';
  for (var i = 0; i < (count || 5); i++) {
    html += '<div class="skeleton-row" style="grid-template-columns:'+(cols||'80px 60px 45px 50px 60px 60px 1fr')+'">';
    for (var j = 0; j < (cols ? cols.split(' ').length : 7); j++) {
      html += '<div class="skeleton-bar"></div>';
    }
    html += '</div>';
  }
  container.innerHTML = html;
}

/* ── Mini Sparkline SVG ────────────────────────── */
function _sparklineSvg(prices, w, h) {
  if (!prices || prices.length < 2) return '';
  var mn = Math.min.apply(null, prices), mx = Math.max.apply(null, prices);
  var range = mx - mn || 1;
  var step = w / (prices.length - 1);
  var pts = prices.map(function(p, i) { return (i * step).toFixed(1) + ',' + (h - ((p - mn) / range) * h).toFixed(1); });
  var color = prices[prices.length-1] >= prices[0] ? '#22c55e' : '#ef4444';
  return '<svg class="mini-spark" width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'"><polyline fill="none" stroke="'+color+'" stroke-width="1.2" points="'+pts.join(' ')+'"/></svg>';
}

/* ── Fullscreen ───────────────────────────────── */
var _chartFullscreen = false;
function toggleFullscreen() {
  _chartFullscreen = !_chartFullscreen;
  var wrapper = document.querySelector('.t-main');
  var btn = document.getElementById('btn-fullscreen');
  if (!wrapper) return;
  if (_chartFullscreen) {
    wrapper.classList.add('chart-fullscreen');
    btn.classList.add('active');
    btn.innerHTML = '&#x2716;';
    btn.title = 'Exit fullscreen (F11)';
  } else {
    wrapper.classList.remove('chart-fullscreen');
    btn.classList.remove('active');
    btn.innerHTML = '&#x26F6;';
    btn.title = 'Fullscreen (F11)';
  }
  var mc = document.getElementById('main-chart');
  if (chart && mc) {
    setTimeout(function() {
      chart.applyOptions({width: mc.clientWidth, height: mc.clientHeight});
      chart.timeScale().fitContent();
      _resizeDrawCanvas();
    }, 50);
  }
}

/* ── Chart Screenshot ─────────────────────────── */
function chartScreenshot() {
  if (!chart) return;
  try {
    var mainEl = document.getElementById('main-chart');
    var canvas = mainEl.querySelector('canvas');
    if (!canvas) { alert('No chart canvas found.'); return; }
    chart.takeScreenshot().toBlob(function(blob) {
      if (!blob) return;
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = (currentTicker || 'chart') + '_' + currentInterval + '_' + new Date().toISOString().slice(0,10) + '.png';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function() { URL.revokeObjectURL(url); }, 5000);
    });
  } catch(e) {
    console.error('[chartScreenshot]', e);
    alert('Screenshot failed: ' + e.message);
  }
}

/* ── Keyboard Shortcuts ────────────────────────── */
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    var _camOv = document.getElementById('chart-alert-modal');
    if (_camOv && _camOv.classList.contains('active')) {
      _closeChartAlertModal();
      e.preventDefault();
      return;
    }
    var _uapCtx = document.getElementById('chart-alert-ctx-menu');
    if (_uapCtx && !_uapCtx.classList.contains('hidden')) {
      _closeChartAlertCtxMenu();
      e.preventDefault();
      return;
    }
  }
  var tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;

  if (e.key === 'Escape') {
    if (_chartFullscreen) { toggleFullscreen(); return; }
    closeAllDrawers();
    clearAnnotations();
    return;
  }
  if (e.key === 'F11') { toggleFullscreen(); e.preventDefault(); return; }
  if (e.key === '/') { openToolbarSearch(); e.preventDefault(); return; }
  if (e.key === 'm' || e.key === 'M') { toggleMultiView(); e.preventDefault(); return; }

  // 1-4 for quick interval switch
  var intervalMap = {'1':'1d','2':'1h','3':'15m','4':'1wk'};
  if (intervalMap[e.key]) {
    changeInterval(intervalMap[e.key]);
    e.preventDefault();
    return;
  }

  // Arrow keys navigate watchlist
  if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
    var wlItems = Array.from(document.querySelectorAll('.wl-item'));
    if (!wlItems.length) return;
    var idx = wlItems.findIndex(function(el) { return el.classList.contains('active'); });
    if (e.key === 'ArrowUp') idx = Math.max(0, idx - 1);
    else idx = Math.min(wlItems.length - 1, idx + 1);
    var ticker = wlItems[idx].dataset.ticker;
    if (ticker) selectTicker(ticker);
    wlItems[idx].scrollIntoView({block:'nearest'});
    e.preventDefault();
  }
});

/* ── Theme ──────────────────────────────────────── */
function toggleTheme() {
  var cur = document.documentElement.getAttribute('data-theme');
  var next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('chili-theme', next);
  var isDark = next === 'dark';
  var themeOpts = {
    layout: { background: { type: 'solid', color: isDark ? '#111827' : '#f9fafb' }, textColor: isDark ? '#f3f4f6' : '#1f2937' },
    grid: { vertLines: { color: isDark ? '#1f293744' : '#e5e7eb44' }, horzLines: { color: isDark ? '#1f293744' : '#e5e7eb44' } },
    crosshair: {
      vertLine: { labelBackgroundColor: isDark ? '#6366f1' : '#4f46e5' },
      horzLine: { labelBackgroundColor: isDark ? '#6366f1' : '#4f46e5' },
    },
    watermark: { color: isDark ? 'rgba(255,255,255,.04)' : 'rgba(0,0,0,.03)' },
  };
  if (chart) chart.applyOptions(themeOpts);
  Object.keys(_multiCharts).forEach(function(tf) {
    if (_multiCharts[tf] && _multiCharts[tf].chart) {
      _multiCharts[tf].chart.applyOptions({
        layout: { background: { type: 'solid', color: isDark ? '#111827' : '#f9fafb' }, textColor: isDark ? '#d1d5db' : '#6b7280' },
        grid: { vertLines: { color: isDark ? '#1f293766' : '#f3f4f666' }, horzLines: { color: isDark ? '#1f293766' : '#f3f4f666' } },
      });
    }
  });
}
