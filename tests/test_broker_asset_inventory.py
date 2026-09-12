from dataclasses import FrozenInstanceError
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.services.trading.momentum_neural import broker_asset_inventory as m
from app.services.trading.venue import alpaca_spot as venue

ACCOUNT="aaaaaaaa-2222-4333-8444-555555555555"


def asset(n,symbol,cls="us_equity",**extra):
    return {"id":str(UUID(int=n)),"class":cls,"symbol":symbol,"status":"active","tradable":True,**extra}


def inventory(equities=None,crypto=None,*,start=10,account=ACCOUNT):
    return m.build_inventory(expected_account_id=account,account_before=account,account_after=account,
        started_ns=start,completed_ns=start+10,catalog_responses={
            "us_equity":(start+1,start+3,equities if equities is not None else [asset(1,"A")]),
            "crypto":(start+4,start+8,crypto if crypto is not None else [asset(2,"BTC/USD","crypto")])})


def test_full_catalog_has_no_top_n_price_volume_or_ross_gate_and_preserves_asset_keys():
    rows=[asset(i+10,"E"+str(i),price=0 if i%2 else 999999,volume=0,ross_rank=999999) for i in range(2501)]
    rows += [asset(3000,"BRK.B"),asset(3001,"ODD-W"),asset(3002,"DISABLED",tradable=False)]
    crypto=[asset(4000,"BTC/USD","crypto",min_trade_increment="0.000000001"),
            asset(4001,"BTC/USDT","crypto"),asset(4002,"E1","crypto")]
    value=inventory(rows,crypto)
    assert len(value.assets)==2507
    assert len(value.tradable_symbols("us_equity"))==2503
    assert value.tradable_symbols("crypto")==("BTC/USD","BTC/USDT","E1")
    assert ("us_equity","E1") in value.tradable_keys and ("crypto","E1") in value.tradable_keys
    assert "ODD-W" in value.tradable_symbols("us_equity")
    disabled=next(a for a in value.assets if a.symbol=="DISABLED")
    assert not disabled.tradable and disabled.key not in value.tradable_keys
    assert json.loads(value.assets[0].metadata_json)["min_trade_increment"]=="0.000000001"
    assert not value.order_authority and not value.cross_class_atomic
    assert sum(c.response_count for c in value.catalogs)==len(value.assets)
    reordered=inventory(list(reversed(rows)),list(reversed(crypto)))
    assert reordered==value
    changed=inventory([{**r,"ross_rank":-100,"volume":999} for r in rows],crypto)
    assert changed.tradable_keys==value.tradable_keys  # Metadata changes, membership cannot.
    with pytest.raises(FrozenInstanceError): value.assets=()


@pytest.mark.parametrize("damage",["duplicate_id","duplicate_symbol","missing_tradable","wrong_class","inactive","string_bool","alias_conflict"])
def test_incomplete_or_ambiguous_catalog_is_failure_not_partial_inventory(damage):
    rows=[asset(1,"A"),asset(3,"B")]
    if damage=="duplicate_id": rows[1]["id"]=rows[0]["id"]
    if damage=="duplicate_symbol": rows[1]["symbol"]="A"
    if damage=="missing_tradable": rows[1].pop("tradable")
    if damage=="wrong_class": rows[1]["class"]="crypto"
    if damage=="inactive": rows[1]["status"]="inactive"
    if damage=="string_bool": rows[1]["tradable"]="false"
    if damage=="alias_conflict": rows[1]["asset_class"]="crypto"
    with pytest.raises(ValueError): inventory(rows)


