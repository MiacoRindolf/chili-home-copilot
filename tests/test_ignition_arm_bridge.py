"""IGNITION→ARM BRIDGE (2026-08-19 YJ miss): scoped auto-arm pass tests.

Ang ignite job ay nailalagay ang isang crosser sa viability board sa loob ng
5-15s, pero ang ARM ay naghihintay sa susunod na FULL pass — sinukat na 300-1200s
kontra 10s cadence, kaya ang 12:28Z YJ ignition leg ay tumakbo nang WALANG live
session. Ang bridge = parehong run_auto_arm_pass na naka-scope sa ignited
symbols; LAHAT ng guard ay tumatakbo pa rin, at ang mga NILALAKTAWAN lang ay
strictly risk-reducing (snapshot build, watching-reaper, displacement, at ang
board-leader privileges).

Fixture pattern mula sa test_loss_cooldown_leader_exemption.py.
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
        pass

    def close(self) -> None:
        pass

    def expunge_all(self) -> None:
        pass

    def query(self, *_a, **_k):
        return self

    def filter(self, *_a, **_k):
        return self

    def all(self):
        return []

    def first(self):
        return None

    def execute(self, *_a, **_k):
        return SimpleNamespace(fetchall=lambda: [], fetchone=lambda: None, scalar=lambda: None)

    def get(self, *_a, **_k):
        return None


def _cand(symbol: str = "LGVN-USD", score: float = 0.70):
    return SimpleNamespace(
        symbol=symbol,
        variant_id=8,
        viability_score=score,
        execution_readiness_json={},
    )


def _happy_path(monkeypatch, *, candidates):
    """Lahat ng seam papunta sa isang matagumpay na arm — kopyang pattern ng
    leader-exemption test, dagdag ang crypto-liquidity + venue-ready mocks para
    DETERMINISTIC ang armed=1 (hindi umaasa sa unmocked na downstream probe)."""
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_live_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_live_scheduler_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_live_runner_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_autotrader_user_id", 1, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_decouple_watching_enabled", False, raising=False)
    # Ang -USD fixture symbol ay dumadaan sa crypto live-arm gate chain — buksan
    # ito para DETERMINISTIC ang armed=1 (hindi clock/flag dependent).
    monkeypatch.setattr(aa.settings, "chili_momentum_crypto_live_arm_enabled", True, raising=False)
    monkeypatch.setattr(aa, "_crypto_paused_us_session", lambda: False)
    from app.services.trading.momentum_neural import market_profile as _mkt

    monkeypatch.setattr(_mkt, "crypto_schedule_enabled", lambda: False)
    monkeypatch.setattr(governance, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 0)
    monkeypatch.setattr(portfolio_risk, "check_portfolio_drawdown_breaker", lambda db, uid: (False, None))
    monkeypatch.setattr(automation_query, "expire_stale_live_arm_sessions", lambda db, *, user_id: 0)

    def _fetch(db, *, limit, ross_universe_symbols=None, as_of_utc=None,
               only_symbols=None, viability_max_age_override=None):
        _fetch.calls.append(
            {
                "only_symbols": only_symbols,
                "viability_max_age_override": viability_max_age_override,
                "ross_universe_symbols": ross_universe_symbols,
            }
        )
        if only_symbols is not None:
            return [c for c in candidates if str(c.symbol).upper() in only_symbols]
        return list(candidates)

    _fetch.calls = []
    monkeypatch.setattr(aa, "_fresh_live_eligible_candidates", _fetch)
    monkeypatch.setattr(aa, "_symbol_free", lambda db, sym, uid: True)
    monkeypatch.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    monkeypatch.setattr(aa, "_candidate_freshness", lambda sym: None)
    monkeypatch.setattr(aa, "crypto_liquidity_ok", lambda sym, c, adapter=None: (True, {}, None))
    monkeypatch.setattr(aa, "_venue_broker_ready_for", lambda sym, cache: True)
    monkeypatch.setattr(aa, "_symbol_loss_guards", lambda db, **kwargs: (set(), {}))
    monkeypatch.setattr(
        account_identity,
        "read_current_non_alpaca_account_identity",
        lambda _family: {"ok": True, "identity": "ignition-bridge-test-v1", "reason": None},
    )
    monkeypatch.setattr(
        risk_policy,
        "load_current_live_loss_history",
        lambda db, **kwargs: (
            (),
            {
                "history_available": True,
                "coverage_grade": "CURRENT_LIVE_COMPLETE",
                "replay_certifiable": False,
            },
        ),
    )
    monkeypatch.setattr(
        risk_policy,
        "consecutive_loss_halt_decision",
        lambda db, **kwargs: (False, {"halted": False, "history_available": True, "config_provenance": {}}),
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
    return _fetch


def _far_future():
    return datetime.utcnow() + timedelta(minutes=15)


# ────────────────────────── scoped run_auto_arm_pass ──────────────────────────


def test_scoped_pass_arms_ignited_symbol(monkeypatch):
    """Happy path: naka-scope sa kaka-ignite na simbolo -> ARMED, at ang fetch ay
    dumaan sa scoped path (only_symbols + tightened freshness override)."""
    fetch = _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"lgvn-usd"})
    assert out.get("scoped_ignition") == ["LGVN-USD"], out
    assert out.get("armed", 0) == 1, out
    assert len(fetch.calls) == 1
    assert fetch.calls[0]["only_symbols"] == frozenset({"LGVN-USD"})
    assert fetch.calls[0]["viability_max_age_override"] == 90.0


def test_scoped_pass_empty_symbols_is_noop(monkeypatch):
    _happy_path(monkeypatch, candidates=[_cand()])
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"", "  "})
    assert out.get("skipped") == "scoped_no_symbols", out
    assert out.get("armed", 0) == 0


def test_scoped_pass_kill_switch_still_blocks(monkeypatch):
    """INVARIANT: bawat account-level guard ay tumatakbo pa rin sa scoped mode."""
    _happy_path(monkeypatch, candidates=[_cand()])
    monkeypatch.setattr(governance, "is_kill_switch_active", lambda: True)
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"LGVN-USD"})
    assert out.get("skipped") == "kill_switch", out
    assert out.get("armed", 0) == 0


def test_scoped_pass_no_leader_cooldown_exemption(monkeypatch):
    """INVARIANT: ang ignited symbol ay HINDI board #1 — ang loss-cooldown TIMER
    ay HINDI ine-exempt sa scoped mode (kung hindi, ang re-ignition ay magiging
    cooldown bypass)."""
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    monkeypatch.setattr(
        aa, "_symbol_loss_guards",
        lambda db, **kwargs: (set(), {"LGVN-USD": _far_future()}),
    )
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"LGVN-USD"})
    assert out.get("armed", 0) == 0, out
    assert out.get("loss_guard_skipped", 0) >= 1, out
    assert out.get("loss_cooldown_leader_exempt", 0) == 0, out


def test_full_pass_leader_exemption_unchanged(monkeypatch):
    """PARITY: walang only_symbols -> ang board-#1 TIMER exemption ay buhay pa rin
    (ang scoped restriction ay hindi tumagas sa full pass)."""
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    monkeypatch.setattr(
        aa, "_symbol_loss_guards",
        lambda db, **kwargs: (set(), {"LGVN-USD": _far_future()}),
    )
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("loss_cooldown_leader_exempt", 0) >= 1, out
    assert out.get("loss_guard_skipped", 0) == 0, out


def test_scoped_pass_skips_snapshot_build_and_watching_reaper(monkeypatch):
    """Ang bigat mismo ng pass (full-market snapshot + watching-reaper) ang
    nilalaktawan ng scoped mode — iyon ang buong punto ng bridge."""
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    calls = {"snapshot": 0, "reaper": 0}
    monkeypatch.setattr(aa, "_auto_arm_equity_only", lambda: True)
    monkeypatch.setattr(
        aa, "_ross_snapshot_rows_by_symbol",
        lambda: calls.__setitem__("snapshot", calls["snapshot"] + 1) or {},
    )
    monkeypatch.setattr(
        aa, "_reap_stale_watching_sessions",
        lambda db, *, user_id, now: calls.__setitem__("reaper", calls["reaper"] + 1) or 0,
    )
    aa.run_auto_arm_pass(_FakeDB(), only_symbols={"LGVN-USD"})
    assert calls == {"snapshot": 0, "reaper": 0}, calls
    # PARITY: ang full pass ay tumatawag pa rin sa pareho.
    aa.run_auto_arm_pass(_FakeDB())
    assert calls["snapshot"] >= 1 and calls["reaper"] == 1, calls


def test_scoped_pass_no_displacement_on_full_slots(monkeypatch):
    """SCOPED: kapag puno ang slots, skip lang — hindi nag-e-evict ng watcher
    (ang displacement ay nangangailangan ng full-board rank context)."""
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    monkeypatch.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 99)
    called = {"displace": 0}
    monkeypatch.setattr(
        aa, "_try_displacement_for_full_slots",
        lambda db, *, uid, out: called.__setitem__("displace", called["displace"] + 1) or True,
    )
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"LGVN-USD"})
    assert out.get("skipped") == "live_session_active", out
    assert called["displace"] == 0, called


def test_scoped_pass_no_rotation_telemetry(monkeypatch):
    """SCOPED: hindi tina-tatakan ang leader-rotation telemetry — ang ignited
    symbol sa slot 0 ng scoped list ay hindi totoong board-leader change."""
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    called = {"rotation": 0}
    monkeypatch.setattr(
        aa, "_emit_leader_rotation_if_changed",
        lambda db, sym: called.__setitem__("rotation", called["rotation"] + 1),
    )
    aa.run_auto_arm_pass(_FakeDB(), only_symbols={"LGVN-USD"})
    assert called["rotation"] == 0, called
    aa.run_auto_arm_pass(_FakeDB())
    assert called["rotation"] == 1, called


# ────────────────────────── run_scoped_ignition_arm ──────────────────────────


def _reset_debounce(monkeypatch):
    monkeypatch.setattr(aa, "_IGNITION_BRIDGE_LAST_ATTEMPT", {})


def test_bridge_flag_off_never_invokes_pass(monkeypatch):
    _reset_debounce(monkeypatch)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_arm_bridge_enabled", False, raising=False
    )
    called = {"pass": 0}
    monkeypatch.setattr(
        aa, "run_auto_arm_pass",
        lambda db, **k: called.__setitem__("pass", called["pass"] + 1) or {},
    )
    assert aa.run_scoped_ignition_arm(_FakeDB(), ["YJ"]) is None
    assert called["pass"] == 0


def test_bridge_invokes_scoped_pass_once_then_debounces(monkeypatch):
    _reset_debounce(monkeypatch)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False
    )
    seen: list[frozenset] = []
    monkeypatch.setattr(
        aa, "run_auto_arm_pass",
        lambda db, *, only_symbols=None, **k: seen.append(only_symbols) or {"armed": 0},
    )
    out1 = aa.run_scoped_ignition_arm(_FakeDB(), ["yj", " yj "])
    assert out1 is not None
    assert seen == [frozenset({"YJ"})]
    # Agad na pangalawang crossing sa loob ng debounce window -> walang pass.
    assert aa.run_scoped_ignition_arm(_FakeDB(), ["YJ"]) is None
    assert len(seen) == 1
    # Ibang simbolo -> hindi apektado ng YJ debounce.
    out3 = aa.run_scoped_ignition_arm(_FakeDB(), ["YJ", "RDAC"])
    assert out3 is not None
    assert seen[-1] == frozenset({"RDAC"})


def test_bridge_debounce_stamped_on_attempt_even_if_blocked(monkeypatch):
    """Ang debounce ay sa ATTEMPT, hindi sa tagumpay — ang begin_blocked na
    simbolo ay hindi maghahammer ng scoped pass bawat ignite cadence."""
    _reset_debounce(monkeypatch)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False
    )
    calls = {"pass": 0}
    monkeypatch.setattr(
        aa, "run_auto_arm_pass",
        lambda db, **k: calls.__setitem__("pass", calls["pass"] + 1)
        or {"armed": 0, "skipped": "begin_blocked"},
    )
    assert aa.run_scoped_ignition_arm(_FakeDB(), ["YJ"]) is not None
    assert aa.run_scoped_ignition_arm(_FakeDB(), ["YJ"]) is None
    assert calls["pass"] == 1


def test_bridge_accepts_bare_string_symbol(monkeypatch):
    """Ang WS ignition scorer ay isang simbolo lang ang ipinapasa — ang bare
    string ay HINDI dapat i-iterate nang paisa-isang letra (C/O/I/W)."""
    _reset_debounce(monkeypatch)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False
    )
    seen: list[frozenset] = []
    monkeypatch.setattr(
        aa, "run_auto_arm_pass",
        lambda db, *, only_symbols=None, **k: seen.append(only_symbols) or {"armed": 0},
    )
    aa.run_scoped_ignition_arm(_FakeDB(), "coiw")
    assert seen == [frozenset({"COIW"})], seen


def test_bridge_single_flight_blocks_concurrent_pass(monkeypatch):
    """3-worker ang WS ignition pool — isang scoped pass lang ang dapat tumakbo
    nang sabay-sabay sa buong proseso.

    LOOP-DRAIN (2026-08-23): ang natalong caller ay TINATANGGIHAN pa rin habang
    tumatakbo ang pass (ang invariant na binabantayan dito), pero ang trabaho
    niya ay dine-drain na ng PAREHONG holder sa isang sumusunod na pass sa halip
    na maghintay ng ibang winner. Sunud-sunod, hindi sabay — buo ang single
    flight.
    """
    _reset_debounce(monkeypatch)
    monkeypatch.setattr(aa, "_IGNITION_BRIDGE_PENDING", set())
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False
    )
    inner: list[frozenset] = []
    concurrent_refusals: list = []

    def _reentrant_pass(db, *, only_symbols=None, **k):
        # Habang tumatakbo ang unang pass, ang pangalawang caller ay dapat ma-skip.
        inner.append(only_symbols)
        if len(inner) == 1:
            concurrent_refusals.append(aa.run_scoped_ignition_arm(_FakeDB(), ["RDAC"]))
        return {"armed": 0}

    monkeypatch.setattr(aa, "run_auto_arm_pass", _reentrant_pass)
    assert aa.run_scoped_ignition_arm(_FakeDB(), ["YJ"]) is not None
    # INVARIANT: ang sabay na caller ay tinanggihan (walang parallel pass).
    assert concurrent_refusals == [None]
    # ...at ang trabaho niya ay hindi naiwan — dinrain ng parehong holder.
    assert inner == [frozenset({"YJ"}), frozenset({"RDAC"})], inner


def test_bridge_lock_released_after_pass_raises(monkeypatch):
    """Ang single-flight lock ay dapat bumitaw kahit sumabog ang pass."""
    _reset_debounce(monkeypatch)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False
    )

    def _boom(db, **k):
        raise RuntimeError("pass exploded")

    monkeypatch.setattr(aa, "run_auto_arm_pass", _boom)
    try:
        aa.run_scoped_ignition_arm(_FakeDB(), ["YJ"])
    except RuntimeError:
        pass
    assert aa._ignition_bridge_inflight.acquire(blocking=False) is True
    aa._ignition_bridge_inflight.release()


# ─────────────────── WS ignition loop hook (ang LIVE na path) ───────────────────


def test_ws_ignition_scorer_invokes_bridge_after_commit(monkeypatch):
    """Ang WS ignition loop ang aktwal na nag-i-ignite sa lane (COIW/MSTX/BMNG
    08-19) — kailangan nitong tawagin ang bridge PAGKATAPOS ng commit."""
    from app.services.trading.momentum_neural import ignition_loop as IL
    from app.services.trading.momentum_neural import pipeline as P

    events: list[str] = []

    class _Rec:
        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

        def close(self):
            events.append("close")

    monkeypatch.setattr(IL, "SessionLocal", _Rec, raising=False)
    monkeypatch.setattr(
        P, "run_momentum_neural_tick", lambda db, *, meta=None, **_k: {}, raising=False
    )
    seen: list = []
    monkeypatch.setattr(
        aa, "run_scoped_ignition_arm",
        lambda db, syms: seen.append(list(syms)) or {"armed": 0},
    )

    IL.IgnitionScoringLoop()._score_symbol("COIW", 13.23)

    assert seen == [["COIW"]], seen
    # Ang bridge ay tumatawag PAGKATAPOS ng score commit, at nagsasara pa rin nang malinis.
    assert events[0] == "commit", events
    assert events[-1] == "close", events


def test_ws_ignition_scorer_survives_bridge_failure(monkeypatch):
    """INVARIANT: ang pagpalya ng bridge ay hindi dapat sumira sa scoring path."""
    from app.services.trading.momentum_neural import ignition_loop as IL
    from app.services.trading.momentum_neural import pipeline as P

    class _Rec:
        def __init__(self):
            self.events = []

        def commit(self):
            self.events.append("commit")

        def rollback(self):
            self.events.append("rollback")

        def close(self):
            self.events.append("close")

    rec = _Rec()
    monkeypatch.setattr(IL, "SessionLocal", lambda: rec, raising=False)
    monkeypatch.setattr(
        P, "run_momentum_neural_tick", lambda db, *, meta=None, **_k: {}, raising=False
    )

    def _boom(db, syms):
        raise RuntimeError("bridge exploded")

    monkeypatch.setattr(aa, "run_scoped_ignition_arm", _boom)

    loop = IL.IgnitionScoringLoop()
    loop._score_symbol("COIW", 13.23)  # dapat HINDI mag-raise

    assert "commit" in rec.events
    assert rec.events[-1] == "close"
    assert "COIW" not in loop._inflight


def test_ws_ignition_signal_carries_price_and_volume(monkeypatch):
    """BLOCKING FIX (napatunayan sa live board 08-19): ang ws_ignition signal ay
    walang price/volume, kaya BAWAT ws_ignition row ay bumabagsak sa Ross universe
    evidence gate (`ross_universe_missing_price`) — hindi kailanman makaka-arm ang
    bridge ng WS-ignited na pangalan. Dapat naka-stamp ang dalawang axis."""
    from app.services.trading.momentum_neural import ignition_loop as IL
    from app.services.trading.momentum_neural import pipeline as P

    metas: list[dict] = []
    monkeypatch.setattr(
        P, "run_momentum_neural_tick",
        lambda db, *, meta=None, **_k: metas.append(dict(meta or {})) or {},
        raising=False,
    )
    monkeypatch.setattr(IL, "SessionLocal", _FakeDB, raising=False)
    monkeypatch.setattr(aa, "run_scoped_ignition_arm", lambda db, syms: None)

    loop = IL.IgnitionScoringLoop()
    loop._tracker._shares = {"BMNG": 4_000_000.0}
    loop._score_symbol("BMNG", 23.27, 2.5)

    sig = metas[0]["ross_signals"]["BMNG"]
    assert sig["price"] == 2.5, sig
    assert sig["volume"] == 4_000_000.0, sig
    # dollar_volume ay dini-derive ng gate mula sa price × volume.
    assert sig["price"] * sig["volume"] == 10_000_000.0


def test_ws_ignition_signal_omits_missing_axes(monkeypatch):
    """FAIL-OPEN: walang price/shares -> hindi na lang isini-stamp (parehong hugis
    ng lumang signal), hindi nagsa-stamp ng zero/None."""
    from app.services.trading.momentum_neural import ignition_loop as IL
    from app.services.trading.momentum_neural import pipeline as P

    metas: list[dict] = []
    monkeypatch.setattr(
        P, "run_momentum_neural_tick",
        lambda db, *, meta=None, **_k: metas.append(dict(meta or {})) or {},
        raising=False,
    )
    monkeypatch.setattr(IL, "SessionLocal", _FakeDB, raising=False)
    monkeypatch.setattr(aa, "run_scoped_ignition_arm", lambda db, syms: None)

    loop = IL.IgnitionScoringLoop()
    loop._score_symbol("NOPX", 11.0, None)

    sig = metas[0]["ross_signals"]["NOPX"]
    assert "price" not in sig, sig
    assert "volume" not in sig, sig


def test_probe_candidate_uses_pass_local_rows(monkeypatch):
    """BLOCKING FIX: ang candidate map ay PASS-LOCAL na. Ang isang scoped bridge
    pass ay hindi na dapat makabura ng candidate row ng KASABAY na full pass."""
    seen: list = []
    monkeypatch.setattr(
        aa, "_entry_trigger_fires",
        lambda sym, row=None: seen.append((sym, row)) or (True, "ok"),
    )
    monkeypatch.setattr(aa, "_candidate_freshness", lambda sym: None)

    full_rows = {"AAAA": "full-row"}
    # Ginagaya ang scoped pass na nagpapalit ng module global habang may laman ito.
    aa._PASS_CANDIDATE_ROWS = {"BBBB": "scoped-row"}

    aa._probe_candidate("AAAA", rows=full_rows)
    assert seen[-1] == ("AAAA", "full-row"), seen
    # Back-compat: walang rows -> babalik sa module global (para sa 1-arg monkeypatch).
    aa._probe_candidate("BBBB")
    assert seen[-1] == ("BBBB", "scoped-row"), seen


def test_bridge_loser_work_is_picked_up_by_next_winner(monkeypatch):
    """BLOCKING FIX: ang natalo sa single-flight ay HINDI nawawalan ng trabaho —
    naka-queue ito at inuunyon ng susunod na winner. (Ang tape-delta job ay
    sumusulong na ng high-water mark, kaya ang nalaglag ay tuluyang nawawala.)"""
    _reset_debounce(monkeypatch)
    monkeypatch.setattr(aa, "_IGNITION_BRIDGE_PENDING", set())
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False
    )
    batches: list[frozenset] = []

    def _pass_that_loses_a_caller(db, *, only_symbols=None, **k):
        batches.append(only_symbols)
        if len(batches) == 1:
            # Habang tumatakbo ito, dumating ang tape-delta batch at natalo.
            assert aa.run_scoped_ignition_arm(_FakeDB(), ["SLE", "WFF"]) is None
        return {"armed": 0}

    monkeypatch.setattr(aa, "run_auto_arm_pass", _pass_that_loses_a_caller)
    aa.run_scoped_ignition_arm(_FakeDB(), ["COIW"])
    # LOOP-DRAIN: ang trabaho ng natalo ay hindi na naghihintay ng SUSUNOD na
    # winner — dinrain ito ng parehong holder sa isang sumusunod na pass.
    assert batches == [frozenset({"COIW"}), frozenset({"SLE", "WFF"})], batches
    # ...kaya walang natirang naka-queue: nabayaran na ang trabaho ng natalo.
    assert aa._IGNITION_BRIDGE_PENDING == set(), aa._IGNITION_BRIDGE_PENDING


def test_bridge_loser_work_unions_into_next_winner_without_drain(monkeypatch):
    """Kapag naka-0 ang drain-pass cap (lumang behavior / kill switch), ang
    trabaho ng natalo ay HINDI pa rin nawawala — inuunyon ito ng susunod na
    winner sa sariling batch. Ito ang saligang safety net sa ilalim ng
    loop-drain."""
    _reset_debounce(monkeypatch)
    monkeypatch.setattr(aa, "_IGNITION_BRIDGE_PENDING", set())
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False
    )
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_bridge_drain_passes", 0, raising=False
    )
    batches: list[frozenset] = []

    def _pass_that_loses_a_caller(db, *, only_symbols=None, **k):
        batches.append(only_symbols)
        assert aa.run_scoped_ignition_arm(_FakeDB(), ["SLE", "WFF"]) is None
        return {"armed": 0}

    monkeypatch.setattr(aa, "run_auto_arm_pass", _pass_that_loses_a_caller)
    aa.run_scoped_ignition_arm(_FakeDB(), ["COIW"])
    assert batches == [frozenset({"COIW"})], batches
    # Ang natalong simbolo ay naka-queue pa rin, hindi nalaglag.
    assert aa._IGNITION_BRIDGE_PENDING == {"SLE", "WFF"}, aa._IGNITION_BRIDGE_PENDING

    # Ang susunod na winner ay inuunyon ang naka-pending sa sariling batch.
    monkeypatch.setattr(
        aa, "run_auto_arm_pass",
        lambda db, *, only_symbols=None, **k: batches.append(only_symbols) or {"armed": 0},
    )
    aa.run_scoped_ignition_arm(_FakeDB(), ["MSTX"])
    assert batches[-1] == frozenset({"SLE", "WFF", "MSTX"}), batches[-1]
    assert aa._IGNITION_BRIDGE_PENDING == set()


def test_bridge_empty_and_blank_symbols_noop(monkeypatch):
    _reset_debounce(monkeypatch)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False
    )
    called = {"pass": 0}
    monkeypatch.setattr(
        aa, "run_auto_arm_pass",
        lambda db, **k: called.__setitem__("pass", called["pass"] + 1) or {},
    )
    assert aa.run_scoped_ignition_arm(_FakeDB(), []) is None
    assert aa.run_scoped_ignition_arm(_FakeDB(), ["", None]) is None
    assert called["pass"] == 0


# ─────────── [64] 2026-09-11: Coinbase connect ONLY when readiness reads Coinbase ───────────
#
# Ang ws-ignition_1 ay naka-ssl.read nang 13,744 s sa coinbase_service.connect()
# (auto_arm "Coinbase connect at PASS START") habang ang lane ay equity sa Alpaca
# paper: 883 bridge pass sa boot na iyon, ZERO ang may -USD na simbolo.
#
# Review fix: ang connect ay tinatanong sa MISMONG lugar ng readiness (bawat
# kandidatong aabot sa _venue_broker_ready_for, parehong routing), hindi mula sa
# -USD substring sa pass start; walang arm-phase connect; ang readiness mismo ay
# hindi na nagtatanong ng Coinbase can_trade para sa equity family.

# Captured BEFORE any monkeypatch: the real module functions.
_REAL_CB_CONNECT = coinbase_service.connect
_REAL_CB_CAN_TRADE = coinbase_service.can_trade
_REAL_VENUE_READY = aa._venue_broker_ready_for
_REAL_IS_CB_TRADEABLE = aa._is_coinbase_tradeable_symbol
_PROBE_BINDING = "chili_momentum_auto_arm_live_scheduler_interval_seconds"


def _counting_connect(calls: list, *, status: str = "connected", **extra):
    def _connect(*_a, **_k):
        calls.append("connect")
        return {"status": status, **extra}

    return _connect


def _recording_real_readiness(order: list):
    def _ready(sym, cache):
        order.append("readiness:" + sym)
        return _REAL_VENUE_READY(sym, cache)

    return _ready


def _equity_lane(monkeypatch, *, pick_family: str = "robinhood_spot"):
    """The 09-11 lane shape: NOT equity_only, NOT crypto_only, crypto NOT via Alpaca.
    Equity picks resolve to ``pick_family``; clock/universe/tape gates are pinned so
    the AAPL fixture deterministically reaches the arm phase."""
    from app.services.trading import execution_family_registry as efr
    from app.services.trading.momentum_neural import market_profile as _mkt

    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_crypto_only", False, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_equity_only", False, raising=False)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_crypto_execution_via_alpaca_paper", False, raising=False
    )
    monkeypatch.setattr(aa, "_lane_execution_family", lambda: "robinhood_spot")
    monkeypatch.setattr(aa, "_ross_equity_universe_required", lambda: False)
    monkeypatch.setattr(aa, "_symbol_market_open", lambda sym: True)
    monkeypatch.setattr(aa, "_tape_delayed", lambda sym, *, as_of: (False, None))
    monkeypatch.setattr(aa, "_top_gainer_concentration_active", lambda *, now=None: False)
    monkeypatch.setattr(_mkt, "schedule_window_now", lambda now=None: "hot")
    monkeypatch.setattr(
        efr,
        "resolve_execution_family_for_symbol",
        lambda sym, *, mode="live": (
            "coinbase_spot" if str(sym).upper().endswith("-USD") else pick_family
        ),
    )


class _RecordingCoinbaseClient:
    """Stands in for BOTH Coinbase clients; records every socket-bound call."""

    timeout = 10.0

    def __init__(self):
        self.calls: list[str] = []

    def get_accounts(self, limit=None):
        self.calls.append("get_accounts")
        return {"accounts": []}

    def get_api_key_permissions(self):
        self.calls.append("get_api_key_permissions")
        return {"can_view": True, "can_trade": True}


def _coinbase_connected_with_stale_can_trade(monkeypatch) -> _RecordingCoinbaseClient:
    """Coinbase ``_connected`` fresh (e.g. the 2-min broker_sync probed it), can_trade
    cache STALE — the exact state in which readiness used to pay a round-trip."""
    import time as _t

    fake = _RecordingCoinbaseClient()
    monkeypatch.setattr(coinbase_service, "_cb_available", True)
    monkeypatch.setattr(coinbase_service, "_probe_client", fake)
    monkeypatch.setattr(coinbase_service, "_client", fake)
    monkeypatch.setattr(coinbase_service, "_connected", True)
    monkeypatch.setattr(coinbase_service, "_last_check", _t.time())
    monkeypatch.setattr(coinbase_service, "_can_trade_cache", {"value": None, "ts": 0.0})
    monkeypatch.setattr(coinbase_service, "connect", _REAL_CB_CONNECT)
    monkeypatch.setattr(coinbase_service, "can_trade", _REAL_CB_CAN_TRADE)
    return fake


def test_scoped_equity_pass_never_calls_coinbase_connect(monkeypatch):
    """The bridge's equity batch (no -USD) must never reach a Coinbase socket."""
    _happy_path(monkeypatch, candidates=[_cand("AAPL")])
    _equity_lane(monkeypatch)
    calls: list = []
    monkeypatch.setattr(coinbase_service, "connect", _counting_connect(calls))
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"AAPL"})
    assert calls == [], out
    assert out["coinbase_connect"] == {
        "called": False,
        "reason": "no_coinbase_spot_readiness",
        "readiness_families": {"robinhood_spot": 1},
    }, out
    phases = out.get("phase_seconds") or {}
    assert "d_board_guards" in phases and "d_eligibility_loop" in phases, phases
    assert "d_coinbase_connect" not in phases, phases


