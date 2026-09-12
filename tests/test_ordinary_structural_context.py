"""Actual commit journal -> one reducer owner -> immutable consumer views."""
from dataclasses import FrozenInstanceError
from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from tests.test_iqfeed_print_publications import source, NOW
from scripts import bench_iqfeed_tape_write_path as bench
from scripts import iqfeed_trade_bridge as bridge
from scripts import iqfeed_print_publications as journal
from app.services.trading.momentum_neural.ordinary_structural_context import (
    OrdinaryStructuralContextOwner,
)
from app.services.trading.momentum_neural.structural_tape_prefix import Limits


RUN = str(uuid4())
RELEASE = NOW + timedelta(seconds=1)


def packet(engine, entries, *, start=1, run=RUN, invalid=None):
    rows = []
    for offset, (symbol, price) in enumerate(entries):
        seq = start + offset
        r = bench._rows(1, [symbol], seq, run, 1, NOW + timedelta(microseconds=seq))[0]
        r.update(px=price, sz=1, bid=price-.01, ask=price+.01,
                 basis="iqfeed_selected_trade_date_timems_exact", message_type="Q", provider_delay_minutes=0)
        if invalid and offset == len(entries)-1:
            r.update(invalid)
        rows.append(r)
    with engine.begin() as c:
        ids, _ = bridge._insert_pending_batch(c, trade_rows=rows, quote_rows=[], return_row_ids=True)
    return rows, ids


def publish(engine, value):
    rows, ids = value
    with engine.begin() as c:
        bridge._release_pending_batch(c, trade_rows=rows, quote_rows=[], available_at=RELEASE,
                                      trade_row_ids=ids)


def owner(engine, *, retained=100, max_rows=100, max_symbols=5):
    publish(engine, packet(engine, [('SEED', 1)]))
    return OrdinaryStructuralContextOwner.cold_start(engine,
        limits=Limits(retained, 100, 100), max_symbols=max_symbols, max_trade_rows=max_rows,
        clock_ns=lambda: int((NOW + timedelta(seconds=2)).timestamp()) * 10**9)


def counts(snapshot): return {v.symbol: v.print_count for v in snapshot.symbols}


def test_actual_publication_has_one_shared_view_and_intermediate_events(source):
    o = owner(source)
    o.update_demand('ranking', revision=1, symbols=['A', 'B'])
    anchor = o.read('selection').source
    publish(source, packet(source, [('A', 10), ('A', 12), ('A', 11), ('A', 13),
                                    ('B', 20), ('B', 19), ('B', 21)], start=2))
    snap = o.advance_one(source)
    assert snap.status == 'observed_prefix' and counts(snap) == {'A': 4, 'B': 3}
    assert snap is o.read('selection', after=anchor) is o.read('entry') is o.read('exit')
    assert snap.source.revision == anchor.revision + 1
    a = snap.symbols[0]
    assert [(e.kind, e.reference.kind) for e in a.events] == [
        ('born', 'peak'), ('breached', 'peak'), ('born', 'valley')]
    assert a.scopes and a.selected_parent_local == 'candidate_geometry'
    assert a.wave_context is not None and not a.wave_context.order_authority
    assert a.wave_evidence and all(not e.order_authority for e in a.wave_evidence)
    assert not snap.order_authority and not snap.provider_completeness_certified
    with pytest.raises(FrozenInstanceError): a.print_count = 100
    with pytest.raises(TypeError): a.scopes[0].phase_bounds[0] = (0, 100)
    # Changes in rank/consumer reads neither reset context nor erase transitions.
    o.update_demand('held', revision=1, symbols=['A'])
    o.update_demand('pending', revision=1, symbols=['B'])
    o.update_demand('ranking', revision=2, symbols=[])
    view = o.read('exit')
    assert view.symbols[0].events == a.events
    assert view.symbols[0].wave_evidence is a.wave_evidence
    assert view.symbols[0].demand_reasons == ('held',)
    assert view.symbols[1].demand_reasons == ('pending',)
    publish(source, packet(source, [('A', 12), ('B', 22)], start=9))
    assert counts(o.advance_one(source)) == {'A': 5, 'B': 4}
    with pytest.raises(ValueError, match='consumer_gap'):
        o.read('entry', after=anchor)
    assert counts(snap) == {'A': 4, 'B': 3}  # Old decision view never mutates.


