"""Phase K - persistence layer for the divergence panel.

Runs the pure divergence model against per-pattern signals gathered
from the five existing substrate log tables (Phase A/B/F/G/H) and
writes one row into ``trading_pattern_divergence_log`` per
(pattern, sweep). Shadow-safe: never touches ``scan_patterns``, never
transitions lifecycle state.

Design
------

* **Single public entry-point per mode.** :func:`evaluate_pattern`
  (one pattern, one sweep) and :func:`run_sweep` (many patterns).
* **Refuses authoritative.** Until Phase K.2 opens explicitly the
  service raises :class:`RuntimeError` on ``mode_override="authoritative"``
  or ``brain_divergence_scorer_mode="authoritative"``.
* **Append-only.** Every sweep appends a new row; the deterministic
  ``divergence_id`` lets callers dedupe.
* **Off-mode short-circuit.** When
  ``brain_divergence_scorer_mode == "off"`` :func:`run_sweep` is a
  no-op and returns an empty list.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.divergence_ops_log import (
    format_divergence_ops_line,
)
from .divergence_model import (
    ALL_LAYERS,
    DivergenceConfig,
    DivergenceInput,
    DivergenceOutput,
    LAYER_BRACKET,
    LAYER_EXIT,
    LAYER_LEDGER,
    LAYER_SIZER,
    LAYER_VENUE,
    LayerSignal,
    compute_divergence,
)

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


def _effective_mode(override: str | None = None) -> str:
    m = (
        override
        or getattr(settings, "brain_divergence_scorer_mode", "off")
        or "off"
    ).lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def mode_is_authoritative(override: str | None = None) -> bool:
    return _effective_mode(override) == "authoritative"


def _ops_log_enabled() -> bool:
    return bool(
        getattr(settings, "brain_divergence_scorer_ops_log_enabled", True)
    )


def _config_from_settings() -> DivergenceConfig:
    return DivergenceConfig(
        layer_weights={
            LAYER_LEDGER: float(getattr(
                settings, "brain_divergence_scorer_layer_weight_ledger", 1.0,
            )),
            LAYER_EXIT: float(getattr(
                settings, "brain_divergence_scorer_layer_weight_exit", 1.0,
            )),
            LAYER_VENUE: float(getattr(
                settings, "brain_divergence_scorer_layer_weight_venue", 0.8,
            )),
            LAYER_BRACKET: float(getattr(
                settings, "brain_divergence_scorer_layer_weight_bracket", 1.0,
            )),
            LAYER_SIZER: float(getattr(
                settings, "brain_divergence_scorer_layer_weight_sizer", 1.0,
            )),
        },
        min_layers_sampled=int(getattr(
            settings, "brain_divergence_scorer_min_layers_sampled", 1,
        )),
        yellow_threshold=float(getattr(
            settings, "brain_divergence_scorer_yellow_threshold", 0.9,
        )),
        red_threshold=float(getattr(
            settings, "brain_divergence_scorer_red_threshold", 1.8,
        )),
    )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DivergenceSweepRow:
    log_id: int
    divergence_id: str
    scan_pattern_id: int
    severity: str
    score: float
    mode: str


@dataclass(frozen=True)
class DivergenceInputBundle:
    """One pattern's inputs for a single sweep.

    ``signals`` is the sequence of :class:`LayerSignal` rows pulled
    from the five source tables for the ``lookback_days`` window.
    """

    scan_pattern_id: int
    pattern_name: str | None
    signals: Sequence[LayerSignal]


# ---------------------------------------------------------------------------
# Signal gathering helpers (read-only)
# ---------------------------------------------------------------------------


def _severity_from_kind(kind: str | None) -> str:
    if not kind:
        return "green"
    if kind in ("agree",):
        return "green"
    if kind in ("qty_drift", "price_drift", "state_drift"):
        return "yellow"
    if kind in ("orphan_stop", "missing_stop", "broker_down"):
        return "red"
    # Unknown kind treated as yellow (observable deviation).
    return "yellow"


def _severity_from_divergence_bps(delta_bps: float | None) -> str:
    if delta_bps is None:
        return "green"
    ab = abs(float(delta_bps))
    if ab >= 200.0:
        return "red"
    if ab >= 50.0:
        return "yellow"
    return "green"


def _severity_from_ledger_agree(agree: bool | None) -> str:
    if agree is None:
        return "green"
    return "green" if agree else "yellow"


def _severity_from_exit_agree(agree: bool | None) -> str:
    if agree is None:
        return "green"
    return "green" if agree else "yellow"


def _severity_from_venue_bps(delta_bps: float | None) -> str:
    if delta_bps is None:
        return "green"
    ab = abs(float(delta_bps))
    if ab >= 100.0:
        return "red"
    if ab >= 25.0:
        return "yellow"
    return "green"


def gather_signals_for_pattern(
    db: Session,
    *,
    scan_pattern_id: int,
    lookback_days: int,
) -> list[LayerSignal]:
    """Pull per-layer signals from the five source tables.

    This is a **read-only** helper. Each layer's severity is derived
    from the single most-recent row in the lookback window:

    * Phase A ``trading_ledger_parity_log``: ``agree_bool`` false -> yellow.
    * Phase B ``trading_exit_parity_log``: ``agree_bool`` false -> yellow.
    * Phase F ``trading_venue_truth_log``: ``|realized_slippage_bps -
      expected_slippage_bps|`` thresholds.
    * Phase G ``trading_bracket_reconciliation_log``: ``kind`` mapped
      to severity.
    * Phase H ``trading_position_sizer_log``: ``|divergence_bps|``
      thresholds.

    Returns an empty list if the pattern has no signals.
    """
    signals: list[LayerSignal] = []

    # Phase A - ledger parity
    row = db.execute(text("""
        SELECT agree_bool, delta_abs, id
        FROM trading_ledger_parity_log
        WHERE scan_pattern_id = :pid
          AND created_at >= (NOW() - (:ld || ' days')::INTERVAL)
        ORDER BY created_at DESC
        LIMIT 1
    """), {"pid": int(scan_pattern_id), "ld": int(lookback_days)}).fetchone()
    if row is not None:
        agree = bool(row[0]) if row[0] is not None else None
        severity = _severity_from_ledger_agree(agree)
        signals.append(LayerSignal(
            layer=LAYER_LEDGER,
            severity=severity,
            reason_code=None if agree else "ledger_disagree",
            sample_size=1,
            source_row_id=int(row[2]) if row[2] is not None else None,
        ))

    # Phase B - exit parity
    row = db.execute(text("""
        SELECT agree_bool, id
        FROM trading_exit_parity_log
        WHERE scan_pattern_id = :pid
          AND created_at >= (NOW() - (:ld || ' days')::INTERVAL)
        ORDER BY created_at DESC
        LIMIT 1
    """), {"pid": int(scan_pattern_id), "ld": int(lookback_days)}).fetchone()
    if row is not None:
        agree = bool(row[0]) if row[0] is not None else None
        severity = _severity_from_exit_agree(agree)
        signals.append(LayerSignal(
            layer=LAYER_EXIT,
            severity=severity,
            reason_code=None if agree else "exit_disagree",
            sample_size=1,
            source_row_id=int(row[1]) if row[1] is not None else None,
        ))

    # Phase F - venue truth (join via trade -> scan_pattern_id on Trade table)
    row = db.execute(text("""
        SELECT vt.realized_slippage_bps, vt.expected_slippage_bps, vt.id
        FROM trading_venue_truth_log vt
        LEFT JOIN trading_trades t ON t.id = vt.trade_id
        WHERE t.scan_pattern_id = :pid
          AND vt.created_at >= (NOW() - (:ld || ' days')::INTERVAL)
        ORDER BY vt.created_at DESC
        LIMIT 1
    """), {"pid": int(scan_pattern_id), "ld": int(lookback_days)}).fetchone()
    if row is not None:
        realized = row[0]
        expected = row[1]
        delta = None
        if realized is not None and expected is not None:
            delta = float(realized) - float(expected)
        severity = _severity_from_venue_bps(delta)
        signals.append(LayerSignal(
            layer=LAYER_VENUE,
            severity=severity,
            reason_code=(
                f"delta_bps={delta:.2f}" if delta is not None else None
            ),
            sample_size=1,
            source_row_id=int(row[2]) if row[2] is not None else None,
        ))

    # Phase G - bracket reconciliation (join via trade_id -> scan_pattern_id)
    row = db.execute(text("""
        SELECT br.kind, br.severity, br.id
        FROM trading_bracket_reconciliation_log br
        LEFT JOIN trading_trades t ON t.id = br.trade_id
        WHERE t.scan_pattern_id = :pid
          AND br.observed_at >= (NOW() - (:ld || ' days')::INTERVAL)
        ORDER BY br.observed_at DESC
        LIMIT 1
    """), {"pid": int(scan_pattern_id), "ld": int(lookback_days)}).fetchone()
    if row is not None:
        kind = row[0]
        severity = _severity_from_kind(kind)
        signals.append(LayerSignal(
            layer=LAYER_BRACKET,
            severity=severity,
            reason_code=f"kind={kind}" if kind else None,
            sample_size=1,
            source_row_id=int(row[2]) if row[2] is not None else None,
        ))

    # Phase H - position sizer
    row = db.execute(text("""
        SELECT divergence_bps, id
        FROM trading_position_sizer_log
        WHERE pattern_id = :pid
          AND observed_at >= (NOW() - (:ld || ' days')::INTERVAL)
        ORDER BY observed_at DESC
        LIMIT 1
    """), {"pid": int(scan_pattern_id), "ld": int(lookback_days)}).fetchone()
    if row is not None:
        delta = row[0]
        severity = _severity_from_divergence_bps(delta)
        signals.append(LayerSignal(
            layer=LAYER_SIZER,
            severity=severity,
            reason_code=(
                f"divergence_bps={float(delta):.2f}" if delta is not None else None
            ),
            sample_size=1,
            source_row_id=int(row[1]) if row[1] is not None else None,
        ))

    return signals


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_pattern(
    db: Session,
    *,
    bundle: DivergenceInputBundle,
    as_of_key: str | None,
    mode_override: str | None = None,
    config: DivergenceConfig | None = None,
) -> DivergenceSweepRow | None:
    """Evaluate a single pattern and persist the row.

    Returns ``None`` in ``off`` mode. Raises ``RuntimeError`` in
    ``authoritative`` mode until Phase K.2 opens explicitly.
    """
    mode = _effective_mode(mode_override)
    if mode == "off":
        return None
    if mode == "authoritative":
        if _ops_log_enabled():
            logger.warning(
                format_divergence_ops_line(
                    event="divergence_refused_authoritative",
                    mode=mode,
                    scan_pattern_id=bundle.scan_pattern_id,
                    reason="phase_k_2_not_opened",
                )
            )
        raise RuntimeError(
            "divergence_scorer authoritative mode is not permitted "
            "until Phase K.2 is explicitly opened",
        )

    cfg = config or _config_from_settings()
    inp = DivergenceInput(
        scan_pattern_id=bundle.scan_pattern_id,
        pattern_name=bundle.pattern_name,
        as_of_key=as_of_key,
        signals=bundle.signals,
    )
    out: DivergenceOutput = compute_divergence(inp, config=cfg)

    as_of_date = as_of_key or datetime.utcnow().date().isoformat()
    now = datetime.utcnow()
    row = db.execute(text("""
        INSERT INTO trading_pattern_divergence_log (
            divergence_id, scan_pattern_id, pattern_name,
            as_of_date,
            ledger_severity, exit_severity, venue_severity,
            bracket_severity, sizer_severity,
            severity, score,
            layers_sampled, layers_agreed, layers_total,
            payload_json, mode,
            sweep_at, observed_at
        ) VALUES (
            :divergence_id, :scan_pattern_id, :pattern_name,
            CAST(:as_of_date AS DATE),
            :ledger_sev, :exit_sev, :venue_sev,
            :bracket_sev, :sizer_sev,
            :severity, :score,
            :layers_sampled, :layers_agreed, :layers_total,
            CAST(:payload AS JSONB), :mode,
            :now, :now
        )
        RETURNING id
    """), {
        "divergence_id": out.divergence_id,
        "scan_pattern_id": out.scan_pattern_id,
        "pattern_name": out.pattern_name,
        "as_of_date": as_of_date,
        "ledger_sev": out.ledger_severity,
        "exit_sev": out.exit_severity,
        "venue_sev": out.venue_severity,
        "bracket_sev": out.bracket_severity,
        "sizer_sev": out.sizer_severity,
        "severity": out.severity,
        "score": float(out.score),
        "layers_sampled": int(out.layers_sampled),
        "layers_agreed": int(out.layers_agreed),
        "layers_total": int(out.layers_total),
        "payload": json.dumps(out.payload, default=str, separators=(",", ":")),
        "mode": mode,
        "now": now,
    })
    new_id = int(row.scalar_one())
    db.commit()

    if _ops_log_enabled():
        logger.info(
            format_divergence_ops_line(
                event="divergence_persisted",
                mode=mode,
                divergence_id=out.divergence_id,
                scan_pattern_id=out.scan_pattern_id,
                pattern_name=out.pattern_name,
                severity=out.severity,
                score=float(out.score),
                layers_sampled=int(out.layers_sampled),
                layers_agreed=int(out.layers_agreed),
                as_of_key=as_of_key,
            )
        )

    return DivergenceSweepRow(
        log_id=new_id,
        divergence_id=out.divergence_id,
        scan_pattern_id=out.scan_pattern_id,
        severity=out.severity,
        score=float(out.score),
        mode=mode,
    )


def run_sweep(
    db: Session,
    *,
    bundles: Sequence[DivergenceInputBundle],
    as_of_date: date | str | None = None,
    mode_override: str | None = None,
    config: DivergenceConfig | None = None,
) -> list[DivergenceSweepRow]:
    """Iterate ``bundles`` and persist one row per pattern."""
    mode = _effective_mode(mode_override)
    if mode == "off":
        return []

    as_of_key = (
        as_of_date.isoformat()
        if isinstance(as_of_date, date)
        else (
            str(as_of_date)
            if as_of_date
            else datetime.utcnow().date().isoformat()
        )
    )

    rows: list[DivergenceSweepRow] = []
    for bundle in bundles:
        res = evaluate_pattern(
            db,
            bundle=bundle,
            as_of_key=as_of_key,
            mode_override=mode_override,
            config=config,
        )
        if res is not None:
            rows.append(res)
    return rows


def discover_active_patterns(
    db: Session,
    *,
    lookback_days: int,
    limit: int | None = None,
) -> list[tuple[int, str | None]]:
    """Return ``(scan_pattern_id, pattern_name)`` tuples with at least
    one signal in the lookback across the five source tables.

    Used by the scheduler to pick patterns worth sweeping.
    """
    sql = """
        WITH recent AS (
            SELECT DISTINCT scan_pattern_id
              FROM trading_ledger_parity_log
             WHERE created_at >= (NOW() - (:ld || ' days')::INTERVAL)
               AND scan_pattern_id IS NOT NULL
            UNION
            SELECT DISTINCT scan_pattern_id
              FROM trading_exit_parity_log
             WHERE created_at >= (NOW() - (:ld || ' days')::INTERVAL)
               AND scan_pattern_id IS NOT NULL
            UNION
            SELECT DISTINCT t.scan_pattern_id
              FROM trading_venue_truth_log v
              JOIN trading_trades t ON t.id = v.trade_id
             WHERE v.created_at >= (NOW() - (:ld || ' days')::INTERVAL)
               AND t.scan_pattern_id IS NOT NULL
            UNION
            SELECT DISTINCT t.scan_pattern_id
              FROM trading_bracket_reconciliation_log br
              JOIN trading_trades t ON t.id = br.trade_id
             WHERE br.observed_at >= (NOW() - (:ld || ' days')::INTERVAL)
               AND t.scan_pattern_id IS NOT NULL
            UNION
            SELECT DISTINCT pattern_id AS scan_pattern_id
              FROM trading_position_sizer_log
             WHERE observed_at >= (NOW() - (:ld || ' days')::INTERVAL)
               AND pattern_id IS NOT NULL
        )
        SELECT r.scan_pattern_id, sp.name
          FROM recent r
     LEFT JOIN scan_patterns sp ON sp.id = r.scan_pattern_id
         ORDER BY r.scan_pattern_id
    """
    if limit is not None:
        sql += " LIMIT :lim"
        rows = db.execute(
            text(sql),
            {"ld": int(lookback_days), "lim": int(limit)},
        ).fetchall()
    else:
        rows = db.execute(text(sql), {"ld": int(lookback_days)}).fetchall()
    return [(int(r[0]), r[1]) for r in rows if r[0] is not None]


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def divergence_summary(
    db: Session,
    *,
    lookback_days: int = 14,
) -> dict[str, Any]:
    """Frozen-shape diagnostics summary for divergence sweeps.

    Keys (stable, order-preserving):
      * mode
      * lookback_days
      * divergence_events_total
      * by_severity {green, yellow, red}
      * patterns_red
      * patterns_yellow
      * mean_score
      * layers_tracked
      * latest_divergence {divergence_id, scan_pattern_id,
                           pattern_name, severity, score, observed_at}
    """
    mode = _effective_mode()

    total = int(db.execute(text("""
        SELECT COUNT(*) FROM trading_pattern_divergence_log
        WHERE sweep_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0)

    by_sev_rows = db.execute(text("""
        SELECT severity, COUNT(*) FROM trading_pattern_divergence_log
        WHERE sweep_at >= (NOW() - (:ld || ' days')::INTERVAL)
        GROUP BY severity
    """), {"ld": int(lookback_days)}).fetchall()
    by_sev = {"green": 0, "yellow": 0, "red": 0}
    for sev, cnt in by_sev_rows:
        if sev in by_sev:
            by_sev[sev] = int(cnt or 0)

    patterns_red = int(db.execute(text("""
        SELECT COUNT(DISTINCT scan_pattern_id)
          FROM trading_pattern_divergence_log
         WHERE sweep_at >= (NOW() - (:ld || ' days')::INTERVAL)
           AND severity = 'red'
    """), {"ld": int(lookback_days)}).scalar_one() or 0)
    patterns_yellow = int(db.execute(text("""
        SELECT COUNT(DISTINCT scan_pattern_id)
          FROM trading_pattern_divergence_log
         WHERE sweep_at >= (NOW() - (:ld || ' days')::INTERVAL)
           AND severity = 'yellow'
    """), {"ld": int(lookback_days)}).scalar_one() or 0)

    mean_score = float(db.execute(text("""
        SELECT AVG(score) FROM trading_pattern_divergence_log
         WHERE sweep_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0.0)

    latest = db.execute(text("""
        SELECT divergence_id, scan_pattern_id, pattern_name,
               severity, score, observed_at
          FROM trading_pattern_divergence_log
         ORDER BY observed_at DESC
         LIMIT 1
    """)).fetchone()
    latest_payload: dict[str, Any] | None = None
    if latest:
        latest_payload = {
            "divergence_id": latest[0],
            "scan_pattern_id": latest[1],
            "pattern_name": latest[2],
            "severity": latest[3],
            "score": float(latest[4]) if latest[4] is not None else 0.0,
            "observed_at": latest[5].isoformat() if latest[5] else None,
        }

    return {
        "mode": mode,
        "lookback_days": int(lookback_days),
        "divergence_events_total": total,
        "by_severity": by_sev,
        "patterns_red": patterns_red,
        "patterns_yellow": patterns_yellow,
        "mean_score": round(mean_score, 6),
        "layers_tracked": list(ALL_LAYERS),
        "latest_divergence": latest_payload,
    }


__all__ = [
    "DivergenceInputBundle",
    "DivergenceSweepRow",
    "discover_active_patterns",
    "divergence_summary",
    "evaluate_pattern",
    "gather_signals_for_pattern",
    "mode_is_active",
    "mode_is_authoritative",
    "run_sweep",
]