def test_scoped_crypto_pass_connects_before_readiness(monkeypatch):
    """Guards the 06-12 chicken-and-egg: a coinbase_spot candidate connects ONCE,
    BEFORE the venue-readiness filter reads Coinbase's connected state — and still arms.
    The receipt copies the probe bound connect() reported (the value in force)."""
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    order: list = []
    monkeypatch.setattr(
        coinbase_service,
        "connect",
        _counting_connect(order, probe_timeout_s=10.0, probe_timeout_binding=_PROBE_BINDING),
    )

    def _ready(sym, cache):
        order.append("readiness:" + sym)
        return True

    monkeypatch.setattr(aa, "_venue_broker_ready_for", _ready)
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"LGVN-USD"})
    assert out.get("armed", 0) == 1, out
    assert order.count("connect") == 1, order
    assert order.index("connect") < order.index("readiness:LGVN-USD"), order
    rc = out["coinbase_connect"]
    assert rc["called"] is True and rc["reason"] == "coinbase_spot_readiness", rc
    assert rc["symbol"] == "LGVN-USD" and rc["status"] == "connected", rc
    assert rc["timed_out"] is False
    assert rc["probe_timeout_s"] == 10.0 and rc["probe_timeout_binding"] == _PROBE_BINDING
    assert rc["readiness_families"] == {"coinbase_spot": 1}, rc
    assert "coinbase_connect_arm_phase" not in out, out  # no second probe, ever


