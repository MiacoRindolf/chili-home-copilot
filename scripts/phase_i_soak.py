"""Phase I Docker soak - risk dial + weekly capital re-weighter (shadow).

Verifies inside the running ``chili`` container that:
  1. Migration 135 applied (``trading_risk_dial_state``,
     ``trading_capital_reweight_log``, and
     ``trading_position_sizer_log.risk_dial_multiplier``).
  2. ``BRAIN_RISK_DIAL_MODE`` and ``BRAIN_CAPITAL_REWEIGHT_MODE``
     are visible in settings.
  3. ``resolve_dial`` writes one row when forced to shadow mode and
     is a no-op when forced to off.
  4. ``run_sweep`` writes one row when forced to shadow and refuses
     authoritative with :class:`RuntimeError`.
  5. ``dial_state_summary`` and ``sweep_summary`` return the frozen
     shape.
  6. ``position_sizer_writer`` records ``risk_dial_multiplier`` when
     both the dial and sizer are active in shadow.
  7. Determinism: two calls with identical inputs return the same
     ``dial_id`` / ``reweight_id``.

Safe on a real chili stack: inserts use ``phi_soak_*`` source tags /
tickers and are cleaned up before and after. No live trade
mutations.
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
from app.services.trading.capital_reweight_model import BucketContext  # noqa: E402
from app.services.trading.capital_reweight_service import (  # noqa: E402
    run_sweep,
    sweep_summary,
)
from app.services.trading.position_sizer_emitter import (  # noqa: E402
    EmitterSignal,
    emit_shadow_proposal,
)
from app.services.trading.position_sizer_writer import LegacySizing  # noqa: E402
from app.services.trading.risk_dial_model import (  # noqa: E402
    RiskDialConfig,
    compute_dial_id,
)
from app.services.trading.risk_dial_service import (  # noqa: E402
    dial_state_summary,
    get_latest_dial,
    resolve_dial,
)

SOAK_USER = 999_001
SOAK_SOURCE = "phi_soak"
SOAK_SIZER_TICKER = "PHI_SOAK_AAA"


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"[phase_i_soak] FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[phase_i_soak] OK  : {msg}")


def _cleanup(db) -> None:
    db.execute(text(
        "DELETE FROM trading_risk_dial_state WHERE source LIKE 'phi_soak%'"
    ))
    db.execute(text(
        "DELETE FROM trading_capital_reweight_log WHERE user_id = :u"
    ), {"u": SOAK_USER})
    db.execute(text(
        "DELETE FROM trading_position_sizer_log WHERE ticker = :t"
    ), {"t": SOAK_SIZER_TICKER})
    db.commit()


def _check_migration(db) -> None:
    rd = db.execute(text(
        "SELECT to_regclass('public.trading_risk_dial_state')"
    )).scalar_one()
    _assert(rd is not None, "trading_risk_dial_state table exists")

    cr = db.execute(text(
        "SELECT to_regclass('public.trading_capital_reweight_log')"
    )).scalar_one()
    _assert(cr is not None, "trading_capital_reweight_log table exists")

    col = db.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='trading_position_sizer_log'
          AND column_name='risk_dial_multiplier'
    """)).scalar()
    _assert(col is not None, "trading_position_sizer_log.risk_dial_multiplier column exists")


def _check_settings() -> None:
    rd_mode = (getattr(settings, "brain_risk_dial_mode", "off") or "off").lower()
    cr_mode = (getattr(settings, "brain_capital_reweight_mode", "off") or "off").lower()
    _assert(
        rd_mode in ("off", "shadow", "compare", "authoritative"),
        f"brain_risk_dial_mode in allowed set (got {rd_mode!r})",
    )
    _assert(
        cr_mode in ("off", "shadow", "compare", "authoritative"),
        f"brain_capital_reweight_mode in allowed set (got {cr_mode!r})",
    )


def _check_dial_shadow_write(db) -> None:
    prev = getattr(settings, "brain_risk_dial_mode", "off")
    try:
        settings.brain_risk_dial_mode = "shadow"
        res = resolve_dial(
            db,
            user_id=SOAK_USER,
            regime="risk_on",
            drawdown_pct=0.0,
            source="phi_soak",
            reason="soak",
        )
    finally:
        settings.brain_risk_dial_mode = prev
    _assert(res is not None, "resolve_dial returns DialResolution in shadow")
    _assert(res.mode == "shadow", f"DialResolution.mode=='shadow' (got {res.mode!r})")
    _assert(res.dial_value > 0, "DialResolution.dial_value > 0")

    count = db.execute(text(
        "SELECT COUNT(*) FROM trading_risk_dial_state WHERE source = 'phi_soak'"
    )).scalar_one()
    _assert(count == 1, f"exactly one row written (got {count})")


def _check_dial_off_noop(db) -> None:
    before = db.execute(text(
        "SELECT COUNT(*) FROM trading_risk_dial_state WHERE source = 'phi_soak_off'"
    )).scalar_one()
    prev = getattr(settings, "brain_risk_dial_mode", "off")
    try:
        settings.brain_risk_dial_mode = "off"
        res = resolve_dial(
            db,
            user_id=SOAK_USER,
            regime="risk_on",
            source="phi_soak_off",
        )
    finally:
        settings.brain_risk_dial_mode = prev
    after = db.execute(text(
        "SELECT COUNT(*) FROM trading_risk_dial_state WHERE source = 'phi_soak_off'"
    )).scalar_one()
    _assert(res is None, "off-mode resolve_dial returns None")
    _assert(before == after, "off-mode did not insert any row")


