from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.config import settings
from app.models.trading import (
    BrainBatchJob,
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
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


def test_live_monitor_returns_runtime_pnl_events_and_bounded_candles(
    paired_client,
    db,
    monkeypatch,
):
    from app.services.trading.momentum_neural import live_monitor as monitor

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
    monkeypatch.setattr(
        monitor,
        "_captured_watch_inventory",
        lambda _db, *, user_id, now_utc: (
            [
                {
                    "symbol": "OBS",
                    "updated_at": now,
                    "confidence": 0.99,
                    "strategy": "Must not replace live session strategy",
                    "route_count": 2,
                    "policy_eligible_route_count": 2,
                    "policy_eligible": True,
                    "last_price": 999.0,
                    "activation_generation": "11111111-1111-4111-8111-111111111111",
                    "runtime_heartbeat_at": now,
                    "runtime_stale": False,
                }
            ],
            {"status": "ok", "heartbeat_at": now},
        ),
    )
    db.commit()
    monitor.clear_live_monitor_caches()

    response = client.get("/api/trading/momentum/replay/live")
    assert response.status_code == 200
    assert response.headers["x-chili-live-observer"] == "read-only"
    body = response.json()
    assert body["read_only"] is True
    assert body["observer"]["broker_calls"] == 0
    assert body["observer"]["provider_calls"] == 0
    assert body["observer"]["writes"] == 0
    assert body["observer"]["quote_row_cap_per_symbol"] == 120
    assert body["totals"] == {
        "realized_usd": 12.0,
        "unrealized_usd": 2.0,
        "trades": 1,
        "total_usd": 14.0,
    }
    symbol = next(row for row in body["symbols"] if row["symbol"] == "OBS")
    assert len([row for row in body["symbols"] if row["symbol"] == "OBS"]) == 1
    assert body["watching_symbol_count"] == 0
    assert symbol["card_status"] == "IN POSITION"
    assert symbol["state"] == "live_entered"
    assert symbol["strategy"] == "Ross momentum"
    assert symbol["last_price"] == 2.2
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


def test_live_monitor_adds_captured_paper_watch_chart_without_synthetic_session(
    paired_client,
    db,
    monkeypatch,
):
    from app.services.trading.momentum_neural import live_monitor as monitor

    client, _user = paired_client
    now = datetime.utcnow().replace(second=0, microsecond=0)
    candidate = {
        "symbol": "WATCH",
        "updated_at": now - timedelta(seconds=12),
        "confidence": 0.91,
        "strategy": "Breakout continuation",
        "route_count": 3,
        "policy_eligible_route_count": 1,
        "policy_eligible": True,
        "last_price": 4.25,
        "activation_generation": "11111111-1111-4111-8111-111111111111",
        "runtime_heartbeat_at": now - timedelta(seconds=4),
        "runtime_stale": False,
    }
    monkeypatch.setattr(
        monitor,
        "_captured_watch_inventory",
        lambda _db, *, user_id, now_utc: (
            [candidate],
            {"status": "ok", "heartbeat_at": candidate["runtime_heartbeat_at"]},
        ),
    )
    db.execute(
        text(
            "INSERT INTO momentum_nbbo_spread_tape "
            "(symbol, observed_at, bid, ask, mid, day_volume, source) "
            "VALUES ('WATCH', :ts, 4.24, 4.26, 4.25, 1000, 'test')"
        ),
        {"ts": now - timedelta(seconds=20)},
    )
    db.commit()
    monitor.clear_live_monitor_caches()

    response = client.get("/api/trading/momentum/replay/live")

    assert response.status_code == 200
    body = response.json()
    assert body["active_symbol_count"] == 0
    assert body["watching_symbol_count"] == 1
    assert body["observer"]["captured_watch_status"] == "ok"
    assert body["latest_runtime_utc"] is not None
    assert [row["symbol"] for row in body["symbols"]] == ["WATCH"]
    watched = body["symbols"][0]
    assert watched["card_status"] == "SETUP"
    assert watched["active"] is False
    assert watched["armed"] is False
    assert watched["lanes"] == []
    assert watched["positions"] == []
    assert watched["events"] == []
    assert watched["watch"]["policy_eligible"] is True
    assert body["series"]["WATCH"][-1][4] == 4.25


def test_live_monitor_rejects_mutated_captured_viability_observation():
    from app.services.trading.momentum_neural import live_monitor as monitor

    available_at = datetime(2026, 8, 3, 14, 5, 0)
    identity = {
        "expected_account_id": "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f",
        "runtime_generation": "11111111-1111-4111-8111-111111111111",
    }
    row = {
        "symbol": "WATCH",
        "variant_id": 7,
        "latest_source_sequence": 22,
        "batch_sha256": "b" * 64,
        "source_event_at": available_at - timedelta(seconds=1),
        "source_available_at": available_at,
        "authority_sha256": "a" * 64,
        "policy_sha256": "c" * 64,
        "settings_projection_sha256": "d" * 64,
        "code_build_sha256": "e" * 64,
        "variant_set_sha256": "f" * 64,
        "viability_score": 0.75,
        "paper_eligible": False,
        "live_eligible": False,
        "route_count": 1,
        "policy_eligible_route_count": 0,
        "freshness_ts": available_at,
        "regime_snapshot_json": {},
        "correlation_id": "watch-correlation",
        "family": "ross_momentum",
        "label": "Breakout continuation",
    }
    observation = {
        "schema_version": "chili.captured-paper-selection-observation.v1",
        "source_sequence": 22,
        "source_event_at": "2026-08-03T14:04:59Z",
        "source_available_at": "2026-08-03T14:05:00Z",
        "symbol": "WATCH",
        "variant_id": 7,
        "viability_score": 0.75,
        "paper_eligible": False,
        "live_eligible": False,
        "regime_snapshot_json": {},
        "execution_readiness_json": {},
        "explain_json": {},
        "evidence_window_json": {},
        "correlation_id": "watch-correlation",
    }
    row["evidence_sha256"] = monitor._sha256_json(observation)
    provenance = {
        "schema_version": "chili.captured-paper-selection-viability-provenance.v1",
        "account_scope": "alpaca:paper",
        "expected_account_id": identity["expected_account_id"],
        "activation_generation": identity["runtime_generation"],
        "authority_sha256": row["authority_sha256"],
        "policy_sha256": row["policy_sha256"],
        "settings_projection_sha256": row["settings_projection_sha256"],
        "code_build_sha256": row["code_build_sha256"],
        "variant_set_sha256": row["variant_set_sha256"],
        "variant_id": 7,
        "batch_sha256": row["batch_sha256"],
        "observation_sha256": row["evidence_sha256"],
        "source_name": "captured_queue",
        "source_generation": "22222222-2222-4222-8222-222222222222",
        "source_sequence": 22,
        "queue_receipt_sha256": "1" * 64,
        "coverage_receipt_sha256": "2" * 64,
        "paper_only_strategy_override": False,
        "live_cash_authorized": False,
    }
    for key in (
        "execution_readiness_json",
        "explain_json",
        "evidence_window_json",
    ):
        row[key] = {monitor._CAPTURED_PROVENANCE_KEY: dict(provenance)}

    assert monitor._validated_captured_watch_route(row, identity=identity) is not None

    row["paper_eligible"] = True
    row["live_eligible"] = True
    row["policy_eligible_route_count"] = 1
    assert monitor._validated_captured_watch_route(row, identity=identity) is None


def test_live_monitor_binds_v1_heartbeat_to_one_owned_active_paper_generation(
    paired_client,
    db,
    monkeypatch,
):
    from app.services.trading.momentum_neural import live_monitor as monitor

    _client, user = paired_client
    now = datetime.utcnow().replace(microsecond=0)
    owner = str(uuid.uuid4())
    runtime_generation = str(uuid.uuid4())
    db.add(
        MomentumStrategyVariant(
            family="ross_momentum",
            variant_key=f"captured-observer-{runtime_generation[:8]}",
            label="Captured observer binding",
            params_json={},
            is_active=True,
            execution_family="alpaca_spot",
            refinement_meta_json={
                "captured_paper_variant_binding": {
                    "schema_version": (
                        "chili.captured-paper-variant-binding-meta.v1"
                    ),
                    "account_scope": "alpaca:paper",
                    "execution_family": "alpaca_spot",
                    "expected_account_id": (
                        "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f"
                    ),
                    "activation_generation": runtime_generation,
                    "policy_sha256": "a" * 64,
                    "settings_projection_sha256": "b" * 64,
                    "code_build_sha256": "c" * 64,
                    "bound_at": now.replace(tzinfo=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "paper_order_submission_authorized": False,
                    "live_cash_authorized": False,
                    "real_money_authorized": False,
                }
            },
        )
    )
    db.add(
        BrainBatchJob(
            id=str(uuid.uuid4()),
            job_type=JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT,
            status="ok",
            started_at=now - timedelta(seconds=1),
            ended_at=now,
            meta_json={
                "schema": "momentum_live_loop_control_heartbeat_v1",
                "scope": "tracker_refresh_and_callback_registration",
                "owner": "momentum_live_runner_loop",
                "owner_instance_id": owner,
                "generation": 1,
                "generation_identity": f"{owner}:1",
                "generation_started_at_utc": (
                    (now - timedelta(seconds=10))
                    .replace(tzinfo=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
            },
        )
    )
    db.commit()
    monkeypatch.setattr(
        monitor.settings,
        "chili_autotrader_user_id",
        user.id,
        raising=False,
    )
    monkeypatch.setattr(
        monitor.settings,
        "chili_alpaca_expected_account_id",
        "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f",
        raising=False,
    )

    identity, status = monitor._captured_runtime_identity(
        db,
        user_id=user.id,
        now_utc=now,
    )

    assert status == "ok"
    assert identity is not None
    assert identity["runtime_generation"] == runtime_generation
    assert identity["live_cash_authorized"] is False

    monkeypatch.setattr(
        monitor.settings,
        "chili_autotrader_user_id",
        None,
        raising=False,
    )
    identity, status = monitor._captured_runtime_identity(
        db,
        user_id=user.id,
        now_utc=now,
    )
    assert identity is None
    assert status == "captured_watch_user_scope_unconfigured"
