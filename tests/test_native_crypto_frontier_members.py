from dataclasses import replace
import json
import pytest

from app.crypto_execution.frontier_members import seed_members, merge_members
from app.crypto_execution.frontier_source import plan_frontiers, collect_frontiers
from scripts.crypto_history_pages import HistoryRequest
from scripts.crypto_trade_frames import timestamp_ns
from tests.test_native_crypto_history_source import BASE, make_batch, q, t
from tests.test_native_crypto_frontier_source import SYMBOLS, LIMITS, fake_get


def seed():
    request=HistoryRequest('us-1',SYMBOLS,BASE,BASE+100,100,'a'*64)
    batch,_=make_batch(request,trades=[t(90)],quotes=[q(80)])
    return seed_members(batch)


def observe(state,*,trades,quotes,mode='observed_frontiers',end=200):
    plan=plan_frontiers(histories=state.histories(),location=state.location,symbols=state.symbols,
        anchor_ns=state.anchor_ns,observed_through_ns=state.through_ns,end_ns=BASE+end,
        page_limit=100,inventory_sha256=state.inventory_sha256,prior_observation_sha256=state.identity,mode=mode)
    responses=[]
    for group in plan.groups:
        rows=trades if group.channel=='trades' else quotes
        selected=[r for r in rows if group.request.start_ns<=timestamp_ns(r['t'])<=group.request.end_ns]
        members={'BTC/USD':selected} if 'BTC/USD' in group.request.symbols else {}
        responses.append({group.channel:members,'next_page_token':None})
    get,_=fake_get(responses)
    return collect_frontiers(plan,get,record=lambda _:None,**LIMITS)


def merge(state,observation,**changes):
    limits=dict(max_trades=100,max_quotes=100);limits.update(changes)
    return merge_members(state,observation,**limits)


def test_delta_deduplicates_members_and_preserves_original_views():
    before=seed();old_id=before.identity
    observation=observe(before,trades=[t(90),t(150,tid=2)],quotes=[q(80),q(140)])
    result=merge(before,observation)
    assert len(result.new_trades)==len(result.new_quotes)==1
    assert not result.reconstruction_symbols
    assert len(result.state.trades)==len(result.state.quotes)==2
    assert before.identity==old_id and len(before.trades)==1
    assert result.state.ancestors==(old_id,)


def test_full_audit_discovers_old_trade_without_rewriting_the_old_observation():
    before=seed()
    audit=observe(before,trades=[t(85,tid=2),t(90)],quotes=[q(80)],mode='full_anchor_audit')
    result=merge(before,audit)
    assert result.reconstruction_symbols==('BTC/USD',)
    assert [t.trade_id for t in before.trades]==[1]
    assert [t.trade_id for t in result.new_trades]==[2]


@pytest.mark.parametrize('event,rebuild',[(85,True),(90,False)])
def test_late_quote_rebuild_uses_strict_event_before_trade_semantics(event,rebuild):
    before=seed()
    audit=observe(before,trades=[t(90)],quotes=[q(80),q(event,'8','12')],mode='full_anchor_audit')
    result=merge(before,audit)
    assert bool(result.reconstruction_symbols)==rebuild


def test_equal_time_new_trade_requires_reconstruction_of_current_order():
    before=seed()
    result=merge(before,observe(before,trades=[t(90),t(90,tid=2)],quotes=[q(80)]))
    assert result.reconstruction_symbols==('BTC/USD',)


def test_slow_old_audit_keeps_newer_ticks_and_monotonic_boundary():
    before=seed()
    audit=observe(before,trades=[t(90),t(150,tid=2)],quotes=[q(80),q(140)],mode='full_anchor_audit')
    fast=observe(before,trades=[t(90),t(250,tid=3)],quotes=[q(80),q(240)],end=300)
    current=merge(before,fast).state
    result=merge(current,audit)
    assert result.older_audit_merged and result.state.through_ns==BASE+300
    assert {t.trade_id for t in result.state.trades}=={1,2,3}
    assert {q.event_ns for q in result.state.quotes}=={BASE+80,BASE+140,BASE+240}
    assert result.reconstruction_symbols==('BTC/USD',)


def test_stale_fast_plan_cannot_overwrite_newer_state():
    before=seed()
    fast=observe(before,trades=[t(90),t(150,tid=2)],quotes=[q(80),q(140)])
    current=merge(before,fast).state
    with pytest.raises(ValueError,match='stale_or_unrelated_plan'):merge(current,fast)


@pytest.mark.parametrize('fault',['missing_trade','missing_quote','changed_trade'])
def test_provider_conflicts_keep_current_state_and_return_no_partial_merge(fault):
    before=seed();identity=before.identity
    trades=[] if fault=='missing_trade' else [t(90,'12' if fault=='changed_trade' else '10')]
    quotes=[] if fault=='missing_quote' else [q(80)]
    audit=observe(before,trades=trades,quotes=quotes,mode='full_anchor_audit')
    with pytest.raises(ValueError,match='observed_.*_missing|identity_conflict'):merge(before,audit)
    assert before.identity==identity and before.through_ns==BASE+100


def test_retained_capacity_failure_does_not_truncate_old_or_new_symbols():
    before=seed()
    observation=observe(before,trades=[t(90),t(150,tid=2)],quotes=[q(80)])
    with pytest.raises(ValueError,match='retained_capacity'):merge(before,observation,max_trades=1)
    assert before.symbols==SYMBOLS and len(before.trades)==1


def test_mutated_member_data_cannot_keep_its_old_response_receipt():
    before=seed()
    observation=observe(before,trades=[t(90)],quotes=[q(80)])
    changed=replace(observation,trades=())
    with pytest.raises(ValueError,match='observation_binding_changed'):merge(before,changed)


def test_unrelated_audit_and_asset_universe_do_not_merge():
    before=seed()
    observation=observe(before,trades=[t(90)],quotes=[q(80)],mode='full_anchor_audit')
    changed=replace(observation,plan=replace(observation.plan,prior_observation_sha256='f'*64))
    with pytest.raises(ValueError,match='stale_or_unrelated_plan'):merge(before,changed)
    different=replace(before,inventory_sha256='f'*64)
    with pytest.raises(ValueError,match='source_changed'):merge(different,observation)
