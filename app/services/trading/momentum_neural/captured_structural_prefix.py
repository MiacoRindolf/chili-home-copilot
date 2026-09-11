"""Convert exact committed sequence reads into persistent structural evidence.

This consumes the coordinator's actual CapturedReadResult. It is not a source
authenticator, provider-continuity certificate, strategy rule or order authority.
The initial anchor must come from the caller's verified capture boundary. No
failed read advances that anchor, drops a tick, rewrites clocks or resets carry.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from .live_replay_capture import CapturedReadResult, build_executed_capture_read_inventory
from .replay_capture_contract import (
    CaptureContractError, CaptureIqfeedPrint, CaptureIqfeedSequenceReadQuery,
    CaptureRunIdentity, CaptureStream, IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION,
    IQFEED_L1_SOURCE_PROVENANCE_FIELD, resolve_capture_source_payload, sha256_json,
)
from .structural_tape_prefix import ConsumerFrontierReceipt, Limits, Prefix, Result, Tick, rows_sha256


EPOCH_FIELDS = (
    "bridge_run_id", "connection_generation", "bridge_version", "bridge_source_sha256",
    "bridge_configuration_sha256", "capture_resource_binding_sha256",
    "handoff_configuration_sha256", "timestamp_basis", "selected_update_fields_sha256",
    "selected_update_fields_ack_sha256",
)
UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ns(value):
    """Exact integer conversion at the source datetime's supplied precision."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("source_clock_missing_or_naive")
    delta = value.astimezone(timezone.utc)-UTC_EPOCH
    return (delta.days*86400+delta.seconds)*10**9+delta.microseconds*1000


@dataclass(frozen=True)
class SourceWitness:
    capture_sequence: int
    event_sha256: str
    payload_sha256: str
    provenance_sha256: str
    provider_identity: tuple


