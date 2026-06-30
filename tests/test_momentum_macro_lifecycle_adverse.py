"""MACRO TIER — full-session-lifecycle ADVERSE / SAFETY scenarios for the Ross
momentum live lane.

These drive the REAL ``tick_live_session`` FSM against a ``FakeVenueAdapter`` and
the seeded ``db`` fixture (study: ``tests/test_momentum_live_runner.py``), asserting
the *terminal* state AND a safety property per scenario:

  1. JUNK name (sub-floor viability / not live-eligible) NEVER advances to
     ``live_entered`` — it stays watching (slot is not consumed by a non-mover).
  2. STOP-OUT: enter -> bid drops to the protective stop -> the confirmed stop
     fires -> terminal ``live_exited`` with the realized loss BOUNDED by the
     structural stop distance (no naked market overshoot).
  3. PER-BROKER DAILY-LOSS breaker: a tripped broker block halts NEW arming/entry
     but a HELD position's EXIT still flattens (a breaker must NEVER strand risk).
  4. OVER-RESTRICTION (the 5th-pass concern, HIGHEST value): a legitimate Ross
     mover (live-eligible, high score, valid tight quote) that has already placed
     its entry order must reach ``live_entered`` end-to-end — the stacked vetoes
     do NOT choke a real mover, and the position is sized/stopped correctly.
  5. DUPLICATE / LATE fill event: a second tick re-polling the SAME entry order
     reconciles to ONE position (no double-count, no naked orphan).

All adapters are fakes; no source is modified. The ``db`` fixture truncates, so a
``_test``-suffixed DB is mandatory (enforced by conftest).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import MomentumSymbolViability
from app.services.trading.governance import (
    REAL_DAILY_LOSS_FAMILIES,
    _kill_switch_halts_exits,
    _normalize_real_family,
    clear_stale_broker_daily_loss_blocks,
    is_broker_daily_loss_blocked,
    set_broker_daily_loss_block,
)
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_ENTERED,
    STATE_LIVE_EXITED,
    STATE_LIVE_PENDING_ENTRY,
    STATE_WATCHING_LIVE,
)
from app.services.trading.momentum_neural.live_runner import (
    _live_exec,
    tick_live_session,
)
from app.services.trading.momentum_neural.persistence import create_trading_automation_session
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
from app.services.trading.venue.coinbase_spot import reset_duplicate_client_order_guard_for_tests
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedOrder, NormalizedProduct, NormalizedTicker

from tests.test_momentum_paper_runner import _seed_live_eligible_row


# ── Fakes / helpers ───────────────────────────────────────────────────────────


def _fresh() -> FreshnessMeta:
    return FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=120.0)


def _uid(db: Session, suffix: str) -> int:
    u = User(name=f"MacroLC_{suffix}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


def _product(pid: str) -> NormalizedProduct:
    return NormalizedProduct(
        product_id=pid,
        base_currency=pid.split("-")[0],
        quote_currency="USD",
        status="online",
        trading_disabled=False,
        cancel_only=False,
        limit_only=False,
        post_only=False,
        auction_mode=False,
        base_increment=0.001,
        base_min_size=0.001,
    )


class FakeVenueAdapter:
    """A deterministic, introspectable VenueAdapter stand-in.

    Per-order-id ``get_order`` dispatch so entry and exit (sell) orders can resolve
    to DIFFERENT broker truth in the same tick. Records every place/cancel so a test
    can assert no oversize / no double-submit / exit-never-blocked.
    """

    def __init__(self, product_id: str, *, bid: float, ask: float, spread_bps: float = 10.0) -> None:
        self.product_id = product_id
        self._bid = bid
        self._ask = ask
        self._spread_bps = spread_bps
        self._orders: dict[str, NormalizedOrder] = {}
        self.place_market_calls: list[dict[str, Any]] = []
        self.place_limit_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[str] = []
        self._next_exit_seq = 0

    # -- quote --
    def set_quote(self, *, bid: float, ask: float, spread_bps: float | None = None) -> None:
        self._bid = bid
        self._ask = ask
        if spread_bps is not None:
            self._spread_bps = spread_bps

    def is_enabled(self) -> bool:
        return True

    def get_best_bid_ask(self, _pid: str):
        mid = (self._bid + self._ask) / 2.0
        return (
            NormalizedTicker(
                product_id=self.product_id,
                bid=self._bid,
                ask=self._ask,
                mid=mid,
                spread_bps=self._spread_bps,
                freshness=_fresh(),
            ),
            _fresh(),
        )

    def get_product(self, _pid: str):
        return (_product(self.product_id), _fresh())

    # -- order registry --
    def register_order(self, order: NormalizedOrder) -> None:
        self._orders[str(order.order_id)] = order

    def get_order(self, oid: str):
        o = self._orders.get(str(oid))
        if o is None:
            return (None, _fresh())
        return (o, _fresh())

    # -- placement (exit market sell registers a FILLED sell so the poll completes) --
    def place_market_order(self, **kwargs):
        self.place_market_calls.append(dict(kwargs))
        side = kwargs.get("side", "sell")
        size = float(kwargs.get("base_size") or kwargs.get("quote_size") or 0.0)
        self._next_exit_seq += 1
        oid = f"fake-exit-{self._next_exit_seq}"
        # The exit sell fills at the live bid (a protective market exit pays the bid).
        self.register_order(
            NormalizedOrder(
                order_id=oid,
                client_order_id=kwargs.get("client_order_id"),
                product_id=self.product_id,
                side=side,
                status="FILLED",
                order_type="market",
                filled_size=size,
                average_filled_price=self._bid,
            )
        )
        return {"ok": True, "order_id": oid, "client_order_id": kwargs.get("client_order_id")}

    def place_limit_order_gtc(self, **kwargs):
        self.place_limit_calls.append(dict(kwargs))
        side = kwargs.get("side", "sell")
        size = float(kwargs.get("base_size") or 0.0)
        self._next_exit_seq += 1
        oid = f"fake-limit-{self._next_exit_seq}"
        self.register_order(
            NormalizedOrder(
                order_id=oid,
                client_order_id=kwargs.get("client_order_id"),
                product_id=self.product_id,
                side=side,
                status="FILLED",
                order_type="limit",
                filled_size=size,
                average_filled_price=(self._bid if side == "sell" else self._ask),
            )
        )
        return {"ok": True, "order_id": oid, "client_order_id": kwargs.get("client_order_id")}

    def cancel_order(self, oid: str):
        self.cancel_calls.append(str(oid))
        return {"ok": True, "raw": {}}


def _entered_session_snapshot(
    *,
    entry_order_id: str,
    max_notional: float = 5000.0,
) -> dict[str, Any]:
    """A frozen risk snapshot for a LIVE_PENDING_ENTRY session that has already
    SUBMITTED its entry order (so the fill-handler — the real admission path — runs
    on the next tick rather than the OHLCV-driven trigger gate)."""
    return {
        RISK_SNAPSHOT_KEY: {"allowed": True, "evaluated_at_utc": "2026-01-01T00:00:00+00:00"},
        "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
        "momentum_policy_caps": {"max_notional_per_trade_usd": max_notional, "max_hold_seconds": 3600},
        "momentum_live_execution": {
            "entry_submitted": True,
            "entry_order_id": entry_order_id,
            "entry_submit_utc": datetime.utcnow().isoformat(),
            "entry_limit_price": "100.0",
            "entry_spread_bps_at_decision": 10.0,
        },
    }


@pytest.fixture(autouse=True)
def _venue_connected_by_default(monkeypatch):
    """``_venue_broker_connected`` short-circuits with ``venue_broker_not_connected``
    in the test env (no live creds). Default it CONNECTED so the lifecycle logic runs
    (mirrors test_momentum_live_runner's autouse fixture)."""
    import app.services.trading.momentum_neural.live_runner as _lr
    monkeypatch.setattr(_lr, "_venue_broker_connected", lambda ef: True)


@pytest.fixture(autouse=True)
def _clear_broker_blocks():
    """Per-broker daily-loss blocks are MODULE-GLOBAL in-memory state; clear before and
    after each test so a sticky block set here cannot leak into another test."""
    import app.services.trading.governance as _gov
    with _gov._per_broker_lock:
        _gov._per_broker_daily_loss.clear()
    yield
    with _gov._per_broker_lock:
        _gov._per_broker_daily_loss.clear()


# ── Scenario 1: JUNK name never reaches LIVE_ENTERED ──────────────────────────


def test_junk_name_below_floor_never_enters(monkeypatch, db: Session) -> None:
    """A sub-floor / not-live-eligible name watched LIVE must NOT advance past
    ``watching_live`` — the trigger/score gate keeps a non-mover from consuming a
    live slot, and crucially it never places an order."""
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="JUNK-USD")
    db.commit()

    # Degrade the persisted viability row to JUNK: well below the ~0.52 entry floor
    # AND not live-eligible (no catalyst / not an A-setup).
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "JUNK-USD", MomentumSymbolViability.variant_id == vid)
        .one()
    )
    via.viability_score = 0.10
    via.live_eligible = False
    db.commit()

    uid = _uid(db, "junk")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="JUNK-USD",
        variant_id=vid,
        mode="live",
        state=STATE_WATCHING_LIVE,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_live_execution": {},
        },
    )
    db.commit()
    ad = FakeVenueAdapter("JUNK-USD", bid=99.95, ask=100.05, spread_bps=10.0)

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    assert out.get("ok") is True, out
    # Stays watching — the sub-floor score blocked the candidate transition.
    assert sess.state == STATE_WATCHING_LIVE, sess.state
    assert sess.state != STATE_LIVE_ENTERED
    # Safety property: a JUNK name NEVER placed an entry order.
    assert ad.place_market_calls == []
    assert ad.place_limit_calls == []
    le = _live_exec(dict(sess.risk_snapshot_json or {}))
    assert le.get("position") is None


