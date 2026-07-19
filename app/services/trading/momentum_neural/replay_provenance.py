"""Causal timestamp/provenance checks for recorded IQFeed replay inputs.

Replay must advance on the instant at which CHILI could have observed a row,
not on a provider reference timestamp that may precede host receipt.  The
current IQFeed bridge writes a complete provenance tuple for that purpose.
Legacy rows remain useful for exploratory diagnostics, but they cannot certify
Ross-parity or PnL claims.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import Any, Mapping
import uuid

from app.config import settings


IQFEED_SOURCE = "iqfeed_l1"
IQFEED_MESSAGE_TYPE = "Q"
IQFEED_NBBO_TIMESTAMP_BASIS = "iqfeed_q_receive_trade_reference_fenced"
IQFEED_TRADE_TIMESTAMP_BASIS = "iqfeed_trade_reference_date_inferred"
IQFEED_AVAILABILITY_QUARANTINE_MIGRATION_ID = (
    "349_iqfeed_availability_incident_quarantine"
)
IQFEED_MAX_REFERENCE_AGE_SECONDS = 2.0
IQFEED_FUTURE_TOLERANCE_SECONDS = 1.0
IQFEED_BRIDGE_BUILD_RE = re.compile(
    r"^iqfeed-l1-exact-print-provenance-v3\+sha256:[0-9a-f]{16}$"
)


@dataclass(frozen=True)
class CausalTapeProvenance:
    """Certification result for one persisted quote or trade row."""

    certified: bool
    availability_ts: datetime | None
    received_ts: datetime | None
    market_reference_ts: datetime | None
    receive_reference_delta_seconds: float | None
    reasons: tuple[str, ...]


def _aware_utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _stored_observed_utc(value: Any) -> datetime | None:
    """Parse the legacy TIMESTAMP-without-time-zone UTC storage column."""

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def certify_iqfeed_tape_row(
    row: Mapping[str, Any],
    *,
    expected_timestamp_basis: str,
    expected_bridge_build: str | None = None,
) -> CausalTapeProvenance:
    """Fail closed unless ``row`` carries the bridge's complete causal tuple.

    ``provider_trade_reference_at`` is a containment reference, not a quote
    event clock. ``received_at`` records socket receipt, while ``available_at``
    is the bridge's conservative post-persist/post-notify release marker and is
    therefore the replay clock. ``observed_at`` must still match the fenced
    provider reference so buffered data cannot be made to look fresh by host
    receipt or publication time alone.
    """

    reasons: list[str] = []
    source = str(row.get("source") or "").strip().lower()
    message_type = str(row.get("message_type") or "").strip().upper()
    timestamp_basis = str(row.get("timestamp_basis") or "").strip()
    bridge_version = str(row.get("bridge_version") or "").strip()
    bridge_run_raw = row.get("bridge_run_id")
    bridge_run_id = bridge_run_raw if isinstance(bridge_run_raw, str) else ""
    bridge_run_id = bridge_run_id.strip()
    generation_raw = row.get("connection_generation")
    generation = (
        generation_raw
        if isinstance(generation_raw, int) and not isinstance(generation_raw, bool)
        else 0
    )

    received_at = _aware_utc_datetime(row.get("received_at"))
    available_at = _aware_utc_datetime(row.get("available_at"))
    reference_at = _aware_utc_datetime(row.get("provider_trade_reference_at"))
    observed_at = _stored_observed_utc(row.get("observed_at"))
    provider_event_raw = row.get("provider_event_at")
    availability_quarantined = row.get("availability_quarantined")
    availability_quarantine_checked = row.get("availability_quarantine_checked")
    pinned_build = str(
        expected_bridge_build
        if expected_bridge_build is not None
        else getattr(settings, "chili_iqfeed_l1_authoritative_bridge_build", "")
        or ""
    ).strip()

    if source != IQFEED_SOURCE:
        reasons.append("source_not_iqfeed_l1")
    if message_type != IQFEED_MESSAGE_TYPE:
        reasons.append("message_type_not_q")
    if timestamp_basis != expected_timestamp_basis:
        reasons.append("timestamp_basis_unverified")
    if IQFEED_BRIDGE_BUILD_RE.fullmatch(pinned_build) is None:
        reasons.append("expected_bridge_build_unpinned")
    elif bridge_version != pinned_build:
        reasons.append("bridge_version_not_pinned")
    if not bridge_run_id:
        reasons.append("bridge_run_id_missing")
    else:
        try:
            if str(uuid.UUID(bridge_run_id)) != bridge_run_id:
                reasons.append("bridge_run_id_invalid")
        except ValueError:
            reasons.append("bridge_run_id_invalid")
    if generation <= 0:
        reasons.append("connection_generation_invalid")
    if received_at is None:
        reasons.append("received_at_missing_or_naive")
    if available_at is None:
        reasons.append("available_at_missing_or_naive")
    if availability_quarantine_checked is not True:
        reasons.append("availability_quarantine_not_checked")
    elif availability_quarantined is not False:
        reasons.append("availability_quarantined")
    if reference_at is None:
        reasons.append("provider_trade_reference_missing_or_naive")
    # The bridge deliberately leaves this NULL: IQFeed's Most-Recent-Trade-Time
    # reference is not a quote-event timestamp.
    if provider_event_raw is not None:
        reasons.append("provider_event_at_mislabeled")

    if available_at is not None and received_at is not None and available_at < received_at:
        reasons.append("available_before_receive")

    delta: float | None = None
    if received_at is not None and reference_at is not None:
        delta = (received_at - reference_at).total_seconds()
        if not math.isfinite(delta):
            reasons.append("receive_reference_delta_nonfinite")
        elif not (
            -IQFEED_FUTURE_TOLERANCE_SECONDS
            <= delta
            <= IQFEED_MAX_REFERENCE_AGE_SECONDS
        ):
            reasons.append("receive_reference_delta_out_of_bounds")

    if observed_at is None:
        reasons.append("observed_at_missing")
    elif reference_at is not None:
        observed_delta = abs((observed_at - reference_at).total_seconds())
        if not math.isfinite(observed_delta) or observed_delta > 0.001:
            reasons.append("observed_at_reference_mismatch")

    return CausalTapeProvenance(
        certified=not reasons,
        availability_ts=available_at,
        received_ts=received_at,
        market_reference_ts=reference_at,
        receive_reference_delta_seconds=delta,
        reasons=tuple(reasons),
    )
