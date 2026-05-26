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
import math
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional

import pandas as pd
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from ...config import (
    PATTERN_DIRECTIONAL_DEFAULT_HOLD_HOURS,
    PATTERN_DIRECTIONAL_DEFAULT_MAX_ALERTS_PER_RUN,
    PATTERN_DIRECTIONAL_DEFAULT_MAX_LOOKBACK_HOURS,
    PATTERN_DIRECTIONAL_DEFAULT_THRESHOLD_PCT,
    PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_ASSET_TYPES,
    PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_ENABLED,
    PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_LOOKBACK_MINUTES,
    PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_MANAGED_REASONS,
    settings as _module_settings,
)

logger = logging.getLogger(__name__)


# Public name kept symmetric with the brief.
ALERT_TYPE_PATTERN_BREAKOUT_IMMINENT = "pattern_breakout_imminent"
AUTOTRADER_EDGE_DEBT_SKIP_REASON = "non_positive_expected_edge"
DEFAULT_PRIORITY_ASSET_TYPE = "stock"


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


def _hold_hours_from_duration_estimate(
    duration_estimate: Any,
    *,
    default_hold_hours: int,
    max_lookback_hours: int,
) -> float:
    """Resolve alert duration text into a hold window in hours.

    Imminent-alert labels can be intraday ("~15-35 min", "~1-6 hours") or
    swing-style ("~1-2 days"). Use the upper bound so the outcome window gives
    the setup its advertised time to work, while still clamping to the
    evaluator's configured lookback budget.
    """
    default = max(1.0, float(default_hold_hours))
    text_value = str(duration_estimate or "").strip().lower()
    if not text_value:
        return min(default, float(max_lookback_hours))
    text_value = (
        text_value
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("hrs", "hours")
        .replace("hr", "hours")
    )
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text_value)]
    if not nums:
        return min(default, float(max_lookback_hours))
    upper = max(nums)
    if "min" in text_value:
        hours = upper / 60.0
    elif "day" in text_value:
        hours = upper * 24.0
    elif "hour" in text_value:
        hours = upper
    else:
        hours = default
    if not math.isfinite(hours) or hours <= 0.0:
        hours = default
    return min(max(hours, 1.0 / 60.0), float(max_lookback_hours))


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


def _csv_tokens(value: Any, default: str) -> tuple[str, ...]:
    raw = value if value not in (None, "") else default
    if isinstance(raw, (list, tuple, set, frozenset)):
        values = raw
    else:
        values = str(raw).split(",")
    tokens = tuple(
        str(v).strip().lower()
        for v in values
        if str(v).strip()
    )
    return tokens


def _positive_int_setting(settings_: Any, name: str, default: int) -> int:
    try:
        value = int(getattr(settings_, name, default))
    except (TypeError, ValueError):
        value = int(default)
    return max(0, value)


def _edge_debt_priority_pattern_ids(
    db: Session,
    *,
    now: datetime,
    settings_: Any,
) -> list[int]:
    """Patterns whose live edge skips need directional evidence first."""
    if not bool(
        getattr(
            settings_,
            "chili_pattern_directional_edge_debt_priority_enabled",
            PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_ENABLED,
        )
    ):
        return []
    lookback_minutes = _positive_int_setting(
        settings_,
        "chili_pattern_directional_edge_debt_priority_lookback_minutes",
        PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_LOOKBACK_MINUTES,
    )
    if lookback_minutes <= 0:
        return []
    asset_types = _csv_tokens(
        getattr(
            settings_,
            "chili_pattern_directional_edge_debt_priority_asset_types",
            PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_ASSET_TYPES,
        ),
        PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_ASSET_TYPES,
    )
    managed_reasons = _csv_tokens(
        getattr(
            settings_,
            "chili_pattern_directional_edge_debt_priority_managed_reasons",
            PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_MANAGED_REASONS,
        ),
        PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_MANAGED_REASONS,
    )
    if not asset_types or not managed_reasons:
        return []
    cutoff = now - timedelta(minutes=lookback_minutes)
    query = text(
        """
        SELECT DISTINCT COALESCE(ar.scan_pattern_id, a.scan_pattern_id) AS pattern_id
        FROM trading_autotrader_runs ar
        LEFT JOIN trading_breakout_alerts a ON a.id = ar.breakout_alert_id
        WHERE ar.created_at >= :cutoff
          AND ar.reason = :edge_debt_reason
          AND COALESCE(a.asset_type, :default_asset_type) IN :asset_types
          AND COALESCE(
              ar.rule_snapshot->'entry_edge'->'managed_exit_edge'
                  ->'geometry'->>'reason',
              ar.rule_snapshot->'entry_edge'->'managed_exit_edge'
                  ->>'selection_reason',
              ''
          ) IN :managed_reasons
          AND COALESCE(ar.scan_pattern_id, a.scan_pattern_id) IS NOT NULL
        ORDER BY pattern_id
        """
    ).bindparams(
        bindparam("asset_types", expanding=True),
        bindparam("managed_reasons", expanding=True),
    )
    try:
        rows = db.execute(
            query,
            {
                "cutoff": cutoff,
                "edge_debt_reason": AUTOTRADER_EDGE_DEBT_SKIP_REASON,
                "default_asset_type": DEFAULT_PRIORITY_ASSET_TYPE,
                "asset_types": asset_types,
                "managed_reasons": managed_reasons,
            },
        ).fetchall()
    except Exception:
        logger.debug("[pattern_directional] edge-debt priority read failed", exc_info=True)
        return []
    out: list[int] = []
    for row in rows:
        try:
            out.append(int(row[0]))
        except (TypeError, ValueError):
            continue
    return out


