"""Autonomous auto-arm-live guard + selection logic (Ross-style)."""
from __future__ import annotations

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


class _FakeDB:
    def add(self, *_a, **_k) -> None:
        pass

    def commit(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def rollback(self) -> None:
        # Exercised by the read-txn release before the probe phase.
        pass

    def expunge_all(self) -> None:
        pass


def _cand(symbol="RSC-USD", variant_id=8, score=0.61):
    return SimpleNamespace(
        symbol=symbol,
        variant_id=variant_id,
        viability_score=score,
        execution_readiness_json={},
    )


@pytest.fixture
def happy(monkeypatch):
    """Patch every seam to the happy path; tests override one to exercise a guard."""
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_live_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_live_scheduler_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_live_runner_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_autotrader_user_id", 1, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_decouple_watching_enabled", False, raising=False)
    # ── Crypto live-arm gates added AFTER this fixture (PR #675 crypto-live-off,
    #    PR #685 liquidity floor): the happy-path candidates are crypto (-USD), so
    #    neutralize the new gates here. These are settings/time/data seams, not the
    #    selection logic the tests exercise — left at prod defaults the crypto path
    #    silently never arms (crypto_live_arm OFF, US-session pause, UTC dead-band). ──
    monkeypatch.setattr(aa.settings, "chili_momentum_crypto_live_arm_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_crypto_pause_during_us_session", False, raising=False)
    monkeypatch.setattr(aa.settings, "chili_crypto_schedule_enabled", False, raising=False)
    monkeypatch.setattr(
        aa.settings,
        "chili_momentum_equity_execution_via_alpaca_paper",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        aa.settings,
        "chili_momentum_crypto_execution_via_alpaca_paper",
        False,
        raising=False,
    )
    monkeypatch.setattr(aa.settings, "chili_momentum_ross_equity_universe_required", False, raising=False)
    # Post-reap cooldown (#701) writes reaped crypto names into a module-global dict;
    # disable it here so a happy-path arm is deterministic and never order-dependent on
    # whatever the reaper unit-tests left in aa._REAP_COOLDOWN (0 = cooldown off).
    monkeypatch.setattr(aa.settings, "chili_momentum_reap_cooldown_sec", 0, raising=False)
    # Pin ONE arm per pass so the single-candidate selection assertions hold; the A6
    # multi-arm fan-out (default 3) is a separate behaviour from WHICH name is chosen.
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_max_arms_per_pass", 1, raising=False)
    # The equity late-window gate in _live_armable (no NEW equity arms >=14:30 ET) is
    # wall-clock driven — pin it to a productive window so the equity selection tests
    # are deterministic regardless of when the suite runs (no test asserts the late path).
    monkeypatch.setattr(market_profile, "schedule_window_now", lambda *a, **k: "midday", raising=False)
    # Candidate-selection tests exercise arm policy, not the wall clock. Keep the
    # default happy path deterministic on weekends; the dedicated market-closed
    # regression below overrides this seam explicitly.
    monkeypatch.setattr(aa, "_symbol_market_open", lambda _symbol: True)
    monkeypatch.setattr(governance, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 0)
    monkeypatch.setattr(portfolio_risk, "check_portfolio_drawdown_breaker", lambda db, uid: (False, None))
    monkeypatch.setattr(automation_query, "expire_stale_live_arm_sessions", lambda db, *, user_id: 0)
    monkeypatch.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: [_cand()])
    monkeypatch.setattr(aa, "_symbol_free", lambda db, sym, uid: True)
    # Broker for the candidate's venue is connected/ready by default; the
    # broker-not-ready guard test overrides this seam.
    monkeypatch.setattr(aa, "_venue_broker_ready_for", lambda sym, cache: True)
    # Crypto liquidity floor (#685) is a data seam (the viability row carries no
    # turnover for synthetic symbols -> "liquidity_data_missing"); happy path passes it.
    monkeypatch.setattr(
        aa, "crypto_liquidity_ok", lambda symbol, row, adapter=None: (True, {"liquidity_gate": "ok"}, None)
    )
    monkeypatch.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    # Default freshness UNKNOWN (None) — keeps existing tests network-free and on the
    # arm-on-active-break contract; freshness-specific tests override this seam.
    monkeypatch.setattr(aa, "_candidate_freshness", lambda sym: None)
    monkeypatch.setattr(
        account_identity,
        "read_current_non_alpaca_account_identity",
        lambda _family: {
            "ok": True,
            "identity": "auto-arm-test-account-v1",
            "reason": None,
        },
    )
    monkeypatch.setattr(aa, "_symbol_loss_guards", lambda db, **kwargs: (set(), {}))
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
    monkeypatch.setattr(coinbase_service, "connect", lambda: {"ok": True})
    monkeypatch.setattr(
        operator_actions, "begin_live_arm",
        lambda db, **k: {"ok": True, "arm_token": "tok", "session_id": 99},
    )
    monkeypatch.setattr(
        operator_actions, "confirm_live_arm",
        lambda db, **k: {"ok": True, "state": "queued_live"},
    )
    return monkeypatch


def test_happy_path_arms(happy):
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "RSC-USD"
    assert out["session_id"] == 99
    assert out["state"] == "queued_live"


def test_crypto_candidate_never_creates_alpaca_twin(happy):
    """A Coinbase crypto primary must never spawn the equity-only Alpaca twin."""
    calls: list[tuple[str, str]] = []

    happy.setattr(
        aa.settings,
        "chili_momentum_crypto_execution_via_alpaca_paper",
        False,
        raising=False,
    )
    happy.setattr(aa.settings, "chili_momentum_alpaca_twin_arm_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_alpaca_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_alpaca_paper", True, raising=False)
    happy.setattr(aa.settings, "chili_alpaca_api_key", "paper-test-key", raising=False)
    happy.setattr(
        aa,
        "_alpaca_lists_symbol",
        lambda _symbol: (_ for _ in ()).throw(AssertionError("crypto twin probe must not run")),
    )

    def _begin(_db, **kwargs):
        calls.append((kwargs["symbol"], kwargs["execution_family"]))
        return {"ok": True, "arm_token": "tok", "session_id": len(calls)}

    happy.setattr(operator_actions, "begin_live_arm", _begin)

    out = aa.run_auto_arm_pass(_FakeDB())

    assert out["armed"] == 1
    assert calls == [("RSC-USD", "coinbase_spot")]
    assert "alpaca_twin_session_id" not in out


