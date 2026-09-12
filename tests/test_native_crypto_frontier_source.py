from dataclasses import replace
from itertools import product
import json
import pytest

from app.crypto_execution.frontier_source import plan_frontiers, collect_frontiers, validate_plan
from scripts.crypto_history_pages import canonical, sha
from tests.test_native_crypto_history_source import BASE, q, t

SYMBOLS=('BTC/USD','ETH/USD','SOL/BTC')
LIMITS=dict(max_pages=20,max_trades=20,max_quotes=20,max_page_bytes=65536)


def plan(histories=None, **changes):
    args=dict(histories=histories or {k:{s:[] for s in SYMBOLS} for k in ('trades','quotes')},
        location='us-1',symbols=SYMBOLS,anchor_ns=BASE,observed_through_ns=BASE+100,
        end_ns=BASE+200,page_limit=100,inventory_sha256='a'*64,prior_observation_sha256='b'*64)
    args.update(changes)
    return plan_frontiers(**args)


def fake_get(responses=None, *, fault=None):
    calls=[]
    def get(*,location,kind,parameters,record):
        calls.append((kind,parameters))
        value=responses.pop(0) if responses is not None else {kind:{},'next_page_token':None}
        raw=canonical(value).encode();received=BASE+1000+len(calls)
        ev=dict(kind='http_response',path='/v1beta3/crypto/'+location+'/'+kind,parameters=parameters,
            status=200,complete=True,body_hex=raw.hex(),body_sha256=sha(raw),received_ns=received,rate_headers={})
        if fault=='rate':ev['rate_headers']={'X-Ratelimit-Remaining':'0'}
        if fault=='path':ev['path']='/v1beta3/crypto/eu-1/'+kind
        if fault!='unrecorded':record(ev)
        if fault=='body':raw=b'{}'
        if fault=='clock':received+=1
        return raw,received
    return get,calls


def test_empty_and_non_usd_symbols_are_covered_on_both_channels():
    p=plan();get,calls=fake_get();log=[]
    batch=collect_frontiers(p,get,record=log.append,**LIMITS)
    assert len(calls)==2
    assert all(c[1]['symbols']==','.join(SYMBOLS) for c in calls)
    receipt=json.loads(batch.receipt_json)
    assert receipt['all_requested_channel_page_chains_exhausted']
    assert receipt['full_history_audit_required'] and not receipt['provider_finality_certified']
    assert not receipt['order_authority']
    assert log[0]['plan_sha256']==p.identity
    assert log[-1]['receipt_sha256']==batch.identity


