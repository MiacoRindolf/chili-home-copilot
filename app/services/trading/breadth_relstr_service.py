"""Phase L.18 - persistence layer for the breadth + RS snapshot.

Runs the pure breadth + relative-strength model against per-ETF OHLCV
readings fetched via
:func:`app.services.trading.market_data.fetch_ohlcv_df` for the fixed
reference basket (11 sector SPDRs plus SPY / QQQ / IWM).

Design
------

* **Single public entry-point per mode.** :func:`compute_and_persist`
  (one sweep, one row) and :func:`breadth_relstr_summary`
  (diagnostics).
* **Refuses authoritative.** Until Phase L.18.2 opens explicitly the
  service raises :class:`RuntimeError` on
  ``mode_override="authoritative"`` or
  ``brain_breadth_relstr_mode="authoritative"``.
* **Append-only.** Every sweep appends a new row; the deterministic
  ``snapshot_id`` lets callers dedupe.
* **Off-mode short-circuit.** When
  ``brain_breadth_relstr_mode == "off"``
  :func:`compute_and_persist` is a no-op and returns ``None``.
* **Additive-only.** :func:`market_data.get_market_regime` and Phase
  L.17's ``trading_macro_regime_snapshots`` are never modified.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.breadth_relstr_ops_log import (
    format_breadth_relstr_ops_line,
)
from .breadth_relstr_model import (
    ALL_SYMBOLS,
    BreadthRelstrConfig,
    BreadthRelstrInput,
    BreadthRelstrOutput,
    TREND_MISSING,
    UniverseMember,
    classify_direction,
    classify_trend,
    compute_breadth_relstr,
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
        or getattr(settings, "brain_breadth_relstr_mode", "off")
        or "off"
    ).lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def mode_is_authoritative(override: str | None = None) -> bool:
    return _effective_mode(override) == "authoritative"


def _ops_log_enabled() -> bool:
    return bool(
        getattr(settings, "brain_breadth_relstr_ops_log_enabled", True)
    )


def _config_from_settings() -> BreadthRelstrConfig:
    return BreadthRelstrConfig(
        trend_up_threshold=float(getattr(
            settings, "brain_breadth_relstr_trend_up_threshold", 0.01,
        )),
        strong_trend_threshold=float(getattr(
            settings, "brain_breadth_relstr_strong_trend_threshold", 0.03,
        )),
        tilt_threshold=float(getattr(
            settings, "brain_breadth_relstr_tilt_threshold", 0.02,
        )),
        min_coverage_score=float(getattr(
            settings, "brain_breadth_relstr_min_coverage_score", 0.5,
        )),
        risk_on_ratio=float(getattr(
            settings, "brain_breadth_relstr_risk_on_ratio", 0.65,
        )),
        risk_off_ratio=float(getattr(
            settings, "brain_breadth_relstr_risk_off_ratio", 0.35,
        )),
    )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreadthRelstrRow:
    """Thin reference to a persisted snapshot."""

    pk_id: int
    snapshot_id: str
    as_of_date: date
    breadth_label: str
    breadth_numeric: int
    advance_ratio: float
    coverage_score: float
    mode: str


# ---------------------------------------------------------------------------
# ETF fetching / reading assembly
# ---------------------------------------------------------------------------


def _build_universe_member(symbol: str) -> UniverseMember:
    """Fetch OHLCV for one ETF and derive trend + momentum + direction.

    Defensive: any provider failure returns a ``missing=True`` member.
    Lookback of ~3mo is enough for 20d momentum, direction (last vs
    previous close), and new-high/new-low windows.
    """
    try:
        # Local import so unit tests can monkey-patch market_data cheaply.
        from .market_data import fetch_ohlcv_df  # noqa: WPS433

        df = fetch_ohlcv_df(symbol, interval="1d", period="3mo")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[breadth_relstr] fetch_ohlcv_df(%s) raised: %s", symbol, exc,
        )
        return UniverseMember(
            symbol=symbol,
            missing=True,
            trend=TREND_MISSING,
            direction=TREND_MISSING,
        )

    if df is None or df.empty or "Close" not in getattr(df, "columns", []):
        return UniverseMember(
            symbol=symbol,
            missing=True,
            trend=TREND_MISSING,
            direction=TREND_MISSING,
        )

    try:
        closes = df["Close"].dropna()
        if len(closes) < 21:
            return UniverseMember(
                symbol=symbol,
                missing=True,
                trend=TREND_MISSING,
                direction=TREND_MISSING,
            )
        last = float(closes.iloc[-1])
        prev = float(closes.iloc[-2])
        prior_20 = float(closes.iloc[-21])
        momentum_20d = (
            (last / prior_20 - 1.0) if prior_20 > 0 else None
        )
        # 20d window new-high / new-low (use the last 21 closes so the
        # "today" bar is comparable against the trailing 20-session
        # high/low from prior_20 up to yesterday).
        window = closes.iloc[-21:-1]
        if len(window) >= 1:
            new_high_20d = last >= float(window.max())
            new_low_20d = last <= float(window.min())
        else:
            new_high_20d = False
            new_low_20d = False
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[breadth_relstr] parse_ohlcv(%s) raised: %s", symbol, exc,
        )
        return UniverseMember(
            symbol=symbol,
            missing=True,
            trend=TREND_MISSING,
            direction=TREND_MISSING,
        )

    cfg = _config_from_settings()
    trend = classify_trend(momentum_20d, cfg=cfg)
    direction = classify_direction(last, prev)
    return UniverseMember(
        symbol=symbol,
        missing=False,
        last_close=float(last),
        prev_close=float(prev),
        momentum_20d=(
            None if momentum_20d is None else float(momentum_20d)
        ),
        trend=trend,
        direction=direction,
        new_high_20d=bool(new_high_20d),
        new_low_20d=bool(new_low_20d),
    )


def gather_universe_members() -> list[UniverseMember]:
    """Fetch per-symbol readings for every ETF in :data:`ALL_SYMBOLS`.

    Never raises. Missing providers produce ``missing=True`` entries so
    the pure model's coverage-score logic is accurate.
    """
    members: list[UniverseMember] = []
    for sym in ALL_SYMBOLS:
        members.append(_build_universe_member(sym))
    return members


# ---------------------------------------------------------------------------
# Persist one sweep
# ---------------------------------------------------------------------------


def _today_utc_date() -> date:
    return datetime.now(tz=timezone.utc).date()


def compute_and_persist(
    db: Session,
    *,
    as_of_date: date | None = None,
    mode_override: str | None = None,
    members_override: Sequence[UniverseMember] | None = None,
) -> BreadthRelstrRow | None:
    """Compute the snapshot and persist one row in shadow / compare mode.

    Off-mode: returns ``None`` and emits a ``breadth_relstr_skipped``
    line. Authoritative-mode: raises :class:`RuntimeError`.

    ``members_override`` lets tests + the soak script drive the service
    without the network.
    """
    mode = _effective_mode(mode_override)
    as_of = as_of_date or _today_utc_date()

    if mode == "off":
        if _ops_log_enabled():
            logger.info(format_breadth_relstr_ops_line(
                event="breadth_relstr_skipped",
                mode=mode,
                as_of_date=as_of.isoformat(),
                reason="mode_off",
            ))
        return None

    if mode == "authoritative":
        if _ops_log_enabled():
            logger.warning(format_breadth_relstr_ops_line(
                event="breadth_relstr_refused_authoritative",
                mode=mode,
                as_of_date=as_of.isoformat(),
                reason="L.18.1_shadow_only",
            ))
        raise RuntimeError(
            "breadth_relstr authoritative mode is not permitted until "
            "Phase L.18.2 is explicitly opened"
        )

    members = (
        list(members_override)
        if members_override is not None
        else gather_universe_members()
    )

    out: BreadthRelstrOutput = compute_breadth_relstr(
        BreadthRelstrInput(as_of_date=as_of, members=members),
        config=_config_from_settings(),
    )

    if _ops_log_enabled():
        logger.info(format_breadth_relstr_ops_line(
            event="breadth_relstr_computed",
            mode=mode,
            snapshot_id=out.snapshot_id,
            as_of_date=as_of.isoformat(),
            breadth_label=out.breadth_label,
            breadth_numeric=out.breadth_numeric,
            advance_ratio=float(out.advance_ratio),
            leader_sector=out.leader_sector,
            laggard_sector=out.laggard_sector,
            size_tilt=(
                None if out.size_tilt is None else float(out.size_tilt)
            ),
            style_tilt=(
                None if out.style_tilt is None else float(out.style_tilt)
            ),
            symbols_sampled=out.symbols_sampled,
            symbols_missing=out.symbols_missing,
            coverage_score=float(out.coverage_score),
        ))

    cfg = _config_from_settings()
    if out.coverage_score < cfg.min_coverage_score:
        if _ops_log_enabled():
            logger.warning(format_breadth_relstr_ops_line(
                event="breadth_relstr_skipped",
                mode=mode,
                snapshot_id=out.snapshot_id,
                as_of_date=as_of.isoformat(),
                breadth_label=out.breadth_label,
                coverage_score=float(out.coverage_score),
                reason="coverage_below_min",
            ))
        return None

    payload_json = json.dumps(out.payload, default=str)
    sector_json = json.dumps(dict(out.sector_map), default=str)
    now = datetime.utcnow()

    row = db.execute(text("""
        INSERT INTO trading_breadth_relstr_snapshots (
            snapshot_id, as_of_date,
            members_sampled, members_advancing, members_declining,
            members_flat, advance_ratio, new_highs_count, new_lows_count,
            sector_json,
            spy_trend, spy_momentum_20d,
            qqq_trend, qqq_momentum_20d,
            iwm_trend, iwm_momentum_20d,
            size_tilt, style_tilt,
            breadth_numeric, breadth_label,
            leader_sector, laggard_sector,
            symbols_sampled, symbols_missing, coverage_score,
            payload_json, mode, computed_at, observed_at
        ) VALUES (
            :snapshot_id, :as_of_date,
            :members_sampled, :members_advancing, :members_declining,
            :members_flat, :advance_ratio, :new_highs_count, :new_lows_count,
            CAST(:sector_json AS JSONB),
            :spy_trend, :spy_momentum_20d,
            :qqq_trend, :qqq_momentum_20d,
            :iwm_trend, :iwm_momentum_20d,
            :size_tilt, :style_tilt,
            :breadth_numeric, :breadth_label,
            :leader_sector, :laggard_sector,
            :symbols_sampled, :symbols_missing, :coverage_score,
            CAST(:payload_json AS JSONB), :mode, :computed_at, :observed_at
        ) RETURNING id
    """), {
        "snapshot_id": out.snapshot_id,
        "as_of_date": out.as_of_date,
        "members_sampled": int(out.members_sampled),
        "members_advancing": int(out.members_advancing),
        "members_declining": int(out.members_declining),
        "members_flat": int(out.members_flat),
        "advance_ratio": float(out.advance_ratio),
        "new_highs_count": int(out.new_highs_count),
        "new_lows_count": int(out.new_lows_count),
        "sector_json": sector_json,
        "spy_trend": out.spy_trend,
        "spy_momentum_20d": out.spy_momentum_20d,
        "qqq_trend": out.qqq_trend,
        "qqq_momentum_20d": out.qqq_momentum_20d,
        "iwm_trend": out.iwm_trend,
        "iwm_momentum_20d": out.iwm_momentum_20d,
        "size_tilt": out.size_tilt,
        "style_tilt": out.style_tilt,
        "breadth_numeric": int(out.breadth_numeric),
        "breadth_label": out.breadth_label,
        "leader_sector": out.leader_sector,
        "laggard_sector": out.laggard_sector,
        "symbols_sampled": int(out.symbols_sampled),
        "symbols_missing": int(out.symbols_missing),
        "coverage_score": float(out.coverage_score),
        "payload_json": payload_json,
        "mode": mode,
        "computed_at": now,
        "observed_at": now,
    }).fetchone()
    db.commit()

    pk_id = int(row[0]) if row else 0

    if _ops_log_enabled():
        logger.info(format_breadth_relstr_ops_line(
            event="breadth_relstr_persisted",
            mode=mode,
            snapshot_id=out.snapshot_id,
            as_of_date=as_of.isoformat(),
            breadth_label=out.breadth_label,
            breadth_numeric=out.breadth_numeric,
            advance_ratio=float(out.advance_ratio),
            leader_sector=out.leader_sector,
            laggard_sector=out.laggard_sector,
            size_tilt=(
                None if out.size_tilt is None else float(out.size_tilt)
            ),
            style_tilt=(
                None if out.style_tilt is None else float(out.style_tilt)
            ),
            symbols_sampled=out.symbols_sampled,
            symbols_missing=out.symbols_missing,
            coverage_score=float(out.coverage_score),
            pk_id=pk_id,
        ))

    return BreadthRelstrRow(
        pk_id=pk_id,
        snapshot_id=out.snapshot_id,
        as_of_date=out.as_of_date,
        breadth_label=out.breadth_label,
        breadth_numeric=int(out.breadth_numeric),
        advance_ratio=float(out.advance_ratio),
        coverage_score=float(out.coverage_score),
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def get_latest_snapshot(db: Session) -> dict[str, Any] | None:
    """Read the most recent snapshot as a plain dict.

    Read-only helper used by the diagnostics endpoint. Not yet wired
    into any hot-path consumer (that is L.18.2).
    """
    row = db.execute(text("""
        SELECT id, snapshot_id, as_of_date,
               members_sampled, members_advancing, members_declining,
               advance_ratio, breadth_label, breadth_numeric,
               leader_sector, laggard_sector,
               size_tilt, style_tilt,
               symbols_sampled, symbols_missing, coverage_score,
               mode, computed_at, observed_at
          FROM trading_breadth_relstr_snapshots
         ORDER BY computed_at DESC
         LIMIT 1
    """)).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "snapshot_id": str(row[1]),
        "as_of_date": row[2].isoformat() if row[2] is not None else None,
        "members_sampled": int(row[3] or 0),
        "members_advancing": int(row[4] or 0),
        "members_declining": int(row[5] or 0),
        "advance_ratio": float(row[6] or 0.0),
        "breadth_label": str(row[7]),
        "breadth_numeric": int(row[8] or 0),
        "leader_sector": (str(row[9]) if row[9] is not None else None),
        "laggard_sector": (str(row[10]) if row[10] is not None else None),
        "size_tilt": (float(row[11]) if row[11] is not None else None),
        "style_tilt": (float(row[12]) if row[12] is not None else None),
        "symbols_sampled": int(row[13] or 0),
        "symbols_missing": int(row[14] or 0),
        "coverage_score": float(row[15] or 0.0),
        "mode": str(row[16]),
        "computed_at": row[17].isoformat() if row[17] is not None else None,
        "observed_at": row[18].isoformat() if row[18] is not None else None,
    }


def breadth_relstr_summary(
    db: Session,
    *,
    lookback_days: int = 14,
) -> dict[str, Any]:
    """Frozen-shape diagnostics summary for the breadth + RS panel.

    Keys (stable, order-preserving):

    * ``mode``
    * ``lookback_days``
    * ``snapshots_total``
    * ``by_breadth_label`` - ``{broad_risk_on, mixed, broad_risk_off}``
    * ``by_leader_sector``
    * ``by_laggard_sector``
    * ``mean_advance_ratio``
    * ``mean_coverage_score``
    * ``latest_snapshot`` (or ``None`` when empty)
    """
    mode = _effective_mode()

    total = int(db.execute(text("""
        SELECT COUNT(*) FROM trading_breadth_relstr_snapshots
        WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0)

    label_rows = db.execute(text("""
        SELECT breadth_label, COUNT(*) FROM trading_breadth_relstr_snapshots
        WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
        GROUP BY breadth_label
    """), {"ld": int(lookback_days)}).fetchall()
    by_breadth_label = {
        "broad_risk_on": 0,
        "mixed": 0,
        "broad_risk_off": 0,
    }
    for lbl, cnt in label_rows:
        key = str(lbl)
        if key in by_breadth_label:
            by_breadth_label[key] = int(cnt or 0)

    def _count_by(col: str) -> dict[str, int]:
        rows = db.execute(text(f"""
            SELECT {col}, COUNT(*) FROM trading_breadth_relstr_snapshots
            WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
              AND {col} IS NOT NULL
            GROUP BY {col}
        """), {"ld": int(lookback_days)}).fetchall()
        return {str(k): int(v or 0) for k, v in rows}

    mean_adv = float(db.execute(text("""
        SELECT AVG(advance_ratio) FROM trading_breadth_relstr_snapshots
         WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0.0)

    mean_coverage = float(db.execute(text("""
        SELECT AVG(coverage_score) FROM trading_breadth_relstr_snapshots
         WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0.0)

    latest = get_latest_snapshot(db)

    return {
        "mode": mode,
        "lookback_days": int(lookback_days),
        "snapshots_total": int(total),
        "by_breadth_label": by_breadth_label,
        "by_leader_sector": _count_by("leader_sector"),
        "by_laggard_sector": _count_by("laggard_sector"),
        "mean_advance_ratio": round(float(mean_adv), 6),
        "mean_coverage_score": round(float(mean_coverage), 6),
        "latest_snapshot": latest,
    }


__all__ = [
    "BreadthRelstrRow",
    "_effective_mode",
    "mode_is_active",
    "mode_is_authoritative",
    "gather_universe_members",
    "compute_and_persist",
    "get_latest_snapshot",
    "breadth_relstr_summary",
]
