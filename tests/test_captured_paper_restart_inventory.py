from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from app.config import settings
from app.services.trading.momentum_neural.captured_paper_restart_inventory import (
    CapturedPaperRestartInventoryError,
    CapturedPaperRestartLineage,
    classify_captured_paper_restart_inventory,
)
from app.services.trading.venue import alpaca_spot as alpaca_mod
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter


UTC = timezone.utc
ACCOUNT_ID = "ae7b7443-9a5f-4db2-8e58-9b872f5015cf"
RUNTIME_GENERATION = "e69f0ee0-8b7c-43b7-bfac-a820f7890117"
CONNECTION_GENERATION = "alpaca-paper-rest:" + "a" * 64
PRIOR_CONNECTION_GENERATION = "alpaca-paper-rest:" + "b" * 64
ADAPTER_BUILD_SHA256 = "f" * 64
READ_BINDING = {"purpose": "captured-paper-restart", "generation": RUNTIME_GENERATION}
OBSERVED_AT = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


READ_BINDING_SHA256 = _sha(READ_BINDING)


def _census(
    kind,
    inventory,
    *,
    readable=True,
    complete=True,
    connection=None,
    adapter_build=ADAPTER_BUILD_SHA256,
    read_binding=READ_BINDING,
):
    connection = connection or CONNECTION_GENERATION
    binding_json = _canonical(read_binding)
    binding_sha256 = hashlib.sha256(binding_json.encode()).hexdigest()
    schema = {
        "orders": "chili.alpaca-paper-open-order-census.v1",
        "positions": "chili.alpaca-paper-position-census.v1",
    }[kind]
    inventory_json = _canonical(inventory)
    inventory_sha = hashlib.sha256(inventory_json.encode()).hexdigest()
    requested_at = datetime(2026, 7, 16, 11, 59, 57, tzinfo=UTC)
    received_at = datetime(2026, 7, 16, 11, 59, 58, tzinfo=UTC)
    available_at = datetime(2026, 7, 16, 11, 59, 59, tzinfo=UTC)
    query = {
        "orders": {
            "status": "open",
            "limit": 500,
            "direction": "asc",
            "nested": False,
        },
        "positions": {
            "resource": "account_positions",
            "asset_class": "us_equity",
            "pagination": "not_applicable",
        },
    }[kind]
    query_json = _canonical(query)
    terminal_reason = {
        "orders": "complete_short_page",
        "positions": "complete_non_pageable_account_positions",
    }[kind]
    exact_count_field = {
        "orders": "exact_order_count",
        "positions": "exact_position_count",
    }[kind]
    page = {
        "page_index": 0,
        "request_page_token": None,
        "request_canonical_json": query_json,
        "request_sha256": hashlib.sha256(query_json.encode()).hexdigest(),
        "requested_at": requested_at.isoformat(),
        "received_at": received_at.isoformat(),
        "available_at": available_at.isoformat(),
        "response_count": len(inventory),
        "response_canonical_json": inventory_json,
        "response_sha256": inventory_sha,
        "next_page_token": None,
        "terminal": True,
    }
    receipt = {
        "schema_version": schema,
        "broker_environment": "paper",
        "asset_class": "us_equity",
        "provider_account_id": ACCOUNT_ID,
        "adapter_connection_generation": connection,
        "adapter_build_sha256": adapter_build,
        "read_binding_sha256": binding_sha256,
        "query": query,
        "pages": [page],
        "inventory_sha256": inventory_sha,
        exact_count_field: len(inventory),
        "terminal_proof": {
            "pagination_complete": complete,
            "reason": terminal_reason,
            "page_count": 1,
            "last_response_sha256": inventory_sha,
            "last_response_count": len(inventory),
            "last_page_terminal": True,
        },
    }
    receipt_json = _canonical(receipt)
    return {
        "readable": readable,
        "pagination_complete": complete,
        "broker_environment": "paper",
        "asset_class": "us_equity",
        "provider_account_id": ACCOUNT_ID,
        "adapter_connection_generation": connection,
        "adapter_build_sha256": adapter_build,
        "read_binding_canonical_json": binding_json,
        "read_binding_sha256": binding_sha256,
        "query_receipt_canonical_json": receipt_json,
        "query_receipt_sha256": hashlib.sha256(receipt_json.encode()).hexdigest(),
        "inventory_canonical_json": inventory_json,
        "inventory_sha256": inventory_sha,
        kind: inventory,
        "requested_at": requested_at,
        "received_at": received_at,
        "available_at": available_at,
        "expires_at": datetime(2026, 7, 16, 12, 0, 59, tzinfo=UTC),
    }


