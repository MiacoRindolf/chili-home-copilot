"""Same-name A/B helper (run at the open with a fresh Ross mover).

Arms ONE symbol on alpaca_spot (PAPER) and — only with the explicit --rh flag — robinhood_spot
(REAL money), then ticks + reports both FSMs side by side so you can SEE whether the Alpaca
marketable-limit POSTS + fills where RH gets spread-gated. PAPER-SAFE by default: without --rh
it touches no real money. (docs/DESIGN/ALPACA_LANE.md)

  usage:  python scripts/_ab_helper.py SYMBOL VARIANT_ID [--rh] [ticks]
  e.g.    python scripts/_ab_helper.py PAVS 5          # Alpaca paper only
          python scripts/_ab_helper.py PAVS 5 --rh     # both (RH real money), supervised
"""
import sys

from app.db import SessionLocal
from app.models.trading import TradingAutomationSession
from app.services.trading.momentum_neural.live_runner import tick_live_session
from app.services.trading.momentum_neural.operator_actions import begin_live_arm, confirm_live_arm


def _arm(db, symbol, variant_id, ef):
    r = begin_live_arm(db, user_id=1, symbol=symbol, variant_id=variant_id, execution_family=ef)
    if not r.get("ok") or not r.get("session_id"):
        return None, "BLOCKED: %s %s" % (r.get("error"), (r.get("message") or "")[:60])
    db.commit()
    c = confirm_live_arm(db, user_id=1, arm_token=r.get("arm_token"), confirm=True)
    db.commit()
    return int(r["session_id"]), c.get("state")


def run_ab(symbol, variant_id, *, include_rh=False, ticks=8):
    symbol = str(symbol).strip().upper()
    with SessionLocal() as db:
        sids = {}
        sa, st = _arm(db, symbol, variant_id, "alpaca_spot")
        print("ALPACA(paper): sid=%s state=%s" % (sa, st))
        if sa:
            sids["alpaca"] = sa
        if include_rh:
            sr, st2 = _arm(db, symbol, variant_id, "robinhood_spot")
            print("RH(REAL):      sid=%s state=%s" % (sr, st2))
            if sr:
                sids["rh"] = sr
        else:
            print("RH(REAL):      SKIPPED (no --rh) — paper-only A/B")
        if not sids:
            print("nothing armed; pick a fresh live-eligible mover (momentum_symbol_viability).")
            return
        print("--- driving FSMs (entry should POST a marketable limit when the quote is fresh) ---")
        for i in range(int(ticks)):
            for venue, sid in sids.items():
                try:
                    t = tick_live_session(db, sid)
                    db.commit()
                    s = db.get(TradingAutomationSession, sid)
                    le = (s.risk_snapshot_json or {}).get("momentum_live_execution", {})
                    pos = le.get("position") if isinstance(le.get("position"), dict) else {}
                    note = {k: t.get(k) for k in ("blocked", "reason", "error", "pending", "timeout") if t.get(k)}
                    print("  t%d %-6s state=%-16s entry_sub=%s fill=%s %s" % (
                        i, venue, s.state, le.get("entry_submitted"),
                        pos.get("avg_entry_price"), note or ""))
                except Exception as e:
                    print("  t%d %-6s CRASH(isolated): %s" % (i, venue, str(e)[:90]))
        print("--- done. To clean up: set each session.state='live_cancelled' + cancel any open orders. ---")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--rh"]
    rh = "--rh" in sys.argv
    if len(args) >= 2:
        run_ab(args[0], int(args[1]), include_rh=rh, ticks=int(args[2]) if len(args) > 2 else 8)
    else:
        print("usage: python scripts/_ab_helper.py SYMBOL VARIANT_ID [--rh] [ticks]")