# ── Scenario 4 (highest value): a REAL mover passes the gauntlet to LIVE_ENTERED


def test_real_mover_passes_full_gauntlet_to_entered(monkeypatch, db: Session) -> None:
    """OVER-RESTRICTION guard: a legitimate Ross mover (live-eligible, high score,
    tight valid quote) that has placed its entry order must reach ``live_entered``
    with a sized position and a structural stop BELOW entry — the stacked live vetoes
    do NOT choke a real mover. This is the highest-value scenario: it proves the
    safety stack is not so restrictive that nothing can ever trade."""
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="MOVR-USD")
    db.commit()

    # A REAL mover: high score, live-eligible (the seed already makes it eligible;
    # pin a high score so no soft midday/run-R bump can mask the assertion).
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "MOVR-USD", MomentumSymbolViability.variant_id == vid)
        .one()
    )
    via.viability_score = 0.92
    via.live_eligible = True
    db.commit()

    uid = _uid(db, "movr")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="MOVR-USD",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json=_entered_session_snapshot(entry_order_id="movr-entry-1"),
        correlation_id="c-movr",
    )
    db.commit()

    ad = FakeVenueAdapter("MOVR-USD", bid=99.95, ask=100.05, spread_bps=10.0)
    # The entry order is FILLED (the mover paid the offer): a clean full fill.
    ad.register_order(
        NormalizedOrder(
            order_id="movr-entry-1",
            client_order_id="cid-movr",
            product_id="MOVR-USD",
            side="buy",
            status="FILLED",
            order_type="limit",
            filled_size=0.25,
            average_filled_price=100.0,
        )
    )

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    # Reached LIVE_ENTERED end-to-end — NOT choked back to watching / error.
    assert sess.state == STATE_LIVE_ENTERED, (sess.state, out)
    le = _live_exec(dict(sess.risk_snapshot_json or {}))
    pos = le.get("position")
    assert isinstance(pos, dict), le
    # Sized correctly off the real fill (no oversize, exact mirror of the fill).
    assert pos["quantity"] == pytest.approx(0.25)
    assert pos["avg_entry_price"] == pytest.approx(100.0)
    # A protective structural stop was set BELOW entry (a real long is risk-bounded).
    assert pos.get("stop_price") is not None
    assert float(pos["stop_price"]) < float(pos["avg_entry_price"]), pos
    # Target above entry (Ross 2:1 geometry direction).
    assert pos.get("target_price") is not None
    assert float(pos["target_price"]) > float(pos["avg_entry_price"]), pos


