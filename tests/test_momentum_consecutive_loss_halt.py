"""ROSS RISK GAP 3 — account-wide consecutive-loss ARM HALT (the tilt rule).

Ross's tilt rule: 2-3 reds in a row = walk away. The streak dial only de-SIZES (never halts),
the COUNT day-blocks are PER-SYMBOL, and the account-wide halts are DOLLAR-based — so N small
losses across N different tickers trip no halt (death by a thousand papercuts).

``account_wide_consecutive_losses`` counts the run of consecutive realized losses across ALL
symbols + families over TODAY's ET session (most-recent first, resets on a win / new day).
``consecutive_loss_halt_decision`` turns it into a HALT once >= the configured count.

  (a) N consecutive account-wide losses -> halt;
  (b) a WIN resets the run -> no halt;
  (c) flag OFF -> never halts (byte-identical);
  (d) exits are NOT blocked (the decision is a read used ONLY in the arm guard).
"""
from __future__ import annotations

import datetime as _dt

import pytest

from app.config import settings
from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural.risk_policy import (
    account_wide_consecutive_losses,
    consecutive_loss_halt_decision,
)

_HALT_N = int(settings.chili_momentum_consecutive_loss_halt_count)  # 4

_seq = 0


def _variant_id(db):
    """A fresh strategy variant (unique key) to satisfy the session FK (NOT NULL variant_id)."""
    global _seq
    _seq += 1
    v = MomentumStrategyVariant(
        family="reclaim", variant_key=f"cl_{_seq}_{id(db) & 0xffff}", label="consec-loss test",
        params_json={}, is_active=True,
    )
    db.add(v)
    db.flush()
    return v.id


def _seed(db, *, pnl, oc="stop_loss", fam="robinhood_spot", symbol="AAA", mins_ago=1,
          variant_id=None):
    """Seed one terminated LIVE outcome today. ``mins_ago`` controls recency (smaller =
    more recent) so we can order the consecutive run deterministically."""
    global _seq
    _seq += 1
    if variant_id is None:
        variant_id = _variant_id(db)
    sess = TradingAutomationSession(
        user_id=None, venue="test", execution_family=fam, mode="live",
        symbol=symbol, variant_id=variant_id, state="live_finished",
        risk_snapshot_json={}, correlation_id=f"corr-cl-{_seq}",
    )
    db.add(sess)
    db.flush()
    o = MomentumAutomationOutcome(
        session_id=sess.id, user_id=None, variant_id=variant_id, symbol=symbol, mode="live",
        execution_family=fam, terminal_state="live_finished",
        terminal_at=_dt.datetime.utcnow() - _dt.timedelta(minutes=mins_ago),
        outcome_class=oc, realized_pnl_usd=pnl,
    )
    db.add(o)
    db.flush()
    return o


def test_no_history_no_halt(db):
    consec, meta = account_wide_consecutive_losses(db)
    assert consec == 0
    halted, _ = consecutive_loss_halt_decision(db)
    assert halted is False


def test_n_consecutive_losses_halt(db):
    # (a) N losses across N DIFFERENT tickers + families (the papercuts case) -> halt.
    for i in range(_HALT_N):
        fam = "robinhood_spot" if i % 2 == 0 else "coinbase_spot"
        _seed(db, pnl=-5.0, symbol=f"S{i}", fam=fam, mins_ago=_HALT_N - i)
    db.commit()
    consec, meta = account_wide_consecutive_losses(db)
    assert consec >= _HALT_N
    halted, hmeta = consecutive_loss_halt_decision(db)
    assert halted is True
    assert hmeta["consecutive_losses"] >= _HALT_N
    assert hmeta["halt_count"] == _HALT_N


def test_below_threshold_no_halt(db):
    # N-1 consecutive losses -> under the bar -> no halt.
    for i in range(_HALT_N - 1):
        _seed(db, pnl=-5.0, symbol=f"S{i}", mins_ago=_HALT_N - i)
    db.commit()
    consec, _ = account_wide_consecutive_losses(db)
    assert consec == _HALT_N - 1
    halted, _ = consecutive_loss_halt_decision(db)
    assert halted is False


def test_a_win_resets_the_run(db):
    # (b) The MOST-RECENT outcome is a WIN -> the consecutive-loss run is 0 even with many
    # older losses -> no halt. (Most-recent-first; the win breaks the run immediately.)
    # Older losses (further back in time).
    for i in range(_HALT_N + 2):
        _seed(db, pnl=-5.0, symbol=f"OLD{i}", mins_ago=100 + i)
    # The newest outcome is a WIN.
    _seed(db, pnl=+12.0, oc="success", symbol="WIN", mins_ago=1)
    db.commit()
    consec, _ = account_wide_consecutive_losses(db)
    assert consec == 0
    halted, _ = consecutive_loss_halt_decision(db)
    assert halted is False


