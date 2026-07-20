from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import getpass
import hashlib
import json
from pathlib import Path
import runpy
import socket
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Mapping

import pytest

from scripts import captured_paper_activation_runner as activation_runner
from scripts import run_captured_paper_operator_chain as chain


ACCOUNT_ID = "11111111-2222-4333-8444-555555555555"
NOW = datetime(2026, 7, 18, 19, 0, tzinfo=UTC)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write(path: Path, raw: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return _sha(raw)


@dataclass(frozen=True)
class ChainFixture:
    activation: activation_runner.ActivationRunnerRequest
    document: dict[str, Any]
    root: Path
    benchmark: Path

    def publish(
        self,
        *,
        document: Mapping[str, Any] | None = None,
        raw: bytes | None = None,
    ) -> tuple[activation_runner.ActivationRunnerRequest, Path, str]:
        content = raw if raw is not None else _canonical(document or self.document)
        path = self.root / "authority" / "operator-chain-request.json"
        digest = _write(path, content)
        return (
            replace(
                self.activation,
                chain_request_path=path.resolve(strict=True),
                chain_request_sha256=digest,
            ),
            path,
            digest,
        )


@pytest.fixture
def chain_fixture(tmp_path: Path) -> ChainFixture:
    root = tmp_path / "allowed"
    candidate = root / "candidate"
    artifacts = root / "artifacts"
    legacy = root / "legacy"
    dependencies = root / "dependencies"
    for path in (candidate, artifacts, legacy, dependencies):
        path.mkdir(parents=True)

    pinned: dict[str, Path] = {}
    for name in (
        "git",
        "python",
        "powershell",
        "schtasks",
        "stage0",
        "chain-script",
        "chain-request",
        "finalizer",
        "cutover",
        "runtime-env",
    ):
        path = root / "authority" / name
        _write(path, f"pinned:{name}\n".encode())
        pinned[name] = path

    benchmark = root / "benchmarks" / "measured.json"
    benchmark_sha = _write(
        benchmark,
        _canonical({"schema_version": "test.resource-benchmark.v1", "measured": True}),
    )
    activation_path = root / "authority" / "activation-request.json"
    request_sha = _write(activation_path, b"outer activation authority\n")
    timeouts = activation_runner.RunnerTimeouts(
        chain=10,
        no_order_smoke=10,
        finalize=10,
        validate_only=10,
        apply=10,
        rollback=10,
        task_query=10,
    )
    activation = activation_runner.ActivationRunnerRequest(
        request_path=activation_path.resolve(strict=True),
        request_sha256=request_sha,
        candidate_root=candidate.resolve(strict=True),
        expected_git_commit="a" * 40,
        git_executable=pinned["git"].resolve(strict=True),
        git_executable_sha256=_sha(pinned["git"].read_bytes()),
        python_executable=pinned["python"].resolve(strict=True),
        python_executable_sha256=_sha(pinned["python"].read_bytes()),
        powershell_executable=pinned["powershell"].resolve(strict=True),
        powershell_executable_sha256=_sha(pinned["powershell"].read_bytes()),
        schtasks_executable=pinned["schtasks"].resolve(strict=True),
        schtasks_executable_sha256=_sha(pinned["schtasks"].read_bytes()),
        bootstrap_stage0_script=pinned["stage0"].resolve(strict=True),
        bootstrap_stage0_script_sha256=_sha(pinned["stage0"].read_bytes()),
        chain_script=pinned["chain-script"].resolve(strict=True),
        chain_script_sha256=_sha(pinned["chain-script"].read_bytes()),
        chain_request_path=pinned["chain-request"].resolve(strict=True),
        chain_request_sha256=_sha(pinned["chain-request"].read_bytes()),
        finalizer_script=pinned["finalizer"].resolve(strict=True),
        finalizer_script_sha256=_sha(pinned["finalizer"].read_bytes()),
        cutover_script=pinned["cutover"].resolve(strict=True),
        cutover_script_sha256=_sha(pinned["cutover"].read_bytes()),
        python_dependency_root=dependencies.resolve(strict=True),
        python_dependency_root_identity_sha256="d" * 64,
        runtime_env_path=pinned["runtime-env"].resolve(strict=True),
        runtime_env_sha256=_sha(pinned["runtime-env"].read_bytes()),
        artifact_root=artifacts.resolve(strict=True),
        expected_account_id=ACCOUNT_ID,
        test_database_name="captured_paper_test",
        allowed_read_roots=(str(root.resolve(strict=True)),),
        timeouts=timeouts,
    )
    document = {
        "schema_version": chain.CHAIN_REQUEST_SCHEMA_VERSION,
        "account_scope": chain.ACCOUNT_SCOPE,
        "live_cash_authorized": False,
        "resource_benchmark": {
            "path": str(benchmark.resolve(strict=True)),
            "sha256": benchmark_sha,
        },
        "legacy_root": str(legacy.resolve(strict=True)),
        "python_dependency_root": str(dependencies.resolve(strict=True)),
        "python_dependency_root_identity_sha256": "d" * 64,
        "bootstrap_stage0_script": str(pinned["stage0"].resolve(strict=True)),
        "bootstrap_stage0_script_sha256": _sha(pinned["stage0"].read_bytes()),
        "host_principal_user_id": getpass.getuser(),
        "bridge_configuration": {
            "iqfeed_l1": {"transport": "loopback", "capture": "exact-print"},
            "iqfeed_l2": {"transport": "loopback", "capture": "hot-symbol-delta"},
        },
    }
    return ChainFixture(
        activation=activation,
        document=document,
        root=root.resolve(strict=True),
        benchmark=benchmark.resolve(strict=True),
    )


def test_import_is_inert_and_does_not_touch_network_db_broker_or_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("import attempted external I/O")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(
        activation_runner, "load_activation_runner_request", forbidden
    )
    monkeypatch.setattr(
        chain.bootstrap,
        "build_iqfeed_capture_bootstrap_bundle_from_request",
        forbidden,
    )
    monkeypatch.setattr(chain.host_snapshot, "collect_host_snapshot", forbidden)
    monkeypatch.setattr(
        chain.operator_flow, "run_captured_paper_operator_flow", forbidden
    )
    monkeypatch.chdir(tmp_path)
    before = tuple(tmp_path.rglob("*"))

    namespace = runpy.run_path(
        str(Path(chain.__file__).resolve(strict=True)),
        run_name="captured_paper_operator_chain_import_probe",
    )

    assert callable(namespace["main"])
    assert tuple(tmp_path.rglob("*")) == before
    assert capsys.readouterr().out == ""


def test_chain_request_is_canonical_hash_bound_and_pinned_by_outer_request(
    chain_fixture: ChainFixture,
) -> None:
    activation, path, digest = chain_fixture.publish()

    loaded = chain._load_chain_request(
        request_path=path,
        request_sha256=digest,
        activation_request=activation,
    )

    assert loaded == chain_fixture.document
    with pytest.raises(
        chain.CapturedPaperOperatorChainError,
        match="CHAIN_REQUEST_REFERENCE_INVALID",
    ):
        chain._load_chain_request(
            request_path=path,
            request_sha256="f" * 64,
            activation_request=activation,
        )


def test_chain_request_rejects_noncanonical_and_duplicate_json(
    chain_fixture: ChainFixture,
) -> None:
    pretty = json.dumps(chain_fixture.document, indent=2).encode("utf-8")
    activation, path, digest = chain_fixture.publish(raw=pretty)
    with pytest.raises(
        chain.CapturedPaperOperatorChainError,
        match="CHAIN_REQUEST_SCHEMA_INVALID",
    ):
        chain._load_chain_request(
            request_path=path,
            request_sha256=digest,
            activation_request=activation,
        )

    duplicate = b'{"schema_version":"a","schema_version":"b"}'
    activation, path, digest = chain_fixture.publish(raw=duplicate)
    with pytest.raises(
        chain.CapturedPaperOperatorChainError,
        match="JSON_DUPLICATE_KEY",
    ):
        chain._load_chain_request(
            request_path=path,
            request_sha256=digest,
            activation_request=activation,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (("account_scope", "alpaca:live"), ("live_cash_authorized", True)),
)
def test_chain_request_structurally_rejects_live_cash(
    chain_fixture: ChainFixture,
    field: str,
    value: object,
) -> None:
    document = dict(chain_fixture.document)
    document[field] = value
    activation, path, digest = chain_fixture.publish(document=document)

    with pytest.raises(
        chain.CapturedPaperOperatorChainError,
        match="PAPER_SCOPE_INVALID",
    ):
        chain._load_chain_request(
            request_path=path,
            request_sha256=digest,
            activation_request=activation,
        )


def test_strict_path_enforces_allowlist_and_rejects_reparse_alias(
    chain_fixture: ChainFixture,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.json"
    digest = _write(outside, b"outside")
    with pytest.raises(
        chain.CapturedPaperOperatorChainError,
        match="PATH_OUTSIDE_ALLOWLIST",
    ):
        chain._strict_path(
            outside,
            field="outside",
            roots=(chain_fixture.root,),
            directory=False,
            expected_sha256=digest,
        )

    target = chain_fixture.root / "authority" / "reparse-target.json"
    digest = _write(target, b"reparse target")
    alias = chain_fixture.root / "authority" / "reparse-alias.json"
    try:
        alias.symlink_to(target)
    except OSError:
        pytest.skip("host does not permit creating a symlink for the reparse test")
    with pytest.raises(
        activation_runner.CapturedPaperActivationRunnerError,
        match="REPARSE_PATH",
    ):
        chain._strict_path(
            alias,
            field="reparse_alias",
            roots=(chain_fixture.root,),
            directory=False,
            expected_sha256=digest,
        )


def test_chain_request_rejects_reparse_cli_alias(
    chain_fixture: ChainFixture,
) -> None:
    activation, path, digest = chain_fixture.publish()
    alias = chain_fixture.root / "authority" / "chain-request-alias.json"
    try:
        alias.symlink_to(path)
    except OSError:
        pytest.skip("host does not permit creating a symlink for the reparse test")

    with pytest.raises(
        activation_runner.CapturedPaperActivationRunnerError,
        match="REPARSE_PATH",
    ):
        chain._load_chain_request(
            request_path=alias,
            request_sha256=digest,
            activation_request=activation,
        )


def test_host_principal_must_equal_current_user(
    chain_fixture: ChainFixture,
) -> None:
    document = dict(chain_fixture.document)
    document["host_principal_user_id"] = "different.user"

    with pytest.raises(
        chain.CapturedPaperOperatorChainError,
        match="HOST_PRINCIPAL_MISMATCH",
    ):
        chain.run_operator_chain(
            activation_request=chain_fixture.activation,
            chain_document=document,
        )


class _FakeAccount:
    id = ACCOUNT_ID
    status = "ACTIVE"
    equity = "71868.33"
    last_equity = "72000.00"
    buying_power = "287473.32"
    cash = "71868.33"
    account_blocked = False
    trading_blocked = False
    transfers_blocked = False
    trade_suspended_by_user = False


def _install_fake_alpaca(
    monkeypatch: pytest.MonkeyPatch,
    account: object,
    calls: list[tuple[str, bool]],
) -> None:
    class TradingClient:
        def __init__(self, key: str, secret: str, *, paper: bool) -> None:
            calls.append((f"{key}:{secret}", paper))

        def get_account(self) -> object:
            return account

    alpaca = ModuleType("alpaca")
    trading = ModuleType("alpaca.trading")
    client = ModuleType("alpaca.trading.client")
    client.TradingClient = TradingClient  # type: ignore[attr-defined]
    alpaca.trading = trading  # type: ignore[attr-defined]
    trading.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "alpaca", alpaca)
    monkeypatch.setitem(sys.modules, "alpaca.trading", trading)
    monkeypatch.setitem(sys.modules, "alpaca.trading.client", client)
    monkeypatch.setenv("CHILI_ALPACA_API_KEY", "paper-key")
    monkeypatch.setenv("CHILI_ALPACA_API_SECRET", "paper-secret")


def test_exact_paper_account_read_requires_paper_active_unblocked_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool]] = []
    _install_fake_alpaca(monkeypatch, _FakeAccount(), calls)

    posture, query, requested_at, available_at = chain._read_exact_paper_account(
        expected_account_id=ACCOUNT_ID
    )

    assert calls == [("paper-key:paper-secret", True)]
    assert query["environment"] == "paper"
    assert query["account_id"] == ACCOUNT_ID
    assert posture["status"] == "ACTIVE"
    assert posture["equity"] == "71868.33"
    assert not any(
        posture[name]
        for name in (
            "account_blocked",
            "trading_blocked",
            "transfers_blocked",
            "trade_suspended_by_user",
        )
    )
    assert requested_at.tzinfo is UTC
    assert available_at >= requested_at


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}, "IDENTITY_MISMATCH"),
        ({"status": "INACTIVE"}, "PAPER_ACCOUNT_INACTIVE"),
        ({"trading_blocked": True}, "PAPER_ACCOUNT_BLOCKED"),
        ({"account_blocked": "false"}, "ACCOUNT_POSTURE_INVALID"),
    ),
)
def test_exact_paper_account_fails_closed_on_wrong_or_unsafe_posture(
    monkeypatch: pytest.MonkeyPatch,
    changes: Mapping[str, object],
    reason: str,
) -> None:
    account = SimpleNamespace(
        **{
            name: getattr(_FakeAccount, name)
            for name in (
                "id",
                "status",
                "equity",
                "last_equity",
                "buying_power",
                "cash",
                "account_blocked",
                "trading_blocked",
                "transfers_blocked",
                "trade_suspended_by_user",
            )
        }
    )
    for name, value in changes.items():
        setattr(account, name, value)
    _install_fake_alpaca(monkeypatch, account, [])

    with pytest.raises(chain.CapturedPaperOperatorChainError, match=reason):
        chain._read_exact_paper_account(expected_account_id=ACCOUNT_ID)


