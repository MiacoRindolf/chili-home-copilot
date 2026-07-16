"""Replay v3 P0 — sim-clock ContextVar on ``_utcnow`` + MockBrokerAdapter skeleton.

These tests pin the TWO independently-testable, provably-inert P0 pieces:

  P0a — the simulated clock chokepoint (``live_runner._utcnow`` + ``replay_clock``):
        prod (no sim clock) is BYTE-IDENTICAL to ``datetime.utcnow()``; the ContextVar
        injects/nests/auto-resets (on normal exit AND on exception) so a frozen clock can
        never leak into a real lane.

  P0b — ``MockBrokerAdapter`` conforms to the ``VenueAdapter`` protocol, fills
        deterministically at a recorded NBBO via the pure paper-fill math, and rejects a
        no-bbo place.

Pure (no DB) — see docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §4 (P0).
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import inspect
import socket
import textwrap
import time
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import replay_v3 as rv3
from app.services.trading.momentum_neural import risk_evaluator as re
from app.services.trading.momentum_neural import risk_policy as rp
from app.services.trading.momentum_neural.replay_mock_broker import (
    MockBrokerAdapter,
    RecordedQuote,
    make_mock_broker_factory,
)
from app.services.trading.venue.protocol import (
    FreshnessMeta,
    NormalizedOrder,
    NormalizedTicker,
    VenueAdapter,
)

# A fixed sim instant (naive-UTC, the prod ``_utcnow`` shape).
_T = datetime(2026, 6, 29, 13, 30, 0)


# ── P0a: the simulated clock ─────────────────────────────────────────────────────
def test_utcnow_no_sim_clock_is_real_now_naive_utc():
    """With NO sim clock (prod ALWAYS), ``_utcnow`` returns the real ``datetime.utcnow()``:
    naive (tz-unaware), within a hair of the real wall clock — the byte-identical path."""
    assert lr._SIM_NOW.get() is None  # default unset
    before = datetime.utcnow()
    got = lr._utcnow()
    after = datetime.utcnow()
    assert got.tzinfo is None  # naive UTC, exactly as datetime.utcnow()
    assert before <= got <= after  # same real clock, same code path


def test_utcnow_no_sim_clock_matches_datetime_utcnow_identity(monkeypatch):
    """Prove the no-sim-clock branch is LITERALLY ``datetime.utcnow()`` — patch utcnow to a
    sentinel and confirm ``_utcnow`` returns it unchanged when the ContextVar is unset."""
    sentinel = datetime(2000, 1, 2, 3, 4, 5)

    class _FrozenDT(datetime):
        @classmethod
        def utcnow(cls):  # type: ignore[override]
            return sentinel

    monkeypatch.setattr(lr, "datetime", _FrozenDT)
    assert lr._SIM_NOW.get() is None
    assert lr._utcnow() == sentinel


def test_replay_clock_injects_sim_time():
    with lr.replay_clock(_T):
        assert lr._utcnow() == _T
        # the aware-UTC + ET helpers DERIVE from the same chokepoint
        assert lr._utcnow_aware() == _T.replace(tzinfo=timezone.utc)
        assert lr._now_in_tz(timezone.utc).replace(tzinfo=None) == _T
    # auto-reset after the block
    assert lr._SIM_NOW.get() is None


def test_replay_clock_tz_aware_input_normalized_to_naive_utc():
    aware = datetime(2026, 6, 29, 13, 30, 0, tzinfo=timezone.utc)
    with lr.replay_clock(aware):
        got = lr._utcnow()
        assert got.tzinfo is None
        assert got == _T


def test_replay_clock_nests_and_restores_outer():
    outer = datetime(2026, 6, 29, 13, 0, 0)
    inner = datetime(2026, 6, 29, 14, 0, 0)
    with lr.replay_clock(outer):
        assert lr._utcnow() == outer
        with lr.replay_clock(inner):
            assert lr._utcnow() == inner
        # inner exit restores OUTER, not None
        assert lr._utcnow() == outer
    assert lr._SIM_NOW.get() is None


def test_replay_clock_resets_on_exception():
    with pytest.raises(RuntimeError):
        with lr.replay_clock(_T):
            assert lr._utcnow() == _T
            raise RuntimeError("boom")
    # the finally-block restored the prior (None) value despite the exception
    assert lr._SIM_NOW.get() is None
    # and prod is back to real now
    assert lr._utcnow().tzinfo is None
    assert abs((datetime.utcnow() - lr._utcnow()).total_seconds()) < 2.0


def test_set_reset_sim_clock_token_roundtrip():
    token = lr.set_sim_clock(_T)
    try:
        assert lr._utcnow() == _T
    finally:
        lr.reset_sim_clock(token)
    assert lr._SIM_NOW.get() is None


def test_replay_clock_governs_lease_expiry_and_broker_clock_truth():
    future = _T.replace(tzinfo=timezone.utc) + timedelta(hours=1)
    past = _T.replace(tzinfo=timezone.utc) - timedelta(seconds=1)

    class _ClockAdapter:
        def get_market_clock_snapshot(self):
            return {
                "ok": True,
                "paper": True,
                "is_open": True,
                "timestamp": _T.replace(tzinfo=timezone.utc).isoformat(),
                "next_close": future.isoformat(),
            }

    with lr.replay_clock(_T):
        assert lr._claim_lease_elapsed(future.isoformat()) is False
        assert lr._claim_lease_elapsed(past.isoformat()) is True
        assert lr._owner_transport_lease_expired(
            {"lease_expires_at_utc": future.isoformat()}
        ) is False
        clock, detail = lr._strict_alpaca_clock_truth(_ClockAdapter())
        assert clock is not None
        assert detail["broker_clock_ok"] is True
        assert clock["_clock_age_seconds"] == pytest.approx(0.0)


def test_replay_l2_boundary_is_present_only_under_sim_clock():
    assert lr._replay_l2_as_of_or_none() is None
    with lr.replay_clock(_T):
        assert lr._replay_l2_as_of_or_none() == _T
    assert lr._replay_l2_as_of_or_none() is None


def test_decision_runtime_state_nests_and_resets_on_exception():
    process_state = lr._PROCESS_DECISION_RUNTIME_STATE
    outer = lr.DecisionRuntimeState(clock_domain="replay_utc")
    inner = lr.DecisionRuntimeState(clock_domain="replay_utc")
    assert lr._current_decision_runtime_state() is process_state

    with pytest.raises(RuntimeError, match="boom"):
        with lr.decision_runtime_state(outer):
            assert lr._current_decision_runtime_state() is outer
            with lr.decision_runtime_state(inner):
                assert lr._current_decision_runtime_state() is inner
            assert lr._current_decision_runtime_state() is outer
            raise RuntimeError("boom")

    assert lr._current_decision_runtime_state() is process_state


def test_decision_runtime_state_uses_replay_clock_and_isolates_all_three_maps():
    process_daily = dict(lr._PROCESS_DECISION_RUNTIME_STATE.daily_ctx_cache)
    process_frontside = list(lr._PROCESS_DECISION_RUNTIME_STATE.frontside_dist)
    process_a4 = dict(lr._PROCESS_DECISION_RUNTIME_STATE.a4_rescore_last)
    first = lr.DecisionRuntimeState(clock_domain="replay_utc")
    second = lr.DecisionRuntimeState(clock_domain="replay_utc")
    sentinel = object()

    with lr.replay_clock(_T), lr.decision_runtime_state(first):
        key = f"TEST|{_T:%Y%m%d}"
        first.daily_ctx_cache[key] = sentinel
        first.a4_rescore_last["TEST"] = lr._decision_runtime_clock_seconds()
        lr._frontside_dist_note(0.7)
        assert lr._daily_ctx_cached("TEST") is sentinel
        assert first.frontside_dist == [
            (_T.replace(tzinfo=timezone.utc).timestamp(), 0.7)
        ]
        assert first.a4_rescore_last["TEST"] == pytest.approx(
            _T.replace(tzinfo=timezone.utc).timestamp()
        )

    with lr.replay_clock(_T), lr.decision_runtime_state(second):
        assert second.daily_ctx_cache == {}
        assert second.frontside_dist == []
        assert second.a4_rescore_last == {}

    assert lr._PROCESS_DECISION_RUNTIME_STATE.daily_ctx_cache == process_daily
    assert lr._PROCESS_DECISION_RUNTIME_STATE.frontside_dist == process_frontside
    assert lr._PROCESS_DECISION_RUNTIME_STATE.a4_rescore_last == process_a4


def test_runtime_state_consumers_do_not_read_raw_process_aliases():
    cases = (
        (lr._daily_ctx_cached, "_DAILY_CTX_CACHE"),
        (lr._frontside_dist_note, "_FRONTSIDE_DIST"),
        (lr._frontside_adaptive_thresholds, "_FRONTSIDE_DIST"),
        (lr._maybe_rescore_eligibility_block, "_A4_RESCORE_LAST"),
    )
    for fn, forbidden_name in cases:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        referenced_names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        assert "_current_decision_runtime_state" in referenced_names
        assert forbidden_name not in referenced_names


def test_replay_ross_snapshot_refuses_warm_process_cache_before_provider(monkeypatch):
    from app.services import massive_client

    warm_rows = {"CLRO": {"ticker": "CLRO", "price": 99.0}}
    provider_calls = []
    monkeypatch.setattr(re, "_ROSS_RISK_SNAPSHOT_ROWS", warm_rows)
    monkeypatch.setattr(re, "_ROSS_RISK_SNAPSHOT_TS", 10**12)

    def _forbidden_snapshot(*args, **kwargs):
        provider_calls.append((args, kwargs))
        raise AssertionError("live scanner provider reached during replay")

    monkeypatch.setattr(
        massive_client,
        "get_full_market_snapshot",
        _forbidden_snapshot,
    )

    with lr.replay_clock(_T):
        with pytest.raises(
            re.ReplayScannerSnapshotUnavailableError,
            match="scanner_snapshot input is unavailable",
        ):
            re._ross_risk_snapshot_rows()

    assert re._ROSS_RISK_SNAPSHOT_ROWS is warm_rows
    assert provider_calls == []


def test_live_ross_snapshot_refresh_failure_does_not_return_expired_rows(monkeypatch):
    from app.services import massive_client

    monkeypatch.setattr(
        re,
        "_ROSS_RISK_SNAPSHOT_ROWS",
        {"STALE": {"ticker": "STALE", "price": 10.0}},
    )
    monkeypatch.setattr(re, "_ROSS_RISK_SNAPSHOT_TS", -1.0)

    def _refresh_failed(**_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        massive_client,
        "get_full_market_snapshot",
        _refresh_failed,
    )

    assert re._ross_risk_snapshot_rows() == {}
    assert re._ROSS_RISK_SNAPSHOT_ROWS == {}
    assert re._ROSS_RISK_SNAPSHOT_TS == 0.0


def test_capture_required_live_scanner_failure_is_not_swallowed(monkeypatch):
    from app.services import massive_client

    rows = [{"ticker": "CLRO"}]
    monkeypatch.setattr(
        massive_client,
        "_snapshot_cache",
        (time.time(), rows),
    )

    class RejectingSink:
        def on_massive_full_snapshot(self, **_kwargs):
            return False

    with massive_client.massive_full_snapshot_capture_sink(RejectingSink()):
        with pytest.raises(
            re.ReplayScannerSnapshotUnavailableError,
            match="not durably receipted",
        ):
            re._ross_risk_snapshot_rows()


def test_ross_snapshot_has_no_second_local_ttl(monkeypatch):
    from app.services import massive_client

    calls = []

    def _provider_owns_ttl(**kwargs):
        calls.append(kwargs)
        return [{"ticker": "CLRO", "price": 4.0}]

    monkeypatch.setattr(
        massive_client,
        "get_full_market_snapshot",
        _provider_owns_ttl,
    )

    assert "CLRO" in re._ross_risk_snapshot_rows()
    assert "CLRO" in re._ross_risk_snapshot_rows()
    assert len(calls) == 2


def test_ross_universe_check_propagates_replay_snapshot_contract(monkeypatch):
    from app.services.trading.momentum_neural import universe

    monkeypatch.setattr(
        universe,
        "ross_smallcap_profile_evidence",
        lambda *_args, **_kwargs: (
            False,
            "ross_universe_missing_price",
            {},
        ),
    )

    def _unavailable():
        raise re.ReplayScannerSnapshotUnavailableError("scanner unavailable")

    monkeypatch.setattr(re, "_ross_risk_snapshot_rows", _unavailable)
    with pytest.raises(
        re.ReplayScannerSnapshotUnavailableError,
        match="scanner unavailable",
    ):
        re._ross_lane_universe_check("CLRO", None)


def test_replay_ross_universe_check_refuses_persisted_complete_signal_before_read(monkeypatch):
    from app.services.trading.momentum_neural import universe

    class ForbiddenViabilityRead:
        @property
        def execution_readiness_json(self):
            raise AssertionError("mutable viability payload read during replay")

    profile_calls = []

    def _would_pass_complete_signal(*args, **kwargs):
        profile_calls.append((args, kwargs))
        return True, "ross_universe_profile_ok", {"complete": True}

    monkeypatch.setattr(
        universe,
        "ross_smallcap_profile_evidence",
        _would_pass_complete_signal,
    )

    with lr.replay_clock(_T):
        with pytest.raises(
            re.ReplayScannerSnapshotUnavailableError,
            match="scanner_snapshot input is unavailable",
        ):
            re._ross_lane_universe_check("CLRO", ForbiddenViabilityRead())

    assert profile_calls == []


def test_direct_replay_risk_evaluator_rejects_scanner_before_any_mutable_read(
    monkeypatch,
):
    """A direct call cannot bypass the driver's scanner capability preflight."""

    from app.config import settings
    from app.services import massive_client

    touched: list[str] = []
    warm_rows = {"CLRO": {"ticker": "CLRO", "price": 99.0}}
    monkeypatch.setattr(re, "_ROSS_RISK_SNAPSHOT_ROWS", warm_rows)
    monkeypatch.setattr(re, "_ROSS_RISK_SNAPSHOT_TS", 10**12)
    monkeypatch.setattr(
        settings,
        "chili_momentum_ross_equity_universe_required",
        True,
        raising=False,
    )

    def _forbidden(name):
        def _boom(*_args, **_kwargs):
            touched.append(name)
            raise AssertionError(f"mutable replay read reached: {name}")

        return _boom

    class ForbiddenDb:
        query = _forbidden("db.query")

    monkeypatch.setattr(
        re,
        "get_kill_switch_status",
        _forbidden("get_kill_switch_status"),
    )
    monkeypatch.setattr(
        re,
        "is_kill_switch_active",
        _forbidden("is_kill_switch_active"),
    )
    monkeypatch.setattr(
        re,
        "resolve_execution_family_for_symbol",
        _forbidden("resolve_execution_family_for_symbol"),
    )
    monkeypatch.setattr(
        massive_client,
        "get_full_market_snapshot",
        _forbidden("get_full_market_snapshot"),
    )

    with monkeypatch.context() as replay_patch:
        replay_patch.setattr(
            re.MomentumAutomationRiskPolicy,
            "from_settings",
            _forbidden("risk_policy.from_settings"),
        )
        with lr.replay_clock(_T):
            with pytest.raises(
                re.ReplayScannerSnapshotUnavailableError,
                match="scanner_snapshot input is unavailable",
            ):
                re.evaluate_proposed_momentum_automation(
                    ForbiddenDb(),
                    user_id=7,
                    symbol="CLRO",
                    variant_id=1,
                    mode="live",
                    execution_family="robinhood_spot",
                )

    assert touched == []
    assert re._ROSS_RISK_SNAPSHOT_ROWS is warm_rows
    assert re._ROSS_RISK_SNAPSHOT_TS == 10**12

    # With no replay clock the new boundary is inert and the unchanged live
    # evaluator proceeds to its first governance read.
    with pytest.raises(AssertionError, match="get_kill_switch_status"):
        re.evaluate_proposed_momentum_automation(
            ForbiddenDb(),
            user_id=7,
            symbol="CLRO",
            variant_id=1,
            mode="live",
            execution_family="robinhood_spot",
        )
    assert touched == ["get_kill_switch_status"]


