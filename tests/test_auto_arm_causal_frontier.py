"""Focused causal-clock regressions for auto-arm helpers used by ReplayV3."""

from __future__ import annotations

from datetime import datetime, timedelta
import inspect
import uuid

import pytest

from app.models.core import User
from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import auto_arm as aa
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import risk_policy
from app.services.trading.momentum_neural.risk_policy import CurrentLiveLossHistoryEntry
from app.services.trading.venue import account_identity


_EF = "robinhood_spot"
_USER_ID = 9017
_ACCOUNT_IDENTITY = "auto-arm-frontier-account-v1"


@pytest.fixture(autouse=True)
def _stable_current_account_identity(monkeypatch):
    monkeypatch.setattr(
        account_identity,
        "read_current_non_alpaca_account_identity",
        lambda _family: {
            "ok": True,
            "identity": _ACCOUNT_IDENTITY,
            "reason": None,
        },
    )


def _variant(db) -> MomentumStrategyVariant:
    row = MomentumStrategyVariant(
        family="auto_arm_frontier_test",
        variant_key=f"frontier_{uuid.uuid4().hex[:16]}",
        label="auto-arm causal frontier",
        params_json={},
    )
    db.add(row)
    db.flush()
    return row


def _session(
    db,
    variant: MomentumStrategyVariant,
    *,
    symbol: str,
    started_at: datetime,
    execution_family: str = _EF,
    user_id: int = _USER_ID,
    risk_snapshot_json: dict | None = None,
) -> TradingAutomationSession:
    if db.get(User, user_id) is None:
        db.add(User(id=user_id, name=f"auto-arm-frontier-user-{user_id}"))
        db.flush()
    snapshot = dict(risk_snapshot_json or {})
    if execution_family not in {"alpaca_spot", "alpaca_short"}:
        snapshot.setdefault("non_alpaca_account_identity", _ACCOUNT_IDENTITY)
    row = TradingAutomationSession(
        user_id=user_id,
        mode="live",
        execution_family=execution_family,
        symbol=symbol,
        variant_id=variant.id,
        state="live_finished",
        risk_snapshot_json=snapshot,
        started_at=started_at,
        ended_at=started_at,
        created_at=started_at,
    )
    db.add(row)
    db.flush()
    return row


def _winning_outcome(
    db,
    variant: MomentumStrategyVariant,
    *,
    symbol: str,
    terminal_at: datetime,
) -> None:
    session = _session(
        db,
        variant,
        symbol=symbol,
        started_at=terminal_at,
    )
    db.add(
        MomentumAutomationOutcome(
            session_id=session.id,
            user_id=_USER_ID,
            variant_id=variant.id,
            symbol=symbol,
            mode="live",
            execution_family=_EF,
            terminal_state="live_finished",
            terminal_at=terminal_at,
            created_at=terminal_at,
            outcome_class="small_win",
            realized_pnl_usd=25.0,
            return_bps=100.0,
            broker_recon_status="reconciled",
            broker_realized_pnl_usd=25.0,
            broker_return_bps=100.0,
            broker_reconciled_at=terminal_at,
            regime_snapshot_json={},
            entry_regime_snapshot_json={},
            exit_regime_snapshot_json={},
            readiness_snapshot_json={},
            admission_snapshot_json={},
            governance_context_json={},
            extracted_summary_json={},
            evidence_weight=1.0,
            contributes_to_evolution=False,
        )
    )
    db.flush()


