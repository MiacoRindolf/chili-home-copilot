"""Atomic restart checkpoints for native PAPER observation demand.

This is retained membership, not a broker ledger, quote freshness certificate or
provider-symbol binding. No broker orders, schema creation at startup, background
threads, silent recovery reset, or mutation retry after an uncertain DB commit.
"""
from __future__ import annotations

from dataclasses import fields
import json
import threading

import sqlalchemy as sa

from . import broker_asset_inventory as assets
from . import broker_coverage_inventory as coverage
from . import broker_native_demand as native
from .alpaca_paper_identity import alpaca_paper_account_identity_sha256

CONTRACT = 'broker_native_demand_checkpoint_v1'
TYPES = {c.__name__: c for c in (
    assets.AssetListing, assets.CatalogRead, assets.AssetInventory, assets.InventoryState,
    coverage.CoverageMember, coverage.CoverageRead, coverage.CoverageProbe, coverage.CoverageState,
    native.NativeDemand, native.NativeDemandSnapshot,
)}


def _encode(value):
    if type(value) in (str, int, bool, type(None)):
        return value
    if type(value) is tuple:
        return {'tuple': [_encode(v) for v in value]}
    cls = TYPES.get(type(value).__name__)
    if cls is not None and type(value) is cls:
        return {'type': cls.__name__, 'fields': {f.name: _encode(getattr(value, f.name)) for f in fields(cls)}}
    raise ValueError('native_checkpoint_type_invalid')


def _decode(value):
    if type(value) in (str, int, bool, type(None)):
        return value
    if type(value) is not dict:
        raise ValueError('native_checkpoint_shape_invalid')
    if set(value) == {'tuple'} and type(value['tuple']) is list:
        return tuple(_decode(v) for v in value['tuple'])
    if set(value) == {'type', 'fields'} and type(value['type']) is str:
        cls, members = TYPES.get(value['type']), value['fields']
        if cls is not None and type(members) is dict and set(members) == {f.name for f in fields(cls)}:
            return cls(**{k: _decode(v) for k, v in members.items()})
    raise ValueError('native_checkpoint_tag_invalid')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('native_checkpoint_duplicate_json_key')
        result[key] = value
    return result


def _same(left, right):
    # Python equality treats 0/1 as False/True. A durable contract must preserve
    # their distinct types, including non-authority flags and source revisions.
    return assets._json(_encode(left)) == assets._json(_encode(right))


def _members(reason, members, expected):
    if type(members) is not tuple or any(type(m) is not coverage.CoverageMember for m in members):
        raise ValueError('native_checkpoint_members_invalid')
    rows = [json.loads(m.metadata_json, object_pairs_hook=_unique) for m in members]
    rebuilt = coverage.coverage_read(reason, started_ns=1, completed_ns=1,
                                    expected_account_id=expected, response=rows)
    # A retained union may exceed the API's single-response bound. Validate its
    # exact native records, not the union's completeness as a broker response.
    if not _same(rebuilt.members, members):
        raise ValueError('native_checkpoint_member_metadata_mismatch')


def _coverage_read(read, expected):
    if type(read) is not coverage.CoverageRead or type(read.bracket_reads) is not tuple:
        raise ValueError('native_checkpoint_coverage_read_invalid')
    if read.bracket_reads:
        if type(read.bracket_reads) is not tuple or len(read.bracket_reads) != 2:
            raise ValueError('native_checkpoint_bracket_invalid')
        for child in read.bracket_reads:
            if type(child) is not coverage.CoverageRead or child.bracket_reads:
                raise ValueError('native_checkpoint_nested_bracket')
            _coverage_read(child, expected)
        if not _same(coverage.bracket_pending_reads(*read.bracket_reads), read):
            raise ValueError('native_checkpoint_bracket_mismatch')
        return
    _members(read.reason, read.members, expected)
    if (read.reason not in coverage.REASONS or type(read.started_ns) is not int
            or type(read.completed_ns) is not int or not 0 < read.started_ns <= read.completed_ns
            or type(read.complete) is not bool or read.complete != (read.error is None)
            or read.error is not None and (type(read.error) is not str or not read.error)
            or read.response_count is None and (read.members or read.complete)
            or read.response_count is not None and (type(read.response_count) is not int
                or read.response_count < len(read.members))
            or read.complete and read.response_count != len(read.members)
            or read.reason == 'pending' and read.response_count is not None
                and read.response_count >= coverage.OPEN_ORDER_RESPONSE_LIMIT and read.complete):
        raise ValueError('native_checkpoint_coverage_read_metadata_invalid')
    digest = assets._sha(assets._json([read.reason, [m.metadata_json for m in read.members],
                                      read.response_count, read.error]))
    if digest != read.content_sha256:
        raise ValueError('native_checkpoint_coverage_read_digest_mismatch')


