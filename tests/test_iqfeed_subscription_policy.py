from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

import scripts.iqfeed_depth_bridge as depth_bridge
import scripts.iqfeed_trade_bridge as trade_bridge
from scripts.iqfeed_subscription_policy import (
    ACTIVE_EXECUTION_SESSION_SQL,
    LifecycleStage,
    SubscriptionConnectionIndeterminate,
    SourceRead,
    SubscriptionLifecycleRecord,
    TargetCause,
    WatchCapacityGovernor,
    active_capture_symbols,
    is_active_capture_session,
    resolve_subscription_target,
)


def _all_sources(
    *,
    active=(),
    hints=(),
    eligible=(),
    ross=(),
):
    return [
        SourceRead.success(TargetCause.ACTIVE, active),
        SourceRead.success(TargetCause.HINT, hints),
        SourceRead.success(TargetCause.ELIGIBLE, eligible),
        SourceRead.success(TargetCause.ROSS, ross),
    ]


def test_limit_transition_uses_current_cap_and_never_increases_it() -> None:
    governor = WatchCapacityGovernor.start(
        cap=1_000,
        floor=64,
        hard_max=1_000,
        quiet_interval_seconds=300.0,
        now=0.0,
    )

    reduced = governor.advance(
        now=1.0,
        limit_hit=True,
        capacity_pressure=True,
        at_capacity=True,
    )
    assert reduced.cap == 500

    floor_governor = WatchCapacityGovernor.start(
        cap=64,
        floor=64,
        hard_max=1_000,
        quiet_interval_seconds=300.0,
        now=0.0,
    )
    assert floor_governor.advance(
        now=1.0,
        limit_hit=True,
        capacity_pressure=True,
        at_capacity=True,
    ).cap == 64


def test_capacity_recovers_one_slot_only_after_quiet_interval() -> None:
    governor = WatchCapacityGovernor.start(
        cap=500,
        floor=64,
        hard_max=1_000,
        quiet_interval_seconds=300.0,
        now=100.0,
    )

    assert governor.advance(
        now=399.999,
        limit_hit=False,
        capacity_pressure=True,
        at_capacity=True,
    ).cap == 500
    first_probe = governor.advance(
        now=400.0,
        limit_hit=False,
        capacity_pressure=True,
        at_capacity=True,
    )
    assert first_probe.cap == 501
    assert first_probe.advance(
        now=699.999,
        limit_hit=False,
        capacity_pressure=True,
        at_capacity=True,
    ).cap == 501
    assert first_probe.advance(
        now=700.0,
        limit_hit=False,
        capacity_pressure=True,
        at_capacity=True,
    ).cap == 502

    final_probe = WatchCapacityGovernor.start(
        cap=999,
        floor=64,
        hard_max=1_000,
        quiet_interval_seconds=10.0,
        now=0.0,
    ).advance(
        now=10.0,
        limit_hit=False,
        capacity_pressure=True,
        at_capacity=True,
    )
    assert final_probe.cap == 1_000
    assert final_probe.advance(
        now=20.0,
        limit_hit=False,
        capacity_pressure=True,
        at_capacity=True,
    ).cap == 1_000


def test_failed_recovery_probe_reverts_without_second_halving() -> None:
    governor = WatchCapacityGovernor.start(
        cap=64,
        floor=64,
        hard_max=1_000,
        quiet_interval_seconds=10.0,
        now=0.0,
    )

    probe = governor.advance(
        now=10.0,
        limit_hit=False,
        capacity_pressure=True,
        at_capacity=True,
    )
    assert probe.cap == 65
    reverted = probe.advance(
        now=10.1,
        limit_hit=True,
        capacity_pressure=True,
        at_capacity=True,
    )
    assert reverted.cap == 64
    assert reverted.advance(
        now=20.099,
        limit_hit=False,
        capacity_pressure=True,
        at_capacity=True,
    ).cap == 64


def test_capacity_does_not_recover_without_excess_demand() -> None:
    governor = WatchCapacityGovernor.start(
        cap=500,
        floor=64,
        hard_max=1_000,
        quiet_interval_seconds=10.0,
        now=0.0,
    )

    assert governor.advance(
        now=1_000.0,
        limit_hit=False,
        capacity_pressure=False,
        at_capacity=False,
    ).cap == 500