def _losing_outcome(
    db,
    variant: MomentumStrategyVariant,
    *,
    symbol: str,
    terminal_at: datetime,
    execution_family: str = _EF,
    user_id: int = _USER_ID,
    risk_snapshot_json: dict | None = None,
    created_at: datetime | None = None,
) -> None:
    available_at = created_at or terminal_at
    session = _session(
        db,
        variant,
        symbol=symbol,
        started_at=available_at,
        execution_family=execution_family,
        user_id=user_id,
        risk_snapshot_json=risk_snapshot_json,
    )
    db.add(
        MomentumAutomationOutcome(
            session_id=session.id,
            user_id=user_id,
            variant_id=variant.id,
            symbol=symbol,
            mode="live",
            execution_family=execution_family,
            terminal_state="live_finished",
            terminal_at=terminal_at,
            created_at=available_at,
            outcome_class="stop_loss",
            realized_pnl_usd=-25.0,
            return_bps=-100.0,
            broker_recon_status="reconciled",
            broker_realized_pnl_usd=-25.0,
            broker_return_bps=-100.0,
            broker_reconciled_at=terminal_at,
            regime_snapshot_json={},
            entry_regime_snapshot_json={},
            exit_regime_snapshot_json={},
            readiness_snapshot_json={},
            admission_snapshot_json={},
            governance_context_json={},
            extracted_summary_json={},
            evidence_weight=1.0,
            contributes_to_evolution=False,
        )
    )
    db.flush()


def test_win_cycle_ignores_future_same_day_outcome(db) -> None:
    frontier = datetime(2026, 7, 14, 15, 0, 0)
    variant = _variant(db)
    _winning_outcome(
        db,
        variant,
        symbol="PAST",
        terminal_at=frontier - timedelta(minutes=1),
    )
    _winning_outcome(
        db,
        variant,
        symbol="FUTURE",
        terminal_at=frontier + timedelta(minutes=1),
    )

    assert aa._win_cycle_clean_win_count(
        db,
        execution_family=_EF,
        as_of_utc=frontier,
    ) == 1


def test_win_cycle_and_symbol_attempts_use_et_day_across_dst_fall_back(db) -> None:
    # 2026-11-01 is a 25-hour ET day: [04:00Z Nov 1, 05:00Z Nov 2).
    frontier = datetime(2026, 11, 2, 4, 30, 0)
    variant = _variant(db)
    _winning_outcome(
        db,
        variant,
        symbol="DST",
        terminal_at=datetime(2026, 11, 1, 4, 15, 0),
    )
    _winning_outcome(
        db,
        variant,
        symbol="DST",
        terminal_at=datetime(2026, 11, 1, 3, 59, 59),
    )

    with lr.replay_clock(frontier):
        assert aa._win_cycle_clean_win_count(
            db,
            execution_family=_EF,
        ) == 1
        assert aa._per_symbol_attempt_count(
            db,
            "DST",
            execution_family=_EF,
        ) == 1


def test_per_symbol_fatigue_ignores_future_same_day_session(db) -> None:
    frontier = datetime(2026, 7, 14, 15, 0, 0)
    variant = _variant(db)
    _session(
        db,
        variant,
        symbol="CLRO",
        started_at=frontier - timedelta(minutes=1),
    )
    _session(
        db,
        variant,
        symbol="CLRO",
        started_at=frontier + timedelta(minutes=1),
    )

    assert aa._per_symbol_attempt_count(
        db,
        "CLRO",
        execution_family=_EF,
        as_of_utc=frontier,
    ) == 1


def test_symbol_loss_guard_ignores_future_same_day_loss(db) -> None:
    frontier = datetime(2026, 7, 14, 15, 0, 0)
    variant = _variant(db)
    _losing_outcome(
        db,
        variant,
        symbol="FUTURE",
        terminal_at=frontier + timedelta(minutes=1),
    )

    blocked, cooldown = aa._symbol_loss_guards(
        db,
        user_id=_USER_ID,
        execution_family=_EF,
        as_of_utc=frontier,
    )

    assert "FUTURE" not in blocked
    assert "FUTURE" not in cooldown


def test_symbol_loss_guard_ignores_late_backfill_before_availability(db) -> None:
    frontier = datetime(2026, 7, 14, 15, 0, 0)
    variant = _variant(db)
    _losing_outcome(
        db,
        variant,
        symbol="BACKFILL",
        terminal_at=frontier - timedelta(minutes=2),
        created_at=frontier + timedelta(minutes=1),
    )

    blocked, cooldown = aa._symbol_loss_guards(
        db,
        user_id=_USER_ID,
        execution_family=_EF,
        as_of_utc=frontier,
    )

    assert "BACKFILL" not in blocked
    assert "BACKFILL" not in cooldown