def test_two_coinbase_candidates_connect_once(monkeypatch):
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD"), _cand("DOGE-USD")])
    calls: list = []
    monkeypatch.setattr(coinbase_service, "connect", _counting_connect(calls))
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"LGVN-USD", "DOGE-USD"})
    assert calls == ["connect"], (calls, out)


def test_full_pass_equity_candidates_skip_coinbase_connect(monkeypatch):
    _happy_path(monkeypatch, candidates=[_cand("AAPL"), _cand("SLE")])
    _equity_lane(monkeypatch)
    calls: list = []
    monkeypatch.setattr(coinbase_service, "connect", _counting_connect(calls))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert calls == [], out
    assert out["coinbase_connect"]["called"] is False, out
    assert out["coinbase_connect"]["readiness_families"] == {"robinhood_spot": 2}, out


def test_full_pass_crypto_via_alpaca_paper_skips_coinbase_connect(monkeypatch):
    """An unavailable selected PAPER route refuses before any broker connect."""
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    monkeypatch.setattr(
        aa.settings, "chili_momentum_crypto_execution_via_alpaca_paper", True, raising=False
    )
    # The test env has no Alpaca paper identity; pin the lane family so the pass
    # reaches the connect decision (what is under test) instead of failing closed
    # at execution_family_resolution_unavailable.
    monkeypatch.setattr(aa, "_lane_execution_family", lambda: "coinbase_spot")
    monkeypatch.setattr(aa, "_venue_broker_ready_for", _REAL_VENUE_READY)
    calls: list = []
    monkeypatch.setattr(coinbase_service, "connect", _counting_connect(calls))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert calls == [], out
    assert out["coinbase_connect"] == {
        "called": False,
        "reason": "no_coinbase_spot_readiness",
        "readiness_families": {"family_unresolved": 1},
    }, out
    assert out.get("broker_not_ready_skipped") == 1, out
    assert out.get("armed", 0) == 0, out


