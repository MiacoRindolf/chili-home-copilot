"""Phase L.22 — persistence layer for the intraday session regime snapshot.

Runs the pure :mod:`intraday_session_model` against SPY 5-minute bars
fetched via :func:`app.services.trading.market_data.fetch_ohlcv_df`,
filters to the target ``as_of_date`` in US/Eastern, and persists at
most one row per sweep to ``trading_intraday_session_snapshots``.

Design
------
* **Two public entry-points.** :func:`compute_and_persist` writes at
  most one row per ``as_of_date`` and emits ops log entries.
  :func:`intraday_session_summary` returns the diagnostics dict for
  the FastAPI route.
* **Refuses authoritative.** Until Phase L.22.2 opens explicitly the
  service raises :class:`RuntimeError` on
  ``mode_override="authoritative"`` or
  ``brain_intraday_session_mode="authoritative"``. A refusal ops line
  is emitted before the raise so ops / release blockers can see the
  attempt.
* **Append-only.** Every call appends a row. The deterministic
  ``snapshot_id`` keyed on ``as_of_date`` lets callers dedupe.
* **Off-mode short-circuit.** When
  ``brain_intraday_session_mode == "off"`` :func:`compute_and_persist`
  emits a single skip line and returns ``None``.
* **Additive-only.** No downstream consumer (scanner, promotion,
  sizing, alerts, playbook) reads this table in L.22.1.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.intraday_session_ops_log import (
    format_intraday_session_ops_line,
)
from .intraday_session_model import (
    IntradayBar,
    IntradaySessionConfig,
    IntradaySessionInput,
    IntradaySessionOutput,
    RTH_OPEN_MINUTE,
    SESSION_COMPRESSED,
    SESSION_GAP_AND_GO,
    SESSION_GAP_FADE,
    SESSION_NEUTRAL,
    SESSION_RANGE_BOUND,
    SESSION_REVERSAL,
    SESSION_TRENDING_DOWN,
    SESSION_TRENDING_UP,
    VALID_SESSION_LABELS,
    compute_intraday_session,
    compute_snapshot_id,
)

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------


def _effective_mode(override: str | None = None) -> str:
    m = (
        override
        or getattr(settings, "brain_intraday_session_mode", "off")
        or "off"
    ).lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def mode_is_authoritative(override: str | None = None) -> bool:
    return _effective_mode(override) == "authoritative"


def _ops_log_enabled() -> bool:
    return bool(
        getattr(settings, "brain_intraday_session_ops_log_enabled", True)
    )


def _config_from_settings() -> IntradaySessionConfig:
    return IntradaySessionConfig(
        bar_minutes=5,
        or_minutes=int(getattr(
            settings, "brain_intraday_session_or_minutes", 30,
        )),
        power_minutes=int(getattr(
            settings, "brain_intraday_session_power_minutes", 30,
        )),
        min_bars=int(getattr(
            settings, "brain_intraday_session_min_bars", 40,
        )),
        min_coverage_score=float(getattr(
            settings, "brain_intraday_session_min_coverage_score", 0.5,
        )),
        or_range_low=float(getattr(
            settings, "brain_intraday_session_or_range_low", 0.003,
        )),
        or_range_high=float(getattr(
            settings, "brain_intraday_session_or_range_high", 0.012,
        )),
        midday_compression_cut=float(getattr(
            settings, "brain_intraday_session_midday_compression_cut", 0.5,
        )),
        gap_magnitude_go=float(getattr(
            settings, "brain_intraday_session_gap_go", 0.005,
        )),
        gap_magnitude_fade=float(getattr(
            settings, "brain_intraday_session_gap_fade", 0.005,
        )),
        trending_close_threshold=float(getattr(
            settings, "brain_intraday_session_trending_close", 0.006,
        )),
        reversal_close_threshold=float(getattr(
            settings, "brain_intraday_session_reversal_close", 0.003,
        )),
    )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntradaySessionRow:
    """Thin reference to a persisted row."""

    pk_id: int
    snapshot_id: str
    as_of_date: date
    session_label: str
    session_numeric: int
    coverage_score: float
    mode: str


# ---------------------------------------------------------------------------
# OHLCV → IntradayBar conversion
# ---------------------------------------------------------------------------


def _today_utc_date() -> date:
    return datetime.now(tz=timezone.utc).date()


def _to_et_minute(ts_utc) -> int | None:
    """Convert a pandas UTC Timestamp (or tz-naive timestamp treated as
    UTC) into a minute-of-day integer in US/Eastern.

    Returns ``None`` if the timestamp cannot be parsed. The returned
    minute matches the pure model's ``ts_minute`` convention (e.g.
    09:30 ET → 570).
    """
    try:
        import pandas as pd  # noqa: WPS433

        t = pd.Timestamp(ts_utc)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        t_et = t.tz_convert("US/Eastern")
        return int(t_et.hour) * 60 + int(t_et.minute)
    except Exception:  # pragma: no cover - defensive
        return None


def _fetch_session_bars(
    symbol: str, *, as_of_date: date, period: str = "5d", interval: str = "5m",
) -> list[IntradayBar]:
    """Fetch 5-min bars via ``market_data.fetch_ohlcv_df`` and convert
    the rows for ``as_of_date`` (US/Eastern) into :class:`IntradayBar`."""
    try:
        from .market_data import fetch_ohlcv_df  # noqa: WPS433

        df = fetch_ohlcv_df(symbol, interval=interval, period=period)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[intraday_session] fetch_ohlcv_df(%s) raised: %s", symbol, exc,
        )
        return []
    if df is None or getattr(df, "empty", True):
        return []

    import pandas as pd  # noqa: WPS433

    try:
        idx = df.index
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")
        idx_et = idx.tz_convert("US/Eastern")
        mask = pd.Series(
            [ts.date() == as_of_date for ts in idx_et], index=df.index
        )
        rows = df[mask]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[intraday_session] date filter(%s) raised: %s", symbol, exc,
        )
        return []

    bars: list[IntradayBar] = []
    for ts, row in rows.iterrows():
        ts_min = _to_et_minute(ts)
        if ts_min is None:
            continue
        try:
            bars.append(
                IntradayBar(
                    ts_minute=int(ts_min),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0.0) or 0.0),
                )
            )
        except Exception:
            continue
    bars.sort(key=lambda b: b.ts_minute)
    return bars


def _fetch_prev_close(
    symbol: str, *, as_of_date: date,
) -> float | None:
    """Fetch the most recent daily close strictly before ``as_of_date``."""
    try:
        from .market_data import fetch_ohlcv_df  # noqa: WPS433

        df = fetch_ohlcv_df(symbol, interval="1d", period="1mo")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[intraday_session] prev_close fetch(%s) raised: %s", symbol, exc,
        )
        return None
    if df is None or getattr(df, "empty", True):
        return None
    try:
        import pandas as pd  # noqa: WPS433

        idx = df.index
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize("UTC")
        # Take strict prior dates
        prior = [i for i in idx if pd.Timestamp(i).date() < as_of_date]
        if not prior:
            return None
        last_prior = max(prior)
        return float(df.loc[last_prior, "Close"])
    except Exception:  # pragma: no cover - defensive
        return None


# ---------------------------------------------------------------------------
# Persist one row
# ---------------------------------------------------------------------------


def _insert_row(
    db: Session, *, out: IntradaySessionOutput, mode: str,
) -> int:
    payload_json = json.dumps(out.payload, default=str)
    now = datetime.utcnow()
    row = db.execute(text("""
        INSERT INTO trading_intraday_session_snapshots (
            snapshot_id, as_of_date, source_symbol,
            open_price, close_price, session_high, session_low,
            session_range_pct,
            prev_close, gap_open, gap_open_pct,
            or_high, or_low, or_range_pct, or_volume_ratio,
            midday_range_pct, midday_compression_ratio,
            ph_range_pct, ph_volume_ratio, close_vs_or_mid_pct,
            intraday_rv,
            session_numeric, session_label,
            bars_observed, coverage_score,
            payload_json, mode, computed_at, observed_at
        ) VALUES (
            :snapshot_id, :as_of_date, :source_symbol,
            :open_price, :close_price, :session_high, :session_low,
            :session_range_pct,
            :prev_close, :gap_open, :gap_open_pct,
            :or_high, :or_low, :or_range_pct, :or_volume_ratio,
            :midday_range_pct, :midday_compression_ratio,
            :ph_range_pct, :ph_volume_ratio, :close_vs_or_mid_pct,
            :intraday_rv,
            :session_numeric, :session_label,
            :bars_observed, :coverage_score,
            CAST(:payload_json AS JSONB), :mode, :computed_at, :observed_at
        ) RETURNING id
    """), {
        "snapshot_id": out.snapshot_id,
        "as_of_date": out.as_of_date,
        "source_symbol": out.source_symbol,
        "open_price": out.open_price,
        "close_price": out.close_price,
        "session_high": out.session_high,
        "session_low": out.session_low,
        "session_range_pct": out.session_range_pct,
        "prev_close": out.prev_close,
        "gap_open": out.gap_open,
        "gap_open_pct": out.gap_open_pct,
        "or_high": out.or_high,
        "or_low": out.or_low,
        "or_range_pct": out.or_range_pct,
        "or_volume_ratio": out.or_volume_ratio,
        "midday_range_pct": out.midday_range_pct,
        "midday_compression_ratio": out.midday_compression_ratio,
        "ph_range_pct": out.ph_range_pct,
        "ph_volume_ratio": out.ph_volume_ratio,
        "close_vs_or_mid_pct": out.close_vs_or_mid_pct,
        "intraday_rv": out.intraday_rv,
        "session_numeric": int(out.session_numeric),
        "session_label": out.session_label,
        "bars_observed": int(out.bars_observed),
        "coverage_score": float(out.coverage_score),
        "payload_json": payload_json,
        "mode": mode,
        "computed_at": now,
        "observed_at": now,
    }).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_and_persist(
    db: Session,
    *,
    as_of_date: date | None = None,
    mode_override: str | None = None,
    bars_override: Sequence[IntradayBar] | None = None,
    prev_close_override: float | None = None,
) -> IntradaySessionRow | None:
    """Run one daily snapshot computation and persist it.

    ``bars_override`` / ``prev_close_override`` are used by the Docker
    soak and API smoke tests to feed deterministic synthetic inputs.
    In production both are ``None`` and the service pulls SPY 5-min
    bars live.
    """
    mode = _effective_mode(mode_override)
    as_of = as_of_date or _today_utc_date()
    symbol = str(
        getattr(settings, "brain_intraday_session_source_symbol", "SPY")
    ).upper()

    if mode == "off":
        if _ops_log_enabled():
            logger.info(format_intraday_session_ops_line(
                event="intraday_session_skipped",
                mode=mode,
                as_of_date=as_of.isoformat(),
                source_symbol=symbol,
                reason="mode_off",
            ))
        return None

    if mode == "authoritative":
        if _ops_log_enabled():
            logger.warning(format_intraday_session_ops_line(
                event="intraday_session_refused_authoritative",
                mode=mode,
                as_of_date=as_of.isoformat(),
                source_symbol=symbol,
                reason="L.22.1_shadow_only",
            ))
        raise RuntimeError(
            "intraday_session authoritative mode is not permitted until "
            "Phase L.22.2 is explicitly opened"
        )

    cfg = _config_from_settings()

    # --- Bars ---------------------------------------------------------------
    if bars_override is not None:
        bars = list(bars_override)
        prev_close = prev_close_override
    else:
        bars = _fetch_session_bars(
            symbol,
            as_of_date=as_of,
            period=str(getattr(settings, "brain_intraday_session_period", "5d")),
            interval=str(getattr(
                settings, "brain_intraday_session_interval", "5m"
            )),
        )
        prev_close = (
            prev_close_override
            if prev_close_override is not None
            else _fetch_prev_close(symbol, as_of_date=as_of)
        )

    # --- Pure model --------------------------------------------------------
    inp = IntradaySessionInput(
        as_of_date=as_of,
        bars=bars,
        prev_close=prev_close,
        source_symbol=symbol,
        config=cfg,
    )
    out = compute_intraday_session(inp)

    if _ops_log_enabled():
        logger.info(format_intraday_session_ops_line(
            event="intraday_session_computed",
            mode=mode,
            snapshot_id=out.snapshot_id,
            as_of_date=as_of.isoformat(),
            source_symbol=symbol,
            open_price=out.open_price,
            close_price=out.close_price,
            session_high=out.session_high,
            session_low=out.session_low,
            session_range_pct=out.session_range_pct,
            prev_close=out.prev_close,
            gap_open_pct=out.gap_open_pct,
            or_high=out.or_high,
            or_low=out.or_low,
            or_range_pct=out.or_range_pct,
            or_volume_ratio=out.or_volume_ratio,
            midday_range_pct=out.midday_range_pct,
            midday_compression_ratio=out.midday_compression_ratio,
            ph_range_pct=out.ph_range_pct,
            ph_volume_ratio=out.ph_volume_ratio,
            close_vs_or_mid_pct=out.close_vs_or_mid_pct,
            intraday_rv=out.intraday_rv,
            session_label=out.session_label,
            session_numeric=int(out.session_numeric),
            bars_observed=int(out.bars_observed),
            coverage_score=float(out.coverage_score),
        ))

    # Below-coverage rows are still persisted for post-mortem, but the
    # public return is None so callers know not to rely on the labels.
    if out.coverage_score < cfg.min_coverage_score:
        if _ops_log_enabled():
            logger.info(format_intraday_session_ops_line(
                event="intraday_session_skipped",
                mode=mode,
                snapshot_id=out.snapshot_id,
                as_of_date=as_of.isoformat(),
                source_symbol=symbol,
                coverage_score=float(out.coverage_score),
                reason="below_coverage",
            ))
        try:
            _insert_row(db, out=out, mode=mode)
            db.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[intraday_session] low-coverage persist failed: %s", exc,
            )
            db.rollback()
        return None

    try:
        pk = _insert_row(db, out=out, mode=mode)
        db.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[intraday_session] insert failed: %s", exc)
        db.rollback()
        return None

    if _ops_log_enabled():
        logger.info(format_intraday_session_ops_line(
            event="intraday_session_persisted",
            mode=mode,
            snapshot_id=out.snapshot_id,
            as_of_date=as_of.isoformat(),
            source_symbol=symbol,
            session_label=out.session_label,
            session_numeric=int(out.session_numeric),
            coverage_score=float(out.coverage_score),
            pk_id=pk,
        ))

    return IntradaySessionRow(
        pk_id=pk,
        snapshot_id=out.snapshot_id,
        as_of_date=out.as_of_date,
        session_label=out.session_label,
        session_numeric=int(out.session_numeric),
        coverage_score=float(out.coverage_score),
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Read helpers + diagnostics summary
# ---------------------------------------------------------------------------


_SELECT_COLUMNS = """
    id, snapshot_id, as_of_date, source_symbol,
    open_price, close_price, session_high, session_low,
    session_range_pct,
    prev_close, gap_open, gap_open_pct,
    or_high, or_low, or_range_pct, or_volume_ratio,
    midday_range_pct, midday_compression_ratio,
    ph_range_pct, ph_volume_ratio, close_vs_or_mid_pct,
    intraday_rv,
    session_numeric, session_label,
    bars_observed, coverage_score,
    mode, computed_at, observed_at