def test_invalid_sibling_leaves_every_reducer_and_source_cursor_unchanged(source):
    o = owner(source)
    o.update_demand('inventory', revision=1, symbols=['A', 'B'])
    before = o.read('audit')
    publish(source, packet(source, [('A', 10), ('B', 20)], start=2,
                           invalid={'provider_delay_minutes': None}))
    snap = o.advance_one(source)
    assert snap.status == 'unresolved' and snap.reason == 'ordinary_provider_delay_unresolved'
    assert snap.source == before.source and snap.root_sha256 == before.root_sha256
    assert counts(snap) == {'A': 0, 'B': 0}
    assert snap.observed_frontier.revision == before.source.revision+1


def test_resource_failure_after_valid_sibling_staging_does_not_partially_commit(source):
    o = owner(source, retained=2)
    o.update_demand('watch', revision=1, symbols=['A', 'B'])
    publish(source, packet(source, [('A', 10), ('B', 20), ('B', 21)], start=2))
    before = o.advance_one(source)
    publish(source, packet(source, [('A', 11), ('B', 22)], start=5))
    after = o.advance_one(source)
    assert after.reason == 'resource_capacity_unresolved'
    assert after.source == before.source and after.root_sha256 == before.root_sha256
    assert counts(after) == counts(before) == {'A': 1, 'B': 2}
    assert [v.prefix_sha256 for v in after.symbols] == [v.prefix_sha256 for v in before.symbols]


def test_demand_failure_is_not_empty_and_enrollment_never_silently_evicts(source):
    o = owner(source, max_symbols=2)
    o.update_demand('held', revision=1, symbols=['A'])
    o.update_demand('ranking', revision=1, symbols=['A', 'B'])
    o.update_demand('held', revision=2, symbols=None)
    o.update_demand('ranking', revision=2, symbols=[])
    snap = o.read('selection')
    assert snap.stale_demand_sources == ('held',)
    assert snap.symbols[0].demand_reasons == ('held',) and snap.symbols[1].demand_reasons == ()
    with pytest.raises(ValueError, match='resource_capacity'):
        o.update_demand('pending', revision=1, symbols=['C'])
    assert o.read('exit') is snap
    with pytest.raises(ValueError, match='stale_demand'):
        o.update_demand('held', revision=2, symbols=[])
    o.update_demand('held', revision=3, symbols=[])
    assert not o.read('audit').stale_demand_sources
    assert len(o.read('audit').symbols) == 2


@pytest.mark.parametrize('kind', ['epoch', 'frame', 'clock', 'basis', 'digest'])
def test_epoch_late_order_bad_clock_and_quote_proxy_are_explicit_gaps(source, kind):
    o = owner(source)
    o.update_demand('held', revision=1, symbols=['A'])
    publish(source, packet(source, [('A', 10)], start=10))
    before = o.advance_one(source)
    kwargs = {'start': 11}
    if kind == 'epoch': kwargs['run'] = str(uuid4())
    if kind == 'frame': kwargs['start'] = 9
    if kind == 'clock': kwargs['invalid'] = {'received_at': RELEASE + timedelta(seconds=1)}
    if kind == 'basis': kwargs['invalid'] = {'basis': 'iqfeed_q_receive_trade_reference_fenced'}
    value = packet(source, [('A', 11)], **kwargs)
    publish(source, value)
    if kind == 'digest':
        # The bridge correctly rejects this before insert. Simulate later
        # retained-row corruption only in the isolated test schema.
        with source.begin() as c:
            c.execute(sa.text('UPDATE iqfeed_trade_ticks SET source_frame_sha256=NULL WHERE id=:id'),
                      {'id': value[1][0]})
    after = o.advance_one(source)
    assert after.status == 'unresolved'
    assert after.source == before.source and counts(after) == {'A': 1}


