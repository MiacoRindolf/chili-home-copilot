from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.trading.momentum_neural.market_profile import market_open_now, market_session_for_symbol
from app.services.trading.momentum_neural.auto_arm import _candidate_tick_scalp_watch_reason
from app.services.trading.momentum_neural.tick_scalp import (
    INDEPENDENT_A_PLUS_WATCH_REASON,
    TRIGGER_REASON,
    evaluate_tick_first_pullback,
    expected_move_bps_from_ross_signal,
    independent_smallcap_a_plus_evidence_ok,
    ross_tick_scalp_evidence_ok,
)
from app.services.trading.momentum_neural.nbbo_tape import tape_running_up_signal_map


def _canf_signal() -> dict:
    return {
        "ticker": "CANF",
        "price": 6.79,
        "daily_change_pct": 128.62,
        "gap_pct": 119.19,
        "rvol_pace": 23.76,
        "float_shares": 2_120_000,
        "volume": 6_010_000,
        "scanner_source": "Ross's 5 Pillars Alert (Online)",
        "strategies": ["Low Float - High Rel Vol", "Squeeze Alert Up 10% in 10min"],
        "headline": "Phase 2a pancreatic cancer study update",
    }


def _jem_ws_ignition_signal() -> dict:
    return {
        "source": "ws_ignition",
        "ticker": "JEM",
        "direction": "long",
        "rvol_basis": "cumulative_day_over_prev_day",
        "signal_type": "ws_ignition",
        "float_shares": 1_410_968.0,
        "todays_change_perc": 132.99,
        "daily_structure_pct": 0.6613,
        "daily_breaking_major": True,
        "intraday_cumulative_rvol": 0.152,
    }


def test_ross_evidence_uses_setup_shape_not_fixed_score_floor() -> None:
    ok, reason, debug = ross_tick_scalp_evidence_ok(_canf_signal())

    assert ok is True
    assert reason == "tick_first_pullback_watch"
    assert debug["change_pct"] == 128.62
    assert debug["rvol"] == 23.76
    assert debug["float_shares"] == 2_120_000
    assert debug["failed_pillars"] == []
    assert debug["pillar_pass"]["change_pct"] is True
    assert debug["pillar_pass"]["rvol"] is True


def test_ross_evidence_accepts_ws_ignition_field_names() -> None:
    ok, reason, debug = ross_tick_scalp_evidence_ok(_jem_ws_ignition_signal())

    assert ok is True
    assert reason == "tick_first_pullback_watch"
    assert debug["change_pct"] == 132.99
    assert debug["source_support"] is True
    assert debug["daily_breaking_major"] is True


def test_ross_evidence_rejects_non_trading_transcript_context() -> None:
    signal = {
        "ticker": "GP",
        "price": 1.595,
        "daily_change_pct": -5.05,
        "rvol_pace": 0.44,
        "dollar_volume": 574_019.76,
        "source": "ross_audio_transcript warrior ross 5 pillars",
        "scanner_source": "ross_audio_transcript",
        "signal_type": "ross_transcript_mention",
        "transcript_text": "I got five minutes with a GP when I was sick.",
    }

    ok, reason, debug = ross_tick_scalp_evidence_ok(signal)

    assert ok is False
    assert reason == "ross_transcript_context_rejected"
    assert debug["signal_type"] == "ross_transcript_mention"


def test_direct_ross_trade_can_watch_when_scanner_pillars_lag() -> None:
    ok, reason, debug = ross_tick_scalp_evidence_ok(
        {
            "ticker": "CETX",
            "price": 3.095,
            "daily_change_pct": 4.03,
            "rvol": None,
            "source": "ross_audio_transcript warrior ross",
            "scanner_source": "ross_audio_transcript",
            "signal_type": "ross_trade",
            "transcript_text": "I took a starter long on CETX for a scalp.",
        }
    )

    assert ok is True
    assert reason == "tick_first_pullback_watch"
    assert debug["direct_ross_trade"] is True
    assert debug["direct_ross_trade_relaxed_scanner_pillars"] is True
    assert debug["failed_pillars"] == ["change_pct", "rvol"]


