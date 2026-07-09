from __future__ import annotations

from types import SimpleNamespace

import app.services.trading.momentum_neural.ross_event_admission as admission
from app.services.trading.momentum_neural.universe import (
    EQUITY_ROSS_SMALLCAP,
    build_equity_universe,
    ross_smallcap_profile_evidence,
)


class _FakeDB:
    def flush(self) -> None:
        pass


class _FakeCandidateQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.rows


class _FakeCandidateDB(_FakeDB):
    def __init__(self, rows):
        self.rows = rows
        self.flushes = 0

    def query(self, *_args, **_kwargs):
        return _FakeCandidateQuery(self.rows)

    def flush(self) -> None:
        self.flushes += 1


def _signal(symbol: str = "DXTS") -> dict:
    return {
        "ticker": symbol,
        "symbol": symbol,
        "price": 5.40,
        "last_price": 5.40,
        "daily_change_pct": 62.0,
        "todays_change_perc": 62.0,
        "rvol_pace": 18.0,
        "rvol": 18.0,
        "volume": 750_000,
        "day_volume": 750_000,
        "dollar_volume": 4_050_000,
        "scanner_source": "ross warrior 5 pillars low float",
        "daily_breaking_major": True,
    }


def _candidate(symbol: str = "DXTS") -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        variant_id=42,
        viability_score=0.83,
        execution_readiness_json={"extra": {"ross_signals": {symbol: _signal(symbol)}}},
    )


def _candidate_without_signal(symbol: str = "JEM") -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        variant_id=84,
        viability_score=0.81,
        execution_readiness_json={"extra": {}},
    )


def _enable(monkeypatch) -> None:
    admission._LAST_ATTEMPT_MONOTONIC.clear()
    monkeypatch.setattr(admission.settings, "chili_momentum_ross_event_admission_enabled", True, raising=False)
    monkeypatch.setattr(admission.settings, "chili_momentum_live_runner_enabled", True, raising=False)
    monkeypatch.setattr(admission.settings, "chili_autotrader_user_id", 7, raising=False)
    monkeypatch.setattr(admission.settings, "chili_momentum_auto_arm_live_scheduler_enabled", False, raising=False)
    monkeypatch.setattr(admission.settings, "chili_momentum_ross_event_admission_tick_count", 1, raising=False)