def test_alpaca_twin_loss_denial_does_not_undo_primary_arm(happy):
    calls: list[tuple[str, str]] = []
    happy.setattr(
        aa.settings, "chili_momentum_auto_arm_crypto_only", False, raising=False
    )
    happy.setattr(
        aa.settings, "chili_momentum_auto_arm_equity_only", False, raising=False
    )
    happy.setattr(
        aa.settings,
        "chili_momentum_equity_execution_via_alpaca_paper",
        False,
        raising=False,
    )
    happy.setattr(
        aa.settings, "chili_momentum_alpaca_twin_arm_enabled", True, raising=False
    )
    happy.setattr(aa.settings, "chili_alpaca_enabled", True, raising=False)
    happy.setattr(aa.settings, "chili_alpaca_paper", True, raising=False)
    happy.setattr(aa.settings, "chili_alpaca_api_key", "paper-key", raising=False)
    happy.setattr(
        aa,
        "_fresh_live_eligible_candidates",
        lambda db, *, limit: [_cand("PRIMARY", 8, 0.75)],
    )
    happy.setattr(
        aa,
        "_alpaca_twin_loss_guard_decision",
        lambda db, **kwargs: (
            False,
            {
                "allowed": False,
                "reason": "alpaca_twin_consecutive_loss_halt",
                "coverage_grade": "CURRENT_LIVE_COMPLETE",
            },
            {
                "account_scope": "alpaca:paper",
                "account_identity": "paper-account",
            },
        ),
    )
    happy.setattr(
        aa,
        "_alpaca_lists_symbol",
        lambda _symbol: (_ for _ in ()).throw(
            AssertionError("denied twin must not probe provider/listing")
        ),
    )

    def _begin(_db, **kwargs):
        calls.append((kwargs["symbol"], kwargs["execution_family"]))
        return {"ok": True, "arm_token": "tok", "session_id": len(calls)}

    happy.setattr(operator_actions, "begin_live_arm", _begin)

    out = aa.run_auto_arm_pass(_FakeDB())

    assert out["armed"] == 1
    assert calls == [("PRIMARY", "robinhood_spot")]
    assert out["alpaca_twin_skipped"] == "alpaca_twin_consecutive_loss_halt"
    assert "alpaca_twin_session_id" not in out


def test_crypto_resolution_defaults_away_from_alpaca_paper(monkeypatch):
    from app.services.trading import execution_family_registry as registry
    from app.services.trading.venue import alpaca_spot

    monkeypatch.setattr(aa.settings, "chili_alpaca_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_alpaca_api_key", "paper-test-key", raising=False)
    monkeypatch.setattr(
        aa.settings,
        "chili_momentum_crypto_execution_via_alpaca_paper",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        alpaca_spot,
        "alpaca_lists_symbol",
        lambda _symbol: (_ for _ in ()).throw(AssertionError("flag-off must not probe Alpaca")),
    )

    assert registry.resolve_execution_family_for_symbol("BTC-USD") == "coinbase_spot"


def test_lane_execution_family_uses_authoritative_direct_alpaca_route(monkeypatch):
    from app.services.trading import execution_family_registry as registry

    monkeypatch.setattr(
        aa.settings, "chili_momentum_auto_arm_crypto_only", False, raising=False
    )
    monkeypatch.setattr(
        aa.settings,
        "chili_momentum_equity_execution_via_alpaca_paper",
        True,
        raising=False,
    )
    calls: list[tuple[str, str]] = []

    def _resolve(symbol: str, *, mode: str = "live") -> str:
        calls.append((symbol, mode))
        return "alpaca_spot"

    monkeypatch.setattr(registry, "resolve_execution_family_for_symbol", _resolve)

    assert aa._lane_execution_family() == "alpaca_spot"
    assert calls == [("SPY", "live")]


def test_alpaca_arm_budget_uses_current_watcher_and_rail_headroom(monkeypatch):
    import app.services.trading.momentum_neural.risk_evaluator as risk_evaluator

    monkeypatch.setattr(
        aa.settings,
        "chili_momentum_auto_arm_max_arms_per_pass",
        1,
        raising=False,
    )
    monkeypatch.setattr(
        risk_evaluator,
        "alpaca_paper_arm_resource_capacity",
        lambda *_args, **_kwargs: {
            "available": True,
            "risk_usd": 0.0,
            "field_size": 12,
            "watching": 4,
            "capacity": 10,
            "headroom": 6,
            "provenance": {
                "authority": "resource_only_watch_fanout",
                "financial_authority": "final_adaptive_reservation",
            },
        },
    )
    monkeypatch.setattr(
        aa,
        "_current_rail_dispatch_capacity",
        lambda **_kwargs: {
            "available": True,
            "enabled": True,
            "call_headroom": 4,
            "provenance": {"authority": "current_adaptive_rail_bucket"},
        },
    )

    budget, provenance = aa._resolved_auto_arm_capacity_budget(
        _FakeDB(),
        user_id=1,
        execution_family="alpaca_spot",
        candidate_count=9,
    )

    assert budget == 4
    assert provenance["resolved_budget"] == 4
    assert provenance["legacy_max_arms_per_pass_used"] is False
    assert provenance["financial_authority"] == "final_adaptive_reservation"


def test_non_alpaca_arm_budget_retains_configured_behavior(monkeypatch):
    monkeypatch.setattr(
        aa.settings,
        "chili_momentum_auto_arm_max_arms_per_pass",
        2,
        raising=False,
    )

    budget, provenance = aa._resolved_auto_arm_capacity_budget(
        _FakeDB(),
        user_id=1,
        execution_family="coinbase_spot",
        candidate_count=9,
    )

    assert budget == 2
    assert provenance["policy"] == "legacy_configured_non_alpaca"
    assert provenance["configured_max_arms_per_pass"] == 2


def test_flag_off_skips(happy):
    happy.setattr(aa.settings, "chili_momentum_auto_arm_live_enabled", False, raising=False)
    assert aa.run_auto_arm_pass(_FakeDB())["skipped"] == "flag_off"


def test_live_runner_off_skips(happy):
    happy.setattr(aa.settings, "chili_momentum_live_runner_enabled", False, raising=False)
    assert aa.run_auto_arm_pass(_FakeDB())["skipped"] == "live_runner_off"


def test_kill_switch_skips(happy):
    happy.setattr(governance, "is_kill_switch_active", lambda: True)
    assert aa.run_auto_arm_pass(_FakeDB())["skipped"] == "kill_switch"


def test_concurrency_skips(happy):
    # at the (adaptive) live-session cap -> skip. Pin the cap deterministically.
    happy.setattr(aa, "_max_live_sessions", lambda: 5)
    happy.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 5)
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "live_session_active"
    assert out["active"] == 5


def test_arms_when_below_concurrency_cap(happy):
    # 3 active < 5 cap -> still arms a new one
    happy.setattr(aa, "_max_live_sessions", lambda: 5)
    happy.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 3)
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1