def test_symbol_loss_guard_isolates_user_and_normalized_family(db) -> None:
    frontier = datetime(2026, 7, 14, 15, 0, 0)
    variant = _variant(db)
    # These two losses would falsely trip the two-strike block if either scope
    # axis were omitted.
    _losing_outcome(
        db,
        variant,
        symbol="SCOPE",
        terminal_at=frontier - timedelta(minutes=3),
        user_id=_USER_ID + 1,
    )
    _losing_outcome(
        db,
        variant,
        symbol="SCOPE",
        terminal_at=frontier - timedelta(minutes=2),
        execution_family="coinbase_spot",
    )
    _losing_outcome(
        db,
        variant,
        symbol="SCOPE",
        terminal_at=frontier - timedelta(minutes=1),
    )

    blocked, cooldown = aa._symbol_loss_guards(
        db,
        user_id=_USER_ID,
        execution_family="ROBINHOOD_SPOT",
        as_of_utc=frontier,
    )

    assert "SCOPE" not in blocked
    assert "SCOPE" in cooldown


def test_symbol_loss_guard_isolates_alpaca_account_before_cycle_settlement(
    db, monkeypatch
) -> None:
    frontier = datetime(2026, 7, 14, 15, 0, 0)
    variant = _variant(db)
    selected = {
        "alpaca_account_scope": "alpaca:paper",
        "alpaca_account_id": "paper-selected",
    }
    other = {
        "alpaca_account_scope": "alpaca:paper",
        "alpaca_account_id": "paper-other",
    }
    monkeypatch.setattr(
        aa.settings,
        "chili_alpaca_expected_account_id",
        "paper-selected",
        raising=False,
    )
    for minute in (4, 3):
        _losing_outcome(
            db,
            variant,
            symbol="PAPER",
            terminal_at=frontier - timedelta(minutes=minute),
            execution_family="alpaca_spot",
            risk_snapshot_json=other,
        )
    _losing_outcome(
        db,
        variant,
        symbol="PAPER",
        terminal_at=frontier - timedelta(minutes=1),
        execution_family="alpaca_spot",
        risk_snapshot_json=selected,
    )

    entries, meta = risk_policy.load_current_live_loss_history(
        db,
        user_id=_USER_ID,
        execution_family="ALPACA_SPOT",
        account_scope="alpaca:paper",
        account_identity="paper-selected",
        decision_as_of=frontier,
    )
    assert entries == ()
    assert meta["history_unavailable"] is True
    assert meta["coverage_grade"] == "COVERAGE_UNAVAILABLE"
    assert meta["reason"] == "loss_guard_alpaca_cycle_settlement_unavailable"
    # The two rows from another paper-account generation are excluded before
    # coverage is graded; only the selected account's unsupported legacy cycle
    # is allowed to make this decision fail closed.
    assert meta["coverage_gap_counts"] == {
        "loss_guard_alpaca_cycle_settlement_unavailable": 1,
    }

    with pytest.raises(aa._LossGuardHistoryUnavailable) as exc:
        aa._symbol_loss_guards(
            db,
            user_id=_USER_ID,
            execution_family="ALPACA_SPOT",
            account_scope="alpaca:paper",
            account_identity="paper-selected",
            as_of_utc=frontier,
        )
    assert str(exc.value) == "loss_guard_alpaca_cycle_settlement_unavailable"


def test_symbol_loss_guard_missing_alpaca_identity_is_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        aa.settings,
        "chili_alpaca_expected_account_id",
        "",
        raising=False,
    )
    with pytest.raises(aa._LossGuardScopeUnavailable) as exc:
        aa._symbol_loss_guards(
            object(),
            user_id=_USER_ID,
            execution_family="alpaca_spot",
            account_scope="alpaca:paper",
            as_of_utc=datetime(2026, 7, 14, 15, 0, 0),
        )
    assert str(exc.value) == "alpaca_loss_guard_identity_unavailable"


