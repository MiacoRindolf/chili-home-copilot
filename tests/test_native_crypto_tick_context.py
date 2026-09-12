"""Real decoded crypto frames share causal wave math and retain exact taker flow."""
from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import pytest

from app.crypto_execution.tick_context import CryptoTickContext
from app.services.trading.momentum_neural.structural_tape_prefix import Limits
from scripts.crypto_trade_frames import timestamp_ns

BASE=timestamp_ns('2026-09-12T00:00:00Z')
SYMBOLS=('BTC/USD','ETH/USD')
def book(*,symbols=SYMBOLS,retained=100,quotes=100):
    return CryptoTickContext(assets=[dict(id=str(uuid4()),symbol=s,**{'class':'crypto'},status='active',tradable=True) for s in symbols],
        location='us',connection_id='retained-test-connection',limits=Limits(retained,100,100),
        max_frame_bytes=65536,max_pending_quotes=quotes)
def stamp(i):return f'2026-09-12T00:00:00.{i:09d}Z'
def quote(i,bid,ask,*,symbol='BTC/USD',size='1'):
    return dict(T='q',S=symbol,t=stamp(i),bp=str(bid),ap=str(ask),bs=size,**{'as':size})
def trade(i,price,*,symbol='BTC/USD',tid=None,side='B',size='0.00000000000000000001'):
    return dict(T='t',S=symbol,t=stamp(i),i=i if tid is None else tid,p=str(price),s=size,tks=side)
def add(b,*rows):
    n=b.frame_sequence+1
    return b.append(json.dumps(list(rows)),frame_sequence=n,received_ns=BASE+1000*n,published_ns=BASE+1000*n+1)
def ack(b):return add(b,dict(T='subscription',trades=list(b.assets),quotes=list(b.assets)))

def test_all_symbols_visible_without_ranking_and_no_data_does_not_mean_no_opportunity():
    b=book();assert b.view('BTC/USD').coverage=='awaiting_subscription'
    ack(b);add(b,quote(1,9,11),trade(2,10))
    btc,eth=b.view('BTC/USD'),b.view('ETH/USD')
    assert btc.print_count==1 and eth.print_count==0
    assert btc.coverage=='observed_connection_prefix' and eth.coverage=='cold_no_observed_prints'
    assert btc.history_before_connection==eth.history_before_connection=='unknown'
    assert btc.source_sequence==eth.source_sequence==3
    assert btc.order_authority is eth.order_authority is False

def test_quote_join_obeys_both_event_time_and_actual_member_arrival():
    b=book();ack(b)
    add(b,quote(10,9,11),quote(30,29,31),trade(20,10),trade(25,12),quote(25,11,13))
    p=b.prefixes['BTC/USD']
    assert [p.tick(i).bid for i in range(2)]==[D(9),D(9)]
    # The quote received after the second print does not rewrite that print.
    add(b,trade(26,12))
    assert p.tick(2).bid==11 and p.tick(1).bid==9
    assert b.view('BTC/USD').last_binding.quote_event_ns==BASE+25
    assert b.view('BTC/USD').current_quote.event_ns==BASE+30

def test_invalid_latest_quote_does_not_fall_back_to_older_valid_quote():
    b=book();ack(b);add(b,quote(1,9,11),quote(2,12,10),trade(3,10))
    view=b.view('BTC/USD')
    assert view.last_print.bid is None and view.last_print.ask is None
    assert view.last_binding.quote_basis=='invalid_asof_quote'

def test_reported_taker_flow_is_distinct_from_quote_inference_and_exact():
    b=book();ack(b)
    for i,price in enumerate([10,15,12,13,14],1):
        add(b,quote(i,D(price)-D('.1'),D(price)+D('.1')),trade(i,price,side='S' if i in (4,5) else 'B'))
    view=b.view('BTC/USD')
    assert view.wave.local_phase.reason=='recovery_after_confirmed_valley'
    valley=next(f for f in view.reported_flow if f.basis=='quote_resolved' and f.kind=='valley')
    assert valley.formation==(0,F('0.00000000000000000001'),0)
    assert valley.follow_through==(0,F('0.00000000000000000001'),0)
    assert valley.follow_net_bounds==(-F('0.00000000000000000001'),)*2
    # Midpoint quote mass is unresolved, while provider-reported sells are known.
    q=next(v for v in view.quote_inferred_evidence if v.reference.kind=='valley' and v.reference.basis=='quote_resolved')
    assert q.follow_through.quote_mass==(0,0,F('0.00000000000000000001'))
    assert type(view.last_print.price) is D

