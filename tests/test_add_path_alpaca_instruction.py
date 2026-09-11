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
    # Ang kwargs ay hindi hinipo: walang naganap na gfd->day rewrite. ITO ang
    # buong patunay -- kung tumakbo muna ang normalization ay "day" na sana ito
    # at tatawid na sana ang add. (Review round: ang dating src.index() na
    # paghahambing ng teksto ay tinanggal; ang gawi na mismo ang sinusukat.)
    assert kwargs["time_in_force"] == "gfd"
    # ... at ang TAMANG hugis ay TUMATAWID sa gate na iyon: hindi na ito ang
    # (None, "", None) na "hindi risk-increasing", kundi umaabot na sa mga
    # susunod na seam (na may cid sa resibo). Iyon ang buong kaibahan.
    ok_claim, ok_cid, ok_early = lr._prepare_alpaca_place_claim(
        object(),
        _sess(),
        _kwargs(tif="day", extended=True) | {"client_order_id": "chili_ml_pba_1"},
        order_role="pullback",
        generation_session="premarket",
    )
    assert (ok_claim, ok_cid, ok_early) != (None, "", None)
    assert ok_cid == "chili_ml_pba_1"
    assert ok_early is not None and ok_early["ok"] is False


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


@pytest.mark.parametrize("carried", ["premarket", "regular", "unknown", ""])
def test_a_stale_or_absent_add_generation_stays_refused_in_afterhours(
    carried, monkeypatch
):
    """⚠️ BUO ANG PINTO. Kahit "afterhours" ang stamp ng primary sa ``le``, ang
    add na binuo sa ibang session ay HINDI tumatawid — ang dala ng add ang
    hinahatulan, at ang walang-laman na string ay fail-CLOSED.

    REVIEW ROUND: ang pagtanggi ay may SARILING pangalan na ngayon
    (``invalid_entry_generation_session``) sa halip na ilubog sa
    ``invalid_entry_extended_hours`` — kung hindi ay hindi mapaghihiwalay ng
    resibo ang "maling hugis" sa "lumipat ang session"."""
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: False)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: True)
    kind = lr._alpaca_place_instruction_kind(
        _sess(stamp="afterhours"),
        _kwargs(tif="day", extended=True),
        generation_session=carried,
    )
    assert kind == "invalid_entry_generation_session"
    assert kind not in lr._ALPACA_CERTIFIED_INSTRUCTION_KINDS
    assert (
        lr._ALPACA_INSTRUCTION_REFUSAL_ERRORS[kind]
        == "alpaca_entry_generation_session_not_certified"
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


def test_the_add_never_overwrites_the_primary_one_way_door_stamp(monkeypatch):
    """⚠️ ANG DEVIATION MULA SA UNANG DISENYO. Ang re-stamp ng shared key ay
    magbubuhay ng stale na premarket generation — iyon mismo ang pinipigilan ng
    pinto.

    REVIEW ROUND: sinusukat na ito bilang GAWI. Patakbuhin ang buong
    ``_governed_place`` para sa isang add na may SARILING stamp at tingnan ang
    ``le`` pagkatapos: ang stamp ng primary ay hindi ginalaw."""
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: True)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: False)
    monkeypatch.setattr(lr, "captured_paper_selection_required", lambda **k: False)
    monkeypatch.setattr(lr, "_alpaca_execution_quarantine_reason", lambda s: None)
    monkeypatch.setattr(lr, "_alpaca_entries_quarantined", lambda s: False)
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: True)
    monkeypatch.setattr(
        lr, "_strict_alpaca_account_identity",
        lambda a, s: (False, {"reason": "sentinel_stop_here"}),
    )
    sess = _sess(stamp="premarket")
    res = lr._governed_place(
        object(),
        lambda **k: None,
        sess=sess,
        alpaca_order_role="pullback",
        alpaca_role_metadata={"entry_extended_session": "premarket"},
        product_id="TPET",
        side="buy",
        position_intent="buy_to_open",
        time_in_force="day",
        extended_hours=True,
        limit_price="1.03",
        base_size="10",
        client_order_id="chili_ml_pba_19480_aaa1",
    )
    assert res["error"] == "sentinel_stop_here"
    # ang stamp ng PRIMARY sa `le` ay hindi hinipo ng add
    assert (
        sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]["entry_extended_session"]
        == "premarket"
    )
    assert set(sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]) == {
        "entry_extended_session"
    }


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
    # REVIEW ROUND: ang STAMP ay "unknown", hindi ginawa-gawang "regular" (ang
    # ginagawa rin ng primary sa parehong sitwasyon). Ang HUGIS ay hindi nagbago.
    assert shape["generation_session"] == "unknown"


# ── Ang limang site mismo ────────────────────────────────────────────────────


def _tick_src() -> str:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "tick_live_session"
    )
    return ast.unparse(fn)


def _governed_place_calls() -> list[ast.Call]:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_governed_place"
    ]


