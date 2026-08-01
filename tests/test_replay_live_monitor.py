from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.config import settings
from app.models.captured_paper_selection_frontier import (
    CapturedPaperSelectionRouteState,
)
from app.models.trading import (
    BrainBatchJob,
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    MomentumSymbolViability,
    Trade,
    TradingAutomationEvent,
    TradingAutomationRuntimeSnapshot,
    TradingAutomationSession,
)
from app.services.trading.batch_job_constants import (
    JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT,
)
from app.services.trading.momentum_neural import lane_health as lh


_PAPER_ACCOUNT_ID = "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f"
_PAPER_RUNTIME_GENERATION = "64fb7911-1a67-4e2c-a1ca-73cbe6efe5c6"


def _runtime_heartbeat(
    db,
    *,
    age_seconds: float,
    captured_paper: bool,
    owner_instance_id: str | None = None,
    generation_started_age_seconds: float = 120.0,
    tamper_content: bool = False,
) -> BrainBatchJob:
    now = datetime.utcnow()
    row_started_at = now - timedelta(seconds=age_seconds + 1)
    heartbeat_at = now - timedelta(seconds=age_seconds)
    owner = owner_instance_id or str(uuid.uuid4())
    generation_started_at = (
        datetime.now(timezone.utc)
        - timedelta(seconds=generation_started_age_seconds)
    )
    meta = {
        "schema": lh.LIVE_LOOP_HEARTBEAT_SCHEMA,
        "scope": lh.LIVE_LOOP_HEARTBEAT_SCOPE,
        "owner": "momentum_live_runner_loop",
        "owner_instance_id": owner,
        "generation": 1,
        "generation_identity": f"{owner}:1",
        "generation_started_at_utc": (
            generation_started_at.isoformat().replace("+00:00", "Z")
        ),
    }
    if captured_paper:
        meta.update(
            {
                "schema": lh.LIVE_LOOP_HEARTBEAT_SCHEMA_V2,
                "account_scope": "alpaca:paper",
                "expected_account_id": _PAPER_ACCOUNT_ID,
                "runtime_generation": _PAPER_RUNTIME_GENERATION,
                "execution_family": "alpaca_spot",
                "live_cash_authorized": False,
                "row_started_at_utc": (
                    row_started_at.replace(tzinfo=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
                "heartbeat_at_utc": (
                    heartbeat_at.replace(tzinfo=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
            }
        )
        meta["content_sha256"] = lh._heartbeat_content_sha256(meta)
        if tamper_content:
            meta["runtime_generation"] = str(uuid.uuid4())
    row = BrainBatchJob(
        id=str(uuid.uuid4()),
        job_type=JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT,
        status="ok",
        started_at=row_started_at,
        ended_at=heartbeat_at,
        meta_json=meta,
    )
    db.add(row)
    db.commit()
    return row


def _variant(db):
    row = MomentumStrategyVariant(
        family="ross_momentum",
        variant_key="live-observer-test",
        label="Live observer test",
        params_json={},
        is_active=True,
        execution_family="robinhood_spot",
    )
    db.add(row)
    db.flush()
    return row


def _paper_variant(db, key: str):
    row = MomentumStrategyVariant(
        family="ross_momentum",
        variant_key=key,
        label=f"Captured PAPER {key}",
        params_json={},
        is_active=True,
        execution_family="alpaca_spot",
    )
    db.add(row)
    db.flush()
    return row


def _captured_route(
    db,
    *,
    symbol: str,
    variant,
    sequence: int,
    observed_at: datetime,
    state: str = "eligible",
    paper_eligible: bool = False,
    live_eligible: bool = False,
    warning: str | None = None,
):
    from app.services.trading.momentum_neural.captured_paper_initial_candidate_reader import (
        _PROVENANCE_SCHEMA_VERSION,
        _viability_observation_sha256,
    )

    authority_sha256 = "a" * 64
    batch_sha256 = f"{sequence % 16:x}" * 64
    evidence_sha256 = "e" * 64
    route = CapturedPaperSelectionRouteState(
        account_scope="alpaca:paper",
        expected_account_id=_PAPER_ACCOUNT_ID,
        activation_generation=_PAPER_RUNTIME_GENERATION,
        execution_family="alpaca_spot",
        authority_sha256=authority_sha256,
        symbol=symbol,
        variant_id=variant.id,
        latest_source_sequence=sequence,
        state=state,
        evidence_sha256=evidence_sha256,
        batch_sha256=batch_sha256,
        source_event_at=observed_at - timedelta(seconds=1),
        source_available_at=observed_at,
        version=1,
        state_sha256="f" * 64,
        created_at=observed_at,
        updated_at=observed_at,
    )
    db.add(route)
    viability = None
    if state == "eligible":
        provenance = {
            "schema_version": _PROVENANCE_SCHEMA_VERSION,
            "account_scope": "alpaca:paper",
            "expected_account_id": _PAPER_ACCOUNT_ID,
            "activation_generation": _PAPER_RUNTIME_GENERATION,
            "authority_sha256": authority_sha256,
            "policy_sha256": "1" * 64,
            "settings_projection_sha256": "2" * 64,
            "code_build_sha256": "3" * 64,
            "variant_set_sha256": "4" * 64,
            "variant_id": variant.id,
            "batch_sha256": batch_sha256,
            "observation_sha256": evidence_sha256,
            "source_name": "test_source",
            "source_generation": "11111111-1111-4111-8111-111111111111",
            "source_sequence": sequence,
            "queue_receipt_sha256": "5" * 64,
            "coverage_receipt_sha256": "6" * 64,
            "paper_only_strategy_override": False,
            "live_cash_authorized": False,
        }
        explain = {
            "scorer_output": {
                "warnings": [warning] if warning else [],
                "rationale": "persisted test rationale",
            },
            "captured_paper_selection_producer": provenance,
        }
        viability = MomentumSymbolViability(
            symbol=symbol,
            scope="symbol",
            variant_id=variant.id,
            viability_score=0.67 if live_eligible else 0.31,
            paper_eligible=paper_eligible,
            live_eligible=live_eligible,
            freshness_ts=observed_at.replace(tzinfo=None),
            regime_snapshot_json={},
            execution_readiness_json={
                "captured_paper_selection_producer": provenance
            },
            explain_json=explain,
            evidence_window_json={
                "captured_paper_selection_producer": provenance
            },
            source_node_id="captured_paper_selection_producer",
            correlation_id=f"test-{symbol.lower()}",
            created_at=observed_at.replace(tzinfo=None),
            updated_at=observed_at.replace(tzinfo=None),
        )
        observation_sha256 = _viability_observation_sha256(
            viability,
            provenance=provenance,
            route_state=route,
        )
        provenance = {**provenance, "observation_sha256": observation_sha256}
        viability.execution_readiness_json = {
            "captured_paper_selection_producer": provenance
        }
        viability.explain_json = {
            **explain,
            "captured_paper_selection_producer": provenance,
        }
        viability.evidence_window_json = {
            "captured_paper_selection_producer": provenance
        }
        route.evidence_sha256 = observation_sha256
        db.add(viability)
    return route, viability


def test_live_monitor_requires_paired_account(client):
    response = client.get("/api/trading/momentum/replay/live")
    assert response.status_code == 403


def test_zero_session_monitor_uses_fresh_exact_captured_paper_heartbeat(
    paired_client,
    db,
    monkeypatch,
):
    from app.services.trading.momentum_neural.live_monitor import (
        clear_live_monitor_caches,
    )

    client, _user = paired_client
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        _PAPER_ACCOUNT_ID,
        raising=False,
    )
    row = _runtime_heartbeat(db, age_seconds=2, captured_paper=True)
    clear_live_monitor_caches()

    response = client.get("/api/trading/momentum/replay/live")
    assert response.status_code == 200
    body = response.json()
    assert body["active_symbol_count"] == 0
    assert body["latest_runtime_utc"] == (
        row.ended_at.replace(tzinfo=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    assert body["observer"]["broker_calls"] == 0
    assert body["observer"]["provider_calls"] == 0
    assert body["observer"]["writes"] == 0


@pytest.mark.parametrize("case", ("generic", "stale", "tampered", "overlap"))
def test_non_exact_heartbeat_never_populates_zero_session_runtime(
    paired_client,
    db,
    monkeypatch,
    case,
):
    from app.services.trading.momentum_neural.live_monitor import (
        clear_live_monitor_caches,
    )

    client, _user = paired_client
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        _PAPER_ACCOUNT_ID,
        raising=False,
    )
    if case == "generic":
        _runtime_heartbeat(db, age_seconds=2, captured_paper=False)
    elif case == "stale":
        _runtime_heartbeat(db, age_seconds=600, captured_paper=True)
    elif case == "tampered":
        _runtime_heartbeat(
            db,
            age_seconds=2,
            captured_paper=True,
            tamper_content=True,
        )
    else:
        _runtime_heartbeat(
            db,
            age_seconds=10,
            captured_paper=True,
            owner_instance_id=str(uuid.uuid4()),
            generation_started_age_seconds=120,
        )
        _runtime_heartbeat(
            db,
            age_seconds=2,
            captured_paper=True,
            owner_instance_id=str(uuid.uuid4()),
            generation_started_age_seconds=30,
        )
    clear_live_monitor_caches()

    response = client.get("/api/trading/momentum/replay/live")
    assert response.status_code == 200
    assert response.json()["latest_runtime_utc"] is None


def test_live_monitor_exposes_persisted_selection_funnel_without_creating_timeline_rows(
    paired_client,
    db,
    monkeypatch,
):
    from app.services.trading.momentum_neural.live_monitor import (
        clear_live_monitor_caches,
    )

    client, user = paired_client
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        _PAPER_ACCOUNT_ID,
        raising=False,
    )
    _runtime_heartbeat(db, age_seconds=2, captured_paper=True)
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=4)
    blocked_variant = _paper_variant(db, "live-funnel-blocked")
    live_variant = _paper_variant(db, "live-funnel-live")
    live_sibling_variant = _paper_variant(db, "live-funnel-live-sibling")
    admitted_variant = _paper_variant(db, "live-funnel-admitted")
    _captured_route(
        db,
        symbol="BLOCK",
        variant=blocked_variant,
        sequence=101,
        observed_at=observed_at,
        warning="Below A-setup quality floor — not a live setup",
    )
    _captured_route(
        db,
        symbol="READY",
        variant=live_variant,
        sequence=102,
        observed_at=observed_at + timedelta(seconds=1),
        paper_eligible=True,
        live_eligible=True,
    )
    _captured_route(
        db,
        symbol="READY",
        variant=live_sibling_variant,
        sequence=105,
        observed_at=observed_at + timedelta(seconds=3),
        warning="Below A-setup quality floor — not a live setup",
    )
    _admit_route, admitted_viability = _captured_route(
        db,
        symbol="ADMIT",
        variant=admitted_variant,
        sequence=103,
        observed_at=observed_at + timedelta(seconds=2),
        paper_eligible=True,
        live_eligible=True,
    )
    db.flush()
    from app.services.trading.momentum_neural.captured_paper_initial_admission import (
        INITIAL_SESSION_MATERIAL_SCHEMA_VERSION,
        INITIAL_PREOWNER_MARKER_SCHEMA_VERSION,
        _sha256_json,
        captured_paper_initial_viability_sha256,
    )
    from app.services.trading.momentum_neural.captured_paper_preowner_promotion import (
        CAPTURED_PAPER_INITIAL_MATERIAL_KEY,
        CAPTURED_PAPER_PENDING_OWNER_KEY,
        PENDING_OWNER_SCHEMA_VERSION,
    )

    stored_at = (observed_at + timedelta(seconds=3)).replace(tzinfo=None)
    viability_sha256 = captured_paper_initial_viability_sha256(
        admitted_viability
    )
    material_body = {
        "schema_version": INITIAL_SESSION_MATERIAL_SCHEMA_VERSION,
        "symbol": "ADMIT",
        "user_id": user.id,
        "variant_id": admitted_variant.id,
        "account_scope": "alpaca:paper",
        "expected_account_id": _PAPER_ACCOUNT_ID,
        "runtime_generation": _PAPER_RUNTIME_GENERATION,
        "execution_family": "alpaca_spot",
        "viability_snapshot_sha256": viability_sha256,
    }
    initial_material_sha256 = _sha256_json(material_body)
    material = {
        **material_body,
        "material_sha256": initial_material_sha256,
    }
    marker_body = {
        "schema_version": INITIAL_PREOWNER_MARKER_SCHEMA_VERSION,
        "session_id": 0,
        "symbol": "ADMIT",
        "user_id": user.id,
        "variant_id": admitted_variant.id,
        "account_scope": "alpaca:paper",
        "expected_account_id": _PAPER_ACCOUNT_ID,
        "runtime_generation": _PAPER_RUNTIME_GENERATION,
        "execution_family": "alpaca_spot",
        "viability_snapshot_sha256": viability_sha256,
        "initial_material_sha256": initial_material_sha256,
    }
    session = TradingAutomationSession(
        user_id=user.id,
        venue="alpaca",
        execution_family="alpaca_spot",
        mode="live",
        symbol="ADMIT",
        variant_id=admitted_variant.id,
        state="captured_paper_preowner",
        risk_snapshot_json={},
        source_node_id="captured_paper_initial_admission",
        started_at=stored_at,
        created_at=stored_at,
        updated_at=stored_at,
    )
    db.add(session)
    db.flush()
    marker_body["session_id"] = session.id
    marker = {**marker_body, "content_sha256": _sha256_json(marker_body)}
    pending_body = {
        "schema_version": PENDING_OWNER_SCHEMA_VERSION,
        "session_id": session.id,
        "symbol": "ADMIT",
        "variant_id": admitted_variant.id,
        "account_scope": "alpaca:paper",
        "expected_account_id": _PAPER_ACCOUNT_ID,
        "runtime_generation": _PAPER_RUNTIME_GENERATION,
        "execution_family": "alpaca_spot",
        "initial_material_sha256": initial_material_sha256,
        "preowner_marker_sha256": marker["content_sha256"],
        "viability_snapshot_sha256": viability_sha256,
    }
    pending = {
        **pending_body,
        "content_sha256": _sha256_json(pending_body),
    }
    session.state = "queued_live"
    session.correlation_id = initial_material_sha256
    session.source_node_id = "captured_paper_preowner_promotion"
    session.risk_snapshot_json = {
        CAPTURED_PAPER_INITIAL_MATERIAL_KEY: material,
        CAPTURED_PAPER_PENDING_OWNER_KEY: pending,
    }
    db.add(
        TradingAutomationEvent(
            session_id=session.id,
            ts=stored_at,
            event_type="captured_paper_initial_preowner_committed",
            payload_json={
                "schema_version": INITIAL_PREOWNER_MARKER_SCHEMA_VERSION,
                "symbol": "ADMIT",
                "account_scope": "alpaca:paper",
                "expected_account_id": _PAPER_ACCOUNT_ID,
                "runtime_generation": _PAPER_RUNTIME_GENERATION,
                "initial_material_sha256": initial_material_sha256,
                "preowner_marker_sha256": marker["content_sha256"],
            },
            source_node_id="captured_paper_initial_admission",
        )
    )
    db.commit()
    clear_live_monitor_caches()

    response = client.get("/api/trading/momentum/replay/live")
    assert response.status_code == 200
    body = response.json()
    assert body["symbols"] == []
    assert body["series"] == {}
    funnel = body["selection_funnel"]
    assert funnel["status"] == "ready"
    assert funnel["counts"] == {
        "monitored": 3,
        "routed_scored": 3,
        "live_eligible": 2,
        "setup_admitted": 1,
    }
    rows = {row["symbol"]: row for row in funnel["rows"]}
    assert rows["BLOCK"]["route_state"] == "scored"
    assert rows["BLOCK"]["stage"] == "routed_scored"
    assert rows["BLOCK"]["live_eligible"] is False
    assert "not a live setup" in rows["BLOCK"]["veto_reason"]
    assert rows["READY"]["stage"] == "live_eligible"
    assert rows["READY"]["paper_eligible"] is True
    assert rows["READY"]["route_count"] == 2
    assert rows["READY"]["variant_id"] == live_variant.id
    assert rows["READY"]["veto_reason"] is None
    assert rows["ADMIT"]["stage"] == "setup_admitted"
    assert rows["ADMIT"]["session_id"] == session.id
    assert rows["ADMIT"]["session_state"] == "queued_live"
    assert all(row["age_seconds"] is not None for row in rows.values())
    assert body["observer"]["broker_calls"] == 0
    assert body["observer"]["provider_calls"] == 0
    assert body["observer"]["writes"] == 0


def test_live_monitor_coverage_tombstone_never_reuses_older_eligible_viability(
    paired_client,
    db,
    monkeypatch,
):
    from app.services.trading.momentum_neural.live_monitor import (
        clear_live_monitor_caches,
    )

    client, _user = paired_client
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        _PAPER_ACCOUNT_ID,
        raising=False,
    )
    _runtime_heartbeat(db, age_seconds=2, captured_paper=True)
    variant = _paper_variant(db, "live-funnel-coverage")
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=3)
    route, _viability = _captured_route(
        db,
        symbol="GAP",
        variant=variant,
        sequence=104,
        observed_at=observed_at,
        state="coverage_unavailable",
    )
    stale_provenance = {
        "account_scope": "alpaca:paper",
        "expected_account_id": _PAPER_ACCOUNT_ID,
        "activation_generation": _PAPER_RUNTIME_GENERATION,
        "authority_sha256": route.authority_sha256,
        "variant_id": variant.id,
        "batch_sha256": route.batch_sha256,
        "observation_sha256": route.evidence_sha256,
        "source_sequence": route.latest_source_sequence,
        "paper_only_strategy_override": False,
        "live_cash_authorized": False,
    }
    db.add(
        MomentumSymbolViability(
            symbol="GAP",
            scope="symbol",
            variant_id=variant.id,
            viability_score=0.9,
            paper_eligible=True,
            live_eligible=True,
            freshness_ts=(observed_at - timedelta(minutes=1)).replace(
                tzinfo=None
            ),
            regime_snapshot_json={},
            execution_readiness_json={
                "captured_paper_selection_producer": stale_provenance
            },
            explain_json={
                "scorer_output": {"warnings": []},
                "captured_paper_selection_producer": stale_provenance,
            },
            evidence_window_json={
                "captured_paper_selection_producer": stale_provenance
            },
            source_node_id="captured_paper_selection_producer",
        )
    )
    db.commit()
    clear_live_monitor_caches()

    response = client.get("/api/trading/momentum/replay/live")
    assert response.status_code == 200
    funnel = response.json()["selection_funnel"]
    assert funnel["counts"] == {
        "monitored": 1,
        "routed_scored": 0,
        "live_eligible": 0,
        "setup_admitted": 0,
    }
    assert funnel["rows"][0]["symbol"] == "GAP"
    assert funnel["rows"][0]["route_state"] == "coverage_unavailable"
    assert funnel["rows"][0]["stage"] == "monitored"
    assert funnel["rows"][0]["live_eligible"] is False


