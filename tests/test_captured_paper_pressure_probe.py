from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import psutil

from scripts import captured_paper_pressure_probe as probe


HELPER = Path(probe.__file__).resolve(strict=True)


def _helper_sha256() -> str:
    return hashlib.sha256(HELPER.read_bytes()).hexdigest()


def _request(session_nonce: str, sequence: int, op: str) -> bytes:
    return probe._encode_json_line(
        {
            "schema": probe.REQUEST_SCHEMA,
            "op": op,
            "session_nonce": session_nonce,
            "sequence": sequence,
            "request_nonce": probe._request_nonce(session_nonce, sequence, op),
        }
    )


def test_measure_once_times_exact_write_flush_fsync_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((100, 725))
    observed: list[tuple[int, int]] = []

    monkeypatch.setattr(probe.time, "perf_counter_ns", lambda: next(clock))

    def fsync(descriptor: int) -> None:
        observed.append((descriptor, os.fstat(descriptor).st_size))

    monkeypatch.setattr(probe.os, "fsync", fsync)
    root = tmp_path.resolve(strict=True)
    status = root.stat()
    result = probe._measure_once(
        root,
        int(status.st_dev),
        int(status.st_ino),
        "a" * 64,
    )

    assert result == {
        "latency_ns": 625,
        "bytes_written": 4096,
        "fsync_completed": True,
        "cleanup_completed": True,
    }
    assert len(observed) == 1
    assert observed[0][1] == 4096
    assert list(root.glob(".chili-pressure-probe-*.tmp")) == []


def test_measure_once_fsync_failure_fails_closed_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_fsync(_descriptor: int) -> None:
        raise OSError("synthetic durable-flush failure")

    monkeypatch.setattr(probe.os, "fsync", fail_fsync)
    root = tmp_path.resolve(strict=True)
    with pytest.raises(probe._ProbeFailure) as raised:
        status = root.stat()
        probe._measure_once(
            root,
            int(status.st_dev),
            int(status.st_ino),
            "a" * 64,
        )

    assert raised.value.error_code == "probe_durable_flush_failed"
    assert list(root.glob(".chili-pressure-probe-*.tmp")) == []


def test_measure_once_rejects_same_volume_root_swap_during_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (tmp_path / "probe").resolve(strict=False)
    root.mkdir()
    original_root = tmp_path / "original-probe"
    status = root.stat()
    original_revalidate = probe._revalidate_probe_root

    def swap_after_revalidation(
        checked_root: Path,
        expected_st_dev: int,
        expected_st_ino: int,
    ) -> None:
        original_revalidate(
            checked_root,
            expected_st_dev,
            expected_st_ino,
        )
        checked_root.rename(original_root)
        checked_root.mkdir()

    monkeypatch.setattr(
        probe,
        "_revalidate_probe_root",
        swap_after_revalidation,
    )

    with pytest.raises(probe._ProbeFailure) as raised:
        probe._measure_once(
            root,
            int(status.st_dev),
            int(status.st_ino),
            "a" * 64,
        )

    assert raised.value.error_code == "probe_file_identity_mismatch"
    assert root.stat().st_ino != status.st_ino
    assert list(root.glob(".chili-pressure-probe-*.tmp")) == []
    assert list(original_root.glob(".chili-pressure-probe-*.tmp")) == []


@pytest.mark.parametrize(
    "mutation",
    (
        lambda body: {**body, "sequence": 0},
        lambda body: {**body, "session_nonce": "f" * 64},
        lambda body: {**body, "request_nonce": "e" * 64},
        lambda body: {**body, "extra": True},
    ),
)
def test_request_validation_rejects_replay_wrong_session_nonce_and_extra_fields(
    mutation,
) -> None:
    session = "a" * 64
    body = {
        "schema": probe.REQUEST_SCHEMA,
        "op": "measure",
        "session_nonce": session,
        "sequence": 1,
        "request_nonce": probe._request_nonce(session, 1, "measure"),
    }
    malformed = probe._encode_json_line(mutation(body))

    with pytest.raises(probe.CapturedPaperPressureProbeProtocolError):
        probe._parse_request(
            malformed,
            expected_sequence=1,
            expected_session_nonce=session,
        )


