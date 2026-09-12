"""Native fractional evidence cannot be replaced by whole shares or guessed flatness."""
from decimal import Decimal, localcontext
from types import SimpleNamespace

import pytest

from app.services.trading.venue.crypto_execution_truth import crypto_order_truth, crypto_position_truth
from app.services.trading.venue import alpaca_spot as ap

ASSET={'id':'276e2673-764b-4ab6-a611-caf665ca6340','symbol':'BTC/USD','class':'crypto'}
OID='10000000-0000-0000-0000-000000000001'
ACCOUNT='20000000-0000-0000-0000-000000000001'


def order(**change):
    return dict(id=OID,client_order_id='owned-buy',asset_id=ASSET['id'],symbol='BTC/USD',
        asset_class='crypto',side='buy',status='partially_filled',type='limit',time_in_force='ioc',
        qty='0.000100000000000001',notional=None,filled_qty='0.000040000000000001',
        filled_avg_price='61234.56789123456789',limit_price='61235',**change)


def position():
    return dict(asset_id=ASSET['id'],symbol='BTCUSD',asset_class='crypto',side='long',
        qty='0.000039900000000001',qty_available='0.000039900000000001',avg_entry_price='61234.56789123456789')


def decoded(row):
    return crypto_order_truth(row,asset=ASSET,expected_order_id=OID)


def test_actual_fractional_fill_and_fee_adjusted_balance_remain_distinct():
    with localcontext() as ctx:
        ctx.prec=2
        o=decoded(order())
        p=crypto_position_truth(position(),asset=ASSET)
    assert o.filled_quantity==Decimal('0.000040000000000001')
    assert o.average_fill_price==Decimal('61234.56789123456789')
    assert not o.terminal
    assert p.available_quantity==Decimal('0.000039900000000001')
    assert p.whole_balance_available and p.quantity<o.filled_quantity
    # The balance difference alone does not establish a fee or ownership.
    assert not hasattr(p,'proven_fee') and not hasattr(p,'order_authority')


@pytest.mark.parametrize('available,expected',[(None,False),('0',False),('0.00001',False),('0.000039900000000001',True)])
def test_missing_or_reserved_available_balance_never_becomes_full_sale(available,expected):
    row=position(); row['qty_available']=available
    p=crypto_position_truth(row,asset=ASSET)
    assert p.whole_balance_available is expected
    assert p.available_quantity is None if available is None else p.available_quantity==Decimal(available)


@pytest.mark.parametrize('status,terminal',[('canceled',True),('expired',True),('rejected',True),
    ('pending_cancel',False),('replaced',False),('new_provider_status',False)])
def test_terminal_partial_fill_is_not_zero_and_uncertain_status_retains_reservation(status,terminal):
    row=order(); row['status']=status
    result=decoded(row)
    assert result.terminal is terminal and result.filled_quantity>0
    row['replaced_by']='10000000-0000-0000-0000-000000000002'
    assert not decoded(row).terminal


@pytest.mark.parametrize('change',[
    {'filled_qty':0.00004},{'filled_qty':None},{'filled_qty':'NaN'}, {'filled_qty':'-1'},
    {'filled_qty':'0.0002'},{'status':'filled'}, {'qty':None,'notional':None},
    {'asset_id':'10000000-0000-0000-0000-000000000099'}, {'asset_class':'us_equity'},
    {'symbol':'ETH/USD'}, {'time_in_force':'day'}, {'extended_hours':True},
    {'type':'stop'},{'order_class':'oco'},{'limit_price':None},{'filled_avg_price':123.5},
    {'id':'10000000-0000-0000-0000-000000000099'}, {'legs':[{}]},
])
def test_incompatible_echo_never_certifies_native_quantity(change):
    row=order(); row.update(change)
    with pytest.raises(ValueError): decoded(row)


def test_notional_order_and_missing_average_are_not_rounded_into_share_quantity():
    row=order(); row.update(qty=None,notional='2.50',filled_avg_price=None)
    result=decoded(row)
    assert result.quantity is None and result.notional==Decimal('2.50')
    assert result.average_fill_price is None and result.filled_quantity>0


@pytest.mark.parametrize('change',[{'qty':0.001},{'qty_available':'1'},{'side':'short'},
    {'asset_id':'10000000-0000-0000-0000-000000000099'},{'symbol':'BT/USD'}])
def test_incompatible_position_cannot_become_sellable(change):
    row=position(); row.update(change)
    with pytest.raises(ValueError): crypto_position_truth(row,asset=ASSET)


def adapter(monkeypatch,client):
    monkeypatch.setattr(ap,'_paper',lambda:True)
    monkeypatch.setattr(ap,'_expected_account_id',lambda:ACCOUNT)
    a=ap.AlpacaSpotAdapter(); assert a.bind_account_id(ACCOUNT)
    monkeypatch.setattr(a,'_account_client',lambda:client)
    return a


def test_native_adapter_uses_uuid_reads_and_exact_evidence(monkeypatch):
    calls=[]
    class Client:
        def get_order_by_id(self,oid,filter):
            calls.append(('order',oid)); return SimpleNamespace(**order())
        def get_open_position(self,asset_id):
            calls.append(('position',asset_id)); return SimpleNamespace(**position())
    a=adapter(monkeypatch,Client())
    o=a.get_crypto_order_truth(OID,asset=ASSET)
    p=a.get_crypto_position_truth(asset=ASSET)
    assert o['readable'] and o['found'] and o['order'].filled_quantity==Decimal('0.000040000000000001')
    assert p['readable'] and p['found'] and p['position'].whole_balance_available
    assert calls==[('order',OID),('position',ASSET['id'])]
    # Existing equity certification is deliberately not silently broadened.
    legacy=a._normalize_order(SimpleNamespace(**order()))
    assert not legacy.raw['fill_truth_readable'] and legacy.raw['filled_size'] is None


@pytest.mark.parametrize('status,readable,found',[(404,True,False),(429,False,None),(500,False,None),(None,False,None)])
def test_only_authenticated_endpoint_404_is_absence(monkeypatch,status,readable,found):
    class Client:
        def get_order_by_id(self,*args,**kwargs):
            error=RuntimeError('transport'); error.status_code=status; raise error
        get_open_position=get_order_by_id
    a=adapter(monkeypatch,Client())
    for result in [a.get_crypto_order_truth(OID,asset=ASSET),a.get_crypto_position_truth(asset=ASSET)]:
        assert result['readable'] is readable and result['found'] is found


@pytest.mark.parametrize('posture,bound',[(False,ACCOUNT),(True,None),(True,'wrong')])
def test_unbound_or_live_read_fails_before_transport(monkeypatch,posture,bound):
    a=adapter(monkeypatch,None); a._bound_account_id=bound
    monkeypatch.setattr(ap,'_paper',lambda:posture)
    monkeypatch.setattr(a,'_account_client',lambda:pytest.fail('wrong account reached transport'))
    assert not a.get_crypto_order_truth(OID,asset=ASSET)['readable']
    assert not a.get_crypto_position_truth(asset=ASSET)['readable']


def test_found_but_malformed_response_is_unknown_not_absence(monkeypatch):
    class Client:
        def get_order_by_id(self,*args,**kwargs): return {}
        get_open_position=get_order_by_id
    a=adapter(monkeypatch,Client())
    for result in [a.get_crypto_order_truth(OID,asset=ASSET),a.get_crypto_position_truth(asset=ASSET)]:
        assert not result['readable'] and result['found'] is True
