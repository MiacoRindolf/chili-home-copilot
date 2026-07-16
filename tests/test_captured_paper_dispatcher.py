from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import inspect
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import captured_paper_entry_intent as intent_contract
from app.services.trading.momentum_neural import captured_paper_dispatcher as dispatch
from app.services.trading.momentum_neural import live_runner_loop as loop_mod


_ACCOUNT_ID = "d7cc580c-2b8f-432f-b771-1cecfb3fe87a"
_GENERATION = "f6ef5ba0-5b91-49bf-a2f5-e71e8e270eb3"
_INTENT_GENERATION = "39f55a65-e6f2-4ccc-bd02-f50dc9c27c69"
_COMPLETION_GENERATION = "73dbcf92-94ea-436e-978c-b0e31ce7252d"
_ARM_TOKEN = "d2b8f7d8-6ad5-4cd0-a94e-8a9ca146d3ab"
_BINDER_ID = "122158cc-18ae-4cef-bc52-f1c5b689b352"


class _Query:
    def __init__(self, row):
        self._row = row
        self.for_update_calls = []

    def filter(self, *_args):
        return self

    def with_for_update(self, **kwargs):
        self.for_update_calls.append(dict(kwargs))
        return self

    def one_or_none(self):
        return self._row


class _Db:
    def __init__(self, row, *, rows=None):
        self.row = row
        self.rows = list(rows) if rows is not None else None
        self.queries = []

    def query(self, *_columns):
        if self.rows is None:
            row = self.row
        else:
            index = min(len(self.queries), len(self.rows) - 1)
            row = self.rows[index]
        query = _Query(row)
        self.queries.append(query)
        return query


def _session(*, family: str, account_id: str = _ACCOUNT_ID):
    return SimpleNamespace(
        id=41,
        mode="live",
        symbol="ACTU",
        execution_family=family,
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": account_id,
        },
    )


def _runtime(
    handler,
    *,
    mode: str = "candidate",
    post_commit_handler=None,
    config_sha256_resolver=None,
) -> dispatch.CapturedPaperRuntime:
    return dispatch.CapturedPaperRuntime(
        handler=handler,
        expected_account_id=_ACCOUNT_ID,
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        capture_receipt_sha256="c" * 64,
        runtime_generation=_GENERATION,
        first_dip_policy_mode=mode,
        settings_projection_sha256=(
            "7" * 64 if config_sha256_resolver is not None else None
        ),
        config_sha256_resolver=config_sha256_resolver,
        post_commit_handler=post_commit_handler,
    )


def _entry_intent(
    route_token: intent_contract.CapturedPaperRouteToken,
    *,
    intent_generation: str = _INTENT_GENERATION,
) -> intent_contract.CapturedPaperEntryIntent:
    arm = intent_contract.CapturedPaperConfirmedArmGeneration(
        session_id=route_token.session_id,
        arm_token=_ARM_TOKEN,
        expires_at=datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc),
        symbol_claim_token=f"arm-{_ARM_TOKEN}",
        account_scope=route_token.account_scope,
        expected_account_id=route_token.expected_account_id,
        confirmed_at=datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc),
    )
    opportunity_key = intent_contract.CapturedPaperOpportunityKey(
        account_scope=route_token.account_scope,
        symbol=route_token.symbol,
        trading_date=datetime(2026, 7, 15).date(),
        setup_family="first_dip_reclaim",
    )
    return intent_contract.CapturedPaperEntryIntent(
        route_token=route_token,
        confirmed_arm_generation=arm,
        symbol_claim_token=arm.symbol_claim_token,
        binder_id=_BINDER_ID,
        opportunity_key=opportunity_key,
        intent_generation=intent_generation,
        client_order_id="chili_ml_ACTU_41_1",
        decision_id="chili_ml_ACTU_41_1",
        setup_family="first_dip_reclaim",
        decision_at=datetime(2026, 7, 15, 16, 30, tzinfo=timezone.utc),
        structural_stop_price="2.50",
        entry_limit_ceiling_price="3.00",
        account_receipt_sha256="d" * 64,
        bbo_receipt_sha256="e" * 64,
        setup_evidence_sha256="f" * 64,
        policy_sha256="1" * 64,
        feature_flags_sha256="2" * 64,
    )


