"""Native identity survives actual publication, failure, and warm recovery."""
from dataclasses import replace
from uuid import UUID

import pytest

from tests.test_native_iqfeed_mapping import catalog, listing, broker, snapshot, NativeIQFeedMapper
from tests.test_broker_native_demand import ACCOUNT, inventory, coverage, held, order
from tests.test_ordinary_structural_context import source, packet, publish
from tests.test_structural_context_recovery import CLOCK, CAP, restore
from tests.test_current_structural_context import observe
from tests.test_structural_context_publisher import context_db, worker, seed, observed
from app.services.trading.momentum_neural import structural_context_journal as j
from app.services.trading.momentum_neural import structural_context_recovery as r
from app.services.trading.momentum_neural import native_tick_enrollment as n
from app.services.trading.momentum_neural.broker_native_demand import BrokerNativeDemandService
from app.services.trading.momentum_neural.current_structural_context import ordinary_paper_context_stream
from app.services.trading.momentum_neural.structural_tape_prefix import Limits


def enrollment(rows=None, provider_rows=None, *, positions=(), orders=()):
    return n.from_mapping(NativeIQFeedMapper(catalog(
        [listing('ABR-D')] if provider_rows is None else provider_rows)).bind(snapshot(
        [broker(1, 'ABR.PRD')] if rows is None else rows, positions, orders)))


@pytest.fixture
def native_owner(source):
    with source.begin() as c:
        j.create_schema(c)
        r.create_schema(c)
    publish(source, packet(source, [('SEED', 1)]))
    service = r.RecoverableStructuralContext.create(source, stream_id=ordinary_paper_context_stream(ACCOUNT),
        max_payload_bytes=CAP, max_input_bytes=CAP, limits=Limits(100, 100, 100),
        max_symbols=10, max_trade_rows=100, clock_ns=lambda: CLOCK)
    try:
        yield source, service
    finally:
        service.close()


def test_native_alias_and_uuid_use_same_published_wave_after_restart(native_owner):
    engine, service = native_owner
    value = enrollment(positions=[held(1, 'ABR.PRD')], orders=[order(1, 'ABR.PRD')])
    service.update_native_enrollment(value)
    publish(engine, packet(engine, [('ABR-D', 10), ('ABR-D', 12), ('ABR-D', 11), ('ABR-D', 13)], start=2))
    before = service.owner.advance_one(engine)
    stream = service.writer.cursor.stream_id
    asset_id = str(UUID(int=1))
    receipt = observe(engine, stream).receipt(['ABR.PRD', 'ABR-D'], native_asset_ids=[asset_id])
    state = receipt['symbols']['ABR.PRD']
    assert state == receipt['native_assets'][asset_id]
    assert state['provider_symbol'] == 'ABR-D' and state['print_count'] == 4
    assert state['flow_references'] and state['native_binding_current']
    assert receipt['symbols']['ABR-D']['status'] == 'not_enrolled'  # No raw-provider alias guessing.
    assert before.symbols[0].demand_reasons == ('held', 'inventory', 'pending')
    cursor = service.writer.cursor
    service.update_native_enrollment(value)
    assert service.writer.cursor == cursor
    service.close()
    resumed = restore(engine, stream, clock_ns=lambda: CLOCK)
    try:
        assert resumed.owner.read('entry') == before
        assert observe(engine, stream).receipt(['ABR.PRD'])['symbols']['ABR.PRD'] == state
        assert resumed.writer.cursor == cursor
    finally:
        resumed.close()


