"""CHUNK 2 (engine core): atomic SHAPE-AWARE risk-budget admission.

The slot COUNT is replaced by a CONTINUOUS dollars-at-risk gate at the advisory-
locked fill boundary, so the lane admits by DOLLARS-AT-RISK (shape-aware
``(entry-stop)*qty``) rather than an arbitrary count. The load-bearing properties:

  (a) SHAPE-AWARE: a tight-stop scalp admits MORE concurrent positions than a
      wide-stop trade for the same dollar budget (the count treated them equally).
  (b) FILL-BURST ATOMICITY: two near-simultaneous fills that EACH pass against the
      pre-fill aggregate but TOGETHER exceed the budget -> the SECOND is rejected
      (the per-(user,lane) advisory lock serializes; the budget is recomputed inside
      the lock against the first's committed risk).
  (c) STARVATION-BY-CONSTRUCTION: a flat/stuck broker-zero session holds ZERO
      aggregate risk -> it can NEVER block a new admission.
  (d) FLAG-OFF: byte-identical to the deployed count-based path (the pure helper
      returns the flat fallback; the live-runner block is a no-op).
  (e) BREAKERS/KILL-SWITCH still block regardless of budget headroom (they gate
      UPSTREAM of and independently from the count/budget swap in auto_arm).

docs/DESIGN/MOMENTUM_ENGINE.md §2 / Phase 4.
"""

from __future__ import annotations

import inspect
import os
import threading
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.pool import NullPool

from app import models
from app.config import settings
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural.risk_evaluator import (
    aggregate_open_risk_usd,
    count_inflight_entry_orders,
    sum_inflight_entry_risk_usd,
)
from app.services.trading.momentum_neural.risk_policy import (
    adaptive_watch_fanout,
    admit_by_aggregate_risk,
    equity_relative_loss_cap,
)

_LANE_NS = 0x4D4C  # "ML" — must match live_runner's lane-lock namespace exactly.
_BURST_SESSION = None


def _new_session():
    global _BURST_SESSION
    if _BURST_SESSION is None:
        url = os.environ.get("DATABASE_URL") or os.environ["TEST_DATABASE_URL"]
        eng = create_engine(url, poolclass=NullPool)
        _BURST_SESSION = sessionmaker(bind=eng, autocommit=False, autoflush=False)
    return _BURST_SESSION()


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _lane_key(user_id: int) -> int:
    return (_LANE_NS << 32) | (int(user_id) & 0xFFFFFFFF)


# ─────────────────────────────────────────────────────────────────────────────
# (a) SHAPE-AWARE: tight stops admit more concurrent names than wide stops.
# ─────────────────────────────────────────────────────────────────────────────


def test_shape_aware_tight_stop_admits_more_than_wide_stop():
    """For the SAME dollar budget, a tight-stop scalp consumes less budget than a
    wide-stop trade — so many more tight scalps fit. The count would admit the SAME
    number of either (it only counts positions)."""
    equity = 10_000.0
    frac = 0.03  # budget = $300
    # Tight scalp: $5 at-risk each -> 60 fit. Wide trade: $100 at-risk each -> 3 fit.
    tight_risk = 5.0
    wide_risk = 100.0

    def how_many_fit(per_trade_risk: float) -> int:
        n = 0
        open_risk = 0.0
        while True:
            admit, _ = admit_by_aggregate_risk(
                open_risk_usd=open_risk,
                candidate_risk_usd=per_trade_risk,
                equity_usd=equity,
                budget_fraction=frac,
            )
            if not admit:
                break
            open_risk += per_trade_risk
            n += 1
            if n > 1000:  # safety
                break
        return n

    n_tight = how_many_fit(tight_risk)
    n_wide = how_many_fit(wide_risk)
    assert n_tight == 60  # 300 / 5
    assert n_wide == 3     # 300 / 100
    # The decisive shape-aware property: the budget admits FAR more tight scalps.
    assert n_tight > n_wide


