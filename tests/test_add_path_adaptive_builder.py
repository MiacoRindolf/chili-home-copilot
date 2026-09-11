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
import uuid
from types import SimpleNamespace

import pytest

import app.services.trading.momentum_neural.live_runner as lr
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    resolve_adaptive_risk,
)
from app.services.trading.momentum_neural.adaptive_risk_request_builder import (
    AdaptiveRiskBuilderError,
    adaptive_risk_source_provider,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AdaptiveRiskContractError,
)

_ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"

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
        user_id=7,
        correlation_id="add-path-packet",
        execution_family="alpaca_spot",
        symbol="TPET",
        risk_snapshot_json={
            lr.KEY_LIVE_EXEC: {},
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": _ACCOUNT_ID,
            "alpaca_symbol_claim_token": "claim-token",
        },
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
    """Ang ebidensya sa likod ng deviation sa itaas, IPINAPATAKBO.

    REVIEW ROUND: ang dating porma nito ay naghahanap ng tatlong substring sa
    isang 2500-byte na bintana ng pinagmulan -- wala itong pinatutunayan tungkol
    sa GAWI ng exit transport, gayong ito ang TANGING katwiran ng fail-closed na
    ``adaptive_risk_add_lifecycle_slot_occupied``. Ngayon ay tinatawag na ang
    mambabasa mismo: alisin ang iisang lifecycle slot at ang captured-PAPER exit
    ay nagta-``raise`` -- kaya ang pag-angkin ng add doon ay pag-alis ng
    kakayahang lumabas ng bukas na leg, hindi ingay lamang."""
    sess = SimpleNamespace(
        id=19480,
        correlation_id="add-path-packet",
        execution_family="alpaca_spot",
        symbol="TPET",
        risk_snapshot_json={
            lr.KEY_LIVE_EXEC: {},
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": _ACCOUNT_ID,
            "alpaca_symbol_claim_token": "claim-token",
        },
    )
    with lr.captured_paper_exit_runtime_authority(
        owner_generation=1,
        expected_account_id=_ACCOUNT_ID,
        runtime_generation=str(uuid.uuid4()),
        broker_connection_generation="gen-1",
    ):
        # may reservation request pero WALANG lifecycle slot => walang awtoridad
        with pytest.raises(AdaptiveRiskContractError) as exc:
            lr._captured_paper_exit_binding_for_lease(
                sess,
                {lr.KEY_ADAPTIVE_RISK_RESERVATION_REQUEST: {"x": 1}},
                transport_kind="exit",
                client_order_id="chili_ml_exit_19480",
                order_request={},
                lease_token="lease-1",
            )
        assert "lacks adaptive reservation authority" in str(exc.value)
        # at ganoon din kapag may slot pero walang request payload
        with pytest.raises(AdaptiveRiskContractError):
            lr._captured_paper_exit_binding_for_lease(
                sess,
                {lr.KEY_ADAPTIVE_ALPACA_LIFECYCLE: {"state": "filled"}},
                transport_kind="exit",
                client_order_id="chili_ml_exit_19480",
                order_request={},
                lease_token="lease-1",
            )


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


