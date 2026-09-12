"""Native PAPER UUID -> explicit provider catalog instrument, with visible gaps.

Catalog symbol/market agreement is observation routing evidence, not global-ID
equivalence, provider freshness, entitlement, subscription or order authority.
See docs/native_iqfeed_mapping.md for format sources and unmatched cases.
"""
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import re

from scripts.iqfeed_equity_catalog import EquityCatalog, EquityListing
from .broker_native_demand import NativeDemandSnapshot

CONTRACT = 'native_iqfeed_symbol_market_mapping_v1'

# Explicit broker listing exchange -> provider consolidated/listed exchange.
# These are venue identities, not statistical or strategy parameters.
MARKETS = {
    'NYSE': frozenset({('NYSE', 'NYSE')}),
    'ARCA': frozenset({('NYSE', 'NYSE_ARCA')}),
    'NYSEARCA': frozenset({('NYSE', 'NYSE_ARCA')}),
    'AMEX': frozenset({('NYSE_AMERICAN', 'NYSE_AMERICAN')}),
    'BATS': frozenset({('BATS', 'BATS')}),
    'NASDAQ': frozenset({('NASDAQ', 'NGM'), ('NASDAQ', 'NCM'), ('NASDAQ', 'NGSM')}),
    'OTC': frozenset({('NASDAQ', 'OTC')}),
}
SERIES = '[A-TV-Z]'
# The 2022 Alpaca format guide excludes U, but the actual paired catalogs contain
# FLG/NEE/PSA/TDS preferred Series U as .PRU -> -U. Preserve observed instruments
# rather than making that outdated character list an eligibility restriction.
PREFERRED_SERIES = '[A-Z]'


def symbol_candidates(symbol):
    """Exact spelling plus supported documented CMS/IQFeed suffix conversions.

    No common-share/root fallback, fuzzy company match or crypto pair rewriting.
    Unrecognized compound suffixes remain explicit unsupported/missing mappings.
    Even a converted spelling must exist in the right provider listing market.
    """
    if type(symbol) is not str or not symbol or symbol != symbol.strip().upper() or '/' in symbol:
        raise ValueError('native_mapping_equity_symbol_invalid')
    candidates = {symbol: 'exact_symbol'}
    root, dot, suffix = symbol.partition('.')
    if dot and root and re.fullmatch('[A-Z0-9]+', root):
        converted = {'PR': '-', 'RT': '.R', 'CL': '.L', 'CT': '.T',
                     'CV': '.V', 'CVCL': '.VL', 'WI': '*', 'PRWI': '-*',
                     'PRCL': '-L'}.get(suffix)
        if re.fullmatch('PR'+PREFERRED_SERIES, suffix):
            converted = '-'+suffix[2:]
        elif re.fullmatch('WS'+SERIES, suffix):
            converted = '.WS.'+suffix[2:]
        if converted is not None:
            candidates[root+converted] = ('paired_catalog_preferred_u_suffix' if suffix == 'PRU'
                                         else 'documented_cms_to_iqfeed_suffix')
    return tuple(sorted(candidates.items()))


@dataclass(frozen=True)
class NativeIQFeedBinding:
    asset_id: str
    asset_class: str
    broker_symbols: tuple[str, ...]
    broker_exchanges: tuple[str, ...]
    demand_reasons: tuple[str, ...]
    status: str
    reason: str | None
    provider_symbol: str | None = None
    provider_listing: EquityListing | None = None
    mapping_rule: str | None = None
    candidate_listings: tuple[EquityListing, ...] = ()
    order_authority: bool = False


@dataclass(frozen=True)
class NativeIQFeedMapping:
    account_identity_sha256: str
    native_revision: int
    native_observation_sha256: str
    catalog_source_sha256: str | None
    catalog_observed_ns: int | None
    bindings: tuple[NativeIQFeedBinding, ...]
    stale_native_reasons: tuple[str, ...]
    content_sha256: str
    contract: str = CONTRACT
    full_broker_universe_tick_observed: bool = False
    order_authority: bool = False