def test_event_admission_arms_and_ticks_without_scheduler(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(admission.settings, "chili_momentum_ross_event_admission_tick_count", 2, raising=False)
    calls: list[tuple[str, object]] = []

    def begin(db, **kwargs):
        calls.append(("begin", kwargs))
        return {"ok": True, "arm_token": "tok", "session_id": 777}

    def confirm(db, **kwargs):
        calls.append(("confirm", kwargs))
        return {"ok": True, "state": "queued_live", "session_id": 777}

    def tick(db, session_id):
        calls.append(("tick", session_id))
        return {"ok": True, "state": "watching_live"}

    out = admission.admit_ross_event(
        _FakeDB(),
        symbol="DXTS",
        signal=_signal("DXTS"),
        source="ross_transcript",
        refresh_viability=False,
        begin_live_arm_fn=begin,
        confirm_live_arm_fn=confirm,
        tick_live_session_fn=tick,
        append_event_fn=lambda *args, **kwargs: None,
        candidate_provider=lambda db, sym, now: _candidate(sym),
        market_open_fn=lambda sym: True,
        ignore_cooldown=True,
    )

    assert out["admitted"] is True
    assert out["session_id"] == 777
    assert out["ross_universe_reason"] == "ross_universe_profile_ok"
    assert [name for name, _ in calls] == ["begin", "confirm", "tick", "tick"]
    assert calls[0][1]["execution_family"] == "robinhood_agentic_mcp"


def test_event_admission_defaults_to_immediate_runner_tick(monkeypatch) -> None:
    _enable(monkeypatch)
    calls: list[tuple[str, object]] = []

    out = admission.admit_ross_event(
        _FakeDB(),
        symbol="DXTS",
        signal=_signal("DXTS"),
        source="iqfeed_l1",
        refresh_viability=False,
        begin_live_arm_fn=lambda db, **kwargs: calls.append(("begin", kwargs))
        or {"ok": True, "arm_token": "tok", "session_id": 777},
        confirm_live_arm_fn=lambda db, **kwargs: calls.append(("confirm", kwargs))
        or {"ok": True, "state": "queued_live", "session_id": 777},
        tick_live_session_fn=lambda db, session_id: calls.append(("tick", session_id))
        or {"ok": True, "state": "watching_live"},
        append_event_fn=lambda *args, **kwargs: None,
        candidate_provider=lambda db, sym, now: _candidate(sym),
        market_open_fn=lambda sym: True,
        ignore_cooldown=True,
    )

    assert out["admitted"] is True
    assert out["ticked"] == 1
    assert [name for name, _ in calls] == ["begin", "confirm", "tick"]


def test_event_admission_rejects_mega_cap_even_if_live_candidate(monkeypatch) -> None:
    _enable(monkeypatch)
    begin_calls = []
    meta_signal = _signal("META")
    meta_signal.update({"price": 710.0, "last_price": 710.0, "dollar_volume": 2_000_000_000})

    out = admission.admit_ross_event(
        _FakeDB(),
        symbol="META",
        signal=meta_signal,
        source="iqfeed_l1",
        refresh_viability=False,
        begin_live_arm_fn=lambda *a, **k: begin_calls.append(k),
        confirm_live_arm_fn=lambda *a, **k: {"ok": True},
        tick_live_session_fn=lambda *a, **k: {"ok": True},
        candidate_provider=lambda db, sym, now: _candidate(sym),
        market_open_fn=lambda sym: True,
        ignore_cooldown=True,
    )

    assert out["skipped"] == "ross_universe_rejected"
    assert out["ross_universe_reason"] == "ross_universe_price_above_profile"
    assert begin_calls == []


def test_iqfeed_repeated_universe_reject_uses_front_door_cooldown(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        admission.settings,
        "chili_momentum_ross_event_admission_cooldown_seconds",
        2.0,
        raising=False,
    )
    meta_signal = _signal("META")
    meta_signal.update({"price": 710.0, "last_price": 710.0, "dollar_volume": 2_000_000_000})

    first = admission.admit_ross_event(
        _FakeDB(),
        symbol="META",
        signal=meta_signal,
        source="iqfeed_l1",
        refresh_viability=False,
        candidate_provider=lambda db, sym, now: _candidate(sym),
        market_open_fn=lambda sym: True,
    )
    second = admission.admit_ross_event(
        _FakeDB(),
        symbol="META",
        signal=meta_signal,
        source="iqfeed_l1",
        refresh_viability=False,
        candidate_provider=lambda db, sym, now: _candidate(sym),
        market_open_fn=lambda sym: True,
    )

    assert first["skipped"] == "ross_universe_rejected"
    assert second["skipped"] == "cooldown"


def test_iqfeed_without_candidate_skips_before_universe_probe(monkeypatch) -> None:
    _enable(monkeypatch)
    snapshot_calls: list[str] = []

    out = admission.admit_ross_event(
        _FakeDB(),
        symbol="BB",
        signal=None,
        source="iqfeed_l1",
        refresh_viability=False,
        candidate_provider=lambda db, sym, now: None,
        snapshot_provider=lambda sym: snapshot_calls.append(sym) or {},
        market_open_fn=lambda sym: True,
        ignore_cooldown=True,
    )

    assert out["skipped"] == "no_fresh_live_eligible_candidate"
    assert snapshot_calls == []


def test_iqfeed_event_can_seed_viability_when_no_candidate_exists(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(admission.settings, "chili_momentum_ross_event_admission_tick_count", 2, raising=False)
    calls: list[tuple[str, object]] = []
    candidate = _candidate("LHAI")
    candidate.variant_id = 123

    def candidate_provider(db, sym, now):
        calls.append(("candidate", sym))
        # Before pipeline refresh there is no row; after refresh, return the new one.
        return candidate if any(name == "pipeline" for name, _ in calls) else None

    def pipeline(db, meta):
        calls.append(("pipeline", meta))
        return {"ok": True, "created": 1}

    def begin(db, **kwargs):
        calls.append(("begin", kwargs))
        return {"ok": True, "arm_token": "tok", "session_id": 991}

    def confirm(db, **kwargs):
        calls.append(("confirm", kwargs))
        return {"ok": True, "state": "queued_live", "session_id": 991}

    out = admission.admit_ross_event(
        _FakeDB(),
        symbol="LHAI",
        signal=None,
        source="iqfeed_l1",
        refresh_viability=True,
        run_momentum_tick_fn=pipeline,
        begin_live_arm_fn=begin,
        confirm_live_arm_fn=confirm,
        tick_live_session_fn=lambda db, sid: calls.append(("tick", sid)) or {"ok": True, "state": "watching_live"},
        append_event_fn=lambda *args, **kwargs: None,
        candidate_provider=candidate_provider,
        snapshot_provider=lambda sym: {
            "ticker": sym,
            "todaysChangePerc": 277.0,
            "day": {"v": 300_000_000, "h": 2.84, "l": 1.26},
            "lastTrade": {"p": 2.49},
        },
        market_open_fn=lambda sym: True,
        ignore_cooldown=True,
    )

    assert out["admitted"] is True
    assert out["variant_id"] == 123
    assert [name for name, _ in calls].count("pipeline") == 1
    assert calls[-2][0] == "tick"


def test_event_admission_dry_run_proves_would_admit_without_arming(monkeypatch) -> None:
    _enable(monkeypatch)
    begin_calls = []

    out = admission.admit_ross_event(
        _FakeDB(),
        symbol="LHAI",
        signal=_signal("LHAI"),
        source="iqfeed_l1",
        refresh_viability=False,
        begin_live_arm_fn=lambda *a, **k: begin_calls.append(k),
        confirm_live_arm_fn=lambda *a, **k: {"ok": True},
        tick_live_session_fn=lambda *a, **k: {"ok": True},
        append_event_fn=lambda *args, **kwargs: None,
        candidate_provider=lambda db, sym, now: _candidate(sym),
        market_open_fn=lambda sym: True,
        ignore_cooldown=True,
        dry_run=True,
    )

    assert out["skipped"] == "dry_run"
    assert out["would_admit"] is True
    assert out["variant_id"] == 42
    assert begin_calls == []


def test_iqfeed_rejects_poisoned_transcript_candidate_before_universe_probe(monkeypatch) -> None:
    _enable(monkeypatch)
    snapshot_calls: list[str] = []
    bad_signal = _signal("GP")
    bad_signal.update(
        {
            "price": 1.595,
            "last_price": 1.595,
            "daily_change_pct": -5.05,
            "todays_change_perc": -5.05,
            "rvol_pace": 0.44,
            "dollar_volume": 574_019.76,
            "source": "ross_audio_transcript warrior ross 5 pillars",
            "scanner_source": "ross_audio_transcript",
            "signal_type": "ross_transcript_mention",
            "transcript_text": "I got five minutes with a GP when I was sick.",
        }
    )
    candidate = SimpleNamespace(
        symbol="GP",
        variant_id=42,
        viability_score=0.48,
        live_eligible=True,
        paper_eligible=True,
        explain_json={},
        execution_readiness_json={"extra": {"ross_signals": {"GP": bad_signal}}},
    )

    out = admission.admit_ross_event(
        _FakeDB(),
        symbol="GP",
        signal=None,
        source="iqfeed_l1",
        refresh_viability=False,
        candidate_provider=lambda db, sym, now: candidate,
        snapshot_provider=lambda sym: snapshot_calls.append(sym) or {},
        market_open_fn=lambda sym: True,
        ignore_cooldown=True,
    )

    assert out["skipped"] == "ross_transcript_context_rejected"
    assert snapshot_calls == []
    assert candidate.live_eligible is False
    assert candidate.paper_eligible is False
    assert candidate.explain_json["demoted_reason"] == "ross_transcript_context_rejected"


def test_ross_universe_rejects_warrants_and_leveraged_etps() -> None:
    warrant_signal = _signal("RAAQW")
    warrant_signal.update({"price": 5.10, "last_price": 5.10, "dollar_volume": 2_600_000})
    ok, reason, _debug = ross_smallcap_profile_evidence("RAAQW", signal=warrant_signal)
    assert ok is False
    assert reason == "not_equity_symbol"

    etp_signal = _signal("SOXS")
    etp_signal.update({"price": 3.89, "last_price": 3.89, "daily_change_pct": 20.0, "dollar_volume": 675_000_000})
    ok, reason, _debug = ross_smallcap_profile_evidence("SOXS", signal=etp_signal)
    assert ok is False
    assert reason == "not_equity_symbol"


def test_ross_universe_builder_filters_non_common_stock_symbols() -> None:
    snapshot = [
        {
            "ticker": "RAAQW",
            "todaysChangePerc": 40.0,
            "day": {"v": 600_000, "h": 5.5, "l": 4.0},
            "lastTrade": {"p": 5.1},
        },
        {
            "ticker": "SOXS",
            "todaysChangePerc": 20.0,
            "day": {"v": 200_000_000, "h": 3.9, "l": 3.5},
            "lastTrade": {"p": 3.89},
        },
        {
            "ticker": "LHAI",
            "todaysChangePerc": 277.0,
            "day": {"v": 300_000_000, "h": 2.84, "l": 1.26},
            "lastTrade": {"p": 2.49},
        },
    ]

    assert build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snapshot) == ["LHAI"]


def test_fresh_live_candidate_demotes_poisoned_transcript_duplicates(monkeypatch) -> None:
    _enable(monkeypatch)
    bad_signal = _signal("GP")
    bad_signal.update(
        {
            "source": "ross_audio_transcript warrior ross 5 pillars",
            "scanner_source": "ross_audio_transcript",
            "signal_type": "ross_transcript_mention",
            "transcript_text": "I got five minutes with a GP when I was sick.",
        }
    )
    rows = [
        SimpleNamespace(
            symbol="GP",
            live_eligible=True,
            paper_eligible=True,
            explain_json={},
            execution_readiness_json={"extra": {"ross_signals": {"GP": bad_signal}}},
        ),
        SimpleNamespace(
            symbol="GP",
            live_eligible=True,
            paper_eligible=True,
            explain_json={},
            execution_readiness_json={"extra": {"ross_signals": {"GP": bad_signal}}},
        ),
    ]
    db = _FakeCandidateDB(rows)

    candidate = admission._fresh_live_candidate(db, "GP")

    assert candidate is None
    assert db.flushes == 1
    assert [r.live_eligible for r in rows] == [False, False]
    assert [r.paper_eligible for r in rows] == [False, False]


def test_iqfeed_admission_uses_live_universe_evidence_without_stored_signal(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(admission.settings, "chili_momentum_ross_event_admission_tick_count", 2, raising=False)
    calls: list[tuple[str, object]] = []

    snapshot = {
        "ticker": "JEM",
        "lastTrade": {"p": 4.20},
        "min": {"c": 4.20, "av": 2_000_000},
        "todaysChangePerc": 35.0,
    }

    out = admission.admit_ross_event(
        _FakeDB(),
        symbol="JEM",
        signal=None,
        source="iqfeed_l1",
        refresh_viability=False,
        snapshot_provider=lambda sym: snapshot,
        begin_live_arm_fn=lambda db, **k: calls.append(("begin", k))
        or {"ok": True, "arm_token": "tok", "session_id": 888},
        confirm_live_arm_fn=lambda db, **k: calls.append(("confirm", k))
        or {"ok": True, "state": "queued_live", "session_id": 888},
        tick_live_session_fn=lambda db, sid: calls.append(("tick", sid))
        or {"ok": True, "state": "watching_live"},
        append_event_fn=lambda *args, **kwargs: None,
        candidate_provider=lambda db, sym, now: _candidate_without_signal(sym),
        market_open_fn=lambda sym: True,
        ignore_cooldown=True,
    )

    assert out["admitted"] is True
    assert out["session_id"] == 888
    assert out["ross_universe_reason"] == "ross_universe_profile_ok"
    assert out["ross_evidence_reason"] == "tick_first_pullback_watch"
    assert [name for name, _ in calls] == ["begin", "confirm", "tick", "tick"]


def test_iqfeed_can_admit_independent_a_plus_smallcap_without_ross_source(monkeypatch) -> None:
    _enable(monkeypatch)
    calls: list[tuple[str, object]] = []
    signal = {
        "ticker": "PPCB",
        "symbol": "PPCB",
        "price": 1.72,
        "last_price": 1.72,
        "daily_change_pct": 18.5,
        "todays_change_perc": 18.5,
        "volume": 4_100_000,
        "day_volume": 4_100_000,
        "dollar_volume": 7_052_000,
        "source": "iqfeed_l1 tape_delta_ignite running_up_ignite",
        "scanner_source": "iqfeed_l1 tape_delta_ignite",
        "signal_type": "running_up_ignite",
    }

    out = admission.admit_ross_event(
        _FakeDB(),
        symbol="PPCB",
        signal=signal,
        source="iqfeed_l1",
        refresh_viability=False,
        begin_live_arm_fn=lambda db, **k: calls.append(("begin", k))
        or {"ok": True, "arm_token": "tok", "session_id": 889},
        confirm_live_arm_fn=lambda db, **k: calls.append(("confirm", k))
        or {"ok": True, "state": "queued_live", "session_id": 889},
        tick_live_session_fn=lambda db, sid: calls.append(("tick", sid))
        or {"ok": True, "state": "watching_live"},
        append_event_fn=lambda *args, **kwargs: None,
        candidate_provider=lambda db, sym, now: _candidate(sym),
        market_open_fn=lambda sym: True,
        ignore_cooldown=True,
    )

    assert out["admitted"] is True
    assert out["ross_universe_reason"] == "ross_universe_profile_ok"
    assert out["ross_evidence_reason"] == "independent_smallcap_a_plus_watch"
    assert out["independent_smallcap_a_plus_reason"] == "independent_smallcap_a_plus_watch"
    assert [name for name, _ in calls] == ["begin", "confirm", "tick"]


def test_iqfeed_rejects_weak_independent_smallcap_when_not_ross_quality(monkeypatch) -> None:
    _enable(monkeypatch)
    begin_calls = []
    signal = {
        "ticker": "PPCB",
        "symbol": "PPCB",
        "price": 1.72,
        "last_price": 1.72,
        "daily_change_pct": 6.0,
        "todays_change_perc": 6.0,
        "volume": 120_000,
        "day_volume": 120_000,
        "dollar_volume": 206_400,
        "source": "iqfeed_l1 tape_delta_ignite",
        "scanner_source": "iqfeed_l1 tape_delta_ignite",
        "signal_type": "running_up_ignite",
    }

    out = admission.admit_ross_event(
        _FakeDB(),
        symbol="PPCB",
        signal=signal,
        source="iqfeed_l1",
        refresh_viability=False,
        begin_live_arm_fn=lambda *a, **k: begin_calls.append(k),
        confirm_live_arm_fn=lambda *a, **k: {"ok": True},
        tick_live_session_fn=lambda *a, **k: {"ok": True},
        candidate_provider=lambda db, sym, now: _candidate(sym),
        market_open_fn=lambda sym: True,
        ignore_cooldown=True,
    )

    assert out["skipped"] == "ross_universe_rejected"
    assert out["ross_universe_reason"] == "ross_universe_dollar_volume_below_profile"
    assert begin_calls == []


def test_event_admission_fails_closed_without_universe_proof(monkeypatch) -> None:
    _enable(monkeypatch)
    weak_signal = {
        "ticker": "CANF",
        "daily_change_pct": 120.0,
        "rvol_pace": 22.0,
        "scanner_source": "ross 5 pillars",
    }

    out = admission.admit_ross_event(
        _FakeDB(),
        symbol="CANF",
        signal=weak_signal,
        source="ross_transcript",
        refresh_viability=False,
        candidate_provider=lambda db, sym, now: _candidate(sym),
        snapshot_provider=lambda sym: None,
        market_open_fn=lambda sym: True,
        ignore_cooldown=True,
    )

    assert out["skipped"] == "ross_universe_rejected"
    assert out["ross_universe_reason"] == "ross_universe_missing_price"


def test_confirm_blocked_never_ticks(monkeypatch) -> None:
    _enable(monkeypatch)
    calls: list[str] = []

    out = admission.admit_ross_event(
        _FakeDB(),
        symbol="DXTS",
        signal=_signal("DXTS"),
        source="ws_ignition",
        refresh_viability=False,
        begin_live_arm_fn=lambda db, **k: {"ok": True, "arm_token": "tok", "session_id": 555},
        confirm_live_arm_fn=lambda db, **k: {"ok": False, "error": "broker_not_ready"},
        tick_live_session_fn=lambda db, sid: calls.append("tick"),
        candidate_provider=lambda db, sym, now: _candidate(sym),
        market_open_fn=lambda sym: True,
        ignore_cooldown=True,
    )

    assert out["skipped"] == "confirm_blocked"
    assert out["confirm_error"] == "broker_not_ready"
    assert calls == []