"""


def _row_to_dict(row: Any) -> dict[str, Any]:
    def _f(v: Any) -> float | None:
        return None if v is None else float(v)

    return {
        "id": int(row[0]),
        "snapshot_id": str(row[1]),
        "as_of_date": row[2].isoformat() if row[2] is not None else None,
        "source_symbol": str(row[3]),
        "open_price": _f(row[4]),
        "close_price": _f(row[5]),
        "session_high": _f(row[6]),
        "session_low": _f(row[7]),
        "session_range_pct": _f(row[8]),
        "prev_close": _f(row[9]),
        "gap_open": _f(row[10]),
        "gap_open_pct": _f(row[11]),
        "or_high": _f(row[12]),
        "or_low": _f(row[13]),
        "or_range_pct": _f(row[14]),
        "or_volume_ratio": _f(row[15]),
        "midday_range_pct": _f(row[16]),
        "midday_compression_ratio": _f(row[17]),
        "ph_range_pct": _f(row[18]),
        "ph_volume_ratio": _f(row[19]),
        "close_vs_or_mid_pct": _f(row[20]),
        "intraday_rv": _f(row[21]),
        "session_numeric": int(row[22] or 0),
        "session_label": str(row[23]),
        "bars_observed": int(row[24] or 0),
        "coverage_score": float(row[25] or 0.0),
        "mode": str(row[26]),
        "computed_at": row[27].isoformat() if row[27] is not None else None,
        "observed_at": row[28].isoformat() if row[28] is not None else None,
    }


def get_latest_snapshot(db: Session) -> dict[str, Any] | None:
    row = db.execute(text(f"""
        SELECT {_SELECT_COLUMNS}
          FROM trading_intraday_session_snapshots
         ORDER BY computed_at DESC
         LIMIT 1
    """)).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def intraday_session_summary(
    db: Session,
    *,
    lookback_days: int = 14,
) -> dict[str, Any]:
    """Frozen-shape diagnostics summary.

    Keys (stable):

    * ``mode``
    * ``lookback_days``
    * ``snapshots_total``
    * ``by_session_label`` (8 keys)
    * ``mean_or_range_pct``
    * ``mean_midday_compression_ratio``
    * ``mean_ph_range_pct``
    * ``mean_intraday_rv``
    * ``mean_session_range_pct``
    * ``mean_gap_open_pct_abs``
    * ``mean_coverage_score``
    * ``latest_snapshot`` — full latest row as a dict (or ``None``).
    """
    mode = _effective_mode()
    ld = int(lookback_days)

    total = int(db.execute(text("""
        SELECT COUNT(*) FROM trading_intraday_session_snapshots
         WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": ld}).scalar_one() or 0)

    by_label: dict[str, int] = {k: 0 for k in sorted(VALID_SESSION_LABELS)}
    rows = db.execute(text("""
        SELECT session_label, COUNT(*)
          FROM trading_intraday_session_snapshots
         WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
           AND session_label IS NOT NULL
         GROUP BY session_label
    """), {"ld": ld}).fetchall()
    for k, v in rows:
        key = str(k)
        if key in by_label:
            by_label[key] = int(v or 0)

    means = db.execute(text("""
        SELECT
            AVG(or_range_pct),
            AVG(midday_compression_ratio),
            AVG(ph_range_pct),
            AVG(intraday_rv),
            AVG(session_range_pct),
            AVG(ABS(gap_open_pct)),
            AVG(coverage_score)
          FROM trading_intraday_session_snapshots
         WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": ld}).fetchone()

    def _f(idx: int) -> float | None:
        if means is None:
            return None
        v = means[idx]
        return None if v is None else round(float(v), 6)

    return {
        "mode": mode,
        "lookback_days": ld,
        "snapshots_total": int(total),
        "by_session_label": by_label,
        "mean_or_range_pct": _f(0),
        "mean_midday_compression_ratio": _f(1),
        "mean_ph_range_pct": _f(2),
        "mean_intraday_rv": _f(3),
        "mean_session_range_pct": _f(4),
        "mean_gap_open_pct_abs": _f(5),
        "mean_coverage_score": _f(6),
        "latest_snapshot": get_latest_snapshot(db),
    }


__all__ = [
    "IntradaySessionRow",
    "_effective_mode",
    "mode_is_active",
    "mode_is_authoritative",
    "compute_and_persist",
    "get_latest_snapshot",
    "intraday_session_summary",
]