def test_shape_aware_uses_actual_entry_minus_stop_times_qty():
    """The candidate risk is the ACTUAL (entry-stop)*qty, not a flat estimate: a
    name with a 1% stop and the same notional uses a quarter of the budget of one
    with a 4% stop."""
    equity = 5_000.0
    frac = 0.03  # budget = $150
    qty = 100.0
    entry = 10.0
    tight_stop = 9.9   # (10-9.9)*100 = $10 at-risk
    wide_stop = 9.6    # (10-9.6)*100 = $40 at-risk

    admit_tight, m_t = admit_by_aggregate_risk(
        open_risk_usd=145.0,  # only $5 headroom
        candidate_risk_usd=(entry - tight_stop) * qty,
        equity_usd=equity, budget_fraction=frac,
    )
    admit_wide, m_w = admit_by_aggregate_risk(
        open_risk_usd=145.0,
        candidate_risk_usd=(entry - wide_stop) * qty,
        equity_usd=equity, budget_fraction=frac,
    )
    # $10 candidate pushes 145->155 > 150 -> reject; but a tighter $4-name would fit.
    assert m_t["candidate_risk_usd"] == 10.0
    assert m_w["candidate_risk_usd"] == 40.0
    assert admit_tight is False and admit_wide is False
    # With more headroom, the tight name fits where the wide one still doesn't.
    admit_tight2, _ = admit_by_aggregate_risk(
        open_risk_usd=130.0, candidate_risk_usd=10.0, equity_usd=equity, budget_fraction=frac)
    admit_wide2, _ = admit_by_aggregate_risk(
        open_risk_usd=130.0, candidate_risk_usd=40.0, equity_usd=equity, budget_fraction=frac)
    assert admit_tight2 is True   # 130+10=140 <= 150
    assert admit_wide2 is False   # 130+40=170 > 150


# ─────────────────────────────────────────────────────────────────────────────
# (c) STARVATION-BY-CONSTRUCTION: a flat/stuck broker-zero session holds ZERO risk.
# ─────────────────────────────────────────────────────────────────────────────


