"""Alpaca equities VenueAdapter (docs/DESIGN/ALPACA_LANE.md) — pure normalization + the
execution-family wiring. The live paper validation (P1) happens once API keys are set;
these tests need neither alpaca-py installed nor keys (lazy SDK imports)."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.db as db_mod
from app.config import settings
from app.services.trading.venue import alpaca_spot as alpaca_mod
from app.services.trading.execution_family_registry import (
    EXECUTION_FAMILY_ALPACA_SPOT,
    DOCUMENTED_EXECUTION_FAMILIES,
    IMPLEMENTED_MOMENTUM_AUTOMATION_FAMILIES,
    momentum_runner_supports_execution_family,
    normalize_execution_family,
    resolve_live_spot_adapter_factory,
    venue_for_execution_family,
)
from app.services.trading.venue.alpaca_spot import (
    AlpacaSpotAdapter,
    _f,
    _norm_status,
    _submit_failure_metadata,
    _to_symbol,
    quantize_alpaca_equity_limit_price,
    quantize_alpaca_equity_sell_stop_price,
)
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedTicker


# ── pure: status normalization (the fiddly bit — must align with _order_done_for_entry /
#    _order_open from #550/#551) ────────────────────────────────────────────────────────
def test_norm_status_terminal_and_working():
    # terminal -> canonical terminal words
    assert _norm_status("filled") == "filled"
    assert _norm_status("canceled") == "canceled"
    assert _norm_status("cancelled") == "canceled"
    assert _norm_status("expired") == "expired"
    assert _norm_status("rejected") == "rejected"
    assert _norm_status("done_for_day") == "pending"
    assert _norm_status("replaced") == "pending"
    # Routed/working states stay open so the fill poll continues.
    assert _norm_status("new") == "open"
    assert _norm_status("accepted") == "open"
    assert _norm_status("pending_new") == "open"
    assert _norm_status("partially_filled") == "open"
    # Rare non-executable or completed-for-day states remain unresolved.  Their
    # raw Alpaca lifecycle is required before recovery can decide accounting or
    # reprotection; none may be mistaken for a working protective order.
    assert _norm_status("held") == "pending"
    assert _norm_status("calculated") == "pending"
    assert _norm_status("suspended") == "pending"
    assert _norm_status("pending_cancel") == "pending"


def test_norm_status_handles_enum_like_and_unknown():
    class _E:
        value = "FILLED"
    assert _norm_status(_E()) == "filled"
    assert _norm_status(None) == "unknown"
    assert _norm_status("some_new_alpaca_state") == "some_new_alpaca_state"


def test_to_symbol_and_float_coercion():
    assert _to_symbol("  aapl ") == "AAPL"
    assert _to_symbol("CLSK") == "CLSK"
    assert _f("2.21") == 2.21
    assert _f(None) is None
    assert _f("not-a-number") is None
    assert _f(float("nan")) is None


def test_equity_limit_quantizer_preserves_exact_sub_dollar_ticks_and_boundary():
    assert quantize_alpaca_equity_limit_price("0.12371", "buy") == "0.1238"
    assert quantize_alpaca_equity_limit_price("0.12371", "sell") == "0.1237"
    assert quantize_alpaca_equity_limit_price("0.5000", "buy") == "0.5000"
    assert quantize_alpaca_equity_limit_price("0.99999", "buy") == "1.00"
    assert quantize_alpaca_equity_limit_price("1.0001", "buy") == "1.01"
    with pytest.raises(ValueError):
        quantize_alpaca_equity_limit_price("0.1237", "unknown")


def test_adapter_rejects_noncanonical_entry_before_sdk_or_transport(monkeypatch):
    calls = {"client": 0}

    def _forbidden_client():
        calls["client"] += 1
        raise AssertionError("noncanonical entry reached Alpaca transport")

    adapter = AlpacaSpotAdapter()
    monkeypatch.setattr(adapter, "_account_client", _forbidden_client)
    result = adapter.place_limit_order_gtc(
        product_id="PENNY",
        side="buy",
        position_intent="buy_to_open",
        base_size="100000",
        limit_price="0.12371",
        client_order_id="cid-noncanonical-penny",
        time_in_force="day",
        extended_hours=False,
    )

    assert result["ok"] is False
    assert result["error"] == "alpaca_entry_limit_not_canonical"
    assert result["canonical_limit_price"] == "0.1238"
    assert result["submit_outcome"] == "pre_transport_blocked"
    assert calls["client"] == 0


def test_submit_failure_metadata_distinguishes_timeout_from_broker_reject():
    timeout = _submit_failure_metadata(TimeoutError("response timed out"))
    assert timeout["submit_outcome"] == "indeterminate"
    assert timeout["error_type"] == "TimeoutError"
    assert timeout["http_status"] is None

    class _ExplicitReject(Exception):
        status_code = 422

    reject = _submit_failure_metadata(_ExplicitReject("invalid order"))
    assert reject["submit_outcome"] == "broker_rejected"
    assert reject["http_status"] == 422


def test_submit_boundary_rejects_non_long_equity_or_gtc_entry_before_client(monkeypatch):
    calls = {"client": 0}

    def _forbidden_client():
        calls["client"] += 1
        raise AssertionError("invalid instruction reached the Alpaca client")

    monkeypatch.setattr(alpaca_mod, "_trading_client", _forbidden_client)
    adapter = AlpacaSpotAdapter()
    invalid = [
        {"side": "typo", "position_intent": "buy_to_open", "time_in_force": "day"},
        {"side": "sell", "position_intent": None, "time_in_force": "day"},
        {"side": "sell", "position_intent": "sell_to_open", "time_in_force": "day"},
        {"side": "buy", "position_intent": "buy_to_close", "time_in_force": "day"},
        {"side": "buy", "position_intent": "sell_to_close", "time_in_force": "day"},
        {"side": "buy", "position_intent": "buy_to_open", "time_in_force": "gtc"},
    ]
    for index, instruction in enumerate(invalid):
        result = adapter.place_limit_order_gtc(
            product_id="ACTU",
            base_size="1",
            limit_price="10.00",
            client_order_id=f"cid-invalid-{index}",
            **instruction,
        )
        assert result["ok"] is False
        assert result["pre_submit_blocked"] is True
    for index, crypto_instruction in enumerate(
        (
            {"product_id": "BTC-USD"},
            {"product_id": "BTC/USD"},
            {"product_id": "ACTU", "asset_class": "crypto"},
        )
    ):
        crypto = adapter.place_limit_order_gtc(
            side="buy",
            position_intent="buy_to_open",
            base_size="1",
            limit_price="10.00",
            client_order_id=f"cid-crypto-{index}",
            time_in_force="day",
            **crypto_instruction,
        )
        assert crypto["ok"] is False
        assert crypto["pre_submit_blocked"] is True
    assert calls["client"] == 0


def test_submit_boundary_rejects_extended_hours_entry_before_client(monkeypatch):
    calls = {"client": 0}

    def _forbidden_client():
        calls["client"] += 1
        raise AssertionError("extended-hours entry reached the Alpaca client")

    monkeypatch.setattr(alpaca_mod, "_trading_client", _forbidden_client)
    result = AlpacaSpotAdapter().place_limit_order_gtc(
        product_id="ACTU",
        side="buy",
        position_intent="buy_to_open",
        base_size="1",
        limit_price="10.00",
        client_order_id="cid-stale-premarket-generation",
        time_in_force="day",
        extended_hours=True,
    )

    assert result["ok"] is False
    assert result["error"] == "alpaca_extended_hours_entry_not_certified"
    assert result["pre_submit_blocked"] is True
    assert result["submit_outcome"] == "pre_transport_blocked"
    assert calls["client"] == 0


def test_deadman_boundary_rejects_crypto_or_malformed_request_before_client(monkeypatch):
    calls = {"client": 0}

    def _forbidden_client():
        calls["client"] += 1
        raise AssertionError("invalid deadman request reached the Alpaca client")

    monkeypatch.setattr(alpaca_mod, "_trading_client", _forbidden_client)
    adapter = AlpacaSpotAdapter()
    for kwargs in (
        {
            "product_id": "BTC-USD",
            "base_size": "1",
            "stop_price": 9.0,
            "client_order_id": "cid-crypto-deadman",
        },
        {
            "product_id": "BTC/USD",
            "base_size": "1",
            "stop_price": 9.0,
            "client_order_id": "cid-slash-crypto-deadman",
        },
        {
            "product_id": "ACTU",
            "base_size": "1",
            "stop_price": 9.0,
            "client_order_id": "cid-explicit-crypto-deadman",
            "asset_class": "crypto",
        },
        {
            "product_id": "ACTU",
            "base_size": "0",
            "stop_price": 9.0,
            "client_order_id": "cid-zero-deadman",
        },
        {
            "product_id": "ACTU",
            "base_size": "1",
            "stop_price": 0.0,
            "client_order_id": "cid-zero-stop",
        },
        {
            "product_id": "ACTU",
            "base_size": "1",
            "stop_price": 9.0,
            "client_order_id": None,
        },
    ):
        result = adapter.place_deadman_stop(**kwargs)
        assert result["ok"] is False
        assert result["pre_submit_blocked"] is True
    assert calls["client"] == 0


def test_is_enabled_false_without_config(monkeypatch):
    # default: chili_alpaca_enabled is False -> disabled regardless of keys.
    monkeypatch.setattr(settings, "chili_alpaca_enabled", False, raising=False)
    assert AlpacaSpotAdapter().is_enabled() is False
    # enabled but no keys -> still disabled (keys are a real activation dependency).
    monkeypatch.setattr(settings, "chili_alpaca_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_api_key", "", raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_api_secret", "", raising=False)
    assert AlpacaSpotAdapter().is_enabled() is False


def test_live_posture_has_no_keys_and_cannot_construct_any_client(monkeypatch):
    monkeypatch.setattr(settings, "chili_alpaca_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_paper", False, raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_api_key", "paper-key", raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_api_secret", "paper-secret", raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_live_api_key", "live-key", raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_live_api_secret", "live-secret", raising=False)
    alpaca_mod.reset_clients_for_tests()

    cached_paper_client = object()
    alpaca_mod._clients["trading:paper"] = cached_paper_client

    assert AlpacaSpotAdapter().is_enabled() is False
    assert alpaca_mod._keys() == ("", "")
    for factory in (
        alpaca_mod._trading_client,
        alpaca_mod._data_client,
        alpaca_mod._crypto_data_client,
    ):
        try:
            factory()
        except RuntimeError as exc:
            assert "paper-only" in str(exc)
        else:  # pragma: no cover - explicit safety assertion
            raise AssertionError("live posture constructed an Alpaca client")
    assert alpaca_mod._clients == {"trading:paper": cached_paper_client}
    alpaca_mod.reset_clients_for_tests()


def test_posture_flip_blocks_all_cached_public_broker_surfaces(monkeypatch):
    class _ForbiddenCachedClient:
        calls = 0

        def __getattr__(self, _name):
            self.calls += 1
            raise AssertionError("cached paper client was accessed after posture flip")

    forbidden = _ForbiddenCachedClient()
    alpaca_mod.reset_clients_for_tests()
    alpaca_mod._clients.update(
        {
            "trading:paper": forbidden,
            "data:paper": forbidden,
            "crypto_data:paper": forbidden,
        }
    )
    monkeypatch.setattr(settings, "chili_alpaca_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_paper", False, raising=False)
    adapter = AlpacaSpotAdapter()
    monkeypatch.setattr(adapter, "_iqfeed_l1_quote", lambda *_a, **_k: None)

    assert adapter.get_order("oid")[0] is None
    assert adapter.get_order_truth("oid")["readable"] is False
    assert adapter.get_order_by_client_order_id("cid")[0] is None
    assert adapter.get_order_by_client_order_id_truth("cid")["readable"] is False
    assert adapter.list_open_orders(strict=True)[0] is None
    assert adapter.get_fills(order_id="oid")[0] == []
    assert adapter.list_positions()[0] is None
    assert adapter.get_position_quantity("ACTU") is None
    assert adapter.get_product("ACTU")[0] is None
    assert adapter.get_products()[0] == []
    assert adapter.get_best_bid_ask("ACTU")[0] is None
    assert adapter.get_account_snapshot()["ok"] is False
    assert adapter.place_market_order(
        product_id="ACTU", side="sell", base_size="1", position_intent="sell_to_close"
    )["ok"] is False
    assert adapter.place_limit_order_gtc(
        product_id="ACTU",
        side="sell",
        base_size="1",
        limit_price="1.00",
        position_intent="sell_to_close",
    )["ok"] is False
    assert adapter.place_deadman_stop(
        product_id="ACTU", base_size="1", stop_price=0.90
    )["ok"] is False
    assert adapter.cancel_order("oid")["ok"] is False
    assert adapter.cancel_order_by_id("oid") is False
    assert forbidden.calls == 0
    alpaca_mod.reset_clients_for_tests()


def test_production_alpaca_sdk_and_hosts_are_confined_to_paper_only_adapter():
    app_root = Path(__file__).resolve().parents[1] / "app"
    adapter_path = (
        app_root / "services" / "trading" / "venue" / "alpaca_spot.py"
    ).resolve()
    violations: list[str] = []
    for path in app_root.rglob("*.py"):
        if path.resolve() == adapter_path:
            continue
        source = path.read_text(encoding="utf-8")
        if "api.alpaca.markets" in source or "paper-api.alpaca.markets" in source:
            violations.append(f"{path}:direct_host")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and (
                    str(node.module or "") == "alpaca"
                    or str(node.module or "").startswith("alpaca.")
                )
            ):
                violations.append(f"{path}:{node.lineno}:alpaca_import")
            elif isinstance(node, ast.Import):
                if any(
                    alias.name == "alpaca" or alias.name.startswith("alpaca.")
                    for alias in node.names
                ):
                    violations.append(f"{path}:{node.lineno}:alpaca_import")
    assert violations == []


def test_scheduled_socket_guard_checks_paper_posture_before_direct_read():
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "docker-socket-guard.ps1"
    ).read_text(encoding="utf-8")
    posture_check = script.index("CHILI_ALPACA_PAPER")
    direct_read = script.index("Invoke-RestMethod")
    assert posture_check < direct_read
    assert "paper posture not explicitly certified" in script


# ── execution-family wiring ──────────────────────────────────────────────────
def test_alpaca_family_registered_and_implemented():
    assert EXECUTION_FAMILY_ALPACA_SPOT == "alpaca_spot"
    assert EXECUTION_FAMILY_ALPACA_SPOT in DOCUMENTED_EXECUTION_FAMILIES
    assert EXECUTION_FAMILY_ALPACA_SPOT in IMPLEMENTED_MOMENTUM_AUTOMATION_FAMILIES
    assert momentum_runner_supports_execution_family("alpaca_spot") is True
    assert normalize_execution_family("ALPACA_SPOT") == "alpaca_spot"


def test_resolve_factory_returns_alpaca_adapter():
    factory = resolve_live_spot_adapter_factory("alpaca_spot")
    assert factory is AlpacaSpotAdapter
    # the factory produces an adapter exposing the Protocol surface the runner uses
    ad = factory()
    for m in ("get_best_bid_ask", "place_market_order", "place_limit_order_gtc",
              "get_order", "get_order_by_client_order_id", "cancel_order",
              "get_account_snapshot", "is_enabled"):
        assert hasattr(ad, m)


def test_venue_for_alpaca_family():
    assert venue_for_execution_family("alpaca_spot") == "alpaca"


def test_get_order_by_client_order_id_normalizes_broker_order(monkeypatch):
    class _Order:
        id = "broker-order-123"
        client_order_id = "chili_ml_e_123"
        symbol = "ACTU"
        side = "buy"
        status = "filled"
        order_type = "limit"
        filled_qty = "17991"
        filled_avg_price = "1.48"
        stop_price = "1.23"
        created_at = "2026-07-13T16:03:55Z"
        submitted_at = "2026-07-13T16:03:55.918210Z"
        filled_at = "2026-07-13T16:03:57.073291Z"

    class _Client:
        requested_client_id = None

        def get_order_by_client_id(self, client_id):
            self.requested_client_id = client_id
            return _Order()

    client = _Client()
    monkeypatch.setattr(alpaca_mod, "_trading_client", lambda: client)

    order, _ = AlpacaSpotAdapter().get_order_by_client_order_id("chili_ml_e_123")

    assert client.requested_client_id == "chili_ml_e_123"
    assert order is not None
    assert order.order_id == "broker-order-123"
    assert order.client_order_id == "chili_ml_e_123"
    assert order.filled_size == 17991.0
    assert order.average_filled_price == 1.48
    assert order.raw["stop_price"] == 1.23
    assert order.raw["submitted_at"] == "2026-07-13T16:03:55.918210Z"
    assert order.raw["filled_at"] == "2026-07-13T16:03:57.073291Z"


def test_strict_order_id_truth_distinguishes_404_from_transport(monkeypatch):
    class _BrokerError(Exception):
        def __init__(self, status_code):
            super().__init__(f"broker status {status_code}")
            self.status_code = status_code

    class _Client:
        status_code = 404

        def get_order_by_id(self, _order_id):
            raise _BrokerError(self.status_code)

    client = _Client()
    monkeypatch.setattr(alpaca_mod, "_trading_client", lambda: client)

    absent = AlpacaSpotAdapter().get_order_truth("missing")
    assert absent == {"readable": True, "found": False, "order": None}

    client.status_code = 503
    unknown = AlpacaSpotAdapter().get_order_truth("unknown")
    assert unknown["readable"] is False
    assert unknown["found"] is False
    assert unknown["error"]["http_status"] == 503


def test_account_snapshot_includes_last_equity(monkeypatch):
    class _Account:
        id = "acct-snapshot-test"
        equity = "71876.85"
        last_equity = "73588.07"
        buying_power = "287507.40"
        cash = "71876.85"
        status = "ACTIVE"
        shorting_enabled = True
        multiplier = "4"

    class _Client:
        def get_account(self):
            return _Account()

    monkeypatch.setattr(alpaca_mod, "_trading_client", lambda: _Client())
    snap = AlpacaSpotAdapter().get_account_snapshot()

    assert snap["ok"] is True
    assert snap["equity"] == 71876.85
    assert snap["last_equity"] == 73588.07
    assert snap["paper"] is True
    assert snap["account_id"] == "acct-snapshot-test"


_IQFEED_PIN = "iqfeed-l1-quote-provenance-v2+sha256:0123456789abcdef"
_IQFEED_RUN_ID = "12553525-2da8-4b22-a69f-d3034871e90c"


def test_iqfeed_authoritative_build_pin_defaults_empty():
    field = settings.__class__.model_fields[
        "chili_iqfeed_l1_authoritative_bridge_build"
    ]
    assert field.default == ""


def _iqfeed_row(**overrides):
    now = datetime.now(timezone.utc)
    reference = now - timedelta(milliseconds=250)
    received = now - timedelta(milliseconds=100)
    values = {
        "id": 42,
        "bid": 1.47,
        "ask": 1.48,
        "mid": 1.475,
        "spread_bps": 67.7966,
        "observed_at": reference.replace(tzinfo=None),
        "source": "iqfeed_l1",
        "provider_event_at": None,
        "received_at": received,
        "timestamp_basis": "iqfeed_q_receive_trade_reference_fenced",
        "bridge_version": _IQFEED_PIN,
        "provider_trade_reference_at": reference,
        "message_type": "Q",
        "bridge_run_id": _IQFEED_RUN_ID,
        "connection_generation": 3,
    }
    values.update(overrides)
    return tuple(values[key] for key in (
        "id", "bid", "ask", "mid", "spread_bps", "observed_at", "source",
        "provider_event_at", "received_at", "timestamp_basis", "bridge_version",
        "provider_trade_reference_at", "message_type", "bridge_run_id",
        "connection_generation",
    ))


def _install_iqfeed_row(monkeypatch, row, captured=None):
    captured = captured if captured is not None else {}

    class _Result:
        def fetchone(self):
            return row

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, stmt, params):
            captured["sql"] = str(stmt)
            captured["params"] = params
            return _Result()

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(
        settings,
        "chili_iqfeed_l1_authoritative_bridge_build",
        _IQFEED_PIN,
        raising=False,
    )
    return captured


def test_iqfeed_quote_accepts_only_complete_exact_build_v2_tuple(monkeypatch):
    row = _iqfeed_row()
    captured = {}
    _install_iqfeed_row(monkeypatch, row, captured)
    tick, meta = AlpacaSpotAdapter()._iqfeed_l1_quote("ACTU", max_age_seconds=2.0)

    assert "source = 'iqfeed_l1'" in captured["sql"]
    assert "received_at IS NOT NULL" in captured["sql"]
    assert "ORDER BY observed_at DESC, id DESC" in captured["sql"]
    assert captured["params"] == {"s": "ACTU"}
    assert tick.product_id == "ACTU"
    assert tick.raw["feed"] == "iqfeed_l1"
    assert tick.raw["tape_row_id"] == 42
    assert tick.raw["timestamp_basis"] == "iqfeed_q_receive_trade_reference_fenced"
    assert tick.raw["provider_event_at_utc"] is None
    assert tick.raw["provider_trade_reference_at_utc"] == row[11].isoformat()
    assert tick.raw["received_at_utc"] == row[8].isoformat()
    assert tick.raw["bridge_version"] == _IQFEED_PIN
    assert tick.raw["message_type"] == "Q"
    assert tick.raw["bridge_run_id"] == _IQFEED_RUN_ID
    assert tick.raw["connection_generation"] == 3
    assert meta.provider_time_utc is None  # trade reference is not relabelled quote time
    assert meta.retrieved_at_utc == row[8]
    assert meta.age_seconds() < 2.0


def test_iqfeed_quote_rejects_legacy_receive_time_basis(monkeypatch):
    row = _iqfeed_row(
        timestamp_basis="bridge_received_at",
        bridge_version="iqfeed-l1-quote-provenance-v1+sha256:0123456789abcdef",
        provider_trade_reference_at=None,
        message_type=None,
        bridge_run_id=None,
        connection_generation=None,
    )
    _install_iqfeed_row(monkeypatch, row)
    assert AlpacaSpotAdapter()._iqfeed_l1_quote("ACTU", max_age_seconds=2.0) is None


def test_execution_bbo_falls_back_to_direct_quote_when_iqfeed_is_stale(monkeypatch):
    adapter = AlpacaSpotAdapter()
    now = datetime.now(timezone.utc)
    meta = FreshnessMeta(
        retrieved_at_utc=now,
        provider_time_utc=now - timedelta(milliseconds=100),
        max_age_seconds=60.0,
    )
    marker = NormalizedTicker(
        product_id="ACTU", bid=1.0, ask=1.01, mid=1.005, freshness=meta
    )
    monkeypatch.setattr(adapter, "_iqfeed_l1_quote", lambda *_a, **_k: None)
    monkeypatch.setattr(adapter, "_alpaca_latest_quote", lambda _pid: (marker, meta))
    monkeypatch.setattr(settings, "chili_alpaca_quotes_via_iqfeed", True, raising=False)

    tick, execution_meta = adapter.get_execution_bbo("ACTU", max_age_seconds=2.0)
    assert tick.product_id == "ACTU"
    assert execution_meta.max_age_seconds == 2.0


def test_execution_bbo_rejects_missing_or_future_provider_timestamp(monkeypatch):
    adapter = AlpacaSpotAdapter()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(adapter, "_iqfeed_l1_quote", lambda *_a, **_k: None)
    monkeypatch.setattr(settings, "chili_alpaca_quotes_via_iqfeed", True, raising=False)

    def direct(provider_time):
        meta = FreshnessMeta(
            retrieved_at_utc=now,
            provider_time_utc=provider_time,
            max_age_seconds=60.0,
        )
        tick = NormalizedTicker(
            product_id="ACTU", bid=1.0, ask=1.01, mid=1.005, freshness=meta
        )
        return tick, meta

    monkeypatch.setattr(adapter, "_alpaca_latest_quote", lambda _pid: direct(None))
    tick, _ = adapter.get_execution_bbo("ACTU", max_age_seconds=2.0)
    assert tick is None

    monkeypatch.setattr(
        adapter,
        "_alpaca_latest_quote",
        lambda _pid: direct(now + timedelta(seconds=5)),
    )
    tick, _ = adapter.get_execution_bbo("ACTU", max_age_seconds=2.0)
    assert tick is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"bridge_version": "iqfeed-l1-quote-provenance-v2+sha256:ffffffffffffffff"},
        {"message_type": "P"},
        {"connection_generation": 0},
        {"bridge_run_id": "not-a-uuid"},
        {"timestamp_basis": "bridge_received_at"},
        {"provider_event_at": datetime.now(timezone.utc)},
        {"received_at": datetime.now(timezone.utc).replace(tzinfo=None)},
        {"received_at": datetime.now(timezone(timedelta(hours=-4)))},
        {"provider_trade_reference_at": datetime.now(timezone.utc).replace(tzinfo=None)},
    ],
)
def test_iqfeed_quote_rejects_incomplete_or_mismatched_v2_tuple(
    monkeypatch,
    overrides,
):
    _install_iqfeed_row(monkeypatch, _iqfeed_row(**overrides))
    assert AlpacaSpotAdapter()._iqfeed_l1_quote("ACTU", max_age_seconds=2.0) is None


@pytest.mark.parametrize(
    ("reference_age_s", "received_age_s"),
    [
        (2.01, 0.10),   # replay: fresh receive cannot revive an old reference
        (0.10, 2.01),   # fresh reference cannot revive an old receive
        (-1.01, -0.90), # future provider/reference clocks are impossible truth
        (3.10, 0.10),   # receive-reference delta exceeds the 2s causal fence
        (-0.90, 0.20),  # reference follows receive by more than 1s
    ],
)
def test_iqfeed_quote_ages_both_clocks_and_rejects_replay(
    monkeypatch,
    reference_age_s,
    received_age_s,
):
    now = datetime.now(timezone.utc)
    reference = now - timedelta(seconds=reference_age_s)
    received = now - timedelta(seconds=received_age_s)
    row = _iqfeed_row(
        observed_at=reference.replace(tzinfo=None),
        provider_trade_reference_at=reference,
        received_at=received,
    )
    _install_iqfeed_row(monkeypatch, row)
    assert AlpacaSpotAdapter()._iqfeed_l1_quote("ACTU", max_age_seconds=2.0) is None


def test_unset_exact_build_pin_skips_iqfeed_and_uses_direct_alpaca(monkeypatch):
    adapter = AlpacaSpotAdapter()
    now = datetime.now(timezone.utc)
    meta = FreshnessMeta(
        retrieved_at_utc=now,
        provider_time_utc=now - timedelta(milliseconds=50),
        max_age_seconds=60.0,
    )
    marker = NormalizedTicker(
        product_id="ACTU", bid=1.0, ask=1.01, mid=1.005, freshness=meta
    )
    monkeypatch.setattr(
        settings,
        "chili_iqfeed_l1_authoritative_bridge_build",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        db_mod,
        "SessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("unpinned IQFeed queried")),
    )
    monkeypatch.setattr(adapter, "_alpaca_latest_quote", lambda _pid: (marker, meta))
    monkeypatch.setattr(settings, "chili_alpaca_quotes_via_iqfeed", True, raising=False)

    tick, execution_meta = adapter.get_execution_bbo("ACTU", max_age_seconds=2.0)
    assert tick.product_id == marker.product_id
    assert tick.bid == marker.bid
    assert tick.ask == marker.ask
    assert execution_meta.max_age_seconds == 2.0


def test_bound_adapter_blocks_credential_rotation_before_account_mutation(monkeypatch):
    old_client = object()
    new_calls = []

    class _NewClient:
        def cancel_order_by_id(self, order_id):
            new_calls.append(order_id)

    new_client = _NewClient()
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        "acct-old",
        raising=False,
    )
    adapter = AlpacaSpotAdapter()
    assert adapter.bind_account_id("acct-old") is True
    with alpaca_mod._clients_lock:
        alpaca_mod._clients.update(
            {
                "trading:paper": old_client,
                "trading:observed_account_id": "acct-old",
            }
        )

    def _rotated_client():
        with alpaca_mod._clients_lock:
            alpaca_mod._clients["trading:paper"] = new_client
            alpaca_mod._clients["trading:observed_account_id"] = "acct-new"
        return new_client

    monkeypatch.setattr(alpaca_mod, "_trading_client", _rotated_client)
    assert adapter.cancel_order_by_id("old-session-order") is False
    assert new_calls == []
    alpaca_mod.reset_clients_for_tests()


def test_governed_equity_product_and_entry_are_whole_share_only(monkeypatch):
    class _Asset:
        tradable = True
        status = "active"
        fractionable = True
        min_trade_increment = "0.000001"
        min_order_size = "0.000001"
        price_increment = "0.01"
        symbol = "ACTU"

    class _Client:
        def get_asset(self, _symbol):
            return _Asset()

        def submit_order(self, _request):
            raise AssertionError("fractional entry reached broker transport")

    adapter = AlpacaSpotAdapter()
    monkeypatch.setattr(adapter, "_account_client", lambda: _Client())
    product, _ = adapter.get_product("ACTU")
    assert product.base_increment == 1.0
    assert product.base_min_size == 1.0

    entry = adapter.place_limit_order_gtc(
        product_id="ACTU",
        side="buy",
        base_size="1.5",
        limit_price="10.00",
        client_order_id="fractional-entry",
        position_intent="buy_to_open",
        time_in_force="day",
    )
    deadman = adapter.place_deadman_stop(
        product_id="ACTU",
        base_size="1.5",
        stop_price=9.5,
        client_order_id="fractional-deadman",
    )
    assert entry["error"] == "alpaca_fractional_entry_not_certified"
    assert entry["pre_submit_blocked"] is True
    assert deadman["error"] == "alpaca_fractional_deadman_not_certified"
    assert deadman["pre_submit_blocked"] is True


def test_sub_dollar_deadman_stop_keeps_four_decimal_protection(monkeypatch):
    import sys
    import types

    submitted = []

    alpaca_pkg = types.ModuleType("alpaca")
    alpaca_pkg.__path__ = []
    trading_pkg = types.ModuleType("alpaca.trading")
    trading_pkg.__path__ = []
    enums_mod = types.ModuleType("alpaca.trading.enums")
    requests_mod = types.ModuleType("alpaca.trading.requests")

    class _OrderSide:
        SELL = "sell"

    class _PositionIntent:
        SELL_TO_CLOSE = "sell_to_close"

    class _TimeInForce:
        GTC = "gtc"

    class _StopOrderRequest:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    enums_mod.OrderSide = _OrderSide
    enums_mod.PositionIntent = _PositionIntent
    enums_mod.TimeInForce = _TimeInForce
    requests_mod.StopOrderRequest = _StopOrderRequest
    monkeypatch.setitem(sys.modules, "alpaca", alpaca_pkg)
    monkeypatch.setitem(sys.modules, "alpaca.trading", trading_pkg)
    monkeypatch.setitem(sys.modules, "alpaca.trading.enums", enums_mod)
    monkeypatch.setitem(sys.modules, "alpaca.trading.requests", requests_mod)

    class _Order:
        id = "deadman-sub-dollar-oid"
        status = type("_Status", (), {"value": "new"})()

    class _Client:
        def submit_order(self, request):
            submitted.append(request)
            return _Order()

    adapter = AlpacaSpotAdapter()
    monkeypatch.setattr(adapter, "_account_client", lambda: _Client())

    assert quantize_alpaca_equity_sell_stop_price(0.004) == "0.0040"
    assert quantize_alpaca_equity_sell_stop_price(0.94561) == "0.9457"
    assert quantize_alpaca_equity_sell_stop_price(1.0001) == "1.01"

    result = adapter.place_deadman_stop(
        product_id="PENNY",
        base_size="10",
        stop_price=0.94561,
        client_order_id="deadman-sub-dollar-parent",
    )
    assert result["ok"] is True
    assert result["client_order_id"] == "deadman-sub-dollar-parent"
    assert result["stop_price"] == "0.9457"
    assert result["order_request"]["stop_price"] == "0.9457"
    assert len(submitted) == 1
    assert float(submitted[0].stop_price) == pytest.approx(0.9457)
    assert str(submitted[0].client_order_id) == "deadman-sub-dollar-parent"
