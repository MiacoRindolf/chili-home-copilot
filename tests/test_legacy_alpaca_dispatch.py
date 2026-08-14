"""Legacy time-share dispatch (2026-08-14, operator-approved dual-track).

Ang takeover (#920) ay naglagay ng hard reroute sa dispatch boundary: bawat
alpaca_spot na session ay hinihingan ng CapturedPaperRuntime, kaya ang legacy
loop ay hindi na makapag-tick kailanman. Ang operator mode switch na
`chili_momentum_legacy_alpaca_dispatch_enabled` (default OFF) ay nagpapadaan
sa mga session na WALANG durable captured-paper owner marker sa ordinaryong
runner — ang parehong absence-preserves-the-pre-bind-path na semantics ng
`revalidate_captured_paper_session_owner`, itinaas sa dispatch boundary.
"""
import inspect
import types

import pytest

from app.config import Settings
from app.services.trading.momentum_neural import captured_paper_dispatcher as cpd


class _Sess:
    def __init__(self, sid, family="alpaca_spot", symbol="TEST", snapshot=None):
        self.id = sid
        self.execution_family = family
        self.symbol = symbol
        self.risk_snapshot_json = snapshot if snapshot is not None else {}


def _wire(monkeypatch, *, flag, marker, ordinary_calls):
    sess = _Sess(7, snapshot=({cpd._SESSION_OWNER_KEY: {"x": 1}} if marker else {}))
    monkeypatch.setattr(cpd, "_load_live_session", lambda db, sid: sess)
    monkeypatch.setattr(
        cpd, "_load_ordinary_live_session_for_update", lambda db, sid: sess
    )
    monkeypatch.setattr(
        cpd,
        "_ordinary_tick",
        lambda db, sid, non_paper_tick=None: ordinary_calls.append(sid) or {"ok": True},
    )
    monkeypatch.setattr(
        cpd.settings,
        "chili_momentum_legacy_alpaca_dispatch_enabled",
        flag,
        raising=False,
    )
    monkeypatch.setattr(cpd.settings, "chili_alpaca_paper", True, raising=False)
    return sess


def test_default_off_ay_byte_identical_legacy(monkeypatch):
    """Flag OFF: ang alpaca_spot na walang marker ay nananatiling humihingi ng
    runtime — ang eksaktong pre-change na kalabasan."""
    calls: list = []
    _wire(monkeypatch, flag=False, marker=False, ordinary_calls=calls)
    with pytest.raises(cpd.CapturedPaperRuntimeUnavailableError):
        cpd.dispatch_live_runner_tick(object(), 7)
    assert calls == []


def test_flag_on_walang_marker_ay_dumadaan_sa_ordinaryo(monkeypatch):
    calls: list = []
    _wire(monkeypatch, flag=True, marker=False, ordinary_calls=calls)
    out = cpd.dispatch_live_runner_tick(object(), 7)
    assert out == {"ok": True}
    assert calls == [7]


def test_flag_on_may_marker_ay_HINDI_nililiko(monkeypatch):
    """Ang sesyong pag-aari ng captured service (may durable marker) ay hindi
    kailanman dinadaan sa legacy escape — runtime pa rin ang hinihingi."""
    calls: list = []
    _wire(monkeypatch, flag=True, marker=True, ordinary_calls=calls)
    with pytest.raises(cpd.CapturedPaperRuntimeUnavailableError):
        cpd.dispatch_live_runner_tick(object(), 7)
    assert calls == []


def test_config_default_ay_off():
    assert (
        Settings.model_fields[
            "chili_momentum_legacy_alpaca_dispatch_enabled"
        ].default
        is False
    ), "operator mode switch — dapat OFF hangga't walang time-share window"


def test_dedicated_process_ay_hindi_gumagamit_ng_escape():
    """Istrukturang bantay: ang escape ay naka-guard sa `not captured_paper_only`
    at ang marker check ay sa LOCKED row."""
    src = inspect.getsource(cpd.dispatch_live_runner_tick)
    i_escape = src.find("chili_momentum_legacy_alpaca_dispatch_enabled")
    assert i_escape > 0
    guard_window = src[max(0, i_escape - 700) : i_escape]
    assert "not captured_paper_only" in guard_window
    after = src[i_escape : i_escape + 2200]
    assert "_load_ordinary_live_session_for_update" in after
    assert "_SESSION_OWNER_KEY" in after