def test_equity_only_lane_skips_coinbase_connect_even_with_usd_candidate(monkeypatch):
    """equity_only skips every crypto candidate before readiness, so no connect."""
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    monkeypatch.setattr(aa, "_auto_arm_equity_only", lambda: True)
    calls: list = []
    monkeypatch.setattr(coinbase_service, "connect", _counting_connect(calls))
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"LGVN-USD"})
    assert calls == [], out
    assert out["coinbase_connect"] == {
        "called": False,
        "reason": "no_coinbase_spot_readiness",
        "readiness_families": {},
    }, out


@pytest.mark.parametrize("pick_family", ["alpaca_spot", "robinhood_spot"])
def test_equity_pick_reaches_arm_phase_with_zero_connects(monkeypatch, pick_family):
    """An equity pick (Alpaca paper / RH) reaches the arm phase with ZERO connects,
    and there is no arm-phase connect any more."""
    _happy_path(monkeypatch, candidates=[_cand("AAPL")])
    _equity_lane(monkeypatch, pick_family=pick_family)
    calls: list = []
    monkeypatch.setattr(coinbase_service, "connect", _counting_connect(calls))
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"AAPL"})
    assert "arm_capacity" in out or out.get("skipped"), out  # the arm phase WAS reached
    assert "coinbase_connect_arm_phase" not in out, out
    assert calls == [], out