def test_live_monitor_rejects_viability_mutated_after_observation_hash(
    paired_client,
    db,
    monkeypatch,
):
    from app.services.trading.momentum_neural.live_monitor import (
        clear_live_monitor_caches,
    )

    client, _user = paired_client
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        _PAPER_ACCOUNT_ID,
        raising=False,
    )
    _runtime_heartbeat(db, age_seconds=2, captured_paper=True)
    variant = _paper_variant(db, "live-funnel-mutated")
    _route, viability = _captured_route(
        db,
        symbol="MUT",
        variant=variant,
        sequence=106,
        observed_at=datetime.now(timezone.utc) - timedelta(seconds=2),
        paper_eligible=False,
        live_eligible=False,
        warning="Not a live setup",
    )
    viability.paper_eligible = True
    viability.live_eligible = True
    db.commit()
    clear_live_monitor_caches()

    response = client.get("/api/trading/momentum/replay/live")
    assert response.status_code == 200
    funnel = response.json()["selection_funnel"]
    assert funnel["counts"]["routed_scored"] == 1
    assert funnel["counts"]["live_eligible"] == 0
    assert funnel["rows"][0]["stage"] == "routed_scored"
    assert funnel["rows"][0]["live_eligible"] is False


def test_live_monitor_returns_runtime_pnl_events_and_bounded_candles(paired_client, db):
    from app.services.trading.momentum_neural.live_monitor import clear_live_monitor_caches

    client, user = paired_client
    variant = _variant(db)
    now = datetime.utcnow().replace(second=0, microsecond=0)
    active = TradingAutomationSession(
        user_id=user.id,
        venue="robinhood",
        execution_family="robinhood_spot",
        mode="live",
        symbol="OBS",
        variant_id=variant.id,
        state="live_entered",
        risk_snapshot_json={
            "momentum_live_execution": {
                "position": {
                    "quantity": 10,
                    "avg_entry_price": 2.0,
                    "stop_price": 1.8,
                    "target_price": 2.6,
                },
                "last_mid": 2.2,
                "last_tick_utc": now.isoformat(),
                "realized_pnl_usd": 5.0,
            }
        },
        started_at=now - timedelta(minutes=15),
        updated_at=now,
    )
    completed = TradingAutomationSession(
        user_id=user.id,
        venue="robinhood",
        execution_family="robinhood_spot",
        mode="live",
        symbol="OBS",
        variant_id=variant.id,
        state="live_finished",
        risk_snapshot_json={},
        started_at=now - timedelta(minutes=40),
        ended_at=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=10),
    )
    db.add_all([active, completed])
    db.flush()
    db.add(
        TradingAutomationRuntimeSnapshot(
            session_id=active.id,
            user_id=user.id,
            symbol="OBS",
            mode="live",
            lane="live",
            state="live_entered",
            strategy_family="ross_momentum",
            strategy_label="Ross momentum",
            current_position_state="live-long",
            last_action="live_entry_filled",
            last_price=2.2,
            latest_levels_json={"entry": 2.0, "stop": 1.8, "target": 2.6},
            updated_at=now,
        )
    )
    db.add(
        TradingAutomationEvent(
            session_id=active.id,
            ts=now - timedelta(minutes=2),
            event_type="live_entry_filled",
            payload_json={"reason": "breakout_confirmed"},
        )
    )
    db.add(
        MomentumAutomationOutcome(
            session_id=completed.id,
            user_id=user.id,
            variant_id=variant.id,
            symbol="OBS",
            mode="live",
            execution_family="robinhood_spot",
            terminal_state="live_finished",
            terminal_at=now - timedelta(minutes=10),
            outcome_class="target_hit",
            realized_pnl_usd=7.0,
        )
    )
    db.add(
        Trade(
            user_id=user.id,
            ticker="OBS",
            direction="long",
            entry_price=2.0,
            quantity=10,
            status="open",
            broker_source="robinhood",
        )
    )
    db.flush()
    for offset, bid, ask, volume in (
        (3, 1.99, 2.01, 1000),
        (2, 2.04, 2.06, 1120),
        (1, 2.14, 2.16, 1300),
        (0, 2.19, 2.21, 1450),
    ):
        db.execute(
            text(
                "INSERT INTO momentum_nbbo_spread_tape "
                "(symbol, observed_at, bid, ask, mid, day_volume, source) "
                "VALUES ('OBS', :ts, :bid, :ask, (:bid + :ask) / 2.0, :volume, 'test')"
            ),
            {"ts": now - timedelta(minutes=offset), "bid": bid, "ask": ask, "volume": volume},
        )
    db.commit()
    clear_live_monitor_caches()

    response = client.get("/api/trading/momentum/replay/live")
    assert response.status_code == 200
    assert response.headers["x-chili-live-observer"] == "read-only"
    body = response.json()
    assert body["read_only"] is True
    assert body["observer"]["broker_calls"] == 0
    assert body["observer"]["provider_calls"] == 0
    assert body["observer"]["writes"] == 0
    assert body["observer"]["quote_row_cap_per_symbol"] == 480
    assert body["totals"] == {
        "realized_usd": 12.0,
        "unrealized_usd": 2.0,
        "trades": 1,
        "total_usd": 14.0,
    }
    symbol = next(row for row in body["symbols"] if row["symbol"] == "OBS")
    assert symbol["state"] == "live_entered"
    assert symbol["armed"] is False
    assert symbol["positions"][0]["quantity"] == 10.0
    assert symbol["pnl"]["realized_usd"] == 12.0
    assert symbol["pnl"]["unrealized_usd"] == 2.0
    assert symbol["events"][0]["stage"] == "live_entry_filled"
    assert body["series"]["OBS"][-1][4] == 2.2