def _post_commit_request(
    *,
    runtime_generation: str = _GENERATION,
) -> intent_contract.CapturedPaperPostCommitRequest:
    route = intent_contract.CapturedPaperRouteToken(
        session_id=41,
        symbol="ACTU",
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        expected_account_id=_ACCOUNT_ID,
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        capture_receipt_sha256="c" * 64,
        runtime_generation=runtime_generation,
        first_dip_policy_mode="candidate",
    )
    return intent_contract.CapturedPaperPostCommitRequest(
        intent=_entry_intent(route),
        completion_generation=_COMPLETION_GENERATION,
    )


def _paper_settings(monkeypatch, *, paper: bool = True, mode: str = "candidate"):
    monkeypatch.setattr(dispatch.settings, "chili_alpaca_paper", paper, raising=False)
    monkeypatch.setattr(
        dispatch.settings,
        "chili_alpaca_expected_account_id",
        _ACCOUNT_ID,
        raising=False,
    )
    monkeypatch.setattr(
        dispatch.settings,
        "chili_momentum_first_dip_reclaim_policy_mode",
        mode,
        raising=False,
    )


def test_non_alpaca_session_preserves_ordinary_runner(monkeypatch):
    _paper_settings(monkeypatch)
    calls = []

    def ordinary(db, session_id):
        calls.append((db, session_id))
        return {"ok": True, "path": "ordinary"}

    db = _Db(_session(family="coinbase_spot"))
    result = dispatch.dispatch_live_runner_tick(db, 41, non_paper_tick=ordinary)

    assert result == {"ok": True, "path": "ordinary"}
    assert calls == [(db, 41)]
    assert len(db.queries) == 2
    assert db.queries[0].for_update_calls == []
    assert db.queries[1].for_update_calls == [{"nowait": True}]


def test_dedicated_dispatch_has_no_ordinary_fallback(monkeypatch):
    _paper_settings(monkeypatch)
    monkeypatch.setattr(
        dispatch,
        "_ordinary_tick",
        lambda *_args, **_kwargs: pytest.fail("ordinary path must be unreachable"),
    )

    with pytest.raises(
        dispatch.CapturedPaperExecutionProhibitedError,
        match="captured_paper_dedicated_foreign_execution_family",
    ):
        dispatch.dispatch_captured_paper_live_runner_tick(
            _Db(_session(family="coinbase_spot")),
            41,
            expected_account_id=_ACCOUNT_ID,
            expected_runtime_generation=_GENERATION,
        )


def test_dedicated_dispatch_rejects_foreign_runtime_generation_before_handler(
    monkeypatch,
):
    _paper_settings(monkeypatch)
    calls = []
    with dispatch.register_captured_paper_runtime(
        _runtime(lambda *_args: calls.append("handler"))
    ):
        with pytest.raises(
            dispatch.CapturedPaperRuntimeUnavailableError,
            match="captured_paper_dedicated_runtime_scope_mismatch",
        ):
            dispatch.dispatch_captured_paper_live_runner_tick(
                _Db(_session(family="alpaca_spot")),
                41,
                expected_account_id=_ACCOUNT_ID,
                expected_runtime_generation=(
                    "9b16a3f6-d2a1-4f88-bae3-cb54e2b58f72"
                ),
            )
    assert calls == []


