from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import inspect
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import captured_paper_selection as selection
from app.services.trading.momentum_neural.captured_paper_dispatcher import (
    CapturedPaperDispatchRequest,
)
from app.services.trading.momentum_neural.captured_paper_entry_intent import (
    CapturedPaperConfirmedArmGeneration,
    CapturedPaperOpportunityKey,
)


UTC = timezone.utc
ACCOUNT_ID = "c349bc0f-a5e2-48f8-ad5e-ec5d531414ac"
RUNTIME_GENERATION = "66cdcb77-684f-4466-917a-547090575793"
ARM_TOKEN = "cfa87890-43a1-4dc4-82a6-cc935b008e92"
DECISION_AT = datetime(2026, 7, 15, 14, 31, 12, tzinfo=UTC)
VIABILITY_UPDATED_AT = DECISION_AT - timedelta(seconds=1)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _dispatch(*, symbol: str = "ACTU") -> CapturedPaperDispatchRequest:
    return CapturedPaperDispatchRequest(
        session_id=77,
        symbol=symbol,
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        expected_account_id=ACCOUNT_ID,
        code_build_sha256=_digest("build"),
        config_sha256=_digest("config"),
        capture_receipt_sha256=_digest("runtime-capture"),
        runtime_generation=RUNTIME_GENERATION,
        first_dip_policy_mode="candidate",
    )


def _arm() -> CapturedPaperConfirmedArmGeneration:
    return CapturedPaperConfirmedArmGeneration(
        session_id=77,
        arm_token=ARM_TOKEN,
        expires_at=DECISION_AT + timedelta(minutes=2),
        symbol_claim_token=f"arm-{ARM_TOKEN}",
        account_scope="alpaca:paper",
        expected_account_id=ACCOUNT_ID,
        confirmed_at=DECISION_AT - timedelta(seconds=15),
    )


def _arm_marker(*, arm: CapturedPaperConfirmedArmGeneration | None = None):
    arm = arm or _arm()
    return {
        "version": 1,
        "session_id": arm.session_id,
        "arm_token": arm.arm_token,
        # The existing durable arm uses naive UTC strings.  The selection
        # context binds those exact bytes while proving their UTC instants.
        "expires_at_utc": arm.expires_at.replace(tzinfo=None).isoformat(),
        "alpaca_symbol_claim_token": arm.symbol_claim_token,
        "alpaca_account_scope": arm.account_scope,
        "alpaca_account_id": arm.expected_account_id,
        "non_alpaca_account_identity": "",
        "confirmed_at_utc": arm.confirmed_at.replace(tzinfo=None).isoformat(),
    }


def _debug(*, setup_family: str):
    if setup_family == "first_dip_reclaim":
        return {
            "front_side_via": "first_dip_day_leg",
            "first_dip_tape_confirmed": True,
            "first_dip_tape_run_bound": True,
            "first_dip_tape_decision_receipt_binding_sha256": _digest(
                "setup-evidence"
            ),
            "opportunity_key": {
                "symbol": "ACTU",
                "trading_date": "2026-07-15",
                "setup_family": "first_dip_reclaim",
            },
            "pullback_low": 2.8,
        }
    return {"pullback_low": 2.8, "pullback_high": 3.0, "bar": 19}