class _FakeIqfeedSocket:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames = list(frames)
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, _timeout: float) -> None:
        return None

    def sendall(self, value: bytes) -> None:
        self.sent.append(value)

    def recv(self, _size: int) -> bytes:
        return self.frames.pop(0) if self.frames else b""

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("delay", ["", "0"])
def test_iqfeed_realtime_delay_is_required_and_unsubscribes(
    monkeypatch: pytest.MonkeyPatch,
    delay: str,
) -> None:
    connection = _FakeIqfeedSocket(
        [
            b"S,SERVER CONNECTED\r\n"
            b"S,CUST,real_time,127.0.0.1,5009\r\n"
            b"S,CURRENT UPDATE FIELDNAMES,Symbol,Most Recent Trade,Delay\r\n"
            + f"Q,AAPL,201.25,{delay},\r\n".encode("ascii")
        ]
    )
    monkeypatch.setattr(
        chain.socket,
        "create_connection",
        lambda *_args, **_kwargs: connection,
    )

    assert chain._delay_is_zero("AAPL") is True
    assert b"wAAPL\r\n" in connection.sent
    assert b"rAAPL\r\n" in connection.sent
    assert connection.closed is True


@pytest.mark.parametrize(
    "frames",
    [
        [
            b"S,SERVER CONNECTED\r\n"
            b"S,CUST,real_time,127.0.0.1,5009\r\n"
            b"S,CURRENT UPDATE FIELDNAMES,Symbol,Most Recent Trade,Delay\r\n"
            b"Q,AAPL,201.25,15,\r\n"
        ],
        [
            b"S,SERVER CONNECTED\r\n"
            b"S,CUST,delayed,127.0.0.1,5009\r\n"
            b"S,CURRENT UPDATE FIELDNAMES,Symbol,Most Recent Trade,Delay\r\n"
            b"Q,AAPL,201.25,,\r\n"
        ],
        [b"Q,AAPL,201.25,,\r\n"],
        [
            b"S,SERVER CONNECTED\r\n"
            b"S,CUST,real_time,127.0.0.1,5009\r\n"
            b"S,CURRENT UPDATE FIELDNAMES,Symbol,Most Recent Trade\r\n"
            b"Q,AAPL,201.25,,\r\n"
        ],
        [
            b"S,SERVER CONNECTED\r\n"
            b"S,CUST,real_time,127.0.0.1,5009\r\n"
            b"S,CURRENT UPDATE FIELDNAMES,Symbol,Most Recent Trade,Delay\r\n"
            b"P,AAPL,201.25,,\r\n"
        ],
        [
            b"S,SERVER CONNECTED\r\n"
            b"S,CUST,real_time,127.0.0.1,5009\r\n"
            b"S,CURRENT UPDATE FIELDNAMES,Symbol,Most Recent Trade,Delay\r\n"
            b"Q,AAPL,201.25,",
            b"15,\r\n",
        ],
        [
            b"Q,AAPL,201.25,,\r\n"
            b"S,SERVER CONNECTED\r\n"
            b"S,CUST,real_time,127.0.0.1,5009\r\n"
            b"S,CURRENT UPDATE FIELDNAMES,Symbol,Most Recent Trade,Delay\r\n"
        ],
    ],
)
def test_iqfeed_realtime_delay_fails_closed_without_exact_authority(
    monkeypatch: pytest.MonkeyPatch,
    frames: list[bytes],
) -> None:
    connection = _FakeIqfeedSocket(frames)
    monkeypatch.setattr(
        chain.socket,
        "create_connection",
        lambda *_args, **_kwargs: connection,
    )

    assert chain._delay_is_zero("AAPL") is False
    assert b"rAAPL\r\n" in connection.sent
    assert connection.closed is True