def test_generic_ross_scanner_mention_still_rejects_lagging_pillars() -> None:
    ok, reason, debug = ross_tick_scalp_evidence_ok(
        {
            "ticker": "CETX",
            "price": 3.095,
            "daily_change_pct": 4.03,
            "rvol": None,
            "scanner_source": "Warrior Ross scanner watchlist",
        }
    )

    assert ok is False
    assert reason == "ross_pillars_not_explosive"
    assert debug["source_support"] is True
    assert debug["direct_ross_trade"] is False
    assert debug["failed_pillars"] == ["change_pct", "rvol"]
    assert debug["pillar_pass"]["source_support"] is True


def test_ross_pillars_not_explosive_explains_missing_source_context() -> None:
    ok, reason, debug = ross_tick_scalp_evidence_ok(
        {
            "ticker": "CRE",
            "price": 2.88,
            "rvol": 1.99,
            "scanner_source": "generic_mover",
        }
    )

    assert ok is False
    assert reason == "ross_pillars_not_explosive"
    assert debug["failed_pillars"] == [
        "change_pct",
        "rvol",
        "source_support",
        "catalyst_or_direct_context",
    ]
    assert debug["pillar_pass"]["change_pct"] is False
    assert debug["pillar_pass"]["rvol"] is False
    assert debug["pillar_pass"]["source_support"] is False


def test_tick_first_pullback_fires_on_reclaim_without_bar_wait() -> None:
    signal = _canf_signal()
    first = evaluate_tick_first_pullback(
        symbol="CANF",
        signal=signal,
        state=None,
        bid=6.03,
        ask=6.05,
        mid=6.04,
        now_utc=datetime(2026, 7, 1, 11, 5, 0, tzinfo=timezone.utc),
    )

    assert first.fire is False
    assert first.reason == "waiting_for_tick_reclaim"
    assert first.state["phase"] == "pullback"

    second = evaluate_tick_first_pullback(
        symbol="CANF",
        signal=signal,
        state=first.state,
        bid=6.08,
        ask=6.10,
        mid=6.09,
        now_utc=datetime(2026, 7, 1, 11, 5, 1, tzinfo=timezone.utc),
    )

    assert second.fire is True
    assert second.reason == TRIGGER_REASON
    assert second.debug["structural_stop_price"] < 6.04
    assert second.debug["breakout_level_price"] > 6.04
    assert second.debug["max_hold_seconds"] == 12.0


# ── STEP-B #12: one-shot latch → placeability-gated + rearm-on-no-order ──

def _drive_to_reclaim_ready(signal: dict) -> dict:
    """Return a tick-scalp state parked in the reclaim-ready pullback (one tick before fire)."""
    first = evaluate_tick_first_pullback(
        symbol="CANF",
        signal=signal,
        state=None,
        bid=6.03,
        ask=6.05,
        mid=6.04,
        now_utc=datetime(2026, 7, 1, 11, 5, 0, tzinfo=timezone.utc),
    )
    assert first.fire is False
    assert first.state["phase"] == "pullback"
    return first.state


def test_placeability_gate_blocked_clock_does_not_consume_and_rearms() -> None:
    """A reclaim fired while NOT placeable (e.g. blocked clock) must NOT latch `fired` —
    it rearms so the next placeable tick can still fire."""
    signal = _canf_signal()
    ready = _drive_to_reclaim_ready(signal)

    blocked = evaluate_tick_first_pullback(
        symbol="CANF",
        signal=signal,
        state=ready,
        bid=6.08,
        ask=6.10,
        mid=6.09,
        now_utc=datetime(2026, 7, 1, 11, 5, 1, tzinfo=timezone.utc),
        placeable=False,  # market-hours blocked / adapter dark / spread unpassable
    )

    assert blocked.fire is False
    assert blocked.reason == "tick_reclaim_not_placeable_rearmed"
    assert blocked.state.get("fired") is not True  # NOT consumed
    assert blocked.state["rearm_count"] == 1
    assert blocked.debug["placeable"] is False
    assert blocked.debug["placeability_gate_enabled"] is True


