"""Autopilot P&L rollup (2026-06-12 money-first redesign).

The page used to sum totals client-side over a capped, archived-excluded
session list — the operator could not answer "what's my total PnL?" from it.
These tests pin the server-side truth: uncapped, archived included, per
symbol × bucket, with alpaca twin-soak (fake money) fenced off from live.
"""

from datetime import datetime, timedelta, timezone

from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
    TradingAutomationSimulatedFill,
)
from app.services.trading.momentum_neural.automation_query import automation_pnl_rollup


def _seed_user(db):
    from app.models.core import User

    u = User(name="rollup-op")
    db.add(u)
    db.flush()
    return int(u.id)


def _variant(db):
    v = MomentumStrategyVariant(family="ro", variant_key="ro_v", label="ro", params_json={})
    db.add(v)
    db.flush()
    return v


def _sess(db, uid, vid, *, symbol, mode, state, family="coinbase_spot", exec_state=None):
    key = "momentum_live_execution" if mode == "live" else "momentum_paper_execution"
    sess = TradingAutomationSession(
        user_id=uid, symbol=symbol, mode=mode, state=state,
        execution_family=family, variant_id=vid,
        risk_snapshot_json={key: (exec_state or {})},
    )
    db.add(sess)
    db.flush()
    return sess


def test_rollup_buckets_and_totals(db):
    uid = _seed_user(db)
    v = _variant(db)
    now = datetime.utcnow()

    # Paper: open position (floating +$10) + a closed trade today (+$2.46).
    paper = _sess(db, uid, v.id, symbol="FIDA-USD", mode="paper", state="entered", exec_state={
        "realized_pnl_usd": 2.46,
        "last_mid": 0.030,
        "last_tick_utc": now.replace(tzinfo=timezone.utc).isoformat(),
        "position": {"quantity": 1000.0, "entry_price": 0.020, "stop_price": 0.015},
    })
    db.add(TradingAutomationSimulatedFill(
        session_id=int(paper.id), ts=now, symbol="FIDA-USD", action="exit_long",
        fill_type="exit", quantity=100.0, price=0.025, pnl_usd=2.46, reason="target",
    ))

    # Live: open position with stop → at-risk computable.
    _sess(db, uid, v.id, symbol="ZRO-USD", mode="live", state="live_entered", family="coinbase_spot", exec_state={
        "realized_pnl_usd": 5.0,
        "last_mid": 2.10,
        "position": {"quantity": 10.0, "avg_entry_price": 2.00, "stop_price": 1.80},
    })

    # Live terminal outcome today (+$7) on another symbol.
    done = _sess(db, uid, v.id, symbol="KAIO-USD", mode="live", state="live_exited")
    db.add(MomentumAutomationOutcome(
        session_id=int(done.id), user_id=uid, variant_id=int(v.id), symbol="KAIO-USD",
        mode="live", execution_family="coinbase_spot", terminal_state="live_exited",
        terminal_at=now, outcome_class="target_hit", realized_pnl_usd=7.0,
    ))

    # Alpaca twin (live mode, fake money) — must land in its OWN bucket.
    _sess(db, uid, v.id, symbol="BTC-USD", mode="live", state="live_entered", family="alpaca_spot", exec_state={
        "realized_pnl_usd": 100.0,
        "last_mid": 101000.0,
        "position": {"quantity": 0.001, "avg_entry_price": 100000.0, "stop_price": 99000.0},
    })

    out = automation_pnl_rollup(db, user_id=uid)
    live, paper_b, alpaca = out["buckets"]["live"], out["buckets"]["paper"], out["buckets"]["alpaca"]

    # Paper: realized from today's fill, floating from the open position.
    assert paper_b["realized_usd"] == 2.46
    assert paper_b["floating_usd"] == 10.0  # (0.030-0.020)*1000
    assert paper_b["open_count"] == 1

    # Live: outcome (+7) + active runtime realized (+5); floating (2.10-2.00)*10 = 1.0.
    assert live["realized_usd"] == 12.0
    assert live["floating_usd"] == 1.0
    assert live["at_risk_usd"] == 2.0  # (2.00-1.80)*10
    assert live["at_risk_unknown_stops"] == 0

    # Alpaca fenced: its +$100 never leaks into live.
    assert alpaca["realized_usd"] == 100.0
    assert all(s["symbol"] == "BTC-USD" for s in alpaca["symbols"])
    assert "BTC-USD" not in [s["symbol"] for s in live["symbols"]]

    # Totals = floating + realized per bucket.
    assert live["total_usd"] == 13.0
    assert paper_b["total_usd"] == 12.46


def test_rollup_includes_archived_sessions_fills(db):
    uid = _seed_user(db)
    v = _variant(db)
    now = datetime.utcnow()
    arch = _sess(db, uid, v.id, symbol="INX-USD", mode="paper", state="archived")
    db.add(TradingAutomationSimulatedFill(
        session_id=int(arch.id), ts=now, symbol="INX-USD", action="exit_long",
        fill_type="exit", quantity=50.0, price=0.009, pnl_usd=-7.43, reason="stop",
    ))
    db.flush()
    out = automation_pnl_rollup(db, user_id=uid)
    assert out["buckets"]["paper"]["realized_usd"] == -7.43
    syms = {s["symbol"]: s for s in out["buckets"]["paper"]["symbols"]}
    assert syms["INX-USD"]["state"] == "FLAT"
    assert syms["INX-USD"]["losses"] == 1