class _Rows:
    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[str, int]]:
        return self.rows


class _Connection:
    def __init__(self, engine: "_Engine") -> None:
        self.engine = engine
        self.rows = engine.rows

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _query: object, _parameters: object = None) -> _Rows:
        self.engine.executions.append((str(_query), _parameters))
        return _Rows(self.rows)


class _Engine:
    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self.rows = rows
        self.disposed = False
        self.executions: list[tuple[str, object]] = []

    def connect(self) -> _Connection:
        return _Connection(self)

    def dispose(self) -> None:
        self.disposed = True


def test_live_certification_symbol_uses_current_exact_prints_and_delay_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import sqlalchemy

    engine = _Engine([("STALE", 100), ("VIVS", 90)])
    checked: list[str] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://protected-authority")
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *_a, **_k: engine)
    monkeypatch.setattr(sqlalchemy, "text", lambda value: value)
    monkeypatch.setattr(
        chain,
        "_delay_is_zero",
        lambda symbol: checked.append(symbol) is None and symbol == "VIVS",
    )

    evidence = tmp_path / "evidence.json"
    _write(evidence, b"candidate evidence")
    receipt = chain.ExactPrintPreselectionReceipt(
        evidence_path=evidence,
        evidence_sha256=_sha(evidence.read_bytes()),
        started_at=NOW,
        completed_at=NOW,
        bridge_version="candidate-v3+sha256:" + "1" * 16,
        bridge_run_id="11111111-2222-4333-8444-555555555555",
        timestamp_basis="iqfeed_selected_trade_date_timems_exact",
        bridge_source_sha256="1" * 64,
    )

    assert chain._select_live_certification_symbol(preselection=receipt) == "VIVS"
    assert checked == ["STALE", "VIVS"]
    assert engine.disposed is True
    query, parameters = engine.executions[-1]
    assert "received_at >= :started_at" in query
    assert "received_at <= :completed_at" in query
    assert "bridge_run_id = :bridge_run_id" in query
    assert "provider_trade_reference_at = provider_event_at" in query
    assert parameters == {
        "started_at": NOW,
        "completed_at": NOW,
        "timestamp_basis": "iqfeed_selected_trade_date_timems_exact",
        "bridge_version": "candidate-v3+sha256:" + "1" * 16,
        "bridge_run_id": "11111111-2222-4333-8444-555555555555",
    }