def test_drawdown_breaker_skips(happy):
    happy.setattr(portfolio_risk, "check_portfolio_drawdown_breaker", lambda db, uid: (True, "dd_15pct"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "drawdown_breaker"
    assert out["dd_reason"] == "dd_15pct"


def test_no_candidates_skips(happy):
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: [])
    assert aa.run_auto_arm_pass(_FakeDB())["skipped"] == "no_fresh_live_eligible"


def test_no_active_trigger_skips(happy):
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (False, "waiting_for_break"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "no_active_trigger"
    assert out["scanned"] == 1


def test_symbol_owned_by_other_skips_candidate(happy):
    happy.setattr(aa, "_symbol_free", lambda db, sym, uid: False)
    out = aa.run_auto_arm_pass(_FakeDB())
    # the only candidate is owned by another autopilot -> nothing arms
    assert out["skipped"] == "no_active_trigger"


def test_broker_not_ready_skips_candidate_at_selection(happy):
    # Venue disconnected (e.g. RH token expired): the candidate is dropped at SELECTION
    # so the single per-pass arm can fall through to a fillable venue instead of being
    # burned on a name whose confirm would fail broker_not_ready (the lane-stall bug).
    happy.setattr(aa, "_venue_broker_ready_for", lambda sym, cache: False)
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 0
    assert out["broker_not_ready_skipped"] == 1
    assert out["skipped"] == "no_active_trigger"  # only candidate dropped -> nothing eligible


def test_read_txn_released_before_probe(happy):
    # The read transaction (incl. the trading_automation_sessions SELECT from
    # busy_symbols) MUST be released before the network-bound OHLCV probe phase, else
    # it sits idle-in-transaction across the multi-second probe wave and the per-
    # connection idle-in-transaction timeout kills the connection.
    calls = {"rollback": 0, "expunge_all": 0}

    class _TrackDB(_FakeDB):
        def rollback(self) -> None:
            calls["rollback"] += 1

        def expunge_all(self) -> None:
            calls["expunge_all"] += 1

    out = aa.run_auto_arm_pass(_TrackDB())
    assert out["armed"] == 1  # happy path still arms
    assert calls["expunge_all"] >= 1
    assert calls["rollback"] >= 1


def test_begin_blocked_does_not_arm(happy):
    happy.setattr(operator_actions, "begin_live_arm", lambda db, **k: {"ok": False, "error": "risk_blocked"})
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 0
    assert out["skipped"] == "begin_blocked"
    assert out["begin_error"] == "risk_blocked"


def test_confirm_blocked_does_not_arm(happy):
    happy.setattr(operator_actions, "confirm_live_arm", lambda db, **k: {"ok": False, "error": "broker_not_ready"})
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 0
    assert out["skipped"] == "confirm_blocked"
    assert out["confirm_error"] == "broker_not_ready"


def test_deduped_begin_skips_confirm(happy):
    # begin_live_arm dedups (the symbol already holds an active live session):
    # it returns that session's token, whose session is no longer arm-pending.
    # The pass must NOT forward that stale token to confirm_live_arm (which
    # would fail invalid_token and churn) — it reports already_active instead.
    def _confirm_must_not_run(*a, **k):
        raise AssertionError("confirm_live_arm must not run on a deduped begin")

    happy.setattr(
        operator_actions,
        "begin_live_arm",
        lambda db, **k: {
            "ok": True,
            "deduped": True,
            "session_id": 77,
            "arm_token": "stale-token",
            "state": "watching_live",
        },
    )
    happy.setattr(operator_actions, "confirm_live_arm", _confirm_must_not_run)
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "already_active"
    assert out["session_id"] == 77
    assert out.get("armed", 0) == 0


def test_daily_loss_cap_skips_scan(happy, monkeypatch):
    # Today's realized loss already breached the equity-relative daily cap: the pass
    # must early-out with skipped=daily_loss_cap (Guard 4) instead of scanning +
    # churning candidates that begin_live_arm would all risk_block on the same cap.
    from app.services.trading.momentum_neural import risk_evaluator, risk_policy

    # Legacy single-cap Guard 4 (the per-broker path is its own, covered in
    # tests/test_per_broker_daily_loss.py). Exercise the legacy branch explicitly.
    monkeypatch.setattr(aa.settings, "chili_per_broker_daily_loss_enabled", False)
    monkeypatch.setattr(risk_policy, "equity_relative_daily_loss_cap", lambda *a, **k: 130.0)
    monkeypatch.setattr(
        risk_evaluator, "_daily_realized_pnl", lambda db, uid, **kwargs: -131.0
    )
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "daily_loss_cap"
    assert out.get("armed", 0) == 0
    assert out["daily_pnl_usd"] == -131.0


def test_within_daily_loss_cap_does_not_skip(happy, monkeypatch):
    # Comfortably within the cap -> Guard 4 must NOT trip (the pass proceeds to arm).
    from app.services.trading.momentum_neural import risk_evaluator, risk_policy

    monkeypatch.setattr(aa.settings, "chili_per_broker_daily_loss_enabled", False)
    monkeypatch.setattr(risk_policy, "equity_relative_daily_loss_cap", lambda *a, **k: 130.0)
    monkeypatch.setattr(
        risk_evaluator, "_daily_realized_pnl", lambda db, uid, **kwargs: -10.0
    )
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("skipped") != "daily_loss_cap"


def test_profit_giveback_halts_scan(happy, monkeypatch):
    # Today's realized PnL peaked green ($200) then gave back >=50% (down to $90): the
    # pass must early-out with skipped=profit_giveback (Guard 5) — lock in the green day
    # instead of churning candidates that begin_live_arm would all risk_block.
    from app.services.trading.momentum_neural import risk_evaluator

    monkeypatch.setattr(
        risk_evaluator,
        "evaluate_profit_giveback_halt",
        lambda db, **k: {
            "halted": True, "armed": True, "peak_pnl_usd": 200.0, "daily_pnl_usd": 90.0,
            "activation_threshold_usd": 110.0, "giveback_fraction": 0.5, "giveback_floor_usd": 100.0,
        },
    )
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "profit_giveback"
    assert out.get("armed", 0) == 0
    assert out["peak_pnl_usd"] == 200.0
    assert out["daily_pnl_usd"] == 90.0
    assert out["giveback_fraction"] == 0.5


def test_within_giveback_band_does_not_skip(happy, monkeypatch):
    # Peaked $200, only down to $150 (gave back 25% < 50%): Guard 5 must NOT trip — the
    # pass proceeds to arm the fresh mover.
    from app.services.trading.momentum_neural import risk_evaluator

    monkeypatch.setattr(
        risk_evaluator,
        "evaluate_profit_giveback_halt",
        lambda db, **k: {
            "halted": False, "armed": True, "peak_pnl_usd": 200.0, "daily_pnl_usd": 150.0,
            "activation_threshold_usd": 110.0, "giveback_fraction": 0.5, "giveback_floor_usd": 100.0,
        },
    )
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("skipped") != "profit_giveback"
    assert out["armed"] == 1


def test_dedupe_by_symbol_keeps_best_variant_distinct_symbols():
    # 10 RSC variants (top), then FIDA, then SOL — dedupe must yield 3 distinct symbols
    rows = (
        [_cand("RSC-USD", v, 0.65) for v in range(1, 11)]
        + [_cand("FIDA-USD", 2, 0.63)]
        + [_cand("SOL-USD", 5, 0.61)]
    )
    out = aa._dedupe_by_symbol(rows, limit=10)
    syms = [r.symbol for r in out]
    assert syms == ["RSC-USD", "FIDA-USD", "SOL-USD"]  # one per symbol, order preserved


def test_dedupe_respects_limit():
    rows = [_cand(f"S{i}-USD", 1, 0.6 - i * 0.01) for i in range(20)]
    out = aa._dedupe_by_symbol(rows, limit=5)
    assert len(out) == 5
    assert [r.symbol for r in out] == [f"S{i}-USD" for i in range(5)]


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_a, **_k):
        return self

    def all(self):
        return self._rows


class _DBWithRows(_FakeDB):
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_a, **_k):
        return _FakeQuery(self._rows)