# ───── the bare numeric crypto base: readiness, connect and the crypto kill agree ─────


def _bare_crypto_base() -> str:
    from app.services.trading.venue.robinhood_spot import _KNOWN_NUMERIC_CRYPTO_BASES

    return sorted(_KNOWN_NUMERIC_CRYPTO_BASES)[0]


def _fresh_process_real_readiness(monkeypatch, order: list):
    """Fresh process: Coinbase NOT connected; the REAL readiness filter decides.
    connect() is stubbed to succeed (records + sets the connected state it reads)."""
    import time as _t

    monkeypatch.setattr(coinbase_service, "_cb_available", True)
    monkeypatch.setattr(coinbase_service, "_connected", False)
    monkeypatch.setattr(coinbase_service, "_last_check", 0.0)

    def _connect(*_a, **_k):
        order.append("connect")
        coinbase_service._connected = True
        coinbase_service._last_check = _t.time()
        return {"status": "connected"}

    monkeypatch.setattr(coinbase_service, "connect", _connect)
    monkeypatch.setattr(coinbase_service, "can_trade", lambda: order.append("can_trade") or True)
    monkeypatch.setattr(aa.settings, "chili_coinbase_spot_adapter_enabled", True, raising=False)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_crypto_execution_via_alpaca_paper", False, raising=False
    )
    monkeypatch.setattr(aa, "_venue_broker_ready_for", _recording_real_readiness(order))
    monkeypatch.setattr(aa, "_symbol_market_open", lambda sym: True)
    monkeypatch.setattr(aa, "_tape_delayed", lambda sym, *, as_of: (False, None))


