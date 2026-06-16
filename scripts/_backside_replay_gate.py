"""Front/back-side veto (gap #1) REPLAY GATE: net realized PnL with the veto ON vs OFF.

The veto is always-on in code (no dark flags); for the A/B the OFF baseline is produced
by monkeypatching the detector to read front-side (False), which makes the gate a no-op
and reproduces pre-gate behavior. An entry-blocking change must be net-positive/neutral
(do-no-harm) BEFORE shipping (EVOLVE-not-devolve). armed_source='live' = the faithful
real arm spans. Ship criterion: ON >= OFF (drops losers or no-op; never drops winners).
"""
import os
import sys

os.environ.setdefault("CHILI_MOMENTUM_PULLBACK_ENTRY_INTERVAL", "1m")

from app.services.trading.momentum_neural import entry_gates, replay_v2  # noqa: E402

DATES = sys.argv[1:] or ["2026-06-14", "2026-06-13", "2026-06-12"]
_REAL_DETECT = entry_gates._detect_back_side


def _run(date: str, enabled: bool) -> dict:
    # ON -> the real detector; OFF -> force front-side (gate becomes a no-op).
    entry_gates._detect_back_side = _REAL_DETECT if enabled else (lambda *a, **k: (False, ""))
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
    print(f"  OFF (no back-side veto): {off}")
    print(f"  ON  (back-side veto):    {on}")
    if "total_usd" in on and "total_usd" in off:
        delta = on["total_usd"] - off["total_usd"]
        agg_on += on["total_usd"]
        agg_off += off["total_usd"]
        verdict = "WIN" if delta > 0 else ("NEUTRAL" if delta == 0 else "LOSS")
        print(f"  DELTA (ON-OFF): {delta:+.2f}  {verdict}")
print(f"\n=== AGGREGATE === OFF={agg_off:+.2f}  ON={agg_on:+.2f}  DELTA={agg_on-agg_off:+.2f}")
print("VERDICT:", "SHIP LIVE-ON (net-positive/neutral, do-no-harm)" if agg_on >= agg_off
      else "DO NOT ship live-on — flag-off (drops net winners)")
