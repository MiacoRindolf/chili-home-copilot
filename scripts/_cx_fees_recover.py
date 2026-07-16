"""_cx_fees_recover.py — recover ACTUAL Coinbase fees CHILI paid per fill.

Read-only: get_transaction_summary (current fee tier) + get_fills (commission
per fill, liquidity indicator). Caches raw fills to scripts/_cx_cache/.

Run from main repo root with PYTHONPATH at the night-ops worktree so the
.env (API keys) resolves from cwd.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cx_cache")
os.makedirs(CACHE, exist_ok=True)
FILLS_CACHE = os.path.join(CACHE, "cb_fills.json")
SUMMARY_CACHE = os.path.join(CACHE, "cb_tx_summary.json")


def _to_dict(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    return {"repr": repr(obj)}


def get_client():
    from app.config import settings
    from coinbase.rest import RESTClient
    secret = settings.coinbase_api_secret.replace("\\n", "\n")
    return RESTClient(api_key=settings.coinbase_api_key, api_secret=secret)


def fetch_summary(client):
    resp = client.get_transaction_summary()
    d = _to_dict(resp)
    # make it json-serializable
    out = json.loads(json.dumps(d, default=lambda o: _to_dict(o)))
    with open(SUMMARY_CACHE, "w") as f:
        json.dump(out, f, indent=2)
    return out


def fetch_fills(client, days=60):
    if os.path.exists(FILLS_CACHE):
        with open(FILLS_CACHE) as f:
            return json.load(f)
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fills = []
    cursor = None
    for page in range(40):
        kwargs = {"limit": 250, "start_sequence_timestamp": start}
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.get_fills(**kwargs)
        d = _to_dict(resp)
        batch = d.get("fills") or []
        batch = [json.loads(json.dumps(_to_dict(b), default=lambda o: _to_dict(o))) for b in batch]
        fills.extend(batch)
        cursor = d.get("cursor") or ""
        if not cursor or not batch:
            break
        time.sleep(0.4)
    with open(FILLS_CACHE, "w") as f:
        json.dump(fills, f, indent=2)
    return fills


def main():
    client = get_client()
    print("=== TRANSACTION SUMMARY (current fee tier) ===")
    try:
        s = fetch_summary(client)
        ft = s.get("fee_tier") or {}
        print(json.dumps({
            "pricing_tier": ft.get("pricing_tier"),
            "usd_from": ft.get("usd_from"), "usd_to": ft.get("usd_to"),
            "maker_fee_rate": ft.get("maker_fee_rate"),
            "taker_fee_rate": ft.get("taker_fee_rate"),
            "total_volume_30d": s.get("total_volume"),
            "total_fees_30d": s.get("total_fees"),
            "advanced_trade_only_volume": s.get("advanced_trade_only_volume"),
            "advanced_trade_only_fees": s.get("advanced_trade_only_fees"),
        }, indent=2))
    except Exception as e:
        print(f"summary failed: {e}")

    print("\n=== FILLS (last 60d) ===")
    try:
        fills = fetch_fills(client)
    except Exception as e:
        print(f"fills failed: {e}")
        return
    print(f"n_fills={len(fills)}")
    agg = defaultdict(lambda: {"n": 0, "notional": 0.0, "fees": 0.0})
    by_day = defaultdict(lambda: {"n": 0, "notional": 0.0, "fees": 0.0})
    for f in fills:
        try:
            size = float(f.get("size") or 0)
            price = float(f.get("price") or 0)
            size_in_quote = str(f.get("size_in_quote")).lower() == "true" or f.get("size_in_quote") is True
            notional = size if size_in_quote else size * price
            fee = float(f.get("commission") or 0)
            liq = f.get("liquidity_indicator") or "UNKNOWN"
            side = f.get("side") or "?"
            key = (liq, side)
            agg[key]["n"] += 1
            agg[key]["notional"] += notional
            agg[key]["fees"] += fee
            day = (f.get("trade_time") or "")[:10]
            by_day[day]["n"] += 1
            by_day[day]["notional"] += notional
            by_day[day]["fees"] += fee
        except Exception:
            continue
    print(f"{'liq':12} {'side':5} {'n':>5} {'notional$':>12} {'fees$':>9} {'eff_bps':>8}")
    tot_n = tot_f = 0.0
    for key, v in sorted(agg.items()):
        bps = v["fees"] / v["notional"] * 10000 if v["notional"] else 0
        print(f"{key[0]:12} {key[1]:5} {v['n']:>5} {v['notional']:>12.2f} {v['fees']:>9.4f} {bps:>8.1f}")
        tot_n += v["notional"]; tot_f += v["fees"]
    if tot_n:
        print(f"{'TOTAL':18} {sum(v['n'] for v in agg.values()):>5} {tot_n:>12.2f} {tot_f:>9.4f} {tot_f/tot_n*10000:>8.1f}")
    print("\nby day:")
    for day, v in sorted(by_day.items()):
        bps = v["fees"] / v["notional"] * 10000 if v["notional"] else 0
        print(f"  {day} n={v['n']:>4} notional=${v['notional']:>10.2f} fees=${v['fees']:>8.4f} eff_bps={bps:>6.1f}")


if __name__ == "__main__":
    sys.exit(main())