class NativeIQFeedMapper:
    def __init__(self, catalog: EquityCatalog | None):
        if catalog is not None and type(catalog) is not EquityCatalog:
            raise ValueError('provider_equity_catalog_required')
        self.catalog, self._symbols = catalog, {}
        if catalog is not None:
            for listing in catalog.equities:
                self._symbols.setdefault(listing.symbol, []).append(listing)

    def bind(self, snapshot: NativeDemandSnapshot):
        if type(snapshot) is not NativeDemandSnapshot:
            raise ValueError('native_demand_snapshot_required')
        exchanges, invalid_exchanges = {}, set()
        def add_exchange(asset_id, value):
            if value is None or value == '':
                return
            if type(value) is not str or value != value.strip().upper():
                invalid_exchanges.add(asset_id)
                return
            exchanges.setdefault(asset_id, set()).add(value)
        if snapshot.inventory.last_success is not None:
            for item in snapshot.inventory.last_success.assets:
                value = json.loads(item.metadata_json).get('exchange')
                add_exchange(item.asset_id, value)
        for member in (*snapshot.coverage.held, *snapshot.coverage.pending):
            value = json.loads(member.metadata_json).get('exchange')
            add_exchange(member.asset_id, value)
        output = []
        for member in snapshot.members:
            exchange = tuple(sorted(exchanges.get(member.asset_id, ())))
            names = ((member.catalog_symbol,) if member.catalog_symbol is not None
                     else member.observed_symbols)
            base = dict(asset_id=member.asset_id, asset_class=member.asset_class,
                        broker_symbols=member.observed_symbols, broker_exchanges=exchange,
                        demand_reasons=member.reasons)
            def gap(reason, candidates=()):
                return NativeIQFeedBinding(**base, status='unbound', reason=reason,
                                          candidate_listings=candidates)
            if member.asset_class != 'us_equity':
                output.append(gap('native_crypto_source_required' if member.asset_class == 'crypto'
                                  else 'asset_class_source_unmapped'))
                continue
            if self.catalog is None:
                output.append(gap('provider_catalog_unavailable'))
                continue
            if member.asset_id in invalid_exchanges:
                output.append(gap('broker_listing_exchange_invalid'))
                continue
            if len(exchange) != 1:
                output.append(gap('broker_listing_exchange_missing' if not exchange
                                  else 'broker_listing_exchange_conflict'))
                continue
            markets = MARKETS.get(exchange[0])
            if markets is None:
                output.append(gap('broker_listing_exchange_unmapped'))
                continue
            candidates = {}
            rules = {}
            invalid_symbol = False
            for name in names:
                try:
                    spellings = symbol_candidates(name)
                except ValueError:
                    invalid_symbol = True
                    break
                for candidate, rule in spellings:
                    for listing in self._symbols.get(candidate, ()):
                        key = (listing.symbol, listing.exchange, listing.listed_market)
                        candidates[key] = listing
                        rules[key] = rule
            if invalid_symbol:
                output.append(gap('broker_equity_symbol_format_unsupported'))
                continue
            observed = tuple(candidates[k] for k in sorted(candidates))
            matched = [(k, v) for k, v in candidates.items() if (v.exchange, v.listed_market) in markets]
            if not matched:
                output.append(gap('provider_listing_market_mismatch' if observed
                                  else 'provider_symbol_not_in_catalog', observed))
            elif len(matched) != 1:
                output.append(gap('provider_instrument_ambiguous', observed))
            else:
                key, listing = matched[0]
                output.append(NativeIQFeedBinding(**base, status='catalog_matched', reason=None,
                    provider_symbol=listing.symbol, provider_listing=listing,
                    mapping_rule=rules[key], candidate_listings=observed))
        # A reused ticker cannot attach an old held UUID and a different current
        # catalog UUID to one instrument without explicit identity reconciliation.
        owners = {}
        for binding in output:
            if binding.provider_listing is not None:
                r = binding.provider_listing
                owners.setdefault((r.symbol, r.exchange, r.listed_market), set()).add(binding.asset_id)
        for index, binding in enumerate(output):
            r = binding.provider_listing
            if r is not None and len(owners[(r.symbol, r.exchange, r.listed_market)]) != 1:
                output[index] = replace(binding, status='unbound',
                    reason='provider_instrument_native_identity_collision', provider_symbol=None,
                    provider_listing=None, mapping_rule=None)
        digest = hashlib.sha256(json.dumps([CONTRACT, snapshot.account_identity_sha256,
            snapshot.revision, snapshot.observation_sha256,
            self.catalog.source_text_sha256 if self.catalog else None,
            self.catalog.observed_ns if self.catalog else None,
            [asdict(b) for b in output], snapshot.stale_reasons], sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        # A full native account catalog remains full even with unsupported assets,
        # failed mappings, nontradable records, or absent provider observations.
        return NativeIQFeedMapping(snapshot.account_identity_sha256, snapshot.revision,
            snapshot.observation_sha256, self.catalog.source_text_sha256 if self.catalog else None,
            self.catalog.observed_ns if self.catalog else None, tuple(output), snapshot.stale_reasons, digest)