def test_rollup_excludes_yesterday_fills(db):
    uid = _seed_user(db)
    v = _variant(db)
    old = datetime.utcnow() - timedelta(days=3)
    s = _sess(db, uid, v.id, symbol="OLD-USD", mode="paper", state="archived")
    db.add(TradingAutomationSimulatedFill(
        session_id=int(s.id), ts=old, symbol="OLD-USD", action="exit_long",
        fill_type="exit", quantity=1.0, price=1.0, pnl_usd=99.0, reason="target",
        created_at=old,
    ))
    db.flush()
    out = automation_pnl_rollup(db, user_id=uid)
    assert out["buckets"]["paper"]["realized_usd"] == 0.0
    # ...but it still shows up in the 7-day window ("since binuksan" context).
    assert out["buckets"]["paper"]["realized_7d_usd"] == 99.0


def test_rollup_unknown_stop_reported_not_hidden(db):
    uid = _seed_user(db)
    v = _variant(db)
    _sess(db, uid, v.id, symbol="NOSTOP-USD", mode="live", state="live_entered", exec_state={
        "last_mid": 1.0,
        "position": {"quantity": 10.0, "avg_entry_price": 1.0},
    })
    out = automation_pnl_rollup(db, user_id=uid)
    assert out["buckets"]["live"]["at_risk_unknown_stops"] == 1
    assert out["buckets"]["live"]["at_risk_usd"] == 0.0


def test_rollup_open_symbols_sort_first(db):
    uid = _seed_user(db)
    v = _variant(db)
    now = datetime.utcnow()
    flat = _sess(db, uid, v.id, symbol="FLAT-USD", mode="paper", state="exited")
    db.add(TradingAutomationSimulatedFill(
        session_id=int(flat.id), ts=now, symbol="FLAT-USD", action="exit_long",
        fill_type="exit", quantity=1.0, price=1.0, pnl_usd=50.0, reason="target",
    ))
    _sess(db, uid, v.id, symbol="OPEN-USD", mode="paper", state="entered", exec_state={
        "last_mid": 1.01,
        "position": {"quantity": 10.0, "entry_price": 1.00, "stop_price": 0.95},
    })
    out = automation_pnl_rollup(db, user_id=uid)
    symbols = [s["symbol"] for s in out["buckets"]["paper"]["symbols"]]
    assert symbols[0] == "OPEN-USD"  # open beats bigger flat realized


def test_paper_alpaca_sessions_count_once_in_paper_bucket(db):
    """Paper equities route to alpaca_spot (#649) but stay the paper SIMULATOR:
    money comes from fills only — no runtime double count, no lane bleed."""
    uid = _seed_user(db)
    v = _variant(db)
    now = datetime.utcnow()
    sess = _sess(db, uid, v.id, symbol="NVDA", mode="paper", state="entered",
                 family="alpaca_spot", exec_state={"realized_pnl_usd": 5.0, "last_mid": 100.0})
    db.add(TradingAutomationSimulatedFill(
        session_id=int(sess.id), ts=now, symbol="NVDA", action="exit_long",
        fill_type="exit", quantity=1.0, price=100.0, pnl_usd=5.0, reason="target",
    ))
    db.flush()
    out = automation_pnl_rollup(db, user_id=uid)
    assert out["buckets"]["paper"]["realized_usd"] == 5.0  # once, not 10
    assert out["buckets"]["alpaca"]["realized_usd"] == 0.0


def test_live_session_started_yesterday_not_in_today(db):
    """A 24/7 crypto session that banked before ET midnight must not put
    yesterday's money into the TODAY hero — but it stays in the 7d window."""
    uid = _seed_user(db)
    v = _variant(db)
    sess = _sess(db, uid, v.id, symbol="KAIO-USD", mode="live", state="live_watching",
                 exec_state={"realized_pnl_usd": 40.0})
    sess.started_at = datetime.utcnow() - timedelta(days=2)
    db.flush()
    out = automation_pnl_rollup(db, user_id=uid)
    assert out["buckets"]["live"]["realized_usd"] == 0.0
    assert out["buckets"]["live"]["realized_7d_usd"] == 40.0


def test_cancelled_pre_entry_outcomes_are_not_trades(db):
    uid = _seed_user(db)
    v = _variant(db)
    sess = _sess(db, uid, v.id, symbol="GHOST", mode="live", state="live_cancelled")
    db.add(MomentumAutomationOutcome(
        session_id=int(sess.id), user_id=uid, variant_id=int(v.id), symbol="GHOST",
        mode="live", execution_family="robinhood_spot", terminal_state="live_cancelled",
        terminal_at=datetime.utcnow(), outcome_class="cancelled_pre_entry",
        realized_pnl_usd=None,
    ))
    db.flush()
    out = automation_pnl_rollup(db, user_id=uid)
    assert out["buckets"]["live"]["trades"] == 0
    assert "GHOST" not in [s["symbol"] for s in out["buckets"]["live"]["symbols"]]


