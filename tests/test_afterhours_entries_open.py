"""Bukas na ang afterhours entries (2026-08-27, tahasang utos ng operator).

ANG KASAYSAYAN: WAVE-1 FIX-8 ang nagsara (14d AH: 1W/11L −$72.65). Ngayong
araw, 11 triggers ang pumutok afterhours (AEMD, INHD, LGPS na may fresh BBO)
nang ZERO submits — dalawang gate ang nagsara: afterhours schedule mult=0.0
at ang certifier na premarket-only ang extended carve-out. Ang pagbubukas ay
REDUCED size (0.5, midday treatment) hindi full — ang lumang record ang
dahilan; ang knob ang hawakan ng operator.

Runnable: pytest tests/test_afterhours_entries_open.py -v
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

from app.config import settings
from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(LR.__file__)


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    return next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == name
    )


# ── Mga flag ─────────────────────────────────────────────────────────────────


def test_the_flags_ship_ON_at_reduced_size_with_the_record_written():
    assert settings.chili_momentum_alpaca_afterhours_entries_enabled is True
    assert float(settings.chili_momentum_afterhours_schedule_mult) == 0.5
    for name in (
        "chili_momentum_alpaca_afterhours_entries_enabled",
        "chili_momentum_afterhours_schedule_mult",
    ):
        desc = str(type(settings).model_fields[name].description or "")
        assert "1W/11L" in desc, "dapat nakasulat ang lumang AH record"
        assert "2026-08-27" in desc


# ── Certifier ────────────────────────────────────────────────────────────────


def _ah_sess(stamp: str | None = "afterhours") -> SimpleNamespace:
    le = {} if stamp is None else {"entry_extended_session": stamp}
    return SimpleNamespace(
        execution_family="alpaca_spot",
        symbol="AEMD",
        risk_snapshot_json={"momentum_live_execution": le},
    )


def test_the_certifier_accepts_the_extended_shape_in_afterhours(monkeypatch):
    """ANG PANGUNAHING KASO: LIMIT+DAY+ext=True + afterhours session + ang
    instruction ay BINUO sa afterhours (generation stamp) = entry."""
    monkeypatch.setattr(LR, "_alpaca_session_is_premarket_now", lambda s: False)
    monkeypatch.setattr(LR, "_alpaca_session_is_afterhours_now", lambda s: True)
    kind = LR._alpaca_place_instruction_kind(_ah_sess("afterhours"), {
        "side": "buy", "position_intent": "buy_to_open",
        "time_in_force": "day", "extended_hours": True,
    })
    assert kind == "entry"


def test_a_stale_premarket_generation_does_NOT_revive_in_afterhours(monkeypatch):
    """⚠️ ANG ONE-WAY DOOR. Ang instruction na binuo sa PREMARKET (stamp
    "premarket") ay hindi dapat maging placeable muli pagsapit ng 16:00 —
    ito mismo ang freeze-intent na binabantayan ng certifier."""
    monkeypatch.setattr(LR, "_alpaca_session_is_premarket_now", lambda s: False)
    monkeypatch.setattr(LR, "_alpaca_session_is_afterhours_now", lambda s: True)
    kind = LR._alpaca_place_instruction_kind(_ah_sess("premarket"), {
        "side": "buy", "position_intent": "buy_to_open",
        "time_in_force": "day", "extended_hours": True,
    })
    assert kind == "invalid_entry_extended_hours"


def test_a_missing_generation_stamp_fails_closed_in_afterhours(monkeypatch):
    """⚠️ Walang stamp = hindi mapatunayang pinagmulan = tanggihan; ang
    upstream ay magre-regenerate na may stamp sa susunod na tick."""
    monkeypatch.setattr(LR, "_alpaca_session_is_premarket_now", lambda s: False)
    monkeypatch.setattr(LR, "_alpaca_session_is_afterhours_now", lambda s: True)
    kind = LR._alpaca_place_instruction_kind(_ah_sess(None), {
        "side": "buy", "position_intent": "buy_to_open",
        "time_in_force": "day", "extended_hours": True,
    })
    assert kind == "invalid_entry_extended_hours"


def test_the_generation_site_stamps_the_session():
    src = _SRC.read_text(encoding="utf-8")
    assert 'le["entry_extended_session"] = _entry_session_now' in src


def test_the_certifier_still_rejects_extended_outside_both_windows(monkeypatch):
    """⚠️ Regular/closed session + ext=True ay invalid pa rin — walang silent
    crossover; buo ang freeze-intent."""
    monkeypatch.setattr(LR, "_alpaca_session_is_premarket_now", lambda s: False)
    monkeypatch.setattr(LR, "_alpaca_session_is_afterhours_now", lambda s: False)
    sess = SimpleNamespace(execution_family="alpaca_spot", symbol="AEMD")
    kind = LR._alpaca_place_instruction_kind(sess, {
        "side": "buy", "position_intent": "buy_to_open",
        "time_in_force": "day", "extended_hours": True,
    })
    assert kind == "invalid_entry_extended_hours"


def test_the_flag_off_restores_premarket_only(monkeypatch):
    monkeypatch.setattr(LR, "_alpaca_session_is_premarket_now", lambda s: False)
    monkeypatch.setattr(LR, "_alpaca_session_is_afterhours_now", lambda s: True)
    monkeypatch.setattr(
        LR.settings, "chili_momentum_alpaca_afterhours_entries_enabled",
        False, raising=False)
    sess = SimpleNamespace(execution_family="alpaca_spot", symbol="AEMD")
    kind = LR._alpaca_place_instruction_kind(sess, {
        "side": "buy", "position_intent": "buy_to_open",
        "time_in_force": "day", "extended_hours": True,
    })
    assert kind == "invalid_entry_extended_hours"


def test_the_session_helper_fails_closed(monkeypatch):
    """⚠️ Ang clock error ay hindi kailanman nagbubukas ng carve-out."""
    import app.services.trading.momentum_neural.market_profile as MP

    def _boom(*a, **k):
        raise RuntimeError("clock unreadable")

    monkeypatch.setattr(MP, "market_session_now", _boom)
    sess = SimpleNamespace(symbol="AEMD")
    assert LR._alpaca_session_is_afterhours_now(sess) is False


# ── Admission window + multiplier + spread tighten (source assertions) ──────


def test_the_admission_window_has_the_afterhours_analog():
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("_afterhours_window_ok = (")
    region = src[i:i + 500]
    assert '"afterhours"' in region
    assert "clock.get(\"ok\") is True" in region.replace("'", '"')
    assert "chili_momentum_alpaca_afterhours_entries_enabled" in region
    assert "or _afterhours_window_ok" in src


def test_the_schedule_mult_is_a_knob_and_late_stays_zero():
    src = ast.unparse(_fn("tick_live_session"))
    assert "chili_momentum_afterhours_schedule_mult" in src
    i = src.index("chili_momentum_afterhours_schedule_mult")
    region = src[i - 800:i + 800]
    assert "'late': 0.0" in region or '"late": 0.0' in region, (
        "ang late window ay nananatiling 0.0"
    )


def test_the_spread_tighten_covers_afterhours_too():
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("afterhours idinagdag 2026-08-27 kasabay ng AH entry window")
    region = src[max(0, i - 600):i + 300]
    assert "_skip_spread_gate = False" in region, (
        "ang manipis-na-book na higpit ay dapat sakop din ang afterhours"
    )