def test_json_protocol_rejects_duplicate_keys_and_oversized_lines() -> None:
    duplicate = (
        b'{"schema":"x","schema":"y","op":"init","session_nonce":"'
        + (b"a" * 64)
        + b'","sequence":0,"request_nonce":"'
        + (b"b" * 64)
        + b'"}\n'
    )
    with pytest.raises(probe.CapturedPaperPressureProbeProtocolError):
        probe._decode_json_line(duplicate)
    with pytest.raises(probe.CapturedPaperPressureProbeProtocolError):
        probe._decode_json_line(b"x" * (probe._MAX_JSON_LINE_BYTES + 1))
    with pytest.raises(probe.CapturedPaperPressureProbeProtocolError):
        probe._decode_json_line(b'{"value":NaN}\n')


def test_parent_wall_rtt_rejects_already_queued_late_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = probe.CapturedPaperPressureProbeClient(
        python_executable=sys.executable,
        probe_root=tmp_path,
        helper_path=probe.__file__,
        expected_helper_sha256=hashlib.sha256(
            Path(probe.__file__).read_bytes()
        ).hexdigest(),
        response_timeout_seconds=0.001,
    )
    sequence = 0
    request_nonce = probe._request_nonce(client._session_nonce, sequence, "init")
    client._responses.put(
        probe._encode_json_line(
            {
                "schema": probe.RESPONSE_SCHEMA,
                "status": "ready",
                "session_nonce": client._session_nonce,
                "sequence": sequence,
                "request_nonce": request_nonce,
                "helper_sha256": client.helper_sha256,
                "probe_root_identity_sha256": client.probe_root_identity_sha256,
                "python_major": 3,
                "python_minor": 11,
                "write_latency_profile": probe.PRESSURE_WRITE_LATENCY_PROFILE,
            }
        )
    )
    client._process = SimpleNamespace(
        poll=lambda: None,
        stdin=io.BytesIO(),
    )
    clock = iter((0, 1, 1_000_001))
    monkeypatch.setattr(probe.time, "perf_counter_ns", lambda: next(clock))

    with pytest.raises(
        probe.CapturedPaperPressureProbeUnavailableError,
        match="wall-RTT bound",
    ):
        client._exchange("init")


def test_parent_rejects_child_latency_larger_than_observed_rtt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = probe.CapturedPaperPressureProbeClient(
        python_executable=sys.executable,
        probe_root=tmp_path,
        helper_path=probe.__file__,
        expected_helper_sha256=hashlib.sha256(
            Path(probe.__file__).read_bytes()
        ).hexdigest(),
    )
    response = {
        "schema": probe.RESPONSE_SCHEMA,
        "status": "ok",
        "session_nonce": client._session_nonce,
        "sequence": 1,
        "request_nonce": "b" * 64,
        "latency_ns": 101,
        "bytes_written": 4096,
        "fsync_completed": True,
        "cleanup_completed": True,
        "probe_root_identity_sha256": client.probe_root_identity_sha256,
        "write_latency_profile": probe.PRESSURE_WRITE_LATENCY_PROFILE,
    }
    monkeypatch.setattr(
        client,
        "_exchange",
        lambda _op, *, response_timeout_seconds=None: (response, 100),
    )

    with pytest.raises(
        probe.CapturedPaperPressureProbeProtocolError,
        match="measurement is incomplete",
    ):
        client.measure()


