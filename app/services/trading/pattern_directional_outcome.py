"""f-promotion-pipeline-rebalance Phase 2 (2026-05-09).

Directional-correctness evaluator for ``pattern_breakout_imminent``
alerts. Writes one row per closed-window alert into
``pattern_alert_directional_outcome`` (mig 235).

Why this module exists
----------------------

The autotrader's 7-stage gate chain (kill switch, drawdown breaker,
rule floor, LLM revalidation, PDT cooldown, cost gate, cap check,
bracket writer) protects capital — but it also filters 99% of the
imminent-alert volume that a promoted pattern produces. Pattern 585
fired 1,284 imminent alerts in its promoted window; only 8 became
real trades. Demoting on those 8 trades' realized win-rate is a
category error: those 8 are gate-laundered noise, not a random
sample of the pattern's directional calls.

The clean signal is "did price actually move in the predicted
direction within the hold window of the alert?" — measured on every
imminent alert, regardless of whether it survived the gate chain.
Phases 3 and 4 read the rolling-30 directional WR from
``pattern_directional_quality_v`` to drive shadow-promotion and
cohort-promotion decisions.

Public API
----------

``evaluate_directional_outcomes(db, *, now=None, ...)``: idempotent
batch run. Selects every ``pattern_breakout_imminent`` row whose hold
window has closed and that doesn't yet have a row in
``pattern_alert_directional_outcome``, fetches OHLC for the window,
computes max-favorable/max-adverse against the predicted direction,
and inserts the outcome row.

Robustness
----------

- OHLC fetch failures: skip with reason ``ohlc_unavailable``; the
  alert remains unevaluated and will retry next cycle. Phase 4's
  rolling view simply has fewer rows for that pattern.
- Alerts missing ``scan_pattern_id``: skip with reason
  ``no_pattern_id`` (those are emergency / system alerts, not
  pattern-imminent predictions).
- Alerts whose ``ScanPattern`` row was deleted: skip with reason
  ``pattern_missing``.
- Per-run cap on alerts evaluated (``settings.chili_pattern_directional
  _max_alerts_per_run``, default 200) bounds OHLC fan-out.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings as _module_settings

logger = logging.getLogger(__name__)


# Public name kept symmetric with the brief.
ALERT_TYPE_PATTERN_BREAKOUT_IMMINENT = "pattern_breakout_imminent"


def _resolve_predicted_direction(pat: Any) -> str:
    """Resolve "up" or "down" from a ``ScanPattern`` row.

    ``pattern_breakout_imminent`` is bullish-breakout by default, so
    "up" is the default. We check ``rules_json`` for explicit override
    (operator-authored short patterns) and the pattern's name for a
    "short"/"fade"/"downtrend" keyword as a backstop heuristic. This
    is intentionally conservative — false-positive "up" classification
    is harmless because the directional_threshold also wouldn't fire
    for an actual down move.
    """
    rules = getattr(pat, "rules_json", None) or {}
    if isinstance(rules, dict):
        for key in ("direction", "bias", "side", "trade_side"):
            val = rules.get(key)
            if isinstance(val, str):
                v = val.strip().lower()
                if v in ("down", "short", "sell", "bearish"):
                    return "down"
                if v in ("up", "long", "buy", "bullish"):
                    return "up"
    name = (getattr(pat, "name", "") or "").lower()
    if any(token in name for token in ("short_", "_short", "fade", "downtrend", "bearish")):
        return "down"
    return "up"


def _entry_price_from_df(df: pd.DataFrame, alert_at: datetime) -> Optional[float]:
    """Last close at-or-before alert_at, else first close after.

    pandas DataFrame is expected to have a DatetimeIndex (UTC-naive)
    matching ``fetch_ohlcv_df``'s output and a 'Close' column.
    """
    if df is None or df.empty or "Close" not in df.columns:
        return None
    try:
        # Match the time semantics of the index (might be tz-aware).
        idx = df.index
        ts = pd.Timestamp(alert_at)
        if getattr(idx, "tz", None) is not None and ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        elif getattr(idx, "tz", None) is None and ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        before = df.loc[df.index <= ts]
        if not before.empty:
            return float(before["Close"].iloc[-1])
        # Fall through: alert_at is before first bar — use first close
        # we have. Better than skipping the row outright.
        return float(df["Close"].iloc[0])
    except Exception:
        return None


def _slice_window(
    df: pd.DataFrame, alert_at: datetime, window_close_at: datetime
) -> pd.DataFrame:
    """Inclusive slice of df between alert_at and window_close_at."""
    if df is None or df.empty:
        return df
    idx = df.index
    start = pd.Timestamp(alert_at)
    end = pd.Timestamp(window_close_at)
    if getattr(idx, "tz", None) is not None:
        if start.tzinfo is None:
            start = start.tz_localize("UTC")
        if end.tzinfo is None:
            end = end.tz_localize("UTC")
    else:
        if start.tzinfo is not None:
            start = start.tz_localize(None)
        if end.tzinfo is not None:
            end = end.tz_localize(None)
    return df.loc[(df.index >= start) & (df.index <= end)]


def _compute_window_outcome(
    df: pd.DataFrame,
    *,
    alert_at: datetime,
    window_close_at: datetime,
    entry_price: float,
    direction: str,
    threshold_pct: float,
) -> Optional[dict[str, Any]]:
    """Compute max-favorable, max-adverse, and directional_correct.

    Returns ``None`` if the window slice is empty (no usable OHLC bars
    inside [alert_at, window_close_at]). Caller should treat that as a
    skip.
    """
    if entry_price is None or entry_price <= 0:
        return None
    win = _slice_window(df, alert_at, window_close_at)
    if win is None or win.empty:
        return None
    if "High" not in win.columns or "Low" not in win.columns:
        return None
    try:
        hi = float(win["High"].max())
        lo = float(win["Low"].min())
    except Exception:
        return None
    if direction == "down":
        # Favorable = price moved DOWN; adverse = price moved UP.
        max_favorable_pct = (entry_price - lo) / entry_price * 100.0
        max_adverse_pct = (entry_price - hi) / entry_price * 100.0
    else:
        max_favorable_pct = (hi - entry_price) / entry_price * 100.0
        max_adverse_pct = (lo - entry_price) / entry_price * 100.0
    directional_correct = bool(max_favorable_pct >= float(threshold_pct))
    return {
        "max_favorable_pct": round(max_favorable_pct, 6),
        "max_adverse_pct": round(max_adverse_pct, 6),
        "directional_correct": directional_correct,
    }


def _default_fetch_ohlcv(
    ticker: str, *, start: datetime, end: datetime
) -> pd.DataFrame:
    """Lazy import of market_data so unit tests can avoid the dep."""
    from .market_data import fetch_ohlcv_df

    # Use 1h bars when the window fits; fall back to 1d for very long
    # windows. The default hold is 24h so 1h is the right default.
    span = end - start
    if span >= timedelta(days=7):
        interval = "1d"
    else:
        interval = "1h"
    try:
        return fetch_ohlcv_df(
            ticker,
            interval=interval,
            start=start.strftime("%Y-%m-%d"),
            end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
        )
    except Exception as e:
        logger.debug(
            "[pattern_directional] OHLC fetch failed ticker=%s err=%s",
            ticker, e,
        )
        return pd.DataFrame()


def evaluate_directional_outcomes(
    db: Session,
    *,
    now: Optional[datetime] = None,
    fetch_ohlcv: Optional[Callable[..., pd.DataFrame]] = None,
    settings_: Any = None,
) -> dict[str, Any]:
    """Process closed-window imminent alerts; insert outcome rows.

    Returns a structured summary suitable for scheduler logging:

    ``{"ok": bool, "candidates": int, "evaluated": int, "skipped_no_pattern": int,
       "skipped_no_ohlc": int, "skipped_window_empty": int, "errors": int,
       "elapsed_ms": int}``
    """
    s = settings_ or _module_settings
    enabled = bool(getattr(s, "chili_pattern_directional_outcome_enabled", True))
    if not enabled:
        return {"ok": True, "skipped": "flag_disabled", "candidates": 0, "evaluated": 0}

    threshold_pct = float(getattr(s, "chili_pattern_directional_threshold_pct", 1.5))
    default_hold_hours = int(
        getattr(s, "chili_pattern_directional_default_hold_hours", 24)
    )
    max_lookback_hours = int(
        getattr(s, "chili_pattern_directional_max_lookback_hours", 168)
    )
    max_per_run = int(
        getattr(s, "chili_pattern_directional_max_alerts_per_run", 200)
    )

    fetcher = fetch_ohlcv or _default_fetch_ohlcv
    now = now or datetime.utcnow()
    lookback_floor = now - timedelta(hours=max_lookback_hours)
    window_close_floor = now - timedelta(hours=default_hold_hours)

    import time as _t
    t0 = _t.monotonic()

    # Closed-window candidates not yet evaluated. We compute hold per
    # alert below, but use the default-hold floor as a coarse SQL
    # prefilter to keep the candidate set small.
    rows = db.execute(
        text(
            """
            SELECT a.id, a.scan_pattern_id, a.ticker, a.created_at, a.duration_estimate
            FROM trading_alerts a
            LEFT JOIN pattern_alert_directional_outcome p
              ON p.alert_id = a.id
            WHERE a.alert_type = :atype
              AND a.scan_pattern_id IS NOT NULL
              AND a.ticker IS NOT NULL
              AND a.created_at >= :lookback_floor
              AND a.created_at <= :window_close_floor
              AND p.alert_id IS NULL
            ORDER BY a.created_at ASC
            LIMIT :max_per_run
            """
        ),
        {
            "atype": ALERT_TYPE_PATTERN_BREAKOUT_IMMINENT,
            "lookback_floor": lookback_floor,
            "window_close_floor": window_close_floor,
            "max_per_run": max_per_run,
        },
    ).fetchall()

    candidates = len(rows)
    evaluated = 0
    skipped_no_pattern = 0
    skipped_no_ohlc = 0
    skipped_window_empty = 0
    skipped_window_open = 0
    errors = 0

    if candidates == 0:
        return {
            "ok": True,
            "candidates": 0,
            "evaluated": 0,
            "skipped_no_pattern": 0,
            "skipped_no_ohlc": 0,
            "skipped_window_empty": 0,
            "skipped_window_open": 0,
            "errors": 0,
            "elapsed_ms": int((_t.monotonic() - t0) * 1000),
        }

    from ...models.trading import ScanPattern

    pat_ids = sorted({int(r[1]) for r in rows if r[1] is not None})
    pat_by_id: dict[int, Any] = {}
    if pat_ids:
        for p in (
            db.query(ScanPattern).filter(ScanPattern.id.in_(pat_ids)).all()
        ):
            pat_by_id[int(p.id)] = p

    for alert_id, scan_pattern_id, ticker, created_at, _duration in rows:
        try:
            pat = pat_by_id.get(int(scan_pattern_id))
            if pat is None:
                skipped_no_pattern += 1
                continue
            hold_hours = default_hold_hours
            window_close_at = created_at + timedelta(hours=hold_hours)
            if window_close_at > now:
                skipped_window_open += 1
                continue
            direction = _resolve_predicted_direction(pat)
            df = fetcher(
                ticker,
                start=created_at - timedelta(hours=2),
                end=window_close_at + timedelta(hours=1),
            )
            if df is None or df.empty:
                skipped_no_ohlc += 1
                continue
            entry_price = _entry_price_from_df(df, created_at)
            if entry_price is None or entry_price <= 0:
                skipped_no_ohlc += 1
                continue
            outcome = _compute_window_outcome(
                df,
                alert_at=created_at,
                window_close_at=window_close_at,
                entry_price=entry_price,
                direction=direction,
                threshold_pct=threshold_pct,
            )
            if outcome is None:
                skipped_window_empty += 1
                continue
            db.execute(
                text(
                    """
                    INSERT INTO pattern_alert_directional_outcome (
                        alert_id, scan_pattern_id, ticker, alert_at,
                        predicted_direction, entry_price,
                        hold_window_hours, window_close_at,
                        window_max_favorable_pct, window_max_adverse_pct,
                        directional_threshold_pct, directional_correct,
                        evaluated_at
                    ) VALUES (
                        :alert_id, :scan_pattern_id, :ticker, :alert_at,
                        :predicted_direction, :entry_price,
                        :hold_window_hours, :window_close_at,
                        :max_fav, :max_adv,
                        :threshold_pct, :directional_correct,
                        :evaluated_at
                    )
                    ON CONFLICT (alert_id) DO NOTHING
                    """
                ),
                {
                    "alert_id": int(alert_id),
                    "scan_pattern_id": int(scan_pattern_id),
                    "ticker": str(ticker)[:32],
                    "alert_at": created_at,
                    "predicted_direction": direction,
                    "entry_price": Decimal(str(round(entry_price, 8))),
                    "hold_window_hours": int(hold_hours),
                    "window_close_at": window_close_at,
                    "max_fav": Decimal(str(outcome["max_favorable_pct"])),
                    "max_adv": Decimal(str(outcome["max_adverse_pct"])),
                    "threshold_pct": Decimal(str(threshold_pct)),
                    "directional_correct": bool(outcome["directional_correct"]),
                    "evaluated_at": now,
                },
            )
            evaluated += 1
        except Exception as e:
            errors += 1
            logger.warning(
                "[pattern_directional] alert_id=%s ticker=%s eval failed: %s",
                alert_id, ticker, e,
            )
    try:
        db.commit()
    except Exception as e:
        errors += 1
        logger.warning("[pattern_directional] commit failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass

    summary = {
        "ok": errors == 0,
        "candidates": candidates,
        "evaluated": evaluated,
        "skipped_no_pattern": skipped_no_pattern,
        "skipped_no_ohlc": skipped_no_ohlc,
        "skipped_window_empty": skipped_window_empty,
        "skipped_window_open": skipped_window_open,
        "errors": errors,
        "elapsed_ms": int((_t.monotonic() - t0) * 1000),
    }
    logger.info("[pattern_directional] %s", summary)
    return summary


def get_rolling_directional_quality(
    db: Session, scan_pattern_id: int
) -> Optional[dict[str, Any]]:
    """Read a single pattern's row from ``pattern_directional_quality_v``.

    Phase 4 reads from the view in bulk; this single-row helper is for
    ad-hoc inspection / smoke tests.
    """
    row = db.execute(
        text(
            """
            SELECT scan_pattern_id, rolling_sample_n, rolling_directional_wr,
                   last_alert_at, last_evaluated_at
            FROM pattern_directional_quality_v
            WHERE scan_pattern_id = :pid
            """
        ),
        {"pid": int(scan_pattern_id)},
    ).fetchone()
    if row is None:
        return None
    return {
        "scan_pattern_id": int(row[0]),
        "rolling_sample_n": int(row[1] or 0),
        "rolling_directional_wr": float(row[2]) if row[2] is not None else None,
        "last_alert_at": row[3],
        "last_evaluated_at": row[4],
    }
