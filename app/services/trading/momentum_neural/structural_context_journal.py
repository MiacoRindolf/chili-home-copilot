"""Durable immutable shared observations and explicit consumer catch-up.

One session-lock-fenced writer; consumers read the published calculations rather
than re-reducing tape. No broker authority, source authentication or automatic
warm owner recovery is inferred from this journal. Schema creation is migration-
only. The producer must reconstruct before reopening an existing stream.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
import math
from uuid import uuid4

import sqlalchemy as sa

from scripts.iqfeed_print_publications import Cursor
from .ordinary_structural_context import ContextSnapshot, ScopeView, SymbolView
from .structural_tape_prefix import Tick, Reference, StructuralEvent, MASS_FIELDS
from app.tick_math.wave_context import WaveTurn, WavePair, WaveParent, WavePhase, WaveContext, phase, number
from app.tick_math.wave_evidence import WaveInterval, WaveEvidence
from .native_tick_enrollment import (NativeEquityIdentity, NativeEnrollmentReference,
    NativeMappingGap, validate_identity, validate_reference, validate_gap)


CONTRACT = "ordinary_shared_context_publication_v5"
LOCK_NAMESPACE = "chili.ordinary.shared.context.v1"
CHANNEL = "momentum_structural_context"
TYPES = {c.__name__: c for c in (Cursor, Tick, Reference, StructuralEvent,
                               ScopeView, SymbolView, ContextSnapshot,
                               WaveTurn, WavePair, WaveParent, WavePhase, WaveContext,
                               WaveInterval, WaveEvidence, NativeEquityIdentity,
                               NativeEnrollmentReference, NativeMappingGap)}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _digest(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _encode(value):
    if type(value) in (str, int, bool, type(None)):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is Decimal and value.is_finite():
        return {'decimal': format(value, 'f')}
    if type(value) is Fraction:
        return {"fraction": [str(value.numerator), str(value.denominator)]}
    if type(value) is tuple:
        return {"tuple": [_encode(v) for v in value]}
    if type(value).__name__ in TYPES and type(value) is TYPES[type(value).__name__]:
        return {"type": type(value).__name__, "fields": {
            f.name: _encode(getattr(value, f.name)) for f in fields(value)}}
    raise ValueError("context_value_not_serializable")


def _decode(value):
    if type(value) in (str, int, bool, type(None)):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is not dict:
        raise ValueError("context_payload_shape_invalid")
    if set(value) == {'decimal'}:
        if type(value['decimal']) is not str:
            raise ValueError('context_decimal_invalid')
        try:
            exact = Decimal(value['decimal'])
        except InvalidOperation:
            raise ValueError('context_decimal_invalid') from None
        if not exact.is_finite() or _encode(exact) != value:
            raise ValueError('context_decimal_not_canonical')
        return exact
    if set(value) == {"tuple"} and type(value["tuple"]) is list:
        return tuple(_decode(v) for v in value["tuple"])
    if set(value) == {"fraction"}:
        pair = value["fraction"]
        if type(pair) is not list or len(pair) != 2 or any(type(v) is not str for v in pair):
            raise ValueError("context_fraction_invalid")
        try:
            number = Fraction(int(pair[0]), int(pair[1]))
        except (ValueError, ZeroDivisionError):
            raise ValueError("context_fraction_invalid") from None
        if _encode(number) != value:
            raise ValueError("context_fraction_not_canonical")
        return number
    if set(value) == {"type", "fields"} and type(value["type"]) is str:
        cls = TYPES.get(value["type"])
        members = value["fields"]
        if cls is None or type(members) is not dict or set(members) != {f.name for f in fields(cls)}:
            raise ValueError("context_record_shape_invalid")
        return cls(**{key: _decode(v) for key, v in members.items()})
    raise ValueError("context_payload_tag_invalid")


def _validate(snapshot):
    if type(snapshot) is not ContextSnapshot or snapshot.order_authority is not False \
            or snapshot.provider_completeness_certified is not False:
        raise ValueError("observation_context_required")
    if (snapshot.source_observed_ns is not None and
            (type(snapshot.source_observed_ns) is not int or snapshot.source_observed_ns <= 0)
            or snapshot.status == 'observed_prefix' and snapshot.source_observed_ns is None):
        raise ValueError('context_source_observation_clock_invalid')
    for cursor in (snapshot.source, snapshot.observed_frontier):
        if (type(cursor) is not Cursor or type(cursor.epoch) is not str or not cursor.epoch
                or type(cursor.revision) is not int or cursor.revision < 0):
            raise ValueError("context_source_cursor_invalid")
    if (snapshot.source.epoch != snapshot.observed_frontier.epoch
            or snapshot.source.revision > snapshot.observed_frontier.revision
            or not _digest(snapshot.root_sha256)
            or type(snapshot.status) is not str
            or snapshot.status not in {"cold", "observed_prefix", "unresolved"}
            or type(snapshot.symbols) is not tuple
            or type(snapshot.stale_demand_sources) is not tuple
            or (snapshot.status == "unresolved" and (type(snapshot.reason) is not str or not snapshot.reason))
            or (snapshot.status != "unresolved" and snapshot.reason is not None)):
        raise ValueError("context_snapshot_invalid")
    def reasons(values):
        return type(values) is tuple and all(type(v) is str for v in values) \
            and list(values) == sorted(set(values)) \
            and set(values) <= {"inventory", "ranking", "held", "pending", "watch"}
    if not reasons(snapshot.stale_demand_sources):
        raise ValueError("context_demand_reasons_invalid")
    if type(snapshot.native_mapping_gaps) is not tuple:
        raise ValueError('context_native_mapping_gaps_invalid')
    if snapshot.native_enrollment is not None:
        validate_reference(snapshot.native_enrollment)
    elif snapshot.native_mapping_gaps:
        raise ValueError('context_native_enrollment_missing')
    for gap in snapshot.native_mapping_gaps:
        validate_gap(gap)
    if [g.asset_id for g in snapshot.native_mapping_gaps] != sorted({g.asset_id for g in snapshot.native_mapping_gaps}):
        raise ValueError('context_native_mapping_gap_duplicate')
    names, current_native_ids = [], set()
    gap_ids = {g.asset_id for g in snapshot.native_mapping_gaps}
    for view in snapshot.symbols:
        if (type(view) is not SymbolView or type(view.symbol) is not str or not view.symbol
                or not _digest(view.prefix_sha256) or type(view.print_count) is not int
                or view.print_count < 0 or type(view.scopes) is not tuple or type(view.events) is not tuple
                or not reasons(view.demand_reasons)
                or (view.last_print is None) != (view.print_count == 0)
                or view.last_print is not None and type(view.last_print) is not Tick
                or any(type(s) is not ScopeView or type(s.reference) is not Reference for s in view.scopes)
                or any(type(e) is not StructuralEvent or type(e.reference) is not Reference for e in view.events)):
            raise ValueError("context_symbol_view_invalid")
        names.append(view.symbol)
        if type(view.native_binding_current) is not bool:
            raise ValueError('context_native_binding_status_invalid')
        if view.native_identity is not None:
            validate_identity(view.native_identity)
            if snapshot.native_enrollment is None or view.native_identity.provider_symbol != view.symbol:
                raise ValueError('context_native_binding_symbol_mismatch')
            if view.native_binding_current:
                identity = view.native_identity.asset_id
                if (snapshot.native_enrollment.catalog_sha256 is None
                        or identity in current_native_ids or identity in gap_ids):
                    raise ValueError('context_current_native_binding_conflict')
                current_native_ids.add(identity)
        elif view.native_binding_current:
            raise ValueError('context_native_identity_missing')
        if (view.history_before_anchor != "unknown"
                or view.quote_freshness != "not_certified_by_trade_row"
                or view.prefix_basis != 'nonempty_symbol_publications; coverage=context_source'):
            raise ValueError("context_v2_evidence_claim_invalid")
        _validate_wave(view)
        _validate_wave_evidence(view)
        def reference(ref):
            indices = (ref.origin_index, ref.confirmation_index, ref.plateau_first_index)
            ids = (ref.origin_id, ref.confirmation_id, ref.plateau_first_id)
            return (ref.kind in {"peak", "valley"} and ref.stream_key == "iqfeed:"+view.symbol
                and type(ref.segment_key) is str and bool(ref.segment_key)
                and view.last_print is not None and ref.epoch == view.last_print.epoch
                and all(type(i) is int for i in indices)
                and 0 <= ref.plateau_first_index <= ref.origin_index < ref.confirmation_index < view.print_count
                and all(type(i) is int and i > 0 for i in ids))
        def mass(value):
            return type(value) is tuple and len(value) == len(MASS_FIELDS) \
                and all(type(v) is Fraction and v >= 0 for v in value)
        for scope in view.scopes:
            bounds = scope.phase_bounds
            if (not reference(scope.reference) or scope.end_index != view.print_count-1
                    or type(bounds) is not tuple or len(bounds) != 3
                    or any(type(b) is not tuple or len(b) != 2
                           or any(type(i) is not int for i in b)
                           or not 0 <= b[0] <= b[1] <= scope.end_index for b in bounds)
                    or bounds[0][0] != scope.reference.origin_index or bounds[-1][1] != scope.end_index
                    or bounds[0][1] != bounds[1][0] or bounds[1][1] != bounds[2][0]
                    or not mass(scope.whole_mass) or type(scope.phase_mass) is not tuple
                    or len(scope.phase_mass) != 3 or not all(mass(v) for v in scope.phase_mass)
                    or tuple(sum(values) for values in zip(*scope.phase_mass)) != scope.whole_mass):
                raise ValueError("context_scope_arithmetic_invalid")
        prior_index = -1
        for event in view.events:
            if (event.kind not in {"born", "breached"} or not reference(event.reference)
                    or type(event.at_index) is not int or not prior_index <= event.at_index < view.print_count
                    or event.at_index < event.reference.confirmation_index
                    or type(event.at_id) is not int or event.at_id <= 0):
                raise ValueError("context_structural_event_invalid")
            prior_index = event.at_index
    if names != sorted(set(names)):
        raise ValueError("context_symbol_membership_invalid")


def _validate_wave(view):
    wave = view.wave_context
    if not view.print_count:
        if wave is not None or view.selected_parent_local != 'not_yet_derived':
            raise ValueError('empty_context_wave_invalid')
        return
    if (type(wave) is not WaveContext or wave.order_authority is not False
            or wave.definition != 'quote_local_minimal_enclosing_raw_parent_candidate_v1'
            or view.selected_parent_local != 'candidate_geometry'
            or wave.end_index != view.print_count-1 or wave.end_id != view.last_print.id
            or type(wave.events) is not tuple or type(wave.minimal_parents) is not tuple):
        raise ValueError('context_wave_evidence_invalid')
    def turn(t, basis=None, kind=None):
        return (type(t) is WaveTurn and t.basis in {'raw_recursive', 'quote_resolved'}
            and (basis is None or t.basis == basis) and t.kind in {'peak', 'valley'}
            and (kind is None or t.kind == kind) and type(t.order) is int and t.order > 0
            and (t.basis != 'quote_resolved' or t.order == 1)
            and type(t.origin_index) is int and type(t.confirmation_index) is int
            and 0 <= t.origin_index < t.confirmation_index <= wave.end_index
            and type(t.origin_id) is int and t.origin_id > 0
            and type(t.confirmation_id) is int and t.confirmation_id > 0
            and type(t.price) is Fraction and t.price > 0)
    for t, kind in ((wave.local_peak, 'peak'), (wave.local_valley, 'valley')):
        if t is not None and not turn(t, 'quote_resolved', kind):
            raise ValueError('context_local_wave_invalid')
    last = -1
    for t in wave.events:
        if not turn(t) or t.confirmation_index < last:
            raise ValueError('context_wave_event_invalid')
        last = t.confirmation_index
    if wave.local_phase != phase(wave.local_peak, wave.local_valley, number(view.last_print.price)):
        raise ValueError('context_local_phase_invalid')
    usable = (wave.local_peak is not None and wave.local_valley is not None
              and wave.local_valley.price < wave.local_peak.price)
    if usable:
        if (type(wave.local_path_low) is not Fraction or type(wave.local_path_high) is not Fraction
                or not 0 < wave.local_path_low <= wave.local_valley.price < wave.local_peak.price <= wave.local_path_high):
            raise ValueError('context_local_envelope_invalid')
    elif wave.local_path_low is not None or wave.local_path_high is not None or wave.minimal_parents:
        raise ValueError('context_unavailable_local_envelope_invalid')
    for parent in wave.minimal_parents:
        if type(parent) is not WaveParent or type(parent.aliases) is not tuple or not parent.aliases:
            raise ValueError('context_parent_wave_invalid')
        for pair in parent.aliases:
            if (type(pair) is not WavePair or not turn(pair.peak, 'raw_recursive', 'peak')
                    or not turn(pair.valley, 'raw_recursive', 'valley')
                    or pair.peak.order != pair.valley.order
                    or parent.root_index != min(pair.peak.origin_index, pair.valley.origin_index)
                    or parent.low != pair.valley.price or parent.high != pair.peak.price):
                raise ValueError('context_parent_alias_invalid')
        if (not usable or parent.root_index >= min(wave.local_peak.origin_index, wave.local_valley.origin_index)
                or parent.low > wave.local_path_low or parent.high < wave.local_path_high
                or (parent.low == wave.local_path_low and parent.high == wave.local_path_high)):
            raise ValueError('context_parent_enclosure_invalid')
    status = ('local_unavailable' if not usable else 'unique' if len(wave.minimal_parents) == 1
              else 'ambiguous' if wave.minimal_parents else 'unavailable')
    if wave.parent_status != status:
        raise ValueError('context_parent_status_invalid')
    pair = wave.minimal_parents[0].aliases[0] if status == 'unique' else None
    expected = phase(pair.peak, pair.valley, number(view.last_print.price)) if pair else WavePhase('unknown', status)
    if wave.parent_phase != expected:
        raise ValueError('context_parent_phase_invalid')


def _validate_wave_evidence(view):
    wave = view.wave_context
    expected = set()
    if wave is not None:
        expected.update(t for t in (wave.local_peak, wave.local_valley) if t is not None)
        for parent in wave.minimal_parents:
            for pair in parent.aliases:
                expected.update((pair.peak, pair.valley))
    values = view.wave_evidence
    if type(values) is not tuple or any(type(v) is not WaveEvidence for v in values):
        raise ValueError('context_wave_evidence_shape_invalid')
    if len(values) != len(expected) or {v.reference for v in values} != expected:
        raise ValueError('context_wave_evidence_membership_invalid')
    for value in values:
        if (value.order_authority is not False
                or value.bound_assumption != 'inside_quote_midpoint_signs_correct'
                or value.quote_freshness != 'not_certified_by_trade_row'):
            raise ValueError('context_wave_evidence_claim_invalid')
        ref = value.reference
        a, b, whole = value.formation, value.follow_through, value.whole
        for span, start, end, first_id, last_id in (
            (a, ref.origin_index, ref.confirmation_index, ref.origin_id, ref.confirmation_id),
            (b, ref.confirmation_index, wave.end_index, ref.confirmation_id, wave.end_id),
            (whole, ref.origin_index, wave.end_index, ref.origin_id, wave.end_id),
        ):
            if (type(span) is not WaveInterval or type(span.print_count) is not int
                    or (span.start_index, span.end_index, span.start_id, span.end_id, span.print_count)
                    != (start, end, first_id, last_id, end-start)
                    or type(span.mass) is not tuple or len(span.mass) != len(MASS_FIELDS)
                    or any(type(v) is not Fraction or v < 0 for v in span.mass)
                    or type(span.price_change) is not Fraction
                    or any(x is not None and type(x) is not Fraction for x in (span.bid_change, span.ask_change))):
                raise ValueError('context_wave_interval_invalid')
            mass = span.mass
            quoted = span.quote_mass
            if (type(quoted) is not tuple or len(quoted) != 3
                    or any(type(v) is not Fraction or v < 0 for v in quoted) or sum(quoted) != mass[0]):
                raise ValueError('context_wave_quote_mass_invalid')
            unknown = quoted[2]
            net = quoted[0]-quoted[1]
            if (mass[0] != sum(mass[1:4]) or mass[1] != mass[4]+mass[6] or mass[2] != mass[5]+mass[7]
                    or unknown < 0 or span.quote_unresolved_volume != unknown
                    or type(span.quote_unresolved_volume) is not Fraction
                    or span.conditional_quote_net_bounds != (net-unknown, net+unknown)
                    or type(span.conditional_quote_net_bounds) is not tuple
                    or any(type(x) is not Fraction for x in span.conditional_quote_net_bounds)
                    or bool(mass[0]) != bool(end-start)):
                raise ValueError('context_wave_interval_mass_invalid')
            if not span.print_count and (span.price_change or any(x not in (None, 0) for x in (span.bid_change, span.ask_change))):
                raise ValueError('context_empty_wave_interval_invalid')
        if (tuple(x+y for x, y in zip(a.mass, b.mass)) != whole.mass
                or tuple(x+y for x,y in zip(a.quote_mass, b.quote_mass)) != whole.quote_mass
                or a.price_change+b.price_change != whole.price_change
                or whole.price_change != number(view.last_print.price)-ref.price):
            raise ValueError('context_wave_partition_invalid')
        for field in ('bid_change', 'ask_change'):
            left, right, total = (getattr(x, field) for x in (a, b, whole))
            if left is not None and right is not None and left+right != total:
                raise ValueError('context_wave_quote_partition_invalid')


def encode_snapshot(snapshot):
    _validate(snapshot)
    return _json(_encode(snapshot))


def decode_snapshot(payload):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("context_duplicate_json_key")
            result[key] = value
        return result
    value = json.loads(payload, object_pairs_hook=unique)
    snapshot = _decode(value)
    _validate(snapshot)
    if encode_snapshot(snapshot) != payload:
        raise ValueError("context_payload_not_canonical")
    return snapshot


def create_schema(c):
    for statement in (
        """CREATE TABLE IF NOT EXISTS momentum_structural_context_heads (
            stream_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
            revision BIGINT NOT NULL CHECK(revision>=0), root_sha256 TEXT NOT NULL,
            anchor_sha256 TEXT NOT NULL, source_epoch TEXT NOT NULL,
            source_revision BIGINT NOT NULL CHECK(source_revision>=0), payload_sha256 TEXT)""",
        """CREATE TABLE IF NOT EXISTS momentum_structural_context_publications (
            stream_id TEXT NOT NULL, generation TEXT NOT NULL,
            revision BIGINT NOT NULL CHECK(revision>0), previous_sha256 TEXT NOT NULL,
            root_sha256 TEXT NOT NULL, payload_sha256 TEXT NOT NULL, payload TEXT NOT NULL,
            source_advanced BOOLEAN NOT NULL,
            PRIMARY KEY(stream_id,generation,revision))""",
        """CREATE TABLE IF NOT EXISTS momentum_structural_context_consumers (
            stream_id TEXT NOT NULL, generation TEXT NOT NULL, consumer TEXT NOT NULL,
            revision BIGINT NOT NULL CHECK(revision>=0), root_sha256 TEXT NOT NULL,
            PRIMARY KEY(stream_id,generation,consumer))""",
    ):
        c.execute(sa.text(statement))
    from .structural_context_delta import create_schema as create_delta_schema
    create_delta_schema(c)


@dataclass(frozen=True)
class JournalCursor:
    stream_id: str
    generation: str
    revision: int
    root_sha256: str


@dataclass(frozen=True)
class ContextPublication:
    cursor: JournalCursor
    snapshot: ContextSnapshot
    source_advanced: bool
    payload_sha256: str | None = None
    state_sha256: str | None = None
    projected: bool = False
    _state: object = field(default=None, repr=False, compare=False)

    @property
    def new_events(self):
        # Demand/status publications may repeat the latest source snapshot.
        # Those are not a second occurrence of its structural events.
        if self.projected:
            raise ValueError('context_projection_has_no_event_delivery')
        return tuple((v.symbol, v.events) for v in self.snapshot.symbols if v.events) \
            if self.source_advanced else ()

    @property
    def new_wave_events(self):
        if self.projected:
            raise ValueError('context_projection_has_no_event_delivery')
        return tuple((v.symbol, v.wave_context.events) for v in self.snapshot.symbols
                     if v.wave_context is not None and v.wave_context.events) if self.source_advanced else ()


@dataclass(frozen=True)
class ContextRead:
    after: JournalCursor
    observed_head: JournalCursor
    publications: tuple[ContextPublication, ...]
    writer_present: bool

    @property
    def consumed(self):
        return self.publications[-1].cursor if self.publications else self.after

    @property
    def caught_up(self):
        return self.consumed == self.observed_head


def _cursor(row):
    return JournalCursor(row["stream_id"], row["generation"], row["revision"], row["root_sha256"])


def _head(c, stream_id):
    row = c.execute(sa.text("SELECT * FROM momentum_structural_context_heads WHERE stream_id=:s"),
                    {"s": stream_id}).mappings().one_or_none()
    if row is None:
        raise ValueError("context_stream_missing")
    return row


def _lock_present(c, stream_id, *, own=False):
    query = """SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND granted
        AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
        AND classid::bigint=(hashtext(:ns)::bigint & 4294967295)
        AND objid::bigint=(hashtext(:s)::bigint & 4294967295) AND objsubid=2"""
    if own:
        query += " AND pid=pg_backend_pid()"
    return c.execute(sa.text(query + ")"), {"ns": LOCK_NAMESPACE, "s": stream_id}).scalar_one()


def _publication_root(cursor, revision, payload_sha, source_advanced):
    return _sha(_json([CONTRACT, cursor.stream_id, cursor.generation, revision,
                       cursor.root_sha256, payload_sha, source_advanced]))


class ContextJournalWriter:
    """Explicit new stream only. Existing head needs verified owner reconstruction.

    The held database session is the writer fence, not an expiring time lease.
    Consumers must distinguish fence presence from freshness/provider health.
    No connection is automatically reacquired after a failure.
    """
    @classmethod
    def create(cls, engine, *, stream_id, source_anchor: Cursor, max_payload_bytes):
        if (type(stream_id) is not str or not stream_id or type(source_anchor) is not Cursor
                or type(source_anchor.epoch) is not str or not source_anchor.epoch
                or type(source_anchor.revision) is not int or source_anchor.revision < 0
                or type(max_payload_bytes) is not int or max_payload_bytes <= 0):
            raise ValueError("invalid_context_writer_configuration")
        self = cls()
        self._c, self._closed = engine.connect(), False
        self._stream, self._max_bytes = stream_id, max_payload_bytes
        self._locked = False
        self._state = None
        try:
            self._locked = self._c.execute(sa.text(
                "SELECT pg_try_advisory_lock(hashtext(:ns),hashtext(:s))"),
                {"ns": LOCK_NAMESPACE, "s": stream_id}).scalar_one()
            if not self._locked:
                raise ValueError("context_writer_already_present")
            self._pid = self._c.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
            if self._c.execute(sa.text(
                    "SELECT 1 FROM momentum_structural_context_heads WHERE stream_id=:s"),
                    {"s": stream_id}).first():
                raise ValueError("context_owner_restore_required")
            generation = str(uuid4())
            root = _sha(_json([CONTRACT, stream_id, generation, source_anchor.epoch, source_anchor.revision]))
            self.anchor = self.cursor = JournalCursor(stream_id, generation, 0, root)
            self._c.execute(sa.text("""INSERT INTO momentum_structural_context_heads
                (stream_id,generation,revision,root_sha256,anchor_sha256,source_epoch,source_revision)
                VALUES(:s,:g,0,:root,:root,:epoch,:revision)"""),
                {"s": stream_id, "g": generation, "root": root,
                 "epoch": source_anchor.epoch, "revision": source_anchor.revision})
            self._c.commit()
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._c.rollback()
            if self._locked and not self._c.invalidated:
                self._c.execute(sa.text("SELECT pg_advisory_unlock(hashtext(:ns),hashtext(:s))"),
                                {"ns": LOCK_NAMESPACE, "s": self._stream})
                self._c.commit()
        finally:
            self._c.close()

    @classmethod
    def _acquire_for_reconstruction(cls, engine, *, stream_id, max_payload_bytes):
        """Internal recovery fence. Caller must verify history before exposing it."""
        if (type(stream_id) is not str or not stream_id or type(max_payload_bytes) is not int
                or max_payload_bytes <= 0):
            raise ValueError("invalid_context_writer_configuration")
        self = cls()
        self._c, self._closed = engine.connect(), False
        self._stream, self._max_bytes, self._locked = stream_id, max_payload_bytes, False
        self._state = None
        try:
            self._locked = self._c.execute(sa.text(
                "SELECT pg_try_advisory_lock(hashtext(:ns),hashtext(:s))"),
                {"ns": LOCK_NAMESPACE, "s": stream_id}).scalar_one()
            if not self._locked:
                raise ValueError("context_writer_already_present")
            self._pid = self._c.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
            head = _head(self._c, stream_id)
            self.cursor = _cursor(head)
            self.anchor = JournalCursor(stream_id, head["generation"], 0, head["anchor_sha256"])
            self._c.commit()
            return self
        except BaseException:
            self.close()
            raise

    def publish(self, snapshot, *, _before_commit=None):
        if self._closed:
            raise ValueError("context_writer_closed")
        try:
            from . import structural_context_delta as delta
            state = delta.prepare(snapshot, self._state)
            payload = state.payload
            _validate_native_stream(snapshot, self._stream)
            if len(payload.encode()) > self._max_bytes:
                raise ValueError("context_publication_byte_capacity")
            with self._c.begin():
                if self._c.execute(sa.text("SELECT pg_backend_pid()")).scalar_one() != self._pid \
                        or not _lock_present(self._c, self._stream, own=True):
                    raise ValueError("context_writer_fence_lost")
                head = _head(self._c, self._stream)
                if _cursor(head) != self.cursor:
                    raise ValueError("context_writer_cursor_changed")
                if (snapshot.source.epoch != head["source_epoch"]
                        or not head["source_revision"] <= snapshot.source.revision <= head["source_revision"]+1):
                    raise ValueError("context_source_revision_gap")
                advanced = snapshot.source.revision > head["source_revision"]
                rev, digest = self.cursor.revision+1, _sha(payload)
                if self._state is not None and state.state_sha256 == self._state.state_sha256:
                    if _before_commit is not None:
                        _before_commit(self._c, self.cursor, head['payload_sha256'])
                    return  # Identical observation, not a new source occurrence.
                root = _publication_root(self.cursor, rev, digest, advanced)
                self._c.execute(sa.text("""INSERT INTO momentum_structural_context_publications
                    (stream_id,generation,revision,previous_sha256,root_sha256,payload_sha256,payload,source_advanced)
                    VALUES(:s,:g,:r,:prev,:root,:sha,:payload,:advanced)"""),
                    {"s": self._stream, "g": self.cursor.generation, "r": rev,
                     "prev": self.cursor.root_sha256, "root": root, "sha": digest,
                     "payload": payload, "advanced": advanced})
                delta.persist(self._c, JournalCursor(self._stream, self.cursor.generation, rev, root), state)
                changed = self._c.execute(sa.text("""UPDATE momentum_structural_context_heads
                    SET revision=:r,root_sha256=:root,source_revision=:source,payload_sha256=:sha
                    WHERE stream_id=:s AND generation=:g AND revision=:prior AND root_sha256=:prev
                    RETURNING revision"""),
                    {"r": rev, "root": root, "source": snapshot.source.revision, "s": self._stream,
                     "g": self.cursor.generation, "prior": self.cursor.revision,
                     "prev": self.cursor.root_sha256, "sha": digest}).scalar_one_or_none()
                if changed != rev:
                    raise ValueError("context_writer_compare_and_swap_failed")
                # Wake only. Durable publications, not notification delivery,
                # define what each consumer still has to process.
                self._c.execute(sa.text("SELECT pg_notify(:channel,:payload)"),
                    {"channel": CHANNEL, "payload": _json([self._stream, self.cursor.generation, rev])})
                if _before_commit is not None:
                    _before_commit(self._c, JournalCursor(self._stream, self.cursor.generation, rev, root), digest)
            self.cursor = JournalCursor(self._stream, self.cursor.generation, rev, root)
            self._state = state
        except BaseException:
            self.close()
            raise


def _validate_native_stream(snapshot, stream_id):
    if (snapshot.native_enrollment is not None and
            stream_id != 'ordinary-paper:' + snapshot.native_enrollment.account_identity_sha256):
        raise ValueError('context_native_account_stream_mismatch')


def read_context(c, *, after: JournalCursor, max_publications: int, max_payload_bytes: int,
                 selected_symbols=None, _prior_state=None):
    from . import structural_context_delta as delta
    if c.get_isolation_level() not in {"REPEATABLE READ", "SERIALIZABLE"}:
        raise ValueError("context_read_requires_snapshot")
    if (type(after) is not JournalCursor or type(after.revision) is not int or after.revision < 0
            or not _digest(after.root_sha256) or type(max_publications) is not int or max_publications <= 0
            or type(max_payload_bytes) is not int or max_payload_bytes <= 0):
        raise ValueError("context_read_arguments_invalid")
    head = _head(c, after.stream_id)
    if selected_symbols is not None and (max_publications != 1 or after.revision != head['revision']-1):
        raise ValueError('context_projection_requires_current_head')
    if head["generation"] != after.generation or head["revision"] < after.revision:
        raise ValueError("context_generation_or_cursor_mismatch")
    if after.revision == 0:
        expected = head["anchor_sha256"]
    else:
        expected = c.execute(sa.text("""SELECT root_sha256 FROM momentum_structural_context_publications
            WHERE stream_id=:s AND generation=:g AND revision=:r"""),
            {"s": after.stream_id, "g": after.generation, "r": after.revision}).scalar_one_or_none()
    if expected != after.root_sha256:
        raise ValueError("context_consumer_anchor_missing_or_changed")
    end = min(head["revision"], after.revision + max_publications)
    rows = c.execute(sa.text("""SELECT revision,previous_sha256,root_sha256,payload_sha256,
        source_advanced,octet_length(payload) AS payload_bytes FROM momentum_structural_context_publications
        WHERE stream_id=:s AND generation=:g AND revision>:r AND revision<=:end ORDER BY revision"""),
        {"s": after.stream_id, "g": after.generation, "r": after.revision, "end": end}).mappings().all()
    if [row["revision"] for row in rows] != list(range(after.revision+1, end+1)):
        raise ValueError("context_publication_retention_gap")
    result, prior, size = [], after, 0
    state = _prior_state
    if rows and after.revision and selected_symbols is None:
        base = c.execute(sa.text('''SELECT previous_sha256,payload_sha256,source_advanced,
            octet_length(payload) AS bytes FROM momentum_structural_context_publications
            WHERE stream_id=:s AND generation=:g AND revision=:r'''),
            {'s': after.stream_id, 'g': after.generation, 'r': after.revision}).mappings().one()
        if base['bytes'] > max_payload_bytes:
            raise ValueError('atomic_context_base_exceeds_read_byte_capacity')
        payload = c.execute(sa.text('''SELECT payload FROM momentum_structural_context_publications
            WHERE stream_id=:s AND generation=:g AND revision=:r'''),
            {'s': after.stream_id, 'g': after.generation, 'r': after.revision}).scalar_one()
        previous = JournalCursor(after.stream_id, after.generation, after.revision-1, base['previous_sha256'])
        if (_sha(payload) != base['payload_sha256'] or
                _publication_root(previous, after.revision, base['payload_sha256'], base['source_advanced']) != after.root_sha256):
            raise ValueError('context_base_publication_digest_mismatch')
        if state is None:
            state = delta.materialize(c, after, payload=payload, max_payload_bytes=max_payload_bytes)
        elif state.state_sha256 != delta.decode_wire(payload)[0]['state_sha256']:
            raise ValueError('context_prior_state_digest_mismatch')
    for row in rows:
        n = row["payload_bytes"]
        if n > max_payload_bytes:
            if result:
                break
            raise ValueError("atomic_context_exceeds_read_byte_capacity")
        if size + n > max_payload_bytes:
            break
        payload = c.execute(sa.text("""SELECT payload FROM momentum_structural_context_publications
            WHERE stream_id=:s AND generation=:g AND revision=:r"""),
            {"s": after.stream_id, "g": after.generation, "r": row["revision"]}).scalar_one()
        if (_sha(payload) != row["payload_sha256"]
                or row["previous_sha256"] != prior.root_sha256
                or _publication_root(prior, row["revision"], row["payload_sha256"], row["source_advanced"])
                   != row["root_sha256"]):
            raise ValueError("context_publication_digest_mismatch")
        prior = JournalCursor(after.stream_id, after.generation, row["revision"], row["root_sha256"])
        if selected_symbols is not None:
            state = delta.materialize(c, prior, payload=payload, max_payload_bytes=max_payload_bytes,
                                      selected_symbols=selected_symbols)
        else:
            state = delta.apply(payload, state)
            if sum(len(p.encode()) for p in state.payloads.values()) > max_payload_bytes:
                raise ValueError('atomic_context_objects_exceed_read_byte_capacity')
        snapshot = state.snapshot
        _validate_native_stream(snapshot, after.stream_id)
        result.append(ContextPublication(prior, snapshot, row['source_advanced'], row['payload_sha256'],
                                         state.state_sha256, selected_symbols is not None, state))
        size += n
    if prior.revision == head["revision"] and prior.root_sha256 != head["root_sha256"]:
        raise ValueError("context_terminal_head_mismatch")
    return ContextRead(after, _cursor(head), tuple(result), _lock_present(c, after.stream_id))


def consumer_cursor(c, *, stream_id, consumer):
    if type(consumer) is not str or not consumer:
        raise ValueError("context_consumer_name_invalid")
    head = _head(c, stream_id)
    row = c.execute(sa.text("""SELECT revision,root_sha256 FROM momentum_structural_context_consumers
        WHERE stream_id=:s AND generation=:g AND consumer=:consumer"""),
        {"s": stream_id, "g": head["generation"], "consumer": consumer}).mappings().one_or_none()
    return JournalCursor(stream_id, head["generation"], row["revision"] if row else 0,
                         row["root_sha256"] if row else head["anchor_sha256"])


def read_current_context(c, *, stream_id, max_payload_bytes, selected_symbols=None):
    """Read the latest complete observation without consuming historical events.

    Reuses payload/chain/byte validation against the immediate predecessor in a
    repeatable-read transaction. This is a snapshot read, NOT event catch-up or
    acknowledgement. Consumers needing every trigger must use read_context and
    transactional acknowledge instead. No older state substitutes for a bad head.
    """
    if c.get_isolation_level() not in {'REPEATABLE READ', 'SERIALIZABLE'}:
        raise ValueError('context_read_requires_snapshot')
    head = _head(c, stream_id)
    if head['revision'] == 0:
        after = JournalCursor(stream_id, head['generation'], 0, head['anchor_sha256'])
    else:
        previous = c.execute(sa.text('''SELECT previous_sha256 FROM momentum_structural_context_publications
            WHERE stream_id=:s AND generation=:g AND revision=:r'''),
            {'s': stream_id, 'g': head['generation'], 'r': head['revision']}).scalar_one_or_none()
        if previous is None:
            raise ValueError('context_current_publication_missing')
        after = JournalCursor(stream_id, head['generation'], head['revision']-1, previous)
    return read_context(c, after=after, max_publications=1, max_payload_bytes=max_payload_bytes,
                        selected_symbols=selected_symbols if head['revision'] else None)


def acknowledge(c, *, consumer, read: ContextRead):
    """CAS offset in caller's transaction after processing; not an order receipt.

    Caller may atomically commit its decision/outbox and this offset. External
    broker side effects still need their existing idempotent execution protocol.
    """
    if type(read) is not ContextRead or type(consumer) is not str or not consumer:
        raise ValueError("context_ack_arguments_invalid")
    after, end = read.after, read.consumed
    if consumer_cursor(c, stream_id=after.stream_id, consumer=consumer) != after:
        raise ValueError("context_consumer_offset_changed")
    if not read.publications:
        return
    if any(p.projected for p in read.publications):
        raise ValueError('context_projection_cannot_acknowledge_events')
    if (type(read.publications) is not tuple or end.stream_id != after.stream_id
            or end.generation != after.generation
            or [p.cursor.revision for p in read.publications]
               != list(range(after.revision+1, end.revision+1))):
        raise ValueError("context_ack_publication_gap")
    rows = c.execute(sa.text("""SELECT revision,root_sha256,payload_sha256,source_advanced
        FROM momentum_structural_context_publications
        WHERE stream_id=:s AND generation=:g AND revision>:start AND revision<=:end ORDER BY revision"""),
        {"s": after.stream_id, "g": after.generation, "start": after.revision,
         "end": end.revision}).mappings().all()
    from . import structural_context_delta as delta
    if len(rows) != len(read.publications) or any(
            p.cursor.stream_id != after.stream_id or p.cursor.generation != after.generation
            or r["root_sha256"] != p.cursor.root_sha256
            or p._state is None or r['payload_sha256'] != _sha(p._state.payload)
            or p.payload_sha256 != r['payload_sha256']
            or delta.prepare(p.snapshot, p._state).state_sha256 != p.state_sha256
            or p.state_sha256 != delta.decode_wire(p._state.payload)[0]['state_sha256']
            or r["source_advanced"] is not p.source_advanced
            for r, p in zip(rows, read.publications)):
        raise ValueError("context_ack_evidence_missing_or_changed")
    anchor = _head(c, after.stream_id)["anchor_sha256"]
    c.execute(sa.text("""INSERT INTO momentum_structural_context_consumers
        (stream_id,generation,consumer,revision,root_sha256) VALUES(:s,:g,:consumer,0,:anchor)
        ON CONFLICT DO NOTHING"""),
        {"s": after.stream_id, "g": after.generation, "consumer": consumer, "anchor": anchor})
    changed = c.execute(sa.text("""UPDATE momentum_structural_context_consumers
        SET revision=:end,root_sha256=:root
        WHERE stream_id=:s AND generation=:g AND consumer=:consumer
        AND revision=:prior AND root_sha256=:prev RETURNING revision"""),
        {"end": end.revision, "root": end.root_sha256, "s": after.stream_id, "g": after.generation,
         "consumer": consumer, "prior": after.revision, "prev": after.root_sha256}).scalar_one_or_none()
    if changed is None:
        raise ValueError("context_consumer_offset_changed")
