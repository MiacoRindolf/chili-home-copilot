"""ADD PATH — ang mga pag-aayos mula sa REVIEW ng PR ng item [30].

Bawat test dito ay tumutugma sa ISANG natuklasan ng refuter at pinapatunayan ang
GAWI (hindi ang teksto ng pinagmulan):

1. ANG POSTURE ANG HULING 100% NA HARANG. Ang ``_strict_alpaca_empty_entry_posture``
   ay bumabalik ng ``alpaca_account_position_exposure_present`` sa sandaling may
   bukas na posisyon — at ang add, sa kahulugan, ay pumuputok LAMANG habang may
   bukas na posisyon. Kaya kahit tama na ang instruction shape ay sarado pa rin
   ang landas. Ang tamang kontrata ng exposure-on-top-of-exposure ay ang OWNED
   posture.
2. ANG ADD AY HINDI KAILANMAN PUMIPILA SA HARAP NG EXIT. Iisang token bucket kada
   lane ang pinaghahatian ng bawat place at poll; ang add ay kumukuha lamang ng
   token kapag may SOBRA (reserve = 1.0 = presyo ng isang exit place) at hindi
   kailanman naghihintay (max_wait_s = 0.0).
3. ANG ITINALANG LIMIT AY ANG ISINUMITENG LIMIT. Ang key na iyon ang fallback na
   presyo ng fill na nagbu-blend ng posisyon (at ng derived na stop).
4. ANG HINDI MABASANG ORASAN AY "unknown", HINDI GINAWA-GAWANG "regular".
5. ANG MASAMANG PRESYO AY MAY PANGALAN (``alpaca_add_limit_price_invalid``),
   hindi hubad na ValueError na lalamunin ng fail-open na except ng site.
6. ANG CALLER-ASSERTED NA GENERATION STAMP AY MAY SARILING PANGALAN kapag
   naghiwalay ito sa obserbasyon ng certifier.
7. FAIL-CLOSED SA COMPLEMENT: tatlong hatol lang ang tumatawid sa
   ``_governed_place``.
8. ANG RE-PEG AY MAY KAPAREHONG DEPEKTO NG ADD (gfd + extended) AT TAHIMIK ITO.

Runnable: pytest tests/test_add_path_review_fixes.py -v
"""
from __future__ import annotations

import ast
import pathlib
import uuid
from types import SimpleNamespace

import pytest

import app.services.trading.momentum_neural.live_runner as lr
import app.services.trading.momentum_neural.rail_governor as rg
from app.services.trading.momentum_neural.adaptive_risk_request_builder import (
    AdaptiveRiskBuilderError,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AdaptiveRiskContractError,
)

_SRC = pathlib.Path(lr.__file__)
_ADD_ROLES = ("anticipation", "pyramid", "micro", "pullback", "flag")
_ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"


def _sess(**over) -> SimpleNamespace:
    snap = {
        lr.KEY_LIVE_EXEC: {},
        "alpaca_account_scope": "alpaca:paper",
        "alpaca_account_id": _ACCOUNT_ID,
        "alpaca_symbol_claim_token": "claim-token",
    }
    snap.update(over.pop("risk_snapshot_json", None) or {})
    return SimpleNamespace(
        id=19480,
        user_id=7,
        correlation_id="add-path-review",
        execution_family="alpaca_spot",
        symbol="TPET",
        risk_snapshot_json=snap,
        **over,
    )


def _entry_kwargs(**over) -> dict:
    kwargs = {
        "product_id": "TPET",
        "side": "buy",
        "position_intent": "buy_to_open",
        "time_in_force": "day",
        "extended_hours": True,
        "limit_price": "4.20",
        "base_size": "10",
        "client_order_id": "chili_ml_pba_19480_abc123def456",
    }
    kwargs.update(over)
    return kwargs


class _Acquired:
    """Minimal stand-in for ``AcquireResult``."""

    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.waited_s = 0.0
        self.deferred = not acquired
        self.refill_rps = 2.0


