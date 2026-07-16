from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import pickle
from types import SimpleNamespace
import uuid

import pytest

from app.services.trading.momentum_neural.alpaca_fill_activity import (
    AlpacaFillActivityCorruption,
    AlpacaFillActivityError,
    AlpacaPaperFillCycleBinding,
    FILL_READ_BINDING_SCHEMA_VERSION,
    append_prepared_alpaca_paper_fill_batch,
    capture_verified_alpaca_paper_order_fills,
    read_verified_alpaca_paper_fill_batch,
)
from app.services.trading.momentum_neural.alpaca_fill_read_capability import (
    AlpacaFillReadCapabilityError,
    issue_alpaca_fill_read_capability,
)
from app.services.trading.momentum_neural.alpaca_paper_identity import (
    alpaca_paper_account_identity_sha256,
)
from app.services.trading.venue import alpaca_spot
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter


UTC = timezone.utc
ACCOUNT_ID = "ae7b7443-9a5f-4db2-8e58-9b872f5015cf"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _cycle() -> AlpacaPaperFillCycleBinding:
    return AlpacaPaperFillCycleBinding(
        reservation_id=uuid.uuid4(),
        decision_packet_sha256=_sha("packet"),
        reservation_request_sha256=_sha("request"),
        account_scope="alpaca:paper",
        account_identity_sha256=alpaca_paper_account_identity_sha256(ACCOUNT_ID),
        account_snapshot_sha256=_sha("account-snapshot"),
        account_snapshot_generation="paper-account-generation:7",
        broker_connection_generation="alpaca-arm:generation-7",
        execution_family="alpaca_spot",
        position_direction="long",
        cycle_client_order_id="entry-cid",
        entry_provider_order_id="entry-oid",
        symbol="VEEE",
    )


def _read_binding(cycle: AlpacaPaperFillCycleBinding) -> dict[str, object]:
    return {
        "schema_version": FILL_READ_BINDING_SCHEMA_VERSION,
        "cycle": cycle.to_payload(),
        "provider_order_id": cycle.entry_provider_order_id,
        "expected_client_order_id": cycle.cycle_client_order_id,
        "order_role": "entry",
    }


def _activity(
    *,
    activity_id: str = "fill-1",
    order_id: str = "entry-oid",
    order_status: str = "filled",
    observed_at: datetime,
) -> dict:
    return {
        "id": activity_id,
        "account_id": ACCOUNT_ID,
        "activity_type": "fill",
        "transaction_time": observed_at.isoformat(),
        "type": "fill",
        "price": "2.5000000000",
        "qty": "10.0000000000",
        "side": "buy",
        "symbol": "VEEE",
        "leaves_qty": "0.0000000000",
        "order_id": order_id,
        "cum_qty": "10.0000000000",
        "order_status": order_status,
    }


