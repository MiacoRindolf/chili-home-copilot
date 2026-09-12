"""One native-ID inventory/held/pending view for shared tape enrollment.

Broker symbol spelling is evidence, not the identity key. No ranking or strategy
gate can remove held/pending observation demand. This is a broker demand view;
provider-symbol binding and fresh tick coverage must be supplied separately.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import threading
import time

from .alpaca_paper_identity import alpaca_paper_account_identity_sha256
from .broker_asset_inventory import (
    InventoryBook, InventoryProbe, InventoryState, _json, _sha,
)
from .broker_coverage_inventory import (
    CoverageBook, CoverageState, REASONS, coverage_probe, coverage_read,
)


@dataclass(frozen=True)
class NativeDemand:
    asset_id: str
    asset_class: str
    catalog_symbol: str | None
    observed_symbols: tuple[str, ...]
    reasons: tuple[str, ...]
    held_identities: tuple[str, ...]
    pending_order_ids: tuple[str, ...]
    catalog_tradable: bool | None
    # A native broker identity is not proof of a provider's instrument mapping.
    tick_source_binding: str = "not_bound"


@dataclass(frozen=True)
class NativeDemandSnapshot:
    account_identity_sha256: str
    revision: int
    inventory: InventoryState
    coverage: CoverageState
    members: tuple[NativeDemand, ...]
    stale_reasons: tuple[str, ...]
    content_sha256: str
    observation_sha256: str
    atomic_account_snapshot: bool = False
    order_authority: bool = False


def _compose(account, revision, inventory, coverage, previous):
    groups = {}

    def member(asset_id, asset_class, symbol):
        if asset_id not in groups:
            groups[asset_id] = dict(asset_class=asset_class, symbols=set(), reasons=set(),
                held=set(), pending=set(), catalog_symbol=None, tradable=None)
        group = groups[asset_id]
        if group['asset_class'] != asset_class:
            raise ValueError('native_asset_class_conflict')
        group['symbols'].add(symbol)
        return group

    if inventory.last_success is not None:
        for listing in inventory.last_success.assets:
            g = member(listing.asset_id, listing.asset_class, listing.symbol)
            g.update(catalog_symbol=listing.symbol, tradable=listing.tradable)
            if listing.tradable and listing.status == 'active':
                g['reasons'].add('inventory')
    for reason in REASONS:
        for exposure in getattr(coverage, reason):
            g = member(exposure.asset_id, exposure.asset_class, exposure.broker_symbol)
            g['reasons'].add(reason)
            g[reason].add(exposure.identity)
    # A UUID cannot silently switch asset classes across successful refreshes.
    if previous is not None:
        for old in previous.members:
            if old.asset_id in groups and old.asset_class != groups[old.asset_id]['asset_class']:
                raise ValueError('native_asset_class_changed')
    members = tuple(NativeDemand(identifier, g['asset_class'], g['catalog_symbol'],
        tuple(sorted(g['symbols'])), tuple(sorted(g['reasons'])), tuple(sorted(g['held'])),
        tuple(sorted(g['pending'])), g['tradable']) for identifier, g in sorted(groups.items()))
    stale = set()
    if not inventory.available:
        stale.add('inventory')
    # Either failed section prevents removing either exposure demand.
    if not coverage.membership_replacement_complete:
        stale.update(REASONS)
    content = _sha(_json(['broker_native_demand_v1', account,
        [asdict(m) for m in members], sorted(stale)]))
    observation = _sha(_json([content, revision, inventory.revision, inventory.error,
        inventory.last_success.observation_sha256 if inventory.last_success else None,
        coverage.revision, coverage.probe.observation_sha256 if coverage.probe else None]))
    return NativeDemandSnapshot(account, revision, inventory, coverage, members,
                                tuple(sorted(stale)), content, observation)


class BrokerNativeDemandService:
    """Serialize real broker refreshes into one immutable native demand view.

    Both books are staged privately. Validation/composition failure leaves the
    complete previous public view and both source revisions unchanged. HTTP
    failure is a new stale observation retaining prior membership; it is not a
    successful empty response. Restart/recovery must seed both retained books
    from the last durable snapshot; this class does not provide persistence.
    """
    def __init__(self, *, expected_account_id, initial: NativeDemandSnapshot | None = None):
        self._expected = expected_account_id
        self._account = alpaca_paper_account_identity_sha256(expected_account_id)
        self._lock = threading.RLock()
        if initial is not None:
            if type(initial) is not NativeDemandSnapshot or initial.account_identity_sha256 != self._account:
                raise ValueError('native_demand_seed_account_mismatch')
            rebuilt = _compose(self._account, initial.revision, initial.inventory, initial.coverage, None)
            if rebuilt != initial:
                raise ValueError('native_demand_seed_mismatch')
        self._snapshot = initial

    def read(self):
        with self._lock:
            return self._snapshot

    def apply(self, inventory_probe, exposure_probe):
        with self._lock:
            inventory = InventoryBook(expected_account_id=self._expected)
            coverage = CoverageBook(expected_account_id=self._expected)
            prior = self._snapshot
            if prior is not None:
                inventory.state, coverage.state = prior.inventory, prior.coverage
            staged_inventory = inventory.apply(inventory_probe)
            staged_coverage = coverage.apply(exposure_probe)
            snapshot = _compose(self._account, 1 if prior is None else prior.revision+1,
                                staged_inventory, staged_coverage, prior)
            self._snapshot = snapshot
            return snapshot

    def refresh(self, adapter):
        # Hold the same lock across reads so concurrent wakes cannot invert the
        # observation order. No broker calls are retried and no orders are sent.
        with self._lock:
            try:
                inventory = adapter.get_asset_inventory_probe()
            except Exception as exc:
                inventory = InventoryProbe(None, 'inventory_transport_unavailable:' + type(exc).__name__)
            started = time.time_ns()
            try:
                exposure = adapter.get_coverage_inventory_probe()
            except Exception as exc:
                ended = time.time_ns()
                reads = tuple(coverage_read(reason, started_ns=started, completed_ns=ended,
                    expected_account_id=self._expected,
                    error='coverage_transport_unavailable:' + type(exc).__name__) for reason in REASONS)
                exposure = coverage_probe(expected_account_id=self._expected,
                    started_ns=started, completed_ns=ended, reads=reads)
            return self.apply(inventory, exposure)
