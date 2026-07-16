from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import captured_paper_dispatcher as dispatch
from app.services.trading.momentum_neural import captured_paper_selection as selection
from app.services.trading.momentum_neural import live_runner
from app.services.trading.momentum_neural.adaptive_risk_account_lock import (
    AdaptiveRiskAccountLockIdentity,
)


_ACCOUNT_ID = "d7cc580c-2b8f-432f-b771-1cecfb3fe87a"
_GENERATION = "f6ef5ba0-5b91-49bf-a2f5-e71e8e270eb3"


class _Query:
    def __init__(self, row):
        self._row = row
        self.for_update_calls: list[dict] = []

    def populate_existing(self):
        return self

    def filter(self, *_args):
        return self

    def with_for_update(self, **kwargs):
        self.for_update_calls.append(dict(kwargs))
        return self

    def one_or_none(self):
        return self._row


class _Db:
    def __init__(self, row):
        self.row = row
        self.queries: list[_Query] = []
        self.flush_calls = 0

    def in_transaction(self):
        return True

    def query(self, *_columns):
        query = _Query(self.row)
        self.queries.append(query)
        return query

    def flush(self):
        self.flush_calls += 1


def _request() -> dispatch.CapturedPaperDispatchRequest:
    return dispatch.CapturedPaperDispatchRequest(
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


def _session(*, marker: dict | None = None):
    snapshot = {
        "alpaca_account_scope": "alpaca:paper",
        "alpaca_account_id": _ACCOUNT_ID,
    }
    if marker is not None:
        snapshot["captured_paper_session_owner"] = deepcopy(marker)
    return SimpleNamespace(
        id=41,
        mode="live",
        symbol="ACTU",
        execution_family="alpaca_spot",
        state="WATCHING_LIVE",
        risk_snapshot_json=snapshot,
    )


def _paper_settings(monkeypatch) -> None:
    monkeypatch.setattr(dispatch.settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(
        dispatch.settings,
        "chili_alpaca_expected_account_id",
        _ACCOUNT_ID,
        raising=False,
    )
    monkeypatch.setattr(
        dispatch.settings,
        "chili_momentum_first_dip_reclaim_policy_mode",
        "candidate",
        raising=False,
    )
    monkeypatch.setattr(
        live_runner.settings,
        "chili_momentum_live_runner_enabled",
        True,
        raising=False,
    )


def _runtime(handler) -> dispatch.CapturedPaperRuntime:
    return dispatch.CapturedPaperRuntime(
        handler=handler,
        expected_account_id=_ACCOUNT_ID,
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        capture_receipt_sha256="c" * 64,
        runtime_generation=_GENERATION,
        first_dip_policy_mode="candidate",
    )


def test_exact_bind_is_content_addressed_and_idempotent(monkeypatch) -> None:
    _paper_settings(monkeypatch)
    request = _request()
    row = _session()
    db = _Db(row)
    modified: list[tuple[object, str]] = []
    monkeypatch.setattr(
        dispatch,
        "flag_modified",
        lambda entity, name: modified.append((entity, name)),
    )
    lock_identity = AdaptiveRiskAccountLockIdentity.for_scope("alpaca:paper")

    with (
        selection.require_captured_paper_selection(request),
        dispatch._activate_session_owner_request(request),
    ):
        first = dispatch.bind_captured_paper_session_owner(
            db,
            request=request,
            account_lock_identity=lock_identity,
        )
        second = dispatch.bind_captured_paper_session_owner(
            db,
            request=request,
            account_lock_identity=lock_identity,
        )

    assert first == second == dispatch.captured_paper_session_owner_marker(request)
    assert first["schema_version"] == "chili.captured-paper-session-owner.v1"
    assert first["account_scope"] == "alpaca:paper"
    assert first["expected_account_id"] == _ACCOUNT_ID
    assert first["runtime_generation"] == _GENERATION
    assert first["execution_family"] == "alpaca_spot"
    assert first["route_token_sha256"] == request.route_token.route_token_sha256
    assert len(first["content_sha256"]) == 64
    assert db.flush_calls == 1
    assert modified == [(row, "risk_snapshot_json")]
    assert [query.for_update_calls for query in db.queries] == [[{}], [{}]]


def test_owner_marker_tamper_rejects_before_captured_handler(monkeypatch) -> None:
    _paper_settings(monkeypatch)
    request = _request()
    marker = dispatch.captured_paper_session_owner_marker(request)
    marker["config_sha256"] = "9" * 64
    row = _session(marker=marker)
    handled: list[str] = []

    with dispatch.register_captured_paper_runtime(
        _runtime(lambda *_args: handled.append("handled"))
    ):
        with pytest.raises(
            dispatch.CapturedPaperRuntimeUnavailableError,
            match="captured_paper_session_owner_marker_hash_mismatch",
        ):
            dispatch.dispatch_live_runner_tick(_Db(row), 41)

    assert handled == []


def test_bare_tick_on_owned_session_has_zero_broker_or_session_mutation(
    monkeypatch,
) -> None:
    _paper_settings(monkeypatch)
    marker = dispatch.captured_paper_session_owner_marker(_request())
    row = _session(marker=marker)
    before = deepcopy(row.risk_snapshot_json)
    db = _Db(row)
    adapter_calls: list[str] = []

    result = live_runner.tick_live_session(
        db,
        41,
        adapter_factory=lambda: adapter_calls.append("adapter") or object(),
    )

    assert result == {
        "ok": True,
        "blocked": True,
        "reason": "captured_paper_session_owned_by_isolated_runtime",
        "broker_calls": 0,
        "session_mutations": 0,
        "order_posted": False,
    }
    assert adapter_calls == []
    assert db.flush_calls == 0
    assert row.risk_snapshot_json == before
    assert db.queries[0].for_update_calls == [{"nowait": True}]


def test_runtime_request_without_selection_or_observation_context_is_blocked(
    monkeypatch,
) -> None:
    _paper_settings(monkeypatch)
    request = _request()
    row = _session(marker=dispatch.captured_paper_session_owner_marker(request))
    db = _Db(row)
    adapter_calls: list[str] = []

    def handler(owned_db, exact_request):
        return live_runner.tick_live_session(
            owned_db,
            exact_request.session_id,
            adapter_factory=lambda: adapter_calls.append("adapter") or object(),
        )

    with dispatch.register_captured_paper_runtime(_runtime(handler)):
        result = dispatch.dispatch_live_runner_tick(db, 41)

    assert result["reason"] == "captured_paper_session_owner_decision_context_missing"
    assert result["broker_calls"] == 0
    assert result["session_mutations"] == 0
    assert adapter_calls == []
    assert db.flush_calls == 0


def test_exact_captured_dispatch_context_passes_owner_gate(monkeypatch) -> None:
    _paper_settings(monkeypatch)
    request = _request()
    row = _session(marker=dispatch.captured_paper_session_owner_marker(request))
    db = _Db(row)
    adapter_calls: list[str] = []
    reached: list[str] = []

    # The production host installs a typed selection context that is already
    # matched to the dispatch request.  This focused gate test substitutes only
    # that independently tested predicate, then stops at the next runner fence.
    monkeypatch.setattr(
        selection,
        "captured_paper_selection_context_active",
        lambda *, execution_family: execution_family == "alpaca_spot",
    )
    monkeypatch.setattr(
        live_runner,
        "_alpaca_execution_quarantine_reason",
        lambda _sess: reached.append("after_owner_gate") or "test_stop_after_owner_gate",
    )

    def handler(owned_db, exact_request):
        assert exact_request.provenance_sha256 == request.provenance_sha256
        return live_runner.tick_live_session(
            owned_db,
            exact_request.session_id,
            adapter_factory=lambda: adapter_calls.append("adapter") or object(),
        )

    with dispatch.register_captured_paper_runtime(_runtime(handler)):
        result = dispatch.dispatch_live_runner_tick(db, 41)

    assert result["skipped"] == "test_stop_after_owner_gate"
    assert result["broker_calls"] == 0
    assert reached == ["after_owner_gate"]
    assert adapter_calls == []
    assert db.flush_calls == 0


def test_valid_foreign_owner_marker_rejects_current_runtime_request(monkeypatch) -> None:
    _paper_settings(monkeypatch)
    foreign_request = dispatch.CapturedPaperDispatchRequest(
        session_id=41,
        symbol="ACTU",
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        expected_account_id=_ACCOUNT_ID,
        code_build_sha256="a" * 64,
        config_sha256="9" * 64,
        capture_receipt_sha256="c" * 64,
        runtime_generation=_GENERATION,
        first_dip_policy_mode="candidate",
    )
    row = _session(
        marker=dispatch.captured_paper_session_owner_marker(foreign_request)
    )
    handled: list[str] = []

    with dispatch.register_captured_paper_runtime(
        _runtime(lambda *_args: handled.append("handled"))
    ):
        with pytest.raises(
            dispatch.CapturedPaperRuntimeUnavailableError,
            match="captured_paper_session_owner_request_mismatch",
        ):
            dispatch.dispatch_live_runner_tick(_Db(row), 41)

    assert handled == []


def test_bind_rejects_without_canonical_account_lock_identity(monkeypatch) -> None:
    _paper_settings(monkeypatch)
    request = _request()
    row = _session()
    db = _Db(row)

    with (
        selection.require_captured_paper_selection(request),
        dispatch._activate_session_owner_request(request),
    ):
        with pytest.raises(
            dispatch.CapturedPaperRuntimeUnavailableError,
            match="captured_paper_session_owner_account_lock_missing",
        ):
            dispatch.bind_captured_paper_session_owner(
                db,
                request=request,
                account_lock_identity=object(),
            )

    assert db.queries == []
    assert db.flush_calls == 0
    assert "captured_paper_session_owner" not in row.risk_snapshot_json