def _open_every_seam_before_posture(monkeypatch, *, rail_calls: list | None = None):
    """Buksan ang bawat seam ng ``_governed_place`` HANGGANG sa broker posture.

    Walang HTTP: bawat panlabas na seam ay pinapalitan ng stand-in, kaya ang
    tanging bagay na sinusukat ng test ay ang PAGPILI ng posture contract at ang
    rail-yield contract."""
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: True)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: False)
    monkeypatch.setattr(lr, "captured_paper_selection_required", lambda **k: False)
    monkeypatch.setattr(lr, "_alpaca_execution_quarantine_reason", lambda s: None)
    monkeypatch.setattr(lr, "_alpaca_entries_quarantined", lambda s: False)
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: True)
    monkeypatch.setattr(
        lr,
        "_strict_alpaca_account_identity",
        lambda a, s: (
            True,
            {
                "account_id": _ACCOUNT_ID,
                "equity": 13000.0,
                "buying_power": 26000.0,
            },
        ),
    )
    monkeypatch.setattr(
        lr, "_strict_alpaca_rth_entry_window", lambda a, s: (True, {"ok": True})
    )
    monkeypatch.setattr(
        lr,
        "_final_alpaca_execution_bbo_check",
        lambda *a, **k: (True, {"max_age_seconds": 2.0, "_execution_freshness": None}),
    )

    def _acquire(settings, **kw):
        if rail_calls is not None:
            rail_calls.append(dict(kw))
        return _Acquired(True)

    monkeypatch.setattr(rg, "acquire_rail", _acquire)


# ── 1. ANG POSTURE ANG HULING 100% NA HARANG ────────────────────────────────


@pytest.mark.parametrize("role", _ADD_ROLES)
def test_an_add_is_judged_by_the_owned_posture_not_by_strict_flatness(
    role, monkeypatch
):
    """⚠️ ANG REGRESSION NA INAAYOS. Ang "strictly flat" ay bumabalik ng
    ``alpaca_account_position_exposure_present`` sa BAWAT add (may bukas na
    posisyon ang add sa kahulugan), kaya 100% sarado ang landas kahit certified
    na ang shape. Ang add ay dapat hatulan ng OWNED posture."""
    seen: list[str] = []
    _open_every_seam_before_posture(monkeypatch)
    monkeypatch.setattr(
        lr,
        "_strict_alpaca_empty_entry_posture",
        lambda a, **k: (
            seen.append("empty"),
            (False, {"reason": "alpaca_account_position_exposure_present"}),
        )[1],
    )
    monkeypatch.setattr(
        lr,
        "_strict_alpaca_owned_entry_posture",
        lambda a, s, **k: (
            seen.append("owned"),
            (False, {"reason": "sentinel_owned_posture_ran"}),
        )[1],
    )
    res = lr._governed_place(
        object(),
        lambda **k: None,
        sess=_sess(),
        alpaca_order_role=role,
        alpaca_role_metadata={"entry_extended_session": "premarket"},
        **_entry_kwargs(),
    )
    assert seen == ["owned"], role
    assert res["error"] == "sentinel_owned_posture_ran"
    assert res["broker_account_posture"]["posture_contract"] == "owned_exposure"


@pytest.mark.parametrize("role", ["primary", "repeg", None])
def test_the_first_exposure_still_requires_a_strictly_flat_account(role, monkeypatch):
    """Walang nagbago para sa primary at sa re-peg: ang UNANG exposure ay
    nananatiling "strictly flat" — doon ang manual na posisyon at ang orphan
    order ay dapat pa ring humarang nang buo."""
    seen: list[str] = []
    _open_every_seam_before_posture(monkeypatch)
    monkeypatch.setattr(
        lr,
        "_strict_alpaca_empty_entry_posture",
        lambda a, **k: (
            seen.append("empty"),
            (False, {"reason": "alpaca_account_position_exposure_present"}),
        )[1],
    )
    monkeypatch.setattr(
        lr,
        "_strict_alpaca_owned_entry_posture",
        lambda a, s, **k: (
            seen.append("owned"),
            (False, {"reason": "sentinel_owned_posture_ran"}),
        )[1],
    )
    res = lr._governed_place(
        object(),
        lambda **k: None,
        sess=_sess(),
        alpaca_order_role=role,
        alpaca_role_metadata={"entry_extended_session": "premarket"},
        **_entry_kwargs(),
    )
    assert seen == ["empty"], role
    assert res["error"] == "alpaca_account_position_exposure_present"
    assert res["broker_account_posture"]["posture_contract"] == "strictly_flat"


