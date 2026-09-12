import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.services.trading.momentum_neural import broker_coverage_inventory as m
from app.services.trading.venue import alpaca_spot as venue

ACCOUNT = "aaaaaaaa-2222-4333-8444-555555555555"


def position(n=1, symbol="A", **extra):
    return dict(asset_id=str(UUID(int=n)), asset_class="us_equity", symbol=symbol,
                qty="0.000000000123456789", **extra)


def order(n=10, symbol="B", **extra):
    return dict(asset_id=str(UUID(int=n+1000)), asset_class="us_equity", symbol=symbol,
                id=str(UUID(int=n)), side="sell", status="pending_cancel", qty=None,
                notional="1.23456789", filled_qty="0.00000001", **extra)


def read(reason, rows=None, *, start=10, error=None):
    return m.coverage_read(reason, started_ns=start, completed_ns=start+1,
                           response=rows, error=error, expected_account_id=ACCOUNT)


def probe(held=None, pending=None, *, start=10):
    return m.coverage_probe(expected_account_id=ACCOUNT, started_ns=start, completed_ns=start+5,
        reads=(read("held", held, start=start+1), read("pending", pending, start=start+3)))


def test_all_asset_classes_and_native_symbols_preserved_without_quantity_or_status_gates():
    crypto = {**position(2,"BTCUSD"), "asset_class":"crypto"}
    unusual = {**position(3,"ODD-W"), "qty":"-1.234", "side":"short"}
    other = {**position(4,"OPTION"), "asset_class":"us_option"}
    pending = [{**order(10,"BTC/USD"), "asset_class":"crypto", "status":"unfamiliar"},
               {**order(11,"B"), "side":"buy", "status":"partially_filled"}]
    value = probe([position(),crypto,unusual,other],pending)
    assert all(r.complete for r in value.reads)
    assert {r.broker_symbol for r in value.reads[0].members} == {"A","BTCUSD","ODD-W","OPTION"}
    assert json.loads(value.reads[0].members[0].metadata_json)["qty"] == "0.000000000123456789"
    assert json.loads(value.reads[1].members[0].metadata_json)["notional"] == "1.23456789"
    assert not value.atomic_account_snapshot and not value.order_authority
    assert probe(list(reversed([position(),crypto,unusual,other])),list(reversed(pending))) == value


def test_partial_observation_cannot_clear_either_side_of_an_exposure_transition():
    book=m.CoverageBook(expected_account_id=ACCOUNT)
    prior=book.apply(probe([position()],[order()]))
    latest=book.apply(probe([],None,start=30))
    assert latest.held == prior.held and latest.pending == prior.pending
    assert latest.unavailable_reasons == ("pending",)
    latest=book.apply(probe(None,[],start=50))
    assert latest.pending == prior.pending and latest.unavailable_reasons == ("held",)
    assert latest.revision == 3
    assert not latest.membership_replacement_complete
    latest=book.apply(probe([],[],start=70))
    assert latest.held == () and latest.pending == () and latest.membership_replacement_complete


def test_incomplete_census_adds_new_observed_names_without_erasing_previous_exposure():
    book=m.CoverageBook(expected_account_id=ACCOUNT)
    book.apply(probe([position()],[order()]))
    rows=[order(n,"S"+str(n)) for n in range(20,520)]
    value=probe([position(2,"C")],rows,start=30)
    assert value.reads[1].error == "open_order_response_bound_reached"
    assert len(value.reads[1].members) == m.OPEN_ORDER_RESPONSE_LIMIT
    state=book.apply(value)
    assert len(state.pending) == 501 and {p.broker_symbol for p in state.held} == {"A","C"}
    assert state.unavailable_reasons == ("pending",)
    state=book.apply(probe([],[],start=50))
    assert state.pending == () and state.held == () and state.unavailable_reasons == ()


@pytest.mark.parametrize("bad", [None, {}, {"asset_id":"bad"}, {**position(),"symbol":" a "}])
def test_malformed_member_cannot_turn_partial_response_into_empty_success(bad):
    value=read("held",[position(2,"GOOD"),bad])
    assert not value.complete and value.error == "coverage_member_invalid"
    assert [r.broker_symbol for r in value.members] == ["GOOD"]


def test_conflicting_identity_and_foreign_account_discard_whole_read():
    for rows in ([position(),{**position(),"symbol":"OTHER"}],
                 [position(),{**position(2,"B"),"account_id":"bbbbbbbb-2222-4333-8444-555555555555"}]):
        value=read("held",rows)
        assert not value.complete and value.members == ()


def test_duplicate_identity_is_incomplete_even_when_payloads_match():
    value=read("pending",[order(),order()])
    assert not value.complete and value.error == "coverage_duplicate_identity"
    assert len(value.members)==1


def test_response_at_api_bound_never_certifies_full_pending_set():
    assert read("pending",[order(n,"S"+str(n)) for n in range(1,500)]).complete
    assert not read("pending",[order(n,"S"+str(n)) for n in range(1,501)]).complete
    assert read("held",[position(n,"S"+str(n)) for n in range(1,502)]).complete