def test_ordinary_route_drift_never_reaches_bare_runner(monkeypatch):
    _paper_settings(monkeypatch)
    bare_calls = []
    db = _Db(
        None,
        rows=[
            _session(family="coinbase_spot"),
            _session(family="alpaca_spot"),
        ],
    )

    with pytest.raises(
        dispatch.CapturedPaperRuntimeUnavailableError,
        match="captured_paper_ordinary_route_drift",
    ):
        dispatch.dispatch_live_runner_tick(
            db,
            41,
            non_paper_tick=lambda *_args: bare_calls.append("bare"),
        )

    assert bare_calls == []
    assert db.queries[0].for_update_calls == []
    assert db.queries[1].for_update_calls == [{"nowait": True}]


def test_unknown_session_cannot_race_into_bare_runner(monkeypatch):
    _paper_settings(monkeypatch)
    bare_calls = []

    with pytest.raises(
        dispatch.CapturedPaperRuntimeUnavailableError,
        match="captured_paper_live_session_not_found",
    ):
        dispatch.dispatch_live_runner_tick(
            _Db(None),
            41,
            non_paper_tick=lambda *_args: bare_calls.append("bare"),
        )

    assert bare_calls == []


def test_alpaca_paper_without_runtime_fails_before_bare_fsm(monkeypatch):
    _paper_settings(monkeypatch)
    bare_calls = []

    with pytest.raises(
        dispatch.CapturedPaperRuntimeUnavailableError,
        match="captured_paper_runtime_not_registered",
    ):
        dispatch.dispatch_live_runner_tick(
            _Db(_session(family="alpaca_spot")),
            41,
            non_paper_tick=lambda *_args: bare_calls.append("bare"),
        )

    assert bare_calls == []


def test_registered_runtime_owns_paper_tick_and_receives_bound_provenance(monkeypatch):
    _paper_settings(monkeypatch)
    handled = []
    bare_calls = []

    def handler(db, request):
        handled.append((db, request))
        return {"ok": True, "path": "captured"}

    runtime = _runtime(handler)
    db = _Db(_session(family="alpaca_spot"))
    with dispatch.register_captured_paper_runtime(runtime):
        result = dispatch.dispatch_live_runner_tick(
            db,
            41,
            non_paper_tick=lambda *_args: bare_calls.append("bare"),
        )

    assert result == {"ok": True, "path": "captured"}
    assert bare_calls == []
    assert len(handled) == 1
    request = handled[0][1]
    assert request.account_scope == "alpaca:paper"
    assert request.expected_account_id == _ACCOUNT_ID
    assert request.code_build_sha256 == "a" * 64
    assert request.config_sha256 == "b" * 64
    assert request.capture_receipt_sha256 == "c" * 64
    assert request.runtime_generation == _GENERATION
    assert request.first_dip_policy_mode == "candidate"
    assert len(request.provenance_sha256) == 64
    assert request.provenance_sha256 == request.route_token.route_token_sha256
    request.verify()
    assert len(db.queries) == 1
    assert db.queries[0].for_update_calls == []
    assert (
        dispatch.revalidate_captured_paper_route_token(
            request.route_token,
            _session(family="alpaca_spot"),
            runtime,
        )
        is request.route_token
    )


def test_dynamic_runtime_stamps_and_revalidates_actual_symbol_capture_config(
    monkeypatch,
):
    _paper_settings(monkeypatch)
    resolved = {"ACTU": "8" * 64}
    handled = []
    runtime = _runtime(
        lambda _db, request: handled.append(request) or {"ok": True},
        config_sha256_resolver=lambda symbol: resolved[symbol],
    )
    with dispatch.register_captured_paper_runtime(runtime):
        dispatch.dispatch_live_runner_tick(
            _Db(_session(family="alpaca_spot")),
            41,
            non_paper_tick=lambda *_args: pytest.fail("bare path reached"),
        )

    request = handled[0]
    assert runtime.settings_projection_sha256 == "7" * 64
    assert runtime.config_sha256 == "b" * 64
    assert request.config_sha256 == "8" * 64
    assert (
        dispatch.revalidate_captured_paper_route_token(
            request.route_token,
            _session(family="alpaca_spot"),
            runtime,
        )
        is request.route_token
    )
    resolved["ACTU"] = "9" * 64
    with pytest.raises(
        dispatch.CapturedPaperRuntimeUnavailableError,
        match="captured_paper_route_runtime_provenance_drift",
    ):
        dispatch.revalidate_captured_paper_route_token(
            request.route_token,
            _session(family="alpaca_spot"),
            runtime,
        )