def test_placeability_gate_adapter_dark_then_placeable_fires_once() -> None:
    """After a fired-but-blocked rearm, the next PLACEABLE reclaim tick fires exactly once
    and consumes the latch."""
    signal = _canf_signal()
    ready = _drive_to_reclaim_ready(signal)

    dark = evaluate_tick_first_pullback(
        symbol="CANF", signal=signal, state=ready,
        bid=6.08, ask=6.10, mid=6.09,
        now_utc=datetime(2026, 7, 1, 11, 5, 1, tzinfo=timezone.utc),
        placeable=False,
    )
    assert dark.fire is False
    assert dark.state.get("fired") is not True

    live = evaluate_tick_first_pullback(
        symbol="CANF", signal=signal, state=dark.state,
        bid=6.11, ask=6.13, mid=6.12,
        now_utc=datetime(2026, 7, 1, 11, 5, 2, tzinfo=timezone.utc),
        placeable=True,  # adapter healthy now
    )
    assert live.fire is True
    assert live.reason == TRIGGER_REASON
    assert live.state["fired"] is True

    # Latch consumed: a subsequent tick short-circuits to already_fired.
    after = evaluate_tick_first_pullback(
        symbol="CANF", signal=signal, state=live.state,
        bid=6.14, ask=6.16, mid=6.15,
        now_utc=datetime(2026, 7, 1, 11, 5, 3, tzinfo=timezone.utc),
        placeable=True,
    )
    assert after.fire is False
    assert after.reason == "already_fired"


def test_placeability_gate_placeable_fires_once_and_consumes() -> None:
    """placeable=True fires exactly once and latches (no rearm)."""
    signal = _canf_signal()
    ready = _drive_to_reclaim_ready(signal)

    fired = evaluate_tick_first_pullback(
        symbol="CANF", signal=signal, state=ready,
        bid=6.08, ask=6.10, mid=6.09,
        now_utc=datetime(2026, 7, 1, 11, 5, 1, tzinfo=timezone.utc),
        placeable=True,
    )
    assert fired.fire is True
    assert fired.reason == TRIGGER_REASON
    assert fired.state["fired"] is True
    assert "rearm_count" not in fired.state  # no rearm on the happy path


def test_placeability_gate_rearm_cap_consumes_after_floor(monkeypatch) -> None:
    """Once the per-day rearm cap is exhausted the latch consumes anyway (no infinite spin).

    The config knob is the documented base/floor; the caller override can only RAISE it.
    Lower the config base here (the real binding mechanism) so exhaustion is reachable in
    a short loop, proving the knob binds."""
    from app.config import settings as _settings

    cap = 2
    monkeypatch.setattr(_settings, "chili_momentum_tick_scalp_max_rearms_per_day", cap, raising=False)

    signal = _canf_signal()
    state = _drive_to_reclaim_ready(signal)

    last = None
    # Each blocked reclaim rearms; after `cap` rearms the next one consumes. Prices uptick
    # each tick (so tick_uptick holds) but stay well under the pullback high (6.79) so the
    # state never flips to a fresh thrust — it stays reclaim-ready across the blocked ticks.
    for i in range(cap):
        _mid = 6.09 + i * 0.01
        last = evaluate_tick_first_pullback(
            symbol="CANF", signal=signal, state=state,
            bid=_mid - 0.01, ask=_mid + 0.01, mid=_mid,
            now_utc=datetime(2026, 7, 1, 11, 5, 1 + i, tzinfo=timezone.utc),
            placeable=False,
        )
        assert last.fire is False
        assert last.reason == "tick_reclaim_not_placeable_rearmed"
        assert last.state.get("fired") is not True
        state = last.state

    assert state["rearm_count"] == cap
    exhausted = evaluate_tick_first_pullback(
        symbol="CANF", signal=signal, state=state,
        bid=6.11, ask=6.13, mid=6.12,
        now_utc=datetime(2026, 7, 1, 11, 5, 9, tzinfo=timezone.utc),
        placeable=False,
    )
    assert exhausted.fire is False
    assert exhausted.reason == "tick_reclaim_rearm_cap_exhausted"
    assert exhausted.state["fired"] is True  # consumed to stop the spin