def test_alpaca_twin_is_blocked_by_its_own_account_loss_history(monkeypatch) -> None:
    frontier = datetime(2026, 7, 14, 15, 0, 0)
    account_id = "alpaca-paper-twin-account"
    monkeypatch.setattr(aa.settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(
        aa.settings,
        "chili_alpaca_expected_account_id",
        account_id,
        raising=False,
    )
    threshold = int(
        getattr(aa.settings, "chili_momentum_consecutive_loss_halt_count", 4)
        or 4
    )
    entries = tuple(
        CurrentLiveLossHistoryEntry(
            session_id=index,
            outcome_id=index,
            symbol=f"TWIN{index}",
            terminal_at=frontier - timedelta(minutes=index),
            outcome_class="stop_loss",
            realized_pnl_usd=-5.0,
            return_bps=-50.0,
            broker_reconciled_at=frontier - timedelta(minutes=index),
        )
        for index in range(1, threshold + 1)
    )
    history = (
        entries,
        {
            "history_available": True,
            "coverage_grade": "CURRENT_LIVE_COMPLETE",
            "history_authority": "broker_reconciled_current_live_db_only",
            "replay_certifiable": False,
        },
    )
    monkeypatch.setattr(
        risk_policy,
        "load_current_live_loss_history",
        lambda db, **kwargs: history,
    )

    allowed, meta, scope = aa._alpaca_twin_loss_guard_decision(
        object(),
        user_id=_USER_ID,
        symbol="NEXT",
        as_of_utc=frontier,
    )

    assert allowed is False
    assert meta["reason"] == "alpaca_twin_consecutive_loss_halt"
    assert scope is not None
    assert scope["account_identity"] == account_id


def test_supplied_live_identity_cannot_override_current_truth(monkeypatch) -> None:
    with pytest.raises(aa._LossGuardScopeUnavailable) as exc:
        aa._resolve_loss_guard_scope(
            user_id=_USER_ID,
            execution_family=_EF,
            account_identity="different-account",
        )
    assert str(exc.value) == "non_alpaca_loss_guard_identity_mismatch"

    monkeypatch.setattr(
        aa.settings,
        "chili_alpaca_expected_account_id",
        "expected-paper-account",
        raising=False,
    )
    with pytest.raises(aa._LossGuardScopeUnavailable) as alpaca_exc:
        aa._resolve_loss_guard_scope(
            user_id=_USER_ID,
            execution_family="alpaca_spot",
            account_scope="alpaca:paper",
            account_identity="different-paper-account",
        )
    assert str(alpaca_exc.value) == "alpaca_loss_guard_identity_mismatch"


def test_explicit_historical_auto_arm_never_reads_adapter_or_current_db(
    monkeypatch,
) -> None:
    frontier = datetime(2026, 7, 14, 15, 0, 0)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_auto_arm_live_enabled", True, raising=False
    )
    monkeypatch.setattr(
        aa.settings, "chili_momentum_live_runner_enabled", True, raising=False
    )
    monkeypatch.setattr(aa.settings, "chili_autotrader_user_id", _USER_ID, raising=False)

    def _forbidden(*args, **kwargs):
        raise AssertionError("historical decision attempted current fallback")

    monkeypatch.setattr(
        account_identity,
        "read_current_non_alpaca_account_identity",
        _forbidden,
    )

    out = aa.run_auto_arm_pass(_forbidden, decision_at=frontier)

    assert out["skipped"] == "loss_guard_history_coverage_unavailable"
    assert out["loss_guard_history"]["coverage_grade"] == "COVERAGE_UNAVAILABLE"
    assert out["loss_guard_history"]["adapter_fallback_attempted"] is False
    assert out["loss_guard_history"]["current_db_fallback_attempted"] is False


