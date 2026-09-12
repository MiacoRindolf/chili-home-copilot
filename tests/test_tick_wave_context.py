"""Wave identity/confirmation boundaries, batch invariance and partial order."""
from dataclasses import replace
from fractions import Fraction as F

from app.tick_math.wave_context import WaveParent, minimal_enclosing
from tests.test_structural_tape_prefix import m, prefix


def feed(prices, *, batch=1, spreads=None):
    p = prefix(n=len(prices)+10, front=len(prices)+10, active=len(prices)+10)
    events, views = [], []
    for start in range(0, len(prices), batch):
        rows = []
        for i in range(start, min(start+batch, len(prices))):
            bid, ask = spreads[i] if spreads else (prices[i]-.1, prices[i]+.1)
            rows.append(m.Tick(i+1, prices[i], 1, bid, ask, i+1, i+1, i+1, ('test',)))
        revision = start//batch+1
        old = p.last_receipt
        receipt = m.PublicationFrontierReceipt(rows[-1].known_ns, len(rows), m.rows_sha256(rows),
            p.prefix_sha256, 'a'*64, revision, f'{revision:064x}',
            old.source_sequence if old else 0, old.source_root_sha256 if old else 'b'*64)
        assert p.append_frontier(rows, receipt).status == 'applied'
        events.extend(p.wave_context.events)
        views.append(p.wave_context)
    return p, events, views


def identity(t):
    return t.basis, t.kind, t.order, t.origin_index, t.confirmation_index


def test_spread_bounce_does_not_create_quote_confirmed_turn():
    p, events, _ = feed([10, 10.01, 9.99, 10.02, 10])
    assert not [t for t in events if t.basis == 'quote_resolved']
    assert p.wave_context.local_phase.side == 'unknown'


def test_recovery_below_prior_peak_is_front_and_level_touch_unknown():
    _, events, views = feed([10, 15, 12, 13, 12])
    assert views[3].local_peak.price == 15
    assert views[3].local_valley.price == 12
    assert views[3].local_phase.reason == 'recovery_after_confirmed_valley'
    assert views[4].local_phase.reason == 'level_touch'
    assert all(t.confirmation_index < len(views) for t in events)


def test_no_invented_intermediate_opportunity_when_whole_wave_arrives_together():
    prices = [10, 15, 12, 14, 11, 13, 10, 12, 13.5, 14]
    one, one_events, views = feed(prices)
    batched, batched_events, together = feed(prices, batch=len(prices))
    assert len(together) == 1
    assert batched_events == one_events
    assert replace(batched.wave_context, events=()) == replace(one.wave_context, events=())
    assert len([e for e in batched_events if e.basis == 'quote_resolved']) > 1
    assert not together[0].order_authority
    for end, full in enumerate(views):
        partial, _, _ = feed(prices[:end+1], batch=end+1)
        assert replace(full, events=()) == replace(partial.wave_context, events=())


def test_recursive_promotion_waits_for_confirmed_right_witness_and_uses_plateau_last():
    _, events, _ = feed([1, 3, 2, 5, 2, 5, 2, 4, 1])
    higher = [e for e in events if e.basis == 'raw_recursive' and e.order == 2 and e.kind == 'peak']
    assert [(e.origin_index, e.confirmation_index) for e in higher] == [(5, 8)]
    assert higher[0].origin_id == 6 and higher[0].confirmation_id == 9


def test_partial_order_has_no_arbitrary_weight_or_hierarchy_order_tiebreak():
    outer = WaveParent(0, F(0), F(10), ())
    near = WaveParent(5, F(3), F(7), ())
    assert minimal_enclosing(root=10, low=F(4), high=F(6), candidates=(outer, near)) == (near,)
    wide_newer = WaveParent(6, F(2), F(8), ())
    assert minimal_enclosing(root=10, low=F(4), high=F(6), candidates=(near, wide_newer)) == (near, wide_newer)
    # An unconfirmed excursion outside old bounds invalidates enclosure even
    # when the final price has returned inside the old range.
    assert minimal_enclosing(root=10, low=F(4), high=F('7.5'), candidates=(near,)) == ()


def test_invalid_quote_does_not_get_treated_as_zero_spread_confirmation():
    _, events, _ = feed([10, 15, 12, 13], spreads=[(None, None)]*4)
    assert not [e for e in events if e.basis == 'quote_resolved']


def test_resource_refusal_does_not_advance_wave_state():
    p, _, _ = feed([10, 15, 12, 13])
    before = p.wave_context
    rows = [m.Tick(100+i, 10, 1, 9, 11, 100+i, 100+i, 200, ('test',)) for i in range(15)]
    receipt = m.FrontierReceipt(200, len(rows), m.rows_sha256(rows), p.prefix_sha256)
    assert p.append_frontier(rows, receipt).status == 'unresolved'
    assert p.wave_context is before