@pytest.mark.parametrize(
    "dropped",
    [
        None,
        "adaptive_risk_decision_packet",
        "adaptive_risk_reservation_claim",
        "request",
    ],
)
def test_the_triple_is_what_governed_place_actually_counts(dropped, monkeypatch):
    """REVIEW ROUND: sinusukat na ito bilang GAWI sa halip na basahin ang isang
    600-byte na bintana sa paligid ng ``_adaptive_risk_pair =``.

    Ang buong triple ay nagbubukas ng ADAPTIVE na landas (walang
    ``builder_missing_capture_binding``); ang KULANG na triple ay hindi -- at
    dahil OFF ang time-share escape dito, ang pagtanggi ay eksaktong pangalan ng
    choke point."""
    monkeypatch.setattr(lr, "_alpaca_session_is_premarket_now", lambda s: True)
    monkeypatch.setattr(lr, "_alpaca_session_is_afterhours_now", lambda s: False)
    monkeypatch.setattr(lr, "captured_paper_selection_required", lambda **k: False)
    monkeypatch.setattr(lr, "_alpaca_execution_quarantine_reason", lambda s: None)
    monkeypatch.setattr(lr, "_alpaca_entries_quarantined", lambda s: False)
    monkeypatch.setattr(lr, "_legacy_alpaca_timeshare_escape", lambda s: False)
    built = SimpleNamespace(
        decision_packet={"packet": "p"},
        reservation_claim=SimpleNamespace(to_payload=lambda: {"claim": "c"}),
        request=SimpleNamespace(to_payload=lambda: {"request": "r"}),
        source_sha256="sha",
        audit_payload=lambda: {"audit": "a"},
    )
    meta = dict(lr._alpaca_add_adaptive_role_metadata(built))
    meta["entry_extended_session"] = "premarket"
    if dropped == "request":
        meta.pop(lr.KEY_ADAPTIVE_RISK_RESERVATION_REQUEST)
    elif dropped is not None:
        meta.pop(dropped)
    res = lr._governed_place(
        object(),
        lambda **k: None,
        sess=_sess(),
        alpaca_order_role="pullback",
        alpaca_role_metadata=meta,
        product_id="TPET",
        side="buy",
        position_intent="buy_to_open",
        time_in_force="day",
        extended_hours=True,
        limit_price="4.20",
        base_size="10",
        client_order_id="chili_ml_pba_19480_abc123def456",
    )
    if dropped is None:
        assert res.get("error") != "builder_missing_capture_binding"
    else:
        assert res["error"] == "builder_missing_capture_binding"
        assert res["adaptive_risk_path"] == "exposure_increase_pair_required"


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
    """WIRING LINT (AST node, hindi substring ng pinagmulan). REVIEW ROUND: ang
    dating porma ay `f"role='{role}'" in src`, na pumupasa kahit ibang site ang
    naglalaman ng teksto. Ngayon ay ang mismong tawag ang hinahatulan."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    prefix = _ADD_SITE_PREFIX[role]
    builds = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_build_adaptive_alpaca_add_before_legacy_sizing"
        and any(
            k.arg == "role"
            and isinstance(k.value, ast.Constant)
            and k.value.value == role
            for k in n.keywords
        )
    ]
    assert len(builds) == 1, role
    kw = {k.arg: k.value for k in builds[0].keywords if k.arg}
    assert ast.unparse(kw["client_order_id"]) == f"{prefix}_cid"
    src = _tick_src()
    assert f"{prefix}_blocked = _adaptive_risk_blocker_payload(" in src
    assert event in src
    # ang triple ay isinasaksak sa role metadata ng SITE mismo
    assert f"**_alpaca_add_adaptive_role_metadata({prefix}_built)" in src
    # at kapag may build, ang canonical na limit ng packet ang ipinapadala
    assert f"{prefix}_limit_str = {prefix}_canon" in src


def test_a_blocked_add_never_reaches_the_broker_and_never_falls_back():
    """Walang legacy fallback at walang pagmamana ng economics ng primary: ang
    ``_governed_place`` ay nasa ``else`` arm ng bawat blocker check.

    REVIEW ROUND: AST node, hindi ``src[i:i+1800]`` na bintana na bumabagsak sa
    bawat pagdagdag ng komento."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    guards: dict[str, ast.If] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)):
            continue
        rendered = ast.unparse(node.test)
        for prefix in _ADD_SITE_PREFIX.values():
            if rendered == f"{prefix}_blocked is not None":
                guards[prefix] = node
    assert set(guards) == set(_ADD_SITE_PREFIX.values())

    def _calls(nodes, name):
        return [
            n
            for body in nodes
            for n in ast.walk(body)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == name
        ]

    for prefix, node in guards.items():
        assert _calls(node.body, "_governed_place") == [], prefix
        assert _calls(node.body, "_emit"), prefix
        assert _calls(node.orelse, "_governed_place"), prefix


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
    """Walang bagong ledger code sa PR na ito -- at ito ang dahilan.

    REVIEW ROUND: PINAPATAKBO na ang resolver sa halip na basahin ang teksto ng
    ``adaptive_risk_policy.py`` (dating ``src[i:i+400]``). Differential: ISANG
    tuloy-tuloy na baseline na may BUKAS na leg, at ang tanging binabago ay ang
    same-symbol na hilera -- ang ``symbol_remaining`` ay bumababa nang EKSAKTO
    kasing-laki niyon habang walang ibang cap ang gumagalaw. Iyon ang aggregate
    3-D ledger na awtomatikong nakikita ng add.
    """
    import importlib.util
    from dataclasses import replace as _replace

    # Ang `tests/` ay hindi package; i-load ang kapitbahay na fixture module sa
    # pamamagitan ng landas nito para hindi na kopyahin ang 30-field na inputs.
    _spec = importlib.util.spec_from_file_location(
        "_add_path_policy_fixture",
        pathlib.Path(__file__).with_name("test_adaptive_risk_policy.py"),
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _inputs, _policy = _mod._inputs, _mod._policy

    policy = _policy()
    open_leg = 2_000.0
    base = _replace(
        _inputs(surface="alpaca_paper"),
        # isang KUMPLETO at magkakatugmang bukas na reservation (tatlong dimensyon)
        open_structural_risk_usd=open_leg,
        portfolio_gross_notional_usd=open_leg,
        open_buying_power_impact_usd=open_leg,
        current_cluster_structural_risk_usd=open_leg,
        policy_buying_power_capacity_usd=(
            float(_inputs().buying_power_usd) + open_leg
        ),
    )
    flat = resolve_adaptive_risk(policy, base)
    assert flat.valid is True, flat.rejection_reasons
    symbol_cap = float(base.equity_usd) * policy.symbol_risk_fraction_of_equity
    assert (
        flat.risk_budget_caps_usd["symbol_remaining_after_existing_and_pending"]
        == symbol_cap
    )

    same_symbol = 750.0
    for field in (
        "existing_same_symbol_structural_risk_usd",
        "pending_same_symbol_structural_risk_usd",
    ):
        kw = {field: same_symbol}
        if field.startswith("pending"):
            # ang nakabinbing hilera ay may sariling kumpletong tatlong dimensyon
            kw.update(
                pending_reserved_risk_usd=same_symbol,
                pending_correlation_cluster_risk_usd=same_symbol,
                pending_portfolio_gross_notional_usd=same_symbol,
                pending_buying_power_impact_usd=same_symbol,
            )
        netted = resolve_adaptive_risk(policy, _replace(base, **kw))
        assert netted.valid is True, (field, netted.rejection_reasons)
        assert (
            netted.risk_budget_caps_usd[
                "symbol_remaining_after_existing_and_pending"
            ]
            == symbol_cap - same_symbol
        ), field
        assert (
            netted.risk_budget_caps_usd["correlation_cluster_remaining"]
            <= flat.risk_budget_caps_usd["correlation_cluster_remaining"]
        ), field

    # at kapag naubos na ng bukas na leg ang BUONG symbol budget, walang bagong
    # exposure -- ang add ay hindi kailanman makakalusot sa ledger.
    exhausted = resolve_adaptive_risk(
        policy,
        _replace(
            base,
            existing_same_symbol_structural_risk_usd=symbol_cap,
            open_structural_risk_usd=symbol_cap,
            portfolio_gross_notional_usd=symbol_cap,
            open_buying_power_impact_usd=symbol_cap,
            current_cluster_structural_risk_usd=symbol_cap,
            policy_buying_power_capacity_usd=(
                float(_inputs().buying_power_usd) + symbol_cap
            ),
        ),
    )
    assert exhausted.quantity_shares == 0
    assert "risk_budget_exhausted" in exhausted.rejection_reasons
