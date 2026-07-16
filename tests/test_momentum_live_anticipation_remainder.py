from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedOrder


def _fresh_meta(*, age_seconds: float = 0.0, max_age_seconds: float = 30.0) -> FreshnessMeta:
    return FreshnessMeta(
        retrieved_at_utc=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        max_age_seconds=max_age_seconds,
    )


def _tick(*, bid: float = 10.0, ask: float = 10.02, mid: float | None = None, freshness: FreshnessMeta | None = None):
    mid_f = float(mid if mid is not None else (bid + ask) / 2.0)
    return SimpleNamespace(
        bid=bid,
        ask=ask,
        mid=mid_f,
        spread_bps=((ask - bid) / mid_f) * 10_000.0,
        freshness=freshness or _fresh_meta(),
    )


def _session() -> SimpleNamespace:
    return SimpleNamespace(id=10125, correlation_id="test-corr", symbol="LGPS", risk_snapshot_json={})


def _params() -> dict[str, float]:
    return {"stop_atr_mult": 0.60, "target_atr_mult": 1.20}


def _le(*, submitted: bool = False, order_id: str | None = None) -> dict:
    le = {
        "anticipation_full_qty": 178.0,
        "anticipation_probe_qty": 44.0,
        "anticipation_remainder_qty": 134.0,
        "anticipation_remainder_state": "waiting",
        "breakout_level_price": 9.90,
        "entry_stop_atr_pct": 0.02,
        "position": {
            "quantity": 44.0,
            "original_quantity": 44.0,
            "avg_entry_price": 10.00,
            "notional_usd": 440.0,
            "high_water_mark": 10.00,
            "stop_price": 9.60,
            "target_price": 10.40,
        },
    }
    if submitted:
        le["anticipation_remainder_submitted"] = True
        le["anticipation_remainder_order_id"] = order_id or "old-order"
    return le


class FakeAdapter:
    def __init__(self) -> None:
        self.orders: dict[str, NormalizedOrder] = {}
        self.placed: list[dict] = []

    def get_order(self, order_id: str):
        return self.orders.get(order_id), _fresh_meta()

    def place_market_order(self, **kwargs):
        self.placed.append(kwargs)
        oid = f"new-order-{len(self.placed)}"
        return {"ok": True, "order_id": oid, "client_order_id": kwargs.get("client_order_id")}


