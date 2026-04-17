"""Triple-barrier labeler (Phase D, shadow-safe DB writer).

Connects the pure math in :mod:`triple_barrier` to the persistence layer.

Responsibilities:
  * Convert a MarketSnapshot (or an arbitrary ``ticker, label_date, entry_close``)
    into a triple-barrier row in ``trading_triple_barrier_labels``.
  * Idempotent upsert on the configured barrier tuple.
  * Emit one ``[triple_barrier_ops] event=label_write ...`` line per write
    and one ``event=run_summary`` line per batch invocation.

The labeler **does not** change promotion behavior. It only populates a
label store; Phase D uses those labels downstream to compute expected-PnL
and Brier-based economic scores (see :mod:`promotion_metric`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.triple_barrier_ops_log import (
    format_triple_barrier_ops_line,
)
from .triple_barrier import (
    TripleBarrierConfig,
    TripleBarrierLabel,
    compute_label,
)

logger = logging.getLogger(__name__)

_ALLOWED_MODES = ("off", "shadow", "authoritative")


def _effective_mode(override: str | None = None) -> str:
    mode = (override or getattr(settings, "brain_triple_barrier_mode", "off") or "off").lower()
    if mode not in _ALLOWED_MODES:
        mode = "off"
    return mode


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def _ops_log_enabled() -> bool:
    return bool(getattr(settings, "brain_triple_barrier_ops_log_enabled", True))


@dataclass
class LabelWriteOutcome:
    """Result of trying to persist a single label."""
    inserted: bool
    label: TripleBarrierLabel
    ticker: str
    label_date: date
    side: str
    cfg: TripleBarrierConfig
    snapshot_id: int | None = None


@dataclass
class LabelerReport:
    """Summary of one labeler invocation."""
    mode: str
    requested: int = 0
    written: int = 0
    skipped_existing: int = 0
    missing_data: int = 0
    labels_tp: int = 0
    labels_sl: int = 0
    labels_timeout: int = 0
    errors: int = 0
    details: list[LabelWriteOutcome] = field(default_factory=list)


def _upsert_label_row(
    db: Session,
    *,
    ticker: str,
    label_date: date,
    side: str,
    cfg: TripleBarrierConfig,
    label: TripleBarrierLabel,
    snapshot_id: int | None,
    mode: str,
) -> bool:
    """Idempotent insert. Returns True when a new row was inserted."""
    params = {
        "snapshot_id": snapshot_id,
        "ticker": ticker.upper(),
        "label_date": label_date,
        "side": side,
        "tp_pct": float(cfg.tp_pct),
        "sl_pct": float(cfg.sl_pct),
        "max_bars": int(cfg.max_bars),
        "entry_close": float(label.entry_close),
        "tp_price": float(label.tp_price),
        "sl_price": float(label.sl_price),
        "label": int(label.label),
        "barrier_hit": label.barrier_hit,
        "exit_bar_idx": int(label.exit_bar_idx),
        "realized_return_pct": float(label.realized_return_pct),
        "mode": mode,
    }
    sql = text(
        """
        INSERT INTO trading_triple_barrier_labels (
            snapshot_id, ticker, label_date, side,
            tp_pct, sl_pct, max_bars,
            entry_close, tp_price, sl_price,
            label, barrier_hit, exit_bar_idx, realized_return_pct,
            mode, created_at
        ) VALUES (
            :snapshot_id, :ticker, :label_date, :side,
            :tp_pct, :sl_pct, :max_bars,
            :entry_close, :tp_price, :sl_price,
            :label, :barrier_hit, :exit_bar_idx, :realized_return_pct,
            :mode, NOW()
        )
        ON CONFLICT ON CONSTRAINT uq_triple_barrier_labels DO NOTHING
        RETURNING id
        """
    )
    res = db.execute(sql, params).fetchone()
    db.commit()
    return res is not None


def label_single(
    db: Session,
    *,
    ticker: str,
    label_date: date,
    entry_close: float,
    future_bars: Sequence[object] | Iterable[object],
    side: str = "long",
    cfg: TripleBarrierConfig | None = None,
    snapshot_id: int | None = None,
    mode_override: str | None = None,
) -> LabelWriteOutcome:
    """Label a single (ticker, date, entry_close) pair against ``future_bars``.

    Mode-gated: when ``brain_triple_barrier_mode == 'off'``, the DB write is
    skipped and ``inserted=False`` is returned. The computed label itself
    is still returned so callers can inspect it for diagnostics/testing.
    """
    if cfg is None:
        cfg = TripleBarrierConfig(
            tp_pct=float(settings.brain_triple_barrier_tp_pct),
            sl_pct=float(settings.brain_triple_barrier_sl_pct),
            max_bars=int(settings.brain_triple_barrier_max_bars),
            side=side,  # type: ignore[arg-type]
        )

    label = compute_label(entry_close=entry_close, future_bars=future_bars, cfg=cfg)

    mode = _effective_mode(mode_override)
    outcome = LabelWriteOutcome(
        inserted=False,
        label=label,
        ticker=ticker.upper(),
        label_date=label_date,
        side=side,
        cfg=cfg,
        snapshot_id=snapshot_id,
    )
    if mode == "off":
        return outcome

    try:
        outcome.inserted = _upsert_label_row(
            db,
            ticker=ticker,
            label_date=label_date,
            side=side,
            cfg=cfg,
            label=label,
            snapshot_id=snapshot_id,
            mode=mode,
        )
    except Exception:
        logger.exception("[triple_barrier_labeler] upsert failed for %s %s", ticker, label_date)
        db.rollback()
        outcome.inserted = False
        return outcome

    if _ops_log_enabled():
        try:
            line = format_triple_barrier_ops_line(
                event="label_write",
                mode=mode,
                ticker=outcome.ticker,
                label_date=label_date.isoformat(),
                side=side,
                tp_pct=cfg.tp_pct,
                sl_pct=cfg.sl_pct,
                max_bars=cfg.max_bars,
                label=label.label,
                barrier_hit=label.barrier_hit,
                exit_bar_idx=label.exit_bar_idx,
                realized_return_pct=label.realized_return_pct,
                snapshot_id=snapshot_id,
                inserted=outcome.inserted,
            )
            logger.info("%s", line)
        except Exception:
            logger.debug("[triple_barrier_labeler] ops log format failed", exc_info=True)

    return outcome


def _fetch_forward_bars(
    ticker: str,
    from_date: date,
    max_bars: int,
    *,
    buffer_days: int = 10,
) -> list[dict[str, Any]]:
    """Pull up to ``max_bars`` daily bars strictly after ``from_date``.

    Returns [] on any fetch failure to keep the labeler fail-closed.
    """
    try:
        from .market_data import fetch_ohlcv  # local to avoid import cycles
    except Exception:
        logger.debug("[triple_barrier_labeler] market_data unavailable", exc_info=True)
        return []

    start_date = from_date + timedelta(days=1)
    # request a generous window to account for weekends/holidays
    end_date = from_date + timedelta(days=max_bars + buffer_days)
    try:
        bars = fetch_ohlcv(
            ticker,
            interval="1d",
            start=start_date.isoformat(),
            end=end_date.isoformat(),
        )
    except Exception:
        logger.debug(
            "[triple_barrier_labeler] fetch_ohlcv failed for %s %s..%s",
            ticker, start_date, end_date, exc_info=True,
        )
        return []

    out: list[dict[str, Any]] = []
    for b in bars or []:
        # market_data returns dicts with ISO 'date' or 'timestamp' fields
        raw_date = b.get("date") or b.get("timestamp") or b.get("t")
        try:
            bar_date = (
                datetime.fromisoformat(str(raw_date)[:10]).date()
                if raw_date is not None
                else None
            )
        except Exception:
            bar_date = None
        if bar_date is None or bar_date <= from_date:
            continue
        out.append(b)
        if len(out) >= max_bars:
            break
    return out


def label_snapshots(
    db: Session,
    *,
    limit: int = 200,
    side: str = "long",
    cfg: TripleBarrierConfig | None = None,
    mode_override: str | None = None,
    min_lookback_days: int = 10,
) -> LabelerReport:
    """Label recent MarketSnapshots with sufficient forward-bar availability.

    Selects the most recent ``limit`` snapshots whose ``snapshot_date`` is
    at least ``min_lookback_days`` old (to ensure we have forward bars to
    evaluate barriers against).
    """
    mode = _effective_mode(mode_override)
    rep = LabelerReport(mode=mode)

    if cfg is None:
        cfg = TripleBarrierConfig(
            tp_pct=float(settings.brain_triple_barrier_tp_pct),
            sl_pct=float(settings.brain_triple_barrier_sl_pct),
            max_bars=int(settings.brain_triple_barrier_max_bars),
            side=side,  # type: ignore[arg-type]
        )

    if mode == "off":
        return rep

    from ...models.trading import MarketSnapshot  # local to avoid eager import

    cutoff = datetime.utcnow() - timedelta(days=min_lookback_days)
    rows = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.snapshot_date <= cutoff)
        .order_by(MarketSnapshot.snapshot_date.desc())
        .limit(limit)
        .all()
    )
    rep.requested = len(rows)

    for snap in rows:
        try:
            snap_date = snap.snapshot_date.date() if hasattr(snap.snapshot_date, "date") else snap.snapshot_date
            bars = _fetch_forward_bars(
                ticker=snap.ticker,
                from_date=snap_date,
                max_bars=cfg.max_bars,
            )
            if not bars:
                rep.missing_data += 1
                continue
            outcome = label_single(
                db,
                ticker=snap.ticker,
                label_date=snap_date,
                entry_close=float(snap.close_price),
                future_bars=bars,
                side=side,
                cfg=cfg,
                snapshot_id=int(snap.id),
                mode_override=mode,
            )
            rep.details.append(outcome)
            if outcome.inserted:
                rep.written += 1
            else:
                rep.skipped_existing += 1

            lbl = outcome.label.label
            if outcome.label.barrier_hit == "tp":
                rep.labels_tp += 1
            elif outcome.label.barrier_hit == "sl":
                rep.labels_sl += 1
            elif outcome.label.barrier_hit == "timeout":
                rep.labels_timeout += 1
            elif outcome.label.barrier_hit == "missing_data":
                rep.missing_data += 1
            _ = lbl  # label value preserved in outcome
        except Exception:
            logger.exception(
                "[triple_barrier_labeler] snapshot %s failed", getattr(snap, "id", None),
            )
            rep.errors += 1

    if _ops_log_enabled():
        try:
            line = format_triple_barrier_ops_line(
                event="run_summary",
                mode=mode,
                labels_total=rep.requested,
                labels_tp=rep.labels_tp,
                labels_sl=rep.labels_sl,
                labels_timeout=rep.labels_timeout,
                labels_missing=rep.missing_data,
                written=rep.written,
                skipped_existing=rep.skipped_existing,
                errors=rep.errors,
            )
            logger.info("%s", line)
        except Exception:
            logger.debug("[triple_barrier_labeler] summary log format failed", exc_info=True)

    return rep


def label_summary(
    db: Session, *, lookback_hours: int = 24
) -> dict[str, Any]:
    """Diagnostics-friendly summary of recent label writes."""
    cutoff = datetime.utcnow() - timedelta(hours=max(1, int(lookback_hours)))
    row = db.execute(
        text(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(DISTINCT ticker) AS tickers_distinct,
                COUNT(*) FILTER (WHERE barrier_hit = 'tp') AS tp,
                COUNT(*) FILTER (WHERE barrier_hit = 'sl') AS sl,
                COUNT(*) FILTER (WHERE barrier_hit = 'timeout') AS timeout,
                COUNT(*) FILTER (WHERE barrier_hit = 'missing_data') AS missing,
                MAX(created_at) AS last_created_at
            FROM trading_triple_barrier_labels
            WHERE created_at >= :cutoff
            """
        ),
        {"cutoff": cutoff},
    ).fetchone()

    total = int(row[0] or 0)
    tp = int(row[2] or 0)
    sl = int(row[3] or 0)
    to_ = int(row[4] or 0)
    missing = int(row[5] or 0)

    return {
        "mode": _effective_mode(),
        "lookback_hours": int(lookback_hours),
        "labels_total": total,
        "tickers_distinct": int(row[1] or 0),
        "by_barrier": {
            "tp": tp,
            "sl": sl,
            "timeout": to_,
            "missing_data": missing,
        },
        "label_distribution": {
            "+1": tp,
            "-1": sl,
            "0": to_ + missing,
        },
        "last_label_at": row[6].isoformat() + "Z" if row[6] else None,
        "tp_pct_cfg": float(settings.brain_triple_barrier_tp_pct),
        "sl_pct_cfg": float(settings.brain_triple_barrier_sl_pct),
        "max_bars_cfg": int(settings.brain_triple_barrier_max_bars),
    }


__all__ = [
    "LabelerReport",
    "LabelWriteOutcome",
    "label_single",
    "label_snapshots",
    "label_summary",
    "mode_is_active",
]