def test_diagnostic_driver_preflights_missing_scanner_before_eligibility_mutation(
    db,
    monkeypatch,
):
    from app.config import settings
    from app.models.trading import TradingAutomationSession

    frontier = datetime(2026, 7, 14, 13, 0, 0)
    arm = rv3.RecordedArm(
        symbol="CLRO",
        live_eligible_at_utc=(frontier - timedelta(seconds=30)).isoformat()
        + "+00:00",
    )
    seed = rv3.seed_replay_session(
        db,
        arm,
        execution_family="robinhood_spot",
    )
    db.flush()
    state_before = db.get(TradingAutomationSession, seed.session_id).state
    eligibility_calls = []

    class MutatingEligibility:
        def apply(self, *_args, **_kwargs):
            eligibility_calls.append(True)
            raise AssertionError("eligibility mutation reached before scanner preflight")

    monkeypatch.setattr(
        settings,
        "chili_momentum_ross_equity_universe_required",
        True,
        raising=False,
    )
    driver = rv3.ReplayV3Driver(
        db,
        seed,
        mock=MockBrokerAdapter(freshness_mode="wall"),
        ohlcv_provider=rv3.RecordedOhlcvProvider(
            {"15m": rv3.synthetic_uptrend_ohlcv()}
        ),
        grid=[
            rv3.RecordedNbboTick(
                ts=frontier,
                bid=3.99,
                ask=4.01,
                last=4.00,
            )
        ],
        risk_gate_allows=None,
        eligibility=MutatingEligibility(),
    )

    with pytest.raises(
        re.ReplayScannerSnapshotUnavailableError,
        match="scanner_snapshot input is unavailable",
    ):
        driver.run()

    with pytest.raises(
        re.ReplayScannerSnapshotUnavailableError,
        match="scanner_snapshot input is unavailable",
    ):
        driver.step(frontier, None)

    db.expire_all()
    assert eligibility_calls == []
    assert db.get(TradingAutomationSession, seed.session_id).state == state_before


