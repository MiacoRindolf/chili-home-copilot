"""L9-C1/C4 — subscribe-hint wiring + ignition commit-fence predicate.

HYFM 500% day (2026-08-03): na-detect at route-eligible sa loob ng 10 min mula
sa headline, pero ang TRADES subscription ng bridge ay 18m31s pa (watch-resolver
starvation; depth watch 11:39:01Z vs trades 11:58:01Z). Ang C1 ay nagsusulat ng
subscribe hint kada refresh-detected mover — ang bridge fast-poll (3s) ang
consumer. Ang C4 ay ang latent na 'iqfeed' substring predicate na hindi
tumutugma sa source='ignition_tick' (immediate ticks nang walang commit fence).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.trading.momentum_neural.ross_event_admission import (
    _source_owns_commit_fence,
)
from app.services.trading_scheduler import _emit_viability_subscribe_hints


def _mock_db():
    db = MagicMock()
    db.begin_nested.return_value.__enter__ = MagicMock(return_value=None)
    db.begin_nested.return_value.__exit__ = MagicMock(return_value=False)
    return db


def test_hints_isinusulat_para_sa_movers(monkeypatch):
    db = _mock_db()
    movers = [{"symbol": "HYFM"}, {"ticker": "EZRA"}, {"symbol": "FCUV"}]
    n = _emit_viability_subscribe_hints(db, movers)
    assert n == 3
    assert db.execute.call_count == 3
    db.commit.assert_called_once()


def test_pair_shaped_at_walang_symbol_ay_nilalaktawan():
    db = _mock_db()
    movers = [{"symbol": "BTC-USD"}, {"symbol": ""}, {}, {"symbol": "HYFM"}]
    n = _emit_viability_subscribe_hints(db, movers)
    assert n == 1


def test_cap_ay_verbatim(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_momentum_viability_refresh_subscribe_hint_cap", 2,
        raising=False,
    )
    db = _mock_db()
    movers = [{"symbol": f"SYM{i}"} for i in range(10)]
    n = _emit_viability_subscribe_hints(db, movers)
    assert n == 2


def test_flag_off_ay_zero(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_momentum_viability_refresh_subscribe_hints_enabled", False,
        raising=False,
    )
    db = _mock_db()
    n = _emit_viability_subscribe_hints(db, [{"symbol": "HYFM"}])
    assert n == 0
    db.execute.assert_not_called()


def test_error_ay_fail_open_zero():
    db = _mock_db()
    db.execute.side_effect = RuntimeError("db patay")
    # ang writer mismo (request_bridge_subscription) ay lumulunok ng error at
    # nagre-return False — kaya 0 hints, walang exception paakyat.
    n = _emit_viability_subscribe_hints(db, [{"symbol": "HYFM"}])
    assert n == 0


@pytest.mark.parametrize(
    "source,expected",
    [
        ("iqfeed", True),
        ("iqfeed_l1", True),
        ("ignition_tick", True),      # ang C4 bug: dating False ito
        ("IGNITION_TICK", True),
        ("scanner", False),
        ("equity_viability_refresh", False),
        ("", False),
        (None, False),
    ],
)
def test_commit_fence_predicate(source, expected):
    assert _source_owns_commit_fence(source) is expected
