"""Pure, bounded research facts from complete recorded-known print frontiers.

No stop, D input, provider-completeness claim, I/O or runtime adapter lives here.
The caller supplies frontier membership; hashing verifies those supplied rows,
not physical database visibility. A frontier is atomic even across feed chunks.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import math


VERSION = "completed_local_swing_facts_v1"
MICRO = "micro_upturn"
TOUCH = "local_peak_reclaim_ge"
STRICT = "local_peak_reclaim_gt"


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _positive_int(value):
    return type(value) is int and value > 0


@dataclass(frozen=True)
class Print:
    id: int
    price: float
    size: float
    event_us: int
    received_us: int
    published_us: int
    epoch: tuple

    def __post_init__(self):
        if not _positive_int(self.id):
            raise ValueError("invalid_print_id")
        if any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0
               for x in (self.price, self.size)):
            raise ValueError("invalid_price_or_size")
        if any(type(x) is not int for x in (self.event_us, self.received_us, self.published_us)):
            raise ValueError("invalid_clock")
        if self.published_us < self.received_us:
            raise ValueError("publication_precedes_receipt")
        if not isinstance(self.epoch, tuple) or not self.epoch or any(
            type(x) not in (str, int, type(None)) for x in self.epoch
        ):
            raise ValueError("invalid_epoch")

    @property
    def cursor(self):
        return self.event_us, self.id

    @property
    def known_us(self):
        return max(self.event_us, self.received_us, self.published_us)


@dataclass(frozen=True)
class Plateau:
    first: Print
    last: Print
    count: int
    max_gap_us: int = 0

    def __post_init__(self):
        if (not isinstance(self.first, Print) or not isinstance(self.last, Print)
                or not _positive_int(self.count) or self.first.price != self.last.price
                or self.first.epoch != self.last.epoch or self.first.known_us > self.last.known_us
                or self.first.cursor > self.last.cursor or type(self.max_gap_us) is not int
                or self.max_gap_us < 0):
            raise ValueError("invalid_plateau")


@dataclass(frozen=True)
class Candidate:
    stream_key: str
    segment_key: str
    peak: Plateau
    low: Plateau
    micro_confirmation: Print
    peak_has_prior_rise: bool
    path_max_gap_us: int

    def __post_init__(self):
        if (not isinstance(self.stream_key, str) or not self.stream_key
                or not isinstance(self.segment_key, str) or not self.segment_key
                or not isinstance(self.peak, Plateau) or not isinstance(self.low, Plateau)
                or not isinstance(self.micro_confirmation, Print)
                or not self.peak.first.price > self.low.first.price
                or not self.micro_confirmation.price > self.low.first.price
                or not self.peak.last.cursor < self.low.first.cursor <= self.low.last.cursor
                < self.micro_confirmation.cursor or type(self.peak_has_prior_rise) is not bool
                or not self.peak.first.epoch == self.low.first.epoch == self.micro_confirmation.epoch
                or not self.peak.last.known_us <= self.low.first.known_us
                <= self.low.last.known_us <= self.micro_confirmation.known_us
                or type(self.path_max_gap_us) is not int or self.path_max_gap_us < 0):
            raise ValueError("invalid_fixed_candidate")

    @property
    def candidate_id(self):
        # As in the pinned ledger; identity is also scoped by segment_key.
        return f"{self.stream_key}:low:{self.low.first.id}"


@dataclass(frozen=True)
class Pending:
    candidate: Candidate
    variants: tuple[str, ...]

    def __post_init__(self):
        if (not isinstance(self.candidate, Candidate) or type(self.variants) is not tuple
                or not self.variants or len(set(self.variants)) != len(self.variants)
                or any(v not in (TOUCH, STRICT) for v in self.variants)
                or not self.candidate.peak_has_prior_rise):
            raise ValueError("invalid_pending_candidate")


@dataclass(frozen=True)
class Receipt:
    stream_key: str
    segment_key: str
    known_us: int
    row_count: int
    last_cursor: tuple[int, int]
    rows_sha256: str
    previous_state_sha256: str
    contract: str = "recorded_known_frontier_v1"

    def __post_init__(self):
        if (self.contract != "recorded_known_frontier_v1"
                or not isinstance(self.stream_key, str) or not self.stream_key
                or not isinstance(self.segment_key, str) or not self.segment_key or type(self.known_us) is not int
                or not _positive_int(self.row_count) or type(self.last_cursor) is not tuple or len(self.last_cursor) != 2
                or any(type(x) is not int for x in self.last_cursor)
                or any(type(x) is not str or len(x) != 64 or any(c not in "0123456789abcdef" for c in x)
                       for x in (self.rows_sha256, self.previous_state_sha256))):
            raise ValueError("invalid_frontier_receipt")


@dataclass(frozen=True)
class State:
    stream_key: str
    segment_key: str
    plateau: Plateau | None = None
    direction: int = 0
    decline_peak: Plateau | None = None
    peak_has_prior_rise: bool = False
    path_max_gap_us: int = 0
    pending: tuple[Pending, ...] = ()
    last_receipt: Receipt | None = None
    version: str = VERSION

    def __post_init__(self):
        if (self.version != VERSION or not isinstance(self.stream_key, str) or not self.stream_key
                or not isinstance(self.segment_key, str) or not self.segment_key
                or type(self.direction) is not int or self.direction not in (-1, 0, 1)
                or type(self.peak_has_prior_rise) is not bool
                or type(self.path_max_gap_us) is not int or self.path_max_gap_us < 0
                or type(self.pending) is not tuple or any(not isinstance(p, Pending) for p in self.pending)
                or (self.plateau is not None and not isinstance(self.plateau, Plateau))
                or (self.decline_peak is not None and not isinstance(self.decline_peak, Plateau))
                or (self.last_receipt is not None and not isinstance(self.last_receipt, Receipt))
                or (self.direction == -1 and self.decline_peak is None)):
            raise ValueError("invalid_state")
        if any((p.candidate.stream_key, p.candidate.segment_key)
               != (self.stream_key, self.segment_key) for p in self.pending):
            raise ValueError("pending_identity_mismatch")
        if len({p.candidate.candidate_id for p in self.pending}) != len(self.pending):
            raise ValueError("duplicate_pending_identity")
        if bool(self.plateau) != bool(self.last_receipt) or (
            self.plateau is None and (self.pending or self.direction or self.decline_peak)
        ):
            raise ValueError("state_history_missing")
        if self.direction != -1 and (self.decline_peak or self.peak_has_prior_rise or self.path_max_gap_us):
            raise ValueError("state_direction_mismatch")
        if self.direction == -1 and not (
            self.decline_peak.last.cursor < self.plateau.first.cursor
            and self.decline_peak.first.price > self.plateau.first.price
            and self.decline_peak.first.epoch == self.plateau.first.epoch
            and self.decline_peak.last.known_us <= self.plateau.first.known_us
        ):
            raise ValueError("invalid_decline_state")
        if self.plateau and any(
            p.candidate.micro_confirmation.cursor > self.plateau.last.cursor
            or p.candidate.micro_confirmation.known_us > self.plateau.last.known_us
            or p.candidate.micro_confirmation.epoch != self.plateau.last.epoch for p in self.pending
        ):
            raise ValueError("pending_history_mismatch")
        if self.last_receipt and (
            (self.last_receipt.stream_key, self.last_receipt.segment_key)
            != (self.stream_key, self.segment_key) or self.plateau is None
            or self.last_receipt.last_cursor != self.plateau.last.cursor
            or self.last_receipt.known_us != self.plateau.last.known_us
        ):
            raise ValueError("state_cursor_mismatch")


@dataclass(frozen=True)
class Capacities:
    """Caller resource bounds, not market thresholds or a process RSS promise.

    state_bytes limits the serialized checkpoint and an individual input row;
    transient facts/candidates are separately bounded by their object counts.
    """
    pending_candidates: int
    frontier_facts: int
    frontier_prints: int
    state_bytes: int

    def __post_init__(self):
        if any(not _positive_int(x) for x in asdict(self).values()):
            raise ValueError("invalid_resource_capacity")


@dataclass(frozen=True)
class Fact:
    candidate: Candidate
    variant: str
    status: str
    frontier_known_us: int
    frontier_rows_sha256: str
    confirmation: Print | None = None
    invalidating_print: Print | None = None
    first_break_in_frontier: Print | None = None


@dataclass(frozen=True)
class Reduction:
    status: str
    state: State
    facts: tuple[Fact, ...] = ()
    reason: str | None = None


def state_sha256(state: State) -> str:
    return hashlib.sha256(_json(asdict(state))).hexdigest()


def dump_state(state: State) -> str:
    return _json({"payload": asdict(state), "sha256": state_sha256(state)}).decode()


def restore_state(serialized: str) -> State:
    """Versioned restart with exact shape and corruption detection, not auth."""
    envelope = json.loads(serialized)
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "sha256"}:
        raise ValueError("invalid_state_envelope")
    payload = envelope["payload"]
    if hashlib.sha256(_json(payload)).hexdigest() != envelope["sha256"]:
        raise ValueError("state_digest_mismatch")

    def shape(cls, value):
        if not isinstance(value, dict) or set(value) != {f.name for f in fields(cls)}:
            raise ValueError("state_shape_mismatch")
        return value

    shape(State, payload)

    def array(value):
        # JSON tuples serialize as arrays. Strings/dict keys are not witnesses.
        if type(value) is not list:
            raise ValueError("state_array_shape_mismatch")
        return value

    def row(value):
        shape(Print, value)
        return Print(**{**value, "epoch": tuple(array(value["epoch"]))})

    def plateau(value):
        if value is not None:
            shape(Plateau, value)
        return None if value is None else Plateau(
            **{**value, "first": row(value["first"]), "last": row(value["last"])},
        )

    def candidate(value):
        shape(Candidate, value)
        return Candidate(**{**value, "peak": plateau(value["peak"]), "low": plateau(value["low"]),
                            "micro_confirmation": row(value["micro_confirmation"])})

    receipt = payload["last_receipt"]
    if receipt is not None:
        shape(Receipt, receipt)
        receipt = Receipt(**{**receipt, "last_cursor": tuple(array(receipt["last_cursor"]))})
    for pending in array(payload["pending"]):
        shape(Pending, pending)
    return State(**{**payload, "plateau": plateau(payload["plateau"]),
                   "decline_peak": plateau(payload["decline_peak"]), "last_receipt": receipt,
                   "pending": tuple(Pending(candidate(x["candidate"]), tuple(array(x["variants"])))
                                    for x in payload["pending"])})


def rows_sha256(rows) -> str:
    """Hash supplied membership; it does not prove that a caller fetched all rows."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_json(asdict(row)) + b"\n")
    return digest.hexdigest()