def test_bare_numeric_crypto_base_connects_before_its_readiness(monkeypatch):
    """Finding: the pass-start decision keyed on the '-USD' substring missed '00',
    which the router sends to coinbase_spot — so a fresh process dropped it as
    broker_not_ready (the 06-12 chicken-and-egg, reopened) and the arm-phase
    'safety net' could never fire. Now the decision runs the readiness routing."""
    bare = _bare_crypto_base()
    _happy_path(monkeypatch, candidates=[_cand(bare)])
    order: list = []
    _fresh_process_real_readiness(monkeypatch, order)
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={bare})
    assert order[:2] == ["connect", "readiness:" + bare], order
    assert out["coinbase_connect"]["called"] is True, out
    assert out["coinbase_connect"]["symbol"] == bare
    assert out["coinbase_connect"]["readiness_families"] == {"coinbase_spot": 1}
    assert out.get("broker_not_ready_skipped") == 0, out
    assert out.get("armed", 0) == 1, out


def test_bare_numeric_crypto_base_obeys_the_crypto_live_arm_kill(monkeypatch):
    """Side note of the same finding: _live_armable classified '00' by the '-USD'
    substring, took the EQUITY branch and skipped chili_momentum_crypto_live_arm_enabled.
    Negative control: with the old substring predicate the kill is bypassed."""
    bare = _bare_crypto_base()
    _happy_path(monkeypatch, candidates=[_cand(bare)])
    monkeypatch.setattr(aa.settings, "chili_momentum_crypto_live_arm_enabled", False, raising=False)
    from app.services.trading.momentum_neural import market_profile as _mkt

    # The 09-11 lane shape (mixed: EQUITY_ONLY=false, CRYPTO_ONLY=false), with the
    # loss-guard scope on coinbase_spot so a coinbase pick is not refused for an
    # unrelated family mismatch — the kill is the only thing under test.
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_crypto_only", False, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_equity_only", False, raising=False)
    monkeypatch.setattr(aa, "_ross_equity_universe_required", lambda: False)
    monkeypatch.setattr(aa, "_lane_execution_family", lambda: "coinbase_spot")
    monkeypatch.setattr(_mkt, "schedule_window_now", lambda now=None: "hot")
    monkeypatch.setattr(aa, "_top_gainer_concentration_active", lambda *, now=None: False)
    order: list = []
    _fresh_process_real_readiness(monkeypatch, order)
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={bare})
    assert out.get("armed", 0) == 0, out
    assert out.get("crypto_live_disabled_skipped", 0) >= 1, out

    # Negative control: the pre-fix substring predicate lets the kill be bypassed.
    monkeypatch.setattr(aa, "_is_coinbase_tradeable_symbol", lambda s: "-USD" in str(s or "").upper())
    out_old = aa.run_auto_arm_pass(_FakeDB(), only_symbols={bare})
    assert out_old.get("crypto_live_disabled_skipped", 0) == 0, out_old
    assert out_old.get("armed", 0) == 1, out_old


