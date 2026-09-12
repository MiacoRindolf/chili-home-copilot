"""Pure incremental candidate local/parent geometry from print evidence.

Quote-resolved local turns and recursive like-kind raw extrema are explicit
modeling conventions, not proof of predictive edge or market causation. No
selected count, elapsed-time window, hierarchy order or weighted parent tie-break.
"""
from dataclasses import dataclass, replace
from fractions import Fraction


def number(value):
    return Fraction(str(value))


@dataclass(frozen=True)
class WaveTurn:
    basis: str
    kind: str
    order: int
    origin_index: int
    origin_id: int
    confirmation_index: int
    confirmation_id: int
    price: Fraction


@dataclass(frozen=True)
class WavePair:
    peak: WaveTurn
    valley: WaveTurn


@dataclass(frozen=True)
class WaveParent:
    root_index: int
    low: Fraction
    high: Fraction
    aliases: tuple[WavePair, ...]


@dataclass(frozen=True)
class WavePhase:
    side: str
    reason: str


@dataclass(frozen=True)
class WaveContext:
    end_index: int
    end_id: int
    local_peak: WaveTurn | None
    local_valley: WaveTurn | None
    local_phase: WavePhase
    local_path_low: Fraction | None
    local_path_high: Fraction | None
    parent_status: str
    minimal_parents: tuple[WaveParent, ...]
    parent_phase: WavePhase
    events: tuple[WaveTurn, ...]
    definition: str = 'quote_local_minimal_enclosing_raw_parent_candidate_v1'
    order_authority: bool = False


def phase(peak, valley, price):
    if peak is None or valley is None:
        return WavePhase('unknown', 'missing_confirmed_pair')
    if valley.price >= peak.price:
        return WavePhase('unknown', 'unordered_confirmed_bounds')
    if price > peak.price:
        return WavePhase('front', 'strict_peak_reclaim')
    if price < valley.price:
        return WavePhase('back', 'strict_valley_break')
    if price in (peak.price, valley.price):
        return WavePhase('unknown', 'level_touch')
    return (WavePhase('front', 'recovery_after_confirmed_valley')
            if valley.origin_index > peak.origin_index
            else WavePhase('back', 'pullback_after_confirmed_peak'))


def minimal_enclosing(*, root, low, high, candidates):
    qualified = [c for c in candidates if c.root_index < root and c.low <= low and c.high >= high
                 and (c.low < low or c.high > high)]
    def closer(a, b):
        return (a.root_index >= b.root_index and a.low >= b.low and a.high <= b.high
                and (a.root_index > b.root_index or a.low > b.low or a.high < b.high))
    return tuple(c for c in qualified if not any(closer(other, c) for other in qualified))


class WaveState:
    """Small staged state; source ticks and range queries belong to Prefix.

    Each hierarchy level retains the two latest distinct like-kind groups. A
    third group supplies the right witness needed to confirm the middle extremum;
    this is adjacency geometry, not a three-print strategy window. Equal-price
    groups retain their last representative. No full-history replay on each tick.
    """
    def __init__(self):
        self.direction, self.low, self.high = 0, 0, 0
        self.local = {}
        self.groups = {}
        self.latest = {}

    def clone(self):
        value = WaveState()
        value.direction, value.low, value.high = self.direction, self.low, self.high
        value.local, value.groups, value.latest = dict(self.local), dict(self.groups), dict(self.latest)
        return value

    def _raw(self, turn, events):
        events.append(turn)
        key = turn.order, turn.kind
        self.latest[key] = turn
        groups = self.groups.get(key, ())
        if groups and groups[-1].price == turn.price:
            self.groups[key] = (*groups[:-1], turn)
            return
        promoted = None
        if len(groups) == 2:
            left, center = groups
            qualifies = (center.price > left.price and center.price > turn.price if turn.kind == 'peak'
                         else center.price < left.price and center.price < turn.price)
            if qualifies:
                promoted = replace(center, order=center.order+1,
                    confirmation_index=turn.confirmation_index, confirmation_id=turn.confirmation_id)
        self.groups[key] = (*groups[-1:], turn)
        if promoted is not None:
            self._raw(promoted, events)

    def advance(self, index, *, at, extrema, raw_reference=None):
        row = at(index)
        events = []
        if raw_reference is not None:
            r = raw_reference
            self._raw(WaveTurn('raw_recursive', r.kind, 1, r.origin_index, r.origin_id,
                index, row.id, number(at(r.origin_index).price)), events)
        if not index:
            return tuple(events)
        price = number(row.price)
        def valid(t):
            return t.bid is not None and t.ask is not None and 0 < number(t.bid) < number(t.ask)
        def up(origin):
            t = at(origin)
            return price > number(t.price) and valid(t) and valid(row) and number(row.bid) > number(t.ask)
        def down(origin):
            t = at(origin)
            return price < number(t.price) and valid(t) and valid(row) and number(row.ask) < number(t.bid)
        if self.direction == 0:
            if price <= number(at(self.low).price): self.low = index
            if price >= number(at(self.high).price): self.high = index
            rise, fall = up(self.low), down(self.high)
            if rise == fall:
                return tuple(events)
            kind, origin = ('valley', self.low) if rise else ('peak', self.high)
            self.direction = 1 if rise else -1
        elif self.direction == 1:
            if price >= number(at(self.high).price): self.high = index
            if not down(self.high): return tuple(events)
            kind, origin, self.direction = 'peak', self.high, -1
        else:
            if price <= number(at(self.low).price): self.low = index
            if not up(self.low): return tuple(events)
            kind, origin, self.direction = 'valley', self.low, 1
        turn = WaveTurn('quote_resolved', kind, 1, origin, at(origin).id, index, row.id, number(at(origin).price))
        self.local[kind] = turn
        events.append(turn)
        # Preserve any more extreme print between origin and delayed confirmation.
        # Tree ties match the research conventions: last high, first low here.
        span = extrema(origin+1, index)
        if self.direction == 1: self.high = span[6]
        else: self.low = span[1]
        return tuple(events)

    def view(self, *, end, at, extrema, events):
        row = at(end)
        peak, valley = self.local.get('peak'), self.local.get('valley')
        local_phase = phase(peak, valley, number(row.price))
        low = high = None
        parents, status = (), 'local_unavailable'
        if peak is not None and valley is not None and valley.price < peak.price:
            root = min(peak.origin_index, valley.origin_index)
            span = extrema(root, end)
            low, high = number(span[0]), number(span[4])
            groups = {}
            for order in sorted({k[0] for k in self.latest}):
                p, v = self.latest.get((order, 'peak')), self.latest.get((order, 'valley'))
                if p is not None and v is not None and v.price < p.price:
                    groups.setdefault((p.origin_id, v.origin_id), []).append(WavePair(p, v))
            candidates = tuple(WaveParent(min(pairs[0].peak.origin_index, pairs[0].valley.origin_index),
                pairs[0].valley.price, pairs[0].peak.price, tuple(pairs)) for pairs in groups.values())
            parents = minimal_enclosing(root=root, low=low, high=high, candidates=candidates)
            status = 'unique' if len(parents) == 1 else 'ambiguous' if parents else 'unavailable'
        pair = parents[0].aliases[0] if status == 'unique' else None
        parent_phase = phase(pair.peak, pair.valley, number(row.price)) if pair else WavePhase('unknown', status)
        return WaveContext(end, row.id, peak, valley, local_phase, low, high, status,
                           parents, parent_phase, tuple(events))