def test_multi_symbol_reader_never_splits_one_release_to_fit_capacity(source):
    value = packet(source, [('A', 10), ('B', 20)], start=2)
    publish(source, value)
    with source.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        frontier = journal.capture_frontier(c)
        after = journal.Cursor(frontier.epoch, 0)
        with pytest.raises(ValueError, match='atomic_publication_exceeds'):
            journal.read_publications(c, symbols=frozenset({'A','B'}), after=after,
                                      max_publications=1, max_trade_rows=1)
        read = journal.read_publications(c, symbols=frozenset({'A','B'}), after=after,
                                        max_publications=1, max_trade_rows=2)
    p, = read.publications
    assert p.source_trade_ids == value[1] and p.source_symbols == ('A', 'B')
    assert {r['symbol'] for r in p.rows} == {'A','B'}


def test_other_symbol_publications_advance_observation_without_inventing_prints(source):
    o = owner(source)
    o.update_demand('watch', revision=1, symbols=['A'])
    before = o.read('entry')
    publish(source, packet(source, [('OTHER', 20)], start=2))
    snap = o.advance_one(source)
    assert snap.source.revision == before.source.revision+1
    assert counts(snap) == {'A': 0}
    assert snap.symbols[0].history_before_anchor == 'unknown'
    assert snap.symbols[0].last_print is None


def test_rollback_release_is_invisible_to_shared_owner(source):
    o = owner(source)
    o.update_demand('held', revision=1, symbols=['A'])
    before = o.read('exit')
    rows, ids = packet(source, [('A', 10)], start=2)
    with pytest.raises(RuntimeError, match='rollback'):
        with source.begin() as c:
            bridge._release_pending_batch(c, trade_rows=rows, quote_rows=[], available_at=RELEASE,
                                          trade_row_ids=ids)
            raise RuntimeError('rollback')
    assert o.advance_one(source) is before
    publish(source, (rows, ids))
    assert counts(o.advance_one(source)) == {'A': 1}


def test_concurrent_wakes_serialize_one_reducer_per_symbol(source):
    from concurrent.futures import ThreadPoolExecutor
    o = owner(source)
    o.update_demand('held', revision=1, symbols=['A'])
    publish(source, packet(source, [('A', 10)], start=2))
    publish(source, packet(source, [('A', 12)], start=3))
    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshots = list(pool.map(lambda _: o.advance_one(source), range(2)))
    assert sorted(counts(s)['A'] for s in snapshots) == [1, 2]
    assert counts(o.read('exit')) == {'A': 2}


def test_catastrophic_commit_failure_never_exposes_a_partial_context(source, monkeypatch):
    from app.services.trading.momentum_neural.structural_tape_prefix import Result
    o = owner(source)
    o.update_demand('held', revision=1, symbols=['A', 'B'])
    before = o.read('exit')
    publish(source, packet(source, [('A', 10), ('B', 20)], start=2))
    monkeypatch.setattr(o._prefixes['B'], 'commit_frontier',
                        lambda _: Result('unresolved', o._prefixes['B'].prefix_sha256))
    with pytest.raises(RuntimeError, match='prepared_context_commit_failed'):
        o.advance_one(source)
    snap = o.read('exit')
    assert snap.status == 'unresolved' and snap.reason == 'context_owner_reconstruction_required'
    assert counts(snap) == counts(before) == {'A': 0, 'B': 0}
    assert snap.source == before.source
    with pytest.raises(RuntimeError, match='reconstruction_required'):
        o.update_demand('ranking', revision=1, symbols=['C'])
    with pytest.raises(RuntimeError, match='reconstruction_required'):
        o.advance_one(source)


def test_unchanged_symbol_reuses_immutable_scopes_instead_of_reducing_again(source, monkeypatch):
    o = owner(source)
    o.update_demand('inventory', revision=1, symbols=['A', 'B'])
    publish(source, packet(source, [('A', 10), ('B', 20), ('B', 21), ('B', 20)], start=2))
    before = o.advance_one(source)
    assert before.symbols[1].scopes
    def no_rescan(_): raise AssertionError('unchanged symbol was recomputed')
    monkeypatch.setattr(o._prefixes['B'], 'context', no_rescan)
    o.update_demand('held', revision=1, symbols=['B'])
    publish(source, packet(source, [('A', 11)], start=6))
    after = o.advance_one(source)
    assert after.status == 'observed_prefix'
    assert after.symbols[1].scopes is before.symbols[1].scopes
    assert after.symbols[1].prefix_sha256 != before.symbols[1].prefix_sha256
    assert counts(after) == {'A': 2, 'B': 3}