def test_is_coinbase_tradeable_symbol():
    assert aa._is_coinbase_tradeable_symbol("KAIO-USD") is True
    assert aa._is_coinbase_tradeable_symbol("BTC-USDC") is True
    assert aa._is_coinbase_tradeable_symbol("ARKK") is False
    assert aa._is_coinbase_tradeable_symbol("CLSK") is False
    assert aa._is_coinbase_tradeable_symbol("") is False


def test_equity_candidate_skipped_even_if_higher_viability(happy):
    # ARKK (equity) ranks higher + its trigger fires, but the coinbase_spot lane
    # cannot trade it -> must be skipped; the crypto KAIO is armed instead.
    happy.setattr(
        aa, "_fresh_live_eligible_candidates",
        lambda db, *, limit: [_cand("ARKK", 8, 0.80), _cand("KAIO-USD", 8, 0.65)],
    )
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "momentum_ok"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "KAIO-USD"


def test_market_closed_equity_skipped(happy):
    # crypto_only OFF so equities can flow; an equity whose market is CLOSED must be
    # skipped (would not fill), the 24/7 crypto armed instead.
    happy.setattr(aa.settings, "chili_momentum_auto_arm_crypto_only", False, raising=False)
    happy.setattr(
        aa, "_fresh_live_eligible_candidates",
        lambda db, *, limit: [_cand("ARKK", 8, 0.80), _cand("KAIO-USD", 8, 0.65)],
    )
    happy.setattr(aa, "_symbol_market_open", lambda sym: sym.endswith("-USD"))
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "momentum_ok"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "KAIO-USD"  # ARKK skipped: market closed


def test_market_open_helper_crypto_always_open(monkeypatch):
    # crypto is 24/7 -> always True regardless of market_open_now
    assert aa._symbol_market_open("BTC-USD") is True


def test_reaper_cancels_stale_pre_entry_sessions(monkeypatch):
    from datetime import datetime
    cancelled = []
    monkeypatch.setattr(
        operator_actions, "begin_live_arm", lambda *a, **k: {"ok": False},
    )  # unused
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query.cancel_automation_session",
        lambda db, *, user_id, session_id: cancelled.append(session_id) or {"ok": True},
    )
    # risk_snapshot_json={} : the 2026-08-27 broker-order guard reads the snapshot
    # and SKIPS (fail-closed) a row it cannot read — a bare SimpleNamespace was
    # being skipped, so this test was red on main until 2026-09-02.
    rows = [
        SimpleNamespace(id=8, symbol="RSC-USD", state="watching_live", risk_snapshot_json={}),
        SimpleNamespace(id=9, symbol="FIDA-USD", state="queued_live", risk_snapshot_json={}),
    ]
    n = aa._reap_stale_watching_sessions(_DBWithRows(rows), user_id=1, now=datetime.utcnow())
    assert n == 2
    assert cancelled == [8, 9]


def test_reaper_returns_zero_when_none(monkeypatch):
    from datetime import datetime
    n = aa._reap_stale_watching_sessions(_DBWithRows([]), user_id=1, now=datetime.utcnow())
    assert n == 0


def test_pass_surfaces_reaped_count(happy):
    happy.setattr(aa, "_reap_stale_watching_sessions", lambda db, *, user_id, now: 1)
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("reaped") == 1
    assert out["armed"] == 1  # reaping a stale slot then arming the fresh mover


def test_picks_first_firing_candidate(happy):
    cands = [_cand("AAA-USD", 8, 0.70), _cand("BBB-USD", 8, 0.65), _cand("CCC-USD", 8, 0.60)]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: cands)
    # only BBB is surging now
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (sym == "BBB-USD", "pullback_break_ok" if sym == "BBB-USD" else "waiting_for_break"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "BBB-USD"


# ── Selection->entry alignment (M4 keystone): freshness filter + re-rank ───────

def _fresh(is_fresh: bool, pos: float, score: float | None = None):
    """Stand-in for ross_momentum.ImpulseFreshness (only the read attrs matter)."""
    return SimpleNamespace(
        is_fresh=is_fresh,
        position_in_range=pos,
        score=score if score is not None else min(1.0, max(0.0, pos)),
    )


def test_watches_freshest_known_fresh_when_none_firing(happy):
    """No break is firing, but two names are positively in a fresh up-impulse — arm the
    FRESHEST one to WATCH (instead of skipping), and prefer it over the lower-position one."""
    cands = [_cand("LO-USD", 8, 0.80), _cand("HI-USD", 8, 0.55)]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: cands)
    happy.setattr(aa, "_symbol_market_open", lambda sym: True)
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (False, "waiting_for_break"))
    # HI-USD is fresher (closer to its recent high) despite lower 24h viability.
    fr = {"LO-USD": _fresh(True, 0.55), "HI-USD": _fresh(True, 0.97)}
    happy.setattr(aa, "_candidate_freshness", lambda sym: fr[sym])
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "HI-USD"  # re-ranked by freshness, not viability
    assert out["chosen_firing"] is False
    assert str(out["trigger"]).startswith("fresh_watch:")


def test_drops_faded_non_firing_candidate(happy):
    """A faded 24h mover that is not firing is NOT watched — the slot stays free."""
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: [_cand("FADED-USD")])
    happy.setattr(aa, "_symbol_market_open", lambda sym: True)
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (False, "pullback_too_deep"))
    happy.setattr(aa, "_candidate_freshness", lambda sym: _fresh(False, 0.12))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("armed", 0) == 0
    assert out["skipped"] == "no_active_trigger"
    assert out["faded_skipped"] == 1


def test_firing_break_beats_fresh_watch_even_if_faded(happy):
    """An actively-firing break is always a valid entry — it wins over a fresh watch
    candidate, even if the firing name's current price reads 'faded'."""
    cands = [_cand("FIRE-USD", 8, 0.50), _cand("WATCH-USD", 8, 0.90)]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: cands)
    happy.setattr(aa, "_symbol_market_open", lambda sym: True)
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (sym == "FIRE-USD", "pullback_break_ok" if sym == "FIRE-USD" else "waiting_for_break"))
    fr = {"FIRE-USD": _fresh(False, 0.30), "WATCH-USD": _fresh(True, 0.95)}
    happy.setattr(aa, "_candidate_freshness", lambda sym: fr[sym])
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "FIRE-USD"
    assert out["chosen_firing"] is True
    assert out["trigger"] == "pullback_break_ok"