# ── Scenario 2: STOP-OUT — protective exit fires, loss bounded by the stop ─────


def test_stop_out_fires_and_loss_is_bounded(monkeypatch, db: Session) -> None:
    """Enter -> bid drops to the protective stop -> the confirmed stop submits a
    market exit -> terminal ``live_exited``. The realized loss is BOUNDED by the
    structural stop distance (the sell pays the bid AT/below the stop, never a naked
    far-overshoot), and the exit DID place an order (risk was managed out)."""
    import app.services.trading.momentum_neural.live_runner as lr

    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    # The fake registers every place_limit_order_gtc SELL as instantly FILLED; the
    # resting first-target scale-out limit (placed at the entry fill) would therefore be
    # adopted on the next tick and HALVE the position before the stop fires. Disable it so
    # the REACTIVE stop path under test runs against the full position (mirrors the happy
    # macro file's pattern); the reactive market exit remains the path being exercised.
    monkeypatch.setattr(lr, "_place_scale_out_limit", lambda *a, **k: None)
    vid, _ = _seed_live_eligible_row(db, symbol="STOP-USD")
    db.commit()
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "STOP-USD", MomentumSymbolViability.variant_id == vid)
        .one()
    )
    via.viability_score = 0.92
    via.live_eligible = True
    db.commit()

    uid = _uid(db, "stop")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="STOP-USD",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json=_entered_session_snapshot(entry_order_id="stop-entry-1"),
        correlation_id="c-stop",
    )
    db.commit()

    ad = FakeVenueAdapter("STOP-USD", bid=99.95, ask=100.05, spread_bps=10.0)
    ad.register_order(
        NormalizedOrder(
            order_id="stop-entry-1",
            client_order_id="cid-stop",
            product_id="STOP-USD",
            side="buy",
            status="FILLED",
            order_type="limit",
            filled_size=0.25,
            average_filled_price=100.0,
        )
    )

    # Tick 1: fill -> LIVE_ENTERED with a stop set.
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_LIVE_ENTERED
    le = _live_exec(dict(sess.risk_snapshot_json or {}))
    stop_px = float(le["position"]["stop_price"])
    entry_px = float(le["position"]["avg_entry_price"])
    qty = float(le["position"]["quantity"])

    # Drop the bid TO the stop (breach). First breach tick only ARMS the flicker
    # guard (a single bad print does not sell); it returns stop_pending_confirm.
    ad.set_quote(bid=stop_px, ask=stop_px + 0.02)
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out2 = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    assert out2.get("stop_pending_confirm") is True, out2
    assert sess.state == STATE_LIVE_ENTERED  # not yet sold — confirm pending

    # Backdate the pending marker so the >=1s confirm window has elapsed, then re-tick:
    # the persisted breach now CONFIRMS and the protective market exit fires.
    snap = dict(sess.risk_snapshot_json or {})
    snap["momentum_live_execution"]["stop_breach_pending_utc"] = (
        datetime.utcnow() - timedelta(seconds=5)
    ).isoformat()
    sess.risk_snapshot_json = snap
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(sess, "risk_snapshot_json")
    db.commit()

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out3 = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    # Terminal: position flattened to LIVE_EXITED.
    assert sess.state == STATE_LIVE_EXITED, (sess.state, out3)
    le2 = _live_exec(dict(sess.risk_snapshot_json or {}))
    assert le2.get("position") is None
    # The protective exit DID place a SELL (risk managed out, not stranded). The lane's
    # exit ladder prices a MARKETABLE LIMIT at bid-guard on attempts<=2 (the naked market
    # is only the attempt-3+ floor), so a clean first-attempt stop-out flattens via
    # place_limit_order_gtc — assert the SELL leg regardless of which order type the ladder
    # chose. Both fake sell paths fill at the live bid, so the loss-bounded check is identical.
    _sell_calls = [c for c in (ad.place_market_calls + ad.place_limit_calls) if c.get("side") == "sell"]
    assert len(_sell_calls) >= 1
    _sell = _sell_calls[-1]
    assert _sell.get("side") == "sell"
    # Loss BOUNDED by the structural stop: the realized exit fill is at the bid
    # (== stop), so the loss per share is the stop distance, not a runaway overshoot.
    realized_loss_per_share = entry_px - stop_px
    assert realized_loss_per_share > 0
    # Booked exit basis reflects the stop fill price, never a far-below naked print.
    assert float(le2.get("last_exit_notional_basis_usd") or 0.0) == pytest.approx(entry_px * qty, rel=1e-3)
    # The realized return is negative but no worse than the stop distance in bps.
    _stop_bps = (entry_px - stop_px) / entry_px * 10_000.0
    assert float(le2.get("last_exit_return_bps")) == pytest.approx(-_stop_bps, rel=1e-3)


