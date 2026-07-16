from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
import uuid

import pytest

from app import migrations
from app.models.trading import AlpacaPaperFillActivity
from app.services.trading.momentum_neural import alpaca_cycle_settlement
from app.services.trading.momentum_neural import alpaca_fill_activity
from app.services.trading.momentum_neural import live_runner
from app.services.trading.momentum_neural.adaptive_risk_account_lock import (
    CanonicalAccountRiskRowLockGuard,
)
from app.services.trading.momentum_neural.alpaca_fill_activity import (
    AUTHORITATIVE_CAPTURE_SCHEMA_VERSION,
    AlpacaFillActivityConflict,
    AlpacaFillActivityError,
    AlpacaPaperFillCycleBinding,
    capture_verified_alpaca_paper_order_fills,
    prepare_verified_alpaca_paper_fill_activity,
    verify_alpaca_paper_fill_activity_row,
)
from app.services.trading.venue import alpaca_spot
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter
from app.services.trading.momentum_neural.alpaca_paper_identity import (
    alpaca_paper_account_identity_sha256,
)


UTC = timezone.utc
ACCOUNT_ID = "ae7b7443-9a5f-4db2-8e58-9b872f5015cf"


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0

    def execute(self, statement):
        self.statements.append(str(statement))
        return None

    def commit(self) -> None:
        self.commits += 1


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _cycle() -> AlpacaPaperFillCycleBinding:
    return AlpacaPaperFillCycleBinding(
        reservation_id=uuid.UUID("03e91489-95dd-48f8-b0a4-e930777222a3"),
        decision_packet_sha256=_sha("packet"),
        reservation_request_sha256=_sha("request"),
        account_scope="alpaca:paper",
        account_identity_sha256=alpaca_paper_account_identity_sha256(ACCOUNT_ID),
        account_snapshot_sha256=_sha("snapshot"),
        account_snapshot_generation="paper-snapshot-generation",
        broker_connection_generation="paper-connection-generation",
        execution_family="alpaca_spot",
        position_direction="long",
        cycle_client_order_id="entry-cid",
        entry_provider_order_id="entry-oid",
        symbol="ACTU",
    )


def _activity(*, side: str, order_id: str, activity_id: str) -> dict:
    return {
        "id": activity_id,
        "account_id": ACCOUNT_ID,
        "activity_type": "FILL",
        "transaction_time": "2026-07-15T18:00:00Z",
        "type": "fill",
        "price": "10.2500000000",
        "qty": "4.0000000000",
        "side": side,
        "symbol": "ACTU",
        "leaves_qty": "0.0000000000",
        "order_id": order_id,
        "cum_qty": "4.0000000000",
        "order_status": "filled",
    }


def _fee(activity_id: str, order_id: str) -> dict:
    return {
        "schema_version": "chili.alpaca-paper-equity-fee-contract.v1",
        "provider_activity_id": activity_id,
        "provider_order_id": order_id,
        "fee_usd": "0.0000000000",
        "currency": "USD",
        "broker_environment": "paper",
        "asset_class": "us_equity",
        "basis": "alpaca_paper_does_not_account_for_regulatory_fees",
        "source": "https://docs.alpaca.markets/us/docs/paper-trading",
    }


def test_verified_fill_uses_exact_execution_clock_and_durable_exit_owner() -> None:
    cycle = _cycle()
    activity = _activity(
        side="sell", order_id="exit-oid", activity_id="exit-fill-1"
    )
    order = {
        "id": "exit-oid",
        "client_order_id": "exit-cid",
        "account_id": ACCOUNT_ID,
        "symbol": "ACTU",
        "side": "sell",
        "status": "filled",
        "asset_class": "us_equity",
    }
    prepared = prepare_verified_alpaca_paper_fill_activity(
        cycle,
        provider_activity=activity,
        provider_order=order,
        received_at=datetime(2026, 7, 15, 18, 0, 1, tzinfo=UTC),
        available_at=datetime(2026, 7, 15, 18, 0, 2, tzinfo=UTC),
        expected_exit_client_order_id="exit-cid",
        fee_usd="0.0000000000",
        fee_evidence=_fee("exit-fill-1", "exit-oid"),
    )
    row = AlpacaPaperFillActivity(
        **prepared.model_kwargs(sequence=1, previous_event_sha256=None)
    )

    assert prepared.capture_schema_version == AUTHORITATIVE_CAPTURE_SCHEMA_VERSION
    assert prepared.provider_event_at == prepared.provider_transaction_at
    assert prepared.order_ownership_status == "authoritative"
    assert prepared.fee_status == "authoritative"
    verify_alpaca_paper_fill_activity_row(row)

    with pytest.raises(AlpacaFillActivityConflict, match="durable exit owner"):
        prepare_verified_alpaca_paper_fill_activity(
            cycle,
            provider_activity=activity,
            provider_order=order,
            received_at=datetime(2026, 7, 15, 18, 0, 1, tzinfo=UTC),
            available_at=datetime(2026, 7, 15, 18, 0, 2, tzinfo=UTC),
            expected_exit_client_order_id="different-cid",
            fee_usd="0.0000000000",
            fee_evidence=_fee("exit-fill-1", "exit-oid"),
        )


