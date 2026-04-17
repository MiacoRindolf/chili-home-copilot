"""Phase M.1 — persistence layer for the pattern x regime performance ledger.

Joins closed paper trades (``trading_paper_trades`` where
``status='closed'``) against the latest-on-or-before-entry L.17 – L.22
regime snapshot per dimension, runs the pure
:mod:`pattern_regime_performance_model`, and writes one aggregate row
per (pattern_id, regime_dimension, regime_label) to
``trading_pattern_regime_performance_daily``.

Design
------
* **Two public entry-points.** :func:`compute_and_persist` writes all
  cells for a single ``as_of_date`` and emits ops log entries.
  :func:`pattern_regime_perf_summary` returns the diagnostics dict
  for the FastAPI route.
* **Refuses authoritative.** Until Phase M.2 opens explicitly the
  service raises :class:`RuntimeError` on
  ``mode_override="authoritative"`` or
  ``brain_pattern_regime_perf_mode="authoritative"``. A refusal ops
  line is emitted before the raise so ops / release blockers can
  see the attempt.
* **Append-only.** Every call appends all cells for the run. The
  deterministic ``ledger_run_id`` keyed on ``(as_of_date,
  window_days)`` lets callers dedupe.
* **Off-mode short-circuit.** When ``brain_pattern_regime_perf_mode
  == "off"`` :func:`compute_and_persist` emits a single skip line and
  returns ``None``.
* **Additive-only.** No downstream consumer (scanner, promotion,
  sizing, alerts, NetEdgeRanker, playbook) reads this table in M.1.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.pattern_regime_perf_ops_log import (
    format_pattern_regime_perf_ops_line,
)
from .pattern_regime_performance_model import (
    DEFAULT_DIMENSIONS,
    DIMENSION_BREADTH_LABEL,
    DIMENSION_CORRELATION_LABEL,
    DIMENSION_CROSS_ASSET_LABEL,
    DIMENSION_DISPERSION_LABEL,
    DIMENSION_MACRO_REGIME,
    DIMENSION_SESSION_LABEL,
    DIMENSION_TICKER_REGIME,
    DIMENSION_VOL_REGIME,
    ClosedTradeRecord,
    PatternRegimeCell,
    PatternRegimePerfConfig,
    PatternRegimePerfInput,
    PatternRegimePerfOutput,
    RegimeLookup,
    build_pattern_regime_cells,
    compute_ledger_run_id,
)

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------


def _effective_mode(override: str | None = None) -> str:
    m = (
        override
        or getattr(settings, "brain_pattern_regime_perf_mode", "off")
        or "off"
    ).lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def mode_is_authoritative(override: str | None = None) -> bool:
    return _effective_mode(override) == "authoritative"


def _ops_log_enabled() -> bool:
    return bool(
        getattr(settings, "brain_pattern_regime_perf_ops_log_enabled", True)
    )


def _config_from_settings() -> PatternRegimePerfConfig:
    return PatternRegimePerfConfig(
        window_days=int(
            getattr(settings, "brain_pattern_regime_perf_window_days", 90)
        ),
        min_trades_per_cell=int(
            getattr(
                settings,
                "brain_pattern_regime_perf_min_trades_per_cell",
                3,
            )
        ),
        dimensions=DEFAULT_DIMENSIONS,
    )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternRegimePerfRunRef:
    """Thin reference to a persisted ledger run."""

    ledger_run_id: str
    as_of_date: date
    window_days: int
    cells_persisted: int
    mode: str


# ---------------------------------------------------------------------------
# Trade fetching
# ---------------------------------------------------------------------------


def _today_utc_date() -> date:
    return datetime.now(tz=timezone.utc).date()


def _fetch_closed_trades(
    db: Session, *, as_of_date: date, window_days: int
) -> List[ClosedTradeRecord]:
    """Load closed paper trades in the rolling exit_date window."""
    start = as_of_date - timedelta(days=int(window_days))
    end = as_of_date - timedelta(days=1)
    rows = db.execute(
        text(
            """
            SELECT scan_pattern_id, ticker, entry_date, exit_date, pnl_pct
              FROM trading_paper_trades
             WHERE status = 'closed'
               AND scan_pattern_id IS NOT NULL
               AND exit_date IS NOT NULL
               AND pnl_pct IS NOT NULL
               AND DATE(exit_date) >= :start
               AND DATE(exit_date) <= :end
            """
        ),
        {"start": start, "end": end},
    ).fetchall()

    trades: List[ClosedTradeRecord] = []
    for r in rows:
        try:
            entry = r[2]
            exit_ = r[3]
            entry_d = entry.date() if isinstance(entry, datetime) else entry
            exit_d = exit_.date() if isinstance(exit_, datetime) else exit_
            hold: Optional[float]
            if entry is not None and exit_ is not None:
                try:
                    hold = float(
                        (exit_ - entry).total_seconds() / 86400.0
                    )
                except Exception:
                    hold = None
                if hold is not None and hold < 0:
                    hold = 0.0
            else:
                hold = None
            trades.append(
                ClosedTradeRecord(
                    pattern_id=int(r[0]),
                    ticker=str(r[1]),
                    entry_date=entry_d,
                    exit_date=exit_d,
                    pnl_pct=float(r[4]),
                    hold_days=hold,
                )
            )
        except Exception as exc:
            logger.warning(
                "[pattern_regime_perf] skipping malformed paper trade row: %s",
                exc,
            )
            continue
    return trades


# ---------------------------------------------------------------------------
# Regime lookup building (one SQL per dimension)
# ---------------------------------------------------------------------------


def _load_market_wide_labels(
    db: Session, *, table: str, col: str, start: date, end: date
) -> List[Tuple[date, str]]:
    """Return (as_of_date, label) sorted ascending for one market-wide
    regime dimension. ``start``/``end`` bracket loads to reasonable row
    counts; callers extend them slightly beyond the trade window to
    cover entry_date - N boundary cases.
    """
    rows = db.execute(
        text(
            f"""
            SELECT as_of_date, {col}
              FROM {table}
             WHERE as_of_date >= :start
               AND as_of_date <= :end
               AND {col} IS NOT NULL
             ORDER BY as_of_date ASC
            """
        ),
        {"start": start, "end": end},
    ).fetchall()
    out: List[Tuple[date, str]] = []
    for r in rows:
        d = r[0]
        if isinstance(d, datetime):
            d = d.date()
        out.append((d, str(r[1])))
    return out


def _load_ticker_keyed_labels(
    db: Session,
    *,
    table: str,
    col: str,
    tickers: Sequence[str],
    start: date,
    end: date,
) -> Dict[str, List[Tuple[date, str]]]:
    """Return ``{ticker: [(as_of_date, label), ...]}`` ascending."""
    if not tickers:
        return {}
    tickers_uniq = sorted({str(t).upper() for t in tickers})
    # Postgres IN list via expanding bindparams
    stmt = text(
        f"""
        SELECT ticker, as_of_date, {col}
          FROM {table}
         WHERE ticker = ANY(:tickers)
           AND as_of_date >= :start
           AND as_of_date <= :end
           AND {col} IS NOT NULL
         ORDER BY ticker ASC, as_of_date ASC
        """
    )
    rows = db.execute(
        stmt, {"tickers": tickers_uniq, "start": start, "end": end}
    ).fetchall()
    out: Dict[str, List[Tuple[date, str]]] = {}
    for r in rows:
        t = str(r[0]).upper()
        d = r[1]
        if isinstance(d, datetime):
            d = d.date()
        out.setdefault(t, []).append((d, str(r[2])))
    return out


def _build_regime_lookup(
    db: Session,
    *,
    trades: Sequence[ClosedTradeRecord],
    window_start: date,
    window_end: date,
) -> RegimeLookup:
    """Build the RegimeLookup covering all 8 dimensions.

    Loads each dimension with a small buffer before window_start so
    trades near the window boundary can still resolve a most-recent
    snapshot that predates the window.
    """
    buffer_days = 45  # extra room for pre-window latest-snapshot lookups
    load_start = window_start - timedelta(days=buffer_days)
    load_end = window_end

    lookup = RegimeLookup()

    # L.17 macro regime
    lookup.market_wide[DIMENSION_MACRO_REGIME] = _load_market_wide_labels(
        db,
        table="trading_macro_regime_snapshots",
        col="regime_label",
        start=load_start,
        end=load_end,
    )

    # L.18 breadth
    lookup.market_wide[DIMENSION_BREADTH_LABEL] = _load_market_wide_labels(
        db,
        table="trading_breadth_relstr_snapshots",
        col="breadth_composite_label",
        start=load_start,
        end=load_end,
    )

    # L.19 cross-asset
    lookup.market_wide[
        DIMENSION_CROSS_ASSET_LABEL
    ] = _load_market_wide_labels(
        db,
        table="trading_cross_asset_snapshots",
        col="composite_label",
        start=load_start,
        end=load_end,
    )

    # L.21 vol_dispersion — three dimensions from the same table
    vol_rows = db.execute(
        text(
            """
            SELECT
                as_of_date,
                vol_regime_label,
                dispersion_label,
                correlation_label
              FROM trading_vol_dispersion_snapshots
             WHERE as_of_date >= :start
               AND as_of_date <= :end
             ORDER BY as_of_date ASC
            """
        ),
        {"start": load_start, "end": load_end},
    ).fetchall()
    vol_rows_list: List[Tuple[date, str, str, str]] = []
    for r in vol_rows:
        d = r[0]
        if isinstance(d, datetime):
            d = d.date()
        vol_rows_list.append(
            (
                d,
                str(r[1]) if r[1] is not None else "",
                str(r[2]) if r[2] is not None else "",
                str(r[3]) if r[3] is not None else "",
            )
        )
    lookup.market_wide[DIMENSION_VOL_REGIME] = [
        (d, lab) for d, lab, _, _ in vol_rows_list if lab
    ]
    lookup.market_wide[DIMENSION_DISPERSION_LABEL] = [
        (d, lab) for d, _, lab, _ in vol_rows_list if lab
    ]
    lookup.market_wide[DIMENSION_CORRELATION_LABEL] = [
        (d, lab) for d, _, _, lab in vol_rows_list if lab
    ]

    # L.22 intraday session
    lookup.market_wide[DIMENSION_SESSION_LABEL] = _load_market_wide_labels(
        db,
        table="trading_intraday_session_snapshots",
        col="session_label",
        start=load_start,
        end=load_end,
    )

    # L.20 per-ticker regime
    tickers = [t.ticker for t in trades]
    lookup.ticker_keyed[DIMENSION_TICKER_REGIME] = _load_ticker_keyed_labels(
        db,
        table="trading_ticker_regime_snapshots",
        col="regime_label",
        tickers=tickers,
        start=load_start,
        end=load_end,
    )

    lookup.sort_inplace()
    return lookup


# ---------------------------------------------------------------------------
# Persist + max_patterns cap
# ---------------------------------------------------------------------------


def _apply_max_patterns_cap(
    trades: Sequence[ClosedTradeRecord], *, max_patterns: int
) -> Tuple[List[ClosedTradeRecord], int]:
    """Keep only the top-N patterns by trade count; return survivors +
    how many patterns were truncated."""
    if not trades or max_patterns <= 0:
        return list(trades), 0
    counts: Dict[int, int] = {}
    for t in trades:
        counts[t.pattern_id] = counts.get(t.pattern_id, 0) + 1
    if len(counts) <= max_patterns:
        return list(trades), 0
    kept = set(
        p
        for p, _ in sorted(
            counts.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )[:max_patterns]
    )
    survivors = [t for t in trades if t.pattern_id in kept]
    truncated = len(counts) - max_patterns
    return survivors, int(truncated)


def _insert_cell_row(
    db: Session,
    *,
    cell: PatternRegimeCell,
    ledger_run_id: str,
    as_of_date: date,
    window_days: int,
    config_payload: Dict[str, Any],
    mode: str,
) -> int:
    payload = dict(config_payload)
    payload.update(cell.payload or {})
    payload_json = json.dumps(payload, default=str)
    now = datetime.utcnow()
    row = db.execute(
        text(
            """
            INSERT INTO trading_pattern_regime_performance_daily (
                ledger_run_id, as_of_date, window_days,
                pattern_id, regime_dimension, regime_label,
                n_trades, n_wins, hit_rate,
                mean_pnl_pct, median_pnl_pct, sum_pnl,
                expectancy, mean_win_pct, mean_loss_pct,
                profit_factor, sharpe_proxy, avg_hold_days,
                has_confidence,
                payload_json, mode, computed_at
            ) VALUES (
                :ledger_run_id, :as_of_date, :window_days,
                :pattern_id, :regime_dimension, :regime_label,
                :n_trades, :n_wins, :hit_rate,
                :mean_pnl_pct, :median_pnl_pct, :sum_pnl,
                :expectancy, :mean_win_pct, :mean_loss_pct,
                :profit_factor, :sharpe_proxy, :avg_hold_days,
                :has_confidence,
                CAST(:payload_json AS JSONB), :mode, :computed_at
            ) RETURNING id
            """
        ),
        {
            "ledger_run_id": ledger_run_id,
            "as_of_date": as_of_date,
            "window_days": int(window_days),
            "pattern_id": int(cell.pattern_id),
            "regime_dimension": str(cell.regime_dimension),
            "regime_label": str(cell.regime_label),
            "n_trades": int(cell.n_trades),
            "n_wins": int(cell.n_wins),
            "hit_rate": cell.hit_rate,
            "mean_pnl_pct": cell.mean_pnl_pct,
            "median_pnl_pct": cell.median_pnl_pct,
            "sum_pnl": cell.sum_pnl,
            "expectancy": cell.expectancy,
            "mean_win_pct": cell.mean_win_pct,
            "mean_loss_pct": cell.mean_loss_pct,
            "profit_factor": cell.profit_factor,
            "sharpe_proxy": cell.sharpe_proxy,
            "avg_hold_days": cell.avg_hold_days,
            "has_confidence": bool(cell.has_confidence),
            "payload_json": payload_json,
            "mode": mode,
            "computed_at": now,
        },
    ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_and_persist(
    db: Session,
    *,
    as_of_date: date | None = None,
    mode_override: str | None = None,
    trades_override: Sequence[ClosedTradeRecord] | None = None,
    lookup_override: RegimeLookup | None = None,
) -> PatternRegimePerfRunRef | None:
    """Run one daily ledger computation and persist all cells.

    ``trades_override`` / ``lookup_override`` are used by the Docker
    soak and smoke tests to feed deterministic synthetic inputs. In
    production both are ``None`` and the service pulls closed paper
    trades + L.17 - L.22 snapshots live.
    """
    mode = _effective_mode(mode_override)
    as_of = as_of_date or _today_utc_date()
    cfg = _config_from_settings()
    max_patterns = int(
        getattr(settings, "brain_pattern_regime_perf_max_patterns", 500)
    )

    if mode == "off":
        if _ops_log_enabled():
            logger.info(
                format_pattern_regime_perf_ops_line(
                    event="pattern_regime_perf_skipped",
                    mode=mode,
                    as_of_date=as_of.isoformat(),
                    window_days=int(cfg.window_days),
                    min_trades_per_cell=int(cfg.min_trades_per_cell),
                    reason="mode_off",
                )
            )
        return None

    if mode == "authoritative":
        if _ops_log_enabled():
            logger.warning(
                format_pattern_regime_perf_ops_line(
                    event="pattern_regime_perf_refused_authoritative",
                    mode=mode,
                    as_of_date=as_of.isoformat(),
                    window_days=int(cfg.window_days),
                    reason="M.1_shadow_only",
                )
            )
        raise RuntimeError(
            "pattern_regime_perf authoritative mode is not permitted "
            "until Phase M.2 is explicitly opened"
        )

    # --- Load or inject trades + lookup ---------------------------------
    if trades_override is not None:
        trades: List[ClosedTradeRecord] = list(trades_override)
    else:
        trades = _fetch_closed_trades(
            db, as_of_date=as_of, window_days=int(cfg.window_days)
        )

    trades_post_cap, truncated_patterns = _apply_max_patterns_cap(
        trades, max_patterns=max_patterns
    )
    if truncated_patterns > 0 and _ops_log_enabled():
        logger.info(
            format_pattern_regime_perf_ops_line(
                event="pattern_regime_perf_skipped",
                mode=mode,
                as_of_date=as_of.isoformat(),
                window_days=int(cfg.window_days),
                truncated_patterns=int(truncated_patterns),
                max_patterns=int(max_patterns),
                reason="pattern_cap",
            )
        )

    if lookup_override is not None:
        lookup = lookup_override
    elif trades_post_cap:
        window_start = as_of - timedelta(days=int(cfg.window_days))
        window_end = as_of
        lookup = _build_regime_lookup(
            db,
            trades=trades_post_cap,
            window_start=window_start,
            window_end=window_end,
        )
    else:
        lookup = RegimeLookup()

    # --- Pure model ------------------------------------------------------
    inp = PatternRegimePerfInput(
        as_of_date=as_of,
        trades=trades_post_cap,
        lookup=lookup,
        config=cfg,
    )
    out: PatternRegimePerfOutput = build_pattern_regime_cells(inp)
    confident = sum(1 for c in out.cells if c.has_confidence)

    if _ops_log_enabled():
        logger.info(
            format_pattern_regime_perf_ops_line(
                event="pattern_regime_perf_computed",
                mode=mode,
                as_of_date=as_of.isoformat(),
                ledger_run_id=out.ledger_run_id,
                window_days=int(cfg.window_days),
                min_trades_per_cell=int(cfg.min_trades_per_cell),
                max_patterns=int(max_patterns),
                pattern_count=int(out.patterns_observed),
                trade_count=int(out.total_trades_observed),
                cell_count=int(len(out.cells)),
                confident_cells=int(confident),
                unavailable_cells=int(out.unavailable_cells),
                dimensions_count=int(len(cfg.dimensions)),
            )
        )

    # --- Persist all cells ------------------------------------------------
    config_payload = {
        "config": cfg.as_mapping(),
        "patterns_observed": int(out.patterns_observed),
        "total_trades_observed": int(out.total_trades_observed),
        "unavailable_cells": int(out.unavailable_cells),
    }
    persisted = 0
    try:
        for cell in out.cells:
            _insert_cell_row(
                db,
                cell=cell,
                ledger_run_id=out.ledger_run_id,
                as_of_date=out.as_of_date,
                window_days=int(out.window_days),
                config_payload=config_payload,
                mode=mode,
            )
            persisted += 1
        db.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[pattern_regime_perf] bulk insert failed at cell %d: %s",
            persisted,
            exc,
        )
        db.rollback()
        return None

    if _ops_log_enabled():
        logger.info(
            format_pattern_regime_perf_ops_line(
                event="pattern_regime_perf_persisted",
                mode=mode,
                as_of_date=as_of.isoformat(),
                ledger_run_id=out.ledger_run_id,
                window_days=int(cfg.window_days),
                cell_count=int(persisted),
                confident_cells=int(confident),
                pattern_count=int(out.patterns_observed),
                trade_count=int(out.total_trades_observed),
            )
        )

    return PatternRegimePerfRunRef(
        ledger_run_id=out.ledger_run_id,
        as_of_date=out.as_of_date,
        window_days=int(out.window_days),
        cells_persisted=persisted,
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Diagnostics summary
# ---------------------------------------------------------------------------


def _latest_as_of(db: Session) -> Optional[date]:
    row = db.execute(
        text(
            """
            SELECT MAX(as_of_date)
              FROM trading_pattern_regime_performance_daily
            """
        )
    ).fetchone()
    if row is None or row[0] is None:
        return None
    d = row[0]
    return d.date() if isinstance(d, datetime) else d


def _latest_ledger_run_id(db: Session) -> Optional[str]:
    row = db.execute(
        text(
            """
            SELECT ledger_run_id
              FROM trading_pattern_regime_performance_daily
             ORDER BY computed_at DESC
             LIMIT 1
            """
        )
    ).fetchone()
    if row is None:
        return None
    return str(row[0])


def pattern_regime_perf_summary(
    db: Session, *, lookback_days: int = 14
) -> Dict[str, Any]:
    """Frozen-shape diagnostics summary.

    Keys (stable):

    * ``mode``
    * ``lookback_days``
    * ``window_days``
    * ``min_trades_per_cell``
    * ``latest_as_of_date`` (ISO-8601 or ``None``)
    * ``latest_ledger_run_id`` (str or ``None``)
    * ``ledger_rows_total``
    * ``confident_cells_total``
    * ``by_dimension`` (8 entries, each with
      ``total_cells``, ``confident_cells``, ``by_label``)
    * ``top_pattern_label_expectancy`` — top 25 confident cells
    * ``bottom_pattern_label_expectancy`` — bottom 25 confident cells
    """
    mode = _effective_mode()
    ld = int(lookback_days)
    cfg = _config_from_settings()

    latest_date = _latest_as_of(db)
    latest_run = _latest_ledger_run_id(db)

    rows_total = int(
        db.execute(
            text(
                """
                SELECT COUNT(*)
                  FROM trading_pattern_regime_performance_daily
                 WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
                """
            ),
            {"ld": ld},
        ).scalar_one()
        or 0
    )
    confident_total = int(
        db.execute(
            text(
                """
                SELECT COUNT(*)
                  FROM trading_pattern_regime_performance_daily
                 WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
                   AND has_confidence
                """
            ),
            {"ld": ld},
        ).scalar_one()
        or 0
    )

    # by_dimension block
    by_dimension: Dict[str, Dict[str, Any]] = {
        d: {"total_cells": 0, "confident_cells": 0, "by_label": {}}
        for d in DEFAULT_DIMENSIONS
    }

    totals_rows = db.execute(
        text(
            """
            SELECT regime_dimension, COUNT(*)
              FROM trading_pattern_regime_performance_daily
             WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
             GROUP BY regime_dimension
            """
        ),
        {"ld": ld},
    ).fetchall()
    for dim, cnt in totals_rows:
        k = str(dim)
        if k in by_dimension:
            by_dimension[k]["total_cells"] = int(cnt or 0)

    confident_rows = db.execute(
        text(
            """
            SELECT regime_dimension, COUNT(*)
              FROM trading_pattern_regime_performance_daily
             WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
               AND has_confidence
             GROUP BY regime_dimension
            """
        ),
        {"ld": ld},
    ).fetchall()
    for dim, cnt in confident_rows:
        k = str(dim)
        if k in by_dimension:
            by_dimension[k]["confident_cells"] = int(cnt or 0)

    label_rows = db.execute(
        text(
            """
            SELECT regime_dimension, regime_label, COUNT(*)
              FROM trading_pattern_regime_performance_daily
             WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
             GROUP BY regime_dimension, regime_label
            """
        ),
        {"ld": ld},
    ).fetchall()
    for dim, lab, cnt in label_rows:
        k = str(dim)
        if k in by_dimension:
            by_dimension[k]["by_label"][str(lab)] = int(cnt or 0)

    # top / bottom by expectancy among confident cells
    def _extreme(order: str) -> List[Dict[str, Any]]:
        rows = db.execute(
            text(
                f"""
                SELECT pattern_id, regime_dimension, regime_label,
                       n_trades, hit_rate, expectancy, profit_factor
                  FROM trading_pattern_regime_performance_daily
                 WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
                   AND has_confidence
                   AND expectancy IS NOT NULL
                 ORDER BY expectancy {order} NULLS LAST, pattern_id ASC
                 LIMIT 25
                """
            ),
            {"ld": ld},
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "pattern_id": int(r[0]),
                    "regime_dimension": str(r[1]),
                    "regime_label": str(r[2]),
                    "n_trades": int(r[3] or 0),
                    "hit_rate": None if r[4] is None else float(r[4]),
                    "expectancy": None if r[5] is None else float(r[5]),
                    "profit_factor": (
                        None if r[6] is None else float(r[6])
                    ),
                }
            )
        return out

    return {
        "mode": mode,
        "lookback_days": ld,
        "window_days": int(cfg.window_days),
        "min_trades_per_cell": int(cfg.min_trades_per_cell),
        "latest_as_of_date": (
            latest_date.isoformat() if latest_date is not None else None
        ),
        "latest_ledger_run_id": latest_run,
        "ledger_rows_total": rows_total,
        "confident_cells_total": confident_total,
        "by_dimension": by_dimension,
        "top_pattern_label_expectancy": _extreme("DESC"),
        "bottom_pattern_label_expectancy": _extreme("ASC"),
    }


__all__ = [
    "PatternRegimePerfRunRef",
    "_effective_mode",
    "mode_is_active",
    "mode_is_authoritative",
    "compute_and_persist",
    "pattern_regime_perf_summary",
]
