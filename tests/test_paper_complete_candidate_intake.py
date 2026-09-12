"""Persisted whole-symbol intake and bounded service across PAPER passes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
import threading

import pytest

from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability
from app.services.trading.momentum_neural import auto_arm as aa
from app.services.trading.momentum_neural.paper_probe_fairness import PaperProbeService, ProbeCapacityDeferred

AS_OF = datetime(2026, 9, 12, 12)


@pytest.fixture
def paper(monkeypatch):
    monkeypatch.setattr(aa.settings, 'chili_momentum_equity_execution_via_alpaca_paper', True)
    monkeypatch.setattr(aa, '_auto_arm_crypto_only', lambda: False)
    monkeypatch.setattr(aa, '_auto_arm_equity_only', lambda: True)
    monkeypatch.setattr(aa, '_ross_equity_universe_required', lambda: True)
    monkeypatch.setattr(aa, '_symbol_market_open', lambda _: True)
    monkeypatch.setattr(aa, '_filter_fresh_tape', lambda rows: rows)
    monkeypatch.setattr(aa.settings, 'chili_momentum_risk_viability_max_age_seconds', 600)
    def forbidden(*a, **kw):
        pytest.fail('Ross lookup/reranking must not determine PAPER intake')
    for name in ('_ross_snapshot_rows_by_symbol', '_ross_dash_mirror_symbols', '_velocity_qualified_symbols',
                 '_liquidity_rerank', '_crypto_liquidity_rerank', '_hoist_leader'):
        monkeypatch.setattr(aa, name, forbidden)


def variants(db, n=1):
    rows = [MomentumStrategyVariant(family='paper_intake_test', variant_key=str(i), label=str(i)) for i in range(n)]
    db.add_all(rows)
    db.flush()
    return rows


def row(db, variant, symbol, *, score=0.5, fresh=AS_OF, live=True, scope='symbol', ross=0):
    value = MomentumSymbolViability(symbol=symbol, variant_id=variant.id, scope=scope,
        viability_score=score, live_eligible=live, freshness_ts=fresh,
        execution_readiness_json={'extra': {'ross_scores': {symbol: ross}}})
    db.add(value)
    return value


def test_many_variants_and_scan_limit_do_not_hide_another_eligible_symbol(db, paper):
    vs = variants(db, 201)
    for v in vs:
        row(db, v, 'AAA', score=1, ross=1)
    row(db, vs[0], 'ZZZ', score=0.001, ross=0)
    db.flush()
    result = aa._fresh_live_eligible_candidates(db, limit=1, ross_universe_symbols=set(), as_of_utc=AS_OF)
    assert [r.symbol for r in result] == ['AAA', 'ZZZ']


def test_symbol_membership_is_invariant_to_ross_and_viability_ordering(db, paper):
    v = variants(db)[0]
    a, z = row(db, v, 'AAA', score=1, ross=1), row(db, v, 'ZZZ', score=0.01, ross=0)
    db.flush()
    before = aa._fresh_live_eligible_candidates(db, limit=1, ross_universe_symbols={'AAA'}, as_of_utc=AS_OF)
    a.viability_score, z.viability_score = 0.001, 100
    a.execution_readiness_json, z.execution_readiness_json = {}, {'extra': {'ross_scores': {'ZZZ': 100}}}
    db.flush()
    after = aa._fresh_live_eligible_candidates(db, limit=1, ross_universe_symbols={'ZZZ'}, as_of_utc=AS_OF)
    assert [r.symbol for r in before] == [r.symbol for r in after] == ['AAA', 'ZZZ']


def test_scope_row_eligibility_market_and_quote_readiness_still_apply(db, paper, monkeypatch):
    v = variants(db)[0]
    for symbol in ('READY', 'CLOSED', 'NOQUOTE', 'BTC-USD', 'BTC/USD', 'ODD-W'):
        row(db, v, symbol)
    row(db, v, 'STALE', fresh=AS_OF-timedelta(seconds=601))
    row(db, v, 'FUTURE', fresh=AS_OF+timedelta(microseconds=1))
    row(db, v, 'DISABLED', live=False)
    row(db, v, 'PORTFOLIO', scope='portfolio')
    db.flush()
    monkeypatch.setattr(aa, '_symbol_market_open', lambda s: s != 'CLOSED')
    monkeypatch.setattr(aa, '_filter_fresh_tape', lambda rows: [r for r in rows if r.symbol != 'NOQUOTE'])
    result = aa._fresh_live_eligible_candidates(db, limit=1, as_of_utc=AS_OF)
    assert [r.symbol for r in result] == ['ODD-W', 'READY']
    scoped = aa._fresh_live_eligible_candidates(db, limit=1, only_symbols=frozenset({'READY'}), as_of_utc=AS_OF)
    assert [r.symbol for r in scoped] == ['READY']
    assert aa._fresh_live_eligible_candidates(db, limit=1, only_symbols=frozenset(), as_of_utc=AS_OF) == []


def test_unstarted_queue_tail_gets_service_before_previously_started_names():
    service = PaperProbeService()
    candidates = [SimpleNamespace(symbol=s) for s in ('AAA', 'BBB', 'ZZZ')]
    assert service.call('AAA', capacity=1, probe=lambda: 'done') == 'done'
    assert [c.symbol for c in service.order(candidates)] == ['BBB', 'ZZZ', 'AAA']
    service.call('BBB', capacity=1, probe=lambda: 'done')
    assert [c.symbol for c in service.order(candidates)] == ['ZZZ', 'AAA', 'BBB']


def test_old_inflight_request_keeps_capacity_across_new_pass_and_does_not_mark_tail_observed():
    service, started, finish = PaperProbeService(), threading.Event(), threading.Event()
    def slow():
        started.set()
        assert finish.wait(5)
        return 'old_pass_result'
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.call, 'AAA', capacity=1, probe=slow)
        try:
            assert started.wait(5)
            for symbol in ('AAA', 'BBB'):
                with pytest.raises(ProbeCapacityDeferred):
                    service.call(symbol, capacity=1, probe=lambda: pytest.fail('must not execute'))
            assert [c.symbol for c in service.order([SimpleNamespace(symbol=s) for s in ('AAA', 'BBB')])] == ['BBB', 'AAA']
        finally:
            finish.set()
        assert future.result() == 'old_pass_result'
    assert service.call('BBB', capacity=1, probe=lambda: 'new_pass_result') == 'new_pass_result'


def test_failed_probe_releases_resource_without_erasing_service_history():
    service = PaperProbeService()
    with pytest.raises(RuntimeError):
        service.call('AAA', capacity=1, probe=lambda: (_ for _ in ()).throw(RuntimeError('provider failed')))
    assert service.call('BBB', capacity=1, probe=lambda: 'ok') == 'ok'
