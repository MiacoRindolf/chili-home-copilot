# RESEARCH: Coinbase universe scan + simple alpha replay.
#
# Goal: prove or kill the hypothesis that mid-tier Coinbase pairs (rank
# ~10-50 by 24h volume) produce more 1m-5m forward-return alpha than
# the current 5 majors (BTC/ETH/SOL/AVAX/DOGE).
#
# Method:
#   1. Fetch all USD-quoted SPOT products from Coinbase Exchange API.
#   2. For each, pull /stats (24h volume, last) and /ticker (best
#      bid/ask = current spread).
#   3. Compute composite_score = volume_24h_usd / max(spread_bps, 0.5).
#   4. Print top-30 ranked.
#   5. For 10 candidate names (5 majors as control + 5 mid-tier as
#      treatment), pull 24h of 1m candles. Compute "high-vol bar"
#      events (vol >= 2.0 * mean(vol[-20:])) and forward returns at
#      1m / 5m / 15m horizons.
#   6. Compare event count + mean forward-return + Sharpe-ish ratio
#      across treatment vs control.
#
# Side-effects:
#   - Output to scripts/research-fastpath-universe-2026-05-07-output.txt
#   - No DB writes, no code changes. Pure research.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\research-fastpath-universe-2026-05-07-output.txt"
"# fastpath universe research $(Get-Date -Format o)" | Out-File $out -Encoding utf8

$chili = "chili-home-copilot-chili-1"

"" | Add-Content $out
"## Step 1+2+3: scan Coinbase universe + rank" | Add-Content $out
$q1 = @'
import json, time, urllib.request, urllib.error

BASE = "https://api.exchange.coinbase.com"

