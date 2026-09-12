"""Changed-symbol publications with revision-bound materialization.

Each output binds a complete membership manifest and the shared source header.
Only changed immutable symbol values are serialized/stored again. Current subset
reads verify the complete compact manifest before fetching requested symbol values;
historical/event reads reconstruct every publication without re-reducing ticks.
"""
from dataclasses import dataclass, replace
import json
from types import MappingProxyType

import sqlalchemy as sa

from . import structural_context_journal as j

CONTRACT = 'ordinary_changed_symbol_publication_v1'


@dataclass(frozen=True)
class State:
    snapshot: object
    views: object
    payloads: object
    descriptors: object
    state_sha256: str
    payload: str
    changed: tuple


def descriptor(view, digest):
    native = view.native_identity
    return (view.symbol, digest, native.asset_id if native else None,
            native.broker_symbols if native else (), view.native_binding_current)


def state_digest(header, descriptors):
    # This compact complete membership hash is O(symbols), not O(all wave/tick
    # payloads). It proves omitted/extra/changed index rows without calling them
    # a successful partial universe. No probabilistic/XOR membership digest.
    return j._sha(j._json([CONTRACT, j._sha(j.encode_snapshot(header)),
                          tuple(descriptors[k] for k in sorted(descriptors))]))


def prepare(snapshot, previous=None):
    header = replace(snapshot, symbols=())
    j._validate(header)
    if type(snapshot.symbols) is not tuple or any(type(v) is not j.SymbolView for v in snapshot.symbols):
        raise ValueError('context_delta_symbol_membership_invalid')
    names = tuple(v.symbol for v in snapshot.symbols)
    if names != tuple(sorted(set(names))):
        raise ValueError('context_delta_symbol_membership_invalid')
    if previous is not None and not previous.views.keys() <= set(names):
        raise ValueError('context_delta_symbol_retirement_requires_reconstruction')
    views, payloads, descriptors, changed = {}, {}, {}, []
    for view in snapshot.symbols:
        name = view.symbol
        if previous is not None and view == previous.views.get(name):
            payloads[name] = previous.payloads[name]
            descriptors[name] = previous.descriptors[name]
        else:
            payloads[name] = j._json(j._encode(view))
            descriptors[name] = descriptor(view, j._sha(payloads[name]))
            changed.append(view)
        views[name] = view
    j._validate(replace(header, symbols=tuple(changed)))
    current_ids = [d[2] for d in descriptors.values() if d[4]]
    if (len(current_ids) != len(set(current_ids)) or
            set(current_ids) & {g.asset_id for g in header.native_mapping_gaps} or
            current_ids and (header.native_enrollment is None or header.native_enrollment.catalog_sha256 is None)):
        raise ValueError('context_current_native_binding_conflict')
    digest = state_digest(header, descriptors)
    payload = j._json(dict(contract=CONTRACT,
        base_state_sha256=previous.state_sha256 if previous else None,
        state_sha256=digest, header=j._encode(header), changed=j._encode(tuple(changed))))
    return State(snapshot, MappingProxyType(views), MappingProxyType(payloads),
                 MappingProxyType(descriptors), digest, payload, tuple(changed))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('context_delta_duplicate_json_key')
        result[key] = value
    return result


def decode_wire(payload):
    value = json.loads(payload, object_pairs_hook=_unique)
    if (type(value) is not dict or set(value) != {'contract', 'base_state_sha256', 'state_sha256', 'header', 'changed'}
            or value['contract'] != CONTRACT or not j._digest(value['state_sha256'])
            or value['base_state_sha256'] is not None and not j._digest(value['base_state_sha256'])
            or j._json(value) != payload):
        raise ValueError('context_delta_wire_invalid')
    header, changed = j._decode(value['header']), j._decode(value['changed'])
    j._validate(header)
    if (header.symbols or type(changed) is not tuple or
            any(type(v) is not j.SymbolView for v in changed) or
            tuple(v.symbol for v in changed) != tuple(sorted({v.symbol for v in changed}))):
        raise ValueError('context_delta_changed_membership_invalid')
    j._validate(replace(header, symbols=changed))
    return value, header, changed