def test_never_entered_rows_do_not_count_or_reset(db):
    # Never-entered churn (cancelled_pre_entry / no_fill, realized=0.0) sits between losses
    # in time but must NEITHER count as a loss NOR reset the consecutive run.
    # Newest: a never-entered cancel; then N real losses behind it.
    _seed(db, pnl=0.0, oc="cancelled_pre_entry", symbol="CXL", mins_ago=1)
    _seed(db, pnl=0.0, oc="no_fill", symbol="NOF", mins_ago=2)
    for i in range(_HALT_N):
        _seed(db, pnl=-5.0, symbol=f"L{i}", mins_ago=10 + i)
    db.commit()
    consec, _ = account_wide_consecutive_losses(db)
    # The two never-entered rows are pruned -> the run is the N real losses behind them.
    assert consec >= _HALT_N
    halted, _ = consecutive_loss_halt_decision(db)
    assert halted is True


def test_break_even_ends_the_run(db):
    # A real-entered BREAK-EVEN ($0 realized) entry is a non-loss -> it ends the run (a red in
    # a row means realized < 0). Newest is a break-even success; losses behind it don't count.
    for i in range(_HALT_N + 1):
        _seed(db, pnl=-5.0, symbol=f"L{i}", mins_ago=10 + i)
    _seed(db, pnl=0.0, oc="success", symbol="BE", mins_ago=1)
    db.commit()
    consec, _ = account_wide_consecutive_losses(db)
    assert consec == 0


def test_alpaca_paper_twins_excluded(db):
    # alpaca_spot paper twins trade FAKE money -> their (losing) outcomes must NOT trip the
    # real-account tilt halt. N alpaca losses alone -> consec 0 (excluded) -> no halt.
    for i in range(_HALT_N + 2):
        _seed(db, pnl=-9.0, fam="alpaca_spot", symbol=f"PAP{i}", mins_ago=1 + i)
    db.commit()
    consec, _ = account_wide_consecutive_losses(db)
    assert consec == 0
    halted, _ = consecutive_loss_halt_decision(db)
    assert halted is False


def test_flag_off_never_halts(db, monkeypatch):
    # (c) Flag OFF -> the decision returns not-halted even with N+ consecutive losses
    # (byte-identical: arming is never blocked by this guard).
    for i in range(_HALT_N + 2):
        _seed(db, pnl=-5.0, symbol=f"S{i}", mins_ago=_HALT_N + 2 - i)
    db.commit()
    monkeypatch.setattr(settings, "chili_momentum_consecutive_loss_halt_enabled", False)
    halted, meta = consecutive_loss_halt_decision(db)
    assert halted is False
    assert meta["reason"] == "disabled"


def test_halt_decision_fails_open_on_error(db, monkeypatch):
    # Any error inside the count -> fail-OPEN (not halted) so arming is byte-identical.
    import app.services.trading.momentum_neural.risk_policy as rp

    def _boom(db, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(rp, "account_wide_consecutive_losses", _boom)
    halted, meta = consecutive_loss_halt_decision(db)
    assert halted is False
    assert meta["reason"] == "error_fail_open"


def test_halt_is_arming_only_not_an_exit_gate(db):
    # (d) The consecutive-loss halt is a READ consumed ONLY by the arm guard. It exposes NO
    # exit/position-management surface: it never imports, reads, or mutates an order/position
    # and is not referenced on any exit path. Assert the decision is a pure read that returns
    # a (bool, meta) verdict — there is no exit-blocking side effect to invoke.
    for i in range(_HALT_N):
        _seed(db, pnl=-5.0, symbol=f"S{i}", mins_ago=_HALT_N - i)
    db.commit()
    halted, meta = consecutive_loss_halt_decision(db)
    assert halted is True
    # The verdict is advisory to the ARM path only; calling it does not touch positions.
    # Re-calling is idempotent (read-only) and yields the same verdict.
    halted2, _ = consecutive_loss_halt_decision(db)
    assert halted2 is True
    # Confirm the auto_arm guard consumes it (the ONLY wiring), and the live runner exit
    # paths never reference it.
    import inspect
    from app.services.trading.momentum_neural import auto_arm, live_runner
    assert "consecutive_loss_halt_decision" in inspect.getsource(auto_arm)
    assert "consecutive_loss_halt_decision" not in inspect.getsource(live_runner)
