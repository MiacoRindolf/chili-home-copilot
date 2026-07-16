from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import inspect
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import (
    captured_paper_pending_owner as pending,
)
from app.services.trading.momentum_neural import (
    captured_paper_preowner_promotion as promotion,
)
from app.services.trading.momentum_neural.adaptive_risk_account_lock import (
    AdaptiveRiskAccountLockIdentity,
)
from app.services.trading.momentum_neural import (
    captured_paper_initial_admission as initial,
)
from tests.test_captured_paper_preowner_promotion import _pure_projection


def _pending_session(*, snapshot=None, **overrides):
    material, receipt, _request, projection = _pure_projection()
    values = {
        "id": receipt.session_id,
        "mode": "live",
        "venue": "alpaca",
        "state": promotion.CAPTURED_PAPER_PENDING_OWNER_STATE,
        "symbol": material.symbol,
        "variant_id": material.variant_id,
        "user_id": material.user_id,
        "execution_family": material.execution_family,
        "correlation_id": material.material_sha256,
        "source_node_id": "captured_paper_preowner_promotion",
        "ended_at": None,
        "allocation_decision_json": {},
        "risk_snapshot_json": (
            promotion._canonical_value(projection.risk_snapshot)
            if snapshot is None
            else snapshot
        ),
    }
    values.update(overrides)
    return (
        material,
        receipt,
        _request,
        projection,
        SimpleNamespace(**values),
    )


def _validate(session, material, *, account_id=None, generation=None, family=None):
    return pending.validate_captured_paper_pending_owner_inventory(
        session,
        expected_account_id=(
            material.expected_account_id if account_id is None else account_id
        ),
        expected_runtime_generation=(
            material.runtime_generation if generation is None else generation
        ),
        expected_execution_family=(
            material.execution_family if family is None else family
        ),
    )


def test_exact_pending_projection_reconstructs_all_typed_authority():
    material, receipt, request, projection, session = _pending_session()

    validated = _validate(session, material)

    assert validated.session_id == receipt.session_id
    assert validated.material == material
    assert validated.material.to_dict() == material.to_dict()
    assert validated.request == request
    assert validated.request.provenance_sha256 == request.provenance_sha256
    assert validated.projection == projection
    assert promotion._canonical_value(
        validated.projection.risk_snapshot
    ) == promotion._canonical_value(projection.risk_snapshot)


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ("material", "pending_owner_initial_material"),
        ("marker", "pending_owner_projection_mismatch"),
        ("dispatch", "pending_owner_dispatch_request"),
        ("extra_snapshot", "pending_owner_projection_mismatch"),
    ],
)
def test_pending_inventory_rejects_material_marker_dispatch_or_snapshot_drift(
    tamper,
    reason,
):
    material, _receipt, _request, _projection, session = _pending_session()
    snapshot = deepcopy(session.risk_snapshot_json)
    if tamper == "material":
        snapshot[promotion.CAPTURED_PAPER_INITIAL_MATERIAL_KEY][
            "policy_sha256"
        ] = "0" * 64
    elif tamper == "marker":
        snapshot[promotion.CAPTURED_PAPER_PENDING_OWNER_KEY][
            "content_sha256"
        ] = "0" * 64
    elif tamper == "dispatch":
        snapshot[promotion.CAPTURED_PAPER_PENDING_OWNER_KEY][
            "dispatch_request"
        ]["first_dip_policy_mode"] = "baseline"
    else:
        snapshot["unsealed_extra_authority"] = True
    session.risk_snapshot_json = snapshot

    with pytest.raises(pending.CapturedPaperPendingOwnerError, match=reason):
        _validate(session, material)


@pytest.mark.parametrize(
    ("field", "foreign"),
    [
        ("account_id", "77777777-7777-4777-8777-777777777777"),
        ("generation", "88888888-8888-4888-8888-888888888888"),
        ("family", "alpaca_crypto"),
    ],
)
def test_pending_inventory_rejects_foreign_runtime_scope(field, foreign):
    material, _receipt, _request, _projection, session = _pending_session()
    kwargs = {field: foreign}

    with pytest.raises(
        pending.CapturedPaperPendingOwnerError,
        match="pending_owner_inventory_scope_mismatch",
    ):
        _validate(session, material, **kwargs)


@pytest.mark.parametrize(
    ("row_field", "foreign_value"),
    [
        ("mode", "paper"),
        ("venue", "simulated"),
        ("state", "running"),
        ("symbol", "EVIL"),
        ("execution_family", "alpaca_crypto"),
        ("source_node_id", "foreign_promoter"),
        ("ended_at", "2026-07-16T12:00:01Z"),
        ("allocation_decision_json", {"already": "allocated"}),
    ],
)
def test_pending_inventory_rejects_foreign_or_already_mutated_row(
    row_field,
    foreign_value,
):
    material, _receipt, _request, _projection, session = _pending_session(
        **{row_field: foreign_value}
    )

    with pytest.raises(
        pending.CapturedPaperPendingOwnerError,
        match="pending_owner_inventory_scope_mismatch",
    ):
        _validate(session, material)


