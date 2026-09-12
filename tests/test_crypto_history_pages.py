"""Whole-universe pagination, exact ticks and incomplete/corrupt source boundaries."""
from dataclasses import asdict, replace
from decimal import Decimal
import json

import pytest

from scripts.crypto_history_pages import HistoryRequest, HistoryWalk, decode_page, format_ns
from scripts.crypto_trade_frames import timestamp_ns

START=timestamp_ns('2026-09-12T12:00:00.000000001Z')
END=timestamp_ns('2026-09-12T12:00:00.999999999Z')


def request(**kwargs):
    return HistoryRequest(**dict(dict(location='us',symbols=('A/USD','B/USDT','C/BTC'),
        start_ns=START,end_ns=END,page_limit=2,inventory_sha256='a'*64),**kwargs))


def trade(i=1, ns=START, **kwargs):
    return dict(dict(i=i,p='1.123456789123456789',s='0.000000001',t=format_ns(ns),tks='B'),**kwargs)


def page(rows=None, token=None):
    return json.dumps({'trades':rows or {},'next_page_token':token})


def walk(**kwargs):
    return HistoryWalk(request(),**dict(dict(max_pages=10,max_trades=20,max_page_bytes=10000),**kwargs))


def test_all_symbols_survive_short_empty_and_paginated_alphabetical_results():
    w=walk()
    queries=[w.parameters()]
    w.accept(page({'A/USD':[trade()]},'next-a'),received_ns=END+1)
    with pytest.raises(ValueError,match='incomplete'): w.complete_trades()
    queries.append(w.parameters())
    w.accept(page({},'next-b'),received_ns=END+2)
    queries.append(w.parameters())
    w.accept(page({'B/USDT':[trade(2,END)]}),received_ns=END+3)
    result=w.receipt()
    assert result['provider_page_chain_exhausted']
    assert result['trade_counts']=={'A/USD':1,'B/USDT':1,'C/BTC':0}
    assert not result['provider_event_time_finality_certified'] and not result['momentum_eligibility']
    for q in queries:
        assert q['symbols']=='A/USD,B/USDT,C/BTC' and q['start']==format_ns(START) and q['end']==format_ns(END)
    assert queries[1]['page_token']=='next-a' and queries[2]['page_token']=='next-b'


def test_exact_id_price_side_nanoseconds_and_inclusive_bounds():
    raw=page({'A/USD':[trade(9223372036854775000,START),trade(0,END,tks=None)]})
    raw=raw.replace('"1.123456789123456789"','1.123456789123456789')
    p=decode_page(raw,request=request(),request_token=None,received_ns=END+1,max_bytes=10000)
    assert p.trades[0].price==Decimal('1.123456789123456789')
    assert p.trades[0].trade_id==9223372036854775000 and p.trades[0].event_ns==START
    assert p.trades[1].event_ns==END and p.trades[1].side_basis=='unknown'
    assert p.trades[1].aggressor_sign==0
    assert timestamp_ns(format_ns(START))==START and timestamp_ns(format_ns(END))==END


@pytest.mark.parametrize('kwargs',[{'page_limit':0},{'page_limit':True},{'page_limit':10001},
    {'symbols':('B/USDT','A/USD')},{'symbols':('A/USD','A/USD')},{'symbols':('A-USD',)},
    {'start_ns':True},{'end_ns':START-1},{'inventory_sha256':'wrong'},{'location':'unlabelled'}])
def test_request_requires_exact_source_inventory_and_documented_transport_bounds(kwargs):
    with pytest.raises(ValueError): request(**kwargs)


@pytest.mark.parametrize('raw',[
    '{"trades":{}}', '{"trades":{},"next_page_token":""}',
    '{"trades":null,"next_page_token":null}',
    '{"trades":{},"trades":{},"next_page_token":null}',
    page({'UNKNOWN/USD':[trade()]}), page({'A/USD':[trade(ns=START-1)]}),
    page({'A/USD':[trade(ns=END+1)]}), page({'A/USD':[trade(i=True)]}),
    page({'A/USD':[trade(p='NaN')]}), page({'A/USD':[trade(tks='SELL')]}),
    page({'A/USD':[trade(S='B/USDT')]}), page({'A/USD':[trade(T='b')]}),
    page({'A/USD':[trade(1),trade(2),trade(3)]}),
])
def test_invalid_member_rejects_entire_page_and_preserves_frontier(raw):
    w=walk()
    before=w.receipt()
    with pytest.raises(ValueError): w.accept(raw,received_ns=END+1)
    assert w.receipt()==before


def test_nonmonotonic_ids_are_not_mistaken_for_late_trades():
    w=walk()
    w.accept(page({'A/USD':[trade(999)]},'a'),received_ns=END+1)
    w.accept(page({'A/USD':[trade(1,END)]}),received_ns=END+2)
    assert [t.trade_id for t in w.complete_trades()]==[999,1]


def test_exact_duplicate_is_counted_without_double_counting_volume_and_equal_time_order_is_uncertified():
    w=walk()
    w.accept(page({'A/USD':[trade(1)]},'a'),received_ns=END+1)
    w.accept(page({'A/USD':[trade(1),trade(2)]}),received_ns=END+2)
    r=w.receipt()
    assert r['unique_trades']==2 and r['duplicate_occurrences']==1
    assert r['equal_timestamp_occurrences']==1 and not r['source_order_at_equal_timestamp_certified']
    assert sum(t.size for t in w.complete_trades())==Decimal('0.000000002')


@pytest.mark.parametrize('case',['conflict','regression','loop','bytes','trades','clock'])
def test_cross_page_failure_is_atomic(case):
    w=walk(max_trades=1 if case=='trades' else 20)
    w.accept(page({'A/USD':[trade(1,START+1)]},'a'),received_ns=END+2)
    raw=page({'A/USD':[trade(2,END)]})
    clock=END+3
    if case=='conflict': raw=page({'A/USD':[trade(1,END)]})
    elif case=='regression': raw=page({'A/USD':[trade(2,START)]})
    elif case=='loop': raw=page({},'a')
    elif case=='bytes': raw='x'*10001
    elif case=='clock': clock=END+1
    before=w.receipt()
    with pytest.raises(ValueError): w.accept(raw,received_ns=clock)
    assert w.receipt()==before


def test_multi_page_token_cycle_and_resource_limit_do_not_certify_exhaustion():
    w=walk(max_pages=2)
    w.accept(page({},'a'),received_ns=END+1)
    w.accept(page({},'b'),received_ns=END+2)
    with pytest.raises(ValueError,match='page_capacity'): w.parameters()
    assert not w.receipt()['provider_page_chain_exhausted']
    other=walk()
    other.accept(page({},'a'),received_ns=END+1)
    other.accept(page({},'b'),received_ns=END+2)
    with pytest.raises(ValueError,match='pagination_cycle'):
        other.accept(page({},'a'),received_ns=END+3)


def test_restore_replays_raw_pages_to_the_same_root_then_resumes_the_exact_token():
    rows=[(page({'A/USD':[trade(1)]},'a'),END+1),(page({},'b'),END+2)]
    original=walk(); restored=walk()
    for raw,ns in rows:
        original.accept(raw,received_ns=ns)
        restored.accept(raw,received_ns=ns)
    assert restored.receipt()==original.receipt() and restored.parameters()['page_token']=='b'
    restored.accept(page({'C/BTC':[trade(3,END)]}),received_ns=END+3)
    assert restored.receipt()['trade_counts']['C/BTC']==1
    with pytest.raises(ValueError,match='already_complete'): restored.parameters()