def test_replay_driver_allocates_one_fresh_runtime_state_per_run(monkeypatch):
    observed: list[lr.DecisionRuntimeState] = []

    def fake_bound_run(self):
        state = lr._current_decision_runtime_state()
        state.a4_rescore_last["RUN"] = float(len(observed) + 1)
        observed.append(state)
        return state

    monkeypatch.setattr(
        rv3.ReplayV3Driver,
        "_run_with_bound_decision_runtime_state",
        fake_bound_run,
    )
    driver = object.__new__(rv3.ReplayV3Driver)
    driver.sealed_inputs = None

    first = driver.run()
    second = driver.run()

    assert first is observed[0]
    assert second is observed[1]
    assert first is not second
    assert first.a4_rescore_last == {"RUN": 1.0}
    assert second.a4_rescore_last == {"RUN": 2.0}
    assert lr._current_decision_runtime_state() is lr._PROCESS_DECISION_RUNTIME_STATE


def test_direct_driver_steps_share_only_their_own_driver_state(monkeypatch):
    observed: list[tuple[lr.DecisionRuntimeState, int]] = []

    def fake_step(self, _t, _quote):
        state = lr._current_decision_runtime_state()
        count = int(state.a4_rescore_last.get("RUN", 0.0)) + 1
        state.a4_rescore_last["RUN"] = float(count)
        observed.append((state, count))
        return count

    monkeypatch.setattr(
        rv3.ReplayV3Driver,
        "_step_with_bound_decision_runtime_state",
        fake_step,
    )
    first_driver = object.__new__(rv3.ReplayV3Driver)
    first_driver._decision_runtime_state = lr.DecisionRuntimeState(
        clock_domain="replay_utc"
    )
    first_driver.sealed_inputs = None
    first_driver._sealed_run_active = False
    first_driver._run_network_guard_active = True
    second_driver = object.__new__(rv3.ReplayV3Driver)
    second_driver._decision_runtime_state = lr.DecisionRuntimeState(
        clock_domain="replay_utc"
    )
    second_driver.sealed_inputs = None
    second_driver._sealed_run_active = False
    second_driver._run_network_guard_active = True

    assert first_driver.step(_T, None) == 1
    assert first_driver.step(_T + timedelta(seconds=1), None) == 2
    assert second_driver.step(_T, None) == 1

    assert observed[0][0] is observed[1][0]
    assert observed[2][0] is not observed[0][0]
    assert lr._current_decision_runtime_state() is lr._PROCESS_DECISION_RUNTIME_STATE


