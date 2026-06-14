"""Decouple-watching concurrency: count helpers, the effective position cap, and
the flag-gated gate branching.

The feature splits live momentum sessions into $0-risk WATCHERS (pre-fill, governed
by a watch-fanout cap) and HELD positions (governed by the risk-budget cap). The
authoritative position cap is enforced atomically at the fill boundary (see
``test_concurrency_decouple_fill_burst.py``); these tests pin the building blocks:
the count helpers, the cap math, and that the master flag OFF is a no-op.
"""

from __future__ import annotations

from app import models
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural import risk_policy
from app.services.trading.momentum_neural.live_runner import cleanup_leaked_lane_locks
from app.services.trading.momentum_neural.risk_evaluator import (
    aggregate_open_crypto_risk_usd,
    count_inflight_entry_orders,
    count_open_positions,
)
from app.services.trading.momentum_neural.risk_policy import effective_position_cap


_VAR_FOR_USER: dict[int, int] = {}  # user_id -> variant_id (variant_id is NOT NULL)


def _mk_user(db, name):
    """User + a strategy variant (variant_id is NOT NULL on the session table)."""
    u = models.User(name=name)
    db.add(u)
    db.flush()
    v = MomentumStrategyVariant(
        family="dec", variant_key=f"dec_{name}", label="dec", params_json={}
    )
    db.add(v)
    db.flush()
    _VAR_FOR_USER[int(u.id)] = int(v.id)
    return u


def _sess(db, *, user_id, symbol, state, execution_family="coinbase_spot",
          entry_submitted=None, position=None):
    le: dict = {}
    if entry_submitted is not None:
        le["entry_submitted"] = entry_submitted
    if position is not None:
        le["position"] = position
    snap = {"momentum_live_execution": le} if le else {}
    s = TradingAutomationSession(
        user_id=user_id, symbol=symbol, mode="live", state=state,
        variant_id=_VAR_FOR_USER[int(user_id)],
        execution_family=execution_family, risk_snapshot_json=snap,
    )
    db.add(s)
    db.flush()
    return s


# ── count_open_positions ─────────────────────────────────────────────────────

def test_count_open_positions_counts_only_holding_states(db) -> None:
    u = _mk_user(db, "cop-states")
    for st in ("live_entered", "live_scaling_out", "live_trailing", "live_bailout"):
        _sess(db, user_id=u.id, symbol="AAA", state=st)
    # Pre-fill watchers + terminal states must NOT count.
    for st in ("watching_live", "live_pending_entry", "live_entry_candidate",
               "live_exited", "live_cooldown", "live_finished"):
        _sess(db, user_id=u.id, symbol="BBB", state=st)
    db.commit()
    assert count_open_positions(db, user_id=u.id, mode="live") == 4


def test_count_open_positions_excludes_alpaca_twin(db) -> None:
    u = _mk_user(db, "cop-twin")
    _sess(db, user_id=u.id, symbol="AAA", state="live_entered")
    _sess(db, user_id=u.id, symbol="AAA", state="live_entered", execution_family="alpaca_spot")
    db.commit()
    assert count_open_positions(db, user_id=u.id, mode="live") == 1


def test_count_open_positions_crypto_filter(db) -> None:
    u = _mk_user(db, "cop-crypto")
    _sess(db, user_id=u.id, symbol="BTC-USD", state="live_entered")
    _sess(db, user_id=u.id, symbol="ETH-USD", state="live_trailing")
    _sess(db, user_id=u.id, symbol="NVDA", state="live_entered")
    db.commit()
    assert count_open_positions(db, user_id=u.id, mode="live", crypto_only=True) == 2
    assert count_open_positions(db, user_id=u.id, mode="live", crypto_only=False) == 1
    assert count_open_positions(db, user_id=u.id, mode="live") == 3


# ── count_inflight_entry_orders ──────────────────────────────────────────────