def _reissue_receipt(census, mutate):
    amended = dict(census)
    receipt = json.loads(amended["query_receipt_canonical_json"])
    mutate(receipt)
    receipt_json = _canonical(receipt)
    amended["query_receipt_canonical_json"] = receipt_json
    amended["query_receipt_sha256"] = hashlib.sha256(receipt_json.encode()).hexdigest()
    return amended


def _lineage(
    *,
    status="transport_indeterminate",
    phase="submit_indeterminate",
    cumulative=0,
    opened=0,
    reservation_state="submit_indeterminate",
    broker_order_id="paper-order-1",
    fill_watch_state=None,
):
    order = {
        "client_order_id": "paper-cid-1",
        "symbol": "ACTU",
        "side": "buy",
        "type": "limit",
        "asset_class": "us_equity",
        "position_intent": "buy_to_open",
        "qty": 10,
        "limit_price": "10.00",
        "time_in_force": "day",
        "extended_hours": False,
    }
    return CapturedPaperRestartLineage(
        completion_sha256="1" * 64,
        payload_sha256="2" * 64,
        route_token_sha256="3" * 64,
        runtime_generation=RUNTIME_GENERATION,
        expected_account_id=ACCOUNT_ID,
        session_id=71,
        symbol="ACTU",
        client_order_id="paper-cid-1",
        binder_id="133a4db1-bdde-42d3-8ddd-c55451fe11a1",
        action_claim_token="arm-9dad8c50-7fcc-4eb7-9f4a-911b8666b2cd",
        reservation_id="27f23288-84be-487c-8410-75ff99d52e93",
        order_request_sha256=_sha(order),
        order_request=order,
        outbox_status=status,
        outbox_transport_started=True,
        action_claim_phase=phase,
        action_claim_client_order_id="paper-cid-1",
        action_claim_broker_order_id=broker_order_id,
        reservation_state=reservation_state,
        planned_quantity_shares=10,
        cumulative_filled_quantity_shares=cumulative,
        open_quantity_shares=opened,
        reservation_broker_order_id=broker_order_id,
        reservation_broker_connection_generation=(
            PRIOR_CONNECTION_GENERATION if broker_order_id else None
        ),
        fill_activity_inventory_sha256=_sha([]),
        verified_entry_fill_quantity_shares=cumulative,
        verified_exit_fill_quantity_shares=cumulative - opened,
        verified_entry_average_price=("10.00" if cumulative else None),
        session_state="holding" if opened else "armed",
        fill_watch_state=fill_watch_state,
        fill_watch_broker_order_id=(
            broker_order_id if fill_watch_state is not None else None
        ),
    )


def _open_order(*, filled=0, cid="paper-cid-1", oid="paper-order-1"):
    return {
        "id": oid,
        "client_order_id": cid,
        "symbol": "ACTU",
        "side": "buy",
        "type": "limit",
        "status": "partially_filled" if filled else "new",
        "asset_class": "us_equity",
        "qty": "10",
        "filled_qty": str(filled),
        "limit_price": "10.00",
        "time_in_force": "day",
        "extended_hours": False,
        "position_intent": "buy_to_open",
    }


def _position(*, qty=4, symbol="ACTU"):
    return {
        "symbol": symbol,
        "asset_class": "us_equity",
        "qty": str(qty),
        "avg_entry_price": "10.00",
    }


def _classify(*, lineages=(), orders=(), positions=(), order_census=None, position_census=None):
    return classify_captured_paper_restart_inventory(
        expected_account_id=ACCOUNT_ID,
        expected_runtime_generation=RUNTIME_GENERATION,
        expected_connection_generation=CONNECTION_GENERATION,
        expected_adapter_build_sha256=ADAPTER_BUILD_SHA256,
        expected_read_binding_sha256=READ_BINDING_SHA256,
        open_order_census=(order_census or _census("orders", list(orders))),
        position_census=(position_census or _census("positions", list(positions))),
        durable_lineages=lineages,
        observed_at=OBSERVED_AT,
    )


def test_strict_flat_first_cutover_requires_both_complete_empty_censuses():
    receipt = _classify()

    assert receipt["disposition"] == "strict_flat_first_cutover"
    assert receipt["broker_inventory_flat"] is True
    assert receipt["recovery_required"] is False
    assert receipt["new_admissions_quarantined"] is False
    assert receipt["live_cash_authorized"] is False
    body = json.loads(receipt["receipt_canonical_json"])
    assert hashlib.sha256(receipt["receipt_canonical_json"].encode()).hexdigest() == receipt[
        "receipt_sha256"
    ]
    assert body["runtime_generation"] == RUNTIME_GENERATION


