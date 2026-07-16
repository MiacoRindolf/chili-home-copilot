from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.db import engine
from app.models.trading import (
    AlpacaPaperBuyingPowerReflectionItem,
    AlpacaPaperBuyingPowerReflectionReceipt,
)
from app.services.trading.momentum_neural import captured_paper_admission
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    AdaptiveRiskContractError,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AdaptiveRiskReservationStore,
)
from app.services.trading.momentum_neural.alpaca_buying_power_reflection import (
    AlpacaBuyingPowerReflectionError,
    PreparedAlpacaPaperOpenOrderCensus,
    prepare_alpaca_paper_buying_power_double_census,
    read_verified_alpaca_paper_open_order_census,
)
from app.services.trading.venue import alpaca_spot as alpaca_mod
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter
from tests.test_captured_paper_admission import (
    PHASE_ONE_MATERIAL_SHA256,
    _inputs,
    _pre_reservation_authority,
    _record_phase_one,
    _seed_session,
)


UTC = timezone.utc


class _SdkOrder:
    def __init__(self, payload: dict) -> None:
        self._payload = dict(payload)

    def model_dump(self, *, mode: str) -> dict:
        assert mode == "json"
        return dict(self._payload)


class _SdkClient:
    def __init__(self, orders: list[dict]) -> None:
        self._orders = [_SdkOrder(order) for order in orders]

    def get_orders(self, *, filter) -> list[_SdkOrder]:
        assert filter is not None
        return list(self._orders)


def _read_census(
    monkeypatch,
    authority,
    *,
    phase: str,
    orders: list[dict],
    adapter: AlpacaSpotAdapter | None = None,
    client: _SdkClient | None = None,
) -> PreparedAlpacaPaperOpenOrderCensus:
    if phase == "before_account":
        times = iter(
            (
                authority.observed_at - timedelta(milliseconds=3),
                authority.observed_at - timedelta(milliseconds=2),
                authority.observed_at - timedelta(milliseconds=1),
            )
        )
    else:
        times = iter(
            (
                authority.available_at + timedelta(milliseconds=1),
                authority.available_at + timedelta(milliseconds=2),
                authority.available_at + timedelta(milliseconds=3),
            )
        )
    exact = AlpacaSpotAdapter() if adapter is None else adapter
    exact._bound_account_id = authority.account_id
    exact_client = _SdkClient(orders) if client is None else client
    fingerprint = "a" * 64
    monkeypatch.setattr(alpaca_mod, "_paper", lambda: True)
    monkeypatch.setattr(
        alpaca_mod,
        "_expected_account_id",
        lambda: authority.account_id,
    )
    monkeypatch.setattr(
        alpaca_mod,
        "_raw_trading_client",
        lambda: exact_client,
    )
    monkeypatch.setattr(alpaca_mod, "_now", lambda: next(times))
    monkeypatch.setitem(alpaca_mod._clients, "trading:paper", exact_client)
    monkeypatch.setitem(
        alpaca_mod._clients,
        "trading:observed_account_id",
        authority.account_id,
    )
    monkeypatch.setitem(
        alpaca_mod._clients,
        "trading:fingerprint",
        fingerprint,
    )
    return read_verified_alpaca_paper_open_order_census(
        exact,
        decision_id=authority.decision_id,
        phase=phase,
    )


def _double_census(monkeypatch, inputs, *, orders: list[dict]):
    authority = inputs.broker_account_facts.capture_authority
    adapter = AlpacaSpotAdapter()
    client = _SdkClient(orders)
    before = _read_census(
        monkeypatch,
        authority,
        phase="before_account",
        orders=orders,
        adapter=adapter,
        client=client,
    )
    after = _read_census(
        monkeypatch,
        authority,
        phase="after_account",
        orders=orders,
        adapter=adapter,
        client=client,
    )
    return prepare_alpaca_paper_buying_power_double_census(
        account_authority=authority,
        before=before,
        after=after,
        verified_at=datetime.now(UTC),
    )