def test_breakeven_ratcheted_stop_is_known_zero_risk(db):
    uid = _seed_user(db)
    v = _variant(db)
    _sess(db, uid, v.id, symbol="SAFE-USD", mode="live", state="live_trailing", exec_state={
        "last_mid": 2.30,
        "position": {"quantity": 10.0, "avg_entry_price": 2.00, "stop_price": 2.10},
    })
    out = automation_pnl_rollup(db, user_id=uid)
    assert out["buckets"]["live"]["at_risk_unknown_stops"] == 0
    assert out["buckets"]["live"]["at_risk_usd"] == 0.0


def test_phantom_position_floating_suppressed(db):
    """A live session still tracking a position whose broker holding has EXITED
    (a recent CLOSED broker-synced trade + NO open one) must NOT contribute phantom
    floating — the TAO +$16.70 cockpit lie. A real holding (OPEN trade) keeps its
    floating. Suppression needs POSITIVE exit evidence, never mere absence."""
    from app.models.trading import Trade

    uid = _seed_user(db)
    v = _variant(db)
    now = datetime.utcnow()
    # PHANTOM: live session shows a position, but the broker-synced trade is CLOSED.
    _sess(db, uid, v.id, symbol="TAO-USD", mode="live", state="live_trailing",
          family="coinbase_spot", exec_state={
              "last_mid": 272.89,
              "position": {"quantity": 1.4021, "avg_entry_price": 260.98, "stop_price": 255.0},
          })
    db.add(Trade(user_id=uid, ticker="TAO-USD", status="closed",
                 entry_price=260.98, quantity=1.4021, exit_date=now))
    # REAL: live session with an OPEN broker-synced trade -> floating kept.
    _sess(db, uid, v.id, symbol="MEGA-USD", mode="live", state="live_trailing",
          family="coinbase_spot", exec_state={
              "last_mid": 0.0560,
              "position": {"quantity": 5000.0, "avg_entry_price": 0.0554, "stop_price": 0.050},
          })
    db.add(Trade(user_id=uid, ticker="MEGA-USD", status="open",
                 entry_price=0.0554, quantity=5000.0))
    db.flush()
    out = automation_pnl_rollup(db, user_id=uid)
    live = out["buckets"]["live"]
    syms = {s["symbol"]: s for s in live["symbols"]}
    # TAO phantom: floating suppressed + flagged.
    assert syms["TAO-USD"]["floating_usd"] == 0.0
    assert syms["TAO-USD"]["broker_unconfirmed"] is True
    # MEGA real: floating computed = (0.0560-0.0554)*5000 = 3.0, not flagged.
    assert round(syms["MEGA-USD"]["floating_usd"], 2) == 3.0
    assert syms["MEGA-USD"]["broker_unconfirmed"] is False
    # bucket floating excludes the phantom.
    assert round(live["floating_usd"], 2) == 3.0


def test_phantom_guard_needs_positive_exit_evidence(db):
    """A live position with NO broker-synced trade at all (e.g. a fresh fill whose
    synced trade row has not landed yet) must NOT be suppressed — absence of an open
    trade is not proof of exit. Guards against false-suppressing real money."""
    uid = _seed_user(db)
    v = _variant(db)
    _sess(db, uid, v.id, symbol="FRESH-USD", mode="live", state="live_entered",
          family="coinbase_spot", exec_state={
              "last_mid": 1.10,
              "position": {"quantity": 100.0, "avg_entry_price": 1.00, "stop_price": 0.90},
          })
    out = automation_pnl_rollup(db, user_id=uid)
    syms = {s["symbol"]: s for s in out["buckets"]["live"]["symbols"]}
    assert round(syms["FRESH-USD"]["floating_usd"], 2) == 10.0  # (1.10-1.00)*100, kept
    assert syms["FRESH-USD"]["broker_unconfirmed"] is False


def test_alpaca_twin_floating_never_suppressed(db):
    """Alpaca paper twins have no real broker holding by design — even with a
    closed trade lying around, their floating must NEVER be suppressed."""
    from app.models.trading import Trade

    uid = _seed_user(db)
    v = _variant(db)
    now = datetime.utcnow()
    _sess(db, uid, v.id, symbol="NVDA", mode="live", state="live_trailing",
          family="alpaca_spot", exec_state={
              "last_mid": 101.0,
              "position": {"quantity": 10.0, "avg_entry_price": 100.0, "stop_price": 95.0},
          })
    db.add(Trade(user_id=uid, ticker="NVDA", status="closed",
                 entry_price=100.0, quantity=10.0, exit_date=now))
    db.flush()
    out = automation_pnl_rollup(db, user_id=uid)
    syms = {s["symbol"]: s for s in out["buckets"]["alpaca"]["symbols"]}
    assert round(syms["NVDA"]["floating_usd"], 2) == 10.0  # (101-100)*10, NOT suppressed
    assert syms["NVDA"]["broker_unconfirmed"] is False