def test_sealed_capability_gate_precedes_runtime_state_binding(monkeypatch):
    class UnavailableInputs:
        @staticmethod
        def assert_runtime_input_capabilities():
            raise LookupError("sealed inputs unavailable")

    def forbidden_binding(_state):
        raise AssertionError("runtime state bound before sealed capability gate")

    monkeypatch.setattr(lr, "decision_runtime_state", forbidden_binding)
    driver = object.__new__(rv3.ReplayV3Driver)
    driver.sealed_inputs = UnavailableInputs()

    with pytest.raises(LookupError, match="sealed inputs unavailable"):
        driver.run()


def test_direct_sealed_step_cannot_bypass_run_capability_preflight():
    driver = object.__new__(rv3.ReplayV3Driver)
    driver.sealed_inputs = object()
    driver._sealed_run_active = False
    driver._decision_runtime_state = lr.DecisionRuntimeState(
        clock_domain="replay_utc"
    )

    with pytest.raises(
        rv3.SealedReplayInputError,
        match=r"step requires run\(\) capability preflight",
    ):
        driver.step(_T, None)


def test_diagnostic_driver_detects_swallowed_network_attempt():
    class _Bind:
        url = SimpleNamespace(host="", port=None)

    class _Query:
        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def all(self):
            return []

    class _DB:
        @staticmethod
        def get_bind():
            return _Bind()

        @staticmethod
        def query(*_args, **_kwargs):
            return _Query()

    class _Mock:
        @staticmethod
        def get_fills(*_args, **_kwargs):
            return [], None

    driver = object.__new__(rv3.ReplayV3Driver)
    driver.db = _DB()
    driver.seed = SimpleNamespace(
        economic_seed_mode="legacy_config_diagnostic",
        session_id=1,
    )
    driver.mock = _Mock()
    driver.sealed_inputs = None
    driver.grid = [SimpleNamespace(ts=_T, as_quote=lambda: None)]
    # This fixture exercises network fencing, not the genuine entry risk gate.
    driver.risk_gate_allows = True
    driver.eligibility = None
    driver._python_network_attempt_count = 0
    driver._reproduced_decision_output_sha256s = []
    driver._broker_lifecycle_architectural_blockers = []
    driver._state = lambda: "watching_live"

    def _swallowed_attempt(_t, _quote):
        try:
            socket.create_connection(("203.0.113.1", 443), timeout=0.01)
        except rv3.ReplayNetworkAccessError:
            pass
        return rv3.TickTrace(
            ts=_T,
            state_before="watching_live",
            state_after="watching_live",
            result={},
        )

    driver.step = _swallowed_attempt

    with pytest.raises(
        rv3.ReplayNetworkAccessError,
        match="swallowed a forbidden network attempt",
    ):
        driver.run()
    assert driver.python_network_attempt_count == 1


