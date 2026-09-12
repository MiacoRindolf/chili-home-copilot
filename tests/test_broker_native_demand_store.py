"""Actual checkpoint transactions, restart retention and uncertain commit handling."""
from contextlib import contextmanager
from dataclasses import replace
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
import sqlalchemy as sa

from tests.test_ordinary_structural_context import source
from tests.test_broker_native_demand import ACCOUNT, asset, held, order, inventory, coverage, by_id
from app.services.trading.momentum_neural.broker_asset_inventory import InventoryProbe, _sha
from app.services.trading.momentum_neural.broker_coverage_inventory import (
    coverage_probe, coverage_read, bracket_pending_reads,
)
from app.services.trading.momentum_neural.broker_native_demand import BrokerNativeDemandService
from app.services.trading.momentum_neural import broker_native_demand_store as store

CAP = 2_000_000  # Test allocation only; never a market window or symbol selection.


@pytest.fixture
def db(source):
    with source.begin() as c:
        store.create_schema(c)
        store.create_schema(c)
    return source


def opened(db, *, cap=CAP):
    return store.DurableBrokerNativeDemandService.open(db, expected_account_id=ACCOUNT,
                                                     max_payload_bytes=cap)


def checkpoint(db):
    with db.connect() as c:
        return c.execute(sa.text('SELECT revision,payload_sha256,payload '
                                 'FROM momentum_native_demand_checkpoints')).mappings().one_or_none()


def test_restart_failure_keeps_all_inventory_and_pending_to_held_transition(db):
    a = opened(db)
    assert a.read() is None and checkpoint(db) is None
    first = a.apply(inventory([asset(1, 'BTC/USD', 'crypto'), asset(2, 'ODD-W')]),
                    coverage([], [order(1, 'BTCUSD', 'crypto')]))
    assert first.revision == 1 and checkpoint(db)['revision'] == 1
    b = opened(db)
    assert b.read() == first
    partial = b.apply(InventoryProbe(None, 'transport_unavailable'),
                      coverage([held(1, 'BTC/USD', 'crypto')], None, start=50))
    assert by_id(partial, 1).reasons == ('held', 'inventory', 'pending')
    assert by_id(partial, 2).reasons == ('inventory',)
    assert partial.stale_reasons == ('held', 'inventory', 'pending')
    c = opened(db)
    assert c.read() == partial
    final = c.apply(inventory(start=70), coverage([held(1, 'BTC/USD', 'crypto')], [], start=80))
    assert by_id(final, 1).reasons == ('held',) and len(final.members) == 1
    assert final.revision == 3 and opened(db).read() == final
    assert not final.order_authority and by_id(final, 1).tick_source_binding == 'not_bound'


def test_complete_empty_catalog_and_exposure_is_durable_and_not_a_read_failure(db):
    a = opened(db)
    value = a.apply(inventory(), coverage())
    assert value.members == () and value.stale_reasons == ()
    assert opened(db).read() == value


def test_initial_failed_reads_preserve_unknown_state_across_restart(db):
    a = opened(db)
    value = a.apply(InventoryProbe(None, 'unavailable'), coverage(None, None))
    assert value.members == () and value.stale_reasons == ('held', 'inventory', 'pending')
    assert opened(db).read() == value


@pytest.mark.parametrize('initial', [True, False])
def test_two_writers_cannot_overwrite_newer_snapshot_even_at_first_insert(db, initial):
    if not initial:
        opened(db).apply(inventory(), coverage())
    a, b = opened(db), opened(db)
    committed = a.apply(inventory([asset(1, 'A')], start=40), coverage(start=50))
    with pytest.raises(ValueError, match='concurrent_refresh'):
        b.apply(inventory([asset(2, 'B')], start=40), coverage(start=50))
    with pytest.raises(ValueError, match='reopen_required'):
        b.read()
    assert opened(db).read() == committed


def test_commit_acknowledgement_loss_requires_authoritative_reopen_without_retry(db):
    a = opened(db)
    class CommitThenRaise:
        @contextmanager
        def begin(self):
            with db.begin() as c:
                yield c
            raise RuntimeError('commit acknowledgement lost')
    a._engine = CommitThenRaise()
    with pytest.raises(RuntimeError, match='acknowledgement lost'):
        a.apply(inventory([asset(1, 'A')]), coverage([held(1, 'A')]))
    with pytest.raises(ValueError, match='reopen_required'):
        a.apply(inventory(start=40), coverage(start=50))
    actual = opened(db).read()
    assert actual.revision == 1 and by_id(actual, 1).reasons == ('held', 'inventory')


def test_transaction_failure_preserves_entire_previous_committed_pair(db):
    a = opened(db)
    prior = a.apply(inventory([asset(1, 'A')]), coverage([], [order(1, 'A')]))
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('UPDATE momentum_native_demand_checkpoints'):
            raise RuntimeError('write failed')
    sa.event.listen(db, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='write failed'):
            a.apply(inventory(start=40), coverage([held(1, 'A')], [], start=50))
    finally:
        sa.event.remove(db, 'before_cursor_execute', fail)
    assert opened(db).read() == prior
    with pytest.raises(ValueError, match='reopen_required'):
        a.read()


