"""Recompute CPCV / DSR / PBO metrics for promoted or live scan_patterns.

Demotes patterns that fail :func:`promotion_gate_passes` to ``lifecycle_stage='challenged'``
(pruning remains lifecycle-driven elsewhere).

Usage (repo root, conda ``chili-env``)::

    conda run -n chili-env python scripts/backfill_cpcv_metrics.py --dry-run
    conda run -n chili-env python scripts/backfill_cpcv_metrics.py --commit

Rollback SQL (see docs/CPCV_PROMOTION_GATE_RUNBOOK.md)::

    UPDATE scan_patterns SET promotion_gate_passed = NULL, promotion_gate_reasons = NULL,
      cpcv_n_paths = NULL, cpcv_median_sharpe = NULL, cpcv_median_sharpe_by_regime = NULL,
      deflated_sharpe = NULL, pbo = NULL, n_effective_trials = NULL
    WHERE id IN (...);
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.models.trading import PatternTradeRow, ScanPattern  # noqa: E402
from app.services.trading.promotion_gate import (  # noqa: E402
    cpcv_eval_to_scan_pattern_fields,
    evaluate_pattern_cpcv,
    normalize_ptr_row_features,
    promotion_gate_passes,
)

logger = logging.getLogger("backfill_cpcv_metrics")


def _rows_for_pattern(db, scan_pattern_id: int) -> list[dict]:
    ptrs = (
        db.query(PatternTradeRow)
        .filter(
            PatternTradeRow.scan_pattern_id == scan_pattern_id,
            PatternTradeRow.outcome_return_pct.isnot(None),
        )
        .order_by(PatternTradeRow.as_of_ts.asc())
        .all()
    )
    out: list[dict] = []
    for r in ptrs:
        fj = r.features_json if isinstance(r.features_json, dict) else {}
        d = normalize_ptr_row_features(
            outcome_return_pct=r.outcome_return_pct,
            as_of_ts=r.as_of_ts,
            ticker=r.ticker,
            timeframe=r.timeframe,
            features_json=fj,
        )
        d["ret_5d"] = float(r.outcome_return_pct or 0.0)
        out.append(d)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Backfill CPCV promotion metrics on scan_patterns.")
    ap.add_argument("--commit", action="store_true", help="Persist changes (default dry-run).")
    ap.add_argument("--hypotheses", type=int, default=1, help="n_hypotheses_tested for DSR.")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        pats = (
            db.query(ScanPattern)
            .filter(ScanPattern.lifecycle_stage.in_(("promoted", "live")))
            .all()
        )
        n_ok = n_demote = n_skip = 0
        for pat in pats:
            rows = _rows_for_pattern(db, pat.id)
            if len(rows) < 30:
                logger.info("skip id=%s name=%s (rows=%s)", pat.id, pat.name, len(rows))
                n_skip += 1
                continue
            payload = evaluate_pattern_cpcv(
                pat.id,
                rows,
                n_hypotheses_tested=max(1, int(args.hypotheses)),
            )
            ok, reasons = promotion_gate_passes(payload)
            payload["promotion_gate_passed"] = ok
            payload["promotion_gate_reasons"] = reasons
            patch = cpcv_eval_to_scan_pattern_fields(payload)
            logger.info(
                "id=%s ok=%s paths=%s dsr=%s pbo=%s reasons=%s commit=%s",
                pat.id,
                ok,
                payload.get("cpcv_n_paths"),
                payload.get("deflated_sharpe"),
                payload.get("pbo"),
                reasons,
                args.commit,
            )
            if args.commit:
                for k, v in patch.items():
                    setattr(pat, k, v)
                if not ok and not payload.get("skipped"):
                    pat.lifecycle_stage = "challenged"
                    pat.lifecycle_changed_at = datetime.utcnow()
                    n_demote += 1
                elif ok:
                    n_ok += 1
        if args.commit:
            db.commit()
        logger.info("summary ok=%s demoted=%s skipped=%s", n_ok, n_demote, n_skip)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
