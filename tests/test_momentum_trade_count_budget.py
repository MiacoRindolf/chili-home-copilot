"""A1 (Ross CLRO-lesson 2026-07-02) — QUALITY-AWARE daily trade-count budget.

Ross spent 3 trades on ONE name (CLRO) for +$8,917.32; CHILI's raw 5/5 FIFO budget was
consumed by B-names (IPW x2, ARCT, IREZ, EMPD) by 11:26 ET, then DENIED 98 CLRO candidates
11:56-13:39 ET spanning the +200% run. This locks the three A1 fixes:

  (a) EPISODE COUNTING — all same-day entries into ONE symbol = 1 episode; a symbol whose
      banked realized PnL today > 0 costs 0 for a re-entry (green round banked => free).
  (b) TOP-RANK EXEMPTION — when the ceiling is reached, the #1 freshness-valid live-eligible
      symbol whose score >= today's within-day p90 gets its OWN episode sub-budget = the SAME
      base (the CLRO-class name is never blocked). FAIL-CLOSED: unreadable rank / not-#1 /
      below-p90 => the ceiling stands.
  (c) the set_next_day_trading_lockout("daily_trade_count_budget") arming call is REMOVED
      (grep-assert) — a per-day ceiling block is not a next-day-lockout event on a bot.

[[project_arm_step_gap]] [[feedback_adaptive_no_magic]] [[feedback_report_binding_not_defaults]]
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural.persistence import ensure_momentum_strategy_variants
from app.services.trading.momentum_neural.risk_policy import (
    RISK_SNAPSHOT_KEY,
    _count_symbol_episodes_today,
    _percentile,
    _top_ranked_live_eligible_symbol,
    daily_trade_count_budget_decision,
)

_EF = "robinhood_agentic"
_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")


# ── helpers ───────────────────────────────────────────────────────────────────


def _require_tables(db: Session) -> None:
    names = set(sa_inspect(db.bind).get_table_names())
    for t in ("momentum_automation_outcomes", "momentum_symbol_viability"):
        if t not in names:
            pytest.skip(f"{t} table not present")


def _setup(db: Session) -> tuple[User, list[MomentumStrategyVariant]]:
    _require_tables(db)
    ensure_momentum_strategy_variants(db)
    db.commit()
    variants = db.query(MomentumStrategyVariant).all()
    assert variants
    u = User(name="TradeBudgetA1")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u, variants


def _utc_today_et(hour: int = 12, minute: int = 0) -> datetime:
    """Naive-UTC instant that lands on TODAY's ET calendar day (so it is inside today's
    ET session bounds the budget reads)."""
    now_et = datetime.now(_ET).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now_et.astimezone(_UTC).replace(tzinfo=None)


def _add_entry(
    db: Session,
    u: User,
    v: MomentumStrategyVariant,
    *,
    symbol: str,
    pnl: float | None,
    outcome_class: str = "small_win",
    mode: str = "live",
    execution_family: str = _EF,
    hour: int = 11,
    minute: int = 0,
) -> None:
    """Insert one REAL-entered live outcome for TODAY's ET session."""
    ts = _utc_today_et(hour=hour, minute=minute)
    s = TradingAutomationSession(
        user_id=u.id,
        mode=mode,
        symbol=symbol,
        variant_id=v.id,
        state="live_finished",
        risk_snapshot_json={RISK_SNAPSHOT_KEY: {"allowed": True}},
        ended_at=ts,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    db.add(
        MomentumAutomationOutcome(
            session_id=s.id,
            user_id=u.id,
            variant_id=v.id,
            symbol=symbol,
            mode=mode,
            execution_family=execution_family,
            terminal_state=s.state,
            terminal_at=ts,
            outcome_class=outcome_class,
            realized_pnl_usd=pnl,
            return_bps=(pnl * 10.0) if pnl is not None else None,
            regime_snapshot_json={},
            entry_regime_snapshot_json={},
            exit_regime_snapshot_json={},
            readiness_snapshot_json={},
            admission_snapshot_json={},
            governance_context_json={},
            evidence_weight=1.0,
            contributes_to_evolution=True,
        )
    )
    db.commit()


def _add_viability(
    db: Session, *, symbol: str, variant_id: int, score: float, live: bool = True, fresh: bool = True
) -> None:
    ts = datetime.utcnow() if fresh else (datetime.utcnow() - timedelta(hours=2))
    db.add(
        MomentumSymbolViability(
            symbol=symbol,
            scope="symbol",
            variant_id=variant_id,
            viability_score=score,
            paper_eligible=True,
            live_eligible=live,
            freshness_ts=ts,
        )
    )
    db.commit()


def _five_b_episodes(db: Session, u: User, variants: list[MomentumStrategyVariant]) -> None:
    """The 07-02 shape: 5 distinct B-name episodes consumed the FIFO budget (all red/flat so
    NONE are green-banked). IPW entered twice (2 entries -> ONE episode)."""
    v = variants[0]
    # IPW x2 same-symbol -> ONE episode (two real entries, both small losses)
    _add_entry(db, u, v, symbol="IPW", pnl=-68.0, outcome_class="stop_loss", hour=10, minute=5)
    _add_entry(db, u, v, symbol="IPW", pnl=-68.93, outcome_class="stop_loss", hour=10, minute=40)
    # 4 more distinct B names -> 4 more episodes (5 total)
    _add_entry(db, u, v, symbol="ARCT", pnl=-30.0, outcome_class="stop_loss", hour=10, minute=50)
    _add_entry(db, u, v, symbol="IREZ", pnl=-25.0, outcome_class="stop_loss", hour=11, minute=5)
    _add_entry(db, u, v, symbol="EMPD", pnl=-20.0, outcome_class="stop_loss", hour=11, minute=15)
    _add_entry(db, u, v, symbol="BBBB", pnl=-5.0, outcome_class="stop_loss", hour=11, minute=20)


def _enable(monkeypatch) -> None:
    # These two are real declared Settings fields (default True). The base (5) and
    # max_multiple (2.0) are getattr-with-default reads (NOT declared fields, so pydantic
    # rejects setattr) — the DEFAULTS already give ceiling=base=5 with a neutral heat/exp
    # (all test episodes are red => cushion 0 => heat_mult 1.0; <5 recent R => exp_mult 1.0
    # => raw_ceiling = 5*1*1 = 5 => ceiling = max(5, min(5, 10)) = 5).
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_budget_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_trade_budget_top_rank_exempt_enabled", True, raising=False)


# ── _percentile pure helper ─────────────────────────────────────────────────────


def test_percentile_pure_helper() -> None:
    assert _percentile([], 0.9) is None
    assert _percentile([0.7], 0.9) == 0.7
    # p90 of 0..1 in 0.1 steps = 0.9 (interp)
    vals = [i / 10.0 for i in range(11)]  # 0.0..1.0
    assert _percentile(vals, 0.90) == pytest.approx(0.9)
    assert _percentile(vals, 0.0) == pytest.approx(0.0)
    assert _percentile(vals, 1.0) == pytest.approx(1.0)


# ── A1(a) EPISODE COUNTING ──────────────────────────────────────────────────────


def test_episode_counting_same_symbol_is_one_episode(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    v = variants[0]
    # 3 real entries into ONE symbol, all red -> ONE charged episode.
    _add_entry(db, u, v, symbol="CLRO", pnl=-10.0, outcome_class="stop_loss", hour=10)
    _add_entry(db, u, v, symbol="CLRO", pnl=-12.0, outcome_class="stop_loss", hour=11)
    _add_entry(db, u, v, symbol="CLRO", pnl=-8.0, outcome_class="stop_loss", hour=12)
    episodes, green, meta = _count_symbol_episodes_today(db, execution_family=_EF)
    assert episodes == 1, meta
    assert green == set()
    assert meta["distinct_symbols"] == 1


def test_green_banked_symbol_costs_zero(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    v = variants[0]
    # WINNER symbol nets green today -> costs 0; a red symbol costs 1.
    _add_entry(db, u, v, symbol="WINR", pnl=+120.0, outcome_class="success", hour=10)
    _add_entry(db, u, v, symbol="LOSR", pnl=-30.0, outcome_class="stop_loss", hour=11)
    episodes, green, meta = _count_symbol_episodes_today(db, execution_family=_EF)
    assert "WINR" in green
    assert episodes == 1, meta  # only LOSR is charged; the green WINR is free


def test_never_entered_rows_not_counted(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    v = variants[0]
    # a cancelled/no-fill row (realized 0.0 NOT NULL) is NOT a real entry -> not an episode.
    _add_entry(db, u, v, symbol="NOFILL", pnl=0.0, outcome_class="no_fill", hour=10)
    _add_entry(db, u, v, symbol="CANC", pnl=0.0, outcome_class="cancelled_pre_entry", hour=11)
    episodes, green, meta = _count_symbol_episodes_today(db, execution_family=_EF)
    assert episodes == 0, meta


# ── A1(b) TOP-RANK read helper ──────────────────────────────────────────────────


def test_top_ranked_symbol_and_p90(db: Session, monkeypatch) -> None:
    _setup(db)
    variants = db.query(MomentumStrategyVariant).all()
    vid = variants[0].id
    vid2 = variants[1].id if len(variants) > 1 else vid
    # CLRO is the clear #1; a spread of lower-scored eligibles sets the p90.
    _add_viability(db, symbol="CLRO", variant_id=vid, score=0.92)
    _add_viability(db, symbol="AAAA", variant_id=vid2, score=0.55)
    # distinct symbols need distinct (symbol,variant) rows; reuse variants round-robin
    for i, sc in enumerate([0.50, 0.52, 0.48, 0.60, 0.58, 0.45]):
        _add_viability(db, symbol=f"N{i}", variant_id=variants[i % len(variants)].id, score=sc)
    top_sym, top_score, p90, meta = _top_ranked_live_eligible_symbol(db, crypto=False)
    assert top_sym == "CLRO"
    assert top_score == pytest.approx(0.92)
    assert p90 is not None and p90 > 0.0
    assert meta["n_eligible"] >= 6


def test_top_rank_empty_board_fail_closed(db: Session, monkeypatch) -> None:
    _setup(db)
    top_sym, top_score, p90, meta = _top_ranked_live_eligible_symbol(db, crypto=False)
    assert top_sym is None and top_score is None and p90 is None
    assert meta["reason"] in ("empty_board", "no_scored_symbols")


# ── A1 the 07-02-SHAPE REPLAY: 5 B-episodes -> #1 allowed, non-#1 denied ─────────


def test_0702_shape_top_ranked_clro_allowed_after_five_b_episodes(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    _enable(monkeypatch)
    _five_b_episodes(db, u, variants)  # ceiling (base=5) reached with 5 distinct red episodes
    # CLRO is the #1 freshness-valid live-eligible name, top-percentile score.
    _add_viability(db, symbol="CLRO", variant_id=variants[0].id, score=0.95)
    for i, sc in enumerate([0.55, 0.52, 0.50, 0.58, 0.48, 0.45]):
        _add_viability(db, symbol=f"J{i}", variant_id=variants[i % len(variants)].id, score=sc)
    ok, meta = daily_trade_count_budget_decision(
        db, execution_family=_EF, open_entry_count=0, symbol="CLRO"
    )
    assert ok is True, meta
    assert meta["reason"] == "top_rank_exempt", meta
    assert meta["exempt"] is True
    assert meta["exempt_sub_budget"] == 5
    # instrument: the block would have fired (5/5) but the #1 name is exempt.
    assert meta["used"] >= meta["ceiling"], meta


def test_0702_shape_non_top_ranked_denied(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    _enable(monkeypatch)
    _five_b_episodes(db, u, variants)
    _add_viability(db, symbol="CLRO", variant_id=variants[0].id, score=0.95)  # #1 is CLRO
    for i, sc in enumerate([0.55, 0.52, 0.50]):
        _add_viability(db, symbol=f"K{i}", variant_id=variants[i % len(variants)].id, score=sc)
    # a NON-#1 name (ZZZZ, not on the board / not top) is denied — the ceiling stands.
    ok, meta = daily_trade_count_budget_decision(
        db, execution_family=_EF, open_entry_count=0, symbol="ZZZZ"
    )
    assert ok is False, meta
    assert meta["reason"] == "daily_trade_count_budget_reached"
    assert meta["exempt"] is False
    assert meta["exempt_reason"] == "not_top_ranked"


def test_0702_shape_second_best_denied(db: Session, monkeypatch) -> None:
    # Only the #1 (max-score) name earns the exemption. The 2nd-best fresh live-eligible name
    # is NOT the #1 => denied (the p90 leg is a belt-and-suspenders guard: the true #1 IS the
    # max, so it always clears p90; a non-#1 fails on the identity check first).
    u, variants = _setup(db)
    _enable(monkeypatch)
    _five_b_episodes(db, u, variants)
    _add_viability(db, symbol="TOP1", variant_id=variants[0].id, score=0.99)
    _add_viability(db, symbol="CAND", variant_id=variants[1 % len(variants)].id, score=0.60)
    for i, sc in enumerate([0.58, 0.57, 0.56, 0.59]):
        _add_viability(db, symbol=f"M{i}", variant_id=variants[i % len(variants)].id, score=sc)
    ok, meta = daily_trade_count_budget_decision(
        db, execution_family=_EF, open_entry_count=0, symbol="CAND"
    )
    assert ok is False, meta
    assert meta["exempt"] is False
    assert meta["exempt_reason"] == "not_top_ranked"  # CAND is not the #1 (TOP1 is)


def test_unreadable_rank_denied_fail_closed(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    _enable(monkeypatch)
    _five_b_episodes(db, u, variants)
    # NO viability rows at all -> rank unreadable -> the exemption fails closed -> denied.
    ok, meta = daily_trade_count_budget_decision(
        db, execution_family=_EF, open_entry_count=0, symbol="CLRO"
    )
    assert ok is False, meta
    assert meta["exempt"] is False
    assert meta["exempt_reason"] == "rank_unreadable"


def test_green_banked_reentry_is_free(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    _enable(monkeypatch)
    _five_b_episodes(db, u, variants)  # 5 red B-episodes
    # CLRO banked GREEN today -> a re-entry into CLRO costs 0 (free), regardless of rank.
    _add_entry(db, u, variants[0], symbol="CLRO", pnl=+300.0, outcome_class="success", hour=11, minute=45)
    ok, meta = daily_trade_count_budget_decision(
        db, execution_family=_EF, open_entry_count=0, symbol="CLRO"
    )
    assert ok is True, meta
    assert meta["reason"] == "green_banked_reentry_free"
    assert meta["candidate_green_banked"] is True


def test_below_ceiling_allowed_plainly(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    _enable(monkeypatch)
    # only 2 episodes -> below the base=5 ceiling -> plain allow (no exemption needed).
    _add_entry(db, u, variants[0], symbol="AAA", pnl=-10.0, outcome_class="stop_loss", hour=10)
    _add_entry(db, u, variants[0], symbol="BBB", pnl=-10.0, outcome_class="stop_loss", hour=11)
    ok, meta = daily_trade_count_budget_decision(
        db, execution_family=_EF, open_entry_count=0, symbol="CCC"
    )
    assert ok is True, meta
    assert meta["episodes_today"] == 2
    # a plain sub-ceiling allow carries NO reason key (reason is set only on blocks/exemptions).
    assert meta.get("reason") != "top_rank_exempt"  # allowed on the plain budget, not the exemption
    assert meta["allowed"] is True


def test_exempt_flag_off_hard_blocks(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_trade_budget_top_rank_exempt_enabled", False, raising=False)
    _five_b_episodes(db, u, variants)
    _add_viability(db, symbol="CLRO", variant_id=variants[0].id, score=0.95)
    ok, meta = daily_trade_count_budget_decision(
        db, execution_family=_EF, open_entry_count=0, symbol="CLRO"
    )
    assert ok is False, meta  # exemption OFF -> the ceiling is a hard block even for #1
    assert meta["reason"] == "daily_trade_count_budget_reached"


def test_budget_flag_off_byte_identical_allow(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_budget_enabled", False, raising=False)
    _five_b_episodes(db, u, variants)
    ok, meta = daily_trade_count_budget_decision(
        db, execution_family=_EF, open_entry_count=0, symbol="ANY"
    )
    assert ok is True
    assert meta == {"reason": "disabled"}


# ── A1(c) grep-assert: the next-day-lockout arming call is GONE ──────────────────


def test_next_day_lockout_arming_call_removed() -> None:
    """The set_next_day_trading_lockout("daily_trade_count_budget") landmine (it fired 98x on
    07-02) must be REMOVED from live_runner.py — a per-day ceiling block is not a next-day
    lockout event on a bot."""
    lr = Path(__file__).resolve().parents[1] / "app" / "services" / "trading" / "momentum_neural" / "live_runner.py"
    text = lr.read_text(encoding="utf-8")
    assert 'set_next_day_trading_lockout("daily_trade_count_budget")' not in text