def test_book_refuses_account_and_clock_regression_without_mutation():
    from dataclasses import replace
    book=m.CoverageBook(expected_account_id=ACCOUNT)
    value=probe([position()],[order()])
    state=book.apply(value)
    with pytest.raises(ValueError,match="regression"): book.apply(value)
    with pytest.raises(ValueError,match="account_mismatch"):
        book.apply(replace(value,account_identity_sha256="0"*64))
    assert book.state is state


class Client:
    def __init__(self):
        self.calls=[]; self.account_calls=0; self.fail=None; self.change_account=False
    def get_account(self):
        self.calls.append("account"); self.account_calls+=1
        return SimpleNamespace(id="bbbbbbbb-2222-4333-8444-555555555555" if self.change_account and self.account_calls>1 else ACCOUNT)
    def get_all_positions(self):
        self.calls.append("positions")
        if self.fail=="held": raise RuntimeError("sensitive detail")
        return [position()]
    def get_orders(self,*,filter):
        self.calls.append("orders")
        assert filter.model_dump(exclude_none=True)==dict(status="open",limit=500,direction="asc",nested=False)
        if self.fail=="pending": raise RuntimeError("sensitive detail")
        return [order()]


def adapter(monkeypatch,client):
    result=venue.AlpacaSpotAdapter()
    monkeypatch.setattr(venue,"_expected_account_id",lambda:ACCOUNT)
    monkeypatch.setattr(venue,"_paper",lambda:True)
    monkeypatch.setattr(result,"_account_client",lambda:client)
    return result


def test_adapter_independent_collections_are_account_bracketed_and_unfiltered(monkeypatch):
    client=Client(); value=adapter(monkeypatch,client).get_coverage_inventory_probe()
    assert client.calls == ["account","orders","positions","orders","account"]
    assert all(r.complete for r in value.reads)
    assert len(value.reads[1].bracket_reads) == 2


@pytest.mark.parametrize("failed_reason",["held","pending"])
def test_adapter_one_failed_collection_does_not_hide_the_other(monkeypatch,failed_reason):
    client=Client(); client.fail=failed_reason
    value=adapter(monkeypatch,client).get_coverage_inventory_probe()
    for r in value.reads:
        assert r.complete == (r.reason != failed_reason)
        assert "sensitive" not in str(r.error)


def test_adapter_post_read_account_change_invalidates_both_sections(monkeypatch):
    client=Client(); client.change_account=True
    value=adapter(monkeypatch,client).get_coverage_inventory_probe()
    assert all(not r.complete and r.members == () for r in value.reads)


def test_live_posture_cannot_consult_client_or_credentials(monkeypatch):
    monkeypatch.setattr(venue,"_expected_account_id",lambda:ACCOUNT)
    monkeypatch.setattr(venue,"_paper",lambda:False)
    def forbidden(): raise AssertionError("client accessed")
    value=venue.AlpacaSpotAdapter()
    monkeypatch.setattr(value,"_account_client",forbidden)
    result=value.get_coverage_inventory_probe()
    assert all(r.error=="coverage_account_unavailable:RuntimeError" and not r.complete for r in result.reads)


def test_fill_between_position_and_final_order_read_cannot_release_pending_coverage(monkeypatch):
    client=Client(); count=0
    def orders(*,filter):
        nonlocal count
        count+=1
        return [order()] if count==1 else []
    monkeypatch.setattr(client,"get_all_positions",lambda:[])
    monkeypatch.setattr(client,"get_orders",orders)
    value=adapter(monkeypatch,client).get_coverage_inventory_probe()
    assert value.reads[0].complete and not value.reads[1].complete
    state=m.CoverageBook(expected_account_id=ACCOUNT).apply(value)
    assert state.held == () and [p.broker_symbol for p in state.pending] == ["B"]
    assert state.unavailable_reasons == ("pending",)


@pytest.mark.parametrize("before,after",[
    ([order()],[]), ([],[order()]), (None,[order()]), ([order()],None),
    ([order()],[{**order(),"status":"partially_filled"}]),
])
def test_changed_or_failed_order_bracket_keeps_union_and_requires_reconciliation(before,after):
    value=m.bracket_pending_reads(read("pending",before,start=10),read("pending",after,start=30))
    assert not value.complete and len(value.members)==1
    assert value.error=="pending_bracket_changed_or_incomplete"
    assert value.bracket_reads[0].started_ns==10 and value.bracket_reads[1].started_ns==30


def test_bracket_identity_conflict_cannot_relabel_retained_order():
    value=m.bracket_pending_reads(read("pending",[order()],start=10),
        read("pending",[{**order(),"asset_id":str(UUID(int=999))}],start=30))
    assert not value.complete and value.members==()
    assert value.error=="pending_bracket_identity_conflict"