def _install_reader(
    monkeypatch,
    *,
    cycle: AlpacaPaperFillCycleBinding,
    pages: list[list[dict]] | None = None,
    order_status: str = "filled",
    asset_class: str = "us_equity",
    order_quantity: str = "10.0000000000",
    filled_quantity: str = "10.0000000000",
    observed_at: datetime | None = None,
) -> tuple[AlpacaSpotAdapter, list[dict]]:
    observed_at = observed_at or datetime.now(UTC)
    pages = pages or [[_activity(observed_at=observed_at, order_status=order_status)]]
    calls: list[dict] = []
    order = {
        "id": cycle.entry_provider_order_id,
        "client_order_id": cycle.cycle_client_order_id,
        "account_id": ACCOUNT_ID,
        "symbol": cycle.symbol,
        "side": "buy",
        "status": order_status,
        "qty": order_quantity,
        "filled_qty": filled_quantity,
        "asset_class": asset_class,
        "created_at": observed_at.isoformat(),
    }

    class _Order:
        def model_dump(self, mode: str = "json"):
            assert mode == "json"
            return dict(order)

    class _Client:
        def __init__(self) -> None:
            self.page_index = 0

        def get_order_by_id(self, order_id: str):
            calls.append({"kind": "order", "order_id": order_id})
            return _Order()

        def _request(self, method, path, *, data, api_version):
            calls.append(
                {
                    "kind": "page",
                    "method": method,
                    "path": path,
                    "data": dict(data),
                    "api_version": api_version,
                }
            )
            result = pages[self.page_index]
            self.page_index += 1
            return [dict(item) for item in result]

    client = _Client()
    adapter = AlpacaSpotAdapter()
    adapter._bound_account_id = ACCOUNT_ID
    monkeypatch.setattr(alpaca_spot, "_trading_client", lambda: client)
    monkeypatch.setitem(alpaca_spot._clients, "trading:paper", client)
    monkeypatch.setitem(
        alpaca_spot._clients, "trading:observed_account_id", ACCOUNT_ID
    )
    monkeypatch.setitem(alpaca_spot._clients, "trading:fingerprint", "a" * 64)
    monkeypatch.setattr(alpaca_spot, "_paper", lambda: True)
    monkeypatch.setattr(alpaca_spot, "_require_paper_posture", lambda: None)
    return adapter, calls


def _read(monkeypatch, **kwargs):
    cycle = kwargs.pop("cycle", _cycle())
    adapter, calls = _install_reader(monkeypatch, cycle=cycle, **kwargs)
    batch = read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=cycle,
        provider_order_id=cycle.entry_provider_order_id,
        expected_client_order_id=cycle.cycle_client_order_id,
    )
    return cycle, adapter, calls, batch


def test_exact_reader_issues_nonserializable_capability_and_full_terminal_receipt(
    monkeypatch,
) -> None:
    cycle = _cycle()
    observed_at = datetime.now(UTC)
    first_page = [
        _activity(
            activity_id=f"other-{index}",
            order_id=f"other-order-{index}",
            observed_at=observed_at,
        )
        for index in range(100)
    ]
    second_page = [_activity(observed_at=observed_at)]
    _cycle_value, _adapter, calls, batch = _read(
        monkeypatch,
        cycle=cycle,
        pages=[first_page, second_page],
        observed_at=observed_at,
    )

    receipt = json.loads(batch.query_receipt_canonical_json)
    assert receipt["terminal_proof"]["reason"] == "pagination_complete_short_page"
    assert receipt["terminal_proof"]["pagination_complete"] is True
    assert receipt["terminal_proof"]["scope"] == (
        "pagination_only_not_fill_absence_or_economic_completeness"
    )
    assert receipt["terminal_proof"]["page_count"] == 2
    assert receipt["pages"][0]["next_page_token"] == "other-99"
    assert receipt["pages"][1]["request_page_token"] == "other-99"
    assert receipt["pages"][0]["response_count"] == 100
    assert receipt["pages"][1]["terminal"] is True
    assert receipt["pages"][0]["requested_at"]
    assert receipt["pages"][0]["received_at"]
    assert receipt["pages"][0]["available_at"]
    assert calls[2]["data"]["page_token"] == "other-99"
    with pytest.raises(TypeError, match="not serializable"):
        pickle.dumps(batch.read_capability)


def test_instance_monkeypatch_cannot_mint_or_replace_exact_reader_authority(
    monkeypatch,
) -> None:
    cycle = _cycle()
    adapter, _calls = _install_reader(monkeypatch, cycle=cycle)
    forged_calls = 0

    def _forged(_order_id: str):
        nonlocal forged_calls
        forged_calls += 1
        return {"readable": True, "pagination_complete": True}

    adapter.get_paper_fill_activity_batch = _forged
    batch = read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=cycle,
        provider_order_id=cycle.entry_provider_order_id,
        expected_client_order_id=cycle.cycle_client_order_id,
    )
    assert forged_calls == 0
    assert batch.activities[0].provider_activity_id == "fill-1"

    with pytest.raises(AlpacaFillReadCapabilityError, match="only the exact"):
        issue_alpaca_fill_read_capability({"expires_at": datetime.now(UTC).isoformat()})