def test_discovery_rows_are_only_seeds_and_require_delay_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlalchemy

    engine = _Engine([("LEGACY", 100), ("VIVS", 90), ("bad symbol", 80)])
    checked: list[str] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://protected-authority")
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *_a, **_k: engine)
    monkeypatch.setattr(sqlalchemy, "text", lambda value: value)
    monkeypatch.setattr(
        chain,
        "_delay_is_zero",
        lambda symbol: checked.append(symbol) is None and symbol == "VIVS",
    )

    assert chain._discover_capture_seed_symbols() == ("VIVS",)
    assert checked == ["LEGACY", "VIVS"]
    assert engine.disposed is True
    query, parameters = engine.executions[-1]
    assert "WITH recent_tail AS MATERIALIZED" in query
    assert "ORDER BY id DESC LIMIT :tail_rows" in query
    assert parameters == {
        "tail_rows": chain._PRESELECTION_SEED_TAIL_ROWS,
        "limit": chain._PRESELECTION_SEED_LIMIT,
    }


def test_live_certification_symbol_fails_closed_when_no_delay_zero_symbol(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import sqlalchemy

    engine = _Engine([("VIVS", 90), ("bad symbol", 80)])
    monkeypatch.setenv("DATABASE_URL", "postgresql://protected-authority")
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *_a, **_k: engine)
    monkeypatch.setattr(sqlalchemy, "text", lambda value: value)
    monkeypatch.setattr(chain, "_delay_is_zero", lambda _symbol: False)

    evidence = tmp_path / "evidence.json"
    _write(evidence, b"candidate evidence")
    receipt = chain.ExactPrintPreselectionReceipt(
        evidence_path=evidence,
        evidence_sha256=_sha(evidence.read_bytes()),
        started_at=NOW,
        completed_at=NOW,
        bridge_version="candidate-v3+sha256:" + "1" * 16,
        bridge_run_id="11111111-2222-4333-8444-555555555555",
        timestamp_basis="iqfeed_selected_trade_date_timems_exact",
        bridge_source_sha256="1" * 64,
    )
    with pytest.raises(
        chain.CapturedPaperOperatorChainError,
        match="LIVE_TAPE_REALTIME_SYMBOL_UNAVAILABLE",
    ):
        chain._select_live_certification_symbol(preselection=receipt)
    assert engine.disposed is True