def test_owned_open_order_enters_recovery_envelope_without_new_admission():
    lineage = _lineage()
    receipt = _classify(lineages=(lineage,), orders=(_open_order(),))

    assert receipt["disposition"] == "owned_restart_recovery"
    assert receipt["recovery_required"] is True
    assert receipt["new_admissions_quarantined"] is True
    assert receipt["exposure_decreasing_only"] is True
    assert receipt["owned_open_orders"][0]["client_order_id"] == "paper-cid-1"
    assert receipt["owned_open_orders"][0]["lineage_sha256"] == lineage.lineage_sha256


def test_equivalent_decimal_encoding_does_not_create_false_foreign_order():
    lineage = _lineage()
    order = _open_order()
    order["limit_price"] = "10"

    receipt = _classify(lineages=(lineage,), orders=(order,))

    assert receipt["disposition"] == "owned_restart_recovery"


def test_flat_broker_with_durable_pending_intent_is_restart_not_first_cutover():
    pending = replace(
        _lineage(broker_order_id=None),
        outbox_status="pending",
        outbox_transport_started=False,
        action_claim_phase="claimed",
    )

    receipt = _classify(lineages=(pending,))

    assert receipt["broker_inventory_flat"] is True
    assert receipt["disposition"] == "owned_restart_recovery"
    assert receipt["recovery_required"] is True
    assert receipt["new_admissions_quarantined"] is True


def test_partial_fill_and_position_must_match_one_exact_durable_lineage():
    lineage = _lineage(
        status="fill_handoff_committed",
        phase="submitted",
        cumulative=4,
        opened=4,
        reservation_state="partially_filled",
        fill_watch_state="pending",
    )
    receipt = _classify(
        lineages=(lineage,),
        orders=(_open_order(filled=4),),
        positions=(_position(qty=4),),
    )

    assert receipt["owned_open_orders"][0]["filled_quantity_shares"] == 4
    assert receipt["owned_positions"][0]["quantity_shares"] == 4
    assert receipt["owned_positions"][0]["reservation_ids"] == [
        lineage.reservation_id
    ]


def test_terminal_late_fill_remains_explicitly_quarantined():
    lineage = _lineage(
        status="fill_handoff_committed",
        phase="resolved",
        cumulative=2,
        opened=2,
        reservation_state="exposure_quarantined",
        fill_watch_state="fill_handoff_committed",
    )
    receipt = _classify(
        lineages=(lineage,),
        positions=(_position(qty=2),),
    )

    assert receipt["disposition"] == "owned_restart_recovery"
    assert receipt["owned_positions"][0]["terminal_late_fill_quarantined"] is True
    assert receipt["terminal_late_fill_quarantines"] == [
        {
            "completion_sha256": lineage.completion_sha256,
            "reservation_id": lineage.reservation_id,
            "symbol": "ACTU",
            "lineage_sha256": lineage.lineage_sha256,
        }
    ]
    assert receipt["new_admissions_quarantined"] is True


@pytest.mark.parametrize(
    "kwargs,reason",
    (
        (
            {"orders": (_open_order(cid="foreign-cid", oid="foreign-order"),)},
            "foreign_open_order",
        ),
        (
            {"positions": (_position(symbol="FOREIGN"),)},
            "foreign_position",
        ),
        (
            {
                "order_census": _census(
                    "orders", [], readable=False, complete=False
                )
            },
            "orders_census_incomplete",
        ),
        (
            {
                "position_census": _census(
                    "positions", [], readable=False, complete=False
                )
            },
            "positions_census_incomplete",
        ),
    ),
)
def test_foreign_or_unreadable_inventory_fails_closed(kwargs, reason):
    with pytest.raises(CapturedPaperRestartInventoryError, match=reason):
        _classify(lineages=(_lineage(),), **kwargs)


def test_mismatched_runtime_or_connection_generation_is_rejected():
    lineage = replace(
        _lineage(),
        runtime_generation="7b1559cc-b9ac-458f-b5c2-38827ad9b7b0",
    )
    with pytest.raises(
        CapturedPaperRestartInventoryError,
        match="durable_generation_mismatch",
    ):
        _classify(lineages=(lineage,), orders=(_open_order(),))

    with pytest.raises(
        CapturedPaperRestartInventoryError,
        match="positions_census_incomplete",
    ):
        _classify(
            position_census=_census(
                "positions", [], connection="alpaca-paper-rest:" + "c" * 64
            )
        )


