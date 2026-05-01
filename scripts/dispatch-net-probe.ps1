# Network connectivity probe — what crypto OHLCV endpoints actually work?
# (PowerShell-safe: avoid $host (reserved); use $hn for hostname.)
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "scripts/dispatch-net-probe-output.txt"
"# net probe $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"---DNS + TCP via single python script---" | Add-Content $out
$pyScript = @'
import socket
import requests

DNS_HOSTS = [
    "api.exchange.coinbase.com",
    "api.coinbase.com",
    "advanced-trade-ws.coinbase.com",
    "api.kraken.com",
    "api.coingecko.com",
    "api.binance.us",
    "www.bitstamp.net",
]

print("=== DNS resolution ===")
for h in DNS_HOSTS:
    try:
        ip = socket.gethostbyname(h)
        print(f"DNS OK : {h} -> {ip}")
    except Exception as e:
        print(f"DNS FAIL: {h}: {e}")

print()
print("=== TCP:443 connect (5s timeout) ===")
for h in DNS_HOSTS:
    try:
        s = socket.create_connection((h, 443), timeout=5)
        s.close()
        print(f"TCP OK : {h}:443")
    except Exception as e:
        print(f"TCP FAIL: {h}:443: {e}")

print()
print("=== HTTPS GET (5s timeout) ===")
ENDPOINTS = [
    "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400",
    "https://api.coinbase.com/api/v3/brokerage/products/BTC-USD/ticker",
    "https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440",
    "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=30",
    "https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit=30",
]
for url in ENDPOINTS:
    try:
        r = requests.get(url, timeout=5)
        body = len(r.text) if r.text else 0
        print(f"OK [{r.status_code}] {body}B: {url[:78]}")
    except requests.exceptions.ConnectionError as e:
        msg = str(e)[:140]
        print(f"CONN_ERR: {url[:78]}: {msg}")
    except requests.exceptions.Timeout:
        print(f"TIMEOUT : {url[:78]}")
    except Exception as e:
        print(f"OTHER {type(e).__name__}: {url[:78]}: {e}")
'@

$pyScript | docker compose exec -T brain-worker python -c "import sys; exec(sys.stdin.read())" 2>&1 | Add-Content $out

Write-Output "done"