def test_class_monkeypatch_of_authenticated_client_seam_cannot_mint_authority(
    monkeypatch,
) -> None:
    cycle = _cycle()
    adapter, _calls = _install_reader(monkeypatch, cycle=cycle)
    monkeypatch.setattr(
        AlpacaSpotAdapter,
        "_account_client",
        lambda _self: pytest.fail("monkeypatched client seam executed"),
    )
    raw = AlpacaSpotAdapter.get_paper_fill_activity_batch(
        adapter,
        cycle.entry_provider_order_id,
        read_binding=_read_binding(cycle),
    )
    assert raw["readable"] is False
    assert raw["pagination_complete"] is False
    assert "_capture_capability" not in raw


def test_absent_raw_activity_account_is_not_injected_and_query_authority_binds_it(
    monkeypatch,
) -> None:
    cycle = _cycle()
    activity = _activity(observed_at=datetime.now(UTC))
    activity.pop("account_id")
    adapter, _calls = _install_reader(
        monkeypatch,
        cycle=cycle,
        pages=[[activity]],
    )
    batch = read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=cycle,
        provider_order_id=cycle.entry_provider_order_id,
        expected_client_order_id=cycle.cycle_client_order_id,
    )
    retained_raw = json.loads(
        batch.activities[0].provider_payload_canonical_json
    )
    receipt = json.loads(batch.query_receipt_canonical_json)
    assert "account_id" not in retained_raw
    assert receipt["provider_account_id"] == ACCOUNT_ID
    assert batch.activities[0].provider_account_id_sha256 == (
        cycle.account_identity_sha256
    )


def test_empty_exact_inventory_proves_only_pagination_not_fill_absence(
    monkeypatch,
) -> None:
    cycle = _cycle()
    adapter, _calls = _install_reader(
        monkeypatch,
        cycle=cycle,
        pages=[[]],
        order_status="new",
        filled_quantity="0.0000000000",
    )
    batch = read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=cycle,
        provider_order_id=cycle.entry_provider_order_id,
        expected_client_order_id=cycle.cycle_client_order_id,
    )
    receipt = json.loads(batch.query_receipt_canonical_json)
    assert batch.activities == ()
    assert receipt["exact_activity_count"] == 0
    assert receipt["terminal_proof"]["pagination_complete"] is True
    assert "fill_absence" in receipt["terminal_proof"]["scope"]