def test_reranks_among_simultaneous_firing_by_freshness(happy):
    """When several names fire at once, arm the freshest of them."""
    cands = [_cand("A-USD", 8, 0.80), _cand("B-USD", 8, 0.60)]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: cands)
    happy.setattr(aa, "_symbol_market_open", lambda sym: True)
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    fr = {"A-USD": _fresh(True, 0.62), "B-USD": _fresh(True, 0.99)}
    happy.setattr(aa, "_candidate_freshness", lambda sym: fr[sym])
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "B-USD"  # fresher firing name wins despite lower viability


def test_require_fresh_off_restores_arm_on_break_only(happy):
    """Knob OFF: a fresh-but-not-firing name is NOT watched — old contract preserved."""
    happy.setattr(aa.settings, "chili_momentum_auto_arm_require_fresh_impulse", False, raising=False)
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: [_cand("HI-USD", 8, 0.7)])
    happy.setattr(aa, "_symbol_market_open", lambda sym: True)
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (False, "waiting_for_break"))
    happy.setattr(aa, "_candidate_freshness", lambda sym: _fresh(True, 0.98))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("armed", 0) == 0
    assert out["skipped"] == "no_active_trigger"


# ── Wide probe net + time budget (a fresh-firing #11+ name is no longer starved by the
#    old top-10-by-viability truncation; the net is widened but the wave stays bounded) ──

def test_scan_limit_widened_beyond_old_top10():
    """The candidate probe net must be meaningfully wider than the old top-10 truncation
    that starved a fresh-firing mid-viability name (NPT Jun-8 ranked #11+)."""
    assert aa._scan_limit() >= 25


def test_probe_time_budget_reads_and_floors(happy):
    """The probe budget reads the setting and floors at 1.0s (never zero/negative)."""
    happy.setattr(aa.settings, "chili_momentum_auto_arm_probe_time_budget_seconds", 12.0, raising=False)
    assert aa._probe_time_budget() == 12.0
    happy.setattr(aa.settings, "chili_momentum_auto_arm_probe_time_budget_seconds", -1.0, raising=False)
    assert aa._probe_time_budget() == 1.0


def test_probe_budget_arms_fast_firing_despite_slow_straggler(happy, monkeypatch):
    """A slow-probing HIGHER-viability straggler must NOT block arming a name that probed
    quickly and is firing — the time budget bounds the wave so the pass returns ~budget,
    not ~straggler. This is the anti-starvation property behind the NPT arm-timing fix."""
    import time

    happy.setattr(aa.settings, "chili_momentum_auto_arm_probe_time_budget_seconds", 1.0, raising=False)
    cands = [_cand("SLOW-USD", 8, 0.90), _cand("FAST-USD", 8, 0.55)]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: cands)
    happy.setattr(aa, "_symbol_market_open", lambda sym: True)

    def _probe(sym):
        if sym == "SLOW-USD":
            time.sleep(3.0)  # straggler far beyond the 1s budget
        return (True, "pullback_break_ok", None)

    monkeypatch.setattr(aa, "_probe_candidate", _probe)
    t0 = time.monotonic()
    out = aa.run_auto_arm_pass(_FakeDB())
    elapsed = time.monotonic() - t0
    assert out["armed"] == 1
    assert out["symbol"] == "FAST-USD"  # higher-viability SLOW straggler did not block it
    assert out.get("probe_timed_out") is True
    assert elapsed < 2.5  # returned ~budget (1s), not ~straggler (3s)


# ── Equity-only focus (Ross lane disables crypto) ─────────────────────────────

def test_equity_only_skips_crypto(happy):
    """Equity-only focus: crypto ('-USD') is excluded; the equity is armed instead."""
    happy.setattr(aa.settings, "chili_momentum_auto_arm_crypto_only", False, raising=False)
    happy.setattr(aa.settings, "chili_momentum_auto_arm_equity_only", True, raising=False)
    happy.setattr(
        aa,
        "_ross_snapshot_rows_by_symbol",
        lambda: {
            "MOVE": {
                "ticker": "MOVE",
                "lastTrade": {"p": 4.25},
                "day": {"v": 400_000},
                "todaysChangePerc": 18.0,
            }
        },
    )
    happy.setattr(
        aa, "_fresh_live_eligible_candidates",
        lambda db, *, limit, ross_universe_symbols=None: [_cand("KAIO-USD", 8, 0.80), _cand("MOVE", 8, 0.55)],
    )
    happy.setattr(aa, "_symbol_market_open", lambda sym: True)
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "momentum_ok"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "MOVE"  # crypto KAIO-USD excluded by equity-only focus


# ── Adaptive concurrency (equity-relative, risk-bounded) ──────────────────────

def test_ross_required_empty_universe_refuses_generic_equity_fallback(happy):
    happy.setattr(aa.settings, "chili_momentum_auto_arm_crypto_only", False, raising=False)
    happy.setattr(aa.settings, "chili_momentum_auto_arm_equity_only", True, raising=False)
    happy.setattr(aa.settings, "chili_momentum_ross_equity_universe_required", True, raising=False)
    happy.setattr(aa, "_ross_snapshot_rows_by_symbol", lambda: {})

    def _must_not_fetch_candidates(*_a, **_k):
        raise AssertionError("generic viability candidates must not be fetched for an empty Ross universe")

    happy.setattr(aa, "_fresh_live_eligible_candidates", _must_not_fetch_candidates)
    out = aa.run_auto_arm_pass(_FakeDB())

    assert out["skipped"] == "no_fresh_live_eligible"
    assert out["ross_universe_symbols"] == 0
    assert out["scanned"] == 0


def test_adaptive_concurrency_falls_back_to_base_without_equity(monkeypatch):
    """No equity available -> use the fixed base cap (never scale against unknown equity)."""
    from app.services.trading.momentum_neural import risk_policy as rp
    monkeypatch.setattr(rp, "_account_equity_usd", lambda ef=None: None)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_max_concurrent_live_sessions", 5, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_concurrent_open_risk_fraction", 0.05, raising=False)
    assert rp.adaptive_max_concurrent_live_sessions() == 5


def test_adaptive_concurrency_is_basis_independent_ratio(monkeypatch):
    """N = open_risk_fraction / loss_fraction (= simultaneous-risk budget ratio) and is
    INDEPENDENT of account size/margin — growth scales per-trade SIZE, not the slot count.
    0.10 / 0.01 = 10, the same at $10k, $25k, or $100k (this is what stops a 2x buying-power
    basis from also doubling the slots)."""
    from app.services.trading.momentum_neural import risk_policy as rp
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_max_concurrent_live_sessions", 5, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_concurrent_open_risk_fraction", 0.10, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_max_loss_per_trade_usd", 50.0, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_auto_arm_crypto_only", False, raising=False)
    for eq in (10_000.0, 25_000.0, 100_000.0):
        # _equity_relative_cap now calls _account_equity_usd(ef, prefer_equity=...),
        # so the stub must accept (and ignore) the keyword.
        monkeypatch.setattr(rp, "_account_equity_usd", lambda ef=None, _e=eq, **_k: _e)
        assert rp.adaptive_max_concurrent_live_sessions() == 10, f"eq={eq}"