@pytest.mark.parametrize(
    ("order_census", "position_census", "reason"),
    [
        (
            _census("orders", [], adapter_build="e" * 64),
            _census("positions", [], adapter_build="e" * 64),
            "restart_inventory_orders_census_incomplete",
        ),
        (
            _census("orders", [], read_binding={"purpose": "foreign"}),
            _census("positions", [], read_binding={"purpose": "foreign"}),
            "restart_inventory_orders_census_incomplete",
        ),
        (
            _reissue_receipt(
                _census("orders", []),
                lambda receipt: receipt["terminal_proof"].update(
                    {"reason": "self_attested_complete"}
                ),
            ),
            _census("positions", []),
            "restart_inventory_orders_inventory_hash_mismatch",
        ),
    ],
)
def test_censuses_are_bound_to_expected_reader_and_exact_terminal_proof(
    order_census,
    position_census,
    reason,
):
    with pytest.raises(CapturedPaperRestartInventoryError) as exc:
        _classify(order_census=order_census, position_census=position_census)

    assert exc.value.reason == reason


class _Position:
    def __init__(self, symbol, qty, avg, asset_id):
        self._payload = {
            "symbol": symbol,
            "qty": qty,
            "avg_entry_price": avg,
            "asset_id": asset_id,
            "asset_class": "us_equity",
            "account_id": ACCOUNT_ID,
        }

    def model_dump(self, *, mode):
        assert mode == "json"
        return dict(self._payload)


def test_adapter_position_census_is_complete_sorted_and_content_addressed(monkeypatch):
    monkeypatch.setattr(settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        ACCOUNT_ID,
        raising=False,
    )

    class _Client:
        def get_all_positions(self):
            return [
                _Position("ZZZ", "2", "4.50", "asset-z"),
                _Position("ACTU", "4", "10.00", "asset-a"),
            ]

    client = _Client()
    alpaca_mod.reset_clients_for_tests()
    monkeypatch.setattr(alpaca_mod, "_trading_client", lambda: client)
    alpaca_mod._clients["trading:paper"] = client
    alpaca_mod._clients["trading:observed_account_id"] = ACCOUNT_ID
    alpaca_mod._clients["trading:fingerprint"] = "d" * 64

    adapter = AlpacaSpotAdapter()
    assert adapter.bind_account_id(ACCOUNT_ID) is True
    census = adapter.get_paper_position_census(
        read_binding={"purpose": "restart-test", "account_id": ACCOUNT_ID}
    )

    assert census["readable"] is True
    assert census["pagination_complete"] is True
    assert [row["symbol"] for row in census["positions"]] == ["ACTU", "ZZZ"]
    assert hashlib.sha256(census["inventory_canonical_json"].encode()).hexdigest() == census[
        "inventory_sha256"
    ]
    assert hashlib.sha256(census["query_receipt_canonical_json"].encode()).hexdigest() == census[
        "query_receipt_sha256"
    ]
    assert json.loads(census["query_receipt_canonical_json"])["terminal_proof"][
        "reason"
    ] == "complete_non_pageable_account_positions"
    alpaca_mod.reset_clients_for_tests()


def test_adapter_position_census_never_coerces_read_failure_to_flat(monkeypatch):
    monkeypatch.setattr(settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        ACCOUNT_ID,
        raising=False,
    )

    class _Client:
        def get_all_positions(self):
            raise TimeoutError("unreadable")

    client = _Client()
    alpaca_mod.reset_clients_for_tests()
    monkeypatch.setattr(alpaca_mod, "_trading_client", lambda: client)
    alpaca_mod._clients["trading:paper"] = client
    alpaca_mod._clients["trading:observed_account_id"] = ACCOUNT_ID
    alpaca_mod._clients["trading:fingerprint"] = "e" * 64
    adapter = AlpacaSpotAdapter()
    assert adapter.bind_account_id(ACCOUNT_ID) is True

    census = adapter.get_paper_position_census(read_binding={"purpose": "restart"})

    assert census["readable"] is False
    assert census["pagination_complete"] is False
    assert "positions" not in census
    alpaca_mod.reset_clients_for_tests()


def test_restart_loader_is_read_only_and_uses_verified_outbox_loader():
    source = Path(
        "app/services/trading/momentum_neural/captured_paper_restart_inventory.py"
    ).read_text(encoding="utf-8")
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in source
    assert "load_captured_paper_outbox(" in source
    forbidden = ("INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE ")
    loader = source.split("def load_captured_paper_restart_lineages", 1)[1]
    loader = loader.split("def build_captured_paper_restart_inventory_receipt", 1)[0]
    assert not any(token in loader for token in forbidden)