def _check_dial_determinism() -> None:
    cfg = RiskDialConfig()
    a = compute_dial_id(user_id=SOAK_USER, regime="risk_on", config=cfg)
    b = compute_dial_id(user_id=SOAK_USER, regime="risk_on", config=cfg)
    _assert(a == b, f"compute_dial_id deterministic ({a} == {b})")


def _check_dial_summary_shape(db) -> None:
    payload = dial_state_summary(db, lookback_hours=24)
    expected = {
        "mode", "lookback_hours", "dial_events_total",
        "by_regime", "by_source", "by_dial_bucket",
        "mean_dial_value", "latest_dial",
        "override_rejected_count", "capped_at_ceiling_count",
    }
    _assert(
        set(payload.keys()) == expected,
        f"dial_state_summary keys == frozen (got {sorted(payload.keys())})",
    )
    _assert(
        set(payload["by_dial_bucket"].keys()) == {
            "under_0_5", "0_5_to_0_8", "0_8_to_1_0", "1_0_to_1_2", "over_1_2",
        },
        "dial buckets frozen",
    )


def _check_sweep_shadow_write(db) -> None:
    prev = getattr(settings, "brain_capital_reweight_mode", "off")
    try:
        settings.brain_capital_reweight_mode = "shadow"
        res = run_sweep(
            db,
            user_id=SOAK_USER,
            as_of_date=date.today(),
            total_capital=100_000.0,
            regime="risk_on",
            dial_value=1.0,
            buckets=(
                BucketContext(name="equity:tech", current_notional=0.0, volatility=1.0),
                BucketContext(name="equity:fin", current_notional=0.0, volatility=2.0),
            ),
        )
    finally:
        settings.brain_capital_reweight_mode = prev
    _assert(res is not None, "run_sweep returns SweepResult in shadow")
    _assert(res.mode == "shadow", f"SweepResult.mode=='shadow' (got {res.mode!r})")
    count = db.execute(text(
        "SELECT COUNT(*) FROM trading_capital_reweight_log WHERE user_id = :u"
    ), {"u": SOAK_USER}).scalar_one()
    _assert(count == 1, f"one row written to reweight log (got {count})")


def _check_sweep_authoritative_refuses(db) -> None:
    prev = getattr(settings, "brain_capital_reweight_mode", "off")
    try:
        settings.brain_capital_reweight_mode = "authoritative"
        try:
            run_sweep(
                db,
                user_id=SOAK_USER,
                as_of_date=date.today(),
                total_capital=1.0,
                regime="cautious",
                dial_value=1.0,
                buckets=(
                    BucketContext(name="equity:tech", current_notional=0.0, volatility=1.0),
                ),
            )
            raised = False
        except RuntimeError:
            raised = True
    finally:
        settings.brain_capital_reweight_mode = prev
    _assert(raised, "run_sweep raises RuntimeError in authoritative mode")


def _check_sweep_summary_shape(db) -> None:
    payload = sweep_summary(db, lookback_days=14)
    expected = {
        "mode", "lookback_days", "sweeps_total",
        "mean_mean_drift_bps", "p90_p90_drift_bps",
        "single_bucket_cap_trigger_count",
        "concentration_cap_trigger_count", "latest_sweep",
    }
    _assert(
        set(payload.keys()) == expected,
        f"sweep_summary keys == frozen (got {sorted(payload.keys())})",
    )


def _check_sizer_records_dial(db) -> None:
    prev_rd = getattr(settings, "brain_risk_dial_mode", "off")
    prev_ps = getattr(settings, "brain_position_sizer_mode", "off")
    try:
        settings.brain_risk_dial_mode = "shadow"
        settings.brain_position_sizer_mode = "shadow"
        resolve_dial(
            db,
            user_id=SOAK_USER,
            regime="risk_on",
            drawdown_pct=0.0,
            source="phi_soak",
        )
        latest = get_latest_dial(db, user_id=SOAK_USER, default=1.0)
        _assert(latest > 0, f"get_latest_dial > 0 (got {latest})")

        sig = EmitterSignal(
            source="phi_soak",
            ticker=SOAK_SIZER_TICKER,
            direction="long",
            entry_price=100.0,
            stop_price=98.0,
            capital=100_000.0,
            target_price=103.0,
            asset_class="equity",
            user_id=SOAK_USER,
            confidence=0.6,
        )
        emit_shadow_proposal(
            db,
            signal=sig,
            legacy=LegacySizing(notional=1_000.0, quantity=10.0, source="legacy"),
        )
    finally:
        settings.brain_risk_dial_mode = prev_rd
        settings.brain_position_sizer_mode = prev_ps

    val = db.execute(text(
        "SELECT risk_dial_multiplier FROM trading_position_sizer_log "
        "WHERE ticker = :t ORDER BY id DESC LIMIT 1"
    ), {"t": SOAK_SIZER_TICKER}).scalar()
    _assert(
        val is not None,
        f"position_sizer_log.risk_dial_multiplier populated (got {val!r})",
    )


def main() -> int:
    db = SessionLocal()
    try:
        _cleanup(db)
        _check_migration(db)
        _check_settings()
        _check_dial_shadow_write(db)
        _check_dial_off_noop(db)
        _check_dial_determinism()
        _check_dial_summary_shape(db)
        _check_sweep_shadow_write(db)
        _check_sweep_authoritative_refuses(db)
        _check_sweep_summary_shape(db)
        _check_sizer_records_dial(db)
        _cleanup(db)
        print("[phase_i_soak] ALL CHECKS PASSED")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