def test_bound_replay_clock_never_reads_adapter_or_current_db(monkeypatch) -> None:
    frontier = datetime(2026, 7, 14, 15, 0, 0)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_auto_arm_live_enabled", True, raising=False
    )
    monkeypatch.setattr(
        aa.settings, "chili_momentum_live_runner_enabled", True, raising=False
    )
    monkeypatch.setattr(aa.settings, "chili_autotrader_user_id", _USER_ID, raising=False)

    def _forbidden(*args, **kwargs):
        raise AssertionError("replay attempted current fallback")

    monkeypatch.setattr(
        account_identity,
        "read_current_non_alpaca_account_identity",
        _forbidden,
    )

    with lr.replay_clock(frontier):
        out = aa.run_auto_arm_pass(_forbidden)

    assert out["skipped"] == "loss_guard_history_coverage_unavailable"
    assert out["loss_guard_history"]["decision_as_of_utc"].startswith(
        "2026-07-14T15:00:00"
    )


def test_replay_authority_seam_failure_is_coverage_unavailable(monkeypatch) -> None:
    monkeypatch.delattr(lr, "_SIM_NOW")
    monkeypatch.setattr(
        account_identity,
        "read_current_non_alpaca_account_identity",
        lambda _family: (_ for _ in ()).throw(
            AssertionError("authority seam failure must not read adapter identity")
        ),
    )

    class _NoCurrentDB:
        def query(self, *_args, **_kwargs):
            raise AssertionError("authority seam failure must not read current DB")

        def add(self, *_args, **_kwargs):
            raise AssertionError("authority seam failure must not mutate")

        def commit(self):
            raise AssertionError("authority seam failure must not commit")

    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_live_enabled", True)
    monkeypatch.setattr(aa.settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(aa.settings, "chili_autotrader_user_id", _USER_ID)

    out = aa.run_auto_arm_pass(_NoCurrentDB())

    assert out["armed"] == 0
    assert out["skipped"] == "loss_guard_history_coverage_unavailable"
    assert out["loss_guard_history"]["adapter_fallback_attempted"] is False
    assert out["loss_guard_history"]["current_db_fallback_attempted"] is False


def test_prime_window_default_clock_uses_replay_instant(monkeypatch) -> None:
    # 13:00 UTC in July is 09:00 ET, inside the configured prime window.
    frontier = datetime(2026, 7, 14, 13, 0, 0)
    monkeypatch.setattr(
        aa.settings,
        "chili_momentum_timeofday_schedule_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        aa.settings,
        "chili_momentum_timeofday_prime_window_start_et",
        "04:00",
        raising=False,
    )
    monkeypatch.setattr(
        aa.settings,
        "chili_momentum_timeofday_prime_window_end_et",
        "10:30",
        raising=False,
    )
    monkeypatch.setattr(
        aa.settings,
        "chili_momentum_timeofday_prime_window_size_mult_max",
        1.4,
        raising=False,
    )

    with lr.replay_clock(frontier):
        assert aa._utcnow() == frontier
        multiplier, meta = aa.prime_window_size_multiplier()

    assert multiplier == pytest.approx(1.4)
    assert meta["et_min"] == 9 * 60


def test_live_runner_threads_one_entry_sizing_frontier_to_auto_arm_helpers() -> None:
    source = inspect.getsource(lr.tick_live_session)

    assert "_entry_sizing_as_of = _utcnow()" in source
    assert source.count("as_of_utc=_entry_sizing_as_of") >= 2
    assert "now=_entry_sizing_as_of" in source


def test_auto_arm_pass_threads_one_explicit_decision_frontier() -> None:
    source = inspect.getsource(aa.run_auto_arm_pass)

    assert "pass_as_of = _decision_as_of_naive_utc(decision_at)" in source
    assert "datetime.utcnow()" not in source
    assert "as_of_utc=pass_as_of" in source
    assert source.count("pass_as_of") >= 8


def test_viability_board_helpers_do_not_read_bare_wall_clock() -> None:
    for helper in (
        aa._count_live_eligible_field,
        aa._fresh_live_eligible_candidates,
    ):
        source = inspect.getsource(helper)
        assert "datetime.utcnow()" not in source
        assert "_decision_as_of_naive_utc(as_of_utc)" in source