@pytest.mark.parametrize(
    ("locked_session", "reason"),
    [
        (
            _session(family="coinbase_spot"),
            "captured_paper_route_execution_family_drift",
        ),
        (
            _session(
                family="alpaca_spot",
                account_id="3e324204-dc02-4ec8-964d-b7792d4093ef",
            ),
            "captured_paper_route_account_id_drift",
        ),
        (
            SimpleNamespace(
                **{
                    **vars(_session(family="alpaca_spot")),
                    "symbol": "OTHER",
                }
            ),
            "captured_paper_route_symbol_drift",
        ),
    ],
)
def test_later_locked_route_drift_rejects_token(
    monkeypatch,
    locked_session,
    reason,
):
    _paper_settings(monkeypatch)
    runtime = _runtime(lambda *_args: None)
    request = dispatch.CapturedPaperDispatchRequest(
        session_id=41,
        symbol="ACTU",
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        expected_account_id=_ACCOUNT_ID,
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        capture_receipt_sha256="c" * 64,
        runtime_generation=_GENERATION,
        first_dip_policy_mode="candidate",
    )

    with pytest.raises(
        dispatch.CapturedPaperRuntimeUnavailableError,
        match=reason,
    ):
        dispatch.revalidate_captured_paper_route_token(
            request.route_token,
            locked_session,
            runtime,
        )


def test_route_runtime_provenance_drift_is_rejected_later(monkeypatch):
    _paper_settings(monkeypatch)
    original = _runtime(lambda *_args: None)
    drifted = replace(original, config_sha256="9" * 64)
    request = dispatch.CapturedPaperDispatchRequest(
        session_id=41,
        symbol="ACTU",
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        expected_account_id=_ACCOUNT_ID,
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        capture_receipt_sha256="c" * 64,
        runtime_generation=_GENERATION,
        first_dip_policy_mode="candidate",
    )

    with pytest.raises(
        dispatch.CapturedPaperRuntimeUnavailableError,
        match="captured_paper_route_runtime_provenance_drift",
    ):
        dispatch.revalidate_captured_paper_route_token(
            request.route_token,
            _session(family="alpaca_spot"),
            drifted,
        )


def test_entry_intent_and_post_commit_request_are_content_addressed_immutable():
    route = intent_contract.CapturedPaperRouteToken(
        session_id=41,
        symbol="ACTU",
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        expected_account_id=_ACCOUNT_ID,
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        capture_receipt_sha256="c" * 64,
        runtime_generation=_GENERATION,
        first_dip_policy_mode="candidate",
    )
    intent = _entry_intent(route)
    completion = intent_contract.CapturedPaperPostCommitRequest(
        intent=intent,
        completion_generation=_COMPLETION_GENERATION,
    )

    intent.verify()
    completion.verify()
    assert completion.route_token is route
    assert completion.to_payload()["intent_sha256"] == intent.intent_sha256
    assert "quantity" not in intent.to_payload()
    with pytest.raises(FrozenInstanceError):
        intent.intent_generation = "9b3e11c9-b32a-4081-93d1-4c42a76d421e"

    next_generation = replace(
        intent,
        intent_generation="9b3e11c9-b32a-4081-93d1-4c42a76d421e",
    )
    assert next_generation.intent_sha256 != intent.intent_sha256

    tampered = replace(intent)
    object.__setattr__(
        tampered,
        "intent_generation",
        "9b3e11c9-b32a-4081-93d1-4c42a76d421e",
    )
    with pytest.raises(
        intent_contract.CapturedPaperIntentContractError,
        match="entry_intent_hash_mismatch",
    ):
        tampered.verify()