@pytest.mark.skipif(sys.version_info[:2] != (3, 11), reason="helper is CPython 3.11-pinned")
def test_persistent_private_pipe_client_measures_twice_and_cleans_up(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"must remain byte-identical")
    sentinel_sha256 = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    with probe.CapturedPaperPressureProbeClient(
        python_executable=sys.executable,
        probe_root=tmp_path,
        helper_path=probe.__file__,
        expected_helper_sha256=hashlib.sha256(
            Path(probe.__file__).read_bytes()
        ).hexdigest(),
        response_timeout_seconds=10.0,
    ) as client:
        pid = client.pid
        if os.name == "nt":
            assert client._job_handle is not None
        first = client.measure()
        second = client.measure()

        assert pid is not None
        assert client.pid == pid
        assert first.sequence == 1
        assert second.sequence == 2
        assert first.request_nonce != second.request_nonce
        assert first.latency_ns >= 0
        assert second.latency_ns >= 0
        assert first.bytes_written == second.bytes_written == 4096
        assert first.fsync_completed is second.fsync_completed is True
        assert first.cleanup_completed is second.cleanup_completed is True
        assert first.probe_root_identity_sha256 == (
            client.probe_root_identity_sha256
        )
        assert second.probe_root_identity_sha256 == (
            client.probe_root_identity_sha256
        )
        assert first.write_latency_profile == (
            probe.PRESSURE_WRITE_LATENCY_PROFILE
        )
        assert second.write_latency_profile == (
            "chili.capture-pressure.durable-write-fsync-helper-process.v1"
        )
        assert list(tmp_path.glob(".chili-pressure-probe-*.tmp")) == []
        assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == sentinel_sha256

    assert client.pid is None
    assert client._job_handle is None
    assert list(tmp_path.glob(".chili-pressure-probe-*.tmp")) == []
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == sentinel_sha256


@pytest.mark.skipif(sys.version_info[:2] != (3, 11), reason="helper is CPython 3.11-pinned")
def test_runtime_timeout_reaps_then_next_measure_restarts_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = probe.CapturedPaperPressureProbeClient(
        python_executable=sys.executable,
        probe_root=tmp_path,
        helper_path=probe.__file__,
        expected_helper_sha256=_helper_sha256(),
        response_timeout_seconds=10.0,
    )
    client.start()
    first_pid = client.pid
    original_exchange = client._exchange
    failed = False

    def fail_once(op: str, *, response_timeout_seconds=None):
        nonlocal failed
        if op == "measure" and not failed:
            failed = True
            raise probe.CapturedPaperPressureProbeUnavailableError(
                "synthetic bounded timeout"
            )
        return original_exchange(
            op,
            response_timeout_seconds=response_timeout_seconds,
        )

    monkeypatch.setattr(client, "_exchange", fail_once)
    with pytest.raises(probe.CapturedPaperPressureProbeUnavailableError):
        client.measure()
    assert client.pid is None
    assert client._job_handle is None

    monkeypatch.setattr(client, "_exchange", original_exchange)
    recovered = client.measure()
    try:
        assert client.pid is not None
        assert client.pid != first_pid
        assert recovered.fsync_completed is True
        assert recovered.cleanup_completed is True
    finally:
        client.close()


@pytest.mark.skipif(sys.version_info[:2] != (3, 11), reason="helper is CPython 3.11-pinned")
def test_held_loader_rejects_helper_source_changed_after_client_binding(
    tmp_path: Path,
) -> None:
    helper_copy = tmp_path / "pressure-helper.py"
    helper_copy.write_bytes(HELPER.read_bytes())
    expected = hashlib.sha256(helper_copy.read_bytes()).hexdigest()
    probe_root = tmp_path / "probe"
    probe_root.mkdir()
    client = probe.CapturedPaperPressureProbeClient(
        python_executable=sys.executable,
        probe_root=probe_root,
        helper_path=helper_copy,
        expected_helper_sha256=expected,
    )
    helper_copy.write_bytes(helper_copy.read_bytes() + b"\n# drift\n")

    with pytest.raises(probe.CapturedPaperPressureProbeProtocolError):
        client.start()
    assert client.pid is None
    assert client._job_handle is None
    assert list(probe_root.glob(".chili-pressure-probe-*.tmp")) == []


@pytest.mark.skipif(sys.version_info[:2] != (3, 11), reason="helper is CPython 3.11-pinned")
def test_probe_root_replacement_is_rejected_before_measurement(
    tmp_path: Path,
) -> None:
    probe_root = tmp_path / "probe"
    probe_root.mkdir()
    client = probe.CapturedPaperPressureProbeClient(
        python_executable=sys.executable,
        probe_root=probe_root,
        helper_path=probe.__file__,
        expected_helper_sha256=_helper_sha256(),
    )
    original_root = tmp_path / "original-probe"
    probe_root.rename(original_root)
    probe_root.mkdir()

    with pytest.raises(probe.CapturedPaperPressureProbeProtocolError):
        client.start()
    assert client.pid is None
    assert client._job_handle is None
    assert list(probe_root.glob(".chili-pressure-probe-*.tmp")) == []