def test_refresh_failure_keeps_all_prior_inventory_and_successful_empty_is_distinct():
    book=m.InventoryBook(expected_account_id=ACCOUNT)
    good=inventory()
    assert book.apply(m.InventoryProbe(good,None)).available
    failed=book.apply(m.InventoryProbe(None,"fixture-provider-unavailable"))
    assert not failed.available and failed.last_success is good and failed.revision==2
    assert failed.last_success.tradable_keys==good.tradable_keys
    empty=inventory([],[],start=30)
    latest=book.apply(m.InventoryProbe(empty,None))
    assert latest.available and latest.last_success.tradable_keys==() and latest.revision==3
    with pytest.raises(ValueError,match="regression"): book.apply(m.InventoryProbe(good,None))
    assert book.state is latest
    with pytest.raises(ValueError,match="account_mismatch"):
        book.apply(m.InventoryProbe(inventory(account="bbbbbbbb-2222-4333-8444-555555555555",start=60),None))
    assert book.state is latest


def test_missing_class_or_non_list_response_never_claims_complete_inventory():
    args=dict(expected_account_id=ACCOUNT,account_before=ACCOUNT,account_after=ACCOUNT,started_ns=10,completed_ns=20)
    with pytest.raises(ValueError,match="catalogs_incomplete"):
        m.build_inventory(**args,catalog_responses={"us_equity":(11,12,[])})
    with pytest.raises(ValueError,match="response_invalid"):
        m.build_inventory(**args,catalog_responses={"us_equity":(11,12,[]),"crypto":(13,14,None)})


class Client:
    def __init__(self): self.calls=[];self.account_calls=0;self.fail_crypto=False;self.change_account=False
    def get_account(self):
        self.account_calls+=1
        return SimpleNamespace(id="bbbbbbbb-2222-4333-8444-555555555555" if self.change_account and self.account_calls>1 else ACCOUNT)
    def get_all_assets(self,request):
        filters=request.model_dump(exclude_none=True)
        assert set(filters)=={"asset_class","status"}
        assert request.status.value=="active"
        cls=request.asset_class.value;self.calls.append(cls)
        if cls=="crypto" and self.fail_crypto: raise RuntimeError("sensitive fixture provider detail")
        return [asset(1,"A")] if cls=="us_equity" else [asset(2,"BTC/USD","crypto")]


def adapter(monkeypatch,client):
    value=venue.AlpacaSpotAdapter()
    monkeypatch.setattr(value,"_account_client",lambda:client)
    monkeypatch.setattr(venue,"_expected_account_id",lambda:ACCOUNT)
    return value


def test_actual_adapter_probes_both_full_classes_and_fresh_account_pins(monkeypatch):
    client=Client();value=adapter(monkeypatch,client)
    probe=value.get_asset_inventory_probe()
    assert probe.error is None and probe.snapshot.tradable_keys==(("crypto","BTC/USD"),("us_equity","A"))
    assert client.calls==["us_equity","crypto"] and client.account_calls==2
    assert probe.snapshot.started_ns<=min(c.started_ns for c in probe.snapshot.catalogs)
    assert probe.snapshot.completed_ns>=max(c.completed_ns for c in probe.snapshot.catalogs)


@pytest.mark.parametrize("failure",["second_class","changed_account","first_account"])
def test_adapter_failure_cannot_publish_partial_catalog_or_leak_provider_message(monkeypatch,failure):
    client=Client();value=adapter(monkeypatch,client)
    if failure=="second_class": client.fail_crypto=True
    if failure=="changed_account": client.change_account=True
    if failure=="first_account": monkeypatch.setattr(client,"get_account",lambda:SimpleNamespace(id="wrong"))
    probe=value.get_asset_inventory_probe()
    assert probe.snapshot is None and probe.error.startswith("asset_inventory_unavailable:")
    assert "sensitive" not in probe.error
    if failure=="first_account": assert client.calls==[]


def test_live_posture_refuses_before_client_construction(monkeypatch):
    monkeypatch.setattr(venue,"_paper",lambda:False)
    def forbidden(): raise AssertionError("credentials consulted")
    monkeypatch.setattr(venue,"_keys",forbidden)
    probe=venue.AlpacaSpotAdapter().get_asset_inventory_probe()
    assert probe.snapshot is None and probe.error=="asset_inventory_unavailable:RuntimeError"
