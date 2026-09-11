from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import entry_fill_clock as clock


def _case():
    binding = dict(session_id=7, symbol="TNON", account_scope="alpaca:paper", account_id="acct-A",
                   claim_token="token-A", order_id="OID-A", client_order_id="CID-A", side="buy", adopted_quantity=156.)
    request = dict(product_id="TNON", side="buy", client_order_id="CID-A", base_size="530",
                   order_type="limit", position_intent="buy_to_open", time_in_force="day",
                   extended_hours=True, limit_price="7.19")
    authority = dict(binding, account_verified=True, order_request=request)
    order = SimpleNamespace(order_id="OID-A", client_order_id="CID-A", product_id="TNON", side="buy",
                            status="canceled", order_type="limit", filled_size=156.,
                            created_time="2026-09-11T10:41:47.428166833Z", raw={
        "filled_at": "2026-09-11T10:42:10.424407832Z", "submitted_at": "2026-09-11T10:41:47.437556087Z",
        "canceled_at": "2026-09-11T10:42:11.328409185Z", "qty": 530., "filled_size": 156,
        "alpaca_status": "canceled", "alpaca_filled_qty": "156", "fill_truth_readable": True,
        "broker_order_id_echo": "OID-A", "broker_client_order_id_echo": "CID-A",
        "broker_symbol_echo": "TNON", "broker_side_echo": "buy", "broker_quantity_echo": "530",
        "broker_filled_quantity_echo": "156", "broker_order_status_echo": "canceled",
        "time_in_force": "day", "position_intent": "buy_to_open", "extended_hours": True, "limit_price": 7.19,
    })
    observed = datetime(2026,9,11,10,42,12,tzinfo=timezone.utc)
    recorded = datetime(2026,9,11,10,42,15,tzinfo=timezone.utc)
    return clock.observe(order, at=observed), binding, authority, recorded


def _choose(case, mode="ordinary_alpaca"):
    o,b,a,t = case
    return clock.select(o, binding=b, authority=a, recorded_at=t, mode=mode)


def test_actual_terminal_partial_shape_needs_no_optional_account_echo():
    case = _case()
    result = _choose(case)
    assert result["source"] == "broker_order_reported_fill_at"
    assert result["entry_filled_at_utc"] == "2026-09-11T10:42:10.424407+00:00"
    assert result["broker_filled_at_raw"].endswith("424407832Z")
    assert result["recorded_at_utc"] == case[3].isoformat()
    assert result["quantity_kind"] == "terminal_partial_quantity"
    assert result["first_execution_time_known"] is False


@pytest.mark.parametrize("value", [None, "", "unknown", {}, [], 123, True,
    "2026-09-11T10:42:10", "2026-09-11T10:42:12.000000001Z",
    "2026-09-11T10:41:40Z", "9999-12-31T23:59:59-12:00"])
def test_missing_malformed_unaware_future_or_reversed_clock_falls_back(value):
    case = _case(); case[0]["raw"]["filled_at"] = value
    result = _choose(case)
    assert result["source"] == "local_adoption_stamp"
    assert result["entry_filled_at_utc"] == case[3].isoformat()
    assert result["fallback_reason"]


@pytest.mark.parametrize("key,value", [
    ("broker_account_id_echo", "foreign"), ("broker_order_id_echo", "wrong"),
    ("broker_client_order_id_echo", "wrong"), ("broker_symbol_echo", "OTHER"),
    ("broker_side_echo", "sell"), ("broker_order_status_echo", "filled"),
    ("broker_quantity_echo", "531"), ("broker_filled_quantity_echo", "157"),
    ("alpaca_filled_qty", float("nan")), ("filled_size", None), ("qty", float("inf")),
    ("fill_truth_readable", False), ("replaced_by", "another-order"), ("replaces", "prior-order"),
    ("broker_extended_hours_echo", "true"), ("broker_limit_price_echo", 7.20),
])
def test_contradictory_broker_evidence_never_grants_clock(key, value):
    case = _case();case[0]["raw"][key] = value
    assert _choose(case)["source"] == "local_adoption_stamp"


@pytest.mark.parametrize("key,value", [("session_id", 8), ("account_id", "acct-B"),
    ("claim_token", "recycled"), ("order_id", "OID-B"), ("client_order_id", "CID-B"),
    ("symbol", "OTHER"), ("side", "sell"), ("adopted_quantity", 155)])
def test_stale_binding_cannot_take_new_clock(key, value):
    case = _case();case[1][key] = value
    assert _choose(case)["source"] == "local_adoption_stamp"


@pytest.mark.parametrize("mode", ["replay", "captured", "other_venue"])
def test_other_clock_contracts_keep_existing_timestamp(mode):
    case = _case()
    result = _choose(case, mode)
    assert result["entry_filled_at_utc"] == case[3].isoformat()
    assert result["fallback_reason"] == mode+"_clock_contract_preserved"


def test_observation_and_selected_binding_do_not_alias_mutable_source():
    case = _case(); result = _choose(case)
    case[1]["order_id"] = "another"
    assert result["binding"]["order_id"] == "OID-A"
    original = deepcopy(case[0])
    _choose(case)
    assert case[0] == original


@pytest.mark.parametrize("link", [None, "replaces", "replaced_by"])
def test_actual_alpaca_normalizer_retains_terminal_partial_clock_without_account_echo(link):
    from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter
    case = _case()
    provider = SimpleNamespace(
        id="OID-A", client_order_id="CID-A", symbol="TNON", side="buy", status="canceled",
        type="limit", qty="530", filled_qty="156", filled_avg_price="7.19",
        created_at="2026-09-11T10:41:47.428166833Z", submitted_at="2026-09-11T10:41:47.437556087Z",
        filled_at="2026-09-11T10:42:10.424407832Z", time_in_force="day", extended_hours=True,
        position_intent="buy_to_open", limit_price="7.19", asset_class="us_equity",
    )
    if link is not None:
        setattr(provider, link, "linked-order")
    order = AlpacaSpotAdapter._normalize_order(None, provider)
    case = (clock.observe(order, at=datetime(2026,9,11,10,42,12,tzinfo=timezone.utc)), *case[1:])
    assert order.raw["broker_account_id_echo"] is None
    if link is None:
        assert _choose(case)["source"] == "broker_order_reported_fill_at"
    else:
        assert order.raw[link] == case[0]["raw"][link] == "linked-order"
        result = _choose(case)
        assert result["source"] == "local_adoption_stamp"
        assert result["entry_filled_at_utc"] == case[3].isoformat()


@pytest.mark.parametrize("change", ["account", "oid_case", "cid_case", "open", "oversized"])
def test_remaining_authority_and_terminal_counterexamples(change):
    case = _case()
    if change == "account": case[2]["claim_account_id"] = "foreign"
    if change == "oid_case": case[0]["order_id"] = "oid-a"
    if change == "cid_case": case[0]["client_order_id"] = "cid-a"
    if change == "open":
        case[0]["status"] = "open";case[0]["raw"]["alpaca_status"] = "partially_filled"
    if change == "oversized": case[0]["filled_size"] = 531
    assert _choose(case)["source"] == "local_adoption_stamp"