def test_the_add_role_set_matches_the_setup_family_table():
    """Ang dalawang talahanayan ay dapat hindi kailanman maghiwalay: kung may
    idinagdag na add role na walang setup family (o kabaligtaran), ang isa sa
    dalawang seam ay tahimik na hindi na ito makikilala."""
    assert set(lr._ALPACA_ADD_SETUP_FAMILY) == set(lr._ALPACA_ADD_ORDER_ROLES)
    assert lr._is_alpaca_add_order_role("PULLBACK") is True
    assert lr._is_alpaca_add_order_role(" pyramid ") is True
    for not_an_add in ("primary", "repeg", "", None, "exit"):
        assert lr._is_alpaca_add_order_role(not_an_add) is False


# ── 2. ANG ADD AY NAGBIBIGAY-DAAN SA EXIT SA RAIL ───────────────────────────


@pytest.mark.parametrize("role", _ADD_ROLES)
def test_an_add_never_queues_for_a_rail_token_and_leaves_one_for_the_exit(
    role, monkeypatch
):
    """⚠️ ISANG bucket kada lane ang pinaghahatian ng bawat place AT poll, kaya
    ang token na kinukuha ng add ay ang token na kailangan ng stop-breach exit sa
    parehong tick, at ang 1.5s na bounded wait ay 1.5s na antala sa exit. Ang add
    ay hindi pumipila (0.0s) at nag-iiwan ng eksaktong isang token (1.0)."""
    calls: list[dict] = []
    _open_every_seam_before_posture(monkeypatch, rail_calls=calls)
    monkeypatch.setattr(
        lr, "_strict_alpaca_owned_entry_posture", lambda a, s, **k: (False, {})
    )
    lr._governed_place(
        object(),
        lambda **k: None,
        sess=_sess(),
        alpaca_order_role=role,
        alpaca_role_metadata={"entry_extended_session": "premarket"},
        **_entry_kwargs(),
    )
    assert len(calls) == 1, role
    assert calls[0]["max_wait_s"] == 0.0
    assert calls[0]["reserve_tokens"] == 1.0


def test_the_primary_keeps_the_deployed_bounded_wait(monkeypatch):
    calls: list[dict] = []
    _open_every_seam_before_posture(monkeypatch, rail_calls=calls)
    monkeypatch.setattr(
        lr, "_strict_alpaca_empty_entry_posture", lambda a, **k: (False, {})
    )
    lr._governed_place(
        object(),
        lambda **k: None,
        sess=_sess(),
        alpaca_order_role="primary",
        alpaca_role_metadata={"entry_extended_session": "premarket"},
        **_entry_kwargs(),
    )
    assert len(calls) == 1
    assert "max_wait_s" not in calls[0]
    assert "reserve_tokens" not in calls[0]


def test_a_yielded_add_is_refused_by_its_own_name_not_a_silent_drop(monkeypatch):
    _open_every_seam_before_posture(monkeypatch)
    monkeypatch.setattr(rg, "acquire_rail", lambda settings, **kw: _Acquired(False))
    res = lr._governed_place(
        object(),
        lambda **k: None,
        sess=_sess(),
        alpaca_order_role="pullback",
        alpaca_role_metadata={"entry_extended_session": "premarket"},
        **_entry_kwargs(),
    )
    assert res["ok"] is False
    assert res["error"] == "rail_governor_add_yielded_to_exit"
    assert res["rail_yielded_to_exit"] is True
    assert res["deferred"] is True
    assert res["client_order_id"] == "chili_ml_pba_19480_abc123def456"