def apply(payload, previous=None):
    wire, header, changed = decode_wire(payload)
    if wire['base_state_sha256'] != (previous.state_sha256 if previous else None):
        raise ValueError('context_delta_base_state_mismatch')
    views = dict(previous.views) if previous else {}
    views.update((view.symbol, view) for view in changed)
    state = prepare(replace(header, symbols=tuple(views[k] for k in sorted(views))), previous)
    if state.payload != payload:
        raise ValueError('context_delta_recomputed_state_mismatch')
    return state


def create_schema(c):
    c.execute(sa.text('''CREATE TABLE IF NOT EXISTS momentum_structural_context_objects (
        stream_id TEXT NOT NULL, generation TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(stream_id,generation,payload_sha256))'''))
    for table, primary in (('momentum_structural_context_members', 'stream_id,generation,symbol'),
                           ('momentum_structural_context_versions', 'stream_id,generation,symbol,revision')):
        c.execute(sa.text('CREATE TABLE IF NOT EXISTS '+table+''' (
            stream_id TEXT NOT NULL, generation TEXT NOT NULL, symbol TEXT NOT NULL,
            revision BIGINT NOT NULL CHECK(revision>0), payload_sha256 TEXT NOT NULL,
            descriptor TEXT NOT NULL, PRIMARY KEY('''+primary+'))'))


def persist(c, cursor, state):
    if not state.changed:
        return
    # One bounded publication transaction, not one network round trip per symbol.
    # PostgreSQL arrays preserve all changed rows; there is no strategy batch cap.
    values = dict(s=cursor.stream_id, g=cursor.generation, r=cursor.revision,
        symbols=[v.symbol for v in state.changed], hashes=[state.descriptors[v.symbol][1] for v in state.changed],
        descriptors=[j._json(state.descriptors[v.symbol]) for v in state.changed],
        payloads=[state.payloads[v.symbol] for v in state.changed])
    c.execute(sa.text('''INSERT INTO momentum_structural_context_objects
        (stream_id,generation,payload_sha256,payload)
        SELECT :s,:g,v.sha,v.payload FROM unnest(CAST(:hashes AS TEXT[]),CAST(:payloads AS TEXT[])) AS v(sha,payload)
        ON CONFLICT DO NOTHING'''), values)
    c.execute(sa.text('''INSERT INTO momentum_structural_context_versions
        (stream_id,generation,symbol,revision,payload_sha256,descriptor)
        SELECT :s,:g,v.symbol,:r,v.sha,v.descriptor FROM unnest(CAST(:symbols AS TEXT[]),
            CAST(:hashes AS TEXT[]),CAST(:descriptors AS TEXT[])) AS v(symbol,sha,descriptor)'''), values)
    c.execute(sa.text('''INSERT INTO momentum_structural_context_members
        (stream_id,generation,symbol,revision,payload_sha256,descriptor)
        SELECT :s,:g,v.symbol,:r,v.sha,v.descriptor FROM unnest(CAST(:symbols AS TEXT[]),
            CAST(:hashes AS TEXT[]),CAST(:descriptors AS TEXT[])) AS v(symbol,sha,descriptor)
        ON CONFLICT(stream_id,generation,symbol) DO UPDATE SET revision=EXCLUDED.revision,
        payload_sha256=EXCLUDED.payload_sha256,descriptor=EXCLUDED.descriptor'''), values)