def test_migration_336_preserves_v1_and_requires_strict_v2(monkeypatch) -> None:
    required = {
        "adaptive_risk_reservations",
        "adaptive_risk_decision_packets",
        "alpaca_paper_fill_activities",
        "alpaca_paper_account_settlement_heads",
        "alpaca_paper_cycle_settlements",
    }
    monkeypatch.setattr(migrations, "_tables", lambda _conn: required)
    monkeypatch.setattr(
        migrations,
        "_reassert_adaptive_late_fill_quarantine_if_present",
        lambda _conn: None,
    )
    conn = _RecordingConnection()
    migrations._migration_336_alpaca_paper_fill_activity_authority(conn)
    sql = "\n".join(conn.statements).lower()

    assert "chili.alpaca-paper-fill-activity.v1" in sql
    assert "chili.alpaca-paper-fill-activity.v2" in sql
    assert "capture_authority_status = 'verified'" in sql
    assert "provider_event_clock_field = 'transaction_time'" in sql
    assert "provider_client_order_id_status = 'authoritative'" in sql
    assert "fee_status = 'authoritative'" in sql
    assert "new fill cannot append after alpaca cycle settled" in sql
    assert conn.commits == 1


def test_adapter_reads_complete_exact_paper_activity_batch(monkeypatch) -> None:
    calls: list[tuple[str, str, dict, str]] = []
    observed_at = datetime.now(UTC)

    class _Order:
        def model_dump(self, *, mode: str):
            assert mode == "json"
            return {
                "id": "entry-oid",
                "client_order_id": "entry-cid",
                "account_id": ACCOUNT_ID,
                "symbol": "ACTU",
                "side": "buy",
                "status": "filled",
                "asset_class": "us_equity",
                "created_at": observed_at.isoformat(),
            }

    class _Client:
        def get_order_by_id(self, order_id: str):
            assert order_id == "entry-oid"
            return _Order()

        def _request(self, method: str, path: str, *, data: dict, api_version: str):
            calls.append((method, path, dict(data), api_version))
            return [
                _activity(
                    side="buy",
                    order_id="entry-oid",
                    activity_id="entry-fill-1",
                ),
                _activity(
                    side="buy",
                    order_id="another-order",
                    activity_id="other-fill",
                ),
            ]

    client = _Client()
    adapter = AlpacaSpotAdapter()
    adapter._bound_account_id = ACCOUNT_ID
    monkeypatch.setattr(alpaca_spot, "_require_paper_posture", lambda: None)
    monkeypatch.setattr(alpaca_spot, "_paper", lambda: True)
    monkeypatch.setattr(alpaca_spot, "_now", lambda: observed_at)
    monkeypatch.setattr(alpaca_spot, "_trading_client", lambda: client)
    with alpaca_spot._clients_lock:
        prior = alpaca_spot._clients.get("trading:observed_account_id")
        prior_client = alpaca_spot._clients.get("trading:paper")
        prior_fingerprint = alpaca_spot._clients.get("trading:fingerprint")
        alpaca_spot._clients["trading:observed_account_id"] = ACCOUNT_ID
        alpaca_spot._clients["trading:paper"] = client
        alpaca_spot._clients["trading:fingerprint"] = "c" * 64
    try:
        batch = adapter.get_paper_fill_activity_batch(
            "entry-oid",
            read_binding={
                "schema_version": "chili.alpaca-paper-fill-read-binding.v1",
                "cycle": _cycle().to_payload(),
                "provider_order_id": "entry-oid",
                "expected_client_order_id": "entry-cid",
                "order_role": "entry",
            },
        )
    finally:
        with alpaca_spot._clients_lock:
            if prior is None:
                alpaca_spot._clients.pop("trading:observed_account_id", None)
            else:
                alpaca_spot._clients["trading:observed_account_id"] = prior
            if prior_client is None:
                alpaca_spot._clients.pop("trading:paper", None)
            else:
                alpaca_spot._clients["trading:paper"] = prior_client
            if prior_fingerprint is None:
                alpaca_spot._clients.pop("trading:fingerprint", None)
            else:
                alpaca_spot._clients["trading:fingerprint"] = prior_fingerprint

    assert batch["readable"] is True
    assert batch["pagination_complete"] is True
    assert len(batch["activities"]) == 1
    assert batch["activities"][0]["fee_usd"] == "0.0000000000"
    assert calls == [
        (
            "GET",
            "/account/activities",
            {
                "activity_types": "FILL",
                "after": f"{observed_at.date().isoformat()}T00:00:00Z",
                "until": observed_at.isoformat(),
                "direction": "asc",
                "page_size": 100,
            },
            "v2",
        )
    ]