def validate_snapshot(snapshot, expected):
    account = alpaca_paper_account_identity_sha256(expected)
    if (type(snapshot) is not native.NativeDemandSnapshot or snapshot.account_identity_sha256 != account
            or type(snapshot.revision) is not int or snapshot.revision <= 0
            or type(snapshot.inventory) is not assets.InventoryState
            or type(snapshot.coverage) is not coverage.CoverageState
            or type(snapshot.inventory.revision) is not int or type(snapshot.coverage.revision) is not int
            or snapshot.inventory.revision != snapshot.revision
            or snapshot.coverage.revision != snapshot.revision):
        raise ValueError('native_checkpoint_identity_or_revision_invalid')
    inv = snapshot.inventory
    if inv.error is not None and (type(inv.error) is not str or not inv.error):
        raise ValueError('native_checkpoint_inventory_error_invalid')
    if inv.last_success is None:
        if inv.error is None:
            raise ValueError('native_checkpoint_inventory_missing')
    else:
        value = inv.last_success
        if type(value) is not assets.AssetInventory or value.account_identity_sha256 != account:
            raise ValueError('native_checkpoint_inventory_account_invalid')
        catalogs = {r.asset_class: (r.started_ns, r.completed_ns,
            [json.loads(a.metadata_json, object_pairs_hook=_unique)
             for a in value.assets if a.asset_class == r.asset_class]) for r in value.catalogs}
        rebuilt = assets.build_inventory(expected_account_id=expected, account_before=expected,
            account_after=expected, started_ns=value.started_ns, completed_ns=value.completed_ns,
            catalog_responses=catalogs)
        if not _same(rebuilt, value):
            raise ValueError('native_checkpoint_inventory_metadata_mismatch')
    state = snapshot.coverage
    probe = state.probe
    if type(probe) is not coverage.CoverageProbe:
        raise ValueError('native_checkpoint_coverage_probe_missing')
    for read in probe.reads:
        _coverage_read(read, expected)
    rebuilt = coverage.coverage_probe(expected_account_id=expected, started_ns=probe.started_ns,
                                    completed_ns=probe.completed_ns, reads=probe.reads)
    if not _same(rebuilt, probe):
        raise ValueError('native_checkpoint_coverage_probe_mismatch')
    if state.unavailable_reasons != tuple(r.reason for r in probe.reads if not r.complete):
        raise ValueError('native_checkpoint_coverage_availability_mismatch')
    for read in probe.reads:
        members = getattr(state, read.reason)
        _members(read.reason, members, expected)
        retained = {m.identity: m for m in members}
        if (any(retained.get(m.identity) != m for m in read.members)
                or state.membership_replacement_complete and members != read.members):
            raise ValueError('native_checkpoint_observed_members_missing')
    # Recompute native unions, alias sets, reasons, both hashes and false
    # authority flags. No caller-supplied composed member is trusted on restore.
    rebuilt = native._compose(account, snapshot.revision, snapshot.inventory, snapshot.coverage, None)
    if not _same(rebuilt, snapshot):
        raise ValueError('native_checkpoint_composed_snapshot_mismatch')
    return snapshot


def encode_snapshot(snapshot, *, expected_account_id):
    validate_snapshot(snapshot, expected_account_id)
    return assets._json({'contract': CONTRACT, 'snapshot': _encode(snapshot)})


def decode_snapshot(payload, *, expected_account_id):
    envelope = json.loads(payload, object_pairs_hook=_unique)
    if type(envelope) is not dict or set(envelope) != {'contract', 'snapshot'} or envelope['contract'] != CONTRACT:
        raise ValueError('native_checkpoint_contract_invalid')
    result = _decode(envelope['snapshot'])
    if encode_snapshot(result, expected_account_id=expected_account_id) != payload:
        raise ValueError('native_checkpoint_not_canonical')
    return result


