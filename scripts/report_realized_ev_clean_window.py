"""Print the per-asset-class realized-EV clean-window report.

Post-floor (>= chili_realized_ev_clean_window_since), dirty-excluded, LIVE-only
realized EV split by asset class, plus per-promoted-pattern representativeness.

Usage:
    conda run -n chili-env python scripts/report_realized_ev_clean_window.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CHILI_PYTEST", "1")  # never run migrations from this read-only probe


def main() -> int:
    from app.db import SessionLocal
    from app.services.trading.realized_ev_clean_window_report import build_clean_window_report

    db = SessionLocal()
    try:
        rep = build_clean_window_report(db)
    finally:
        db.close()

    print("=" * 72)
    print(f"REALIZED-EV CLEAN WINDOW REPORT  (floor >= {rep['clean_window_since']})")
    print(f"representative iff n >= {rep['min_trades']} and span >= {rep['min_days']} days")
    print("=" * 72)
    print("\nPER ASSET CLASS (post-floor, clean, LIVE):")
    print(f"  {'asset':10} {'n':>5} {'win_rate':>9} {'avg_ret%':>9} {'span_d':>7} {'repr':>5}")
    for a in rep["per_asset_class"]:
        wr = f"{a['win_rate']:.3f}" if a["win_rate"] is not None else "  -  "
        ar = f"{a['avg_ret_pct']:+.3f}" if a["avg_ret_pct"] is not None else "  -  "
        print(f"  {a['asset_class']:10} {a['n']:>5} {wr:>9} {ar:>9} {a['span_days']:>7} {str(a['representative']):>5}")

    print(f"\nPROMOTED PATTERNS ({rep['promoted_count']}):")
    print(f"  {'id':>6} {'n':>4} {'win_rate':>9} {'avg_ret%':>9} {'span_d':>7} {'verdict':>20}  name")
    for p in rep["promoted_patterns"]:
        wr = f"{p['post_floor_win_rate']:.3f}" if p["post_floor_win_rate"] is not None else "  -  "
        ar = f"{p['post_floor_avg_ret_pct']:+.3f}" if p["post_floor_avg_ret_pct"] is not None else "  -  "
        print(
            f"  {p['pattern_id']:>6} {p['post_floor_n']:>4} {wr:>9} {ar:>9} "
            f"{p['post_floor_span_days']:>7} {p['verdict']:>20}  {p['name'] or ''}"
        )
    print("\n" + rep["note"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
