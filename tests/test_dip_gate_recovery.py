"""Executable regression cases from the two independent reviews of PR1391."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.services.trading.momentum_neural import entry_gates as gates
from app.services.trading.momentum_neural.live_runner import (
    _micro_pullback_primary_stop, structural_trigger_reasons,
)
from app.services.trading.momentum_neural.paper_execution import (
    structural_or_vol_floored_atr_pct, stop_target_prices,
)
from app.services.trading.momentum_neural.risk_policy import (
    compute_risk_first_quantity, starter_size_multiplier,
)


@pytest.mark.parametrize("gaps", [(40,), (200, 200)])
def test_print_mode_preserves_existing_halt_splits(gaps):
    # Each halt is shorter than the total active span. Half-span trimming would
    # retain it (and can never remove both), manufacturing cross-halt accel.
    rows = []
    at = 1e9
    for segment in range(len(gaps) + 1):
        for i in range(128):
            rows.append((10.01 if segment else 9.99, 100, 9.99, 10.01, at))
            at += 4.0
        if segment < len(gaps):
            at += gaps[segment]
    old = gates._signed_tape_features(rows, window_s=15, tick_rate_floor_pctile=0)
    got = gates._signed_tape_features(
        rows, window_s=15, tick_rate_floor_pctile=0, window_mode="prints",
    )
    assert old is not None and got is not None
    assert got["gap_restricted"] is True
    assert got["n_ticks"] == 128
    assert got["gap_split_s"] == 7.5
    assert got["signed_tape_accel"] == old["signed_tape_accel"]
    assert got["buy_share_delta"] == old["buy_share_delta"]
    assert gates.tape_print_age_bound_s(
        age_floor_s=14.69, gap_p99_s=got["gap_p99_s"],
    ) == 14.69


@pytest.mark.parametrize("reason", ["micro_pullback_primary", "micro_pullback_primary_tick_ok"])
def test_primary_stop_has_no_structural_bypass_or_double_starter_budget(reason):
    assert reason not in structural_trigger_reasons()
    multiplier, _ = starter_size_multiplier(
        reason, structural_reasons=structural_trigger_reasons(), fraction=0.5,
    )
    assert multiplier == 0.5
    assert _micro_pullback_primary_stop(
        reason, {"pullback_low": 8.0, "pullback_high": 10.0},
    ) == 8.0
    assert _micro_pullback_primary_stop(
        "momentum_ok", {"pullback_low": 8.0, "pullback_high": 10.0},
    ) is None


@pytest.mark.parametrize("low", [9.0, 8.0, 6.0])
def test_deep_dip_uses_actual_stop_distance_at_unchanged_dollar_risk(low):
    effective, model = structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=0.03, structural_stop_price=low,
        entry_price=10, stop_atr_mult=0.6, trigger_reason="micro_pullback_primary",
    )
    stop, _ = stop_target_prices(10, atr_pct=effective, stop_atr_mult=0.6)
    quantity, meta = compute_risk_first_quantity(
        entry_price=10, atr_pct=effective, stop_atr_mult=0.6,
        max_loss_usd=30, max_notional_ceiling_usd=10000,
    )
    assert stop == pytest.approx(low)
    assert quantity == pytest.approx(30 / (10 - low))
    assert meta["risk_usd"] == 30
    assert model == "micro_pullback_observed_dip"
    # Every unrelated pattern still has the prior cap.
    prior, _ = structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=0.03, structural_stop_price=low,
        entry_price=10, stop_atr_mult=0.6, trigger_reason="first_pullback_ok",
    )
    assert prior == 0.15


@pytest.mark.parametrize("low", [None, 0, -1, 10, 11, float("nan"), float("inf")])
def test_primary_invalid_observed_stop_cannot_fall_back_to_depth_blind_size(low):
    with pytest.raises(ValueError, match="micro_pullback_observed_stop_invalid"):
        structural_or_vol_floored_atr_pct(
            vol_floored_atr_pct=0.03, structural_stop_price=low,
            entry_price=10, stop_atr_mult=0.6, trigger_reason="micro_pullback_primary",
        )


def test_retired_flow_receipt_preserves_effective_overrides_and_old_verdict():
    receipt = gates.retired_micro_pullback_flow_receipt(
        0.1, 0.4, ofi_threshold=0.2, trade_flow_threshold=0.3,
    )
    assert receipt["retired_ofi_threshold"] == 0.2
    assert receipt["retired_trade_flow_threshold"] == 0.3
    assert receipt["retired_flow_would_have_blocked"] is True
    assert gates.retired_micro_pullback_flow_receipt(
        0.2, 0.3, ofi_threshold=0.2, trade_flow_threshold=0.3,
    )["retired_flow_would_have_blocked"] is False
    assert gates.retired_micro_pullback_flow_receipt(
        None, 0.9, ofi_threshold=0, trade_flow_threshold=0,
    )["retired_ofi_threshold"] == 0.3  # prior ``raw or default`` contract


def test_missing_or_unsealed_break_is_a_reachable_wait_then_re_reads(monkeypatch):
    rows = [(3.70, 20, False), (3.71, 3, False)]
    monkeypatch.setattr(gates, "high_print_in_window", lambda *a, **kw: rows.pop(0))
    evidence = gates.micro_pullback_print_evidence("SKYQ")
    assert evidence["break_ref_observed_px"] == 3.70
    assert evidence["break_ref_px"] is None
    assert evidence["break_ref_sealed"] is False
    assert gates.micro_pullback_reload_proof(
        veto=False, last_print=3.71, signed_tape_accel=100, tape_stale=False,
        break_ref_px=evidence["break_ref_px"], reclaim_high_px=evidence["reclaim_high_px"],
    ) == "break_reference_unreadable"
    rows.extend([(3.72, 22, True), (3.71, 3, False)])
    evidence = gates.micro_pullback_print_evidence("SKYQ")
    assert evidence["break_ref_px"] == 3.72
    assert gates.micro_pullback_reload_proof(
        veto=False, last_print=3.71, signed_tape_accel=100, tape_stale=False,
        break_ref_px=evidence["break_ref_px"], reclaim_high_px=evidence["reclaim_high_px"],
    ) == "reclaim_wait"


def test_high_print_query_filters_publication_and_seals_with_later_arrived_print(db):
    # A temporary table on this test session shadows the production-shaped table.
    db.execute(text("CREATE TEMP TABLE iqfeed_trade_ticks (symbol text, price double precision, "
                    "observed_at timestamp, received_at timestamptz, available_at timestamptz) ON COMMIT DROP"))
    begin = datetime(2026, 9, 10, 13, 52)
    end = begin + timedelta(seconds=10)
    frontier = end + timedelta(seconds=1)
    def put(price, event, received, available):
        db.execute(text("INSERT INTO iqfeed_trade_ticks VALUES ('SKYQ', :p, :e, :r, :a)"),
                   {"p": price, "e": event, "r": received, "a": available})
    def utc(t):
        return t.replace(tzinfo=timezone.utc)
    put(3.70, begin, utc(begin), utc(begin))
    # Neither an available-before-received row nor a missing publication clock
    # can lower the integrity of the reference or provide a seal.
    put(9.0, begin, utc(frontier + timedelta(seconds=1)), utc(begin))
    put(8.0, begin, utc(begin), None)
    put(8.1, begin, None, utc(begin))
    # Reversed and infinite publication clocks cannot supply the interval seal,
    # even after their nominal timestamps are all before the frontier.
    put(9.1, end, utc(frontier), utc(begin))
    put(9.2, end, "-infinity", "-infinity")
    put(9.3, begin, "-infinity", utc(begin))
    put(9.4, begin, utc(begin), "infinity")
    put(9.5, "infinity", utc(begin), utc(begin))
    put(9.6, "-infinity", utc(begin), utc(begin))
    put(3.72, end - timedelta(milliseconds=1), utc(frontier + timedelta(seconds=1)),
        utc(frontier + timedelta(seconds=1)))
    got = gates.high_print_in_window("SKYQ", db=db, start_at=begin, end_at=end, as_of=frontier)
    assert got == (3.70, 1, False)
    put(3.71, end, utc(frontier + timedelta(seconds=1)), utc(frontier + timedelta(seconds=1)))
    got = gates.high_print_in_window(
        "SKYQ", db=db, start_at=begin, end_at=end,
        as_of=(frontier + timedelta(seconds=2)).replace(tzinfo=timezone.utc),
    )
    assert got == (3.72, 2, True)


@pytest.mark.parametrize("low", [8.0, 6.0])
def test_db_paper_producer_and_final_recompute_keep_the_observed_dip_stop(db, monkeypatch, low):
    from app.config import settings
    from app.models.trading import (
        AdaptiveRiskDecisionPacket, MomentumSymbolViability, TradingAutomationSession,
        TradingAutomationEvent, TradingAutomationSimulatedFill,
    )
    from app.services.trading.momentum_neural.operator_actions import create_paper_draft_session
    from app.services.trading.momentum_neural import paper_runner
    from app.services.trading.momentum_neural.paper_runner import tick_paper_session
    from tests.test_momentum_paper_runner import _seed_live_eligible_row, _uid

    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    # Control the detector's geometry; leave production capture, evidence sealing,
    # recompute, risk resolution and simulated placement fully executable.
    gate = lambda *a, **kw: (
        True, "micro_pullback_primary", {"pullback_low": low, "pullback_high": 10.0},
    )
    monkeypatch.setattr(gates, "run_paper_entry_gates", gate)
    monkeypatch.setattr(paper_runner, "run_paper_entry_gates", gate)
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    variant_id, _ = _seed_live_eligible_row(db, symbol="DIPT")
    via = db.query(MomentumSymbolViability).filter_by(symbol="DIPT", variant_id=variant_id).one()
    via.viability_score = 0.95
    via.paper_eligible = True
    via.regime_snapshot_json = {"atr_pct": 0.02, "chop_expansion": "trend"}
    db.commit()
    created = create_paper_draft_session(
        db, user_id=_uid(db, "dip"), symbol="DIPT", variant_id=variant_id,
        execution_family="alpaca_spot",
    )
    assert created["ok"], created
    sid = created["session_id"]
    db.commit()
    quote = lambda symbol: {"mid": 10.0, "bid": 9.99, "ask": 10.01, "source": "test"}
    for _ in range(6):
        tick_paper_session(db, sid, quote_fn=quote)
        db.commit()
    session = db.query(TradingAutomationSession).filter_by(id=sid).one()
    execution = (session.risk_snapshot_json or {}).get("momentum_paper_execution") or {}
    position = execution.get("position") or {}
    fills = db.query(TradingAutomationSimulatedFill).filter_by(session_id=sid).all()
    events = db.query(TradingAutomationEvent).filter_by(session_id=sid).all()
    assert fills, "\n".join(str((e.event_type, e.payload_json)) for e in events)
    assert position.get("stop_price") == pytest.approx(low), position
    entry = float(position["entry_price"])
    shares = float(position["quantity"])
    equity = float(getattr(settings, "chili_db_paper_equity_usd"))
    packet = db.query(AdaptiveRiskDecisionPacket).filter_by(symbol="DIPT").one()
    resolution = packet.decision_packet_json
    assert float(packet.structural_stop) == pytest.approx(low)
    assert resolution["base_r_usd"] == pytest.approx(equity * float(settings.chili_momentum_risk_loss_fraction_of_equity))
    assert shares * (entry - low) <= resolution["candidate_risk_budget_usd"]