def get(path, timeout=10):
    req = urllib.request.Request(BASE + path, headers={"User-Agent":"chili-research/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

# ---------- products ----------
try:
    prods = get("/products")
except Exception as e:
    print("FATAL: /products failed:", repr(e)); raise SystemExit(1)
print(f"total products in API: {len(prods)}")

usd = [p for p in prods
       if p.get("quote_currency") == "USD"
       and not p.get("trading_disabled", False)
       and p.get("status") == "online"]
print(f"USD-quoted, tradeable: {len(usd)}")

# ---------- stats + ticker per product (rate-limited) ----------
rows = []
for i, p in enumerate(usd):
    pid = p["id"]
    try:
        stats = get(f"/products/{pid}/stats")
        tick  = get(f"/products/{pid}/ticker")
    except Exception as e:
        print(f"  [skip] {pid}: {e}")
        continue
    try:
        last = float(tick.get("price") or stats.get("last") or 0)
        vol_base = float(stats.get("volume") or 0)
        bid = float(tick.get("bid") or 0)
        ask = float(tick.get("ask") or 0)
        bid_sz = float(tick.get("size") or 0)  # not exact, but a hint
        if last <= 0 or bid <= 0 or ask <= 0 or vol_base <= 0:
            continue
        vol_usd = vol_base * last
        spread_bps = (ask - bid) / ((ask + bid) / 2.0) * 1e4
        if spread_bps < 0:
            continue
        composite = vol_usd / max(spread_bps, 0.5)
        rows.append({
            "id": pid,
            "last": last,
            "volume_24h_usd": vol_usd,
            "spread_bps": spread_bps,
            "composite": composite,
        })
    except Exception as e:
        continue
    # gentle pacing for the 10-req/s ceiling (we do 2/req, so 5/s)
    if (i+1) % 5 == 0:
        time.sleep(0.4)

rows.sort(key=lambda r: -r["composite"])
print(f"\nproducts with usable stats: {len(rows)}")
print(f"\n--- TOP 30 by composite (volume_usd / spread_bps) ---")
print(f"{'rank':>4}  {'pair':<14} {'last':>14} {'vol_24h_usd':>16} {'spread_bps':>10} {'composite':>14}")
for i, r in enumerate(rows[:30], 1):
    print(f"{i:>4}  {r['id']:<14} {r['last']:>14.6g} {r['volume_24h_usd']:>16,.0f} {r['spread_bps']:>10.2f} {r['composite']:>14,.0f}")

print(f"\n--- ranks 30..60 (mid-tier candidates) ---")
for i, r in enumerate(rows[30:60], 31):
    print(f"{i:>4}  {r['id']:<14} {r['last']:>14.6g} {r['volume_24h_usd']:>16,.0f} {r['spread_bps']:>10.2f} {r['composite']:>14,.0f}")

# ---------- alpha replay on a curated subset ----------
# Control: current 5 pairs
# Treatment: 5 mid-tier candidates (rank 10-40 with spread <= 8 bps)
control = ["BTC-USD","ETH-USD","SOL-USD","AVAX-USD","DOGE-USD"]
treat_pool = [r["id"] for r in rows[5:60] if r["spread_bps"] <= 8.0]
treatment = treat_pool[:10]
print(f"\n--- treatment pool (top mid-tier with spread<=8bps) ---")
for t in treatment: print(" ", t)

# Pull 24h of 1m candles, compute simple alpha test
# Coinbase /products/{id}/candles?granularity=60&start=...&end=...
# Returns max 300 bars per call. 24h = 1440 bars = 5 calls per pair.
import datetime as dt
now = int(time.time())
day = 24 * 3600

def candles_24h(pid):
    # Pull in ~5 hour chunks (300 bars at 60s granularity)
    out = []
    for chunk in range(5):
        end = now - chunk * 5 * 3600
        start = end - 5 * 3600
        try:
            data = get(f"/products/{pid}/candles?granularity=60&start={start}&end={end}")
            # Each row: [time, low, high, open, close, volume]
            out.extend(data)
        except Exception as e:
            print(f"  [{pid}] candles chunk fail: {e}")
        time.sleep(0.25)
    # dedupe+sort by time ascending
    seen = {}
    for row in out:
        seen[row[0]] = row
    return [seen[k] for k in sorted(seen)]

def replay(pid, bars):
    # high-vol event = bar.volume >= 2 * mean(prev 20 bars vol)
    # forward-return at +1, +5, +15 bars (close-to-close)
    if len(bars) < 30:
        return None
    closes = [b[4] for b in bars]
    vols   = [b[5] for b in bars]
    events = []
    for i in range(20, len(bars) - 15):
        ref_vol = sum(vols[i-20:i]) / 20.0
        if ref_vol <= 0:
            continue
        if vols[i] < 2.0 * ref_vol:
            continue
        # use close[i] as entry reference
        c0 = closes[i]
        if c0 <= 0:
            continue
        ret_1  = (closes[i+1]  / c0 - 1.0) * 1e4 if i+1  < len(closes) else None
        ret_5  = (closes[i+5]  / c0 - 1.0) * 1e4 if i+5  < len(closes) else None
        ret_15 = (closes[i+15] / c0 - 1.0) * 1e4 if i+15 < len(closes) else None
        events.append((ret_1, ret_5, ret_15))
    return events

def summarize(events):
    if not events: return None
    def stats(idx):
        xs = [e[idx] for e in events if e[idx] is not None]
        if not xs: return (0, 0.0, 0.0, 0.0)
        n = len(xs); m = sum(xs)/n
        v = sum((x-m)**2 for x in xs)/max(n-1,1)
        sd = v ** 0.5
        sharpe_like = (m / sd) * (n ** 0.5) if sd > 0 else 0.0
        return (n, m, sd, sharpe_like)
    return {"n": len(events),
            "h1m":  stats(0),
            "h5m":  stats(1),
            "h15m": stats(2)}

print(f"\n--- alpha replay (24h 1m bars, vol>=2x mean(20) breakout, fwd-return bps) ---")
print(f"{'pair':<14} {'group':<7} {'evts':>5} | {'1m_n':>4} {'1m_mean':>9} {'1m_shp':>9} | {'5m_n':>4} {'5m_mean':>9} {'5m_shp':>9} | {'15m_n':>4} {'15m_mean':>9} {'15m_shp':>9}")

universe_for_replay = [(t, "ctrl") for t in control] + [(t, "treat") for t in treatment]
results = []
for pid, group in universe_for_replay:
    bars = candles_24h(pid)
    print(f"  [{pid}] bars={len(bars)}")
    evs = replay(pid, bars) or []
    s = summarize(evs)
    if not s:
        continue
    results.append((pid, group, s))

# Print the comparison
for pid, group, s in results:
    h1, h5, h15 = s["h1m"], s["h5m"], s["h15m"]
    print(f"{pid:<14} {group:<7} {s['n']:>5} | "
          f"{h1[0]:>4} {h1[1]:>9.2f} {h1[3]:>9.2f} | "
          f"{h5[0]:>4} {h5[1]:>9.2f} {h5[3]:>9.2f} | "
          f"{h15[0]:>4} {h15[1]:>9.2f} {h15[3]:>9.2f}")

# Group means
def grp_avg(group, h):
    pairs = [s[h][1] for (_,g,s) in results if g == group]
    return (sum(pairs) / len(pairs)) if pairs else 0.0
print(f"\n--- group averages (mean fwd-return in bps) ---")
print(f"control    1m={grp_avg('ctrl','h1m'):>7.2f}  5m={grp_avg('ctrl','h5m'):>7.2f}  15m={grp_avg('ctrl','h15m'):>7.2f}")
print(f"treatment  1m={grp_avg('treat','h1m'):>7.2f}  5m={grp_avg('treat','h5m'):>7.2f}  15m={grp_avg('treat','h15m'):>7.2f}")
print()
# Cost benchmark: Coinbase taker = 60 bps round-trip 120 bps
print("Coinbase taker round-trip cost = ~120 bps. A 5m return must exceed this to be tradeable.")
'@
$q1 | docker exec -i $chili python 2>&1 | Add-Content $out

"" | Add-Content $out
"---" | Add-Content $out
"# end" | Add-Content $out