class Frontier:
    """Private working copy; only finish() can return committed facts/state.

    Dropping this object is an interrupted frontier: the supplied frozen State
    is unchanged. A failed feed poisons this working copy until it is discarded.
    """

    def __init__(self, state: State, receipt: Receipt, capacities: Capacities):
        self.original, self.receipt, self.capacities = state, receipt, capacities
        self.reason = None
        self.count, self.digest = 0, hashlib.sha256()
        self.rows_last = None
        self._facts: list[Fact] = []
        self._pending = list(state.pending)
        self.plateau, self.direction = state.plateau, state.direction
        self.peak, self.peak_has_rise = state.decline_peak, state.peak_has_prior_rise
        self.path_max_gap = state.path_max_gap_us
        self.duplicate = receipt == state.last_receipt
        if (receipt.stream_key, receipt.segment_key) != (state.stream_key, state.segment_key):
            self.reason = "frontier_identity_mismatch"
        elif not self.duplicate and receipt.previous_state_sha256 != state_sha256(state):
            self.reason = "previous_state_mismatch"
        elif not self.duplicate and state.last_receipt and receipt.known_us <= state.last_receipt.known_us:
            self.reason = "stale_or_conflicting_frontier"
        elif (receipt.row_count > capacities.frontier_prints
              or len(state.pending) > capacities.pending_candidates
              or len(dump_state(state).encode()) > capacities.state_bytes):
            self.reason = "resource_capacity_unresolved"

    def _fact(self, candidate, variant, status, *, confirmation=None, invalidating=None):
        if len(self._facts) >= self.capacities.frontier_facts:
            raise ValueError("resource_capacity_unresolved")
        self._facts.append(Fact(candidate, variant, status, self.receipt.known_us,
                                self.receipt.rows_sha256, confirmation, invalidating))

    def _resolve(self, candidate, variants, row):
        remaining = []
        for variant in variants:
            if row.price < candidate.low.first.price:
                self._fact(candidate, variant, "undercut_before_reclaim", invalidating=row)
            elif row.price > candidate.peak.first.price or (
                variant == TOUCH and row.price == candidate.peak.first.price
            ):
                self._fact(candidate, variant, "confirmed", confirmation=row)
            else:
                remaining.append(variant)
        return Pending(candidate, tuple(remaining)) if remaining else None

    def _step(self, row):
        # Previously staged confirmations must survive the WHOLE frontier,
        # including a break followed by recovery. A last-price check is not enough.
        for i, fact in enumerate(self._facts):
            if (fact.confirmation and not fact.first_break_in_frontier
                    and row.price < fact.candidate.low.first.price):
                self._facts[i] = replace(fact, first_break_in_frontier=row)
        remaining = []
        for pending in self._pending:
            result = self._resolve(pending.candidate, pending.variants, row)
            if result:
                remaining.append(result)
        self._pending = remaining
        prior = self.plateau
        if prior is None:
            self.plateau = Plateau(row, row, 1)
            return
        gap = row.event_us - prior.last.event_us
        if row.price == prior.last.price:
            self.plateau = Plateau(prior.first, row, prior.count + 1, max(prior.max_gap_us, gap))
            if self.direction == -1:
                self.path_max_gap = max(self.path_max_gap, gap)
            return
        if row.price < prior.last.price:
            if self.direction != -1:
                self.peak, self.peak_has_rise = prior, self.direction == 1
                self.path_max_gap = prior.max_gap_us
            self.path_max_gap = max(self.path_max_gap, gap)
            self.direction = -1
        else:
            if self.direction == -1:
                candidate = Candidate(self.original.stream_key, self.original.segment_key,
                                      self.peak, prior, row, self.peak_has_rise,
                                      max(self.path_max_gap, gap))
                self._fact(candidate, MICRO, "confirmed", confirmation=row)
                if not self.peak_has_rise:
                    for variant in (TOUCH, STRICT):
                        self._fact(candidate, variant, "unknown_peak_at_boundary")
                else:
                    pending = self._resolve(candidate, (TOUCH, STRICT), row)
                    if pending:
                        if len(self._pending) >= self.capacities.pending_candidates:
                            raise ValueError("resource_capacity_unresolved")
                        self._pending.append(pending)
            self.direction = 1
            self.peak, self.peak_has_rise, self.path_max_gap = None, False, 0
        self.plateau = Plateau(row, row, 1)

    def feed(self, rows) -> None:
        if self.reason:
            return
        try:
            for row in rows:
                if not isinstance(row, Print):
                    raise ValueError("invalid_print_type")
                if row.known_us != self.receipt.known_us:
                    raise ValueError("row_outside_recorded_frontier")
                previous = self.rows_last if self.duplicate else (
                    self.plateau.last if self.plateau else None
                )
                if previous and row.cursor <= previous.cursor:
                    raise ValueError("late_or_duplicate_print")
                if previous and row.epoch != previous.epoch:
                    raise ValueError("epoch_change_requires_new_segment")
                self.count += 1
                if self.count > self.receipt.row_count:
                    raise ValueError("frontier_membership_mismatch")
                encoded = _json(asdict(row)) + b"\n"
                if len(encoded) > self.capacities.state_bytes:
                    raise ValueError("resource_capacity_unresolved")
                self.digest.update(encoded)
                self.rows_last = row
                if not self.duplicate:
                    self._step(row)
        except (ValueError, TypeError, OverflowError) as exc:
            self.reason = str(exc)

    def finish(self) -> Reduction:
        if self.reason:
            return Reduction("unresolved", self.original, reason=self.reason)
        if (self.count != self.receipt.row_count or self.rows_last is None
                or self.rows_last.cursor != self.receipt.last_cursor
                or self.digest.hexdigest() != self.receipt.rows_sha256):
            return Reduction("unresolved", self.original, reason="frontier_membership_mismatch")
        if self.duplicate:
            return Reduction("already_applied", self.original)
        state = State(self.original.stream_key, self.original.segment_key,
                      self.plateau, self.direction, self.peak, self.peak_has_rise,
                      self.path_max_gap, tuple(self._pending), self.receipt)
        if len(dump_state(state).encode()) > self.capacities.state_bytes:
            return Reduction("unresolved", self.original, reason="resource_capacity_unresolved")
        facts = tuple(replace(f, status=("confirmed_broken_by_frontier" if f.first_break_in_frontier
                                        else "confirmed_intact_at_frontier"))
                      if f.status == "confirmed" else f for f in self._facts)
        return Reduction("applied", state, facts)


def reduce_frontier(state: State, receipt: Receipt, rows, capacities: Capacities) -> Reduction:
    working = Frontier(state, receipt, capacities)
    working.feed(rows)
    return working.finish()