def _capture_events(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(lr, "_emit", lambda _db, _sess, typ, payload: events.append((typ, payload)))
    monkeypatch.setattr(lr, "_record_live_entry_ledger_safe", lambda *_args, **_kwargs: None)
    return events


def test_adaptive_starter_plan_keeps_probe_safe_and_remainder_sellable() -> None:
    plan = lr._anticipation_starter_plan(
        full_qty=178.0,
        base_increment=1.0,
        base_min_size=1.0,
        le={},
        bid=1.16,
        ask=1.17,
        mid=1.165,
    )

    assert plan["enabled"] is True
    assert 0.0 < plan["probe_qty"] < plan["full_qty"]
    assert plan["remainder_qty"] > 0.0
    assert plan["probe_qty"] + plan["remainder_qty"] <= plan["full_qty"]
    assert plan["reason"] == "adaptive_starter"


def test_valid_event_confirmation_submits_remainder_without_green_bid(monkeypatch) -> None:
    events = _capture_events(monkeypatch)
    adapter = FakeAdapter()
    le = _le()

    out = lr._handle_anticipation_remainder(
        None,
        _session(),
        adapter,
        le=le,
        product_id="LGPS",
        tick=_tick(bid=9.98, ask=10.02, mid=10.00),
        freshness=_fresh_meta(),
        bid=9.98,
        ask=10.02,
        mid=10.00,
        max_spread_bps=100.0,
        boundary_ok=True,
        market_open=True,
        max_notional=5_000.0,
        guarded_ask=10.03,
        params=_params(),
        held_seconds=5.0,
    )

    assert out["submitted"] is True
    assert adapter.placed and adapter.placed[0]["base_size"] == "134"
    assert le["anticipation_remainder_submitted"] is True
    assert events[-1][0] == "live_anticipation_remainder_submitted"
    assert events[-1][1]["confirmation_legs"]["structural_hold"] is True
    assert events[-1][1]["bid"] < events[-1][1]["avg_entry"]


def test_stale_or_non_confirmed_state_logs_explicit_wait(monkeypatch) -> None:
    events = _capture_events(monkeypatch)
    adapter = FakeAdapter()
    le = _le()
    stale = _fresh_meta(age_seconds=60.0, max_age_seconds=1.0)

    out = lr._handle_anticipation_remainder(
        None,
        _session(),
        adapter,
        le=le,
        product_id="LGPS",
        tick=_tick(bid=9.98, ask=10.02, mid=10.00, freshness=stale),
        freshness=stale,
        bid=9.98,
        ask=10.02,
        mid=10.00,
        max_spread_bps=100.0,
        boundary_ok=True,
        market_open=True,
        max_notional=5_000.0,
        guarded_ask=10.03,
        params=_params(),
        held_seconds=5.0,
    )

    assert out["wait"] is True
    assert not adapter.placed
    typ, payload = events[-1]
    assert typ == "live_anticipation_remainder_wait"
    assert payload["reason"] == "stale_bbo"
    assert payload["stale_or_missing_data_reason"] == "stale_bbo"
    assert {"bid", "ask", "mid", "avg_entry", "remainder_qty", "confirmation_legs"} <= set(payload)

    le2 = _le()
    le2.pop("breakout_level_price")
    le2["position"]["high_water_mark"] = 10.0
    out2 = lr._handle_anticipation_remainder(
        None,
        _session(),
        adapter,
        le=le2,
        product_id="LGPS",
        tick=_tick(bid=9.80, ask=9.84, mid=9.82),
        freshness=_fresh_meta(),
        bid=9.80,
        ask=9.84,
        mid=9.82,
        max_spread_bps=100.0,
        boundary_ok=True,
        market_open=True,
        max_notional=5_000.0,
        guarded_ask=9.85,
        params=_params(),
        held_seconds=5.0,
    )
    assert out2["reason"] == "no_confirmation"
    assert events[-1][0] == "live_anticipation_remainder_wait"
    assert events[-1][1]["reason"] == "no_confirmation"


def test_in_flight_remainder_does_not_duplicate_order(monkeypatch) -> None:
    events = _capture_events(monkeypatch)
    adapter = FakeAdapter()
    adapter.orders["old-order"] = NormalizedOrder(
        order_id="old-order",
        client_order_id="coid",
        product_id="LGPS",
        side="buy",
        status="open",
        order_type="market",
        filled_size=0.0,
        average_filled_price=None,
    )
    le = _le(submitted=True, order_id="old-order")

    out = lr._handle_anticipation_remainder(
        None,
        _session(),
        adapter,
        le=le,
        product_id="LGPS",
        tick=_tick(),
        freshness=_fresh_meta(),
        bid=10.0,
        ask=10.02,
        mid=10.01,
        max_spread_bps=100.0,
        boundary_ok=True,
        market_open=True,
        max_notional=5_000.0,
        guarded_ask=10.03,
        params=_params(),
        held_seconds=5.0,
    )

    assert out["pending"] is True
    assert adapter.placed == []
    assert events[-1][0] == "live_anticipation_remainder_wait"
    assert events[-1][1]["reason"] == "in_flight"


def test_terminal_no_fill_clears_inflight_and_can_retry(monkeypatch) -> None:
    events = _capture_events(monkeypatch)
    adapter = FakeAdapter()
    adapter.orders["old-order"] = NormalizedOrder(
        order_id="old-order",
        client_order_id="coid",
        product_id="LGPS",
        side="buy",
        status="canceled",
        order_type="market",
        filled_size=0.0,
        average_filled_price=None,
    )
    le = _le(submitted=True, order_id="old-order")

    first = lr._handle_anticipation_remainder(
        None,
        _session(),
        adapter,
        le=le,
        product_id="LGPS",
        tick=_tick(),
        freshness=_fresh_meta(),
        bid=10.0,
        ask=10.02,
        mid=10.01,
        max_spread_bps=100.0,
        boundary_ok=True,
        market_open=True,
        max_notional=5_000.0,
        guarded_ask=10.03,
        params=_params(),
        held_seconds=5.0,
    )

    assert first["retryable"] is True
    assert "anticipation_remainder_submitted" not in le
    assert le["anticipation_remainder_state"] == "waiting"
    assert events[-1][0] == "live_anticipation_remainder_terminal_no_fill"

    second = lr._handle_anticipation_remainder(
        None,
        _session(),
        adapter,
        le=le,
        product_id="LGPS",
        tick=_tick(),
        freshness=_fresh_meta(),
        bid=10.0,
        ask=10.02,
        mid=10.01,
        max_spread_bps=100.0,
        boundary_ok=True,
        market_open=True,
        max_notional=5_000.0,
        guarded_ask=10.03,
        params=_params(),
        held_seconds=5.0,
    )

    assert second["submitted"] is True
    assert len(adapter.placed) == 1
    assert events[-1][0] == "live_anticipation_remainder_submitted"