def _context(
    *,
    setup_family: str = "momentum_pullback",
    evidence_expires_at: datetime | None = None,
    entry_place_count: int = 3,
):
    dispatch = _dispatch()
    arm = _arm()
    debug = _debug(setup_family=setup_family)
    marker = _arm_marker(arm=arm)
    cid = "chili_ml_e_77_captured_35df88b6c1"
    candidate_generation_sha256 = (
        selection.captured_paper_candidate_generation_sha256(
            session_id=dispatch.session_id,
            symbol=dispatch.symbol,
            execution_family=dispatch.execution_family,
            entry_place_count=entry_place_count,
            client_order_id=cid,
            setup_family=setup_family,
            structural_stop_price="2.80",
            trigger_reason="pullback_break",
            trigger_debug=debug,
            confirmed_arm_marker=marker,
            viability_updated_at=VIABILITY_UPDATED_AT,
            viability_score="0.8",
            viability_payload_sha256=_digest("viability-payload"),
            execution_readiness_sha256=_digest("execution-readiness"),
        )
    )
    opportunity = (
        CapturedPaperOpportunityKey(
            account_scope="alpaca:paper",
            symbol="ACTU",
            trading_date=DECISION_AT.date(),
            setup_family="first_dip_reclaim",
        )
        if setup_family == "first_dip_reclaim"
        else None
    )
    return selection.CapturedPaperSelectionContext.create(
        dispatch_request=dispatch,
        confirmed_arm_generation=arm,
        confirmed_arm_marker=marker,
        entry_place_count=entry_place_count,
        client_order_id=cid,
        setup_family=setup_family,
        decision_at=DECISION_AT,
        evidence_available_at=DECISION_AT - timedelta(milliseconds=20),
        evidence_expires_at=(
            evidence_expires_at
            if evidence_expires_at is not None
            else DECISION_AT + timedelta(milliseconds=250)
        ),
        bid="2.99",
        ask="3.00",
        structural_stop_price="2.80",
        entry_limit_ceiling_price="3.00",
        trigger_reason="pullback_break",
        trigger_debug=debug,
        candidate_generation_sha256=candidate_generation_sha256,
        viability_updated_at=VIABILITY_UPDATED_AT,
        viability_score="0.8",
        viability_payload_sha256=_digest("viability-payload"),
        execution_readiness_sha256=_digest("execution-readiness"),
        account_receipt_sha256=_digest("account"),
        bbo_receipt_sha256=_digest("bbo"),
        setup_evidence_sha256=(
            _digest("setup-evidence")
            if setup_family == "first_dip_reclaim"
            else _digest("generic-setup-evidence")
        ),
        policy_sha256=_digest("adaptive-policy"),
        feature_flags_sha256=_digest("feature-flags"),
        opportunity_key=opportunity,
    )


def _resolve(context, **overrides):
    values = {
        "session_id": 77,
        "symbol": "ACTU",
        "execution_family": "alpaca_spot",
        "decision_at": DECISION_AT,
        "bid": "2.99",
        "ask": "3.00",
        "structural_stop_price": "2.80",
        "entry_limit_ceiling_price": "3.00",
        "entry_place_count": context.entry_place_count,
        "client_order_id": context.draft.intent.client_order_id,
        "setup_family": context.draft.intent.setup_family,
        "trigger_reason": context.trigger_reason,
        "trigger_debug": _debug(setup_family=context.draft.intent.setup_family),
        "confirmed_arm_marker": _arm_marker(),
        "candidate_generation_sha256": context.candidate_generation_sha256,
        "viability_updated_at": VIABILITY_UPDATED_AT,
        "viability_score": "0.8",
        "viability_payload_sha256": _digest("viability-payload"),
        "execution_readiness_sha256": _digest("execution-readiness"),
    }
    values.update(overrides)
    return selection.resolve_captured_paper_selection(**values)


@contextmanager
def _installed(context):
    with selection.require_captured_paper_selection(context.dispatch_request):
        with selection.install_captured_paper_selection_context(context):
            yield


def test_exact_captured_selection_returns_the_same_reverified_draft() -> None:
    context = _context()
    with _installed(context):
        result = _resolve(context)

    assert result.active is True
    assert result.reason is None
    assert result.request is context.draft
    result.request.verify()
    assert result.request.to_canonical_json() == context.draft.to_canonical_json()
    assert result.request.intent.account_receipt_sha256 == _digest("account")
    assert result.request.intent.bbo_receipt_sha256 == _digest("bbo")
    assert result.request.intent.policy_sha256 == _digest("adaptive-policy")
    assert result.request.intent.feature_flags_sha256 == _digest("feature-flags")

    # ContextVar cleanup is exception-safe and leaves ordinary ticks inactive.
    inactive = selection.resolve_captured_paper_selection(
        session_id=77,
        symbol="ACTU",
        execution_family="alpaca_spot",
        decision_at=DECISION_AT,
        bid="2.99",
        ask="3.00",
        structural_stop_price="2.80",
        entry_limit_ceiling_price="3.00",
        entry_place_count=3,
        client_order_id=context.draft.intent.client_order_id,
        setup_family=context.draft.intent.setup_family,
        trigger_reason=context.trigger_reason,
        trigger_debug=_debug(setup_family=context.draft.intent.setup_family),
        confirmed_arm_marker=_arm_marker(),
        candidate_generation_sha256=context.candidate_generation_sha256,
        viability_updated_at=VIABILITY_UPDATED_AT,
        viability_score="0.8",
        viability_payload_sha256=_digest("viability-payload"),
        execution_readiness_sha256=_digest("execution-readiness"),
    )
    assert inactive == selection.CapturedPaperSelectionResolution(active=False)