# ── Scenario 3: per-broker daily-loss breaker blocks ENTRY but never EXIT ──────


def test_daily_loss_breaker_blocks_entry_but_allows_exit(monkeypatch, db: Session) -> None:
    """A tripped per-broker daily-loss block must (a) report THIS broker blocked so
    new arming/entries are halted, while (b) NEVER halting an exit: the kill-switch
    'halts exits?' predicate is False for a daily-loss reason, so a held position can
    always be flattened. This is the load-bearing safety invariant: a breaker stops
    bleeding into NEW risk, it does not trap you in EXISTING risk."""
    monkeypatch.setattr(settings, "chili_per_broker_daily_loss_enabled", True)
    clear_stale_broker_daily_loss_blocks()

    fam = _normalize_real_family("coinbase_spot")
    assert fam in REAL_DAILY_LOSS_FAMILIES
    assert is_broker_daily_loss_blocked(fam) is False  # clean to start

    # Trip the per-broker block (simulating the cap breach the monitor would set).
    set_broker_daily_loss_block(fam, reason="broker_daily_loss_breach_coinbase_spot_usd_$300", realized=-305.0, limit=300.0)

    # (a) NEW entries for THIS broker are blocked...
    assert is_broker_daily_loss_blocked(fam) is True
    # ...but the OTHER broker is untouched (isolation — one breach does not freeze all).
    other = _normalize_real_family("robinhood_spot")
    assert other != fam
    assert is_broker_daily_loss_blocked(other) is False

    # (b) EXITS are NEVER halted by a daily-loss breach. A per-broker block never
    # touches the GLOBAL kill switch, so even with a global daily-loss reason set,
    # _kill_switch_halts_exits() returns False -> the live exit chokepoint flattens.
    import app.services.trading.governance as _gov
    with _gov._kill_switch_lock:
        _gov._kill_switch = True
        _gov._kill_switch_reason = "global_daily_loss_breach_pct_$250"
    try:
        assert _kill_switch_halts_exits() is False  # daily-loss => exits ALLOWED
    finally:
        with _gov._kill_switch_lock:
            _gov._kill_switch = False
            _gov._kill_switch_reason = None

    # And a MANUAL/emergency reason DOES halt exits (the predicate is reason-specific,
    # not blanket-permissive) — proves the daily-loss carve-out is intentional.
    with _gov._kill_switch_lock:
        _gov._kill_switch = True
        _gov._kill_switch_reason = "manual_operator_halt"
    try:
        assert _kill_switch_halts_exits() is True
    finally:
        with _gov._kill_switch_lock:
            _gov._kill_switch = False
            _gov._kill_switch_reason = None