def test_contract_slice_has_no_admission_or_transport_capability(monkeypatch):
    _paper_settings(monkeypatch)
    completion_calls = []
    runtime = _runtime(
        lambda *_args: {"ok": True},
        post_commit_handler=lambda request: completion_calls.append(request),
    )
    with dispatch.register_captured_paper_runtime(runtime):
        dispatch.dispatch_live_runner_tick(
            _Db(_session(family="alpaca_spot")),
            41,
        )

    # Dispatching phase one never invokes completion.  The separately called
    # dispatcher carries only the typed request and no outer DB session.
    assert completion_calls == []
    assert list(
        inspect.signature(
            dispatch.dispatch_captured_paper_post_commit
        ).parameters
    ) == ["request"]

    tree = ast.parse(inspect.getsource(intent_contract))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    forbidden_imports = {
        "AdaptiveRiskReservationStore",
        "AdaptiveRiskOpportunityClaim",
        "BrokerSymbolActionClaim",
        "AlpacaSpotAdapter",
        "reserve_alpaca_entry_risk_committed",
        "mark_entry_transport_started_committed",
    }
    assert imported_names.isdisjoint(forbidden_imports)
    assert list(
        inspect.signature(
            intent_contract.CapturedPaperPostCommitHandler.__call__
        ).parameters
    ) == ["self", "request"]


def test_post_commit_dispatch_leases_runtime_and_passes_only_verified_request(
    monkeypatch,
):
    _paper_settings(monkeypatch)
    completion = _post_commit_request()
    observed = []

    def post_commit_handler(request):
        observed.append(request)
        return {"completed": request.completion_sha256}

    runtime = _runtime(
        lambda *_args: None,
        post_commit_handler=post_commit_handler,
    )
    with dispatch.register_captured_paper_runtime(runtime):
        result = dispatch.dispatch_captured_paper_post_commit(completion)

    assert result == {"completed": completion.completion_sha256}
    assert observed == [completion]


def test_post_commit_dispatch_requires_registered_completion_owner(monkeypatch):
    _paper_settings(monkeypatch)
    with dispatch.register_captured_paper_runtime(_runtime(lambda *_args: None)):
        with pytest.raises(
            dispatch.CapturedPaperRuntimeUnavailableError,
            match="captured_paper_post_commit_handler_not_registered",
        ):
            dispatch.dispatch_captured_paper_post_commit(_post_commit_request())


def test_post_commit_dispatch_rejects_runtime_or_config_drift(monkeypatch):
    _paper_settings(monkeypatch)
    completion = _post_commit_request()
    observed = []

    drifted_runtime = replace(
        _runtime(
            lambda *_args: None,
            post_commit_handler=lambda request: observed.append(request),
        ),
        runtime_generation="9b3e11c9-b32a-4081-93d1-4c42a76d421e",
    )
    with dispatch.register_captured_paper_runtime(drifted_runtime):
        with pytest.raises(
            dispatch.CapturedPaperRuntimeUnavailableError,
            match="captured_paper_post_commit_runtime_provenance_drift",
        ):
            dispatch.dispatch_captured_paper_post_commit(completion)

    with dispatch.register_captured_paper_runtime(
        _runtime(
            lambda *_args: None,
            post_commit_handler=lambda request: observed.append(request),
        )
    ):
        monkeypatch.setattr(
            dispatch.settings,
            "chili_alpaca_expected_account_id",
            "3e324204-dc02-4ec8-964d-b7792d4093ef",
            raising=False,
        )
        with pytest.raises(
            dispatch.CapturedPaperRuntimeUnavailableError,
            match="captured_paper_config_account_mismatch",
        ):
            dispatch.dispatch_captured_paper_post_commit(completion)

    assert observed == []