def test_generation_is_deterministic_and_place_generation_is_bound() -> None:
    first = _context(entry_place_count=3)
    same = _context(entry_place_count=3)
    next_place = _context(entry_place_count=4)

    assert first.context_sha256 == same.context_sha256
    assert first.draft.completion_sha256 == same.draft.completion_sha256
    assert first.draft.intent.binder_id == same.draft.intent.binder_id
    assert next_place.context_sha256 != first.context_sha256
    assert next_place.draft.intent.intent_generation != first.draft.intent.intent_generation

    with _installed(first):
        rejected = _resolve(first, entry_place_count=4)
    assert rejected.request is None
    assert rejected.reason == "captured_paper_entry_place_generation_mismatch"

    with _installed(first):
        viability_drift = _resolve(first, viability_score="0.81")
    assert viability_drift.request is None
    assert viability_drift.reason == "captured_paper_candidate_generation_mismatch"

    # execution_readiness_json can change sizing/slippage while score/time stay
    # identical, so its full-row digest is independently generation-bound.
    with _installed(first):
        payload_drift = _resolve(
            first,
            viability_payload_sha256=_digest("changed-viability-payload"),
        )
    assert payload_drift.request is None
    assert payload_drift.reason == "captured_paper_candidate_generation_mismatch"

    with _installed(first):
        readiness_drift = _resolve(
            first,
            execution_readiness_sha256=_digest("changed-execution-readiness"),
        )
    assert readiness_drift.request is None
    assert readiness_drift.reason == "captured_paper_candidate_generation_mismatch"

@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"ask": "3.01"}, "captured_paper_selection_bbo_mismatch"),
        (
            {"structural_stop_price": "2.79"},
            "captured_paper_selection_price_mismatch",
        ),
        (
            {"trigger_reason": "vwap_reclaim"},
            "captured_paper_trigger_reason_mismatch",
        ),
        (
            {"client_order_id": "chili_ml_e_77_wrong"},
            "captured_paper_client_order_id_mismatch",
        ),
        (
            {"decision_at": DECISION_AT + timedelta(seconds=1)},
            "captured_paper_selection_evidence_stale",
        ),
    ],
)
def test_mismatch_or_stale_evidence_rejects_only_this_decision(
    override, reason
) -> None:
    context = _context()
    before = context.draft.to_canonical_json()
    with _installed(context):
        result = _resolve(context, **override)
    assert result.active is True
    assert result.request is None
    assert result.reason == reason
    assert context.draft.to_canonical_json() == before


def test_first_dip_binds_typed_opportunity_and_detector_receipt() -> None:
    context = _context(setup_family="first_dip_reclaim")
    with _installed(context):
        accepted = _resolve(context)
        missing_receipt_debug = _debug(setup_family="first_dip_reclaim")
        missing_receipt_debug.pop(
            "first_dip_tape_decision_receipt_binding_sha256"
        )
        rejected = _resolve(context, trigger_debug=missing_receipt_debug)

    assert accepted.request is context.draft
    opportunity = accepted.request.intent.opportunity_key
    assert opportunity is not None
    assert opportunity.symbol == "ACTU"
    assert opportunity.trading_date == date.fromisoformat("2026-07-15")
    assert rejected.request is None
    # The exact trigger snapshot fails before serialized debug can become
    # order authority.  Nothing in this module can consume the opportunity.
    assert rejected.reason == "captured_paper_trigger_snapshot_mismatch"
def test_non_alpaca_and_no_context_are_strict_noops() -> None:
    context = _context()
    with _installed(context):
        result = _resolve(context, execution_family="coinbase_spot")
        assert result == selection.CapturedPaperSelectionResolution(active=False)
        assert selection.captured_paper_selection_context_active(
            execution_family="coinbase_spot"
        ) is False
    assert selection.captured_paper_selection_context_active(
        execution_family="alpaca_spot"
    ) is False