def test_daily_loss_block_does_not_block_held_position_tick_exit(monkeypatch, db: Session) -> None:
    """End-to-end: a session HOLDING a position keeps managing toward its stop/target
    even while its broker is daily-loss-blocked. The per-broker block lives in the
    arming layer, NOT in the live-runner exit path — so a held LIVE_ENTERED session's
    stop still fires and flattens. (No exit may EVER be blocked by a loss cap.)"""
    import app.services.trading.momentum_neural.live_runner as lr

    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_per_broker_daily_loss_enabled", True)
    # Disable the resting first-target scale-out limit: the fake auto-FILLS every sell
    # limit, so the entry-time scale-out would be adopted next tick and HALVE the position
    # before the stop fires. The path under test is the held-position STOP exit on the full
    # position (mirrors the happy macro file's pattern).
    monkeypatch.setattr(lr, "_place_scale_out_limit", lambda *a, **k: None)
    vid, _ = _seed_live_eligible_row(db, symbol="HELD-USD")
    db.commit()
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "HELD-USD", MomentumSymbolViability.variant_id == vid)
        .one()
    )
    via.viability_score = 0.92
    via.live_eligible = True
    db.commit()

    uid = _uid(db, "held")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="HELD-USD",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        execution_family="coinbase_spot",
        venue="coinbase",
        risk_snapshot_json=_entered_session_snapshot(entry_order_id="held-entry-1"),
        correlation_id="c-held",
    )
    db.commit()

    ad = FakeVenueAdapter("HELD-USD", bid=99.95, ask=100.05, spread_bps=10.0)
    ad.register_order(
        NormalizedOrder(
            order_id="held-entry-1",
            client_order_id="cid-held",
            product_id="HELD-USD",
            side="buy",
            status="FILLED",
            order_type="limit",
            filled_size=0.25,
            average_filled_price=100.0,
        )
    )

    # Tick 1: enter.
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_LIVE_ENTERED
    le = _live_exec(dict(sess.risk_snapshot_json or {}))
    stop_px = float(le["position"]["stop_price"])

    # Now trip THIS broker's daily-loss block.
    set_broker_daily_loss_block(
        _normalize_real_family("coinbase_spot"),
        reason="broker_daily_loss_breach_coinbase_spot_usd_$300",
        realized=-305.0,
        limit=300.0,
    )
    assert is_broker_daily_loss_blocked("coinbase_spot") is True

    # Breach the stop and confirm — the exit must STILL flatten despite the block.
    ad.set_quote(bid=stop_px, ask=stop_px + 0.02)
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    # arm the flicker confirm
    snap = dict(sess.risk_snapshot_json or {})
    snap["momentum_live_execution"]["stop_breach_pending_utc"] = (
        datetime.utcnow() - timedelta(seconds=5)
    ).isoformat()
    sess.risk_snapshot_json = snap
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(sess, "risk_snapshot_json")
    db.commit()

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    # Exit fired -> flattened, EVEN THOUGH the broker is daily-loss blocked.
    assert sess.state == STATE_LIVE_EXITED, sess.state
    assert _live_exec(dict(sess.risk_snapshot_json or {})).get("position") is None
    # The exit was NOT blocked: a SELL leg was placed. The exit ladder prices a
    # marketable LIMIT on attempts<=2 (market is only the attempt-3+ floor), so accept
    # either order type — the point is that a protective sell DID go out.
    _sell_calls = [c for c in (ad.place_market_calls + ad.place_limit_calls) if c.get("side") == "sell"]
    assert len(_sell_calls) >= 1  # the exit was NOT blocked