def test_live_monitor_snapshot_is_single_flight_cached(monkeypatch):
    from app.services.trading.momentum_neural import live_monitor as monitor

    monitor.clear_live_monitor_caches()
    calls = {"state": 0, "chart": 0}

    def fake_state(_db, *, user_id, now_utc):
        calls["state"] += 1
        return {
            "ok": True,
            "read_only": True,
            "symbols": [{"symbol": "CACHE"}],
            "observer": {},
        }

    def fake_chart(_db, *, user_id, symbols, now_utc, now_mono):
        calls["chart"] += 1
        return {"CACHE": []}, "2026-07-10T00:00:00Z"

    monkeypatch.setattr(monitor, "_build_state_snapshot", fake_state)
    monkeypatch.setattr(monitor, "_cached_chart_series", fake_chart)
    first = monitor.live_monitor_snapshot(object(), user_id=71)
    second = monitor.live_monitor_snapshot(object(), user_id=71)
    assert first is second
    assert calls == {"state": 1, "chart": 1}


def test_minute_bar_series_builds_ohlc_without_external_market_data():
    from app.services.trading.momentum_neural.live_monitor import _minute_bar_series

    at = datetime(2026, 7, 10, 14, 31, 5)
    rows = [
        ("OBS", at, 9.9, 10.1, 10.0, 100.0),
        ("OBS", at + timedelta(seconds=20), 10.4, 10.6, 10.5, 140.0),
        ("OBS", at + timedelta(seconds=40), 9.7, 9.9, 9.8, 170.0),
    ]
    bars = _minute_bar_series(rows)
    assert bars["OBS"] == [["14:31", 10.0, 10.5, 9.8, 9.8, 70.0]]
