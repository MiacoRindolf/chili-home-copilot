"""Shared, deterministic first-dip tape policy for sealed replay and paper.

This module has no provider, database, broker, or wall-clock reads.  Callers
must supply the exact receipt-bound print window and decision clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .replay_capture_contract import (
    CaptureEvent,
    CaptureIqfeedPrint,
    CaptureReadReceipt,
    captured_read_result_sha256,
    sha256_json,
)


FIRST_DIP_TAPE_POLICY_SCHEMA_VERSION = "chili.first-dip-tape-policy.v1"
FIRST_DIP_TAPE_EVALUATION_SCHEMA_VERSION = (
    "chili.first-dip-tape-evaluation.v1"
)
FIRST_DIP_TAPE_READ_QUERY_SCHEMA_VERSION = (
    "chili.first-dip-tape-read-query.v1"
)


class FirstDipTapePolicyError(ValueError):
    """The candidate tape policy or its receipt-bound inputs are malformed."""


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FirstDipTapePolicyError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite_positive(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise FirstDipTapePolicyError(f"{name} must be a positive finite number")
    try:
        resolved = float(value)
    except (TypeError, ValueError) as exc:
        raise FirstDipTapePolicyError(
            f"{name} must be a positive finite number"
        ) from exc
    if not math.isfinite(resolved) or resolved <= 0:
        raise FirstDipTapePolicyError(f"{name} must be a positive finite number")
    return resolved


def _parse_utc(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, name)
    if not isinstance(value, str) or not value.strip():
        raise FirstDipTapePolicyError(f"{name} must be a timezone-aware timestamp")
    try:
        return _utc(
            datetime.fromisoformat(value.strip().replace("Z", "+00:00")),
            name,
        )
    except ValueError as exc:
        raise FirstDipTapePolicyError(
            f"{name} must be a timezone-aware timestamp"
        ) from exc


def _sha256(value: Any, name: str) -> str:
    resolved = str(value or "").strip().lower()
    if len(resolved) != 64 or any(char not in "0123456789abcdef" for char in resolved):
        raise FirstDipTapePolicyError(f"{name} must be a lowercase SHA256")
    return resolved


@dataclass(frozen=True)
class FirstDipTapeReadQuery:
    """Exact event-time window and durable prefix a tape receipt must cover.

    The bounds are ``(event_start_exclusive, event_end_inclusive]``.  A receipt
    is authoritative only when the capture runtime, not the strategy caller,
    enumerates every matching durable print through ``source_frontier_sequence``.
    """

    symbol: str
    provider: str
    event_start_exclusive: datetime
    event_end_inclusive: datetime
    decision_at: datetime
    available_at_most: datetime
    source_frontier_sequence: int
    policy_sha256: str
    schema_version: str = FIRST_DIP_TAPE_READ_QUERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FIRST_DIP_TAPE_READ_QUERY_SCHEMA_VERSION:
            raise FirstDipTapePolicyError(
                "first-dip tape read query schema is unsupported"
            )
        symbol = str(self.symbol or "").strip().upper()
        provider = str(self.provider or "").strip().lower()
        if not symbol or provider != "iqfeed":
            raise FirstDipTapePolicyError(
                "first-dip tape read query identity is invalid"
            )
        start = _utc(self.event_start_exclusive, "event_start_exclusive")
        end = _utc(self.event_end_inclusive, "event_end_inclusive")
        decision = _utc(self.decision_at, "decision_at")
        available = _utc(self.available_at_most, "available_at_most")
        if not start < end or end != decision or available != decision:
            raise FirstDipTapePolicyError(
                "first-dip tape read query clocks are inconsistent"
            )
        if (
            type(self.source_frontier_sequence) is not int
            or self.source_frontier_sequence <= 0
        ):
            raise FirstDipTapePolicyError(
                "first-dip tape source frontier must be positive"
            )
        source_frontier_sequence = self.source_frontier_sequence
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "event_start_exclusive", start)
        object.__setattr__(self, "event_end_inclusive", end)
        object.__setattr__(self, "decision_at", decision)
        object.__setattr__(self, "available_at_most", available)
        object.__setattr__(
            self, "source_frontier_sequence", source_frontier_sequence
        )
        object.__setattr__(
            self, "policy_sha256", _sha256(self.policy_sha256, "policy_sha256")
        )

    def validate_for_policy(self, policy: "FirstDipTapePolicy") -> None:
        if not isinstance(policy, FirstDipTapePolicy):
            raise FirstDipTapePolicyError("first-dip tape policy is not typed")
        if self.policy_sha256 != policy.policy_sha256 or not math.isclose(
            (self.event_end_inclusive - self.event_start_exclusive).total_seconds(),
            policy.window_seconds,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise FirstDipTapePolicyError(
                "first-dip tape query does not match its policy"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "provider": self.provider,
            "event_start_exclusive": self.event_start_exclusive.isoformat().replace(
                "+00:00", "Z"
            ),
            "event_end_inclusive": self.event_end_inclusive.isoformat().replace(
                "+00:00", "Z"
            ),
            "decision_at": self.decision_at.isoformat().replace("+00:00", "Z"),
            "available_at_most": self.available_at_most.isoformat().replace(
                "+00:00", "Z"
            ),
            "source_frontier_sequence": self.source_frontier_sequence,
            "policy_sha256": self.policy_sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FirstDipTapeReadQuery":
        expected = {
            "schema_version",
            "symbol",
            "provider",
            "event_start_exclusive",
            "event_end_inclusive",
            "decision_at",
            "available_at_most",
            "source_frontier_sequence",
            "policy_sha256",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise FirstDipTapePolicyError(
                "first-dip tape read query fields do not match schema"
            )
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            symbol=str(raw.get("symbol") or ""),
            provider=str(raw.get("provider") or ""),
            event_start_exclusive=_parse_utc(
                raw.get("event_start_exclusive"), "event_start_exclusive"
            ),
            event_end_inclusive=_parse_utc(
                raw.get("event_end_inclusive"), "event_end_inclusive"
            ),
            decision_at=_parse_utc(raw.get("decision_at"), "decision_at"),
            available_at_most=_parse_utc(
                raw.get("available_at_most"), "available_at_most"
            ),
            source_frontier_sequence=raw.get("source_frontier_sequence"),
            policy_sha256=str(raw.get("policy_sha256") or ""),
        )


@dataclass(frozen=True)
class FirstDipTapePolicy:
    """All P&L-affecting and freshness inputs to the tape confirmer."""

    window_seconds: float
    max_source_age_seconds: float
    tick_rate_floor_pctile: float
    minimum_prints: int = 3
    schema_version: str = FIRST_DIP_TAPE_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FIRST_DIP_TAPE_POLICY_SCHEMA_VERSION:
            raise FirstDipTapePolicyError("first-dip tape policy schema is unsupported")
        object.__setattr__(
            self,
            "window_seconds",
            _finite_positive(self.window_seconds, "window_seconds"),
        )
        object.__setattr__(
            self,
            "max_source_age_seconds",
            _finite_positive(
                self.max_source_age_seconds,
                "max_source_age_seconds",
            ),
        )
        if isinstance(self.tick_rate_floor_pctile, bool):
            raise FirstDipTapePolicyError(
                "tick_rate_floor_pctile must be within [0, 1]"
            )
        try:
            percentile = float(self.tick_rate_floor_pctile)
        except (TypeError, ValueError) as exc:
            raise FirstDipTapePolicyError(
                "tick_rate_floor_pctile must be within [0, 1]"
            ) from exc
        if not math.isfinite(percentile) or not 0.0 <= percentile <= 1.0:
            raise FirstDipTapePolicyError(
                "tick_rate_floor_pctile must be within [0, 1]"
            )
        object.__setattr__(self, "tick_rate_floor_pctile", percentile)
        if (
            isinstance(self.minimum_prints, bool)
            or int(self.minimum_prints) < 3
        ):
            raise FirstDipTapePolicyError("minimum_prints must be at least three")
        object.__setattr__(self, "minimum_prints", int(self.minimum_prints))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "window_seconds": self.window_seconds,
            "max_source_age_seconds": self.max_source_age_seconds,
            "tick_rate_floor_pctile": self.tick_rate_floor_pctile,
            "minimum_prints": self.minimum_prints,
        }

    @classmethod
    def from_settings(cls, settings_obj: Any) -> "FirstDipTapePolicy":
        """Resolve every candidate knob explicitly for decision provenance."""

        return cls(
            window_seconds=getattr(
                settings_obj,
                "chili_momentum_l2_confirm_window_s",
                None,
            ),
            max_source_age_seconds=getattr(
                settings_obj,
                "chili_momentum_first_dip_tape_max_source_age_seconds",
                None,
            ),
            tick_rate_floor_pctile=getattr(
                settings_obj,
                "chili_momentum_l2_confirm_tick_rate_floor_pctile",
                None,
            ),
            minimum_prints=getattr(
                settings_obj,
                "chili_momentum_first_dip_tape_minimum_prints",
                None,
            ),
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FirstDipTapePolicy":
        expected = {
            "schema_version",
            "window_seconds",
            "max_source_age_seconds",
            "tick_rate_floor_pctile",
            "minimum_prints",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise FirstDipTapePolicyError(
                "first-dip tape policy fields do not match schema"
            )
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            window_seconds=raw.get("window_seconds"),
            max_source_age_seconds=raw.get("max_source_age_seconds"),
            tick_rate_floor_pctile=raw.get("tick_rate_floor_pctile"),
            minimum_prints=raw.get("minimum_prints"),
        )

    @property
    def policy_sha256(self) -> str:
        return sha256_json(self.to_dict())


@dataclass(frozen=True)
class FirstDipTapeWindow:
    """Exact ordered raw facts selected by one content-addressed read receipt."""

    read_id: str
    symbol: str
    requested_at: datetime
    returned_at: datetime
    result_sha256: str
    source_event_sha256s: tuple[str, ...]
    provider_event_ats: tuple[datetime, ...]
    rows: tuple[
        tuple[float, float, float | None, float | None, float], ...
    ]

    def __post_init__(self) -> None:
        read_id = str(self.read_id or "").strip()
        symbol = str(self.symbol or "").strip().upper()
        result_sha256 = str(self.result_sha256 or "").strip().lower()
        if not read_id or not symbol:
            raise FirstDipTapePolicyError("first-dip tape window identity is missing")
        if len(result_sha256) != 64 or any(
            value not in "0123456789abcdef" for value in result_sha256
        ):
            raise FirstDipTapePolicyError(
                "first-dip tape result digest is malformed"
            )
        requested = _utc(self.requested_at, "requested_at")
        returned = _utc(self.returned_at, "returned_at")
        if returned < requested:
            raise FirstDipTapePolicyError("first-dip tape receipt clock regressed")
        source_hashes = tuple(
            str(value or "").strip().lower()
            for value in self.source_event_sha256s
        )
        if (
            len(source_hashes) != len(set(source_hashes))
            or any(
                len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
                for value in source_hashes
            )
        ):
            raise FirstDipTapePolicyError(
                "first-dip tape source digest inventory is malformed"
            )
        event_ats = tuple(
            _utc(value, "provider_event_at") for value in self.provider_event_ats
        )
        rows = tuple(tuple(row) for row in self.rows)
        if not (
            len(source_hashes) == len(event_ats) == len(rows)
            and event_ats == tuple(sorted(event_ats))
        ):
            raise FirstDipTapePolicyError(
                "first-dip tape source inventory is unordered or inconsistent"
            )
        if not source_hashes and result_sha256 != captured_read_result_sha256(()):
            raise FirstDipTapePolicyError(
                "empty first-dip tape result digest is incorrect"
            )
        for row, event_at in zip(rows, event_ats):
            if len(row) != 5:
                raise FirstDipTapePolicyError("first-dip tape row is malformed")
            try:
                row_event_seconds = float(row[4])
            except (TypeError, ValueError) as exc:
                raise FirstDipTapePolicyError(
                    "first-dip tape row event clock is malformed"
                ) from exc
            if (
                not math.isfinite(row_event_seconds)
                or not math.isclose(
                    row_event_seconds,
                    event_at.timestamp(),
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            ):
                raise FirstDipTapePolicyError(
                    "first-dip tape row/event clock binding mismatch"
                )
        object.__setattr__(self, "read_id", read_id)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "requested_at", requested)
        object.__setattr__(self, "returned_at", returned)
        object.__setattr__(self, "result_sha256", result_sha256)
        object.__setattr__(self, "source_event_sha256s", source_hashes)
        object.__setattr__(self, "provider_event_ats", event_ats)
        object.__setattr__(self, "rows", rows)


@dataclass(frozen=True)
class FirstDipTapeEvaluation:
    """Canonical positive, negative, unavailable, or invalid tape verdict."""

    symbol: str
    decision_at: datetime
    read_id: str
    result_sha256: str
    source_event_sha256s: tuple[str, ...]
    policy_sha256: str
    status: str
    reason: str
    confirmed: bool
    features: Mapping[str, float | int] | None
    newest_source_age_seconds: float | None
    schema_version: str = FIRST_DIP_TAPE_EVALUATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FIRST_DIP_TAPE_EVALUATION_SCHEMA_VERSION:
            raise FirstDipTapePolicyError(
                "first-dip tape evaluation schema is unsupported"
            )
        if self.status not in {
            "valid_positive",
            "valid_negative",
            "coverage_unavailable",
            "invalid",
        }:
            raise FirstDipTapePolicyError("first-dip tape evaluation status is invalid")
        if type(self.confirmed) is not bool:
            raise FirstDipTapePolicyError("first-dip tape verdict must be boolean")
        if self.confirmed != (self.status == "valid_positive"):
            raise FirstDipTapePolicyError(
                "first-dip tape verdict/status are inconsistent"
            )
        features = self.features
        if features is not None:
            if not isinstance(features, Mapping):
                raise FirstDipTapePolicyError(
                    "first-dip tape evaluation features must be a mapping"
                )
            # ``frozen=True`` is otherwise only shallow: retaining a caller-owned
            # dict would let an already-issued receipt's content hash change after
            # verification.  Copy once and expose a read-only view.  Values in the
            # typed feature contract are scalars; reject mutable containers here
            # while leaving exact numeric/semantic validation to the authority.
            frozen_features: dict[str, Any] = {}
            for key, value in features.items():
                if type(value) not in {str, int, float, bool, type(None)}:
                    raise FirstDipTapePolicyError(
                        "first-dip tape evaluation feature values must be immutable"
                    )
                frozen_features[key] = value
            object.__setattr__(
                self,
                "features",
                MappingProxyType(frozen_features),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "decision_at": self.decision_at.isoformat().replace("+00:00", "Z"),
            "read_id": self.read_id,
            "result_sha256": self.result_sha256,
            "source_event_sha256s": list(self.source_event_sha256s),
            "policy_sha256": self.policy_sha256,
            "status": self.status,
            "reason": self.reason,
            "confirmed": self.confirmed,
            "features": None if self.features is None else dict(self.features),
            "newest_source_age_seconds": self.newest_source_age_seconds,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FirstDipTapeEvaluation":
        expected = {
            "schema_version",
            "symbol",
            "decision_at",
            "read_id",
            "result_sha256",
            "source_event_sha256s",
            "policy_sha256",
            "status",
            "reason",
            "confirmed",
            "features",
            "newest_source_age_seconds",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise FirstDipTapePolicyError(
                "first-dip tape evaluation fields are invalid"
            )
        source_hashes = raw["source_event_sha256s"]
        if not isinstance(source_hashes, (list, tuple)):
            raise FirstDipTapePolicyError(
                "first-dip tape evaluation source inventory is invalid"
            )
        try:
            decision_at = datetime.fromisoformat(
                str(raw["decision_at"]).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise FirstDipTapePolicyError(
                "first-dip tape evaluation decision clock is invalid"
            ) from exc
        return cls(
            schema_version=str(raw["schema_version"]),
            symbol=str(raw["symbol"]),
            decision_at=_utc(decision_at, "evaluation.decision_at"),
            read_id=str(raw["read_id"]),
            result_sha256=str(raw["result_sha256"]),
            source_event_sha256s=tuple(str(value) for value in source_hashes),
            policy_sha256=str(raw["policy_sha256"]),
            status=str(raw["status"]),
            reason=str(raw["reason"]),
            confirmed=raw["confirmed"],
            features=raw["features"],
            newest_source_age_seconds=raw["newest_source_age_seconds"],
        )

    @property
    def evaluation_sha256(self) -> str:
        return sha256_json(self.to_dict())


def first_dip_tape_window_from_capture(
    receipt: CaptureReadReceipt,
    source_events: Sequence[CaptureEvent],
) -> FirstDipTapeWindow:
    """Resolve the typed raw payloads named by one already-verified receipt."""

    events = tuple(source_events)
    if (
        not isinstance(receipt, CaptureReadReceipt)
        or tuple(event.event_sha256 for event in events)
        != receipt.source_event_sha256s
    ):
        raise FirstDipTapePolicyError(
            "first-dip tape receipt/source inventory mismatch"
        )
    prints = tuple(CaptureIqfeedPrint.from_event(event) for event in events)
    return FirstDipTapeWindow(
        read_id=receipt.read_id,
        symbol=str(receipt.symbol or ""),
        requested_at=receipt.requested_at,
        returned_at=receipt.returned_at,
        result_sha256=receipt.result_sha256,
        source_event_sha256s=receipt.source_event_sha256s,
        provider_event_ats=tuple(
            event.clocks.provider_event_at
            for event in events
            if event.clocks.provider_event_at is not None
        ),
        rows=tuple(print_event.tape_row() for print_event in prints),
    )


def evaluate_first_dip_tape(
    window: FirstDipTapeWindow,
    *,
    policy: FirstDipTapePolicy,
    decision_at: datetime,
    symbol: str,
) -> FirstDipTapeEvaluation:
    """Evaluate one exact window with no hidden settings, I/O, or clock reads."""

    if not isinstance(window, FirstDipTapeWindow) or not isinstance(
        policy, FirstDipTapePolicy
    ):
        raise FirstDipTapePolicyError("first-dip tape inputs are not typed")
    decision = _utc(decision_at, "decision_at")
    normalized_symbol = str(symbol or "").strip().upper()

    def _result(
        status: str,
        reason: str,
        *,
        features: dict[str, float | int] | None = None,
        newest_age: float | None = None,
    ) -> FirstDipTapeEvaluation:
        return FirstDipTapeEvaluation(
            symbol=normalized_symbol,
            decision_at=decision,
            read_id=window.read_id,
            result_sha256=window.result_sha256,
            source_event_sha256s=window.source_event_sha256s,
            policy_sha256=policy.policy_sha256,
            status=status,
            reason=reason,
            confirmed=status == "valid_positive",
            features=features,
            newest_source_age_seconds=newest_age,
        )

    if not normalized_symbol or window.symbol != normalized_symbol:
        return _result("invalid", "first_dip_tape_symbol_mismatch")
    if window.returned_at > decision:
        return _result("invalid", "first_dip_tape_receipt_from_future")
    if not window.provider_event_ats:
        return _result("valid_negative", "first_dip_tape_no_prints")
    oldest = window.provider_event_ats[0]
    newest = window.provider_event_ats[-1]
    if newest > decision:
        return _result("invalid", "first_dip_tape_source_from_future")
    window_start = decision - timedelta(seconds=policy.window_seconds)
    if oldest < window_start:
        return _result("invalid", "first_dip_tape_source_outside_window")
    newest_age = (decision - newest).total_seconds()
    if newest_age > policy.max_source_age_seconds:
        return _result(
            "coverage_unavailable",
            "first_dip_tape_source_stale",
            newest_age=newest_age,
        )
    if len(window.rows) < policy.minimum_prints:
        return _result(
            "valid_negative",
            "first_dip_tape_insufficient_prints",
            newest_age=newest_age,
        )

    # Import lazily so the shared policy stays free of market/provider modules.
    from .entry_gates import _signed_tape_features

    features = _signed_tape_features(
        window.rows,
        window_s=policy.window_seconds,
        tick_rate_floor_pctile=policy.tick_rate_floor_pctile,
    )
    if features is None:
        return _result(
            "valid_negative",
            "first_dip_tape_features_unavailable",
            newest_age=newest_age,
        )
    normalized_features: dict[str, float | int] = {
        "signed_tape_accel": float(features["signed_tape_accel"]),
        "tick_rate": float(features["tick_rate"]),
        "tick_rate_floor": float(features["tick_rate_floor"]),
        "n_ticks": int(features["n_ticks"]),
    }
    confirmed = (
        normalized_features["signed_tape_accel"] > 0.0
        and normalized_features["tick_rate"]
        >= normalized_features["tick_rate_floor"]
    )
    return _result(
        "valid_positive" if confirmed else "valid_negative",
        "first_dip_tape_confirmed"
        if confirmed
        else "first_dip_tape_not_confirmed",
        features=normalized_features,
        newest_age=newest_age,
    )