def test_manual_rehash_or_query_receipt_mutation_fails_before_session_access(
    monkeypatch,
) -> None:
    _cycle_value, _adapter, _calls, batch = _read(monkeypatch)

    class _NoSessionAccess:
        def __getattribute__(self, name):
            raise AssertionError(f"Session touched before authority verification: {name}")

    changed = replace(batch, adapter_connection_generation="forged-generation")
    changed = replace(
        changed, batch_content_sha256=changed.computed_batch_content_sha256()
    )
    with pytest.raises(AlpacaFillActivityCorruption, match="capability payload changed"):
        append_prepared_alpaca_paper_fill_batch(_NoSessionAccess(), changed)

    receipt = json.loads(batch.query_receipt_canonical_json)
    receipt["terminal_proof"]["reason"] = "caller_says_complete"
    receipt_json = json.dumps(
        receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    changed = replace(
        batch,
        query_receipt_canonical_json=receipt_json,
        query_receipt_sha256=hashlib.sha256(receipt_json.encode()).hexdigest(),
    )
    changed = replace(
        changed, batch_content_sha256=changed.computed_batch_content_sha256()
    )
    with pytest.raises(AlpacaFillActivityCorruption, match="capability payload changed"):
        append_prepared_alpaca_paper_fill_batch(_NoSessionAccess(), changed)


def test_appender_rejects_contaminated_session_before_database_access(
    monkeypatch,
) -> None:
    _cycle_value, _adapter, _calls, batch = _read(monkeypatch)

    class _Session:
        new = ()
        dirty = ()
        deleted = ()

        @staticmethod
        def in_transaction() -> bool:
            return True

    dirty = _Session()
    dirty.dirty = (object(),)
    with pytest.raises(AlpacaFillActivityError, match="pristine Session.*dirty"):
        append_prepared_alpaca_paper_fill_batch(dirty, batch)

def test_same_fill_new_query_clock_and_order_status_has_stable_fill_identity(
    monkeypatch,
) -> None:
    cycle = _cycle()
    execution_at = datetime.now(UTC)
    _c1, _a1, _calls1, first = _read(
        monkeypatch,
        cycle=cycle,
        observed_at=execution_at,
        order_status="partially_filled",
    )
    # A second concrete REST client/retrieval observes the same immutable fill
    # under a later mutable order projection and a distinct query receipt.
    activity = _activity(observed_at=execution_at, order_status="filled")
    adapter, _calls2 = _install_reader(
        monkeypatch,
        cycle=cycle,
        pages=[[activity]],
        order_status="filled",
        observed_at=execution_at,
    )
    second = read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=cycle,
        provider_order_id=cycle.entry_provider_order_id,
        expected_client_order_id=cycle.cycle_client_order_id,
    )
    assert (
        first.activities[0].immutable_fill_identity_sha256
        == second.activities[0].immutable_fill_identity_sha256
    )
    assert first.activities[0].record_content_sha256 != (
        second.activities[0].record_content_sha256
    )
    assert first.query_receipt_sha256 != second.query_receipt_sha256


def test_exit_caller_cid_and_legacy_combined_wrapper_are_fail_closed(
    monkeypatch,
) -> None:
    cycle = _cycle()
    adapter, calls = _install_reader(monkeypatch, cycle=cycle)
    with pytest.raises(AlpacaFillActivityError, match="sealed durable exit-order"):
        read_verified_alpaca_paper_fill_batch(
            adapter,
            cycle=cycle,
            provider_order_id="exit-oid",
            expected_client_order_id="caller-exit-cid",
        )
    assert calls == []

    with pytest.raises(AlpacaFillActivityError, match="unsafe and disabled"):
        capture_verified_alpaca_paper_order_fills(
            SimpleNamespace(),
            adapter=adapter,
            reservation_id=cycle.reservation_id,
            provider_order_id=cycle.entry_provider_order_id,
        )
    assert calls == []


def test_exact_zero_fee_authority_rejects_non_us_equity_before_capability(
    monkeypatch,
) -> None:
    cycle = _cycle()
    adapter, _calls = _install_reader(
        monkeypatch, cycle=cycle, asset_class="crypto"
    )
    raw = AlpacaSpotAdapter.get_paper_fill_activity_batch(
        adapter,
        cycle.entry_provider_order_id,
        read_binding=_read_binding(cycle),
    )
    assert raw["readable"] is False
    assert raw["pagination_complete"] is False
    assert "_capture_capability" not in raw


def test_expired_reader_capability_is_rejected_by_verified_reader(monkeypatch) -> None:
    cycle = _cycle()
    captured_at = datetime.now(UTC) - timedelta(seconds=10)
    adapter, _calls = _install_reader(
        monkeypatch,
        cycle=cycle,
        observed_at=captured_at,
    )
    monkeypatch.setattr(alpaca_spot, "_now", lambda: captured_at)
    monkeypatch.setattr(alpaca_spot, "_FILL_READER_CAPABILITY_TTL_SECONDS", 1)
    with pytest.raises(AlpacaFillActivityCorruption, match="expired"):
        read_verified_alpaca_paper_fill_batch(
            adapter,
            cycle=cycle,
            provider_order_id=cycle.entry_provider_order_id,
            expected_client_order_id=cycle.cycle_client_order_id,
        )
