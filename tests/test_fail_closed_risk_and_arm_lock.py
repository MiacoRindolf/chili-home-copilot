from __future__ import annotations

import uuid
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.models.core import User
from app.models.trading import (
    BrokerSymbolActionClaim,
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import operator_actions
from app.services.trading.momentum_neural import risk_evaluator
from app.services.trading.momentum_neural.risk_evaluator import (
    aggregate_open_risk_usd,
    count_inflight_entry_orders,
    sum_inflight_entry_risk_usd,
)

pytestmark = pytest.mark.usefixtures("stable_non_alpaca_account_identity")


def _user_and_variant(
    db: Session,
    *,
    execution_family: str = "robinhood_spot",
) -> tuple[User, MomentumStrategyVariant]:
    suffix = uuid.uuid4().hex[:10]
    user = User(name=f"FailClosedRiskArm_{suffix}")
    variant = MomentumStrategyVariant(
        family=f"fail_closed_risk_arm_{suffix}",
        variant_key="base",
        version=1,
        label="Fail-closed risk and arm lock",
        params_json={},
        is_active=True,
        execution_family=execution_family,
    )
    db.add_all([user, variant])
    db.flush()
    return user, variant


def _session(
    db: Session,
    *,
    user_id: int,
    variant_id: int,
    symbol: str,
    state: str,
    execution_family: str,
    mode: str = "live",
    live_execution: dict | None = None,
    extra_snapshot: dict | None = None,
) -> TradingAutomationSession:
    snapshot = dict(extra_snapshot or {})
    snapshot["momentum_live_execution"] = dict(live_execution or {})
    if execution_family in {"alpaca_spot", "alpaca_short"}:
        snapshot.setdefault("alpaca_account_scope", "alpaca:paper")
        snapshot.setdefault(
            "alpaca_account_id",
            "00000000-0000-0000-0000-000000000001",
        )
    row = TradingAutomationSession(
        user_id=int(user_id),
        venue=("alpaca" if execution_family.startswith("alpaca_") else "robinhood"),
        execution_family=execution_family,
        mode=mode,
        symbol=symbol,
        variant_id=int(variant_id),
        state=state,
        risk_snapshot_json=snapshot,
        allocation_decision_json={},
        correlation_id=f"corr-{uuid.uuid4().hex[:12]}",
        source_node_id="fail_closed_risk_arm_test",
    )
    db.add(row)
    db.flush()
    return row


def _set_live_execution(
    db: Session,
    row: TradingAutomationSession,
    live_execution: dict,
) -> None:
    snapshot = dict(row.risk_snapshot_json or {})
    snapshot["momentum_live_execution"] = dict(live_execution)
    row.risk_snapshot_json = snapshot
    flag_modified(row, "risk_snapshot_json")
    db.flush()


def _contended_or_failed_lock(raise_error: bool):
    def _lock(*_args, **_kwargs) -> bool:
        if raise_error:
            raise RuntimeError("synthetic advisory-lock failure")
        return False

    return _lock


def _add_viability(
    db: Session,
    *,
    symbol: str,
    variant_id: int,
    freshness_ts: datetime | None = None,
) -> MomentumSymbolViability:
    row = MomentumSymbolViability(
        symbol=symbol,
        scope="symbol",
        variant_id=int(variant_id),
        viability_score=0.95,
        paper_eligible=True,
        live_eligible=True,
        freshness_ts=freshness_ts or datetime.utcnow(),
        regime_snapshot_json={},
        execution_readiness_json={},
        explain_json={},
        evidence_window_json={},
    )
    db.add(row)
    db.flush()
    return row


def _stub_promotion_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.trading import portfolio_allocator

    monkeypatch.setattr(operator_actions, "_paper_promotion_gate", lambda _row: (True, "ok"))
    monkeypatch.setattr(operator_actions, "_alpaca_execution_quarantine_reason", lambda *_a: None)
    monkeypatch.setattr(operator_actions, "is_momentum_automation_implemented", lambda _ef: True)
    monkeypatch.setattr(
        operator_actions,
        "guard_alpaca_entry_ownership",
        lambda *_a, **_k: (True, None, None),
    )
    monkeypatch.setattr(
        operator_actions,
        "resolve_effective_risk_policy",
        lambda: {"auto_expire_pending_live_arm_seconds": 120.0},
    )
    monkeypatch.setattr(
        operator_actions,
        "evaluate_proposed_momentum_automation",
        lambda *_a, **_k: {"allowed": True, "severity": "ok", "checks": []},
    )
    monkeypatch.setattr(
        operator_actions,
        "build_session_risk_snapshot",
        lambda *args, **kwargs: dict(kwargs.get("extra") or {}),
    )
    monkeypatch.setattr(
        portfolio_allocator,
        "build_session_allocation_decision",
        lambda *_a, **_k: {"allowed_if_enforced": True},
    )


def test_alpaca_held_and_pending_positions_reject_unknown_risk_fields(
    db: Session,
) -> None:
    user, variant = _user_and_variant(db, execution_family="alpaca_spot")
    row = _session(
        db,
        user_id=int(user.id),
        variant_id=int(variant.id),
        symbol="RISK",
        state="live_trailing",
        execution_family="alpaca_spot",
    )
    missing = object()
    bad_values = (missing, None, "bad", "nan", "inf", True, 0, -1)

    for state in ("live_trailing", "live_pending_entry"):
        row.state = state
        for field in ("quantity", "avg_entry_price", "stop_price"):
            for bad in bad_values:
                position = {
                    "quantity": 10,
                    "avg_entry_price": 10.0,
                    "stop_price": 9.5,
                }
                if bad is missing:
                    position.pop(field)
                else:
                    position[field] = bad
                _set_live_execution(
                    db,
                    row,
                    {"side_long": True, "position": position},
                )
                with pytest.raises(
                    RuntimeError,
                    match="alpaca_risk_ledger_unavailable",
                ):
                    aggregate_open_risk_usd(
                        db,
                        user_id=int(user.id),
                        execution_family="alpaca_spot",
                    )


def test_alpaca_zero_risk_and_no_exposure_rows_remain_zero(db: Session) -> None:
    user, variant = _user_and_variant(db, execution_family="alpaca_spot")
    row = _session(
        db,
        user_id=int(user.id),
        variant_id=int(variant.id),
        symbol="FLAT",
        state="live_trailing",
        execution_family="alpaca_spot",
        live_execution={
            "side_long": True,
            "position": {
                "quantity": 10,
                "avg_entry_price": 10.0,
                "stop_price": 10.0,
            },
        },
    )

    total, rows = aggregate_open_risk_usd(
        db,
        user_id=int(user.id),
        execution_family="alpaca_spot",
    )
    assert total == 0.0
    assert rows == []

    row.state = "finished"
    _set_live_execution(
        db,
        row,
        {"side_long": True, "position": {"quantity": 0}},
    )
    total, rows = aggregate_open_risk_usd(
        db,
        user_id=int(user.id),
        execution_family="alpaca_spot",
    )
    assert total == 0.0
    assert rows == []

    row.state = "live_pending_entry"
    _set_live_execution(db, row, {})
    db.add(
        BrokerSymbolActionClaim(
            account_scope="alpaca:paper",
            symbol="FLAT",
            claim_token=f"claim-{uuid.uuid4().hex[:12]}",
            action="entry",
            phase="claimed",
            owner_session_id=int(row.id),
            metadata_json={"stage": "pre_http_reservation"},
        )
    )
    db.flush()
    total, rows = aggregate_open_risk_usd(
        db,
        user_id=int(user.id),
        execution_family="alpaca_spot",
    )
    assert total == 0.0
    assert rows == []
    assert count_inflight_entry_orders(
        db,
        user_id=int(user.id),
        execution_family="alpaca_spot",
    ) == 0
    assert sum_inflight_entry_risk_usd(
        db,
        user_id=int(user.id),
        execution_family="alpaca_spot",
        per_trade_fallback_usd=0.0,
    ) == 0.0


def test_alpaca_pending_without_claim_or_transport_proof_fails_closed(
    db: Session,
) -> None:
    user, variant = _user_and_variant(db, execution_family="alpaca_spot")
    _session(
        db,
        user_id=int(user.id),
        variant_id=int(variant.id),
        symbol="MISS",
        state="live_pending_entry",
        execution_family="alpaca_spot",
        live_execution={},
    )

    readers = (
        lambda: aggregate_open_risk_usd(
            db,
            user_id=int(user.id),
            execution_family="alpaca_spot",
        ),
        lambda: count_inflight_entry_orders(
            db,
            user_id=int(user.id),
            execution_family="alpaca_spot",
        ),
        lambda: sum_inflight_entry_risk_usd(
            db,
            user_id=int(user.id),
            execution_family="alpaca_spot",
            per_trade_fallback_usd=50.0,
        ),
    )
    for reader in readers:
        with pytest.raises(RuntimeError, match="pending_entry_evidence_missing"):
            reader()


def test_real_risk_scope_sql_keeps_unknown_null_family(db: Session) -> None:
    query = risk_evaluator._scope_account_risk_query(
        db.query(TradingAutomationSession),
        None,
    )
    sql = str(
        query.statement.compile(
            dialect=db.get_bind().dialect,
            compile_kwargs={"literal_binds": True},
        )
    ).lower()

    assert "execution_family is null" in sql
    assert "execution_family not in ('alpaca_spot', 'alpaca_short')" in sql


def test_alpaca_pending_exposure_requires_exact_risk_or_valid_fallback(
    db: Session,
) -> None:
    user, variant = _user_and_variant(db, execution_family="alpaca_spot")
    row = _session(
        db,
        user_id=int(user.id),
        variant_id=int(variant.id),
        symbol="PEND",
        state="live_pending_entry",
        execution_family="alpaca_spot",
    )
    claim = BrokerSymbolActionClaim(
        account_scope="alpaca:paper",
        symbol="PEND",
        claim_token=f"claim-{uuid.uuid4().hex[:12]}",
        action="entry",
        phase="submitted",
        owner_session_id=int(row.id),
        client_order_id=f"cid-{uuid.uuid4().hex[:12]}",
        broker_order_id=f"oid-{uuid.uuid4().hex[:12]}",
        metadata_json={"reserved_risk_usd": "nan"},
    )
    db.add(claim)
    db.flush()

    with pytest.raises(RuntimeError, match="pending_claim_fallback_invalid"):
        sum_inflight_entry_risk_usd(
            db,
            user_id=int(user.id),
            execution_family="alpaca_spot",
            per_trade_fallback_usd=0.0,
        )
    assert sum_inflight_entry_risk_usd(
        db,
        user_id=int(user.id),
        execution_family="alpaca_spot",
        per_trade_fallback_usd=25.0,
    ) == 25.0

    claim.phase = "resolved"
    db.flush()
    for fallback in (None, 0.0, -1.0, "bad", "nan", "inf", True):
        _set_live_execution(
            db,
            row,
            {"entry_submitted": True, "entry_inflight_risk_usd": "nan"},
        )
        with pytest.raises(RuntimeError, match="pending_session_fallback_invalid"):
            sum_inflight_entry_risk_usd(
                db,
                user_id=int(user.id),
                execution_family="alpaca_spot",
                per_trade_fallback_usd=fallback,
            )

    assert sum_inflight_entry_risk_usd(
        db,
        user_id=int(user.id),
        execution_family="alpaca_spot",
        per_trade_fallback_usd=25.0,
    ) == 25.0
    _set_live_execution(
        db,
        row,
        {"entry_submitted": True, "entry_inflight_risk_usd": 30.0},
    )
    assert sum_inflight_entry_risk_usd(
        db,
        user_id=int(user.id),
        execution_family="alpaca_spot",
        per_trade_fallback_usd=0.0,
    ) == 30.0


def test_begin_live_arm_lock_contention_or_failure_creates_nothing(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, variant = _user_and_variant(db)
    for raise_error in (False, True):
        monkeypatch.setattr(
            operator_actions,
            "_lock_live_symbol_arm",
            _contended_or_failed_lock(raise_error),
        )
        result = operator_actions.begin_live_arm(
            db,
            user_id=int(user.id),
            symbol="LOCK",
            variant_id=int(variant.id),
            execution_family="robinhood_spot",
        )
        assert result["ok"] is False
        assert result["error"] == "live_arm_generation_lock_unavailable"
        assert db.query(TradingAutomationSession).filter(
            TradingAutomationSession.mode == "live"
        ).count() == 0


def test_promote_live_arm_lock_contention_or_failure_creates_nothing(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, variant = _user_and_variant(db)
    paper = _session(
        db,
        user_id=int(user.id),
        variant_id=int(variant.id),
        symbol="PROM",
        state="finished",
        execution_family="robinhood_spot",
        mode="paper",
    )
    monkeypatch.setattr(operator_actions, "_paper_promotion_gate", lambda _row: (True, "ok"))

    for raise_error in (False, True):
        monkeypatch.setattr(
            operator_actions,
            "_lock_live_symbol_arm",
            _contended_or_failed_lock(raise_error),
        )
        result = operator_actions.promote_paper_session_to_live_arm(
            db,
            user_id=int(user.id),
            paper_session_id=int(paper.id),
            execution_family="robinhood_spot",
        )
        assert result["ok"] is False
        assert result["error"] == "live_arm_generation_lock_unavailable"
        assert db.query(TradingAutomationSession).filter(
            TradingAutomationSession.mode == "live"
        ).count() == 0
        db.refresh(paper)
        assert paper.mode == "paper"
        assert paper.state == "finished"


def test_confirm_live_arm_lock_contention_or_failure_never_promotes(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, variant = _user_and_variant(db)
    symbol = "CONF"
    viability = MomentumSymbolViability(
        symbol=symbol,
        scope="symbol",
        variant_id=int(variant.id),
        viability_score=0.95,
        paper_eligible=True,
        live_eligible=True,
        freshness_ts=datetime.utcnow(),
        regime_snapshot_json={},
        execution_readiness_json={},
        explain_json={},
        evidence_window_json={},
    )
    token = f"arm-{uuid.uuid4().hex[:12]}"
    arm = _session(
        db,
        user_id=int(user.id),
        variant_id=int(variant.id),
        symbol=symbol,
        state=operator_actions.STATE_LIVE_ARM_PENDING,
        execution_family="robinhood_spot",
        extra_snapshot={
            "arm_token": token,
            "expires_at_utc": (datetime.utcnow() + timedelta(minutes=5)).isoformat(),
        },
    )
    db.add(viability)
    db.flush()
    monkeypatch.setattr(
        operator_actions.settings,
        "chili_momentum_arm_time_viability_refresh_enabled",
        False,
        raising=False,
    )

    for raise_error in (False, True):
        monkeypatch.setattr(
            operator_actions,
            "_lock_live_symbol_arm",
            _contended_or_failed_lock(raise_error),
        )
        result = operator_actions.confirm_live_arm(
            db,
            user_id=int(user.id),
            arm_token=token,
            confirm=True,
        )
        assert result["ok"] is False
        assert result["error"] == "live_arm_generation_lock_unavailable"
        db.refresh(arm)
        assert arm.state == operator_actions.STATE_LIVE_ARM_PENDING
        assert arm.state not in {
            operator_actions.STATE_QUEUED,
            operator_actions.STATE_ARMED_PENDING_RUNNER,
        }


def test_required_viability_refresh_failure_keeps_arm_pending(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.trading.momentum_neural import pipeline

    user, variant = _user_and_variant(db)
    symbol = "STALE"
    _add_viability(
        db,
        symbol=symbol,
        variant_id=int(variant.id),
        freshness_ts=datetime.utcnow() - timedelta(seconds=400),
    )
    token = f"arm-{uuid.uuid4().hex[:12]}"
    arm = _session(
        db,
        user_id=int(user.id),
        variant_id=int(variant.id),
        symbol=symbol,
        state=operator_actions.STATE_LIVE_ARM_PENDING,
        execution_family="robinhood_spot",
        extra_snapshot={
            "arm_token": token,
            "expires_at_utc": (datetime.utcnow() + timedelta(minutes=5)).isoformat(),
        },
    )
    monkeypatch.setattr(
        operator_actions.settings,
        "chili_momentum_arm_time_viability_refresh_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        operator_actions.settings,
        "chili_momentum_risk_viability_max_age_seconds",
        600.0,
        raising=False,
    )
    monkeypatch.setattr(
        pipeline,
        "run_momentum_neural_tick",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("refresh offline")),
    )

    result = operator_actions.confirm_live_arm(
        db,
        user_id=int(user.id),
        arm_token=token,
        confirm=True,
    )

    assert result["ok"] is False
    assert result["error"] == "viability_refresh_unavailable"
    db.refresh(arm)
    assert arm.state == operator_actions.STATE_LIVE_ARM_PENDING


def test_promotion_reuses_existing_active_generation_sequentially(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_promotion_dependencies(monkeypatch)
    user, variant = _user_and_variant(db)
    paper = _session(
        db,
        user_id=int(user.id),
        variant_id=int(variant.id),
        symbol="SEQD",
        state="finished",
        execution_family="robinhood_spot",
        mode="paper",
    )
    _add_viability(db, symbol="SEQD", variant_id=int(variant.id))

    first = operator_actions.promote_paper_session_to_live_arm(
        db,
        user_id=int(user.id),
        paper_session_id=int(paper.id),
        execution_family="robinhood_spot",
    )
    second = operator_actions.promote_paper_session_to_live_arm(
        db,
        user_id=int(user.id),
        paper_session_id=int(paper.id),
        execution_family="robinhood_spot",
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["deduped"] is True
    assert second["session_id"] == first["session_id"]
    assert db.query(TradingAutomationSession).filter(
        TradingAutomationSession.user_id == int(user.id),
        TradingAutomationSession.symbol == "SEQD",
        TradingAutomationSession.mode == "live",
    ).count() == 1


def test_promotion_reuses_existing_active_generation_concurrently(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("transaction advisory-lock regression requires PostgreSQL")
    _stub_promotion_dependencies(monkeypatch)
    user, variant = _user_and_variant(db)
    paper = _session(
        db,
        user_id=int(user.id),
        variant_id=int(variant.id),
        symbol="CONC",
        state="finished",
        execution_family="robinhood_spot",
        mode="paper",
    )
    _add_viability(db, symbol="CONC", variant_id=int(variant.id))
    user_id = int(user.id)
    paper_id = int(paper.id)
    db.commit()

    make_session = sessionmaker(autocommit=False, autoflush=False, bind=db.get_bind())
    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []

    def _promote() -> None:
        worker_db = make_session()
        try:
            barrier.wait(timeout=10)
            result = operator_actions.promote_paper_session_to_live_arm(
                worker_db,
                user_id=user_id,
                paper_session_id=paper_id,
                execution_family="robinhood_spot",
            )
            worker_db.commit()
            results.append(result)
        except BaseException as exc:  # surfaced below with both worker outcomes
            worker_db.rollback()
            errors.append(exc)
        finally:
            worker_db.close()

    workers = [threading.Thread(target=_promote, daemon=True) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(results) == 2
    assert all(result["ok"] is True for result in results)
    assert sum(result.get("deduped") is True for result in results) == 1
    assert len({int(result["session_id"]) for result in results}) == 1
    db.expire_all()
    assert db.query(TradingAutomationSession).filter(
        TradingAutomationSession.user_id == user_id,
        TradingAutomationSession.symbol == "CONC",
        TradingAutomationSession.mode == "live",
    ).count() == 1