def test_dispatcher_marks_registered_callback_selection_required(monkeypatch) -> None:
    from app.services.trading.momentum_neural import captured_paper_dispatcher as dispatcher

    dispatch = _dispatch()
    session = SimpleNamespace(
        id=dispatch.session_id,
        symbol=dispatch.symbol,
        execution_family=dispatch.execution_family,
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": ACCOUNT_ID,
        },
    )
    monkeypatch.setattr(dispatcher, "_load_live_session", lambda *_args: session)
    monkeypatch.setattr(dispatcher.settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(
        dispatcher.settings,
        "chili_alpaca_expected_account_id",
        ACCOUNT_ID,
    )
    monkeypatch.setattr(
        dispatcher.settings,
        "chili_momentum_first_dip_reclaim_policy_mode",
        "candidate",
    )

    observed = {}

    def bare_handler(_db, request):
        observed["request"] = request
        observed["required"] = selection.captured_paper_selection_required(
            execution_family="alpaca_spot"
        )
        observed["active"] = selection.captured_paper_selection_context_active(
            execution_family="alpaca_spot"
        )
        return {"bare_runner_would_defer": observed["required"] and not observed["active"]}

    runtime = dispatcher.CapturedPaperRuntime(
        handler=bare_handler,
        expected_account_id=ACCOUNT_ID,
        code_build_sha256=dispatch.code_build_sha256,
        config_sha256=dispatch.config_sha256,
        capture_receipt_sha256=dispatch.capture_receipt_sha256,
        runtime_generation=RUNTIME_GENERATION,
        first_dip_policy_mode="candidate",
    )
    with dispatcher.register_captured_paper_runtime(runtime):
        result = dispatcher.dispatch_live_runner_tick(object(), 77)

    assert observed["request"].provenance_sha256 == dispatch.provenance_sha256
    assert observed["required"] is True
    assert observed["active"] is False
    assert result == {"bare_runner_would_defer": True}
    assert selection.captured_paper_selection_required(
        execution_family="alpaca_spot"
    ) is False


def test_registered_bare_runner_defers_before_adapter_builder_or_order(
    monkeypatch,
) -> None:
    from app.services.trading.momentum_neural import live_runner

    class Query:
        def populate_existing(self):
            return self

        def filter(self, *_args, **_kwargs):
            return self

        def with_for_update(self, **_kwargs):
            return self

        def one_or_none(self):
            return session

    class Db:
        flushed = 0

        def query(self, *_args, **_kwargs):
            return Query()

        def flush(self):
            self.flushed += 1

    session = SimpleNamespace(
        id=77,
        symbol="ACTU",
        execution_family="alpaca_spot",
        state=live_runner.STATE_WATCHING_LIVE,
        risk_snapshot_json={
            "momentum_risk_policy_summary": {
                "disable_live_if_governance_inhibit": True
            }
        },
    )
    events = []
    monkeypatch.setattr(
        live_runner.settings, "chili_momentum_live_runner_enabled", True
    )
    monkeypatch.setattr(
        live_runner, "_alpaca_execution_quarantine_reason", lambda *_args: None
    )
    monkeypatch.setattr(
        live_runner, "_confirmed_alpaca_arm_generation_reason", lambda *_args: None
    )
    monkeypatch.setattr(live_runner, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(
        live_runner,
        "_safe_transition",
        lambda _db, sess, state: setattr(sess, "state", state),
    )
    monkeypatch.setattr(
        live_runner,
        "_emit",
        lambda _db, _sess, event, payload: events.append((event, payload)),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("registered bare runner reached a legacy capability")

    monkeypatch.setattr(live_runner, "resolve_live_spot_adapter_factory", forbidden)
    monkeypatch.setattr(
        live_runner,
        "_build_adaptive_alpaca_primary_before_legacy_sizing",
        forbidden,
    )
    monkeypatch.setattr(live_runner, "_governed_place", forbidden)

    db = Db()
    dispatch = _dispatch()
    with selection.require_captured_paper_selection(dispatch):
        result = live_runner.tick_live_session(
            db,
            77,
            adapter_factory=forbidden,
        )

    assert result == {
        "ok": True,
        "session_id": 77,
        "state": live_runner.STATE_WATCHING_LIVE,
        "deferred": True,
        "reason": "captured_paper_selection_context_missing",
        "broker_calls": 0,
    }
    assert db.flushed == 1
    assert events == [
        (
            "live_entry_captured_paper_selection_deferred",
            {
                "reason": "captured_paper_selection_context_missing",
                "opportunity_consumed": False,
                "risk_reserved": False,
                "order_posted": False,
                "broker_calls": 0,
            },
        )
    ]


def test_context_mutation_and_nested_install_fail_closed() -> None:
    context = _context()
    with selection.require_captured_paper_selection(context.dispatch_request):
        with selection.install_captured_paper_selection_context(context):
            with pytest.raises(
                selection.CapturedPaperSelectionContextError,
                match="captured_paper_selection_context_already_active",
            ):
                with selection.install_captured_paper_selection_context(context):
                    pass

    object.__setattr__(context, "expected_bid", "2.98")
    with selection.require_captured_paper_selection(context.dispatch_request):
        with pytest.raises(
            selection.CapturedPaperSelectionContextError,
            match="captured_paper_selection_(?:context_mutated|generation_mismatch)",
        ):
            with selection.install_captured_paper_selection_context(context):
                pass


def test_selection_module_has_no_db_network_broker_or_mutation_capability() -> None:
    source = inspect.getsource(selection)
    imports = {
        line.strip()
        for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    }
    assert not any(
        forbidden in line
        for line in imports
        for forbidden in (
            "sqlalchemy",
            "requests",
            "httpx",
            "alpaca_spot",
            "captured_paper_admission",
            "captured_paper_outbox",
            "transport_coordinator",
        )
    )
    assert "def __call__(self, db" not in source
    assert "object_session(" not in source


def test_live_runner_hook_precedes_every_legacy_sizing_and_order_path() -> None:
    from app.services.trading.momentum_neural import (
        captured_paper_dispatcher,
        live_runner,
    )

    source = inspect.getsource(live_runner.tick_live_session)
    bare_fence = source.index(
        "_captured_missing_reason = ("
    )
    adapter_construction = source.index(
        "factory = adapter_factory or resolve_live_spot_adapter_factory(ef)"
    )
    required_gate = source.index(
        "if captured_paper_selection_required(execution_family=ef):"
    )
    hook = source.index("_captured_resolution = resolve_captured_paper_selection(")
    success_return = source.index("return _captured_resolution.request", hook)
    adaptive_builder = source.index(
        ") = _build_adaptive_alpaca_primary_before_legacy_sizing(", hook
    )
    decision_ledger = source.index("dec = run_momentum_entry_decision(", hook)
    legacy_risk_sizing = source.index("compute_risk_first_quantity(", hook)
    governed_place = source.index("_governed_place(", hook)

    assert bare_fence < adapter_construction < required_gate
    assert required_gate < hook < success_return < adaptive_builder
    assert success_return < decision_ledger < legacy_risk_sizing < governed_place
    hook_block = source[hook:adaptive_builder]
    assert "_commit_le(" not in hook_block
    assert "reserve_" not in hook_block
    assert "acquire_action_claim" not in hook_block
    assert "_governed_place(" not in hook_block
    assert "$50" not in hook_block and "$250" not in hook_block
    dispatcher_source = inspect.getsource(
        captured_paper_dispatcher.dispatch_live_runner_tick
    )
    assert "with require_captured_paper_selection(request):" in dispatcher_source
    assert "return runtime.handler(db, request)" in dispatcher_source


def test_observation_context_permits_only_exact_watcher_generation_and_clock() -> None:
    dispatch = _dispatch()
    updated = DECISION_AT - timedelta(seconds=2)
    hashes = {
        "risk_snapshot_sha256": _digest("observation-risk"),
        "viability_payload_sha256": _digest("observation-viability"),
        "variant_payload_sha256": _digest("observation-variant"),
        "confirmed_arm_marker_sha256": _digest("observation-arm"),
    }
    generation = selection.captured_paper_observation_generation_sha256(
        session_id=dispatch.session_id,
        symbol=dispatch.symbol,
        execution_family=dispatch.execution_family,
        state="watching_live",
        correlation_id="corr-77",
        variant_id=4,
        session_updated_at=updated,
        **hashes,
    )
    context = selection.CapturedPaperObservationContext(
        dispatch_request=dispatch,
        initial_state="watching_live",
        correlation_id="corr-77",
        variant_id=4,
        session_updated_at=updated,
        decision_at=DECISION_AT,
        evidence_available_at=DECISION_AT - timedelta(milliseconds=10),
        evidence_expires_at=DECISION_AT + timedelta(seconds=30),
        observation_decision_id=(
            f"captured-paper-observe-{dispatch.session_id}-{generation[:24]}"
        ),
        observation_generation_sha256=generation,
        **hashes,
    )

    with selection.require_captured_paper_selection(dispatch):
        with selection.install_captured_paper_observation_context(context):
            assert selection.captured_paper_observation_context_active(
                execution_family="alpaca_spot"
            )
            assert not selection.captured_paper_selection_context_active(
                execution_family="alpaca_spot"
            )
            exact = selection.resolve_captured_paper_observation(
                session_id=dispatch.session_id,
                symbol=dispatch.symbol,
                execution_family=dispatch.execution_family,
                state="watching_live",
                correlation_id="corr-77",
                variant_id=4,
                session_updated_at=updated,
                decision_at=DECISION_AT,
                **hashes,
            )
            assert exact.active and exact.permitted and exact.reason is None
            drift = selection.resolve_captured_paper_observation(
                session_id=dispatch.session_id,
                symbol=dispatch.symbol,
                execution_family=dispatch.execution_family,
                state="watching_live",
                correlation_id="corr-77",
                variant_id=4,
                session_updated_at=updated,
                decision_at=DECISION_AT,
                **{
                    **hashes,
                    "viability_payload_sha256": _digest("changed-viability"),
                },
            )
            assert drift.active and not drift.permitted
            assert drift.reason == "captured_paper_observation_generation_mismatch"
