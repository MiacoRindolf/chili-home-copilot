"""Catalog provenance and native identity routing, without live subscriptions."""
from dataclasses import FrozenInstanceError
import hashlib
import io

import pytest

from scripts.iqfeed_equity_catalog import HEADER, read_catalog
from tests.test_broker_native_demand import ACCOUNT, asset, inventory, coverage, held, order
from app.services.trading.momentum_neural.broker_asset_inventory import InventoryProbe
from app.services.trading.momentum_neural.broker_native_demand import BrokerNativeDemandService
from app.services.trading.momentum_neural.native_iqfeed_mapping import NativeIQFeedMapper, symbol_candidates


def text(rows):
    return HEADER+b'\r\n'+b''.join(('\t'.join(row)+'\r\n').encode('latin-1') for row in rows)


def listing(symbol, exchange='NYSE', listed='NYSE', description='NAME', kind='EQUITY'):
    return [symbol, description, exchange, listed, kind, '', '', '']


def catalog(rows):
    raw = text(rows)
    return read_catalog(io.BytesIO(raw), observed_ns=1000, max_bytes=len(raw), max_equities=len(rows)+1)


def broker(n, symbol, exchange='NYSE', **kwargs):
    return dict(asset(n, symbol, **kwargs), exchange=exchange)


def snapshot(rows, positions=(), orders=()):
    return BrokerNativeDemandService(expected_account_id=ACCOUNT).apply(inventory(rows), coverage(positions, orders))


def test_catalog_consumes_and_hashes_non_equities_without_confusing_them_with_missing_stocks():
    rows = [listing('A'), listing('AOPTION', kind='IEOPTION')]
    raw = text(rows)
    result = catalog(rows)
    assert result.source_text_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.source_bytes == len(raw) and result.source_rows == 2
    assert [r.symbol for r in result.equities] == ['A']
    assert not result.current_tick_coverage_certified and result.provider_published_at is None
    with pytest.raises(FrozenInstanceError): result.source_rows = 1


@pytest.mark.parametrize('failure', ['bytes', 'equities', 'duplicate', 'header', 'row'])
def test_catalog_failure_never_returns_a_truncated_successful_universe(failure):
    raw = text([listing('A'), listing('B')])
    cap, members = len(raw), 2
    if failure == 'bytes': cap -= 1
    elif failure == 'equities': members = 1
    elif failure == 'duplicate': raw = text([listing('A'), listing('A')]); cap = len(raw)
    elif failure == 'header': raw = b'WRONG\n'
    else: raw = text([listing('A')])+b'BROKEN\tROW\n'; cap = len(raw)
    with pytest.raises(ValueError):
        read_catalog(io.BytesIO(raw), observed_ns=1000, max_bytes=cap, max_equities=members)


def test_same_ticker_toronto_collision_resolves_to_documented_nyse_preferred_symbol():
    mapper = NativeIQFeedMapper(catalog([
        listing('C.PRN', 'TSE', 'TSE', 'PROFOUND MEDICAL CORP'),
        listing('C-N', description='CITIGROUP CAPITAL'),
    ]))
    value = mapper.bind(snapshot([broker(1, 'C.PRN')])).bindings[0]
    assert value.status == 'catalog_matched' and value.provider_symbol == 'C-N'
    assert value.mapping_rule == 'documented_cms_to_iqfeed_suffix'
    assert len(value.candidate_listings) == 2 and not value.order_authority


def test_same_spelling_without_matching_listing_exchange_remains_unbound():
    value = NativeIQFeedMapper(catalog([listing('C.PRN', 'TSE', 'TSE')])).bind(
        snapshot([broker(1, 'C.PRN')])).bindings[0]
    assert value.reason == 'provider_listing_market_mismatch' and value.provider_symbol is None


@pytest.mark.parametrize('native,provider', [('ABR.PRD', 'ABR-D'), ('AIIA.RT', 'AIIA.R'),
                                           ('A.WSA', 'A.WS.A'), ('A.PR', 'A-'), ('FLG.PRU', 'FLG-U')])
def test_documented_suffix_conversion_requires_actual_catalog_record(native, provider):
    snap = snapshot([broker(1, native)])
    assert NativeIQFeedMapper(catalog([])).bind(snap).bindings[0].provider_symbol is None
    result = NativeIQFeedMapper(catalog([listing(provider)])).bind(snap).bindings[0]
    assert result.status == 'catalog_matched' and result.provider_symbol == provider
    if native.endswith('.PRU'):
        assert result.mapping_rule == 'paired_catalog_preferred_u_suffix'


def test_two_same_market_instruments_are_ambiguous_without_preferring_exact_spelling():
    value = NativeIQFeedMapper(catalog([listing('A.PRA'), listing('A-A')])).bind(
        snapshot([broker(1, 'A.PRA')])).bindings[0]
    assert value.reason == 'provider_instrument_ambiguous' and value.provider_symbol is None


