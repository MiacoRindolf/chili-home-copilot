"""Whole-position ownership must not create a new fractional sibling."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.paper_execution import (
    first_target_exit_shape, first_target_leaves_runner,
)
from tests.test_exit_verdict_held_priority import _held_tick, _no_external_market_or_broker_http
from tests.test_held_tick_bbo_iqfeed_l1_first import _wired


class NoOrders:
    def __getattr__(self, name):
        raise AssertionError('No broker access under whole-position placement policy: ' + name)


@pytest.mark.parametrize('family', ['alpaca_spot', 'robinhood_spot', 'robinhood_agentic_mcp'])
@pytest.mark.parametrize('can_split,partial_taken', [(True, False), (False, False), (True, True)])
def test_shared_shape_declared_whole_exit_dominates_split_capability(family, can_split, partial_taken):
    assert first_target_leaves_runner(family, whole_position_exit=True) is False
    assert first_target_exit_shape(
        execution_family=family, can_split=can_split, partial_taken=partial_taken,
        whole_position_exit=True,
    ) == (False, 'target')


@pytest.mark.parametrize('family', ['alpaca_spot', 'robinhood_spot', 'robinhood_agentic_mcp'])
def test_supported_entry_preserves_old_protection_without_placing_or_cancelling(monkeypatch, family):
    sess = SimpleNamespace(id=99, symbol='BATL', execution_family=family, risk_snapshot_json={})
    le = {
        'entry_filled_at_utc': '2026-09-10T14:00:00+00:00',
        'position': {'quantity': 1000, 'avg_entry_price': 2.0, 'stop_price': 1.9},
        'scale_limit_order_id': 'preexisting-oco',
        'scale_limit_is_oco': True,
        'deadman_stop': {'order_id': 'preexisting-remainder-protection', 'qty': 500},
    }
    before = deepcopy(le)
    events = []
    monkeypatch.setattr(lr, '_emit', lambda _db, _sess, kind, payload: events.append((kind, payload)))
    lr._place_scale_out_limit(None, sess, NoOrders(), le=le, product_id='BATL',
                             target_px=2.3, filled=1000, prod=None)
    assert le['position'] == before['position']
    assert le['deadman_stop'] == before['deadman_stop']
    assert le['scale_limit_order_id'] == before['scale_limit_order_id']
    assert le['scale_limit_is_oco'] is True
    assert le['scale_limit_policy']['exit_fraction'] == 1.0
    assert events == [('scale_out_limit_suppressed', {
        'reason': 'whole_position_exit_policy', 'quantity': 1000.0, 'target_price': 2.3,
        'binding': 'exit_verdict_g_all', 'exit_fraction': 1.0, 'new_fractional_order': False,
    })]


@pytest.mark.parametrize('symbol,anchor', [('BATL', None), ('BTC-USD', '2026-09-10T14:00:00+00:00')])
def test_unsupported_entry_retains_legacy_fractional_route(monkeypatch, symbol, anchor):
    sess = SimpleNamespace(id=99, symbol=symbol, execution_family='robinhood_spot', risk_snapshot_json={})
    le = {'position': {'quantity': 1000, 'avg_entry_price': 2.0, 'stop_price': 1.9}}
    if anchor:
        le['entry_filled_at_utc'] = anchor
    placed = []
    adapter = SimpleNamespace(place_limit_order_gtc=lambda **kw: placed.append(kw) or {'ok': True, 'order_id': 'legacy-scale'})
    monkeypatch.setattr(lr, '_emit', lambda *a, **k: None)
    lr._place_scale_out_limit(None, sess, adapter, le=le, product_id=symbol,
                             target_px=2.3, filled=1000, prod=None)
    assert len(placed) == 1
    assert 0 < float(placed[0]['base_size']) < 1000
    assert le['scale_limit_order_id'] == 'legacy-scale'
    assert 'scale_limit_policy' not in le


@pytest.mark.parametrize('family', ['alpaca_spot', 'robinhood_spot'])
def test_actual_supported_secondary_target_requests_whole_position(db, monkeypatch, _wired, family):
    sess, _, adapter, _, _ = _held_tick(db, monkeypatch, rollover=False)
    sess.state = lr.STATE_LIVE_SCALING_OUT
    sess.execution_family = family
    db.commit()
    requested = []
    # The adapter is an in-memory test venue. Supply its verified account
    # boundary without contacting a real non-Alpaca account; transport is
    # replaced below and the no-external-HTTP fixture remains active.
    monkeypatch.setattr(lr, 'verify_frozen_non_alpaca_account_identity', lambda *a, **kw: {'ok': True})
    monkeypatch.setattr(lr, '_submit_live_market_exit', lambda *a, **kw: requested.append(kw) or {'ok': False})
    # Inspect the real target's request while the broker transport is unavailable.
    monkeypatch.setattr(lr, '_live_exit_submit_succeeded', lambda *a, **kw: False)
    result = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    assert result.get('exit_submit_failed') is True, result
    assert len(requested) == 1
    assert requested[0]['reason'] == 'target'
    assert requested[0]['quantity'] == 10.0
    assert requested[0]['extra']['runner_qty'] == 0.0
    assert requested[0]['extra']['exit_shape_basis'] == 'exit_verdict_g_all'
    assert adapter.market_calls == [] and adapter.limit_calls == []
