"""Gap #2 (round-number first-scale) REPLAY GATE: net realized PnL with the round-number
first-scale ON vs OFF. The OFF baseline forces round_numbers_above -> [] (no qualifying
level -> the rr target stands -> pre-gate behavior). An exit change must be net-positive/
neutral BEFORE shipping (EVOLVE-not-devolve). armed_source='live' = the faithful real arm
spans. Ship criterion: ON >= OFF (captures give-back or no-op; must not bleed net PnL).
"""
import os
import sys

os.environ.setdefault("CHILI_MOMENTUM_PULLBACK_ENTRY_INTERVAL", "1m")

from app.services.trading.momentum_neural import paper_execution, replay_v2  # noqa: E402

DATES = sys.argv[1:] or ["2026-06-14", "2026-06-13", "2026-06-12"]
_REAL = paper_execution.round_numbers_above


def _run(date: str, enabled: bool) -> dict:
    paper_execution.round_numbers_above = _REAL if enabled else (lambda price: [])
    try:
        r = replay_v2.run_replay(date, persist=False, armed_source="live")
        return {
            "total_usd": round(float(r.get("total_usd", 0.0)), 2),
            "wins": r.get("wins"),
            "losses": r.get("losses"),
            "trades": len(r.get("trades", [])),
        }
    except Exception as e:
        return {"error": str(e)[:140]}


agg_on = agg_off = 0.0
for d in DATES:
    off = _run(d, False)
    on = _run(d, True)
    print(f"\n=== {d} ===")
    print(f"  OFF (fixed R:R target):   {off}")
    print(f"  ON  (round-# first-scale): {on}")
    if "total_usd" in on and "total_usd" in off:
        delta = on["total_usd"] - off["total_usd"]
        agg_on += on["total_usd"]
        agg_off += off["total_usd"]
        print(f"  DELTA (ON-OFF): {delta:+.2f}  {'WIN' if delta > 0 else ('NEUTRAL' if delta == 0 else 'LOSS')}")
print(f"\n=== AGGREGATE === OFF={agg_off:+.2f}  ON={agg_on:+.2f}  DELTA={agg_on-agg_off:+.2f}")
print("VERDICT:", "SHIP (net-positive/neutral)" if agg_on >= agg_off else "DO NOT ship — revert (bleeds PnL)")
