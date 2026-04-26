"""F.4 — Gateway distiller.

Mines patterns from ``llm_gateway_log`` joined with
``context_brain_outcome`` and writes correlations to ``gateway_pattern``.

Three pattern kinds are produced today:

  * ``strategy_vs_outcome``   — for each (purpose, routing_strategy):
                                avg quality, success_rate, avg_latency.
  * ``chunks_vs_outcome``     — for each (purpose, chunk_bucket): same.
                                Buckets: 0, 1-2, 3-4, 5-6, 7+.
  * ``model_vs_outcome``      — for each (purpose, synthesizer_model): same.

A ``confidence`` ∈ [0, 1] is attached based on sample size + spread:

    confidence = 1 - exp(-n / 30)        # ~0.6 at 30 samples, ~0.95 at 100

Writes to ``gateway_pattern`` are idempotent on the unique
(purpose, pattern_kind, pattern_key) constraint via ON CONFLICT UPDATE.

The distiller is purposefully read-mostly + cheap; one pass should land
under 200ms even on tens of thousands of gateway rows because every join
is on indexed columns.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _confidence_from_samples(n: int) -> float:
    if n <= 0:
        return 0.0
    return round(1.0 - math.exp(-n / 30.0), 4)


def _start_run(db: Session) -> Optional[int]:
    try:
        row = db.execute(
            text(
                "INSERT INTO gateway_learning_run (phase) VALUES ('distiller') "
                "RETURNING id"
            )
        ).fetchone()
        db.commit()
        return int(row[0]) if row else None
    except Exception as e:  # pragma: no cover
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[distiller] start_run failed: %s", e)
        return None


def _end_run(
    db: Session,
    run_id: Optional[int],
    *,
    success: bool,
    patterns_touched: int = 0,
    error: Optional[str] = None,
) -> None:
    if run_id is None:
        return
    try:
        db.execute(
            text(
                "UPDATE gateway_learning_run SET ended_at=NOW(), success=:s, "
                "patterns_touched=:n, error_message=:e WHERE id=:rid"
            ),
            {"s": success, "n": patterns_touched, "e": error, "rid": run_id},
        )
        db.commit()
    except Exception as e:  # pragma: no cover
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[distiller] end_run failed: %s", e)


def _upsert_pattern(
    db: Session,
    *,
    purpose: str,
    pattern_kind: str,
    pattern_key: str,
    sample_count: int,
    avg_quality: Optional[float],
    success_rate: Optional[float],
    avg_latency_ms: Optional[float],
    confidence: float,
    description: str,
) -> bool:
    try:
        db.execute(
            text(
                """
                INSERT INTO gateway_pattern (
                    purpose, pattern_kind, pattern_key,
                    sample_count, avg_quality, success_rate, avg_latency_ms,
                    confidence, description, last_seen_at
                ) VALUES (
                    :purpose, :kind, :key,
                    :n, :q, :sr, :lat,
                    :c, :desc, NOW()
                )
                ON CONFLICT (purpose, pattern_kind, pattern_key)
                DO UPDATE SET
                    sample_count   = EXCLUDED.sample_count,
                    avg_quality    = EXCLUDED.avg_quality,
                    success_rate   = EXCLUDED.success_rate,
                    avg_latency_ms = EXCLUDED.avg_latency_ms,
                    confidence     = EXCLUDED.confidence,
                    description    = EXCLUDED.description,
                    last_seen_at   = NOW()
                """
            ),
            {
                "purpose": purpose,
                "kind": pattern_kind,
                "key": pattern_key,
                "n": sample_count,
                "q": avg_quality,
                "sr": success_rate,
                "lat": avg_latency_ms,
                "c": confidence,
                "desc": description,
            },
        )
        return True
    except Exception as e:  # pragma: no cover
        logger.warning(
            "[distiller] upsert pattern failed (%s/%s/%s): %s",
            purpose, pattern_kind, pattern_key, e,
        )
        return False


def _chunk_bucket(n: Optional[int]) -> str:
    if n is None or n <= 0:
        return "0"
    if n <= 2:
        return "1-2"
    if n <= 4:
        return "3-4"
    if n <= 6:
        return "5-6"
    return "7+"


def distill_patterns(
    db: Session,
    *,
    lookback_hours: int = 24,
    min_samples: int = 5,
) -> dict:
    """Run a single distillation pass.

    Returns a dict ``{patterns_touched, run_id, ok}`` for the caller.
    """
    run_id = _start_run(db)
    touched = 0

    try:
        # Strategy vs outcome --------------------------------------------------
        rows = db.execute(
            text(
                """
                SELECT g.purpose, g.routing_strategy,
                       COUNT(*) AS n,
                       AVG(o.quality_signal) AS avg_q,
                       AVG(CASE WHEN g.success THEN 1.0 ELSE 0.0 END) AS sr,
                       AVG(g.total_latency_ms) AS lat
                FROM llm_gateway_log g
                LEFT JOIN context_brain_outcome o ON o.gateway_log_id = g.id
                WHERE g.started_at >= NOW() - (:h * INTERVAL '1 hour')
                GROUP BY g.purpose, g.routing_strategy
                HAVING COUNT(*) >= :ms
                """
            ),
            {"h": lookback_hours, "ms": min_samples},
        ).fetchall()

        for purpose, strat, n, avg_q, sr, lat in rows:
            conf = _confidence_from_samples(int(n))
            avg_q_f = float(avg_q) if avg_q is not None else None
            sr_f = float(sr) if sr is not None else None
            lat_f = float(lat) if lat is not None else None
            desc = (
                f"strategy={strat}: n={n}, "
                f"avg_q={('%.3f' % avg_q_f) if avg_q_f is not None else 'NA'}, "
                f"success={('%.1f%%' % (sr_f * 100)) if sr_f is not None else 'NA'}, "
                f"latency={('%.0fms' % lat_f) if lat_f is not None else 'NA'}"
            )
            if _upsert_pattern(
                db,
                purpose=purpose,
                pattern_kind="strategy_vs_outcome",
                pattern_key=strat,
                sample_count=int(n),
                avg_quality=avg_q_f,
                success_rate=sr_f,
                avg_latency_ms=lat_f,
                confidence=conf,
                description=desc,
            ):
                touched += 1

        # Chunks vs outcome ----------------------------------------------------
        rows = db.execute(
            text(
                """
                SELECT g.purpose, g.chunk_count,
                       COUNT(*) AS n,
                       AVG(o.quality_signal) AS avg_q,
                       AVG(CASE WHEN g.success THEN 1.0 ELSE 0.0 END) AS sr,
                       AVG(g.total_latency_ms) AS lat
                FROM llm_gateway_log g
                LEFT JOIN context_brain_outcome o ON o.gateway_log_id = g.id
                WHERE g.started_at >= NOW() - (:h * INTERVAL '1 hour')
                GROUP BY g.purpose, g.chunk_count
                HAVING COUNT(*) >= :ms
                """
            ),
            {"h": lookback_hours, "ms": min_samples},
        ).fetchall()

        # Re-bucket in Python (group buckets, not raw chunk_count, for stable
        # pattern keys across small fluctuations).
        bucketed: dict = {}
        for purpose, cc, n, avg_q, sr, lat in rows:
            bucket = _chunk_bucket(int(cc) if cc is not None else 0)
            agg = bucketed.setdefault((purpose, bucket), {
                "n": 0, "q_sum": 0.0, "q_n": 0, "sr_sum": 0.0, "sr_n": 0,
                "lat_sum": 0.0, "lat_n": 0,
            })
            agg["n"] += int(n)
            if avg_q is not None:
                agg["q_sum"] += float(avg_q) * int(n)
                agg["q_n"] += int(n)
            if sr is not None:
                agg["sr_sum"] += float(sr) * int(n)
                agg["sr_n"] += int(n)
            if lat is not None:
                agg["lat_sum"] += float(lat) * int(n)
                agg["lat_n"] += int(n)

        for (purpose, bucket), agg in bucketed.items():
            if agg["n"] < min_samples:
                continue
            conf = _confidence_from_samples(agg["n"])
            avg_q = agg["q_sum"] / agg["q_n"] if agg["q_n"] else None
            sr = agg["sr_sum"] / agg["sr_n"] if agg["sr_n"] else None
            lat = agg["lat_sum"] / agg["lat_n"] if agg["lat_n"] else None
            desc = (
                f"chunk_bucket={bucket}: n={agg['n']}, "
                f"avg_q={('%.3f' % avg_q) if avg_q is not None else 'NA'}, "
                f"success={('%.1f%%' % (sr * 100)) if sr is not None else 'NA'}, "
                f"latency={('%.0fms' % lat) if lat is not None else 'NA'}"
            )
            if _upsert_pattern(
                db,
                purpose=purpose,
                pattern_kind="chunks_vs_outcome",
                pattern_key=bucket,
                sample_count=agg["n"],
                avg_quality=avg_q,
                success_rate=sr,
                avg_latency_ms=lat,
                confidence=conf,
                description=desc,
            ):
                touched += 1

        # Synthesizer model vs outcome ----------------------------------------
        rows = db.execute(
            text(
                """
                SELECT g.purpose, COALESCE(g.synthesizer_model, '<none>'),
                       COUNT(*) AS n,
                       AVG(o.quality_signal) AS avg_q,
                       AVG(CASE WHEN g.success THEN 1.0 ELSE 0.0 END) AS sr,
                       AVG(g.total_latency_ms) AS lat
                FROM llm_gateway_log g
                LEFT JOIN context_brain_outcome o ON o.gateway_log_id = g.id
                WHERE g.started_at >= NOW() - (:h * INTERVAL '1 hour')
                GROUP BY g.purpose, g.synthesizer_model
                HAVING COUNT(*) >= :ms
                """
            ),
            {"h": lookback_hours, "ms": min_samples},
        ).fetchall()

        for purpose, model, n, avg_q, sr, lat in rows:
            conf = _confidence_from_samples(int(n))
            avg_q_f = float(avg_q) if avg_q is not None else None
            sr_f = float(sr) if sr is not None else None
            lat_f = float(lat) if lat is not None else None
            desc = (
                f"model={model}: n={n}, "
                f"avg_q={('%.3f' % avg_q_f) if avg_q_f is not None else 'NA'}, "
                f"success={('%.1f%%' % (sr_f * 100)) if sr_f is not None else 'NA'}, "
                f"latency={('%.0fms' % lat_f) if lat_f is not None else 'NA'}"
            )
            if _upsert_pattern(
                db,
                purpose=purpose,
                pattern_kind="model_vs_outcome",
                pattern_key=model,
                sample_count=int(n),
                avg_quality=avg_q_f,
                success_rate=sr_f,
                avg_latency_ms=lat_f,
                confidence=conf,
                description=desc,
            ):
                touched += 1

        db.commit()
        _end_run(db, run_id, success=True, patterns_touched=touched)
        return {"ok": True, "patterns_touched": touched, "run_id": run_id}

    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("[distiller] pass failed")
        _end_run(db, run_id, success=False, error=str(e)[:500])
        return {"ok": False, "patterns_touched": touched, "run_id": run_id, "error": str(e)}