def test_every_add_site_places_with_its_own_shape():
    """WIRING LINT (AST keyword nodes, hindi substring ng pinagmulan).

    REVIEW ROUND: ang dating porma nito ay naghahambing ng TEKSTO
    (``f"time_in_force={prefix}_shape[...]" in src``), kaya pumapasa ito sa maling
    gawi at bumabagsak sa walang-saysay na refactor. Ngayon ang hinahatulan ay ang
    AST ng tawag: ang bawat isa sa limang add ay nagpapasa ng ``time_in_force`` /
    ``extended_hours`` / ``entry_extended_session`` na galing sa SARILING
    ``_<prefix>_shape``, at ng ``limit_price`` mula sa ``_<prefix>_limit_str``."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    # Ang isang site (pyramid) ay bumubuo muna ng `_<prefix>_kwargs` at
    # nagpapasa nito bilang `**`; sundan iyon para pareho ang hinahatulan.
    prebuilt: dict[str, dict] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.endswith("_kwargs")
        ):
            value = node.value
            if isinstance(value, ast.IfExp):
                value = value.body
            if isinstance(value, ast.Call) and ast.unparse(value.func) == "dict":
                prebuilt[node.targets[0].id] = {
                    k.arg: k.value for k in value.keywords if k.arg
                }

    by_role = {}
    for call in _governed_place_calls():
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        for spread in (k.value for k in call.keywords if k.arg is None):
            if isinstance(spread, ast.Name) and spread.id in prebuilt:
                kw = {**prebuilt[spread.id], **kw}
        role = kw.get("alpaca_order_role")
        if isinstance(role, ast.Constant) and role.value in _ADD_SITE_PREFIX:
            by_role[role.value] = kw
    assert set(by_role) == set(_ADD_SITE_PREFIX)
    for role, kw in by_role.items():
        prefix = _ADD_SITE_PREFIX[role]
        assert ast.unparse(kw["time_in_force"]) == f"{prefix}_shape['time_in_force']"
        assert ast.unparse(kw["extended_hours"]) == f"{prefix}_shape['extended_hours']"
        assert ast.unparse(kw["limit_price"]) == f"{prefix}_limit_str"
        meta = kw["alpaca_role_metadata"]
        assert isinstance(meta, ast.Dict)
        stamped = [
            ast.unparse(v)
            for k, v in zip(meta.keys, meta.values)
            if isinstance(k, ast.Constant) and k.value == "entry_extended_session"
        ]
        assert stamped == [f"{prefix}_shape['generation_session']"], role


def test_no_add_site_still_formats_its_limit_with_the_rh_formatter():
    """WIRING LINT (AST): walang add site na kumukuha ng limit nito mula sa
    ``_fmt_limit_price_buy`` — ang shape ang may-ari ng hugis na iyon."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "tick_live_session"
    )
    prefixes = set(_ADD_SITE_PREFIX.values())
    seen = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id.endswith("_limit_str")):
            continue
        prefix = target.id[: -len("_limit_str")]
        if prefix not in prefixes:
            continue
        seen.add(prefix)
        assert "_fmt_limit_price_buy(" not in ast.unparse(node.value), target.id
    assert seen == prefixes


def test_the_governed_place_threads_one_generation_read_to_every_seam(monkeypatch):
    """⚠️ Kung magkaiba ang hatol ng certification seam at ng claim-prep seam,
    ang add ay tatawid nang WALANG committed na risk reservation.

    REVIEW ROUND: sinusukat na ito bilang GAWI — pareho ang stamp na NATANGGAP ng
    dalawang seam, hindi pareho ang teksto ng pinagmulan."""
    seen: list = []

    def _kind(sess, kwargs, *, generation_session=None):
        seen.append(("certify", generation_session))
        return "entry"

    def _claim(adapter, sess, kwargs, *, generation_session=None, **rest):
        seen.append(("claim", generation_session))
        return None, "", {"ok": False, "error": "sentinel_claim_seam"}

    monkeypatch.setattr(lr, "_alpaca_place_instruction_kind", _kind)
    monkeypatch.setattr(lr, "_prepare_alpaca_place_claim", _claim)
    monkeypatch.setattr(lr, "captured_paper_selection_required", lambda **k: False)
    monkeypatch.setattr(lr, "_alpaca_execution_quarantine_reason", lambda s: None)
    monkeypatch.setattr(lr, "_alpaca_entries_quarantined", lambda s: False)
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: True)
    monkeypatch.setattr(
        lr,
        "_strict_alpaca_account_identity",
        lambda a, s: (
            True,
            {"account_id": "acct", "equity": 13000.0, "buying_power": 26000.0},
        ),
    )
    monkeypatch.setattr(lr, "_strict_alpaca_rth_entry_window", lambda a, s: (True, {}))
    monkeypatch.setattr(
        lr,
        "_final_alpaca_execution_bbo_check",
        lambda *a, **k: (True, {"max_age_seconds": 2.0, "_execution_freshness": None}),
    )
    monkeypatch.setattr(
        lr, "_strict_alpaca_owned_entry_posture", lambda a, s, **k: (True, {})
    )
    monkeypatch.setattr(
        lr, "_adaptive_alpaca_daily_risk_admission", lambda *a, **k: (True, {})
    )
    monkeypatch.setattr(
        lr, "_final_first_dip_adaptive_confirmation", lambda *a, **k: (True, {})
    )
    monkeypatch.setattr(
        lr, "_final_alpaca_financial_breaker_admission", lambda *a, **k: (True, {})
    )
    monkeypatch.setattr(
        lr, "_record_final_alpaca_breaker_admission", lambda *a, **k: None
    )
    res = lr._governed_place(
        object(),
        lambda **k: None,
        sess=_sess(),
        alpaca_order_role="pullback",
        alpaca_role_metadata={"entry_extended_session": "afterhours"},
        product_id="TPET",
        side="buy",
        position_intent="buy_to_open",
        time_in_force="day",
        extended_hours=True,
        limit_price="1.03",
        base_size="10",
        client_order_id="chili_ml_pba_19480_aaa1",
    )
    assert res["error"] == "sentinel_claim_seam"
    stamps = {stage: value for stage, value in seen}
    assert set(stamps) == {"certify", "claim"}
    assert stamps["certify"] == stamps["claim"] == "afterhours"
