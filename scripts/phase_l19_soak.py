"""Phase L.19 Docker soak - cross-asset signals snapshot (shadow).

Verifies inside the running ``chili`` container that:

  1. Migration 140 applied (``trading_cross_asset_snapshots`` exists).
  2. ``BRAIN_CROSS_ASSET_MODE`` is visible on ``settings`` (and all
     related tuning thresholds).
  3. ``compute_cross_asset`` pure classifier returns the frozen output
     shape for risk_on / risk_off / divergence synthetic readings.
  4. ``compute_and_persist`` writes exactly one row when forced to
     shadow mode with synthetic legs.
  5. ``compute_and_persist`` is a no-op when the current mode is
     ``off`` and hard-refuses ``authoritative``.
  6. Coverage-score gate: compute_and_persist returns ``None`` and
     skips persistence when coverage is below
     ``brain_cross_asset_min_coverage_score``.
  7. Determinism: same ``as_of_date`` yields identical ``snapshot_id``;
     a subsequent sweep with the same date is append-only (no silent
     overwrite).
  8. ``cross_asset_summary`` returns the frozen wire shape.
  9. Additive-only: L.17's ``trading_macro_regime_snapshots`` and
     L.18's ``trading_breadth_relstr_snapshots`` are not mutated by
     any L.19 write (row counts stable around the sweep).
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
from app.services.trading.cross_asset_model import (  # noqa: E402
    ALL_SYMBOLS,
    CROSS_ASSET_DIVERGENCE,
    CROSS_ASSET_NEUTRAL,
    CROSS_ASSET_RISK_OFF,
    CROSS_ASSET_RISK_ON,
    LEAD_RISK_OFF,
    LEAD_RISK_ON,
    SYMBOL_BTC,
    SYMBOL_ETH,
    SYMBOL_HYG,
    SYMBOL_LQD,
    SYMBOL_SPY,
    SYMBOL_TLT,
    SYMBOL_UUP,
    AssetLeg,
    CrossAssetInput,
    compute_cross_asset,
    compute_snapshot_id,
)
from app.services.trading.cross_asset_service import (  # noqa: E402
    compute_and_persist,
    cross_asset_summary,
)

SOAK_AS_OF = date(2026, 4, 17)
SOAK_AS_OF_2 = date(2026, 4, 18)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"[phase_l19_soak] FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[phase_l19_soak] OK  : {msg}")


def _cleanup(db) -> None:
    db.execute(text(
        "DELETE FROM trading_cross_asset_snapshots "
        "WHERE as_of_date IN (:a, :b)"
    ), {"a": SOAK_AS_OF, "b": SOAK_AS_OF_2})
    db.commit()


def _leg(
    sym: str,
    *,
    r5: float | None = 0.0,
    r20: float | None = 0.0,
    missing: bool = False,
    daily: tuple[float, ...] = (),
    close: float = 100.0,
) -> AssetLeg:
    return AssetLeg(
        symbol=sym,
        last_close=(None if missing else close),
        ret_1d=(None if missing else 0.0),
        ret_5d=(None if missing else r5),
        ret_20d=(None if missing else r20),
        missing=missing,
        returns_daily=daily,
    )


def _risk_on_legs() -> dict[str, AssetLeg]:
    return {
        SYMBOL_SPY: _leg(SYMBOL_SPY, r5=0.03, r20=0.06),
        SYMBOL_TLT: _leg(SYMBOL_TLT, r5=-0.02, r20=-0.04),
        SYMBOL_HYG: _leg(SYMBOL_HYG, r5=0.06, r20=0.12),
        SYMBOL_LQD: _leg(SYMBOL_LQD, r5=0.01, r20=0.02),
        SYMBOL_UUP: _leg(SYMBOL_UUP, r5=-0.02, r20=-0.04),
        SYMBOL_BTC: _leg(SYMBOL_BTC, r5=0.06, r20=0.12),
        SYMBOL_ETH: _leg(SYMBOL_ETH, r5=0.07, r20=0.14),
    }


def _risk_off_legs() -> dict[str, AssetLeg]:
    return {
        SYMBOL_SPY: _leg(SYMBOL_SPY, r5=-0.03, r20=-0.06),
        SYMBOL_TLT: _leg(SYMBOL_TLT, r5=0.03, r20=0.06),
        SYMBOL_HYG: _leg(SYMBOL_HYG, r5=-0.05, r20=-0.10),
        SYMBOL_LQD: _leg(SYMBOL_LQD, r5=0.00, r20=0.00),
        SYMBOL_UUP: _leg(SYMBOL_UUP, r5=0.03, r20=0.06),
        SYMBOL_BTC: _leg(SYMBOL_BTC, r5=-0.06, r20=-0.12),
        SYMBOL_ETH: _leg(SYMBOL_ETH, r5=-0.07, r20=-0.14),
    }


def _partial_legs() -> dict[str, AssetLeg]:
    """Only 2/7 present -> coverage=2/7~=0.286 < 0.5 default."""
    return {
        SYMBOL_SPY: _leg(SYMBOL_SPY, r5=0.01, r20=0.02),
        SYMBOL_TLT: _leg(SYMBOL_TLT, r5=-0.01, r20=-0.02),
        SYMBOL_HYG: _leg(SYMBOL_HYG, missing=True),
        SYMBOL_LQD: _leg(SYMBOL_LQD, missing=True),
        SYMBOL_UUP: _leg(SYMBOL_UUP, missing=True),
        SYMBOL_BTC: _leg(SYMBOL_BTC, missing=True),
        SYMBOL_ETH: _leg(SYMBOL_ETH, missing=True),
    }


def _check_schema_and_settings(db) -> None:
    row = db.execute(text(
        "SELECT to_regclass('public.trading_cross_asset_snapshots')"
    )).scalar_one()
    _assert(
        row is not None,
        "trading_cross_asset_snapshots exists",
    )

    row = db.execute(text(
        "SELECT version_id FROM schema_version "
        "WHERE version_id = '140_cross_asset_snapshot'"
    )).fetchone()
    _assert(
        row is not None,
        "migration 140_cross_asset_snapshot recorded",
    )

    for attr in (
        "brain_cross_asset_mode",
        "brain_cross_asset_ops_log_enabled",
        "brain_cross_asset_cron_hour",
        "brain_cross_asset_cron_minute",
        "brain_cross_asset_min_coverage_score",
        "brain_cross_asset_fast_lead_threshold",
        "brain_cross_asset_slow_lead_threshold",
        "brain_cross_asset_vix_percentile_shock",
        "brain_cross_asset_beta_window_days",
        "brain_cross_asset_composite_min_agreement",
        "brain_cross_asset_lookback_days",
    ):
        _assert(hasattr(settings, attr), f"settings.{attr} exists")


def _check_pure_model() -> None:
    legs = _risk_on_legs()
    out_on = compute_cross_asset(CrossAssetInput(
        as_of_date=SOAK_AS_OF,
        equity=legs[SYMBOL_SPY], rates=legs[SYMBOL_TLT],
        credit_hy=legs[SYMBOL_HYG], credit_ig=legs[SYMBOL_LQD],
        usd=legs[SYMBOL_UUP], crypto_btc=legs[SYMBOL_BTC],
        crypto_eth=legs[SYMBOL_ETH],
        vix_level=14.0, vix_percentile=0.25,
        breadth_advance_ratio=0.70, breadth_label="broad_risk_on",
        macro_label="risk_on",
    ))
    _assert(
        out_on.snapshot_id == compute_snapshot_id(SOAK_AS_OF),
        "snapshot_id deterministic",
    )
    _assert(
        out_on.cross_asset_label == CROSS_ASSET_RISK_ON,
        f"cross_asset_label=risk_on_crosscheck for risk-on legs "
        f"(got {out_on.cross_asset_label!r})",
    )
    _assert(out_on.cross_asset_numeric == 1, "cross_asset_numeric=1 on risk_on")
    _assert(
        out_on.bond_equity_label == LEAD_RISK_ON,
        f"bond_equity_label=risk_on (got {out_on.bond_equity_label!r})",
    )
    _assert(
        out_on.usd_crypto_label == LEAD_RISK_ON,
        f"usd_crypto_label=risk_on (got {out_on.usd_crypto_label!r})",
    )
    _assert(
        abs(out_on.coverage_score - 1.0) < 1e-9,
        "coverage_score=1.0 with all 7 legs present",
    )

    legs_off = _risk_off_legs()
    out_off = compute_cross_asset(CrossAssetInput(
        as_of_date=SOAK_AS_OF,
        equity=legs_off[SYMBOL_SPY], rates=legs_off[SYMBOL_TLT],
        credit_hy=legs_off[SYMBOL_HYG], credit_ig=legs_off[SYMBOL_LQD],
        usd=legs_off[SYMBOL_UUP], crypto_btc=legs_off[SYMBOL_BTC],
        crypto_eth=legs_off[SYMBOL_ETH],
        vix_level=28.0, vix_percentile=0.95,
        breadth_advance_ratio=0.40, breadth_label="broad_risk_off",
        macro_label="risk_off",
    ))
    _assert(
        out_off.cross_asset_label == CROSS_ASSET_RISK_OFF,
        f"cross_asset_label=risk_off_crosscheck for risk-off legs "
        f"(got {out_off.cross_asset_label!r})",
    )
    _assert(out_off.cross_asset_numeric == -1, "cross_asset_numeric=-1 on risk_off")
    _assert(
        out_off.bond_equity_label == LEAD_RISK_OFF,
        f"bond_equity_label=risk_off (got {out_off.bond_equity_label!r})",
    )

    # Divergence: mix risk_on + risk_off signals. Need TLT outperforming
    # SPY by more than fast_lead_threshold (0.01) so bond_equity flips
    # to risk_off while credit/usd-crypto stay risk_on.
    mixed = _risk_on_legs()
    mixed[SYMBOL_TLT] = _leg(SYMBOL_TLT, r5=0.05, r20=0.10)   # tlt-spy=0.02 -> risk_off
    out_div = compute_cross_asset(CrossAssetInput(
        as_of_date=SOAK_AS_OF,
        equity=mixed[SYMBOL_SPY], rates=mixed[SYMBOL_TLT],
        credit_hy=mixed[SYMBOL_HYG], credit_ig=mixed[SYMBOL_LQD],
        usd=mixed[SYMBOL_UUP], crypto_btc=mixed[SYMBOL_BTC],
        crypto_eth=mixed[SYMBOL_ETH],
        vix_level=15.0, vix_percentile=0.30,
        breadth_advance_ratio=0.60, breadth_label="broad_risk_on",
        macro_label="risk_on",
    ))
    _assert(
        out_div.cross_asset_label == CROSS_ASSET_DIVERGENCE,
        f"divergence when bond=risk_off + credit/usd-crypto=risk_on "
        f"(got {out_div.cross_asset_label!r})",
    )


def _check_persist_shadow(db) -> None:
    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="shadow",
        legs_override=_risk_on_legs(),
        vix_level_override=14.0,
        vix_percentile_override=0.25,
        breadth_advance_ratio_override=0.70,
        breadth_label_override="broad_risk_on",
        macro_label_override="risk_on",
    )
    _assert(row is not None, "compute_and_persist writes row in shadow")
    assert row is not None
    _assert(row.mode == "shadow", "persisted row mode=shadow")
    _assert(row.as_of_date == SOAK_AS_OF, "persisted row as_of_date matches")
    _assert(
        row.cross_asset_label == CROSS_ASSET_RISK_ON,
        f"persisted row cross_asset_label=risk_on_crosscheck "
        f"(got {row.cross_asset_label!r})",
    )

    count = db.execute(text(
        "SELECT COUNT(*) FROM trading_cross_asset_snapshots "
        "WHERE as_of_date = :d AND mode = 'shadow'"
    ), {"d": SOAK_AS_OF}).scalar_one()
    _assert(count == 1, "exactly 1 row persisted for soak as_of")


def _check_off_and_authoritative(db) -> None:
    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="off",
        legs_override=_risk_on_legs(),
        vix_level_override=14.0,
        vix_percentile_override=0.25,
    )
    _assert(row is None, "off mode is a no-op")

    raised = False
    try:
        compute_and_persist(
            db,
            as_of_date=SOAK_AS_OF,
            mode_override="authoritative",
            legs_override=_risk_on_legs(),
            vix_level_override=14.0,
            vix_percentile_override=0.25,
        )
    except RuntimeError:
        raised = True
    _assert(raised, "authoritative mode raises RuntimeError (refused)")


def _check_coverage_gate(db) -> None:
    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF_2,
        mode_override="shadow",
        legs_override=_partial_legs(),
        vix_level_override=14.0,
        vix_percentile_override=0.25,
        breadth_advance_ratio_override=0.55,
        breadth_label_override="mixed",
        macro_label_override="risk_on",
    )
    _assert(row is None, "coverage_below_min skips persistence")

    count = db.execute(text(
        "SELECT COUNT(*) FROM trading_cross_asset_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF_2}).scalar_one()
    _assert(count == 0, "no row persisted when coverage_below_min")


def _check_determinism_and_append_only(db) -> None:
    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="shadow",
        legs_override=_risk_on_legs(),
        vix_level_override=14.0,
        vix_percentile_override=0.25,
        breadth_advance_ratio_override=0.70,
        breadth_label_override="broad_risk_on",
        macro_label_override="risk_on",
    )
    _assert(row is not None, "append-only second write succeeds")

    count = db.execute(text(
        "SELECT COUNT(*) FROM trading_cross_asset_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF}).scalar_one()
    _assert(count == 2, "append-only: 2 rows after second write")

    rows = db.execute(text(
        "SELECT snapshot_id FROM trading_cross_asset_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF}).fetchall()
    ids = {r[0] for r in rows}
    _assert(len(ids) == 1, "same as_of shares same snapshot_id")
    _assert(
        compute_snapshot_id(SOAK_AS_OF) in ids,
        "snapshot_id matches compute_snapshot_id(as_of)",
    )


def _check_summary(db) -> None:
    payload = cross_asset_summary(db, lookback_days=14)
    expected_keys = {
        "mode",
        "lookback_days",
        "snapshots_total",
        "by_cross_asset_label",
        "by_bond_equity_label",
        "by_credit_equity_label",
        "by_usd_crypto_label",
        "by_vix_breadth_label",
        "mean_coverage_score",
        "latest_snapshot",
    }
    _assert(
        set(payload.keys()) == expected_keys,
        "cross_asset_summary frozen top-level shape",
    )
    _assert(
        set(payload["by_cross_asset_label"].keys()) == {
            "risk_on_crosscheck", "risk_off_crosscheck",
            "divergence", "neutral",
        },
        "by_cross_asset_label has all four composite labels",
    )
    _assert(payload["lookback_days"] == 14, "lookback_days echoes arg")
    for k in (
        "by_bond_equity_label",
        "by_credit_equity_label",
        "by_usd_crypto_label",
        "by_vix_breadth_label",
    ):
        _assert(isinstance(payload[k], dict), f"{k} is a dict")


def _check_l17_l18_additive(db) -> None:
    # Baseline counts before another L.19 shadow write.
    mr_pre = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_macro_regime_snapshots"
    )).scalar_one() or 0)
    br_pre = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_breadth_relstr_snapshots"
    )).scalar_one() or 0)

    compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="shadow",
        legs_override=_risk_on_legs(),
        vix_level_override=14.0,
        vix_percentile_override=0.25,
        breadth_advance_ratio_override=0.70,
        breadth_label_override="broad_risk_on",
        macro_label_override="risk_on",
    )

    mr_post = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_macro_regime_snapshots"
    )).scalar_one() or 0)
    br_post = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_breadth_relstr_snapshots"
    )).scalar_one() or 0)
    _assert(
        mr_post == mr_pre,
        f"L.17 macro snapshots unchanged by L.19 writes "
        f"(pre={mr_pre} post={mr_post})",
    )
    _assert(
        br_post == br_pre,
        f"L.18 breadth_relstr snapshots unchanged by L.19 writes "
        f"(pre={br_pre} post={br_post})",
    )


def main() -> int:
    print("[phase_l19_soak] starting")
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
        _check_l17_l18_additive(db)
        _cleanup(db)
        print("[phase_l19_soak] ALL GREEN")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