def test_mapping_failure_retains_exposure_but_never_presents_old_binding_as_current(native_owner):
    engine, service = native_owner
    book = BrokerNativeDemandService(expected_account_id=ACCOUNT)
    snap = book.apply(inventory([broker(1, 'ABR.PRD')]), coverage([held(1, 'ABR.PRD')], [order(1, 'ABR.PRD')]))
    service.update_native_enrollment(n.from_mapping(NativeIQFeedMapper(catalog([listing('ABR-D')])).bind(snap)))
    missing = n.from_mapping(NativeIQFeedMapper(None).bind(snap))
    service.update_native_enrollment(missing)
    stale = service.owner.read('exit')
    assert stale.symbols[0].demand_reasons == ('held', 'inventory', 'pending')
    assert stale.stale_demand_sources == ('held', 'inventory', 'pending')
    assert not stale.symbols[0].native_binding_current
    assert observe(engine, service.writer.cursor.stream_id).receipt(['ABR.PRD'])['symbols']['ABR.PRD']['status'] == 'native_mapping_gap'
    # A later authoritative empty book clears membership, not retained history.
    empty = book.apply(inventory([], start=30), coverage(start=40))
    service.update_native_enrollment(n.from_mapping(NativeIQFeedMapper(catalog([listing('ABR-D')])).bind(empty)))
    assert service.owner.read('exit').symbols[0].demand_reasons == ()
    assert observe(engine, service.writer.cursor.stream_id).receipt(['ABR.PRD'])['symbols']['ABR.PRD']['status'] == 'native_binding_not_current'


def test_reused_provider_symbol_cannot_inherit_other_native_assets_wave(native_owner):
    engine, service = native_owner
    service.update_native_enrollment(enrollment())
    publish(engine, packet(engine, [('ABR-D', 10)], start=2))
    before = service.owner.advance_one(engine)
    other = enrollment(rows=[broker(2, 'ABR.PRD')])
    other = replace(other, reference=replace(other.reference, native_revision=2))
    with pytest.raises(ValueError, match='identity_requires_reconstruction'):
        service.update_native_enrollment(other)
    assert service.owner.read('exit') == before


def test_account_mismatch_is_rejected_before_owner_mutation(native_owner):
    _, service = native_owner
    value = enrollment()
    wrong = replace(value, reference=replace(value.reference, account_identity_sha256='f'*64))
    before = service.owner.read('exit')
    with pytest.raises(ValueError, match='stream_account_mismatch'):
        service.update_native_enrollment(wrong)
    assert service.owner.read('exit') == before
    with pytest.raises(ValueError, match='account_stream_mismatch'):
        j._validate_native_stream(replace(before, native_enrollment=wrong.reference), service.writer.cursor.stream_id)


def test_native_ownership_cannot_be_overwritten_by_legacy_role_updates(native_owner):
    _, service = native_owner
    service.update_native_enrollment(enrollment())
    with pytest.raises(ValueError, match='native_demand_requires_bound_update'):
        service.owner.update_demand('inventory', revision=99, symbols=[])
    for symbol in ('BTC-USD', 'BTC/USD'):
        with pytest.raises(ValueError):
            service.owner.update_demand('ranking', revision=1, symbols=[symbol])


def test_catalog_failure_does_not_erase_known_observation_frontier(native_owner):
    _, service = native_owner
    value = enrollment()
    service.update_native_enrollment(value)
    service.update_native_enrollment(n.from_mapping(NativeIQFeedMapper(None).bind(snapshot([broker(1, 'ABR.PRD')]))))
    old = replace(value, reference=replace(value.reference, catalog_observed_ns=999))
    with pytest.raises(ValueError, match='catalog_frontier'):
        service.update_native_enrollment(old)


def test_crypto_gap_does_not_claim_equity_tick_coverage_or_erase_equity_membership():
    value = enrollment(rows=[broker(1, 'ABR.PRD'), broker(2, 'BTC/USD', cls='crypto', exchange='CRYPTO')])
    assert value.inventory == ('ABR-D',) and value.incomplete_reasons == ()
    assert value.gaps[0].reason == 'native_crypto_source_required'


def test_unmapped_equity_is_retained_as_gap_and_incomplete_demand():
    value = enrollment(rows=[broker(1, 'ABR.PRD'), broker(2, 'MISSING')])
    assert value.inventory == ('ABR-D',) and value.incomplete_reasons == ('inventory',)
    assert value.gaps[0].broker_symbols == ('MISSING',)