def test_direct_diagnostic_step_detects_swallowed_network_attempt():
    class _Bind:
        url = SimpleNamespace(host="", port=None)

    class _DB:
        @staticmethod
        def get_bind():
            return _Bind()

    driver = object.__new__(rv3.ReplayV3Driver)
    driver.db = _DB()
    driver.sealed_inputs = None
    driver._sealed_run_active = False
    driver._run_network_guard_active = False
    driver._python_network_attempt_count = 0
    driver._decision_runtime_state = lr.DecisionRuntimeState(
        clock_domain="replay_utc"
    )

    def _swallowed_attempt(_t, _quote):
        try:
            socket.create_connection(("203.0.113.1", 443), timeout=0.01)
        except rv3.ReplayNetworkAccessError:
            pass
        return rv3.TickTrace(
            ts=_T,
            state_before="watching_live",
            state_after="watching_live",
            result={},
        )

    driver._step_with_bound_decision_runtime_state = _swallowed_attempt

    with pytest.raises(
        rv3.ReplayNetworkAccessError,
        match="direct diagnostic ReplayV3 step swallowed",
    ):
        driver.step(_T, None)
    assert driver.python_network_attempt_count == 1


def test_replay_reachable_detector_and_l2_calls_thread_explicit_boundaries():
    """Tripwire for live-FSM calls that previously fell back to wall/DB now()."""

    tree = ast.parse(textwrap.dedent(inspect.getsource(lr.tick_live_session)))
    required_keywords = {
        "momentum_pullback_trigger": {"now", "l2_as_of"},
        "micro_pullback_primary_confirmation": {"now", "l2_as_of"},
        "hod_break_confirmation": {"now", "l2_as_of"},
        "blue_sky_break_confirmation": {"now", "l2_as_of"},
        # The Batch-C loop dispatches ABCD/double-bottom/IHS/cup-and-handle
        # through one local callable; every member accepts the same boundary.
        "_bc_fn": {"now", "l2_as_of"},
        "_l2_entry_confirm": {"l2_as_of"},
        "read_ladder_distribution": {"as_of"},
    }
    seen = {name: 0 for name in required_keywords}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        expected = required_keywords.get(name)
        if expected is None:
            continue
        seen[name] += 1
        actual = {keyword.arg for keyword in node.keywords if keyword.arg}
        assert expected <= actual, (name, expected - actual, node.lineno)

    assert seen == {
        "momentum_pullback_trigger": 1,
        "micro_pullback_primary_confirmation": 1,
        "hod_break_confirmation": 1,
        "blue_sky_break_confirmation": 1,
        "_bc_fn": 1,
        "_l2_entry_confirm": 1,
        "read_ladder_distribution": 2,
    }


