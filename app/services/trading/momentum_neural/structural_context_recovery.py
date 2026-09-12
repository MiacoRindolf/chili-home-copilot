"""Replay the one ordinary owner from transactionally retained original inputs.

Recovery is fenced and compares every generated output before returning a usable
owner. It never re-reads mutable input trade rows, guesses past observation clocks,
or treats a last-print snapshot as warm structural state. No broker authority.
"""
from __future__ import annotations

from dataclasses import fields
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import time
from types import MappingProxyType

import sqlalchemy as sa

from scripts import iqfeed_print_publications as source
from . import ordinary_structural_context as ordinary
from . import structural_context_journal as journal
from . import structural_tape_prefix as prefix
from app.tick_math import wave_context as waves
from app.tick_math import wave_evidence as evidence

CONTRACT = "ordinary_context_recovery_inputs_v1"
TYPES = {c.__name__: c for c in (source.Cursor, source.Publication, source.ReadResult, prefix.Limits)}


def code_identity():
    paths = [Path(__file__), *(Path(m.__file__) for m in (source, ordinary, journal, prefix, waves, evidence))]
    return journal._sha(journal._json({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}))


def _encode(value):
    if type(value) in (str, int, bool, type(None)):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is datetime:
        return {"datetime": value.isoformat()}
    if type(value) is tuple:
        return {"tuple": [_encode(v) for v in value]}
    if type(value) in (dict, MappingProxyType):
        if any(type(k) is not str for k in value):
            raise ValueError("recovery_map_key_invalid")
        return {"map": [[k, _encode(v)] for k, v in sorted(value.items())]}
    cls = TYPES.get(type(value).__name__)
    if cls is not None and type(value) is cls:
        return {"type": cls.__name__, "fields": {f.name: _encode(getattr(value, f.name)) for f in fields(cls)}}
    raise ValueError("recovery_input_type_invalid")


def _decode(value):
    if type(value) in (str, int, bool, type(None)):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is not dict:
        raise ValueError("recovery_input_shape_invalid")
    if set(value) == {"datetime"} and type(value["datetime"]) is str:
        return datetime.fromisoformat(value["datetime"])
    if set(value) == {"tuple"} and type(value["tuple"]) is list:
        return tuple(_decode(v) for v in value["tuple"])
    if set(value) == {"map"} and type(value["map"]) is list:
        pairs = value["map"]
        if any(type(p) is not list or len(p) != 2 or type(p[0]) is not str for p in pairs):
            raise ValueError("recovery_map_invalid")
        if [p[0] for p in pairs] != sorted({p[0] for p in pairs}):
            raise ValueError("recovery_map_keys_invalid")
        return {k: _decode(v) for k, v in pairs}
    if set(value) == {"type", "fields"} and type(value["type"]) is str:
        cls = TYPES.get(value["type"])
        members = value["fields"]
        if cls is not None and type(members) is dict and set(members) == {f.name for f in fields(cls)}:
            return cls(**{k: _decode(v) for k, v in members.items()})
    raise ValueError("recovery_input_tag_invalid")


def encode_input(value):
    return journal._json(_encode(value))


def decode_input(payload):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("recovery_duplicate_json_key")
            result[k] = v
        return result
    value = _decode(json.loads(payload, object_pairs_hook=unique))
    if encode_input(value) != payload:
        raise ValueError("recovery_input_not_canonical")
    return value