def test_authoritative_capture_rejects_duck_typed_adapter_before_read() -> None:
    class _Session:
        @staticmethod
        def in_transaction() -> bool:
            return True

    class _DuckAdapter:
        @staticmethod
        def get_paper_fill_activity_batch(_order_id: str):
            raise AssertionError("duck-typed adapter reached broker-read seam")

    with pytest.raises(AlpacaFillActivityError, match="unsafe and disabled"):
        capture_verified_alpaca_paper_order_fills(
            _Session(),
            adapter=_DuckAdapter(),
            reservation_id=_cycle().reservation_id,
            provider_order_id="entry-oid",
        )


def _legacy_two_fill_writers_read_broker_then_share_one_canonical_lock_walk(
    monkeypatch,
) -> None:
    """Both contenders use broker -> A1/A2 -> reservation -> fill ordering."""

    cycle = _cycle()
    packet = SimpleNamespace(decision_packet_sha256=cycle.decision_packet_sha256)
    reservation = SimpleNamespace(
        reservation_id=cycle.reservation_id,
        decision_packet_sha256=cycle.decision_packet_sha256,
    )
    traces: dict[str, list[str]] = {"writer-a": [], "writer-b": []}

    class _Session:
        def __init__(self, writer: str) -> None:
            self.writer = writer
            self.scalar_calls = 0

        @staticmethod
        def in_transaction() -> bool:
            return True

        def scalar(self, _statement):
            self.scalar_calls += 1
            if self.scalar_calls == 1:
                traces[self.writer].append("reservation_for_update")
                return reservation
            traces[self.writer].append("fill_activity_for_update")
            return None

        @staticmethod
        def get(_model, _key):
            return packet

    def _locks(session, *, account_scope: str):
        assert account_scope == "alpaca:paper"
        traces[session.writer].append("a1_then_a2")

    def _append(session, _prepared, *, reservation, packet):
        assert reservation is not None and packet is not None
        traces[session.writer].append("append_under_locked_cycle")
        return SimpleNamespace(
            created=True,
            row=SimpleNamespace(event_sha256=_sha(f"event:{session.writer}")),
        )

    monkeypatch.setattr(
        alpaca_fill_activity,
        "acquire_adaptive_risk_account_locks",
        _locks,
    )
    monkeypatch.setattr(
        alpaca_fill_activity.AlpacaPaperFillCycleBinding,
        "from_rows",
        classmethod(lambda _cls, _reservation, _packet: cycle),
    )
    monkeypatch.setattr(
        alpaca_fill_activity,
        "_append_alpaca_paper_fill_activity_under_locked_cycle",
        _append,
    )
    monkeypatch.setattr(
        alpaca_fill_activity,
        "append_alpaca_paper_fill_activity",
        lambda *_args, **_kwargs: pytest.fail(
            "batch capture re-entered the public append lock walk"
        ),
    )

    for writer in traces:
        adapter = AlpacaSpotAdapter()
        adapter._bound_account_id = ACCOUNT_ID

        def _read(_order_id: str, *, _writer: str = writer):
            traces[_writer].append("broker_read")
            return {
                "readable": True,
                "complete": True,
                "provider_order": {
                    "id": "entry-oid",
                    "client_order_id": "entry-cid",
                    "symbol": "ACTU",
                    "side": "buy",
                    "status": "filled",
                },
                "received_at": datetime(2026, 7, 15, 18, 0, 1, tzinfo=UTC),
                "available_at": datetime(2026, 7, 15, 18, 0, 2, tzinfo=UTC),
                "activities": [
                    {
                        "provider_activity": _activity(
                            side="buy",
                            order_id="entry-oid",
                            activity_id=f"fill:{_writer}",
                        ),
                        "fee_usd": "0.0000000000",
                        "fee_evidence": _fee(
                            f"fill:{_writer}", "entry-oid"
                        ),
                    }
                ],
            }

        adapter.get_paper_fill_activity_batch = _read
        result = capture_verified_alpaca_paper_order_fills(
            _Session(writer),
            adapter=adapter,
            reservation_id=cycle.reservation_id,
            provider_order_id="entry-oid",
        )
        assert result.created_count == 1

    assert traces == {
        "writer-a": [
            "broker_read",
            "a1_then_a2",
            "reservation_for_update",
            "fill_activity_for_update",
            "append_under_locked_cycle",
        ],
        "writer-b": [
            "broker_read",
            "a1_then_a2",
            "reservation_for_update",
            "fill_activity_for_update",
            "append_under_locked_cycle",
        ],
    }