def test_one_source_sentinel_exposes_excess_capacity_demand() -> None:
    governor = WatchCapacityGovernor.start(
        cap=2,
        floor=1,
        hard_max=10,
        quiet_interval_seconds=10.0,
        now=0.0,
    )
    assert governor.source_read_limit == 3

    result = resolve_subscription_target(
        reads=_all_sources(eligible=("E1", "E2", "E3")[: governor.source_read_limit]),
        prior_causes={},
        capacity=governor.cap,
    )

    assert tuple(target.symbol for target in result.targets) == ("E1", "E2")
    assert any(
        gap.code == "capacity_eviction" and gap.symbol == "E3"
        for gap in result.gaps
    )


def test_reconnect_reverts_unproven_probe_and_restarts_quiet_interval() -> None:
    governor = WatchCapacityGovernor.start(
        cap=64,
        floor=64,
        hard_max=1_000,
        quiet_interval_seconds=10.0,
        now=0.0,
    )
    probe = governor.advance(
        now=10.0,
        limit_hit=False,
        capacity_pressure=True,
        at_capacity=True,
    )

    restarted = probe.connection_started(now=100.0)

    assert restarted.cap == 64
    assert restarted.probe_base_cap is None
    assert restarted.next_probe_at == 110.0


def test_capacity_governor_rejects_non_finite_clocks() -> None:
    with pytest.raises(ValueError, match="quiet interval"):
        WatchCapacityGovernor.start(
            cap=64,
            floor=64,
            hard_max=1_000,
            quiet_interval_seconds=float("nan"),
            now=0.0,
        )
    with pytest.raises(ValueError, match="clock"):
        WatchCapacityGovernor.start(
            cap=64,
            floor=64,
            hard_max=1_000,
            quiet_interval_seconds=10.0,
            now=float("inf"),
        )


@pytest.mark.parametrize("bridge", (trade_bridge, depth_bridge))
def test_both_bridges_apply_the_shared_capacity_transition(
    monkeypatch,
    bridge,
) -> None:
    governor = WatchCapacityGovernor.start(
        cap=1_000,
        floor=64,
        hard_max=1_000,
        quiet_interval_seconds=300.0,
        now=0.0,
    )
    monkeypatch.setattr(bridge, "_watch_capacity", governor)
    monkeypatch.setattr(bridge, "_limit_hit", True)

    previous, current, limit_hit = bridge._advance_watch_capacity(
        now=1.0,
        capacity_pressure=True,
        at_capacity=True,
    )

    assert previous.cap == 1_000
    assert current.cap == 500
    assert limit_hit is True
    assert bridge._watch_capacity == current
    assert bridge._limit_hit is False


@pytest.mark.parametrize("bridge", (trade_bridge, depth_bridge))
def test_pending_probe_limit_is_consumed_before_reconnect_reset(
    monkeypatch,
    bridge,
) -> None:
    stable = WatchCapacityGovernor.start(
        cap=500,
        floor=64,
        hard_max=1_000,
        quiet_interval_seconds=300.0,
        now=0.0,
    )
    probe = stable.advance(
        now=300.0,
        limit_hit=False,
        capacity_pressure=True,
        at_capacity=True,
    )
    assert probe.cap == 501
    monkeypatch.setattr(bridge, "_watch_capacity", probe)
    monkeypatch.setattr(bridge, "_limit_hit", True)

    previous, current, limit_hit = bridge._start_watch_capacity_connection(
        now=301.0
    )

    assert previous.cap == 501
    assert current.cap == 500
    assert current.probe_base_cap is None
    assert limit_hit is True
    assert bridge._limit_hit is False