def test_adaptive_concurrency_clamps_to_ceiling(monkeypatch):
    """A large risk-budget ratio is clamped at the 15 guardrail (0.30 / 0.01 = 30 -> 15)."""
    from app.services.trading.momentum_neural import risk_policy as rp
    # _equity_relative_cap now calls _account_equity_usd(ef, prefer_equity=...),
    # so the stub must accept (and ignore) the keyword.
    monkeypatch.setattr(rp, "_account_equity_usd", lambda ef=None, **_k: 100_000.0)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_max_concurrent_live_sessions", 5, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_concurrent_open_risk_fraction", 0.30, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_max_loss_per_trade_usd", 50.0, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_auto_arm_crypto_only", False, raising=False)
    assert rp.adaptive_max_concurrent_live_sessions() == 15


def test_adaptive_concurrency_zero_fraction_disables(monkeypatch):
    """frac=0 disables the adaptive scaling -> fixed base cap."""
    from app.services.trading.momentum_neural import risk_policy as rp
    monkeypatch.setattr(rp, "_account_equity_usd", lambda ef=None: 10_000.0)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_max_concurrent_live_sessions", 5, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_concurrent_open_risk_fraction", 0.0, raising=False)
    assert rp.adaptive_max_concurrent_live_sessions() == 5


# ── [63] BORROW RECEIPT ──────────────────────────────────────────────────────────────────
# Ang resibo ay sumasakay sa MISMONG asset probe (walang dagdag na network call) at
# INIUULAT LAMANG: ang arm ay eksaktong pareho kahit `shortable=False`.
#
# DALAWANG landas ang sinusukat dito, at ang UNA ang mahalaga:
#   * `alpaca_primary` — ang equity route NGAYON
#     (CHILI_MOMENTUM_EQUITY_EXECUTION_VIA_ALPACA_PAPER=true; 5,871 live alpaca_spot session
#     sa 60 araw, ZERO robinhood_spot). Ang unang bersyon ng resibo ay nakatira LAMANG sa
#     loob ng twin block, na may guard na `_exec_family in ("robinhood_spot","coinbase_spot")`
#     — kaya hindi ito kailanman pumutok sa tumatakbong lane.
#   * `alpaca_twin`   — ang RH-primary na A/B soak (dormant habang naka-ON ang flag sa itaas).


def _fake_alpaca_adapter(mp, raw):
    from app.services.trading.venue import alpaca_spot as ap

    ap._LISTED_CACHE.clear()

    class _FakeAdapter:
        broker_environment = "paper"

        def get_product_probe(self, sym):
            return SimpleNamespace(trading_disabled=False, raw=dict(raw)), None, None

        def get_product(self, sym):
            prod, _m, _e = self.get_product_probe(sym)
            return prod, None

    mp.setattr(ap, "AlpacaSpotAdapter", _FakeAdapter)
    return ap


def _capture_events(mp):
    events: list[tuple[int, str, dict]] = []
    from app.services.trading.momentum_neural import persistence as _persistence

    mp.setattr(
        _persistence,
        "append_trading_automation_event",
        lambda db, sid, et, payload, **k: events.append((int(sid), et, dict(payload))),
    )
    return events


def _capture_begins(mp):
    calls: list[tuple[str, str]] = []

    def _begin(_db, **kwargs):
        calls.append((kwargs["symbol"], kwargs["execution_family"]))
        return {"ok": True, "arm_token": "tok", "session_id": 900 + len(calls)}

    mp.setattr(operator_actions, "begin_live_arm", _begin)
    return calls


def _primary_alpaca(happy, *, raw):
    """Iruta ang PRIMARY equity arm sa alpaca_spot — ang eksaktong topolohiya ng lane ngayon."""
    from app.services.trading.momentum_neural import risk_evaluator as _re

    for name, value in (
        ("chili_momentum_auto_arm_crypto_only", False),
        ("chili_momentum_auto_arm_equity_only", False),
        ("chili_momentum_equity_execution_via_alpaca_paper", True),
        ("chili_momentum_alpaca_twin_arm_enabled", True),
        ("chili_alpaca_enabled", True),
        ("chili_alpaca_paper", True),
        ("chili_alpaca_api_key", "paper-key"),
        ("chili_alpaca_api_secret", "paper-secret"),
        ("chili_alpaca_expected_account_id", "paper-account-uuid"),
    ):
        happy.setattr(aa.settings, name, value, raising=False)
    happy.setattr(
        _re,
        "alpaca_paper_arm_resource_capacity",
        lambda db, *, user_id: {
            "available": True, "watching": 0, "capacity": 8, "headroom": 8,
        },
        raising=False,
    )
    happy.setattr(
        aa, "_current_rail_dispatch_capacity", lambda *, user_id: {"enabled": False}
    )
    happy.setattr(
        governance, "broker_daily_loss_breached", lambda db, fam, **k: (False, {})
    )
    happy.setattr(
        aa, "_fresh_live_eligible_candidates", lambda db, *, limit: [_cand("PRIMARY", 8, 0.75)]
    )
    _fake_alpaca_adapter(happy, raw)
    return _capture_begins(happy), _capture_events(happy)


def _twin_happy(happy, *, raw):
    """Ihanda ang twin path na PINAPAYAGAN, na may pekeng Alpaca asset na may ibinigay na raw."""
    for name, value in (
        ("chili_momentum_auto_arm_crypto_only", False),
        ("chili_momentum_auto_arm_equity_only", False),
        ("chili_momentum_equity_execution_via_alpaca_paper", False),
        ("chili_momentum_alpaca_twin_arm_enabled", True),
        ("chili_alpaca_enabled", True),
        ("chili_alpaca_paper", True),
        ("chili_alpaca_api_key", "paper-key"),
    ):
        happy.setattr(aa.settings, name, value, raising=False)
    happy.setattr(
        aa, "_fresh_live_eligible_candidates", lambda db, *, limit: [_cand("PRIMARY", 8, 0.75)]
    )
    happy.setattr(
        aa,
        "_alpaca_twin_loss_guard_decision",
        lambda db, **kwargs: (
            True,
            {"allowed": True, "reason": None, "coverage_grade": "CURRENT_LIVE_COMPLETE"},
            {"account_scope": "alpaca:paper", "account_identity": "paper-account"},
        ),
    )
    _fake_alpaca_adapter(happy, raw)
    return _capture_begins(happy), _capture_events(happy)


# ── ANG LANDAS NA TALAGANG PUMUPUTOK: primary alpaca_spot ────────────────────────────────


def test_primary_alpaca_arm_emits_the_borrow_receipt(happy):
    """ANG AYOS (review, blocking): ang resibo ay dating nakatira LAMANG sa twin block, na
    hindi kailanman naabot ng isang alpaca_spot na primary — kaya zero ang hilera magpakailanman."""
    calls, events = _primary_alpaca(happy, raw={"shortable": False, "easy_to_borrow": False})

    out = aa.run_auto_arm_pass(_FakeDB())

    assert calls == [("PRIMARY", "alpaca_spot")]
    assert out["armed"] == 1
    (_receipt,) = out["alpaca_borrow_receipts"]
    assert _receipt["symbol"] == "PRIMARY"
    assert _receipt["route"] == "alpaca_primary"
    assert _receipt["shortable"] is False
    assert (_receipt["session_id"], _receipt["durable"]) == (901, True)
    assert len(events) == 1
    sid, etype, payload = events[0]
    assert (sid, etype) == (901, "live_alpaca_borrow_receipt")
    assert payload["route"] == "alpaca_primary"
    assert payload["shortable"] is False
    # Walang twin (ang primary MISMO ang alpaca session).
    assert "alpaca_twin_session_id" not in out


