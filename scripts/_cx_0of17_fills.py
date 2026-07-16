"""0/17 forensics — step 3: pull the REAL fills (incl. commissions) from Coinbase.

The economic ledger recorded fee=0 for every live coinbase trade; the broker
fills API is the only place the actual commissions live. READ-ONLY (get_fills).
Caches to scripts/_cx_cache/cx_0of17_broker_fills.json.

Run: cd D:/dev/chili-home-copilot && PYTHONPATH=<worktree> conda run -n chili-env python scripts/_cx_0of17_fills.py
"""
import json
import pathlib
import time

from app.services import coinbase_service as cb

CACHE = pathlib.Path(__file__).resolve().parent / "_cx_cache"
OUT = CACHE / "cx_0of17_broker_fills.json"
DB = json.loads((CACHE / "cx_0of17_db.json").read_text())


def main() -> None:
    client = cb.get_coinbase_rest_client()
    if client is None:
        print("NO CLIENT (creds missing)")
        return
    products = sorted({o["symbol"] for o in DB["outcomes"]})
    window_start = "2026-06-05T00:00:00Z"
    window_end = "2026-06-10T00:00:00Z"
    all_fills = []
    for p in products:
        cursor = None
        for _page in range(10):
            try:
                kw = dict(product_ids=[p], start_sequence_timestamp=window_start,
                          end_sequence_timestamp=window_end, limit=250)
                if cursor:
                    kw["cursor"] = cursor
                resp = client.get_fills(**kw)
            except Exception as e:
                print(f"{p}: ERROR {e}")
                break
            d = resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
            fills = d.get("fills") or []
            for f in fills:
                all_fills.append({
                    "product_id": f.get("product_id"),
                    "order_id": f.get("order_id"),
                    "trade_time": f.get("trade_time"),
                    "side": f.get("side"),
                    "size": f.get("size"),
                    "size_in_quote": f.get("size_in_quote"),
                    "price": f.get("price"),
                    "commission": f.get("commission"),
                    "liquidity_indicator": f.get("liquidity_indicator"),
                })
            cursor = d.get("cursor")
            print(f"{p}: +{len(fills)} fills (cursor={'yes' if cursor else 'no'})")
            if not cursor or not fills:
                break
            time.sleep(0.3)
        time.sleep(0.3)
    OUT.write_text(json.dumps(all_fills, indent=1))
    print(f"total fills={len(all_fills)} -> {OUT}")


if __name__ == "__main__":
    main()