def test_live_certification_symbol_rejects_untyped_preselection() -> None:
    with pytest.raises(
        chain.CapturedPaperOperatorChainError,
        match="CAPTURE_ONLY_ATTESTATION_INVALID",
    ):
        chain._select_live_certification_symbol(preselection=object())  # type: ignore[arg-type]


def test_candidate_preselection_publishes_only_closed_zero_order_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts import iqfeed_capture_bootstrap_preflight as preflight_module
    from scripts import iqfeed_capture_only_smoke as smoke_module
    from scripts import iqfeed_trade_bridge

    capture_root = tmp_path / "capture-store"
    artifact_root = tmp_path / "artifacts"
    capture_root.mkdir()
    artifact_root.mkdir()
    preflight = SimpleNamespace(capture_store_root=capture_root)
    monkeypatch.setattr(
        preflight_module,
        "load_iqfeed_capture_bootstrap_preflight",
        lambda *_a, **_k: preflight,
    )
    observed: dict[str, Any] = {}

    def fake_measure(
        *,
        preflight: object,
        wall_clock: Any,
        monotonic_clock: Any,
    ) -> object:
        observed["measure"] = {
            "preflight": preflight,
            "wall_clock": wall_clock,
            "monotonic_clock": monotonic_clock,
        }
        return object()

    monkeypatch.setattr(
        chain.operator_flow,
        "_measure_capture_pressure",
        fake_measure,
    )

    class FakeConfig:
        def __init__(self, **kwargs: Any) -> None:
            observed.update(kwargs)

    class FakeAuthority:
        def __init__(self, **kwargs: Any) -> None:
            observed["authority"] = kwargs

    evidence_payload = {
        "schema_version": "chili.iqfeed-l1-exact-print-preselection-smoke.v1",
        "source_hashes": {
            "iqfeed_trade_bridge": iqfeed_trade_bridge.BRIDGE_SOURCE_SHA256,
        },
        "host_binding": {
            "execution_surface": "capture_only",
            "provider_scope": "l1_exact_print_preselection",
            "trade_bridge_bound": True,
            "depth_bridge_bound": False,
            "l2_snapshot_completion_required": False,
            "l2_decision_coverage_policy": "decision_local_fail_closed",
            "dispatcher_constructed": False,
            "live_runner_loop_constructed": False,
            "broker_adapter_constructed": False,
            "order_transport_constructed": False,
        },
        "capture_health": {
            "dropped_event_count": 0,
            "overflow_count": 0,
            "unreported_gap_count": 0,
        },
        "provider_health": {
            "exact_print_clock_observed": True,
            "exact_print_event_count": 1,
            "depth_provider_started": False,
        },
        "closure": {
            "orders_submitted": False,
            "bridges_unbound": True,
            "l2_opportunity_consumed": False,
            "l2_risk_reserved": False,
        },
    }
    evidence_document = dict(evidence_payload)
    evidence_document["evidence_sha256"] = _sha(_canonical(evidence_payload))

    class FakeEvidence:
        started_at = NOW
        completed_at = NOW

        def to_dict(self) -> dict[str, Any]:
            return dict(evidence_document)

    evidence = FakeEvidence()
    monkeypatch.setattr(smoke_module, "CaptureOnlySmokeConfiguration", FakeConfig)
    monkeypatch.setattr(smoke_module, "CaptureOnlySmokeEvidence", FakeEvidence)
    monkeypatch.setattr(
        smoke_module, "IngressCaptureOnlyHealthAuthority", FakeAuthority
    )
    monkeypatch.setattr(
        smoke_module,
        "run_capture_only_preactivation_smoke",
        lambda _configuration, **kwargs: observed.update(smoke_clocks=kwargs)
        or evidence,
    )

    receipt = chain._capture_candidate_exact_print_preselection(
        bootstrap_manifest_path=tmp_path / "bootstrap.json",
        bootstrap_manifest_sha256="a" * 64,
        capture_store_root=capture_root,
        artifact_root=artifact_root,
        allowed_read_roots=(str(tmp_path),),
        seed_symbols=("VIVS",),
    )

    assert observed["trade_forced_symbols"] == ("VIVS",)
    assert observed["depth_forced_symbols"] == ()
    assert observed["l1_only_exact_print_preselection"] is True
    assert observed["measure"]["preflight"] is preflight
    assert callable(observed["measure"]["wall_clock"])
    assert callable(observed["measure"]["monotonic_clock"])
    assert observed["authority"]["wall_clock"] is observed["measure"]["wall_clock"]
    assert observed["smoke_clocks"] == {
        "wall_clock": observed["measure"]["wall_clock"],
        "monotonic_clock": observed["measure"]["monotonic_clock"],
    }
    assert receipt.bridge_run_id == iqfeed_trade_bridge.BRIDGE_RUN_ID
    assert receipt.bridge_source_sha256 == iqfeed_trade_bridge.BRIDGE_SOURCE_SHA256
    assert receipt.evidence_path.read_bytes() == _canonical(evidence_document)
    assert receipt.evidence_sha256 == _sha(receipt.evidence_path.read_bytes())
    assert not hasattr(receipt, "broker_adapter")
    assert not hasattr(receipt, "order_transport")


