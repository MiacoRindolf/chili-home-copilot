"""Replay v3 P0b — MockBrokerAdapter (standalone, provably-inert scaffolding).

A drop-in ``VenueAdapter`` (``app/services/trading/venue/protocol.py:133-183``) that the
Replay v3 FSM driver will pass to ``tick_live_session(..., adapter_factory=)`` so the REAL
live runner can be stepped over historical data with ZERO real broker / network I/O.

P0 scope (this module) is the SKELETON + interface conformance + a SIMPLE deterministic
fill model:

  * BBO comes from an *injected* recorded NBBO (one ``RecordedQuote`` per product), NOT a
    network read. ``get_best_bid_ask`` returns ``(NormalizedTicker, FreshnessMeta)`` stamped
    at the injected sim clock so the runner's stale-quote checks compare sim-to-sim.
  * Orders fill DETERMINISTICALLY at the recorded NBBO using the *pure paper-fill math*
    (``paper_execution.long_entry_fill_price`` / ``long_exit_fill_price`` /
    ``modeled_fill_leg_fee_usd``) — REUSE, not a re-derivation. A long entry
    (buy) crosses the ask + adverse slippage; an exit (sell) crosses the bid −
    slippage; each fill books only its leg of the bound round-trip fee model.
  * No BBO at ``t`` ⇒ ``get_best_bid_ask`` returns ``(None, …)`` and any place is REJECTED
    (``ok=False, error="no_bbo"``) — the RVMDW/warrant-class path the live runner branches on.
  * NO partials, NO ack-timeouts, NO fault injection — those are P1 (this is the skeleton).

It is intentionally NOT wired into the live runner here (that is P1). It is standalone:
importable, instantiable, and unit-testable in isolation. It places NO real orders and makes
NO network calls — every output is derived purely from the injected recorded NBBO/price.

See docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §2.1 / §4 (P0).
"""

from __future__ import annotations

import itertools
import copy
import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from ..venue.protocol import (
    FreshnessMeta,
    NormalizedFill,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
)
from .paper_execution import (
    long_entry_fill_price,
    long_exit_fill_price,
    modeled_fill_leg_fee_usd,
)

_log = logging.getLogger(__name__)

_VENUE = "replay_mock"
REPLAY_MOCK_ACCOUNT_IDENTITY = "replay-mock-account-v1"
SEALED_REPLAY_SYNC_ACK_ARCHITECTURAL_BLOCKER = (
    "sealed_replay_synchronous_ack_unavailable_at_place"
)
SEALED_REPLAY_CANCEL_RECEIPT_ARCHITECTURAL_BLOCKER = (
    "sealed_replay_cancel_request_response_not_captured"
)
EXACT_PRINT_MARKET_FILL_UNAVAILABLE = (
    "exact_print_market_order_fill_authority_unavailable"
)
EXACT_PRINT_FILL_EVIDENCE_GRADE = "DIAGNOSTIC_ONLY"
EXACT_PRINT_COUNTERFACTUAL_AUTHORITY_BLOCKERS = (
    "sealed_counterfactual_driver_receipt_unavailable",
    "exact_print_release_tail_completeness_receipt_unavailable",
    "l2_queue_position_and_market_impact_unavailable",
    "full_trade_fee_equity_path_receipt_unavailable",
)
_VERIFIED_EXACT_PRINT_TOKEN = object()
_VERIFIED_EXACT_PRINT_INVENTORY_TOKEN = object()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

# ── STEP-2 REALISTIC FILL MODEL — documented base constants ──────────────────────────
#
# These are the ONE-documented-setting bases (the reference FLOORS, per
# [[feedback_adaptive_no_magic]]): the driver may override any of them with an
# adaptive/recorded value (e.g. the per-day printed-volume series, the per-venue
# measured ack-latency percentiles). They exist so a mock constructed with NO driver
# feed still fills conservatively and never fills through an empty tape.
#
# (b) FILL-VOLUME REALISM — cumulative fill ≤ this fraction of the recorded printed
#     volume at-or-through the limit during the order's live window. 0.25 = we assume the
#     replayed order can capture at most 25 % of the shares that actually printed at or
#     through its price while it rested. A conservative participation cap; partial fills
#     result when the printed volume is thin relative to the order size.
DEFAULT_VOLUME_PARTICIPATION_FRAC = 0.25
#
# (c) ACK/LATENCY — the observed distribution of real place→fill latencies measured from
#     ``trading_automation_events`` (live_entry_submitted → live_entry_filled), 2026-07-02
#     live DB: n=218 sessions, plausible (0-60 s) window median ≈ 10.1 s, p25 ≈ 6.3 s,
#     p75 ≈ 27.9 s (full-sample median 12.7 s; a long tail to broker-timeout is excluded
#     from the ack model). The documented FALLBACK BASE ack latency when the driver
#     supplies no measured distribution is this median. The driver's
#     ``set_latency_distribution`` overrides it with the recomputed per-run percentiles.
DEFAULT_ACK_LATENCY_SECONDS = 10.0
DEFAULT_ACK_LATENCY_P25_SECONDS = 6.3
DEFAULT_ACK_LATENCY_P75_SECONDS = 27.9


class FillMode:
    """The explicit conservative/optimistic fill-realism mode (STEP-2 (e)).

    * ``CONSERVATIVE`` (DEFAULT): fills cross the ADVERSE side (buy at ask+slip, sell at
      bid−slip), are VOLUME-CAPPED against the recorded printed volume (partial fills when
      the tape is thin), and pay the full ack latency. This is the floor the operator
      trusts for a lower-bound PnL estimate — it never over-credits a fill the tape could
      not have supplied.
    * ``OPTIMISTIC``: fills cross at the FAVORABLE mid (no adverse slippage), are NOT
      volume-capped (assume full size available), and pay a shorter ack latency. This is
      the upper-bound of the PnL band. It is still bounded by the recorded quote (no
      fill-through-empty-tape), so even optimistic mode is honest about a quoteless name.
    """

    CONSERVATIVE = "conservative"
    OPTIMISTIC = "optimistic"

    @staticmethod
    def normalize(value: Any) -> str:
        v = str(value).strip().lower() if value is not None else FillMode.CONSERVATIVE
        return FillMode.OPTIMISTIC if v == FillMode.OPTIMISTIC else FillMode.CONSERVATIVE