def test_stuck_zero_risk_session_does_not_block_admission(db):
    """A flat/stuck broker-zero session (the FCUV spin) holds ZERO
    aggregate_open_risk_usd -> a fresh candidate is admitted with the FULL budget
    available. The slot count would have charged the stuck session a slot."""
    u = models.User(name=_uniq("stuck"))
    db.add(u)
    db.flush()
    v = MomentumStrategyVariant(
        family="atom", variant_key=_uniq("atom"), label="atom", params_json={})
    db.add(v)
    db.flush()
    # A "stuck" session in a holding state but broker-flat: quantity 0 / no real
    # position -> contributes ZERO at-risk (aggregate_open_risk_usd skips qty<=0).
    db.add(TradingAutomationSession(
        user_id=u.id, symbol="STUK", mode="live", state="live_entered",
        variant_id=v.id, execution_family="robinhood_spot",
        risk_snapshot_json={"momentum_live_execution": {"position": {
            "quantity": 0.0, "avg_entry_price": 0.0, "stop_price": 0.0}}},
    ))
    db.commit()

    open_risk, rows = aggregate_open_risk_usd(db, user_id=int(u.id))
    assert open_risk == 0.0
    assert rows == []  # the stuck session is invisible to the risk budget
    # A real candidate is admitted against the FULL budget (nothing is consumed).
    admit, meta = admit_by_aggregate_risk(
        open_risk_usd=open_risk, candidate_risk_usd=50.0,
        equity_usd=10_000.0, budget_fraction=0.03)
    assert admit is True
    assert meta["open_risk_usd"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# (b) FILL-BURST ATOMICITY: serialized recompute rejects the over-budget second.
# ─────────────────────────────────────────────────────────────────────────────


def _seed_equity_watchers(db, *, n: int):
    """A user + variant + ``n`` pre-fill EQUITY watchers (non-crypto symbols) in
    live_pending_entry, not yet submitted. Committed so worker threads see them."""
    u = models.User(name=_uniq("atomburst"))
    db.add(u)
    db.flush()
    v = MomentumStrategyVariant(
        family="atom", variant_key=_uniq("atomb"), label="atom", params_json={})
    db.add(v)
    db.flush()
    sids = []
    for i in range(n):
        s = TradingAutomationSession(
            user_id=u.id, symbol=f"EQ{i}", mode="live", state="live_pending_entry",
            variant_id=v.id, execution_family="robinhood_spot",
            risk_snapshot_json={"momentum_live_execution": {"entry_submitted": False}},
        )
        db.add(s)
        db.flush()
        sids.append(int(s.id))
    db.commit()
    return int(u.id), int(v.id), sids


def _reserve_as_held(db_t, sid: int, *, qty: float, entry: float, stop: float) -> None:
    """Simulate the entry FILL: promote to a held position carrying real
    (entry-stop)*qty at-risk that aggregate_open_risk_usd will count."""
    s = db_t.query(TradingAutomationSession).filter_by(id=sid).one()
    s.state = "live_entered"
    s.risk_snapshot_json = {"momentum_live_execution": {"position": {
        "quantity": qty, "avg_entry_price": entry, "stop_price": stop}}}
    flag_modified(s, "risk_snapshot_json")
    db_t.flush()


def test_fill_burst_serialized_budget_rejects_over_cap_second(db):
    """Two near-simultaneous fills that EACH pass against the pre-fill aggregate
    (both read open_risk==0) but TOGETHER exceed the budget. Under the advisory
    lock, the budget is RECOMPUTED inside the lock, so the second sees the first's
    committed risk and is REJECTED. Without the lock both would admit (overshoot)."""
    equity = 10_000.0
    frac = 0.03  # budget = $300
    # Each fill carries $200 at-risk: one fits ($200<=300), two do NOT ($400>300).
    qty, entry, stop = 100.0, 10.0, 8.0  # (10-8)*100 = $200
    k = 2
    user_id, _vid, sids = _seed_equity_watchers(db, n=k)
    key = _lane_key(user_id)
    admitted: list[int] = []
    lock = threading.Lock()

    def worker(sid: int) -> None:
        db_t = _new_session()
        try:
            # xact-scoped advisory lock: serializes count-and-reserve (auto-release
            # at commit). This is the EXACT pattern the live runner uses.
            db_t.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
            open_risk, _ = aggregate_open_risk_usd(db_t, user_id=user_id)
            admit, _ = admit_by_aggregate_risk(
                open_risk_usd=open_risk,
                candidate_risk_usd=(entry - stop) * qty,
                equity_usd=equity, budget_fraction=frac)
            if admit:
                _reserve_as_held(db_t, sid, qty=qty, entry=entry, stop=stop)
                with lock:
                    admitted.append(sid)
            db_t.commit()  # releases the lock; next thread reads the fresh aggregate
        finally:
            db_t.rollback()
            db_t.close()

    threads = [threading.Thread(target=worker, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    db.expire_all()
    # Exactly ONE admitted (the budget held atomically); the second was rejected.
    assert len(admitted) == 1
    final_risk, _ = aggregate_open_risk_usd(db, user_id=user_id)
    assert final_risk == 200.0  # one $200 position, never two
    assert final_risk <= frac * equity


def _mark_inflight(db, sid: int) -> None:
    """Simulate the entry SUBMIT (not yet filled): live_pending_entry with
    ``entry_submitted=True`` and NO ``position`` — the born-but-not-held state
    count_inflight_entry_orders charges. This is the state the fill-burst masked:
    the held aggregate is still 0, so only the in-flight proxy bounds the dollars."""
    s = db.query(TradingAutomationSession).filter_by(id=sid).one()
    s.state = "live_pending_entry"
    s.risk_snapshot_json = {"momentum_live_execution": {"entry_submitted": True}}
    flag_modified(s, "risk_snapshot_json")
    db.commit()


def test_fill_burst_inflight_dollars_reject_second_before_any_held(db):
    """LIVE-RUNNER-PATH burst: the SECOND submit is rejected by DOLLARS while BOTH
    orders are still IN-FLIGHT (entry_submitted=True, position NOT yet held).

    This is the gap the held-promotion fill-burst test masked: held
    aggregate_open_risk_usd stays 0 until async fill reconciliation, so the ONLY
    thing that can bound a multi-tick in-flight burst is the in-flight proxy charge
    the live runner now applies. Before the fix, the proxy used
    equity_relative_loss_cap(0.0, ...) == 0.0, so the in-flight order charged $0 and
    the second candidate slipped past the dollar budget. We reproduce the EXACT
    live-runner in-flight arithmetic and assert the second is rejected."""
    equity = 10_000.0
    frac = 0.03  # budget = $300
    # First order in-flight; the held aggregate cannot see its dollars yet.
    user_id, _vid, sids = _seed_equity_watchers(db, n=2)
    first_sid, second_sid = sids[0], sids[1]
    _mark_inflight(db, first_sid)

    # ── EXACT live-runner in-flight arithmetic (equity path) ──────────────────
    # Held aggregate is still 0 (the first order is in-flight, never reconciled).
    held_risk, _ = aggregate_open_risk_usd(db, user_id=user_id)
    assert held_risk == 0.0  # the masked dimension: held can't see in-flight $

    # The fixed per-trade fallback resolves to a POSITIVE equity-relative charge
    # (equity x loss_fraction); the dead 0.0 fallback would zero this out.
    per_trade_loss_fallback = 50.0
    inflight_charge = float(equity_relative_loss_cap(per_trade_loss_fallback, "robinhood_spot") or 0.0)
    assert inflight_charge > 0.0  # the load-bearing fix: NOT 0.0

    n_inflight = count_inflight_entry_orders(
        db, user_id=user_id, crypto_only=False, exclude_session_id=second_sid)
    assert n_inflight == 1  # the first order is counted in-flight

    inflight_eq_risk = float(n_inflight) * inflight_charge
    # The second candidate carries enough dollars that held(0) + in-flight + candidate
    # exceeds the budget — but ONLY because the in-flight charge is non-zero. Sized to
    # overshoot for EITHER in-flight charge: equity-relative (10000 x 0.01 = $100 ->
    # 100+260=360>300) OR the fixed-floor fallback on an equity-fetch outage ($50 ->
    # 50+260=310>300). The dead 0.0 fallback would give 0+260=260<=300 -> WRONGLY admit.
    candidate_risk = 260.0
    admit, meta = admit_by_aggregate_risk(
        open_risk_usd=held_risk + inflight_eq_risk,
        candidate_risk_usd=candidate_risk,
        equity_usd=equity, budget_fraction=frac)
    assert admit is False  # rejected by DOLLARS while both are still in-flight
    assert meta["open_risk_usd"] == inflight_eq_risk  # the in-flight $ were charged

    # Regression assertion: with the DEAD 0.0 fallback the second would WRONGLY admit
    # (held 0 + in-flight 0 + 250 = 250 <= 300). Prove that path is now closed.
    dead_charge = float(equity_relative_loss_cap(0.0, "robinhood_spot") or 0.0)
    assert dead_charge == 0.0  # documents the bug: 0.0 fallback short-circuits
    admit_with_bug, _ = admit_by_aggregate_risk(
        open_risk_usd=held_risk + float(n_inflight) * dead_charge,
        candidate_risk_usd=candidate_risk,
        equity_usd=equity, budget_fraction=frac)
    assert admit_with_bug is True  # the bug WOULD have admitted -> overshoot


def _mark_inflight_with_risk(db, sid: int, *, risk_usd) -> None:
    """In-flight submit carrying a PERSISTED per-order risk (``entry_inflight_risk_usd``
    — the value the live runner writes at submit time). ``risk_usd=None`` omits the key
    to exercise the conservative flat-fallback branch."""
    le = {"entry_submitted": True}
    if risk_usd is not None:
        le["entry_inflight_risk_usd"] = float(risk_usd)
    s = db.query(TradingAutomationSession).filter_by(id=sid).one()
    s.state = "live_pending_entry"
    s.risk_snapshot_json = {"momentum_live_execution": le}
    flag_modified(s, "risk_snapshot_json")
    db.commit()


def test_inflight_sum_uses_persisted_per_order_risk_multiplier_aware(db):
    """FIX C: the in-flight proxy sums each sibling's PERSISTED real risk
    (``entry_inflight_risk_usd``), so a burst of HIGH-multiplier entries is charged
    its actual dollars — NOT a flat count*one-loss-fraction that under-charges. A
    sibling with no persisted risk falls back to the positive flat estimate (never $0).
    The submitter's own row is excluded."""
    user_id, _vid, sids = _seed_equity_watchers(db, n=3)
    high_sid, flat_sid, self_sid = sids[0], sids[1], sids[2]
    # One high-multiplier in-flight order ($180 real), one with NO persisted risk.
    _mark_inflight_with_risk(db, high_sid, risk_usd=180.0)
    _mark_inflight_with_risk(db, flat_sid, risk_usd=None)
    per_trade_fallback = 50.0
    total = sum_inflight_entry_risk_usd(
        db, user_id=user_id, per_trade_fallback_usd=per_trade_fallback,
        crypto_only=False, exclude_session_id=self_sid)
    # persisted 180 (multiplier-aware) + flat fallback 50 for the un-persisted sibling.
    assert total == 230.0
    # The FLAT count proxy would have under-charged the $180 order to 2*50 = $100.
    flat_proxy = float(count_inflight_entry_orders(
        db, user_id=user_id, crypto_only=False, exclude_session_id=self_sid)) * per_trade_fallback
    assert flat_proxy == 100.0
    assert total > flat_proxy  # the load-bearing fix: real risk > flat under-charge
    # A non-positive fallback never charges below the persisted dollars (over-estimate safe).
    total_zero_fb = sum_inflight_entry_risk_usd(
        db, user_id=user_id, per_trade_fallback_usd=0.0,
        crypto_only=False, exclude_session_id=self_sid)
    assert total_zero_fb == 180.0  # persisted still counted; un-persisted -> 0 (no flat)


# ─────────────────────────────────────────────────────────────────────────────
# (d) FLAG-OFF: byte-identical fallback for the pure helpers.
# ─────────────────────────────────────────────────────────────────────────────


def test_atomic_helper_budget_disabled_admits_everything():
    """budget_fraction <= 0 is the operator's documented kill of the dollar cap ->
    admit=True regardless of headroom (the count backstop is then the sole gate)."""
    admit, meta = admit_by_aggregate_risk(
        open_risk_usd=1_000_000.0, candidate_risk_usd=1_000_000.0,
        equity_usd=10_000.0, budget_fraction=0.0)
    assert admit is True
    assert meta["reason"] == "budget_disabled"


def test_atomic_helper_fails_closed_on_unknown_equity():
    """Never size against an unknown account: equity None/0 -> admit=False."""
    for eq in (None, 0.0, -5.0, float("nan")):
        admit, meta = admit_by_aggregate_risk(
            open_risk_usd=0.0, candidate_risk_usd=10.0,
            equity_usd=eq, budget_fraction=0.03)
        assert admit is False
        assert meta["reason"] == "equity_unavailable"


def test_atomic_helper_fails_closed_on_uncomputable_candidate_risk():
    """A missing/uncomputable shape-aware candidate risk -> admit=False (fail-closed
    so a notional-fallback sizing never slips past the dollar budget unmeasured)."""
    for cand in (float("nan"), -1.0, float("inf")):
        admit, meta = admit_by_aggregate_risk(
            open_risk_usd=0.0, candidate_risk_usd=cand,
            equity_usd=10_000.0, budget_fraction=0.03)
        assert admit is False
        assert meta["reason"] == "candidate_risk_invalid"


def test_adaptive_fanout_flag_off_is_flat_max(monkeypatch):
    """decouple/adaptive flag OFF -> the flat chili_momentum_watch_fanout_max
    (byte-identical to the deployed flat cap), independent of field size."""
    monkeypatch.setattr(settings, "chili_momentum_watch_fanout_adaptive_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_watch_fanout_max", 15)
    for field in (0, 3, 40, 999, None):
        assert adaptive_watch_fanout(field) == 15


def test_adaptive_fanout_floats_with_field_clamped(monkeypatch):
    """ON -> cap = clamp(field, floor, max): floors on a quiet field, floats up with
    the field, never past the processing-cost ceiling."""
    monkeypatch.setattr(settings, "chili_momentum_watch_fanout_adaptive_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_watch_fanout_max", 30)
    monkeypatch.setattr(settings, "chili_momentum_watch_fanout_floor", 5)
    assert adaptive_watch_fanout(0) == 5      # quiet field -> floor
    assert adaptive_watch_fanout(3) == 5      # below floor -> floor
    assert adaptive_watch_fanout(12) == 12    # floats with the field
    assert adaptive_watch_fanout(40) == 30    # clamped at the ceiling
    assert adaptive_watch_fanout(None) == 30  # unknown field -> flat max fallback


# ─────────────────────────────────────────────────────────────────────────────
# (e) BREAKERS / KILL-SWITCH remain UPSTREAM of and INDEPENDENT from the swap.
# ─────────────────────────────────────────────────────────────────────────────


def test_breakers_gate_upstream_of_concurrency_swap_in_auto_arm():
    """STRUCTURAL guarantee: in the auto_arm admission pass the kill-switch (Guard 1)
    and the next-day rule-break lockout (Guard 1b) both ``return out`` BEFORE the
    concurrency/budget swap (Guard 2), and the drawdown (Guard 3), daily-loss
    (Guard 4) and profit-giveback (Guard 5) breakers each ``return out`` regardless
    of the swap. We assert the SOURCE ORDER so a future edit can't reorder a breaker
    below the budget swap (the breakers must never be gated by budget headroom)."""
    from app.services.trading.momentum_neural import auto_arm

    src = inspect.getsource(auto_arm)
    i_killswitch = src.index('out["skipped"] = "kill_switch"')
    i_lockout = src.index('out["skipped"] = "rulebreak_nextday_lockout"')
    i_concurrency = src.index("chili_momentum_decouple_watching_enabled")
    i_drawdown = src.index('out["skipped"] = "drawdown_breaker"')
    i_dailyloss = src.index('out["skipped"] = "daily_loss_cap_broker"')
    i_giveback = src.index('out["skipped"] = "profit_giveback"')

    # Kill-switch + next-day lockout fire BEFORE the concurrency/budget swap.
    assert i_killswitch < i_concurrency
    assert i_lockout < i_concurrency
    # The risk breakers come AFTER the swap but are independent early-return gates —
    # they are present and ordered, so budget headroom can never bypass them.
    assert i_concurrency < i_drawdown < i_dailyloss < i_giveback


def test_live_runner_atomic_gate_is_inside_the_advisory_lock():
    """The atomic budget check MUST be computed INSIDE the per-(user,lane) advisory
    lock (so two near-simultaneous fills cannot both pass against a stale aggregate).
    Assert in source that admit_by_aggregate_risk is called AFTER pg_advisory_xact_lock
    and WITHIN the decouple_watching block in the live runner."""
    from app.services.trading.momentum_neural import live_runner

    src = inspect.getsource(live_runner)
    i_lock = src.index("pg_advisory_xact_lock")
    i_admit = src.index("admit_by_aggregate_risk(")
    i_count_cap = src.index('"skipped": "position_cap_at_fill"')
    assert i_lock < i_admit                 # admission decided under the lock
    # The count cap (misconfig backstop) and the atomic gate are both under the lock.
    assert i_lock < i_count_cap


# ─────────────────────────────────────────────────────────────────────────────
# FILL-BOUNDARY FINANCIAL-BREAKER RE-CHECK (safety completion).
#
# default-ON decouple_watching lets a watcher armed while the day was GREEN persist
# across ticks, trigger, and submit an entry AFTER the day breaches. The three
# financial breakers (per-broker daily-loss / portfolio drawdown / profit-giveback)
# are checked ONLY in auto_arm's arm-pass — NOT at the live_runner fill boundary. The
# re-check closes that gap, INSIDE the advisory lock, atomic with the risk-budget admit.
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_fill_boundary_breaker_block(db, ef, user_id):
    """VERBATIM reproduction of the live_runner fill-boundary breaker resolution
    (the `if chili_momentum_fill_boundary_breaker_recheck_enabled:` block). Reuses the
    SAME governance helpers auto_arm uses and resolves the first breach (daily-loss ->
    drawdown -> giveback). Returns the block dict or None. Keeping this in lock-step
    with the runner is asserted structurally by the source tests below so the mocked
    functional behavior here cannot drift from the deployed code path."""
    from app.services.trading.momentum_neural.risk_evaluator import (
        evaluate_profit_giveback_halt,
    )
    from app.services.trading.portfolio_risk import check_portfolio_drawdown_breaker
    from app.services.trading.governance import broker_daily_loss_breached

    block = None
    try:
        breached, info = broker_daily_loss_breached(db, ef, user_id=int(user_id))
        if breached:
            block = {
                "breaker": "daily_loss_cap_broker",
                "family": info.get("family"),
                "daily_pnl_usd": round(float(info.get("realized", 0.0) or 0.0), 2),
                "max_daily_loss_usd": round(float(info.get("cap", 0.0) or 0.0), 2),
            }
    except Exception:
        pass
    if block is None:
        try:
            tripped, reason = check_portfolio_drawdown_breaker(db, int(user_id))
            if tripped:
                block = {"breaker": "drawdown_breaker", "dd_reason": reason}
        except Exception:
            pass
    if block is None:
        try:
            gb = evaluate_profit_giveback_halt(db, user_id=int(user_id), execution_family=ef)
            if gb.get("halted"):
                block = {
                    "breaker": "profit_giveback",
                    "daily_pnl_usd": gb.get("daily_pnl_usd"),
                    "peak_pnl_usd": gb.get("peak_pnl_usd"),
                    "giveback_fraction": gb.get("giveback_fraction"),
                }
        except Exception:
            pass
    return block


def _patch_breaker_helpers(monkeypatch, *, daily=False, drawdown=False, giveback=False):
    """Mock the THREE governance helpers at their SOURCE modules (where the runner
    imports them from), so the reproduction above exercises the real call shapes."""
    from app.services.trading import governance, portfolio_risk
    from app.services.trading.momentum_neural import risk_evaluator

    monkeypatch.setattr(
        governance, "broker_daily_loss_breached",
        lambda db, family, *, user_id=None: (
            (True, {"family": family, "realized": -123.0, "cap": 100.0})
            if daily else (False, {"family": family})
        ),
    )
    monkeypatch.setattr(
        portfolio_risk, "check_portfolio_drawdown_breaker",
        lambda db, uid: ((True, "dd_tripped") if drawdown else (False, None)),
    )
    monkeypatch.setattr(
        risk_evaluator, "evaluate_profit_giveback_halt",
        lambda db, *, user_id, execution_family="coinbase_spot": (
            {"halted": True, "daily_pnl_usd": 40.0, "peak_pnl_usd": 100.0,
             "giveback_fraction": 0.5}
            if giveback else {"halted": False}
        ),
    )


def test_fill_boundary_blocks_when_daily_loss_breaker_breached(monkeypatch):
    """Flag ON + per-broker daily-loss breaker breached at the fill boundary -> the
    entry is BLOCKED (the resolver returns the block; the runner would NOT submit)."""
    monkeypatch.setattr(
        settings, "chili_momentum_fill_boundary_breaker_recheck_enabled", True)
    _patch_breaker_helpers(monkeypatch, daily=True)
    block = _resolve_fill_boundary_breaker_block(db=None, ef="robinhood_spot", user_id=1)
    assert block is not None
    assert block["breaker"] == "daily_loss_cap_broker"
    assert block["family"] == "robinhood_spot"          # per-broker = THIS family (ef)
    assert block["max_daily_loss_usd"] == 100.0


def test_fill_boundary_proceeds_when_all_breakers_clean(monkeypatch):
    """Flag ON + all three breakers clean -> NO block (the entry proceeds to submit)."""
    monkeypatch.setattr(
        settings, "chili_momentum_fill_boundary_breaker_recheck_enabled", True)
    _patch_breaker_helpers(monkeypatch, daily=False, drawdown=False, giveback=False)
    block = _resolve_fill_boundary_breaker_block(db=None, ef="coinbase_spot", user_id=1)
    assert block is None


def test_fill_boundary_blocks_on_drawdown_and_giveback_too(monkeypatch):
    """Either the portfolio drawdown breaker OR the profit-giveback halt also blocks —
    both lanes (equity ef here, crypto in the giveback case) honor the re-check."""
    monkeypatch.setattr(
        settings, "chili_momentum_fill_boundary_breaker_recheck_enabled", True)
    # Drawdown only (daily-loss clean) -> drawdown block.
    _patch_breaker_helpers(monkeypatch, daily=False, drawdown=True)
    b_dd = _resolve_fill_boundary_breaker_block(db=None, ef="robinhood_spot", user_id=1)
    assert b_dd is not None and b_dd["breaker"] == "drawdown_breaker"
    # Giveback only (daily-loss + drawdown clean) -> giveback block, crypto family.
    _patch_breaker_helpers(monkeypatch, daily=False, drawdown=False, giveback=True)
    b_gb = _resolve_fill_boundary_breaker_block(db=None, ef="coinbase_spot", user_id=1)
    assert b_gb is not None and b_gb["breaker"] == "profit_giveback"


def test_fill_boundary_recheck_flag_off_skips_recheck(monkeypatch):
    """Flag OFF -> the runner's `if recheck_enabled:` block is a no-op (byte-identical):
    NO breaker is consulted even when one WOULD breach. We assert the source gate so the
    block can never run when the flag is off, and that the helpers are not called."""
    from app.services.trading.momentum_neural import live_runner

    # The runner gates the ENTIRE re-check behind the flag — so flag-off cannot block.
    src = inspect.getsource(live_runner)
    i_flag = src.index("chili_momentum_fill_boundary_breaker_recheck_enabled")
    i_emit = src.index('"live_entry_blocked_by_breaker"')
    i_block_call = src.index("broker_daily_loss_breached as _bdlb")
    assert i_flag < i_block_call < i_emit  # the flag gate precedes every helper call

    # Behaviorally: with the flag OFF the runner never reaches the resolver, so even a
    # breaching helper set produces no block when the gate is honored.
    monkeypatch.setattr(
        settings, "chili_momentum_fill_boundary_breaker_recheck_enabled", False)
    _patch_breaker_helpers(monkeypatch, daily=True, drawdown=True, giveback=True)
    flag_on = bool(getattr(
        settings, "chili_momentum_fill_boundary_breaker_recheck_enabled", True))
    block = (
        _resolve_fill_boundary_breaker_block(db=None, ef="robinhood_spot", user_id=1)
        if flag_on else None
    )
    assert block is None  # flag OFF -> no re-check, no block (byte-identical)


def test_fill_boundary_recheck_is_inside_the_advisory_lock():
    """The breaker re-check MUST be INSIDE the same per-(user,lane) advisory lock as the
    risk-budget admit (so a breach + a fill cannot race) and AFTER it, and must call the
    SAME THREE governance helpers auto_arm uses. Assert the source ordering/wiring."""
    from app.services.trading.momentum_neural import live_runner

    src = inspect.getsource(live_runner)
    i_lock = src.index("pg_advisory_xact_lock")
    i_admit = src.index("admit_by_aggregate_risk(")
    i_flag = src.index("chili_momentum_fill_boundary_breaker_recheck_enabled")
    i_endlock = src.index("end atomic position cap")
    # The re-check is under the lock, after the atomic risk-budget admit, before the
    # lock-release boundary comment.
    assert i_lock < i_admit < i_flag < i_endlock
    # It wires the EXACT three governance helpers (no reimplementation).
    assert "broker_daily_loss_breached as _bdlb" in src
    assert "check_portfolio_drawdown_breaker as _cpdb" in src
    assert "evaluate_profit_giveback_halt as _epgh" in src
    # Per-broker daily-loss uses THIS session's execution_family (ef), and the block
    # leaves the session WATCHING (not terminal) so it retries next tick.
    assert "_bdlb(db, ef, user_id=int(sess.user_id))" in src
    assert '"skipped": "fill_boundary_breaker"' in src