@pytest.mark.skipif(
    os.name != "nt" or sys.version_info[:2] != (3, 11),
    reason="Windows CPython 3.11 Job Object contract",
)
def test_abnormal_parent_exit_kills_pressure_helper_job(
    tmp_path: Path,
) -> None:
    probe_root = tmp_path / "probe"
    probe_root.mkdir()
    parent_code = textwrap.dedent(
        """
        import hashlib, importlib.util, pathlib, sys, threading
        helper = pathlib.Path(sys.argv[1]).resolve(strict=True)
        spec = importlib.util.spec_from_file_location("isolated_probe_parent", helper)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        client = module.CapturedPaperPressureProbeClient(
            python_executable=sys.executable,
            probe_root=pathlib.Path(sys.argv[2]),
            helper_path=helper,
            expected_helper_sha256=hashlib.sha256(helper.read_bytes()).hexdigest(),
            response_timeout_seconds=10.0,
        )
        client.start()
        print(client.pid, flush=True)
        threading.Event().wait()
        """
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, str(HELPER), str(probe_root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    helper_pid: int | None = None
    gone = False
    try:
        assert parent.stdout is not None
        helper_pid = int(parent.stdout.readline().strip())
        helper_process = psutil.Process(helper_pid)
        helper_process.suspend()
        parent.kill()
        parent.wait(timeout=5.0)
        for _attempt in range(100):
            if not psutil.pid_exists(helper_pid):
                gone = True
                break
            time.sleep(0.05)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5.0)
        if helper_pid is not None and psutil.pid_exists(helper_pid):
            orphan = psutil.Process(helper_pid)
            try:
                orphan.resume()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            try:
                orphan.kill()
                orphan.wait(timeout=5.0)
            except psutil.NoSuchProcess:
                pass
    assert gone is True
    assert list(probe_root.glob(".chili-pressure-probe-*.tmp")) == []


@pytest.mark.skipif(sys.version_info[:2] != (3, 11), reason="helper is CPython 3.11-pinned")
def test_child_rejects_wrong_self_hash_without_accepting_a_session(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(HELPER),
            "--serve",
            "--self-sha256",
            "0" * 64,
            "--probe-root",
            str(tmp_path.resolve(strict=True)),
            "--probe-root-st-dev",
            str(tmp_path.stat().st_dev),
            "--probe-root-st-ino",
            str(tmp_path.stat().st_ino),
        ],
        input=_request("a" * 64, 0, "init"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10.0,
        check=False,
    )

    assert completed.returncode == 2
    response = probe._decode_json_line(completed.stdout)
    assert response == {
        "schema": probe.RESPONSE_SCHEMA,
        "status": "error",
        "session_nonce": "",
        "sequence": -1,
        "request_nonce": "",
        "error_code": "helper_sha256_mismatch",
    }
    assert b"Traceback" not in completed.stderr


@pytest.mark.skipif(sys.version_info[:2] != (3, 11), reason="helper is CPython 3.11-pinned")
def test_child_rejects_missing_isolated_runtime_flags(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(HELPER),
            "--serve",
            "--self-sha256",
            _helper_sha256(),
            "--probe-root",
            str(tmp_path.resolve(strict=True)),
            "--probe-root-st-dev",
            str(tmp_path.stat().st_dev),
            "--probe-root-st-ino",
            str(tmp_path.stat().st_ino),
        ],
        input=_request("a" * 64, 0, "init"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10.0,
        check=False,
    )

    assert completed.returncode == 2
    response = probe._decode_json_line(completed.stdout)
    assert response["error_code"] == "python_runtime_flags_invalid"
    assert list(tmp_path.glob(".chili-pressure-probe-*.tmp")) == []