def test_combined_fill_writer_is_disabled_before_session_or_broker_access() -> None:
    class _Poison:
        def __getattribute__(self, name):
            raise AssertionError(f"legacy combined wrapper touched {name}")

    with pytest.raises(AlpacaFillActivityError, match="unsafe and disabled"):
        capture_verified_alpaca_paper_order_fills(
            _Poison(),
            adapter=_Poison(),
            reservation_id=_cycle().reservation_id,
            provider_order_id="entry-oid",
        )


def test_cycle_settlement_uses_advisories_then_head_reservation_and_fills(
    monkeypatch,
) -> None:
    identity = _cycle().account_identity_sha256
    reservation_id = _cycle().reservation_id
    events: list[str] = []

    class _Preflight:
        @staticmethod
        def one_or_none():
            return ("alpaca:paper", identity)

    head = SimpleNamespace(
        settled_cycle_sequence=1,
        last_settlement_sha256=_sha("settlement"),
    )
    reservation = SimpleNamespace(
        reservation_id=reservation_id,
        decision_packet_sha256=_sha("packet"),
        account_scope="alpaca:paper",
        state="closed",
    )
    packet = SimpleNamespace(
        account_scope="alpaca:paper",
        execution_surface="alpaca_paper",
        execution_family="alpaca_spot",
        broker_environment="paper",
        account_identity_sha256=identity,
    )
    existing = SimpleNamespace(
        account_scope="alpaca:paper",
        account_identity_sha256=identity,
        terminal_sequence=1,
        settlement_sha256=head.last_settlement_sha256,
    )

    class _Session:
        def __init__(self) -> None:
            self.scalar_values = iter((head, reservation, existing))

        @staticmethod
        def in_transaction() -> bool:
            return True

        @staticmethod
        def execute(_statement):
            return _Preflight()

        def scalar(self, _statement):
            return next(self.scalar_values)

        @staticmethod
        def get(_model, _key):
            return packet

    class _RecordingGuard(CanonicalAccountRiskRowLockGuard):
        def observe(self, stage, *, sort_key):
            super().observe(stage, sort_key=sort_key)
            events.append(stage.value)

    monkeypatch.setattr(
        alpaca_cycle_settlement,
        "acquire_adaptive_risk_account_locks",
        lambda _session, *, account_scope: events.append(
            f"advisories:{account_scope}"
        ),
    )
    monkeypatch.setattr(
        alpaca_cycle_settlement,
        "CanonicalAccountRiskRowLockGuard",
        _RecordingGuard,
    )
    monkeypatch.setattr(
        alpaca_cycle_settlement, "verify_settlement_head_content", lambda _row: None
    )
    monkeypatch.setattr(
        alpaca_cycle_settlement, "verify_cycle_settlement_content", lambda _row: None
    )
    monkeypatch.setattr(
        alpaca_fill_activity,
        "append_alpaca_paper_terminal_fill_observation_receipt",
        lambda _session, *, settlement: SimpleNamespace(),
    )

    result = alpaca_cycle_settlement.settle_flat_alpaca_paper_cycle(
        _Session(), reservation_id=reservation_id
    )

    assert result.created is False
    assert events == [
        "advisories:alpaca:paper",
        "account_settlement_head",
        "adaptive_risk_reservation",
        "fill_activity_or_cycle_settlement",
    ]