def test_all_native_members_remain_visible_without_crypto_alias_or_common_root_fallback():
    snap = snapshot([broker(1, 'A.PRA'), broker(2, 'BTC/USD', cls='crypto', exchange='CRYPTO'),
                     broker(3, 'OFF', tradable=False)])
    mapper = NativeIQFeedMapper(catalog([listing('A'), listing('BTC/USD'), listing('OFF')]))
    result = mapper.bind(snap)
    assert len(result.bindings) == 3
    bysymbol = {b.broker_symbols[0]: b for b in result.bindings}
    assert bysymbol['A.PRA'].reason == 'provider_symbol_not_in_catalog'
    assert bysymbol['BTC/USD'].reason == 'native_crypto_source_required'
    assert bysymbol['OFF'].demand_reasons == ()
    assert not result.full_broker_universe_tick_observed and not result.order_authority


def test_held_instrument_without_current_catalog_uses_explicit_exposure_exchange_and_symbol():
    snap = snapshot([], [dict(held(1, 'DELISTED'), exchange='NYSE')])
    value = NativeIQFeedMapper(catalog([listing('DELISTED')])).bind(snap).bindings[0]
    assert value.status == 'catalog_matched' and value.demand_reasons == ('held',)


def test_exposure_and_catalog_exchange_conflict_is_not_silently_resolved():
    snap = snapshot([broker(1, 'A')], [dict(held(1, 'A'), exchange='NASDAQ')])
    value = NativeIQFeedMapper(catalog([listing('A')])).bind(snap).bindings[0]
    assert value.reason == 'broker_listing_exchange_conflict'


def test_same_provider_instrument_cannot_silently_combine_different_native_uuids():
    snap = snapshot([broker(2, 'A')], [dict(held(1, 'A'), exchange='NYSE')])
    result = NativeIQFeedMapper(catalog([listing('A')])).bind(snap)
    assert len(result.bindings) == 2
    assert all(b.reason == 'provider_instrument_native_identity_collision' for b in result.bindings)
    assert all(b.provider_symbol is None for b in result.bindings)
    assert result.bindings[0].demand_reasons == ('held',)


def test_mapping_digest_binds_native_and_provider_observations_and_result():
    snap = snapshot([broker(1, 'A')])
    mapper = NativeIQFeedMapper(catalog([listing('A')]))
    first = mapper.bind(snap)
    assert mapper.bind(snap) == first
    assert NativeIQFeedMapper(catalog([listing('B')])).bind(snap).content_sha256 != first.content_sha256
    assert NativeIQFeedMapper(None).bind(snap).content_sha256 != first.content_sha256


def test_missing_exchange_and_bad_symbol_are_per_member_gaps():
    snap = snapshot([asset(1, 'A'), broker(2, 'B/C'), broker(3, 'OK')])
    values = NativeIQFeedMapper(catalog([listing('OK')])).bind(snap).bindings
    assert [b.reason for b in values] == ['broker_listing_exchange_missing',
                                        'broker_equity_symbol_format_unsupported', None]


def test_provider_unavailability_and_stale_native_sources_do_not_drop_demands():
    service = BrokerNativeDemandService(expected_account_id=ACCOUNT)
    first = service.apply(inventory([broker(1, 'A')]), coverage([dict(held(1, 'A'), exchange='NYSE')]))
    stale = service.apply(InventoryProbe(None, 'failed'), coverage(None, None, start=50))
    value = NativeIQFeedMapper(None).bind(stale)
    assert len(value.bindings) == 1 and value.bindings[0].demand_reasons == ('held', 'inventory')
    assert value.bindings[0].reason == 'provider_catalog_unavailable'
    assert value.stale_native_reasons == ('held', 'inventory', 'pending')
    assert value.native_revision == first.revision+1 and value.catalog_source_sha256 is None


def test_supported_market_identities_distinguish_listing_from_consolidated_exchange():
    mapper = NativeIQFeedMapper(catalog([listing('ARCA', 'NYSE', 'NYSE_ARCA'),
        listing('AMEX', 'NYSE_AMERICAN', 'NYSE_AMERICAN'), listing('OTC', 'NASDAQ', 'OTC'),
        listing('NMS', 'NASDAQ', 'NGSM')]))
    snap = snapshot([broker(1, 'ARCA', 'ARCA'), broker(2, 'AMEX', 'AMEX'),
                     broker(3, 'OTC', 'OTC'), broker(4, 'NMS', 'NASDAQ')])
    assert all(b.status == 'catalog_matched' for b in mapper.bind(snap).bindings)


def test_no_suffix_removal_or_unsupported_compound_conversion():
    assert symbol_candidates('BRK.B') == (('BRK.B', 'exact_symbol'),)
    assert symbol_candidates('A.PRCV') == (('A.PRCV', 'exact_symbol'),)
    assert not any(name == 'A' for name, _ in symbol_candidates('A.PRA'))