def test_union_preserves_every_source_and_overlapping_causes() -> None:
    result = resolve_subscription_target(
        reads=_all_sources(
            active=("HELD", "BOTH"),
            hints=("HINT", "BOTH"),
            eligible=("PLSM", "BOTH"),
            ross=("VEEE", "BOTH"),
        ),
        prior_causes={},
        capacity=16,
    )

    assert result.symbols == {"HELD", "HINT", "PLSM", "VEEE", "BOTH"}
    assert result.causes_by_symbol["BOTH"] == {
        TargetCause.ACTIVE,
        TargetCause.HINT,
        TargetCause.ELIGIBLE,
        TargetCause.ROSS,
    }
    assert result.coverage_complete


def test_active_is_non_evictable_even_when_it_exceeds_capacity() -> None:
    result = resolve_subscription_target(
        reads=_all_sources(
            active=("HELD1", "HELD2"),
            hints=("NEW",),
            eligible=("PLSM",),
            ross=("VEEE",),
        ),
        prior_causes={},
        capacity=1,
    )

    assert {"HELD1", "HELD2"} <= result.symbols
    assert any(gap.code == "protected_targets_exceed_capacity" for gap in result.gaps)
    assert {gap.symbol for gap in result.gaps if gap.code == "capacity_eviction"} == {
        "NEW",
        "PLSM",
        "VEEE",
    }


def test_late_hints_are_additive_and_cannot_erase_broad_targets() -> None:
    broad = resolve_subscription_target(
        reads=_all_sources(eligible=("PLSM",), ross=("VEEE",)),
        prior_causes={},
        capacity=2,
    )
    late = resolve_subscription_target(
        reads=_all_sources(
            hints=("PLSM", "VEEE", "LATE"),
            eligible=("PLSM",),
            ross=("VEEE",),
        ),
        prior_causes=broad.causes_by_symbol,
        capacity=2,
    )

    assert late.symbols == {"PLSM", "VEEE"}
    assert TargetCause.HINT in late.causes_by_symbol["PLSM"]
    assert TargetCause.HINT in late.causes_by_symbol["VEEE"]
    assert any(
        gap.code == "capacity_eviction" and gap.symbol == "LATE"
        for gap in late.gaps
    )


def test_fresh_hot_hint_outranks_cold_ross_fallback_but_not_active() -> None:
    result = resolve_subscription_target(
        reads=_all_sources(
            active=("HELD",),
            hints=("PLSM",),
            eligible=("ELIGIBLE_COLD",),
            ross=("ROSS_COLD",),
        ),
        prior_causes={
            "HELD": {TargetCause.ACTIVE},
            "ROSS_COLD": {TargetCause.ROSS},
        },
        capacity=2,
    )

    assert result.symbols == {"HELD", "PLSM"}
    assert "HELD" not in result.evicted_symbols
    assert any(
        gap.code == "capacity_eviction" and gap.symbol == "ROSS_COLD"
        for gap in result.gaps
    )


def test_ross_outranks_standing_eligible_without_reordering_canonical_causes() -> None:
    result = resolve_subscription_target(
        reads=_all_sources(
            eligible=("E_ONLY", "BOTH"),
            ross=("R_ONLY", "BOTH"),
        ),
        prior_causes={},
        capacity=1,
    )

    assert tuple(target.symbol for target in result.targets) == ("R_ONLY",)
    capacity_gaps = tuple(
        gap for gap in result.gaps if gap.code == "capacity_eviction"
    )
    assert tuple(gap.symbol for gap in capacity_gaps) == ("BOTH", "E_ONLY")
    assert capacity_gaps[0].causes == (
        TargetCause.ELIGIBLE,
        TargetCause.ROSS,
    )


def test_hint_flood_input_is_deduped_without_losing_newest_first_rank() -> None:
    hints = SourceRead.success(
        TargetCause.HINT,
        ("plsm", "PLSM", "veee", "VEEE", "third"),
    )

    assert hints.symbols == ("PLSM", "VEEE", "THIRD")