@pytest.mark.parametrize(
    ("as_of", "expected_hours"),
    (
        (datetime(2026, 3, 8, 14, 0, tzinfo=timezone.utc), 23),
        (datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc), 25),
    ),
)
def test_replay_risk_clock_controls_true_et_day_bounds(as_of, expected_hours):
    with rp.replay_risk_clock(as_of):
        start, end = rp._et_day_bounds_utc(days_ago=0)
    assert end - start == timedelta(hours=expected_hours)


class _ReplayRows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _ReplayReadDb:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), dict(params)))
        return _ReplayRows(self.rows)


def test_time_of_day_history_is_prefix_bounded_and_replay_cache_isolated(monkeypatch):
    """A later replay run cannot seed a wall-TTL cache observed by an earlier run."""

    monkeypatch.setattr(
        rp.settings, "chili_momentum_time_of_day_risk_enabled", True, raising=False
    )
    rp._TOD_CACHE.clear()
    db = _ReplayReadDb()
    a = datetime(2026, 6, 29, 13, 30)
    b = datetime(2026, 6, 29, 15, 30)
    for frontier in (a, b, a):
        with rp.replay_risk_clock(frontier):
            rp.time_of_day_risk_multiplier(db, now_et_hour_frac=9.5)

    assert len(db.calls) == 3
    assert rp._TOD_CACHE == {}
    assert [params["as_of_utc"] for _, params in db.calls] == [a, b, a]
    assert all("ts <= :as_of_utc" in sql for sql, _ in db.calls)


def test_recent_mfe_samples_are_prefix_bounded_by_replay_clock(monkeypatch):
    monkeypatch.setattr(
        lr.settings, "chili_momentum_mfe_samples_epoch", "", raising=False
    )
    db = _ReplayReadDb(
        [({"setup_family": "micro_pullback", "mfe_r": 2.5},)]
    )
    frontier = datetime(2026, 6, 29, 13, 45)
    with lr.replay_clock(frontier):
        samples = lr._recent_mfe_samples(db, "micro_pullback", limit=10)

    assert samples == [2.5]
    assert len(db.calls) == 1
    sql, params = db.calls[0]
    assert "ts <= :as_of_utc" in sql
    assert params["as_of_utc"] == frontier


def test_alpaca_final_breaker_threads_one_exact_phase_frontier(monkeypatch):
    """Each order phase gets one instant shared by all local financial ledgers."""

    from types import SimpleNamespace

    from app.services.trading import governance, portfolio_risk

    phase_at = datetime(2026, 6, 29, 13, 46, tzinfo=timezone.utc)
    db = object()
    seen = []

    monkeypatch.setattr(lr, "_utcnow_aware", lambda: phase_at)
    monkeypatch.setattr(lr, "object_session", lambda _sess: db)
    monkeypatch.setattr(governance, "get_kill_switch_status", lambda: {"active": False})
    monkeypatch.setattr(governance, "kill_switch_halts_new_entries", lambda: False)
    monkeypatch.setattr(
        lr,
        "_fresh_alpaca_broker_daily_loss_admission",
        lambda _sess: (
            True,
            {
                "family": "alpaca_spot",
                "realized": 0.0,
                "cap": 100.0,
                "broker_snapshot_cache_bypassed": True,
                "transient": False,
            },
        ),
    )
    monkeypatch.setattr(rp, "equity_relative_daily_loss_cap", lambda *_a, **_k: 100.0)
    monkeypatch.setattr(
        re,
        "_daily_realized_pnl",
        lambda *_a, **kw: seen.append(("daily", kw["as_of_utc"])) or 0.0,
    )
    monkeypatch.setattr(
        re,
        "evaluate_profit_giveback_halt",
        lambda *_a, **kw: seen.append(("giveback", kw["as_of_utc"]))
        or {"halted": False},
    )
    monkeypatch.setattr(
        re,
        "evaluate_green_to_red_halt",
        lambda *_a, **kw: seen.append(("green_to_red", kw["as_of_utc"]))
        or {"halted": False},
    )
    monkeypatch.setattr(
        portfolio_risk, "check_portfolio_drawdown_breaker", lambda *_a, **_k: (False, None)
    )
    monkeypatch.setattr(
        lr.settings,
        "chili_momentum_risk_daily_loss_fraction_of_equity",
        0.01,
        raising=False,
    )
    sess = SimpleNamespace(user_id=7, execution_family="alpaca_spot")

    allowed, evidence = lr._final_alpaca_financial_breaker_admission(
        sess, phase="pre_reservation"
    )

    assert allowed is True
    assert evidence["checked_at_utc"] == phase_at.isoformat()
    assert seen == [
        ("daily", phase_at),
        ("giveback", phase_at),
        ("green_to_red", phase_at),
    ]