def test_candidate_preselection_rejects_execution_surface_attestation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts import iqfeed_capture_bootstrap_preflight as preflight_module
    from scripts import iqfeed_capture_only_smoke as smoke_module
    from scripts import iqfeed_trade_bridge

    capture_root = tmp_path / "capture-store"
    artifact_root = tmp_path / "artifacts"
    capture_root.mkdir()
    artifact_root.mkdir()
    preflight = SimpleNamespace(capture_store_root=capture_root)
    monkeypatch.setattr(
        preflight_module,
        "load_iqfeed_capture_bootstrap_preflight",
        lambda *_a, **_k: preflight,
    )
    monkeypatch.setattr(
        chain.operator_flow,
        "_measure_capture_pressure",
        lambda **_k: object(),
    )
    monkeypatch.setattr(
        smoke_module, "CaptureOnlySmokeConfiguration", lambda **_k: object()
    )
    monkeypatch.setattr(
        smoke_module, "IngressCaptureOnlyHealthAuthority", lambda **_k: object()
    )
    payload = {
        "schema_version": "chili.iqfeed-l1-exact-print-preselection-smoke.v1",
        "source_hashes": {
            "iqfeed_trade_bridge": iqfeed_trade_bridge.BRIDGE_SOURCE_SHA256,
        },
        "host_binding": {
            "execution_surface": "capture_only",
            "provider_scope": "l1_exact_print_preselection",
            "trade_bridge_bound": True,
            "depth_bridge_bound": False,
            "l2_snapshot_completion_required": False,
            "l2_decision_coverage_policy": "decision_local_fail_closed",
            "dispatcher_constructed": False,
            "live_runner_loop_constructed": False,
            "broker_adapter_constructed": True,
            "order_transport_constructed": False,
        },
        "capture_health": {
            "dropped_event_count": 0,
            "overflow_count": 0,
            "unreported_gap_count": 0,
        },
        "provider_health": {
            "exact_print_clock_observed": True,
            "exact_print_event_count": 1,
            "depth_provider_started": False,
        },
        "closure": {
            "orders_submitted": False,
            "bridges_unbound": True,
            "l2_opportunity_consumed": False,
            "l2_risk_reserved": False,
        },
    }
    document = dict(payload)
    document["evidence_sha256"] = _sha(_canonical(payload))

    class FakeEvidence:
        started_at = NOW
        completed_at = NOW

        def to_dict(self) -> dict[str, Any]:
            return dict(document)

    monkeypatch.setattr(smoke_module, "CaptureOnlySmokeEvidence", FakeEvidence)
    monkeypatch.setattr(
        smoke_module,
        "run_capture_only_preactivation_smoke",
        lambda _configuration, **_kwargs: FakeEvidence(),
    )

    with pytest.raises(
        chain.CapturedPaperOperatorChainError,
        match="CAPTURE_ONLY_ATTESTATION_INVALID",
    ):
        chain._capture_candidate_exact_print_preselection(
            bootstrap_manifest_path=tmp_path / "bootstrap.json",
            bootstrap_manifest_sha256="a" * 64,
            capture_store_root=capture_root,
            artifact_root=artifact_root,
            allowed_read_roots=(str(tmp_path),),
            seed_symbols=("VIVS",),
        )
    assert not tuple((artifact_root / "capture-preselection").glob("*"))


