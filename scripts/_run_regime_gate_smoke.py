"""Smoke-test the regime gate against known cases."""
from __future__ import annotations
from app.db import SessionLocal
from app.services.trading.regime_gate import evaluate_regime_gate


CASES = [
    # (pattern_id, ticker, expected_note)
    (1052, "AGL",     "1052 in AGL: AGL is a bleeder, current ticker_regime?"),
    (1052, "ACMR",    "1052 in ACMR: edge ticker"),
    (537,  "ACHC",    "537 confirmed positive on ACHC"),
    (1047, "AAPL",    "1047 retired pattern"),
]


def main() -> int:
    sess = SessionLocal()
    try:
        for pid, ticker, note in CASES:
            r = evaluate_regime_gate(sess, pattern_id=pid, ticker=ticker)
            print(f"[{note}]")
            print(f"  pid={pid}  ticker={ticker}  regime={r.regime_label}")
            print(f"  blocked={r.blocked}  mode={r.mode}  reason={r.reason}")
            print(f"  n_trades={r.n_trades}  hit_rate={r.hit_rate}  mean_pnl_pct={r.mean_pnl_pct}")
            print()
    finally:
        # FIX 46 pattern (rollback before close).
        try:
            sess.rollback()
        except Exception:
            pass
        sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