def materialize(c, cursor, *, payload, max_payload_bytes, selected_symbols=None):
    wire, header, _ = decode_wire(payload)
    j._validate_native_stream(header, cursor.stream_id)
    params = dict(s=cursor.stream_id, g=cursor.generation, r=cursor.revision)
    head = j._head(c, cursor.stream_id)
    if head['generation'] != cursor.generation or head['revision'] < cursor.revision:
        raise ValueError('context_materialization_generation_mismatch')
    if head['revision'] == cursor.revision:
        query = '''SELECT symbol,revision,payload_sha256,descriptor
            FROM momentum_structural_context_members WHERE stream_id=:s AND generation=:g ORDER BY symbol'''
    else:
        query = '''SELECT DISTINCT ON(symbol) symbol,revision,payload_sha256,descriptor
            FROM momentum_structural_context_versions WHERE stream_id=:s AND generation=:g AND revision<=:r
            ORDER BY symbol,revision DESC'''
    # Bound metadata allocation before loading any text. Never return a subset
    # when the complete manifest cannot be verified within the caller's budget.
    size = c.execute(sa.text('SELECT coalesce(sum(octet_length(descriptor)),0) FROM ('+query+') AS index_rows'), params).scalar_one()
    if size > max_payload_bytes:
        raise ValueError('atomic_context_manifest_exceeds_read_byte_capacity')
    rows = c.execute(sa.text(query), params).mappings().all()
    descriptors = {}
    for row in rows:
        d = json.loads(row['descriptor'], object_pairs_hook=_unique)
        if (type(d) is not list or len(d) != 5 or type(d[0]) is not str or
                not j._digest(d[1]) or type(d[3]) is not list or any(type(a) is not str for a in d[3])
                or d[2] is not None and type(d[2]) is not str or type(d[4]) is not bool
                or d[0] != row['symbol'] or d[1] != row['payload_sha256']
                or not 0 < row['revision'] <= cursor.revision or j._json(d) != row['descriptor']):
            raise ValueError('context_materialized_descriptor_invalid')
        descriptors[d[0]] = (d[0], d[1], d[2], tuple(d[3]), d[4])
    if state_digest(header, descriptors) != wire['state_sha256']:
        raise ValueError('context_materialized_membership_digest_mismatch')
    names = set(selected_symbols) if selected_symbols is not None else None
    selected = [d for d in descriptors.values() if names is None or
                (d[2] in names or bool(names & set(d[3])) if header.native_enrollment is not None else d[0] in names)]
    hashes = list({d[1] for d in selected})
    objects = {}
    if hashes:
        q = sa.text('''SELECT payload_sha256,octet_length(payload) AS bytes
            FROM momentum_structural_context_objects WHERE stream_id=:s AND generation=:g
            AND payload_sha256 IN :hashes''').bindparams(sa.bindparam('hashes', expanding=True))
        found = c.execute(q, dict(params, hashes=hashes)).mappings().all()
        if {r['payload_sha256'] for r in found} != set(hashes):
            raise ValueError('context_materialized_object_retention_gap')
        if sum(r['bytes'] for r in found) > max_payload_bytes:
            raise ValueError('atomic_context_objects_exceed_read_byte_capacity')
        q = sa.text('''SELECT payload_sha256,payload FROM momentum_structural_context_objects
            WHERE stream_id=:s AND generation=:g AND payload_sha256 IN :hashes''').bindparams(sa.bindparam('hashes', expanding=True))
        objects = {r['payload_sha256']: r['payload'] for r in c.execute(q, dict(params, hashes=hashes)).mappings()}
    views, payloads = {}, {}
    for d in selected:
        encoded = objects[d[1]]
        if j._sha(encoded) != d[1]:
            raise ValueError('context_materialized_object_digest_mismatch')
        view = j._decode(json.loads(encoded, object_pairs_hook=_unique))
        if (type(view) is not j.SymbolView or j._json(j._encode(view)) != encoded or descriptor(view, d[1]) != d):
            raise ValueError('context_materialized_object_descriptor_mismatch')
        views[view.symbol], payloads[view.symbol] = view, encoded
    snapshot = replace(header, symbols=tuple(views[k] for k in sorted(views)))
    j._validate(snapshot)
    # A subset is a current observation projection, never a full event/recovery
    # state. Callers must not apply deltas to it or acknowledge historical events.
    return State(snapshot, MappingProxyType(views), MappingProxyType(payloads),
        MappingProxyType(descriptors), wire['state_sha256'], payload, ())
