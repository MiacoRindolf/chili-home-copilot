"""Recompute CPCV / DSR / PBO metrics for promoted or live scan_patterns.

**One-time / gap-fill:** This CLI backfills rows that already reached ``promoted`` or ``live``
but never got CPCV columns (e.g. after a migration). **New promotions** get CPCV from the
normal mining funnel via :func:`finalize_promotion_with_cpcv` in ``mining_validation`` when
``check_promotion_ready*`` runs — no cron required for that path.

By default only rows with ``cpcv_n_paths IS NULL`` are selected so reruns **resume** after
crashes. Pass ``--all`` to re-evaluate every promoted/live pattern (full recompute).

Demotes patterns that fail :func:`promotion_gate_passes` to ``lifecycle_stage='challenged'``
(pruning remains lifecycle-driven elsewhere).

For a **production-shaped** database (real promoted/live rows), use ``chili_staging`` and
point ``DATABASE_URL`` at it for the run — see ``docs/STAGING_DATABASE.md``. Do **not** use
``chili_test`` for this dry-run (pytest data is usually empty of promoted patterns).

Usage (repo root, conda ``chili-env``)::

    conda run -n chili-env python scripts/backfill_cpcv_metrics.py
    conda run -n chili-env python scripts/backfill_cpcv_metrics.py --dry-run
    conda run -n chili-env python scripts/backfill_cpcv_metrics.py --commit

Dry-run (default): evaluates CPCV, logs per-pattern lines, prints a summary including
demotions by scanner bucket. **No database writes**: no ``commit()``, no shadow-log inserts
(``persist_cpcv_shadow_eval`` is not used by this script). The session is rolled back on
exit when not ``--commit``.

With ``--commit``, each evaluated pattern is **committed immediately** after its log line
so a crash or OOM later does not lose earlier updates. Patterns are processed **newest
first** (``scan_patterns.updated_at`` descending, then ``id`` descending).

Exits with code **2** if would-demote count exceeds **20%** of evaluated patterns (operator review gate).

Rollback SQL (see docs/CPCV_PROMOTION_GATE_RUNBOOK.md)::

    UPDATE scan_patterns SET promotion_gate_passed = NULL, promotion_gate_reasons = NULL,
      cpcv_n_paths = NULL, cpcv_median_sharpe = NULL, cpcv_median_sharpe_by_regime = NULL,
      deflated_sharpe = NULL, pbo = NULL, n_effective_trials = NULL
    WHERE id IN (...);
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if importlib.util.find_spec("skfolio") is None:
    sys.stderr.write(
        "backfill_cpcv_metrics: missing dependency `skfolio` (CPCV promotion gate).\n"
        "Use the repo conda env — plain `python` on PATH is usually wrong:\n"
        "  conda run -n chili-env python scripts/backfill_cpcv_metrics.py --dry-run\n"
        "See docs/CPCV_PROMOTION_GATE_RUNBOOK.md.\n"
    )
    raise SystemExit(1)

from sqlalchemy import desc  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.trading import PatternTradeRow, ScanPattern  # noqa: E402
from app.services.trading.promotion_gate import (  # noqa: E402
    CPCV_FEATURE_NAMES,
    cpcv_eval_to_scan_pattern_fields,
    evaluate_pattern_cpcv,
    infer_scanner_bucket,
    normalize_ptr_row_features,
    promotion_gate_passes,
    SCANNER_BUCKETS,
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
    ap.add_argument(
        "--commit",
        action="store_true",
        help="Persist changes to scan_patterns (default is dry-run: compute and log only).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run (same as default when --commit is not passed).",
    )
    ap.add_argument("--hypotheses", type=int, default=1, help="n_hypotheses_tested for DSR.")
    ap.add_argument(
        "--max-labeled-rows",
        type=int,
        default=20_000,
        help=(
            "Cap labeled rows per pattern before CPCV (avoids OOM on huge PTR histories). "
            "Use 0 for no cap (uses CHILI_CPCV_MAX_LABELED_ROWS from env if set)."
        ),
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help=(
            "Include every promoted/live row. Default: only "
            "``cpcv_n_paths IS NULL`` (resume / skip already backfilled)."
        ),
    )
    args = ap.parse_args()

    if args.commit and args.dry_run:
        logger.error("Pass only one of --commit or --dry-run.")
        return 1

    do_commit = bool(args.commit)
    demote_by_scanner: Counter[str] = Counter()
    n_evaluated = n_pass = n_demote_would = n_skip = 0

    db = SessionLocal()
    try:
        base_pf = db.query(ScanPattern).filter(
            ScanPattern.lifecycle_stage.in_(("promoted", "live"))
        )
        n_promoted_live_db = base_pf.count()
        q = base_pf
        if not args.all:
            q = q.filter(ScanPattern.cpcv_n_paths.is_(None))
        pats = q.order_by(desc(ScanPattern.updated_at), desc(ScanPattern.id)).all()
        logger.info(
            "candidates=%s promoted_or_live_db_total=%s filter=%s",
            len(pats),
            n_promoted_live_db,
            "none (--all)" if args.all else "cpcv_n_paths IS NULL",
        )
        cap_kw: dict = {}
        if int(args.max_labeled_rows) > 0:
            cap_kw["max_labeled_rows"] = int(args.max_labeled_rows)

        min_ptr = int(getattr(settings, "chili_cpcv_min_trades", 15))
        for pat in pats:
            rows = _rows_for_pattern(db, pat.id)
            if len(rows) < min_ptr:
                logger.info("skip id=%s name=%s (rows=%s)", pat.id, pat.name, len(rows))
                n_skip += 1
                continue

            bucket = infer_scanner_bucket(pat)
            feat_dim = len(CPCV_FEATURE_NAMES)
            n_ptr = len(rows)
            logger.info(
                "[cpcv_backfill] pattern_id=%s scanner=%s feature_count=%s row_count=%s",
                pat.id,
                bucket,
                feat_dim,
                n_ptr,
            )
            try:
                payload = evaluate_pattern_cpcv(
                    pat.id,
                    rows,
                    n_hypotheses_tested=max(1, int(args.hypotheses)),
                    **cap_kw,
                )
            except Exception:
                logger.exception(
                    "[cpcv_backfill] evaluation_failed pattern_id=%s scanner=%s "
                    "feature_count=%s row_count=%s (skipping)",
                    pat.id,
                    bucket,
                    feat_dim,
                    n_ptr,
                )
                continue

            ok, reasons = promotion_gate_passes(payload)
            payload["promotion_gate_passed"] = ok
            payload["promotion_gate_reasons"] = reasons
            patch = cpcv_eval_to_scan_pattern_fields(payload)

            would_demote = (not ok) and (not payload.get("skipped"))

            action = "no_change"
            if would_demote:
                action = "DEMOTE_TO_CHALLENGED" if do_commit else "would_demote_to_challenged"
            elif do_commit and patch:
                action = "patch_cpcv_columns"

            if do_commit:
                for k, v in patch.items():
                    setattr(pat, k, v)
                if would_demote:
                    pat.lifecycle_stage = "challenged"
                    pat.lifecycle_changed_at = datetime.utcnow()
                try:
                    db.commit()
                except Exception:
                    logger.exception(
                        "backfill_cpcv_metrics: commit failed id=%s name=%s",
                        pat.id,
                        pat.name,
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    continue

            n_evaluated += 1
            if would_demote:
                n_demote_would += 1
                demote_by_scanner[bucket] += 1
            if ok:
                n_pass += 1

            logger.info(
                "id=%s scanner=%s action=%s ok=%s skipped=%s paths=%s dsr=%s pbo=%s reasons=%s",
                pat.id,
                bucket,
                action,
                ok,
                bool(payload.get("skipped")),
                payload.get("cpcv_n_paths"),
                payload.get("deflated_sharpe"),
                payload.get("pbo"),
                reasons,
            )

        # ── Summary (production-shape report) ─────────────────────────────
        logger.info("--- CPCV backfill summary ---")
        logger.info("promoted_or_live_db_total=%s", n_promoted_live_db)
        logger.info("this_run_candidates=%s", len(pats))
        logger.info("evaluated (>=min_PTR rows, min=%s)=%s", min_ptr, n_evaluated)
        logger.info("would_pass_cpcv_gate=%s", n_pass)
        logger.info("would_demote_total=%s", n_demote_would)
        for b in SCANNER_BUCKETS:
            c = demote_by_scanner.get(b, 0)
            if c:
                logger.info("would_demote_scanner[%s]=%s", b, c)
        logger.info("skipped_insufficient_rows=%s", n_skip)
        logger.info("commit=%s", do_commit)

        review_exit = 0
        if n_evaluated > 0 and (n_demote_would / n_evaluated) > 0.20:
            logger.error(
                "OPERATOR_REVIEW: would_demote=%s exceeds 20%% of evaluated=%s "
                "(ratio=%.2f). Do not run --commit without review.",
                n_demote_would,
                n_evaluated,
                n_demote_would / n_evaluated,
            )
            review_exit = 2

        return review_exit
    finally:
        if not do_commit:
            try:
                db.rollback()
            except Exception:
                pass
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
