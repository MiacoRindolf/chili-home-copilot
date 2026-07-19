"""Reversible strategy-variant routing for the dedicated Alpaca PAPER lane.

This module deliberately does *not* start services, contact a broker, or commit a
database transaction.  It prepares and applies a bounded set of route-specific
strategy clones inside the caller's transaction.  The host cutover owns the
surrounding quiescence, durable journal, commit, and compensation sequence.

The source strategy remains untouched.  A PAPER clone preserves the source
strategy parameters byte-for-byte at the JSON value level and adds provenance
only to ``refinement_meta_json``.  Rollback deactivates an exact, receipt-bound
clone; it never deletes a row that may already be referenced by evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.trading import MomentumStrategyVariant

from .captured_paper_initial_admission import (
    captured_paper_initial_variant_sha256,
)
from .variants import (
    CAPTURED_PAPER_VARIANT_KEY_PREFIX,
    iter_momentum_families,
)


ACCOUNT_SCOPE = "alpaca:paper"
EXECUTION_FAMILY = "alpaca_spot"
VARIANT_KEY_PREFIX = CAPTURED_PAPER_VARIANT_KEY_PREFIX
BINDING_META_KEY = "captured_paper_variant_binding"

AUTHORITY_SCHEMA_VERSION = "chili.captured-paper-variant-binding-authority.v1"
PLAN_SCHEMA_VERSION = "chili.captured-paper-variant-binding-plan.v1"
APPLICATION_SCHEMA_VERSION = "chili.captured-paper-variant-binding-application.v1"
ROLLBACK_SCHEMA_VERSION = "chili.captured-paper-variant-binding-rollback.v2"
RECOVERY_SCHEMA_VERSION = "chili.captured-paper-variant-binding-recovery.v1"
BINDING_META_SCHEMA_VERSION = "chili.captured-paper-variant-binding-meta.v1"
APPLICATION_RECEIPT_SCHEMA_VERSION = (
    "chili.captured-paper-variant-application-receipt.v1"
)
NOT_APPLIED_PROOF_SCHEMA_VERSION = (
    "chili.captured-paper-variant-application-not-applied.v1"
)
APPLICATION_EVENT_SCHEMA_VERSION = (
    "chili.captured-paper-variant-application-event.v1"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TARGET_STATES = frozenset({"absent", "inactive_reusable", "already_applied"})
# ASCII-ish ``CPV1`` namespace.  Every apply locks the deterministic target
# keys in lexical order, closing the no-row/gap race that ``FOR UPDATE`` alone
# cannot close under PostgreSQL READ COMMITTED.
_VARIANT_BINDING_LOCK_NAMESPACE = 0x43505631
_VARIANT_BINDING_LOCK_SQL = text(
    "SELECT pg_advisory_xact_lock(:namespace, hashtext(:target_key))"
)


class CapturedPaperVariantBindingError(RuntimeError):
    """Typed fail-closed rejection from the variant-binding boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(f"{self.code}: {message}")


def _reject(code: str, message: str) -> None:
    raise CapturedPaperVariantBindingError(code, message)


def _sha256(value: Any, field_name: str) -> str:
    text = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(text) is None:
        _reject("AUTHORITY_INVALID", f"{field_name} must be a lowercase SHA-256")
    return text


def _canonical_uuid(value: Any, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperVariantBindingError(
            "AUTHORITY_INVALID", f"{field_name} must be a canonical UUID"
        ) from exc


def _utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        _reject("AUTHORITY_INVALID", f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        _reject("AUTHORITY_INVALID", f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _db_naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        return _iso_utc(normalized)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        _reject("CANONICALIZATION_FAILED", "non-finite JSON number is forbidden")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    _reject(
        "CANONICALIZATION_FAILED",
        f"unsupported canonical value type {type(value).__name__}",
    )


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        _canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class CapturedPaperVariantBindingAuthority:
    expected_account_id: str
    activation_generation: str
    policy_sha256: str
    settings_projection_sha256: str
    code_build_sha256: str
    bound_at: datetime
    account_scope: str = ACCOUNT_SCOPE
    execution_family: str = EXECUTION_FAMILY

    def __post_init__(self) -> None:
        if self.account_scope != ACCOUNT_SCOPE:
            _reject("AUTHORITY_INVALID", "account scope must be alpaca:paper")
        if self.execution_family != EXECUTION_FAMILY:
            _reject("AUTHORITY_INVALID", "execution family must be alpaca_spot")
        object.__setattr__(
            self,
            "expected_account_id",
            _canonical_uuid(self.expected_account_id, "expected_account_id"),
        )
        object.__setattr__(
            self,
            "activation_generation",
            _canonical_uuid(self.activation_generation, "activation_generation"),
        )
        for field_name in (
            "policy_sha256",
            "settings_projection_sha256",
            "code_build_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _sha256(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "bound_at", _utc(self.bound_at, "bound_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": AUTHORITY_SCHEMA_VERSION,
            "account_scope": self.account_scope,
            "execution_family": self.execution_family,
            "expected_account_id": self.expected_account_id,
            "activation_generation": self.activation_generation,
            "policy_sha256": self.policy_sha256,
            "settings_projection_sha256": self.settings_projection_sha256,
            "code_build_sha256": self.code_build_sha256,
            "bound_at": _iso_utc(self.bound_at),
            "paper_order_submission_authorized": False,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }


@dataclass(frozen=True, slots=True)
class CapturedPaperVariantBindingPlanItem:
    family: str
    version: int
    source_variant_id: int
    source_variant_sha256: str
    source_parent_variant_id: int | None
    target_variant_key: str
    target_variant_id: int | None
    target_state: str
    target_before_sha256: str | None
    target_projection_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "version": self.version,
            "source_variant_id": self.source_variant_id,
            "source_variant_sha256": self.source_variant_sha256,
            "source_parent_variant_id": self.source_parent_variant_id,
            "target_variant_key": self.target_variant_key,
            "target_variant_id": self.target_variant_id,
            "target_state": self.target_state,
            "target_before_sha256": self.target_before_sha256,
            "target_projection_sha256": self.target_projection_sha256,
        }


@dataclass(frozen=True, slots=True)
class CapturedPaperVariantBindingPlan:
    authority: CapturedPaperVariantBindingAuthority
    items: tuple[CapturedPaperVariantBindingPlanItem, ...]
    plan_sha256: str

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "authority": self.authority.to_dict(),
            "items": [item.to_dict() for item in self.items],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "plan_sha256": self.plan_sha256}


@dataclass(frozen=True, slots=True)
class CapturedPaperVariantBindingApplicationItem:
    family: str
    version: int
    source_variant_id: int
    source_variant_sha256: str
    target_variant_key: str
    target_variant_id: int
    target_before_sha256: str | None
    target_after_sha256: str
    action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "version": self.version,
            "source_variant_id": self.source_variant_id,
            "source_variant_sha256": self.source_variant_sha256,
            "target_variant_key": self.target_variant_key,
            "target_variant_id": self.target_variant_id,
            "target_before_sha256": self.target_before_sha256,
            "target_after_sha256": self.target_after_sha256,
            "action": self.action,
        }


@dataclass(frozen=True, slots=True)
class CapturedPaperVariantBindingApplication:
    plan: CapturedPaperVariantBindingPlan
    items: tuple[CapturedPaperVariantBindingApplicationItem, ...]
    application_sha256: str

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": APPLICATION_SCHEMA_VERSION,
            "plan_sha256": self.plan.plan_sha256,
            "authority": self.plan.authority.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "paper_order_submission_authorized": False,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "application_sha256": self.application_sha256}


@dataclass(frozen=True, slots=True)
class CapturedPaperVariantApplicationReceipt:
    receipt_id: int
    application: CapturedPaperVariantBindingApplication
    activation_manifest_sha256: str
    status: str
    rollback: Mapping[str, Any] | None
    version: int
    latest_event_sha256: str


def _parse_iso_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        _reject("RECEIPT_INVALID", f"{field_name} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CapturedPaperVariantBindingError(
            "RECEIPT_INVALID", f"{field_name} must be canonical UTC"
        ) from exc
    if _iso_utc(parsed) != value:
        _reject("RECEIPT_INVALID", f"{field_name} must be canonical UTC")
    return parsed


def _application_receipt_payload(
    application: CapturedPaperVariantBindingApplication,
) -> dict[str, Any]:
    return {
        "schema_version": APPLICATION_RECEIPT_SCHEMA_VERSION,
        "plan": application.plan.to_dict(),
        "application": application.to_dict(),
    }


