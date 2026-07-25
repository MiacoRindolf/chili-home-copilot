"""Read-only PostgreSQL candidate inventory for captured Alpaca PAPER.

The initial-material provider must record the complete considered candidate set,
including candidates that are inactive or economically ineligible.  This port
therefore applies only the durable route boundary (exact symbol and
``scope='symbol'``), joins every referenced strategy variant, and leaves all
eligibility decisions to the provider.

Each reader is constructed with the exact selection authority owned by the
captured PAPER generation.  A call admits only clone variants and viability
rows which still match that authority, the current ready selection frontier,
and each row's own successfully-applied ancestor batch in the current complete,
gap-free event chain.  It additionally requires the latest durable route state
to be ``eligible`` at that exact observation sequence.  A later
``coverage_unavailable`` receipt therefore supersedes only its symbol/variant;
an older scored row can never become executable merely because it still exists.

Each call owns one short REPEATABLE READ / READ ONLY transaction.  Returned ORM
rows are expunged before rollback so downstream content-hash helpers can inspect
stable detached values without retaining a SQLAlchemy session or performing a
lazy read.  No user ownership predicate is applied: neither backing table has a
user/owner column.  ``user_id`` remains an exact positive route value carried by
the typed read receipt.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import re
import threading
from typing import Any, Mapping
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ....models.captured_paper_selection_frontier import (
    CapturedPaperSelectionFrontier,
    CapturedPaperSelectionFrontierEvent,
    CapturedPaperSelectionRouteState,
)
from ....models.trading import MomentumStrategyVariant, MomentumSymbolViability
from .captured_paper_initial_admission import (
    captured_paper_initial_variant_sha256,
)
from .captured_paper_initial_provider import (
    CapturedPaperInitialCandidateRead,
    CapturedPaperInitialCandidateReadPort,
    CapturedPaperInitialCandidateRow,
)
from .captured_paper_selection_producer import (
    EVENT_SCHEMA_VERSION,
    FRONTIER_SCHEMA_VERSION,
    OBSERVATION_SCHEMA_VERSION,
    PROVENANCE_KEY,
    ROUTE_COVERAGE_UNAVAILABLE,
    ROUTE_ELIGIBLE,
    ROUTE_STATE_SCHEMA_VERSION,
    ROUTE_STATE_UPDATE_SCHEMA_VERSION,
    SOURCE_NODE_ID,
    CapturedPaperSelectionAuthority,
)
from .captured_paper_variant_binding import (
    BINDING_META_KEY,
    BINDING_META_SCHEMA_VERSION,
)


_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.]{0,35}")
_READ_ONLY_TRANSACTION_SQL = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
)
_READ_AT_SQL = (
    "SELECT LEAST(transaction_timestamp(), "
    "CAST(:decision_at AS timestamptz))"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
_PROVENANCE_SCHEMA_VERSION = (
    "chili.captured-paper-selection-viability-provenance.v1"
)
_PROVENANCE_KEYS = frozenset(
    {
        "schema_version",
        "account_scope",
        "expected_account_id",
        "activation_generation",
        "authority_sha256",
        "policy_sha256",
        "settings_projection_sha256",
        "code_build_sha256",
        "variant_set_sha256",
        "variant_id",
        "batch_sha256",
        "observation_sha256",
        "source_name",
        "source_generation",
        "source_sequence",
        "queue_receipt_sha256",
        "coverage_receipt_sha256",
        "paper_only_strategy_override",
        "live_cash_authorized",
    }
)
_BINDING_META_KEYS = frozenset(
    {
        "schema_version",
        "account_scope",
        "execution_family",
        "expected_account_id",
        "activation_generation",
        "source_variant_id",
        "source_variant_sha256",
        "source_family",
        "source_version",
        "policy_sha256",
        "settings_projection_sha256",
        "code_build_sha256",
        "plan_sha256",
        "bound_at",
        "strategy_params_overridden",
        "paper_order_submission_authorized",
        "live_cash_authorized",
        "real_money_authorized",
    }
)
_FRONTIER_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "frontier_id",
        "event_sequence",
        "event_type",
        "expected_version",
        "next_version",
        "expected_frontier_sha256",
        "previous_event_sha256",
        "batch_sha256",
        "gap_sha256",
        "source_sequence_from",
        "source_sequence_through",
        "detail",
        "recorded_at",
        "next_state",
    }
)
_FRONTIER_BATCH_DETAIL_KEYS = frozenset(
    {
        "authority_sha256",
        "source_name",
        "source_generation",
        "queue_receipt_sha256",
        "coverage_receipt_sha256",
        "watermark_at",
        "read_at",
        "observation_sha256s",
        "route_state_updates",
    }
)
_ROUTE_STATE_UPDATE_KEYS = frozenset(
    {
        "schema_version",
        "source_sequence",
        "source_event_at",
        "source_available_at",
        "symbol",
        "variant_id",
        "state",
        "evidence_sha256",
        "bundle_sha256",
        "scoring_authority_sha256",
        "score_result_sha256",
        "reason_codes",
        "update_sha256",
    }
)


class CapturedPaperInitialCandidateReaderUnavailable(RuntimeError):
    """The exact read-only candidate inventory could not be produced."""

    def __init__(self, reason: str):
        self.reason = str(reason or "initial_candidate_reader_unavailable")
        super().__init__(self.reason)


def _reject(reason: str) -> None:
    raise CapturedPaperInitialCandidateReaderUnavailable(reason)


def _positive_user_id(value: Any) -> int:
    if type(value) is not int or value <= 0:
        _reject("initial_candidate_reader_user_id_invalid")
    return value


def _symbol(value: Any) -> str:
    if (
        not isinstance(value, str)
        or _SYMBOL_RE.fullmatch(value) is None
        or value.endswith(".")
        or ".." in value
    ):
        _reject("initial_candidate_reader_symbol_invalid")
    return value


def _aware_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _reject(f"{field_name}_invalid")
    try:
        offset = value.utcoffset()
    except Exception as exc:  # pragma: no cover - defensive tzinfo boundary
        raise CapturedPaperInitialCandidateReaderUnavailable(
            f"{field_name}_invalid"
        ) from exc
    if offset is None:
        _reject(f"{field_name}_invalid")
    return value.astimezone(timezone.utc)


def _db_utc(value: Any, field_name: str) -> datetime:
    """Interpret ORM-naive timestamps as UTC, matching the existing models."""

    if not isinstance(value, datetime):
        _reject(f"{field_name}_invalid")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return _aware_utc(value, field_name)


def _iso(value: datetime | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _db_utc(value, field_name).isoformat().replace("+00:00", "Z")


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _canonical_uuid(value: Any) -> str | None:
    raw = str(value or "")
    try:
        normalized = str(uuid.UUID(raw))
    except (AttributeError, TypeError, ValueError):
        return None
    return normalized if raw == normalized else None


def _event_iso_utc(
    value: Any,
    field_name: str,
    *,
    allow_none: bool = False,
) -> datetime | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        _reject(f"{field_name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapturedPaperInitialCandidateReaderUnavailable(
            f"{field_name}_invalid"
        ) from exc
    normalized = _aware_utc(parsed, field_name)
    if value != normalized.isoformat().replace("+00:00", "Z"):
        _reject(f"{field_name}_invalid")
    return normalized


def _authority_is_exact(authority: Any) -> bool:
    if type(authority) is not CapturedPaperSelectionAuthority:
        return False
    try:
        variant_set_sha256 = _sha256_json(
            {
                "schema_version": "chili.captured-paper-selection-variant-set.v1",
                "variants": [
                    binding.to_dict() for binding in authority.variant_bindings
                ],
            }
        )
        return bool(
            authority.account_scope == "alpaca:paper"
            and authority.execution_family == "alpaca_spot"
            and authority.variant_bindings
            and len(authority.variant_ids) == len(set(authority.variant_ids))
            and variant_set_sha256 == authority.variant_set_sha256
            and _sha256_json(authority.body()) == authority.authority_sha256
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _frontier_value(frontier: Any, key: str) -> Any:
    if isinstance(frontier, Mapping):
        return frontier[key]
    return getattr(frontier, key)


def _frontier_body(frontier: Any) -> dict[str, Any]:
    return {
        "schema_version": FRONTIER_SCHEMA_VERSION,
        "account_scope": _frontier_value(frontier, "account_scope"),
        "expected_account_id": _frontier_value(frontier, "expected_account_id"),
        "activation_generation": _frontier_value(
            frontier, "activation_generation"
        ),
        "execution_family": _frontier_value(frontier, "execution_family"),
        "authority_sha256": _frontier_value(frontier, "authority_sha256"),
        "policy_sha256": _frontier_value(frontier, "policy_sha256"),
        "settings_projection_sha256": _frontier_value(
            frontier, "settings_projection_sha256"
        ),
        "code_build_sha256": _frontier_value(frontier, "code_build_sha256"),
        "variant_set_sha256": _frontier_value(frontier, "variant_set_sha256"),
        "last_source_sequence": _frontier_value(
            frontier, "last_source_sequence"
        ),
        "last_source_event_at": _iso(
            _frontier_value(frontier, "last_source_event_at"),
            "initial_candidate_frontier_event_at",
        ),
        "last_source_available_at": _iso(
            _frontier_value(frontier, "last_source_available_at"),
            "initial_candidate_frontier_available_at",
        ),
        "last_batch_sha256": _frontier_value(frontier, "last_batch_sha256"),
        "status": _frontier_value(frontier, "status"),
        "gap_count": _frontier_value(frontier, "gap_count"),
        "version": _frontier_value(frontier, "version"),
        "event_sequence": _frontier_value(frontier, "event_sequence"),
        "last_event_sha256": _frontier_value(frontier, "last_event_sha256"),
    }


def _route_state_body(
    row: CapturedPaperSelectionRouteState,
) -> dict[str, Any]:
    return {
        "schema_version": ROUTE_STATE_SCHEMA_VERSION,
        "account_scope": row.account_scope,
        "expected_account_id": row.expected_account_id,
        "activation_generation": row.activation_generation,
        "execution_family": row.execution_family,
        "authority_sha256": row.authority_sha256,
        "symbol": row.symbol,
        "variant_id": row.variant_id,
        "latest_source_sequence": row.latest_source_sequence,
        "state": row.state,
        "evidence_sha256": row.evidence_sha256,
        "batch_sha256": row.batch_sha256,
        "source_event_at": _iso(
            row.source_event_at,
            "initial_candidate_route_state_event_at",
        ),
        "source_available_at": _iso(
            row.source_available_at,
            "initial_candidate_route_state_available_at",
        ),
        "version": row.version,
    }


def _expected_route_state_body(
    *,
    authority: CapturedPaperSelectionAuthority,
    update_row: Mapping[str, Any],
    batch_sha256: str,
    version: int,
) -> dict[str, Any]:
    return {
        "schema_version": ROUTE_STATE_SCHEMA_VERSION,
        "account_scope": authority.account_scope,
        "expected_account_id": authority.expected_account_id,
        "activation_generation": authority.activation_generation,
        "execution_family": authority.execution_family,
        "authority_sha256": authority.authority_sha256,
        "symbol": update_row["symbol"],
        "variant_id": update_row["variant_id"],
        "latest_source_sequence": update_row["source_sequence"],
        "state": update_row["state"],
        "evidence_sha256": update_row["evidence_sha256"],
        "batch_sha256": batch_sha256,
        "source_event_at": update_row["source_event_at"],
        "source_available_at": update_row["source_available_at"],
        "version": version,
    }


def _validated_frontier_cache_key(
    frontier: Any,
    *,
    authority: CapturedPaperSelectionAuthority,
    read_at: datetime,
) -> tuple[int, int, str, str]:
    if not isinstance(frontier, CapturedPaperSelectionFrontier):
        _reject("initial_candidate_frontier_unavailable")
    expected_authority = {
        "account_scope": authority.account_scope,
        "expected_account_id": authority.expected_account_id,
        "activation_generation": authority.activation_generation,
        "execution_family": authority.execution_family,
        "authority_sha256": authority.authority_sha256,
        "policy_sha256": authority.policy_sha256,
        "settings_projection_sha256": authority.settings_projection_sha256,
        "code_build_sha256": authority.code_build_sha256,
        "variant_set_sha256": authority.variant_set_sha256,
    }
    if any(
        getattr(frontier, key, None) != value
        for key, value in expected_authority.items()
    ):
        _reject("initial_candidate_frontier_authority_mismatch")
    if (
        frontier.status != "ready"
        or type(frontier.id) is not int
        or frontier.id <= 0
        or type(frontier.gap_count) is not int
        or frontier.gap_count != 0
        or type(frontier.last_source_sequence) is not int
        or frontier.last_source_sequence <= 0
        or _SHA256_RE.fullmatch(str(frontier.last_batch_sha256 or "")) is None
        or type(frontier.version) is not int
        or frontier.version <= 1
        or type(frontier.event_sequence) is not int
        or frontier.event_sequence <= 0
        or _SHA256_RE.fullmatch(str(frontier.last_event_sha256 or "")) is None
        or _SHA256_RE.fullmatch(str(frontier.frontier_sha256 or "")) is None
    ):
        _reject("initial_candidate_frontier_not_ready")
    if (
        _db_utc(
            frontier.last_source_available_at,
            "initial_candidate_frontier_available_at",
        )
        > read_at
        or _db_utc(
            frontier.updated_at,
            "initial_candidate_frontier_updated_at",
        )
        > read_at
        or _sha256_json(_frontier_body(frontier)) != frontier.frontier_sha256
    ):
        _reject("initial_candidate_frontier_invalid")
    return (
        frontier.id,
        frontier.event_sequence,
        str(frontier.frontier_sha256),
        str(frontier.last_event_sha256),
    )


def _validate_frontier(
    frontier: Any,
    events: Any,
    *,
    authority: CapturedPaperSelectionAuthority,
    read_at: datetime,
) -> tuple[
    Mapping[str, Mapping[str, Any]],
    Mapping[tuple[str, int], Mapping[str, Any]],
]:
    _validated_frontier_cache_key(
        frontier,
        authority=authority,
        read_at=read_at,
    )
    expected_authority = {
        "account_scope": authority.account_scope,
        "expected_account_id": authority.expected_account_id,
        "activation_generation": authority.activation_generation,
        "execution_family": authority.execution_family,
        "authority_sha256": authority.authority_sha256,
        "policy_sha256": authority.policy_sha256,
        "settings_projection_sha256": authority.settings_projection_sha256,
        "code_build_sha256": authority.code_build_sha256,
        "variant_set_sha256": authority.variant_set_sha256,
    }
    if not isinstance(events, (tuple, list)) or len(events) != frontier.event_sequence:
        _reject("initial_candidate_frontier_event_unavailable")

    previous_values = {
        **expected_authority,
        "last_source_sequence": 0,
        "last_source_event_at": None,
        "last_source_available_at": None,
        "last_batch_sha256": None,
        "status": "ready",
        "gap_count": 0,
        "version": 1,
        "event_sequence": 0,
        "last_event_sha256": None,
    }
    previous_frontier_sha256 = _sha256_json(_frontier_body(previous_values))
    previous_event_sha256: str | None = None
    batch_details: dict[str, Mapping[str, Any]] = {}
    observation_sha256s: set[str] = set()
    expected_route_states: dict[tuple[str, int], Mapping[str, Any]] = {}

    for expected_sequence, event in enumerate(events, start=1):
        if not isinstance(event, CapturedPaperSelectionFrontierEvent):
            _reject("initial_candidate_frontier_event_unavailable")
        recorded_at = _db_utc(
            event.recorded_at,
            "initial_candidate_frontier_recorded_at",
        )
        if not (
            type(event.frontier_id) is int
            and event.frontier_id == frontier.id
            and type(event.event_sequence) is int
            and event.event_sequence == expected_sequence
            and event.event_type == "batch_applied"
            and type(event.expected_version) is int
            and event.expected_version == expected_sequence
            and type(event.next_version) is int
            and event.next_version == expected_sequence + 1
            and event.expected_frontier_sha256
            == previous_frontier_sha256
            and event.previous_event_sha256 == previous_event_sha256
            and _SHA256_RE.fullmatch(str(event.event_sha256 or ""))
            is not None
            and _SHA256_RE.fullmatch(str(event.batch_sha256 or ""))
            is not None
            and event.gap_sha256 is None
            and type(event.source_sequence_from) is int
            and event.source_sequence_from
            == previous_values["last_source_sequence"]
            and type(event.source_sequence_through) is int
            and event.source_sequence_through > event.source_sequence_from
            and recorded_at <= read_at
        ):
            _reject("initial_candidate_frontier_event_mismatch")

        raw_detail = str(event.detail_canonical_json or "")
        try:
            detail = json.loads(raw_detail)
            canonical_detail = json.dumps(
                detail,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CapturedPaperInitialCandidateReaderUnavailable(
                "initial_candidate_frontier_event_invalid"
            ) from exc
        if not (
            isinstance(detail, dict)
            and frozenset(detail) == _FRONTIER_EVENT_KEYS
            and raw_detail == canonical_detail
            and hashlib.sha256(raw_detail.encode("utf-8")).hexdigest()
            == event.event_sha256
            and detail.get("schema_version") == EVENT_SCHEMA_VERSION
            and detail.get("frontier_id") == frontier.id
            and detail.get("event_sequence") == expected_sequence
            and detail.get("event_type") == "batch_applied"
            and detail.get("expected_version") == event.expected_version
            and detail.get("next_version") == event.next_version
            and detail.get("expected_frontier_sha256")
            == event.expected_frontier_sha256
            and detail.get("previous_event_sha256")
            == event.previous_event_sha256
            and detail.get("batch_sha256") == event.batch_sha256
            and detail.get("gap_sha256") is None
            and detail.get("source_sequence_from")
            == event.source_sequence_from
            and detail.get("source_sequence_through")
            == event.source_sequence_through
            and isinstance(detail.get("detail"), dict)
            and frozenset(detail["detail"]) == _FRONTIER_BATCH_DETAIL_KEYS
            and detail["detail"].get("authority_sha256")
            == authority.authority_sha256
            and isinstance(detail.get("next_state"), dict)
            and detail.get("recorded_at")
            == _iso(event.recorded_at, "initial_candidate_frontier_recorded_at")
        ):
            _reject("initial_candidate_frontier_event_invalid")

        batch_detail = detail["detail"]
        next_state = detail["next_state"]
        try:
            batch_watermark_at = _event_iso_utc(
                batch_detail["watermark_at"],
                "initial_candidate_frontier_watermark_at",
            )
            batch_read_at = _event_iso_utc(
                batch_detail["read_at"],
                "initial_candidate_frontier_batch_read_at",
            )
            next_event_at = _event_iso_utc(
                next_state["last_source_event_at"],
                "initial_candidate_frontier_event_at",
                allow_none=True,
            )
            next_available_at = _event_iso_utc(
                next_state["last_source_available_at"],
                "initial_candidate_frontier_available_at",
                allow_none=True,
            )
        except KeyError as exc:
            raise CapturedPaperInitialCandidateReaderUnavailable(
                "initial_candidate_frontier_event_invalid"
            ) from exc
        raw_observation_sha256s = batch_detail.get("observation_sha256s")
        raw_route_updates = batch_detail.get("route_state_updates")
        if not (
            frozenset(next_state)
            == {
                "last_source_sequence",
                "last_source_event_at",
                "last_source_available_at",
                "last_batch_sha256",
                "status",
                "gap_count",
            }
            and next_state.get("last_source_sequence")
            == event.source_sequence_through
            and next_state.get("last_batch_sha256") == event.batch_sha256
            and next_state.get("status") == "ready"
            and next_state.get("gap_count") == 0
            and isinstance(raw_observation_sha256s, list)
            and all(
                isinstance(value, str)
                and _SHA256_RE.fullmatch(value) is not None
                and value not in observation_sha256s
                for value in raw_observation_sha256s
            )
            and len(raw_observation_sha256s)
            == len(set(raw_observation_sha256s))
            and isinstance(raw_route_updates, list)
            and len(raw_route_updates)
            == event.source_sequence_through - event.source_sequence_from
            and isinstance(batch_watermark_at, datetime)
            and isinstance(batch_read_at, datetime)
            and batch_watermark_at <= batch_read_at <= recorded_at <= read_at
            and next_event_at is not None
            and next_event_at <= batch_watermark_at
            and next_available_at is not None
            and next_available_at <= batch_read_at
            and (
                previous_values["last_source_event_at"] is None
                or (
                    next_event_at is not None
                    and next_event_at
                    >= _db_utc(
                        previous_values["last_source_event_at"],
                        "initial_candidate_frontier_event_at",
                    )
                )
            )
            and (
                previous_values["last_source_available_at"] is None
                or (
                    next_available_at is not None
                    and next_available_at
                    >= _db_utc(
                        previous_values["last_source_available_at"],
                        "initial_candidate_frontier_available_at",
                    )
                )
            )
        ):
            _reject("initial_candidate_frontier_event_invalid")

        event_route_states = dict(expected_route_states)
        parsed_updates: list[tuple[Mapping[str, Any], datetime, datetime]] = []
        event_routes: set[tuple[str, int]] = set()
        eligible_evidence: set[str] = set()
        for source_sequence, raw_update in zip(
            range(
                event.source_sequence_from + 1,
                event.source_sequence_through + 1,
            ),
            raw_route_updates,
        ):
            if not isinstance(raw_update, Mapping):
                _reject("initial_candidate_route_state_invalid")
            update = dict(raw_update)
            update_body = {
                key: value for key, value in update.items() if key != "update_sha256"
            }
            reason_codes = update.get("reason_codes")
            route = (update.get("symbol"), update.get("variant_id"))
            if not (
                frozenset(update) == _ROUTE_STATE_UPDATE_KEYS
                and update.get("schema_version")
                == ROUTE_STATE_UPDATE_SCHEMA_VERSION
                and update.get("source_sequence") == source_sequence
                and isinstance(route[0], str)
                and _SYMBOL_RE.fullmatch(route[0]) is not None
                and type(route[1]) is int
                and route[1] in authority.variant_ids
                and route not in event_routes
                and update.get("state")
                in {ROUTE_ELIGIBLE, ROUTE_COVERAGE_UNAVAILABLE}
                and all(
                    _SHA256_RE.fullmatch(str(update.get(key) or "")) is not None
                    for key in (
                        "evidence_sha256",
                        "bundle_sha256",
                        "scoring_authority_sha256",
                        "score_result_sha256",
                        "update_sha256",
                    )
                )
                and isinstance(reason_codes, list)
                and len(reason_codes) == len(set(reason_codes))
                and all(
                    isinstance(reason, str) and 0 < len(reason) <= 128
                    for reason in reason_codes
                )
                and (update.get("state") == ROUTE_ELIGIBLE)
                == (not reason_codes)
                and _sha256_json(update_body) == update.get("update_sha256")
            ):
                _reject("initial_candidate_route_state_invalid")
            update_event_at = _event_iso_utc(
                update.get("source_event_at"),
                "initial_candidate_route_state_event_at",
            )
            update_available_at = _event_iso_utc(
                update.get("source_available_at"),
                "initial_candidate_route_state_available_at",
            )
            if not (
                isinstance(update_event_at, datetime)
                and isinstance(update_available_at, datetime)
                and update_event_at <= update_available_at <= batch_read_at
                and update_event_at <= batch_watermark_at
            ):
                _reject("initial_candidate_route_state_invalid")
            if update.get("state") == ROUTE_ELIGIBLE:
                eligible_evidence.add(str(update["evidence_sha256"]))
            elif update.get("evidence_sha256") != update.get(
                "score_result_sha256"
            ):
                _reject("initial_candidate_route_state_invalid")
            previous_route = event_route_states.get(route)
            if previous_route is not None:
                previous_event_at = _event_iso_utc(
                    previous_route["body"]["source_event_at"],
                    "initial_candidate_route_state_event_at",
                )
                previous_available_at = _event_iso_utc(
                    previous_route["body"]["source_available_at"],
                    "initial_candidate_route_state_available_at",
                )
                if not (
                    isinstance(previous_event_at, datetime)
                    and isinstance(previous_available_at, datetime)
                    and update_event_at >= previous_event_at
                    and update_available_at >= previous_available_at
                ):
                    _reject("initial_candidate_route_state_clock_regressed")
            version = (
                int(previous_route["body"]["version"]) + 1
                if previous_route is not None
                else 1
            )
            state_body = _expected_route_state_body(
                authority=authority,
                update_row=update,
                batch_sha256=str(event.batch_sha256),
                version=version,
            )
            event_route_states[route] = {
                "body": state_body,
                "state_sha256": _sha256_json(state_body),
                "created_at": (
                    previous_route["created_at"]
                    if previous_route is not None
                    else recorded_at
                ),
                "updated_at": recorded_at,
                "update_sha256": update["update_sha256"],
            }
            event_routes.add(route)
            parsed_updates.append((update, update_event_at, update_available_at))
        if not (
            eligible_evidence == set(raw_observation_sha256s)
            and max(item[1] for item in parsed_updates) == next_event_at
            and max(item[2] for item in parsed_updates) == next_available_at
        ):
            _reject("initial_candidate_route_state_invalid")
        expected_route_states = event_route_states

        normalized_next_state = {
            **next_state,
            "last_source_event_at": next_event_at,
            "last_source_available_at": next_available_at,
        }
        next_values = {
            **expected_authority,
            **normalized_next_state,
            "version": event.next_version,
            "event_sequence": event.event_sequence,
            "last_event_sha256": event.event_sha256,
        }
        next_frontier_sha256 = _sha256_json(_frontier_body(next_values))
        if event.next_frontier_sha256 != next_frontier_sha256:
            _reject("initial_candidate_frontier_event_mismatch")
        if event.batch_sha256 in batch_details:
            _reject("initial_candidate_frontier_event_invalid")
        observation_sha256s.update(raw_observation_sha256s)
        batch_details[str(event.batch_sha256)] = {
            **batch_detail,
            "source_sequence_from": event.source_sequence_from,
            "source_sequence_through": event.source_sequence_through,
            "batch_read_at": batch_read_at,
            "batch_recorded_at": recorded_at,
        }
        previous_values = next_values
        previous_frontier_sha256 = next_frontier_sha256
        previous_event_sha256 = event.event_sha256

    if not (
        previous_frontier_sha256 == frontier.frontier_sha256
        and previous_event_sha256 == frontier.last_event_sha256
        and previous_values["last_source_sequence"]
        == frontier.last_source_sequence
        and previous_values["last_batch_sha256"]
        == frontier.last_batch_sha256
        and previous_values["version"] == frontier.version
        and previous_values["event_sequence"] == frontier.event_sequence
        and _iso(
            previous_values["last_source_event_at"],
            "initial_candidate_frontier_event_at",
        )
        == _iso(frontier.last_source_event_at, "initial_candidate_frontier_event_at")
        and _iso(
            previous_values["last_source_available_at"],
            "initial_candidate_frontier_available_at",
        )
        == _iso(
            frontier.last_source_available_at,
            "initial_candidate_frontier_available_at",
        )
    ):
        _reject("initial_candidate_frontier_event_mismatch")
    return batch_details, expected_route_states


def _validate_variant(
    variant: Any,
    *,
    authority: CapturedPaperSelectionAuthority,
    binding: Any,
) -> None:
    if not isinstance(variant, MomentumStrategyVariant):
        _reject("initial_candidate_reader_variant_invalid")
    marker = dict(variant.refinement_meta_json or {}).get(BINDING_META_KEY)
    if not isinstance(marker, Mapping):
        _reject("initial_candidate_reader_variant_authority_mismatch")
    expected_marker = {
        "schema_version": BINDING_META_SCHEMA_VERSION,
        "account_scope": authority.account_scope,
        "execution_family": authority.execution_family,
        "expected_account_id": authority.expected_account_id,
        "activation_generation": authority.activation_generation,
        "source_family": binding.family,
        "source_version": binding.version,
        "policy_sha256": authority.policy_sha256,
        "settings_projection_sha256": authority.settings_projection_sha256,
        "code_build_sha256": authority.code_build_sha256,
        "strategy_params_overridden": False,
        "paper_order_submission_authorized": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    if not (
        int(variant.id or 0) == binding.variant_id
        and str(variant.family or "") == binding.family
        and str(variant.variant_key or "") == binding.variant_key
        and int(variant.version or 0) == binding.version
        and bool(variant.is_active)
        and str(variant.execution_family or "") == authority.execution_family
        and captured_paper_initial_variant_sha256(variant)
        == binding.target_after_sha256
        and frozenset(marker) == _BINDING_META_KEYS
        and all(marker.get(key) == value for key, value in expected_marker.items())
        and type(marker.get("source_variant_id")) is int
        and marker.get("source_variant_id") > 0
        and variant.parent_variant_id == marker.get("source_variant_id")
        and _SHA256_RE.fullmatch(str(marker.get("source_variant_sha256") or ""))
        is not None
        and _SHA256_RE.fullmatch(str(marker.get("plan_sha256") or "")) is not None
        and isinstance(marker.get("bound_at"), str)
    ):
        _reject("initial_candidate_reader_variant_authority_mismatch")


def _validate_viability_provenance(
    viability: Any,
    *,
    authority: CapturedPaperSelectionAuthority,
    binding: Any,
    frontier: CapturedPaperSelectionFrontier,
    batch_details: Mapping[str, Mapping[str, Any]],
    read_at: datetime,
) -> Mapping[str, Any]:
    if not isinstance(viability, MomentumSymbolViability):
        _reject("initial_candidate_reader_row_invalid")
    if viability.source_node_id != SOURCE_NODE_ID:
        _reject("initial_candidate_reader_source_mismatch")
    containers: list[Mapping[str, Any]] = []
    for raw in (
        viability.execution_readiness_json,
        viability.explain_json,
        viability.evidence_window_json,
    ):
        if not isinstance(raw, Mapping):
            _reject("initial_candidate_reader_provenance_invalid")
        provenance = raw.get(PROVENANCE_KEY)
        if not isinstance(provenance, Mapping):
            _reject("initial_candidate_reader_provenance_invalid")
        containers.append(dict(provenance))
    provenance = containers[0]
    if not all(item == provenance for item in containers[1:]):
        _reject("initial_candidate_reader_provenance_mismatch")
    batch_sha256 = str(provenance.get("batch_sha256") or "")
    ancestor_batch_detail = batch_details.get(batch_sha256)
    if ancestor_batch_detail is None:
        _reject("initial_candidate_reader_provenance_not_ancestor")
    expected = {
        "schema_version": _PROVENANCE_SCHEMA_VERSION,
        "account_scope": authority.account_scope,
        "expected_account_id": authority.expected_account_id,
        "activation_generation": authority.activation_generation,
        "authority_sha256": authority.authority_sha256,
        "policy_sha256": authority.policy_sha256,
        "settings_projection_sha256": authority.settings_projection_sha256,
        "code_build_sha256": authority.code_build_sha256,
        "variant_set_sha256": authority.variant_set_sha256,
        "variant_id": binding.variant_id,
        "batch_sha256": batch_sha256,
        "paper_only_strategy_override": False,
        "live_cash_authorized": False,
    }
    observation_sha256 = str(provenance.get("observation_sha256") or "")
    source_sequence = provenance.get("source_sequence")
    if not (
        frozenset(provenance) == _PROVENANCE_KEYS
        and all(provenance.get(key) == value for key, value in expected.items())
        and _SHA256_RE.fullmatch(observation_sha256) is not None
        and _SHA256_RE.fullmatch(
            str(provenance.get("queue_receipt_sha256") or "")
        )
        is not None
        and _SHA256_RE.fullmatch(
            str(provenance.get("coverage_receipt_sha256") or "")
        )
        is not None
        and _TOKEN_RE.fullmatch(str(provenance.get("source_name") or ""))
        is not None
        and _canonical_uuid(provenance.get("source_generation")) is not None
        and type(source_sequence) is int
        and _event_source_from(ancestor_batch_detail) < source_sequence
        <= ancestor_batch_detail.get("source_sequence_through")
        and observation_sha256
        in tuple(ancestor_batch_detail.get("observation_sha256s") or ())
        and provenance.get("source_name")
        == ancestor_batch_detail.get("source_name")
        and provenance.get("source_generation")
        == ancestor_batch_detail.get("source_generation")
        and provenance.get("queue_receipt_sha256")
        == ancestor_batch_detail.get("queue_receipt_sha256")
        and provenance.get("coverage_receipt_sha256")
        == ancestor_batch_detail.get("coverage_receipt_sha256")
    ):
        _reject("initial_candidate_reader_provenance_mismatch")
    freshness = _db_utc(
        viability.freshness_ts,
        "initial_candidate_reader_freshness",
    )
    updated_at = _db_utc(
        viability.updated_at,
        "initial_candidate_reader_updated_at",
    )
    frontier_available = _db_utc(
        frontier.last_source_available_at,
        "initial_candidate_frontier_available_at",
    )
    batch_read_at = ancestor_batch_detail.get("batch_read_at")
    batch_recorded_at = ancestor_batch_detail.get("batch_recorded_at")
    if not isinstance(batch_read_at, datetime) or not isinstance(
        batch_recorded_at, datetime
    ):
        _reject("initial_candidate_frontier_event_invalid")
    if (
        freshness > read_at
        or freshness > batch_read_at
        or freshness > frontier_available
        or updated_at != batch_recorded_at
        or updated_at > read_at
    ):
        _reject("initial_candidate_reader_row_from_future")
    return provenance


def _event_source_from(detail: Mapping[str, Any]) -> int:
    value = detail.get("source_sequence_from")
    if type(value) is not int or value < 0:
        _reject("initial_candidate_frontier_event_invalid")
    return value


def _viability_observation_sha256(
    viability: MomentumSymbolViability,
    *,
    provenance: Mapping[str, Any],
    route_state: CapturedPaperSelectionRouteState,
) -> str:
    def without_provenance(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            _reject("initial_candidate_reader_provenance_invalid")
        normalized = dict(raw)
        if normalized.pop(PROVENANCE_KEY, None) != provenance:
            _reject("initial_candidate_reader_provenance_mismatch")
        return normalized

    freshness = _db_utc(
        viability.freshness_ts,
        "initial_candidate_reader_freshness",
    )
    route_available = _db_utc(
        route_state.source_available_at,
        "initial_candidate_route_state_available_at",
    )
    if freshness != route_available:
        _reject("initial_candidate_route_state_mismatch")
    body = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "source_sequence": provenance.get("source_sequence"),
        "source_event_at": _iso(
            route_state.source_event_at,
            "initial_candidate_route_state_event_at",
        ),
        "source_available_at": _iso(
            route_state.source_available_at,
            "initial_candidate_route_state_available_at",
        ),
        "symbol": viability.symbol,
        "variant_id": viability.variant_id,
        "viability_score": float(viability.viability_score),
        "paper_eligible": bool(viability.paper_eligible),
        "live_eligible": bool(viability.live_eligible),
        "regime_snapshot_json": dict(viability.regime_snapshot_json or {}),
        "execution_readiness_json": without_provenance(
            viability.execution_readiness_json
        ),
        "explain_json": without_provenance(viability.explain_json),
        "evidence_window_json": without_provenance(
            viability.evidence_window_json
        ),
        "correlation_id": viability.correlation_id,
    }
    return _sha256_json(body)


class SqlAlchemyCapturedPaperInitialCandidateReader(
    CapturedPaperInitialCandidateReadPort
):
    """Exact detached candidate read port bound to one SQLAlchemy Engine."""

    def __init__(
        self,
        bind: Engine,
        *,
        authority: CapturedPaperSelectionAuthority,
    ) -> None:
        if not isinstance(bind, Engine):
            _reject("initial_candidate_reader_engine_invalid")
        if not _authority_is_exact(authority):
            _reject("initial_candidate_reader_authority_invalid")
        self._bind = bind
        self._authority = authority
        self._cache_lock = threading.RLock()
        self._cached_frontier_key: tuple[int, int, str, str] | None = None
        self._cached_batch_details: Mapping[str, Mapping[str, Any]] | None = None
        self._cached_route_states: Mapping[
            tuple[str, int], Mapping[str, Any]
        ] | None = None
        self._cached_max_recorded_at: datetime | None = None

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    @property
    def mutation_allowed(self) -> bool:
        return False

    def read_candidates(
        self,
        *,
        user_id: int,
        symbol: str,
        decision_at: datetime,
    ) -> CapturedPaperInitialCandidateRead:
        exact_user_id = _positive_user_id(user_id)
        exact_symbol = _symbol(symbol)
        decision_utc = _aware_utc(
            decision_at,
            "initial_candidate_reader_decision_at",
        )
        authority = self._authority
        if not _authority_is_exact(authority):
            _reject("initial_candidate_reader_authority_invalid")

        db = Session(bind=self._bind, expire_on_commit=False)
        try:
            # This must be the first statement.  The database, rather than a
            # process wall clock, owns the stable snapshot/read frontier.
            db.execute(text(_READ_ONLY_TRANSACTION_SQL))
            raw_read_at = db.execute(
                text(_READ_AT_SQL),
                {"decision_at": decision_utc},
            ).scalar_one()
            read_at = _aware_utc(
                raw_read_at,
                "initial_candidate_reader_read_at",
            )

            frontier = (
                db.query(CapturedPaperSelectionFrontier)
                .filter(
                    CapturedPaperSelectionFrontier.account_scope
                    == authority.account_scope,
                    CapturedPaperSelectionFrontier.expected_account_id
                    == authority.expected_account_id,
                    CapturedPaperSelectionFrontier.activation_generation
                    == authority.activation_generation,
                )
                .one_or_none()
            )
            if frontier is None:
                _reject("initial_candidate_frontier_unavailable")
            cache_key = _validated_frontier_cache_key(
                frontier,
                authority=authority,
                read_at=read_at,
            )
            with self._cache_lock:
                cache_hit = bool(
                    self._cached_frontier_key == cache_key
                    and self._cached_batch_details is not None
                    and self._cached_route_states is not None
                    and self._cached_max_recorded_at is not None
                    and self._cached_max_recorded_at <= read_at
                )
                if cache_hit:
                    batch_details = copy.deepcopy(
                        self._cached_batch_details
                    )
                    expected_route_states = copy.deepcopy(
                        self._cached_route_states
                    )
            if not cache_hit:
                frontier_events = (
                    db.query(CapturedPaperSelectionFrontierEvent)
                    .filter(
                        CapturedPaperSelectionFrontierEvent.frontier_id
                        == frontier.id,
                    )
                    .order_by(
                        CapturedPaperSelectionFrontierEvent.event_sequence.asc()
                    )
                    .all()
                )
                batch_details, expected_route_states = _validate_frontier(
                    frontier,
                    frontier_events,
                    authority=authority,
                    read_at=read_at,
                )
                max_recorded_at = max(
                    _db_utc(
                        event.recorded_at,
                        "initial_candidate_frontier_recorded_at",
                    )
                    for event in frontier_events
                )
                with self._cache_lock:
                    self._cached_frontier_key = cache_key
                    self._cached_batch_details = copy.deepcopy(batch_details)
                    self._cached_route_states = copy.deepcopy(
                        expected_route_states
                    )
                    self._cached_max_recorded_at = max_recorded_at

            bound_variants = (
                db.query(MomentumStrategyVariant)
                .filter(MomentumStrategyVariant.id.in_(authority.variant_ids))
                .order_by(MomentumStrategyVariant.id.asc())
                .all()
            )
            if (
                len(bound_variants) != len(authority.variant_bindings)
                or len({int(row.id or 0) for row in bound_variants})
                != len(bound_variants)
            ):
                _reject("initial_candidate_reader_variant_set_unavailable")
            variants_by_id = {
                int(row.id): row for row in bound_variants
            }
            bindings_by_id = {
                binding.variant_id: binding
                for binding in authority.variant_bindings
            }
            if set(variants_by_id) != set(bindings_by_id):
                _reject("initial_candidate_reader_variant_set_unavailable")
            for variant_id, binding in bindings_by_id.items():
                _validate_variant(
                    variants_by_id[variant_id],
                    authority=authority,
                    binding=binding,
                )

            route_state_rows = (
                db.query(CapturedPaperSelectionRouteState)
                .filter(
                    CapturedPaperSelectionRouteState.account_scope
                    == authority.account_scope,
                    CapturedPaperSelectionRouteState.expected_account_id
                    == authority.expected_account_id,
                    CapturedPaperSelectionRouteState.activation_generation
                    == authority.activation_generation,
                    CapturedPaperSelectionRouteState.symbol == exact_symbol,
                    CapturedPaperSelectionRouteState.variant_id.in_(
                        authority.variant_ids
                    ),
                )
                .order_by(CapturedPaperSelectionRouteState.variant_id.asc())
                .all()
            )
            expected_symbol_states = {
                route: state
                for route, state in expected_route_states.items()
                if route[0] == exact_symbol
            }
            route_states_by_key: dict[
                tuple[str, int], CapturedPaperSelectionRouteState
            ] = {}
            for state_row in route_state_rows:
                if not isinstance(state_row, CapturedPaperSelectionRouteState):
                    _reject("initial_candidate_route_state_invalid")
                route = (state_row.symbol, int(state_row.variant_id or 0))
                expected_state = expected_symbol_states.get(route)
                if not (
                    expected_state is not None
                    and route not in route_states_by_key
                    and _route_state_body(state_row) == expected_state["body"]
                    and state_row.state_sha256
                    == expected_state["state_sha256"]
                    and _sha256_json(_route_state_body(state_row))
                    == state_row.state_sha256
                    and _db_utc(
                        state_row.created_at,
                        "initial_candidate_route_state_created_at",
                    )
                    == expected_state["created_at"]
                    and _db_utc(
                        state_row.updated_at,
                        "initial_candidate_route_state_updated_at",
                    )
                    == expected_state["updated_at"]
                    and expected_state["updated_at"] <= read_at
                    and state_row.latest_source_sequence
                    <= frontier.last_source_sequence
                ):
                    _reject("initial_candidate_route_state_invalid")
                route_states_by_key[route] = state_row
            if set(route_states_by_key) != set(expected_symbol_states):
                _reject("initial_candidate_route_state_unavailable")

            pairs = (
                db.query(MomentumSymbolViability, MomentumStrategyVariant)
                .join(
                    MomentumStrategyVariant,
                    MomentumStrategyVariant.id
                    == MomentumSymbolViability.variant_id,
                )
                .filter(
                    MomentumSymbolViability.symbol == exact_symbol,
                    MomentumSymbolViability.scope == "symbol",
                    MomentumSymbolViability.variant_id.in_(
                        authority.variant_ids
                    ),
                    MomentumSymbolViability.source_node_id == SOURCE_NODE_ID,
                    MomentumStrategyVariant.id.in_(authority.variant_ids),
                )
                .order_by(
                    MomentumStrategyVariant.id.asc(),
                    MomentumSymbolViability.id.asc(),
                )
                .all()
            )
            exact_pairs: list[
                tuple[MomentumSymbolViability, MomentumStrategyVariant]
            ] = []
            observed_viability_routes: set[tuple[str, int]] = set()
            for pair in pairs:
                try:
                    viability, variant = pair
                except (TypeError, ValueError) as exc:
                    raise CapturedPaperInitialCandidateReaderUnavailable(
                        "initial_candidate_reader_row_invalid"
                    ) from exc
                if not isinstance(
                    viability, MomentumSymbolViability
                ) or not isinstance(variant, MomentumStrategyVariant):
                    _reject("initial_candidate_reader_row_invalid")
                if (
                    viability.symbol != exact_symbol
                    or viability.scope != "symbol"
                    or int(viability.variant_id or 0) != int(variant.id or 0)
                ):
                    _reject("initial_candidate_reader_row_scope_mismatch")
                binding = bindings_by_id.get(int(variant.id or 0))
                if binding is None:
                    _reject("initial_candidate_reader_variant_authority_mismatch")
                if variant is not variants_by_id[int(variant.id)]:
                    _validate_variant(
                        variant,
                        authority=authority,
                        binding=binding,
                    )
                route = (exact_symbol, int(variant.id))
                route_state = route_states_by_key.get(route)
                observed_viability_routes.add(route)
                if route_state is None or route_state.state != ROUTE_ELIGIBLE:
                    continue
                provenance = _validate_viability_provenance(
                    viability,
                    authority=authority,
                    binding=binding,
                    frontier=frontier,
                    batch_details=batch_details,
                    read_at=read_at,
                )
                if not (
                    route_state.latest_source_sequence
                    == provenance.get("source_sequence")
                    and route_state.evidence_sha256
                    == provenance.get("observation_sha256")
                    and route_state.batch_sha256 == provenance.get("batch_sha256")
                    and _viability_observation_sha256(
                        viability,
                        provenance=provenance,
                        route_state=route_state,
                    )
                    == route_state.evidence_sha256
                ):
                    _reject("initial_candidate_route_state_mismatch")
                exact_pairs.append((viability, variant))

            eligible_routes = {
                route
                for route, state_row in route_states_by_key.items()
                if state_row.state == ROUTE_ELIGIBLE
            }
            if not eligible_routes.issubset(observed_viability_routes):
                _reject("initial_candidate_reader_row_unavailable")

            exact_pairs.sort(
                key=lambda pair: (
                    int(pair[1].id or 0),
                    int(pair[0].id or 0),
                )
            )
            detached_ids: set[int] = set()
            rows: list[CapturedPaperInitialCandidateRow] = []
            for viability, variant in exact_pairs:
                for orm_row in (viability, variant):
                    object_id = id(orm_row)
                    if object_id not in detached_ids:
                        db.expunge(orm_row)
                        detached_ids.add(object_id)
                rows.append(
                    CapturedPaperInitialCandidateRow(
                        variant=variant,
                        viability=viability,
                    )
                )
            return CapturedPaperInitialCandidateRead(
                user_id=exact_user_id,
                symbol=exact_symbol,
                read_at=read_at,
                rows=tuple(rows),
            )
        finally:
            try:
                db.rollback()
            finally:
                db.close()


__all__ = [
    "CapturedPaperInitialCandidateReaderUnavailable",
    "SqlAlchemyCapturedPaperInitialCandidateReader",
]
