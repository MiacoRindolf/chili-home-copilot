"""f-promotion-pipeline-rebalance Phase 4 (2026-05-10).

Composite quality scoring for scan patterns. Reads CPCV / DSR / PBO
evidence from ``scan_patterns``, the rolling-30 directional WR from
``pattern_directional_quality_v`` (Phase 2), and computes a decay
factor on-the-fly from ``pattern_alert_directional_outcome``. Persists
the result to ``scan_patterns.quality_composite_score`` (mig 237).

Composite formula
-----------------

``composite = w1*clip(cpcv_sharpe/2.0, 0, 1)
            + w2*clip(deflated_sharpe/1.0, 0, 1)
            + w3*(1 - clip(pbo, 0, 1))
            + w4*directional_wr
            + w5*(1 - decay)``

Each component is normalized to ``[0, 1]`` so composite ∈ ``[0, 1]``
when weights sum to 1. Targets (cpcv→2.0, dsr→1.0) are calibrated to
the eligibility floor: ``cpcv_median_sharpe >= 1.0`` (the gate floor)
lands at half-credit, ``cpcv_median_sharpe == 2.0`` (academic
"excellent") lands at full credit. Patterns above 2.0 saturate.

Decay
-----

``decay = max(0, older_wr - newer_wr)`` where ``older_wr`` and
``newer_wr`` are the directional WR of the older 15 and newer 15
outcomes in ``pattern_alert_directional_outcome`` for the pattern.
Bounded ``[0, 1]``. Improving patterns have ``decay = 0`` (they get
full credit; we do not penalize improvement).

Decay requires the full 30-row split. Patterns with
``rolling_sample_n < 30`` produce ``decay = None`` and the composite
score is ``None`` (excluded from cohort eligibility — they wait until
30 outcomes accumulate). NO magic-fallback values per advisor brief
§2.6.

Public API
----------

- ``compute_quality_composite_score(pat, directional_wr, decay,
  weights)``: pure function. Returns ``None`` if any required
  component is ``None``.
- ``compute_and_persist_scores(db, *, settings_=None)``: idempotent
  batch run. Computes scores for all ``active`` patterns and
  persists ``quality_composite_score``. Patterns with insufficient
  evidence have ``quality_composite_score = None`` (NULL).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.trading import ScanPattern

logger = logging.getLogger(__name__)


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Numeric clip — numpy-free for the unit-test path."""
    if x is None:
        return None  # type: ignore[return-value]
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def compute_quality_composite_score(
    pat: ScanPattern,
    directional_wr: Optional[float],
    decay: Optional[float],
    weights: dict,
) -> Optional[float]:
    """Compute the composite quality score for a single pattern.

    Returns ``None`` if any of the required CPCV / DSR / PBO /
    directional / decay components is ``None``. NULL propagation —
    NOT a magic-default fallback (advisor brief §2.6).

    Parameters
    ----------
    pat : ScanPattern
        Pattern row with ``cpcv_median_sharpe``, ``deflated_sharpe``,
        ``pbo`` already populated by the promotion gate.
    directional_wr : Optional[float]
        Rolling-30 directional WR from ``pattern_directional_quality_v``.
        ``None`` means the pattern has no view row (insufficient
        outcomes).
    decay : Optional[float]
        Decay factor in ``[0, 1]`` — see module docstring. ``None``
        means rolling_sample_n < 30 (insufficient evidence to detect
        decay).
    weights : dict
        Five-element dict with keys ``cpcv_sharpe``, ``deflated_sharpe``,
        ``pbo_inverse``, ``directional_wr``, ``decay_inverse``.
    """
    cpcv = getattr(pat, "cpcv_median_sharpe", None)
    dsr = getattr(pat, "deflated_sharpe", None)
    pbo = getattr(pat, "pbo", None)

    if cpcv is None or dsr is None or pbo is None:
        return None
    if directional_wr is None or decay is None:
        return None

    cpcv_n = _clip(float(cpcv) / 2.0)
    dsr_n = _clip(float(dsr) / 1.0)
    pbo_inv = 1.0 - _clip(float(pbo))
    wr = _clip(float(directional_wr))
    dec_inv = 1.0 - _clip(float(decay))

    w1 = float(weights.get("cpcv_sharpe", 0.30))
    w2 = float(weights.get("deflated_sharpe", 0.20))
    w3 = float(weights.get("pbo_inverse", 0.15))
    w4 = float(weights.get("directional_wr", 0.25))
    w5 = float(weights.get("decay_inverse", 0.10))

    return (
        w1 * cpcv_n
        + w2 * dsr_n
        + w3 * pbo_inv
        + w4 * wr
        + w5 * dec_inv
    )


