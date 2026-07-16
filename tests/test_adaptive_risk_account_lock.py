from __future__ import annotations

import inspect
import uuid

import pytest

from app.services.trading.momentum_neural import adaptive_risk_account_lock as lock_mod


def _check(condition: bool, message: str) -> None:
    if not condition:
        pytest.fail(message)


class _RecordingSession:
    def __init__(self, *, in_transaction: bool = True, fail_on_call: int | None = None):
        self._in_transaction = in_transaction
        self._fail_on_call = fail_on_call
        self.calls: list[tuple[str, dict[str, object]]] = []

    def in_transaction(self) -> bool:
        return self._in_transaction

    def execute(self, statement, parameters):
        self.calls.append((str(statement), dict(parameters)))
        if self._fail_on_call == len(self.calls):
            raise RuntimeError("database lock failure")
        return object()

    def commit(self):  # pragma: no cover - a regression would fail immediately
        pytest.fail("lock helper must not commit")

    def rollback(self):  # pragma: no cover - a regression would fail immediately
        pytest.fail("lock helper must not roll back")

    def begin(self):  # pragma: no cover - a regression would fail immediately
        pytest.fail("lock helper must not create a transaction")

    def begin_nested(self):  # pragma: no cover - a regression would fail immediately
        pytest.fail("lock helper must not create a savepoint")


def test_action_lock_key_is_stable_and_matches_legacy_derivation() -> None:
    from app.services.trading.momentum_neural.alpaca_orphan_claims import (
        alpaca_account_risk_lock_key,
    )

    expected = {
        "alpaca:paper": -1189378743262703527,
        "alpaca:live": -6920285956500205138,
    }
    for scope, key in expected.items():
        actual = lock_mod.alpaca_action_account_lock_key(scope)
        _check(actual == key, f"unexpected stable key for {scope}: {actual}")
        _check(
            actual == alpaca_account_risk_lock_key(scope),
            f"new key differs from legacy action key for {scope}",
        )


@pytest.mark.parametrize(
    "scope",
    [None, "", "  ", " Alpaca:Paper ", "alpaca:Paper", "alpaca:\x7fpaper"],
)
def test_account_scope_aliases_fail_instead_of_splitting_lock_domains(scope) -> None:
    with pytest.raises(lock_mod.AccountRiskLockContractError):
        lock_mod.AdaptiveRiskAccountLockIdentity.for_scope(scope)  # type: ignore[arg-type]


def test_helper_acquires_action_then_adaptive_lock_in_caller_transaction() -> None:
    session = _RecordingSession()
    identity = lock_mod.acquire_adaptive_risk_account_locks(
        session,  # type: ignore[arg-type]
        account_scope="alpaca:paper",
    )

    expected_calls = [
        (
            "SELECT pg_advisory_xact_lock(:key)",
            {"key": -1189378743262703527},
        ),
        (
            "SELECT pg_advisory_xact_lock(:namespace, hashtext(:account_scope))",
            {"namespace": 0x4152, "account_scope": "alpaca:paper"},
        ),
    ]
    _check(session.calls == expected_calls, f"unexpected SQL order: {session.calls}")
    _check(identity.account_scope == "alpaca:paper", "scope identity changed")
    _check(
        identity.adaptive_advisory_namespace == 0x4152,
        "adaptive namespace differs from legacy AR namespace",
    )


def test_helper_requires_outer_transaction_before_any_sql() -> None:
    session = _RecordingSession(in_transaction=False)
    with pytest.raises(lock_mod.AccountRiskLockContractError):
        lock_mod.acquire_adaptive_risk_account_locks(
            session,  # type: ignore[arg-type]
            account_scope="alpaca:paper",
        )
    _check(session.calls == [], "helper executed SQL outside a caller transaction")


def test_second_lock_failure_propagates_without_transaction_management() -> None:
    session = _RecordingSession(fail_on_call=2)
    with pytest.raises(RuntimeError, match="database lock failure"):
        lock_mod.acquire_adaptive_risk_account_locks(
            session,  # type: ignore[arg-type]
            account_scope="alpaca:paper",
        )
    _check(len(session.calls) == 2, "helper did not stop at the failed adaptive lock")


