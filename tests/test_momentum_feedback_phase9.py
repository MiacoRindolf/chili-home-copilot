"""Phase 9: closed-loop momentum automation feedback (neural evolution path)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.core import User
from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
    TradingDecisionPacket,
)
from app.services.trading.decision_ledger import seal_decision_packet_snapshot
from app.services.trading.momentum_neural.evolution import paper_vs_live_performance_slices
from app.services.trading.momentum_neural.feedback_emit import (
    reingest_regraded_momentum_outcomes,
    regrade_momentum_outcome_evolution_credit,
    try_emit_momentum_session_feedback,
)
from app.services.trading.momentum_neural.feedback_query import evolution_credit_diagnostics
from app.services.trading.momentum_neural.outcome_extract import derive_outcome_class, session_terminal_for_feedback
from app.services.trading.momentum_neural.outcome_labels import (
    OUTCOME_CANCELLED_PRE_ENTRY,
    OUTCOME_NO_FILL,
    OUTCOME_SMALL_WIN,
    OUTCOME_SUCCESS,
)
from app.services.trading.momentum_neural.persistence import ensure_momentum_strategy_variants
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY


def test_derive_outcome_labels() -> None:
    ev: list = []
    assert (
        derive_outcome_class(
            mode="paper",
            terminal_state="finished",
            entry_occurred=True,
            partial_exit=False,
            realized_pnl_usd=10.0,
            return_bps=30.0,
            exit_reason="target",
            governance_context={},
            events=ev,
        )
        == OUTCOME_SUCCESS
    )
    assert (
        derive_outcome_class(
            mode="paper",
            terminal_state="finished",
            entry_occurred=True,
            partial_exit=False,
            realized_pnl_usd=1.0,
            return_bps=8.0,
            exit_reason="target",
            governance_context={},
            events=ev,
        )
        == OUTCOME_SMALL_WIN
    )
    assert (
        derive_outcome_class(
            mode="paper",
            terminal_state="cancelled",
            entry_occurred=False,
            partial_exit=False,
            realized_pnl_usd=None,
            return_bps=None,
            exit_reason=None,
            governance_context={},
            events=ev,
        )
        == OUTCOME_CANCELLED_PRE_ENTRY
    )


def test_session_terminal_for_feedback_modes() -> None:
    assert session_terminal_for_feedback("paper", "finished")
    assert session_terminal_for_feedback("live", "live_finished")
    assert not session_terminal_for_feedback("paper", "watching")
    assert not session_terminal_for_feedback("live", "live_exited")


def test_zero_fill_maps_to_no_fill() -> None:
    class E:
        event_type = "live_error"
        payload_json = {"reason": "zero_fill"}

    assert (
        derive_outcome_class(
            mode="live",
            terminal_state="live_error",
            entry_occurred=False,
            partial_exit=False,
            realized_pnl_usd=None,
            return_bps=None,
            exit_reason=None,
            governance_context={},
            events=[E()],
        )
        == OUTCOME_NO_FILL
    )


def test_feedback_emit_idempotent(db: Session) -> None:
    """Requires migration 091 applied (test schema bootstrap)."""
    from sqlalchemy import inspect as sa_inspect

    names = set(sa_inspect(db.bind).get_table_names())
    if "momentum_automation_outcomes" not in names:
        pytest.skip("momentum_automation_outcomes table not present (run migrations)")

    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()

    u = User(name="FbPhase9")
    db.add(u)
    db.commit()
    db.refresh(u)

    sess = TradingAutomationSession(
        user_id=u.id,
        mode="paper",
        symbol="FB9-USD",
        variant_id=v.id,
        state="finished",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_paper_execution": {"realized_pnl_usd": 5.0, "last_exit_reason": "target"},
        },
        ended_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)

    r1 = try_emit_momentum_session_feedback(db, sess)
    assert r1.get("emitted") or r1.get("ok")
    db.commit()

    r2 = try_emit_momentum_session_feedback(db, sess)
    assert r2.get("deduped")

    n = db.query(MomentumAutomationOutcome).filter(MomentumAutomationOutcome.session_id == sess.id).count()
    assert n == 1


def test_paper_vs_live_slices_separate(db: Session) -> None:
    from sqlalchemy import inspect as sa_inspect

    if "momentum_automation_outcomes" not in set(sa_inspect(db.bind).get_table_names()):
        pytest.skip("momentum_automation_outcomes table not present")

    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    u = User(name="FbSlice")
    db.add(u)
    db.commit()
    db.refresh(u)

    for mode, rb in (("paper", 10.0), ("live", -15.0)):
        s = TradingAutomationSession(
            user_id=u.id,
            mode=mode,
            symbol="SL-USD",
            variant_id=v.id,
            state="finished" if mode == "paper" else "live_finished",
            risk_snapshot_json={RISK_SNAPSHOT_KEY: {"allowed": True}},
            ended_at=datetime.utcnow(),
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        db.add(
            MomentumAutomationOutcome(
                session_id=s.id,
                user_id=u.id,
                variant_id=v.id,
                symbol=s.symbol,
                mode=mode,
                execution_family="coinbase_spot",
                terminal_state=s.state,
                terminal_at=s.ended_at or datetime.utcnow(),
                outcome_class="small_win" if rb > 0 else "small_loss",
                realized_pnl_usd=rb / 10.0,
                return_bps=rb,
                regime_snapshot_json={},
                entry_regime_snapshot_json={},
                exit_regime_snapshot_json={},
                readiness_snapshot_json={},
                admission_snapshot_json={},
                governance_context_json={},
                extracted_summary_json={
                    "evolution_credit": {
                        "contributes_to_evolution": True,
                        "reason_codes": [],
                        "entry_decision_packet_id": 1000 + int(s.id),
                    }
                },
                evidence_weight=1.0,
                contributes_to_evolution=True,
            )
        )
        db.commit()

    pv = paper_vs_live_performance_slices(db, variant_id=v.id, days=14)
    assert pv["paper"]["n"] >= 1
    assert pv["live"]["n"] >= 1
    assert pv["paper"]["mean_return_bps"] is not None or pv["live"]["mean_return_bps"] is not None


def test_evolution_credit_diagnostics_counts_training_grade_outcomes(db: Session) -> None:
    from sqlalchemy import inspect as sa_inspect

    if "momentum_automation_outcomes" not in set(sa_inspect(db.bind).get_table_names()):
        pytest.skip("momentum_automation_outcomes table not present")

    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    u = User(name="FbCreditDiag")
    db.add(u)
    db.commit()
    db.refresh(u)

    sessions = [
        TradingAutomationSession(
            user_id=u.id,
            mode=mode,
            symbol=symbol,
            variant_id=v.id,
            state="finished" if mode == "paper" else "live_finished",
            risk_snapshot_json={RISK_SNAPSHOT_KEY: {"allowed": True}},
            ended_at=datetime.utcnow(),
        )
        for mode, symbol in (("paper", "CRD1-USD"), ("paper", "CRD2-USD"), ("live", "CRD3-USD"))
    ]
    db.add_all(sessions)
    db.commit()
    for sess in sessions:
        db.refresh(sess)

    now = datetime.utcnow()
    rows = [
        MomentumAutomationOutcome(
            session_id=sessions[0].id,
            user_id=u.id,
            variant_id=v.id,
            symbol=sessions[0].symbol,
            mode="paper",
            execution_family="coinbase_spot",
            terminal_state="finished",
            terminal_at=now,
            outcome_class="small_win",
            realized_pnl_usd=1.0,
            return_bps=10.0,
            regime_snapshot_json={},
            entry_regime_snapshot_json={},
            exit_regime_snapshot_json={},
            readiness_snapshot_json={},
            admission_snapshot_json={},
            governance_context_json={},
            extracted_summary_json={
                "evolution_credit": {
                    "contributes_to_evolution": True,
                    "reason_codes": [],
                    "entry_decision_packet_id": 101,
                }
            },
            evidence_weight=1.0,
            contributes_to_evolution=True,
            created_at=now - timedelta(seconds=2),
        ),
        MomentumAutomationOutcome(
            session_id=sessions[1].id,
            user_id=u.id,
            variant_id=v.id,
            symbol=sessions[1].symbol,
            mode="paper",
            execution_family="coinbase_spot",
            terminal_state="finished",
            terminal_at=now,
            outcome_class="small_win",
            realized_pnl_usd=1.0,
            return_bps=10.0,
            regime_snapshot_json={},
            entry_regime_snapshot_json={},
            exit_regime_snapshot_json={},
            readiness_snapshot_json={},
            admission_snapshot_json={},
            governance_context_json={},
            extracted_summary_json={
                "evolution_credit": {
                    "contributes_to_evolution": False,
                    "reason_codes": ["missing_entry_decision_packet"],
                    "entry_decision_packet_id": None,
                }
            },
            evidence_weight=1.0,
            contributes_to_evolution=False,
            created_at=now - timedelta(seconds=1),
        ),
        MomentumAutomationOutcome(
            session_id=sessions[2].id,
            user_id=u.id,
            variant_id=v.id,
            symbol=sessions[2].symbol,
            mode="live",
            execution_family="coinbase_spot",
            terminal_state="live_finished",
            terminal_at=now,
            outcome_class="small_loss",
            realized_pnl_usd=-1.0,
            return_bps=-10.0,
            regime_snapshot_json={},
            entry_regime_snapshot_json={},
            exit_regime_snapshot_json={},
            readiness_snapshot_json={},
            admission_snapshot_json={},
            governance_context_json={},
            extracted_summary_json={
                "evolution_credit": {
                    "contributes_to_evolution": False,
                    "reason_codes": ["economic_ledger_parity_missing"],
                    "entry_decision_packet_id": 102,
                }
            },
            evidence_weight=1.0,
            contributes_to_evolution=False,
            created_at=now,
        ),
    ]
    db.add_all(rows)
    db.commit()

    out = evolution_credit_diagnostics(db, days=30, user_id=u.id)

    assert out["mode"] == "audit_only"
    assert out["total"] == 3
    assert out["credited"] == 1
    assert out["blocked"] == 2
    assert out["credit_rate"] == pytest.approx(0.3333)
    assert out["reingest_required"] == 0
    reasons = {row["reason_code"]: row["n"] for row in out["reason_counts"]}
    assert reasons == {
        "missing_entry_decision_packet": 1,
        "economic_ledger_parity_missing": 1,
    }
    repairs = {row["reason_code"]: row for row in out["recommended_repairs"]}
    assert repairs["missing_entry_decision_packet"]["repair_kind"] == "decision_packet_lineage"
    assert repairs["economic_ledger_parity_missing"]["repair_kind"] == "automation_ledger_parity"
    by_mode = {row["key"]: row for row in out["by_mode"]}
    assert by_mode["paper"]["total"] == 2
    assert by_mode["paper"]["credited"] == 1
    assert by_mode["live"]["blocked"] == 1
    assert [row["symbol"] for row in out["blocked_examples"]] == ["CRD3-USD", "CRD2-USD"]


def test_evolution_credit_regrade_promotes_repaired_packet_snapshot(db: Session, monkeypatch) -> None:
    from sqlalchemy import inspect as sa_inspect

    if "momentum_automation_outcomes" not in set(sa_inspect(db.bind).get_table_names()):
        pytest.skip("momentum_automation_outcomes table not present")

    import app.services.trading.momentum_neural.feedback_emit as feedback_emit

    monkeypatch.setattr(feedback_emit, "economic_ledger_active", lambda: False)
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    u = User(name="FbCreditRegrade")
    db.add(u)
    db.commit()
    db.refresh(u)

    now = datetime.utcnow()
    sess = TradingAutomationSession(
        user_id=u.id,
        mode="paper",
        symbol="RGRD-USD",
        variant_id=v.id,
        state="finished",
        risk_snapshot_json={RISK_SNAPSHOT_KEY: {"allowed": True}},
        ended_at=now,
    )
    db.add(sess)
    db.flush()
    pkt = TradingDecisionPacket(
        user_id=u.id,
        automation_session_id=sess.id,
        chosen_ticker=sess.symbol,
        decision_type="trade",
        execution_mode="paper",
        deployment_stage="paper",
        source_surface="autopilot",
        outcome_status="executed",
        regime_snapshot_json={},
        allocator_input_json={},
        allocator_output_json={},
        portfolio_context_json={},
        candidate_count=1,
        capacity_blocked=False,
        capacity_reason_json={},
        shadow_advisory_only=False,
    )
    db.add(pkt)
    db.flush()
    seal_decision_packet_snapshot(pkt)
    db.flush()
    row = MomentumAutomationOutcome(
        session_id=sess.id,
        user_id=u.id,
        variant_id=v.id,
        symbol=sess.symbol,
        mode="paper",
        execution_family="coinbase_spot",
        terminal_state="finished",
        terminal_at=now,
        outcome_class="small_win",
        realized_pnl_usd=1.25,
        return_bps=12.0,
        regime_snapshot_json={},
        entry_regime_snapshot_json={},
        exit_regime_snapshot_json={},
        readiness_snapshot_json={},
        admission_snapshot_json={},
        governance_context_json={},
        extracted_summary_json={
            "entry_occurred": True,
            "entry_decision_packet_id": int(pkt.id),
            "evolution_credit": {
                "contributes_to_evolution": False,
                "reason_codes": ["decision_snapshot_invalid"],
                "entry_decision_packet_id": int(pkt.id),
            },
        },
        evidence_weight=1.0,
        contributes_to_evolution=False,
        created_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    dry = regrade_momentum_outcome_evolution_credit(db, days=30, user_id=u.id, limit=10, dry_run=True)

    assert dry["dry_run"] is True
    assert dry["candidate_count"] == 1
    assert dry["applied_count"] == 0
    assert dry["upgraded_to_training_grade"] == 1
    assert dry["reingest_required_count"] == 1
    assert dry["candidates"][0]["old_reason_codes"] == ["decision_snapshot_invalid"]
    assert dry["candidates"][0]["new_reason_codes"] == []
    db.refresh(row)
    assert row.contributes_to_evolution is False

    applied = regrade_momentum_outcome_evolution_credit(db, days=30, user_id=u.id, limit=10, dry_run=False)
    db.refresh(row)

    assert applied["dry_run"] is False
    assert applied["applied_count"] == 1
    assert applied["upgraded_to_training_grade"] == 1
    assert row.contributes_to_evolution is True
    assert row.extracted_summary_json["evolution_credit"]["reason_codes"] == []
    assert row.extracted_summary_json["evolution_credit"]["decision_snapshot_verification"]["ok"] is True
    diag_after_regrade = evolution_credit_diagnostics(db, days=30, user_id=u.id)
    assert diag_after_regrade["reingest_required"] == 1
    assert diag_after_regrade["reingest_examples"][0]["symbol"] == "RGRD-USD"

    reingest_dry = reingest_regraded_momentum_outcomes(db, days=30, user_id=u.id, limit=10, dry_run=True)
    assert reingest_dry["candidate_count"] == 1
    assert reingest_dry["applied_count"] == 0

    reingest_apply = reingest_regraded_momentum_outcomes(db, days=30, user_id=u.id, limit=10, dry_run=False)
    db.refresh(row)

    assert reingest_apply["applied_count"] == 1
    assert reingest_apply["applied"][0]["reingest_result"]["contribution_applied"] is True
    assert row.extracted_summary_json["evolution_ingest_v1"]["contribution_apply_count"] == 1
    assert row.extracted_summary_json["evolution_credit_regrade_v1"]["reingested_at_utc"]

    reingest_again = reingest_regraded_momentum_outcomes(db, days=30, user_id=u.id, limit=10, dry_run=True)
    assert reingest_again["candidate_count"] == 0
    diag_after_reingest = evolution_credit_diagnostics(db, days=30, user_id=u.id)
    assert diag_after_reingest["reingest_required"] == 0