def create_schema(c):
    # Migration/test schemas only. Ordinary service startup never creates tables.
    c.execute(sa.text("""CREATE TABLE IF NOT EXISTS momentum_structural_context_recovery_heads (
        stream_id TEXT NOT NULL, generation TEXT NOT NULL,
        revision BIGINT NOT NULL CHECK(revision>=0), root_sha256 TEXT NOT NULL,
        PRIMARY KEY(stream_id,generation))"""))
    c.execute(sa.text("""CREATE TABLE IF NOT EXISTS momentum_structural_context_inputs (
        stream_id TEXT NOT NULL, generation TEXT NOT NULL, revision BIGINT NOT NULL CHECK(revision>0),
        previous_sha256 TEXT NOT NULL, root_sha256 TEXT NOT NULL,
        input_sha256 TEXT NOT NULL, input_payload TEXT NOT NULL,
        output_revision BIGINT NOT NULL CHECK(output_revision>0),
        output_root_sha256 TEXT NOT NULL, output_payload_sha256 TEXT NOT NULL,
        PRIMARY KEY(stream_id,generation,revision))"""))


def _head(c, cursor):
    return c.execute(sa.text("""SELECT revision,root_sha256 FROM momentum_structural_context_recovery_heads
        WHERE stream_id=:s AND generation=:g"""),
        {"s": cursor.stream_id, "g": cursor.generation}).mappings().one_or_none()


def _root(cursor, revision, previous, digest, output, output_sha):
    return journal._sha(journal._json([CONTRACT, cursor.stream_id, cursor.generation,
        revision, previous, digest, output.revision, output.root_sha256, output_sha]))


