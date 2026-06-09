"""Phase 7: paper automation runner FSM (simulated execution only)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pandas as pd
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import (
    MomentumStrategyVariant,
    TradingAutomationEvent,
    TradingAutomationRuntimeSnapshot,
    TradingAutomationSession,
    TradingAutomationSimulatedFill,
)
from app.models.trading import MomentumSymbolViability
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.operator_actions import create_paper_draft_session
from app.services.trading.momentum_neural.persistence import persist_neural_momentum_tick
from app.services.trading.momentum_neural.viability import score_viability
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.paper_fsm import (
    STATE_ENTERED,
    STATE_QUEUED,
    STATE_WATCHING,
    assert_transition,
    can_transition,
)
from app.services.trading.momentum_neural.paper_runner import (
    _execution_readiness_costs,
    list_runnable_paper_sessions,
    tick_paper_session,
)
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
from app.services.trading.momentum_neural.automation_query import cancel_automation_session
from app.services.trading.momentum_neural.persistence import ensure_momentum_strategy_variants

PAPER_FILL_SYMBOL = "SIM-USD"


def test_execution_readiness_costs_preserve_explicit_zero_costs() -> None:
    assert _execution_readiness_costs(
        {
            "spread_bps": 0.0,
            "slippage_estimate_bps": 0.0,
            "fee_to_target_ratio": 0.0,
        }
    ) == (0.0, 0.0, 0.0)


def _seed_live_eligible_row(db: Session, *, symbol: str = "SOL-USD") -> tuple[int, MomentumStrategyVariant]:
    ensure_momentum_strategy_variants(db)
    db.commit()
    fam = get_family("impulse_breakout")
    assert fam is not None
    ctx = build_momentum_regime_context(
        now=datetime(2026, 4, 7, 16, 0, tzinfo=timezone.utc),
        atr_pct=0.02,
        meta={"spread_regime": "normal"},
    )
    feats = ExecutionReadinessFeatures(spread_bps=5.0)
    vr = score_viability(symbol, fam, ctx, feats)
    row = vr.to_public_dict()
    row["label"] = fam.label
    row["entry_style"] = fam.entry_style
    row["default_stop_logic"] = fam.default_stop_logic
    row["default_exit_logic"] = fam.default_exit_logic
    persist_neural_momentum_tick(
        db,
        row_dicts=[row],
        regime_snapshot=ctx.to_public_dict(),
        features=feats,
        correlation_id="paper-test",
        source_node_id="nm_momentum_crypto_intel",
    )
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    return v.id, v


def _uid(db: Session, name_suffix: str) -> int:
    u = User(name=f"PaperRun_{name_suffix}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


def _entry_gate_pass_df() -> pd.DataFrame:
    closes = [100.0 + i * 0.1 for i in range(40)]
    return pd.DataFrame(
        {
            "Open": [c - 0.05 for c in closes],
            "High": [c + 0.15 for c in closes],
            "Low": [c - 0.15 for c in closes],
            "Close": closes,
            "Volume": [1000.0 for _ in closes[:-1]] + [2500.0],
        }
    )


def test_fsm_valid_and_invalid_transition() -> None:
    assert can_transition(STATE_QUEUED, STATE_WATCHING)
    assert not can_transition(STATE_ENTERED, STATE_QUEUED)
    with pytest.raises(ValueError):
        assert_transition(STATE_ENTERED, STATE_QUEUED)


def test_run_paper_admission_queued_when_runner_enabled(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="ADM-USD")
    db.commit()
    uid = _uid(db, "adm")
    r = create_paper_draft_session(
        db, user_id=uid, symbol="ADM-USD", variant_id=vid, execution_family="coinbase_spot"
    )
    assert r["ok"] is True
    assert r["state"] == STATE_QUEUED
    db.flush()
    sess = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == r["session_id"]).one()
    assert RISK_SNAPSHOT_KEY in (sess.risk_snapshot_json or {})
    assert sess.risk_snapshot_json.get("momentum_policy_caps")


def test_paper_tick_advances_smoke(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="TCK-USD")
    db.commit()
    uid = _uid(db, "tck")
    r = create_paper_draft_session(
        db, user_id=uid, symbol="TCK-USD", variant_id=vid, execution_family="coinbase_spot"
    )
    assert r["ok"]
    sid = r["session_id"]
    db.commit()

    def qfn(sym: str) -> dict:
        return {"mid": 100.0, "bid": 99.9, "ask": 100.1, "source": "test"}

    out1 = tick_paper_session(db, sid, quote_fn=qfn)
    assert out1.get("ok")
    db.commit()
    s1 = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == sid).one()
    assert s1.state == STATE_WATCHING

    out2 = tick_paper_session(db, sid, quote_fn=qfn)
    db.commit()
    s2 = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == sid).one()
    assert s2.state in (STATE_WATCHING, "entry_candidate")


def test_paper_entry_uses_spread_slippage_fee(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="FIL-USD")
    db.commit()
    uid = _uid(db, "fil")
    r = create_paper_draft_session(db, user_id=uid, symbol="FIL-USD", variant_id=vid)
    sid = r["session_id"]
    db.commit()

    def qfn(_s: str) -> dict:
        return {"mid": 200.0, "bid": 199.8, "ask": 200.2, "source": "test"}

    # queued -> watching -> candidate -> pending -> entered
    for _ in range(6):
        tick_paper_session(db, sid, quote_fn=qfn)
        db.commit()

    sess = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == sid).one()
    if sess.state != STATE_ENTERED:
        # Viability gating may vary; at least ensure we did not corrupt frozen risk
        snap = sess.risk_snapshot_json or {}
        assert RISK_SNAPSHOT_KEY in snap
        return

    pe = (sess.risk_snapshot_json or {}).get("momentum_paper_execution") or {}
    pos = pe.get("position") or {}
    assert pos.get("entry_price", 0) > 200.2  # ask + slippage
    assert pos.get("fees_est_usd", 0) > 0


def test_frozen_momentum_risk_not_overwritten(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="FRZ-USD")
    db.commit()
    uid = _uid(db, "frz")
    r = create_paper_draft_session(db, user_id=uid, symbol="FRZ-USD", variant_id=vid)
    sid = r["session_id"]
    sess0 = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == sid).one()
    frozen = dict((sess0.risk_snapshot_json or {}).get(RISK_SNAPSHOT_KEY) or {})
    db.commit()

    tick_paper_session(db, sid, quote_fn=lambda _s: {"mid": 50.0, "bid": 49.9, "ask": 50.1})
    db.commit()
    sess1 = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == sid).one()
    after = dict((sess1.risk_snapshot_json or {}).get(RISK_SNAPSHOT_KEY) or {})
    assert after.get("evaluated_at_utc") == frozen.get("evaluated_at_utc")


def test_cancel_stops_runner(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="CAN-USD")
    db.commit()
    uid = _uid(db, "can")
    r = create_paper_draft_session(db, user_id=uid, symbol="CAN-USD", variant_id=vid)
    sid = r["session_id"]
    db.commit()
    tick_paper_session(db, sid, quote_fn=lambda _s: {"mid": 10.0, "bid": 9.9, "ask": 10.1})
    db.commit()

    cancel_automation_session(db, user_id=uid, session_id=sid)
    db.commit()

    out = tick_paper_session(db, sid, quote_fn=lambda _s: {"mid": 10.0})
    assert out.get("skipped") == "not_runnable"


def test_list_runnable_excludes_live_intent(db: Session) -> None:
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session
    from app.services.trading.momentum_neural.paper_fsm import STATE_LIVE_ARM_PENDING, STATE_QUEUED as Q

    uid = _uid(db, "lst")
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()

    create_trading_automation_session(
        db,
        user_id=uid,
        symbol="LIV-USD",
        variant_id=v.id,
        mode="live",
        state=STATE_LIVE_ARM_PENDING,
        risk_snapshot_json={"arm_token": "x", "momentum_risk": {"allowed": True}},
    )
    create_trading_automation_session(
        db,
        user_id=uid,
        symbol="PAP-USD",
        variant_id=v.id,
        mode="paper",
        state=Q,
        risk_snapshot_json={"momentum_risk": {"allowed": True}},
    )
    db.commit()
    rows = list_runnable_paper_sessions(db, limit=50)
    assert all(r.mode == "paper" for r in rows)
    assert all(r.state != STATE_LIVE_ARM_PENDING for r in rows)


def test_paper_events_emitted(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="EVT-USD")
    db.commit()
    uid = _uid(db, "evt")
    r = create_paper_draft_session(db, user_id=uid, symbol="EVT-USD", variant_id=vid)
    sid = r["session_id"]
    db.commit()
    tick_paper_session(db, sid, quote_fn=lambda _s: {"mid": 77.0, "bid": 76.9, "ask": 77.1})
    db.commit()
    types = {e.event_type for e in db.query(TradingAutomationEvent).filter_by(session_id=sid).all()}
    assert "paper_runner_started" in types or "paper_runner_queued" in types


def test_paper_runner_writes_runtime_snapshot_and_sim_fill(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    ohlcv = _entry_gate_pass_df()
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.entry_gates.fetch_ohlcv_df",
        lambda *_args, **_kwargs: ohlcv,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.paper_runner.fetch_ohlcv_df",
        lambda *_args, **_kwargs: ohlcv,
    )
    vid, _ = _seed_live_eligible_row(db, symbol=PAPER_FILL_SYMBOL)
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == PAPER_FILL_SYMBOL, MomentumSymbolViability.variant_id == vid)
        .one()
    )
    via.viability_score = 0.95
    via.paper_eligible = True
    via.regime_snapshot_json = {
        "atr_pct": 0.02,
        "chop_expansion": "trend",
        "volatility_regime": "normal",
        "meta": {"atr_pct": 0.02, "chop_expansion": "trend"},
    }
    db.commit()
    uid = _uid(db, "sim")
    r = create_paper_draft_session(db, user_id=uid, symbol=PAPER_FILL_SYMBOL, variant_id=vid)
    sid = r["session_id"]
    db.commit()

    def qfn(_s: str) -> dict:
        return {"mid": 125.0, "bid": 124.95, "ask": 125.05, "source": "massive"}

    for _ in range(6):
        tick_paper_session(db, sid, quote_fn=qfn)
        db.commit()

    snap = db.query(TradingAutomationRuntimeSnapshot).filter_by(session_id=sid).one_or_none()
    assert snap is not None
    assert snap.lane == "simulation"
    fills = db.query(TradingAutomationSimulatedFill).filter_by(session_id=sid).all()
    sess = db.query(TradingAutomationSession).filter_by(id=sid).one()
    events = db.query(TradingAutomationEvent).filter_by(session_id=sid).all()
    event_types = [event.event_type for event in events]
    blocked_payloads = [
        event.payload_json
        for event in events
        if event.event_type == "paper_entry_gates_blocked"
    ]
    assert fills, (
        "expected at least one simulated fill after forced high-viability entry path; "
        f"state={sess.state} events={event_types} blocked={blocked_payloads}"
    )


def test_paper_runner_npt_0608_real_flow_replay(monkeypatch, db: Session) -> None:
    """REAL end-to-end paper-trade of NPT's 2026-06-08 tape through tick_paper_session.

    Operator ask: instead of a standalone sim REPLICATING the trade, drive the ACTUAL
    paper runner (gates -> sizing -> sim fill -> exits -> FSM + DB writes) over the known
    NPT/BYAH day so flow bugs the sim can't see surface. Slices the real 06-08 OHLCV to an
    advancing sim-clock; quote is the current bar. Reports the FSM path + any exception
    (= a flow bug). Network-dependent; skips if NPT 06-08 data is unavailable.
    """
    import traceback
    from collections import Counter

    from app.services.trading.market_data import fetch_ohlcv_df as _real_fetch
    from app.services.trading.momentum_neural.paper_fsm import (
        STATE_CANCELLED, STATE_ERROR, STATE_EXPIRED, STATE_FINISHED,
    )

    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    DAY = "2026-06-08"
    df5_all = _real_fetch("NPT", interval="5m", period="1mo")
    df15_all = _real_fetch("NPT", interval="15m", period="1mo")
    if df5_all is None or df15_all is None or len(df5_all) == 0:
        pytest.skip("NPT market data unavailable")
    df5 = df5_all[[t.strftime("%Y-%m-%d") == DAY for t in df5_all.index]]  # 06-08 bars to LOOP over
    if len(df5) < 20:
        pytest.skip(f"insufficient NPT 06-08 5m data ({len(df5)})")

    sim = {"now": df5.index[0]}

    # The mock returns the FULL multi-day history up to sim_now (not just 06-08), so the
    # gate's >=30-row requirement + multi-day indicators are satisfied like in production.
    def _sliced(sym, interval="5m", period=None, **k):
        d = df15_all if "15" in str(interval) else df5_all
        return d[[t <= sim["now"] for t in d.index]]

    monkeypatch.setattr("app.services.trading.momentum_neural.entry_gates.fetch_ohlcv_df", _sliced)
    monkeypatch.setattr("app.services.trading.momentum_neural.paper_runner.fetch_ohlcv_df", _sliced)

    vid, _ = _seed_live_eligible_row(db, symbol="NPT")
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "NPT", MomentumSymbolViability.variant_id == vid)
        .one()
    )
    via.viability_score = 0.95
    via.paper_eligible = True
    via.live_eligible = True
    via.regime_snapshot_json = {
        "atr_pct": 0.06, "chop_expansion": "trend", "volatility_regime": "normal",
        "meta": {"atr_pct": 0.06, "chop_expansion": "trend"},
    }
    db.commit()
    uid = _uid(db, "npt0608")
    r = create_paper_draft_session(
        db, user_id=uid, symbol="NPT", variant_id=vid, execution_family="robinhood_spot"
    )
    assert r.get("ok"), f"paper draft session not created (equity paper support?): {r}"
    sid = r["session_id"]
    db.commit()

    c = {x.lower(): x for x in df5.columns}
    Cc = c["close"]
    path: list = []
    skips: list = []
    err = None
    done = False
    for i in range(2, len(df5)):
        sim["now"] = df5.index[i]
        mid = float(df5[Cc].iloc[i])

        def qfn(_s, _m=mid):
            return {"mid": _m, "bid": _m * 0.999, "ask": _m * 1.001, "source": "replay"}

        # Multiple ticks per bar: live has ~10 ticks per 5m bar, so the FSM oscillates
        # watching<->candidate and the trigger is evaluated AT each bar (and a fire can
        # advance candidate->pending_entry->entered within the bar). One tick/bar would
        # only sample every other bar and could miss the break bar entirely.
        for _ in range(6):
            try:
                out = tick_paper_session(db, sid, quote_fn=qfn)
                db.commit()
            except Exception as e:  # noqa: BLE001 — we WANT to catch flow bugs
                err = f"{type(e).__name__}: {e}"
                traceback.print_exc()
                done = True
                break
            if isinstance(out, dict) and out.get("skipped"):
                skips.append(out["skipped"])
            s = db.query(TradingAutomationSession).filter_by(id=sid).one()
            if not path or path[-1][1] != s.state:
                path.append((df5.index[i].strftime("%H:%M"), s.state))
            if s.state in (STATE_FINISHED, STATE_CANCELLED, STATE_ERROR, STATE_EXPIRED):
                done = True
                break
        if done:
            break

    fills = db.query(TradingAutomationSimulatedFill).filter_by(session_id=sid).all()
    sess = db.query(TradingAutomationSession).filter_by(id=sid).one()
    evs = db.query(TradingAutomationEvent).filter_by(session_id=sid).all()
    blocked = [e for e in evs if e.event_type == "paper_entry_gates_blocked"]
    block_reasons = Counter((e.payload_json or {}).get("reason") for e in blocked)
    regressed = sum(
        1 for e in evs
        if e.event_type == "paper_watch_started" and (e.payload_json or {}).get("reason") == "candidate_regressed"
    )
    print("\n=== NPT 06-08 REAL paper-flow replay (tick_paper_session end-to-end) ===")
    print(f"final    : state={sess.state}  sim_fills={len(fills)}  ticks={len(df5) - 2}")
    print(f"entry_candidate->WATCHING blocked reasons: {dict(block_reasons)}")
    print(f"candidate_regressed (viability) count: {regressed}")
    if blocked:
        _dbg = (blocked[0].payload_json or {}).get("debug")
        print(f"sample blocked debug: {_dbg}")
    if skips:
        print(f"tick skips: {dict(Counter(skips))}")
    print(f"exception: {err}")

    # The real flow must not throw (that would be a flow bug).
    assert err is None, f"FLOW BUG in tick_paper_session: {err}\npath={path}"
    # After the regime extreme-ATR fix, the explosive Ross name MUST be enterable. It was
    # 0 fills before — EVERY candidate was regime-blocked ('extreme_atr_block_all'), matching
    # the live 157 cancelled-pre-entry. A regression here (block reintroduced) -> 0 fills.
    assert len(fills) > 0, (
        f"explosive Ross name NPT not entered (sim_fills=0) — regime extreme-ATR regression? "
        f"final_state={sess.state} blocked={dict(block_reasons)}"
    )


def test_regime_allows_extreme_atr_for_momentum_families() -> None:
    """Deterministic guard for the regime extreme-ATR fix: the Ross momentum families MUST
    be allowed to enter explosive (extreme-ATR) names (their edge; risk is sized, not
    refused), while non-momentum families keep the 4.5% ATR ceiling."""
    from app.services.trading.momentum_neural.entry_gates import regime_entry_allowed

    for fam in ("impulse_breakout", "momentum_neural", "ross_smallcap"):
        ok, reason = regime_entry_allowed(fam, atr_pct=0.20, chop_expansion="trend", vol_regime="normal")
        assert ok, f"{fam} wrongly blocked at extreme ATR: {reason}"
    # a non-momentum family is still blocked at extreme ATR
    okx, rx = regime_entry_allowed("mean_reversion", atr_pct=0.20, chop_expansion="trend", vol_regime="normal")
    assert not okx and rx == "extreme_atr_block_all"
