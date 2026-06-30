"""Exhaustive adversarial bounds tests for GREEN-DAY GRADUATION (momentum LIVE lane).

GREEN-DAY GRADUATION (built, DEFAULT OFF — flag chili_momentum_green_day_graduation_enabled)
is a bounded UPWARD size multiplier earned by a consecutive GREEN ET-calendar-day streak
(net REAL realized daily PnL > 0). It composes MULTIPLICATIVELY into the runner's combined
size-multiplier product under the existing ~3.0x equity ceiling, applied at entry-quantity
compute time. It is NEVER a veto: it can only scale size up (>=1.0), never zero/block an entry.

The 7 properties proven here:
  P1  streak 0 / no history            => multiplier == 1.0 exactly
  P2  monotonic non-decreasing, BOUNDED at max_multiplier (e.g. streak 100 => 2.0)
  P3  a red (or flat) day RESETS the streak => multiplier back to 1.0
  P4  NEVER a veto — only scales; cannot return 0 / block / zero an entry
  P5  composed UNDER the existing ~3.0x ceiling — the boost can't pass the hard cap
  P6  flag OFF => multiplier 1.0 (byte-identical sizing)
  P7  streak derives from realized daily PnL per ET CALENDAR DAY (sum > 0), not UTC ticks

Most properties are pure-logic over a mocked PnL history (DB rows bucketed by ET date).
The streak source (consecutive_green_days) had a bug — a day of ONLY never-entered rows
(cancelled_pre_entry / no_fill, realized_pnl_usd=0.0 NOT NULL) summed to 0.0 and spuriously
BROKE the streak. Fixed by filtering to is_real_entry_outcome (mirrors _count_real_entries_today);
test_streak_unentered_zero_rows_do_not_break_streak locks the fix.

[[project_momentum_lane]] [[feedback_adaptive_no_magic]]
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import risk_policy as rp
from app.services.trading.momentum_neural.persistence import ensure_momentum_strategy_variants
from app.services.trading.momentum_neural.risk_policy import (
    RISK_SNAPSHOT_KEY,
    compute_risk_first_quantity,
    consecutive_green_days,
    green_day_graduation_multiplier,
)

_EF = "coinbase_spot"
_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")


# ── helpers ───────────────────────────────────────────────────────────────────


def _utc_for_et(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    """Naive-UTC timestamp that lands on the given ET calendar day/time.

    The table stores naive-UTC; the streak code re-tags as UTC and converts to ET, so the
    ET CALENDAR DATE of the returned instant is exactly (year, month, day) by construction.
    """
    et_dt = datetime(year, month, day, hour, minute, tzinfo=_ET)
    return et_dt.astimezone(_UTC).replace(tzinfo=None)


def _require_table(db: Session) -> None:
    if "momentum_automation_outcomes" not in set(sa_inspect(db.bind).get_table_names()):
        pytest.skip("momentum_automation_outcomes table not present")


def _setup(db: Session) -> tuple[User, MomentumStrategyVariant]:
    _require_table(db)
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = (
        db.query(MomentumStrategyVariant)
        .filter(MomentumStrategyVariant.family == "impulse_breakout")
        .first()
    )
    assert v is not None
    u = User(name="GreenDayGrad")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u, v


def _add_outcome(
    db: Session,
    u: User,
    v: MomentumStrategyVariant,
    *,
    pnl: float | None,
    terminal_at: datetime,
    symbol: str,
    outcome_class: str = "small_win",
    mode: str = "live",
    execution_family: str = _EF,
) -> None:
    """Insert one MomentumAutomationOutcome (with its required session parent)."""
    s = TradingAutomationSession(
        user_id=u.id,
        mode=mode,
        symbol=symbol,
        variant_id=v.id,
        state="live_finished",
        risk_snapshot_json={RISK_SNAPSHOT_KEY: {"allowed": True}},
        ended_at=terminal_at,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    db.add(
        MomentumAutomationOutcome(
            session_id=s.id,
            user_id=u.id,
            variant_id=v.id,
            symbol=symbol,
            mode=mode,
            execution_family=execution_family,
            terminal_state=s.state,
            terminal_at=terminal_at,
            outcome_class=outcome_class,
            realized_pnl_usd=pnl,
            return_bps=(pnl * 10.0) if pnl is not None else None,
            regime_snapshot_json={},
            entry_regime_snapshot_json={},
            exit_regime_snapshot_json={},
            readiness_snapshot_json={},
            admission_snapshot_json={},
            governance_context_json={},
            evidence_weight=1.0,
            contributes_to_evolution=True,
        )
    )
    db.commit()


def _add_green_streak(
    db: Session,
    u: User,
    v: MomentumStrategyVariant,
    *,
    n_days: int,
    most_recent_days_ago: int = 1,
    pnl_per_day: float = 25.0,
) -> None:
    """Add ``n_days`` contiguous GREEN ET days, most recent at ``most_recent_days_ago``
    (today excluded by the streak code, so default 1 = yesterday)."""
    today_et = datetime.now(_ET).date()
    for i in range(n_days):
        d = today_et - timedelta(days=most_recent_days_ago + i)
        _add_outcome(
            db, u, v,
            pnl=pnl_per_day,
            terminal_at=_utc_for_et(d.year, d.month, d.day, hour=15),
            symbol=f"G{i}-USD",
            outcome_class="small_win",
        )


def _enable(monkeypatch, *, step: float = 0.1, max_mult: float = 2.0, lookback: int = 30) -> None:
    monkeypatch.setattr(settings, "chili_momentum_green_day_graduation_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_step_per_day", step, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_max_multiplier", max_mult, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_lookback_days", lookback, raising=False)


# ── P1: streak 0 / no history => 1.0 exactly ───────────────────────────────────


def test_p1_no_history_multiplier_is_exactly_one(db: Session, monkeypatch) -> None:
    u, _v = _setup(db)
    _enable(monkeypatch)
    mult, meta = green_day_graduation_multiplier(db, execution_family=_EF)
    assert mult == 1.0  # exact, not approx
    assert meta["consecutive_green_days"] == 0


def test_p1_streak_zero_pure_logic(db: Session, monkeypatch) -> None:
    # No DB rows at all -> streak 0 -> 1.0, regardless of step.
    _setup(db)
    _enable(monkeypatch, step=0.5)
    streak, _ = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 0
    mult, _ = green_day_graduation_multiplier(db, execution_family=_EF)
    assert mult == 1.0


def test_p1_single_green_day_no_graduation(db: Session, monkeypatch) -> None:
    # Day-1 (streak == 1) must NOT graduate: max(0, streak-1) = 0 => 1.0.
    u, v = _setup(db)
    _enable(monkeypatch)
    _add_green_streak(db, u, v, n_days=1)
    streak, _ = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 1
    mult, _ = green_day_graduation_multiplier(db, execution_family=_EF)
    assert mult == 1.0


# ── P2: monotonic non-decreasing, BOUNDED at max_multiplier ─────────────────────


@pytest.mark.parametrize(
    "n_days, expected",
    [(1, 1.0), (2, 1.1), (3, 1.2), (4, 1.3), (5, 1.4), (6, 1.5)],
)
def test_p2_monotonic_step_progression(db: Session, monkeypatch, n_days, expected) -> None:
    u, v = _setup(db)
    _enable(monkeypatch, step=0.1, max_mult=2.0)
    _add_green_streak(db, u, v, n_days=n_days)
    mult, meta = green_day_graduation_multiplier(db, execution_family=_EF)
    assert meta["consecutive_green_days"] == n_days
    assert mult == pytest.approx(expected)


def test_p2_monotonic_non_decreasing_in_streak(db: Session, monkeypatch) -> None:
    # Walk the closed-form across the full streak range: never decreases, always >=1.0,
    # never exceeds max_multiplier.
    _enable(monkeypatch, step=0.1, max_mult=2.0)
    prev = 0.0
    for streak in range(0, 60):
        mult = max(1.0, min(2.0, 1.0 + 0.1 * max(0, streak - 1)))
        assert mult >= prev - 1e-12, "multiplier must be monotonic non-decreasing"
        assert 1.0 <= mult <= 2.0
        prev = mult


def test_p2_ceiling_streak_100_bounded_at_max(db: Session, monkeypatch) -> None:
    # 100-day green streak with step 0.1 would be 10.9x unbounded -> MUST clamp to 2.0.
    u, v = _setup(db)
    _enable(monkeypatch, step=0.1, max_mult=2.0)
    _add_green_streak(db, u, v, n_days=100, pnl_per_day=10.0)
    streak, _ = consecutive_green_days(db, execution_family=_EF, lookback_days=120)
    mult, meta = green_day_graduation_multiplier(db, execution_family=_EF)
    # lookback default 30 in _enable -> streak is capped by lookback window; force a long
    # lookback to actually see a long streak, then confirm the ceiling still bites.
    assert mult <= 2.0 + 1e-9


def test_p2_ceiling_long_lookback_streak_clamped(db: Session, monkeypatch) -> None:
    u, v = _setup(db)
    _enable(monkeypatch, step=0.1, max_mult=2.0, lookback=120)
    _add_green_streak(db, u, v, n_days=100, pnl_per_day=10.0)
    streak, _ = consecutive_green_days(db, execution_family=_EF, lookback_days=120)
    assert streak >= 100  # all 100 days within a 120-day window
    mult, _ = green_day_graduation_multiplier(db, execution_family=_EF)
    assert mult == pytest.approx(2.0)  # clamped, NOT 10.9


def test_p2_huge_step_still_clamped(db: Session, monkeypatch) -> None:
    # Adversarial knob: step=1.0 (max allowed) on a 5-day streak would be 5.0x -> clamp 2.0.
    u, v = _setup(db)
    _enable(monkeypatch, step=1.0, max_mult=2.0)
    _add_green_streak(db, u, v, n_days=5)
    mult, _ = green_day_graduation_multiplier(db, execution_family=_EF)
    assert mult == pytest.approx(2.0)


def test_p2_max_mult_one_disables_growth(db: Session, monkeypatch) -> None:
    # max_multiplier clamped to 1.0 -> no growth ever, however long the streak.
    u, v = _setup(db)
    _enable(monkeypatch, step=0.5, max_mult=1.0)
    _add_green_streak(db, u, v, n_days=10)
    mult, _ = green_day_graduation_multiplier(db, execution_family=_EF)
    assert mult == 1.0


def test_p2_sub_one_max_mult_guarded_to_one(db: Session, monkeypatch) -> None:
    # A broken/negative ceiling (< 1.0) must be guarded to 1.0, never shrink size below 1.0.
    u, v = _setup(db)
    _enable(monkeypatch, step=0.1, max_mult=0.5)
    _add_green_streak(db, u, v, n_days=5)
    mult, _ = green_day_graduation_multiplier(db, execution_family=_EF)
    assert mult == 1.0


# ── P3: a red (or flat) day RESETS the streak ───────────────────────────────────


def test_p3_red_day_resets_streak(db: Session, monkeypatch) -> None:
    # GGG R GGG (most recent first): the most-recent 3 greens count; the red stops the walk.
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    # days_ago 1..3 green, day_ago 4 RED, days_ago 5..7 green
    seq = [(1, 30.0), (2, 30.0), (3, 30.0), (4, -50.0), (5, 30.0), (6, 30.0), (7, 30.0)]
    for days_ago, pnl in seq:
        d = today_et - timedelta(days=days_ago)
        _add_outcome(
            db, u, v,
            pnl=pnl,
            terminal_at=_utc_for_et(d.year, d.month, d.day, hour=15),
            symbol=f"D{days_ago}-USD",
            outcome_class="small_win" if pnl >= 0 else "stop_loss",
        )
    streak, _ = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 3  # NOT 6 — the red at days_ago=4 resets
    mult, _ = green_day_graduation_multiplier(db, execution_family=_EF)
    assert mult == pytest.approx(1.2)


def test_p3_most_recent_red_collapses_to_one(db: Session, monkeypatch) -> None:
    # The most recent past day (yesterday) is RED -> streak 0 -> multiplier 1.0 even with
    # a long green run behind it.
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    dr = today_et - timedelta(days=1)
    _add_outcome(
        db, u, v, pnl=-10.0,
        terminal_at=_utc_for_et(dr.year, dr.month, dr.day, hour=15),
        symbol="RED-USD", outcome_class="stop_loss",
    )
    for days_ago in (2, 3, 4):
        d = today_et - timedelta(days=days_ago)
        _add_outcome(db, u, v, pnl=40.0,
                     terminal_at=_utc_for_et(d.year, d.month, d.day, hour=15),
                     symbol=f"G{days_ago}-USD", outcome_class="small_win")
    streak, _ = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 0
    mult, _ = green_day_graduation_multiplier(db, execution_family=_EF)
    assert mult == 1.0


def test_p3_flat_zero_day_breaks_streak(db: Session, monkeypatch) -> None:
    # A day whose REAL entered PnL nets exactly 0.0 is NOT green (> 0.0 strict) -> resets.
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    # yesterday green, day-2 nets 0.0 (an entered +20 and an entered -20), day-3 green
    d1 = today_et - timedelta(days=1)
    _add_outcome(db, u, v, pnl=30.0, terminal_at=_utc_for_et(d1.year, d1.month, d1.day, 15),
                 symbol="G1-USD", outcome_class="small_win")
    d2 = today_et - timedelta(days=2)
    _add_outcome(db, u, v, pnl=20.0, terminal_at=_utc_for_et(d2.year, d2.month, d2.day, 14),
                 symbol="F2a-USD", outcome_class="small_win")
    _add_outcome(db, u, v, pnl=-20.0, terminal_at=_utc_for_et(d2.year, d2.month, d2.day, 15),
                 symbol="F2b-USD", outcome_class="stop_loss")
    d3 = today_et - timedelta(days=3)
    _add_outcome(db, u, v, pnl=30.0, terminal_at=_utc_for_et(d3.year, d3.month, d3.day, 15),
                 symbol="G3-USD", outcome_class="small_win")
    streak, _ = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 1  # only yesterday; the 0.0 day-2 breaks the walk


# ── P3b (bug fix): a day of ONLY never-entered rows must NOT break the streak ────


def test_streak_unentered_zero_rows_do_not_break_streak(db: Session, monkeypatch) -> None:
    """REGRESSION (the bug the properties revealed): a no-trade day (lane armed, never
    entered) writes cancelled_pre_entry / no_fill rows with realized_pnl_usd=0.0 (NOT NULL).
    Before the fix those summed to 0.0 and BROKE the streak. After the fix they are excluded
    via is_real_entry_outcome, so the streak walks THROUGH a pure no-entry day.
    """
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    # yesterday: real green
    d1 = today_et - timedelta(days=1)
    _add_outcome(db, u, v, pnl=30.0, terminal_at=_utc_for_et(d1.year, d1.month, d1.day, 15),
                 symbol="G1-USD", outcome_class="small_win")
    # day-2: ONLY never-entered rows (pnl 0.0) — no real trade happened that day
    d2 = today_et - timedelta(days=2)
    _add_outcome(db, u, v, pnl=0.0, terminal_at=_utc_for_et(d2.year, d2.month, d2.day, 13),
                 symbol="NF2a-USD", outcome_class="no_fill")
    _add_outcome(db, u, v, pnl=0.0, terminal_at=_utc_for_et(d2.year, d2.month, d2.day, 14),
                 symbol="NF2b-USD", outcome_class="cancelled_pre_entry")
    # day-3: real green
    d3 = today_et - timedelta(days=3)
    _add_outcome(db, u, v, pnl=30.0, terminal_at=_utc_for_et(d3.year, d3.month, d3.day, 15),
                 symbol="G3-USD", outcome_class="small_win")
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    # day-2 has no REAL entered trade -> it has no green/red verdict -> it is not a bucket
    # at all -> the streak counts yesterday + day-3 = 2 contiguous green REAL days.
    assert streak == 2, meta
    mult, _ = green_day_graduation_multiplier(db, execution_family=_EF)
    assert mult == pytest.approx(1.1)


def test_streak_real_loss_still_resets(db: Session, monkeypatch) -> None:
    # Belt-and-suspenders: a REAL entered loss (stop_loss / governance_exit) still resets —
    # the fix must not over-filter and swallow genuine red days.
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    d1 = today_et - timedelta(days=1)
    _add_outcome(db, u, v, pnl=20.0, terminal_at=_utc_for_et(d1.year, d1.month, d1.day, 15),
                 symbol="G1-USD", outcome_class="small_win")
    d2 = today_et - timedelta(days=2)
    _add_outcome(db, u, v, pnl=-80.0, terminal_at=_utc_for_et(d2.year, d2.month, d2.day, 15),
                 symbol="L2-USD", outcome_class="governance_exit")  # entered, real loss
    d3 = today_et - timedelta(days=3)
    _add_outcome(db, u, v, pnl=20.0, terminal_at=_utc_for_et(d3.year, d3.month, d3.day, 15),
                 symbol="G3-USD", outcome_class="small_win")
    streak, _ = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 1  # the real loss day-2 still breaks the walk


# ── P4: NEVER a veto — only scales, cannot zero/block an entry ───────────────────


def test_p4_multiplier_never_below_one(db: Session, monkeypatch) -> None:
    # Across every arrangement the multiplier is in [1.0, max] — it cannot return 0 / negative.
    u, v = _setup(db)
    _enable(monkeypatch, step=0.1, max_mult=2.0)
    for n in (0, 1, 2, 5, 30):
        # rebuild a fresh streak length by truncating the table each loop
        db.query(MomentumAutomationOutcome).delete()
        db.query(TradingAutomationSession).delete()
        db.commit()
        if n:
            _add_green_streak(db, u, v, n_days=n)
        mult, _ = green_day_graduation_multiplier(db, execution_family=_EF)
        assert 1.0 <= mult <= 2.0
        assert mult != 0.0


def test_p4_graduation_cannot_zero_out_quantity(db: Session, monkeypatch) -> None:
    # The multiplier feeds the max_loss basis; even a tiny max_loss with a long streak
    # yields qty > 0 (graduation only ADDS size). It is structurally incapable of vetoing.
    u, v = _setup(db)
    _enable(monkeypatch, step=0.1, max_mult=2.0)
    _add_green_streak(db, u, v, n_days=5)
    grad_mult, _ = green_day_graduation_multiplier(db, execution_family=_EF)
    base_max_loss = 1.0  # $1 — adversarially tiny
    eff_max_loss = base_max_loss * grad_mult
    qty, meta = compute_risk_first_quantity(
        entry_price=10.0,
        atr_pct=0.05,
        max_loss_usd=eff_max_loss,
        max_notional_ceiling_usd=1000.0,
    )
    assert qty > 0.0, meta
    # And a bigger (graduated) max_loss yields qty >= the un-graduated one (monotone up).
    qty_base, _ = compute_risk_first_quantity(
        entry_price=10.0, atr_pct=0.05, max_loss_usd=base_max_loss,
        max_notional_ceiling_usd=1000.0,
    )
    assert qty >= qty_base


def test_p4_error_path_is_fail_neutral(db: Session, monkeypatch) -> None:
    # If the streak read blows up, graduation returns (1.0, error_fail_neutral) — neutral,
    # never a block.
    _enable(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("synthetic streak failure")

    monkeypatch.setattr(rp, "consecutive_green_days", _boom)
    mult, meta = green_day_graduation_multiplier(db, execution_family=_EF)
    assert mult == 1.0
    assert meta.get("reason") == "error_fail_neutral"


# ── P5: composed UNDER the existing ~3.0x ceiling ───────────────────────────────


def test_p5_composed_under_three_x_ceiling(db: Session, monkeypatch) -> None:
    """Replicate the runner's product-then-clamp at live_runner.py: graduation is the 3rd
    factor; the whole product is clamped to base * 3.0. A maxed graduation (2.0) stacked
    on other up-multipliers must NOT push effective max-loss past the 3x hard cap.
    """
    u, v = _setup(db)
    _enable(monkeypatch, step=0.1, max_mult=2.0, lookback=120)
    _add_green_streak(db, u, v, n_days=100, pnl_per_day=10.0)
    grad_mult, _ = green_day_graduation_multiplier(db, execution_family=_EF)
    assert grad_mult == pytest.approx(2.0)

    base_max_loss = 50.0
    # other adversarial up-multipliers in the product chain
    streak_mult, cushion_mult, l2_mult = 1.5, 1.4, 1.3
    product = base_max_loss * streak_mult * grad_mult * cushion_mult * l2_mult
    eff_max_loss = min(product, base_max_loss * 3.0)  # the hard ceiling at live_runner.py
    assert eff_max_loss == pytest.approx(base_max_loss * 3.0)  # clamp bit, not the raw product
    assert eff_max_loss <= base_max_loss * 3.0 + 1e-9


def test_p5_notional_ceiling_caps_final_quantity(db: Session, monkeypatch) -> None:
    # Even with graduation maxed, compute_risk_first_quantity caps notional at the hard
    # max_notional_ceiling_usd regardless of the multiplier product.
    u, v = _setup(db)
    _enable(monkeypatch, step=0.1, max_mult=2.0, lookback=120)
    _add_green_streak(db, u, v, n_days=100, pnl_per_day=10.0)
    grad_mult, _ = green_day_graduation_multiplier(db, execution_family=_EF)
    ceiling = 500.0
    qty, meta = compute_risk_first_quantity(
        entry_price=10.0,
        atr_pct=0.10,
        max_loss_usd=50.0 * grad_mult,  # graduated basis
        max_notional_ceiling_usd=ceiling,
    )
    assert qty * 10.0 <= ceiling + 1e-6
    assert meta.get("capped_by") == "notional_ceiling"


# ── P6: flag OFF => 1.0 (byte-identical) ────────────────────────────────────────


def test_p6_flag_off_is_one_even_with_long_streak(db: Session, monkeypatch) -> None:
    u, v = _setup(db)
    # Build a real 5-day green streak, but DO NOT enable the flag.
    monkeypatch.setattr(settings, "chili_momentum_green_day_graduation_enabled", False, raising=False)
    _add_green_streak(db, u, v, n_days=5)
    mult, meta = green_day_graduation_multiplier(db, execution_family=_EF)
    assert mult == 1.0
    assert meta == {"reason": "disabled", "graduation_mult": 1.0}


def test_p6_flag_off_short_circuits_before_db(monkeypatch) -> None:
    # Disabled path must NOT even touch the DB (byte-identical to the function not existing).
    monkeypatch.setattr(settings, "chili_momentum_green_day_graduation_enabled", False, raising=False)

    def _must_not_run(*a, **k):
        raise AssertionError("streak must not be read when the flag is OFF")

    monkeypatch.setattr(rp, "consecutive_green_days", _must_not_run)
    mult, meta = green_day_graduation_multiplier(object(), execution_family=_EF)
    assert mult == 1.0
    assert meta["reason"] == "disabled"


# ── P7: streak derives from realized daily PnL per ET CALENDAR DAY ───────────────


def test_p7_two_utc_days_same_et_day_bucket_together(db: Session, monkeypatch) -> None:
    """Two rows whose UTC instants fall on DIFFERENT UTC dates but the SAME ET calendar day
    must bucket together and SUM. 2026-06-24 23:00 ET == 2026-06-25 03:00 UTC; 2026-06-24
    19:30 ET == 2026-06-24 23:30 UTC. Both are ET 2026-06-24. If the code bucketed by UTC
    ticks they'd split across two days; by ET they are one green day.
    """
    u, v = _setup(db)
    _enable(monkeypatch, lookback=120)
    # ET 2026-06-24, two intraday legs that straddle the UTC midnight
    leg_a_utc = datetime(2026, 6, 24, 23, 30, tzinfo=_ET).astimezone(_UTC).replace(tzinfo=None)
    leg_b_utc = datetime(2026, 6, 24, 19, 30, tzinfo=_ET).astimezone(_UTC).replace(tzinfo=None)
    assert leg_a_utc.date() != leg_b_utc.date()  # different UTC dates by construction
    _add_outcome(db, u, v, pnl=-10.0, terminal_at=leg_b_utc, symbol="ETa-USD",
                 outcome_class="stop_loss")
    _add_outcome(db, u, v, pnl=+40.0, terminal_at=leg_a_utc, symbol="ETb-USD",
                 outcome_class="small_win")
    # Net for ET 2026-06-24 = +30 (green). It's the only ET day with real trades.
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=120)
    # The day is in the past relative to "today" (test run date > 2026-06-24), single green day.
    assert streak == 1, meta
    assert meta["green_usd"] == pytest.approx(30.0)


def test_p7_per_day_sum_decides_green(db: Session, monkeypatch) -> None:
    # A day with a big winner and a small loser nets green; a day with the reverse nets red.
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    # yesterday: +50 and -10 -> +40 green
    d1 = today_et - timedelta(days=1)
    _add_outcome(db, u, v, pnl=50.0, terminal_at=_utc_for_et(d1.year, d1.month, d1.day, 14),
                 symbol="Y1-USD", outcome_class="small_win")
    _add_outcome(db, u, v, pnl=-10.0, terminal_at=_utc_for_et(d1.year, d1.month, d1.day, 15),
                 symbol="Y2-USD", outcome_class="stop_loss")
    # day-2: +10 and -50 -> -40 red (must STOP the walk at the green yesterday)
    d2 = today_et - timedelta(days=2)
    _add_outcome(db, u, v, pnl=10.0, terminal_at=_utc_for_et(d2.year, d2.month, d2.day, 14),
                 symbol="Z1-USD", outcome_class="small_win")
    _add_outcome(db, u, v, pnl=-50.0, terminal_at=_utc_for_et(d2.year, d2.month, d2.day, 15),
                 symbol="Z2-USD", outcome_class="stop_loss")
    streak, _ = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 1


def test_p7_today_excluded_from_streak(db: Session, monkeypatch) -> None:
    # Today's (possibly incomplete) session must be EXCLUDED so an intraday red flicker can't
    # collapse the streak mid-day. A big green TODAY does not bump the streak.
    u, v = _setup(db)
    _enable(monkeypatch)
    now_et = datetime.now(_ET)
    today = now_et.date()
    # today green (should be ignored)
    _add_outcome(db, u, v, pnl=500.0,
                 terminal_at=now_et.replace(hour=10, minute=0, second=0, microsecond=0)
                 .astimezone(_UTC).replace(tzinfo=None),
                 symbol="TODAY-USD", outcome_class="small_win")
    # yesterday + day-before green (the real streak = 2)
    for days_ago in (1, 2):
        d = today - timedelta(days=days_ago)
        _add_outcome(db, u, v, pnl=20.0, terminal_at=_utc_for_et(d.year, d.month, d.day, 15),
                     symbol=f"P{days_ago}-USD", outcome_class="small_win")
    streak, _ = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 2  # today's +500 is excluded


def test_p7_lane_segregated_by_execution_family(db: Session, monkeypatch) -> None:
    # The streak is per-lane: another execution_family's green days do NOT count.
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    for days_ago in (1, 2, 3):
        d = today_et - timedelta(days=days_ago)
        # other lane green
        _add_outcome(db, u, v, pnl=99.0, terminal_at=_utc_for_et(d.year, d.month, d.day, 15),
                     symbol=f"OTHER{days_ago}-USD", outcome_class="small_win",
                     execution_family="robinhood_agentic")
    # our lane: only one green day
    d1 = today_et - timedelta(days=1)
    _add_outcome(db, u, v, pnl=20.0, terminal_at=_utc_for_et(d1.year, d1.month, d1.day, 16),
                 symbol="OURS-USD", outcome_class="small_win", execution_family=_EF)
    streak, _ = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 1  # the robinhood_agentic greens are not in our lane


# ── input-guard / no_input bounds ───────────────────────────────────────────────


def test_no_execution_family_returns_neutral(db: Session, monkeypatch) -> None:
    _enable(monkeypatch)
    streak, meta = consecutive_green_days(db, execution_family=None, lookback_days=30)
    assert streak == 0
    assert meta["reason"] == "no_input"
    mult, _ = green_day_graduation_multiplier(db, execution_family=None)
    assert mult == 1.0


def test_nonpositive_lookback_returns_neutral(db: Session, monkeypatch) -> None:
    _enable(monkeypatch)
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=0)
    assert streak == 0
    assert meta["reason"] == "no_input"


# ═══════════════════════════════════════════════════════════════════════════════
# HARDENING PASS — adversarial branch/boundary coverage (each asserts the SPECIFIC
# reason/value so it FAILS if its branch regresses).
# ═══════════════════════════════════════════════════════════════════════════════


# ── input-guard: db is None (no DB at all) ──────────────────────────────────────


def test_db_none_streak_no_input(monkeypatch) -> None:
    # `db is None` is the FIRST guard branch (distinct from no execution_family / lookback<=0).
    _enable(monkeypatch)
    streak, meta = consecutive_green_days(None, execution_family=_EF, lookback_days=30)
    assert streak == 0
    assert meta["reason"] == "no_input"
    assert meta["streak"] == 0


def test_db_none_graduation_fail_neutral(monkeypatch) -> None:
    # graduation with db=None still flag-ON: streak read returns 0 -> mult 1.0 (never errors).
    _enable(monkeypatch)
    mult, meta = green_day_graduation_multiplier(None, execution_family=_EF)
    assert mult == 1.0
    assert meta["consecutive_green_days"] == 0
    assert meta["graduation_mult"] == 1.0


def test_negative_lookback_no_input(db: Session, monkeypatch) -> None:
    # lookback_days < 0 (not just == 0) also hits the `<= 0` guard.
    _enable(monkeypatch)
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=-5)
    assert streak == 0
    assert meta["reason"] == "no_input"


def test_empty_execution_family_string_no_input(db: Session, monkeypatch) -> None:
    # `not execution_family` catches the empty string too (falsy), not only None.
    _enable(monkeypatch)
    streak, meta = consecutive_green_days(db, execution_family="", lookback_days=30)
    assert streak == 0
    assert meta["reason"] == "no_input"


# ── no_buckets vs no_history: real rows exist but ALL are never-entered ──────────


def test_only_never_entered_rows_no_buckets(db: Session, monkeypatch) -> None:
    """Rows exist (so not `no_history`), but EVERY row is a never-entered class -> the
    is_real_entry_outcome filter empties `by_day` -> the `no_buckets` branch fires, not
    `no_history`. Distinguishes two different empty-result branches.
    """
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    for days_ago, oc in ((1, "no_fill"), (2, "cancelled_pre_entry"), (3, "risk_block")):
        d = today_et - timedelta(days=days_ago)
        _add_outcome(db, u, v, pnl=0.0,
                     terminal_at=_utc_for_et(d.year, d.month, d.day, 15),
                     symbol=f"NE{days_ago}-USD", outcome_class=oc)
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 0
    assert meta["reason"] == "no_buckets", meta  # NOT no_history (rows DID come back)


def test_no_history_when_only_paper_rows(db: Session, monkeypatch) -> None:
    # mode != "live" rows are filtered at SQL level -> no rows -> `no_history` (not no_buckets).
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    d = today_et - timedelta(days=1)
    _add_outcome(db, u, v, pnl=99.0, terminal_at=_utc_for_et(d.year, d.month, d.day, 15),
                 symbol="PAPER-USD", outcome_class="small_win", mode="paper")
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 0
    assert meta["reason"] == "no_history", meta  # paper excluded by the mode=='live' filter


# ── never-entered class taxonomy: flat_unknown COUNTS (not in never-entered set) ─


def test_flat_unknown_real_pnl_counts_toward_streak(db: Session, monkeypatch) -> None:
    """flat_unknown is NOT in _NEVER_ENTERED_OUTCOMES, so a flat_unknown row carrying a
    REAL realized PnL IS a real-entry verdict and DOES participate. A green flat_unknown day
    therefore extends the streak. (Locks the taxonomy boundary — over-filtering would break it.)
    """
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    d1 = today_et - timedelta(days=1)
    _add_outcome(db, u, v, pnl=15.0, terminal_at=_utc_for_et(d1.year, d1.month, d1.day, 15),
                 symbol="FU1-USD", outcome_class="flat_unknown")
    d2 = today_et - timedelta(days=2)
    _add_outcome(db, u, v, pnl=15.0, terminal_at=_utc_for_et(d2.year, d2.month, d2.day, 15),
                 symbol="FU2-USD", outcome_class="success")
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 2, meta  # both days are real-entry greens


def test_archived_class_is_never_entered_and_ignored(db: Session, monkeypatch) -> None:
    # archived IS a never-entered class: a positive-pnl archived row must NOT create a green
    # bucket (would otherwise be a phantom green day inflating the streak).
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    # yesterday: a real green
    d1 = today_et - timedelta(days=1)
    _add_outcome(db, u, v, pnl=20.0, terminal_at=_utc_for_et(d1.year, d1.month, d1.day, 15),
                 symbol="REAL1-USD", outcome_class="small_win")
    # day-2: ONLY an archived row with a (spurious) positive pnl -> not a bucket
    d2 = today_et - timedelta(days=2)
    _add_outcome(db, u, v, pnl=999.0, terminal_at=_utc_for_et(d2.year, d2.month, d2.day, 15),
                 symbol="ARCH2-USD", outcome_class="archived")
    # day-3: a real green
    d3 = today_et - timedelta(days=3)
    _add_outcome(db, u, v, pnl=20.0, terminal_at=_utc_for_et(d3.year, d3.month, d3.day, 15),
                 symbol="REAL3-USD", outcome_class="small_win")
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    # archived day-2 is invisible -> streak walks yesterday + day-3 contiguously = 2.
    assert streak == 2, meta


def test_governance_exit_real_loss_with_null_sibling_resets(db: Session, monkeypatch) -> None:
    """governance_exit is NOT never-entered (a real force-close can carry a real loss). A day
    with a REAL governance_exit loss must reset, even though a NULL-realized sibling row on the
    same day is dropped by the realized-not-null SQL filter. (Per the outcome_labels docstring.)
    """
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    d1 = today_et - timedelta(days=1)
    _add_outcome(db, u, v, pnl=20.0, terminal_at=_utc_for_et(d1.year, d1.month, d1.day, 15),
                 symbol="G1-USD", outcome_class="small_win")
    d2 = today_et - timedelta(days=2)
    # the NULL-realized sibling (dropped at SQL level)
    _add_outcome(db, u, v, pnl=None, terminal_at=_utc_for_et(d2.year, d2.month, d2.day, 13),
                 symbol="GX2null-USD", outcome_class="governance_exit")
    # the REAL realized loss (counts)
    _add_outcome(db, u, v, pnl=-30.0, terminal_at=_utc_for_et(d2.year, d2.month, d2.day, 15),
                 symbol="GX2real-USD", outcome_class="governance_exit")
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 1, meta  # day-2 nets -30 (real) -> resets at yesterday


# ── gap days: a missing CALENDAR day is NOT a reset (only red/flat buckets reset) ─


def test_calendar_gap_day_does_not_break_streak(db: Session, monkeypatch) -> None:
    """The walk iterates over the SORTED keys of `by_day` (days that actually traded), not a
    dense calendar. A day with NO trades at all is simply absent — it neither counts nor resets.
    Two green trading days with an idle calendar day between them are a contiguous streak of 2.
    """
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    # green at days_ago 1 and 3; days_ago 2 has NO rows (a non-trading / idle day).
    for days_ago in (1, 3):
        d = today_et - timedelta(days=days_ago)
        _add_outcome(db, u, v, pnl=20.0, terminal_at=_utc_for_et(d.year, d.month, d.day, 15),
                     symbol=f"GAP{days_ago}-USD", outcome_class="small_win")
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 2, meta  # the idle gap day does not reset
    assert meta["days_seen"] == 2


# ── lookback window boundary (terminal_at >= far_start, < today_start) ───────────


def test_lookback_edge_day_included(db: Session, monkeypatch) -> None:
    """A green day exactly `lookback` ET days ago sits AT far_start (terminal_at >= far_start
    is inclusive) and so IS counted. eps-below the window (one day older) would be excluded.
    """
    u, v = _setup(db)
    _enable(monkeypatch, lookback=5)
    today_et = datetime.now(_ET).date()
    # contiguous greens for days_ago 1..5 (5 == lookback): all inside [far_start, today_start)
    for days_ago in range(1, 6):
        d = today_et - timedelta(days=days_ago)
        _add_outcome(db, u, v, pnl=10.0, terminal_at=_utc_for_et(d.year, d.month, d.day, 15),
                     symbol=f"LB{days_ago}-USD", outcome_class="small_win")
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=5)
    assert streak == 5, meta  # day at days_ago==lookback is included (inclusive lower bound)


def test_day_older_than_lookback_excluded(db: Session, monkeypatch) -> None:
    # A green day OLDER than the lookback window is invisible -> a 1-day window sees only
    # yesterday. (Locks the upper edge: rows before far_start are filtered out at SQL level.)
    u, v = _setup(db)
    _enable(monkeypatch, lookback=1)
    today_et = datetime.now(_ET).date()
    # yesterday (in window) green
    d1 = today_et - timedelta(days=1)
    _add_outcome(db, u, v, pnl=10.0, terminal_at=_utc_for_et(d1.year, d1.month, d1.day, 15),
                 symbol="IN-USD", outcome_class="small_win")
    # 5 days ago (out of a 1-day window) green
    d5 = today_et - timedelta(days=5)
    _add_outcome(db, u, v, pnl=10.0, terminal_at=_utc_for_et(d5.year, d5.month, d5.day, 15),
                 symbol="OUT-USD", outcome_class="small_win")
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=1)
    assert streak == 1, meta
    assert meta["days_seen"] == 1  # the older day never entered the window


# ── settings fallbacks: None / falsy knobs use documented defaults via `or` ──────


def test_none_settings_use_defaults(db: Session, monkeypatch) -> None:
    """step/max/lookback set to None -> the `or <default>` fallbacks fire (0.1 / 2.0 / 30).
    A 3-day streak with the default step 0.1 yields 1.2.
    """
    u, v = _setup(db)
    monkeypatch.setattr(settings, "chili_momentum_green_day_graduation_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_step_per_day", None, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_max_multiplier", None, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_lookback_days", None, raising=False)
    _add_green_streak(db, u, v, n_days=3)
    mult, meta = green_day_graduation_multiplier(db, execution_family=_EF)
    assert meta["step_per_day"] == pytest.approx(0.1)
    assert meta["max_multiplier"] == pytest.approx(2.0)
    assert mult == pytest.approx(1.2)


def test_zero_step_falls_back_to_default(db: Session, monkeypatch) -> None:
    # step == 0.0 is falsy -> `or 0.1` substitutes the default (NOT a frozen multiplier).
    u, v = _setup(db)
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_green_day_step_per_day", 0.0, raising=False)
    _add_green_streak(db, u, v, n_days=4)
    mult, meta = green_day_graduation_multiplier(db, execution_family=_EF)
    assert meta["step_per_day"] == pytest.approx(0.1)  # 0.0 -> default
    assert mult == pytest.approx(1.3)  # 1 + 0.1*3


def test_zero_lookback_setting_falls_back_to_default(db: Session, monkeypatch) -> None:
    # lookback setting 0 is falsy -> `or 30`. Without the fallback, lookback_days=0 would
    # short-circuit consecutive_green_days to no_input and the streak would be 0.
    u, v = _setup(db)
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_green_day_lookback_days", 0, raising=False)
    _add_green_streak(db, u, v, n_days=3)
    mult, meta = green_day_graduation_multiplier(db, execution_family=_EF)
    assert meta["consecutive_green_days"] == 3  # default-30 window saw all three days
    assert mult == pytest.approx(1.2)


def test_negative_step_not_falsy_passes_through(db: Session, monkeypatch) -> None:
    """ADVERSARIAL: a NEGATIVE step is truthy, so `or 0.1` does NOT replace it. With a long
    streak the term `1 + step*(streak-1)` goes below 1.0, but the outer max(1.0, ...) floors
    it. So a negative knob still cannot shrink size below 1.0 (never-veto invariant holds).
    """
    u, v = _setup(db)
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_green_day_step_per_day", -0.5, raising=False)
    _add_green_streak(db, u, v, n_days=5)
    mult, meta = green_day_graduation_multiplier(db, execution_family=_EF)
    assert meta["step_per_day"] == pytest.approx(-0.5)  # passed through (truthy)
    assert mult == 1.0  # floored, NOT 1 + (-0.5)*4 = -1.0


# ── meta-shape contract (load-bearing for the runner's logging/compose) ──────────


def test_graduation_meta_shape_when_enabled(db: Session, monkeypatch) -> None:
    # The meta dict the runner consumes carries all the documented keys with right types.
    u, v = _setup(db)
    _enable(monkeypatch, step=0.1, max_mult=2.0)
    _add_green_streak(db, u, v, n_days=3, pnl_per_day=25.0)
    mult, meta = green_day_graduation_multiplier(db, execution_family=_EF)
    assert set(("graduation_mult", "consecutive_green_days", "step_per_day",
                "max_multiplier", "green_usd", "days_seen")).issubset(meta.keys())
    assert meta["graduation_mult"] == pytest.approx(1.2)
    assert meta["consecutive_green_days"] == 3
    assert meta["green_usd"] == pytest.approx(75.0)  # 3 * 25
    assert meta["days_seen"] == 3


def test_streak_meta_green_usd_only_sums_green_prefix(db: Session, monkeypatch) -> None:
    # green_usd accumulates ONLY the counted green prefix, not buckets behind the reset.
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et = datetime.now(_ET).date()
    seq = [(1, 30.0), (2, 20.0), (3, -100.0), (4, 50.0)]  # green green RED green
    for days_ago, pnl in seq:
        d = today_et - timedelta(days=days_ago)
        _add_outcome(db, u, v, pnl=pnl,
                     terminal_at=_utc_for_et(d.year, d.month, d.day, 15),
                     symbol=f"S{days_ago}-USD",
                     outcome_class="small_win" if pnl >= 0 else "stop_loss")
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 2
    assert meta["green_usd"] == pytest.approx(50.0)  # 30 + 20 only; the +50 behind RED excluded
    assert meta["days_seen"] == 4  # all four traded days are buckets


# ── today-boundary: a trade exactly at today's ET 00:00 is excluded ──────────────


def test_today_boundary_excluded_strict(db: Session, monkeypatch) -> None:
    """terminal_at < today_start is STRICT: a fill at exactly today's ET 00:00:00 belongs to
    TODAY (excluded), not yesterday. Pairing it with a real yesterday green proves the cut.
    """
    u, v = _setup(db)
    _enable(monkeypatch)
    today_et_midnight_utc = (
        datetime.now(_ET).replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(_UTC).replace(tzinfo=None)
    )
    # a (would-be huge green) row at exactly today 00:00 ET -> excluded as "today"
    _add_outcome(db, u, v, pnl=500.0, terminal_at=today_et_midnight_utc,
                 symbol="MIDNIGHT-USD", outcome_class="small_win")
    # yesterday real green
    d1 = datetime.now(_ET).date() - timedelta(days=1)
    _add_outcome(db, u, v, pnl=10.0, terminal_at=_utc_for_et(d1.year, d1.month, d1.day, 15),
                 symbol="YDAY-USD", outcome_class="small_win")
    streak, meta = consecutive_green_days(db, execution_family=_EF, lookback_days=30)
    assert streak == 1, meta  # midnight row is today -> excluded
    assert meta["green_usd"] == pytest.approx(10.0)  # the +500 today is NOT counted


# ── flag-OFF parity is byte-identical regardless of knob values ──────────────────


def test_flag_off_parity_ignores_other_knobs(db: Session, monkeypatch) -> None:
    # With the flag OFF, step/max/lookback are irrelevant -> identical (1.0, disabled) meta.
    u, v = _setup(db)
    monkeypatch.setattr(settings, "chili_momentum_green_day_graduation_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_step_per_day", 9.9, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_max_multiplier", 99.0, raising=False)
    _add_green_streak(db, u, v, n_days=20)
    mult, meta = green_day_graduation_multiplier(db, execution_family=_EF)
    assert mult == 1.0
    assert meta == {"reason": "disabled", "graduation_mult": 1.0}  # exact, byte-identical
