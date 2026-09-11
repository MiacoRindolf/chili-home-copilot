"""Optional-write observation of ordinary held-exit reads; never capture authority.

Exact ordered IDs are retained only for the original bounded N-print query.
Walk/D reads have streaming digests, counts and bounds: their membership cannot
be recovered from this event. Neither form archives source values or proves a
common MVCC/captured prefix. Audit wall clocks never enter strategy decisions.
"""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps
import hashlib
import logging
import math
import time
from typing import Any
from uuid import uuid4

from .replay_capture_contract import canonical_json_bytes

LOG = logging.getLogger(__name__)
SCHEMA = "chili.ordinary-held-evaluation.v1"
_ACTIVE: ContextVar[Any] = ContextVar("ordinary_held_evaluation_audit", default=None)
_STATE = "evaluation_audit"
_FEATURE_COLS = ("price", "size", "bid", "ask", "event_epoch")
_N_COLS = (*_FEATURE_COLS, "id", "observed_at", "received_at", "available_at")
_LEG_COLS = (*_FEATURE_COLS, "observed_at", "id")


def _plain(value: Any) -> Any:
    """Lossless-enough canonical observation encoding, without nonfinite JSON."""
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return aware.isoformat()
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite_float": str(value)}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"unavailable_type": type(value).__name__}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_rows(rows: Any, columns: tuple[str, ...], *, width: int | None = None) -> str:
    digest = hashlib.sha256(canonical_json_bytes({"encoding": "ordered-canonical-jsonl-v1", "columns": columns}) + b"\n")
    for row in rows:
        values = tuple(row) if width is None else tuple(row[:width])
        digest.update(canonical_json_bytes(_plain(values)) + b"\n")
    return digest.hexdigest()