def test_post_commit_dispatch_rejects_mutated_or_untyped_request(monkeypatch):
    _paper_settings(monkeypatch)
    completion = _post_commit_request()
    object.__setattr__(
        completion,
        "completion_generation",
        "9b3e11c9-b32a-4081-93d1-4c42a76d421e",
    )
    observed = []
    with dispatch.register_captured_paper_runtime(
        _runtime(
            lambda *_args: None,
            post_commit_handler=lambda request: observed.append(request),
        )
    ):
        with pytest.raises(
            dispatch.CapturedPaperRuntimeUnavailableError,
            match="post_commit_content_hash_mismatch",
        ):
            dispatch.dispatch_captured_paper_post_commit(completion)
        with pytest.raises(
            dispatch.CapturedPaperRuntimeUnavailableError,
            match="captured_paper_post_commit_request_invalid",
        ):
            dispatch.dispatch_captured_paper_post_commit(object())
    assert observed == []


@pytest.mark.parametrize(
    ("configured_account", "configured_mode", "reason"),
    [
        (
            "3e324204-dc02-4ec8-964d-b7792d4093ef",
            "candidate",
            "captured_paper_config_account_mismatch",
        ),
        (
            _ACCOUNT_ID,
            "baseline",
            "captured_paper_first_dip_policy_mismatch",
        ),
    ],
)
def test_runtime_provenance_drift_fails_before_handler(
    monkeypatch,
    configured_account,
    configured_mode,
    reason,
):
    _paper_settings(monkeypatch, mode=configured_mode)
    monkeypatch.setattr(
        dispatch.settings,
        "chili_alpaca_expected_account_id",
        configured_account,
        raising=False,
    )
    handled = []
    with dispatch.register_captured_paper_runtime(
        _runtime(lambda *_args: handled.append("handled"))
    ):
        with pytest.raises(dispatch.CapturedPaperRuntimeUnavailableError, match=reason):
            dispatch.dispatch_live_runner_tick(
                _Db(_session(family="alpaca_spot")), 41
            )
    assert handled == []


def test_session_account_binding_mismatch_fails_before_handler(monkeypatch):
    _paper_settings(monkeypatch)
    handled = []
    with dispatch.register_captured_paper_runtime(
        _runtime(lambda *_args: handled.append("handled"))
    ):
        with pytest.raises(
            dispatch.CapturedPaperRuntimeUnavailableError,
            match="captured_paper_session_account_id_mismatch",
        ):
            dispatch.dispatch_live_runner_tick(
                _Db(
                    _session(
                        family="alpaca_spot",
                        account_id="3e324204-dc02-4ec8-964d-b7792d4093ef",
                    )
                ),
                41,
            )
    assert handled == []


def test_live_cash_posture_never_reaches_registered_handler_or_bare_fsm(monkeypatch):
    _paper_settings(monkeypatch, paper=False)
    handled = []
    bare_calls = []
    with dispatch.register_captured_paper_runtime(
        _runtime(lambda *_args: handled.append("handled"))
    ):
        with pytest.raises(
            dispatch.CapturedPaperExecutionProhibitedError,
            match="captured_paper_live_cash_execution_prohibited",
        ):
            dispatch.dispatch_live_runner_tick(
                _Db(_session(family="alpaca_spot")),
                41,
                non_paper_tick=lambda *_args: bare_calls.append("bare"),
            )
    assert handled == []
    assert bare_calls == []


def test_alpaca_short_never_reaches_captured_or_bare_path(monkeypatch):
    _paper_settings(monkeypatch)
    handled = []
    bare_calls = []
    with dispatch.register_captured_paper_runtime(
        _runtime(lambda *_args: handled.append("handled"))
    ):
        with pytest.raises(
            dispatch.CapturedPaperExecutionProhibitedError,
            match="captured_paper_short_execution_not_certified",
        ):
            dispatch.dispatch_live_runner_tick(
                _Db(_session(family="alpaca_short")),
                41,
                non_paper_tick=lambda *_args: bare_calls.append("bare"),
            )
    assert handled == []
    assert bare_calls == []


