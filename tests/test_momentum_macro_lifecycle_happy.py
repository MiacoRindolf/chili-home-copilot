"""MACRO TIER — full live-session LIFECYCLE happy paths.

End-to-end scenarios driven through ``tick_live_session`` with a FakeVenueAdapter
and the conftest ``db`` fixture. These exercise the REAL FSM machinery, the REAL
entry-fill -> position-build (stop/target via ``stop_target_prices``), and the REAL
exit/scale-out chokepoints — the integration bugs no unit test can reach.

Pattern mirrors ``tests/test_momentum_live_runner.py``: a seeded live session via
``_seed_live_eligible_row`` + ``create_trading_automation_session``, an adapter
factory, the ``is_kill_switch_active`` patch, and step-by-step FSM-state asserts.

Each test asserts the SPECIFIC correct state + the SPECIFIC realized PnL / position
quantity at each step (never bare truthiness), so a subtly-wrong transition or a
wrong R:R / PnL arithmetic FAILS the test.

Scenarios
---------
1. clean mover: WATCHING_LIVE -> LIVE_ENTRY_CANDIDATE -> LIVE_PENDING_ENTRY ->
   (entry fill) LIVE_ENTERED -> (target bid) LIVE_TRAILING [early trail-arm] ->
   LIVE_SCALING_OUT -> ... a profitable round trip to a terminal state with the
   realized PnL recorded; every FSM transition + the entry order placed ONCE + the
   bracket stop/target intent written.
2. scale-out LADDER (the default E1 grid): the rung-1 partial banked + the balance
   stop at breakeven + the runner held (TRAILING), the rung-2 partial, then the
   final runner — every tranche derived from ``scale_grid_levels``.
3. clean stop/target round trip with the correct class-aware R:R (>= Ross's 2:1
   floor) baked into the bracket.

The runner advances ONE FSM state per tick. Since the EARLY TRAIL-ARM (5d8c207,
2026-06-30; ``chili_momentum_early_trail_arm_enabled``) a held position that is
already in profit above the trail-activation band arms TRAILING FIRST, and the
first-target scale-out fires from TRAILING on the NEXT tick (it is written to fire
from ENTERED *or* TRAILING, once). Every target step below therefore takes two ticks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import MomentumSymbolViability, TradingAutomationSession
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_ENTERED,
    STATE_LIVE_ENTRY_CANDIDATE,
    STATE_LIVE_EXITED,
    STATE_LIVE_PENDING_ENTRY,
    STATE_LIVE_SCALING_OUT,
    STATE_LIVE_TRAILING,
    STATE_WATCHING_LIVE,
)
from app.services.trading.momentum_neural.live_runner import tick_live_session
from app.services.trading.momentum_neural.paper_execution import (
    breakeven_stop_after_partial,
    class_aware_reward_risk,
    first_partial_target_r,
    scale_out_fraction,
    scale_out_quantity,
    stop_target_prices,
)
from app.services.trading.momentum_neural.persistence import create_trading_automation_session
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
from app.services.trading.venue.coinbase_spot import reset_duplicate_client_order_guard_for_tests
from app.services.trading.venue.protocol import (
    FreshnessMeta,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
)

from tests.test_momentum_paper_runner import _seed_live_eligible_row


# ── fixtures / helpers ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _venue_connected_by_default(monkeypatch, stable_non_alpaca_account_identity):
    """The #565 venue-connectivity preflight short-circuits the tick with
    ``venue_broker_not_connected`` whenever the broker isn't connected (ALWAYS, in
    the test env — no live creds). Default it CONNECTED so the lifecycle logic runs.

    ``stable_non_alpaca_account_identity`` (conftest) satisfies the fail-closed
    non-Alpaca account-identity fence (a4ef7a0, 2026-07-14) that otherwise
    quarantines EVERY ``coinbase_spot`` tick at ``tick_start`` with
    ``non_alpaca_account_identity_unfrozen`` before any FSM transition runs — the
    same bypass ``tests/test_momentum_live_runner.py`` uses. The dedicated
    account-rotation tests keep exercising the real verifier.

    ``runner_boundary_risk_ok`` is pinned ALLOWED for the same reason the sibling
    module pins it: aggregate-account admission (``aggregate_open_risk_cap``) now
    requires a live broker-equity source these legacy Coinbase fixtures do not
    provide, so the REAL gate fails ``transient`` on every tick — terminalizing a
    freshly-armed session (``live_cancelled``) and releasing a FILLED entry back
    to the sweep (``watching_live``) before any lifecycle mechanics run. Admission
    is independently covered by the fail-closed risk suites; this module exercises
    the FSM / fill / bracket / exit mechanics behind that boundary."""
    import app.services.trading.momentum_neural.live_runner as _lr

    monkeypatch.setattr(_lr, "_venue_broker_connected", lambda ef: True)
    monkeypatch.setattr(
        _lr,
        "runner_boundary_risk_ok",
        lambda *_args, **_kwargs: (True, {"allowed": True}),
    )


def _uid(db: Session, name_suffix: str) -> int:
    u = User(name=f"MacroLife_{name_suffix}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


def _fresh() -> FreshnessMeta:
    return FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=120.0)


class FakeVenueAdapter:
    """A deterministic, asset-aware fake VenueAdapter for lifecycle ticks.

    Quotes are mutable (``set_quote``) so a test can step price toward a target /
    stop. ``get_order`` is keyed on the order id so the entry order, exit order, and
    any resting scale-out limit each return their own (controllable) fill state.
    Entry/exit ``place_*`` return DISTINCT ids so polls never cross-resolve."""

    def __init__(self, product_id: str, *, base_increment: float = 0.001, base_min: float = 0.001):
        self.product_id = product_id
        self._base_increment = base_increment
        self._base_min = base_min
        # quote
        self._bid = 99.95
        self._ask = 100.05
        # per-order fill state: id -> NormalizedOrder
        self._orders: dict[str, NormalizedOrder] = {}
        # call counters (assert "placed once")
        self.entry_place_calls = 0
        self.exit_sell_calls = 0  # reactive exit SELLs (limit or market), unified
        self.limit_calls: list[dict[str, Any]] = []
        self.market_calls: list[dict[str, Any]] = []
        self._next_exit_n = 0

    # quote control
    def set_quote(self, bid: float, ask: float | None = None) -> None:
        self._bid = float(bid)
        self._ask = float(ask if ask is not None else bid + 0.10)

    def set_order(self, order_id: str, order: NormalizedOrder) -> None:
        self._orders[order_id] = order

    # ── VenueAdapter protocol ────────────────────────────────────────────────
    def is_enabled(self) -> bool:
        return True

    def get_best_bid_ask(self, _product_id=None):
        mid = (self._bid + self._ask) / 2.0
        spread_bps = (self._ask - self._bid) / mid * 10_000.0 if mid > 0 else 0.0
        return (
            NormalizedTicker(
                product_id=self.product_id,
                bid=self._bid,
                ask=self._ask,
                mid=mid,
                spread_bps=spread_bps,
                freshness=_fresh(),
            ),
            _fresh(),
        )

    def get_product(self, _product_id):
        prod = NormalizedProduct(
            product_id=self.product_id,
            base_currency=self.product_id.split("-", 1)[0],
            quote_currency="USD",
            status="online",
            trading_disabled=False,
            cancel_only=False,
            limit_only=False,
            post_only=False,
            auction_mode=False,
            base_increment=self._base_increment,
            base_min_size=self._base_min,
        )
        return (prod, _fresh())

    def get_order(self, oid):
        oid = str(oid)
        if oid in self._orders:
            return (self._orders[oid], _fresh())
        # default: an OPEN order (never auto-resolves a thing we didn't script)
        return (
            NormalizedOrder(
                order_id=oid,
                client_order_id="cid",
                product_id=self.product_id,
                side="buy",
                status="OPEN",
                order_type="limit",
                filled_size=0.0,
                average_filled_price=None,
            ),
            _fresh(),
        )

    def _alloc_exit_id(self, client_order_id):
        """A reactive exit SELL (marketable LIMIT on attempt<=2, MARKET on the floor).
        Allocate a fresh ord-exit-N so the poll resolves THIS leg, and count it."""
        self.exit_sell_calls += 1
        self._next_exit_n += 1
        return {
            "ok": True,
            "order_id": f"ord-exit-{self._next_exit_n}",
            "client_order_id": client_order_id,
        }

    def place_limit_order_gtc(self, **kwargs):
        self.limit_calls.append(kwargs)
        cid = kwargs.get("client_order_id")
        if kwargs.get("side") == "buy":
            self.entry_place_calls += 1
            return {"ok": True, "order_id": "ord-entry", "client_order_id": cid}
        # A sell LIMIT: in the exit-driving scenarios the resting scale-out limit is
        # patched to a no-op, so every sell-limit here is a reactive (marketable) exit.
        return self._alloc_exit_id(cid)

    def place_market_order(self, **kwargs):
        self.market_calls.append(kwargs)
        cid = kwargs.get("client_order_id")
        if kwargs.get("side") == "sell":
            return self._alloc_exit_id(cid)
        return {"ok": True, "order_id": "ord-mkt", "client_order_id": cid}

    def cancel_order(self, _oid):
        return {"ok": True, "raw": {}}

    def get_position_quantity(self, _product_id):
        # None => the broker-qty clamp is a no-op (the session's qty stands)
        return None


def _filled_buy(order_id: str, qty: float, avg: float, product_id: str) -> NormalizedOrder:
    return NormalizedOrder(
        order_id=order_id,
        client_order_id="cid-e",
        product_id=product_id,
        side="buy",
        status="FILLED",
        order_type="limit",
        filled_size=qty,
        average_filled_price=avg,
    )


def _filled_sell(order_id: str, qty: float, avg: float, product_id: str) -> NormalizedOrder:
    return NormalizedOrder(
        order_id=order_id,
        client_order_id="cid-x",
        product_id=product_id,
        side="sell",
        status="FILLED",
        order_type="market",
        filled_size=qty,
        average_filled_price=avg,
    )


def _seed_pending_submitted(
    db: Session,
    monkeypatch,
    symbol: str,
    *,
    atr_pct: float,
) -> TradingAutomationSession:
    """A LIVE_PENDING_ENTRY session with a submitted entry order ready to FILL.

    The fill qty/avg come from the FILLED ``ord-entry`` order each test stages on its
    adapter; ``entry_stop_atr_pct`` is pinned here so the fill-built stop/target are
    deterministic (= ``stop_target_prices(avg, atr_pct=atr_pct, ...)`` + family params)."""
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    uid = _uid(db, symbol.replace("-", "_"))
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        venue="coinbase",
        execution_family="coinbase_spot",
        symbol=symbol,
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True, "evaluated_at_utc": "2026-01-01T00:00:00+00:00"},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 100000, "max_hold_seconds": 36000},
            "momentum_live_execution": {
                "entry_submitted": True,
                "entry_order_id": "ord-entry",
                "entry_stop_atr_pct": atr_pct,
                "entry_slip_bps_ref": 0.0,
            },
        },
        correlation_id=f"macro-{symbol}",
    )
    db.commit()
    return sess


def _impulse_breakout_params() -> dict:
    """Resolve the impulse_breakout strategy params from the SAME source the runner
    uses (normalize_strategy_params), so the test's bracket recompute tracks any
    future param retune instead of hardcoding stop_atr_mult."""
    from app.services.trading.momentum_neural.strategy_params import normalize_strategy_params

    return normalize_strategy_params({}, family_id="impulse_breakout")


def _expected_stop_target(avg: float, atr_pct: float, symbol: str) -> tuple[float, float]:
    """Reproduce the fill-handler's bracket math from the SAME source helpers + params.

    The runner builds the bracket via ``stop_target_prices(avg, atr_pct=entry_stop_atr_pct,
    stop_atr_mult=params['stop_atr_mult'], target_atr_mult=params['target_atr_mult'],
    reward_risk=first_partial_target_r(symbol))``. We pull the SAME params so this is the
    EXACT bracket — a wrong build (or a R:R regression) diverges from this.

    [27b] 2026-09-10: the runner's first-target base moved from ``class_aware_reward_risk``
    (the PLAN R:R, 2.5) to ``first_partial_target_r`` (the measured first-partial level,
    0.8R). This helper mirrors the runner by contract, so it follows. Every symbol in this
    file is ``-USD``, where both resolve to the crypto override (3.0) — so the brackets
    asserted here are byte-identical; the change only matters if an equity case is added."""
    p = _impulse_breakout_params()
    return stop_target_prices(
        avg,
        atr_pct=atr_pct,
        side_long=True,
        stop_atr_mult=float(p["stop_atr_mult"]),
        target_atr_mult=float(p["target_atr_mult"]),
        reward_risk=first_partial_target_r(symbol),
    )


def _kill_switch_off():
    return patch(
        "app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
        return_value=False,
    )


# ── SCENARIO 1 — clean mover full lifecycle, profitable round trip ────────────


def test_full_lifecycle_clean_mover_profitable_round_trip(monkeypatch, db: Session) -> None:
    """WATCHING_LIVE -> LIVE_ENTRY_CANDIDATE -> LIVE_PENDING_ENTRY -> (fill)
    LIVE_ENTERED -> (target) LIVE_TRAILING [early trail-arm] -> LIVE_SCALING_OUT ->
    (confirm) profitable FULL exit, with each FSM transition asserted, NO new entry
    order placed on the already-submitted leg, and the bracket stop/target intent
    written onto the position.

    A single-share position (base_increment/min = 1.0) makes the first-target a SINGLE
    FLATTEN (``scale_out_quantity`` can_split=False — neither leg is independently
    sellable), so the target exit goes straight to a terminal LIVE_EXITED with the full
    realized PnL — fully deterministic (no trailing-runner tail to chase)."""
    import app.services.trading.momentum_neural.live_runner as lr

    symbol = "MAC1-USD"
    avg = 100.0
    qty = 1.0  # with base_increment=1.0 the 0.5 scale leg floors to 0 -> single flatten
    atr_pct = 0.02
    sess_pending = _seed_pending_submitted(db, monkeypatch, symbol, atr_pct=atr_pct)

    adapter = FakeVenueAdapter(symbol, base_increment=1.0, base_min=1.0)
    adapter.set_order("ord-entry", _filled_buy("ord-entry", qty, avg, symbol))
    monkeypatch.setattr(lr, "_place_scale_out_limit", lambda *a, **k: None)

    def factory():
        return adapter

    # ---- BACK UP one boundary: drive WATCHING -> CANDIDATE -> PENDING on a parallel
    # session so the early-FSM transitions are covered by REAL code. ----
    _drive_watching_to_pending(db, monkeypatch)

    # ---- tick 1: PENDING(submitted) + FILLED order -> ENTERED, bracket written ----
    with _kill_switch_off():
        r1 = tick_live_session(db, sess_pending.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess_pending)
    assert r1.get("ok") is True, r1
    assert sess_pending.state == STATE_LIVE_ENTERED
    assert adapter.entry_place_calls == 0  # already-submitted: no NEW entry placed

    le = (sess_pending.risk_snapshot_json or {})["momentum_live_execution"]
    pos = le["position"]
    assert pos is not None
    assert float(pos["quantity"]) == pytest.approx(qty)
    assert float(pos["avg_entry_price"]) == pytest.approx(avg)
    # Bracket stop/target intent written (the load-bearing safety contract).
    exp_stop, exp_target = _expected_stop_target(avg, atr_pct, symbol)
    assert float(pos["stop_price"]) == pytest.approx(exp_stop)
    assert float(pos["target_price"]) == pytest.approx(exp_target)
    assert exp_stop < avg < exp_target  # long bracket geometry

    # ---- tick 2: bid >= target -> TRAILING (the EARLY TRAIL-ARM runs before the
    # first-target block: a position already in profit above the trail-activation
    # band arms TRAILING first so the ride/add paths become reachable). Nothing sold. ----
    target_px = float(pos["target_price"])
    exit_fill = target_px * 1.01
    adapter.set_quote(bid=exit_fill, ask=target_px * 1.02)
    with _kill_switch_off():
        r2 = tick_live_session(db, sess_pending.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess_pending)
    assert r2.get("ok") is True, r2
    assert sess_pending.state == STATE_LIVE_TRAILING, sess_pending.state
    assert adapter.exit_sell_calls == 0  # arming the trail sells nothing

    # ---- tick 3: still at target from TRAILING -> SCALING_OUT (the reactive
    # first-target decision; fires from ENTERED or TRAILING, once) ----
    with _kill_switch_off():
        r3 = tick_live_session(db, sess_pending.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess_pending)
    assert r3.get("ok") is True, r3
    assert sess_pending.state == STATE_LIVE_SCALING_OUT, sess_pending.state
    assert adapter.exit_sell_calls == 0  # SCALING_OUT only sets state this tick

    # ---- tick 4: SCALING_OUT submits the FULL flatten sell; pre-stage it FILLED ----
    for n in range(1, 4):
        oid = f"ord-exit-{n}"
        adapter.set_order(oid, _filled_sell(oid, qty, exit_fill, symbol))
    with _kill_switch_off():
        r4 = tick_live_session(db, sess_pending.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess_pending)
    assert r4.get("ok") is True, r4
    assert sess_pending.state == STATE_LIVE_EXITED, sess_pending.state
    assert adapter.exit_sell_calls == 1  # ONE flatten sell submitted

    le = (sess_pending.risk_snapshot_json or {})["momentum_live_execution"]
    assert le.get("position") is None  # flattened
    assert le.get("last_exit_reason") == "target"
    # Profitable round trip: realized PnL equals (exit - entry) * qty net 0 fees.
    realized = float(le.get("realized_pnl_usd") or 0.0)
    assert realized > 0.0
    assert realized == pytest.approx((exit_fill - avg) * qty, rel=1e-6, abs=1e-6)


def _drive_watching_to_pending(db, monkeypatch) -> None:
    """Cover the early FSM transitions on REAL code, step by step:
    ARMED_PENDING_RUNNER -> QUEUED_LIVE -> WATCHING_LIVE -> LIVE_ENTRY_CANDIDATE ->
    LIVE_PENDING_ENTRY.

    ARMED->QUEUED, QUEUED->WATCHING, and CANDIDATE->PENDING are clean, deterministic
    source transitions (the runner advances them every tick). The WATCHING->CANDIDATE
    fire runs through dozens of entry-gate vetoes (OHLCV trigger + tape + L2 + …) that
    a happy-path macro test cannot drive reliably, so we step the FSM to CANDIDATE at
    that one boundary, then assert the REAL revalidation transition into PENDING."""
    symbol = "MACW-USD"
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    # Make the seeded viability comfortably pass the revalidate floor (0.48) so the
    # CANDIDATE -> PENDING transition is deterministic regardless of scorer drift.
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == symbol, MomentumSymbolViability.variant_id == vid)
        .one()
    )
    via.viability_score = 0.95
    via.live_eligible = True
    db.commit()

    uid = _uid(db, "watchpend")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        venue="coinbase",
        execution_family="coinbase_spot",
        symbol=symbol,
        variant_id=vid,
        mode="live",
        state="armed_pending_runner",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True, "evaluated_at_utc": "2026-01-01T00:00:00+00:00"},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 100000, "max_hold_seconds": 36000},
            "momentum_live_execution": {},
        },
        correlation_id="macro-watchpend",
    )
    db.commit()
    adapter = FakeVenueAdapter(symbol)

    def factory():
        return adapter

    # ARMED_PENDING_RUNNER -> QUEUED_LIVE (real transition).
    with _kill_switch_off():
        tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)
    assert sess.state == "queued_live", sess.state

    # QUEUED_LIVE -> WATCHING_LIVE (real transition).
    with _kill_switch_off():
        tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_WATCHING_LIVE, sess.state

    # WATCHING_LIVE -> LIVE_ENTRY_CANDIDATE: the entry-gate fire is out of macro scope;
    # step the FSM to the candidate boundary (test-data move), then exercise the REAL
    # CANDIDATE -> PENDING revalidation below.
    sess.state = STATE_LIVE_ENTRY_CANDIDATE
    db.commit()

    # LIVE_ENTRY_CANDIDATE -> LIVE_PENDING_ENTRY (real clean revalidation transition).
    with _kill_switch_off():
        r = tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)
    assert r.get("ok") is True, r
    assert sess.state == STATE_LIVE_PENDING_ENTRY, sess.state
    # Sanity: no entry order was placed yet (PENDING places on the NEXT tick).
    assert adapter.entry_place_calls == 0


# ── SCENARIO 2 — measured-move scale-out: partial banked + runner held ────────


def test_measured_move_scale_out_partial_then_runner(monkeypatch, db: Session) -> None:
    """A LARGE position so the first-target scale-out genuinely SPLITS.

    With the default multi-level scale grid (E1, ``chili_momentum_scale_grid_enabled``,
    ON in production since 2026-08-04) the Ross partial is a LADDER frozen at the first
    scale-out off the entry + the stop then in force: rung 1 banks ``grid[0].fraction``
    of the ORIGINAL size at the first target and ratchets the BALANCE stop to
    BREAKEVEN, holding the runner in TRAILING with ``partial_taken`` FALSE (rungs
    remain, the target re-armed at the next rung price); the LAST rung banks its
    tranche and finishes to the final runner (``partial_taken`` True). Assert the exact
    tranche qty, the exact remainder, the breakeven stop, the cumulative PnL and the
    rung bookkeeping at EVERY rung — all derived from the SAME source helpers
    (``scale_grid_levels`` + ``scale_out_quantity``), never hardcoded."""
    import app.services.trading.momentum_neural.live_runner as lr
    from app.services.trading.momentum_neural.paper_execution import scale_grid_levels

    symbol = "MAC2-USD"
    avg = 50.0
    qty = 100.0  # large -> scale_out_quantity splits cleanly at every rung
    atr_pct = 0.02
    base_inc = base_min = 0.001
    sess = _seed_pending_submitted(db, monkeypatch, symbol, atr_pct=atr_pct)
    adapter = FakeVenueAdapter(symbol, base_increment=base_inc, base_min=base_min)
    adapter.set_order("ord-entry", _filled_buy("ord-entry", qty, avg, symbol))
    # Disable the resting scale-out limit so the REACTIVE scale-out path runs.
    monkeypatch.setattr(lr, "_place_scale_out_limit", lambda *a, **k: None)

    def factory():
        return adapter

    def _tick():
        with _kill_switch_off():
            tick_live_session(db, sess.id, adapter_factory=factory)
        db.commit()
        db.refresh(sess)
        _le = (sess.risk_snapshot_json or {})["momentum_live_execution"]
        return _le, _le.get("position")

    # tick 1: fill -> ENTERED
    le, pos = _tick()
    assert sess.state == STATE_LIVE_ENTERED
    target_px = float(pos["target_price"])

    # tick 2: bid >= target -> TRAILING (early trail-arm; nothing sold)
    fill_px = target_px * 1.01
    adapter.set_quote(bid=fill_px, ask=target_px * 1.02)
    le, pos = _tick()
    assert sess.state == STATE_LIVE_TRAILING, sess.state
    assert adapter.exit_sell_calls == 0

    # tick 3: TRAILING at target -> SCALING_OUT (nothing sold yet)
    le, pos = _tick()
    assert sess.state == STATE_LIVE_SCALING_OUT, sess.state
    assert adapter.exit_sell_calls == 0

    # The ladder the runner freezes on its first scale-out, from the SAME source: the
    # entry + the stop in force at this moment (``scale_grid_anchor_stop``).
    anchor_stop = float(pos["stop_price"])
    grid = scale_grid_levels(avg, anchor_stop, side_long=True, symbol=symbol)
    assert len(grid) >= 2, grid  # the default ladder is in force — the point of this scenario
    # Runner stop ratchets to BREAKEVEN on the first rung (= entry; never loosened).
    exp_be = breakeven_stop_after_partial(avg, anchor_stop, side_long=True)
    assert exp_be == pytest.approx(avg)

    held = qty
    realized_total = 0.0
    sells = 0
    for rung, (rung_px, rung_frac) in enumerate(grid):
        last = rung == len(grid) - 1
        tranche_qty, remainder, can_split = scale_out_quantity(
            current_qty=held, original_qty=qty, fraction=float(rung_frac),
            base_increment=base_inc, base_min_size=base_min,
        )
        assert can_split is True and tranche_qty > 0 and remainder > 0
        if rung > 0:
            # Bid at/above the re-armed rung target -> SCALING_OUT again.
            rung_fill = max(fill_px, float(rung_px) * 1.01)
            adapter.set_quote(bid=rung_fill, ask=rung_fill * 1.01)
            le, pos = _tick()
            assert sess.state == STATE_LIVE_SCALING_OUT, (rung, sess.state)
            assert adapter.exit_sell_calls == sells
        else:
            rung_fill = fill_px
        # SCALING_OUT submits this rung's tranche sell; pre-stage it FILLED.
        for n in range(sells + 1, sells + 4):
            oid = f"ord-exit-{n}"
            adapter.set_order(oid, _filled_sell(oid, tranche_qty, rung_fill, symbol))
        le, pos = _tick()
        sells += 1
        held -= tranche_qty
        realized_total += (rung_fill - avg) * tranche_qty
        # Tranche banked + runner held: NOT flattened, NOT terminal, ONE sell per rung.
        assert sess.state == STATE_LIVE_TRAILING, (rung, sess.state)
        assert adapter.exit_sell_calls == sells
        assert pos is not None
        assert float(pos["quantity"]) == pytest.approx(held)
        assert float(pos["quantity"]) == pytest.approx(remainder)
        assert float(le.get("last_partial_exit_quantity") or 0.0) == pytest.approx(tranche_qty)
        # Rung bookkeeping: the frozen ladder, the rung pointer, the re-armed target.
        assert int(pos["scale_grid_idx"]) == rung + 1
        for (px, fr), (epx, efr) in zip(pos["scale_grid"], grid):
            assert float(px) == pytest.approx(epx) and float(fr) == pytest.approx(efr)
        if last:
            assert bool(pos.get("partial_taken")) is True  # the ladder is done -> final runner
        else:
            assert bool(pos.get("partial_taken")) is False  # rungs remain -> trigger re-armed
            assert float(pos["target_price"]) == pytest.approx(float(grid[rung + 1][0]))
        # Breakeven on the first rung; ratchet-only afterwards (INVARIANT-A, never loosened).
        if rung == 0:
            assert float(pos["stop_price"]) == pytest.approx(exp_be)
        else:
            assert float(pos["stop_price"]) >= exp_be - 1e-9
        # Cumulative banked PnL = sum over rungs of (fill - entry) * tranche (0 fees).
        realized = float(le.get("realized_pnl_usd") or 0.0)
        assert realized == pytest.approx(realized_total, rel=1e-6, abs=1e-6)
        assert realized > 0.0

    # The final runner is still live with the measured-move tail intact.
    assert held > 0.0
    assert float(pos["quantity"]) == pytest.approx(held)


# ── SCENARIO 3 — clean stop/target round trip with the correct class-aware R:R ─


def test_bracket_has_correct_reward_risk(monkeypatch, db: Session) -> None:
    """The bracket written at the entry fill must encode the class-aware reward:risk
    (Ross's >= 2:1 floor for equity; the wider crypto override on -USD names).

    Three assertions, all tied to the SOURCE math (no hardcoded numbers):
      (a) the EQUITY R:R is EXACTLY the configured ``chili_momentum_risk_reward_risk_ratio``
          (``class_aware_reward_risk`` on a bare ticker; 2.5 since PR #1271) and never
          below Ross's strict 2:1 floor — the contract this scenario protects;
      (b) the written stop/target are EXACTLY ``stop_target_prices(...)`` — the same
          helper the runner uses (a wrong stop/target build FAILS);
      (c) the UN-PULLED R:R target (avg + rr*risk) encodes EXACTLY the class-aware rr,
          and the actually-written target's reward-from-entry is in [1R, rr*R] — the
          first-scale round-number pull-in only ever PULLS the target IN toward 1R,
          never loosens it. The old ~1.3:1 target_atr/stop_atr bug pushes (b) AND (c)
          out of band -> FAIL."""
    # (a) The EQUITY R:R is EXACTLY the configured plan ratio (PR #1271 moved
    # ``chili_momentum_risk_reward_risk_ratio`` 2.0 -> 2.5) and NEVER below Ross's 2:1
    # floor (crypto takes a wider override). Read from settings so a future retune
    # moves this assertion with it instead of leaving a stale literal behind.
    _equity_rr = float(settings.chili_momentum_risk_reward_risk_ratio)
    assert class_aware_reward_risk("AAPL") == pytest.approx(_equity_rr)
    assert _equity_rr >= 2.0

    symbol = "MAC3-USD"
    avg = 80.0
    qty = 10.0
    atr_pct = 0.02
    sess = _seed_pending_submitted(db, monkeypatch, symbol, atr_pct=atr_pct)
    adapter = FakeVenueAdapter(symbol)
    adapter.set_order("ord-entry", _filled_buy("ord-entry", qty, avg, symbol))

    def factory():
        return adapter

    with _kill_switch_off():
        tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_LIVE_ENTERED

    le = (sess.risk_snapshot_json or {})["momentum_live_execution"]
    pos = le["position"]
    stop = float(pos["stop_price"])
    target = float(pos["target_price"])
    rr = class_aware_reward_risk(symbol)  # the bracket's actual class-aware R:R
    assert rr >= 2.0  # never below the Ross floor

    risk = avg - stop
    assert risk > 0.0
    assert stop < avg < target  # long bracket geometry

    # (a) EXACT reproduction from the SAME source helper (catches a wrong build).
    exp_stop, exp_target = _expected_stop_target(avg, atr_pct, symbol)
    assert stop == pytest.approx(exp_stop)
    assert target == pytest.approx(exp_target)

    # (b) the UN-pulled R:R target encodes EXACTLY the class-aware rr; the written
    # target's R is in [1R, rr*R] (the round-number first-scale only pulls IN, never
    # below the 1R floor).
    rr_target_unpulled = avg + rr * risk
    assert (rr_target_unpulled - avg) / risk == pytest.approx(rr, rel=1e-9)  # exactly rr
    written_reward_r = (target - avg) / risk
    assert 1.0 - 1e-9 <= written_reward_r <= rr + 1e-9
    # And the written target never exceeds the un-pulled rr target.
    assert target <= rr_target_unpulled + 1e-9


# ── SCENARIO 3b — clean STOP-OUT round trip records the (negative) realized PnL ─


def test_clean_stop_out_records_negative_pnl_and_terminal_state(monkeypatch, db: Session) -> None:
    """A held position whose bid breaks the stop must, after the >=1s flicker-confirm,
    submit the stop sell, fill, land in LIVE_EXITED, and record the correct NEGATIVE
    realized PnL = (stop_fill - entry) * qty. Drives the loss side of the round trip."""
    import app.services.trading.momentum_neural.live_runner as lr

    symbol = "MAC4-USD"
    avg = 100.0
    qty = 3.0
    atr_pct = 0.02
    sess = _seed_pending_submitted(db, monkeypatch, symbol, atr_pct=atr_pct)
    adapter = FakeVenueAdapter(symbol)
    adapter.set_order("ord-entry", _filled_buy("ord-entry", qty, avg, symbol))
    monkeypatch.setattr(lr, "_place_scale_out_limit", lambda *a, **k: None)

    def factory():
        return adapter

    # tick 1: fill -> ENTERED
    with _kill_switch_off():
        tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_LIVE_ENTERED
    pos = (sess.risk_snapshot_json or {})["momentum_live_execution"]["position"]
    stop_px = float(pos["stop_price"])

    # Breach the stop by a HAIR (stop_px * 0.999): below the protective stop but well
    # ABOVE the max-loss-circuit floor (avg - K*stop_distance), so the clean STOP path
    # fires — NOT the BAILOUT circuit (which is a different exit we don't test here).
    avg_minus = float(avg)
    stop_distance = avg_minus - stop_px
    k = float(getattr(settings, "chili_momentum_max_loss_risk_multiple", 2.0) or 2.0)
    circuit_floor = avg_minus - k * stop_distance
    breach_bid = stop_px * 0.999
    assert breach_bid <= stop_px  # breaches the stop
    assert breach_bid > circuit_floor  # but NOT the circuit floor -> clean stop, no bailout

    # tick 2: bid <= stop -> the flicker guard arms a pending-confirm (no sell yet).
    adapter.set_quote(bid=breach_bid, ask=stop_px * 1.0005)
    with _kill_switch_off():
        r2 = tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)
    assert r2.get("stop_pending_confirm") is True, r2
    assert sess.state == STATE_LIVE_ENTERED  # still held — one breach print is not a stop
    assert adapter.exit_sell_calls == 0

    # Back-date the pending-confirm marker so the >=1s confirm window has elapsed.
    from sqlalchemy.orm.attributes import flag_modified

    snap = dict(sess.risk_snapshot_json or {})
    snap["momentum_live_execution"]["stop_breach_pending_utc"] = "2026-01-01T00:00:00"
    sess.risk_snapshot_json = snap
    flag_modified(sess, "risk_snapshot_json")
    db.commit()

    # tick 3: breach persists past the confirm window -> submit the stop sell.
    stop_fill = breach_bid
    for n in range(1, 4):
        oid = f"ord-exit-{n}"
        adapter.set_order(oid, _filled_sell(oid, qty, stop_fill, symbol))
    with _kill_switch_off():
        tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)

    assert sess.state == STATE_LIVE_EXITED, sess.state
    assert adapter.exit_sell_calls >= 1
    le = (sess.risk_snapshot_json or {})["momentum_live_execution"]
    realized = float(le.get("realized_pnl_usd") or 0.0)
    assert realized < 0.0  # a stop-out is a loss
    assert realized == pytest.approx((stop_fill - avg) * qty, rel=1e-6, abs=1e-6)
    assert le.get("position") is None  # flattened
    assert le.get("last_exit_reason") == "stop"