def test_live_exit_capture_requires_resolved_exact_owner(monkeypatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(live_runner, "_commit_le", lambda *_args: None)
    monkeypatch.setattr(
        live_runner,
        "_validated_alpaca_close_only_marker",
        lambda _sess: (None, "not_close_only"),
    )

    def _capture(_sess, _le, **kwargs):
        captured.append(kwargs)
        return {"ok": True, "observed_count": 1, "created_count": 1}

    monkeypatch.setattr(
        live_runner, "_capture_adaptive_alpaca_order_fills", _capture
    )
    monkeypatch.setattr(
        live_runner,
        "_retain_adaptive_alpaca_exit_fill_owner",
        lambda *_args, **_kwargs: {"retained": True},
    )
    order = SimpleNamespace(order_id="exit-oid", client_order_id="exit-cid")
    le = {
        "alpaca_last_resolved_exit_owner_transport": {
            "broker_order_id": "exit-oid",
            "client_order_id": "exit-cid",
        }
    }
    result = live_runner._capture_owned_adaptive_alpaca_exit_fills(
        object(),
        le,
        adapter=object(),
        reservation_id=_cycle().reservation_id,
        exit_order=order,
    )
    assert result["ok"] is True
    assert captured[0]["expected_exit_client_order_id"] == "exit-cid"

    captured.clear()
    le["alpaca_last_resolved_exit_owner_transport"] = {
        "broker_order_id": "different-oid",
        "client_order_id": "exit-cid",
    }
    blocked = live_runner._capture_owned_adaptive_alpaca_exit_fills(
        object(),
        le,
        adapter=object(),
        reservation_id=_cycle().reservation_id,
        exit_order=order,
    )
    assert blocked["ok"] is False
    assert blocked["reason"] == "alpaca_exit_fill_owner_unproven"
    assert captured == []


def test_flat_settlement_retries_every_retained_exact_exit_owner(monkeypatch) -> None:
    reservation_id = _cycle().reservation_id
    entry = SimpleNamespace(order_id="entry-oid", client_order_id="entry-cid")
    earlier = SimpleNamespace(order_id="exit-oid-1", client_order_id="exit-cid-1")
    final = SimpleNamespace(order_id="exit-oid-2", client_order_id="exit-cid-2")

    class _Adapter:
        @staticmethod
        def get_order(order_id: str):
            orders = {
                "entry-oid": entry,
                "exit-oid-1": earlier,
                "exit-oid-2": final,
            }
            return orders.get(order_id), {}

    observed: list[tuple[str, str | None]] = []

    def _capture(_sess, _le, *, order, expected_exit_client_order_id=None, **_kw):
        observed.append((order.order_id, expected_exit_client_order_id))
        return {"ok": True, "observed_count": 1, "created_count": 0}

    monkeypatch.setattr(live_runner, "_capture_adaptive_alpaca_order_fills", _capture)
    monkeypatch.setattr(
        live_runner,
        "_capture_owned_adaptive_alpaca_exit_fills",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        live_runner,
        "_adaptive_alpaca_exit_fill_owners",
        lambda *_args, **_kwargs: [
            {
                "provider_order_id": "exit-oid-1",
                "provider_client_order_id": "exit-cid-1",
            },
            {
                "provider_order_id": "exit-oid-2",
                "provider_client_order_id": "exit-cid-2",
            },
        ],
    )
    monkeypatch.setattr(
        live_runner,
        "_settle_adaptive_alpaca_cycle_if_complete",
        lambda *_args, **_kwargs: {"ok": True},
    )

    result = live_runner._capture_and_settle_flat_adaptive_alpaca_cycle(
        object(),
        {},
        adapter=_Adapter(),
        reservation_id=reservation_id,
        entry_order_id="entry-oid",
        exit_order=final,
    )

    assert result["ok"] is True
    assert observed == [
        ("entry-oid", None),
        ("exit-oid-1", "exit-cid-1"),
        ("exit-oid-2", "exit-cid-2"),
    ]