def test_the_primary_defer_name_is_unchanged(monkeypatch):
    _open_every_seam_before_posture(monkeypatch)
    monkeypatch.setattr(rg, "acquire_rail", lambda settings, **kw: _Acquired(False))
    res = lr._governed_place(
        object(),
        lambda **k: None,
        sess=_sess(),
        alpaca_order_role="primary",
        alpaca_role_metadata={"entry_extended_session": "premarket"},
        **_entry_kwargs(),
    )
    assert res["error"] == "rail_governor_deferred"
    assert res["rail_yielded_to_exit"] is False


def test_the_bucket_reserve_really_leaves_the_exits_token():
    """Ang mekanismo mismo, sa bucket: kapag isang token na lang ang natitira,
    ang reserved na acquire ay tumatanggi at ang ordinaryong acquire (ang exit)
    ay tumatawid."""
    cfg = rg.GovernorConfig(refill_rps=1e-3, burst=2.0, max_wait_s=0.0)
    bucket = rg._TokenBucket(cfg)
    assert bucket.acquire(max_wait_s=0.0, reserve_tokens=1.0).acquired is True
    # isang token na lang ang natitira: ang add ay HINDI na kukuha nito ...
    assert bucket.acquire(max_wait_s=0.0, reserve_tokens=1.0).acquired is False
    # ... pero ang exit ay kukuha.
    assert bucket.acquire().acquired is True


def test_the_default_acquire_is_byte_identical_to_the_deployed_path():
    cfg = rg.GovernorConfig(refill_rps=1e-3, burst=2.0, max_wait_s=0.0)
    bucket = rg._TokenBucket(cfg)
    assert bucket.acquire().acquired is True
    assert bucket.acquire().acquired is True
    assert bucket.acquire().acquired is False


# ── 3. ANG ITINALANG LIMIT AY ANG ISINUMITENG LIMIT ─────────────────────────


def test_the_recorded_limit_is_the_submitted_string_not_the_guard_ask():
    """⚠️ Ang key na ito ang FALLBACK na presyo ng fill na ipinapakain sa
    ``pyramid_blend_on_fill`` kapag walang ``average_filled_price`` ang broker,
    at ang blend na iyon ang nagsusulat ng ``avg_entry_price`` AT ng derived na
    stop. Ang pagtatala ng guard ask (≈25 bps mas mataas) ay nagbu-blend ng leg
    sa presyong HINDI kailanman isinumite."""
    ask = 4.00
    guard = ask * lr._adaptive_notional_guard_multiplier(expected_move_bps=None)
    assert guard > ask
    canonical = lr.quantize_alpaca_equity_limit_price(ask, "buy")
    assert lr._submitted_limit_px(canonical, guard) == float(canonical)
    assert lr._submitted_limit_px(canonical, guard) != pytest.approx(guard)


@pytest.mark.parametrize("bad", [None, "", "abc", "0", "-1", float("nan")])
def test_an_unreadable_submitted_limit_falls_back_and_never_raises(bad):
    """Ang tawag ay PAGKATAPOS ng matagumpay na place: ang exception dito ay
    mag-iiwan ng naka-post na order na walang commit sa ``le`` (hubad na leg)."""
    assert lr._submitted_limit_px(bad, 4.01) == 4.01
    assert lr._submitted_limit_px(bad, None) is None