def test_query_failure_retains_complete_prior_watch_set_and_emits_gap() -> None:
    result = resolve_subscription_target(
        reads=[
            SourceRead.failure(
                TargetCause.ACTIVE,
                error_code="database_unavailable",
                error_detail="timeout",
            ),
            SourceRead.success(TargetCause.HINT, ()),
            SourceRead.success(TargetCause.ELIGIBLE, ("NEW",)),
            SourceRead.success(TargetCause.ROSS, ()),
        ],
        prior_causes={
            "HELD": {TargetCause.ACTIVE},
            "PLSM": {TargetCause.ELIGIBLE},
        },
        capacity=3,
    )

    assert result.symbols == {"HELD", "PLSM", "NEW"}
    assert result.retained_prior_on_failure
    assert result.evicted_symbols == ()
    assert any(
        gap.code == "source_query_failed" and gap.source == "active"
        for gap in result.gaps
    )


def test_capacity_eviction_is_deterministic_and_explicit() -> None:
    kwargs = dict(
        reads=_all_sources(
            hints=("H2", "H1"),
            eligible=("E2", "E1"),
            ross=("R2", "R1"),
        ),
        prior_causes={},
        capacity=3,
    )
    first = resolve_subscription_target(**kwargs)
    second = resolve_subscription_target(**kwargs)

    assert first == second
    assert tuple(target.symbol for target in first.targets) == ("H2", "H1", "R2")
    assert tuple(
        gap.symbol for gap in first.gaps if gap.code == "capacity_eviction"
    ) == ("R1", "E2", "E1")


def test_both_bridges_use_the_same_four_source_resolver() -> None:
    reads = _all_sources(
        active=("HELD",),
        hints=("HINT",),
        eligible=("PLSM",),
        ross=("VEEE",),
    )
    expected = {"HELD", "HINT", "VEEE"}

    l1 = trade_bridge._resolve_target(
        reads=reads,
        prior_causes={},
        capacity=3,
    )
    l2 = depth_bridge._resolve_target(
        reads=reads,
        prior_causes={},
        capacity=3,
    )

    assert l1.symbols == expected
    assert l2.symbols == expected
    assert l1 == l2


def test_l2_rejects_session_only_target_regression() -> None:
    try:
        depth_bridge._resolve_target(
            reads=[SourceRead.success(TargetCause.ACTIVE, ("HELD",))],
            prior_causes={},
            capacity=8,
        )
    except ValueError as exc:
        assert "missing sources" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("session-only L2 target must be rejected")


@pytest.mark.parametrize(
    ("mode", "state"),
    [
        ("live", "armed_pending_runner"),
        ("live", "queued_live"),
        ("live", "watching_live"),
        ("live", "live_entry_candidate"),
        ("live", "live_pending_entry"),
        ("live", "live_entered"),
        ("live", "live_scaling_out"),
        ("live", "live_trailing"),
        ("live", "live_bailout"),
        ("paper", "queued"),
        ("paper", "watching"),
        ("paper", "entry_candidate"),
        ("paper", "pending_entry"),
        ("paper", "entered"),
        ("paper", "scaling_out"),
        ("paper", "trailing"),
        ("paper", "bailout"),
    ],
)
def test_active_capture_policy_protects_execution_relevant_live_and_paper_states(
    mode: str,
    state: str,
) -> None:
    assert is_active_capture_session(mode=mode, state=state)
    assert active_capture_symbols([("plsm", mode, state)]) == ("PLSM",)


@pytest.mark.parametrize(
    ("mode", "state"),
    [
        ("live", "live_exited"),
        ("live", "live_cooldown"),
        ("live", "live_finished"),
        ("live", "live_cancelled"),
        ("live", "live_error"),
        ("paper", "exited"),
        ("paper", "cooldown"),
        ("paper", "finished"),
        ("paper", "cancelled"),
        ("paper", "expired"),
        ("paper", "error"),
    ],
)
def test_active_capture_policy_excludes_flat_or_terminal_sessions(
    mode: str,
    state: str,
) -> None:
    assert not is_active_capture_session(mode=mode, state=state)
    assert active_capture_symbols([("PLSM", mode, state)]) == ()


def test_active_capture_sql_inventories_both_fsm_modes() -> None:
    assert "symbol NOT LIKE '%-%'" in ACTIVE_EXECUTION_SESSION_SQL
    assert "mode='live'" in ACTIVE_EXECUTION_SESSION_SQL
    assert "mode='paper'" in ACTIVE_EXECUTION_SESSION_SQL
    assert "live_pending_entry" in ACTIVE_EXECUTION_SESSION_SQL
    assert "pending_entry" in ACTIVE_EXECUTION_SESSION_SQL
    assert "live_finished" not in ACTIVE_EXECUTION_SESSION_SQL
    assert "finished" not in ACTIVE_EXECUTION_SESSION_SQL