def test_payload_limits_reject_whole_snapshot_before_db_mutation_or_payload_read(db):
    a = opened(db, cap=1)
    with pytest.raises(ValueError, match='byte_capacity'):
        a.apply(inventory([asset(1, 'A')]), coverage())
    assert a.read() is None and checkpoint(db) is None
    opened(db).apply(inventory([asset(1, 'A')]), coverage())
    queries = []
    def record(conn, cursor, statement, parameters, context, executemany): queries.append(statement)
    sa.event.listen(db, 'before_cursor_execute', record)
    try:
        with pytest.raises(ValueError, match='byte_capacity'):
            opened(db, cap=1)
    finally:
        sa.event.remove(db, 'before_cursor_execute', record)
    assert not any('SELECT payload FROM' in q for q in queries)


@pytest.mark.parametrize('fault', ['hash', 'contract', 'account', 'metadata', 'revision'])
def test_corrupt_checkpoint_never_becomes_an_empty_book(db, fault):
    opened(db).apply(inventory([asset(1, 'A')]), coverage([held(1, 'A')]))
    row = checkpoint(db)
    payload = row['payload']
    if fault == 'contract': payload = payload.replace(store.CONTRACT, 'future_unknown_contract')
    elif fault == 'account': payload = payload.replace(opened(db).read().account_identity_sha256, 'a'*64)
    elif fault == 'metadata': payload = payload.replace('\\"symbol\\":\\"A\\"', '\\"symbol\\":\\"B\\"')
    elif fault == 'revision': payload = payload.replace('"revision":1', '"revision":2', 1)
    else: payload += ' '
    assert payload != row['payload']
    with db.begin() as c:
        c.execute(sa.text('UPDATE momentum_native_demand_checkpoints SET payload=:p,payload_sha256=:h'),
                  {'p': payload, 'h': row['payload_sha256'] if fault == 'hash' else _sha(payload)})
    with pytest.raises(ValueError):
        opened(db)
    assert checkpoint(db)['payload'] == payload


def test_refresh_does_not_publish_intermediate_broker_reads(db):
    a = opened(db)
    prior = a.apply(inventory(), coverage())
    seen = []
    def inv():
        seen.append(opened(db).read())
        return inventory([asset(1, 'A')], start=40)
    def cov():
        seen.append(opened(db).read())
        return coverage([held(1, 'A')], start=50)
    after = a.refresh(SimpleNamespace(get_asset_inventory_probe=inv, get_coverage_inventory_probe=cov))
    assert seen == [prior, prior] and opened(db).read() == after


def test_bracket_and_partial_response_evidence_round_trip_without_erasing_retained_exposure():
    s = BrokerNativeDemandService(expected_account_id=ACCOUNT)
    s.apply(inventory(), coverage([held(2, 'B')], [order(1, 'A')]))
    def read(reason, rows, start):
        return coverage_read(reason, started_ns=start, completed_ns=start+1,
                             response=rows, expected_account_id=ACCOUNT)
    pending = bracket_pending_reads(read('pending', [order(1, 'A')], 50),
                                    read('pending', [], 54))
    held_read = read('held', [held(1, 'A'), {'broken': 'row'}], 52)
    probe = coverage_probe(expected_account_id=ACCOUNT, started_ns=49, completed_ns=56,
                           reads=(held_read, pending))
    snap = s.apply(inventory(start=40), probe)
    payload = store.encode_snapshot(snap, expected_account_id=ACCOUNT)
    assert store.decode_snapshot(payload, expected_account_id=ACCOUNT) == snap
    assert by_id(snap, 2).reasons == ('held',)


def test_codec_refuses_duplicate_json_keys_and_false_completeness_claims():
    s = BrokerNativeDemandService(expected_account_id=ACCOUNT)
    snap = s.apply(inventory(), coverage(None, None))
    payload = store.encode_snapshot(snap, expected_account_id=ACCOUNT)
    with pytest.raises(ValueError, match='duplicate_json_key'):
        store.decode_snapshot(payload.replace('{', '{"contract":"duplicate",', 1), expected_account_id=ACCOUNT)
    with pytest.raises(ValueError):
        store.encode_snapshot(replace(snap, coverage=replace(snap.coverage, unavailable_reasons=())),
                              expected_account_id=ACCOUNT)


@pytest.mark.parametrize('field', ['authority', 'inventory_revision', 'probe_authority'])
def test_codec_preserves_boolean_and_revision_types(field):
    s = BrokerNativeDemandService(expected_account_id=ACCOUNT)
    snap = s.apply(inventory(), coverage())
    if field == 'authority':
        bad = replace(snap, order_authority=0)
    elif field == 'inventory_revision':
        bad = replace(snap, inventory=replace(snap.inventory, revision=True))
    else:
        bad = replace(snap, coverage=replace(snap.coverage,
            probe=replace(snap.coverage.probe, atomic_account_snapshot=0)))
    with pytest.raises(ValueError):
        store.encode_snapshot(bad, expected_account_id=ACCOUNT)


def test_migration_is_registered_once():
    from app.migrations import MIGRATIONS
    rows = [row for row in MIGRATIONS if row[0].startswith('384_')]
    assert len(rows) == 1 and rows[0][0] == '384_native_demand_checkpoint'