def test_every_add_site_records_the_submitted_limit_string():
    """WIRING LINT (AST, hindi substring): ang bawat isa sa limang site ay
    nagtatala ng ``_<prefix>_limit_str``, hindi ng ``_<prefix>_guard_ask``."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "tick_live_session"
    )
    add_keys = {
        "anticipation_add_limit_px",
        "pyramid_limit_px",
        "micropullback_reentry_limit_px",
        "pullback_add_limit_px",
        "flag_breakout_add_limit_px",
    }
    recorded: dict[str, str] = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id == "le"
            and isinstance(target.slice, ast.Constant)
            and str(target.slice.value) in add_keys
        ):
            continue
        recorded[str(target.slice.value)] = ast.unparse(node.value)
    assert set(recorded) == add_keys
    for key, expr in recorded.items():
        assert "_limit_str" in expr, (key, expr)
        assert expr.startswith("_submitted_limit_px("), (key, expr)


# ── 4./5. ANG SHAPE: "unknown" na stamp at may-pangalang masamang presyo ────


def test_an_unreadable_clock_stamps_unknown_not_a_fabricated_regular(monkeypatch):
    """⚠️ Ang stamp ay PAG-AANGKIN tungkol sa naobserbahang session. Kapag hindi
    mabasa ang orasan ay WALANG naobserbahan, kaya ang "regular" ay ebidensyang
    ginawa-gawa — at ang halagang iyon ay dumadaan nang literal sa resibo AT sa
    one-way door. Ang primary ay nagtatala ng "unknown" sa parehong sitwasyon."""

    def _boom(*a, **k):
        raise RuntimeError("clock unreadable")

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now", _boom
    )
    shape = lr._alpaca_add_instruction_shape(
        _sess(), 4.20, execution_family="alpaca_spot"
    )
    assert shape["generation_session"] == "unknown"
    # ang hugis ay nananatiling REGULAR (walang bagong extended order sa katahimikan)
    assert shape["extended_hours"] is False
    assert shape["time_in_force"] == "gfd"
    # at ang stamp na iyon ay hindi kailanman makakapagbukas ng extended carve-out
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: False)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: True)
    assert (
        lr._alpaca_place_instruction_kind(
            _sess(),
            _entry_kwargs(),
            generation_session="unknown",
        )
        == "invalid_entry_generation_session"
    )


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_an_impossible_price_is_named_not_a_bare_value_error(price, monkeypatch):
    """⚠️ Ang pinalitang ``_fmt_limit_price_buy`` ay hindi kailanman nagta-raise
    (ibinabalik nito ang "0", na tinatanggihan nang MAY PANGALAN sa seam). Ang
    hubad na ValueError ay lalamunin ng fail-open na ``except Exception`` ng
    site: walang event, walang resibo. Ngayon ito ay may pangalan."""
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda *a, **k: "premarket",
    )
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        lr._alpaca_add_instruction_shape(
            _sess(), price, execution_family="alpaca_spot"
        )
    assert exc.value.reason == "alpaca_add_limit_price_invalid"
    payload = lr._adaptive_risk_blocker_payload(exc.value)
    assert payload["reason"] == "alpaca_add_limit_price_invalid"
    assert payload["error_type"] == "AdaptiveRiskBuilderError"


def test_the_named_price_blocker_is_caught_by_the_sites_own_except_tuple():
    """Ang bawat site ay humuhuli ng ``(AdaptiveRiskBuilderError, TypeError,
    ValueError)``; ang bagong pangalan ay nasa unang uri, kaya ito ay
    umaabot sa ``live_<role>_add_builder_blocked`` at hindi sa fail-open."""
    assert issubclass(AdaptiveRiskBuilderError, Exception)
    err = AdaptiveRiskBuilderError("alpaca_add_limit_price_invalid", "ValueError")
    assert isinstance(err, AdaptiveRiskBuilderError)


def test_the_shape_call_is_inside_the_named_blocker_try_at_every_site():
    """WIRING LINT (AST): walang tawag sa ``_alpaca_add_instruction_shape`` sa
    LABAS ng isang ``try`` na humuhuli ng ``AdaptiveRiskBuilderError``."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    guarded: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        handled = {
            ast.unparse(h.type) for h in node.handlers if h.type is not None
        }
        if not any("AdaptiveRiskBuilderError" in h for h in handled):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "_alpaca_add_instruction_shape"
            ):
                guarded.append(inner)
    all_calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_alpaca_add_instruction_shape"
    ]
    assert len(all_calls) == 5
    assert len(guarded) == 5