def test_count_inflight_only_submitted_pending(db) -> None:
    u = _mk_user(db, "inflight")
    # Submitted, no fill yet -> in-flight (counts).
    _sess(db, user_id=u.id, symbol="AAA", state="live_pending_entry", entry_submitted=True)
    _sess(db, user_id=u.id, symbol="BBB", state="live_pending_entry", entry_submitted=True)
    # Pending but NOT yet submitted -> no live order -> does not count.
    _sess(db, user_id=u.id, symbol="CCC", state="live_pending_entry", entry_submitted=False)
    # Submitted AND already has a position -> it is a held state now, not in-flight.
    _sess(db, user_id=u.id, symbol="DDD", state="live_pending_entry",
          entry_submitted=True, position={"quantity": 1})
    # A held position is not 'pending_entry' -> never an in-flight order.
    _sess(db, user_id=u.id, symbol="EEE", state="live_entered", entry_submitted=True)
    db.commit()
    assert count_inflight_entry_orders(db, user_id=u.id) == 2


def test_count_inflight_excludes_self_and_twin(db) -> None:
    u = _mk_user(db, "inflight-excl")
    s_self = _sess(db, user_id=u.id, symbol="AAA", state="live_pending_entry", entry_submitted=True)
    _sess(db, user_id=u.id, symbol="BBB", state="live_pending_entry", entry_submitted=True)
    _sess(db, user_id=u.id, symbol="CCC", state="live_pending_entry",
          entry_submitted=True, execution_family="alpaca_spot")
    db.commit()
    # self excluded -> only BBB; twin (CCC) excluded regardless.
    assert count_inflight_entry_orders(db, user_id=u.id, exclude_session_id=s_self.id) == 1


def test_count_inflight_crypto_filter(db) -> None:
    u = _mk_user(db, "inflight-crypto")
    _sess(db, user_id=u.id, symbol="BTC-USD", state="live_pending_entry", entry_submitted=True)
    _sess(db, user_id=u.id, symbol="NVDA", state="live_pending_entry", entry_submitted=True)
    db.commit()
    assert count_inflight_entry_orders(db, user_id=u.id, crypto_only=True) == 1
    assert count_inflight_entry_orders(db, user_id=u.id, crypto_only=False) == 1


# ── aggregate_open_crypto_risk_usd ───────────────────────────────────────────

def test_aggregate_crypto_risk_sums_only_crypto_below_entry(db) -> None:
    u = _mk_user(db, "agg-crypto")
    v = MomentumStrategyVariant(family="c", variant_key="c_v", label="c", params_json={})
    db.add(v)
    db.flush()

    def _held(symbol, qty, entry, stop):
        s = TradingAutomationSession(
            user_id=u.id, symbol=symbol, mode="live", variant_id=v.id, state="live_entered",
            risk_snapshot_json={"momentum_live_execution": {"position": {
                "quantity": qty, "avg_entry_price": entry, "stop_price": stop}}},
        )
        db.add(s)
        db.flush()

    _held("BTC-USD", 1, 100.0, 90.0)     # $10 at risk
    _held("ETH-USD", 2, 50.0, 45.0)      # $10 at risk
    _held("SOL-USD", 1, 20.0, 20.0)      # breakeven -> 0
    _held("NVDA", 100, 10.0, 9.0)        # EQUITY excluded ($100 ignored)
    db.commit()
    total, rows = aggregate_open_crypto_risk_usd(db, user_id=u.id)
    assert abs(total - 20.0) < 1e-9
    assert sorted(r["symbol"] for r in rows) == ["BTC-USD", "ETH-USD"]


# ── effective_position_cap math ──────────────────────────────────────────────

def test_effective_position_cap_adaptive_binds(db, monkeypatch) -> None:
    # Adaptive N within [floor, ceiling] is the active value.
    monkeypatch.setattr(risk_policy, "adaptive_max_concurrent_live_sessions", lambda: 12)
    monkeypatch.setattr(risk_policy.settings, "chili_momentum_risk_max_concurrent_positions", 5, raising=False)
    monkeypatch.setattr(risk_policy.settings, "chili_momentum_max_open_positions_ceiling", 20, raising=False)
    assert effective_position_cap(crypto=False) == 12
    assert effective_position_cap(crypto=True) == 12