class CapturedStructuralPrefix:
    """Single-owner adapter; domain failures preserve source and reducer state.

    An explicit segment anchor does not claim observed history before it. Root
    hashes bind supplied evidence; provider registration/continuity and existing
    decision attestations remain separate. Late/mixed/repeated identities need
    explicit reconstruction or a new declared segment, never automatic repair.
    Catastrophic allocation/process failures need durable recovery; this class
    does not implement checkpoint persistence or claim a production RSS bound.
    """
    def __init__(self, *, identity: CaptureRunIdentity, symbol: str, segment: str,
                 limits: Limits, anchor_sequence: int, anchor_root_sha256: str):
        if type(identity) is not CaptureRunIdentity or type(symbol) is not str or not symbol.strip():
            raise ValueError("invalid_capture_identity")
        if type(anchor_sequence) is not int or anchor_sequence <= 0:
            raise ValueError("invalid_capture_anchor")
        if (type(anchor_root_sha256) is not str or len(anchor_root_sha256) != 64
                or any(c not in "0123456789abcdef" for c in anchor_root_sha256)):
            raise ValueError("invalid_capture_anchor")
        self.identity, self.symbol = identity, symbol.strip().upper()
        self.prefix = Prefix("iqfeed:"+self.symbol, segment, limits)
        self.source_sequence, self.source_root_sha256 = anchor_sequence, anchor_root_sha256
        self.last_read_event_sha256 = None
        self._provider_keys = set()
        self._witnesses = []

    def witness(self, index):
        self.prefix.tick(index)
        return self._witnesses[index]

    def apply_read(self, captured: CapturedReadResult, *, decision_id: str) -> Result:
        fail = lambda reason: Result("unresolved", self.prefix.prefix_sha256, reason=reason)
        if type(captured) is not CapturedReadResult or type(captured.source_events) is not tuple:
            return fail("invalid_captured_read")
        if len(captured.source_events) > self.prefix.limits.frontier_ticks:
            return fail("resource_capacity_unresolved")
        try:
            # Reuse the existing exact receipt-event/source-content checks and
            # decision/run boundary binding instead of trusting a durable flag.
            build_executed_capture_read_inventory(identity=self.identity, decision_id=decision_id,
                                                  captured_reads=(captured,))
            receipt = captured.receipt
            query = CaptureIqfeedSequenceReadQuery.from_dict(receipt.query)
            event = captured.receipt_submission.event
            if (query.identity_sha256 != self.identity.identity_sha256 or query.symbol != self.symbol
                    or receipt.symbol != self.symbol or receipt.provider != "iqfeed"
                    or receipt.stream is not CaptureStream.IQFEED_PRINT
                    or event.sequence != query.through_sequence+1
                    or query.available_at_most != receipt.returned_at):
                return fail("capture_query_boundary_mismatch")
        except (CaptureContractError, TypeError, ValueError, AttributeError):
            return fail("captured_read_evidence_invalid")
        # Idempotence follows content validation, including the actual rows.
        if event.event_sha256 == self.last_read_event_sha256:
            return Result("already_applied", self.prefix.prefix_sha256)
        if query.after_sequence != self.source_sequence:
            return fail("capture_source_cursor_mismatch")
        if query.through_sequence == self.source_sequence:
            if captured.source_events or query.source_prefix_root_sha256 != self.source_root_sha256:
                return fail("capture_source_anchor_mismatch")
            self.last_read_event_sha256 = event.event_sha256
            return Result("already_applied", self.prefix.prefix_sha256, reason="source_prefix_unchanged")
        if self.prefix.count+len(captured.source_events) > self.prefix.limits.retained_ticks:
            return fail("resource_capacity_unresolved")
        rows, witnesses, keys = [], [], set()
        previous = self.prefix.tick(self.prefix.count-1) if self.prefix.count else None
        try:
            for source in captured.source_events:
                if not self.source_sequence < source.sequence <= query.through_sequence:
                    return fail("capture_row_outside_sequence_query")
                value = CaptureIqfeedPrint.from_event(source)
                provenance = resolve_capture_source_payload(source).payload.get(IQFEED_L1_SOURCE_PROVENANCE_FIELD)
                if not provenance or provenance.get("schema_version") != IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION:
                    return fail("exact_print_provenance_required")
                epoch = (self.identity.identity_sha256,)+(tuple(provenance[name] for name in EPOCH_FIELDS))
                if previous:
                    if previous.epoch != epoch:
                        return fail("epoch_change_requires_new_segment")
                    epoch = previous.epoch  # One immutable epoch object per segment.
                row = Tick(source.sequence, value.price, value.size, value.bid, value.ask,
                    ns(source.clocks.provider_event_at), ns(source.clocks.received_at), ns(source.clocks.available_at),
                    epoch, provenance["source_frame_sequence"], provenance["source_frame_sha256"])
                if row.published_ns > ns(query.source_available_at):
                    return fail("source_publication_outside_capture_boundary")
                if row.known_ns > ns(receipt.returned_at):
                    return fail("source_clock_from_future")
                if previous and row.frame_sequence <= previous.frame_sequence:
                    return fail("source_frame_order_requires_reconstruction")
                # Match the bridge's trade key; TickID alone is not the key.
                provider_key = (provenance["bridge_run_id"], provenance["connection_generation"])+tuple(
                    str(provenance[name]).strip() for name in
                    ("provider_trade_date", "provider_trade_time", "provider_tick_id", "trade_market_center"))
                if provider_key in self._provider_keys or provider_key in keys:
                    return fail("repeated_provider_identity_requires_reconstruction")
                keys.add(provider_key)
                rows.append(row)
                witnesses.append(SourceWitness(source.sequence, source.event_sha256, source.payload_sha256,
                                               sha256_json(provenance), provider_key))
                previous = row
            boundary = ConsumerFrontierReceipt(ns(receipt.returned_at), len(rows), rows_sha256(rows),
                self.prefix.prefix_sha256, self.identity.identity_sha256, query.through_sequence,
                query.source_prefix_root_sha256, self.source_sequence, self.source_root_sha256)
        except (CaptureContractError, TypeError, ValueError, KeyError):
            return fail("source_provenance_or_clock_invalid")
        result = self.prefix.append_frontier(rows, boundary)
        if result.status == "applied":
            self._provider_keys.update(keys)
            self._witnesses.extend(witnesses)
            self.source_sequence, self.source_root_sha256 = query.through_sequence, query.source_prefix_root_sha256
            self.last_read_event_sha256 = event.event_sha256
        return result
