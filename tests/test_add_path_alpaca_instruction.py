"""ADD PATH — ang hugis na tinatanggap ng submit certification (planner item [30]).

ANG NATUKLASAN (measured 2026-09-11, live `chili`, 14 araw):

* Ang premisa ng planner row ay LUMA. ``builder_missing_capture_binding`` = 0 sa
  14 araw — ang lane ay tumatawid sa choke point sa pamamagitan ng
  ``_legacy_alpaca_timeshare_escape``.
* Ang TUNAY na humaharang ay ang certifier. Ang limang add site ay nagpapasa ng
  literal na ``time_in_force="gfd"`` habang ang extended-hours carve-out sa
  ``_alpaca_place_instruction_kind`` ay humihingi ng literal na ``"day"``
  kasabay ng ``extended_hours=True``. Ang ISANG add na umabot sa seam sa 14d ay
  tinanggihan doon: sid 19480, 2026-09-03 09:38:34.006451Z (05:38 ET =
  premarket), ``live_pullback_add_vetoed`` ``{"error":
  "alpaca_entry_extended_hours_not_false", "reason": "submit_failed"}``.
* BINDING: 219 sa 237 (92.4%) ng lahat ng add-path event sa 14d ay PREMARKET
  (17 regular, 1 afterhours) — kaya ang carve-out ang landas at ang literal na
  "gfd" ang harang.
* Ang gfd->day normalization sa ``_prepare_alpaca_place_claim`` ay tumatakbo sa
  claim-prep, STRIKTONG PAGKATAPOS ng certification, kaya hindi nito naisasalba
  ang add — pinapatunayan din dito.

Runnable: pytest tests/test_add_path_alpaca_instruction.py -v
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.services.trading.momentum_neural.live_runner as lr

_SRC = pathlib.Path(lr.__file__)

#: Ang limang add role + ang lokal na prefix ng bawat site sa ``tick_live_session``.
_ADD_ROLES = ("anticipation", "pyramid", "micro", "pullback", "flag")
_ADD_SITE_PREFIX = {
    "anticipation": "_ant",
    "pyramid": "_pyr",
    "micro": "_mpr",
    "pullback": "_pba",
    "flag": "_fba",
}


def _sess(*, stamp: str | None = None, symbol: str = "TPET") -> SimpleNamespace:
    """Session na may PRIMARY na one-way-door stamp lang (walang add metadata)."""
    le = {} if stamp is None else {"entry_extended_session": stamp}
    return SimpleNamespace(
        id=19480,
        correlation_id="add-path-seam",
        execution_family="alpaca_spot",
        symbol=symbol,
        risk_snapshot_json={lr.KEY_LIVE_EXEC: le},
    )


def _kwargs(*, tif: str, extended: bool, limit: str = "1.03") -> dict:
    return {
        "side": "buy",
        "position_intent": "buy_to_open",
        "time_in_force": tif,
        "extended_hours": extended,
        "limit_price": limit,
    }


# ── (A) ANG BINDING NA HARANG: tif sa extended-hours carve-out ───────────────


@pytest.mark.parametrize("role", _ADD_ROLES)
def test_the_current_gfd_shape_is_exactly_what_refuses_every_premarket_add(
    role, monkeypatch
):
    """⚠️ ANG REGRESSION NA INAAYOS NG PR. Ang lumang hugis ng add (tif="gfd" +
    extended_hours=True) ay ``invalid_entry_extended_hours`` — ito mismo ang
    ``alpaca_entry_extended_hours_not_false`` na tumanggi sa sid 19480."""
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: True)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: False)
    kind = lr._alpaca_place_instruction_kind(
        _sess(), _kwargs(tif="gfd", extended=True)
    )
    assert kind == "invalid_entry_extended_hours", role


@pytest.mark.parametrize("role", _ADD_ROLES)
def test_the_corrected_shape_certifies_in_premarket(role, monkeypatch):
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: True)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: False)
    kind = lr._alpaca_place_instruction_kind(
        _sess(),
        _kwargs(tif="day", extended=True),
        generation_session="premarket",
    )
    assert kind == "entry", role


def test_the_regular_session_shape_is_unchanged(monkeypatch):
    """Walang pagbabago sa RTH: extended_hours=False + gfd ay "entry" pa rin."""
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: False)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: False)
    assert (
        lr._alpaca_place_instruction_kind(
            _sess(), _kwargs(tif="gfd", extended=False), generation_session="regular"
        )
        == "entry"
    )


def test_the_claim_prep_normalization_runs_AFTER_certification(monkeypatch):
    """⚠️ ANG PREMISANG PINATUNAYAN: may gfd->day normalization sa claim prep,
    pero hindi nito kayang isalba ang add — hindi kailanman umaabot doon ang
    hindi-certified na instruction dahil ``_prepare_alpaca_place_claim`` ay
    bumabalik agad ng ``(None, "", None)`` kapag hindi "entry" ang hatol."""
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: True)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: False)
    kwargs = _kwargs(tif="gfd", extended=True)
    claim, cid, early = lr._prepare_alpaca_place_claim(
        object(), _sess(), kwargs, order_role="pullback"
    )
    assert (claim, cid, early) == (None, "", None)
    # Ang kwargs ay hindi hinipo: walang naganap na gfd->day rewrite.
    assert kwargs["time_in_force"] == "gfd"
    src = ast.unparse(
        next(
            n
            for n in ast.walk(ast.parse(_SRC.read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_prepare_alpaca_place_claim"
        )
    )
    assert src.index("_alpaca_risk_increasing_place") < src.index(
        "kwargs['time_in_force'] ="
    ), "ang certification ay dapat MAUNA sa normalization"


# ── (B) ANG ONE-WAY DOOR: generation stamp, hindi overwrite ──────────────────


def test_the_afterhours_add_certifies_on_its_OWN_generation_stamp(monkeypatch):
    """Ang add na binuo sa afterhours ay tumatawid kahit ang stamp ng PRIMARY sa
    ``le`` ay stale na "premarket" — dahil ang add ay nagdadala ng sarili."""
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: False)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: True)
    assert (
        lr._alpaca_place_instruction_kind(
            _sess(stamp="premarket"),
            _kwargs(tif="day", extended=True),
            generation_session="afterhours",
        )
        == "entry"
    )


@pytest.mark.parametrize("carried", ["premarket", "regular", ""])
def test_a_stale_or_absent_add_generation_stays_refused_in_afterhours(
    carried, monkeypatch
):
    """⚠️ BUO ANG PINTO. Kahit "afterhours" ang stamp ng primary sa ``le``, ang
    add na binuo sa ibang session ay HINDI tumatawid — ang dala ng add ang
    hinahatulan, at ang walang-laman na string ay fail-CLOSED."""
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: False)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: True)
    assert (
        lr._alpaca_place_instruction_kind(
            _sess(stamp="afterhours"),
            _kwargs(tif="day", extended=True),
            generation_session=carried,
        )
        == "invalid_entry_extended_hours"
    )


def test_the_primary_path_is_byte_identical_when_no_stamp_is_carried(monkeypatch):
    """``generation_session=None`` => ang DATING ``le`` lookup. Ito ang tanging
    porma na ginagamit ng primary, kaya walang nagbago para dito."""
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: False)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: True)
    kwargs = _kwargs(tif="day", extended=True)
    assert lr._alpaca_place_instruction_kind(_sess(stamp="afterhours"), kwargs) == "entry"
    assert (
        lr._alpaca_place_instruction_kind(_sess(stamp="premarket"), kwargs)
        == "invalid_entry_extended_hours"
    )
    assert (
        lr._alpaca_place_instruction_kind(_sess(stamp=None), kwargs)
        == "invalid_entry_extended_hours"
    )


def test_the_add_never_overwrites_the_primary_one_way_door_stamp():
    """⚠️ ANG DEVIATION MULA SA UNANG DISENYO. Ang re-stamp ng shared key ay
    magbubuhay ng stale na premarket generation — iyon mismo ang pinipigilan ng
    pinto. Ang stamp ay isinusulat SA ISANG LUGAR LANG: ang primary."""
    src = _SRC.read_text(encoding="utf-8")
    assert src.count('["entry_extended_session"] =') == 1
    assert 'le["entry_extended_session"] = _entry_session_now' in src


# ── (C) ANG LATENT NA SEAM: canonical na sub-$1 na limit ────────────────────


def test_the_add_shape_uses_the_canonical_alpaca_limit(monkeypatch):
    """LATENT, hindi binding: 0 sa 133 submitted entry sa 14d ang sub-$1
    (min 1.03, p05 1.15). Inaayos pa rin dahil nasa landas na hinihipo."""
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda *a, **k: "premarket",
    )
    shape = lr._alpaca_add_instruction_shape(
        _sess(), 0.123456789, execution_family="alpaca_spot"
    )
    assert shape["limit_price"] == lr.quantize_alpaca_equity_limit_price(
        0.123456789, "buy"
    )
    assert shape["limit_price"] != lr._fmt_limit_price_buy(0.123456789)
    # at ang certifier ay tumatanggap ng eksaktong hugis na ito
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: True)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: False)
    assert (
        lr._alpaca_place_instruction_kind(
            _sess(),
            {
                "side": "buy",
                "position_intent": "buy_to_open",
                "time_in_force": shape["time_in_force"],
                "extended_hours": shape["extended_hours"],
                "limit_price": shape["limit_price"],
            },
            generation_session=shape["generation_session"],
        )
        == "entry"
    )


def test_the_non_alpaca_shape_keeps_the_RH_vocabulary(monkeypatch):
    """Ang crypto / RH family ay nananatiling gfd + ``_fmt_limit_price_buy`` —
    ibang bokabularyo, at walang pagbabago sa kanila."""
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda *a, **k: "premarket",
    )
    shape = lr._alpaca_add_instruction_shape(
        _sess(), 12.3456, execution_family="robinhood_spot"
    )
    assert shape["time_in_force"] == "gfd"
    assert shape["limit_price"] == lr._fmt_limit_price_buy(12.3456)


@pytest.mark.parametrize(
    "session_now,expected_tif,expected_ext",
    [("premarket", "day", True), ("afterhours", "day", True), ("regular", "gfd", False)],
)
def test_the_shape_tracks_the_session(
    session_now, expected_tif, expected_ext, monkeypatch
):
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda *a, **k: session_now,
    )
    shape = lr._alpaca_add_instruction_shape(
        _sess(), 4.20, execution_family="alpaca_spot"
    )
    assert (shape["time_in_force"], shape["extended_hours"]) == (
        expected_tif,
        expected_ext,
    )
    assert shape["generation_session"] == session_now


def test_an_unreadable_clock_keeps_todays_regular_shape(monkeypatch):
    """Ang dating gawi ng limang site ay ``except Exception: _<role>_ext = False``.
    Pinapanatili iyon nang eksakto — walang bagong extended order sa katahimikan."""

    def _boom(*a, **k):
        raise RuntimeError("clock unreadable")

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now", _boom
    )
    shape = lr._alpaca_add_instruction_shape(
        _sess(), 4.20, execution_family="alpaca_spot"
    )
    assert shape["extended_hours"] is False
    assert shape["time_in_force"] == "gfd"
    assert shape["generation_session"] == "regular"


# ── Ang limang site mismo ────────────────────────────────────────────────────


def _tick_src() -> str:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "tick_live_session"
    )
    return ast.unparse(fn)


def test_every_add_site_carries_the_certified_shape_and_no_gfd_literal():
    src = _tick_src()
    for role, prefix in _ADD_SITE_PREFIX.items():
        assert f"{prefix}_shape = _alpaca_add_instruction_shape(" in src, role
        assert f"time_in_force={prefix}_shape['time_in_force']" in src, role
        assert f"extended_hours={prefix}_shape['extended_hours']" in src, role
        assert f"'entry_extended_session': {prefix}_shape['generation_session']" in src, role
    # Ang natitirang "gfd" na literal sa tick ay ang re-peg lang -- NAMED, hindi
    # nakatago: sinukat na 0 refusal sa 14d, at iba ang cancel/replace na makina
    # nito kaya hiwalay na item ito (nakatala sa planner row [30]).
    assert src.count("time_in_force='gfd'") == 1


def test_no_add_site_still_formats_its_limit_with_the_rh_formatter():
    src = _tick_src()
    for prefix in _ADD_SITE_PREFIX.values():
        assert f"{prefix}_limit_str = _fmt_limit_price_buy(" not in src
        assert f"{prefix}_limit_str = {prefix}_shape['limit_price']" in src


def test_the_governed_place_threads_one_generation_read_to_every_seam():
    """⚠️ Kung magkaiba ang hatol ng certification seam at ng claim-prep seam,
    ang add ay tatawid nang WALANG committed na risk reservation."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_governed_place"
    )
    src = ast.unparse(fn)
    assert src.count("_alpaca_generation_session = ") == 2  # basa + normalize
    assert "generation_session=_alpaca_generation_session" in src
    assert src.count("generation_session=_alpaca_generation_session") == 2
    # walang natitirang stamp-blind na re-certification sa loob ng wrapper
    assert "_alpaca_risk_increasing_place(sess, kwargs)" not in src