def test_paper_posture_guard_refuses_bare_numeric_crypto_base(monkeypatch):
    bare = _bare_crypto_base()
    monkeypatch.setattr(
        aa.settings, "chili_momentum_crypto_execution_via_alpaca_paper", True, raising=False
    )
    assert aa._coinbase_spot_paper_posture_refused(bare, "coinbase_spot") is True
    assert aa._coinbase_spot_paper_posture_refused("AAPL", "coinbase_spot") is False
    from app.services.trading import execution_family_registry as efr

    monkeypatch.setattr(efr, "resolve_execution_family_for_symbol", lambda s, *, mode="live": "coinbase_spot")
    assert aa._readiness_reads_coinbase(bare) == (False, "paper_posture_refused")
    assert aa._venue_broker_ready_for(bare, {}) is False


def test_crypto_predicate_widens_only_the_known_numeric_bases():
    bare = _bare_crypto_base()
    for sym in ["AAPL", "BTC-USD", "00-USD", "9988-USD", "ETH-USDC", "X", "0", "000", bare, bare.lower()]:
        old = "-USD" in sym.upper()
        new = _REAL_IS_CB_TRADEABLE(sym)
        if sym.upper() == bare:
            assert new is True and old is False, sym
        else:
            assert new is old, sym


# ───── readiness itself: an equity family never pays a Coinbase round-trip ─────


@pytest.mark.parametrize("family", ["alpaca_spot", "robinhood_spot"])
def test_equity_readiness_never_touches_coinbase(monkeypatch, family):
    """Finding (major): build_momentum_operator_readiness computed coinbase_can_trade for
    ANY family whenever Coinbase was connected, so every equity readiness (the bridge
    included) paid get_api_key_permissions when the 300 s cache was stale."""
    from app.services.trading.momentum_neural.operator_readiness import (
        build_momentum_operator_readiness,
    )

    fake = _coinbase_connected_with_stale_can_trade(monkeypatch)
    rd = build_momentum_operator_readiness(execution_family=family, symbol="AAPL")
    assert fake.calls == [], fake.calls
    assert rd["broker_coinbase_connected"] is True
    assert rd["broker_coinbase_can_trade"] is None  # not probed for this family


def test_coinbase_readiness_still_verifies_trade_scope(monkeypatch):
    """Positive control: the coinbase_spot family keeps its sell-scope preflight."""
    from app.services.trading.momentum_neural.operator_readiness import (
        build_momentum_operator_readiness,
    )

    fake = _coinbase_connected_with_stale_can_trade(monkeypatch)
    monkeypatch.setattr(aa.settings, "chili_coinbase_spot_adapter_enabled", True, raising=False)
    rd = build_momentum_operator_readiness(execution_family="coinbase_spot", symbol="LGVN-USD")
    assert fake.calls == ["get_api_key_permissions"], fake.calls
    assert rd["broker_coinbase_can_trade"] is True
    assert rd["broker_ready_for_live"] is True


def test_real_scoped_equity_pass_with_real_readiness_makes_zero_coinbase_calls(monkeypatch):
    """The reviewer's scenario end to end: the REAL scoped pass, the REAL readiness, the
    REAL connect(); Coinbase connected with a stale can_trade cache. Zero socket calls."""
    _happy_path(monkeypatch, candidates=[_cand("AAPL")])
    _equity_lane(monkeypatch, pick_family="alpaca_spot")
    fake = _coinbase_connected_with_stale_can_trade(monkeypatch)
    order: list = []
    monkeypatch.setattr(aa, "_venue_broker_ready_for", _recording_real_readiness(order))
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"AAPL"})
    assert order == ["readiness:AAPL"], order  # readiness DID run
    assert fake.calls == [], fake.calls
    assert out["coinbase_connect"]["called"] is False, out
    assert out["coinbase_connect"]["readiness_families"] == {"alpaca_spot": 1}, out


# ───── phase billing + the bridge's persisted receipt ─────


def test_board_guard_time_is_not_billed_to_coinbase(monkeypatch):
    """Finding: d_coinbase_connect used to include the leader-rotation DB write and
    Guards 6/7 (it was the first mark after board_done). A slow rotation emit on an
    equity pass that never touches Coinbase now bills d_board_guards."""
    import time as _t

    _happy_path(monkeypatch, candidates=[_cand("AAPL"), _cand("SLE")])
    _equity_lane(monkeypatch)
    calls: list = []
    monkeypatch.setattr(coinbase_service, "connect", _counting_connect(calls))
    monkeypatch.setattr(aa, "_emit_leader_rotation_if_changed", lambda db, sym: _t.sleep(0.3))
    out = aa.run_auto_arm_pass(_FakeDB())
    phases = out.get("phase_seconds") or {}
    assert calls == [] and out["coinbase_connect"]["called"] is False, out
    assert phases.get("d_board_guards", 0.0) >= 0.3, phases
    assert "d_coinbase_connect" not in phases, phases
    assert phases.get("d_eligibility_loop", 1.0) < 0.3, phases


def test_connect_receipt_copies_the_values_connect_returned(monkeypatch):
    monkeypatch.setattr(
        coinbase_service,
        "connect",
        lambda: {
            "status": "error",
            "message": "Connection timed out",
            "timed_out": True,
            "probe_timeout_s": 10.0,
            "probe_timeout_binding": _PROBE_BINDING,
        },
    )
    rc = aa._coinbase_connect_receipt(symbol="lgvn-usd")
    assert rc["called"] is True and rc["symbol"] == "LGVN-USD"
    assert rc["status"] == "error" and rc["timed_out"] is True
    assert rc["probe_timeout_s"] == 10.0 and rc["probe_timeout_binding"] == _PROBE_BINDING