@pytest.mark.parametrize(
    ("row_field", "coerced_value"),
    [
        ("id", "41"),
        ("variant_id", "1"),
        ("user_id", "1"),
        ("id", True),
        ("variant_id", True),
        ("user_id", True),
    ],
)
def test_pending_inventory_never_coerces_durable_integer_identity(
    row_field,
    coerced_value,
):
    material, _receipt, _request, _projection, session = _pending_session(
        **{row_field: coerced_value}
    )

    with pytest.raises(
        pending.CapturedPaperPendingOwnerError,
        match="pending_owner_session_invalid",
    ):
        _validate(session, material)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (
            (
                "captured_paper_initial_runner_risk_template",
                "payload",
                "momentum_policy_caps",
                "cooldown_after_stopout_seconds",
            ),
            83.0,
        ),
        (
            (
                promotion.CAPTURED_PAPER_PENDING_OWNER_KEY,
                "risk_reserved",
            ),
            0,
        ),
    ],
)
def test_pending_inventory_rejects_json_numeric_type_aliases(path, replacement):
    material, _receipt, _request, _projection, session = _pending_session()
    snapshot = deepcopy(session.risk_snapshot_json)
    target = snapshot
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    session.risk_snapshot_json = snapshot

    with pytest.raises(
        pending.CapturedPaperPendingOwnerError,
        match="pending_owner_projection_mismatch",
    ):
        _validate(session, material)


def test_final_owner_is_never_reclassified_as_pending():
    material, _receipt, _request, _projection, session = _pending_session()
    snapshot = deepcopy(session.risk_snapshot_json)
    snapshot["captured_paper_session_owner"] = {
        "content_sha256": "0" * 64,
    }
    session.risk_snapshot_json = snapshot

    with pytest.raises(
        pending.CapturedPaperPendingOwnerError,
        match="pending_owner_already_final",
    ):
        _validate(session, material)


def test_pending_owner_source_has_no_market_broker_risk_or_order_side_effects():
    source = inspect.getsource(pending)
    forbidden = (
        "requests.",
        "httpx.",
        "get_full_market_snapshot(",
        "fetch_ohlcv",
        "AlpacaSpotAdapter(",
        "post_limit_buy(",
        "post_limit_sell(",
        "submit_order(",
        "reserve_adaptive_risk(",
        "claim_adaptive_risk_opportunity(",
        "commit_captured_paper_outbox(",
        "captured_paper_entry_transport(",
    )
    assert all(value not in source for value in forbidden)


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _FakePendingQuery:
    def __init__(self, db):
        self._db = db

    def filter(self, *unused):
        return self

    def with_for_update(self):
        self._db.session_row_locks += 1
        return self

    def populate_existing(self):
        self._db.populate_existing_reads += 1
        return self

    def one_or_none(self):
        return self._db.session


class _FakePendingDb:
    def __init__(self, session, *, now, in_transaction=True):
        self.session = session
        self.now = now
        self._in_transaction = in_transaction
        self.session_row_locks = 0
        self.populate_existing_reads = 0
        self.query_calls = 0
        self.clock_reads = 0

    def in_transaction(self):
        return self._in_transaction

    def query(self, unused_model):
        self.query_calls += 1
        return _FakePendingQuery(self)

    def execute(self, unused_statement):
        self.clock_reads += 1
        return _FakeScalarResult(self.now)


def _pending_claim(material, receipt, projection):
    return {
        "account_scope": material.account_scope,
        "symbol": material.symbol,
        "claim_token": projection.arm.symbol_claim_token,
        "action": "entry",
        "phase": "claimed",
        "owner_session_id": receipt.session_id,
        "client_order_id": None,
        "broker_order_id": None,
        "resolved_at": None,
        "metadata": promotion._canonical_value(
            projection.action_claim_metadata
        ),
        "lease_expires_at": material.expires_at + timedelta(seconds=1),
    }


def _account_lock_identity():
    return AdaptiveRiskAccountLockIdentity.for_scope(
        initial.ALPACA_PAPER_ACCOUNT_SCOPE
    )


def test_activation_revalidates_locked_pending_authority_and_fence_before_bind(
    monkeypatch,
):
    material, receipt, request, projection, session = _pending_session()
    db = _FakePendingDb(
        session,
        now=material.decision_at + timedelta(milliseconds=2),
    )
    claim = _pending_claim(material, receipt, projection)
    reads = []
    binds = []
    fence_checks = 0

    def read_claim(_db, *, symbol, account_scope, for_update):
        reads.append((symbol, account_scope, for_update))
        return True, deepcopy(claim)

    def assert_fence():
        nonlocal fence_checks
        fence_checks += 1

    def bind(_db, *, request, account_lock_identity):
        assert fence_checks == 2
        assert db.session_row_locks == 1
        binds.append((request.provenance_sha256, account_lock_identity))
        return {"content_sha256": "f" * 64}

    monkeypatch.setattr(pending, "read_action_claim", read_claim)
    monkeypatch.setattr(pending, "bind_captured_paper_session_owner", bind)

    activated = pending.activate_captured_paper_session_owner_before_tick(
        db,
        request=request,
        account_lock_identity=_account_lock_identity(),
        assert_service_fence_held=assert_fence,
    )

    assert reads == [(material.symbol, material.account_scope, True)]
    assert fence_checks == 2
    assert db.query_calls == 2
    assert db.session_row_locks == 1
    assert db.populate_existing_reads == 2
    assert db.clock_reads == 1
    assert binds == [(request.provenance_sha256, _account_lock_identity())]
    assert activated.session_id == receipt.session_id
    assert activated.initial_material_sha256 == material.material_sha256
    assert activated.created_from_pending is True
    assert activated.owner_marker == {"content_sha256": "f" * 64}