def test_active_capture_query_has_migration_owned_covering_index() -> None:
    from app import migrations
    from app.models.trading import TradingAutomationSession

    index = next(
        (
            candidate
            for candidate in TradingAutomationSession.__table__.indexes
            if candidate.name == "ix_tas_capture_active"
        ),
        None,
    )
    assert index is not None
    assert [column.name for column in index.columns] == ["mode", "state", "symbol"]
    assert "symbol NOT LIKE '%-%'" in str(
        index.dialect_options["postgresql"]["where"]
    )
    assert any(
        version_id == "358_trading_session_capture_lookup_index"
        and migrate_fn is migrations._migration_358_trading_session_capture_lookup_index
        for version_id, migrate_fn in migrations.MIGRATIONS
    )


def test_active_capture_migration_creates_and_verifies_physical_index(db) -> None:
    from sqlalchemy import text

    from app import migrations

    connection = db.connection()
    connection.execute(text("DROP INDEX IF EXISTS ix_tas_capture_active"))
    migrations._migration_358_trading_session_capture_lookup_index(connection)
    row = connection.execute(
        text(
            "SELECT i.indisvalid,i.indisready,pg_get_expr(i.indpred,i.indrelid) "
            "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "WHERE c.relname='ix_tas_capture_active'"
        )
    ).one()
    assert row[0] is True
    assert row[1] is True
    assert "!~~" in str(row[2])


def test_active_capture_migration_rejects_wrong_same_named_index(db) -> None:
    from sqlalchemy import text

    from app import migrations

    connection = db.connection()
    connection.execute(text("DROP INDEX IF EXISTS ix_tas_capture_active"))
    connection.execute(
        text(
            "CREATE INDEX ix_tas_capture_active "
            "ON trading_automation_sessions (symbol)"
        )
    )
    with pytest.raises(RuntimeError, match="migration 358 capture lookup index"):
        migrations._migration_358_trading_session_capture_lookup_index(connection)


def test_lifecycle_contract_enumerates_every_required_stage() -> None:
    assert {stage.value for stage in LifecycleStage} == {
        "target_evaluation",
        "send_success",
        "send_failure",
        "ack_unavailable",
        "first_valid_frame",
        "reconnect",
        "limit_eviction",
        "gap",
    }


def test_l1_watch_send_failure_invalidates_connection_state(monkeypatch) -> None:
    state = {"HELD"}

    def fail_send(_socket, _command):
        raise OSError("socket closed")

    monkeypatch.setattr(trade_bridge, "_send", fail_send)
    with pytest.raises(SubscriptionConnectionIndeterminate) as raised:
        trade_bridge._try_watch_symbol(
            object(),
            "PLSM",
            causes=(TargetCause.ACTIVE,),
            watched_set=state,
        )

    assert state == set()
    assert raised.value.gap.code == "watch_send_indeterminate"
    assert raised.value.gap.causes == (TargetCause.ACTIVE,)
    assert "command_index=1/1" in str(raised.value.gap.detail)


def test_l1_unwatch_send_failure_invalidates_connection_state(monkeypatch) -> None:
    state = {"PLSM"}

    def fail_send(_socket, _command):
        raise OSError("socket closed")

    monkeypatch.setattr(trade_bridge, "_send", fail_send)
    with pytest.raises(SubscriptionConnectionIndeterminate) as raised:
        trade_bridge._try_unwatch_symbol(object(), "PLSM", watched_set=state)

    assert state == set()
    assert raised.value.gap.code == "unwatch_send_indeterminate"