def create_schema(c):
    """Migration/test entry point only; ordinary open/refresh never creates tables."""
    c.execute(sa.text('''CREATE TABLE IF NOT EXISTS momentum_native_demand_checkpoints (
        account_identity_sha256 TEXT PRIMARY KEY,
        revision BIGINT NOT NULL CHECK(revision>0),
        payload_sha256 TEXT NOT NULL, payload TEXT NOT NULL)'''))


class DurableBrokerNativeDemandService:
    """Stage in private, publish memory only after the atomic checkpoint commits.

    Optimistic CAS fences concurrent refreshes, including concurrent first writes.
    An uncertain DB result makes this instance unusable until a new open reads the
    authoritative checkpoint. The latest checkpoint is not historical event replay.
    """
    @classmethod
    def open(cls, engine, *, expected_account_id, max_payload_bytes):
        if type(max_payload_bytes) is not int or max_payload_bytes <= 0:
            raise ValueError('native_checkpoint_capacity_invalid')
        self = cls()
        self._engine, self._expected = engine, expected_account_id
        self._account = alpaca_paper_account_identity_sha256(expected_account_id)
        self._max_bytes, self._lock, self._failed = max_payload_bytes, threading.RLock(), False
        self._snapshot, self._digest = None, None
        with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
            c.execute(sa.text('SET TRANSACTION READ ONLY'))
            c.execute(sa.text("SET LOCAL statement_timeout='20s'"))
            params = {'account': self._account}
            row = c.execute(sa.text('''SELECT revision,payload_sha256,octet_length(payload) AS bytes
                FROM momentum_native_demand_checkpoints WHERE account_identity_sha256=:account'''), params).mappings().one_or_none()
            if row is not None:
                if row['bytes'] > max_payload_bytes:
                    raise ValueError('native_checkpoint_byte_capacity')
                payload = c.execute(sa.text('''SELECT payload FROM momentum_native_demand_checkpoints
                    WHERE account_identity_sha256=:account'''), params).scalar_one()
                if assets._sha(payload) != row['payload_sha256']:
                    raise ValueError('native_checkpoint_digest_mismatch')
                self._snapshot = decode_snapshot(payload, expected_account_id=expected_account_id)
                if self._snapshot.revision != row['revision']:
                    raise ValueError('native_checkpoint_stored_revision_mismatch')
                self._digest = row['payload_sha256']
        return self

    def read(self):
        with self._lock:
            if self._failed:
                raise ValueError('native_checkpoint_reopen_required')
            return self._snapshot

    def _persist(self, snapshot):
        payload = encode_snapshot(snapshot, expected_account_id=self._expected)
        if len(payload.encode()) > self._max_bytes:
            raise ValueError('native_checkpoint_byte_capacity')
        digest = assets._sha(payload)
        params = dict(account=self._account, revision=snapshot.revision, payload=payload, digest=digest)
        try:
            with self._engine.begin() as c:
                c.execute(sa.text("SET LOCAL statement_timeout='20s'"))
                if self._snapshot is None:
                    updated = c.execute(sa.text('''INSERT INTO momentum_native_demand_checkpoints
                        (account_identity_sha256,revision,payload_sha256,payload)
                        VALUES(:account,:revision,:digest,:payload)
                        ON CONFLICT(account_identity_sha256) DO NOTHING RETURNING revision'''), params).scalar_one_or_none()
                else:
                    params.update(prior=self._snapshot.revision, prior_digest=self._digest)
                    updated = c.execute(sa.text('''UPDATE momentum_native_demand_checkpoints
                        SET revision=:revision,payload_sha256=:digest,payload=:payload
                        WHERE account_identity_sha256=:account AND revision=:prior
                        AND payload_sha256=:prior_digest RETURNING revision'''), params).scalar_one_or_none()
                if updated != snapshot.revision:
                    raise ValueError('native_checkpoint_concurrent_refresh')
            self._snapshot, self._digest = snapshot, digest
            return snapshot
        except BaseException:
            self._failed = True
            raise

    def apply(self, inventory_probe, exposure_probe):
        with self._lock:
            staged = native.BrokerNativeDemandService(expected_account_id=self._expected, initial=self.read())
            return self._persist(staged.apply(inventory_probe, exposure_probe))

    def refresh(self, adapter):
        with self._lock:
            staged = native.BrokerNativeDemandService(expected_account_id=self._expected, initial=self.read())
            return self._persist(staged.refresh(adapter))
