"""Phase L.17 Docker soak - macro regime expansion (shadow).

Verifies inside the running ``chili`` container that:

  1. Migration 138 applied (``trading_macro_regime_snapshots`` exists).
  2. ``BRAIN_MACRO_REGIME_MODE`` is visible on ``settings`` (and all
     related tuning thresholds).
  3. ``compute_macro_regime`` pure classifier returns the frozen output
     shape for green/yellow/red synthetic readings.
  4. ``compute_and_persist`` writes exactly one row when forced to
     shadow mode with synthetic readings and echoes the equity inputs.
  5. ``compute_and_persist`` is a no-op when the current mode is
     ``off`` and hard-refuses ``authoritative``.
  6. Coverage-score gate: compute_and_persist returns ``None`` and
     skips persistence when the coverage is below
     ``brain_macro_regime_min_coverage_score``.
  7. Determinism: same ``as_of_date`` yields identical ``regime_id``;
     a subsequent sweep with the same date is append-only (no silent
     overwrite).
  8. ``macro_regime_summary`` returns the frozen wire shape.
  9. Additive-only guarantee: ``get_market_regime()`` still returns
     its pre-L.17 top-level keys.
"""
from __future__ import annotations

import os
import sys
from datetime import date

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.services.trading.macro_regime_model import (  # noqa: E402
    SYMBOL_HYG,
    SYMBOL_IEF,
    SYMBOL_LQD,
    SYMBOL_SHY,
    SYMBOL_TLT,
    SYMBOL_UUP,
    TREND_DOWN,
    TREND_FLAT,
    TREND_UP,
    AssetReading,
    EquityRegimeInput,
    MacroRegimeInput,
    compute_macro_regime,
    compute_regime_id,
)
from app.services.trading.macro_regime_service import (  # noqa: E402
    compute_and_persist,
    macro_regime_summary,
)

SOAK_AS_OF = date(2026, 4, 17)
SOAK_AS_OF_2 = date(2026, 4, 18)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"[phase_l17_soak] FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[phase_l17_soak] OK  : {msg}")


def _cleanup(db) -> None:
    db.execute(text(
        "DELETE FROM trading_macro_regime_snapshots "
        "WHERE as_of_date IN (:a, :b)"
    ), {"a": SOAK_AS_OF, "b": SOAK_AS_OF_2})
    db.commit()


def _equity_stub() -> EquityRegimeInput:
    return EquityRegimeInput(
        spy_direction="up",
        spy_momentum_5d=0.012,
        vix=16.5,
        vix_regime="low",
        volatility_percentile=0.3,
        composite="risk_on",
        regime_numeric=1,
    )


def _reading_up(sym: str, mom20: float = 0.04) -> AssetReading:
    return AssetReading(
        symbol=sym,
        last_close=100.0,
        momentum_20d=mom20,
        momentum_5d=0.015,
        trend=TREND_UP,
        missing=False,
    )


def _reading_down(sym: str, mom20: float = -0.04) -> AssetReading:
    return AssetReading(
        symbol=sym,
        last_close=100.0,
        momentum_20d=mom20,
        momentum_5d=-0.015,
        trend=TREND_DOWN,
        missing=False,
    )


def _reading_flat(sym: str) -> AssetReading:
    return AssetReading(
        symbol=sym,
        last_close=100.0,
        momentum_20d=0.002,
        momentum_5d=0.001,
        trend=TREND_FLAT,
        missing=False,
    )


def _reading_missing(sym: str) -> AssetReading:
    return AssetReading(symbol=sym, missing=True)


def _check_schema_and_settings(db) -> None:
    row = db.execute(text(
        "SELECT to_regclass('public.trading_macro_regime_snapshots')"
    )).scalar_one()
    _assert(
        row is not None,
        "trading_macro_regime_snapshots exists",
    )

    row = db.execute(text(
        "SELECT version_id FROM schema_version "
        "WHERE version_id = '138_macro_regime_snapshot'"
    )).fetchone()
    _assert(
        row is not None,
        "migration 138_macro_regime_snapshot recorded",
    )

    for attr in (
        "brain_macro_regime_mode",
        "brain_macro_regime_ops_log_enabled",
        "brain_macro_regime_cron_hour",
        "brain_macro_regime_cron_minute",
        "brain_macro_regime_min_coverage_score",
        "brain_macro_regime_trend_up_threshold",
        "brain_macro_regime_strong_trend_threshold",
        "brain_macro_regime_promote_threshold",
        "brain_macro_regime_weight_rates",
        "brain_macro_regime_weight_credit",
        "brain_macro_regime_weight_usd",
        "brain_macro_regime_lookback_days",
    ):
        _assert(hasattr(settings, attr), f"settings.{attr} exists")