def test_primary_receipt_names_the_broker_generation(happy):
    """Ang shortable ay ACCOUNT-SCOPED: ang durable na hilera ay kailangang magpangalan ng
    broker environment at ng account na sumagot — at HINDI hubad ang identity."""
    _calls, events = _primary_alpaca(happy, raw={"shortable": True, "easy_to_borrow": True})

    aa.run_auto_arm_pass(_FakeDB())

    payload = events[0][2]
    assert payload["broker_environment"] == "paper"
    assert payload["account_scope"] == "alpaca:paper"
    assert payload["account_identity_sha256"] is not None
    assert "account_identity" not in payload


def test_primary_receipt_never_blocks_the_arm(happy):
    """Ang isang bigong pagsulat ng resibo ay HINDI dapat makaapekto sa arm — pero MAINGAY."""
    calls, _events = _primary_alpaca(happy, raw={"shortable": True})
    from app.services.trading.momentum_neural import persistence as _persistence

    def _boom(*_a, **_k):
        raise RuntimeError("flush failed")

    happy.setattr(_persistence, "append_trading_automation_event", _boom)

    out = aa.run_auto_arm_pass(_FakeDB())

    assert out["armed"] == 1
    assert calls == [("PRIMARY", "alpaca_spot")]
    # Ang pagkawala ng hilera ay IPINAPAALAM, hindi itinatago (dating logger.debug lamang).
    # LISTA, hindi scalar (review): ang isang pass ay puwedeng mag-arm ng ilang pangalan.
    assert out["alpaca_borrow_receipt_write_failed"] == [901]
    assert "alpaca_borrow" not in out
    assert out["alpaca_borrow_receipts"][0]["symbol"] == "PRIMARY"
    assert out["alpaca_borrow_receipts"][0]["durable"] is False


def test_primary_receipt_reports_probe_error_as_unknown_not_missing(happy):
    """Ang "hindi tayo nakatanong" ay UNKNOWN — hindi "walang ganitong asset sa broker"."""
    from app.services.trading.venue import alpaca_spot as ap

    _calls, events = _primary_alpaca(happy, raw={})

    class _FailingAdapter:
        broker_environment = "paper"

        def get_product_probe(self, sym):
            return None, None, "ConnectionError:no_http_status"

    ap._LISTED_CACHE.clear()
    happy.setattr(ap, "AlpacaSpotAdapter", _FailingAdapter)

    out = aa.run_auto_arm_pass(_FakeDB())

    assert out["armed"] == 1  # ang LONG arm ay hindi apektado ng resibo
    assert events[0][2]["source"] == "probe_error"
    assert events[0][2]["listed"] is False
    assert events[0][2]["shortable"] == "unknown"


# ── ANG DORMANT NA LANDAS: RH primary + alpaca twin ──────────────────────────────────────


def test_twin_arm_emits_borrow_receipt_for_a_not_shortable_name(happy):
    """32/34 ng populasyon ng [62]: hindi shortable — at ang long twin ay umaarm pa rin."""
    calls, events = _twin_happy(
        happy, raw={"shortable": False, "easy_to_borrow": False}
    )

    out = aa.run_auto_arm_pass(_FakeDB())

    assert calls == [("PRIMARY", "robinhood_spot"), ("PRIMARY", "alpaca_spot")]
    assert out["alpaca_twin_session_id"] == 902
    (_receipt,) = out["alpaca_borrow_receipts"]
    assert _receipt["symbol"] == "PRIMARY"
    assert _receipt["route"] == "alpaca_twin"
    assert _receipt["listed"] is True
    assert _receipt["shortable"] is False
    assert _receipt["easy_to_borrow"] is False
    assert _receipt["source"] == "alpaca_asset"
    assert len(events) == 1
    sid, etype, payload = events[0]
    assert (sid, etype) == (902, "live_alpaca_borrow_receipt")
    assert payload["shortable"] is False and payload["symbol"] == "PRIMARY"


def test_twin_arm_receipt_reports_shortable_without_changing_anything(happy):
    """DLTH/LIDR-class: shortable=True — WALANG ibang nangyayari (walang short, walang size)."""
    calls, events = _twin_happy(happy, raw={"shortable": True, "easy_to_borrow": True})

    out = aa.run_auto_arm_pass(_FakeDB())

    assert calls == [("PRIMARY", "robinhood_spot"), ("PRIMARY", "alpaca_spot")]
    assert out["alpaca_borrow_receipts"][0]["shortable"] is True
    assert out["alpaca_borrow_receipts"][0]["easy_to_borrow"] is True
    assert events[0][1] == "live_alpaca_borrow_receipt"
    # Ang twin ay `alpaca_spot` pa rin — kailanman ay HINDI `alpaca_short`.
    assert all(fam != "alpaca_short" for _sym, fam in calls)


def test_unknown_borrow_flags_do_not_block_the_twin(happy):
    """Ang HINDI-ALAM ay hindi veto sa LONG twin — pangalanan, huwag pigilan."""
    calls, events = _twin_happy(happy, raw={})

    out = aa.run_auto_arm_pass(_FakeDB())

    assert calls == [("PRIMARY", "robinhood_spot"), ("PRIMARY", "alpaca_spot")]
    assert out["alpaca_borrow_receipts"][0]["shortable"] == "unknown"
    assert out["alpaca_borrow_receipts"][0]["easy_to_borrow"] == "unknown"
    assert events[0][2]["shortable"] == "unknown"


def test_twin_skip_reason_separates_unknown_from_missing(happy):
    """Ang "hindi tayo nakatanong" ay hindi pareho ng "sinabi ng broker na wala"."""
    from app.services.trading.venue import alpaca_spot as ap

    calls, _events = _twin_happy(happy, raw={})

    class _FailingAdapter:
        broker_environment = "paper"

        def get_product_probe(self, sym):
            return None, None, "ConnectionError:no_http_status"

    ap._LISTED_CACHE.clear()
    happy.setattr(ap, "AlpacaSpotAdapter", _FailingAdapter)

    out = aa.run_auto_arm_pass(_FakeDB())

    assert calls == [("PRIMARY", "robinhood_spot")]  # walang twin
    assert out["alpaca_twin_skipped"] == "alpaca_symbol_probe_error"
    assert out["armed"] == 1  # ang primary arm ay hindi apektado
    # Ang resibo ay NAIULAT pa rin kahit walang sesyon na masusulatan.
    assert out["alpaca_borrow_receipts"][0]["source"] == "probe_error"


