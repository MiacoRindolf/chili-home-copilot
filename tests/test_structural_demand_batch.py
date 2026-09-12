"""Atomic multi-source demand, including newly observed partial exposure."""
import pytest

from scripts.iqfeed_print_publications import Cursor
from app.services.trading.momentum_neural import ordinary_structural_context as context
from app.services.trading.momentum_neural.structural_tape_prefix import Limits


def update(revision, symbols, complete=True):
    return dict(revision=revision, symbols=symbols, complete=complete)


def owner(capacity=10):
    return context.OrdinaryStructuralContextOwner(anchor=Cursor('batch-source', 0),
        limits=Limits(100, 100, 100), max_symbols=capacity, max_trade_rows=100)


def reasons(snapshot):
    return {view.symbol: view.demand_reasons for view in snapshot.symbols}


def test_pending_to_held_transfer_publishes_one_shared_view():
    o = owner()
    o.update_demand('pending', revision=1, symbols=['A'])
    emissions = []
    o.bind_publication_sink(emissions.append)
    before = o.read('entry')
    o.update_demands({'pending': update(2, []), 'held': update(1, ['A']),
                      'inventory': update(1, ['A', 'B'])})
    assert len(emissions) == 2  # initial binding plus exactly one batch
    after = o.read('selection')
    assert after is o.read('entry') is o.read('exit') is emissions[-1]
    assert reasons(after) == {'A': ('held', 'inventory'), 'B': ('inventory',)}
    assert reasons(before) == {'A': ('pending',)}
    assert after.source == before.source  # demand does not invent a print release


def test_partial_observation_adds_new_symbols_without_releasing_prior():
    o = owner()
    o.update_demands({'held': update(1, ['A']), 'pending': update(1, ['B'])})
    o.update_demands({'held': update(2, ['C'], False), 'pending': update(2, None, False)})
    assert reasons(o.read('exit')) == {'A': ('held',), 'B': ('pending',), 'C': ('held',)}
    assert o.read('exit').stale_demand_sources == ('held', 'pending')
    o.update_demands({'held': update(3, ['C']), 'pending': update(3, [])})
    assert reasons(o.read('exit')) == {'A': (), 'B': (), 'C': ('held',)}
    assert o.read('exit').stale_demand_sources == ()


@pytest.mark.parametrize('bad', [update(1, ['C']), update(2, ['BTC-USD']),
                                     update(2, None), update(2, ['C'], 1)])
def test_invalid_sibling_cannot_consume_any_source_revision(bad):
    o = owner()
    o.update_demand('pending', revision=1, symbols=['A'])
    before = o.read('audit')
    with pytest.raises(ValueError):
        o.update_demands({'held': update(1, ['B']), 'pending': bad})
    assert o.read('audit') is before
    o.update_demands({'held': update(1, ['B']), 'pending': update(2, ['A'])})
    assert reasons(o.read('audit')) == {'A': ('pending',), 'B': ('held',)}


def test_capacity_is_checked_for_whole_union_before_any_mutation():
    o = owner(capacity=2)
    before = o.read('audit')
    with pytest.raises(ValueError, match='resource_capacity'):
        o.update_demands({'held': update(1, ['A', 'B']), 'pending': update(1, ['C'])})
    assert o.read('audit') is before
    # Shared membership counts once and refused revisions remain usable.
    o.update_demands({'held': update(1, ['A', 'B']), 'pending': update(1, ['B'])})
    assert reasons(o.read('audit')) == {'A': ('held',), 'B': ('held', 'pending')}


def test_prepublication_failure_fences_owner_without_exposing_private_batch(monkeypatch):
    o = owner()
    o.update_demand('held', revision=1, symbols=['A'])
    before = o.read('exit')
    def fail(*args, **kwargs):
        raise MemoryError('view construction failed')
    monkeypatch.setattr(o, '_publish', fail)
    with pytest.raises(MemoryError):
        o.update_demands({'held': update(2, ['B']), 'pending': update(1, ['C'])})
    failed = o.read('exit')
    assert failed.symbols == before.symbols
    assert failed.status == 'unresolved' and failed.reason == 'context_demand_commit_failed'
    with pytest.raises(RuntimeError, match='reconstruction_required'):
        o.update_demand('inventory', revision=1, symbols=['D'])
