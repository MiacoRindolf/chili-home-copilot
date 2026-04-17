"""Phase L.19 - persistence layer for the cross-asset signals snapshot.

Runs the pure cross-asset model against per-symbol OHLCV readings fetched
via :func:`app.services.trading.market_data.fetch_ohlcv_df` for the fixed
lead/lag basket (SPY, TLT, HYG, LQD, UUP, BTC-USD, ETH-USD) plus context
echoed from:

* Phase L.17 ``trading_macro_regime_snapshots`` (``macro_label`` only);
* Phase L.18 ``trading_breadth_relstr_snapshots`` (``advance_ratio`` +
  ``breadth_label``);
* :func:`market_data.get_market_regime` (``vix`` + ``volatility_percentile``).

Design
------
* **Single public entry-point.** :func:`compute_and_persist` (one sweep,
  one row) and :func:`cross_asset_summary` (diagnostics).
* **Refuses authoritative.** Until Phase L.19.2 opens explicitly the
  service raises :class:`RuntimeError` on
  ``mode_override="authoritative"`` or
  ``brain_cross_asset_mode="authoritative"``.
* **Append-only.** Every sweep appends a new row; the deterministic
  ``snapshot_id`` (dated by ``as_of_date``) lets callers dedupe.
* **Off-mode short-circuit.** When ``brain_cross_asset_mode == "off"``
  :func:`compute_and_persist` is a no-op and returns ``None``.
* **Additive-only.** :func:`market_data.get_market_regime` and Phase
  L.17 / L.18 tables are never mutated.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.cross_asset_ops_log import (
    format_cross_asset_ops_line,
)
from .cross_asset_model import (
    ALL_SYMBOLS,
    AssetLeg,
    CrossAssetConfig,
    CrossAssetInput,
    CrossAssetOutput,
    SYMBOL_BTC,
    SYMBOL_ETH,
    SYMBOL_HYG,
    SYMBOL_LQD,
    SYMBOL_SPY,
    SYMBOL_TLT,
    SYMBOL_UUP,
    compute_cross_asset,
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
        or getattr(settings, "brain_cross_asset_mode", "off")
        or "off"
    ).lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def mode_is_authoritative(override: str | None = None) -> bool:
    return _effective_mode(override) == "authoritative"


def _ops_log_enabled() -> bool:
    return bool(
        getattr(settings, "brain_cross_asset_ops_log_enabled", True)
    )


def _config_from_settings() -> CrossAssetConfig:
    return CrossAssetConfig(
        fast_lead_threshold=float(getattr(
            settings, "brain_cross_asset_fast_lead_threshold", 0.01,
        )),
        slow_lead_threshold=float(getattr(
            settings, "brain_cross_asset_slow_lead_threshold", 0.03,
        )),
        vix_percentile_shock=float(getattr(
            settings, "brain_cross_asset_vix_percentile_shock", 0.80,
        )),
        min_coverage_score=float(getattr(
            settings, "brain_cross_asset_min_coverage_score", 0.5,
        )),
        beta_window_days=int(getattr(
            settings, "brain_cross_asset_beta_window_days", 60,
        )),
        composite_min_agreement=int(getattr(
            settings, "brain_cross_asset_composite_min_agreement", 2,
        )),
    )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrossAssetRow:
    """Thin reference to a persisted snapshot."""

    pk_id: int
    snapshot_id: str
    as_of_date: date
    cross_asset_label: str
    cross_asset_numeric: int
    coverage_score: float
    mode: str


# ---------------------------------------------------------------------------
# Reading assembly
# ---------------------------------------------------------------------------


def _safe_ret(prior: float | None, last: float | None) -> float | None:
    if prior is None or last is None:
        return None
    try:
        prior_f = float(prior)
        last_f = float(last)
    except (TypeError, ValueError):
        return None
    if prior_f <= 0.0:
        return None
    return (last_f / prior_f) - 1.0


def _build_asset_leg(
    symbol: str, *, want_daily: bool = False,
) -> AssetLeg:
    """Fetch OHLCV for one symbol and derive 1d/5d/20d returns.

    Defensive: any provider failure returns a ``missing=True`` leg.
    ``want_daily`` requests that the trailing daily-return series is
    included (needed for beta legs: SPY + BTC).
    """
    try:
        from .market_data import fetch_ohlcv_df  # noqa: WPS433

        # 6mo gives enough bars for a 60-session beta window + 20d
        # return, with a safety margin.
        df = fetch_ohlcv_df(symbol, interval="1d", period="6mo")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[cross_asset] fetch_ohlcv_df(%s) raised: %s", symbol, exc,
        )
        return AssetLeg(
            symbol=symbol,
            last_close=None,
            ret_1d=None,
            ret_5d=None,
            ret_20d=None,
            missing=True,
        )

    if df is None or df.empty or "Close" not in getattr(df, "columns", []):
        return AssetLeg(
            symbol=symbol,
            last_close=None,
            ret_1d=None,
            ret_5d=None,
            ret_20d=None,
            missing=True,
        )

    try:
        closes = df["Close"].dropna()
        n = len(closes)
        if n < 21:
            return AssetLeg(
                symbol=symbol,
                last_close=(float(closes.iloc[-1]) if n else None),
                ret_1d=None,
                ret_5d=None,
                ret_20d=None,
                missing=True,
            )
        last = float(closes.iloc[-1])
        prev = float(closes.iloc[-2])
        ret_1d = _safe_ret(prev, last)
        ret_5d = (
            _safe_ret(float(closes.iloc[-6]), last)
            if n >= 6
            else None
        )
        ret_20d = (
            _safe_ret(float(closes.iloc[-21]), last)
            if n >= 21
            else None
        )
        returns_daily: tuple[float, ...] = ()
        if want_daily:
            # most-recent last
            diffs = closes.pct_change().dropna()
            returns_daily = tuple(
                float(x) for x in diffs.iloc[-252:].tolist()
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[cross_asset] parse_ohlcv(%s) raised: %s", symbol, exc,
        )
        return AssetLeg(
            symbol=symbol,
            last_close=None,
            ret_1d=None,
            ret_5d=None,
            ret_20d=None,
            missing=True,
        )

    return AssetLeg(
        symbol=symbol,
        last_close=float(last),
        ret_1d=(None if ret_1d is None else float(ret_1d)),
        ret_5d=(None if ret_5d is None else float(ret_5d)),
        ret_20d=(None if ret_20d is None else float(ret_20d)),
        missing=False,
        returns_daily=returns_daily,
    )


def gather_asset_legs() -> dict[str, AssetLeg]:
    """Fetch per-symbol readings for every symbol in :data:`ALL_SYMBOLS`.

    Never raises. Missing providers produce ``missing=True`` legs so the
    pure model's coverage logic stays accurate. SPY and BTC-USD request
    the trailing daily-return series for the beta computation.
    """
    legs: dict[str, AssetLeg] = {}
    for sym in ALL_SYMBOLS:
        want_daily = sym in (SYMBOL_SPY, SYMBOL_BTC)
        legs[sym] = _build_asset_leg(sym, want_daily=want_daily)
    return legs


# ---------------------------------------------------------------------------
# L.17 / L.18 context read (safe, read-only)
# ---------------------------------------------------------------------------


def _fetch_latest_macro_label(db: Session) -> str | None:
    """Read most-recent macro_label from L.17 snapshots (best-effort)."""
    try:
        row = db.execute(text("""
            SELECT macro_label FROM trading_macro_regime_snapshots
            ORDER BY computed_at DESC LIMIT 1
        """)).fetchone()
        if row is None:
            return None
        return str(row[0]) if row[0] is not None else None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[cross_asset] macro_label read failed: %s", exc)
        return None


def _fetch_latest_breadth_context(
    db: Session,
) -> tuple[float | None, str | None]:
    """Read most-recent advance_ratio + breadth_label from L.18."""
    try:
        row = db.execute(text("""
            SELECT advance_ratio, breadth_label
              FROM trading_breadth_relstr_snapshots
             ORDER BY computed_at DESC LIMIT 1
        """)).fetchone()
        if row is None:
            return None, None
        adv = float(row[0]) if row[0] is not None else None
        label = str(row[1]) if row[1] is not None else None
        return adv, label
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[cross_asset] breadth_context read failed: %s", exc)
        return None, None


def _fetch_vix_context() -> tuple[float | None, float | None]:
    """Read vix_level + percentile from ``market_data.get_market_regime``."""
    try:
        from .market_data import get_market_regime  # noqa: WPS433

        regime = get_market_regime() or {}
        vix = regime.get("vix")
        pct = regime.get("volatility_percentile")
        vix_f = float(vix) if vix is not None else None
        pct_f = float(pct) if pct is not None else None
        if pct_f is not None and pct_f > 1.0:
            # Some providers return 0-100; normalize to 0-1.
            pct_f = pct_f / 100.0
        return vix_f, pct_f
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[cross_asset] vix context read failed: %s", exc)
        return None, None


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
    legs_override: dict[str, AssetLeg] | None = None,
    vix_level_override: float | None = None,
    vix_percentile_override: float | None = None,
    breadth_advance_ratio_override: float | None = None,
    breadth_label_override: str | None = None,
    macro_label_override: str | None = None,
) -> CrossAssetRow | None:
    """Compute the snapshot and persist one row in shadow / compare mode.

    Off-mode: returns ``None`` and emits a ``cross_asset_skipped`` line.
    Authoritative-mode: raises :class:`RuntimeError`.

    ``*_override`` parameters let tests + the soak script drive the
    service without the network or DB context.
    """
    mode = _effective_mode(mode_override)
    as_of = as_of_date or _today_utc_date()

    if mode == "off":
        if _ops_log_enabled():
            logger.info(format_cross_asset_ops_line(
                event="cross_asset_skipped",
                mode=mode,
                as_of_date=as_of.isoformat(),
                reason="mode_off",
            ))
        return None

    if mode == "authoritative":
        if _ops_log_enabled():
            logger.warning(format_cross_asset_ops_line(
                event="cross_asset_refused_authoritative",
                mode=mode,
                as_of_date=as_of.isoformat(),
                reason="L.19.1_shadow_only",
            ))
        raise RuntimeError(
            "cross_asset authoritative mode is not permitted until "
            "Phase L.19.2 is explicitly opened"
        )

    legs = legs_override if legs_override is not None else gather_asset_legs()

    def _leg(sym: str) -> AssetLeg:
        return legs.get(sym) or AssetLeg(
            symbol=sym,
            last_close=None, ret_1d=None, ret_5d=None, ret_20d=None,
            missing=True,
        )

    # Context
    vix_level, vix_percentile = (
        (vix_level_override, vix_percentile_override)
        if vix_level_override is not None or vix_percentile_override is not None
        else _fetch_vix_context()
    )
    breadth_adv, breadth_label = (
        (breadth_advance_ratio_override, breadth_label_override)
        if breadth_advance_ratio_override is not None
        or breadth_label_override is not None
        else _fetch_latest_breadth_context(db)
    )
    macro_label = (
        macro_label_override
        if macro_label_override is not None
        else _fetch_latest_macro_label(db)
    )

    out: CrossAssetOutput = compute_cross_asset(
        CrossAssetInput(
            as_of_date=as_of,
            equity=_leg(SYMBOL_SPY),
            rates=_leg(SYMBOL_TLT),
            credit_hy=_leg(SYMBOL_HYG),
            credit_ig=_leg(SYMBOL_LQD),
            usd=_leg(SYMBOL_UUP),
            crypto_btc=_leg(SYMBOL_BTC),
            crypto_eth=_leg(SYMBOL_ETH),
            vix_level=vix_level,
            vix_percentile=vix_percentile,
            breadth_advance_ratio=breadth_adv,
            breadth_label=breadth_label,
            macro_label=macro_label,
            config=_config_from_settings(),
        )
    )

    if _ops_log_enabled():
        logger.info(format_cross_asset_ops_line(
            event="cross_asset_computed",
            mode=mode,
            snapshot_id=out.snapshot_id,
            as_of_date=as_of.isoformat(),
            cross_asset_label=out.cross_asset_label,
            cross_asset_numeric=out.cross_asset_numeric,
            bond_equity_label=out.bond_equity_label,
            credit_equity_label=out.credit_equity_label,
            usd_crypto_label=out.usd_crypto_label,
            vix_breadth_label=out.vix_breadth_label,
            crypto_equity_beta=(
                None if out.crypto_equity_beta is None
                else float(out.crypto_equity_beta)
            ),
            symbols_sampled=out.symbols_sampled,
            symbols_missing=out.symbols_missing,
            coverage_score=float(out.coverage_score),
        ))

    cfg = _config_from_settings()
    if out.coverage_score < cfg.min_coverage_score:
        if _ops_log_enabled():
            logger.warning(format_cross_asset_ops_line(
                event="cross_asset_skipped",
                mode=mode,
                snapshot_id=out.snapshot_id,
                as_of_date=as_of.isoformat(),
                cross_asset_label=out.cross_asset_label,
                coverage_score=float(out.coverage_score),
                reason="coverage_below_min",
            ))
        return None

    payload_json = json.dumps(out.payload, default=str)
    now = datetime.utcnow()

    row = db.execute(text("""
        INSERT INTO trading_cross_asset_snapshots (
            snapshot_id, as_of_date,
            bond_equity_lead_5d, bond_equity_lead_20d, bond_equity_label,
            credit_equity_lead_5d, credit_equity_lead_20d, credit_equity_label,
            usd_crypto_lead_5d, usd_crypto_lead_20d, usd_crypto_label,
            vix_level, vix_percentile, breadth_advance_ratio,
            vix_breadth_divergence_score, vix_breadth_label,
            crypto_equity_beta, crypto_equity_beta_window_days,
            crypto_equity_correlation,
            cross_asset_numeric, cross_asset_label,
            symbols_sampled, symbols_missing, coverage_score,
            payload_json, mode, computed_at, observed_at
        ) VALUES (
            :snapshot_id, :as_of_date,
            :be5, :be20, :be_label,
            :ce5, :ce20, :ce_label,
            :uc5, :uc20, :uc_label,
            :vix_level, :vix_percentile, :breadth_adv,
            :vbd_score, :vbd_label,
            :beta, :beta_w, :corr,
            :cross_num, :cross_label,
            :sampled, :missing, :coverage,
            CAST(:payload_json AS JSONB), :mode, :computed_at, :observed_at
        ) RETURNING id
    """), {
        "snapshot_id": out.snapshot_id,
        "as_of_date": out.as_of_date,
        "be5": out.bond_equity_lead_5d,
        "be20": out.bond_equity_lead_20d,
        "be_label": out.bond_equity_label,
        "ce5": out.credit_equity_lead_5d,
        "ce20": out.credit_equity_lead_20d,
        "ce_label": out.credit_equity_label,
        "uc5": out.usd_crypto_lead_5d,
        "uc20": out.usd_crypto_lead_20d,
        "uc_label": out.usd_crypto_label,
        "vix_level": out.vix_level,
        "vix_percentile": out.vix_percentile,
        "breadth_adv": out.breadth_advance_ratio,
        "vbd_score": out.vix_breadth_divergence_score,
        "vbd_label": out.vix_breadth_label,
        "beta": out.crypto_equity_beta,
        "beta_w": out.crypto_equity_beta_window_days,
        "corr": out.crypto_equity_correlation,
        "cross_num": int(out.cross_asset_numeric),
        "cross_label": out.cross_asset_label,
        "sampled": int(out.symbols_sampled),
        "missing": int(out.symbols_missing),
        "coverage": float(out.coverage_score),
        "payload_json": payload_json,
        "mode": mode,
        "computed_at": now,
        "observed_at": now,
    }).fetchone()
    db.commit()

    pk_id = int(row[0]) if row else 0

    if _ops_log_enabled():
        logger.info(format_cross_asset_ops_line(
            event="cross_asset_persisted",
            mode=mode,
            snapshot_id=out.snapshot_id,
            as_of_date=as_of.isoformat(),
            cross_asset_label=out.cross_asset_label,
            cross_asset_numeric=out.cross_asset_numeric,
            bond_equity_label=out.bond_equity_label,
            credit_equity_label=out.credit_equity_label,
            usd_crypto_label=out.usd_crypto_label,
            vix_breadth_label=out.vix_breadth_label,
            crypto_equity_beta=(
                None if out.crypto_equity_beta is None
                else float(out.crypto_equity_beta)
            ),
            symbols_sampled=out.symbols_sampled,
            symbols_missing=out.symbols_missing,
            coverage_score=float(out.coverage_score),
            pk_id=pk_id,
        ))

    return CrossAssetRow(
        pk_id=pk_id,
        snapshot_id=out.snapshot_id,
        as_of_date=out.as_of_date,
        cross_asset_label=out.cross_asset_label,
        cross_asset_numeric=int(out.cross_asset_numeric),
        coverage_score=float(out.coverage_score),
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def get_latest_snapshot(db: Session) -> dict[str, Any] | None:
    """Read the most recent snapshot as a plain dict."""
    row = db.execute(text("""
        SELECT id, snapshot_id, as_of_date,
               bond_equity_label, credit_equity_label,
               usd_crypto_label, vix_breadth_label,
               crypto_equity_beta, crypto_equity_beta_window_days,
               crypto_equity_correlation,
               cross_asset_label, cross_asset_numeric,
               symbols_sampled, symbols_missing, coverage_score,
               mode, computed_at, observed_at
          FROM trading_cross_asset_snapshots
         ORDER BY computed_at DESC
         LIMIT 1
    """)).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "snapshot_id": str(row[1]),
        "as_of_date": row[2].isoformat() if row[2] is not None else None,
        "bond_equity_label": (str(row[3]) if row[3] is not None else None),
        "credit_equity_label": (str(row[4]) if row[4] is not None else None),
        "usd_crypto_label": (str(row[5]) if row[5] is not None else None),
        "vix_breadth_label": (str(row[6]) if row[6] is not None else None),
        "crypto_equity_beta": (
            float(row[7]) if row[7] is not None else None
        ),
        "crypto_equity_beta_window_days": (
            int(row[8]) if row[8] is not None else None
        ),
        "crypto_equity_correlation": (
            float(row[9]) if row[9] is not None else None
        ),
        "cross_asset_label": str(row[10]),
        "cross_asset_numeric": int(row[11] or 0),
        "symbols_sampled": int(row[12] or 0),
        "symbols_missing": int(row[13] or 0),
        "coverage_score": float(row[14] or 0.0),
        "mode": str(row[15]),
        "computed_at": row[16].isoformat() if row[16] is not None else None,
        "observed_at": row[17].isoformat() if row[17] is not None else None,
    }


def cross_asset_summary(
    db: Session,
    *,
    lookback_days: int = 14,
) -> dict[str, Any]:
    """Frozen-shape diagnostics summary for the cross-asset panel.

    Keys (stable, order-preserving):

    * ``mode``
    * ``lookback_days``
    * ``snapshots_total``
    * ``by_cross_asset_label`` - risk_on_crosscheck / risk_off_crosscheck
      / divergence / neutral
    * ``by_bond_equity_label``
    * ``by_credit_equity_label``
    * ``by_usd_crypto_label``
    * ``by_vix_breadth_label``
    * ``mean_coverage_score``
    * ``latest_snapshot`` (or ``None`` when empty)
    """
    mode = _effective_mode()

    total = int(db.execute(text("""
        SELECT COUNT(*) FROM trading_cross_asset_snapshots
        WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0)

    def _count_by(col: str, *, include_null: bool = False) -> dict[str, int]:
        null_clause = "" if include_null else f"AND {col} IS NOT NULL"
        rows = db.execute(text(f"""
            SELECT {col}, COUNT(*) FROM trading_cross_asset_snapshots
            WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
            {null_clause}
            GROUP BY {col}
        """), {"ld": int(lookback_days)}).fetchall()
        return {
            (str(k) if k is not None else "null"): int(v or 0)
            for k, v in rows
        }

    by_cross = {
        "risk_on_crosscheck": 0,
        "risk_off_crosscheck": 0,
        "divergence": 0,
        "neutral": 0,
    }
    for k, v in _count_by("cross_asset_label").items():
        if k in by_cross:
            by_cross[k] = int(v)

    mean_coverage = float(db.execute(text("""
        SELECT AVG(coverage_score) FROM trading_cross_asset_snapshots
         WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0.0)

    latest = get_latest_snapshot(db)

    return {
        "mode": mode,
        "lookback_days": int(lookback_days),
        "snapshots_total": int(total),
        "by_cross_asset_label": by_cross,
        "by_bond_equity_label": _count_by("bond_equity_label"),
        "by_credit_equity_label": _count_by("credit_equity_label"),
        "by_usd_crypto_label": _count_by("usd_crypto_label"),
        "by_vix_breadth_label": _count_by("vix_breadth_label"),
        "mean_coverage_score": round(float(mean_coverage), 6),
        "latest_snapshot": latest,
    }


__all__ = [
    "CrossAssetRow",
    "_effective_mode",
    "mode_is_active",
    "mode_is_authoritative",
    "gather_asset_legs",
    "compute_and_persist",
    "get_latest_snapshot",
    "cross_asset_summary",
]