# ── 6./7. ANG CALLER-ASSERTED NA STAMP AT ANG FAIL-CLOSED NA COMPLEMENT ─────


def test_a_session_flip_between_shape_and_place_is_refused_by_its_own_name(
    monkeypatch,
):
    """⚠️ Dito HINDI tautological ang pinto: ang add ay hinuhugis sa afterhours
    pero umabot sa seam matapos magpalit ang session. Dati ay
    ``invalid_entry_extended_hours`` ito — hindi mapaghihiwalay sa maling hugis."""
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: True)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: False)
    assert (
        lr._alpaca_place_instruction_kind(
            _sess(), _entry_kwargs(), generation_session="afterhours"
        )
        == "invalid_entry_generation_session"
    )


def test_a_closed_session_add_is_refused_by_the_generation_door(monkeypatch):
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: False)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: False)
    assert (
        lr._alpaca_place_instruction_kind(
            _sess(), _entry_kwargs(), generation_session="closed"
        )
        == "invalid_entry_generation_session"
    )


def test_the_generation_refusal_has_a_name_at_the_seam(monkeypatch):
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: True)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: False)
    monkeypatch.setattr(lr, "captured_paper_selection_required", lambda **k: False)
    res = lr._governed_place(
        object(),
        lambda **k: None,
        sess=_sess(),
        alpaca_order_role="pullback",
        alpaca_role_metadata={"entry_extended_session": "afterhours"},
        **_entry_kwargs(),
    )
    assert res["ok"] is False
    assert res["error"] == "alpaca_entry_generation_session_not_certified"
    assert res["alpaca_instruction_kind"] == "invalid_entry_generation_session"
    assert res["pre_place_blocked"] is True


def test_the_primary_never_reaches_the_caller_asserted_door(monkeypatch):
    """``generation_session=None`` (ang tanging porma ng primary) ay hindi
    dumadaan sa bagong arm: byte-identical pa rin ang hatol nito."""
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: False)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: True)
    sess = _sess()
    sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]["entry_extended_session"] = "premarket"
    assert (
        lr._alpaca_place_instruction_kind(sess, _entry_kwargs())
        == "invalid_entry_extended_hours"
    )


def test_only_three_instruction_kinds_may_cross_the_seam():
    """⚠️ FAIL-CLOSED SA COMPLEMENT. Ang dating hugis ay nagbabantay sa POSITIBONG
    listahan ng "invalid*", kaya ang BAGONG pangalan ng pagtanggi ay tahimik na
    tatawid bilang hindi-risk-increasing na place."""
    assert lr._ALPACA_CERTIFIED_INSTRUCTION_KINDS == frozenset(
        {"non_alpaca", "close", "entry"}
    )
    for kind, error in lr._ALPACA_INSTRUCTION_REFUSAL_ERRORS.items():
        assert kind not in lr._ALPACA_CERTIFIED_INSTRUCTION_KINDS
        assert error


def test_an_unknown_future_kind_is_refused_rather_than_placed(monkeypatch):
    """Ang bagong hatol na wala pa sa talahanayan ng pangalan ay HINDI tumatawid."""
    monkeypatch.setattr(
        lr, "_alpaca_place_instruction_kind", lambda *a, **k: "some_future_kind"
    )
    placed: list[dict] = []
    res = lr._governed_place(
        object(),
        lambda **k: placed.append(k),
        sess=_sess(),
        alpaca_order_role="pullback",
        **_entry_kwargs(),
    )
    assert placed == []
    assert res["ok"] is False
    assert res["error"] == "alpaca_instruction_side_intent_not_certified"
    assert res["alpaca_instruction_kind"] == "some_future_kind"


# ── 8. ANG RE-PEG: kaparehong depekto, at tahimik ───────────────────────────