def test_mutated_mapping_digest_and_inconsistent_native_snapshot_are_rejected(native_owner):
    _, service = native_owner
    mapping = NativeIQFeedMapper(catalog([listing('ABR-D')])).bind(snapshot([broker(1, 'ABR.PRD')]))
    with pytest.raises(ValueError, match='digest_mismatch'):
        n.from_mapping(replace(mapping, content_sha256='0'*64))
    service.update_native_enrollment(n.from_mapping(mapping))
    before = service.owner.read('exit')
    corrupt = replace(before, symbols=(replace(before.symbols[0], symbol='WRONG'),))
    with pytest.raises(ValueError, match='binding_symbol_mismatch'):
        j.encode_snapshot(corrupt)
    absent_catalog = replace(before, native_enrollment=replace(before.native_enrollment,
        catalog_sha256=None, catalog_observed_ns=None))
    with pytest.raises(ValueError, match='current_native_binding_conflict'):
        j.encode_snapshot(absent_catalog)


def test_failed_native_commit_recovers_previous_membership(native_owner, monkeypatch):
    engine, service = native_owner
    before, stream = service.owner.read('exit'), service.writer.cursor.stream_id
    original = service.writer._c.execute
    def fail(statement, *args, **kwargs):
        if 'UPDATE momentum_structural_context_recovery_heads' in str(statement):
            raise RuntimeError('native-input-rollback')
        return original(statement, *args, **kwargs)
    monkeypatch.setattr(service.writer._c, 'execute', fail)
    with pytest.raises(RuntimeError, match='native-input-rollback'):
        service.update_native_enrollment(enrollment())
    resumed = restore(engine, stream, clock_ns=lambda: CLOCK)
    try:
        assert resumed.owner.read('exit') == before
        resumed.update_native_enrollment(enrollment())
        assert resumed.owner.read('exit').symbols[0].native_binding_current
    finally:
        resumed.close()


def test_supervised_publisher_accepts_native_enrollment_and_replays_same_identity(context_db):
    seed(context_db)
    w = worker(context_db, account=ACCOUNT)
    value = enrollment()
    try:
        w.start()
        w.step(value)
        publish(context_db, packet(context_db, [('ABR-D', 10), ('ABR-D', 11)], start=2))
        w.step()
        before = observed(w).receipt(['ABR.PRD'])
        assert before['symbols']['ABR.PRD']['print_count'] == 2
    finally:
        w.close()
    resumed = worker(context_db, account=ACCOUNT)
    try:
        assert resumed.start().startup_mode == 'restore'
        assert observed(resumed).receipt(['ABR.PRD']) == before
    finally:
        resumed.close()


def test_ambiguous_native_alias_requires_uuid_instead_of_lexical_pick(native_owner):
    engine, service = native_owner
    value = enrollment(rows=[broker(1, 'A'), broker(2, 'B')], provider_rows=[listing('A'), listing('B')])
    value = replace(value, bindings=tuple(replace(b, broker_symbols=('SHARED',)) for b in value.bindings))
    service.update_native_enrollment(value)
    receipt = observe(engine, service.writer.cursor.stream_id).receipt(['SHARED'], native_asset_ids=[str(UUID(int=1))])
    assert receipt['symbols']['SHARED']['status'] == 'native_identity_ambiguous'
    assert receipt['native_assets'][str(UUID(int=1))]['provider_symbol'] == 'A'


def test_first_native_binding_cannot_adopt_populated_unverified_legacy_prefix(native_owner):
    engine, service = native_owner
    service.owner.update_demand('watch', revision=1, symbols=['A'])
    publish(engine, packet(engine, [('A', 10)], start=2))
    before = service.owner.advance_one(engine)
    with pytest.raises(ValueError, match='requires_cold_provider_prefix'):
        service.update_native_enrollment(enrollment(rows=[broker(1, 'A')], provider_rows=[listing('A')]))
    assert service.owner.read('exit') == before