class RecoverableStructuralContext:
    """One ordinary reducer and input/output transaction owner; no trading callers."""
    @classmethod
    def create(cls, engine, *, stream_id, max_payload_bytes, max_input_bytes, **owner_configuration):
        if type(max_input_bytes) is not int or max_input_bytes <= 0:
            raise ValueError("recovery_input_capacity_invalid")
        self = cls()
        self.owner = ordinary.OrdinaryStructuralContextOwner.cold_start(engine, **owner_configuration)
        self.writer = journal.ContextJournalWriter.create(engine, stream_id=stream_id,
            source_anchor=self.owner.read("audit").source, max_payload_bytes=max_payload_bytes)
        self._max_input_bytes = max_input_bytes
        self._input_revision, self._input_root = 0, self.writer.anchor.root_sha256
        try:
            self._code = code_identity()
            self.owner.bind_replay_sink(self._record)
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        self.writer.close()

    def _record(self, snapshot, capsule):
        envelope = {"contract": CONTRACT, "code": self._code, "input": capsule}
        try:
            payload = encode_input(envelope)
        except BaseException:
            self.close()
            raise
        if len(payload.encode()) > self._max_input_bytes:
            self.close()
            raise ValueError("recovery_input_byte_capacity")
        revision, digest = self._input_revision+1, journal._sha(payload)
        committed = []
        def before_commit(c, output, output_sha):
            prior = _head(c, self.writer.cursor)
            if self._input_revision == 0 and prior is None:
                c.execute(sa.text("""INSERT INTO momentum_structural_context_recovery_heads
                    (stream_id,generation,revision,root_sha256) VALUES(:s,:g,0,:root)"""),
                    {"s": output.stream_id, "g": output.generation, "root": self._input_root})
            elif prior is None or (prior["revision"], prior["root_sha256"]) != (self._input_revision, self._input_root):
                raise ValueError("recovery_input_head_changed")
            root = _root(output, revision, self._input_root, digest, output, output_sha)
            c.execute(sa.text("""INSERT INTO momentum_structural_context_inputs
                (stream_id,generation,revision,previous_sha256,root_sha256,input_sha256,input_payload,
                 output_revision,output_root_sha256,output_payload_sha256)
                VALUES(:s,:g,:r,:prev,:root,:sha,:payload,:out,:out_root,:out_sha)"""),
                {"s": output.stream_id, "g": output.generation, "r": revision, "prev": self._input_root,
                 "root": root, "sha": digest, "payload": payload, "out": output.revision,
                 "out_root": output.root_sha256, "out_sha": output_sha})
            changed = c.execute(sa.text("""UPDATE momentum_structural_context_recovery_heads
                SET revision=:r,root_sha256=:root WHERE stream_id=:s AND generation=:g
                AND revision=:prior AND root_sha256=:prev RETURNING revision"""),
                {"r": revision, "root": root, "s": output.stream_id, "g": output.generation,
                 "prior": self._input_revision, "prev": self._input_root}).scalar_one_or_none()
            if changed != revision:
                raise ValueError("recovery_input_compare_and_swap_failed")
            committed.append(root)
        try:
            self.writer.publish(snapshot, _before_commit=before_commit)
            self._input_revision, self._input_root = revision, committed[0]
        except BaseException:
            self.close()
            raise

    @classmethod
    def restore(cls, engine, *, stream_id, max_payload_bytes, max_input_bytes,
                max_replay_inputs, clock_ns=time.time_ns):
        if any(type(v) is not int or v <= 0 for v in (max_input_bytes, max_replay_inputs)):
            raise ValueError("recovery_capacity_invalid")
        self = cls()
        self.writer = journal.ContextJournalWriter._acquire_for_reconstruction(engine,
            stream_id=stream_id, max_payload_bytes=max_payload_bytes)
        self._max_input_bytes = max_input_bytes
        self._input_revision, self._input_root = 0, self.writer.anchor.root_sha256
        try:
            self._code = code_identity()
            c = self.writer._c.execution_options(isolation_level="REPEATABLE READ")
            with c.begin():
                c.execute(sa.text("SET TRANSACTION READ ONLY"))
                terminal = _head(c, self.writer.cursor)
                if terminal is None or terminal["revision"] < 1:
                    raise ValueError("recovery_inputs_missing")
                if terminal["revision"] > max_replay_inputs:
                    raise ValueError("recovery_work_capacity")
                output_cursor = self.writer.anchor
                last_snapshot = None
                for revision in range(1, terminal["revision"]+1):
                    params = {"s": stream_id, "g": self.writer.cursor.generation, "r": revision}
                    row = c.execute(sa.text("""SELECT revision,previous_sha256,root_sha256,input_sha256,
                        octet_length(input_payload) AS payload_bytes,output_revision,
                        output_root_sha256,output_payload_sha256 FROM momentum_structural_context_inputs
                        WHERE stream_id=:s AND generation=:g AND revision=:r"""), params).mappings().one_or_none()
                    if row is None:
                        raise ValueError("recovery_input_retention_gap")
                    if row["payload_bytes"] > max_input_bytes:
                        raise ValueError("recovery_input_byte_capacity")
                    payload = c.execute(sa.text("""SELECT input_payload FROM momentum_structural_context_inputs
                        WHERE stream_id=:s AND generation=:g AND revision=:r"""), params).scalar_one()
                    out = journal.JournalCursor(stream_id, self.writer.cursor.generation,
                        row["output_revision"], row["output_root_sha256"])
                    if (row["previous_sha256"] != self._input_root or journal._sha(payload) != row["input_sha256"]
                            or _root(out, revision, self._input_root, row["input_sha256"], out,
                                     row["output_payload_sha256"]) != row["root_sha256"]):
                        raise ValueError("recovery_input_digest_mismatch")
                    envelope = decode_input(payload)
                    if (type(envelope) is not dict or set(envelope) != {"contract", "code", "input"}
                            or envelope["contract"] != CONTRACT or envelope["code"] != self._code):
                        raise ValueError("recovery_code_or_contract_changed")
                    capsule = envelope["input"]
                    if type(capsule) is not dict or type(capsule.get("kind")) is not str:
                        raise ValueError("recovery_capsule_invalid")
                    if out.revision == output_cursor.revision+1:
                        read = journal.read_context(c, after=output_cursor,
                            max_publications=1, max_payload_bytes=max_payload_bytes)
                        if len(read.publications) != 1 or read.consumed != out:
                            raise ValueError("recovery_output_cursor_mismatch")
                        publication, = read.publications
                        expected = publication.snapshot
                        prior_source = last_snapshot.source if last_snapshot else expected.source
                        if publication.source_advanced != (expected.source.revision > prior_source.revision):
                            raise ValueError("recovery_output_source_flag_mismatch")
                    elif out == output_cursor and last_snapshot is not None:
                        expected = last_snapshot
                    else:
                        raise ValueError("recovery_output_revision_gap")
                    if journal._sha(journal.encode_snapshot(expected)) != row["output_payload_sha256"]:
                        raise ValueError("recovery_output_payload_changed")
                    emitted = []
                    def compare(snapshot, actual_input):
                        if (encode_input(actual_input) != encode_input(capsule)
                                or journal.encode_snapshot(snapshot) != journal.encode_snapshot(expected)):
                            raise ValueError("recovery_recomputed_output_mismatch")
                        emitted.append(snapshot)
                    if revision == 1:
                        if set(capsule) != {"kind", "anchor", "limits", "max_symbols", "max_trade_rows"} or capsule["kind"] != "init":
                            raise ValueError("recovery_initial_configuration_missing")
                        anchor_root = journal._sha(journal._json([journal.CONTRACT, stream_id,
                            out.generation, capsule["anchor"].epoch, capsule["anchor"].revision]))
                        if anchor_root != self.writer.anchor.root_sha256:
                            raise ValueError("recovery_source_anchor_changed")
                        self.owner = ordinary.OrdinaryStructuralContextOwner(
                            **{k:v for k,v in capsule.items() if k != "kind"}, clock_ns=clock_ns)
                        self.owner.bind_replay_sink(compare)
                    else:
                        self.owner._replay_sink = compare
                        kind = capsule["kind"]
                        if kind == "demand" and set(capsule) == {"kind", "reason", "revision", "symbols"}:
                            self.owner.update_demand(capsule["reason"], revision=capsule["revision"], symbols=capsule["symbols"])
                        elif kind == "demand_batch" and set(capsule) == {"kind", "updates"}:
                            updates = capsule["updates"]
                            if (type(updates) is not tuple or not updates
                                    or any(type(u) is not dict or set(u) != {"reason", "revision", "symbols", "complete"}
                                           or type(u["reason"]) is not str for u in updates)
                                    or [u["reason"] for u in updates] != sorted({u["reason"] for u in updates})):
                                raise ValueError("recovery_demand_batch_invalid")
                            self.owner.update_demands({u["reason"]: {k:v for k,v in u.items() if k != "reason"}
                                                       for u in updates})
                        elif kind == "source" and set(capsule) == {"kind", "read", "known_ns"}:
                            self.owner._apply_observation(capsule["read"], capsule["known_ns"])
                        elif kind == "read_failure" and set(capsule) == {"kind", "frontier", "reason"}:
                            if capsule["frontier"] != self.owner.read("audit").source:
                                raise ValueError("recovery_failure_frontier_changed")
                            self.owner._publish(capsule["frontier"], "unresolved", capsule["reason"], {}, capsule=capsule)
                        else:
                            raise ValueError("recovery_capsule_kind_invalid")
                    if len(emitted) != 1:
                        raise ValueError("recovery_output_count_mismatch")
                    self._input_revision, self._input_root = revision, row["root_sha256"]
                    output_cursor, last_snapshot = out, expected
                head = journal._head(c, stream_id)
                if (self._input_root != terminal["root_sha256"] or output_cursor != self.writer.cursor
                        or journal._cursor(head) != output_cursor or last_snapshot.source.epoch != head["source_epoch"]
                        or last_snapshot.source.revision != head["source_revision"]
                        or journal._sha(journal.encode_snapshot(last_snapshot)) != head["payload_sha256"]
                        or not journal._lock_present(c, stream_id, own=True)):
                    raise ValueError("recovery_terminal_frontier_mismatch")
            c.execution_options(isolation_level="READ COMMITTED")
            self.owner._replay_sink = self._record
            return self
        except BaseException:
            self.close()
            raise