def test_the_old_repeg_shape_is_exactly_what_the_certifier_refuses(monkeypatch):
    """⚠️ ANG BINDING VALUE NA ``repeg_gfd_literal_refusals_14d = 0`` AY ARTIFACT.
    Ang re-peg ay nagpapasa ng literal na "gfd" kasabay ng
    ``extended_hours=entry_session_extended`` — mismong pares na pinatunayan ng
    PR na ``invalid_entry_extended_hours`` — at ang branch ng pagkabigo nito ay
    ``break`` na walang ``_emit``, kaya hindi kailanman makikita ang pagtanggi."""
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: True)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: False)
    assert (
        lr._alpaca_place_instruction_kind(
            _sess(), _entry_kwargs(time_in_force="gfd", extended_hours=True)
        )
        == "invalid_entry_extended_hours"
    )


def test_no_place_call_pairs_a_gfd_literal_with_a_non_false_extended_hours():
    """WIRING LINT (AST, hindi substring): walang natitirang tawag sa
    ``_governed_place`` na nagpapasa ng literal na ``time_in_force="gfd"`` habang
    ang ``extended_hours`` ay HINDI literal na ``False`` — ang eksaktong pares na
    tinatanggihan ng certifier. Sakop nito ang limang add AT ang re-peg."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_governed_place"
        ):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        tif = kw.get("time_in_force")
        ext = kw.get("extended_hours")
        if not (isinstance(tif, ast.Constant) and tif.value == "gfd"):
            continue
        if isinstance(ext, ast.Constant) and ext.value is False:
            continue
        offenders.append(ast.unparse(node)[:160])
    assert offenders == []


def test_the_repeg_uses_the_primary_tif_idiom():
    """WIRING LINT (AST): ang ``time_in_force`` ng re-peg ay kondisyonal sa
    ``entry_session_extended`` + Alpaca family, tulad ng ``_entry_kwargs``."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    repeg = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_governed_place"
        and any(
            k.arg == "alpaca_order_role"
            and isinstance(k.value, ast.Constant)
            and k.value.value == "repeg"
            for k in n.keywords
        )
    ]
    assert len(repeg) == 1
    tif = next(k.value for k in repeg[0].keywords if k.arg == "time_in_force")
    rendered = ast.unparse(tif)
    assert isinstance(tif, ast.IfExp)
    assert "entry_session_extended" in rendered
    assert "ALPACA_EXECUTION_FAMILIES" in rendered
    assert "'day'" in rendered and "'gfd'" in rendered


def test_the_repeg_failure_branch_now_emits_a_receipt():
    """WIRING LINT (AST): ang ``break`` ng re-peg ay may ``_emit`` bago ito —
    dati ay tahimik itong nagpapabaya sa kanseladong entry."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body = node.body
        if not body or not isinstance(body[-1], ast.Break):
            continue
        emitted = [
            ast.unparse(n.value.args[2])
            for n in body
            if isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id == "_emit"
            and len(n.value.args) >= 3
        ]
        if "'entry_repeg_place_blocked'" in emitted:
            found = True
    assert found, "ang re-peg na tinanggihan ay dapat may resibo bago ang break"


# ── ANG KOMENTO NA TUMUTURO SA TUNAY NA MAMBABASA ──────────────────────────
# (ang behavioural na patunay ng premise ay nasa
#  tests/test_add_path_adaptive_builder.py::
#  test_the_exit_transport_really_does_bind_the_single_lifecycle_slot)


def test_the_dead_lifecycle_states_are_exactly_the_ones_the_reader_rejects():
    """Ang set ay tama; ito ang tumuturo sa TUNAY na mambabasa (ang komento ay
    dating tumuturo sa pangalang hindi umiiral kahit saan sa ``app/``)."""
    src = _SRC.read_text(encoding="utf-8")
    assert lr._ALPACA_ADD_DEAD_LIFECYCLE_STATES == frozenset({"released", "closed"})
    assert "_captured_paper_exit_owner_transport_binding" not in src
    assert hasattr(lr, "_captured_paper_exit_binding_for_lease")
    assert hasattr(lr, "_lease_owner_transport_for_runtime")