def test_row_lock_metadata_matches_reviewed_cycle_order() -> None:
    actual = [
        (item.stage.value, item.ordinal, item.stable_order)
        for item in lock_mod.CANONICAL_ACCOUNT_RISK_ROW_LOCK_ORDER
    ]
    expected = [
        ("account_settlement_head", 1, ("account_scope",)),
        ("adaptive_risk_reservation", 2, ("reservation_id_uuid",)),
        (
            "fill_activity_or_cycle_settlement",
            3,
            ("terminal_sequence", "event_sequence"),
        ),
        ("broker_symbol_action_claim", 4, ("symbol",)),
        ("trading_automation_session", 5, ("session_id",)),
        (
            "adaptive_risk_opportunity_claim",
            6,
            ("account_scope", "symbol", "trading_date", "setup_family", "id"),
        ),
    ]
    _check(actual == expected, f"canonical row-lock order changed: {actual}")


def test_row_lock_guard_accepts_skipped_stages_and_stable_same_stage_order() -> None:
    guard = lock_mod.CanonicalAccountRiskRowLockGuard()
    first = uuid.UUID("00000000-0000-0000-0000-000000000001")
    second = uuid.UUID("00000000-0000-0000-0000-000000000002")

    guard.observe(
        lock_mod.AccountRiskRowLockStage.ACCOUNT_SETTLEMENT_HEAD,
        sort_key=("alpaca:paper",),
    )
    guard.observe(
        lock_mod.AccountRiskRowLockStage.ADAPTIVE_RESERVATION,
        sort_key=(first,),
    )
    guard.observe(
        lock_mod.AccountRiskRowLockStage.ADAPTIVE_RESERVATION,
        sort_key=(second,),
    )
    guard.observe(
        lock_mod.AccountRiskRowLockStage.ACTION_CLAIM,
        sort_key=("AAPL",),
    )
    _check(
        guard.last_stage is lock_mod.AccountRiskRowLockStage.ACTION_CLAIM,
        "guard did not retain the last canonical stage",
    )


def test_row_lock_guard_rejects_stage_inversion() -> None:
    guard = lock_mod.CanonicalAccountRiskRowLockGuard()
    guard.observe(
        lock_mod.AccountRiskRowLockStage.ACTION_CLAIM,
        sort_key=("AAPL",),
    )
    with pytest.raises(lock_mod.AccountRiskLockContractError, match="stage inversion"):
        guard.observe(
            lock_mod.AccountRiskRowLockStage.ADAPTIVE_RESERVATION,
            sort_key=(uuid.uuid4(),),
        )


@pytest.mark.parametrize("sort_key", ["AAPL", ("AAPL", "unexpected"), (["AAPL"],)])
def test_row_lock_guard_requires_canonical_key_shape(sort_key) -> None:
    guard = lock_mod.CanonicalAccountRiskRowLockGuard()
    with pytest.raises(lock_mod.AccountRiskLockContractError):
        guard.observe(
            lock_mod.AccountRiskRowLockStage.ACTION_CLAIM,
            sort_key=sort_key,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("next_key", [(2,), (1,)])
def test_row_lock_guard_rejects_duplicate_or_descending_same_stage(next_key) -> None:
    guard = lock_mod.CanonicalAccountRiskRowLockGuard()
    guard.observe(
        lock_mod.AccountRiskRowLockStage.AUTOMATION_SESSION,
        sort_key=(2,),
    )
    with pytest.raises(lock_mod.AccountRiskLockContractError, match="strictly increasing"):
        guard.observe(
            lock_mod.AccountRiskRowLockStage.AUTOMATION_SESSION,
            sort_key=next_key,
        )


def test_lock_helper_source_has_no_transaction_or_external_io_ownership() -> None:
    source = inspect.getsource(lock_mod.acquire_adaptive_risk_account_locks)
    forbidden = (
        ".commit(",
        ".rollback(",
        ".begin(",
        ".begin_nested(",
        "requests.",
        "httpx.",
        "urllib.",
    )
    found = [token for token in forbidden if token in source]
    _check(found == [], f"lock helper acquired forbidden ownership/I/O: {found}")