def _candidate_query(
    *,
    include_priority_filter: bool,
    exclude_alert_ids: bool,
):
    filters = []
    params = []
    if include_priority_filter:
        filters.append("AND a.scan_pattern_id IN :priority_pattern_ids")
        params.append(bindparam("priority_pattern_ids", expanding=True))
    if exclude_alert_ids:
        filters.append("AND a.id NOT IN :exclude_alert_ids")
        params.append(bindparam("exclude_alert_ids", expanding=True))
    sql = text(
        f"""
        SELECT a.id, a.scan_pattern_id, a.ticker, a.created_at,
               a.duration_estimate, a.decision_packet_id
        FROM trading_alerts a
        LEFT JOIN pattern_alert_directional_outcome p
          ON p.alert_id = a.id
        WHERE a.alert_type = :atype
          AND a.scan_pattern_id IS NOT NULL
          AND a.ticker IS NOT NULL
          AND a.created_at >= :lookback_floor
          AND p.alert_id IS NULL
          {' '.join(filters)}
        ORDER BY a.created_at ASC
        LIMIT :max_per_run
        """
    )
    return sql.bindparams(*params) if params else sql


def _load_candidate_rows(
    db: Session,
    *,
    lookback_floor: datetime,
    max_per_run: int,
    priority_pattern_ids: list[int],
) -> tuple[list[Any], int]:
    rows: list[Any] = []
    priority_candidates = 0
    base_params = {
        "atype": ALERT_TYPE_PATTERN_BREAKOUT_IMMINENT,
        "lookback_floor": lookback_floor,
    }
    if priority_pattern_ids:
        priority_rows = db.execute(
            _candidate_query(
                include_priority_filter=True,
                exclude_alert_ids=False,
            ),
            {
                **base_params,
                "priority_pattern_ids": tuple(priority_pattern_ids),
                "max_per_run": max_per_run,
            },
        ).fetchall()
        rows.extend(priority_rows)
        priority_candidates = len(priority_rows)

    remaining = max_per_run - len(rows)
    if remaining <= 0:
        return rows, priority_candidates

    selected_alert_ids = [int(r[0]) for r in rows]
    generic_rows = db.execute(
        _candidate_query(
            include_priority_filter=False,
            exclude_alert_ids=bool(selected_alert_ids),
        ),
        {
            **base_params,
            "exclude_alert_ids": tuple(selected_alert_ids),
            "max_per_run": remaining,
        },
    ).fetchall()
    rows.extend(generic_rows)
    return rows, priority_candidates


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

    threshold_pct = float(
        getattr(
            s,
            "chili_pattern_directional_threshold_pct",
            PATTERN_DIRECTIONAL_DEFAULT_THRESHOLD_PCT,
        )
    )
    default_hold_hours = int(
        getattr(
            s,
            "chili_pattern_directional_default_hold_hours",
            PATTERN_DIRECTIONAL_DEFAULT_HOLD_HOURS,
        )
    )
    max_lookback_hours = int(
        getattr(
            s,
            "chili_pattern_directional_max_lookback_hours",
            PATTERN_DIRECTIONAL_DEFAULT_MAX_LOOKBACK_HOURS,
        )
    )
    max_per_run = int(
        getattr(
            s,
            "chili_pattern_directional_max_alerts_per_run",
            PATTERN_DIRECTIONAL_DEFAULT_MAX_ALERTS_PER_RUN,
        )
    )

    fetcher = fetch_ohlcv or _default_fetch_ohlcv
    now = now or datetime.utcnow()
    lookback_floor = now - timedelta(hours=max_lookback_hours)
    import time as _t
    t0 = _t.monotonic()

    priority_pattern_ids = _edge_debt_priority_pattern_ids(
        db,
        now=now,
        settings_=s,
    )
    # Candidates not yet evaluated. Hold windows are duration-aware per alert,
    # so SQL cannot safely prefilter by a single default-hold floor: a 15-minute
    # setup should not wait behind a 24h default. We do, however, front-load
    # patterns that just hit live edge skips because their managed-edge
    # directional evidence is thin; those rows are learning-critical.
    rows, priority_candidates = _load_candidate_rows(
        db,
        lookback_floor=lookback_floor,
        max_per_run=max_per_run,
        priority_pattern_ids=priority_pattern_ids,
    )

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
            "priority_patterns": len(priority_pattern_ids),
            "priority_candidates": priority_candidates,
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

    for alert_id, scan_pattern_id, ticker, created_at, _duration, decision_packet_id in rows:
        try:
            pat = pat_by_id.get(int(scan_pattern_id))
            if pat is None:
                skipped_no_pattern += 1
                continue
            hold_hours = _hold_hours_from_duration_estimate(
                _duration,
                default_hold_hours=default_hold_hours,
                max_lookback_hours=max_lookback_hours,
            )
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
            result = db.execute(
                text(
                    """
                    INSERT INTO pattern_alert_directional_outcome (
                        alert_id, scan_pattern_id, ticker, alert_at,
                        decision_packet_id,
                        predicted_direction, entry_price,
                        hold_window_hours, window_close_at,
                        window_max_favorable_pct, window_max_adverse_pct,
                        directional_threshold_pct, directional_correct,
                        evaluated_at
                    ) VALUES (
                        :alert_id, :scan_pattern_id, :ticker, :alert_at,
                        :decision_packet_id,
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
                    "decision_packet_id": int(decision_packet_id) if decision_packet_id is not None else None,
                    "predicted_direction": direction,
                    "entry_price": Decimal(str(round(entry_price, 8))),
                    "hold_window_hours": int(math.ceil(hold_hours)),
                    "window_close_at": window_close_at,
                    "max_fav": Decimal(str(outcome["max_favorable_pct"])),
                    "max_adv": Decimal(str(outcome["max_adverse_pct"])),
                    "threshold_pct": Decimal(str(threshold_pct)),
                    "directional_correct": bool(outcome["directional_correct"]),
                    "evaluated_at": now,
                },
            )
            if int(getattr(result, "rowcount", 0) or 0) > 0:
                try:
                    from .decision_ledger import finalize_signal_packet_directional_outcome

                    finalize_signal_packet_directional_outcome(
                        db,
                        packet_id=int(decision_packet_id) if decision_packet_id is not None else None,
                        alert_id=int(alert_id),
                        ticker=str(ticker),
                        scan_pattern_id=int(scan_pattern_id) if scan_pattern_id is not None else None,
                        directional_correct=bool(outcome["directional_correct"]),
                        max_favorable_pct=float(outcome["max_favorable_pct"]),
                        max_adverse_pct=float(outcome["max_adverse_pct"]),
                        entry_price=float(entry_price),
                        hold_window_hours=int(math.ceil(hold_hours)),
                        evaluated_at=now,
                    )
                except Exception:
                    logger.debug(
                        "[pattern_directional] packet outcome update failed alert_id=%s packet_id=%s",
                        alert_id,
                        decision_packet_id,
                        exc_info=True,
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
        "priority_patterns": len(priority_pattern_ids),
        "priority_candidates": priority_candidates,
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
                   last_alert_at, last_evaluated_at,
                   packet_linked_sample_n, packet_lineage_coverage
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
        "packet_linked_sample_n": int(row[5] or 0),
        "packet_lineage_coverage": float(row[6]) if row[6] is not None else None,
    }
