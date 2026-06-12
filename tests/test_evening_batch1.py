"""Evening batch 1 (2026-06-12): A2 state-aware reap, A4 crypto-live-off,
A6 multi-arm per pass, EOD flatten — the throughput + risk-hygiene set."""

from datetime import datetime, timedelta

from app.config import settings as _settings


def test_config_defaults_batch1():
    from app.config import Settings

    f = Settings.model_fields
    assert f["chili_momentum_auto_arm_max_watch_seconds"].default == 300
    assert f["chili_momentum_auto_arm_watch_extend_seconds"].default == 600
    assert f["chili_momentum_auto_arm_max_arms_per_pass"].default == 3
    assert f["chili_momentum_crypto_live_arm_enabled"].default is False
    assert f["chili_momentum_eod_flatten_lead_min"].default == 5.0


def _mk_watch_session(db, uid, vid, symbol, *, age_sec, le=None):
    from app.models.trading import TradingAutomationSession

    sess = TradingAutomationSession(
        user_id=uid, symbol=symbol, mode="live", state="watching_live",
        execution_family="robinhood_spot", variant_id=vid,
        risk_snapshot_json={"momentum_live_execution": (le or {})},
        started_at=datetime.utcnow() - timedelta(seconds=age_sec),
    )
    db.add(sess)
    db.flush()
    return sess


def test_reap_extends_tick_armed_sessions(db, monkeypatch):
    from app.models.core import User
    from app.models.trading import MomentumStrategyVariant
    from app.services.trading.momentum_neural import auto_arm as aa

    u = User(name="reap-op"); db.add(u); db.flush()
    v = MomentumStrategyVariant(family="rp", variant_key="rp_v", label="rp", params_json={})
    db.add(v); db.flush()
    monkeypatch.setattr(_settings, "chili_momentum_auto_arm_max_watch_seconds", 300)
    monkeypatch.setattr(_settings, "chili_momentum_auto_arm_watch_extend_seconds", 600)
    dead = _mk_watch_session(db, u.id, v.id, "DEAD", age_sec=400)               # > 300, no level
    forming = _mk_watch_session(db, u.id, v.id, "FORM", age_sec=400,
                                le={"watch_break_level": 7.5})                   # tick-armed, < 600
    expired = _mk_watch_session(db, u.id, v.id, "EXPR", age_sec=700,
                                le={"watch_break_level": 7.5})                   # tick-armed, > 600
    reaped = aa._reap_stale_watching_sessions(db, user_id=int(u.id), now=datetime.utcnow())
    db.flush()
    states = {s.symbol: s.state for s in (dead, forming, expired)}
    assert states["FORM"] == "watching_live"   # extended — still alive
    assert states["DEAD"] != "watching_live"   # reaped at base cutoff
    assert states["EXPR"] != "watching_live"   # reaped at extend cutoff
    assert reaped == 2


def test_arm_tail_is_a_multi_pick_loop():
    src = open("app/services/trading/momentum_neural/auto_arm.py", encoding="utf-8").read()
    assert "for chosen, chosen_reason in _picks:" in src
    assert "chili_momentum_auto_arm_max_arms_per_pass" in src
    assert "_live_armable" in src
    # crypto gates must NOT filter the shared eligible list (paper learns 24/7)
    assert "crypto live-arm gates apply at the LIVE pick stage" in src


def test_eod_flatten_block_present_and_gated():
    src = open("app/services/trading/momentum_neural/live_runner.py", encoding="utf-8").read()
    i_eod = src.index("eod_flatten_triggered")
    i_opflat = src.index("Operator FLATTEN (system-mediated manual exit")
    assert i_eod < i_opflat  # EOD sets the flag BEFORE the chokepoint honors it
    assert 'endswith("-USD")' in src[i_eod - 2000:i_eod]  # equities only