def _load_directional_quality_map(db: Session) -> dict[int, dict[str, Any]]:
    """Per-pattern map of {scan_pattern_id: {wr, sample_n}} from the
    Phase 2 view ``pattern_directional_quality_v``."""
    rows = db.execute(text(
        "SELECT scan_pattern_id, "
        "       rolling_directional_wr, "
        "       rolling_sample_n "
        "FROM pattern_directional_quality_v"
    )).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        pid = int(r[0]) if r[0] is not None else None
        if pid is None:
            continue
        wr = float(r[1]) if r[1] is not None else None
        n = int(r[2]) if r[2] is not None else 0
        out[pid] = {"directional_wr": wr, "rolling_sample_n": n}
    return out


def _load_decay_map(db: Session) -> dict[int, Optional[float]]:
    """Per-pattern decay from the rolling-30 split.

    Splits the 30 most-recent outcomes per pattern into
    newer-15 (rn 1-15) and older-15 (rn 16-30). Returns
    ``decay = max(0, older_wr - newer_wr)`` only when BOTH halves are
    fully populated (15 rows each — ``rolling_sample_n == 30``);
    otherwise ``None`` (the pattern is excluded from cohort eligibility
    by the score's NULL value).
    """
    rows = db.execute(text(
        """
        WITH ranked AS (
            SELECT scan_pattern_id,
                   directional_correct,
                   ROW_NUMBER() OVER (
                       PARTITION BY scan_pattern_id
                       ORDER BY alert_at DESC
                   ) AS rn
            FROM pattern_alert_directional_outcome
            WHERE directional_correct IS NOT NULL
        ),
        halves AS (
            SELECT scan_pattern_id,
                   AVG(CASE WHEN rn <= 15 AND directional_correct THEN 1.0
                            WHEN rn <= 15 THEN 0.0 END) AS newer_wr,
                   AVG(CASE WHEN rn BETWEEN 16 AND 30 AND directional_correct THEN 1.0
                            WHEN rn BETWEEN 16 AND 30 THEN 0.0 END) AS older_wr,
                   COUNT(*) FILTER (WHERE rn <= 15) AS newer_n,
                   COUNT(*) FILTER (WHERE rn BETWEEN 16 AND 30) AS older_n
            FROM ranked
            WHERE rn <= 30
            GROUP BY scan_pattern_id
        )
        SELECT scan_pattern_id, newer_wr, older_wr, newer_n, older_n
        FROM halves
        """
    )).fetchall()
    out: dict[int, Optional[float]] = {}
    for r in rows:
        pid = int(r[0])
        newer_wr = r[1]
        older_wr = r[2]
        newer_n = int(r[3] or 0)
        older_n = int(r[4] or 0)
        if newer_n != 15 or older_n != 15 or newer_wr is None or older_wr is None:
            out[pid] = None
            continue
        decay = max(0.0, float(older_wr) - float(newer_wr))
        out[pid] = decay
    return out


def _resolve_weights(settings_: Any) -> dict:
    return {
        "cpcv_sharpe": float(getattr(
            settings_, "chili_cohort_score_weight_cpcv_sharpe", 0.30,
        )),
        "deflated_sharpe": float(getattr(
            settings_, "chili_cohort_score_weight_deflated_sharpe", 0.20,
        )),
        "pbo_inverse": float(getattr(
            settings_, "chili_cohort_score_weight_pbo_inverse", 0.15,
        )),
        "directional_wr": float(getattr(
            settings_, "chili_cohort_score_weight_directional_wr", 0.25,
        )),
        "decay_inverse": float(getattr(
            settings_, "chili_cohort_score_weight_decay_inverse", 0.10,
        )),
    }


def compute_and_persist_scores(
    db: Session,
    *,
    settings_: Any = None,
) -> dict:
    """Compute composite quality score for all active patterns and
    persist to ``scan_patterns.quality_composite_score``.

    Always runs (no kill switch) — the score is informational. The
    cohort-promote job consumes the column; the score-refresh job
    populates it. This split lets operators inspect what cohort
    promote WOULD select before flipping the kill switch.

    Returns a summary dict with counts.
    """
    if settings_ is None:
        from ...config import settings as _settings
        settings_ = _settings

    weights = _resolve_weights(settings_)
    weight_sum = sum(weights.values())
    if not (0.99 <= weight_sum <= 1.01):
        logger.warning(
            "[pattern_quality_score] weights sum to %.4f (expected ~1.0) — "
            "operator-tuned weights may produce composite scores outside [0,1]",
            weight_sum,
        )

    dq_map = _load_directional_quality_map(db)
    decay_map = _load_decay_map(db)

    patterns = (
        db.query(ScanPattern)
          .filter(ScanPattern.active.is_(True))
          .all()
    )

    scored = 0
    skipped_null_evidence = 0
    skipped_thin_directional = 0
    cleared = 0
    for pat in patterns:
        dq = dq_map.get(int(pat.id))
        wr = dq["directional_wr"] if dq else None
        sample_n = dq["rolling_sample_n"] if dq else 0
        decay = decay_map.get(int(pat.id))

        # Eligibility tightening from j.1: rolling_sample_n < 30 →
        # excluded entirely (decay un-computable).
        if sample_n < 30 or decay is None:
            new_score = None
            skipped_thin_directional += 1
        else:
            new_score = compute_quality_composite_score(
                pat, wr, decay, weights,
            )
            if new_score is None:
                skipped_null_evidence += 1
            else:
                scored += 1

        prev = pat.quality_composite_score
        if new_score != prev:
            pat.quality_composite_score = new_score
            if prev is not None and new_score is None:
                cleared += 1

    db.flush()
    db.commit()

    result = {
        "ok": True,
        "patterns_examined": len(patterns),
        "scored": scored,
        "skipped_thin_directional": skipped_thin_directional,
        "skipped_null_evidence": skipped_null_evidence,
        "cleared_to_null": cleared,
        "weight_sum": round(weight_sum, 4),
    }
    logger.info("[pattern_quality_score] refresh: %s", result)
    return result


