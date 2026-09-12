"""Print-bounded formation/follow-through evidence for shared wave references.

Quote-side bounds are conditional on the inside-quote midpoint signs being
correct. They are neither confidence intervals nor authenticated aggressor flow.
Fallback tick-rule labels remain visible but cannot narrow those quote bounds.
"""
from dataclasses import dataclass
from fractions import Fraction

from .wave_context import WaveTurn, number


@dataclass(frozen=True)
class WaveInterval:
    start_index: int
    end_index: int
    start_id: int
    end_id: int
    print_count: int
    mass: tuple[Fraction, ...]
    quote_mass: tuple[Fraction, Fraction, Fraction]
    price_change: Fraction
    bid_change: Fraction | None
    ask_change: Fraction | None
    quote_unresolved_volume: Fraction
    conditional_quote_net_bounds: tuple[Fraction, Fraction]


@dataclass(frozen=True)
class WaveEvidence:
    reference: WaveTurn
    formation: WaveInterval
    follow_through: WaveInterval
    whole: WaveInterval
    bound_assumption: str = 'inside_quote_midpoint_signs_correct'
    quote_freshness: str = 'not_certified_by_trade_row'
    order_authority: bool = False


def quote_valid(tick):
    return tick.bid is not None and tick.ask is not None and 0 < number(tick.bid) < number(tick.ask)


def quote_mass(tick):
    """Inside BBO only, exact midpoint; out-of-book and ties stay unresolved."""
    size, price = number(tick.size), number(tick.price)
    sign = 0
    if quote_valid(tick):
        bid, ask = number(tick.bid), number(tick.ask)
        if bid <= price <= ask:
            difference = 2*price-bid-ask
            sign = (difference > 0)-(difference < 0)
    zero = Fraction(0)
    return (size if sign > 0 else zero, size if sign < 0 else zero, size if not sign else zero)


def interval(prefix, start, end):
    first, last = prefix.tick(start), prefix.tick(end)
    mass = prefix.mass(start, end)  # (start,end]; confirmation counted only once.
    # mass schema: volume,inferred_buy,inferred_sell,unknown,quote_buy,
    # quote_sell,fallback_buy,fallback_sell. All sums are exact Fractions.
    quoted = prefix.quote_evidence_mass(start, end)
    unresolved = quoted[2]
    quote_net = quoted[0]-quoted[1]
    valid = quote_valid(first) and quote_valid(last)
    return WaveInterval(start, end, first.id, last.id, end-start, mass, quoted,
        number(last.price)-number(first.price),
        number(last.bid)-number(first.bid) if valid else None,
        number(last.ask)-number(first.ask) if valid else None,
        unresolved, (quote_net-unresolved, quote_net+unresolved))


def wave_evidence(prefix):
    wave = prefix.wave_context
    if wave is None:
        return ()
    references = {t for t in (wave.local_peak, wave.local_valley) if t is not None}
    for parent in wave.minimal_parents:
        for pair in parent.aliases:
            references.update((pair.peak, pair.valley))
    values = []
    for ref in sorted(references, key=lambda t: (t.basis, t.order, t.kind, t.origin_index, t.confirmation_index)):
        values.append(WaveEvidence(ref,
            interval(prefix, ref.origin_index, ref.confirmation_index),
            interval(prefix, ref.confirmation_index, wave.end_index),
            interval(prefix, ref.origin_index, wave.end_index)))
    return tuple(values)
