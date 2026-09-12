from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.services.trading.momentum_neural.broker_asset_inventory import InventoryProbe, build_inventory
from app.services.trading.momentum_neural.broker_coverage_inventory import coverage_probe, coverage_read
from app.services.trading.momentum_neural.broker_native_demand import BrokerNativeDemandService

ACCOUNT = 'aaaaaaaa-2222-4333-8444-555555555555'


def asset(n, symbol, cls='us_equity', tradable=True):
    return dict(id=str(UUID(int=n)), symbol=symbol, **{'class': cls}, status='active', tradable=tradable)


def inventory(rows=(), start=10):
    catalogs = {cls: (start+1, start+2, [r for r in rows if r['class'] == cls])
                for cls in ('crypto', 'us_equity')}
    return InventoryProbe(build_inventory(expected_account_id=ACCOUNT, account_before=ACCOUNT,
        account_after=ACCOUNT, started_ns=start, completed_ns=start+3,
        catalog_responses=catalogs), None)


def held(n, symbol, cls='us_equity'):
    return dict(asset_id=str(UUID(int=n)), asset_class=cls, symbol=symbol, qty='0.00001')


def order(n, symbol, cls='us_equity', oid=100):
    return dict(held(n, symbol, cls), id=str(UUID(int=oid)), side='sell', status='pending_cancel')


def coverage(positions=(), orders=(), start=20):
    reads = tuple(coverage_read(reason, started_ns=start+1, completed_ns=start+2,
        response=None if rows is None else list(rows), expected_account_id=ACCOUNT)
        for reason, rows in [('held', positions), ('pending', orders)])
    return coverage_probe(expected_account_id=ACCOUNT, started_ns=start, completed_ns=start+3, reads=reads)


def service():
    return BrokerNativeDemandService(expected_account_id=ACCOUNT)


def by_id(view, n):
    return next(m for m in view.members if m.asset_id == str(UUID(int=n)))


def test_native_uuid_unifies_crypto_aliases_without_guessing_provider_symbol():
    s = service()
    view = s.apply(inventory([asset(1, 'BTC/USD', 'crypto')]),
        coverage([held(1, 'BTCUSD', 'crypto')], [order(1, 'BTC/USD', 'crypto')]))
    assert len(view.members) == 1
    m = by_id(view, 1)
    assert m.catalog_symbol == 'BTC/USD'
    assert m.observed_symbols == ('BTC/USD', 'BTCUSD')
    assert m.reasons == ('held', 'inventory', 'pending')
    assert m.tick_source_binding == 'not_bound' and not view.order_authority
    assert s.read() is view
    with pytest.raises(FrozenInstanceError):
        m.catalog_symbol = 'BTC-USD'


def test_same_symbol_with_distinct_native_ids_is_not_merged():
    view = service().apply(inventory(), coverage([held(1, 'A')], [order(2, 'A')]))
    assert len(view.members) == 2
    assert by_id(view, 1).reasons == ('held',)
    assert by_id(view, 2).reasons == ('pending',)


def test_all_catalog_members_kept_including_untradable_punctuation_and_non_usd_crypto():
    rows = [asset(1, 'ODD-W'), asset(2, 'OFF', tradable=False), asset(3, 'ETH/BTC', 'crypto')]
    view = service().apply(inventory(rows), coverage())
    assert len(view.members) == 3
    assert by_id(view, 1).catalog_symbol == 'ODD-W'
    assert by_id(view, 2).catalog_tradable is False and by_id(view, 2).reasons == ()
    assert by_id(view, 3).reasons == ('inventory',)


def test_catalog_removal_does_not_remove_delisted_held_or_pending_demand():
    s = service()
    s.apply(inventory([asset(1, 'A')]), coverage([held(1, 'A')], [order(1, 'A')]))
    view = s.apply(inventory(start=40), coverage([held(1, 'A')], [order(1, 'A')], start=50))
    assert by_id(view, 1).catalog_symbol is None
    assert by_id(view, 1).reasons == ('held', 'pending')
    assert by_id(view, 1).catalog_tradable is None