def _float_or_none(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RecordedQuote:
    """A single recorded NBBO snapshot the mock fills against (as-of the sim clock).

    Mirrors the recorded ``momentum_nbbo_spread_tape`` shape (bid/ask, optional last). The
    driver reconstructs one of these per product as-of the simulated instant and injects it
    via ``set_quote`` before the tick."""

    bid: float
    ask: float
    last: Optional[float] = None

    @property
    def mid(self) -> float:
        return (float(self.bid) + float(self.ask)) / 2.0

    def is_valid(self) -> bool:
        try:
            b, a = float(self.bid), float(self.ask)
        except (TypeError, ValueError):
            return False
        return (
            math.isfinite(b)
            and math.isfinite(a)
            and b > 0
            and a > 0
            and a >= b
        )


@dataclass(frozen=True)
class VerifiedExactPrint:
    """One non-serializable exact print released by a verified capture loader.

    The object identity token prevents a dictionary or JSON row from being
    mistaken for sealed input.  ``replay_v3`` mints these only while releasing a
    previously verified ``CaptureIqfeedPrint`` through its dual-clock frontier.
    This is input authority, not by itself counterfactual fill/OOS authority.
    """

    event_sha256: str
    sequence: int
    release_ordinal: int
    capture_identity_sha256: str
    final_capture_seal_sha256: str
    release_order_root_sha256: str
    product_id: str
    provider_event_at: datetime
    received_at: datetime
    available_at: datetime
    price: float
    size: float
    bid: Optional[float]
    ask: Optional[float]
    conditions: tuple[str, ...] = ()
    _verification_token: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _VERIFIED_EXACT_PRINT_TOKEN:
            raise ValueError("exact print lacks verified sealed-capture provenance")
        digest = str(self.event_sha256 or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("exact print event_sha256 is invalid")
        object.__setattr__(self, "event_sha256", digest)
        if isinstance(self.sequence, bool) or int(self.sequence) <= 0:
            raise ValueError("exact print sequence is invalid")
        object.__setattr__(self, "sequence", int(self.sequence))
        if isinstance(self.release_ordinal, bool) or int(self.release_ordinal) <= 0:
            raise ValueError("exact print release_ordinal is invalid")
        object.__setattr__(self, "release_ordinal", int(self.release_ordinal))
        for name in (
            "capture_identity_sha256",
            "final_capture_seal_sha256",
            "release_order_root_sha256",
        ):
            value = str(getattr(self, name) or "").strip().lower()
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError(f"exact print {name} is invalid")
            object.__setattr__(self, name, value)
        product_id = str(self.product_id or "").strip().upper()
        if not product_id:
            raise ValueError("exact print product_id is invalid")
        object.__setattr__(self, "product_id", product_id)
        for name in ("provider_event_at", "received_at", "available_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"exact print {name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if self.available_at < self.received_at:
            raise ValueError("exact print available_at precedes received_at")
        for name in ("price", "size"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"exact print {name} is invalid")
            object.__setattr__(self, name, value)
        if (self.bid is None) != (self.ask is None):
            raise ValueError("exact print must carry both bid and ask or neither")
        if self.bid is not None and self.ask is not None:
            bid = float(self.bid)
            ask = float(self.ask)
            if (
                not math.isfinite(bid)
                or not math.isfinite(ask)
                or bid <= 0.0
                or ask < bid
            ):
                raise ValueError("exact print bid/ask is invalid")
            object.__setattr__(self, "bid", bid)
            object.__setattr__(self, "ask", ask)
        conditions = tuple(str(value).strip() for value in self.conditions)
        if any(not value for value in conditions):
            raise ValueError("exact print conditions are malformed")
        object.__setattr__(self, "conditions", conditions)


def _mint_verified_exact_print(
    *,
    event_sha256: str,
    sequence: int,
    release_ordinal: int,
    capture_identity_sha256: str,
    final_capture_seal_sha256: str,
    release_order_root_sha256: str,
    product_id: str,
    provider_event_at: datetime,
    received_at: datetime,
    available_at: datetime,
    price: float,
    size: float,
    bid: Optional[float],
    ask: Optional[float],
    conditions: Sequence[str],
) -> VerifiedExactPrint:
    """Private bridge used by the already-verified ReplayV3 capture adapter."""

    return VerifiedExactPrint(
        event_sha256=event_sha256,
        sequence=sequence,
        release_ordinal=release_ordinal,
        capture_identity_sha256=capture_identity_sha256,
        final_capture_seal_sha256=final_capture_seal_sha256,
        release_order_root_sha256=release_order_root_sha256,
        product_id=product_id,
        provider_event_at=provider_event_at,
        received_at=received_at,
        available_at=available_at,
        price=price,
        size=size,
        bid=bid,
        ask=ask,
        conditions=tuple(conditions),
        _verification_token=_VERIFIED_EXACT_PRINT_TOKEN,
    )


@dataclass(frozen=True)
class VerifiedExactPrintInventory:
    """Full sealed exact-print inventory expected by one allocation run."""

    capture_identity_sha256: str
    final_capture_seal_sha256: str
    release_order_root_sha256: str
    event_sha256s: tuple[str, ...]
    inventory_root_sha256: str
    _verification_token: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _VERIFIED_EXACT_PRINT_INVENTORY_TOKEN:
            raise ValueError("exact print inventory lacks verified capture provenance")
        for name in (
            "capture_identity_sha256",
            "final_capture_seal_sha256",
            "release_order_root_sha256",
            "inventory_root_sha256",
        ):
            value = str(getattr(self, name) or "").strip().lower()
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError(f"exact print inventory {name} is invalid")
            object.__setattr__(self, name, value)
        event_sha256s = tuple(str(value or "").strip().lower() for value in self.event_sha256s)
        if len(event_sha256s) != len(set(event_sha256s)) or any(
            len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)
            for value in event_sha256s
        ):
            raise ValueError("exact print inventory event hashes are invalid")
        object.__setattr__(self, "event_sha256s", event_sha256s)
        expected_root = _canonical_sha256(
            {
                "schema_version": "chili.replay-exact-print-inventory.v1",
                "capture_identity_sha256": self.capture_identity_sha256,
                "final_capture_seal_sha256": self.final_capture_seal_sha256,
                "release_order_root_sha256": self.release_order_root_sha256,
                "event_sha256s": list(event_sha256s),
            }
        )
        if self.inventory_root_sha256 != expected_root:
            raise ValueError("exact print inventory root does not bind its events")


def _mint_verified_exact_print_inventory(
    *,
    capture_identity_sha256: str,
    final_capture_seal_sha256: str,
    release_order_root_sha256: str,
    event_sha256s: Sequence[str],
) -> VerifiedExactPrintInventory:
    payload = {
        "schema_version": "chili.replay-exact-print-inventory.v1",
        "capture_identity_sha256": str(capture_identity_sha256).strip().lower(),
        "final_capture_seal_sha256": str(final_capture_seal_sha256).strip().lower(),
        "release_order_root_sha256": str(release_order_root_sha256).strip().lower(),
        "event_sha256s": [str(value).strip().lower() for value in event_sha256s],
    }
    return VerifiedExactPrintInventory(
        capture_identity_sha256=payload["capture_identity_sha256"],
        final_capture_seal_sha256=payload["final_capture_seal_sha256"],
        release_order_root_sha256=payload["release_order_root_sha256"],
        event_sha256s=tuple(payload["event_sha256s"]),
        inventory_root_sha256=_canonical_sha256(payload),
        _verification_token=_VERIFIED_EXACT_PRINT_INVENTORY_TOKEN,
    )


@dataclass(frozen=True)
class ExactPrintFillAllocation:
    """One FIFO allocation of a single print's bounded participation budget."""

    event_sha256: str
    sequence: int
    release_ordinal: int
    allocation_index: int
    order_id: str
    client_order_id: Optional[str]
    product_id: str
    side: str
    quantity: float
    price: float
    provider_event_at: datetime
    received_at: datetime
    available_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_sha256": self.event_sha256,
            "sequence": self.sequence,
            "release_ordinal": self.release_ordinal,
            "allocation_index": self.allocation_index,
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "product_id": self.product_id,
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "provider_event_at": self.provider_event_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "available_at": self.available_at.isoformat(),
        }


@dataclass
class _RestingOrder:
    """An order the runner can poll via ``get_order``.

    Every LIMIT obeys exchange price semantics. The compatibility
    ``resting_limit_fills`` flag controls the richer resting-order model:

      * **basic** (P0, default): MARKET orders and marketable LIMIT orders fill immediately;
        an unmarketable LIMIT remains open until recorded NBBO crosses its price.
      * **realistic resting** (P1): the same price invariant plus optional acknowledgement
        delay, partial fills, and printed-volume participation caps. An optional
        ``ack_delay_ticks`` holds the order ``open`` for N quote advances before it is
        eligible to cross (exercises the runner's pending-entry ack-poll/timeout path)."""

    order_id: str
    client_order_id: Optional[str]
    product_id: str
    side: str
    order_type: str
    base_size: float
    limit_price: Optional[float]
    created_time: str
    created_at: Optional[datetime] = None
    priority_sequence: int = 0
    executable_event_at: Optional[datetime] = None
    # mutable fill state
    status: str = "filled"
    filled_size: float = 0.0
    fill_price: Optional[float] = None
    fee: float = 0.0
    ack_delay_remaining: int = 0
    partial_first_fill: bool = False  # fill base_size/2 first, the remainder on the next cross
    # STEP-2 volume-cap bookkeeping: the cumulative printed volume at-or-through this
    # order's limit that has been OBSERVED while the order was resting (advanced by the
    # driver via ``set_printed_volume`` between ticks). The order's cumulative fill is
    # capped at ``volume_participation_frac × observed_printed_volume``.
    observed_printed_volume: float = 0.0
    # 1.0 means all observed prints when capping is enabled; the cap-disabled mode is
    # represented separately by ``MockBrokerAdapter._volume_cap_enabled``.
    volume_participation_frac: float = 1.0

    def to_normalized(self) -> NormalizedOrder:
        return NormalizedOrder(
            order_id=self.order_id,
            client_order_id=self.client_order_id,
            product_id=self.product_id,
            side=self.side,
            status=self.status,
            order_type=self.order_type,
            filled_size=float(self.filled_size),
            average_filled_price=(float(self.fill_price) if self.fill_price is not None else None),
            created_time=self.created_time,
            raw={"venue": _VENUE, "fee": self.fee},
        )


@dataclass(frozen=True)
class RecordedOrderIntent:
    """Exact captured order request admitted by the sealed ReplayV3 broker.

    This deliberately contains only request fields that cross the venue seam.
    Capture/FSM provenance is verified by ``replay_v3`` before an intent reaches
    this class; the mock then enforces byte-for-byte request parity at PLACE.
    """

    order_intent_sha256: str
    client_order_id: str
    product_id: str
    side: str
    order_type: str
    base_size: float
    time_in_force: str
    extended_hours: bool
    limit_price: Optional[float] = None


@dataclass(frozen=True)
class RecordedBrokerTransition:
    """One canonical broker transition released by ``available_at`` order."""

    event_sha256: str
    sequence: int
    available_at: datetime
    order_intent_sha256: str
    client_order_id: str
    broker_order_id: Optional[str]
    transition: str
    order_quantity: float
    cumulative_filled_quantity: float
    last_fill_quantity: float
    last_fill_price: Optional[float]
    reject_or_cancel_reason: Optional[str] = None


class MockBrokerAdapter:
    """In-memory ``VenueAdapter`` for Replay v3 — deterministic fills off recorded NBBO.

    Construct one per replay run. Before each tick the driver calls ``set_clock(t)`` and
    ``set_quote(product_id, RecordedQuote(...))`` (or ``clear_quote`` for a quoteless name);
    the unchanged ``tick_live_session`` then reads BBO + places/polls orders against this
    instance exactly as it would the real RH/Coinbase adapter.

    Determinism: identical inputs (injected quotes + clock + a fixed ``slippage_bps``) ⇒
    identical fills, with a monotonic counter for order ids (no UUID/wall-clock). No RNG.
    """

    def __init__(
        self,
        *,
        slippage_bps: float = 0.0,
        fee_to_target_ratio: float = 0.0,
        venue_rt_bps: float | None = 0.0,
        max_age_seconds: float = 15.0,
        enabled: bool = True,
        resting_limit_fills: bool = False,
        ack_delay_ticks: int = 0,
        partial_first_fill: bool = False,
        freshness_mode: str = "sim",
        # ── STEP-2 realistic fill model ──────────────────────────────────────────────
        fill_mode: str = FillMode.CONSERVATIVE,
        volume_cap_enabled: bool = False,
        volume_participation_frac: float = DEFAULT_VOLUME_PARTICIPATION_FRAC,
        optimistic_slippage_bps: float = 0.0,
        exact_print_fills: bool = False,
        exact_print_order_latency_seconds: Optional[float] = None,
        account_identity: str = REPLAY_MOCK_ACCOUNT_IDENTITY,
    ) -> None:
        # Injected, per-product recorded NBBO (set as-of the sim clock by the driver).
        self._quotes: dict[str, RecordedQuote] = {}
        self._clock: datetime = datetime.now(timezone.utc).replace(tzinfo=None)
        self._clock_explicitly_set = False
        self._orders: dict[str, _RestingOrder] = {}
        self._fills: list[NormalizedFill] = []
        self._order_seq = itertools.count(1)
        self._slippage_bps = float(slippage_bps)
        self._fee_to_target_ratio = float(fee_to_target_ratio)
        self._venue_rt_bps = venue_rt_bps
        self._max_age_seconds = float(max_age_seconds)
        self._enabled = bool(enabled)
        self._account_identity = str(account_identity or "").strip()
        # P1 FIDELITY KNOBS (default OFF keeps basic marketable-limit behavior):
        #  * resting_limit_fills: enable latency/partial/volume-aware resting realism. Basic
        #    LIMIT price semantics remain mandatory even when this richer mode is off.
        #  * ack_delay_ticks: hold a resting order `open` for N quote advances before it can
        #    cross (exercises the runner's pending-entry ack-poll/timeout path).
        #  * partial_first_fill: the first cross fills HALF; the remainder fills on the next
        #    cross (exercises the runner's partial-entry/partial-exit bookkeeping).
        self._resting_limit_fills = bool(resting_limit_fills)
        self._ack_delay_ticks = (
            max(0, int(ack_delay_ticks)) if self._resting_limit_fills else 0
        )
        self._partial_first_fill = (
            bool(partial_first_fill) and self._resting_limit_fills
        )
        # "sim" is mandatory for sealed replay. "wall" remains only for
        # explicitly noncertifying legacy diagnostics.
        self._freshness_mode = "wall" if str(freshness_mode).lower() == "wall" else "sim"
        # ── STEP-2 realistic fill model state ────────────────────────────────────────
        # (e) explicit conservative/optimistic mode. Conservative = adverse-side crossing
        #     + volume cap + full ack latency (the trustworthy lower bound). Optimistic =
        #     favorable mid + no volume cap + shorter latency (the upper bound).
        self._fill_mode = FillMode.normalize(fill_mode)
        # (b) fill-VOLUME realism. When enabled, a resting order's cumulative fill is capped
        #     at ``volume_participation_frac × observed_printed_volume`` (the printed volume
        #     at-or-through its limit while it rested, fed by ``set_printed_volume``). Default
        #     OFF means uncapped. Optimistic mode also bypasses the cap.
        self._volume_cap_enabled = (
            bool(volume_cap_enabled)
            and self._resting_limit_fills
            and self._fill_mode == FillMode.CONSERVATIVE
        )
        participation_frac = float(volume_participation_frac)
        if (
            not math.isfinite(participation_frac)
            or participation_frac < 0.0
            or participation_frac > 1.0
        ):
            raise ValueError("volume_participation_frac must be finite and within [0, 1]")
        self._volume_participation_frac = participation_frac
        # OPTIMISTIC slippage: 0.0 (mid-favorable) by default. Kept separate so a run can be
        # deliberately optimistic-but-not-free.
        self._optimistic_slippage_bps = float(optimistic_slippage_bps)
        self._exact_print_fills_enabled = bool(exact_print_fills)
        if self._exact_print_fills_enabled:
            if (
                not self._resting_limit_fills
                or not self._volume_cap_enabled
                or self._fill_mode != FillMode.CONSERVATIVE
            ):
                raise ValueError(
                    "exact_print_fills requires conservative resting limits with volume caps"
                )
            if exact_print_order_latency_seconds is None:
                raise ValueError(
                    "exact_print_fills requires an explicit order latency"
                )
            latency = float(exact_print_order_latency_seconds)
            if not math.isfinite(latency) or latency < 0.0:
                raise ValueError(
                    "exact_print_order_latency_seconds must be finite and nonnegative"
                )
            if self._ack_delay_ticks:
                raise ValueError(
                    "exact_print_fills cannot mix tick delay with event-time latency"
                )
            self._exact_print_order_latency_seconds: Optional[float] = latency
        else:
            self._exact_print_order_latency_seconds = None
        self._exact_print_policy_sha256 = _canonical_sha256(
            {
                "schema_version": "chili.replay-exact-print-fill-policy.v1",
                "enabled": self._exact_print_fills_enabled,
                "allocation": "fifo_submission_order_single_shared_print_budget",
                "fifo_tie_key": "priority_sequence_then_order_id",
                "participation_fraction": self._volume_participation_frac,
                "order_latency_seconds": self._exact_print_order_latency_seconds,
                "price_semantics": "quote_side_print_within_limit",
                "quote_side_tolerance": "max_1e-9_or_price_times_1e-12_v1",
                "clock_policy": (
                    "provider_event_for_eligibility_available_at_for_release_"
                    "strict_contiguous_release_ordinal"
                ),
                "condition_policy": "empty_conditions_only",
                "market_order_policy": "coverage_unavailable",
                "fee_to_target_ratio": self._fee_to_target_ratio,
                "venue_round_trip_bps": self._venue_rt_bps,
                "exact_print_price_slippage_bps": 0.0,
            }
        )
        self._exact_print_last_release_ordinal = 0
        self._exact_print_last_sequence = 0
        self._exact_print_last_available_at: Optional[datetime] = None
        self._exact_print_capture_binding: Optional[tuple[str, str, str]] = None
        self._exact_print_inventory: Optional[VerifiedExactPrintInventory] = None
        self._exact_print_seen: set[str] = set()
        self._exact_print_allocations: list[ExactPrintFillAllocation] = []
        self._exact_print_audit: list[dict[str, Any]] = []
        # (c) ack/latency: the driver may install a measured distribution via
        #     ``set_latency_distribution`` (percentile seconds). Absent one, the documented
        #     fallback base (module constants) is used to derive a per-order ack-delay.
        self._ack_latency_seconds: float = (
            DEFAULT_ACK_LATENCY_SECONDS
            if self._fill_mode == FillMode.CONSERVATIVE
            else DEFAULT_ACK_LATENCY_P25_SECONDS
        )
        # Per-product printed-volume observed while an order rests (advanced by the driver).
        self._printed_volume_pending: dict[str, float] = {}
        # Sealed ReplayV3 mode is opt-in and mutually exclusive with quote-
        # generated fills.  It is configured only from a verified capture.  Once
        # enabled, PLACE must match one exact captured intent and order/fill state
        # changes only when a canonical lifecycle transition is released.
        self._recorded_lifecycle_enabled = False
        self._recorded_intents: dict[str, RecordedOrderIntent] = {}
        self._recorded_intent_by_sha: dict[str, RecordedOrderIntent] = {}
        self._recorded_all_transitions: dict[
            str, tuple[RecordedBrokerTransition, ...]
        ] = {}
        self._recorded_released: dict[str, list[RecordedBrokerTransition]] = {}
        self._recorded_bound_client_ids: set[str] = set()
        self._recorded_applied_event_sha256s: set[str] = set()
        # A captured PLACE response is not an input that the mock may look up
        # from the final run inventory.  It becomes usable only after the
        # ReplayV3 causal loader explicitly releases that exact SUBMITTED fact.
        # This deliberately makes the current synchronous live-FSM seam fail
        # closed when the response was captured after the decision prefix.
        self._recorded_available_place_responses: dict[
            str, RecordedBrokerTransition
        ] = {}
        self._recorded_request_violations: list[str] = []
        # The current capture contract records broker lifecycle transitions,
        # but not the exact cancel request/response receipt pair.  Remember at
        # configuration time whether such a receipt would be required so the
        # certification check cannot later infer completeness from the final
        # transition inventory (or from a replay-issued cancel call).
        self._recorded_cancel_receipt_required = False

    @property
    def recorded_lifecycle_enabled(self) -> bool:
        return self._recorded_lifecycle_enabled

    @property
    def freshness_mode(self) -> str:
        return self._freshness_mode

    @property
    def recorded_request_violations(self) -> tuple[str, ...]:
        return tuple(self._recorded_request_violations)

    @property
    def recorded_applied_event_sha256s(self) -> tuple[str, ...]:
        return tuple(sorted(self._recorded_applied_event_sha256s))

    @property
    def recorded_bound_client_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._recorded_bound_client_ids))

    def recorded_place_response_available(self, client_order_id: str) -> bool:
        """Whether the exact captured SUBMITTED response is causally released."""

        cid = str(client_order_id or "").strip()
        return cid in self._recorded_available_place_responses

    @property
    def recorded_cancel_request_complete(self) -> bool:
        return not self._recorded_cancel_receipt_required

    @property
    def exact_print_fills_enabled(self) -> bool:
        return self._exact_print_fills_enabled

    @property
    def exact_print_policy_sha256(self) -> str:
        return self._exact_print_policy_sha256

    @property
    def exact_print_evidence_grade(self) -> str:
        return EXACT_PRINT_FILL_EVIDENCE_GRADE

    @property
    def exact_print_counterfactual_authority(self) -> bool:
        return False

    @property
    def exact_print_counterfactual_authority_blockers(self) -> tuple[str, ...]:
        base = tuple(
            value
            for value in EXACT_PRINT_COUNTERFACTUAL_AUTHORITY_BLOCKERS
            if value != "exact_print_release_tail_completeness_receipt_unavailable"
            or not self.exact_print_terminal_complete
        )
        unresolved = {
            "print_nbbo_unavailable",
            "print_conditions_unsupported",
            "inside_spread_aggressor_unresolved",
            "print_quote_side_ambiguous",
        }
        semantic = tuple(
            f"exact_print_execution_semantics_unavailable:{value['event_sha256']}"
            for value in self._exact_print_audit
            if value.get("disposition") in unresolved
        )
        return base + semantic

    @property
    def exact_print_terminal_complete(self) -> bool:
        inventory = self._exact_print_inventory
        return bool(
            inventory is not None
            and self._exact_print_last_release_ordinal == len(inventory.event_sha256s)
            and self._exact_print_seen == set(inventory.event_sha256s)
        )

    @property
    def exact_print_allocations(self) -> tuple[ExactPrintFillAllocation, ...]:
        return tuple(self._exact_print_allocations)

    @property
    def exact_print_audit(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(copy.deepcopy(value) for value in self._exact_print_audit)

    @property
    def exact_print_allocation_root_sha256(self) -> str:
        return _canonical_sha256(
            {
                "schema_version": "chili.replay-exact-print-allocation-ledger.v1",
                "evidence_grade": EXACT_PRINT_FILL_EVIDENCE_GRADE,
                "counterfactual_authority": False,
                "counterfactual_authority_blockers": list(
                    self.exact_print_counterfactual_authority_blockers
                ),
                "policy_sha256": self._exact_print_policy_sha256,
                "capture_binding": (
                    None
                    if self._exact_print_capture_binding is None
                    else {
                        "capture_identity_sha256": self._exact_print_capture_binding[0],
                        "final_capture_seal_sha256": self._exact_print_capture_binding[1],
                        "release_order_root_sha256": self._exact_print_capture_binding[2],
                    }
                ),
                "expected_inventory_root_sha256": (
                    None
                    if self._exact_print_inventory is None
                    else self._exact_print_inventory.inventory_root_sha256
                ),
                "expected_print_count": (
                    None
                    if self._exact_print_inventory is None
                    else len(self._exact_print_inventory.event_sha256s)
                ),
                "terminal_complete": self.exact_print_terminal_complete,
                "events": self._exact_print_audit,
                "allocations": [
                    value.to_dict() for value in self._exact_print_allocations
                ],
            }
        )

    def configure_verified_exact_print_inventory(
        self, inventory: VerifiedExactPrintInventory
    ) -> None:
        if not self._exact_print_fills_enabled:
            raise ValueError("exact print inventory requires exact-print allocation mode")
        if (
            not isinstance(inventory, VerifiedExactPrintInventory)
            or inventory._verification_token
            is not _VERIFIED_EXACT_PRINT_INVENTORY_TOKEN
        ):
            raise ValueError("exact print inventory lacks verified capture provenance")
        if self._exact_print_seen or self._exact_print_allocations or self._exact_print_audit:
            raise ValueError("exact print inventory cannot change after release starts")
        if self._exact_print_inventory is not None:
            raise ValueError("exact print inventory was already configured")
        self._exact_print_inventory = inventory
        self._exact_print_capture_binding = (
            inventory.capture_identity_sha256,
            inventory.final_capture_seal_sha256,
            inventory.release_order_root_sha256,
        )

    def configure_recorded_lifecycle(
        self,
        *,
        intents: Sequence[RecordedOrderIntent],
        transitions: Sequence[RecordedBrokerTransition],
    ) -> None:
        """Enable strict captured-lifecycle mode before the first replay tick.

        The full transition inventory is used only to validate the FSM's output
        request.  Broker state is never advanced from that inventory: callers
        must explicitly release each transition at its captured ``available_at``.
        """

        if self._exact_print_fills_enabled:
            raise ValueError(
                "recorded lifecycle cannot mix with exact-print allocation mode"
            )
        if self._orders or self._fills or self._recorded_lifecycle_enabled:
            raise ValueError("recorded lifecycle must be configured on a fresh mock")
        by_cid: dict[str, RecordedOrderIntent] = {}
        by_sha: dict[str, RecordedOrderIntent] = {}
        for intent in intents:
            if not isinstance(intent, RecordedOrderIntent):
                raise TypeError("recorded order intent is malformed")
            cid = intent.client_order_id.strip()
            if (
                not cid
                or cid in by_cid
                or intent.order_intent_sha256 in by_sha
                or intent.product_id != intent.product_id.strip().upper()
                or intent.side not in {"buy", "sell"}
                or intent.order_type not in {"market", "limit"}
                or intent.base_size <= 0
            ):
                raise ValueError("recorded order intent inventory is ambiguous")
            by_cid[cid] = intent
            by_sha[intent.order_intent_sha256] = intent

        grouped: dict[str, list[RecordedBrokerTransition]] = {}
        seen_events: set[str] = set()
        for transition in sorted(
            transitions,
            key=lambda row: (row.available_at, row.sequence, row.event_sha256),
        ):
            if not isinstance(transition, RecordedBrokerTransition):
                raise TypeError("recorded broker transition is malformed")
            intent = by_sha.get(transition.order_intent_sha256)
            if (
                intent is None
                or transition.client_order_id != intent.client_order_id
                or transition.event_sha256 in seen_events
                or transition.order_quantity != intent.base_size
            ):
                raise ValueError("recorded broker transition binding is ambiguous")
            seen_events.add(transition.event_sha256)
            grouped.setdefault(intent.client_order_id, []).append(transition)
        if set(grouped) != set(by_cid):
            raise ValueError("recorded lifecycle does not cover every order intent")
        for cid, rows in grouped.items():
            if rows[0].transition != "submitted":
                raise ValueError(f"recorded lifecycle does not start submitted: {cid}")
            if not rows[0].broker_order_id:
                # The unchanged live FSM needs the POST response order identity.
                # A capture that retained only a local SUBMITTED marker is useful
                # diagnostically but cannot drive/certify this synchronous seam.
                raise ValueError(
                    f"recorded submitted transition lacks broker order id: {cid}"
                )
            broker_order_id = str(rows[0].broker_order_id or "").strip()
            prior_available_at: datetime | None = None
            prior_sequence = 0
            prior_cumulative = 0.0
            terminal_seen = False
            for index, row in enumerate(rows):
                if row.available_at.tzinfo is None:
                    raise ValueError(
                        f"recorded lifecycle clock is not timezone-aware: {cid}"
                    )
                if str(row.broker_order_id or "").strip() != broker_order_id:
                    raise ValueError(
                        f"recorded lifecycle broker order identity changed: {cid}"
                    )
                if prior_available_at is not None and (
                    row.available_at < prior_available_at
                    or (
                        row.available_at == prior_available_at
                        and row.sequence <= prior_sequence
                    )
                ):
                    raise ValueError(
                        f"recorded lifecycle causal order regressed: {cid}"
                    )
                if terminal_seen:
                    raise ValueError(
                        f"recorded lifecycle continues after terminal state: {cid}"
                    )
                status = self._recorded_status(row.transition)
                cumulative = float(row.cumulative_filled_quantity)
                delta = cumulative - prior_cumulative
                if (
                    row.order_quantity != by_cid[cid].base_size
                    or cumulative < prior_cumulative
                    or cumulative > float(row.order_quantity)
                    or float(row.last_fill_quantity) < 0.0
                    or abs(delta - float(row.last_fill_quantity)) > 1e-9
                ):
                    raise ValueError(
                        f"recorded lifecycle fill chain is inconsistent: {cid}"
                    )
                if index == 0 and (
                    cumulative != 0.0 or float(row.last_fill_quantity) != 0.0
                ):
                    raise ValueError(
                        f"recorded submitted transition already contains a fill: {cid}"
                    )
                if delta > 0.0 and (
                    row.last_fill_price is None
                    or not math.isfinite(float(row.last_fill_price))
                    or float(row.last_fill_price) <= 0.0
                ):
                    raise ValueError(
                        f"recorded lifecycle fill has no exact price: {cid}"
                    )
                if row.transition == "filled" and cumulative != float(
                    row.order_quantity
                ):
                    raise ValueError(
                        f"recorded filled transition is not cumulative: {cid}"
                    )
                terminal_seen = status in {"filled", "canceled", "rejected"}
                prior_available_at = row.available_at
                prior_sequence = int(row.sequence)
                prior_cumulative = cumulative

        self._recorded_intents = by_cid
        self._recorded_intent_by_sha = by_sha
        self._recorded_all_transitions = {
            cid: tuple(rows) for cid, rows in grouped.items()
        }
        self._recorded_cancel_receipt_required = any(
            row.transition
            in {"pending_cancel", "canceled", "pending_replace", "replaced"}
            for rows in grouped.values()
            for row in rows
        )
        self._recorded_released = {cid: [] for cid in by_cid}
        self._recorded_lifecycle_enabled = True

    @staticmethod
    def _recorded_status(transition: str) -> str:
        normalized = str(transition).strip().lower()
        if normalized in {
            "submitted",
            "pending_new",
            "new",
            "accepted",
            "accepted_for_bidding",
            "pending_cancel",
            "pending_replace",
            "held",
            "suspended",
            "stopped",
            "calculated",
        }:
            return "open"
        if normalized == "partially_filled":
            return "partially_filled"
        if normalized == "filled":
            return "filled"
        if normalized in {"canceled", "replaced", "expired", "done_for_day"}:
            return "canceled"
        if normalized in {"rejected", "failed"}:
            return "rejected"
        raise ValueError(f"unsupported recorded broker transition: {transition}")

    def release_recorded_transition(
        self, transition: RecordedBrokerTransition
    ) -> None:
        """Release a post-PLACE lifecycle fact in exact chain order.

        SUBMITTED is the synchronous PLACE response: merely reaching its
        availability frontier must not create an order.  The matching exact
        PLACE request consumes it inside ``_recorded_place``.  Later facts are
        rejected until that request/response pair owns the order.
        """

        if not self._recorded_lifecycle_enabled:
            raise ValueError("recorded lifecycle mode is not configured")
        if transition.transition == "submitted":
            if transition.event_sha256 in self._recorded_applied_event_sha256s:
                return
            expected = self._recorded_all_transitions.get(
                transition.client_order_id, ()
            )
            if not expected or expected[0] != transition:
                raise ValueError(
                    "recorded PLACE response release order diverged"
                )
            if transition.client_order_id in self._recorded_available_place_responses:
                raise ValueError("recorded PLACE response was released twice")
            self._recorded_available_place_responses[
                transition.client_order_id
            ] = transition
            return
        if transition.client_order_id not in self._recorded_bound_client_ids:
            raise ValueError(
                "recorded broker transition became visible before its PLACE request"
            )
        self._apply_recorded_transition(transition)

    def _apply_recorded_transition(
        self,
        transition: RecordedBrokerTransition,
        *,
        released_place_response: bool = False,
    ) -> None:
        """Apply one response/transition after its causal request boundary."""

        if not self._recorded_lifecycle_enabled:
            raise ValueError("recorded lifecycle mode is not configured")
        if transition.event_sha256 in self._recorded_applied_event_sha256s:
            raise ValueError("recorded broker transition was released twice")
        intent = self._recorded_intent_by_sha.get(transition.order_intent_sha256)
        if intent is None or intent.client_order_id != transition.client_order_id:
            raise ValueError("recorded broker transition escaped its intent")
        released = self._recorded_released[intent.client_order_id]
        if released_place_response:
            # PLACE may consume only the response object deposited by the causal
            # release path.  In particular, it must not inspect the complete
            # lifecycle inventory to discover or validate a future ACK.
            if (
                released
                or transition.transition != "submitted"
                or self._recorded_available_place_responses.get(
                    intent.client_order_id
                )
                != transition
            ):
                raise ValueError("released PLACE response binding diverged")
        else:
            expected_rows = self._recorded_all_transitions[intent.client_order_id]
            if (
                len(released) >= len(expected_rows)
                or expected_rows[len(released)] != transition
            ):
                raise ValueError("recorded broker transition release order diverged")

        released.append(transition)
        self._recorded_applied_event_sha256s.add(transition.event_sha256)
        order_id = str(transition.broker_order_id or "").strip()
        if not order_id:
            raise ValueError("released broker transition has no broker order id")
        ro = self._orders.get(order_id)
        if ro is None:
            ro = _RestingOrder(
                order_id=order_id,
                client_order_id=intent.client_order_id,
                product_id=intent.product_id,
                side=intent.side,
                order_type=intent.order_type,
                base_size=float(intent.base_size),
                limit_price=intent.limit_price,
                created_time=transition.available_at.astimezone(timezone.utc).isoformat(),
                status=self._recorded_status(transition.transition),
                filled_size=0.0,
                fill_price=None,
            )
            self._orders[order_id] = ro
        elif ro.client_order_id != intent.client_order_id:
            raise ValueError("recorded broker order id changed ownership")

        prior_cumulative = float(ro.filled_size)
        if transition.cumulative_filled_quantity < prior_cumulative:
            raise ValueError("recorded cumulative fill regressed")
        delta = transition.cumulative_filled_quantity - prior_cumulative
        if abs(delta - transition.last_fill_quantity) > 1e-9:
            raise ValueError("recorded fill delta differs from cumulative fill")
        if delta > 0:
            if transition.last_fill_price is None:
                raise ValueError("recorded fill delta has no exact price")
            self._book_fill(
                ro,
                qty=float(delta),
                price=float(transition.last_fill_price),
                fill_id=f"{order_id}:{transition.event_sha256}",
                raw={
                    "venue": _VENUE,
                    "recorded_lifecycle": True,
                    "event_sha256": transition.event_sha256,
                    "sequence": transition.sequence,
                },
            )
        ro.status = self._recorded_status(transition.transition)

    def _recorded_place(
        self,
        *,
        product_id: str,
        side: str,
        base_size: float,
        order_type: str,
        limit_price: Optional[float],
        client_order_id: Optional[str],
        time_in_force: Optional[str],
        extended_hours: bool,
    ) -> dict[str, Any]:
        cid = str(client_order_id or "").strip()
        intent = self._recorded_intents.get(cid)
        normalized_tif = str(time_in_force or "").strip().lower()
        if normalized_tif == "gfd" or (
            not normalized_tif and intent is not None and intent.order_type == "market"
        ):
            normalized_tif = "day"
        violation: Optional[str] = None
        if intent is None:
            violation = "recorded_order_intent_missing"
        elif cid in self._recorded_bound_client_ids:
            violation = "recorded_order_intent_reused"
        elif (
            str(product_id).strip().upper() != intent.product_id
            or str(side).strip().lower() != intent.side
            or str(order_type).strip().lower() != intent.order_type
            or abs(float(base_size) - float(intent.base_size)) > 1e-9
            or normalized_tif != intent.time_in_force
            or bool(extended_hours) is not intent.extended_hours
            or (
                (limit_price is None) != (intent.limit_price is None)
                or (
                    limit_price is not None
                    and intent.limit_price is not None
                    and abs(float(limit_price) - float(intent.limit_price)) > 1e-9
                )
            )
        ):
            violation = "recorded_order_intent_request_mismatch"
        if violation is not None:
            self._recorded_request_violations.append(f"{cid or '<missing>'}:{violation}")
            return {
                "ok": False,
                "venue": _VENUE,
                "error": violation,
                "client_order_id": cid or None,
            }

        released = self._recorded_released.get(cid, [])
        if released:
            raise ValueError("recorded PLACE response was already consumed")
        current = self._recorded_available_place_responses.get(cid)
        if current is None:
            # The unchanged live FSM expects a synchronous broker order id from
            # PLACE.  If SUBMITTED was captured after this decision frontier,
            # replay cannot manufacture that future response, move the decision
            # clock, or pre-release the ACK.  Record an explicit architectural
            # blocker and leave broker state completely untouched.
            violation = SEALED_REPLAY_SYNC_ACK_ARCHITECTURAL_BLOCKER
            self._recorded_request_violations.append(f"{cid}:{violation}")
            return {
                "ok": False,
                "venue": _VENUE,
                "error": violation,
                "client_order_id": cid,
                "architectural_blocker": True,
                "blocker_detail": (
                    "captured SUBMITTED response was not causally available "
                    "when synchronous PLACE required its broker order id"
                ),
            }
        if current.transition != "submitted":
            raise ValueError("released PLACE response is not submitted")
        # The exact request has now met the exact response at a causally
        # released frontier.  Only this boundary may create broker state.
        self._apply_recorded_transition(current, released_place_response=True)
        order_id = str(current.broker_order_id or "")
        ro = self._orders.get(order_id)
        if ro is None:
            raise ValueError("released recorded order state is missing")
        self._recorded_bound_client_ids.add(cid)
        self._recorded_available_place_responses.pop(cid, None)
        return {
            "ok": True,
            "venue": _VENUE,
            "order_id": order_id,
            "client_order_id": cid,
            "status": ro.status,
            "raw": {
                "recorded_lifecycle": True,
                "filled_size": float(ro.filled_size),
                "fill_price": (
                    float(ro.fill_price) if ro.fill_price is not None else None
                ),
                "order_type": intent.order_type,
                "limit_price": intent.limit_price,
                "released_event_sha256": current.event_sha256,
            },
        }

    def get_account_identity_truth(self) -> dict[str, Any]:
        """Return the deterministic, non-secret identity frozen into replay sessions."""

        identity = self._account_identity
        return {
            "readable": bool(identity),
            "identity": identity or None,
            "reason": None if identity else "replay_mock_account_identity_missing",
        }

    def set_account_identity(self, identity: str) -> None:
        """Replay-only seam for modeling an account switch/identity rotation."""

        self._account_identity = str(identity or "").strip()

    # ── driver-side injection seams (NOT part of the VenueAdapter protocol) ──────────
    def set_clock(self, t: datetime) -> None:
        """Freeze the broker's quote/fill clock at ``t`` (naive-UTC normalized).

        In resting mode this is the tick that ADVANCES resting orders: decrement their
        ack-delay and re-test the cross against the current per-product quote."""
        if t.tzinfo is not None:
            t = t.astimezone(timezone.utc).replace(tzinfo=None)
        if (
            self._exact_print_fills_enabled
            and self._clock_explicitly_set
            and t < self._clock
        ):
            raise ValueError("exact-print simulation clock cannot move backwards")
        self._clock = t
        self._clock_explicitly_set = True
        if not self._recorded_lifecycle_enabled:
            self._advance_resting_orders()

    def set_quote(self, product_id: str, quote: RecordedQuote) -> None:
        self._quotes[str(product_id).upper()] = quote
        if not self._recorded_lifecycle_enabled:
            # A new quote can satisfy a resting cross immediately (the driver may set the clock
            # then the quote, or only the quote, between ticks) — re-test on quote arrival too.
            self._advance_resting_orders(product_id=str(product_id).upper())

    def clear_quote(self, product_id: str) -> None:
        """Remove a product's quote ⇒ subsequent reads return ``no_bbo`` (RVMDW path)."""
        self._quotes.pop(str(product_id).upper(), None)

    # ── STEP-2 realistic-fill driver seams (NOT part of the VenueAdapter protocol) ────
    def release_verified_exact_print(self, print_event: VerifiedExactPrint) -> None:
        """Allocate one sealed print once across eligible orders in FIFO order.

        This path deliberately cannot consume caller-supplied aggregate volume.
        The single participation budget belongs to the print, not to each open
        order, so concurrent orders cannot each claim the same market liquidity.
        """

        if not self._exact_print_fills_enabled:
            raise ValueError("exact print fill allocation is not enabled")
        if (
            not isinstance(print_event, VerifiedExactPrint)
            or print_event._verification_token is not _VERIFIED_EXACT_PRINT_TOKEN
        ):
            raise ValueError("exact print lacks verified sealed-capture provenance")
        inventory = self._exact_print_inventory
        if inventory is None:
            raise ValueError("verified exact print inventory is not configured")
        if print_event.event_sha256 not in set(inventory.event_sha256s):
            raise ValueError("exact print is outside the verified inventory")
        if self._recorded_lifecycle_enabled:
            raise ValueError("exact print fills cannot mix with recorded broker lifecycle")
        if not self._clock_explicitly_set:
            raise ValueError("exact print release requires an explicit simulation clock")
        current_available = self._clock.replace(tzinfo=timezone.utc)
        if print_event.available_at > current_available:
            raise ValueError("exact print was released before its available_at frontier")
        if print_event.provider_event_at > print_event.available_at:
            raise ValueError("exact print provider clock is ahead of its availability clock")
        if print_event.provider_event_at > print_event.received_at:
            raise ValueError("exact print provider clock is ahead of its receive clock")
        capture_binding = (
            print_event.capture_identity_sha256,
            print_event.final_capture_seal_sha256,
            print_event.release_order_root_sha256,
        )
        if print_event.event_sha256 in self._exact_print_seen:
            raise ValueError("exact print was released twice")
        expected_release_ordinal = self._exact_print_last_release_ordinal + 1
        if print_event.release_ordinal != expected_release_ordinal:
            raise ValueError("exact print release ordinal is not contiguous")
        if print_event.sequence <= self._exact_print_last_sequence:
            raise ValueError("exact print capture sequence did not increase")
        if (
            self._exact_print_last_available_at is not None
            and print_event.available_at < self._exact_print_last_available_at
        ):
            # CaptureCoverageManifest rejects this invariant before an adapter
            # can mint the object; retain the check at the allocation boundary
            # so a private-looking counterfeit cannot weaken causal order.
            raise ValueError("exact print availability clock regressed")
        if (
            self._exact_print_capture_binding is not None
            and capture_binding != self._exact_print_capture_binding
        ):
            raise ValueError("exact print capture binding changed within one allocation run")
        self._exact_print_capture_binding = capture_binding
        self._exact_print_seen.add(print_event.event_sha256)
        self._exact_print_last_release_ordinal = print_event.release_ordinal
        self._exact_print_last_sequence = print_event.sequence
        self._exact_print_last_available_at = print_event.available_at

        classified_side: Optional[str] = None
        disposition = "no_eligible_order"
        if print_event.bid is None or print_event.ask is None:
            disposition = "print_nbbo_unavailable"
        elif print_event.conditions:
            disposition = "print_conditions_unsupported"
        else:
            tolerance = max(1e-9, abs(print_event.price) * 1e-12)
            at_ask = print_event.price >= float(print_event.ask) - tolerance
            at_bid = print_event.price <= float(print_event.bid) + tolerance
            if float(print_event.ask) <= float(print_event.bid) + tolerance or (
                at_ask and at_bid
            ):
                disposition = "print_quote_side_ambiguous"
            elif at_ask:
                classified_side = "buy"
            elif at_bid:
                classified_side = "sell"
            else:
                disposition = "inside_spread_aggressor_unresolved"

        candidates: list[_RestingOrder] = []
        if classified_side is not None:
            accepted_sides = (
                ("buy", "bid", "long")
                if classified_side == "buy"
                else ("sell", "ask", "short")
            )
            for order in self._orders.values():
                if (
                    order.status != "open"
                    or order.product_id != print_event.product_id
                    or order.order_type != "limit"
                    or order.side not in accepted_sides
                    or order.executable_event_at is None
                    or order.executable_event_at > print_event.provider_event_at
                    or order.limit_price is None
                ):
                    continue
                limit = float(order.limit_price)
                if classified_side == "buy" and print_event.price > limit:
                    continue
                if classified_side == "sell" and print_event.price < limit:
                    continue
                candidates.append(order)
            candidates.sort(key=lambda value: (value.priority_sequence, value.order_id))

        budget = self._volume_participation_frac * print_event.size
        remaining_budget = budget
        event_allocations: list[str] = []
        for order in candidates:
            remaining_order = max(0.0, order.base_size - order.filled_size)
            quantity = min(remaining_order, remaining_budget)
            if quantity <= 1e-9:
                break
            allocation_index = len(self._exact_print_allocations) + 1
            fill_id = (
                f"{order.order_id}:exact:{print_event.event_sha256}:"
                f"{allocation_index}"
            )
            self._book_fill(
                order,
                qty=quantity,
                price=print_event.price,
                fill_id=fill_id,
                trade_time=print_event.provider_event_at,
                raw={
                    "venue": _VENUE,
                    "exact_print_allocation": True,
                    "event_sha256": print_event.event_sha256,
                    "sequence": print_event.sequence,
                    "release_ordinal": print_event.release_ordinal,
                    "provider_event_at": print_event.provider_event_at.isoformat(),
                    "received_at": print_event.received_at.isoformat(),
                    "available_at": print_event.available_at.isoformat(),
                    "policy_sha256": self._exact_print_policy_sha256,
                },
            )
            order.status = (
                "filled"
                if order.base_size - order.filled_size <= 1e-9
                else "open"
            )
            allocation = ExactPrintFillAllocation(
                event_sha256=print_event.event_sha256,
                sequence=print_event.sequence,
                release_ordinal=print_event.release_ordinal,
                allocation_index=allocation_index,
                order_id=order.order_id,
                client_order_id=order.client_order_id,
                product_id=order.product_id,
                side=classified_side,
                quantity=quantity,
                price=print_event.price,
                provider_event_at=print_event.provider_event_at,
                received_at=print_event.received_at,
                available_at=print_event.available_at,
            )
            self._exact_print_allocations.append(allocation)
            event_allocations.append(fill_id)
            remaining_budget -= quantity
        if event_allocations:
            disposition = "allocated"
        self._exact_print_audit.append(
            {
                "event_sha256": print_event.event_sha256,
                "sequence": print_event.sequence,
                "release_ordinal": print_event.release_ordinal,
                "capture_identity_sha256": print_event.capture_identity_sha256,
                "final_capture_seal_sha256": print_event.final_capture_seal_sha256,
                "release_order_root_sha256": print_event.release_order_root_sha256,
                "product_id": print_event.product_id,
                "provider_event_at": print_event.provider_event_at.isoformat(),
                "received_at": print_event.received_at.isoformat(),
                "available_at": print_event.available_at.isoformat(),
                "price": print_event.price,
                "size": print_event.size,
                "classified_side": classified_side,
                "participation_budget": budget,
                "candidate_order_ids": [value.order_id for value in candidates],
                "allocation_fill_ids": event_allocations,
                "allocated_quantity": budget - remaining_budget,
                "unallocated_quantity": remaining_budget,
                "disposition": disposition,
            }
        )

    def set_printed_volume(self, product_id: str, printed_volume: float) -> None:
        """Feed the recorded printed volume that traded AT-OR-THROUGH a resting order's
        limit during THIS advance window (from ``iqfeed_trade_ticks`` prints the driver
        selected as-of ``t`` with ``price <= ask``/``>= bid`` and inside the limit). The
        volume-cap fill model consumes it: each still-``open`` order for the product accrues
        this volume, and its cumulative fill is capped at ``frac × observed_printed_volume``.

        Additive across advances — the driver passes the INCREMENT since the last advance
        (or the full at-or-through-limit volume for a single-advance immediate order)."""
        if self._exact_print_fills_enabled:
            raise ValueError(
                "aggregate printed volume cannot enter exact-print allocation mode"
            )
        pid = str(product_id).upper()
        inc = float(printed_volume)
        if not math.isfinite(inc) or inc < 0.0:
            raise ValueError("printed_volume must be finite and nonnegative")
        self._printed_volume_pending[pid] = self._printed_volume_pending.get(pid, 0.0) + inc
        # accrue onto every resting order for this product, then re-test the cross/cap
        for ro in self._orders.values():
            if ro.status == "open" and ro.product_id == pid:
                ro.observed_printed_volume += inc
        if not self._recorded_lifecycle_enabled:
            self._advance_resting_orders(product_id=pid)

    def set_latency_distribution(
        self,
        *,
        median_seconds: Optional[float] = None,
        p25_seconds: Optional[float] = None,
        p75_seconds: Optional[float] = None,
    ) -> None:
        """Install the MEASURED place→fill latency distribution (percentile seconds) the
        driver recomputed from ``trading_automation_events`` for this run. Conservative mode
        uses the median (the trustworthy central latency); optimistic uses p25 (fast fills).
        Absent this call the documented fallback base (module constants) applies."""
        if self._fill_mode == FillMode.CONSERVATIVE:
            base = median_seconds if median_seconds is not None else DEFAULT_ACK_LATENCY_SECONDS
        else:
            base = p25_seconds if p25_seconds is not None else DEFAULT_ACK_LATENCY_P25_SECONDS
        self._ack_latency_seconds = max(0.0, float(base))

    def ack_delay_ticks_for(self, tick_seconds: float) -> int:
        """Convert the mode's ack latency (seconds) into an integer number of quote
        advances for a grid whose median spacing is ``tick_seconds``. Deterministic (round
        half-up), never negative. Used by the driver to set ``ack_delay_ticks`` per run so
        the pending-entry ack window reflects the REAL measured broker latency rather than a
        magic constant."""
        if tick_seconds is None or float(tick_seconds) <= 0:
            return 0
        return max(0, int(self._ack_latency_seconds / float(tick_seconds) + 0.5))

    def _quote_for(self, product_id: str) -> Optional[RecordedQuote]:
        q = self._quotes.get(str(product_id).upper())
        if q is None or not q.is_valid():
            return None
        return q

    def _freshness(self) -> FreshnessMeta:
        # The freshness STAMP. Two modes (``freshness_mode``):
        #
        #   * ``"sim"`` (default): stamp at the replay clock.
        #   * ``"wall"``: legacy diagnostic mode only; sealed ReplayV3 rejects it.
        #
        # The live runner passes its replay-aware clock into freshness checks,
        # so historical stamps are deterministic without a wall-time escape.
        if self._freshness_mode == "wall":
            stamp = datetime.now(timezone.utc)
            return FreshnessMeta(
                retrieved_at_utc=stamp,
                provider_time_utc=None,
                max_age_seconds=self._max_age_seconds,
            )
        return FreshnessMeta(
            retrieved_at_utc=self._clock.replace(tzinfo=timezone.utc),
            provider_time_utc=self._clock.replace(tzinfo=timezone.utc),
            max_age_seconds=self._max_age_seconds,
        )

    # ── VenueAdapter protocol surface ───────────────────────────────────────────────
    def is_enabled(self) -> bool:
        return self._enabled

    def get_best_bid_ask(self, product_id: str) -> tuple[Optional[NormalizedTicker], FreshnessMeta]:
        fresh = self._freshness()
        q = self._quote_for(product_id)
        if q is None:
            return None, fresh  # no_bbo — the runner emits live_blocked_by_risk reason=no_bbo
        mid = q.mid
        spread_abs = float(q.ask) - float(q.bid)
        spread_bps = (spread_abs / mid) * 10_000.0 if mid > 0 else None
        ticker = NormalizedTicker(
            product_id=str(product_id).upper(),
            bid=float(q.bid),
            ask=float(q.ask),
            mid=mid,
            spread_abs=spread_abs,
            spread_bps=spread_bps,
            last_price=(float(q.last) if q.last is not None else None),
            freshness=fresh,
            raw={"venue": _VENUE},
        )
        return ticker, fresh

    def get_ticker(self, product_id: str) -> tuple[Optional[NormalizedTicker], FreshnessMeta]:
        return self.get_best_bid_ask(product_id)

    def get_product(self, product_id: str) -> tuple[Optional[NormalizedProduct], FreshnessMeta]:
        # Minimal "online, fully tradable" product so the runner's tradability gate passes.
        prod = NormalizedProduct(
            product_id=str(product_id).upper(),
            base_currency=str(product_id).upper().split("-")[0],
            quote_currency="USD",
            status="online",
            trading_disabled=False,
            cancel_only=False,
            limit_only=False,
            post_only=False,
            auction_mode=False,
            raw={"venue": _VENUE},
        )
        return prod, self._freshness()

    def get_products(self) -> tuple[list[NormalizedProduct], FreshnessMeta]:
        prods = [self.get_product(pid)[0] for pid in list(self._quotes.keys())]
        return [p for p in prods if p is not None], self._freshness()

    def get_recent_trades(
        self, product_id: str, *, limit: int = 50
    ) -> tuple[list[dict[str, Any]], FreshnessMeta]:
        return [], self._freshness()

    def list_open_orders(
        self, *, product_id: Optional[str] = None, limit: int = 50
    ) -> tuple[list[NormalizedOrder], FreshnessMeta]:
        # Immediate-fill (P0): nothing rests open. Resting (P1): the still-``open`` orders.
        pid = str(product_id).upper() if product_id is not None else None
        opens = [
            o.to_normalized()
            for o in self._orders.values()
            if o.status == "open" and (pid is None or o.product_id == pid)
        ]
        return list(opens[: int(limit)]), self._freshness()

    def list_open_orders_truth(
        self, *, product_id: Optional[str] = None, limit: int = 50
    ) -> dict[str, Any]:
        orders, freshness = self.list_open_orders(
            product_id=product_id, limit=limit
        )
        return {
            "readable": True,
            "orders": orders,
            "freshness": freshness,
            "reason": None,
        }

    def get_order(self, order_id: str) -> tuple[Optional[NormalizedOrder], FreshnessMeta]:
        o = self._orders.get(str(order_id))
        return (o.to_normalized() if o is not None else None), self._freshness()

    def get_order_truth(self, order_id: str) -> dict[str, Any]:
        order, freshness = self.get_order(order_id)
        return {
            "readable": True,
            "found": order is not None,
            "order": order,
            "freshness": freshness,
            "reason": None,
        }

    def get_position_quantity_truth(self, product_id: str) -> dict[str, Any]:
        pid = str(product_id or "").strip().upper()
        quantity = 0.0
        for fill in self._fills:
            if fill.product_id != pid:
                continue
            quantity += (
                float(fill.size)
                if str(fill.side).lower() in {"buy", "bid", "long"}
                else -float(fill.size)
            )
        return {
            "readable": True,
            "product_id": pid,
            "quantity": quantity,
            "reason": None,
        }

    def get_fills(
        self, *, product_id: Optional[str] = None, limit: int = 50
    ) -> tuple[list[NormalizedFill], FreshnessMeta]:
        fills = self._fills
        if product_id is not None:
            pid = str(product_id).upper()
            fills = [f for f in fills if f.product_id == pid]
        return list(fills[-int(limit):]), self._freshness()

    def place_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        client_order_id: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # ``**kwargs`` tolerates venue-specific extras the runner threads to the REAL adapters
        # (e.g. time_in_force / overnight / post_only) — the mock ignores them, exactly as a
        # crypto adapter ignores ``overnight``. Keeps the mock a drop-in across families.
        return self._fill_order(
            product_id=product_id,
            side=side,
            base_size=base_size,
            order_type="market",
            limit_price=None,
            client_order_id=client_order_id,
            time_in_force=kwargs.get("time_in_force"),
            extended_hours=bool(kwargs.get("extended_hours", False)),
        )

    def place_limit_order_gtc(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        limit_price: str,
        client_order_id: Optional[str] = None,
        extended_hours: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self._fill_order(
            product_id=product_id,
            side=side,
            base_size=base_size,
            order_type="limit",
            limit_price=limit_price,
            client_order_id=client_order_id,
            time_in_force=kwargs.get("time_in_force"),
            extended_hours=bool(extended_hours),
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        # Always accept (mirrors protocol.cancel_order). In resting mode an ``open`` order is
        # marked ``cancelled`` so a later cross can't fill an order the runner abandoned (the
        # ack-timeout → re-watch path); a partial keeps its already-filled size.
        o = self._orders.get(str(order_id))
        if self._recorded_lifecycle_enabled:
            if o is None:
                self._recorded_request_violations.append(
                    f"{order_id}:recorded_cancel_order_missing"
                )
                return {
                    "ok": False,
                    "venue": _VENUE,
                    "order_id": str(order_id),
                    "error": "recorded_cancel_order_missing",
                }
            cid = str(o.client_order_id or "")
            # A lifecycle-only capture cannot prove the request the FSM sent or
            # the synchronous response it observed.  In particular, looking at
            # the remaining recorded transitions to decide that this request
            # "would have" succeeded leaks future facts.  Until exact cancel
            # request/response receipts are part of the sealed prefix, fail
            # closed and leave the recorded order/lifecycle state untouched.
            violation = SEALED_REPLAY_CANCEL_RECEIPT_ARCHITECTURAL_BLOCKER
            self._recorded_request_violations.append(f"{cid}:{violation}")
            return {
                "ok": False,
                "venue": _VENUE,
                "order_id": str(order_id),
                "client_order_id": cid,
                "status": o.status,
                "error": violation,
                "architectural_blocker": True,
                "blocker_detail": (
                    "sealed capture has broker lifecycle transitions but no exact "
                    "cancel request/response receipt"
                ),
            }
        if o is not None and o.status == "open":
            o.status = "cancelled"
        return {"ok": True, "venue": _VENUE, "order_id": str(order_id), "status": "cancelled"}

    def preview_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: Optional[str] = None,
        quote_size: Optional[str] = None,
    ) -> dict[str, Any]:
        q = self._quote_for(product_id)
        if q is None:
            return {"ok": False, "venue": _VENUE, "error": "no_bbo"}
        return {"ok": True, "venue": _VENUE, "mid": q.mid, "bid": q.bid, "ask": q.ask}

    def get_account_snapshot(self) -> dict[str, Any]:
        return {"ok": True, "venue": _VENUE, "data": {}, "raw": {}}

    # ── deterministic fill model (pure paper-fill math) ─────────────────────────────
    def _fill_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        order_type: str,
        limit_price: Optional[str],
        client_order_id: Optional[str],
        time_in_force: Optional[str] = None,
        extended_hours: bool = False,
    ) -> dict[str, Any]:
        q = self._quote_for(product_id)
        if q is None:
            # no_bbo reject — the runner takes the place-failed / no_bbo decline branch.
            return {
                "ok": False,
                "venue": _VENUE,
                "error": "no_bbo",
                "client_order_id": client_order_id,
            }
        try:
            size = float(base_size)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "venue": _VENUE,
                "error": "bad_base_size",
                "client_order_id": client_order_id,
            }
        if not math.isfinite(size) or not (size > 0):
            return {
                "ok": False,
                "venue": _VENUE,
                "error": "bad_base_size",
                "client_order_id": client_order_id,
            }
        s = str(side or "").strip().lower()
        if s not in {"buy", "bid", "long", "sell", "ask", "short"}:
            return {
                "ok": False,
                "venue": _VENUE,
                "error": "bad_side",
                "client_order_id": client_order_id,
            }
        normalized_order_type = str(order_type or "").strip().lower()
        if normalized_order_type not in {"market", "limit"}:
            return {
                "ok": False,
                "venue": _VENUE,
                "error": "bad_order_type",
                "client_order_id": client_order_id,
            }
        _lim = _float_or_none(limit_price)
        if normalized_order_type == "limit" and (
            _lim is None or not math.isfinite(_lim) or _lim <= 0.0
        ):
            return {
                "ok": False,
                "venue": _VENUE,
                "error": "bad_limit_price",
                "client_order_id": client_order_id,
            }

        if self._recorded_lifecycle_enabled:
            return self._recorded_place(
                product_id=product_id,
                side=s,
                base_size=size,
                order_type=normalized_order_type,
                limit_price=_lim,
                client_order_id=client_order_id,
                time_in_force=time_in_force,
                extended_hours=extended_hours,
            )

        if self._exact_print_fills_enabled and normalized_order_type != "limit":
            # Exact prints can conservatively allocate a bounded resting-limit
            # participation budget. They do not prove the contemporaneous depth,
            # queue, or sweep needed to fabricate a market-order execution.
            return {
                "ok": False,
                "venue": _VENUE,
                "error": EXACT_PRINT_MARKET_FILL_UNAVAILABLE,
                "client_order_id": client_order_id,
                "coverage_unavailable": True,
            }

        priority_sequence = next(self._order_seq)
        order_id = f"{_VENUE}-{priority_sequence:08d}"
        created_at = self._clock.replace(tzinfo=timezone.utc)
        created = created_at.isoformat()
        order_type = normalized_order_type

        # Every LIMIT honors its price even in the legacy immediate model. A marketable limit
        # can still fill on the placement quote; an unmarketable limit rests until a later
        # recorded NBBO crosses it. ``resting_limit_fills`` retains the richer latency,
        # partial-fill, and printed-volume behavior, but can never toggle basic limit-price
        # semantics off. MARKET orders continue to cross immediately.
        if order_type == "limit":
            pid_u = str(product_id).upper()
            ro = _RestingOrder(
                order_id=order_id,
                client_order_id=client_order_id,
                product_id=pid_u,
                side=s,
                order_type=order_type,
                base_size=size,
                limit_price=_lim,
                created_time=created,
                created_at=created_at,
                priority_sequence=priority_sequence,
                executable_event_at=(
                    created_at
                    + timedelta(
                        seconds=float(
                            self._exact_print_order_latency_seconds or 0.0
                        )
                    )
                    if self._exact_print_fills_enabled
                    else created_at
                ),
                status="open",
                filled_size=0.0,
                fill_price=None,
                fee=0.0,
                ack_delay_remaining=self._ack_delay_ticks,
                partial_first_fill=self._partial_first_fill,
                # STEP-2 (b): stamp the participation cap onto the order (1.0 ⇒ uncapped when
                # volume-capping is off). Seed the observed-printed-volume with whatever has
                # already been fed for this product this window.
                volume_participation_frac=(
                    self._volume_participation_frac if self._volume_cap_enabled else 1.0
                ),
                observed_printed_volume=self._printed_volume_pending.get(pid_u, 0.0),
            )
            self._orders[order_id] = ro
            # An at-or-through-market limit with no ack delay crosses on THIS placement quote.
            self._maybe_cross(ro, q)
            return {
                "ok": True,
                "venue": _VENUE,
                "order_id": order_id,
                "client_order_id": client_order_id,
                "status": ro.status,
                "raw": {
                    "filled_size": float(ro.filled_size),
                    "fill_price": (float(ro.fill_price) if ro.fill_price is not None else None),
                    "fee": float(ro.fee),
                    "order_type": order_type,
                    "limit_price": limit_price,
                },
            }

        # IMMEDIATE MODE applies only to MARKET orders: cross now.
        fill_price = self._cross_price(s, q)
        ro = _RestingOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            product_id=str(product_id).upper(),
            side=s,
            order_type=order_type,
            base_size=size,
            limit_price=_lim,
            created_time=created,
            created_at=created_at,
            priority_sequence=priority_sequence,
            executable_event_at=created_at,
            status="filled",
        )
        self._orders[order_id] = ro
        # _book_fill sets filled_size/fill_price/fee + appends the NormalizedFill.
        self._book_fill(ro, qty=float(size), price=float(fill_price))
        return {
            "ok": True,
            "venue": _VENUE,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "status": "filled",
            "raw": {
                "fill_price": float(fill_price),
                "filled_size": float(size),
                "fee": float(ro.fee),
                "order_type": order_type,
                "limit_price": limit_price,
            },
        }

    # ── resting-fill mechanics (P1; zero network, deterministic) ─────────────────────
    def _cross_price(self, side: str, q: RecordedQuote) -> float:
        """Fill PRICE for a marketable order against quote ``q`` (REUSE the pure paper math).

        STEP-2 mode-aware:
          * CONSERVATIVE — cross the ADVERSE side: buy at ask + slip, sell at bid − slip.
          * OPTIMISTIC   — cross at the FAVORABLE MID (buy at mid + optimistic_slip, sell at
            mid − optimistic_slip). Still bounded by the recorded quote (mid is inside the
            spread), so optimistic is generous but NEVER prices outside the recorded book."""
        is_buy = str(side).lower() in ("buy", "bid", "long")
        if self._fill_mode == FillMode.OPTIMISTIC:
            # Favorable: fill at the recorded mid (± a small optional optimistic slip).
            if is_buy:
                return long_entry_fill_price(q.mid, q.mid, self._optimistic_slippage_bps)
            return long_exit_fill_price(q.mid, q.mid, self._optimistic_slippage_bps)
        # Conservative (default): adverse-side crossing.
        if is_buy:
            return long_entry_fill_price(q.ask, q.mid, self._slippage_bps)
        return long_exit_fill_price(q.bid, q.mid, self._slippage_bps)

    def _limit_crosses(self, ro: _RestingOrder, q: RecordedQuote) -> bool:
        """A resting LIMIT crosses when the recorded NBBO trades through it: a BUY limit
        crosses once the ask is at/below the limit; a SELL limit once the bid is at/above it.
        A limit of ``None`` (defensive) is treated as marketable (always crosses)."""
        if ro.limit_price is None:
            return True
        if ro.side in ("buy", "bid", "long"):
            return float(q.ask) <= float(ro.limit_price) + 1e-12
        return float(q.bid) >= float(ro.limit_price) - 1e-12

    def _limit_bounded_cross_price(self, ro: _RestingOrder, q: RecordedQuote) -> float:
        """Return a modeled fill that can never violate the order's limit.

        The quote-side crossing test establishes marketability. Adverse slippage can move the
        modeled execution toward the limit, but a BUY can never execute above its limit and a
        SELL can never execute below it. This is an exchange-order invariant, not optimistic
        price improvement.
        """

        price = self._cross_price(ro.side, q)
        if ro.limit_price is None:
            return price
        if ro.side in ("buy", "bid", "long"):
            return min(price, float(ro.limit_price))
        return max(price, float(ro.limit_price))

    def _volume_cap_available(self, ro: _RestingOrder) -> Optional[float]:
        """STEP-2 (b): the additional qty this order may fill given the printed volume it has
        observed at-or-through its limit. Returns ``None`` when volume-capping is not active
        for this order (⇒ no cap, P0/P1 behavior). When active, returns
        ``max(0, frac × observed_printed_volume − already_filled)`` — a partial results when
        the tape was thin, and 0 (no fill) when NO volume printed through the limit yet
        (the no-fill-through-empty-tape property)."""
        if not self._volume_cap_enabled:
            return None
        allowed_total = ro.volume_participation_frac * float(ro.observed_printed_volume)
        return max(0.0, allowed_total - float(ro.filled_size))

    def _maybe_cross(self, ro: _RestingOrder, q: Optional[RecordedQuote]) -> None:
        """Advance one resting order against the current quote: respect the ack delay, then
        fill (or partial-fill) when the limit crosses, capped by the observed printed volume.
        Idempotent on terminal orders."""
        if ro.status != "open" or q is None or not q.is_valid():
            return
        if self._exact_print_fills_enabled:
            # Quote changes establish marketability but cannot spend liquidity.
            # Only one causally released exact print may allocate its own shared
            # participation budget through ``release_verified_exact_print``.
            return
        if ro.ack_delay_remaining > 0:
            ro.ack_delay_remaining -= 1
            return
        if not self._limit_crosses(ro, q):
            return
        remaining = ro.base_size - ro.filled_size
        if remaining <= 0:
            ro.status = "filled"
            return
        # STEP-2 (b) VOLUME CAP: never fill more than frac × printed-volume-through-limit.
        cap = self._volume_cap_available(ro)
        if cap is not None:
            fillable = min(remaining, cap)
            if fillable <= 1e-9:
                # No (more) printed volume available through the limit yet ⇒ stay open,
                # accrue on later advances. This is the no-fill-through-empty-tape guarantee.
                return
            px = self._limit_bounded_cross_price(ro, q)
            self._book_fill(ro, qty=fillable, price=px)
            # terminal only once the FULL size is filled; else keep resting for more volume
            ro.status = "filled" if (ro.base_size - ro.filled_size) <= 1e-9 else "open"
            return
        # PARTIAL: the first cross fills half, leaving the order ``open`` for the next cross.
        if ro.partial_first_fill and ro.filled_size <= 0 and remaining > 1e-9:
            half = remaining / 2.0
            px = self._limit_bounded_cross_price(ro, q)
            self._book_fill(ro, qty=half, price=px)
            ro.status = "open"  # stays resting for the remainder
            return
        px = self._limit_bounded_cross_price(ro, q)
        self._book_fill(ro, qty=remaining, price=px)
        ro.status = "filled"

    def _advance_resting_orders(self, *, product_id: Optional[str] = None) -> None:
        """Re-test every still-``open`` resting order against its product's current quote."""
        for ro in self._orders.values():
            if ro.status != "open":
                continue
            if product_id is not None and ro.product_id != product_id:
                continue
            self._maybe_cross(ro, self._quote_for(ro.product_id))

    def _book_fill(
        self,
        ro: _RestingOrder,
        *,
        qty: float,
        price: float,
        fill_id: Optional[str] = None,
        trade_time: Optional[datetime] = None,
        raw: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Record a (possibly partial) fill on ``ro`` + append a NormalizedFill. Updates the
        size-weighted average fill price + accrues the proportional fee."""
        if qty <= 0:
            return
        prev_filled = ro.filled_size
        prev_px = ro.fill_price if ro.fill_price is not None else price
        new_filled = prev_filled + qty
        # size-weighted average across partials
        ro.fill_price = (prev_px * prev_filled + price * qty) / new_filled if new_filled > 0 else price
        ro.filled_size = new_filled
        notional = abs(price * qty)
        fill_fee = modeled_fill_leg_fee_usd(
            notional, self._fee_to_target_ratio, venue_rt_bps=self._venue_rt_bps
        )
        ro.fee += fill_fee
        self._fills.append(
            NormalizedFill(
                fill_id=(
                    fill_id
                    or f"{ro.order_id}-f{len([f for f in self._fills if f.order_id == ro.order_id]) + 1}"
                ),
                order_id=ro.order_id,
                product_id=ro.product_id,
                side=ro.side,
                size=float(qty),
                price=float(price),
                fee=float(fill_fee),
                trade_time=(
                    trade_time.astimezone(timezone.utc).isoformat()
                    if trade_time is not None and trade_time.tzinfo is not None
                    else self._clock.replace(tzinfo=timezone.utc).isoformat()
                ),
                raw=dict(raw or {"venue": _VENUE}),
            )
        )


def make_mock_broker_factory(adapter: MockBrokerAdapter):
    """Return an ``adapter_factory`` callable (the shape ``tick_live_session`` accepts) that
    yields the *same* singleton mock so the driver can inject quotes/clock across ticks.

    NOTE: not wired into the live runner in P0 — provided for P1 to pass as
    ``adapter_factory=make_mock_broker_factory(mock)``."""

    def _factory() -> MockBrokerAdapter:
        return adapter

    return _factory