def test_exit_history_reads_are_prefix_bounded_and_replay_cache_isolated(monkeypatch):
    monkeypatch.setattr(
        lr.settings, "chili_momentum_smart_hold_time_floor_min_samples", 1, raising=False
    )
    lr._SCALP_HOLD_CACHE.clear()
    lr._BREAK_RESOLUTION_CACHE.clear()
    db = _ReplayReadDb([(10.0,), (20.0,), (30.0,)])
    a = datetime(2026, 6, 29, 13, 30)
    b = datetime(2026, 6, 29, 15, 30)
    for frontier in (a, b, a):
        with lr.replay_clock(frontier):
            assert lr._recent_scalp_median_hold_s(db, 7) == 20.0
            assert lr._smart_hold_time_floor_s(db, 7) is not None

    assert len(db.calls) == 6
    assert lr._SCALP_HOLD_CACHE == {}
    assert lr._BREAK_RESOLUTION_CACHE == {}
    assert [params["as_of_utc"] for _, params in db.calls] == [a, a, b, b, a, a]
    assert all("terminal_at <= :as_of_utc" in sql for sql, _ in db.calls)
    assert all("terminal_at DESC, id DESC" in sql for sql, _ in db.calls)


class _PerUserHistoryDb:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), dict(params)))
        value = 10.0 if int(params["u"]) == 1 else 100.0
        return _ReplayRows([(value,)])


def test_live_exit_caches_are_per_user_and_bounded(monkeypatch):
    from app.services.trading import governance

    assert lr._SIM_NOW.get() is None
    lr._SCALP_HOLD_CACHE.clear()
    lr._DAY_PNL_CACHE.clear()
    db = _PerUserHistoryDb()

    assert lr._recent_scalp_median_hold_s(db, 1) == 10.0
    assert lr._recent_scalp_median_hold_s(db, 2) == 100.0
    # Same-user repeats use their own cache, not the other user's value.
    assert lr._recent_scalp_median_hold_s(db, 1) == 10.0
    assert len(db.calls) == 2

    pnl_calls = []
    monkeypatch.setattr(
        governance,
        "global_realized_pnl_today_et",
        lambda _db, uid, **_kw: pnl_calls.append(uid) or {"total_usd": float(uid)},
    )
    assert lr._day_realized_usd_cached(db, 1) == 1.0
    assert lr._day_realized_usd_cached(db, 2) == 2.0
    assert lr._day_realized_usd_cached(db, 1) == 1.0
    assert pnl_calls == [1, 2]

    bounded = {}
    for user_id in range(lr._EXIT_DECISION_CACHE_MAX_USERS + 1):
        lr._bounded_exit_cache_put(
            bounded,
            user_id=user_id,
            at=float(user_id),
            generation=user_id,
            value=float(user_id),
        )
    assert len(bounded) == lr._EXIT_DECISION_CACHE_MAX_USERS
    assert 0 not in bounded


def test_exit_cache_bound_is_thread_safe():
    from concurrent.futures import ThreadPoolExecutor

    cache = {}

    def _write(user_id):
        lr._bounded_exit_cache_put(
            cache,
            user_id=user_id,
            at=float(user_id),
            generation=user_id,
            value=float(user_id),
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(_write, range(2048)))

    assert len(cache) == lr._EXIT_DECISION_CACHE_MAX_USERS
    assert all(isinstance(row, dict) and "at" in row and "v" in row for row in cache.values())


def test_exit_cache_rejects_late_older_generation_overwrite():
    cache = {}
    lr._bounded_exit_cache_put(
        cache, user_id=7, at=2.0, generation=2, value="newer"
    )
    lr._bounded_exit_cache_put(
        cache, user_id=7, at=1.0, generation=1, value="older_finished_late"
    )
    assert cache[(7, "*", "live")] == {
        "at": 2.0,
        "generation": 2,
        "v": "newer",
    }


class _ScopedHistoryDb:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), dict(params)))
        value = 10.0 if params.get("execution_family") == "coinbase_spot" else 90.0
        return _ReplayRows([(value,)])


def test_live_hold_history_is_scoped_by_mode_and_execution_family():
    lr._SCALP_HOLD_CACHE.clear()
    db = _ScopedHistoryDb()

    coinbase = lr._recent_scalp_median_hold_s(
        db, 7, execution_family="coinbase_spot", mode="live"
    )
    robinhood = lr._recent_scalp_median_hold_s(
        db, 7, execution_family="robinhood_spot", mode="live"
    )

    assert coinbase == 10.0
    assert robinhood == 90.0
    assert len(db.calls) == 2
    assert all("mode = :mode" in sql for sql, _params in db.calls)
    assert all("execution_family = :execution_family" in sql for sql, _params in db.calls)


# ── P0b: MockBrokerAdapter ───────────────────────────────────────────────────────
def test_mock_broker_conforms_to_venue_adapter_protocol():
    m = MockBrokerAdapter()
    assert isinstance(m, VenueAdapter)  # @runtime_checkable structural conformance
    # the methods the runner calls per tick exist and are callable
    for name in (
        "is_enabled",
        "get_best_bid_ask",
        "get_ticker",
        "get_product",
        "get_order",
        "place_market_order",
        "place_limit_order_gtc",
        "cancel_order",
        "get_account_snapshot",
    ):
        assert callable(getattr(m, name)), name