# ── Scenario 5: duplicate / late fill reconciled to ONE position ──────────────


def test_duplicate_fill_event_reconciles_to_one_position(monkeypatch, db: Session) -> None:
    """A second tick that re-polls the SAME (already-adopted) entry order must NOT
    create a second position — once the fill is adopted the session advances to
    LIVE_ENTERED and the entry path is never re-run, so the quantity is the single
    real fill (no double-count, no naked orphan). [agentic_duplicate_fill_bug]"""
    import app.services.trading.momentum_neural.live_runner as lr

    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    # Isolate the duplicate-fill invariant from the exit machinery: the fake auto-FILLS
    # every resting sell limit, so the entry-time first-target scale-out would be adopted
    # on tick 2 and reduce the position — masking the no-double-count check. Disable it.
    monkeypatch.setattr(lr, "_place_scale_out_limit", lambda *a, **k: None)
    vid, _ = _seed_live_eligible_row(db, symbol="DUPE-USD")
    db.commit()
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "DUPE-USD", MomentumSymbolViability.variant_id == vid)
        .one()
    )
    via.viability_score = 0.92
    via.live_eligible = True
    db.commit()

    uid = _uid(db, "dupe")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="DUPE-USD",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json=_entered_session_snapshot(entry_order_id="dupe-entry-1"),
        correlation_id="c-dupe",
    )
    db.commit()

    ad = FakeVenueAdapter("DUPE-USD", bid=99.95, ask=100.05, spread_bps=10.0)
    # The SAME order id keeps reporting FILLED 809 @2.21 on every poll (the late/dup
    # broker event re-delivers the same fill — it must be counted ONCE).
    ad.register_order(
        NormalizedOrder(
            order_id="dupe-entry-1",
            client_order_id="cid-dupe",
            product_id="DUPE-USD",
            side="buy",
            status="FILLED",
            order_type="limit",
            filled_size=809.0,
            average_filled_price=2.21,
        )
    )
    # Park the quote in the HOLDING zone (just ABOVE the 2.21 entry, below the 2:1 target
    # and above the stop) so tick 2 neither targets nor stops out — the duplicate-fill
    # reconcile invariant is tested in isolation, not masked by a legitimate exit firing.
    ad.set_quote(bid=2.215, ask=2.225, spread_bps=45.0)

    # Tick 1: adopt the fill -> ONE position of 809.
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_LIVE_ENTERED
    le1 = _live_exec(dict(sess.risk_snapshot_json or {}))
    assert float(le1["position"]["quantity"]) == pytest.approx(809.0)
    qty_after_first = float(le1["position"]["quantity"])

    # Tick 2: the SAME fill is re-polled (duplicate/late event). The session is now
    # LIVE_ENTERED, so the entry/admission path is NOT re-run — the position quantity
    # is unchanged (no 2x double-count), and no second BUY order was placed.
    n_limit_before = len(ad.place_limit_calls)
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    le2 = _live_exec(dict(sess.risk_snapshot_json or {}))
    pos2 = le2.get("position")
    assert isinstance(pos2, dict), le2  # still ONE managed position (no naked orphan)
    assert float(pos2["quantity"]) == pytest.approx(qty_after_first)  # NOT doubled
    # No new BUY entry was submitted on the duplicate event.
    _new_buys = [c for c in ad.place_limit_calls[n_limit_before:] if c.get("side") == "buy"]
    assert _new_buys == []
