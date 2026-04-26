"""A4 — FRED DGS10/DGS2 ingestion for the regime classifier.

Replaces the synthetic ``yield_curve_slope_proxy`` with the real
DGS10−DGS2 spread from the St. Louis Fed (FRED). FRED's daily series
data is available without an API key via the public CSV endpoint:

    https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10
    https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2

The fetcher is best-effort: any network/parse error is logged and the
caller falls back to the existing proxy. Successful fetches are
persisted to ``macro_fred_fetch_log`` for observability and to the new
``dgs10_real`` / ``dgs2_real`` columns on ``trading_macro_regime_snapshots``.

Usage::

    from app.services.trading.fred_yield_curve import (
        fetch_yield_slope_for_date,
        attach_real_yield_slope_to_snapshot,
    )

The regime-classifier feature pipeline calls
``current_yield_slope(db)`` which prefers the real value when present
and falls back to ``yield_curve_slope_proxy`` otherwise.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_FRED_CSV_TEMPLATE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
_TIMEOUT_SEC = 10
_MAX_LOOKBACK_DAYS = 14   # FRED publishes with up to a few business-days lag


def _fetch_fred_series_csv(series: str) -> Optional[list[tuple[date, float]]]:
    """Fetch a FRED series and return a list of (date, value).

    Returns ``None`` on any failure (network, parse, empty body). Caller
    is expected to handle the None gracefully.
    """
    url = _FRED_CSV_TEMPLATE.format(series=series)
    try:
        r = requests.get(url, timeout=_TIMEOUT_SEC)
        r.raise_for_status()
    except Exception as e:
        logger.warning("[fred] fetch %s failed: %s", series, e)
        return None

    out: list[tuple[date, float]] = []
    try:
        reader = csv.reader(io.StringIO(r.text))
        header = next(reader, None)
        if not header or len(header) < 2:
            logger.warning("[fred] %s: malformed header %r", series, header)
            return None
        for row in reader:
            if len(row) < 2:
                continue
            d_str, v_str = row[0], row[1]
            if not d_str or v_str in ("", "."):
                continue
            try:
                d = datetime.strptime(d_str, "%Y-%m-%d").date()
                v = float(v_str)
                out.append((d, v))
            except (ValueError, TypeError):
                continue
    except Exception as e:
        logger.warning("[fred] %s: parse error: %s", series, e)
        return None

    return out or None


def _persist_fetch_log(
    db: Session,
    *,
    series_id: str,
    as_of_date: date,
    value: Optional[float],
    success: bool,
    error_message: Optional[str] = None,
) -> None:
    """Idempotent UPSERT into macro_fred_fetch_log."""
    try:
        db.execute(
            text(
                """
                INSERT INTO macro_fred_fetch_log
                    (series_id, as_of_date, value, success, error_message, fetched_at)
                VALUES (:s, :d, :v, :ok, :err, NOW())
                ON CONFLICT (series_id, as_of_date) DO UPDATE SET
                    value = EXCLUDED.value,
                    success = EXCLUDED.success,
                    error_message = EXCLUDED.error_message,
                    fetched_at = NOW()
                """
            ),
            {
                "s": series_id,
                "d": as_of_date,
                "v": value,
                "ok": success,
                "err": (error_message or None),
            },
        )
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[fred] persist_fetch_log %s failed: %s", series_id, e)


def fetch_yield_slope_for_date(
    db: Session,
    target_date: Optional[date] = None,
) -> Optional[float]:
    """Fetch the DGS10−DGS2 spread for ``target_date`` (or the most recent
    business day if None). Returns the spread or ``None`` if either series
    failed to fetch / parse.

    Side effect: writes one row per series to ``macro_fred_fetch_log``.
    """
    if target_date is None:
        target_date = date.today()

    dgs10_data = _fetch_fred_series_csv("DGS10")
    dgs2_data = _fetch_fred_series_csv("DGS2")

    if dgs10_data is None or dgs2_data is None:
        # Record the failures so the operator can see why.
        if dgs10_data is None:
            _persist_fetch_log(
                db, series_id="DGS10", as_of_date=target_date,
                value=None, success=False, error_message="fetch_or_parse_failed",
            )
        if dgs2_data is None:
            _persist_fetch_log(
                db, series_id="DGS2", as_of_date=target_date,
                value=None, success=False, error_message="fetch_or_parse_failed",
            )
        return None

    # Pick the most recent observation on or before target_date for each
    # series. FRED publishes with up to a few business-day lag, so we
    # walk back up to _MAX_LOOKBACK_DAYS.
    def _lookup(rows: list[tuple[date, float]]) -> Optional[tuple[date, float]]:
        rows_sorted = sorted(rows, key=lambda x: x[0], reverse=True)
        cutoff = target_date - timedelta(days=_MAX_LOOKBACK_DAYS)
        for d, v in rows_sorted:
            if d <= target_date and d >= cutoff:
                return (d, v)
        return None

    dgs10 = _lookup(dgs10_data)
    dgs2 = _lookup(dgs2_data)
    if dgs10 is None or dgs2 is None:
        if dgs10 is None:
            _persist_fetch_log(
                db, series_id="DGS10", as_of_date=target_date,
                value=None, success=False,
                error_message=f"no_observation_in_lookback_{_MAX_LOOKBACK_DAYS}d",
            )
        if dgs2 is None:
            _persist_fetch_log(
                db, series_id="DGS2", as_of_date=target_date,
                value=None, success=False,
                error_message=f"no_observation_in_lookback_{_MAX_LOOKBACK_DAYS}d",
            )
        return None

    _persist_fetch_log(
        db, series_id="DGS10", as_of_date=dgs10[0],
        value=dgs10[1], success=True,
    )
    _persist_fetch_log(
        db, series_id="DGS2", as_of_date=dgs2[0],
        value=dgs2[1], success=True,
    )
    return float(dgs10[1] - dgs2[1])


def attach_real_yield_slope_to_snapshot(
    db: Session,
    *,
    as_of_date: date,
    dgs10: float,
    dgs2: float,
) -> bool:
    """Update ``trading_macro_regime_snapshots`` for the given date with
    the real FRED values plus a ``yield_slope_source`` tag of
    ``'fred_dgs10_dgs2'``. Returns True on success."""
    try:
        result = db.execute(
            text(
                """
                UPDATE trading_macro_regime_snapshots
                   SET dgs10_real = :d10,
                       dgs2_real  = :d2,
                       yield_slope_source = 'fred_dgs10_dgs2'
                 WHERE as_of_date = :d
                """
            ),
            {"d10": dgs10, "d2": dgs2, "d": as_of_date},
        )
        db.commit()
        return result.rowcount > 0
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "[fred] attach_real_yield_slope_to_snapshot %s failed: %s",
            as_of_date, e,
        )
        return False


def current_yield_slope(db: Session) -> tuple[Optional[float], str]:
    """Return ``(slope, source)`` for the most recent macro snapshot.

    ``source`` is one of:
      - ``'fred_dgs10_dgs2'`` — real values, computed from FRED
      - ``'proxy'``           — fell back to ``yield_curve_slope_proxy``
      - ``'missing'``         — neither available
    """
    try:
        row = db.execute(
            text(
                """
                SELECT dgs10_real, dgs2_real, yield_curve_slope_proxy
                FROM trading_macro_regime_snapshots
                ORDER BY as_of_date DESC
                LIMIT 1
                """
            )
        ).fetchone()
        if not row:
            return (None, "missing")
        d10, d2, proxy = row
        if d10 is not None and d2 is not None:
            return (float(d10 - d2), "fred_dgs10_dgs2")
        if proxy is not None:
            return (float(proxy), "proxy")
        return (None, "missing")
    except Exception as e:
        logger.warning("[fred] current_yield_slope failed: %s", e)
        return (None, "missing")


def run_weekly_fred_yield_ingestion(db: Session) -> dict:
    """Background-job entry point. Fetches DGS10/DGS2 and persists.

    Wired into the scheduler in ``app/services/trading_scheduler.py`` (next
    to the other macro jobs). Idempotent; safe to call repeatedly.
    """
    started = time.monotonic()
    today = date.today()
    slope = fetch_yield_slope_for_date(db, target_date=today)

    res: dict = {
        "ok": slope is not None,
        "slope": slope,
        "as_of": today.isoformat(),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }

    if slope is not None:
        # Look up the actual fetched DGS10/DGS2 values from the log for the
        # snapshot attach. The persist_fetch_log entries hold them.
        try:
            agg = db.execute(
                text(
                    """
                    SELECT
                      MAX(value) FILTER (WHERE series_id = 'DGS10' AND success) AS d10,
                      MAX(value) FILTER (WHERE series_id = 'DGS2'  AND success) AS d2
                    FROM macro_fred_fetch_log
                    WHERE as_of_date >= :cutoff
                    """
                ),
                {"cutoff": today - timedelta(days=_MAX_LOOKBACK_DAYS)},
            ).fetchone()
            if agg and agg[0] is not None and agg[1] is not None:
                attached = attach_real_yield_slope_to_snapshot(
                    db, as_of_date=today, dgs10=float(agg[0]), dgs2=float(agg[1]),
                )
                res["snapshot_attached"] = attached
        except Exception as e:
            logger.warning("[fred] snapshot attach lookup failed: %s", e)
            res["snapshot_attached"] = False

    logger.info("[fred] weekly_yield_ingestion: %s", res)
    return res
