"""f-promotion-pipeline-rebalance Phase 4 (2026-05-10).

f-composite-quality-reweight-realized-evidence (2026-05-16):
Reweighted toward realized PnL evidence. The original 0.30/0.20/0.15
weighting on CPCV/DSR/PBO produced a Spearman(score, total_pnl) of
−0.757 against realized OOS trades (DSR pegged at 1.0 and PBO pegged
at 0.0 for every scored pattern — 0.35 of the score was a constant
with no discriminatory power). New defaults shift weight onto the
two real-OOS signals (directional WR and realized PnL).

Composite quality scoring for scan patterns. Reads CPCV / DSR / PBO
evidence from ``scan_patterns``, the rolling-30 directional WR from
``pattern_directional_quality_v`` (Phase 2), realized PnL stats from
``trading_trades`` (window settings-driven; default trailing 90d),
and computes a decay factor on-the-fly from
``pattern_alert_directional_outcome``. Persists the result to
``scan_patterns.quality_composite_score`` (mig 237).

Composite formula (new defaults — 2026-05-16)
---------------------------------------------

``composite = 0.10*clip(cpcv_sharpe/2.0, 0, 1)
            + 0.05*clip(deflated_sharpe/1.0, 0, 1)
            + 0.05*(1 - clip(pbo, 0, 1))
            + 0.35*directional_wr
            + 0.10*(1 - decay)
            + 0.35*realized_pnl_score*realized_evidence_score(n)``

Each component is normalized to ``[0, 1]`` so composite ∈ ``[0, 1]``
when weights sum to 1. Targets (cpcv→2.0, dsr→1.0) are calibrated to
the eligibility floor: ``cpcv_median_sharpe >= 1.0`` (the gate floor)
lands at half-credit, ``cpcv_median_sharpe == 2.0`` (academic
"excellent") lands at full credit. Patterns above 2.0 saturate.

Realized component
------------------

``realized_pnl_score = (clip(avg_pnl_pct / w_norm, -1, 1) + 1) / 2``
where ``avg_pnl_pct = avg(pnl / notional)`` over the trailing window of
CLOSED trades joined on ``scan_pattern_id``. For options, notional includes
the 100x contract multiplier. With
``w_norm = 0.01`` (default), +1%/trade saturates to 1.0 and −1%/trade
floors to 0.0; zero PnL is 0.5.

``realized_evidence_score(n) = 1 - exp(-n / tau)`` with default
``tau = 30``. At n=5 contributes ~15%; at n=30, ~63%; at n=85,
~94%. The two multiply: the effective realized contribution is
``realized_pnl_score * realized_evidence_score``.

When realized evidence is present, the directional-WR term is also
scaled by ``realized_pnl_score``. That keeps a high directional hit-rate
from masking fee/slippage-negative realized PnL.

NULL propagation
----------------

When ``realized_n_trades < 5`` (or no realized data at all), the
realized component is ZERO and the remaining five weights
re-normalize to sum to 1.0 (each multiplied by 1 / (1 - w_realized)).
This preserves the composite ∈ ``[0, 1]`` invariant. When CPCV / DSR
/ PBO / directional_wr / decay are NULL, the composite remains
``None`` (no magic-default fallback — advisor brief §2.6).

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
import math as _math
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.trading import ScanPattern
from .realized_pnl_sql import (
    clean_live_pattern_ev_exit_filter_sql,
    paper_dynamic_pattern_ev_exit_filter_sql,
    paper_trade_return_fraction_sql,
    trade_return_fraction_sql,
)

logger = logging.getLogger(__name__)

COMPOSITE_WEIGHT_KEYS = (
    "cpcv_sharpe",
    "deflated_sharpe",
    "pbo_inverse",
    "directional_wr",
    "decay_inverse",
    "realized",
)


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Numeric clip — numpy-free for the unit-test path."""
    if x is None:
        return None  # type: ignore[return-value]
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _finite_float_or_none(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except Exception:
        return None
    if not _math.isfinite(out):
        return None
    return out


def _finite_float_or_default(value: Any, default: float) -> float:
    out = _finite_float_or_none(value)
    return float(default) if out is None else out


def _is_saturated_deflated_sharpe(value: float) -> bool:
    return value >= 1.0


def _is_saturated_pbo(value: float) -> bool:
    return value <= 0.0


def _settings_float(settings_: Any, name: str, default: float) -> float:
    return _finite_float_or_default(getattr(settings_, name, default), default)


def _settings_int(settings_: Any, name: str, default: int) -> int:
    out = _finite_float_or_none(getattr(settings_, name, default))
    return int(default) if out is None else int(out)


def realized_pnl_score(
    avg_pnl_pct: Optional[float],
    w_norm: float,
) -> Optional[float]:
    """Normalized realized-PnL component, mapped to ``[0, 1]``.

    Formula: ``(clip(avg_pnl_pct / w_norm, -1, 1) + 1) / 2``.

    - ``avg_pnl_pct = +w_norm`` → ``1.0`` (full credit)
    - ``avg_pnl_pct = -w_norm`` → ``0.0`` (full debit)
    - ``avg_pnl_pct = 0``       → ``0.5`` (neutral)
    - Saturates outside ``[-w_norm, +w_norm]``.

    Returns ``None`` when ``avg_pnl_pct`` is ``None`` or ``w_norm`` is
    non-positive (NULL propagation — no magic-default fallback).
    """
    avg = _finite_float_or_none(avg_pnl_pct)
    norm = _finite_float_or_none(w_norm)
    if avg is None or norm is None or norm <= 0:
        return None
    normed = avg / norm
    if normed < -1.0:
        normed = -1.0
    elif normed > 1.0:
        normed = 1.0
    return (normed + 1.0) / 2.0


def realized_evidence_score(
    n: Optional[int],
    tau: float,
) -> float:
    """Sample-size confidence multiplier in ``[0, 1)``.

    ``1 - exp(-n / tau)``. At ``n = tau`` contributes ~63%, saturates
    near 1 as ``n → ∞``. Always defined for ``n >= 0`` and ``tau > 0``.
    """
    if n is None:
        raise TypeError("realized_evidence_score requires n, got None")
    if tau is None:
        raise TypeError("realized_evidence_score requires tau, got None")
    n_val = _finite_float_or_none(n)
    tau_val = _finite_float_or_none(tau)
    if n_val is None:
        raise TypeError("realized_evidence_score requires finite n")
    if tau_val is None:
        raise TypeError("realized_evidence_score requires finite tau")
    if n_val < 0:
        raise ValueError("realized_evidence_score requires n >= 0")
    if tau_val <= 0:
        raise ValueError("realized_evidence_score requires tau > 0")
    return 1.0 - _math.exp(-n_val / tau_val)


def compute_quality_composite_score(
    pat: ScanPattern,
    directional_wr: Optional[float],
    decay: Optional[float],
    weights: dict,
    realized_pnl_score: Optional[float] = None,
    realized_n_trades: int = 0,
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
        Six-weight dict + supporting config keys:
        ``cpcv_sharpe``, ``deflated_sharpe``, ``pbo_inverse``,
        ``directional_wr``, ``decay_inverse``, ``realized``,
        ``realized_pnl_normalizer_pct``, ``realized_evidence_tau``,
        and ``realized_window_days``. The six composite weights should
        sum to 1.0; the ``realized`` weight is dormant whenever the
        caller passes ``realized_pnl_score=None`` or
        ``realized_n_trades < 5``.
    realized_pnl_score : Optional[float]
        Normalized realized-PnL component in ``[0, 1]`` (see
        :func:`realized_pnl_score`). ``None`` means insufficient
        evidence (``realized_n_trades < 5`` OR no closed trades).
    realized_n_trades : int
        Count of closed trades in the realized window (used to scale
        the realized component by ``realized_evidence_score``).
    """
    cpcv = _finite_float_or_none(getattr(pat, "cpcv_median_sharpe", None))
    dsr = _finite_float_or_none(getattr(pat, "deflated_sharpe", None))
    pbo = _finite_float_or_none(getattr(pat, "pbo", None))
    directional_wr_f = _finite_float_or_none(directional_wr)
    decay_f = _finite_float_or_none(decay)

    if cpcv is None or dsr is None or pbo is None:
        return None
    if directional_wr_f is None or decay_f is None:
        return None

    w_cpcv = _finite_float_or_default(weights.get("cpcv_sharpe", 0.10), 0.10)
    w_dsr = _finite_float_or_default(weights.get("deflated_sharpe", 0.05), 0.05)
    w_pbo = _finite_float_or_default(weights.get("pbo_inverse", 0.05), 0.05)
    w_wr = _finite_float_or_default(weights.get("directional_wr", 0.35), 0.35)
    w_decay = _finite_float_or_default(weights.get("decay_inverse", 0.10), 0.10)
    w_realized = _finite_float_or_default(weights.get("realized", 0.35), 0.35)
    tau = _finite_float_or_default(weights.get("realized_evidence_tau", 30.0), 30.0)

    n_raw = _finite_float_or_none(realized_n_trades)
    n = int(n_raw) if n_raw is not None and n_raw > 0 else 0
    realized_quality_input = _finite_float_or_none(realized_pnl_score)
    has_realized = realized_quality_input is not None and n >= 5
    realized_quality = _clip(realized_quality_input) if has_realized else None

    active_terms: list[tuple[float, float]] = [
        (w_cpcv, _clip(cpcv / 2.0)),
    ]
    if not _is_saturated_deflated_sharpe(dsr):
        active_terms.append((w_dsr, _clip(dsr / 1.0)))
    if not _is_saturated_pbo(pbo):
        active_terms.append((w_pbo, 1.0 - _clip(pbo)))

    wr_component = _clip(directional_wr_f) * (
        realized_quality if realized_quality is not None else 1.0
    )
    active_terms.extend([
        (w_wr, wr_component),
        (w_decay, 1.0 - _clip(decay_f)),
    ])

    if not has_realized:
        active_weight = sum(w for w, _value in active_terms)
        if active_weight <= 0:
            return None
        return sum(w * value for w, value in active_terms) / active_weight

    evidence = realized_evidence_score(n, tau)
    active_terms.append((w_realized, float(realized_quality) * evidence))
    active_weight = sum(w for w, _value in active_terms)
    if active_weight <= 0:
        return None
    return sum(w * value for w, value in active_terms) / active_weight


def _load_directional_quality_map(db: Session) -> dict[int, dict[str, Any]]:
    """Per-pattern map of {scan_pattern_id: {wr, sample_n}} from the
    Phase 2 view ``pattern_directional_quality_v``."""
    rows = db.execute(text(
        "SELECT scan_pattern_id, "
        "       rolling_directional_wr, "
        "       rolling_sample_n, "
        "       packet_linked_sample_n, "
        "       packet_lineage_coverage "
        "FROM pattern_directional_quality_v"
    )).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        pid = int(r[0]) if r[0] is not None else None
        if pid is None:
            continue
        wr = float(r[1]) if r[1] is not None else None
        n = int(r[2]) if r[2] is not None else 0
        packet_n = int(r[3]) if r[3] is not None else 0
        coverage = float(r[4]) if r[4] is not None else None
        out[pid] = {
            "directional_wr": wr,
            "rolling_sample_n": n,
            "packet_linked_sample_n": packet_n,
            "packet_lineage_coverage": coverage,
        }
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
        "cpcv_sharpe": _settings_float(
            settings_, "chili_cohort_score_weight_cpcv_sharpe", 0.10,
        ),
        "deflated_sharpe": _settings_float(
            settings_, "chili_cohort_score_weight_deflated_sharpe", 0.05,
        ),
        "pbo_inverse": _settings_float(
            settings_, "chili_cohort_score_weight_pbo_inverse", 0.05,
        ),
        "directional_wr": _settings_float(
            settings_, "chili_cohort_score_weight_directional_wr", 0.35,
        ),
        "decay_inverse": _settings_float(
            settings_, "chili_cohort_score_weight_decay_inverse", 0.10,
        ),
        "realized": _settings_float(
            settings_, "chili_cohort_score_weight_realized", 0.35,
        ),
        "realized_pnl_normalizer_pct": _settings_float(
            settings_, "chili_cohort_score_realized_pnl_normalizer_pct", 0.01,
        ),
        "realized_evidence_tau": _settings_float(
            settings_, "chili_cohort_score_realized_evidence_tau", 30.0,
        ),
        "realized_window_days": _settings_int(
            settings_, "chili_cohort_score_realized_window_days", 90,
        ),
    }


def _composite_weight_sum(weights: dict[str, Any]) -> float:
    """Sum only the six score weights, excluding non-weight knobs."""
    return sum(
        _finite_float_or_default(weights.get(key, 0.0), 0.0)
        for key in COMPOSITE_WEIGHT_KEYS
    )


def _load_realized_pnl_map(
    db: Session,
    window_days: int,
    *,
    include_autotrader_paper_dynamic: bool = False,
) -> dict[int, dict[str, Any]]:
    """Per-pattern realized PnL stats over the trailing window.

    Returns ``{scan_pattern_id: {"n": int, "avg_pnl_pct": float,
    "total_pnl": float}}`` for every pattern with at least one
    computable realized-return sample in the window. The caller decides
    the n-floor (default 5) before treating ``avg_pnl_pct`` as a
    realized-component input.

    ``avg_pnl_pct`` is equal-weighted across trades:
    ``avg(pnl / notional)``. For options, notional includes the 100x
    contract multiplier. The schema-level guards
    (mig 214 check constraints) ensure ``entry_price > 0`` and
    ``quantity > 0`` on closed trades; the WHERE clause re-asserts
    them for safety. Sentinel ``scan_pattern_id = -1`` is excluded
    (``_NO_PATTERN_SENTINEL`` — see ``app/models/trading.py``).
    """
    rows = db.execute(
        text(f"""
            WITH realized_samples AS (
                SELECT
                    t.scan_pattern_id,
                    t.pnl,
                    {trade_return_fraction_sql("t")} AS realized_return_frac
                FROM trading_trades t
                WHERE t.scan_pattern_id IS NOT NULL
                  AND t.scan_pattern_id != -1
                  AND t.status = 'closed'
                  AND t.pnl IS NOT NULL
                  AND t.entry_price > 0
                  AND t.quantity > 0
                  AND t.exit_date > NOW() - make_interval(days => :window_days)
                  AND {clean_live_pattern_ev_exit_filter_sql("t")}
            )
            SELECT scan_pattern_id,
                   COUNT(realized_return_frac) AS n,
                   AVG(realized_return_frac) AS avg_pnl_pct,
                   SUM(pnl) AS total_pnl
            FROM realized_samples
            WHERE realized_return_frac IS NOT NULL
            GROUP BY scan_pattern_id
            """
        ),
        {"window_days": int(window_days)},
    ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        pid = int(r[0])
        out[pid] = {
            "n": int(r[1] or 0),
            "avg_pnl_pct": float(r[2]) if r[2] is not None else None,
            "total_pnl": float(r[3]) if r[3] is not None else 0.0,
            "live_n": int(r[1] or 0),
            "paper_dynamic_n": 0,
        }
    if include_autotrader_paper_dynamic:
        paper_rows = db.execute(
            text(f"""
                WITH realized_samples AS (
                    SELECT
                        pt.scan_pattern_id,
                        pt.pnl,
                        {paper_trade_return_fraction_sql("pt")} AS realized_return_frac
                    FROM trading_paper_trades pt
                    WHERE pt.scan_pattern_id IS NOT NULL
                      AND pt.scan_pattern_id != -1
                      AND pt.status = 'closed'
                      AND pt.pnl IS NOT NULL
                      AND pt.entry_price > 0
                      AND pt.quantity > 0
                      AND {paper_dynamic_pattern_ev_exit_filter_sql("pt")}
                      AND pt.exit_date > NOW() - make_interval(days => :window_days)
                      AND (
                        pt.paper_shadow_of_alert_id IS NOT NULL
                        OR COALESCE(pt.signal_json, '{{}}'::jsonb) @> '{{"auto_trader_v1": true}}'::jsonb
                        OR COALESCE(pt.signal_json, '{{}}'::jsonb) @> '{{"paper_shadow": true}}'::jsonb
                      )
                )
                SELECT scan_pattern_id,
                       COUNT(realized_return_frac) AS n,
                       AVG(realized_return_frac) AS avg_pnl_pct,
                       SUM(pnl) AS total_pnl
                FROM realized_samples
                WHERE realized_return_frac IS NOT NULL
                GROUP BY scan_pattern_id
                """
            ),
            {"window_days": int(window_days)},
        ).fetchall()
        for r in paper_rows:
            pid = int(r[0])
            paper_n = int(r[1] or 0)
            paper_avg = float(r[2]) if r[2] is not None else None
            paper_total = float(r[3]) if r[3] is not None else 0.0
            if paper_n <= 0 or paper_avg is None:
                continue
            cur = out.get(pid)
            if not cur:
                out[pid] = {
                    "n": paper_n,
                    "avg_pnl_pct": paper_avg,
                    "total_pnl": paper_total,
                    "live_n": 0,
                    "paper_dynamic_n": paper_n,
                }
                continue
            live_n = int(cur.get("live_n") or 0)
            live_avg = cur.get("avg_pnl_pct")
            total_n = live_n + paper_n
            if total_n <= 0:
                continue
            cur["avg_pnl_pct"] = (
                (float(live_avg or 0.0) * live_n) + (paper_avg * paper_n)
            ) / total_n
            cur["n"] = total_n
            cur["total_pnl"] = float(cur.get("total_pnl") or 0.0) + paper_total
            cur["paper_dynamic_n"] = int(cur.get("paper_dynamic_n") or 0) + paper_n
    return out


def _realized_component_for_pattern(
    pid: int,
    realized_map: dict[int, dict[str, Any]],
    weights: dict,
) -> tuple[Optional[float], int]:
    """Return ``(realized_pnl_score, n_trades)`` for a single pattern.

    Applies the n-floor (default 5) — patterns with fewer than 5 closed
    trades get ``realized_pnl_score = None`` (NULL propagation per
    advisor brief §2.6). Patterns absent from the map (zero closed
    trades in window) get ``(None, 0)``.
    """
    rec = realized_map.get(int(pid))
    if not rec:
        return (None, 0)
    n_raw = _finite_float_or_none(rec.get("n", 0))
    n = int(n_raw) if n_raw is not None and n_raw > 0 else 0
    avg = rec.get("avg_pnl_pct")
    if n < 5 or avg is None:
        return (None, n)
    w_norm = _finite_float_or_none(weights.get("realized_pnl_normalizer_pct", 0.01))
    if w_norm is None:
        return (None, n)
    return (realized_pnl_score(avg, w_norm), n)


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
    weight_sum = _composite_weight_sum(weights)
    if not (0.99 <= weight_sum <= 1.01):
        logger.warning(
            "[pattern_quality_score] weights sum to %.4f (expected ~1.0) — "
            "operator-tuned weights may produce composite scores outside [0,1]",
            weight_sum,
        )

    # f-evaluation-function-fix Tier A #3 (2026-05-18): require >=N
    # closed realized trades before the composite score is materialized.
    # Pre-fix, a pattern with 0 realized trades could still get a score
    # from re-normalized non-realized terms; n=2 patterns ranked above
    # n=86 pattern 585 in the 2026-05-16 diagnostic. Setting min_n=5 (the
    # same floor used by the realized COMPONENT) makes "no evidence ->
    # NULL score" hold end-to-end, so cohort-promote eligibility skips
    # those rows by construction. Set to 0 to restore prior behavior.
    min_realized_n = int(getattr(
        settings_, "chili_composite_min_realized_trades", 5,
    ))

    dq_map = _load_directional_quality_map(db)
    decay_map = _load_decay_map(db)
    include_paper_dynamic = bool(
        getattr(settings_, "chili_cohort_score_include_autotrader_paper_dynamic", True)
    )
    realized_map = _load_realized_pnl_map(
        db,
        int(weights.get("realized_window_days", 90)),
        include_autotrader_paper_dynamic=include_paper_dynamic,
    )

    patterns = (
        db.query(ScanPattern)
          .filter(ScanPattern.active.is_(True))
          .all()
    )

    scored = 0
    scored_with_realized = 0
    skipped_null_evidence = 0
    skipped_thin_directional = 0
    skipped_thin_realized = 0
    cleared = 0
    for pat in patterns:
        dq = dq_map.get(int(pat.id))
        wr = dq["directional_wr"] if dq else None
        sample_n = dq["rolling_sample_n"] if dq else 0
        decay = decay_map.get(int(pat.id))
        rp_score, rp_n = _realized_component_for_pattern(
            int(pat.id), realized_map, weights,
        )

        # Eligibility tightening from j.1: rolling_sample_n < 30 →
        # excluded entirely (decay un-computable).
        if sample_n < 30 or decay is None:
            new_score = None
            skipped_thin_directional += 1
        elif min_realized_n > 0 and int(rp_n or 0) < min_realized_n:
            # f-evaluation-function-fix Tier A #3: realized-evidence
            # floor short-circuit. Without realized trades, the score
            # would lean entirely on CPCV/DSR/PBO/directional/decay --
            # which can rank n=2 noise above n=86 alpha. Keep these
            # patterns out of cohort eligibility until they have data.
            new_score = None
            skipped_thin_realized += 1
        else:
            new_score = compute_quality_composite_score(
                pat, wr, decay, weights,
                realized_pnl_score=rp_score,
                realized_n_trades=rp_n,
            )
            if new_score is None:
                skipped_null_evidence += 1
            else:
                scored += 1
                if rp_score is not None and rp_n >= 5:
                    scored_with_realized += 1

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
        "scored_with_realized": scored_with_realized,
        "skipped_thin_directional": skipped_thin_directional,
        "skipped_thin_realized": skipped_thin_realized,
        "skipped_null_evidence": skipped_null_evidence,
        "cleared_to_null": cleared,
        "weight_sum": round(weight_sum, 4),
        "realized_window_days": int(weights.get("realized_window_days", 90)),
        "include_autotrader_paper_dynamic": include_paper_dynamic,
        "min_realized_n": min_realized_n,
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
    weight_sum = _composite_weight_sum(weights)
    if not (0.99 <= weight_sum <= 1.01):
        logger.warning(
            "[pattern_quality_score] streaming weights sum to %.4f "
            "(expected ~1.0)",
            weight_sum,
        )

    # f-evaluation-function-fix Tier A #3 (2026-05-18): same floor as
    # the non-streaming refresh path -- patterns with realized n below
    # ``chili_composite_min_realized_trades`` get composite=NULL.
    min_realized_n = int(getattr(
        settings_, "chili_composite_min_realized_trades", 5,
    ))

    dq_map = _load_directional_quality_map(db)
    decay_map = _load_decay_map(db)
    include_paper_dynamic = bool(
        getattr(settings_, "chili_cohort_score_include_autotrader_paper_dynamic", True)
    )
    realized_map = _load_realized_pnl_map(
        db,
        int(weights.get("realized_window_days", 90)),
        include_autotrader_paper_dynamic=include_paper_dynamic,
    )

    patterns = (
        db.query(ScanPattern)
          .filter(ScanPattern.active.is_(True))
          .order_by(ScanPattern.id.asc())
          .all()
    )

    scored = 0
    skipped_null_evidence = 0
    skipped_thin_directional = 0
    skipped_thin_realized = 0
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
        rp_score, rp_n = _realized_component_for_pattern(
            int(pat.id), realized_map, weights,
        )

        if sample_n < 30 or decay is None:
            new_score: Optional[float] = None
            skipped_thin_directional += 1
        elif min_realized_n > 0 and int(rp_n or 0) < min_realized_n:
            new_score = None
            skipped_thin_realized += 1
        else:
            new_score = compute_quality_composite_score(
                pat, wr, decay, weights,
                realized_pnl_score=rp_score,
                realized_n_trades=rp_n,
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
                    "realized_pnl_score": rp_score,
                    "realized_n_trades": rp_n,
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
        "skipped_thin_realized": skipped_thin_realized,
        "skipped_null_evidence": skipped_null_evidence,
        "cleared_to_null": cleared,
        "would_write": written if dry_run else None,
        "wrote": (0 if dry_run else written),
        "stopped_by_flag": stopped,
        "weight_sum": round(weight_sum, 4),
        "min_realized_n": min_realized_n,
        "include_autotrader_paper_dynamic": include_paper_dynamic,
        "pending_changes_sample": pending_changes[:8],
    }
    logger.info(
        "[pattern_quality_score] streaming refresh: %s", result,
    )
    return result
