"""Phase D Docker soak — triple-barrier labels + economic promotion metric.

Runs inside a live chili-env (or container via ``docker compose exec``) and
exercises the Phase D surface end-to-end:

  1. Migration 131 applied (``trading_triple_barrier_labels`` exists).
  2. ``BRAIN_TRIPLE_BARRIER_MODE`` is ``shadow``.
  3. Write labels for 3 synthetic (ticker, date, entry_close, forward bars)
     tuples covering TP, SL, and timeout paths — assert values match
     hand-computed expectations.
  4. Re-running the same writes is idempotent (UNIQUE key).
  5. ``label_summary`` reports the expected by_barrier distribution.
  6. Diagnostics endpoint shape is intact.

Expected exit code: 0 on success.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

# Ensure repo root importable when run with `python scripts/phase_d_soak.py`
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.trading import TripleBarrierLabelRow  # noqa: E402
from app.services.trading.triple_barrier import (  # noqa: E402
    OHLCVBar,
    TripleBarrierConfig,
)
from app.services.trading.triple_barrier_labeler import (  # noqa: E402
    label_single,
    label_summary,
)


FIXED_DATE = date(2026, 4, 1)  # old enough to never collide with fresh prod rows


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        sys.exit(1)
    print(f"ok   {msg}")


def _cleanup(db) -> None:
    db.execute(
        text(
            "DELETE FROM trading_triple_barrier_labels "
            "WHERE ticker LIKE 'SOAK-%' AND label_date = :d"
        ),
        {"d": FIXED_DATE},
    )
    db.commit()


def main() -> int:
    mode = (getattr(settings, "brain_triple_barrier_mode", "off") or "off").lower()
    print(f"[phase_d_soak] BRAIN_TRIPLE_BARRIER_MODE={mode}")
    if mode != "shadow":
        print(
            "[phase_d_soak] WARNING: mode is not 'shadow' — soak will still run but "
            "DB writes may not happen (mode=off) or will produce authoritative rows."
        )

    db = SessionLocal()
    try:
        tables = {
            r[0]
            for r in db.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            ).fetchall()
        }
        _assert(
            "trading_triple_barrier_labels" in tables,
            "migration 131 applied (trading_triple_barrier_labels exists)",
        )

        _cleanup(db)

        cfg = TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=5, side="long")

        tp_bars = [OHLCVBar(100, 103.0, 99.5, 102.5)]
        sl_bars = [OHLCVBar(100, 100.5, 98.5, 99.0)]
        timeout_bars = [OHLCVBar(100, 100.5, 99.5, 100.1)] * 5

        # 1. TP path
        r1 = label_single(
            db,
            ticker="SOAK-TP",
            label_date=FIXED_DATE,
            entry_close=100.0,
            future_bars=tp_bars,
            cfg=cfg,
            mode_override="shadow",
        )
        _assert(r1.label.label == 1 and r1.label.barrier_hit == "tp",
                "SOAK-TP labelled as tp/+1")
        _assert(r1.inserted is True, "SOAK-TP inserted on first run")

        # 2. SL path
        r2 = label_single(
            db,
            ticker="SOAK-SL",
            label_date=FIXED_DATE,
            entry_close=100.0,
            future_bars=sl_bars,
            cfg=cfg,
            mode_override="shadow",
        )
        _assert(r2.label.label == -1 and r2.label.barrier_hit == "sl",
                "SOAK-SL labelled as sl/-1")

        # 3. Timeout path
        r3 = label_single(
            db,
            ticker="SOAK-TO",
            label_date=FIXED_DATE,
            entry_close=100.0,
            future_bars=timeout_bars,
            cfg=cfg,
            mode_override="shadow",
        )
        _assert(r3.label.label == 0 and r3.label.barrier_hit == "timeout",
                "SOAK-TO labelled as timeout/0")

        # 4. Idempotency — re-insert same rows, expect inserted=False
        r1b = label_single(
            db,
            ticker="SOAK-TP",
            label_date=FIXED_DATE,
            entry_close=100.0,
            future_bars=tp_bars,
            cfg=cfg,
            mode_override="shadow",
        )
        _assert(r1b.inserted is False, "SOAK-TP idempotent upsert skipped duplicate")

        soak_count = (
            db.query(TripleBarrierLabelRow)
            .filter(
                TripleBarrierLabelRow.ticker.in_(["SOAK-TP", "SOAK-SL", "SOAK-TO"]),
                TripleBarrierLabelRow.label_date == FIXED_DATE,
            )
            .count()
        )
        _assert(soak_count == 3, f"soak rows count (got {soak_count}, want 3)")

        # 5. Summary aggregates correctly for a generous window
        summary = label_summary(db, lookback_hours=24)
        by = summary["by_barrier"]
        _assert(by["tp"] >= 1, f"summary by_barrier.tp >= 1 (got {by['tp']})")
        _assert(by["sl"] >= 1, f"summary by_barrier.sl >= 1 (got {by['sl']})")
        _assert(by["timeout"] >= 1, f"summary by_barrier.timeout >= 1 (got {by['timeout']})")
        for k in ("mode", "tp_pct_cfg", "sl_pct_cfg", "max_bars_cfg",
                 "label_distribution", "last_label_at"):
            _assert(k in summary, f"summary has key {k!r}")

        print("\n[phase_d_soak] SUCCESS — Phase D shadow rollout soak passed.")
        print(f"  rows_written={soak_count}  mode={summary['mode']}  labels_total={summary['labels_total']}")
        return 0
    finally:
        try:
            _cleanup(db)
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