def test_partition_has_exhaustively_minimal_defined_cost():
    histories={
        'BTC/USD':[BASE+i for i in range(1,7)],
        'ETH/USD':[BASE+i for i in (4,6,7,8)],
        'SOL/BTC':[]}
    p=plan({k:histories for k in ('trades','quotes')},page_limit=3)
    members=sorted(SYMBOLS,key=lambda s:(max(histories[s],default=BASE),s))
    scores=[]
    for cuts in product((False,True),repeat=len(members)-1):
        groups=[];start=0
        for i,cut in enumerate((*cuts,True)):
            if cut:groups.append(members[start:i+1]);start=i+1
        counts=[sum(sum(x>=max(histories[g[0]],default=BASE) for x in histories[s]) for s in g) for g in groups]
        scores.append((sum(max(1,(n+2)//3) for n in counts),sum(counts),len(groups)))
    groups=[g for g in p.groups if g.channel=='trades']
    assert (sum(g.estimated_pages for g in groups),sum(g.known_overlap_rows for g in groups),len(groups))==min(scores)
    assert any('SOL/BTC' in g.request.symbols and g.request.start_ns==BASE for g in groups)


def test_trade_and_quote_frontiers_are_independent_and_inclusive():
    h={k:{s:[] for s in SYMBOLS} for k in ('trades','quotes')}
    h['trades']['BTC/USD']=[BASE+1]*10+[BASE+50]*4
    h['quotes']['BTC/USD']=[BASE+1]*10+[BASE+90]*4
    p=plan(h,page_limit=1)
    trade=next(g for g in p.groups if g.channel=='trades' and 'BTC/USD' in g.request.symbols)
    quote=next(g for g in p.groups if g.channel=='quotes' and 'BTC/USD' in g.request.symbols)
    assert trade.request.start_ns==BASE+50 and quote.request.start_ns==BASE+90
    assert trade.known_overlap_rows==quote.known_overlap_rows==4


@pytest.mark.parametrize('bad',['missing','future','negative','boolean'])
def test_missing_inventory_or_unobserved_history_cannot_plan(bad):
    h={k:{s:[] for s in SYMBOLS} for k in ('trades','quotes')}
    if bad=='missing':del h['trades']['SOL/BTC']
    else:h['quotes']['BTC/USD']=[{'future':BASE+101,'negative':-1,'boolean':True}[bad]]
    with pytest.raises(ValueError):plan(h)


def test_audit_keeps_original_anchor_even_with_observed_frontiers():
    h={k:{s:[BASE+90] for s in SYMBOLS} for k in ('trades','quotes')}
    p=plan(h,mode='full_anchor_audit')
    assert len(p.groups)==2 and all(g.request.start_ns==BASE for g in p.groups)


@pytest.mark.parametrize('fault',['missing','duplicate','wrong_end'])
def test_forged_group_coverage_or_boundary_rejected_before_http(fault):
    p=plan()
    if fault=='missing':p=replace(p,groups=p.groups[:-1])
    elif fault=='duplicate':p=replace(p,groups=p.groups+(p.groups[0],))
    else:p=replace(p,groups=(replace(p.groups[0],request=replace(p.groups[0].request,end_ns=BASE+201)),p.groups[1]))
    get,calls=fake_get()
    with pytest.raises(ValueError):collect_frontiers(p,get,record=lambda _:None,**LIMITS)
    assert not calls


def test_short_empty_pages_continue_until_token_exhaustion_with_real_receipts():
    responses=[dict(trades={},next_page_token='next'),dict(trades={'BTC/USD':[t(90)]},next_page_token=None),
        dict(quotes={},next_page_token='qnext'),dict(quotes={'BTC/USD':[q(80)]},next_page_token=None)]
    get,calls=fake_get(responses);records=[]
    observation=collect_frontiers(plan(),get,record=records.append,**LIMITS)
    assert len(observation.trades)==len(observation.quotes)==1
    assert calls[1][1]['page_token']=='next' and calls[3][1]['page_token']=='qnext'
    assert json.loads(observation.receipt_json)['total_pages']==4
    assert [r['kind'] for r in records]==['frontier_observation_started']+['frontier_transport']*4+['frontier_observation_complete']


@pytest.mark.parametrize('fault',['unrecorded','body','clock','path','rate'])
def test_transport_or_durable_evidence_failure_never_completes(fault):
    get,calls=fake_get(fault=fault);log=[]
    with pytest.raises(ValueError):collect_frontiers(plan(),get,record=log.append,**LIMITS)
    assert len(calls)==1 and all(r['kind']!='frontier_observation_complete' for r in log)


def test_fsync_failure_stops_before_normalization_or_completion():
    get,calls=fake_get();records=[]
    def record(value):
        records.append(value)
        if value['kind']=='frontier_transport':raise OSError('fsync failed')
    with pytest.raises(OSError):collect_frontiers(plan(),get,record=record,**LIMITS)
    assert len(calls)==1 and records[-1]['kind']=='frontier_transport'


def test_global_page_limit_does_not_multiply_by_group_count():
    get,calls=fake_get();log=[]
    with pytest.raises(ValueError,match='total_page_capacity'):
        collect_frontiers(plan(),get,record=log.append,**dict(LIMITS,max_pages=1))
    assert len(calls)==1 and all(r['kind']!='frontier_observation_complete' for r in log)


def test_global_member_limit_accumulates_across_disjoint_groups():
    h={k:{s:[] for s in SYMBOLS} for k in ('trades','quotes')}
    h['trades']['BTC/USD']=[BASE+1]*10+[BASE+90]*4
    p=plan(h,page_limit=1)
    responses=[]
    for g in p.groups:
        symbol=g.request.symbols[0]
        responses.append({g.channel:{symbol:[t(100)] if g.channel=='trades' else []},'next_page_token':None})
    get,calls=fake_get(responses);log=[]
    with pytest.raises(ValueError,match='total_member_capacity'):
        collect_frontiers(p,get,record=log.append,**dict(LIMITS,max_trades=1))
    assert len(calls)==2 and all(r['kind']!='frontier_observation_complete' for r in log)


def test_mutating_http_receipt_after_durable_write_does_not_rebind_returned_body():
    def get(*,location,kind,parameters,record):
        raw=canonical({kind:{},'next_page_token':None}).encode();received=BASE+1000
        ev=dict(kind='http_response',path='/v1beta3/crypto/'+location+'/'+kind,parameters=parameters,
            status=200,complete=True,body_hex=raw.hex(),body_sha256=sha(raw),received_ns=received,rate_headers={})
        record(ev)
        raw=canonical({kind:{'BTC/USD':[t(90)]},'next_page_token':None}).encode()
        ev.update(body_hex=raw.hex(),body_sha256=sha(raw))
        return raw,received
    with pytest.raises(ValueError,match='returned_response_changed'):
        collect_frontiers(plan(),get,record=lambda _:None,**LIMITS)


def test_frontier_query_cannot_be_relabeled_as_full_history_audit():
    h={k:{s:[BASE+90] for s in SYMBOLS} for k in ('trades','quotes')}
    p=replace(plan(h),mode='full_anchor_audit')
    with pytest.raises(ValueError,match='audit_anchor_required'):validate_plan(p)


def test_transport_cannot_mutate_the_requested_symbol_coverage():
    def get(*,location,kind,parameters,record):
        parameters['symbols']='BTC/USD'
        raw=canonical({kind:{},'next_page_token':None}).encode();received=BASE+1000
        record(dict(kind='http_response',path='/v1beta3/crypto/'+location+'/'+kind,parameters=parameters,
            status=200,complete=True,body_hex=raw.hex(),body_sha256=sha(raw),received_ns=received,rate_headers={}))
        return raw,received
    with pytest.raises(ValueError,match='transport_binding_changed'):
        collect_frontiers(plan(),get,record=lambda _:None,**LIMITS)
