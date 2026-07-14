"""Order-truth bundle (2026-06-11): a live session must never die with broker
orders still resting (KMRK GTC knife −$255, CPSH/SNDG raced fills), entry limits
are DAY orders (never GTC), and a bracket intent's stop can never sit on the
wrong side of its own trade's entry (AAOG: stop 11.81 over entry 11.65, dumped
in 51s by its own protection)."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from app import models
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.bracket_intent_writer import clamp_stop_geometry
from app.services.trading.momentum_neural.automation_query import cancel_automation_session


# ── stop-geometry clamp (pure) ────────────────────────────────────────────────
def test_clamp_fixes_inverted_long_stop() -> None:
    # the AAOG case: long, stop above entry
    out = clamp_stop_geometry(entry=11.65, stop=11.81, direction="long")
    assert out is not None and out < 11.65
    assert abs(out - 11.65 * 0.995) < 1e-9


def test_clamp_leaves_sane_stops_alone() -> None:
    assert clamp_stop_geometry(entry=11.65, stop=10.27, direction="long") is None
    assert clamp_stop_geometry(entry=5.0, stop=5.4, direction="short") is None


def test_clamp_fixes_inverted_short_and_handles_garbage() -> None:
    out = clamp_stop_geometry(entry=5.0, stop=4.9, direction="short")
    assert out is not None and out > 5.0
    assert clamp_stop_geometry(entry=0.0, stop=1.0, direction="long") is None
    assert clamp_stop_geometry(entry=10.0, stop=None, direction="long") is None


# ── day-TIF contract on the entry path ───────────────────────────────────────
def test_entry_call_site_forces_day_tif_and_adapters_accept_it() -> None:
    from app.services.trading.momentum_neural import live_runner
    from app.services.trading.venue.coinbase_spot import CoinbaseSpotAdapter
    from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter

    assert 'time_in_force="gfd"' in inspect.getsource(live_runner)
    for adapter_cls in (RobinhoodSpotAdapter, CoinbaseSpotAdapter):
        params = inspect.signature(adapter_cls.place_limit_order_gtc).parameters
        assert "time_in_force" in params, adapter_cls.__name__


def test_factory_resolves_momentum_execution_families() -> None:
    # the death sweep resolves the adapter from sess.execution_family — both
    # lane families must map (robinhood_spot was missing before this bundle)
    from app.services.trading.venue.factory import _BUILDERS

    assert "robinhood_spot" in _BUILDERS
    assert "coinbase_spot" in _BUILDERS


# ── session-death order sweep ────────────────────────────────────────────────
class _FakeAdapter:
    def __init__(
        self,
        *,
        order_ids: list[str],
        filled: float = 0.0,
        status: str = "open",
    ) -> None:
        self.cancelled: list[str] = []
        self._filled = filled
        self._statuses = {str(oid): status for oid in order_ids}

    def _order(self, order_id: str):
        return SimpleNamespace(
            order_id=str(order_id),
            client_order_id=f"cid-{order_id}",
            product_id="KMRK",
            filled_size=self._filled,
            status=self._statuses[str(order_id)],
            side="buy",
            raw={"quantity": 10.0},
        )

    def get_account_identity_truth(self):
        return {"readable": True, "identity": "order-truth-test-account-v1"}

    def get_position_quantity_truth(self, _product_id):
        return {"readable": True, "quantity": 0.0}

    def get_order(self, order_id: str):
        return self._order(order_id), None

    def get_order_truth(self, order_id: str):
        if str(order_id) not in self._statuses:
            return {"readable": True, "found": False, "order": None}
        return {"readable": True, "found": True, "order": self._order(order_id)}

    def list_open_orders_truth(self, *, product_id=None, limit=250):
        terminal = {
            "filled", "cancelled", "canceled", "rejected", "failed", "expired",
        }
        orders = [
            self._order(oid)
            for oid, status in self._statuses.items()
            if status.lower() not in terminal
        ]
        return {"readable": True, "orders": orders[:limit]}

    def cancel_order(self, order_id: str):
        self.cancelled.append(order_id)
        self._statuses[str(order_id)] = "cancelled"
        return {"ok": True}


def _live_session_with_orders(db, *, order_ids: list[str]) -> TradingAutomationSession:
    u = models.User(name="order-truth")
    db.add(u)
    db.flush()
    v = MomentumStrategyVariant(
        family="order_truth", variant_key="ot_v", label="ot", params_json={},
    )
    db.add(v)
    db.flush()
    sess = TradingAutomationSession(
        user_id=u.id,
        symbol="KMRK",
        mode="live",
        variant_id=v.id,
        state="watching_live",
        execution_family="robinhood_spot",
        risk_snapshot_json={
            "non_alpaca_account_identity": "order-truth-test-account-v1",
            "momentum_live_execution": {
                "entry_order_id": order_ids[0] if order_ids else None,
                "entry_order_ids_all": list(order_ids),
                "entry_client_order_id": (
                    f"cid-{order_ids[0]}" if order_ids else None
                ),
                "entry_want_qty": 10.0,
            }
        },
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


def test_session_death_cancels_exactly_owned_resting_order(db, monkeypatch) -> None:
    fake = _FakeAdapter(order_ids=["oid-1"], status="confirmed")
    import app.services.trading.venue.factory as factory
    from app.services.trading.momentum_neural import automation_query as aq

    monkeypatch.setattr(factory, "get_adapter", lambda src: fake)
    monkeypatch.setattr(
        aq,
        "_reaper_broker_position_truth",
        lambda _sess: (True, {"broker_quantity": 0.0}),
    )
    sess = _live_session_with_orders(db, order_ids=["oid-1"])
    res = cancel_automation_session(db, user_id=sess.user_id, session_id=sess.id)
    assert res["ok"] and res["state"] == "live_cancelled"
    assert fake.cancelled == ["oid-1"]


def test_session_death_adopts_filled_order_instead_of_cancelling(db, monkeypatch) -> None:
    fake = _FakeAdapter(
        order_ids=["oid-filled"],
        filled=304.0,
        status="filled",
    )
    import app.services.trading.venue.factory as factory

    monkeypatch.setattr(factory, "get_adapter", lambda src: fake)
    sess = _live_session_with_orders(db, order_ids=["oid-filled"])
    res = cancel_automation_session(db, user_id=sess.user_id, session_id=sess.id)
    assert res["ok"] and res["adopted"] is True
    assert fake.cancelled == []  # never cancel a filled order — surface it
    from sqlalchemy import text

    cancelled_event = db.execute(text(
        "SELECT 1 FROM trading_automation_events "
        "WHERE session_id = :sid AND event_type = 'session_cancelled'"
    ), {"sid": sess.id}).scalar()
    db.refresh(sess)
    assert sess.state == "live_pending_entry"
    assert cancelled_event is None


# ── event-driven pending-entry lifecycle (no magic seconds) ──────────────────
def test_pending_entry_cancels_on_setup_invalidation() -> None:
    from app.services.trading.momentum_neural.live_runner import _pending_entry_cancel_reason

    # bid broke the structural stop -> setup dead, cancel NOW (clock irrelevant)
    assert _pending_entry_cancel_reason(
        bid=7.50, structural_stop=7.60, limit_px=7.91,
        elapsed_s=3.0, rest_bars=2.0, interval_s=60.0,
    ) == "entry_invalidated_stop_breach"


def test_pending_entry_cancels_when_limit_left_behind() -> None:
    from app.services.trading.momentum_neural.live_runner import _pending_entry_cancel_reason

    # bid above our buy limit -> can only fill on the way back down
    assert _pending_entry_cancel_reason(
        bid=8.20, structural_stop=7.60, limit_px=7.91,
        elapsed_s=3.0, rest_bars=2.0, interval_s=60.0,
    ) == "entry_limit_left_behind"


def test_pending_entry_rests_through_broker_review_then_backstops() -> None:
    from app.services.trading.momentum_neural.live_runner import _pending_entry_cancel_reason

    common = dict(bid=7.85, structural_stop=7.60, limit_px=7.91,
                  rest_bars=2.0, interval_s=60.0)
    # 13s of RH "unconfirmed" review (the old 10s killer): KEEP RESTING
    assert _pending_entry_cancel_reason(elapsed_s=13.0, **common) is None
    # ...but never outlive the bar evidence (2 bars @1m = 120s)
    assert _pending_entry_cancel_reason(elapsed_s=121.0, **common) == "entry_rest_backstop"


def test_pending_entry_no_quote_falls_back_to_backstop_only() -> None:
    from app.services.trading.momentum_neural.live_runner import _pending_entry_cancel_reason

    assert _pending_entry_cancel_reason(
        bid=None, structural_stop=7.60, limit_px=7.91,
        elapsed_s=30.0, rest_bars=2.0, interval_s=60.0,
    ) is None