def test_activation_lost_service_fence_after_locks_never_binds(monkeypatch):
    material, receipt, request, projection, session = _pending_session()
    db = _FakePendingDb(
        session,
        now=material.decision_at + timedelta(milliseconds=2),
    )
    monkeypatch.setattr(
        pending,
        "read_action_claim",
        lambda *args, **kwargs: (
            True,
            _pending_claim(material, receipt, projection),
        ),
    )
    binds = []
    fence_checks = 0

    def lose_second_fence():
        nonlocal fence_checks
        fence_checks += 1
        if fence_checks == 2:
            raise RuntimeError("fence lost")

    monkeypatch.setattr(
        pending,
        "bind_captured_paper_session_owner",
        lambda *args, **kwargs: binds.append((args, kwargs)),
    )

    with pytest.raises(
        pending.CapturedPaperPendingOwnerError,
        match="pending_owner_activation_service_fence_lost",
    ):
        pending.activate_captured_paper_session_owner_before_tick(
            db,
            request=request,
            account_lock_identity=_account_lock_identity(),
            assert_service_fence_held=lose_second_fence,
        )

    assert fence_checks == 2
    assert db.session_row_locks == 1
    assert db.clock_reads == 1
    assert binds == []


def test_activation_rejects_action_claim_json_numeric_alias_before_bind(
    monkeypatch,
):
    material, receipt, request, projection, session = _pending_session()
    db = _FakePendingDb(
        session,
        now=material.decision_at + timedelta(milliseconds=2),
    )
    claim = _pending_claim(material, receipt, projection)
    claim["metadata"]["variant_id"] = True
    monkeypatch.setattr(
        pending,
        "read_action_claim",
        lambda *args, **kwargs: (True, claim),
    )
    binds = []
    monkeypatch.setattr(
        pending,
        "bind_captured_paper_session_owner",
        lambda *args, **kwargs: binds.append((args, kwargs)),
    )

    with pytest.raises(
        pending.CapturedPaperPendingOwnerError,
        match="pending_owner_activation_action_claim_mismatch",
    ):
        pending.activate_captured_paper_session_owner_before_tick(
            db,
            request=request,
            account_lock_identity=_account_lock_identity(),
            assert_service_fence_held=lambda: None,
        )

    assert db.session_row_locks == 1
    assert db.populate_existing_reads == 2
    assert db.clock_reads == 0
    assert binds == []


def test_activation_expired_material_never_reaches_final_fence_or_bind(
    monkeypatch,
):
    material, receipt, request, projection, session = _pending_session()
    db = _FakePendingDb(session, now=material.expires_at)
    monkeypatch.setattr(
        pending,
        "read_action_claim",
        lambda *args, **kwargs: (
            True,
            _pending_claim(material, receipt, projection),
        ),
    )
    binds = []
    fence_checks = 0

    def assert_fence():
        nonlocal fence_checks
        fence_checks += 1

    monkeypatch.setattr(
        pending,
        "bind_captured_paper_session_owner",
        lambda *args, **kwargs: binds.append((args, kwargs)),
    )

    with pytest.raises(
        pending.CapturedPaperPendingOwnerError,
        match="pending_owner_activation_authority_expired",
    ):
        pending.activate_captured_paper_session_owner_before_tick(
            db,
            request=request,
            account_lock_identity=_account_lock_identity(),
            assert_service_fence_held=assert_fence,
        )

    assert fence_checks == 1
    assert db.clock_reads == 1
    assert binds == []


def test_activation_requires_outer_transaction_before_any_fence_or_db_read():
    material, _receipt, request, _projection, session = _pending_session()
    db = _FakePendingDb(
        session,
        now=material.decision_at,
        in_transaction=False,
    )
    fence_checks = []

    with pytest.raises(
        pending.CapturedPaperPendingOwnerError,
        match="pending_owner_activation_transaction_missing",
    ):
        pending.activate_captured_paper_session_owner_before_tick(
            db,
            request=request,
            account_lock_identity=_account_lock_identity(),
            assert_service_fence_held=lambda: fence_checks.append(True),
        )

    assert fence_checks == []
    assert db.query_calls == 0
    assert db.clock_reads == 0