def _check_pure_model() -> None:
    # Green / risk_on - HYG outperforms LQD (credit tightening), rates
    # selling off (rates_risk_on), USD weak.
    readings_risk_on = [
        _reading_down(SYMBOL_IEF),
        _reading_down(SYMBOL_SHY),
        _reading_down(SYMBOL_TLT, mom20=-0.05),
        _reading_up(SYMBOL_HYG, mom20=0.04),
        _reading_up(SYMBOL_LQD, mom20=0.01),  # HYG leads LQD by >0.5pp
        _reading_down(SYMBOL_UUP),
    ]
    out_on = compute_macro_regime(MacroRegimeInput(
        as_of_date=SOAK_AS_OF,
        equity=_equity_stub(),
        readings=readings_risk_on,
    ))
    _assert(out_on.regime_id == compute_regime_id(SOAK_AS_OF), "regime_id deterministic")
    _assert(out_on.macro_label in ("risk_on", "cautious", "risk_off"), "macro_label in valid set")
    _assert(out_on.symbols_sampled == 6, "6 symbols sampled")
    _assert(out_on.symbols_missing == 0, "0 symbols missing")
    _assert(abs(out_on.coverage_score - 1.0) < 1e-9, "coverage_score=1.0")
    _assert(
        out_on.credit_regime == "credit_tightening",
        "credit_regime=credit_tightening when HYG >> LQD",
    )
    _assert(out_on.usd_regime == "usd_weak", "usd_regime=usd_weak when UUP trending down")
    _assert(
        out_on.rates_regime in ("rates_risk_on", "rates_neutral"),
        "rates_regime risk_on/neutral when TLT selling off",
    )

    # Red / risk_off - credit widening (LQD > HYG), rates rallying, USD strong.
    readings_risk_off = [
        _reading_up(SYMBOL_IEF, mom20=0.03),
        _reading_flat(SYMBOL_SHY),
        _reading_up(SYMBOL_TLT, mom20=0.05),
        _reading_down(SYMBOL_HYG, mom20=-0.04),
        _reading_down(SYMBOL_LQD, mom20=-0.01),  # LQD outperforms HYG
        _reading_up(SYMBOL_UUP, mom20=0.04),
    ]
    out_off = compute_macro_regime(MacroRegimeInput(
        as_of_date=SOAK_AS_OF,
        equity=_equity_stub(),
        readings=readings_risk_off,
    ))
    _assert(
        out_off.credit_regime == "credit_widening",
        "credit_regime=credit_widening when LQD > HYG",
    )
    _assert(out_off.usd_regime == "usd_strong", "usd_regime=usd_strong when UUP up")
    _assert(
        out_off.macro_label in ("risk_off", "cautious"),
        "risk_off path yields risk_off/cautious",
    )

    # Cautious - mixed.
    readings_mixed = [
        _reading_flat(SYMBOL_IEF),
        _reading_flat(SYMBOL_SHY),
        _reading_flat(SYMBOL_TLT),
        _reading_up(SYMBOL_HYG),
        _reading_down(SYMBOL_LQD),
        _reading_flat(SYMBOL_UUP),
    ]
    out_mid = compute_macro_regime(MacroRegimeInput(
        as_of_date=SOAK_AS_OF,
        equity=_equity_stub(),
        readings=readings_mixed,
    ))
    _assert(out_mid.macro_label in ("risk_on", "cautious", "risk_off"), "mixed yields valid label")

    # Missing symbols should reduce coverage.
    partial = [
        _reading_up(SYMBOL_HYG),
        _reading_up(SYMBOL_LQD),
        _reading_missing(SYMBOL_IEF),
        _reading_missing(SYMBOL_SHY),
        _reading_missing(SYMBOL_TLT),
        _reading_missing(SYMBOL_UUP),
    ]
    out_partial = compute_macro_regime(MacroRegimeInput(
        as_of_date=SOAK_AS_OF,
        equity=_equity_stub(),
        readings=partial,
    ))
    _assert(out_partial.symbols_sampled == 2, "partial symbols_sampled=2")
    _assert(out_partial.symbols_missing == 4, "partial symbols_missing=4")
    _assert(abs(out_partial.coverage_score - (2 / 6)) < 1e-6, "partial coverage_score=2/6")


def _check_persist_shadow(db) -> None:
    # All trend_up - 100% coverage, above 0.5 min coverage.
    readings = [
        _reading_up(s)
        for s in (SYMBOL_IEF, SYMBOL_SHY, SYMBOL_TLT,
                  SYMBOL_HYG, SYMBOL_LQD, SYMBOL_UUP)
    ]
    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="shadow",
        readings_override=readings,
        equity_override=_equity_stub(),
    )
    _assert(row is not None, "compute_and_persist writes row in shadow")
    assert row is not None
    _assert(row.mode == "shadow", "persisted row mode=shadow")
    _assert(row.as_of_date == SOAK_AS_OF, "persisted row as_of_date matches")

    count = db.execute(text(
        "SELECT COUNT(*) FROM trading_macro_regime_snapshots "
        "WHERE as_of_date = :d AND mode = 'shadow'"
    ), {"d": SOAK_AS_OF}).scalar_one()
    _assert(count == 1, "exactly 1 row persisted for soak as_of")


