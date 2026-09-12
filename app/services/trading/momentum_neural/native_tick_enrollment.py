"""Compact, replayable native-to-provider observation enrollment evidence."""
from dataclasses import dataclass
from uuid import UUID

from .native_iqfeed_mapping import NativeIQFeedMapping, NativeIQFeedBinding, MARKETS, CONTRACT, symbol_candidates
from scripts.iqfeed_equity_catalog import EquityListing

REASONS = ('held', 'inventory', 'pending')


def digest(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def symbol(value):
    return (type(value) is str and bool(value) and value == value.strip().upper()
            and not any(c in value for c in '/,\r\n') and all(32 < ord(c) < 127 for c in value))


@dataclass(frozen=True)
class NativeEquityIdentity:
    asset_id: str
    broker_symbols: tuple[str, ...]
    provider_symbol: str
    provider_exchange: str
    provider_listed_market: str
    provider_line_sha256: str


@dataclass(frozen=True)
class NativeMappingGap:
    asset_id: str
    asset_class: str
    broker_symbols: tuple[str, ...]
    demand_reasons: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class NativeEnrollmentReference:
    account_identity_sha256: str
    native_revision: int
    native_observation_sha256: str
    mapping_sha256: str
    catalog_sha256: str | None
    catalog_observed_ns: int | None


@dataclass(frozen=True)
class NativeTickEnrollment:
    reference: NativeEnrollmentReference
    bindings: tuple[NativeEquityIdentity, ...]
    held: tuple[str, ...]
    inventory: tuple[str, ...]
    pending: tuple[str, ...]
    incomplete_reasons: tuple[str, ...]
    gaps: tuple[NativeMappingGap, ...]


def _strings(values):
    return (type(values) is tuple and all(type(s) is str and s and s == s.strip().upper() for s in values)
            and tuple(sorted(set(values))) == values)


def validate_identity(binding):
    if (type(binding) is not NativeEquityIdentity or type(binding.asset_id) is not str
            or str(UUID(binding.asset_id)) != binding.asset_id or not _strings(binding.broker_symbols)
            or not binding.broker_symbols or not symbol(binding.provider_symbol)
            or not symbol(binding.provider_exchange) or not symbol(binding.provider_listed_market)
            or not digest(binding.provider_line_sha256)):
        raise ValueError('native_equity_binding_invalid')


def validate_reference(ref):
    if (type(ref) is not NativeEnrollmentReference or type(ref.native_revision) is not int
            or ref.native_revision <= 0 or not all(digest(v) for v in (
                ref.account_identity_sha256, ref.native_observation_sha256, ref.mapping_sha256))
            or ref.catalog_sha256 is not None and not digest(ref.catalog_sha256)):
        raise ValueError('native_enrollment_reference_invalid')
    if ((ref.catalog_sha256 is None) != (ref.catalog_observed_ns is None)
            or ref.catalog_observed_ns is not None and
               (type(ref.catalog_observed_ns) is not int or ref.catalog_observed_ns <= 0)):
        raise ValueError('native_enrollment_catalog_observation_invalid')


def validate_gap(gap):
    if (type(gap) is not NativeMappingGap or type(gap.asset_id) is not str
            or str(UUID(gap.asset_id)) != gap.asset_id or not _strings(gap.broker_symbols)
            or not gap.broker_symbols or type(gap.asset_class) is not str or not gap.asset_class
            or type(gap.reason) is not str or not gap.reason
            or type(gap.demand_reasons) is not tuple
            or tuple(sorted(set(gap.demand_reasons))) != gap.demand_reasons
            or not set(gap.demand_reasons) <= set(REASONS)):
        raise ValueError('native_mapping_gap_invalid')


def validate_enrollment(value):
    if type(value) is not NativeTickEnrollment:
        raise ValueError('native_tick_enrollment_required')
    validate_reference(value.reference)
    if type(value.bindings) is not tuple or type(value.gaps) is not tuple:
        raise ValueError('native_enrollment_membership_invalid')
    for binding in value.bindings:
        validate_identity(binding)
    symbols = [b.provider_symbol for b in value.bindings]
    ids = [b.asset_id for b in value.bindings]
    if symbols != sorted(set(symbols)) or len(ids) != len(set(ids)):
        raise ValueError('native_enrollment_identity_collision')
    if value.bindings and value.reference.catalog_sha256 is None:
        raise ValueError('native_enrollment_catalog_required')
    if (type(value.incomplete_reasons) is not tuple
            or tuple(sorted(set(value.incomplete_reasons))) != value.incomplete_reasons
            or not set(value.incomplete_reasons) <= set(REASONS)):
        raise ValueError('native_enrollment_incomplete_reasons_invalid')
    for reason in REASONS:
        members = getattr(value, reason)
        if not _strings(members) or not set(members) <= set(symbols):
            raise ValueError('native_enrollment_unbound_membership')
    for gap in value.gaps:
        validate_gap(gap)
        if gap.asset_class == 'us_equity' and not set(gap.demand_reasons) <= set(value.incomplete_reasons):
            raise ValueError('native_enrollment_gap_cannot_clear_demand')
    gap_ids = [g.asset_id for g in value.gaps]
    if gap_ids != sorted(set(gap_ids)) or set(gap_ids) & set(ids):
        raise ValueError('native_enrollment_gap_identity_collision')
    return value


def from_mapping(mapping):
    if (type(mapping) is not NativeIQFeedMapping or mapping.order_authority is not False
            or mapping.contract != CONTRACT or mapping.full_broker_universe_tick_observed is not False):
        raise ValueError('native_mapping_observation_required')
    from .native_iqfeed_mapping import mapping_digest
    if mapping_digest(mapping) != mapping.content_sha256:
        raise ValueError('native_mapping_digest_mismatch')
    bindings, gaps = [], []
    members = {reason: set() for reason in REASONS}
    incomplete = set(mapping.stale_native_reasons)
    for binding in mapping.bindings:
        if type(binding) is not NativeIQFeedBinding or binding.order_authority is not False:
            raise ValueError('native_mapping_binding_required')
        if binding.status != 'catalog_matched':
            if binding.demand_reasons:
                gaps.append(NativeMappingGap(binding.asset_id, binding.asset_class,
                    binding.broker_symbols, binding.demand_reasons, binding.reason))
                if binding.asset_class == 'us_equity':
                    incomplete.update(binding.demand_reasons)
            continue
        row = binding.provider_listing
        if (binding.asset_class != 'us_equity' or type(row) is not EquityListing
                or binding.provider_symbol != row.symbol or len(binding.broker_exchanges) != 1
                or (row.exchange, row.listed_market) not in MARKETS.get(binding.broker_exchanges[0], ())
                or not any((row.symbol, binding.mapping_rule) in symbol_candidates(name)
                           for name in binding.broker_symbols)):
            raise ValueError('native_mapping_equity_evidence_invalid')
        bindings.append(NativeEquityIdentity(binding.asset_id, binding.broker_symbols, row.symbol,
            row.exchange, row.listed_market, row.source_line_sha256))
        for reason in binding.demand_reasons:
            members[reason].add(row.symbol)
    value = NativeTickEnrollment(NativeEnrollmentReference(mapping.account_identity_sha256,
        mapping.native_revision, mapping.native_observation_sha256, mapping.content_sha256,
        mapping.catalog_source_sha256, mapping.catalog_observed_ns), tuple(sorted(bindings, key=lambda b: b.provider_symbol)),
        *(tuple(sorted(members[r])) for r in REASONS), tuple(sorted(incomplete)),
        tuple(sorted(gaps, key=lambda g: g.asset_id)))
    return validate_enrollment(value)