def test_l1_sticky_send_failure_invalidates_connection_state(monkeypatch) -> None:
    state = {"PLSM", "OTHER"}

    def fail_send(_socket, _command):
        raise OSError("socket closed")

    monkeypatch.setattr(trade_bridge, "_send", fail_send)
    with pytest.raises(SubscriptionConnectionIndeterminate) as raised:
        trade_bridge._try_sticky_resubscribe_symbol(
            object(),
            "PLSM",
            causes=(TargetCause.ACTIVE,),
            watched_set=state,
        )

    assert state == set()
    assert raised.value.gap.code == "sticky_resubscribe_send_indeterminate"
    assert raised.value.gap.causes == (TargetCause.ACTIVE,)


@pytest.mark.parametrize("failed_index", [1, 2, 3])
def test_l2_watch_failure_at_each_command_invalidates_connection_state(
    monkeypatch,
    failed_index: int,
) -> None:
    state = {"HELD", "OTHER"}
    calls: list[str] = []

    def fail_at_index(command: str) -> None:
        calls.append(command)
        if len(calls) == failed_index:
            raise OSError("dialect send failed")

    monkeypatch.setattr(depth_bridge, "_send", fail_at_index)
    with pytest.raises(SubscriptionConnectionIndeterminate) as raised:
        depth_bridge._try_watch_symbol(
            "VEEE",
            causes=(TargetCause.HINT, TargetCause.ELIGIBLE),
            watched_set=state,
        )

    assert state == set()
    assert calls == ["WOR,VEEE", "WPL,VEEE", "wVEEE"][:failed_index]
    assert raised.value.gap.code == "watch_send_indeterminate"
    assert raised.value.gap.causes == (TargetCause.HINT, TargetCause.ELIGIBLE)
    assert f"command_index={failed_index}/3" in str(raised.value.gap.detail)


@pytest.mark.parametrize("failed_index", [1, 2, 3])
def test_l2_unwatch_failure_at_each_command_invalidates_connection_state(
    monkeypatch,
    failed_index: int,
) -> None:
    state = {"VEEE", "OTHER"}
    calls: list[str] = []

    def fail_at_index(command: str) -> None:
        calls.append(command)
        if len(calls) == failed_index:
            raise OSError("dialect send failed")

    monkeypatch.setattr(depth_bridge, "_send", fail_at_index)
    with pytest.raises(SubscriptionConnectionIndeterminate) as raised:
        depth_bridge._try_unwatch_symbol("VEEE", watched_set=state)

    assert state == set()
    assert calls == ["ROR,VEEE", "RPL,VEEE", "rVEEE"][:failed_index]
    assert raised.value.gap.code == "unwatch_send_indeterminate"
    assert f"command_index={failed_index}/3" in str(raised.value.gap.detail)


@pytest.mark.parametrize("failed_index", [1, 2, 3])
def test_l2_sticky_failure_at_each_command_invalidates_connection_state(
    monkeypatch,
    failed_index: int,
) -> None:
    state = {"VEEE", "OTHER"}
    calls: list[str] = []

    def fail_at_index(command: str) -> None:
        calls.append(command)
        if len(calls) == failed_index:
            raise OSError("dialect send failed")

    monkeypatch.setattr(depth_bridge, "_send", fail_at_index)
    with pytest.raises(SubscriptionConnectionIndeterminate) as raised:
        depth_bridge._try_sticky_resubscribe_symbol(
            "VEEE",
            causes=(TargetCause.ACTIVE,),
            watched_set=state,
        )

    assert state == set()
    assert calls == ["WOR,VEEE", "WPL,VEEE", "wVEEE"][:failed_index]
    assert raised.value.gap.code == "sticky_resubscribe_send_indeterminate"
    assert raised.value.gap.causes == (TargetCause.ACTIVE,)


