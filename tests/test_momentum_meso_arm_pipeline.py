"""MESO TIER — the auto-arm ARM PIPELINE contract (Ross momentum lane).

Drives ``run_auto_arm_pass`` (and its real gate helpers, composed) with synthetic
candidate boards and asserts the *arm decision*: WHICH candidate (if any) arms, and the
SKIP REASON when nothing does. The focus is the MESO composition that the existing
``tests/test_momentum_auto_arm.py`` does NOT cover:

  - a clear A+ board ARMS the top mover;
  - a sub-A+ poor-regime board SITS CASH (``skipped == "no_asetup_sit_cash"``);
  - a board with one EXHAUSTED leader SKIPS it but arms a fresh one;
  - the new-initiation gates COMPOSE in the right ORDER (sit-cash -> time-of-day ->
    eligible scan), each consulted, NONE touching an exit.

Method: feed the *real* gate helpers (``_should_sit_cash_no_asetup`` /
``_move_is_exhausted`` / ``_should_suppress_late_day`` / ``_exhaustion_abandon_eligible``
/ ``_regime_is_poor`` / ``_asetup_quality_floor``) synthetic rows + patched leaf seams
(``settings``, ``_tape_cold``, ``front_side_state``) and assert the SPECIFIC value/state,
so each test FAILS if the composition is subtly wrong.

PURE-LOGIC + mocks; no DB, no network. ``run_auto_arm_pass`` is driven with a tiny fake
DB and every network/broker seam patched (the same shape as test_momentum_auto_arm.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.trading.momentum_neural.auto_arm as aa
from app.services import coinbase_service
from app.services.trading import governance, portfolio_risk
from app.services.trading.momentum_neural import (
    automation_query,
    market_profile,
    operator_actions,
    risk_policy,
)
from app.services.trading.venue import account_identity

# Capture the REAL _entry_trigger_fires at import time so a test that exercises its real
# pullback -> exhaustion composition can restore it (the happy fixture stubs it out).
_REAL_ENTRY_TRIGGER_FIRES = aa._entry_trigger_fires


# ── synthetic candidate row ──────────────────────────────────────────────────────────
def _cand(
    symbol: str = "MOVR-USD",
    variant_id: int = 8,
    score: float = 0.61,
    ross: float | None = None,
    *,
    signal: dict | None = None,
):
    """A MomentumSymbolViability-shaped stand-in. ``ross`` stamps the per-row ross_score
    the board ranker / sit-cash gate read from ``execution_readiness_json.extra.ross_scores``;
    ``signal`` stamps the per-row scanner signal in ``...extra.ross_signals`` (catalyst/RVOL/
    daily_breaking). Only the attrs the auto-arm reads are populated."""
    su = symbol.upper()
    extra: dict = {}
    if ross is not None:
        extra["ross_scores"] = {su: float(ross)}
    if signal is not None:
        extra["ross_signals"] = {su: dict(signal)}
    erj = {"extra": extra} if extra else {}
    return SimpleNamespace(
        symbol=symbol,
        variant_id=variant_id,
        viability_score=score,
        execution_readiness_json=erj,
    )


class _FakeDB:
    """Minimal Session stand-in. The arm pass commits/rolls-back/expunges around the
    read-release boundary; none of the MESO gate logic needs a real query here (the gates
    read the already-loaded candidate rows)."""

    def add(self, *_a, **_k) -> None:
        pass

    def commit(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def expunge_all(self) -> None:
        pass


@pytest.fixture
def happy(monkeypatch):
    """Patch every seam to a deterministic happy path. Tests override ONE board / gate flag
    to exercise the composition. Candidates default to EQUITY (Ross lane) so the gates that
    read equity tape/board run; crypto-specific gates are neutralized."""
    s = aa.settings
    monkeypatch.setattr(s, "chili_momentum_auto_arm_live_enabled", True, raising=False)
    monkeypatch.setattr(s, "chili_momentum_live_runner_enabled", True, raising=False)
    monkeypatch.setattr(s, "chili_autotrader_user_id", 1, raising=False)
    # Ross lane = equities only (so the board / tape / late-day gates run on equity names).
    monkeypatch.setattr(s, "chili_momentum_auto_arm_crypto_only", False, raising=False)
    monkeypatch.setattr(s, "chili_momentum_auto_arm_equity_only", True, raising=False)
    monkeypatch.setattr(s, "chili_momentum_ross_equity_universe_required", False, raising=False)
    monkeypatch.setattr(s, "chili_momentum_decouple_watching_enabled", False, raising=False)
    monkeypatch.setattr(s, "chili_momentum_auto_arm_max_arms_per_pass", 1, raising=False)
    monkeypatch.setattr(s, "chili_momentum_reap_cooldown_sec", 0, raising=False)
    # New-initiation gates default OFF unless a test flips them on.
    monkeypatch.setattr(s, "chili_momentum_no_asetup_sit_cash_enabled", False, raising=False)
    monkeypatch.setattr(s, "chili_momentum_timeofday_schedule_enabled", False, raising=False)
    monkeypatch.setattr(s, "chili_momentum_move_exhaustion_abandon_enabled", False, raising=False)

    # Equity late-window gate (_live_armable) is wall-clock driven -> pin to a productive
    # window so equity selection is deterministic regardless of run time.
    monkeypatch.setattr(market_profile, "schedule_window_now", lambda *a, **k: "midday", raising=False)
    monkeypatch.setattr(aa, "_symbol_market_open", lambda sym: True)

    # Guards: kill-switch / lockout / drawdown / daily-loss all clear.
    monkeypatch.setattr(governance, "kill_switch_halts_new_entries", lambda: False, raising=False)
    monkeypatch.setattr(governance, "check_next_day_trading_lockout", lambda: (False, {}), raising=False)
    monkeypatch.setattr(governance, "broker_daily_loss_breached", lambda *a, **k: (False, {}), raising=False)
    monkeypatch.setattr(portfolio_risk, "check_portfolio_drawdown_breaker", lambda db, uid: (False, None))
    monkeypatch.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 0)
    monkeypatch.setattr(automation_query, "expire_stale_live_arm_sessions", lambda db, *, user_id: 0)
    monkeypatch.setattr(
        automation_query,
        "reap_stale_live_sessions",
        lambda db, *, user_id: {"reaped": 0},
    )
    monkeypatch.setattr(aa, "_reap_stale_watching_sessions", lambda db, *, user_id, now: 0)

    # Selection seams.
    monkeypatch.setattr(aa, "_symbol_free", lambda db, sym, uid: True)
    monkeypatch.setattr(aa, "_venue_broker_ready_for", lambda sym, cache: True)
    monkeypatch.setattr(aa, "_symbols_with_active_live_session", lambda db, *, user_id: set())
    monkeypatch.setattr(aa, "_symbol_loss_guards", lambda db, **kwargs: (set(), {}))
    monkeypatch.setattr(
        account_identity,
        "read_current_non_alpaca_account_identity",
        lambda _family: {
            "ok": True,
            "identity": "meso-account-v1",
            "reason": None,
        },
    )
    monkeypatch.setattr(
        risk_policy,
        "load_current_live_loss_history",
        lambda db, **kwargs: (
            (),
            {
                "history_available": True,
                "coverage_grade": "CURRENT_LIVE_COMPLETE",
                "history_authority": "broker_reconciled_current_live_db_only",
                "replay_certifiable": False,
            },
        ),
    )
    monkeypatch.setattr(
        risk_policy,
        "consecutive_loss_halt_decision",
        lambda db, **kwargs: (
            False,
            {
                "halted": False,
                "consecutive_losses": 0,
                "halt_count": 4,
                "history_available": True,
                "config_provenance": {},
            },
        ),
    )
    monkeypatch.setattr(aa, "_paper_shadow_arm", lambda db, **k: 0)
    # Default board: a single firing equity mover.
    monkeypatch.setattr(
        aa, "_fresh_live_eligible_candidates", lambda db, *, limit: [_cand("MOVR", 8, 0.61)]
    )
    monkeypatch.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    monkeypatch.setattr(aa, "_candidate_freshness", lambda sym: None)

    monkeypatch.setattr(coinbase_service, "connect", lambda: {"ok": True})
    monkeypatch.setattr(
        operator_actions, "begin_live_arm",
        lambda db, **k: {"ok": True, "arm_token": "tok", "session_id": 99},
    )
    monkeypatch.setattr(
        operator_actions, "confirm_live_arm",
        lambda db, **k: {"ok": True, "state": "queued_live"},
    )
    # No alpaca twin noise.
    monkeypatch.setattr(s, "chili_momentum_alpaca_twin_arm_enabled", False, raising=False)
    return monkeypatch


# ════════════════════════════════════════════════════════════════════════════════════
# A. A CLEAR A+ BOARD ARMS THE TOP MOVER
# ════════════════════════════════════════════════════════════════════════════════════
def test_live_loss_guards_share_one_adapter_identity_read(happy):
    calls = {"identity": 0}
    observed: dict[str, object] = {}
    happy.setattr(
        aa.settings,
        "chili_momentum_auto_arm_equity_only",
        False,
        raising=False,
    )

    def _identity(_family):
        calls["identity"] += 1
        return {
            "ok": True,
            "identity": "shared-live-account-v2",
            "reason": None,
        }

    def _symbol_guard(db, **kwargs):
        observed["symbol_scope"] = kwargs.get("_resolved_scope")
        return set(), {}

    def _consecutive(db, **kwargs):
        observed["consecutive_identity"] = kwargs.get("account_identity")
        return False, {
            "halted": False,
            "consecutive_losses": 0,
            "halt_count": 4,
            "history_available": True,
            "config_provenance": {},
        }

    happy.setattr(
        account_identity,
        "read_current_non_alpaca_account_identity",
        _identity,
    )
    happy.setattr(aa, "_symbol_loss_guards", _symbol_guard)
    happy.setattr(risk_policy, "consecutive_loss_halt_decision", _consecutive)

    out = aa.run_auto_arm_pass(_FakeDB())

    assert out["armed"] == 1
    assert calls["identity"] == 1
    assert observed["symbol_scope"]["account_identity"] == (
        "shared-live-account-v2"
    )
    assert observed["consecutive_identity"] == "shared-live-account-v2"
    assert out["loss_guard_policy"]["account_identity_bound"] is True
    assert out["loss_guard_policy"]["account_identity_source"] == (
        "adapter_current_account_identity"
    )


def test_finalized_nth_loss_is_visible_to_same_pass_halt(happy):
    initial = datetime(2026, 7, 14, 16, 0, 0)
    refreshed = initial + timedelta(seconds=1)
    calls = {"clock": 0, "begin": 0}
    threshold = int(
        getattr(aa.settings, "chili_momentum_consecutive_loss_halt_count", 4)
        or 4
    )

    def _frontier(_value=None):
        calls["clock"] += 1
        return initial if calls["clock"] == 1 else refreshed

    entries = tuple(
        risk_policy.CurrentLiveLossHistoryEntry(
            session_id=index,
            outcome_id=index,
            symbol=f"LOSS{index}",
            terminal_at=refreshed - timedelta(milliseconds=index),
            outcome_class="stop_loss",
            realized_pnl_usd=-5.0,
            return_bps=-50.0,
            broker_reconciled_at=refreshed - timedelta(milliseconds=index),
        )
        for index in range(1, threshold + 1)
    )

    def _history(db, **kwargs):
        assert kwargs["decision_as_of"] == refreshed
        return entries, {
            "history_available": True,
            "coverage_grade": "CURRENT_LIVE_COMPLETE",
            "history_authority": "broker_reconciled_current_live_db_only",
            "replay_certifiable": False,
        }

    def _halt(db, **kwargs):
        observed, _meta = kwargs["_current_live_history"]
        return len(observed) >= threshold, {
            "halted": True,
            "consecutive_losses": len(observed),
            "halt_count": threshold,
            "history_available": True,
            "config_provenance": {},
        }

    happy.setattr(aa, "_decision_as_of_naive_utc", _frontier)
    happy.setattr(
        aa,
        "_finalize_stale_exited_sessions",
        lambda db, *, user_id, now: 1,
    )
    happy.setattr(risk_policy, "load_current_live_loss_history", _history)
    happy.setattr(risk_policy, "consecutive_loss_halt_decision", _halt)
    happy.setattr(
        operator_actions,
        "begin_live_arm",
        lambda db, **kwargs: calls.__setitem__("begin", calls["begin"] + 1),
    )

    out = aa.run_auto_arm_pass(_FakeDB())

    assert calls["clock"] >= 2
    assert calls["begin"] == 0
    assert out["finalized_exited"] == 1
    assert out["skipped"] == "consecutive_loss_halt"
    assert out["consecutive_losses"] == threshold


def test_loss_history_read_failure_creates_no_arm(happy):
    calls = {"begin": 0}
    happy.setattr(
        aa.settings,
        "chili_momentum_auto_arm_equity_only",
        False,
        raising=False,
    )

    def _history_unavailable(db, **kwargs):
        raise aa._LossGuardHistoryUnavailable(
            "symbol_loss_guard_history_unavailable"
        )

    def _begin(db, **kwargs):
        calls["begin"] += 1
        return {"ok": True, "arm_token": "must-not-run", "session_id": 999}

    happy.setattr(aa, "_symbol_loss_guards", _history_unavailable)
    happy.setattr(operator_actions, "begin_live_arm", _begin)

    out = aa.run_auto_arm_pass(_FakeDB())

    assert out["armed"] == 0
    assert out["skipped"] == "loss_guard_history_unavailable"
    assert out["loss_guard_history"]["coverage_grade"] == (
        "COVERAGE_UNAVAILABLE"
    )
    assert calls["begin"] == 0


def test_live_account_identity_failure_creates_no_arm(happy):
    calls = {"begin": 0}
    happy.setattr(
        account_identity,
        "read_current_non_alpaca_account_identity",
        lambda _family: {
            "ok": False,
            "identity": None,
            "reason": "non_alpaca_account_identity_unknown",
        },
    )

    def _begin(db, **kwargs):
        calls["begin"] += 1
        return {"ok": True, "arm_token": "must-not-run", "session_id": 999}

    happy.setattr(operator_actions, "begin_live_arm", _begin)

    out = aa.run_auto_arm_pass(_FakeDB())

    assert out["armed"] == 0
    assert out["skipped"] == "loss_guard_scope_unavailable"
    assert out["loss_guard_scope_reason"] == (
        "non_alpaca_account_identity_unknown"
    )
    assert calls["begin"] == 0


def test_live_win_cycle_guard_receives_pass_decision_clock(happy):
    observed: dict[str, object] = {}
    happy.setattr(
        aa.settings,
        "chili_momentum_auto_arm_equity_only",
        False,
        raising=False,
    )

    def _count(db, **kwargs):
        observed.update(kwargs)
        return 0

    happy.setattr(
        aa.settings,
        "chili_momentum_win_cycle_fatigue_enabled",
        True,
        raising=False,
    )
    happy.setattr(aa, "_win_cycle_clean_win_count", _count)

    out = aa.run_auto_arm_pass(_FakeDB())

    assert out["armed"] == 1
    assert isinstance(observed.get("as_of_utc"), datetime)
    assert observed["execution_family"] == aa._lane_execution_family()


def test_aplus_board_arms_top_mover(happy):
    """A clear A+ board (high ross_score top mover, sit-cash + timeofday gates ENABLED but
    the regime is strong) arms the TOP mover and reports it. The sit-cash gate must NOT
    suppress when a genuine A+ is present even if the regime were poor (local A+ beats the
    veto), so we flip both gates on and still expect the top mover armed."""
    happy.setattr(aa.settings, "chili_momentum_no_asetup_sit_cash_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_momentum_continuation_ross_floor", 0.70, raising=False)
    board = [
        _cand("HOTA", 8, 0.61, ross=0.93, signal={"news_catalyst": True}),
        _cand("MIDB", 8, 0.60, ross=0.50),
        _cand("LOWC", 8, 0.59, ross=0.40),
    ]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: board)
    # Only the top A+ name is firing a break NOW (Ross "the one moving right now").
    happy.setattr(
        aa, "_entry_trigger_fires",
        lambda sym: (sym == "HOTA", "pullback_break_ok" if sym == "HOTA" else "waiting"),
    )
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "HOTA"
    assert out["session_id"] == 99
    assert out["state"] == "queued_live"
    assert out.get("skipped") in (None,)  # cleared on a successful arm


def test_aplus_present_overrides_poor_regime(happy):
    """Even with a poor regime (cold tape AND no catalyst on the board), a top ross_score AT/
    ABOVE the A+ bar must NOT sit cash — local A+ beats the regime veto. Exercises the REAL
    _should_sit_cash_no_asetup composition end-to-end (no suppression)."""
    happy.setattr(aa.settings, "chili_momentum_no_asetup_sit_cash_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_momentum_continuation_ross_floor", 0.70, raising=False)
    happy.setattr(aa, "_tape_cold", lambda sym: True)  # cold tape everywhere
    board = [
        _cand("APLS", 8, 0.61, ross=0.95, signal={"news_catalyst": False}),
        _cand("WEAK", 8, 0.60, ross=0.45, signal={"news_catalyst": False}),
    ]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: board)
    happy.setattr(
        aa, "_entry_trigger_fires",
        lambda sym: (sym == "APLS", "pullback_break_ok" if sym == "APLS" else "waiting"),
    )
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("skipped") != "no_asetup_sit_cash"
    assert out["armed"] == 1
    assert out["symbol"] == "APLS"


# ════════════════════════════════════════════════════════════════════════════════════
# B. SUB-A+ POOR-REGIME BOARD SITS CASH
# ════════════════════════════════════════════════════════════════════════════════════
def test_subaplus_poor_regime_sits_cash(happy):
    """A sub-A+ board (best ross_score CLEARLY below the A+ bar) AND a poor regime (cold
    tape-breadth AND no fresh catalyst) must SIT CASH — ``skipped == "no_asetup_sit_cash"``,
    armed 0 — even though a break is firing. This is the gate's whole purpose: refuse a
    fresh initiation when nothing A+ is up and the tape is dead."""
    happy.setattr(aa.settings, "chili_momentum_no_asetup_sit_cash_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_momentum_continuation_ross_floor", 0.70, raising=False)
    # Force the corroborating regime axes affirmatively poor.
    happy.setattr(aa, "_tape_cold", lambda sym: True)
    board = [
        _cand("DULL", 8, 0.61, ross=0.40, signal={"news_catalyst": False}),
        _cand("MEHB", 8, 0.60, ross=0.35, signal={"news_catalyst": False}),
    ]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: board)
    # A break IS firing — proving the SUPPRESSION is the gate, not an absent trigger.
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "no_asetup_sit_cash"
    assert out.get("armed", 0) == 0
    dbg = out["sit_cash"]
    assert dbg["suppress"] is True
    assert dbg["best_below_floor"] is True
    assert dbg["tape_cold"] is True
    assert dbg["has_catalyst"] is False
    assert dbg["regime_poor"] is True
    # best ross (0.40) is below the A+ floor (>= 0.70 conviction floor).
    assert dbg["best_ross"] == pytest.approx(0.40, abs=1e-6)


def test_subaplus_but_catalyst_present_does_not_sit_cash(happy):
    """Sub-A+ board but a FRESH CATALYST is present on a candidate -> regime is NOT poor ->
    the gate must NOT suppress (a single regime axis disagreeing defeats the AND). Proves the
    catalyst axis composes (the regime needs BOTH cold tape AND no catalyst)."""
    happy.setattr(aa.settings, "chili_momentum_no_asetup_sit_cash_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_momentum_continuation_ross_floor", 0.70, raising=False)
    happy.setattr(aa, "_tape_cold", lambda sym: True)  # tape cold...
    board = [
        # ...but THIS sub-A+ name carries a fresh catalyst -> regime not poor.
        _cand("NEWS", 8, 0.61, ross=0.50, signal={"news_catalyst": True}),
        _cand("DULL", 8, 0.60, ross=0.40, signal={"news_catalyst": False}),
    ]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: board)
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("skipped") != "no_asetup_sit_cash"
    assert out["armed"] == 1


def test_subaplus_but_hot_tape_does_not_sit_cash(happy):
    """Sub-A+ board, no catalyst, but at least one leader's tape is HOT -> tape-breadth is
    not cold -> regime not poor -> no suppression. Proves the tape-breadth axis composes
    (one hot leader flips the breadth to HOT, fail-open)."""
    happy.setattr(aa.settings, "chili_momentum_no_asetup_sit_cash_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_momentum_continuation_ross_floor", 0.70, raising=False)
    # First leader hot, the rest cold -> breadth HOT (a single hot leader is enough).
    happy.setattr(aa, "_tape_cold", lambda sym: sym != "WARM")
    board = [
        _cand("WARM", 8, 0.61, ross=0.50, signal={"news_catalyst": False}),
        _cand("COLD", 8, 0.60, ross=0.40, signal={"news_catalyst": False}),
    ]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: board)
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("skipped") != "no_asetup_sit_cash"
    assert out["armed"] == 1


def test_sit_cash_gate_off_is_byte_identical(happy):
    """Flag OFF (default): a sub-A+ poor-regime board still ARMS — the gate never runs.
    Confirms the kill-switch truly bypasses the suppression (the no-dark-flag contract)."""
    happy.setattr(aa.settings, "chili_momentum_no_asetup_sit_cash_enabled", False, raising=False)
    happy.setattr(aa, "_tape_cold", lambda sym: True)
    board = [_cand("DULL", 8, 0.61, ross=0.40, signal={"news_catalyst": False})]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: board)
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("skipped") != "no_asetup_sit_cash"
    assert out["armed"] == 1


# ════════════════════════════════════════════════════════════════════════════════════
# C. EXHAUSTED LEADER SKIPPED, FRESH ONE ARMED
# ════════════════════════════════════════════════════════════════════════════════════
def test_exhausted_leader_skipped_fresh_armed(happy):
    """A board where the TOP mover's break fires but the move is EXHAUSTED (faded-from-HOD
    AND cold-tape) -> the move-exhaustion abandon vetoes ITS arm -> the pass falls through to
    a FRESH mover whose trigger also fires and arms IT instead. Exercises the real
    ``_entry_trigger_fires`` -> ``_move_is_exhausted`` composition by patching only the leaf
    seams (front_side_state, _tape_cold) for the exhausted name."""
    happy.setattr(aa.settings, "chili_momentum_move_exhaustion_abandon_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66, raising=False)
    board = [
        _cand("TIRED", 8, 0.70, ross=0.90),  # top by viability, but exhausted
        _cand("FRESH", 8, 0.60, ross=0.80),  # lower, but a clean fresh thrust
    ]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: board)

    # Real _entry_trigger_fires path: patch its leaf seams so the structure read is
    # deterministic. The pullback trigger fires for both; TIRED is then abandoned by the
    # exhaustion gate (faded retrace 0.80 > 0.66 floor AND cold tape), FRESH survives
    # (retrace 0.00, near HOD -> never faded).
    import app.services.trading.momentum_neural.ross_momentum as rm
    import app.services.trading.momentum_neural.entry_gates as eg
    from app.services.trading import market_data

    # IMPORTANT: restore the REAL _entry_trigger_fires (the happy fixture stubs it). We need
    # its real pullback -> exhaustion-abandon composition to run for this test.
    happy.setattr(aa, "_entry_trigger_fires", _REAL_ENTRY_TRIGGER_FIRES)
    happy.setattr(aa.settings, "chili_momentum_entry_trigger_mode", "hybrid", raising=False)
    happy.setattr(aa.settings, "chili_momentum_auto_arm_trigger_parity_enabled", True, raising=False)
    happy.setattr(
        eg, "momentum_pullback_trigger",
        lambda df, **k: (True, "pullback_break_ok", {}), raising=False,
    )

    def _fss(df, **k):
        # the frame identity is the SimpleNamespace above; we key off the call-site symbol
        # via a thread-local-free trick: the exhausted name is probed with a marker df.
        return SimpleNamespace(retrace_from_hod=getattr(df, "_retrace", 0.0))

    happy.setattr(rm, "front_side_state", _fss, raising=False)

    # Distinguish the two names through fetch_ohlcv_df so front_side_state sees the right
    # retrace per symbol (TIRED faded, FRESH near HOD).
    def _fetch(symbol, *a, **k):
        retrace = 0.80 if symbol == "TIRED" else 0.0
        return SimpleNamespace(empty=False, _retrace=retrace)

    happy.setattr(market_data, "fetch_ohlcv_df", _fetch, raising=False)
    # Tape cold ONLY for the exhausted name (so the agreement rule trips only for TIRED).
    happy.setattr(aa, "_tape_cold", lambda sym: sym == "TIRED")

    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "FRESH"  # exhausted leader skipped, fresh one armed


def test_exhaustion_gate_off_arms_the_leader(happy):
    """With the exhaustion abandon flag OFF, the same faded leader is NOT vetoed and arms
    (it fires + is top-ranked). Pairs with the test above to prove the veto is what diverts
    the arm — not some other selection difference."""
    happy.setattr(aa.settings, "chili_momentum_move_exhaustion_abandon_enabled", False, raising=False)
    board = [
        _cand("TIRED", 8, 0.70, ross=0.90),
        _cand("FRESH", 8, 0.60, ross=0.80),
    ]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: board)
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    # No freshness info -> selection is by firing + viability order -> TIRED (top) wins.
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "TIRED"


# ── exhaustion helper composition (pure-logic, isolated) ──────────────────────────────
def test_move_is_exhausted_agreement_rule(happy):
    """``_move_is_exhausted`` abandons ONLY on FADED-FROM-HOD AND (cold-tape OR regressed).
    A lone faded flag (hot tape, no regression) must NOT abandon (single flicker)."""
    import app.services.trading.momentum_neural.ross_momentum as rm

    happy.setattr(aa.settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66, raising=False)
    happy.setattr(aa.settings, "chili_momentum_move_exhaustion_regress_frac", 0.20, raising=False)
    happy.setattr(rm, "front_side_state", lambda df, **k: SimpleNamespace(retrace_from_hod=0.80))

    df = SimpleNamespace(empty=False)
    row = _cand("ZZ", 8, 0.6, ross=0.90)

    # faded but tape HOT and not regressed (fresh peak just recorded) -> NOT abandoned.
    happy.setattr(aa, "_tape_cold", lambda sym: False)
    abandon, dbg = aa._move_is_exhausted("ZZ", df, row)
    assert dbg["faded_from_hod"] is True
    assert dbg["tape_cold"] is False
    assert abandon is False  # lone faded flag is not enough agreement

    # faded AND tape COLD -> abandoned.
    happy.setattr(aa, "_tape_cold", lambda sym: True)
    abandon2, dbg2 = aa._move_is_exhausted("ZZ", df, row)
    assert dbg2["faded_from_hod"] is True
    assert dbg2["tape_cold"] is True
    assert abandon2 is True


def test_move_is_exhausted_near_hod_never_abandons(happy):
    """A fresh near-HOD thrust (retrace 0.0) is never faded -> never abandoned even with cold
    tape. The structure short-circuit means the tape is not even consulted for a near-HOD
    name (faded is REQUIRED)."""
    import app.services.trading.momentum_neural.ross_momentum as rm

    happy.setattr(aa.settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66, raising=False)
    happy.setattr(rm, "front_side_state", lambda df, **k: SimpleNamespace(retrace_from_hod=0.05))
    tape_calls = {"n": 0}

    def _tape(sym):
        tape_calls["n"] += 1
        return True

    happy.setattr(aa, "_tape_cold", _tape)
    abandon, dbg = aa._move_is_exhausted("ZZ", SimpleNamespace(empty=False), _cand("ZZ", 8, 0.6, ross=0.9))
    assert dbg["faded_from_hod"] is False
    assert abandon is False
    assert tape_calls["n"] == 0  # near-HOD short-circuits the (DB-bound) tape read


# ════════════════════════════════════════════════════════════════════════════════════
# D. GATE COMPOSITION / ORDERING — sit-cash BEFORE time-of-day BEFORE the scan
# ════════════════════════════════════════════════════════════════════════════════════
def test_sit_cash_precedes_timeofday(happy):
    """Both new-initiation gates ENABLED and BOTH would suppress: the FIRST one in the pass
    (sit-cash, Guard 6) wins the skip reason — proving the documented order (sit-cash is
    consulted before the time-of-day cutoff)."""
    happy.setattr(aa.settings, "chili_momentum_no_asetup_sit_cash_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_momentum_timeofday_schedule_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_momentum_continuation_ross_floor", 0.70, raising=False)
    happy.setattr(aa, "_tape_cold", lambda sym: True)
    # Make the time-of-day gate ALSO want to suppress (force its internal decision True).
    happy.setattr(aa, "_should_suppress_late_day", lambda cands, **k: (True, {"reason": "fade_driven"}))
    board = [_cand("DULL", 8, 0.61, ross=0.40, signal={"news_catalyst": False})]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: board)
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "no_asetup_sit_cash"  # sit-cash is evaluated first


def test_timeofday_cutoff_suppresses_when_sit_cash_passes(happy):
    """Sit-cash PASSES (an A+ name is present so it never suppresses) but the time-of-day
    late-day cutoff fires -> ``skipped == "momentum_timeofday_schedule"``. Proves the
    time-of-day gate is the SECOND new-initiation gate and is consulted when the first does
    not skip."""
    happy.setattr(aa.settings, "chili_momentum_no_asetup_sit_cash_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_momentum_timeofday_schedule_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_momentum_continuation_ross_floor", 0.70, raising=False)
    # A+ present -> sit-cash returns no-suppress (asetup_present).
    board = [_cand("APLS", 8, 0.61, ross=0.95, signal={"news_catalyst": True})]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: board)
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    # Force the late-day cutoff to fire.
    happy.setattr(
        aa, "_should_suppress_late_day",
        lambda cands, **k: (True, {"reason": "fade_driven", "et_min": 900, "fallback": 870}),
    )
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "momentum_timeofday_schedule"
    assert out.get("armed", 0) == 0
    assert out["timeofday_schedule"]["reason"] == "fade_driven"


def test_both_new_initiation_gates_off_arms(happy):
    """Both gates OFF (default): a board that WOULD trip both still arms — the gates are
    pure additions behind their kill-switches (byte-identical to legacy when off)."""
    happy.setattr(aa.settings, "chili_momentum_no_asetup_sit_cash_enabled", False, raising=False)
    happy.setattr(aa.settings, "chili_momentum_timeofday_schedule_enabled", False, raising=False)
    happy.setattr(aa, "_tape_cold", lambda sym: True)
    board = [_cand("DULL", 8, 0.61, ross=0.40, signal={"news_catalyst": False})]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: board)
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "DULL"


def test_gates_run_after_concurrency_and_daily_loss_guards(happy):
    """The new-initiation gates must NOT run when an EARLIER guard already short-circuits the
    pass. Concurrency at cap -> skip is ``live_session_active`` and the sit-cash gate (which
    would otherwise want to suppress on this dull board) is never reached / never reported."""
    happy.setattr(aa.settings, "chili_momentum_no_asetup_sit_cash_enabled", True, raising=False)
    happy.setattr(aa, "_tape_cold", lambda sym: True)
    happy.setattr(aa, "_max_live_sessions", lambda: 5)
    happy.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 5)
    # Block displacement so the concurrency cap is the hard stop.
    happy.setattr(aa, "_try_displacement_for_full_slots", lambda db, *, uid, out: False)
    board = [_cand("DULL", 8, 0.61, ross=0.40, signal={"news_catalyst": False})]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: board)
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "live_session_active"
    assert "sit_cash" not in out  # the sit-cash gate never ran


# ── EXIT-ISOLATION INVARIANT: the gates are NEW-INITIATION ONLY ───────────────────────
def test_new_initiation_gates_never_touch_an_exit_path(happy, monkeypatch):
    """The ISOLATION INVARIANT: the sit-cash / time-of-day / exhaustion gates live only in
    ``run_auto_arm_pass`` (the NEW-ARM path). None of them imports or calls a live-runner exit
    primitive. We assert structurally: arming exit functions are NOT referenced by the
    pure-logic gate helpers, so a sit-cash suppression can never block/delay an exit.

    Concretely: invoking the three gate helpers directly NEVER calls any exit-shaped seam."""
    exit_calls = {"n": 0}

    # Sentinel: if any gate helper ever reached for an exit/flatten primitive, it would have
    # to import the live_runner — patch a tripwire there.
    import app.services.trading.momentum_neural.live_runner as lr
    for name in ("submit_exit", "flatten_position", "submit_live_exit"):
        if hasattr(lr, name):
            monkeypatch.setattr(lr, name, lambda *a, **k: exit_calls.__setitem__("n", exit_calls["n"] + 1), raising=False)

    board = [_cand("DULL", 8, 0.61, ross=0.40, signal={"news_catalyst": False})]
    monkeypatch.setattr(aa, "_tape_cold", lambda sym: True)
    monkeypatch.setattr(aa.settings, "chili_momentum_continuation_ross_floor", 0.70, raising=False)

    sit, _ = aa._should_sit_cash_no_asetup(_FakeDB(), board)
    late, _ = aa._should_suppress_late_day(board, now=datetime(2026, 6, 26, 19, 0))  # 15:00 ET
    exhausted, _ = aa._move_is_exhausted("DULL", SimpleNamespace(empty=False), board[0])

    # All three are pure decisions; none touched an exit primitive.
    assert exit_calls["n"] == 0
    # And each returns a plain (bool, dict) decision (no side-effect contract leaked).
    assert isinstance(sit, bool) and isinstance(late, bool) and isinstance(exhausted, bool)


# ════════════════════════════════════════════════════════════════════════════════════
# E. THE GATE LEAF HELPERS — specific-value composition (pure logic)
# ════════════════════════════════════════════════════════════════════════════════════
def test_asetup_quality_floor_is_max_of_convict_floor_and_distribution(happy):
    """``_asetup_quality_floor`` = max(conviction_floor, median - margin*std). With a tight,
    HIGH board the distribution term raises the bar above the floor; with a low board the bar
    collapses to the conviction floor. Assert both regimes to the exact value."""
    happy.setattr(aa.settings, "chili_momentum_continuation_ross_floor", 0.70, raising=False)
    happy.setattr(aa.settings, "chili_momentum_no_asetup_sit_cash_margin_multiple", 1.0, raising=False)

    # High, zero-spread board -> std 0 -> adaptive == median (0.95) > floor -> bar 0.95.
    hi = aa._asetup_quality_floor([0.95, 0.95, 0.95])
    assert hi == pytest.approx(0.95, abs=1e-9)

    # Low board -> adaptive below the floor -> bar clamps to the conviction floor 0.70.
    lo = aa._asetup_quality_floor([0.30, 0.35, 0.40])
    assert lo == pytest.approx(0.70, abs=1e-9)

    # Empty / unreadable distribution -> conviction floor (safe default).
    assert aa._asetup_quality_floor([]) == pytest.approx(0.70, abs=1e-9)


def test_regime_is_poor_requires_both_axes(happy):
    """``_regime_is_poor`` is an AND: poor ONLY when tape cold AND no catalyst. Any single
    disagreement => not poor."""
    assert aa._regime_is_poor(tape_cold=True, has_catalyst=False) is True
    assert aa._regime_is_poor(tape_cold=True, has_catalyst=True) is False
    assert aa._regime_is_poor(tape_cold=False, has_catalyst=False) is False
    assert aa._regime_is_poor(tape_cold=False, has_catalyst=True) is False


def test_exhaustion_abandon_eligible_agreement(happy):
    """``_exhaustion_abandon_eligible`` is faded AND (cold OR regressed). A faded-only flag
    is not enough; faded plus either corroborator abandons."""
    assert aa._exhaustion_abandon_eligible(faded=True, tape_cold=False, regressed=False) is False
    assert aa._exhaustion_abandon_eligible(faded=True, tape_cold=True, regressed=False) is True
    assert aa._exhaustion_abandon_eligible(faded=True, tape_cold=False, regressed=True) is True
    assert aa._exhaustion_abandon_eligible(faded=False, tape_cold=True, regressed=True) is False


def test_best_setup_quality_below_floor_failopen_on_empty_board(happy):
    """FAIL-OPEN: an unreadable board (no ross scores) returns (False, ...) -> the gate can
    never suppress on absent data. A scored board returns the true max + below flag."""
    below, dbg = aa._best_setup_quality_below_floor([_cand("NOSCORE", 8, 0.6)], floor=0.70)
    assert below is False
    assert dbg["best_ross"] is None

    board = [_cand("A", 8, 0.6, ross=0.40), _cand("B", 8, 0.6, ross=0.62)]
    below2, dbg2 = aa._best_setup_quality_below_floor(board, floor=0.70)
    assert below2 is True  # best 0.62 < 0.70
    assert dbg2["best_ross"] == pytest.approx(0.62, abs=1e-6)
    assert dbg2["n_scored"] == 2


def test_should_suppress_late_day_fade_driven_vs_clock(happy):
    """``_should_suppress_late_day`` composition: PAST the fallback clock AND faded regime ->
    suppress; past the clock but a strong (non-faded) regime -> NO suppress. Before the clock
    -> never suppress regardless of fade. Drives the REAL helper with a pinned ET clock + the
    real regime sub-helpers via patched tape/catalyst leaves."""
    happy.setattr(aa.settings, "chili_momentum_timeofday_fade_enabled", True, raising=False)
    # fallback 14:30 ET. 15:00 ET = past; 13:00 ET = before. Use UTC (ET = UTC-4 in June DST).
    past_clock = datetime(2026, 6, 26, 19, 0)   # 15:00 ET
    before_clock = datetime(2026, 6, 26, 17, 0)  # 13:00 ET
    board = [_cand("X", 8, 0.6, ross=0.5, signal={"news_catalyst": False})]

    # Faded regime: cold tape + no catalyst.
    happy.setattr(aa, "_tape_cold", lambda sym: True)
    sup_faded, dbg_faded = aa._should_suppress_late_day(board, now=past_clock)
    assert sup_faded is True
    assert dbg_faded["reason"] == "fade_driven"

    # Strong regime (hot tape) past the clock -> NOT suppressed.
    happy.setattr(aa, "_tape_cold", lambda sym: False)
    sup_strong, dbg_strong = aa._should_suppress_late_day(board, now=past_clock)
    assert sup_strong is False
    assert dbg_strong["reason"] == "afternoon_still_strong"

    # Before the fallback clock -> never suppressed even if faded.
    happy.setattr(aa, "_tape_cold", lambda sym: True)
    sup_before, dbg_before = aa._should_suppress_late_day(board, now=before_clock)
    assert sup_before is False
    assert dbg_before["reason"] == "before_fallback_clock"


def test_should_suppress_late_day_clock_only_when_fade_disabled(happy):
    """With fade-check DISABLED, the cutoff is clock-only: past the fallback => suppress
    regardless of regime strength (the documented fallback degenerate mode)."""
    happy.setattr(aa.settings, "chili_momentum_timeofday_fade_enabled", False, raising=False)
    happy.setattr(aa, "_tape_cold", lambda sym: False)  # hot tape, would not be 'faded'
    board = [_cand("X", 8, 0.6, ross=0.9, signal={"news_catalyst": True})]
    sup, dbg = aa._should_suppress_late_day(board, now=datetime(2026, 6, 26, 19, 0))  # 15:00 ET
    assert sup is True
    assert dbg["reason"] == "past_fallback_clock_only"


def test_prime_window_size_multiplier_bounds(happy):
    """The prime-window size lever is BOUNDED-UPWARD (>= 1.0, <= max) inside the window and
    EXACTLY 1.0 outside / when disabled. Never a shrink, never a veto."""
    happy.setattr(aa.settings, "chili_momentum_timeofday_schedule_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_momentum_timeofday_prime_window_size_mult_max", 1.5, raising=False)
    # 09:00 ET (13:00 UTC, weekday Fri 2026-06-26) is inside the default 04:00-10:30 window.
    mult_in, dbg_in = aa.prime_window_size_multiplier(now=datetime(2026, 6, 26, 13, 0))
    assert mult_in == pytest.approx(1.5, abs=1e-9)
    assert dbg_in["in_prime"] is True
    # 15:00 ET (19:00 UTC) is outside -> exactly 1.0.
    mult_out, _ = aa.prime_window_size_multiplier(now=datetime(2026, 6, 26, 19, 0))
    assert mult_out == 1.0
    # Flag OFF -> exactly 1.0 regardless of clock.
    happy.setattr(aa.settings, "chili_momentum_timeofday_schedule_enabled", False, raising=False)
    assert aa.prime_window_size_multiplier(now=datetime(2026, 6, 26, 13, 0))[0] == 1.0