def test_provider_trade_ids_need_not_increase_but_duplicates_never_double_volume():
    b=book();ack(b)
    first=trade(1,10,tid=100);add(b,first)
    result=add(b,first,trade(2,11,tid=3))
    assert result['duplicates'][0][:2]==('BTC/USD',100)
    assert b.view('BTC/USD').print_count==2
    assert b.view('BTC/USD').last_binding.provider_trade_id==3
    root=b.root
    with pytest.raises(ValueError,match='conflicting_trade_identity'):add(b,trade(1,12,tid=100))
    assert b.root==root and b.view('BTC/USD').coverage.startswith('unavailable:')

@pytest.mark.parametrize('fault',['late_trade','membership','preack','capacity'])
def test_whole_frame_failure_never_commits_other_symbols_or_reopens_old_context(fault):
    b=book(retained=1 if fault=='capacity' else 100)
    if fault!='preack':ack(b);add(b,trade(20,10))
    before={s:(p.count,p.prefix_sha256) for s,p in b.prefixes.items()}
    if fault=='membership':rows=[dict(T='subscription',trades=['BTC/USD'],quotes=['BTC/USD'])]
    else:rows=[trade(30,10,symbol='ETH/USD'),trade(10 if fault=='late_trade' else 30,11)]
    with pytest.raises(ValueError):add(b,*rows)
    assert before=={s:(p.count,p.prefix_sha256) for s,p in b.prefixes.items()}
    with pytest.raises(ValueError,match='segment_unavailable'):add(b,trade(40,12))

def test_frame_prefix_replay_and_batching_preserve_causal_geometry_and_flow():
    one=book(symbols=('BTC/USD',));many=book(symbols=('BTC/USD',));ack(one);ack(many)
    records=[]
    for i,price in enumerate([10,15,12,14,11,13,10,12,13],1):
        rows=[quote(i,D(price)-D('.1'),D(price)+D('.1')),trade(i,price,side=None if i%2 else 'B')]
        records.extend(rows);add(one,*rows)
    add(many,*records)
    a,b=one.view('BTC/USD'),many.view('BTC/USD')
    # Source member IDs agree; batching changes publication roots and knowledge clocks.
    assert replace(a.wave,events=())==replace(b.wave,events=())
    assert a.reported_flow==b.reported_flow
    assert a.prefix_sha256!=b.prefix_sha256

def test_subscription_ack_cannot_retroactively_authorize_prior_member():
    b=book()
    with pytest.raises(ValueError,match='before_subscription_ack'):
        add(b,trade(1,10),dict(T='subscription',trades=list(b.assets),quotes=list(b.assets)))
    assert b.view('BTC/USD').print_count==0

def test_immutable_view_does_not_change_when_quote_only_frame_advances_context():
    b=book();ack(b);add(b,quote(1,9,11),trade(2,10));old=b.view('BTC/USD')
    add(b,quote(3,10,12));new=b.view('BTC/USD')
    assert old.current_quote.bid==9 and new.current_quote.bid==10
    assert old.print_count==new.print_count==1 and old.wave.end_id==new.wave.end_id
    assert old.source_root_sha256!=new.source_root_sha256

def test_native_math_import_does_not_load_database_or_trading_startup(tmp_path):
    env=os.environ.copy()
    for key in ('DATABASE_URL','TEST_DATABASE_URL','CHILI_PYTEST'):env.pop(key,None)
    env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
    code='import sys; from app.crypto_execution.tick_context import CryptoTickContext; assert "app.db" not in sys.modules; assert "app.config" not in sys.modules'
    result=subprocess.run([sys.executable,'-B','-c',code],cwd=tmp_path,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