def test_bridge_log_line_carries_the_coinbase_connect_receipt(monkeypatch, caplog):
    import logging

    _reset_debounce(monkeypatch)
    monkeypatch.setattr(aa, "_IGNITION_BRIDGE_PENDING", set())
    monkeypatch.setattr(aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_ignition_bridge_drain_passes", 0, raising=False)
    receipt = {
        "called": True,
        "reason": "coinbase_spot_readiness",
        "symbol": "LGVN-USD",
        "timed_out": True,
        "probe_timeout_s": 10.0,
    }
    monkeypatch.setattr(
        aa, "run_auto_arm_pass",
        lambda db, **k: {"armed": 0, "skipped": None, "coinbase_connect": receipt},
    )
    with caplog.at_level(logging.INFO, logger=aa.logger.name):
        aa.run_scoped_ignition_arm(_FakeDB(), ["LGVN-USD"])
    lines = [r.getMessage() for r in caplog.records if "ignition→arm bridge:" in r.getMessage()]
    assert lines, [r.getMessage() for r in caplog.records]
    assert "coinbase_connect=" in lines[-1] and "'timed_out': True" in lines[-1], lines[-1]
    assert "'probe_timeout_s': 10.0" in lines[-1], lines[-1]


# ───────────── the bridge can no longer be held by a hung Coinbase socket ─────────────


class _SilentTcpServer:
    """Accepts TCP connections and never sends a byte (the request is never answered)."""

    def __init__(self):
        import socket
        import threading as _th

        self._socket_mod = socket
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.held: list = []
        self._stop = _th.Event()
        self._t = _th.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
                self.held.append(conn)
            except (self._socket_mod.timeout, OSError):
                continue

    def close(self):
        self._stop.set()
        for c in self.held:
            try:
                c.close()
            except OSError:
                pass
        self.sock.close()


def test_hung_coinbase_connect_cannot_hold_bridge_beyond_bound(monkeypatch):
    """The 09-11 wedge, end to end: the REAL scoped pass, the REAL connect(), a REAL
    probe RESTClient against a server that never answers. The bridge returns within
    2x the bound, the single flight is released, and no holder is left behind."""
    import base64
    import os
    import time

    _reset_debounce(monkeypatch)
    monkeypatch.setattr(aa, "_IGNITION_BRIDGE_PENDING", set())
    monkeypatch.setattr(aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_ignition_bridge_drain_passes", 0, raising=False)
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    monkeypatch.setattr(coinbase_service, "connect", _REAL_CB_CONNECT)
    bound = 1
    monkeypatch.setattr(
        aa.settings, "chili_momentum_auto_arm_live_scheduler_interval_seconds", bound, raising=False
    )
    srv = _SilentTcpServer()
    try:
        client = coinbase_service._new_probe_client(
            "organizations/test-org/apiKeys/test-key",
            base64.b64encode(os.urandom(32)).decode("ascii"),
        )
        client.base_url = f"127.0.0.1:{srv.port}"
        monkeypatch.setattr(coinbase_service, "_cb_available", True)
        monkeypatch.setattr(coinbase_service, "_probe_client", client)
        monkeypatch.setattr(coinbase_service, "_connected", False)
        monkeypatch.setattr(coinbase_service, "_last_check", 0.0)
        t0 = time.monotonic()
        out = aa.run_scoped_ignition_arm(_FakeDB(), ["LGVN-USD"])
        elapsed = time.monotonic() - t0
    finally:
        srv.close()
    assert out is not None
    rc = out["coinbase_connect"]
    assert rc["called"] is True and rc["status"] == "error", rc
    assert rc["timed_out"] is True, rc
    assert rc["probe_timeout_s"] == float(bound), rc
    assert elapsed < 2 * bound, (elapsed, out.get("phase_seconds"))
    assert aa._ignition_bridge_inflight.acquire(blocking=False) is True
    aa._ignition_bridge_inflight.release()
    assert aa._ignition_bridge_holder is None


def _fake_get_accounts(started, release):
    """Stands in for the blocking SDK call the 09-11 holder sat in."""
    started.set()
    release.wait(10)


def test_held_warning_names_blocking_call(monkeypatch, caplog):
    """The HELD receipt names WHAT the holder is blocked in (its live stack), and the
    WARNING fires once per HELD bound per holder generation — not ~13/min."""
    import logging
    import threading
    import time

    _reset_debounce(monkeypatch)
    monkeypatch.setattr(aa, "_IGNITION_BRIDGE_PENDING", set())
    monkeypatch.setattr(aa, "_ignition_bridge_held_warned", {})
    monkeypatch.setattr(aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_ignition_bridge_drain_passes", 0, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_ignition_bridge_debounce_seconds", 30.0, raising=False)
    started, release = threading.Event(), threading.Event()

    def _blocking_pass(db, *, only_symbols=None, **k):
        _fake_get_accounts(started, release)
        return {"armed": 0}

    monkeypatch.setattr(aa, "run_auto_arm_pass", _blocking_pass)
    holder = threading.Thread(
        target=aa.run_scoped_ignition_arm, args=(_FakeDB(), ["COIW"]), name="ws-ignition_1"
    )
    holder.start()
    try:
        assert started.wait(5)
        name, stamp, ident = aa._ignition_bridge_holder
        assert name == "ws-ignition_1" and ident == holder.ident
        bound = max(30.0 * 5.0, 120.0)  # the pre-existing literal (150 s on the lane)
        # Age the holder past the bound (same generation stamp for both callers).
        # Plain assignment, NOT monkeypatch: teardown must not resurrect a holder
        # tuple after the holder thread's own finally has cleared it.
        aa._ignition_bridge_holder = (name, time.monotonic() - bound - 1.0, ident)
        with caplog.at_level(logging.DEBUG, logger=aa.logger.name):
            assert aa.run_scoped_ignition_arm(_FakeDB(), ["RDAC"]) is None
            assert aa.run_scoped_ignition_arm(_FakeDB(), ["SLE"]) is None
        held = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "HELD" in r.getMessage()
        ]
        assert len(held) == 1, held
        assert "_fake_get_accounts" in held[0], held[0]
        assert "ws-ignition_1" in held[0]
        assert "held_bound=150s" in held[0], held[0]
        assert any(
            r.levelno == logging.DEBUG and "still HELD" in r.getMessage()
            for r in caplog.records
        )
    finally:
        release.set()
        holder.join(10)
    assert not holder.is_alive()
    assert aa._ignition_bridge_holder is None
    assert aa._ignition_bridge_inflight.acquire(blocking=False) is True
    aa._ignition_bridge_inflight.release()


def test_thread_blocking_chain_is_safe_for_unknown_threads():
    assert aa._thread_blocking_chain(None) == "unknown_thread"
    assert aa._thread_blocking_chain(-12345) == "thread_gone"