def _check_off_and_authoritative(db) -> None:
    readings = [
        _reading_up(s)
        for s in (SYMBOL_IEF, SYMBOL_SHY, SYMBOL_TLT,
                  SYMBOL_HYG, SYMBOL_LQD, SYMBOL_UUP)
    ]
    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="off",
        readings_override=readings,
        equity_override=_equity_stub(),
    )
    _assert(row is None, "off mode is a no-op")

    raised = False
    try:
        compute_and_persist(
            db,
            as_of_date=SOAK_AS_OF,
            mode_override="authoritative",
            readings_override=readings,
            equity_override=_equity_stub(),
        )
    except RuntimeError:
        raised = True
    _assert(raised, "authoritative mode raises RuntimeError (refused)")


def _check_coverage_gate(db) -> None:
    # Below min_coverage_score=0.5: only 2/6 sampled.
    partial = [
        _reading_up(SYMBOL_HYG),
        _reading_up(SYMBOL_LQD),
        _reading_missing(SYMBOL_IEF),
        _reading_missing(SYMBOL_SHY),
        _reading_missing(SYMBOL_TLT),
        _reading_missing(SYMBOL_UUP),
    ]
    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF_2,
        mode_override="shadow",
        readings_override=partial,
        equity_override=_equity_stub(),
    )
    _assert(row is None, "coverage_below_min skips persistence")

    count = db.execute(text(
        "SELECT COUNT(*) FROM trading_macro_regime_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF_2}).scalar_one()
    _assert(count == 0, "no row persisted when coverage_below_min")


def _check_determinism_and_append_only(db) -> None:
    readings = [
        _reading_up(s)
        for s in (SYMBOL_IEF, SYMBOL_SHY, SYMBOL_TLT,
                  SYMBOL_HYG, SYMBOL_LQD, SYMBOL_UUP)
    ]
    # Second write with the same as_of should append a new row (no upsert).
    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="shadow",
        readings_override=readings,
        equity_override=_equity_stub(),
    )
    _assert(row is not None, "append-only second write succeeds")

    count = db.execute(text(
        "SELECT COUNT(*) FROM trading_macro_regime_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF}).scalar_one()
    _assert(count == 2, "append-only: 2 rows after second write")

    # Both rows share the same deterministic regime_id.
    rows = db.execute(text(
        "SELECT regime_id FROM trading_macro_regime_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF}).fetchall()
    ids = {r[0] for r in rows}
    _assert(len(ids) == 1, "same as_of shares same regime_id")
    _assert(
        compute_regime_id(SOAK_AS_OF) in ids,
        "regime_id matches compute_regime_id(as_of)",
    )


def _check_summary(db) -> None:
    payload = macro_regime_summary(db, lookback_days=14)
    expected_keys = {
        "mode",
        "lookback_days",
        "snapshots_total",
        "by_label",
        "by_rates_regime",
        "by_credit_regime",
        "by_usd_regime",
        "mean_coverage_score",
        "latest_snapshot",
    }
    _assert(
        set(payload.keys()) == expected_keys,
        "macro_regime_summary frozen top-level shape",
    )
    _assert(
        set(payload["by_label"].keys()) == {"risk_on", "cautious", "risk_off"},
        "by_label has all three macro labels",
    )
    _assert(payload["lookback_days"] == 14, "lookback_days echoes arg")


def _check_get_market_regime_additive() -> None:
    # Guard: L.17 must not mutate the existing regime surface shape.
    from app.services.trading.market_data import get_market_regime
    r = get_market_regime()
    expected = {
        "spy_direction",
        "spy_momentum_5d",
        "vix",
        "vix_regime",
        "regime",
        "regime_numeric",
    }
    missing = expected - set(r.keys())
    _assert(
        not missing,
        f"get_market_regime() retains pre-L.17 keys (missing={missing})",
    )


def main() -> int:
    print("[phase_l17_soak] starting")
    db = SessionLocal()
    try:
        _cleanup(db)
        _check_schema_and_settings(db)
        _check_pure_model()
        _check_persist_shadow(db)
        _check_off_and_authoritative(db)
        _check_coverage_gate(db)
        _check_determinism_and_append_only(db)
        _check_summary(db)
        _check_get_market_regime_additive()
        _cleanup(db)
        print("[phase_l17_soak] ALL GREEN")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
