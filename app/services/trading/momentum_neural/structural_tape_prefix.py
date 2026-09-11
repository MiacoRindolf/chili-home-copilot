"""Incremental research evidence over caller-supplied source frontiers.

No strategy window, selected parent, trading decision, SQL reader, or runtime
caller. Classification persists across frontiers; structural views never reset
it. Frontier membership is supplied, not proved complete by a digest. Quotes
use the existing float quote/tick classifier and do not assert aggressor truth
or independently fresh quotes. Resource limits reject the whole frontier.
Rational mass sums are exact over the supplied numeric representations.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib
import json
import math


CONTRACT = "structural_tape_prefix_research_v1"
CONSUMER_CONTRACT = "structural_tape_consumer_prefix_research_v1"
MASS_FIELDS = ("volume", "inferred_buy", "inferred_sell", "unknown",
               "quote_buy", "quote_sell", "fallback_buy", "fallback_sell")
ZERO = (Fraction(0),) * len(MASS_FIELDS)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _integer(value):
    return type(value) is int and value > 0


def _finite(value):
    try:
        return type(value) in (float, int) and math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class Tick:
    id: int
    price: float
    size: float
    bid: float | None
    ask: float | None
    event_ns: int
    received_ns: int
    published_ns: int
    epoch: tuple
    frame_sequence: int | None = None
    frame_sha256: str | None = None

    def __post_init__(self):
        if not _integer(self.id) or not all(_finite(v) and v > 0 for v in (self.price, self.size)):
            raise ValueError("invalid_trade")
        if any(v is not None and not _finite(v) for v in (self.bid, self.ask)):
            raise ValueError("invalid_quote_number")
        if any(type(v) is not int for v in (self.event_ns, self.received_ns, self.published_ns)):
            raise ValueError("invalid_clock")
        if self.published_ns < self.received_ns:
            raise ValueError("publication_before_receipt")
        if type(self.epoch) is not tuple or not self.epoch or any(type(v) not in (str, int, type(None)) for v in self.epoch):
            raise ValueError("invalid_epoch")
        if self.frame_sequence is not None and (type(self.frame_sequence) is not int or self.frame_sequence < 0):
            raise ValueError("invalid_frame_sequence")
        if self.frame_sha256 is not None and (type(self.frame_sha256) is not str or len(self.frame_sha256) != 64
                or any(c not in "0123456789abcdef" for c in self.frame_sha256)):
            raise ValueError("invalid_frame_digest")

    @property
    def cursor(self):
        return self.event_ns, self.id

    @property
    def known_ns(self):
        return max(self.event_ns, self.received_ns, self.published_ns)


@dataclass(frozen=True)
class Limits:
    """Storage/work capacities only; reaching them never changes membership."""
    retained_ticks: int
    frontier_ticks: int
    active_references: int

    def __post_init__(self):
        if any(not _integer(v) for v in asdict(self).values()):
            raise ValueError("invalid_resource_limit")


@dataclass(frozen=True)
class FrontierReceipt:
    known_ns: int
    row_count: int
    rows_sha256: str
    previous_prefix_sha256: str

    def __post_init__(self):
        if type(self.known_ns) is not int or not _integer(self.row_count):
            raise ValueError("invalid_frontier_receipt")
        for value in (self.rows_sha256, self.previous_prefix_sha256):
            if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("invalid_frontier_digest")


@dataclass(frozen=True)
class ConsumerFrontierReceipt:
    """Caller-supplied capture-prefix delta, distinct from a clock frontier.

    Tick.id is the global captured event sequence in this contract. Rows must
    exhaust this symbol's eligible prints in (previous_source_sequence,
    source_sequence]; other streams/control events may explain sequence gaps.
    The first predecessor is an explicit segment anchor, not an empty-history
    claim. Hashes bind supplied evidence; the reducer cannot authenticate the
    capture source or independently prove delta completeness.
    """
    known_ns: int
    row_count: int
    rows_sha256: str
    previous_prefix_sha256: str
    source_identity_sha256: str
    source_sequence: int
    source_root_sha256: str
    previous_source_sequence: int
    previous_source_root_sha256: str

    def __post_init__(self):
        if (type(self.known_ns) is not int or type(self.row_count) is not int or self.row_count < 0
                or not _integer(self.source_sequence) or type(self.previous_source_sequence) is not int
                or not 0 <= self.previous_source_sequence < self.source_sequence):
            raise ValueError("invalid_consumer_receipt")
        for value in (self.rows_sha256, self.previous_prefix_sha256, self.source_identity_sha256,
                      self.source_root_sha256, self.previous_source_root_sha256):
            if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("invalid_consumer_digest")
        if self.source_root_sha256 == self.previous_source_root_sha256:
            raise ValueError("unchanged_source_root")


@dataclass(frozen=True)
class Reference:
    stream_key: str
    segment_key: str
    epoch: tuple
    kind: str
    origin_index: int
    confirmation_index: int
    origin_id: int
    confirmation_id: int
    plateau_first_index: int
    plateau_first_id: int


@dataclass(frozen=True)
class Result:
    """Birth/breach events; a birth can be breached in the same frontier.

    active_references(), after commit, is the authority for surviving references.
    """
    status: str
    prefix_sha256: str
    born: tuple[Reference, ...] = ()
    breached: tuple[Reference, ...] = ()
    reason: str | None = None


def rows_sha256(rows):
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_json(asdict(row)) + b"\n")
    return digest.hexdigest()


def classify(row, previous_price, previous_sign):
    """Existing quote/tick semantics with explicit persistent prefix carry."""
    p, b, a = float(row.price), row.bid, row.ask
    quote_sign = 0
    if b is not None and a is not None and float(a) > float(b) > 0:
        midpoint = (float(a) + float(b)) / 2
        if p >= float(a): quote_sign = 1
        elif p <= float(b): quote_sign = -1
        elif p > midpoint: quote_sign = 1
        elif p < midpoint: quote_sign = -1
    side = quote_sign
    if not side:
        side = (1 if p > previous_price else -1) if previous_price is not None and p != previous_price else previous_sign
    mass = Fraction(str(row.size))
    values = (mass, mass if side > 0 else 0, mass if side < 0 else 0, mass if not side else 0,
              mass if quote_sign > 0 else 0, mass if quote_sign < 0 else 0,
              mass if not quote_sign and side > 0 else 0, mass if not quote_sign and side < 0 else 0)
    return side, quote_sign, tuple(Fraction(v) for v in values)


def _merge(a, b):
    if a is None: return b
    if b is None: return a
    low = a[:4] if a[0] < b[0] else b[:4] if b[0] < a[0] else (a[0], min(a[1], b[1]), max(a[2], b[2]), a[3]+b[3])
    high = a[4:] if a[4] > b[4] else b[4:] if b[4] > a[4] else (a[4], min(a[5], b[5]), max(a[6], b[6]), a[7]+b[7])
    return low + high


class Prefix:
    """Single-owner incremental state. Only a fully validated frontier commits.

    Tree changes, stack changes, labels, and cumulative mass are staged privately.
    Readers see committed source rows only. No reference is pruned by age/count.
    A new instance explicitly starts a new segment and unknown classification.
    Resource allocation is bounded by Limits; this is not a production RSS claim.
    """
    def __init__(self, stream_key: str, segment_key: str, limits: Limits):
        if not stream_key or not segment_key or type(stream_key) is not str or type(segment_key) is not str:
            raise ValueError("invalid_stream_identity")
        if not isinstance(limits, Limits):
            raise ValueError("invalid_resource_limits")
        self.stream_key, self.segment_key, self.limits = stream_key, segment_key, limits
        self._ticks, self._labels, self._ids = [], [], set()
        self._mass = [ZERO]
        self._stacks = {"valley": [], "peak": []}
        self._active = set()
        self._direction, self._carry = 0, 0
        self._plateau_first = 0
        self._last_receipt = None
        self._digest = hashlib.sha256(_json([CONTRACT, stream_key, segment_key])+b"\n")
        self._width = 1 << (limits.retained_ticks-1).bit_length()
        self._tree = [None] * (2*self._width)

    @property
    def count(self):
        return len(self._ticks)

    @property
    def prefix_sha256(self):
        return self._digest.hexdigest()

    @property
    def last_receipt(self):
        """Immutable committed boundary; None until the first successful delta."""
        return self._last_receipt

    def tick(self, index):
        if type(index) is not int or not 0 <= index < self.count:
            raise ValueError("index_outside_committed_prefix")
        return self._ticks[index]

    def label(self, index):
        self.tick(index)
        return self._labels[index]

    def active_references(self):
        return tuple(sorted(self._stacks["valley"]+self._stacks["peak"], key=lambda r:r.confirmation_index))

    def append_frontier(self, rows, receipt: FrontierReceipt | ConsumerFrontierReceipt):
        # A materialized frontier is explicit; an unbounded producer/generator is
        # not consumed before a capacity check. The caller handles fetch chunks.
        if type(rows) not in (tuple, list) or type(receipt) not in (FrontierReceipt, ConsumerFrontierReceipt):
            return Result("unresolved", self.prefix_sha256, reason="invalid_frontier_input")
        fail = lambda why: Result("unresolved", self.prefix_sha256, reason=why)
        if len(rows) > self.limits.frontier_ticks:
            return fail("resource_capacity_unresolved")
        if len(rows) != receipt.row_count or any(not isinstance(r, Tick) for r in rows):
            return fail("frontier_membership_mismatch")
        # Even idempotent delivery must present matching actual rows, not just
        # repeat a trusted receipt next to a different batch.
        if rows_sha256(rows) != receipt.rows_sha256:
            return fail("frontier_membership_mismatch")
        if receipt == self._last_receipt:
            return Result("already_applied", self.prefix_sha256)
        if receipt.previous_prefix_sha256 != self.prefix_sha256:
            return fail("previous_prefix_mismatch")
        consumer = type(receipt) is ConsumerFrontierReceipt
        prior = self._last_receipt
        if prior:
            if type(receipt) is not type(prior):
                return fail("frontier_contract_change_requires_new_segment")
            if receipt.known_ns < prior.known_ns or (not consumer and receipt.known_ns == prior.known_ns):
                return fail("late_or_conflicting_frontier")
            if consumer:
                if receipt.source_identity_sha256 != prior.source_identity_sha256:
                    return fail("source_identity_change_requires_new_segment")
                if (receipt.previous_source_sequence != prior.source_sequence
                        or receipt.previous_source_root_sha256 != prior.source_root_sha256):
                    return fail("previous_source_prefix_mismatch")
        if self.count+len(rows) > self.limits.retained_ticks:
            return fail("resource_capacity_unresolved")
        last = self._ticks[-1] if self.count else None
        seen = set()
        source_cursor = receipt.previous_source_sequence if consumer else None
        for row in rows:
            if row.known_ns > receipt.known_ns or (not consumer and row.known_ns != receipt.known_ns):
                return fail("row_outside_frontier")
            if consumer:
                if not source_cursor < row.id <= receipt.source_sequence:
                    return fail("row_outside_source_delta")
                source_cursor = row.id
            if row.id in self._ids or row.id in seen or (last and row.cursor <= last.cursor):
                return fail("late_or_duplicate_tick")
            if last and row.epoch != last.epoch:
                return fail("epoch_change_requires_new_segment")
            seen.add(row.id)
            last = row
        # Staging: no committed arrays, stacks, tree nodes, or digest are mutated.
        start = self.count
        at = lambda i: self._ticks[i] if i < start else rows[i-start]
        stacks = {kind:list(refs) for kind,refs in self._stacks.items()}
        direction, carry = self._direction, self._carry
        plateau_first = self._plateau_first
        born, breached, added_mass, labels = [], [], [], []
        cumulative = self._mass[-1]
        overlay = {}
        tree_at = lambda i: overlay[i] if i in overlay else self._tree[i]
        digest = self._digest.copy()
        if consumer:
            # Even a delta without this symbol's prints advances source proof.
            # Keep the recorded-frontier digest contract byte-for-byte intact.
            digest.update(_json([CONSUMER_CONTRACT, asdict(receipt)])+b"\n")
        for offset,row in enumerate(rows):
            i = start+offset
            previous = at(i-1) if i else None
            side,quote_sign,mass = classify(row, None if previous is None else float(previous.price), carry)
            if side: carry = side
            labels.append((side,quote_sign))
            cumulative = tuple(a+b for a,b in zip(cumulative,mass))
            added_mass.append(cumulative)
            for kind,stack in stacks.items():
                while stack:
                    level = at(stack[-1].origin_index).price
                    if not (row.price < level if kind == "valley" else row.price > level):
                        break
                    breached.append(stack.pop())
            if previous:
                step = (row.price > previous.price) - (row.price < previous.price)
                if step and step != direction:
                    if direction:
                        kind = "valley" if direction < 0 else "peak"
                        ref = Reference(self.stream_key,self.segment_key,row.epoch,kind,i-1,i,previous.id,row.id,
                                        plateau_first,at(plateau_first).id)
                        stacks[kind].append(ref)
                        born.append(ref)
                        if len(stacks["valley"])+len(stacks["peak"]) > self.limits.active_references:
                            return fail("resource_capacity_unresolved")
                    direction = step
                if step:
                    plateau_first = i
            node = self._width+i
            overlay[node] = (row.price,i,i,1,row.price,i,i,1)
            while node > 1:
                node //= 2
                overlay[node] = _merge(tree_at(2*node),tree_at(2*node+1))
            digest.update(_json(asdict(row))+b"\n")
        active = set(stacks["valley"]+stacks["peak"])
        # Commit after all domain validation/resource checks and arithmetic.
        self._ticks.extend(rows)
        self._labels.extend(labels)
        self._mass.extend(added_mass)
        self._ids.update(seen)
        for node,value in overlay.items():
            self._tree[node] = value
        self._stacks, self._direction, self._carry = stacks, direction, carry
        self._active, self._plateau_first = active, plateau_first
        self._digest, self._last_receipt = digest, receipt
        return Result("applied", self.prefix_sha256, tuple(born),tuple(breached))

    def mass(self, origin, end=None):
        """Mass over (origin,end]; origin belongs to geometry, not volume."""
        end = self.count-1 if end is None else end
        self.tick(origin); self.tick(end)
        if end < origin:
            raise ValueError("reversed_range")
        return tuple(a-b for a,b in zip(self._mass[end+1],self._mass[origin+1]))

    def extrema(self, origin, end=None):
        end = self.count-1 if end is None else end
        self.tick(origin); self.tick(end)
        if end < origin:
            raise ValueError("reversed_range")
        left,right = self._width+origin,self._width+end+1
        found = None
        while left < right:
            if left & 1:
                found = _merge(found,self._tree[left]); left += 1
            if right & 1:
                right -= 1; found = _merge(found,self._tree[right])
            left //= 2; right //= 2
        return found

    def context(self, reference):
        if not isinstance(reference, Reference) or reference not in self._active:
            raise ValueError("reference_not_active")
        origin,end = reference.origin_index,self.count-1
        node = self.extrema(origin,end)
        extreme = node[6] if reference.kind == "valley" else node[2]
        suffix = self.extrema(extreme,end)
        opposite = suffix[2] if reference.kind == "valley" else suffix[6]
        bounds = ((origin,extreme),(extreme,opposite),(opposite,end))
        return {"reference":reference,"end_index":end,"latest_directed_extreme_index":extreme,
                "latest_opposite_extreme_index":opposite,"whole_mass":self.mass(origin,end),
                "phase_bounds":bounds,"phase_mass":tuple(self.mass(a,b) for a,b in bounds),
                "prefix_sha256":self.prefix_sha256,
                "meaning":"observed paths may contain child oscillations; no selected trading scope"}
