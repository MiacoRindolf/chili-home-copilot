"""Q1.T4 — adaptive strategy parameter learning.

Strategy parameters are the constants scattered through the trading
brain (e.g. RSI overbought threshold, RVOL minimum, ATR stop multiple,
pullback red-candle cap). Today most are seeded once and rarely
revisited. T4 makes them addressable and adaptive: each parameter
lives in ``strategy_parameter`` with a current value, learning state,
and bounds; outcomes are recorded against the value-used; the learner
proposes updates that the operator (or a low-stakes auto-applier)
approves.

Lifecycle::

    1. ``register_parameter(...)`` (idempotent) — declares the parameter
       and its initial value at module-import time. If the row already
       exists with a different ``current_value``, the DB value wins.
       This means hot-reloading a service after a flag flip preserves
       any learned value without code changes.

    2. ``get_parameter(family, key, ...)`` — every read goes through
       this. Reads from cache (LRU 60s); falls back to DB; falls back
       to the registered default if neither is available.

    3. ``record_outcome(parameter_id, value_used, outcome_score, ...)``
       — every realized outcome (trade closed, scan filtered, pattern
       passed/failed CPCV) is fed back to the learner.

    4. Background job ``run_parameter_learning_pass()`` (scheduler) —
       computes Bayesian posterior updates over recent outcomes,
       writes ``strategy_parameter_proposal`` rows, auto-applies
       low-stakes proposals.

When ``CHILI_STRATEGY_PARAMETER_LEARNING_ENABLED=False`` (default), the
learner does NOT update values. Reads still work — code that uses
``get_parameter()`` always sees a coherent value, just not an evolving
one. This means we can ship the read path live in shadow mode without
risk.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# --- Cache --------------------------------------------------------------

_CACHE: dict[tuple[str, str, str, Optional[str]], tuple[float, float]] = {}
_CACHE_TTL_SEC = 60.0


def _cache_get(key: tuple) -> Optional[float]:
    rec = _CACHE.get(key)
    if rec is None:
        return None
    expires, value = rec
    if expires < time.monotonic():
        _CACHE.pop(key, None)
        return None
    return value


def _cache_put(key: tuple, value: float) -> None:
    _CACHE[key] = (time.monotonic() + _CACHE_TTL_SEC, value)


def invalidate_cache() -> None:
    _CACHE.clear()


# --- Registration -------------------------------------------------------

@dataclass
class ParameterSpec:
    strategy_family: str
    parameter_key: str
    initial_value: float
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    param_type: str = "float"
    description: str = ""
    scope: str = "global"
    scope_value: Optional[str] = None


def register_parameter(db: Session, spec: ParameterSpec) -> int:
    """Idempotent: insert if missing, otherwise return the existing id.

    Does NOT update the current_value if the row already exists. The DB
    is authoritative — code defaults are only used on first registration.
    """
    try:
        existing = db.execute(
            text(
                """
                SELECT id FROM strategy_parameter
                WHERE strategy_family = :f AND parameter_key = :k
                  AND scope = :s
                  AND COALESCE(scope_value, '') = COALESCE(:sv, '')
                LIMIT 1
                """
            ),
            {
                "f": spec.strategy_family,
                "k": spec.parameter_key,
                "s": spec.scope,
                "sv": spec.scope_value,
            },
        ).fetchone()
        if existing:
            return int(existing[0])
        row = db.execute(
            text(
                """
                INSERT INTO strategy_parameter
                    (strategy_family, parameter_key, scope, scope_value,
                     current_value, initial_value, min_value, max_value,
                     param_type, description)
                VALUES (:f, :k, :s, :sv, :v, :v, :mn, :mx, :pt, :d)
                RETURNING id
                """
            ),
            {
                "f": spec.strategy_family,
                "k": spec.parameter_key,
                "s": spec.scope,
                "sv": spec.scope_value,
                "v": spec.initial_value,
                "mn": spec.min_value,
                "mx": spec.max_value,
                "pt": spec.param_type,
                "d": spec.description,
            },
        ).fetchone()
        db.commit()
        return int(row[0])
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[strategy_param] register %s/%s failed: %s",
                       spec.strategy_family, spec.parameter_key, e)
        return -1


# --- Read path ----------------------------------------------------------

def get_parameter(
    db: Session,
    strategy_family: str,
    parameter_key: str,
    *,
    scope: str = "global",
    scope_value: Optional[str] = None,
    default: Optional[float] = None,
) -> Optional[float]:
    """Return the current value for a parameter.

    Cached for ``_CACHE_TTL_SEC`` seconds. On any DB error or missing
    row, returns ``default`` (so call sites can supply a sensible
    fallback that matches the original hard-coded value).
    """
    key = (strategy_family, parameter_key, scope, scope_value)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        row = db.execute(
            text(
                """
                SELECT current_value FROM strategy_parameter
                WHERE strategy_family = :f AND parameter_key = :k
                  AND scope = :s
                  AND COALESCE(scope_value, '') = COALESCE(:sv, '')
                LIMIT 1
                """
            ),
            {"f": strategy_family, "k": parameter_key, "s": scope, "sv": scope_value},
        ).fetchone()
        if row is None or row[0] is None:
            return default
        value = float(row[0])
        _cache_put(key, value)
        return value
    except Exception as e:
        logger.debug(
            "[strategy_param] get %s/%s failed (using default): %s",
            strategy_family, parameter_key, e,
        )
        return default


# --- Write path: outcomes -----------------------------------------------

def record_outcome(
    db: Session,
    *,
    parameter_id: int,
    value_used: float,
    outcome_score: float,
    trade_id: Optional[int] = None,
    pattern_id: Optional[int] = None,
    meta: Optional[dict] = None,
) -> Optional[int]:
    """Append one outcome observation. ``outcome_score`` is normalized to [0,1].

    Best-effort; logs and swallows on failure.
    """
    try:
        clamped = max(0.0, min(1.0, float(outcome_score)))
        row = db.execute(
            text(
                """
                INSERT INTO strategy_parameter_outcome
                    (parameter_id, value_used, outcome_score, trade_id,
                     pattern_id, meta, recorded_at)
                VALUES (:pid, :v, :s, :tid, :patid, :m, NOW())
                RETURNING id
                """
            ),
            {
                "pid": parameter_id,
                "v": value_used,
                "s": clamped,
                "tid": trade_id,
                "patid": pattern_id,
                "m": json.dumps(meta) if meta else None,
            },
        ).fetchone()
        db.commit()
        return int(row[0]) if row else None
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[strategy_param] record_outcome failed: %s", e)
        return None


# --- Learning pass ------------------------------------------------------

# Bayesian update: outcomes within bins centered on parameter values.
# We don't need a heavy MCMC — just compute the posterior mean of
# `value_used` weighted by `outcome_score`. The expected value over
# successful trials is the natural next-step prior.

_MIN_SAMPLES_FOR_PROPOSAL = 30
_MIN_CONFIDENCE_FOR_AUTO_APPLY = 0.85
_MAX_RELATIVE_CHANGE_PER_PASS = 0.05  # 5% nudge cap


def _confidence_from_samples(n: int) -> float:
    if n <= 0:
        return 0.0
    return round(1.0 - math.exp(-n / 100.0), 4)


def run_parameter_learning_pass(db: Session, lookback_days: int = 30) -> dict:
    """Single pass over ``strategy_parameter_outcome``: compute posterior
    means per parameter, generate ``strategy_parameter_proposal`` rows,
    auto-apply low-stakes ones.

    Skipped entirely when the flag is off — read path remains live.
    """
    out: dict = {"params_evaluated": 0, "proposals": 0, "auto_applied": 0}

    try:
        from ...config import settings
        if not getattr(settings, "chili_strategy_parameter_learning_enabled", False):
            out["skipped"] = "flag_off"
            return out
    except Exception:
        out["skipped"] = "flag_off"
        return out

    try:
        # For each parameter, compute success-weighted average of value_used
        # over the lookback window.
        rows = db.execute(
            text(
                """
                SELECT
                  p.id,
                  p.strategy_family,
                  p.parameter_key,
                  p.current_value,
                  p.min_value,
                  p.max_value,
                  p.locked,
                  COUNT(o.*)                                  AS n_outcomes,
                  AVG(o.outcome_score)                        AS avg_score,
                  SUM(o.value_used * o.outcome_score)
                    / NULLIF(SUM(o.outcome_score), 0)         AS posterior_mean
                FROM strategy_parameter p
                LEFT JOIN strategy_parameter_outcome o
                  ON o.parameter_id = p.id
                  AND o.recorded_at >= NOW() - (:d * INTERVAL '1 day')
                GROUP BY p.id, p.strategy_family, p.parameter_key,
                         p.current_value, p.min_value, p.max_value, p.locked
                """
            ),
            {"d": lookback_days},
        ).fetchall()

        for r in rows or []:
            (param_id, family, key, current, mn, mx, locked,
             n_out, avg_score, post_mean) = r
            out["params_evaluated"] += 1

            if locked:
                continue
            if n_out is None or int(n_out) < _MIN_SAMPLES_FOR_PROPOSAL:
                continue
            if post_mean is None:
                continue

            current_f = float(current)
            post_f = float(post_mean)
            confidence = _confidence_from_samples(int(n_out))

            # Cap the per-pass nudge so we evolve gradually.
            max_step = abs(current_f) * _MAX_RELATIVE_CHANGE_PER_PASS
            proposed = current_f + max(
                -max_step, min(max_step, post_f - current_f)
            )

            # Clamp to bounds.
            if mn is not None:
                proposed = max(float(mn), proposed)
            if mx is not None:
                proposed = min(float(mx), proposed)

            # Skip if the proposal would be a no-op.
            if abs(proposed - current_f) < max(abs(current_f) * 0.001, 1e-9):
                continue

            # Skip if a pending proposal already exists for this parameter.
            existing = db.execute(
                text(
                    "SELECT 1 FROM strategy_parameter_proposal "
                    "WHERE parameter_id = :pid AND status = 'pending' LIMIT 1"
                ),
                {"pid": int(param_id)},
            ).fetchone()
            if existing:
                continue

            justification = (
                f"family={family} key={key} n={n_out} "
                f"avg_score={float(avg_score):.3f} posterior_mean={post_f:.4f} "
                f"current={current_f:.4f} -> proposed={proposed:.4f}"
            )
            severity = "low" if confidence >= _MIN_CONFIDENCE_FOR_AUTO_APPLY else "high"

            new_id = db.execute(
                text(
                    """
                    INSERT INTO strategy_parameter_proposal
                        (parameter_id, current_value, proposed_value, confidence,
                         sample_count, justification, severity, status)
                    VALUES (:pid, :cv, :pv, :c, :n, :j, :sv, 'pending')
                    RETURNING id
                    """
                ),
                {
                    "pid": int(param_id),
                    "cv": current_f,
                    "pv": proposed,
                    "c": confidence,
                    "n": int(n_out),
                    "j": justification,
                    "sv": severity,
                },
            ).fetchone()
            db.commit()
            out["proposals"] += 1

            if severity == "low" and new_id:
                db.execute(
                    text(
                        """
                        UPDATE strategy_parameter
                           SET current_value = :v, updated_at = NOW(),
                               learning_state = jsonb_build_object(
                                 'last_posterior_mean', :pm,
                                 'last_avg_score', :avs,
                                 'last_n_outcomes', :n,
                                 'last_pass_confidence', :c
                               )
                         WHERE id = :pid
                        """
                    ),
                    {
                        "v": proposed,
                        "pm": post_f,
                        "avs": float(avg_score) if avg_score is not None else None,
                        "n": int(n_out),
                        "c": confidence,
                        "pid": int(param_id),
                    },
                )
                db.execute(
                    text(
                        "UPDATE strategy_parameter_proposal SET "
                        "status = 'auto_applied', decided_by = 'learner', "
                        "decided_at = NOW() WHERE id = :id"
                    ),
                    {"id": int(new_id[0])},
                )
                db.commit()
                out["auto_applied"] += 1
                invalidate_cache()

    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("[strategy_param] learning pass failed: %s", e)
        out["error"] = str(e)[:500]

    logger.info("[strategy_param] learning_pass: %s", out)
    return out