def test_mock_broker_is_enabled_default():
    assert MockBrokerAdapter().is_enabled() is True
    assert MockBrokerAdapter(enabled=False).is_enabled() is False


def test_mock_broker_bbo_from_injected_quote_stamped_at_sim_clock():
    m = MockBrokerAdapter()
    m.set_clock(_T)
    m.set_quote("UPC", RecordedQuote(bid=10.0, ask=10.04, last=10.02))
    ticker, fresh = m.get_best_bid_ask("UPC")
    assert isinstance(ticker, NormalizedTicker)
    assert isinstance(fresh, FreshnessMeta)
    assert ticker.bid == 10.0 and ticker.ask == 10.04
    assert ticker.mid == pytest.approx(10.02)
    # freshness stamped at the sim clock (sim-to-sim comparison in the runner)
    assert fresh.retrieved_at_utc.replace(tzinfo=None) == _T


def test_mock_broker_deterministic_entry_fill_at_recorded_ask():
    """A long buy crosses the recorded ASK (zero slippage default) via the pure paper math."""
    m = MockBrokerAdapter()
    m.set_clock(_T)
    m.set_quote("UPC", RecordedQuote(bid=10.0, ask=10.04))
    r = m.place_market_order(product_id="UPC", side="buy", base_size="100")
    assert r["ok"] is True
    assert r["raw"]["fill_price"] == pytest.approx(10.04)
    assert r["raw"]["filled_size"] == pytest.approx(100.0)
    # the resting order resolves to a terminal FILLED with the same fill
    o, _ = m.get_order(r["order_id"])
    assert isinstance(o, NormalizedOrder)
    assert o.status == "filled"
    assert o.filled_size == pytest.approx(100.0)
    assert o.average_filled_price == pytest.approx(10.04)


def test_mock_broker_exit_fill_crosses_bid():
    m = MockBrokerAdapter()
    m.set_clock(_T)
    m.set_quote("UPC", RecordedQuote(bid=10.0, ask=10.04))
    r = m.place_market_order(product_id="UPC", side="sell", base_size="100")
    assert r["ok"] is True
    assert r["raw"]["fill_price"] == pytest.approx(10.0)  # crosses the bid


def test_mock_broker_slippage_applied_symmetrically():
    """Non-zero slippage widens the entry (above ask) and the exit (below bid)."""
    m = MockBrokerAdapter(slippage_bps=10.0)  # 10 bps of mid
    m.set_clock(_T)
    m.set_quote("UPC", RecordedQuote(bid=10.0, ask=10.04))
    mid = 10.02
    slip = mid * 10.0 / 10_000.0
    buy = m.place_market_order(product_id="UPC", side="buy", base_size="1")
    sell = m.place_market_order(product_id="UPC", side="sell", base_size="1")
    assert buy["raw"]["fill_price"] == pytest.approx(10.04 + slip)
    assert sell["raw"]["fill_price"] == pytest.approx(10.0 - slip)


def test_mock_broker_no_bbo_returns_none_and_rejects_place():
    m = MockBrokerAdapter()
    m.set_clock(_T)
    # no quote injected for this product ⇒ no_bbo
    ticker, fresh = m.get_best_bid_ask("RVMDW")
    assert ticker is None
    assert isinstance(fresh, FreshnessMeta)
    r = m.place_market_order(product_id="RVMDW", side="buy", base_size="100")
    assert r["ok"] is False
    assert r["error"] == "no_bbo"


def test_mock_broker_clear_quote_reverts_to_no_bbo():
    m = MockBrokerAdapter()
    m.set_clock(_T)
    m.set_quote("UPC", RecordedQuote(bid=10.0, ask=10.04))
    assert m.get_best_bid_ask("UPC")[0] is not None
    m.clear_quote("UPC")
    assert m.get_best_bid_ask("UPC")[0] is None


def test_mock_broker_invalid_quote_treated_as_no_bbo():
    m = MockBrokerAdapter()
    m.set_clock(_T)
    m.set_quote("UPC", RecordedQuote(bid=0.0, ask=10.0))  # invalid bid
    assert m.get_best_bid_ask("UPC")[0] is None
    assert m.place_market_order(product_id="UPC", side="buy", base_size="1")["ok"] is False


def test_mock_broker_deterministic_order_ids_no_wallclock():
    """Identical inputs ⇒ identical order ids (monotonic counter, no UUID/wall-clock)."""
    ids = []
    for _ in range(2):
        m = MockBrokerAdapter()
        m.set_clock(_T)
        m.set_quote("UPC", RecordedQuote(bid=10.0, ask=10.04))
        r = m.place_market_order(product_id="UPC", side="buy", base_size="100")
        ids.append(r["order_id"])
    assert ids[0] == ids[1]  # deterministic across instances


def test_mock_broker_cancel_always_accepts():
    m = MockBrokerAdapter()
    out = m.cancel_order("replay_mock-00000001")
    assert out["ok"] is True


def test_make_mock_broker_factory_returns_same_singleton():
    m = MockBrokerAdapter()
    factory = make_mock_broker_factory(m)
    assert factory() is m
    assert factory() is m