def test_placeability_gate_none_preserves_legacy_consume() -> None:
    """placeable=None (caller not participating) preserves legacy consume-on-reclaim."""
    signal = _canf_signal()
    ready = _drive_to_reclaim_ready(signal)

    fired = evaluate_tick_first_pullback(
        symbol="CANF", signal=signal, state=ready,
        bid=6.08, ask=6.10, mid=6.09,
        now_utc=datetime(2026, 7, 1, 11, 5, 1, tzinfo=timezone.utc),
        # placeable omitted => None
    )
    assert fired.fire is True
    assert fired.reason == TRIGGER_REASON
    assert fired.state["fired"] is True


def test_tick_first_pullback_rejects_non_explosive_large_float_signal() -> None:
    signal = {
        "ticker": "MEH",
        "price": 11.0,
        "daily_change_pct": 4.0,
        "rvol_pace": 1.2,
        "float_shares": 80_000_000,
        "scanner_source": "generic mover",
    }

    ok, reason, _ = ross_tick_scalp_evidence_ok(signal)

    assert ok is False
    assert reason == "float_too_large"


def test_expected_move_can_come_from_ross_signal_without_candle_fetch() -> None:
    assert expected_move_bps_from_ross_signal(_canf_signal()) == 12862.0


def test_auto_arm_can_watch_ross_tick_scalp_without_candle_probe() -> None:
    candidate = SimpleNamespace(
        symbol="CANF",
        execution_readiness_json={"extra": {"ross_signals": {"CANF": _canf_signal()}}},
    )

    assert _candidate_tick_scalp_watch_reason(candidate) == "tick_first_pullback_watch"


def test_auto_arm_can_watch_ws_ignition_ross_signal_without_candle_probe() -> None:
    candidate = SimpleNamespace(
        symbol="JEM",
        execution_readiness_json={"extra": {"ross_signals": {"JEM": _jem_ws_ignition_signal()}}},
    )

    assert _candidate_tick_scalp_watch_reason(candidate) == "tick_first_pullback_watch"


def test_running_up_tape_feeder_preserves_ross_evidence() -> None:
    class _Rows:
        def fetchall(self):
            return [("JEM", 3.00, 6.16, 1_500_000)]

    class _Db:
        def execute(self, *_args, **_kwargs):
            return _Rows()

    signals = tape_running_up_signal_map(_Db())
    signal = signals["JEM"]
    ok, reason, debug = ross_tick_scalp_evidence_ok(signal)

    assert signal["source"] == "tape_delta_ignite"
    assert signal["price"] == 6.16
    assert ok is True
    assert reason == "tick_first_pullback_watch"
    assert debug["source_support"] is True


def test_independent_smallcap_a_plus_accepts_strong_tape_without_ross_source() -> None:
    ok, reason, debug = independent_smallcap_a_plus_evidence_ok(
        {
            "ticker": "PPCB",
            "price": 1.72,
            "daily_change_pct": 18.5,
            "volume": 4_100_000,
            "dollar_volume": 7_052_000,
            "source": "iqfeed_l1 tape_delta_ignite running_up_ignite",
            "signal_type": "running_up_ignite",
        }
    )

    assert ok is True
    assert reason == INDEPENDENT_A_PLUS_WATCH_REASON
    assert debug["source_support"] is True


def test_independent_smallcap_a_plus_rejects_generic_low_proof_mover() -> None:
    ok, reason, debug = independent_smallcap_a_plus_evidence_ok(
        {
            "ticker": "MEH",
            "price": 4.20,
            "daily_change_pct": 6.0,
            "volume": 80_000,
            "dollar_volume": 336_000,
            "source": "generic_momentum_continuation",
        }
    )

    assert ok is False
    assert reason == "independent_smallcap_change_below_floor"
    assert debug["source_support"] is False


def test_equity_extended_hours_are_explicit() -> None:
    premarket = datetime(2026, 7, 1, 11, 5, tzinfo=timezone.utc)

    closed = market_session_for_symbol("CANF", now=premarket)
    open_ext = market_session_for_symbol("CANF", now=premarket, allow_extended_hours=True)

    assert closed["market_session"] == "pre_market"
    assert closed["is_tradable"] is False
    assert market_open_now("CANF", now=premarket) is False
    assert open_ext["market_session"] == "pre_market"
    assert open_ext["is_tradable"] is True
    assert market_open_now("CANF", now=premarket, allow_extended_hours=True) is True
