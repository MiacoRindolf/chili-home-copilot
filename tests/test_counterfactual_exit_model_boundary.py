from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.trading.momentum_neural.counterfactual_replay import (
    CounterfactualReplayResult,
    ReplayEntryCandidate,
    ReplayTapeTick,
    SIMPLE_TARGET_ARMED_1R_TRAIL,
    _simulate_candidate_trade,
    result_to_dict,
)
from scripts.run_counterfactual_replay_v3 import _summary_payload


UTC = timezone.utc
BASE = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)


def _tick(seconds: int, *, bid: float, ask: float, sequence: int) -> ReplayTapeTick:
    return ReplayTapeTick(
        ts=BASE + timedelta(seconds=seconds),
        bid=bid,
        ask=ask,
        mid=(bid + ask) / 2.0,
        sequence=sequence,
    )


def test_live_runner_trail_is_disclosed_as_legacy_simple_trail_alias():
    candidate = ReplayEntryCandidate(
        symbol="VEEE",
        ts=BASE,
        reason="test_break",
        entry_price=10.0,
        stop_price=9.0,
        trigger_debug={},
        gate_family="momentum_pullback",
        bid=9.98,
        ask=10.0,
        spread_bps=20.0,
        sequence=1,
    )

    trade = _simulate_candidate_trade(
        candidate,
        [
            _tick(0, bid=9.98, ask=10.0, sequence=1),
            _tick(1, bid=11.2, ask=11.22, sequence=2),
            _tick(2, bid=10.1, ask=10.12, sequence=3),
        ],
        risk_usd=100.0,
        max_notional_usd=10_000.0,
        reward_risk=1.0,
        max_hold_seconds=300.0,
        fixed_qty=100.0,
        exit_model="live_runner_trail",
    )

    assert trade is not None
    assert trade.exit_reason == "trail_stop"
    assert trade.debug["exit_model"] == SIMPLE_TARGET_ARMED_1R_TRAIL
    assert trade.debug["exit_route"]["requested_exit_model"] == "live_runner_trail"
    assert trade.debug["exit_route"]["legacy_alias"] == "live_runner_trail"
    assert trade.debug["production_exit_parity"] is False


def test_result_and_summary_disclose_non_parity_even_with_zero_trades():
    result = CounterfactualReplayResult(
        since=BASE,
        until=BASE + timedelta(minutes=5),
        symbols=["VEEE"],
        results=[],
        requested_exit_model="live_runner_trail",
    )

    payload = result_to_dict(result)
    summary = _summary_payload(payload)

    assert payload["exit_model"]["effective"] == SIMPLE_TARGET_ARMED_1R_TRAIL
    assert payload["exit_model"]["legacy_alias"] == "live_runner_trail"
    assert payload["exit_model"]["production_exit_parity"] is False
    assert "ReplayV3Driver" in payload["exit_model"]["boundary"]
    assert summary["exit_model"] == payload["exit_model"]