def test_full_operator_chain_bootstraps_exact_print_before_selection_and_is_hash_bound(
    monkeypatch: pytest.MonkeyPatch,
    chain_fixture: ChainFixture,
) -> None:
    activation, _path, _digest = chain_fixture.publish()
    calls: list[str] = []
    manifest = activation.artifact_root / "bootstrap" / "artifacts" / "manifest.json"
    manifest_sha = _write(manifest, _canonical({"paper": True}))

    monkeypatch.setattr(
        chain,
        "install_captured_paper_runtime_environment",
        lambda *_a, **_k: calls.append("install-runtime"),
    )
    monkeypatch.setattr(
        chain,
        "_read_exact_paper_account",
        lambda **_k: (
            {"equity": "71868.33", "status": "ACTIVE"},
            {
                "endpoint": "/v2/account",
                "environment": "paper",
                "account_id": ACCOUNT_ID,
            },
            NOW,
            NOW,
        ),
    )
    monkeypatch.setattr(chain, "_sha_source_inventory", lambda _root: {"test": "b" * 64})

    def fake_bootstrap(**kwargs: Any) -> SimpleNamespace:
        calls.append("bootstrap-read-only")
        request_path = Path(kwargs["request_path"])
        assert _sha(request_path.read_bytes()) == kwargs["request_sha256"]
        return SimpleNamespace(manifest_path=manifest, manifest_sha256=manifest_sha)

    monkeypatch.setattr(
        chain.bootstrap,
        "build_iqfeed_capture_bootstrap_bundle_from_request",
        fake_bootstrap,
    )
    monkeypatch.setattr(
        chain,
        "_discover_capture_seed_symbols",
        lambda: calls.append("discover-seed-read-only") or ("VIVS",),
    )
    preselection_evidence = activation.artifact_root / "candidate-preselection.json"
    preselection_sha = _write(
        preselection_evidence,
        _canonical({"capture_only": True, "orders_submitted": False}),
    )
    preselection = chain.ExactPrintPreselectionReceipt(
        evidence_path=preselection_evidence.resolve(strict=True),
        evidence_sha256=preselection_sha,
        started_at=NOW,
        completed_at=NOW,
        bridge_version="iqfeed-l1-exact-print-provenance-v3+sha256:" + "b" * 16,
        bridge_run_id="11111111-2222-4333-8444-555555555555",
        timestamp_basis="iqfeed_selected_trade_date_timems_exact",
        bridge_source_sha256="b" * 64,
    )
    candidate_exact_rows_available = False

    def fake_preselection(**kwargs: Any) -> chain.ExactPrintPreselectionReceipt:
        nonlocal candidate_exact_rows_available
        calls.append("candidate-capture-only")
        assert candidate_exact_rows_available is False
        assert kwargs["seed_symbols"] == ("VIVS",)
        assert kwargs["bootstrap_manifest_path"] == manifest
        candidate_exact_rows_available = True
        return preselection

    monkeypatch.setattr(
        chain, "_capture_candidate_exact_print_preselection", fake_preselection
    )

    def fake_select(
        *, preselection: chain.ExactPrintPreselectionReceipt
    ) -> str:
        calls.append("candidate-exact-select")
        assert candidate_exact_rows_available is True
        assert preselection.evidence_path == preselection_evidence.resolve(strict=True)
        assert preselection.evidence_sha256 == preselection_sha
        return "VIVS"

    monkeypatch.setattr(chain, "_select_live_certification_symbol", fake_select)
    probe = object()
    monkeypatch.setattr(chain.host_snapshot, "WindowsReadOnlyHostProbe", lambda: probe)

    def fake_collect(**kwargs: Any) -> object:
        calls.append("host-read-only")
        assert kwargs["probe"] is probe
        return object()

    monkeypatch.setattr(chain.host_snapshot, "collect_host_snapshot", fake_collect)

    def fake_persist(_observed: object, *, output_root: Path) -> SimpleNamespace:
        calls.append("persist-snapshot")
        paths: dict[str, Path] = {}
        hashes: dict[str, str] = {}
        for role in ("task_snapshot", "process_snapshot", "restore_plan"):
            path = output_root / f"{role}.json"
            hashes[role] = _write(path, _canonical({"role": role, "read_only": True}))
            paths[role] = path
        return SimpleNamespace(
            verdict="VALIDATED",
            artifact_paths=paths,
            artifact_sha256s=hashes,
        )

    monkeypatch.setattr(chain.host_snapshot, "persist_host_snapshot", fake_persist)
    observed_plan: dict[str, Any] = {}

    def fake_configuration(plan: Mapping[str, Any]) -> object:
        calls.append("plan-validated")
        observed_plan.update(plan)
        for field in (
            "operator_output_root",
            "preactivation_output_root",
            "activation_artifact_root",
        ):
            assert Path(str(plan[field])).is_dir()
        assert Path(str(plan["no_order_receipt_output"])).parent.is_dir()
        return object()

    composition = object()
    monkeypatch.setattr(chain.operator_flow, "configuration_from_plan", fake_configuration)
    monkeypatch.setattr(
        chain.operator_flow,
        "build_live_operator_composition",
        lambda _configuration: composition,
    )

    class Result:
        def to_dict(self) -> dict[str, Any]:
            return {
                "verdict": "CAPTURED_ALPACA_PAPER_BUILD_READY_WITH_EXTERNAL_HOST_BASELINE",
                "host_cutover_invoked": False,
                "broker_order_calls": 0,
            }

    def fake_operator_flow(value: object) -> Result:
        calls.append("operator-read-only")
        assert value is composition
        return Result()

    monkeypatch.setattr(
        chain.operator_flow, "run_captured_paper_operator_flow", fake_operator_flow
    )

    result = chain.run_operator_chain(
        activation_request=activation,
        chain_document=chain_fixture.document,
    )

    assert calls == [
        "install-runtime",
        "bootstrap-read-only",
        "discover-seed-read-only",
        "candidate-capture-only",
        "candidate-exact-select",
        "host-read-only",
        "persist-snapshot",
        "plan-validated",
        "operator-read-only",
    ]
    assert result["live_cash_authorized"] is False
    assert result["paper_order_submission_authorized"] is False
    assert result["paper_service_started"] is False
    assert result["host_cutover_invoked"] is False
    assert result["broker_order_calls"] == 0
    selection_receipt = result["exact_print_selection_receipt"]
    selection_document = json.loads(
        Path(selection_receipt["path"]).read_text(encoding="utf-8")
    )
    assert _sha(Path(selection_receipt["path"]).read_bytes()) == (
        selection_receipt["sha256"]
    )
    assert selection_document["preselection_provider_scope"] == (
        "l1_exact_print_preselection"
    )
    assert selection_document[
        "l2_snapshot_completion_required_for_preselection"
    ] is False
    assert selection_document["l2_decision_coverage_policy"] == (
        "decision_local_fail_closed"
    )
    assert result["activation_runner_request_sha256"] == activation.request_sha256
    assert result["operator_chain_request_sha256"] == activation.chain_request_sha256
    assert result["resource_benchmark_sha256"] == chain_fixture.document[
        "resource_benchmark"
    ]["sha256"]
    assert observed_plan["expected_account_id"] == ACCOUNT_ID
    assert observed_plan["capture_certification_symbol"] == "VIVS"
    assert observed_plan["runtime_env_sha256"] == activation.runtime_env_sha256

    request_paths = list(
        (activation.artifact_root / "bootstrap" / "inputs").glob("*.request.json")
    )
    plan_paths = list((activation.artifact_root / "operator").glob("*.plan.json"))
    assert len(request_paths) == 1
    assert len(plan_paths) == 1
    for path in (*request_paths, *plan_paths):
        raw = path.read_bytes()
        assert path.stem.split(".")[0] == _sha(raw)
        assert _canonical(json.loads(raw)) == raw
        chain._publish_once(path, raw)
        with pytest.raises(
            chain.CapturedPaperOperatorChainError,
            match="APPEND_ONLY_CONFLICT",
        ):
            chain._publish_once(path, b"different bytes")


def test_main_returns_only_sanitized_fail_closed_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "postgresql://chili:super-secret@private-host/prod"

    def rejected(**_kwargs: Any) -> Any:
        raise RuntimeError(secret)

    monkeypatch.setattr(
        activation_runner, "load_activation_runner_request", rejected
    )

    code = chain.main(
        [
            "--request",
            "unused-chain.json",
            "--request-sha256",
            "a" * 64,
            "--activation-request",
            "unused-activation.json",
            "--activation-request-sha256",
            "b" * 64,
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.err == ""
    assert secret not in captured.out
    document = json.loads(captured.out)
    assert document == {
        "schema_version": chain.CHAIN_ERROR_SCHEMA_VERSION,
        "verdict": "CAPTURED_ALPACA_PAPER_BUILD_REJECTED",
        "reason_code": "CAPTURED_PAPER_OPERATOR_CHAIN_REJECTED",
        "account_scope": chain.ACCOUNT_SCOPE,
        "paper_order_submission_authorized": False,
        "paper_service_started": False,
        "host_cutover_invoked": False,
        "live_cash_authorized": False,
    }