def compute_and_persist_scores_streaming(
    db: Session,
    *,
    settings_: Any = None,
    batch_size: int = 50,
    stop_flag_path: Optional[str] = None,
    dry_run: bool = False,
    on_pattern: Any = None,
) -> dict:
    """Phase 3 backfill helper. Iterates active patterns in batches,
    commits per batch, polls a stop-flag file between batches.

    Same math as :func:`compute_and_persist_scores` — reuses the
    pure :func:`compute_quality_composite_score`. The streaming
    wrapper exists so the one-shot backfill script can:

    * emit per-pattern progress to stdout (via ``on_pattern``);
    * honor a kill switch (``stop_flag_path`` — a file whose
      presence interrupts the loop between batches);
    * roll back rather than commit when ``dry_run=True`` so the
      operator can inspect the would-write distribution.

    Returns a summary dict.
    """
    import os as _os

    if settings_ is None:
        from ...config import settings as _settings
        settings_ = _settings

    weights = _resolve_weights(settings_)
    weight_sum = sum(weights.values())
    if not (0.99 <= weight_sum <= 1.01):
        logger.warning(
            "[pattern_quality_score] streaming weights sum to %.4f "
            "(expected ~1.0)",
            weight_sum,
        )

    dq_map = _load_directional_quality_map(db)
    decay_map = _load_decay_map(db)

    patterns = (
        db.query(ScanPattern)
          .filter(ScanPattern.active.is_(True))
          .order_by(ScanPattern.id.asc())
          .all()
    )

    scored = 0
    skipped_null_evidence = 0
    skipped_thin_directional = 0
    cleared = 0
    written = 0
    stopped = False
    processed = 0

    pending_changes: list[dict] = []

    for idx, pat in enumerate(patterns):
        dq = dq_map.get(int(pat.id))
        wr = dq["directional_wr"] if dq else None
        sample_n = dq["rolling_sample_n"] if dq else 0
        decay = decay_map.get(int(pat.id))

        if sample_n < 30 or decay is None:
            new_score: Optional[float] = None
            skipped_thin_directional += 1
        else:
            new_score = compute_quality_composite_score(
                pat, wr, decay, weights,
            )
            if new_score is None:
                skipped_null_evidence += 1
            else:
                scored += 1

        prev = pat.quality_composite_score
        changed = new_score != prev
        if changed:
            pending_changes.append({
                "id": int(pat.id),
                "old": prev,
                "new": new_score,
            })
            if not dry_run:
                pat.quality_composite_score = new_score
            written += 1
            if prev is not None and new_score is None:
                cleared += 1

        processed += 1
        if on_pattern is not None:
            try:
                on_pattern({
                    "id": int(pat.id),
                    "old_score": prev,
                    "new_score": new_score,
                    "changed": changed,
                    "directional_wr": wr,
                    "rolling_sample_n": sample_n,
                    "decay": decay,
                })
            except Exception:
                # Operator callback is best-effort; never block the loop.
                pass

        # End of batch — commit (or rollback in dry-run) and check
        # the stop flag before continuing.
        if (idx + 1) % max(1, int(batch_size)) == 0 or idx == len(patterns) - 1:
            if dry_run:
                try:
                    db.rollback()
                except Exception:
                    pass
            else:
                try:
                    db.flush()
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
            if stop_flag_path and _os.path.exists(stop_flag_path):
                stopped = True
                logger.warning(
                    "[pattern_quality_score] stop flag detected at %s — "
                    "halting streaming backfill at pattern_id=%d "
                    "(processed=%d/%d)",
                    stop_flag_path, int(pat.id), processed, len(patterns),
                )
                break

    result = {
        "ok": True,
        "dry_run": bool(dry_run),
        "patterns_examined": len(patterns),
        "processed": processed,
        "scored": scored,
        "skipped_thin_directional": skipped_thin_directional,
        "skipped_null_evidence": skipped_null_evidence,
        "cleared_to_null": cleared,
        "would_write": written if dry_run else None,
        "wrote": (0 if dry_run else written),
        "stopped_by_flag": stopped,
        "weight_sum": round(weight_sum, 4),
        "pending_changes_sample": pending_changes[:8],
    }
    logger.info(
        "[pattern_quality_score] streaming refresh: %s", result,
    )
    return result