def _seed_pending_admission(db, *, bp_per_share: float = 2.50):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(
        now=now,
        candidate_buying_power_impact_per_share_usd=bp_per_share,
    )
    _seed_session(db, inputs.post_commit_request)
    _record_phase_one(db, inputs)
    committed = captured_paper_admission.commit_captured_paper_admission(
        engine,
        inputs=inputs,
        phase_one_material_sha256=PHASE_ONE_MATERIAL_SHA256,
        executed_read_inventory=inputs.executed_read_inventory,
        **_pre_reservation_authority(inputs),
    )
    return inputs, committed


def test_double_census_rejects_adapter_generation_drift(db, monkeypatch) -> None:
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now)
    authority = inputs.broker_account_facts.capture_authority
    before = _read_census(
        monkeypatch,
        authority,
        phase="before_account",
        orders=[],
    )
    after = _read_census(
        monkeypatch,
        authority,
        phase="after_account",
        orders=[],
    )

    with pytest.raises(
        AlpacaBuyingPowerReflectionError,
        match="unstable or non-causal",
    ):
        prepare_alpaca_paper_buying_power_double_census(
            account_authority=authority,
            before=before,
            after=after,
            verified_at=datetime.now(UTC),
        )


def test_locked_pending_pretransport_receipt_keeps_bp_basis_distinct(
    db,
    monkeypatch,
) -> None:
    inputs, committed = _seed_pending_admission(db, bp_per_share=2.50)
    batch = _double_census(monkeypatch, inputs, orders=[])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        bundle = AdaptiveRiskReservationStore(engine).lock_alpaca_paper_admission_bundle(
            broker_account_facts=inputs.broker_account_facts,
            symbol=inputs.post_commit_request.intent.route_token.symbol,
            correlation_cluster=inputs.correlation_cluster,
            session=session,
            buying_power_double_census=batch,
        )
        assert bundle.buying_power_reflection_receipt_sha256 is not None
        assert (
            bundle.account_snapshot.pending_policy_buying_power_reflected_usd
            == 0.0
        )
        receipt = session.get(
            AlpacaPaperBuyingPowerReflectionReceipt,
            bundle.buying_power_reflection_receipt_sha256,
        )
        assert receipt is not None
        assert receipt.item_count == 1
        assert receipt.reflected_pending_buying_power_usd == 0
        item = session.scalar(
            select(AlpacaPaperBuyingPowerReflectionItem).where(
                AlpacaPaperBuyingPowerReflectionItem.receipt_sha256
                == receipt.receipt_sha256
            )
        )
        assert item is not None
        assert str(item.reservation_id) == committed.reservation_id
        assert item.classification == "unreflected_pre_transport"
        assert item.provider_order_id is None
        assert float(item.local_planned_per_share_buying_power_usd) == 2.50
        assert float(item.local_entry_limit_price) == 3.00


def test_partially_populated_provider_row_cannot_pass_as_pretransport(
    db,
    monkeypatch,
) -> None:
    inputs, _committed = _seed_pending_admission(db)
    cid = inputs.post_commit_request.intent.client_order_id
    batch = _double_census(
        monkeypatch,
        inputs,
        orders=[
            {
                "id": "provider-order-without-economics",
                "client_order_id": cid,
                "asset_class": "us_equity",
            }
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        with pytest.raises(
            AdaptiveRiskContractError,
            match="pre-transport claim is not unreflected",
        ):
            AdaptiveRiskReservationStore(engine).lock_alpaca_paper_admission_bundle(
                broker_account_facts=inputs.broker_account_facts,
                symbol=inputs.post_commit_request.intent.route_token.symbol,
                correlation_cluster=inputs.correlation_cluster,
                session=session,
                buying_power_double_census=batch,
            )
    assert db.execute(
        text("SELECT count(*) FROM alpaca_paper_bp_reflection_receipts")
    ).scalar_one() == 0
