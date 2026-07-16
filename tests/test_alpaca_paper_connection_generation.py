from __future__ import annotations

import hashlib
import json

import pytest

from app.config import settings
from app.services.trading.venue import alpaca_spot
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter


ACCOUNT_ID = "ae7b7443-9a5f-4db2-8e58-9b872f5015cf"


def _install_exact_client(monkeypatch):
    class _Client:
        pass

    client = _Client()
    monkeypatch.setattr(settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        ACCOUNT_ID,
        raising=False,
    )
    monkeypatch.setattr(alpaca_spot, "_paper", lambda: True)
    monkeypatch.setattr(alpaca_spot, "_require_paper_posture", lambda: None)
    monkeypatch.setattr(alpaca_spot, "_trading_client", lambda: client)
    monkeypatch.setitem(alpaca_spot._clients, "trading:paper", client)
    monkeypatch.setitem(
        alpaca_spot._clients,
        "trading:observed_account_id",
        ACCOUNT_ID,
    )
    monkeypatch.setitem(
        alpaca_spot._clients,
        "trading:fingerprint",
        "a" * 64,
    )
    adapter = AlpacaSpotAdapter()
    assert adapter.bind_account_id(ACCOUNT_ID) is True
    return adapter


def test_exact_paper_connection_receipt_is_content_addressed_and_stable(
    monkeypatch,
):
    adapter = _install_exact_client(monkeypatch)

    first = adapter.get_paper_connection_generation_receipt()
    second = adapter.get_paper_connection_generation_receipt()

    assert first["broker_environment"] == "paper"
    assert first["asset_class"] == "us_equity"
    assert first["provider_account_id"] == ACCOUNT_ID
    assert first["adapter_connection_generation"].startswith(
        "alpaca-paper-rest:"
    )
    assert first["adapter_connection_generation"] == second[
        "adapter_connection_generation"
    ]
    payload = json.loads(first["receipt_canonical_json"])
    assert payload["adapter_connection_generation"] == first[
        "adapter_connection_generation"
    ]
    assert hashlib.sha256(
        first["receipt_canonical_json"].encode("utf-8")
    ).hexdigest() == first["receipt_sha256"]


def test_unbound_adapter_cannot_issue_connection_generation(monkeypatch):
    adapter = _install_exact_client(monkeypatch)
    adapter._bound_account_id = None

    with pytest.raises(RuntimeError, match="lacks a frozen PAPER UUID"):
        adapter.get_paper_connection_generation_receipt()


def test_class_monkeypatch_cannot_issue_connection_generation(monkeypatch):
    adapter = _install_exact_client(monkeypatch)
    exact = alpaca_spot._EXACT_PAPER_CONNECTION_RECEIPT_METHOD
    monkeypatch.setattr(
        AlpacaSpotAdapter,
        "_account_client",
        lambda _self: object(),
    )

    with pytest.raises(RuntimeError, match="method identity changed"):
        exact(adapter)
