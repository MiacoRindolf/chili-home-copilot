"""Fix 5B: pattern-monitor exit_now corroboration + reroute-to-tighten.

exit_now is beneficial only ~21% of the time, so a fresh-but-UNCORROBORATED
exit_now is rerouted to a stop-tighten (never a cut), and 0%-beneficial sources
are dropped. The resolver only ever tightens (never loosens) the stop and never
moves it to/beyond the current price; the hard stop + breaker are untouched.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.trading._exit_monitor_common import (
    resolve_monitor_exit_action,
    apply_monitor_exit_reroute_tighten,
)

DENY = frozenset({"heuristic"})
FLOOR = 0.5


def _decision(action="exit_now", source="llm", age_h=1.0, price=None, did=7):
    return SimpleNamespace(
        id=did,
        action=action,
        decision_source=source,
        created_at=datetime.utcnow() - timedelta(hours=age_h),
        price_at_decision=price,
    )


def _resolve(decision, *, entry, stop, px, is_long=True):
    return resolve_monitor_exit_action(
        decision, entry=entry, stop=stop, current_px=px, is_long=is_long,
        corroboration_floor=FLOOR, denylisted_sources=DENY,
    )


# ── resolver ───────────────────────────────────────────────────────────

def test_no_decision_holds():
    assert _resolve(None, entry=100, stop=85, px=98) == ("hold", None, None)


def test_non_exit_action_holds():
    v, ns, meta = _resolve(_decision(action="hold"), entry=100, stop=85, px=98)
    assert v == "hold" and ns is None


def test_denylisted_source_holds():
    v, ns, meta = _resolve(_decision(source="heuristic"), entry=100, stop=85, px=98)
    assert v == "hold" and ns is None


def test_uncorroborated_long_reroutes_to_tighten():
    # px 98: only 2/15 = 13% of the way to the stop -> not corroborated -> tighten
    v, ns, meta = _resolve(_decision(), entry=100, stop=85, px=98)
    assert v == "tighten_stop"
    assert abs(ns - 92.5) < 1e-9            # entry - 0.5*(entry-stop)
    assert meta is not None and meta["decision_id"] == 7


def test_corroborated_long_exits():
    # px 92: 8/15 = 53% of the way to stop -> corroborated -> honor the cut
    v, ns, meta = _resolve(_decision(), entry=100, stop=85, px=92)
    assert v == "exit" and ns is None and meta is not None


def test_long_in_profit_tightens_not_cut():
    # px 110 (winner), stop still below entry -> never cut; raise the stop
    v, ns, meta = _resolve(_decision(), entry=100, stop=85, px=110)
    assert v == "tighten_stop"
    assert abs(ns - 92.5) < 1e-9


def test_long_stop_trailed_into_profit_holds():
    # stop 105 > entry 100 (risk<=0): trailing stop already protects -> hold
    v, ns, meta = _resolve(_decision(), entry=100, stop=105, px=110)
    assert v == "hold" and ns is None


def test_short_uncorroborated_reroutes_to_tighten():
    # short: entry 100, stop 115, px 105 -> 5/15=33% -> tighten to 107.5
    v, ns, meta = _resolve(_decision(), entry=100, stop=115, px=105, is_long=False)
    assert v == "tighten_stop"
    assert abs(ns - 107.5) < 1e-9


def test_missing_geometry_preserves_prior_exit_behavior():
    v, ns, meta = _resolve(_decision(), entry=None, stop=85, px=98)
    assert v == "exit" and meta is not None


# ── apply (tighten-only) ─────────────────────────────────────────────────

class _FakeDB:
    def add(self, _):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


def test_apply_tightens_long_stop_up():
    t = SimpleNamespace(id=1, ticker="ETH-USD", direction="long", stop_loss=85.0)
    moved = apply_monitor_exit_reroute_tighten(_FakeDB(), t, new_stop=92.5)
    assert moved is True
    assert abs(t.stop_loss - 92.5) < 1e-9


def test_apply_refuses_to_loosen_long_stop():
    t = SimpleNamespace(id=1, ticker="ETH-USD", direction="long", stop_loss=85.0)
    moved = apply_monitor_exit_reroute_tighten(_FakeDB(), t, new_stop=80.0)
    assert moved is False
    assert abs(t.stop_loss - 85.0) < 1e-9        # unchanged


def test_apply_refuses_to_loosen_short_stop():
    t = SimpleNamespace(id=2, ticker="X", direction="short", stop_loss=115.0)
    moved = apply_monitor_exit_reroute_tighten(_FakeDB(), t, new_stop=120.0)
    assert moved is False
    assert abs(t.stop_loss - 115.0) < 1e-9
