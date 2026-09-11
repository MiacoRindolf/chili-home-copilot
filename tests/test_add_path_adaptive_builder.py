"""ADD PATH — ang SARILING CID-bound packet ng add (planner item [30]).

Ang choke point sa ``_governed_place`` ay tahasan: "Until an add path builds its
own fresh CID-bound packet against the aggregate 3-D ledger, it is explicitly
unavailable; it may never inherit primary economics or fall back to legacy
sizing."  Ito ang packet na iyon.

DALAWANG KATOTOHANAN NA SINUKAT (2026-09-11, live `chili`):

* ang aggregate 3-D ledger ay MAYROON NA -- ``resolve_adaptive_risk`` ay
  ibinabawas ang ``existing_same_symbol_structural_risk_usd`` at
  ``pending_same_symbol_structural_risk_usd`` sa ``symbol_remaining``, kaya
  awtomatikong nakikita ng add ang risk ng bukas na leg; at
* WALANG uniqueness na humaharang sa pangalawang reservation --
  ``adaptive_risk_reservations`` = 17 hilera (09-04..09-10) na LAHAT
  ``opportunity_claim_id IS NULL``, kasama ang 3 hilera kada simbolo-araw para sa
  SLE 2026-09-08 at SUNE 2026-09-09, habang ang
  ``adaptive_risk_opportunity_claims`` ay 0 hilera magpakailanman.

ANG NATITIRANG HARANG (napatunayan sa code, hindi sa plano): ang lifecycle
projection sa ``le`` ay ISANG slot, at ang captured-PAPER EXIT transport ay
nakatali dito.  Ang pag-commit ng reservation ng add habang buhay ang slot ng
primary ay mag-aalis ng exit authority sa bukas na leg.  Fail-CLOSED na may
PANGALAN -- iyon ang binabantayan dito.

Runnable: pytest tests/test_add_path_adaptive_builder.py -v
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

import app.services.trading.momentum_neural.live_runner as lr
from app.services.trading.momentum_neural.adaptive_risk_request_builder import (
    AdaptiveRiskBuilderError,
    adaptive_risk_source_provider,
)

_SRC = pathlib.Path(lr.__file__)

_ADD_SITE_PREFIX = {
    "anticipation": "_ant",
    "pyramid": "_pyr",
    "micro": "_mpr",
    "pullback": "_pba",
    "flag": "_fba",
}


def _sess() -> SimpleNamespace:
    return SimpleNamespace(
        id=19480,
        correlation_id="add-path-packet",
        execution_family="alpaca_spot",
        symbol="TPET",
        risk_snapshot_json={lr.KEY_LIVE_EXEC: {}},
    )


def _build(le=None, **over):
    kwargs = dict(
        role="pullback",
        execution_family="alpaca_spot",
        bid=4.10,
        ask=4.20,
        structural_stop=3.95,
        client_order_id="chili_ml_pba_19480_abc123def456",
    )
    kwargs.update(over)
    return lr._build_adaptive_alpaca_add_before_legacy_sizing(
        _sess(), {} if le is None else le, **kwargs
    )


# ── Ang dalawang maagang pagbalik (walang pagbabago sa gawi) ─────────────────


@pytest.mark.parametrize("family", ["robinhood_spot", "coinbase_spot", None, ""])
def test_a_non_alpaca_add_is_untouched(family):
    assert _build(execution_family=family) == (None, None)


def test_the_running_lane_still_crosses_on_the_timeshare_escape(monkeypatch):
    """⚠️ ANG INVARIANT NG LANE NGAYONG GABI. Habang aktibo ang escape, ang add
    ay dumadaan pa rin sa legacy sizing + legacy claim (na may restored per-trade
    hard cap) -- WALANG bagong packet, WALANG bagong reservation, walang
    pagbabago sa gawi maliban sa hugis-ayos ng instruction."""
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: True)
    assert _build() == (None, None)


def test_a_short_session_is_refused_by_name(monkeypatch):
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: False)
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        _build(le={"side_long": False})
    assert exc.value.reason == "adaptive_risk_long_alpaca_spot_only"


# ── Walang tahimik na pagdaan ────────────────────────────────────────────────


def test_without_a_capture_provider_the_add_is_refused_by_name(monkeypatch):
    """Ito mismo ang pangalan ng choke point na binabanggit ng planner row --
    at ito ang MAAYOS na sagot kapag walang capture binding: pagtanggi, hindi
    legacy fallback at hindi pagmamana ng packet ng primary."""
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: False)
    with adaptive_risk_source_provider(None):
        with pytest.raises(AdaptiveRiskBuilderError) as exc:
            _build()
    assert exc.value.reason == "builder_missing_capture_binding"


def test_an_empty_client_order_id_is_refused_by_name(monkeypatch):
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: False)
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        _build(client_order_id="   ")
    assert exc.value.reason == "adaptive_risk_builder_boundary_mismatch"
    assert exc.value.detail == "client_order_id_missing"


@pytest.mark.parametrize("stop", [None, "", 0.0, -1.0, 4.20, 9.99, float("nan")])
def test_a_missing_or_impossible_structural_stop_is_refused_by_name(stop, monkeypatch):
    """Ang stop ng ADD ang pinipresyo, hindi ang kay primary -- at kailangan
    nitong nasa ILALIM ng executable ask."""
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: False)
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        _build(structural_stop=stop)
    assert exc.value.reason == "adaptive_risk_structural_stop_missing"


def test_an_unreadable_bbo_is_refused_by_name_not_by_TypeError(monkeypatch):
    """Ang float() ay nasa LOOB ng family gate, kaya ang None na bid ay pangalan
    ng blocker -- hindi isang hubad na TypeError na lalamunin ng fail-open na
    `except Exception` ng site."""
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: False)
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        _build(bid=None)
    assert exc.value.reason == "adaptive_risk_builder_boundary_mismatch"
    assert exc.value.detail == "executable_bbo_unreadable"


# ── ANG CID-BOUND NA BOUNDARY ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "role,setup_family",
    [
        ("anticipation", "anticipation_remainder_add"),
        ("pyramid", "pyramid_continuation_add"),
        ("micro", "micro_pullback_reload_add"),
        ("pullback", "pullback_support_add"),
        ("flag", "flag_breakout_add"),
    ],
)
def test_the_add_asks_the_capture_source_for_its_OWN_cid_and_family(
    role, setup_family, monkeypatch
):
    """⚠️ ANG PUSO NG ITEM [30]. Ang boundary na hinihingi sa capture source ay
    may ``decision_id`` na EKSAKTONG CID ng add (sariwa kada place_n) at
    ``setup_family`` na pangalan ng add role -- kaya ang reservation ng add ay
    hiwalay na hilera at hindi kailanman alias ng packet ng primary.
    ``build_adaptive_risk_request`` mismo ang nagpapatupad ng cid ==
    decision_id para sa alpaca_paper."""
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: False)
    seen: list[dict] = []

    def provider(**boundary):
        seen.append(dict(boundary))
        return None  # => builder_missing_capture_binding, walang tahimik na pagdaan

    cid = f"chili_ml_{role[:3]}_19480_deadbeef0001"
    with adaptive_risk_source_provider(provider):
        with pytest.raises(AdaptiveRiskBuilderError):
            _build(role=role, client_order_id=cid)

    assert len(seen) == 1
    assert seen[0]["decision_id"] == cid
    assert seen[0]["setup_family"] == setup_family
    assert seen[0]["execution_surface"] == "alpaca_paper"
    assert seen[0]["execution_family"] == "alpaca_spot"
    assert seen[0]["venue"] == "alpaca"
    assert seen[0]["broker_environment"] == "paper"
    assert seen[0]["symbol"] == "TPET"


def test_two_successive_places_ask_for_two_different_decision_ids(monkeypatch):
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: False)
    seen: list[str] = []

    def provider(**boundary):
        seen.append(boundary["decision_id"])
        return None

    with adaptive_risk_source_provider(provider):
        for cid in ("chili_ml_pba_19480_aaa1", "chili_ml_pba_19480_bbb2"):
            with pytest.raises(AdaptiveRiskBuilderError):
                _build(client_order_id=cid)
    assert seen == ["chili_ml_pba_19480_aaa1", "chili_ml_pba_19480_bbb2"]
    assert len(set(seen)) == 2


def test_an_unknown_role_still_gets_its_own_named_family(monkeypatch):
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: False)
    seen: list[dict] = []
    with adaptive_risk_source_provider(lambda **b: seen.append(dict(b))):
        with pytest.raises(AdaptiveRiskBuilderError):
            _build(role="orphan_adopt")
    assert seen[0]["setup_family"] == "orphan_adopt_add"


# ── ANG LIFECYCLE SLOT (deviation mula sa unang plano, napatunayan sa code) ──


def _le_with_primary_binding(*, state: str, cid: str = "chili_ml_19480_primary"):
    return {lr.KEY_ADAPTIVE_ALPACA_LIFECYCLE: {"client_order_id": cid, "state": state}}


@pytest.mark.parametrize(
    "state",
    ["reserved", "submitted", "submit_indeterminate", "filled", "exposure_quarantined"],
)
def test_a_live_primary_reservation_blocks_the_add_by_name(state, monkeypatch):
    """⚠️ ANG DEVIATION. Iisa lang ang lifecycle slot sa ``le``, at ang
    captured-PAPER EXIT transport ay binabasa ito (reservation_id +
    request_sha256) bago payagan ang exit POST. Kapag inangkin ito ng add, ang
    bukas na leg ay mawawalan ng exit authority AT walang magpapalaya sa
    naka-reserve na risk. Fail-CLOSED na MAY PANGALAN -- at BAGO pa tawagin ang
    capture provider."""
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: False)
    calls: list[dict] = []
    with adaptive_risk_source_provider(lambda **b: calls.append(dict(b))):
        with pytest.raises(AdaptiveRiskBuilderError) as exc:
            _build(le=_le_with_primary_binding(state=state))
    assert exc.value.reason == "adaptive_risk_add_lifecycle_slot_occupied"
    assert exc.value.detail == state
    assert calls == [], "walang capture lease ang dapat nakonsumo"


@pytest.mark.parametrize("state", ["released", "closed"])
def test_a_dead_binding_does_not_block_the_add(state, monkeypatch):
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: False)
    with adaptive_risk_source_provider(lambda **b: None):
        with pytest.raises(AdaptiveRiskBuilderError) as exc:
            _build(le=_le_with_primary_binding(state=state))
    assert exc.value.reason == "builder_missing_capture_binding"


def test_the_adds_own_binding_does_not_block_itself(monkeypatch):
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: False)
    cid = "chili_ml_pba_19480_abc123def456"
    with adaptive_risk_source_provider(lambda **b: None):
        with pytest.raises(AdaptiveRiskBuilderError) as exc:
            _build(le=_le_with_primary_binding(state="reserved", cid=cid))
    assert exc.value.reason == "builder_missing_capture_binding"


def test_the_exit_transport_really_does_bind_the_single_lifecycle_slot():
    """Ang ebidensya sa likod ng deviation sa itaas: ang exit owner transport ay
    nagta-``raise`` kapag wala ang slot, at hinahatulan ang ``request_sha256``
    nito -- kaya ang pag-overwrite ay hindi lang maingay, ito ay pag-alis ng
    kakayahang lumabas."""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("def _captured_paper_exit_binding_for_lease")
    region = src[i : i + 2500]
    assert "KEY_ADAPTIVE_ALPACA_LIFECYCLE" in region
    assert "captured PAPER exit lacks adaptive reservation authority" in region
    assert "request_sha256" in region


# ── Ang triple na isinasaksak sa role metadata ───────────────────────────────


def test_no_build_means_no_role_metadata_change():
    assert lr._alpaca_add_adaptive_role_metadata(None) == {}


def test_a_build_hands_governed_place_the_exact_triple_it_checks():
    built = SimpleNamespace(
        decision_packet={"packet": "p"},
        reservation_claim=SimpleNamespace(to_payload=lambda: {"claim": "c"}),
        request=SimpleNamespace(to_payload=lambda: {"request": "r"}),
        source_sha256="sha",
        audit_payload=lambda: {"audit": "a"},
    )
    meta = lr._alpaca_add_adaptive_role_metadata(built)
    assert meta["adaptive_risk_decision_packet"] == {"packet": "p"}
    assert meta["adaptive_risk_reservation_claim"] == {"claim": "c"}
    assert meta[lr.KEY_ADAPTIVE_RISK_RESERVATION_REQUEST] == {"request": "r"}
    # ang EKSAKTONG tatlong key na binibilang ng `_adaptive_risk_pair`
    fn = next(
        n
        for n in ast.walk(ast.parse(_SRC.read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == "_governed_place"
    )
    pair_src = ast.unparse(fn)
    i = pair_src.index("_adaptive_risk_pair = ")
    region = pair_src[i : i + 600]
    for key in (
        "adaptive_risk_decision_packet",
        "adaptive_risk_reservation_claim",
        "KEY_ADAPTIVE_RISK_RESERVATION_REQUEST",
    ):
        assert key in region


# ── Ang limang site: receipt na may pangalan, walang tahimik na pagdaan ──────


def _tick_src() -> str:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "tick_live_session"
    )
    return ast.unparse(fn)


@pytest.mark.parametrize(
    "role,event",
    [
        ("anticipation", "live_anticipation_add_builder_blocked"),
        ("pyramid", "live_pyramid_add_builder_blocked"),
        ("micro", "live_micro_pullback_add_builder_blocked"),
        ("pullback", "live_pullback_add_builder_blocked"),
        ("flag", "live_flag_breakout_add_builder_blocked"),
    ],
)
def test_every_add_site_builds_its_own_packet_and_names_its_blocker(role, event):
    src = _tick_src()
    prefix = _ADD_SITE_PREFIX[role]
    assert f"_build_adaptive_alpaca_add_before_legacy_sizing(" in src
    assert f"role='{role}'" in src
    assert f"client_order_id={prefix}_cid" in src
    assert f"{prefix}_blocked = _adaptive_risk_blocker_payload(" in src
    assert event in src
    # ang triple ay isinasaksak sa role metadata ng SITE mismo
    assert f"**_alpaca_add_adaptive_role_metadata({prefix}_built)" in src
    # at kapag may build, ang canonical na limit ng packet ang ipinapadala
    assert f"{prefix}_limit_str = {prefix}_canon" in src


def test_a_blocked_add_never_reaches_the_broker_and_never_falls_back():
    """Walang legacy fallback at walang pagmamana ng economics ng primary: ang
    ``_governed_place`` ay nasa ``else`` arm ng bawat blocker check."""
    src = _tick_src()
    for prefix in _ADD_SITE_PREFIX.values():
        i = src.index(f"if {prefix}_blocked is not None:")
        region = src[i : i + 1800]
        head, _, tail = region.partition("else:")
        assert "_governed_place(" not in head, prefix
        assert "_governed_place(" in tail, prefix
        assert "_emit(db, sess," in head, prefix


def test_the_blocker_payload_carries_reason_type_and_detail():
    payload = lr._adaptive_risk_blocker_payload(
        AdaptiveRiskBuilderError("adaptive_risk_add_lifecycle_slot_occupied", "reserved")
    )
    assert payload == {
        "reason": "adaptive_risk_add_lifecycle_slot_occupied",
        "error_type": "AdaptiveRiskBuilderError",
        "detail": "reserved",
    }


def test_the_ledger_already_nets_the_open_leg_for_the_add():
    """Walang bagong ledger code sa PR na ito -- at ito ang dahilan."""
    policy = pathlib.Path(
        lr.__file__
    ).parent / "adaptive_risk_policy.py"
    src = policy.read_text(encoding="utf-8")
    i = src.index("symbol_remaining = max(")
    region = src[i : i + 400]
    assert "existing_same_symbol_structural_risk_usd" in region
    assert "pending_same_symbol_structural_risk_usd" in region