def test_l1_connection_wrapper_closes_generation_after_indeterminate_send(
    monkeypatch,
) -> None:
    class BlockingSocket:
        def __init__(self) -> None:
            self.closed = trade_bridge.threading.Event()

        def settimeout(self, _timeout) -> None:
            return None

        def recv(self, _size: int) -> bytes:
            assert self.closed.wait(timeout=2.0)
            return b""

        def shutdown(self, _how) -> None:
            self.closed.set()

        def close(self) -> None:
            self.closed.set()

    connection = BlockingSocket()

    def fail_subscription_send(_socket, command: str) -> None:
        if command.startswith("S,"):
            return
        raise OSError("ambiguous send")

    def writer(_forced, _deadline, socket_obj, _stop, _generation) -> None:
        trade_bridge.watched.update({"PLSM", "OTHER"})
        trade_bridge._try_watch_symbol(socket_obj, "VEEE")

    monkeypatch.setattr(
        trade_bridge.socket,
        "create_connection",
        lambda *_args, **_kwargs: connection,
    )
    monkeypatch.setattr(
        trade_bridge,
        "_record_capture_connection_boundary",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        trade_bridge,
        "_wait_for_protocol_ack",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        trade_bridge,
        "_wait_for_selected_fields_ack",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(trade_bridge, "_send", fail_subscription_send)
    monkeypatch.setattr(trade_bridge, "writer", writer)

    with pytest.raises(SubscriptionConnectionIndeterminate):
        trade_bridge._run_connection(set(), None)

    assert connection.closed.is_set()
    assert trade_bridge.watched == set()


def test_l2_connection_wrapper_closes_generation_after_indeterminate_send(
    monkeypatch,
) -> None:
    class BlockingSocket:
        def __init__(self) -> None:
            self.closed = depth_bridge.threading.Event()

        def settimeout(self, _timeout) -> None:
            return None

        def recv(self, _size: int) -> bytes:
            assert self.closed.wait(timeout=2.0)
            return b""

        def shutdown(self, _how) -> None:
            self.closed.set()

        def close(self) -> None:
            self.closed.set()

    connection = BlockingSocket()
    command_count = {"value": 0}

    def fail_second_subscription_command(command: str) -> None:
        if command.startswith("S,"):
            return
        command_count["value"] += 1
        if command_count["value"] == 2:
            raise OSError("ambiguous send")

    def writer(_forced, _deadline, _stop) -> None:
        depth_bridge.watched.update({"PLSM", "OTHER"})
        depth_bridge._try_watch_symbol("VEEE")

    monkeypatch.setattr(
        depth_bridge.socket,
        "create_connection",
        lambda *_args, **_kwargs: connection,
    )
    monkeypatch.setattr(
        depth_bridge,
        "_record_capture_connection_boundary",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(depth_bridge, "_send", fail_second_subscription_command)
    monkeypatch.setattr(depth_bridge, "writer", writer)

    with pytest.raises(SubscriptionConnectionIndeterminate):
        depth_bridge._run_connection(set(), None)

    assert connection.closed.is_set()
    assert depth_bridge.sock is None
    assert depth_bridge.watched == set()


def test_depth_snapshot_retains_price_ladders() -> None:
    book = depth_bridge.Book()
    book.update("ARCX", "B", 3.0, 100)
    book.update("BATS", "B", 2.99, 200)
    book.update("ARCX", "A", 3.01, 150)
    book.update("BATS", "A", 3.02, 250)

    snapshot = book.snapshot()

    assert snapshot is not None
    assert snapshot["bids_json"] == [[3.0, 100], [2.99, 200]]
    assert snapshot["asks_json"] == [[3.01, 150], [3.02, 250]]


def test_lifecycle_record_is_content_addressed_and_never_claims_ack_or_fidelity() -> None:
    at = datetime(2026, 7, 13, 12, 2, 33, tzinfo=timezone.utc)
    record = SubscriptionLifecycleRecord.create(
        feed="iqfeed_l2",
        run_id="run-1",
        generation=4,
        stage=LifecycleStage.ACK_UNAVAILABLE,
        symbol="PLSM",
        causes=(TargetCause.ELIGIBLE, TargetCause.ROSS),
        parent_hashes=("sha256:parent",),
        recorded_at=at,
        sent_at=at,
        build_id="build-sha",
        config_hash="config-sha",
        detail_code="provider_has_no_symbol_ack",
        coverage_state="coverage_unavailable",
    )
    payload = record.canonical_dict()
    content_id = payload.pop("content_id")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    assert content_id == f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    assert record.provider_ack_state == "unavailable"
    assert record.fidelity_claim == "not_certified"
    assert record.coverage_state == "coverage_unavailable"
    assert record.parent_hashes == ("sha256:parent",)
