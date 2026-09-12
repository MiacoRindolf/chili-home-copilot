"""Complete requested asset catalogs, separate from tick coverage and eligibility.

No market-price, volume, Ross, setup, ranking or top-N filter. Broker-listed
tradability is an inventory fact, not current execution or profit authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import threading
from typing import Mapping
from uuid import UUID

from .alpaca_paper_identity import alpaca_paper_account_identity_sha256

ASSET_CLASSES = ("crypto", "us_equity")
CONTRACT = "alpaca_paper_full_asset_inventory_v1"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _text(value):
    if isinstance(value, Enum):
        value = value.value
    return str(value) if isinstance(value, UUID) else value


def _row(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True)
    if not isinstance(value, Mapping):
        raise ValueError("asset_record_invalid")
    return dict(value)


@dataclass(frozen=True)
class AssetListing:
    asset_id: str
    asset_class: str
    symbol: str
    status: str
    tradable: bool
    metadata_json: str

    @property
    def key(self):
        return self.asset_class, self.symbol


@dataclass(frozen=True)
class CatalogRead:
    asset_class: str
    started_ns: int
    completed_ns: int
    response_count: int
    content_sha256: str


@dataclass(frozen=True)
class AssetInventory:
    account_identity_sha256: str
    started_ns: int
    completed_ns: int
    catalogs: tuple[CatalogRead, ...]
    assets: tuple[AssetListing, ...]
    content_sha256: str
    observation_sha256: str
    cross_class_atomic: bool = False
    order_authority: bool = False

    @property
    def tradable_keys(self):
        return tuple(a.key for a in self.assets if a.status == "active" and a.tradable)

    def tradable_symbols(self, asset_class):
        if asset_class not in ASSET_CLASSES:
            raise ValueError("unsupported_inventory_asset_class")
        return tuple(a.symbol for a in self.assets
                     if a.asset_class == asset_class and a.status == "active" and a.tradable)


@dataclass(frozen=True)
class InventoryProbe:
    snapshot: AssetInventory | None
    error: str | None

    def __post_init__(self):
        if ((self.snapshot is None) == (self.error is None)
                or self.snapshot is not None and type(self.snapshot) is not AssetInventory
                or self.error is not None and (type(self.error) is not str or not self.error)):
            raise ValueError("inventory_probe_shape_invalid")


def build_inventory(*, expected_account_id, account_before, account_after,
                    started_ns, completed_ns, catalog_responses):
    """Validate both complete active-class responses without truncation/filtering.

    Responses are the full lists returned by get_all_assets. Their original
    per-call intervals are retained: two HTTP calls are not a broker transaction.
    SDK metadata is preserved as delivered, not asserted to be original wire bytes.
    """
    account_hash = alpaca_paper_account_identity_sha256(expected_account_id)
    if (str(_text(account_before)) != expected_account_id
            or str(_text(account_after)) != expected_account_id):
        raise ValueError("asset_inventory_account_changed")
    if (type(started_ns) is not int or type(completed_ns) is not int
            or not 0 < started_ns <= completed_ns):
        raise ValueError("asset_inventory_clock_invalid")
    if type(catalog_responses) is not dict or set(catalog_responses) != set(ASSET_CLASSES):
        raise ValueError("asset_inventory_catalogs_incomplete")
    assets, reads, seen_ids, seen_keys = [], [], set(), set()
    for asset_class in ASSET_CLASSES:
        start, end, response = catalog_responses[asset_class]
        if (type(start) is not int or type(end) is not int or not started_ns <= start <= end <= completed_ns
                or type(response) is not list):
            raise ValueError("asset_catalog_response_invalid")
        members = []
        for item in response:
            row = _row(item)
            cls = _text(row.get("class", row.get("asset_class")))
            if ("class" in row and "asset_class" in row
                    and _text(row["class"]) != _text(row["asset_class"])):
                raise ValueError("asset_class_alias_conflict")
            status, symbol, identifier = _text(row.get("status")), row.get("symbol"), _text(row.get("id"))
            if cls != asset_class or status != "active":
                raise ValueError("asset_catalog_scope_mismatch")
            if type(symbol) is not str or not symbol or symbol != symbol.strip().upper():
                raise ValueError("asset_symbol_invalid")
            if type(identifier) is not str or str(UUID(identifier)) != identifier:
                raise ValueError("asset_identity_invalid")
            if type(row.get("tradable")) is not bool:
                raise ValueError("asset_tradability_unknown")
            key = (asset_class, symbol)
            if identifier in seen_ids or key in seen_keys:
                raise ValueError("asset_catalog_duplicate_identity")
            seen_ids.add(identifier)
            seen_keys.add(key)
            # No whole-share defaults, crypto alias rewriting or omitted metadata.
            # The later execution path must interpret the broker's asset rules.
            row.pop("asset_class", None)
            row.update({"id": identifier, "class": cls, "status": status})
            metadata = _json(row)
            listing = AssetListing(identifier, cls, symbol, status, row["tradable"], metadata)
            assets.append(listing)
            members.append(metadata)
        reads.append(CatalogRead(asset_class, start, end, len(response), _sha(_json(sorted(members)))))
    assets = tuple(sorted(assets, key=lambda a: a.key))
    content = _sha(_json([CONTRACT, account_hash, [a.metadata_json for a in assets]]))
    observation = _sha(_json([CONTRACT, content, started_ns, completed_ns,
        [[c.asset_class,c.started_ns,c.completed_ns,c.response_count,c.content_sha256] for c in reads]]))
    return AssetInventory(account_hash, started_ns, completed_ns, tuple(reads), assets, content, observation)


@dataclass(frozen=True)
class InventoryState:
    revision: int
    last_success: AssetInventory | None
    error: str | None

    @property
    def available(self):
        return self.last_success is not None and self.error is None


class InventoryBook:
    """Refresh failures preserve the complete last known catalog, marked stale.

    A successful empty catalog is distinct and can clear inventory membership.
    It cannot clear an independent HELD/PENDING source. Durable service ownership
    and publication remain the caller's responsibility.
    """
    def __init__(self, *, expected_account_id):
        self._lock = threading.Lock()
        self._account = alpaca_paper_account_identity_sha256(expected_account_id)
        self.state = InventoryState(0, None, "not_observed")

    def apply(self, probe):
        if type(probe) is not InventoryProbe:
            raise ValueError("inventory_probe_required")
        with self._lock:
            prior = self.state
            if probe.snapshot is not None:
                if probe.snapshot.account_identity_sha256 != self._account:
                    raise ValueError("inventory_book_account_mismatch")
                if prior.last_success is not None and probe.snapshot.started_ns < prior.last_success.completed_ns:
                    raise ValueError("inventory_observation_regression")
                self.state = InventoryState(prior.revision+1, probe.snapshot, None)
            else:
                self.state = InventoryState(prior.revision+1, prior.last_success, probe.error)
            return self.state