def test_partial_pending_to_held_transfer_retains_both_and_marks_both_stale():
    s = service()
    s.apply(inventory(), coverage([], [order(1, 'A')]))
    partial = s.apply(inventory(start=40), coverage([held(1, 'A')], None, start=50))
    assert by_id(partial, 1).reasons == ('held', 'pending')
    assert partial.stale_reasons == ('held', 'pending')
    final = s.apply(inventory(start=70), coverage([held(1, 'A')], [], start=80))
    assert by_id(final, 1).reasons == ('held',)
    assert final.stale_reasons == ()
    # Earlier consumers still have the complete immutable former revision.
    assert by_id(partial, 1).reasons == ('held', 'pending')


def test_failed_catalog_refresh_retains_inventory_without_releasing_exposure():
    s = service()
    s.apply(inventory([asset(1, 'A')]), coverage([held(2, 'B')]))
    view = s.apply(InventoryProbe(None, 'unavailable'), coverage(None, [], start=50))
    assert by_id(view, 1).reasons == ('inventory',)
    assert by_id(view, 2).reasons == ('held',)
    assert view.stale_reasons == ('held', 'inventory', 'pending')


def test_conflicting_class_cannot_commit_either_book_revision():
    s = service()
    before = s.apply(inventory([asset(1, 'A')]), coverage())
    with pytest.raises(ValueError, match='native_asset_class_conflict'):
        s.apply(inventory([asset(1, 'A')], start=40), coverage([held(1, 'A', 'crypto')], start=50))
    assert s.read() is before
    # Corrected input can use the refused source observations' exact clocks.
    final = s.apply(inventory([asset(1, 'A')], start=40), coverage([held(1, 'A')], start=50))
    assert final.revision == final.inventory.revision == final.coverage.revision == 2


def test_invalid_coverage_account_cannot_consume_successful_inventory_revision():
    s = service()
    before = s.apply(inventory(), coverage())
    with pytest.raises(ValueError, match='account_mismatch'):
        s.apply(inventory([asset(1, 'A')], start=40), replace(coverage(start=50), account_identity_sha256='bad'))
    assert s.read() is before


def test_refresh_calls_actual_adapter_interfaces_and_publishes_one_complete_view():
    s = service()
    observed = []
    def inv():
        observed.append(s.read())
        return inventory([asset(1, 'A')])
    def cov():
        observed.append(s.read())
        return coverage([held(2, 'B')])
    view = s.refresh(SimpleNamespace(get_asset_inventory_probe=inv, get_coverage_inventory_probe=cov))
    assert observed == [None, None]
    assert len(view.members) == 2 and view.revision == 1


def test_transport_exception_retains_prior_view_membership_and_sanitizes_details():
    s = service()
    s.apply(inventory([asset(1, 'A')]), coverage([held(2, 'B')]))
    def fail():
        raise RuntimeError('secret transport header must not be retained')
    view = s.refresh(SimpleNamespace(get_asset_inventory_probe=fail, get_coverage_inventory_probe=fail))
    assert len(view.members) == 2 and view.revision == 2
    assert 'secret' not in repr(view)
    assert view.stale_reasons == ('held', 'inventory', 'pending')


def test_restart_seed_preserves_failed_read_retention_and_same_content_hash():
    s = service()
    before = s.apply(inventory([asset(1, 'A')]), coverage([], [order(2, 'B')]))
    restored = BrokerNativeDemandService(expected_account_id=ACCOUNT, initial=before)
    assert restored.read() is before
    after = restored.apply(InventoryProbe(None, 'failed'), coverage(None, None, start=50))
    assert by_id(after, 1).reasons == ('inventory',)
    assert by_id(after, 2).reasons == ('pending',)
    with pytest.raises(ValueError, match='seed_mismatch'):
        BrokerNativeDemandService(expected_account_id=ACCOUNT, initial=replace(before, members=()))


def test_content_hash_is_independent_of_response_order_and_observation_clock():
    rows = [asset(1, 'A'), asset(2, 'B')]
    a = service().apply(inventory(rows), coverage([held(1, 'A')]))
    b = service().apply(inventory(list(reversed(rows)), start=40), coverage([held(1, 'A')], start=50))
    assert a.content_sha256 == b.content_sha256
    assert a.observation_sha256 != b.observation_sha256