def test_twin_skip_reason_for_a_name_the_broker_does_not_have(happy):
    from app.services.trading.venue import alpaca_spot as ap

    calls, _events = _twin_happy(happy, raw={})

    class _MissingAdapter:
        broker_environment = "paper"

        def get_product_probe(self, sym):
            return None, None, None  # SUMAGOT ang broker: 404

    ap._LISTED_CACHE.clear()
    happy.setattr(ap, "AlpacaSpotAdapter", _MissingAdapter)

    out = aa.run_auto_arm_pass(_FakeDB())

    assert calls == [("PRIMARY", "robinhood_spot")]
    assert out["alpaca_twin_skipped"] == "alpaca_symbol_unavailable"
    assert out["alpaca_borrow_receipts"][0]["source"] == "asset_missing"


# ── REVIEW NG [63]: ang savepoint, ang attribution, at ang identity ─────────────────────


class _SavepointDB(_FakeDB):
    """Isang Session-like na MAY ``begin_nested`` — ang landas na TALAGANG tumatakbo.

    Ang lahat ng naunang arm-path na test ay nagpapatakbo ng ``_FakeDB``, na WALANG
    ``begin_nested``, kaya ang sangay na pinupuntahan ng tunay na ``Session`` ay hindi
    kailanman naisasagawa. Itinatala ng klaseng ito ang PAGKAKASUNOD-SUNOD ng mga tawag
    para masukat kung ano talaga ang nangyayari (review ng [63]).
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.savepoints = 0

    def flush(self) -> None:
        self.calls.append("flush")

    def begin_nested(self):
        self.calls.append("begin_nested")
        self.savepoints += 1
        db = self

        class _SP:
            def __enter__(_self):
                return _self

            def __exit__(_self, exc_type, _exc, _tb):
                db.calls.append("savepoint_rollback" if exc_type else "savepoint_release")
                return False

        return _SP()


def test_primary_receipt_flushes_the_arm_before_opening_the_savepoint(happy):
    """Ang INSERT ng arm ay dapat nasa LABAS ng savepoint.

    Ang buong pass ay IISANG transaksyon (walang commit/flush sa begin_live_arm ni sa
    confirm_live_arm), kaya kung ang flush ng ``append_trading_automation_event`` ang
    unang naglalabas ng INSERT ng sesyon, ito ay lilipad SA LOOB ng savepoint at
    ibabalik ng rollback sa pending habang hawak na ng ``armed_session_ids`` ang id.
    """
    _calls, events = _primary_alpaca(happy, raw={"shortable": False})
    db = _SavepointDB()

    out = aa.run_auto_arm_pass(db)

    assert out["armed"] == 1
    assert db.savepoints == 1
    i_flush = db.calls.index("flush")
    i_sp = db.calls.index("begin_nested")
    assert i_flush < i_sp, db.calls
    assert db.calls[i_sp + 1] == "savepoint_release"
    assert events[0][1] == "live_alpaca_borrow_receipt"
    assert out["alpaca_borrow_receipts"][0]["durable"] is True


def test_savepoint_rollback_never_unwinds_the_armed_session(happy):
    """Ang bigong resibo ay bumabalik sa SAVEPOINT lamang — buhay pa rin ang arm."""
    calls, _events = _primary_alpaca(happy, raw={"shortable": True})
    from app.services.trading.momentum_neural import persistence as _persistence

    def _boom(*_a, **_k):
        raise RuntimeError("flush failed inside the savepoint")

    happy.setattr(_persistence, "append_trading_automation_event", _boom)
    db = _SavepointDB()

    out = aa.run_auto_arm_pass(db)

    assert out["armed"] == 1
    assert calls == [("PRIMARY", "alpaca_spot")]
    assert out["armed_session_ids"] == [901]
    assert db.calls.count("savepoint_rollback") == 1
    assert db.calls.index("flush") < db.calls.index("begin_nested")
    assert out["alpaca_borrow_receipt_write_failed"] == [901]
    assert out["alpaca_borrow_receipts"][0]["durable"] is False


def test_every_armed_symbol_gets_its_own_attributable_receipt(happy):
    """LAST-WRITER-WINS ang dating ``out["alpaca_borrow"]``: ang isang pass na nag-arm ng
    SYMA tapos SYMB ay nag-uulat lamang ng SYMB sa iisang scalar — at kapag BUMIGO ang
    dalawang pagsulat, ang huli lang ang nasa failure field. Isang hilera kada pangalan."""
    calls, events = _primary_alpaca(happy, raw={"shortable": False})
    happy.setattr(
        aa,
        "_fresh_live_eligible_candidates",
        lambda db, *, limit: [_cand("SYMA", 8, 0.80), _cand("SYMB", 9, 0.75)],
    )

    out = aa.run_auto_arm_pass(_FakeDB())

    assert out["armed"] == 2
    assert calls == [("SYMA", "alpaca_spot"), ("SYMB", "alpaca_spot")]
    assert "alpaca_borrow" not in out
    assert [r["symbol"] for r in out["alpaca_borrow_receipts"]] == ["SYMA", "SYMB"]
    assert [r["session_id"] for r in out["alpaca_borrow_receipts"]] == [901, 902]
    assert all(r["durable"] for r in out["alpaca_borrow_receipts"])
    assert [e[0] for e in events] == [901, 902]


def test_both_failed_receipts_are_counted_not_just_the_last(happy):
    """Ang denominator ng operator ay hindi puwedeng mawalan ng hilera nang tahimik."""
    _calls, _events = _primary_alpaca(happy, raw={"shortable": False})
    from app.services.trading.momentum_neural import persistence as _persistence

    happy.setattr(
        aa,
        "_fresh_live_eligible_candidates",
        lambda db, *, limit: [_cand("SYMA", 8, 0.80), _cand("SYMB", 9, 0.75)],
    )

    def _boom(*_a, **_k):
        raise RuntimeError("flush failed")

    happy.setattr(_persistence, "append_trading_automation_event", _boom)

    out = aa.run_auto_arm_pass(_FakeDB())

    assert out["armed"] == 2
    assert out["alpaca_borrow_receipt_write_failed"] == [901, 902]


def test_pass_receipt_never_carries_the_bare_account_identity(happy):
    """Ang pass summary ay ini-log NANG BUO kada arm (trading_scheduler), kaya ang isang
    hubad na ``account_identity`` doon ay ang UUID ng broker account sa application log sa
    BAWAT live arm. sha256 lamang — kapareho ng ``loss_guard_policy``."""
    import hashlib

    _calls, events = _primary_alpaca(happy, raw={"shortable": False})

    out = aa.run_auto_arm_pass(_FakeDB())

    receipt = out["alpaca_borrow_receipts"][0]
    assert "account_identity" not in receipt
    assert receipt["account_identity_sha256"] == hashlib.sha256(
        b"paper-account-uuid"
    ).hexdigest()
    assert "account_identity" not in events[0][2]
    # At hindi rin ito nakatago sa ilalim ng ibang pangalan kahit saan sa summary.
    assert "paper-account-uuid" not in repr(out)