def _safe(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except Exception as exc:
        active = _ACTIVE.get()
        if active is not None:
            active.coverage_errors.append(type(exc).__name__)
        LOG.warning("held evaluation observation unavailable (%s)", type(exc).__name__)
        return None


def current_evaluation_id() -> str | None:
    active = _ACTIVE.get()
    return active.evaluation_id if active is not None else None


def role(name: str) -> None:
    active = _ACTIVE.get()
    if active is not None:
        active.role = name
        active.roles[name] = {"status": "invoked_no_ordinary_query_observed"}


def note(name: str, value: Any) -> None:
    active = _ACTIVE.get()
    if active is not None:
        _safe(lambda: active.notes.__setitem__(name, _plain(value)))


def feature_advanced() -> None:
    """Called only beside the existing successful accel_prev assignment."""
    active = _ACTIVE.get()
    if active is not None:
        active.feature_advanced = True


def exact_n_projection(window_prints: int | None) -> bool:
    active = _ACTIVE.get()
    return active is not None and active.role in {"entry_base", "G"} and window_prints is not None


class QueryObservation:
    """Hook called immediately before execute and after materialized fetch/error."""
    def __init__(self, owner, params, *, exact_n=None, feature_contract=None):
        self.owner = owner
        self.exact_n = exact_n
        self.record = {
            "read_id": str(uuid4()), "role": owner.role, "order": len(owner.reads),
            "status": "not_executed", "table": "iqfeed_trade_ticks",
            "database": owner.database, "source_generation_identity": None,
            "query_bounds": _plain(params), "feature_contract": feature_contract,
            "requested_at_utc": None, "returned_at_utc": None, "elapsed_ms": None,
            "request_clock_meaning": "immediately_before_execute",
            "return_clock_meaning": "after_materialized_fetch_or_failure",
            "clock_authority": "observation_wall_utc_not_strategy_or_snapshot_clock",
            "membership_recovery": "ordered_ids" if exact_n is not None else "digest_only",
            "source_values_archived": False, "captured_read": False,
        }
        owner.reads.append(self.record)
        owner.roles[owner.role] = {"status": "query_registered", "read_id": self.record["read_id"]}
        self.started = None

    def requested(self):
        self.record["requested_at_utc"] = _utc()
        self.started = time.monotonic()
        self.record["status"] = "executing"

    def audit_failed(self, *, method, error):
        self.record["observation_error"] = {"hook": method, "error_type": type(error).__name__}
        self.record["membership_recovery"] = "unavailable"
        self.owner.coverage_errors.append("query_observation_" + type(error).__name__)

    def returned(self, rows=None, error=None):
        # Timing closes before hashing, so it describes the read, not audit CPU.
        self.record["returned_at_utc"] = _utc()
        self.record["elapsed_ms"] = None if self.started is None else (time.monotonic()-self.started)*1000
        if error is not None:
            self.record.update(status="error", error_type=type(error).__name__, returned_count=None)
            self.owner.roles[self.record["role"]]["status"] = "query_error"
            return
        self.record.update(status="success", returned_count=len(rows))
        self.owner.roles[self.record["role"]]["status"] = "query_success"
        hash_started = time.monotonic()
        if self.exact_n is not None:
            if len(rows) > self.exact_n or any(len(r) != len(_N_COLS) for r in rows):
                self.record.update(membership_recovery="unavailable", coverage_error="unexpected_metadata_shape_or_count")
                self.owner.coverage_errors.append("N_metadata_unavailable")
                return
            self.record["ordered_ids"] = [int(r[5]) for r in rows]
            self.record["input_columns"] = list(_N_COLS)
            self.record["ordered_content_sha256"] = _hash_rows(rows, _N_COLS)
            self.record["math_input_sha256"] = _hash_rows(rows, _FEATURE_COLS, width=5)
            self.record["math_input_contract"] = "original_five_columns_before_parse_gap_trim_and_aggressor_carry"
            self.record["transformed_membership"] = "not_separately_claimed_use_reported_feature_contract"
            self.record["first_row_clocks"] = _plain(list(rows[0][5:])) if rows else None
            self.record["last_row_clocks"] = _plain(list(rows[-1][5:])) if rows else None
        else:
            self.record["input_columns"] = list(_LEG_COLS)
            self.record["ordered_content_sha256"] = _hash_rows(rows, _LEG_COLS)
            self.record["first_event_id"] = _plain(list(rows[0][5:7])) if rows else None
            self.record["last_event_id"] = _plain(list(rows[-1][5:7])) if rows else None
            self.record["membership_limitation"] = "digest_count_and_bounds_do_not_recover_ordered_membership"
        self.record["audit_hash_elapsed_ms"] = (time.monotonic()-hash_started)*1000


def query_observation(params, *, exact_n=None, feature_contract=None):
    active = _ACTIVE.get()
    if active is None:
        return None
    return _safe(QueryObservation, active, params, exact_n=exact_n, feature_contract=feature_contract)


def _snapshot(le):
    ev = le.get("exit_verdict") if isinstance(le.get("exit_verdict"), dict) else {}
    pos = le.get("position") if isinstance(le.get("position"), dict) else {}
    deadman = le.get("deadman_stop") if isinstance(le.get("deadman_stop"), dict) else {}
    return _plain({
        "verdict": {k: ev.get(k) for k in ("phase", "entry_at", "entry_px", "frontier_at", "frontier_id", "leg_high", "prints_since_entry", "prints_since_high", "last_print", "last_print_at", "accel_prev", "accel_prev_as_of", "deadman", "last", "unavailable", "exit")},
        "position": {k: pos.get(k) for k in ("quantity", "original_quantity", "avg_entry_price", "stop_price", "partial_taken")},
        "protection_snapshot": {k: deadman.get(k) for k in ("order_id", "client_order_id", "qty", "stop_price", "phase", "active")},
        "pending_exit": {k: le.get(k) for k in ("pending_exit_reason", "exit_order_id", "exit_client_order_id")},
    })


def _optional_emit(db, sess, payload, *, emit, timeout_ms):
    """An inner savepoint Session cannot preflush the parent's dirty decision.

    This releases only its savepoint; a later parent rollback removes the event.
    Local administrative GUCs are restored before release, rollback on failure.
    """
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    if not isinstance(db, Session):
        # Existing in-memory FSM fakes have no database transaction to poison.
        return emit(db, sess, "live_exit_evaluation", payload)
    with Session(bind=db.connection(), join_transaction_mode="create_savepoint", autoflush=False, expire_on_commit=False) as audit_db:
        with audit_db.begin():
            old = audit_db.execute(text("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")).one()
            audit_db.execute(text("SELECT set_config('statement_timeout', :s, true), set_config('lock_timeout', :s, true)"), {"s": str(int(timeout_ms))+"ms"})
            event = emit(audit_db, sess, "live_exit_evaluation", payload)
            audit_db.execute(text("SELECT set_config('statement_timeout', :s, true), set_config('lock_timeout', :l, true)"), {"s": old[0], "l": old[1]})
            return event


def _parent_rollback(session, previous_transaction):
    affected, retained = [], []
    for item in session.info.pop("held_audit_parent_pointers", []):
        transaction = item[0]
        while transaction is not None and transaction is not previous_transaction:
            transaction = transaction.parent
        (affected if transaction is previous_transaction else retained).append(item)
    if retained:
        session.info["held_audit_parent_pointers"] = retained
    for _, le, attempt, prior, prior_base in reversed(affected):
        ev = le.get("exit_verdict")
        if not isinstance(ev, dict) or ev.get(_STATE, {}).get("last_attempt", {}).get("evaluation_id") != attempt:
            continue
        if prior:
            ev[_STATE] = prior
        else:
            ev.pop(_STATE, None)
        dm = ev.get("deadman")
        if isinstance(dm, dict) and dm.get("base_observation", {}).get("evaluation_id") == attempt:
            if prior_base:
                dm["base_observation"] = prior_base
            else:
                dm.pop("base_observation", None)


def _parent_commit(session):
    if not session.in_nested_transaction():
        session.info.pop("held_audit_parent_pointers", None)


def _track_parent_pointer(db, le, attempt, prior, prior_base):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    if not isinstance(db, Session):
        return
    if not db.info.get("held_audit_parent_hooks"):
        event.listen(db, "after_soft_rollback", _parent_rollback)
        event.listen(db, "after_commit", _parent_commit)
        db.info["held_audit_parent_hooks"] = True
    transaction = db.get_nested_transaction() or db.get_transaction()
    db.info.setdefault("held_audit_parent_pointers", []).append((transaction, le, attempt, prior, prior_base))


class Evaluation:
    def __init__(self, globals_, db, sess, le, kwargs):
        self.globals = globals_
        self.db, self.sess, self.le, self.kwargs = db, sess, le, kwargs
        self.evaluation_id = str(uuid4())
        self.requested = _utc()
        self.started = time.monotonic()
        self.role = "unassigned"
        self.reads = []
        self.notes = {}
        self.roles = {name: {"status": "not_reached"} for name in ("walk", "entry_base", "G", "D")}
        self.coverage_errors = []
        self.feature_advanced = False
        self.pre = _snapshot(le)
        anchor = {"session_id": getattr(sess, "id", None), "symbol": getattr(sess, "symbol", None), "entry_filled_at_utc": le.get("entry_filled_at_utc"), "entry_fill_event_id": le.get("entry_fill_event_id")}
        self.anchor = _plain(anchor)
        self.leg_key = hashlib.sha256(canonical_json_bytes(self.anchor)).hexdigest() if anchor["entry_filled_at_utc"] else None
        ev = le.get("exit_verdict") or {}
        prior = ev.get(_STATE) if isinstance(ev, dict) else None
        self.prior = _plain(prior) if isinstance(prior, dict) and self.leg_key is not None and prior.get("leg_key") == self.leg_key else {}
        feature_link = self.prior.get("previous_feature")
        if feature_link and (feature_link.get("value") != self.pre["verdict"].get("accel_prev")
                             or feature_link.get("strategy_as_of") != self.pre["verdict"].get("accel_prev_as_of")):
            self.prior["previous_feature"] = {"evaluation_id": None, "read_id": None, "status": "feature_state_no_longer_matches_receipt"}
        try:
            self.database = db.get_bind().url.database
        except Exception:
            self.database = None

    def finish(self, result, error):
        returned = _utc()
        elapsed = (time.monotonic()-self.started)*1000
        post = _snapshot(self.le)
        g_read = next((r for r in reversed(self.reads) if r["role"] == "G"), None)
        base_read = next((r for r in reversed(self.reads) if r["role"] == "entry_base"), None)
        if self.notes.get("D_status"):
            self.roles["D"].setdefault("skip_reason", self.notes["D_status"])
        for name in ("entry_base", "G"):
            if self.roles[name]["status"] == "invoked_no_ordinary_query_observed":
                self.coverage_errors.append(name + "_ordinary_query_not_observed")
        cfg = self.notes.get("settings")
        timeout_ms = int((cfg or {}).get("timeout_ms") or self.globals["_exit_verdict_read_timeout_ms"]())
        payload = {
            "schema": SCHEMA, "evaluation_id": self.evaluation_id, "session_id": getattr(self.sess, "id", None),
            "anchor": self.anchor, "leg_key": self.leg_key, "state": getattr(self.sess, "state", None),
            "source_code_identity": None, "source_code_identity_status": "not_provided_by_runtime",
            "decision_as_of": _plain(self.kwargs.get("as_of")), "requested_at_utc": self.requested,
            "returned_at_utc": returned, "elapsed_ms": elapsed,
            "input_relationship": "independent_recorded_publication_reads", "common_prefix_id": None,
            "captured_prefix": False, "replay_authority": False, "source_values_archived": False,
            "persistence": "flushed_in_caller_transaction_not_independently_durable",
            "administrative_receipt_statement_timeout_ms": timeout_ms,
            "administrative_timeout_basis": "existing_exit_tick_io_budget_per_statement_restored_after_write",
            "feature_units": {"signed_tape_accel": "back_minus_front_aggressor_buy_shares_not_price_acceleration"},
            "previous_evaluation": self.prior.get("last_attempt"),
            "prior_unrecorded_evaluations": self.prior.get("unrecorded_count", 0),
            "previous_feature": self.prior.get("previous_feature") or {"evaluation_id": None, "read_id": None, "status": "prior_receipt_unknown"},
            "feature_pointer_advanced": self.feature_advanced,
            "reads": self.reads, "read_roles": self.roles, "observations": self.notes,
            "coverage_errors": self.coverage_errors, "pre": self.pre, "post": post,
            "quote": _plain({k: self.kwargs.get(k) for k in ("bid", "ask", "mid")}),
            "quote_read_identity": None, "quote_read_identity_status": "unavailable",
            "result": _plain(result), "error_type": type(error).__name__ if error is not None else None,
        }
        event = None
        stored = False
        try:
            event = _optional_emit(self.db, self.sess, payload, emit=self.globals["_emit"], timeout_ms=timeout_ms)
            stored = getattr(event, "id", None) is not None
        except Exception as exc:
            LOG.warning("live_exit_evaluation receipt failed (%s); strategy result retained", type(exc).__name__)
        state = dict(self.prior)
        state.update(leg_key=self.leg_key, last_attempt={"evaluation_id": self.evaluation_id,
                     "event_id": getattr(event, "id", None) if stored else None,
                     "status": "flushed_pending_parent_commit" if stored else "receipt_missing"},
                     unrecorded_count=0 if stored else int(self.prior.get("unrecorded_count", 0))+1)
        if self.feature_advanced:
            state["previous_feature"] = {
                "evaluation_id": self.evaluation_id if stored else None,
                "read_id": g_read["read_id"] if stored and g_read is not None else None,
                "event_id": getattr(event, "id", None) if stored else None,
                "status": "flushed_pending_parent_commit" if stored else "advanced_without_receipt",
                "missing_attempt_id": None if stored else self.evaluation_id,
                "value": post["verdict"].get("accel_prev"),
                "strategy_as_of": post["verdict"].get("accel_prev_as_of"),
            }
        ev = self.le.get("exit_verdict")
        if isinstance(ev, dict):
            prior_base = (self.pre["verdict"].get("deadman") or {}).get("base_observation")
            _track_parent_pointer(self.db, self.le, self.evaluation_id, self.prior, prior_base)
            ev[_STATE] = state
            if base_read is not None and isinstance(ev.get("deadman"), dict):
                ev["deadman"]["base_observation"] = {"evaluation_id": self.evaluation_id if stored else None,
                    "read_id": base_read["read_id"] if stored else None,
                    "status": "flushed_pending_parent_commit" if stored else "receipt_missing"}
            self.globals["_commit_le"](self.sess, self.le)


def observe_exit_evaluation(function):
    @wraps(function)
    def wrapped(db, sess, le, **kwargs):
        audit = _safe(Evaluation, function.__globals__, db, sess, le, kwargs)
        if audit is None:
            return function(db, sess, le, **kwargs)
        token = _ACTIVE.set(audit)
        result, error = None, None
        try:
            result = function(db, sess, le, **kwargs)
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                _safe(audit.finish, result, error)
            finally:
                _ACTIVE.reset(token)
    return wrapped