def _application_from_receipt_payload(
    raw: Mapping[str, Any],
) -> CapturedPaperVariantBindingApplication:
    if set(raw) != {"schema_version", "plan", "application"} or raw.get(
        "schema_version"
    ) != APPLICATION_RECEIPT_SCHEMA_VERSION:
        _reject("RECEIPT_INVALID", "variant application receipt envelope is invalid")
    plan_raw = raw.get("plan")
    application_raw = raw.get("application")
    if not isinstance(plan_raw, Mapping) or not isinstance(
        application_raw, Mapping
    ):
        _reject("RECEIPT_INVALID", "variant application receipt payload is invalid")
    authority_raw = plan_raw.get("authority")
    plan_items_raw = plan_raw.get("items")
    if not isinstance(authority_raw, Mapping) or not isinstance(
        plan_items_raw, list
    ):
        _reject("RECEIPT_INVALID", "variant application plan payload is invalid")
    authority = CapturedPaperVariantBindingAuthority(
        expected_account_id=authority_raw.get("expected_account_id"),
        activation_generation=authority_raw.get("activation_generation"),
        policy_sha256=authority_raw.get("policy_sha256"),
        settings_projection_sha256=authority_raw.get(
            "settings_projection_sha256"
        ),
        code_build_sha256=authority_raw.get("code_build_sha256"),
        bound_at=_parse_iso_utc(authority_raw.get("bound_at"), "bound_at"),
        account_scope=authority_raw.get("account_scope"),
        execution_family=authority_raw.get("execution_family"),
    )
    if authority.to_dict() != dict(authority_raw):
        _reject("RECEIPT_INVALID", "variant application authority is not exact")
    try:
        plan_items = tuple(
            CapturedPaperVariantBindingPlanItem(
                family=item["family"],
                version=item["version"],
                source_variant_id=item["source_variant_id"],
                source_variant_sha256=item["source_variant_sha256"],
                source_parent_variant_id=item["source_parent_variant_id"],
                target_variant_key=item["target_variant_key"],
                target_variant_id=item["target_variant_id"],
                target_state=item["target_state"],
                target_before_sha256=item["target_before_sha256"],
                target_projection_sha256=item["target_projection_sha256"],
            )
            for item in plan_items_raw
            if isinstance(item, Mapping)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CapturedPaperVariantBindingError(
            "RECEIPT_INVALID", "variant application plan item is invalid"
        ) from exc
    if len(plan_items) != len(plan_items_raw):
        _reject("RECEIPT_INVALID", "variant application plan item is invalid")
    plan = CapturedPaperVariantBindingPlan(
        authority=authority,
        items=plan_items,
        plan_sha256=str(plan_raw.get("plan_sha256") or ""),
    )
    if plan.to_dict() != dict(plan_raw) or _hash_json(plan.body()) != plan.plan_sha256:
        _reject("RECEIPT_INVALID", "variant application plan hash is invalid")
    application_items_raw = application_raw.get("items")
    if not isinstance(application_items_raw, list):
        _reject("RECEIPT_INVALID", "variant application items are invalid")
    try:
        application_items = tuple(
            CapturedPaperVariantBindingApplicationItem(
                family=item["family"],
                version=item["version"],
                source_variant_id=item["source_variant_id"],
                source_variant_sha256=item["source_variant_sha256"],
                target_variant_key=item["target_variant_key"],
                target_variant_id=item["target_variant_id"],
                target_before_sha256=item["target_before_sha256"],
                target_after_sha256=item["target_after_sha256"],
                action=item["action"],
            )
            for item in application_items_raw
            if isinstance(item, Mapping)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CapturedPaperVariantBindingError(
            "RECEIPT_INVALID", "variant application item is invalid"
        ) from exc
    if len(application_items) != len(application_items_raw):
        _reject("RECEIPT_INVALID", "variant application item is invalid")
    application = CapturedPaperVariantBindingApplication(
        plan=plan,
        items=application_items,
        application_sha256=str(application_raw.get("application_sha256") or ""),
    )
    if (
        application.to_dict() != dict(application_raw)
        or _hash_json(application.body()) != application.application_sha256
        or application.plan.plan_sha256 != application_raw.get("plan_sha256")
        or application.plan.authority.to_dict() != application_raw.get("authority")
    ):
        _reject("RECEIPT_INVALID", "variant application hash is invalid")
    return application


def record_captured_paper_variant_application_receipt(
    db: Session,
    *,
    application: CapturedPaperVariantBindingApplication,
    activation_manifest_sha256: str,
) -> CapturedPaperVariantApplicationReceipt:
    """Insert the exact application in the same transaction as its clones."""

    if not isinstance(db, Session) or type(
        application
    ) is not CapturedPaperVariantBindingApplication:
        _reject("RECEIPT_INVALID", "exact Session and application are required")
    if _hash_json(application.body()) != application.application_sha256:
        _reject("APPLICATION_TAMPERED", "application self-hash does not match")
    authority = application.plan.authority
    manifest_sha256 = _sha256(
        activation_manifest_sha256, "activation_manifest_sha256"
    )
    payload = _application_receipt_payload(application)
    canonical = _canonical_json_bytes(payload).decode("utf-8")
    authority_sha256 = _hash_json(authority.to_dict())
    at = _db_naive_utc(authority.bound_at)
    event_body = {
        "schema_version": APPLICATION_EVENT_SCHEMA_VERSION,
        "event_sequence": 1,
        "event_type": "applied",
        "application_sha256": application.application_sha256,
        "activation_manifest_sha256": manifest_sha256,
        "previous_event_sha256": None,
        "target_variant_ids": sorted(
            item.target_variant_id for item in application.items
        ),
        "recorded_at": _iso_utc(authority.bound_at),
        "paper_order_submission_authorized": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    event_sha256 = _hash_json(event_body)
    event = {**event_body, "event_sha256": event_sha256}
    try:
        row = db.execute(
            text(
                "INSERT INTO captured_paper_variant_application_receipts ("
                "account_scope, execution_family, expected_account_id, "
                "activation_generation, activation_manifest_sha256, "
                "authority_sha256, plan_sha256, application_sha256, "
                "application_canonical_json, created_at"
                ") VALUES ("
                ":account_scope, :execution_family, :expected_account_id, "
                ":activation_generation, :activation_manifest_sha256, "
                ":authority_sha256, :plan_sha256, :application_sha256, "
                ":application_json, :at"
                ") RETURNING id"
            ),
            {
                "account_scope": authority.account_scope,
                "execution_family": authority.execution_family,
                "expected_account_id": authority.expected_account_id,
                "activation_generation": authority.activation_generation,
                "activation_manifest_sha256": manifest_sha256,
                "authority_sha256": authority_sha256,
                "plan_sha256": application.plan.plan_sha256,
                "application_sha256": application.application_sha256,
                "application_json": canonical,
                "at": at,
            },
        ).mappings().one()
        db.execute(
            text(
                "INSERT INTO captured_paper_variant_application_events ("
                "application_id, event_sequence, event_type, "
                "previous_event_sha256, event_sha256, detail_canonical_json, "
                "recorded_at) VALUES ("
                ":application_id, 1, 'applied', NULL, :event_sha256, "
                ":detail_json, :recorded_at)"
            ),
            {
                "application_id": int(row["id"]),
                "event_sha256": event_sha256,
                "detail_json": _canonical_json_bytes(event).decode("utf-8"),
                "recorded_at": at,
            },
        )
    except Exception as exc:
        raise CapturedPaperVariantBindingError(
            "RECEIPT_INSERT_FAILED",
            "durable variant application receipt insert failed",
        ) from exc
    return CapturedPaperVariantApplicationReceipt(
        receipt_id=int(row["id"]),
        application=application,
        activation_manifest_sha256=manifest_sha256,
        status="applied",
        rollback=None,
        version=1,
        latest_event_sha256=event_sha256,
    )


def load_captured_paper_variant_application_receipt(
    db: Session,
    *,
    authority: CapturedPaperVariantBindingAuthority,
    lock: bool = False,
) -> CapturedPaperVariantApplicationReceipt | None:
    """Read and self-verify one generation-bound durable application receipt."""

    if not isinstance(db, Session) or type(
        authority
    ) is not CapturedPaperVariantBindingAuthority:
        _reject("RECEIPT_INVALID", "exact Session and authority are required")
    suffix = " FOR UPDATE" if lock else ""
    row = db.execute(
        text(
            "SELECT id, execution_family, activation_manifest_sha256, "
            "authority_sha256, plan_sha256, application_sha256, "
            "application_canonical_json "
            "FROM captured_paper_variant_application_receipts "
            "WHERE account_scope=:account_scope "
            "AND expected_account_id=:expected_account_id "
            "AND activation_generation=:activation_generation" + suffix
        ),
        {
            "account_scope": authority.account_scope,
            "expected_account_id": authority.expected_account_id,
            "activation_generation": authority.activation_generation,
        },
    ).mappings().one_or_none()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["application_canonical_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CapturedPaperVariantBindingError(
            "RECEIPT_INVALID", "variant application receipt JSON is invalid"
        ) from exc
    if not isinstance(payload, Mapping):
        _reject("RECEIPT_INVALID", "variant application receipt JSON is invalid")
    application = _application_from_receipt_payload(payload)
    events = db.execute(
        text(
            "SELECT event_sequence, event_type, previous_event_sha256, "
            "event_sha256, detail_canonical_json "
            "FROM captured_paper_variant_application_events "
            "WHERE application_id=:application_id ORDER BY event_sequence"
            + suffix
        ),
        {"application_id": int(row["id"])},
    ).mappings().all()
    if not events or len(events) > 2:
        _reject("RECEIPT_INVALID", "variant application event chain is invalid")
    parsed_events: list[Mapping[str, Any]] = []
    prior_sha: str | None = None
    for expected_sequence, event_row in enumerate(events, start=1):
        try:
            parsed_event = json.loads(str(event_row["detail_canonical_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CapturedPaperVariantBindingError(
                "RECEIPT_INVALID", "variant application event JSON is invalid"
            ) from exc
        if not isinstance(parsed_event, Mapping):
            _reject("RECEIPT_INVALID", "variant application event JSON is invalid")
        event_without_sha = {
            key: value for key, value in parsed_event.items() if key != "event_sha256"
        }
        event_type = "applied" if expected_sequence == 1 else str(
            event_row["event_type"]
        )
        if not (
            int(event_row["event_sequence"]) == expected_sequence
            and parsed_event.get("event_sequence") == expected_sequence
            and event_row["event_type"] == event_type
            and parsed_event.get("event_type") == event_type
            and event_row["previous_event_sha256"] == prior_sha
            and parsed_event.get("previous_event_sha256") == prior_sha
            and parsed_event.get("application_sha256")
            == application.application_sha256
            and parsed_event.get("activation_manifest_sha256")
            == row["activation_manifest_sha256"]
            and parsed_event.get("event_sha256") == event_row["event_sha256"]
            and _hash_json(event_without_sha) == event_row["event_sha256"]
            and parsed_event.get("paper_order_submission_authorized") is False
            and parsed_event.get("live_cash_authorized") is False
            and parsed_event.get("real_money_authorized") is False
        ):
            _reject("RECEIPT_INVALID", "variant application event chain is invalid")
        prior_sha = str(event_row["event_sha256"])
        parsed_events.append(dict(parsed_event))
    expected_target_ids = sorted(
        item.target_variant_id for item in application.items
    )
    applied_event = parsed_events[0]
    if not (
        set(applied_event)
        == {
            "schema_version",
            "event_sequence",
            "event_type",
            "application_sha256",
            "activation_manifest_sha256",
            "previous_event_sha256",
            "target_variant_ids",
            "recorded_at",
            "paper_order_submission_authorized",
            "live_cash_authorized",
            "real_money_authorized",
            "event_sha256",
        }
        and applied_event.get("schema_version")
        == APPLICATION_EVENT_SCHEMA_VERSION
        and applied_event.get("target_variant_ids") == expected_target_ids
        and _iso_utc(
            _parse_iso_utc(applied_event.get("recorded_at"), "recorded_at")
        )
        == applied_event.get("recorded_at")
    ):
        _reject("RECEIPT_INVALID", "applied event target census is invalid")
    status = "applied" if len(events) == 1 else str(events[-1]["event_type"])
    rollback = (
        parsed_events[-1].get("rollback")
        if status == "rolled_back"
        else None
    )
    if status in {"rolled_back", "recovered_stale"}:
        terminal_event = parsed_events[-1]
        detail_key = "rollback" if status == "rolled_back" else "recovery"
        terminal_detail = terminal_event.get(detail_key)
        terminal_items = (
            terminal_detail.get("items")
            if isinstance(terminal_detail, Mapping)
            else None
        )
        terminal_ids = sorted(
            item.get("target_variant_id")
            for item in terminal_items
            if isinstance(item, Mapping)
            and isinstance(item.get("target_variant_id"), int)
        ) if isinstance(terminal_items, list) else []
        expected_terminal_keys = {
            "schema_version",
            "event_sequence",
            "event_type",
            "application_sha256",
            "activation_manifest_sha256",
            "previous_event_sha256",
            detail_key,
            "recorded_at",
            "paper_order_submission_authorized",
            "live_cash_authorized",
            "real_money_authorized",
            "event_sha256",
        }
        detail_hash_name = (
            "rollback_sha256" if status == "rolled_back" else "recovery_sha256"
        )
        detail_without_hash = (
            {
                key: value
                for key, value in terminal_detail.items()
                if key != detail_hash_name
            }
            if isinstance(terminal_detail, Mapping)
            else {}
        )
        if not (
            set(terminal_event) == expected_terminal_keys
            and terminal_event.get("schema_version")
            == APPLICATION_EVENT_SCHEMA_VERSION
            and isinstance(terminal_detail, Mapping)
            and terminal_detail.get("application_sha256")
            == application.application_sha256
            and terminal_ids == expected_target_ids
            and len(terminal_ids) == len(terminal_items)
            and terminal_detail.get(detail_hash_name)
            == _hash_json(detail_without_hash)
            and _iso_utc(
                _parse_iso_utc(
                    terminal_event.get("recorded_at"), "recorded_at"
                )
            )
            == terminal_event.get("recorded_at")
        ):
            _reject(
                "RECEIPT_INVALID",
                "terminal event target census or receipt hash is invalid",
            )
    if not (
        _hash_json(authority.to_dict()) == row["authority_sha256"]
        and authority.to_dict() == application.plan.authority.to_dict()
        and row["execution_family"] == authority.execution_family
        and _SHA256_RE.fullmatch(str(row["activation_manifest_sha256"] or ""))
        and application.plan.plan_sha256 == row["plan_sha256"]
        and application.application_sha256 == row["application_sha256"]
        and status in {"applied", "rolled_back", "recovered_stale"}
        and (status == "applied" or isinstance(rollback, Mapping)
             or status == "recovered_stale")
    ):
        _reject("RECEIPT_INVALID", "variant application receipt binding is invalid")
    return CapturedPaperVariantApplicationReceipt(
        receipt_id=int(row["id"]),
        application=application,
        activation_manifest_sha256=str(row["activation_manifest_sha256"]),
        status=status,
        rollback=(dict(rollback) if isinstance(rollback, Mapping) else None),
        version=len(events),
        latest_event_sha256=str(events[-1]["event_sha256"]),
    )


def load_captured_paper_variant_application_receipt_by_generation(
    db: Session,
    *,
    expected_account_id: str,
    activation_generation: str,
    activation_manifest_sha256: str,
    lock: bool = False,
) -> CapturedPaperVariantApplicationReceipt | None:
    """Resolve an existing generation before manufacturing a new ``bound_at``."""

    account_id = _canonical_uuid(expected_account_id, "expected_account_id")
    generation = _canonical_uuid(
        activation_generation, "activation_generation"
    )
    manifest_sha256 = _sha256(
        activation_manifest_sha256, "activation_manifest_sha256"
    )
    suffix = " FOR UPDATE" if lock else ""
    row = db.execute(
        text(
            "SELECT application_canonical_json, activation_manifest_sha256 "
            "FROM captured_paper_variant_application_receipts "
            "WHERE account_scope=:account_scope "
            "AND execution_family=:execution_family "
            "AND expected_account_id=:expected_account_id "
            "AND activation_generation=:activation_generation" + suffix
        ),
        {
            "account_scope": ACCOUNT_SCOPE,
            "execution_family": EXECUTION_FAMILY,
            "expected_account_id": account_id,
            "activation_generation": generation,
        },
    ).mappings().one_or_none()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["application_canonical_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CapturedPaperVariantBindingError(
            "RECEIPT_INVALID", "variant application receipt JSON is invalid"
        ) from exc
    if not isinstance(payload, Mapping):
        _reject("RECEIPT_INVALID", "variant application receipt JSON is invalid")
    application = _application_from_receipt_payload(payload)
    if not (
        row["activation_manifest_sha256"] == manifest_sha256
        and application.plan.authority.expected_account_id == account_id
        and application.plan.authority.activation_generation == generation
        and application.plan.authority.account_scope == ACCOUNT_SCOPE
        and application.plan.authority.execution_family == EXECUTION_FAMILY
    ):
        _reject(
            "RECEIPT_AUTHORITY_DRIFT",
            "generation-bound application receipt differs from activation authority",
        )
    receipt = load_captured_paper_variant_application_receipt(
        db,
        authority=application.plan.authority,
        lock=lock,
    )
    if receipt is None or receipt.activation_manifest_sha256 != manifest_sha256:
        _reject("RECEIPT_INVALID", "generation-bound application disappeared")
    return receipt


def assert_committed_captured_paper_variant_application(
    db: Session,
    *,
    application: CapturedPaperVariantBindingApplication,
    activation_manifest_sha256: str,
) -> CapturedPaperVariantApplicationReceipt:
    """Prove the durable receipt and every exact active clone are committed."""

    receipt = load_captured_paper_variant_application_receipt(
        db, authority=application.plan.authority, lock=False
    )
    if (
        receipt is None
        or receipt.status != "applied"
        or receipt.application.to_dict() != application.to_dict()
        or receipt.activation_manifest_sha256
        != _sha256(activation_manifest_sha256, "activation_manifest_sha256")
    ):
        _reject("APPLICATION_NOT_COMMITTED", "durable application receipt is absent")
    expected_ids = {item.target_variant_id for item in application.items}
    generation_rows = db.execute(
        text(
            "SELECT id FROM momentum_strategy_variants "
            "WHERE variant_key LIKE :prefix "
            "AND refinement_meta_json -> :meta_key ->> 'activation_generation' "
            "= :activation_generation ORDER BY id"
        ),
        {
            "prefix": f"{VARIANT_KEY_PREFIX}%",
            "meta_key": BINDING_META_KEY,
            "activation_generation": application.plan.authority.activation_generation,
        },
    ).fetchall()
    if {int(row[0]) for row in generation_rows} != expected_ids:
        _reject(
            "APPLICATION_COMMIT_DRIFT",
            "current-generation PAPER clone census differs from its receipt",
        )
    for item in application.items:
        target = db.query(MomentumStrategyVariant).filter(
            MomentumStrategyVariant.id == item.target_variant_id
        ).one_or_none()
        marker = (
            dict(target.refinement_meta_json or {}).get(BINDING_META_KEY)
            if target is not None
            else None
        )
        if not (
            target is not None
            and bool(target.is_active)
            and captured_paper_initial_variant_sha256(target)
            == item.target_after_sha256
            and isinstance(marker, Mapping)
            and marker.get("activation_generation")
            == application.plan.authority.activation_generation
            and marker.get("plan_sha256") == application.plan.plan_sha256
            and marker.get("source_variant_sha256") == item.source_variant_sha256
        ):
            _reject(
                "APPLICATION_COMMIT_DRIFT",
                f"committed PAPER clone differs id={item.target_variant_id}",
            )
    return receipt


def assert_rolled_back_captured_paper_variant_application(
    db: Session,
    *,
    application: CapturedPaperVariantBindingApplication,
) -> CapturedPaperVariantApplicationReceipt:
    """Prove the terminal event and exact post-deactivation target census."""

    receipt = load_captured_paper_variant_application_receipt(
        db, authority=application.plan.authority, lock=False
    )
    if (
        receipt is None
        or receipt.status != "rolled_back"
        or receipt.application.to_dict() != application.to_dict()
        or not isinstance(receipt.rollback, Mapping)
    ):
        _reject("ROLLBACK_NOT_COMMITTED", "durable rollback event is absent")
    rollback_items = receipt.rollback.get("items")
    if not isinstance(rollback_items, list):
        _reject("ROLLBACK_NOT_COMMITTED", "durable rollback items are invalid")
    by_id = {
        row.get("target_variant_id"): row
        for row in rollback_items
        if isinstance(row, Mapping)
    }
    expected_ids = {item.target_variant_id for item in application.items}
    generation_rows = db.execute(
        text(
            "SELECT id FROM momentum_strategy_variants "
            "WHERE variant_key LIKE :prefix "
            "AND refinement_meta_json -> :meta_key ->> 'activation_generation' "
            "= :activation_generation ORDER BY id"
        ),
        {
            "prefix": f"{VARIANT_KEY_PREFIX}%",
            "meta_key": BINDING_META_KEY,
            "activation_generation": application.plan.authority.activation_generation,
        },
    ).fetchall()
    if {int(row[0]) for row in generation_rows} != expected_ids or set(by_id) != expected_ids:
        _reject(
            "ROLLBACK_COMMIT_DRIFT",
            "rolled-back PAPER clone census differs from its receipt",
        )
    for item in application.items:
        target = db.query(MomentumStrategyVariant).filter(
            MomentumStrategyVariant.id == item.target_variant_id
        ).one_or_none()
        rollback_item = by_id[item.target_variant_id]
        if not (
            target is not None
            and not bool(target.is_active)
            and rollback_item.get("target_variant_key") == item.target_variant_key
            and rollback_item.get("target_before_sha256")
            == item.target_after_sha256
            and rollback_item.get("target_after_sha256")
            == captured_paper_initial_variant_sha256(target)
            and rollback_item.get("deactivated") is True
        ):
            _reject(
                "ROLLBACK_COMMIT_DRIFT",
                f"rolled-back PAPER clone differs id={item.target_variant_id}",
            )
    return receipt


def prove_captured_paper_variant_application_not_applied(
    db: Session,
    *,
    authority: CapturedPaperVariantBindingAuthority,
    activation_manifest_sha256: str,
    checked_at: datetime,
) -> Mapping[str, Any]:
    """Return a hash-bound negative proof only when receipt and clones are absent."""

    if load_captured_paper_variant_application_receipt(
        db, authority=authority, lock=False
    ) is not None:
        _reject("APPLICATION_OUTCOME_AMBIGUOUS", "application receipt exists")
    generation_rows = db.execute(
        text(
            "SELECT id FROM momentum_strategy_variants "
            "WHERE variant_key LIKE :prefix "
            "AND refinement_meta_json -> :meta_key ->> 'activation_generation' "
            "= :activation_generation ORDER BY id"
        ),
        {
            "prefix": f"{VARIANT_KEY_PREFIX}%",
            "meta_key": BINDING_META_KEY,
            "activation_generation": authority.activation_generation,
        },
    ).fetchall()
    if generation_rows:
        _reject(
            "APPLICATION_OUTCOME_AMBIGUOUS",
            "generation-bound PAPER clones exist without a durable receipt",
        )
    body = {
        "schema_version": NOT_APPLIED_PROOF_SCHEMA_VERSION,
        "account_scope": authority.account_scope,
        "expected_account_id": authority.expected_account_id,
        "activation_generation": authority.activation_generation,
        "activation_manifest_sha256": _sha256(
            activation_manifest_sha256, "activation_manifest_sha256"
        ),
        "authority_sha256": _hash_json(authority.to_dict()),
        "checked_at": _iso_utc(_utc(checked_at, "checked_at")),
        "durable_application_receipt_present": False,
        "generation_bound_clone_count": 0,
        "paper_order_submission_authorized": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    return {**body, "not_applied_sha256": _hash_json(body)}


def _target_key(family: str) -> str:
    key = f"{VARIANT_KEY_PREFIX}{family}"
    if len(key) > 64:
        _reject(
            "TARGET_KEY_INVALID",
            f"deterministic PAPER key exceeds schema width for family={family}",
        )
    return key


def _source_projection(source: MomentumStrategyVariant) -> dict[str, Any]:
    return {
        "family": str(source.family),
        "version": int(source.version),
        "label": str(source.label),
        "params_json": copy.deepcopy(dict(source.params_json or {})),
        "scan_pattern_id": source.scan_pattern_id,
        "source_refinement_meta_json": copy.deepcopy(
            dict(source.refinement_meta_json or {})
        ),
    }


def _target_projection(
    source: MomentumStrategyVariant,
    authority: CapturedPaperVariantBindingAuthority,
) -> dict[str, Any]:
    return {
        **_source_projection(source),
        "variant_key": _target_key(str(source.family)),
        "is_active": True,
        "execution_family": authority.execution_family,
        "parent_variant_id": int(source.id),
        "account_scope": authority.account_scope,
        "expected_account_id": authority.expected_account_id,
        "policy_sha256": authority.policy_sha256,
        "settings_projection_sha256": authority.settings_projection_sha256,
        "code_build_sha256": authority.code_build_sha256,
    }


def _binding_meta(
    *,
    source: MomentumStrategyVariant,
    source_sha256: str,
    authority: CapturedPaperVariantBindingAuthority,
    plan_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": BINDING_META_SCHEMA_VERSION,
        "account_scope": authority.account_scope,
        "execution_family": authority.execution_family,
        "expected_account_id": authority.expected_account_id,
        "activation_generation": authority.activation_generation,
        "source_variant_id": int(source.id),
        "source_variant_sha256": source_sha256,
        "source_family": str(source.family),
        "source_version": int(source.version),
        "policy_sha256": authority.policy_sha256,
        "settings_projection_sha256": authority.settings_projection_sha256,
        "code_build_sha256": authority.code_build_sha256,
        "plan_sha256": plan_sha256,
        "bound_at": _iso_utc(authority.bound_at),
        "strategy_params_overridden": False,
        "paper_order_submission_authorized": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }


def _expected_refinement_meta(
    *,
    source: MomentumStrategyVariant,
    source_sha256: str,
    authority: CapturedPaperVariantBindingAuthority,
    plan_sha256: str,
) -> dict[str, Any]:
    source_meta = copy.deepcopy(dict(source.refinement_meta_json or {}))
    if BINDING_META_KEY in source_meta:
        _reject(
            "SOURCE_INVALID",
            f"source variant {source.id} already contains reserved binding metadata",
        )
    source_meta[BINDING_META_KEY] = _binding_meta(
        source=source,
        source_sha256=source_sha256,
        authority=authority,
        plan_sha256=plan_sha256,
    )
    return source_meta


def _validate_source(source: MomentumStrategyVariant) -> None:
    raw_family = str(source.family or "")
    raw_variant_key = str(source.variant_key or "")
    family = raw_family.strip()
    variant_key = raw_variant_key.strip()
    if (
        not family
        or raw_family != family
        or raw_variant_key != variant_key
        or variant_key != family
        or not bool(source.is_active)
        or variant_key.startswith("replay_v3")
        or family.startswith("replay_v3")
    ):
        _reject(
            "SOURCE_INVALID",
            f"source variant {getattr(source, 'id', None)} is not an active canonical strategy",
        )
    if int(source.version or 0) <= 0:
        _reject("SOURCE_INVALID", f"source variant {source.id} version is invalid")
    _target_key(family)
    if BINDING_META_KEY in dict(source.refinement_meta_json or {}):
        _reject(
            "SOURCE_INVALID",
            f"source variant {source.id} contains reserved PAPER metadata",
        )


def _same_authority_binding(
    target: MomentumStrategyVariant,
    *,
    source: MomentumStrategyVariant,
    source_sha256: str,
    authority: CapturedPaperVariantBindingAuthority,
) -> bool:
    marker = dict(target.refinement_meta_json or {}).get(BINDING_META_KEY)
    if not isinstance(marker, Mapping):
        return False
    expected = {
        "account_scope": authority.account_scope,
        "execution_family": authority.execution_family,
        "expected_account_id": authority.expected_account_id,
        "activation_generation": authority.activation_generation,
        "source_variant_id": int(source.id),
        "source_variant_sha256": source_sha256,
        "source_family": str(source.family),
        "source_version": int(source.version),
        "policy_sha256": authority.policy_sha256,
        "settings_projection_sha256": authority.settings_projection_sha256,
        "code_build_sha256": authority.code_build_sha256,
        "bound_at": _iso_utc(authority.bound_at),
        "strategy_params_overridden": False,
        "paper_order_submission_authorized": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    expected_keys = frozenset(expected) | frozenset(
        {"schema_version", "plan_sha256"}
    )
    return (
        frozenset(marker) == expected_keys
        and marker.get("schema_version") == BINDING_META_SCHEMA_VERSION
        and all(marker.get(key) == value for key, value in expected.items())
        and _SHA256_RE.fullmatch(str(marker.get("plan_sha256") or "")) is not None
    )


def _structurally_matches_source(
    target: MomentumStrategyVariant,
    *,
    source: MomentumStrategyVariant,
) -> bool:
    target_meta = copy.deepcopy(dict(target.refinement_meta_json or {}))
    target_meta.pop(BINDING_META_KEY, None)
    return bool(
        str(target.family or "") == str(source.family or "")
        and str(target.variant_key or "") == _target_key(str(source.family))
        and int(target.version or 0) == int(source.version or 0)
        and str(target.label or "") == str(source.label or "")
        and dict(target.params_json or {}) == dict(source.params_json or {})
        and str(target.execution_family or "") == EXECUTION_FAMILY
        and target.parent_variant_id == int(source.id)
        and target.scan_pattern_id == source.scan_pattern_id
        and target_meta == dict(source.refinement_meta_json or {})
    )


def _validated_source_ids(source_variant_ids: Sequence[int]) -> tuple[int, ...]:
    values: list[int] = []
    for raw in source_variant_ids:
        if isinstance(raw, bool):
            _reject("PLAN_INVALID", "source variant IDs must be positive integers")
        try:
            value = int(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CapturedPaperVariantBindingError(
                "PLAN_INVALID", "source variant IDs must be positive integers"
            ) from exc
        if value <= 0:
            _reject("PLAN_INVALID", "source variant IDs must be positive integers")
        values.append(value)
    if not values or len(values) != len(set(values)):
        _reject("PLAN_INVALID", "source variant IDs must be non-empty and unique")
    return tuple(sorted(values))


def resolve_intended_canonical_source_variant_ids(db: Session) -> tuple[int, ...]:
    """Resolve the complete intended taxonomy without a PAPER-only allowlist.

    The source set is deliberately derived from ``iter_momentum_families`` and
    the active canonical rows (``variant_key == family``).  A missing, extra,
    duplicated, replay, or reserved-metadata row fails closed;
    the activation caller cannot silently dark-disable one strategy family by
    maintaining a separate PAPER list.
    """

    if not isinstance(db, Session):
        _reject("SOURCE_INVALID", "a SQLAlchemy Session is required")
    expected = {str(family.family_id) for family in iter_momentum_families()}
    if not expected:
        _reject("SOURCE_UNAVAILABLE", "intended strategy taxonomy is empty")
    rows = (
        db.query(MomentumStrategyVariant)
        .filter(
            MomentumStrategyVariant.is_active.is_(True),
            MomentumStrategyVariant.variant_key
            == MomentumStrategyVariant.family,
        )
        .order_by(
            MomentumStrategyVariant.family.asc(),
            MomentumStrategyVariant.id.asc(),
        )
        .all()
    )
    by_family: dict[str, MomentumStrategyVariant] = {}
    for row in rows:
        _validate_source(row)
        family = str(row.family)
        if family in by_family:
            _reject(
                "SOURCE_AMBIGUOUS",
                f"multiple active canonical sources exist family={family}",
            )
        by_family[family] = row
    if set(by_family) != expected:
        missing = sorted(expected - set(by_family))
        extra = sorted(set(by_family) - expected)
        _reject(
            "SOURCE_TAXONOMY_MISMATCH",
            f"canonical strategy taxonomy drifted missing={missing} extra={extra}",
        )
    return tuple(sorted(int(row.id) for row in by_family.values()))


def recover_stale_captured_paper_variant_bindings(
    db: Session,
    *,
    authority: CapturedPaperVariantBindingAuthority,
    recovered_at: datetime,
) -> Mapping[str, Any]:
    """Deactivate exact prior-generation clones while the service fence is held.

    This routine does not infer that a foreign process is absent.  Its caller
    must already own the process-wide captured-PAPER PostgreSQL fence.  It
    touches only active ``captured_paper:`` rows carrying the exact reserved
    binding schema, and refuses malformed/current-generation drift.
    """

    if not isinstance(db, Session) or type(
        authority
    ) is not CapturedPaperVariantBindingAuthority:
        _reject("RECOVERY_INVALID", "exact Session and authority are required")
    at = _utc(recovered_at, "recovered_at")
    recovered: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    with db.begin_nested():
        # First inventory the bounded candidate set without taking row locks.
        # The caller owns the process-wide captured-PAPER fence.  We then lock
        # immutable application receipts in generation order *before* target
        # rows, matching the rollback lock order and avoiding receipt/target
        # deadlocks.
        inventoried = (
            db.query(MomentumStrategyVariant)
            .filter(
                MomentumStrategyVariant.is_active.is_(True),
                MomentumStrategyVariant.variant_key.like(
                    f"{VARIANT_KEY_PREFIX}%"
                ),
            )
            .order_by(MomentumStrategyVariant.id.asc())
            .all()
        )
        expected_keys = {
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
        markers_by_id: dict[int, Mapping[str, Any]] = {}
        rows_by_generation: dict[str, list[int]] = {}
        authorities_by_generation: dict[
            str, CapturedPaperVariantBindingAuthority
        ] = {}
        for row in inventoried:
            marker = dict(row.refinement_meta_json or {}).get(BINDING_META_KEY)
            if not isinstance(marker, Mapping):
                _reject(
                    "RECOVERY_TARGET_DRIFT",
                    f"active PAPER clone lacks binding metadata id={row.id}",
                )
            try:
                prior_authority = CapturedPaperVariantBindingAuthority(
                    expected_account_id=marker.get("expected_account_id"),
                    activation_generation=marker.get("activation_generation"),
                    policy_sha256=marker.get("policy_sha256"),
                    settings_projection_sha256=marker.get(
                        "settings_projection_sha256"
                    ),
                    code_build_sha256=marker.get("code_build_sha256"),
                    bound_at=_parse_iso_utc(marker.get("bound_at"), "bound_at"),
                    account_scope=marker.get("account_scope"),
                    execution_family=marker.get("execution_family"),
                )
            except CapturedPaperVariantBindingError as exc:
                raise CapturedPaperVariantBindingError(
                    "RECOVERY_TARGET_DRIFT",
                    f"active PAPER clone authority is malformed id={row.id}",
                ) from exc
            generation = prior_authority.activation_generation
            if not (
                set(marker) == expected_keys
                and marker.get("schema_version") == BINDING_META_SCHEMA_VERSION
                and str(row.variant_key or "")
                == _target_key(str(row.family or ""))
                and marker.get("source_family") == str(row.family or "")
                and marker.get("source_version") == int(row.version or 0)
                and marker.get("source_variant_id") == row.parent_variant_id
                and marker.get("strategy_params_overridden") is False
                and marker.get("paper_order_submission_authorized") is False
                and marker.get("live_cash_authorized") is False
                and marker.get("real_money_authorized") is False
                and _SHA256_RE.fullmatch(
                    str(marker.get("source_variant_sha256") or "")
                )
                and _SHA256_RE.fullmatch(str(marker.get("plan_sha256") or ""))
            ):
                _reject(
                    "RECOVERY_TARGET_DRIFT",
                    f"active PAPER clone binding is malformed id={row.id}",
                )
            existing_authority = authorities_by_generation.get(generation)
            if (
                existing_authority is not None
                and existing_authority.to_dict() != prior_authority.to_dict()
            ):
                _reject(
                    "RECOVERY_TARGET_DRIFT",
                    f"one generation has conflicting authority id={row.id}",
                )
            authorities_by_generation[generation] = prior_authority
            markers_by_id[int(row.id)] = dict(marker)
            rows_by_generation.setdefault(generation, []).append(int(row.id))

        receipts_by_generation: dict[
            str, CapturedPaperVariantApplicationReceipt
        ] = {}
        for generation in sorted(rows_by_generation):
            prior_authority = authorities_by_generation[generation]
            manifest_row = db.execute(
                text(
                    "SELECT activation_manifest_sha256 "
                    "FROM captured_paper_variant_application_receipts "
                    "WHERE account_scope=:account_scope "
                    "AND execution_family=:execution_family "
                    "AND expected_account_id=:expected_account_id "
                    "AND activation_generation=:activation_generation "
                    "FOR UPDATE"
                ),
                {
                    "account_scope": prior_authority.account_scope,
                    "execution_family": prior_authority.execution_family,
                    "expected_account_id": prior_authority.expected_account_id,
                    "activation_generation": generation,
                },
            ).mappings().one_or_none()
            if manifest_row is None:
                _reject(
                    "RECOVERY_RECEIPT_UNAVAILABLE",
                    "active PAPER clones cannot be adopted or removed without "
                    f"their durable application receipt generation={generation}",
                )
            durable = load_captured_paper_variant_application_receipt_by_generation(
                db,
                expected_account_id=prior_authority.expected_account_id,
                activation_generation=generation,
                activation_manifest_sha256=manifest_row[
                    "activation_manifest_sha256"
                ],
                lock=True,
            )
            if (
                durable is None
                or durable.status != "applied"
                or durable.application.plan.authority.to_dict()
                != prior_authority.to_dict()
            ):
                _reject(
                    "RECOVERY_RECEIPT_DRIFT",
                    f"active PAPER generation is not durably applied {generation}",
                )
            receipt_ids = {
                item.target_variant_id for item in durable.application.items
            }
            if receipt_ids != set(rows_by_generation[generation]):
                _reject(
                    "RECOVERY_RECEIPT_DRIFT",
                    f"active PAPER generation census differs {generation}",
                )
            receipts_by_generation[generation] = durable

        inventoried_ids = sorted(markers_by_id)
        rows = (
            db.query(MomentumStrategyVariant)
            .filter(MomentumStrategyVariant.id.in_(inventoried_ids))
            .order_by(MomentumStrategyVariant.id.asc())
            .with_for_update()
            .all()
            if inventoried_ids
            else []
        )
        current_active_ids = {
            int(row.id)
            for row in db.query(MomentumStrategyVariant.id)
            .filter(
                MomentumStrategyVariant.is_active.is_(True),
                MomentumStrategyVariant.variant_key.like(
                    f"{VARIANT_KEY_PREFIX}%"
                ),
            )
            .all()
        }
        if (
            [int(row.id) for row in rows] != inventoried_ids
            or current_active_ids != set(inventoried_ids)
        ):
            _reject(
                "RECOVERY_INVENTORY_DRIFT",
                "active PAPER clone inventory changed while receipts were locked",
            )

        application_items_by_id = {
            item.target_variant_id: item
            for receipt in receipts_by_generation.values()
            for item in receipt.application.items
        }
        db_time = _db_naive_utc(at)
        recovered_by_generation: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            marker = markers_by_id[int(row.id)]
            generation = str(marker["activation_generation"])
            item = application_items_by_id.get(int(row.id))
            before = captured_paper_initial_variant_sha256(row)
            if not (
                item is not None
                and before == item.target_after_sha256
                and str(row.variant_key or "") == item.target_variant_key
                and row.parent_variant_id == item.source_variant_id
                and marker.get("source_variant_sha256")
                == item.source_variant_sha256
                and marker.get("plan_sha256")
                == receipts_by_generation[generation].application.plan.plan_sha256
            ):
                _reject(
                    "RECOVERY_TARGET_DRIFT",
                    f"active PAPER clone differs from durable receipt id={row.id}",
                )
            if generation == authority.activation_generation:
                if authorities_by_generation[generation].to_dict() != authority.to_dict():
                    _reject(
                        "RECOVERY_CURRENT_GENERATION_DRIFT",
                        f"current-generation PAPER clone authority drifted id={row.id}",
                    )
                retained.append(
                    {
                        "target_variant_id": int(row.id),
                        "target_variant_sha256": before,
                        "activation_generation": generation,
                    }
                )
                continue
            row.is_active = False
            row.updated_at = db_time
            db.flush()
            recovered_item = {
                "target_variant_id": int(row.id),
                "target_variant_key": str(row.variant_key),
                "prior_activation_generation": generation,
                "target_before_sha256": before,
                "target_after_sha256": captured_paper_initial_variant_sha256(
                    row
                ),
                "deactivated": True,
            }
            recovered.append(recovered_item)
            recovered_by_generation.setdefault(generation, []).append(
                recovered_item
            )

        # Terminalize each exact stale application in the same transaction as
        # its target deactivation.  A crash can therefore expose only the old
        # APPLIED state with active bytes or the RECOVERED_STALE state with all
        # exact bytes inactive; never an unreceipted halfway state.
        for generation in sorted(recovered_by_generation):
            durable = receipts_by_generation[generation]
            recovery_body = {
                "schema_version": RECOVERY_SCHEMA_VERSION,
                "application_outcome": "recovered_stale",
                "application_sha256": (
                    durable.application.application_sha256
                ),
                "account_scope": durable.application.plan.authority.account_scope,
                "expected_account_id": (
                    durable.application.plan.authority.expected_account_id
                ),
                "activation_generation": generation,
                "recovered_by_authority": authority.to_dict(),
                "recovered_at": _iso_utc(at),
                "items": sorted(
                    recovered_by_generation[generation],
                    key=lambda item: item["target_variant_id"],
                ),
                "paper_order_submission_authorized": False,
                "live_cash_authorized": False,
                "real_money_authorized": False,
            }
            recovery = {
                **recovery_body,
                "recovery_sha256": _hash_json(recovery_body),
            }
            event_body = {
                "schema_version": APPLICATION_EVENT_SCHEMA_VERSION,
                "event_sequence": 2,
                "event_type": "recovered_stale",
                "application_sha256": durable.application.application_sha256,
                "activation_manifest_sha256": (
                    durable.activation_manifest_sha256
                ),
                "previous_event_sha256": durable.latest_event_sha256,
                "recovery": recovery,
                "recorded_at": _iso_utc(at),
                "paper_order_submission_authorized": False,
                "live_cash_authorized": False,
                "real_money_authorized": False,
            }
            event_sha256 = _hash_json(event_body)
            event = {**event_body, "event_sha256": event_sha256}
            inserted = db.execute(
                text(
                    "INSERT INTO captured_paper_variant_application_events ("
                    "application_id, event_sequence, event_type, "
                    "previous_event_sha256, event_sha256, "
                    "detail_canonical_json, recorded_at) VALUES ("
                    ":application_id, 2, 'recovered_stale', "
                    ":previous_event_sha256, :event_sha256, :detail_json, "
                    ":recorded_at)"
                ),
                {
                    "application_id": durable.receipt_id,
                    "previous_event_sha256": durable.latest_event_sha256,
                    "event_sha256": event_sha256,
                    "detail_json": _canonical_json_bytes(event).decode("utf-8"),
                    "recorded_at": db_time,
                },
            )
            if int(inserted.rowcount or 0) != 1:
                _reject(
                    "RECOVERY_RECEIPT_CAS_FAILED",
                    "durable stale-recovery transition lost ownership",
                )
    body = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "account_scope": authority.account_scope,
        "expected_account_id": authority.expected_account_id,
        "activation_generation": authority.activation_generation,
        "recovered_at": _iso_utc(at),
        "recovered": recovered,
        "retained_current_generation": retained,
        "paper_order_submission_authorized": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    return {**body, "recovery_sha256": _hash_json(body)}


def plan_captured_paper_variant_bindings(
    db: Session,
    *,
    authority: CapturedPaperVariantBindingAuthority,
    source_variant_ids: Sequence[int],
) -> CapturedPaperVariantBindingPlan:
    """Build a deterministic read-only plan for an explicit source set."""

    if not isinstance(db, Session):
        _reject("PLAN_INVALID", "a SQLAlchemy Session is required")
    if type(authority) is not CapturedPaperVariantBindingAuthority:
        _reject("PLAN_INVALID", "an exact binding authority is required")
    ids = _validated_source_ids(source_variant_ids)
    sources = (
        db.query(MomentumStrategyVariant)
        .filter(MomentumStrategyVariant.id.in_(ids))
        .order_by(MomentumStrategyVariant.id.asc())
        .all()
    )
    if len(sources) != len(ids):
        _reject("SOURCE_UNAVAILABLE", "one or more source variants are absent")
    by_id = {int(source.id): source for source in sources}
    if set(by_id) != set(ids):
        _reject("SOURCE_UNAVAILABLE", "source variant identity is ambiguous")

    seen_families: set[str] = set()
    pending: list[CapturedPaperVariantBindingPlanItem] = []
    for source in sorted(sources, key=lambda row: (str(row.family), int(row.id))):
        _validate_source(source)
        family = str(source.family)
        if family in seen_families:
            _reject(
                "SOURCE_AMBIGUOUS",
                f"multiple canonical sources were requested for family={family}",
            )
        seen_families.add(family)
        source_sha256 = captured_paper_initial_variant_sha256(source)
        key = _target_key(family)
        target = (
            db.query(MomentumStrategyVariant)
            .filter(
                MomentumStrategyVariant.family == family,
                MomentumStrategyVariant.variant_key == key,
                MomentumStrategyVariant.version == int(source.version),
            )
            .one_or_none()
        )
        active_sibling = (
            db.query(MomentumStrategyVariant.id)
            .filter(
                MomentumStrategyVariant.family == family,
                MomentumStrategyVariant.variant_key == key,
                MomentumStrategyVariant.version != int(source.version),
                MomentumStrategyVariant.is_active.is_(True),
            )
            .first()
        )
        if active_sibling is not None:
            _reject(
                "TARGET_ACTIVE_CONFLICT",
                f"another active PAPER clone version exists family={family}",
            )
        target_state = "absent"
        target_id: int | None = None
        target_before: str | None = None
        if target is not None:
            target_id = int(target.id)
            target_before = captured_paper_initial_variant_sha256(target)
            if not _structurally_matches_source(target, source=source):
                _reject(
                    "TARGET_DRIFT",
                    f"existing PAPER clone differs from source family={family}",
                )
            if bool(target.is_active):
                if not _same_authority_binding(
                    target,
                    source=source,
                    source_sha256=source_sha256,
                    authority=authority,
                ):
                    _reject(
                        "TARGET_ACTIVE_CONFLICT",
                        f"active PAPER clone belongs to another authority family={family}",
                    )
                target_state = "already_applied"
            else:
                target_state = "inactive_reusable"
        pending.append(
            CapturedPaperVariantBindingPlanItem(
                family=family,
                version=int(source.version),
                source_variant_id=int(source.id),
                source_variant_sha256=source_sha256,
                source_parent_variant_id=source.parent_variant_id,
                target_variant_key=key,
                target_variant_id=target_id,
                target_state=target_state,
                target_before_sha256=target_before,
                target_projection_sha256=_hash_json(
                    _target_projection(source, authority)
                ),
            )
        )
    items = tuple(pending)
    body = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "authority": authority.to_dict(),
        "items": [item.to_dict() for item in items],
    }
    return CapturedPaperVariantBindingPlan(
        authority=authority,
        items=items,
        plan_sha256=_hash_json(body),
    )


def _current_target(
    db: Session,
    *,
    item: CapturedPaperVariantBindingPlanItem,
    lock: bool,
) -> MomentumStrategyVariant | None:
    query = db.query(MomentumStrategyVariant).filter(
        MomentumStrategyVariant.family == item.family,
        MomentumStrategyVariant.variant_key == item.target_variant_key,
        MomentumStrategyVariant.version == item.version,
    )
    if lock:
        query = query.with_for_update()
    return query.one_or_none()


def apply_captured_paper_variant_bindings(
    db: Session,
    *,
    plan: CapturedPaperVariantBindingPlan,
) -> CapturedPaperVariantBindingApplication:
    """Apply ``plan`` atomically inside a savepoint; the caller still owns commit."""

    if not isinstance(db, Session) or type(plan) is not CapturedPaperVariantBindingPlan:
        _reject("APPLICATION_INVALID", "exact Session and plan types are required")
    if _hash_json(plan.body()) != plan.plan_sha256:
        _reject("PLAN_TAMPERED", "plan self-hash does not match")
    if (
        not plan.items
        or any(type(item) is not CapturedPaperVariantBindingPlanItem for item in plan.items)
        or len({item.source_variant_id for item in plan.items}) != len(plan.items)
        or len({item.target_variant_key for item in plan.items}) != len(plan.items)
    ):
        _reject("PLAN_INVALID", "plan items must be non-empty, exact, and unique")

    with db.begin_nested():
        for target_key in sorted(item.target_variant_key for item in plan.items):
            db.execute(
                _VARIANT_BINDING_LOCK_SQL,
                {
                    "namespace": _VARIANT_BINDING_LOCK_NAMESPACE,
                    "target_key": target_key,
                },
            )
        source_ids = [item.source_variant_id for item in plan.items]
        sources = (
            db.query(MomentumStrategyVariant)
            .filter(MomentumStrategyVariant.id.in_(source_ids))
            .order_by(MomentumStrategyVariant.id.asc())
            .with_for_update()
            .all()
        )
        source_by_id = {int(source.id): source for source in sources}
        if len(source_by_id) != len(plan.items):
            _reject("SOURCE_DRIFT", "a planned source variant disappeared")

        prepared: list[
            tuple[
                CapturedPaperVariantBindingPlanItem,
                MomentumStrategyVariant,
                MomentumStrategyVariant | None,
                str,
            ]
        ] = []
        for item in plan.items:
            source = source_by_id.get(item.source_variant_id)
            if source is None:
                _reject("SOURCE_DRIFT", "a planned source variant disappeared")
            _validate_source(source)
            current_source_sha = captured_paper_initial_variant_sha256(source)
            if not (
                str(source.family) == item.family
                and int(source.version) == item.version
                and source.parent_variant_id == item.source_parent_variant_id
                and current_source_sha == item.source_variant_sha256
                and _hash_json(_target_projection(source, plan.authority))
                == item.target_projection_sha256
            ):
                _reject(
                    "SOURCE_DRIFT",
                    f"planned source changed before apply family={item.family}",
                )
            target = _current_target(db, item=item, lock=True)
            active_sibling = (
                db.query(MomentumStrategyVariant.id)
                .filter(
                    MomentumStrategyVariant.family == item.family,
                    MomentumStrategyVariant.variant_key == item.target_variant_key,
                    MomentumStrategyVariant.version != item.version,
                    MomentumStrategyVariant.is_active.is_(True),
                )
                .with_for_update()
                .first()
            )
            if active_sibling is not None:
                _reject(
                    "TARGET_ACTIVE_CONFLICT",
                    "another active PAPER clone version appeared before apply "
                    f"family={item.family}",
                )
            current_target_sha = (
                None
                if target is None
                else captured_paper_initial_variant_sha256(target)
            )
            already_applied = bool(
                target is not None
                and target.is_active
                and _structurally_matches_source(target, source=source)
                and _same_authority_binding(
                    target,
                    source=source,
                    source_sha256=current_source_sha,
                    authority=plan.authority,
                )
            )
            if already_applied:
                prepared.append(
                    (item, source, target, "already_applied")
                )
                continue
            if item.target_state == "absent":
                if target is not None:
                    _reject(
                        "TARGET_DRIFT",
                        f"PAPER clone appeared after plan family={item.family}",
                    )
                action = "created"
            elif item.target_state == "inactive_reusable":
                if (
                    target is None
                    or current_target_sha != item.target_before_sha256
                    or bool(target.is_active)
                    or not _structurally_matches_source(target, source=source)
                ):
                    _reject(
                        "TARGET_DRIFT",
                        f"inactive PAPER clone changed before apply family={item.family}",
                    )
                action = "reactivated"
            elif item.target_state == "already_applied":
                _reject(
                    "TARGET_DRIFT",
                    f"already-applied PAPER clone changed family={item.family}",
                )
            else:
                _reject("PLAN_INVALID", "plan contains an unknown target state")
            prepared.append((item, source, target, action))

        applied: list[CapturedPaperVariantBindingApplicationItem] = []
        bound_at = _db_naive_utc(plan.authority.bound_at)
        for item, source, target, action in prepared:
            before_sha = (
                None
                if target is None
                else captured_paper_initial_variant_sha256(target)
            )
            if action != "already_applied":
                refinement_meta = _expected_refinement_meta(
                    source=source,
                    source_sha256=item.source_variant_sha256,
                    authority=plan.authority,
                    plan_sha256=plan.plan_sha256,
                )
                if target is None:
                    target = MomentumStrategyVariant(
                        family=item.family,
                        variant_key=item.target_variant_key,
                        version=item.version,
                        label=str(source.label),
                        params_json=copy.deepcopy(dict(source.params_json or {})),
                        is_active=True,
                        execution_family=EXECUTION_FAMILY,
                        parent_variant_id=int(source.id),
                        refinement_meta_json=refinement_meta,
                        scan_pattern_id=source.scan_pattern_id,
                        created_at=bound_at,
                        updated_at=bound_at,
                    )
                    db.add(target)
                else:
                    target.family = item.family
                    target.variant_key = item.target_variant_key
                    target.version = item.version
                    target.label = str(source.label)
                    target.params_json = copy.deepcopy(dict(source.params_json or {}))
                    target.is_active = True
                    target.execution_family = EXECUTION_FAMILY
                    target.parent_variant_id = int(source.id)
                    target.refinement_meta_json = refinement_meta
                    target.scan_pattern_id = source.scan_pattern_id
                    target.updated_at = bound_at
                db.flush()
            assert target is not None
            if not (
                bool(target.is_active)
                and _structurally_matches_source(target, source=source)
                and _same_authority_binding(
                    target,
                    source=source,
                    source_sha256=item.source_variant_sha256,
                    authority=plan.authority,
                )
                and dict(target.params_json or {}) == dict(source.params_json or {})
            ):
                _reject(
                    "APPLICATION_POSTCONDITION_FAILED",
                    f"PAPER clone postcondition failed family={item.family}",
                )
            applied.append(
                CapturedPaperVariantBindingApplicationItem(
                    family=item.family,
                    version=item.version,
                    source_variant_id=item.source_variant_id,
                    source_variant_sha256=item.source_variant_sha256,
                    target_variant_key=item.target_variant_key,
                    target_variant_id=int(target.id),
                    target_before_sha256=before_sha,
                    target_after_sha256=captured_paper_initial_variant_sha256(
                        target
                    ),
                    action=action,
                )
            )

        items = tuple(applied)
        body = {
            "schema_version": APPLICATION_SCHEMA_VERSION,
            "plan_sha256": plan.plan_sha256,
            "authority": plan.authority.to_dict(),
            "items": [item.to_dict() for item in items],
            "paper_order_submission_authorized": False,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        return CapturedPaperVariantBindingApplication(
            plan=plan,
            items=items,
            application_sha256=_hash_json(body),
        )


def rollback_captured_paper_variant_bindings(
    db: Session,
    *,
    application: CapturedPaperVariantBindingApplication,
    rolled_back_at: datetime,
) -> Mapping[str, Any]:
    """Deactivate only clones whose exact current bytes match ``application``."""

    if not isinstance(db, Session) or type(
        application
    ) is not CapturedPaperVariantBindingApplication:
        _reject("ROLLBACK_INVALID", "exact Session and application types are required")
    if _hash_json(application.body()) != application.application_sha256:
        _reject("APPLICATION_TAMPERED", "application self-hash does not match")
    rollback_time = _utc(rolled_back_at, "rolled_back_at")

    with db.begin_nested():
        durable = load_captured_paper_variant_application_receipt(
            db, authority=application.plan.authority, lock=True
        )
        if durable is not None and durable.status == "rolled_back":
            assert durable.rollback is not None
            return copy.deepcopy(dict(durable.rollback))
        if durable is not None and durable.application.to_dict() != application.to_dict():
            _reject(
                "ROLLBACK_APPLICATION_DRIFT",
                "durable application differs from the retained application",
            )

        locked: list[
            tuple[CapturedPaperVariantBindingApplicationItem, MomentumStrategyVariant]
        ] = []
        all_before = durable is None
        for item in sorted(application.items, key=lambda row: row.target_variant_id):
            target = (
                db.query(MomentumStrategyVariant)
                .filter(MomentumStrategyVariant.id == item.target_variant_id)
                .with_for_update()
                .one_or_none()
            )
            if durable is None:
                before = bool(
                    (item.action == "created" and target is None)
                    or (
                        item.action == "reactivated"
                        and target is not None
                        and not bool(target.is_active)
                        and item.target_before_sha256 is not None
                        and captured_paper_initial_variant_sha256(target)
                        == item.target_before_sha256
                    )
                )
                all_before = all_before and before
                continue
            if target is None:
                _reject(
                    "ROLLBACK_TARGET_DRIFT",
                    f"PAPER clone disappeared id={item.target_variant_id}",
                )
            current_sha = captured_paper_initial_variant_sha256(target)
            marker = dict(target.refinement_meta_json or {}).get(BINDING_META_KEY)
            if not (
                bool(target.is_active)
                and current_sha == item.target_after_sha256
                and str(target.execution_family or "") == EXECUTION_FAMILY
                and str(target.variant_key or "") == item.target_variant_key
                and target.parent_variant_id == item.source_variant_id
                and isinstance(marker, Mapping)
                and marker.get("activation_generation")
                == application.plan.authority.activation_generation
                and marker.get("source_variant_sha256")
                == item.source_variant_sha256
            ):
                _reject(
                    "ROLLBACK_TARGET_DRIFT",
                    f"PAPER clone changed after apply id={item.target_variant_id}",
                )
            locked.append((item, target))

        if durable is None:
            if not all_before:
                _reject(
                    "ROLLBACK_OUTCOME_AMBIGUOUS",
                    "receipt-absent application is neither atomically before nor applied",
                )
            body = {
                "schema_version": ROLLBACK_SCHEMA_VERSION,
                "application_outcome": "not_applied",
                "application_sha256": application.application_sha256,
                "account_scope": application.plan.authority.account_scope,
                "expected_account_id": application.plan.authority.expected_account_id,
                "activation_generation": (
                    application.plan.authority.activation_generation
                ),
                "rolled_back_at": _iso_utc(rollback_time),
                "items": [],
                "paper_order_submission_authorized": False,
                "live_cash_authorized": False,
                "real_money_authorized": False,
            }
            return {**body, "rollback_sha256": _hash_json(body)}

        rolled_back_items: list[dict[str, Any]] = []
        rollback_db_time = _db_naive_utc(rollback_time)
        for item, target in locked:
            target.is_active = False
            target.updated_at = rollback_db_time
            db.flush()
            rolled_back_items.append(
                {
                    "target_variant_id": int(target.id),
                    "target_variant_key": item.target_variant_key,
                    "target_before_sha256": item.target_after_sha256,
                    "target_after_sha256": captured_paper_initial_variant_sha256(
                        target
                    ),
                    "deactivated": True,
                }
            )

        body = {
            "schema_version": ROLLBACK_SCHEMA_VERSION,
            "application_outcome": "rolled_back",
            "application_sha256": application.application_sha256,
            "account_scope": application.plan.authority.account_scope,
            "expected_account_id": application.plan.authority.expected_account_id,
            "activation_generation": (
                application.plan.authority.activation_generation
            ),
            "rolled_back_at": _iso_utc(rollback_time),
            "items": rolled_back_items,
            "paper_order_submission_authorized": False,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        full = {**body, "rollback_sha256": _hash_json(body)}
        event_body = {
            "schema_version": APPLICATION_EVENT_SCHEMA_VERSION,
            "event_sequence": 2,
            "event_type": "rolled_back",
            "application_sha256": application.application_sha256,
            "activation_manifest_sha256": durable.activation_manifest_sha256,
            "previous_event_sha256": durable.latest_event_sha256,
            "rollback": full,
            "recorded_at": _iso_utc(rollback_time),
            "paper_order_submission_authorized": False,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        event_sha256 = _hash_json(event_body)
        event = {**event_body, "event_sha256": event_sha256}
        inserted = db.execute(
            text(
                "INSERT INTO captured_paper_variant_application_events ("
                "application_id, event_sequence, event_type, "
                "previous_event_sha256, event_sha256, detail_canonical_json, "
                "recorded_at) VALUES ("
                ":application_id, 2, 'rolled_back', :previous_event_sha256, "
                ":event_sha256, :detail_json, :recorded_at)"
            ),
            {
                "application_id": durable.receipt_id,
                "previous_event_sha256": durable.latest_event_sha256,
                "event_sha256": event_sha256,
                "detail_json": _canonical_json_bytes(event).decode("utf-8"),
                "recorded_at": rollback_db_time,
            },
        )
        if int(inserted.rowcount or 0) != 1:
            _reject(
                "ROLLBACK_RECEIPT_CAS_FAILED",
                "durable rollback receipt transition lost ownership",
            )
        return full


__all__ = [
    "ACCOUNT_SCOPE",
    "BINDING_META_KEY",
    "CapturedPaperVariantBindingApplication",
    "CapturedPaperVariantApplicationReceipt",
    "CapturedPaperVariantBindingAuthority",
    "CapturedPaperVariantBindingError",
    "CapturedPaperVariantBindingPlan",
    "EXECUTION_FAMILY",
    "RECOVERY_SCHEMA_VERSION",
    "VARIANT_KEY_PREFIX",
    "apply_captured_paper_variant_bindings",
    "assert_committed_captured_paper_variant_application",
    "assert_rolled_back_captured_paper_variant_application",
    "load_captured_paper_variant_application_receipt",
    "load_captured_paper_variant_application_receipt_by_generation",
    "plan_captured_paper_variant_bindings",
    "recover_stale_captured_paper_variant_bindings",
    "record_captured_paper_variant_application_receipt",
    "resolve_intended_canonical_source_variant_ids",
    "rollback_captured_paper_variant_bindings",
    "prove_captured_paper_variant_application_not_applied",
]