def test_runtime_registration_rejects_nonpaper_scope():
    with pytest.raises(ValueError, match="account scope must be alpaca:paper"):
        dispatch.CapturedPaperRuntime(
            handler=lambda *_args: None,
            expected_account_id=_ACCOUNT_ID,
            code_build_sha256="a" * 64,
            config_sha256="b" * 64,
            capture_receipt_sha256="c" * 64,
            runtime_generation=_GENERATION,
            first_dip_policy_mode="candidate",
            account_scope="alpaca:live",
        )


def test_event_loop_tick_uses_dispatcher_and_commits(monkeypatch):
    lifecycle = SimpleNamespace(commits=0, rollbacks=0, closed=False)

    def commit():
        lifecycle.commits += 1

    def rollback():
        lifecycle.rollbacks += 1

    def close():
        lifecycle.closed = True

    db = SimpleNamespace(commit=commit, rollback=rollback, close=close)
    calls = []
    monkeypatch.setattr(loop_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        loop_mod,
        "dispatch_live_runner_tick",
        lambda owned_db, session_id: calls.append((owned_db, session_id)),
    )

    loop_mod.LiveRunnerLoop()._tick_session(41)

    assert calls == [(db, 41)]
    assert lifecycle.commits == 1
    assert lifecycle.rollbacks >= 1
    assert lifecycle.closed is True


def test_event_loop_commits_before_exact_post_commit_completion(monkeypatch):
    events = []
    completion = _post_commit_request()
    db = SimpleNamespace(
        commit=lambda: events.append("commit"),
        rollback=lambda: events.append("rollback"),
        close=lambda: events.append("close"),
    )
    monkeypatch.setattr(loop_mod, "SessionLocal", lambda: db)

    def phase_one(owned_db, session_id):
        assert owned_db is db
        assert session_id == 41
        events.append("phase_one")
        return completion

    def complete(request):
        assert request is completion
        events.append("completion")

    monkeypatch.setattr(loop_mod, "dispatch_live_runner_tick", phase_one)
    monkeypatch.setattr(
        loop_mod,
        "dispatch_captured_paper_post_commit",
        complete,
    )

    loop_mod.LiveRunnerLoop()._tick_session(41)

    assert events == ["phase_one", "commit", "completion", "close"]


def test_event_loop_never_completes_before_successful_commit(monkeypatch):
    completion = _post_commit_request()
    events = []

    def fail_commit():
        events.append("commit_failed")
        raise RuntimeError("commit failed")

    db = SimpleNamespace(
        commit=fail_commit,
        rollback=lambda: events.append("rollback"),
        close=lambda: events.append("close"),
    )
    monkeypatch.setattr(loop_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        loop_mod,
        "dispatch_live_runner_tick",
        lambda *_args: completion,
    )
    monkeypatch.setattr(
        loop_mod,
        "dispatch_captured_paper_post_commit",
        lambda *_args: events.append("completion"),
    )

    loop_mod.LiveRunnerLoop()._tick_session(41)

    assert "completion" not in events
    assert events[0] == "commit_failed"
    assert events[-1] == "close"
    assert "rollback" in events


def test_event_loop_completion_failure_does_not_rollback_committed_phase_one(
    monkeypatch,
):
    completion = _post_commit_request()
    events = []
    db = SimpleNamespace(
        commit=lambda: events.append("commit"),
        rollback=lambda: events.append("rollback"),
        close=lambda: events.append("close"),
    )
    monkeypatch.setattr(loop_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        loop_mod,
        "dispatch_live_runner_tick",
        lambda *_args: completion,
    )

    def fail_completion(_request):
        events.append("completion_failed")
        raise RuntimeError("retry me")

    monkeypatch.setattr(
        loop_mod,
        "dispatch_captured_paper_post_commit",
        fail_completion,
    )

    loop_mod.LiveRunnerLoop()._tick_session(41)

    assert events == ["commit", "completion_failed", "close"]
