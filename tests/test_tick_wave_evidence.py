from fractions import Fraction as F
from dataclasses import replace

from app.tick_math.wave_evidence import quote_mass, wave_evidence
from tests.test_tick_wave_context import feed
from tests.test_structural_tape_prefix import m


def test_positive_whole_progress_cannot_hide_negative_post_confirmation_progress():
    p, _, _ = feed([10, 15, 12, 13, 12.9],
        spreads=[(9.8, 10), (14.8, 15), (11.8, 12), (12.8, 13), (12.7, 12.9)])
    evidence = next(e for e in wave_evidence(p) if e.reference.basis == 'quote_resolved' and e.reference.kind == 'valley')
    assert evidence.formation.price_change == 1
    assert evidence.follow_through.price_change == F('-0.1')
    assert evidence.whole.price_change == F('0.9')
    assert evidence.follow_through.bid_change == F('-0.1')
    # Buying inference and lost price progress coexist; neither is a veto here.
    assert evidence.follow_through.conditional_quote_net_bounds == (1, 1)
    assert not evidence.order_authority


def test_confirmation_belongs_to_formation_once_and_empty_followthrough_has_zero_mass():
    p, _, _ = feed([10, 12])
    evidence, = wave_evidence(p)
    assert evidence.formation.print_count == 1
    assert evidence.formation.mass[0] == 1
    assert evidence.follow_through.start_id == evidence.follow_through.end_id == 2
    assert evidence.follow_through.print_count == 0
    assert evidence.follow_through.mass == (F(0),)*8
    assert evidence.whole.mass == evidence.formation.mass
    assert evidence.follow_through.conditional_quote_net_bounds == (0, 0)


def test_out_of_book_and_tick_rule_fallback_cannot_narrow_quote_bounds():
    p, _, _ = feed([10, 12, 13], spreads=[(9.8, 10), (11.8, 12), (10, 11)])
    evidence, = wave_evidence(p)
    assert evidence.follow_through.mass[4] == 1  # legacy out-of-book quote buy label
    assert evidence.follow_through.quote_mass == (0, 0, 1)
    assert evidence.follow_through.conditional_quote_net_bounds == (-1, 1)
    p, _, _ = feed([10, 12, 13], spreads=[(9.8, 10), (11.8, 12), (None, None)])
    evidence, = wave_evidence(p)
    assert evidence.follow_through.mass[6] == 1  # legacy tick-rule fallback buy
    assert evidence.follow_through.quote_mass == (0, 0, 1)
    assert evidence.follow_through.bid_change is None


def test_decimal_midpoint_tie_does_not_acquire_float_rounding_direction():
    t = m.Tick(1, 10.015, .125, 10.01, 10.02, 1, 1, 1, ('test',))
    assert quote_mass(t) == (0, 0, F('.125'))
    assert quote_mass(replace(t, price=10.02)) == (F('.125'), 0, 0)
    assert quote_mass(replace(t, price=10.01)) == (0, F('.125'), 0)
    assert quote_mass(replace(t, bid=10.02, ask=10.01)) == (0, 0, F('.125'))


def test_every_reference_has_exact_additive_formation_and_followthrough():
    p, _, _ = feed([10, 15, 12, 14, 11, 13, 10, 12, 13.5, 14], batch=10)
    for e in wave_evidence(p):
        assert tuple(a+b for a,b in zip(e.formation.mass, e.follow_through.mass)) == e.whole.mass
        assert tuple(a+b for a,b in zip(e.formation.quote_mass, e.follow_through.quote_mass)) == e.whole.quote_mass
        assert e.formation.price_change+e.follow_through.price_change == e.whole.price_change
        assert sum(e.whole.quote_mass) == e.whole.mass[0]