def test_effective_position_cap_ceiling_caps(db, monkeypatch) -> None:
    # A misconfigured fraction blows adaptive N up -> the operator ceiling catches it.
    monkeypatch.setattr(risk_policy, "adaptive_max_concurrent_live_sessions", lambda: 100)
    monkeypatch.setattr(risk_policy.settings, "chili_momentum_risk_max_concurrent_positions", 5, raising=False)
    monkeypatch.setattr(risk_policy.settings, "chili_momentum_max_open_positions_ceiling", 20, raising=False)
    assert effective_position_cap(crypto=False) == 20


def test_effective_position_cap_floor_holds(db, monkeypatch) -> None:
    # No equity -> adaptive returns its base; the fixed floor is the fallback.
    monkeypatch.setattr(risk_policy, "adaptive_max_concurrent_live_sessions", lambda: 2)
    monkeypatch.setattr(risk_policy.settings, "chili_momentum_risk_max_concurrent_positions", 5, raising=False)
    monkeypatch.setattr(risk_policy.settings, "chili_momentum_max_open_positions_ceiling", 20, raising=False)
    assert effective_position_cap(crypto=False) == 5  # max(2, 5) -> 5


# ── B2 crypto dollar cap: held + in-flight projection (the fill-boundary formula) ──

def test_crypto_dollar_projection_counts_held_plus_inflight(db) -> None:
    """The boundary charges open-crypto $ at-risk + a per-trade proxy for EACH
    in-flight crypto entry + this entry's planned risk. The held aggregate alone
    (which omits in-flight) would under-count under a fill-burst (B2 in-flight gap)."""
    u = _mk_user(db, "cdc")
    vid = _VAR_FOR_USER[int(u.id)]
    # One HELD crypto position: $10 at risk (entry 100, stop 90, qty 1).
    held = TradingAutomationSession(
        user_id=u.id, symbol="BTC-USD", mode="live", state="live_entered", variant_id=vid,
        execution_family="coinbase_spot",
        risk_snapshot_json={"momentum_live_execution": {"position": {
            "quantity": 1, "avg_entry_price": 100.0, "stop_price": 90.0}}},
    )
    db.add(held)
    # Two IN-FLIGHT crypto entries (submitted, no position yet) — invisible to the
    # held aggregate, but each can fill at any instant.
    _sess(db, user_id=u.id, symbol="ETH-USD", state="live_pending_entry", entry_submitted=True)
    _sess(db, user_id=u.id, symbol="SOL-USD", state="live_pending_entry", entry_submitted=True)
    db.commit()

    open_risk, _ = aggregate_open_crypto_risk_usd(db, user_id=u.id)
    inflight = count_inflight_entry_orders(db, user_id=u.id, crypto_only=True)
    planned = 5.0  # per-trade proxy this entry would charge
    projected = open_risk + inflight * planned + planned

    assert open_risk == 10.0          # held only
    assert inflight == 2              # the two submitted crypto entries
    assert projected == 25.0          # 10 + 2*5 + 5 — the value the boundary compares to cap_usd
    # A $20 cap would BLOCK (25 > 20); the held-only $10 would have wrongly ALLOWED.
    assert projected > 20.0 and open_risk + planned <= 20.0


def test_cleanup_leaked_lane_locks_noop_on_clean_db(db) -> None:
    """The orphan-lock janitor is a safe no-op when no lane lock is leaked."""
    assert cleanup_leaked_lane_locks(db) == 0


def test_effective_position_cap_zero_floor_falls_back_not_below_one(db, monkeypatch) -> None:
    # A 0/blank floor config is treated as unset (``or 5``), never as a literal 0 —
    # so the cap can never collapse to 0 and starve the lane. The max(1, ...) guard
    # is the final backstop if every input were somehow falsy.
    monkeypatch.setattr(risk_policy, "adaptive_max_concurrent_live_sessions", lambda: 0)
    monkeypatch.setattr(risk_policy.settings, "chili_momentum_risk_max_concurrent_positions", 0, raising=False)
    monkeypatch.setattr(risk_policy.settings, "chili_momentum_max_open_positions_ceiling", 20, raising=False)
    assert effective_position_cap(crypto=False) == 5  # 0 floor -> fallback 5; >= 1 always
