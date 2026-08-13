"""Standalone, no-DB benchmark for ReplayV3 capture runtime primitives.

The benchmark drives the real ``BoundedCaptureIngress``,
``CaptureWriterWorker``, and ``ContentAddressedCaptureStore`` with a
deterministic representative mix of NBBO, IQFeed prints, L2 updates, query
receipts, and change-log events.  It does not import the broad trading package,
read runtime configuration, connect to a database, or contact a provider.

Example::

    python -I -S -B -c <approved-held-benchmark-loader> \
        <benchmark-path> <benchmark-sha256> \
        <contract-path> <contract-sha256> \
        <runtime-path> <runtime-sha256> \
        <pressure-probe-path> <pressure-probe-sha256> -- \
        --output-root D:\\CHILI-Docker\\chili-data\\benchmarks \
        --events 100000

The successful stdout payload is one canonical JSON object.  The benchmark
creates a uniquely owned child directory under ``--output-root`` and deletes
only that verified directory unless ``--keep`` is supplied.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import stat
import sys
import tempfile
import threading
import time
from types import ModuleType
from typing import Any, Callable, Iterator, Mapping
import uuid

UTC = timezone.utc
BENCHMARK_SCHEMA_VERSION = "chili.replay-capture-benchmark.v7"
OWNERSHIP_MARKER = ".chili-replay-capture-benchmark-owner.json"
OWNED_DIRECTORY_PREFIX = "chili-replay-capture-benchmark-"
MEBIBYTE = 1024**2
CAPACITY_AUTHORITY = "diagnostic_only"
CAPACITY_AUTHORITY_REASONS = (
    "empirical_hot_symbol_calibration_receipt_unavailable",
    "full_runner_watcher_resource_calibration_unavailable",
    "writer_scaling_calibration_unavailable",
)

# This benchmark produces evidence consumed by PAPER activation.  It must be
# compiled only after an external ``python -c`` loader has held and matched all
# executable source closure to authority-supplied SHA-256 values.  Hashing a
# source after Python has already executed it would be circular
# self-attestation.  The loader also runs with ``-S`` so no site or ``.pth``
# code can execute before this boundary.
_HELD_BENCHMARK_SOURCES = globals().get(
    "__chili_held_benchmark_sources__"
)
if not isinstance(_HELD_BENCHMARK_SOURCES, Mapping) or set(
    _HELD_BENCHMARK_SOURCES
) != {
    "benchmark",
    "contract",
    "runtime",
    "pressure_probe",
    "replay_errors",
    "first_dip_tape_policy",
    "stage0",
}:
    raise RuntimeError(
        "benchmark must execute through the approved held-source bundle loader"
    )
_BENCHMARK_DEPENDENCY_IDENTITY_SHA256 = globals().get(
    "__chili_benchmark_dependency_identity_sha256__"
)
_BENCHMARK_AUTHORITY_MANIFEST_SHA256 = globals().get(
    "__chili_benchmark_authority_manifest_sha256__"
)
_BENCHMARK_PYTHON_EXECUTABLE_SHA256 = globals().get(
    "__chili_benchmark_python_executable_sha256__"
)
if (
    not isinstance(_BENCHMARK_DEPENDENCY_IDENTITY_SHA256, str)
    or len(_BENCHMARK_DEPENDENCY_IDENTITY_SHA256) != 64
    or any(
        character not in "0123456789abcdef"
        for character in _BENCHMARK_DEPENDENCY_IDENTITY_SHA256
    )
):
    raise RuntimeError("benchmark dependency identity is invalid")
if (
    not isinstance(_BENCHMARK_AUTHORITY_MANIFEST_SHA256, str)
    or len(_BENCHMARK_AUTHORITY_MANIFEST_SHA256) != 64
    or any(
        character not in "0123456789abcdef"
        for character in _BENCHMARK_AUTHORITY_MANIFEST_SHA256
    )
):
    raise RuntimeError("benchmark authority manifest identity is invalid")
if (
    not isinstance(_BENCHMARK_PYTHON_EXECUTABLE_SHA256, str)
    or len(_BENCHMARK_PYTHON_EXECUTABLE_SHA256) != 64
    or any(
        character not in "0123456789abcdef"
        for character in _BENCHMARK_PYTHON_EXECUTABLE_SHA256
    )
):
    raise RuntimeError("benchmark Python executable identity is invalid")
if not (
    sys.flags.isolated == 1
    and sys.flags.ignore_environment == 1
    and sys.flags.no_site == 1
    and sys.flags.safe_path is True
    and sys.flags.dont_write_bytecode == 1
):
    raise RuntimeError(
        "benchmark held-source runtime isolation is invalid"
    )


def _stable_source_bytes(path: Path) -> bytes:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError("benchmark source is not a regular file")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    descriptor = os.open(resolved, flags)
    try:
        opened = os.fstat(descriptor)
        identity = lambda value: (
            int(value.st_dev),
            int(value.st_ino),
            int(value.st_size),
            int(value.st_mtime_ns),
            int(value.st_mode),
        )
        if identity(opened) != identity(before):
            raise RuntimeError("benchmark source changed before open")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    after = resolved.stat()
    if identity(after) != identity(before):
        raise RuntimeError("benchmark source changed while read")
    return b"".join(chunks)


def _held_source(role: str) -> tuple[Path, bytes, str]:
    row = _HELD_BENCHMARK_SOURCES.get(role)
    if not isinstance(row, Mapping) or set(row) != {
        "path",
        "sha256",
        "source",
    }:
        raise RuntimeError("held benchmark source bundle is malformed")
    path_raw = row.get("path")
    digest = row.get("sha256")
    source = row.get("source")
    if (
        not isinstance(path_raw, str)
        or not Path(path_raw).is_absolute()
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or type(source) is not bytes
        or hashlib.sha256(source).hexdigest() != digest
    ):
        raise RuntimeError("held benchmark source bundle is invalid")
    path = Path(path_raw).resolve(strict=True)
    if str(path) != path_raw:
        raise RuntimeError("held benchmark source path is not canonical")
    return path, source, digest


def _load_capture_modules() -> tuple[ModuleType, ModuleType, dict[str, str]]:
    """Load only the two capture modules, bypassing trading package side effects."""

    benchmark_path, _, _ = _held_source("benchmark")
    module_dir = (
        benchmark_path.parents[1]
        / "app"
        / "services"
        / "trading"
        / "momentum_neural"
    )
    package_name = "_chili_replay_capture_benchmark"
    package = ModuleType(package_name)
    # No filesystem fallback is permitted: every import needed while these
    # benchmark modules initialize must already be present in ``sys.modules``
    # from the externally held source bundle.
    package.__path__ = []  # type: ignore[attr-defined]
    package.__package__ = package_name
    sys.modules[package_name] = package
    contract_path, contract_raw, contract_sha = _held_source("contract")
    runtime_path, runtime_raw, runtime_sha = _held_source("runtime")
    errors_path, errors_raw, errors_sha = _held_source("replay_errors")
    policy_path, policy_raw, policy_sha = _held_source("first_dip_tape_policy")
    if (
        contract_path != module_dir / "replay_capture_contract.py"
        or runtime_path != module_dir / "replay_capture_runtime.py"
        or errors_path != module_dir / "replay_errors.py"
        or policy_path != module_dir / "first_dip_tape_policy.py"
    ):
        raise RuntimeError("held capture source path binding is invalid")
    contract_name = f"{package_name}.replay_capture_contract"
    runtime_name = f"{package_name}.replay_capture_runtime"
    errors_name = f"{package_name}.replay_errors"
    policy_name = f"{package_name}.first_dip_tape_policy"
    contract = ModuleType(contract_name)
    contract.__file__ = str(contract_path)
    contract.__package__ = package_name
    runtime = ModuleType(runtime_name)
    runtime.__file__ = str(runtime_path)
    runtime.__package__ = package_name
    errors = ModuleType(errors_name)
    errors.__file__ = str(errors_path)
    errors.__package__ = package_name
    policy = ModuleType(policy_name)
    policy.__file__ = str(policy_path)
    policy.__package__ = package_name
    sys.modules[contract_name] = contract
    sys.modules[runtime_name] = runtime
    sys.modules[errors_name] = errors
    sys.modules[policy_name] = policy
    exec(compile(errors_raw, str(errors_path), "exec"), errors.__dict__)
    exec(compile(contract_raw, str(contract_path), "exec"), contract.__dict__)
    exec(compile(policy_raw, str(policy_path), "exec"), policy.__dict__)
    exec(compile(runtime_raw, str(runtime_path), "exec"), runtime.__dict__)
    return contract, runtime, {
        "contract_sha256": contract_sha,
        "first_dip_tape_policy_sha256": policy_sha,
        "replay_errors_sha256": errors_sha,
        "runtime_sha256": runtime_sha,
    }


def _load_pressure_probe_module() -> tuple[ModuleType, str]:
    """Load the shared stdlib-only durability probe without broad app imports."""

    benchmark_path, _, _ = _held_source("benchmark")
    path, raw, digest = _held_source("pressure_probe")
    if path != benchmark_path.with_name("captured_paper_pressure_probe.py"):
        raise RuntimeError("held pressure-probe source path binding is invalid")
    name = "_chili_captured_paper_pressure_probe"
    module = ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = None
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module, digest


CONTRACT, RUNTIME, _CAPTURE_EXECUTED_SOURCE_HASHES = _load_capture_modules()
PRESSURE_PROBE, _PRESSURE_PROBE_EXECUTED_SHA256 = _load_pressure_probe_module()
(
    _BENCHMARK_SCRIPT_PATH,
    _BENCHMARK_SCRIPT_SOURCE,
    _BENCHMARK_SCRIPT_INITIAL_SHA256,
) = _held_source("benchmark")
(
    _BENCHMARK_STAGE0_PATH,
    _BENCHMARK_STAGE0_SOURCE,
    _BENCHMARK_STAGE0_SHA256,
) = _held_source("stage0")
if Path(__file__).resolve(strict=True) != _BENCHMARK_SCRIPT_PATH:
    raise RuntimeError("held benchmark execution path is invalid")
if _BENCHMARK_STAGE0_PATH != _BENCHMARK_SCRIPT_PATH.with_name(
    "captured_paper_isolated_stage0.py"
):
    raise RuntimeError("held benchmark stage0 path binding is invalid")
if hashlib.sha256(
    _stable_source_bytes(_BENCHMARK_SCRIPT_PATH)
).hexdigest() != _BENCHMARK_SCRIPT_INITIAL_SHA256:
    raise RuntimeError("held benchmark source differs from its current path")
CaptureClocks = CONTRACT.CaptureClocks
CaptureEvent = CONTRACT.CaptureEvent
CaptureRunIdentity = CONTRACT.CaptureRunIdentity
CaptureStream = CONTRACT.CaptureStream
canonical_json_bytes = CONTRACT.canonical_json_bytes
BoundedCaptureIngress = RUNTIME.BoundedCaptureIngress
CaptureResourceMeasurement = RUNTIME.CaptureResourceMeasurement
CaptureBudgetPolicy = RUNTIME.CaptureBudgetPolicy
CaptureResourceBinding = RUNTIME.CaptureResourceBinding
capture_storage_volume_identity_sha256 = (
    RUNTIME.capture_storage_volume_identity_sha256
)
CaptureWriterWorker = RUNTIME.CaptureWriterWorker
CaptureWriterPool = RUNTIME.CaptureWriterPool
ContentAddressedCaptureStore = RUNTIME.ContentAddressedCaptureStore
SharedCaptureAdmissionBudget = RUNTIME.SharedCaptureAdmissionBudget
SharedCaptureStoreRuntime = RUNTIME.SharedCaptureStoreRuntime


def _require_codec_available(codec: str) -> None:
    if codec == "zstd" and getattr(RUNTIME, "zstd", None) is None:
        raise RuntimeError(
            "zstd benchmark requested but the zstandard dependency is unavailable; "
            "no fallback codec was selected"
        )


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _at_least_two_int(raw: str) -> int:
    value = _positive_int(raw)
    if value < 2:
        raise argparse.ArgumentTypeError(
            "must be at least 2 to exercise aggregate shared-store concurrency"
        )
    return value


def _representative_event_count(raw: str) -> int:
    value = _positive_int(raw)
    if value < 1_000:
        raise argparse.ArgumentTypeError(
            "must be at least 1000 so every workload stream is represented"
        )
    return value


def _positive_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="Existing or creatable parent for the uniquely owned temporary directory.",
    )
    parser.add_argument("--events", type=_representative_event_count, default=100_000)
    parser.add_argument("--symbols", type=_positive_int, default=16)
    parser.add_argument("--queue-events", type=_positive_int, default=200_000)
    parser.add_argument("--queue-mib", type=_positive_int, default=512)
    parser.add_argument("--gap-keys", type=_positive_int, default=4_096)
    parser.add_argument("--batch-events", type=_positive_int, default=5_000)
    parser.add_argument("--batch-mib", type=_positive_int, default=16)
    parser.add_argument("--poll-ms", type=_positive_float, default=2.0)
    parser.add_argument("--flush-ms", type=_positive_float, default=100.0)
    parser.add_argument("--writers", type=_at_least_two_int, default=2)
    parser.add_argument(
        "--artifact-max-age-s",
        type=_positive_float,
        default=3_600.0,
        help="Maximum accepted age of this host calibration artifact.",
    )
    parser.add_argument("--stop-timeout-s", type=_positive_float, default=120.0)
    parser.add_argument("--rss-sample-ms", type=_positive_float, default=5.0)
    parser.add_argument(
        "--compression-codec",
        choices=("zstd", "zlib"),
        default="zstd",
    )
    parser.add_argument(
        "--compression-level",
        type=int,
        choices=range(1, 23),
        default=3,
    )
    parser.add_argument(
        "--payload-pack-records",
        type=_positive_int,
        default=2_048,
        help="Maximum logical payload records in one immutable physical pack.",
    )
    parser.add_argument(
        "--payload-pack-mib",
        type=_positive_int,
        default=8,
        help="Target uncompressed pack size; one oversized payload is never truncated.",
    )
    parser.add_argument(
        "--payload-pack-read-cache",
        type=_positive_int,
        default=4,
        help="Maximum decompressed payload packs retained by one loader pass.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Retain the uniquely owned benchmark directory for inspection.",
    )
    return parser


def _create_owned_directory(output_root: Path) -> tuple[Path, str]:
    root = output_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise RuntimeError(f"output root is not a directory: {root}")
    owner_token = uuid.uuid4().hex
    directory = Path(
        tempfile.mkdtemp(
            prefix=f"{OWNED_DIRECTORY_PREFIX}{owner_token}-",
            dir=str(root),
        )
    )
    marker = {
        "benchmark_schema_version": BENCHMARK_SCHEMA_VERSION,
        "directory": str(directory.resolve()),
        "output_root": str(root),
        "owner_token": owner_token,
    }
    marker_path = directory / OWNERSHIP_MARKER
    try:
        with marker_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    marker,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            handle.write("\n")
    except Exception:
        # ``mkdtemp`` created this exact empty child in this process.  If its
        # marker cannot be published, remove only that still-empty directory.
        try:
            directory.rmdir()
        except OSError:
            pass
        raise
    return directory, owner_token


def _delete_verified_owned_directory(
    directory: Path,
    *,
    output_root: Path,
    owner_token: str,
) -> None:
    """Delete exactly one directory after re-validating its ownership marker."""

    root = output_root.expanduser().resolve(strict=True)
    is_junction = getattr(directory, "is_junction", lambda: False)
    if directory.is_symlink() or is_junction():
        raise RuntimeError(
            "refusing cleanup: owned directory became a symlink or junction"
        )
    resolved = directory.resolve(strict=True)
    if resolved.parent != root:
        raise RuntimeError("refusing cleanup: owned directory escaped output root")
    expected_prefix = f"{OWNED_DIRECTORY_PREFIX}{owner_token}-"
    if not resolved.name.startswith(expected_prefix):
        raise RuntimeError("refusing cleanup: owned directory name/token mismatch")
    marker_path = resolved / OWNERSHIP_MARKER
    if marker_path.is_symlink() or not marker_path.is_file():
        raise RuntimeError("refusing cleanup: ownership marker is missing or unsafe")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("refusing cleanup: ownership marker is unreadable") from exc
    expected = {
        "benchmark_schema_version": BENCHMARK_SCHEMA_VERSION,
        "directory": str(resolved),
        "output_root": str(root),
        "owner_token": owner_token,
    }
    if marker != expected:
        raise RuntimeError("refusing cleanup: ownership marker content mismatch")
    shutil.rmtree(resolved)


_RESOURCE_SAMPLER_PROFILE = "chili.benchmark-stdlib-resource-sampler.v1"


def _logical_cpu_count() -> int:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GetActiveProcessorCount
        function.argtypes = [wintypes.WORD]
        function.restype = wintypes.DWORD
        value = int(function(0xFFFF))
        if value == 0:
            raise OSError(ctypes.get_last_error(), "GetActiveProcessorCount failed")
    else:
        value = os.cpu_count()
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError("logical CPU count is unavailable")
    return value


def _memory_snapshot() -> dict[str, int]:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = (
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            )

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GlobalMemoryStatusEx
        function.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
        function.restype = wintypes.BOOL
        if not function(ctypes.byref(status)):
            raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx failed")
        total = int(status.ullTotalPhys)
        available = int(status.ullAvailPhys)
    else:
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            total_pages = int(os.sysconf("SC_PHYS_PAGES"))
            available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError("physical memory counters are unavailable") from exc
        total = page_size * total_pages
        available = page_size * available_pages
    if total <= 0 or available < 0 or available > total:
        raise RuntimeError("physical memory counters are invalid")
    return {"total": total, "available": available}


def _current_process_rss_bytes() -> int:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = (
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            )

        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        function = psapi.GetProcessMemoryInfo
        function.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        function.restype = wintypes.BOOL
        if not function(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
        value = int(counters.WorkingSetSize)
    else:
        statm = Path("/proc/self/statm")
        if statm.is_file():
            fields = statm.read_text(encoding="ascii").split()
            if len(fields) < 2:
                raise RuntimeError("process RSS counter is malformed")
            value = int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE"))
        else:
            try:
                import resource

                raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            except (ImportError, OSError, TypeError, ValueError) as exc:
                raise RuntimeError("process RSS counter is unavailable") from exc
            value = raw if sys.platform == "darwin" else raw * 1024
    if value <= 0:
        raise RuntimeError("process RSS counter is invalid")
    return value


def _host_cpu_snapshot() -> tuple[int, int]:
    """Return cumulative ``(idle, total)`` scheduler ticks."""

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        idle = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GetSystemTimes
        function.argtypes = [
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        function.restype = wintypes.BOOL
        if not function(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise OSError(ctypes.get_last_error(), "GetSystemTimes failed")

        def _value(raw: Any) -> int:
            return (int(raw.dwHighDateTime) << 32) | int(raw.dwLowDateTime)

        idle_value = _value(idle)
        return idle_value, _value(kernel) + _value(user)
    stat_path = Path("/proc/stat")
    if not stat_path.is_file():
        raise RuntimeError("host CPU counters are unavailable")
    fields = stat_path.read_text(encoding="ascii").splitlines()[0].split()
    if not fields or fields[0] != "cpu" or len(fields) < 5:
        raise RuntimeError("host CPU counters are malformed")
    values = [int(value) for value in fields[1:]]
    idle_value = values[3] + (values[4] if len(values) > 4 else 0)
    return idle_value, sum(values)


def _host_cpu_percent(before: tuple[int, int], after: tuple[int, int]) -> float:
    idle_delta = int(after[0]) - int(before[0])
    total_delta = int(after[1]) - int(before[1])
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        raise RuntimeError("host CPU counter interval is invalid")
    return (total_delta - idle_delta) * 100.0 / total_delta


class PeakRssSampler:
    """Low-overhead process RSS sampler covering enqueue through writer drain."""

    def __init__(self, *, interval_seconds: float) -> None:
        self._interval = float(interval_seconds)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self.baseline_bytes = _current_process_rss_bytes()
        self.peak_bytes = self.baseline_bytes
        self.samples = 1

    def _sample(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                rss = _current_process_rss_bytes()
            except (OSError, RuntimeError) as exc:
                with self._lock:
                    self._error = exc
                return
            with self._lock:
                self.peak_bytes = max(self.peak_bytes, rss)
                self.samples += 1

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._sample,
            name="capture-benchmark-rss",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._interval * 4))
            if self._thread.is_alive():
                raise RuntimeError("RSS sampler thread did not stop")
        try:
            terminal = _current_process_rss_bytes()
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("terminal RSS sample is unavailable") from exc
        with self._lock:
            if self._error is not None:
                raise RuntimeError("RSS sampler failed during benchmark") from self._error
            self.peak_bytes = max(self.peak_bytes, terminal)
            self.samples += 1
            if self.samples < 2:
                raise RuntimeError("RSS sampler coverage is incomplete")


def _identity(run_id: str) -> Any:
    return CaptureRunIdentity(
        run_id=run_id,
        generation=1,
        code_build_sha256=hashlib.sha256(b"capture-benchmark-code").hexdigest(),
        config_sha256=hashlib.sha256(b"capture-benchmark-config").hexdigest(),
        feature_flags_sha256=hashlib.sha256(b"capture-benchmark-flags").hexdigest(),
        account_identity_sha256=hashlib.sha256(
            b"capture-benchmark-no-account"
        ).hexdigest(),
        broker="offline_benchmark",
        broker_environment="none",
    )


def _symbols(count: int) -> tuple[str, ...]:
    anchors = (
        "VEEE",
        "PLSM",
        "NXTC",
        "UBXG",
        "SILO",
        "CLRO",
        "ZDAI",
        "SDOT",
    )
    values = list(anchors[:count])
    while len(values) < count:
        values.append(f"B{len(values):04d}")
    return tuple(values)


def _event(
    *,
    identity: Any,
    sequence: int,
    symbols: tuple[str, ...],
    base_time: datetime,
) -> Any:
    """Construct one deterministic event from a 1,000-event workload cycle."""

    slot = (sequence - 1) % 1_000
    symbol_index = (sequence - 1) % len(symbols)
    symbol = symbols[symbol_index]
    event_at = base_time + timedelta(microseconds=100 * sequence)
    received_at = event_at + timedelta(microseconds=40 + sequence % 17)
    available_at = received_at + timedelta(microseconds=10 + sequence % 7)
    base_price = 4.0 + symbol_index * 0.37 + (sequence % 211) * 0.001
    bid = round(base_price, 4)
    ask = round(base_price + 0.01 + (sequence % 3) * 0.005, 4)

    provider_event_stream = True
    market_reference_at: datetime | None = None
    query: dict[str, Any] | None = None
    if slot < 390:
        stream = CaptureStream.NBBO_QUOTE
        provider = "iqfeed_l1"
        payload = {
            "ask": ask,
            "ask_size": 100 + sequence % 900,
            "bid": bid,
            "bid_size": 100 + (sequence * 3) % 900,
            "condition": "regular",
            "feed_sequence": sequence,
        }
    elif slot < 690:
        stream = CaptureStream.IQFEED_PRINT
        provider = "iqfeed"
        payload = {
            "ask": ask,
            "bid": bid,
            "condition": "normal",
            "feed_sequence": sequence,
            "price": round(bid + (ask - bid) * ((sequence % 5) / 4), 4),
            "size": 25 + (sequence * 13) % 4_975,
        }
    elif slot < 940:
        stream = CaptureStream.L2_DEPTH_DELTA
        provider = "iqfeed_depth"
        side = "bid" if sequence % 2 else "ask"
        payload = {
            "book_sequence": sequence,
            "level": 1 + sequence % 10,
            "operation": ("update", "insert", "delete")[sequence % 3],
            "price": bid if side == "bid" else ask,
            "side": side,
            "size": (sequence * 29) % 20_000,
        }
    elif slot < 950:
        stream = CaptureStream.L2_DEPTH_CHECKPOINT
        provider = "iqfeed_depth"
        payload = {
            "asks": [
                [round(ask + level * 0.01, 4), 500 + level * 73]
                for level in range(10)
            ],
            "bids": [
                [round(bid - level * 0.01, 4), 600 + level * 67]
                for level in range(10)
            ],
            "book_sequence": sequence,
        }
    elif slot < 965:
        stream = CaptureStream.PROVIDER_OHLCV
        provider = "massive"
        provider_event_stream = False
        market_reference_at = event_at - timedelta(minutes=1)
        query = {
            "adjusted": True,
            "from": (event_at - timedelta(minutes=30)).isoformat(),
            "interval": "1m",
            "symbol": symbol,
            "to": event_at.isoformat(),
        }
        payload = {
            "bars": [
                {
                    "c": round(base_price + minute * 0.002, 4),
                    "h": round(base_price + minute * 0.002 + 0.03, 4),
                    "l": round(base_price + minute * 0.002 - 0.02, 4),
                    "o": round(base_price + minute * 0.002 - 0.005, 4),
                    "t": (
                        market_reference_at - timedelta(minutes=29 - minute)
                    ).isoformat(),
                    "v": 10_000 + minute * 137 + sequence % 1_000,
                }
                for minute in range(30)
            ],
            "provider_request_id": f"bench-{sequence}",
        }
    elif slot < 970:
        stream = CaptureStream.ORTEX_SNAPSHOT
        provider = "ortex"
        provider_event_stream = False
        market_reference_at = event_at - timedelta(milliseconds=250)
        query = {
            "fields": ["float", "short_interest", "utilization"],
            "symbol": symbol,
        }
        payload = {
            "float": 1_000_000 + sequence * 10,
            "short_interest": round(0.10 + (sequence % 40) / 100, 4),
            "snapshot_id": f"ortex-{sequence}",
            "utilization": round(0.50 + (sequence % 50) / 100, 4),
        }
    elif slot < 980:
        stream = CaptureStream.SCANNER_SNAPSHOT
        provider = "massive_scanner"
        provider_event_stream = False
        market_reference_at = event_at
        query = {
            "include_otc": False,
            "max_age_seconds": 300.0,
            "operation": "full_market_snapshot_ross_projection",
        }
        payload = {
            "change_id": sequence,
            "gap_percent": round(10 + sequence % 80, 2),
            "rank": 1 + sequence % 50,
            "relative_volume": round(5 + (sequence % 200) / 10, 2),
        }
    elif slot < 990:
        stream = CaptureStream.HALT_LULD_STATE
        provider = "iqfeed"
        payload = {
            "change_id": sequence,
            "luld_band_high": round(ask * 1.10, 4),
            "luld_band_low": round(bid * 0.90, 4),
            "state": "halted" if sequence % 2 else "resumed",
        }
    elif slot < 995:
        stream = CaptureStream.SSR_STATE
        provider = "alpaca_assets"
        provider_event_stream = False
        market_reference_at = event_at
        payload = {
            "change_id": sequence,
            "effective": bool(sequence % 2),
        }
    else:
        stream = CaptureStream.MARKET_SESSION_STATE
        provider = "exchange_calendar"
        provider_event_stream = False
        market_reference_at = event_at
        payload = {
            "change_id": sequence,
            "session": ("premarket", "regular", "afterhours")[sequence % 3],
        }

    clocks = CaptureClocks(
        provider_event_at=event_at if provider_event_stream else None,
        market_reference_at=market_reference_at,
        received_at=received_at,
        available_at=available_at,
    )
    return CaptureEvent(
        identity=identity,
        sequence=sequence,
        stream=stream,
        symbol=symbol,
        provider=provider,
        clocks=clocks,
        query=query,
        payload=payload,
    )


def _percentile(sorted_values: list[int], percentile: float) -> int:
    if not sorted_values:
        return 0
    rank = max(0, math.ceil(percentile * len(sorted_values)) - 1)
    return int(sorted_values[rank])


def _latency_summary(values: list[int]) -> dict[str, int]:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "max_ns": int(ordered[-1]) if ordered else 0,
        "mean_ns": int(sum(ordered) / len(ordered)) if ordered else 0,
        "min_ns": int(ordered[0]) if ordered else 0,
        "p50_ns": _percentile(ordered, 0.50),
        "p95_ns": _percentile(ordered, 0.95),
        "p99_ns": _percentile(ordered, 0.99),
    }


def _rate(numerator: int | float, seconds: float) -> float:
    return round(float(numerator) / max(float(seconds), 1e-12), 6)


def _publish_with_parent_durability(source: Path, target: Path) -> tuple[str, int]:
    """Atomically publish after file fsync and make the directory entry durable."""

    started = time.perf_counter_ns()
    if os.name == "nt":
        # Windows has no portable directory fsync.  MOVEFILE_WRITE_THROUGH is
        # the documented durable-publication primitive for a rename.
        import ctypes

        move_file_ex = ctypes.windll.kernel32.MoveFileExW
        move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move_file_ex.restype = ctypes.c_int
        movefile_replace_existing = 0x1
        movefile_write_through = 0x8
        if not move_file_ex(
            str(source),
            str(target),
            movefile_replace_existing | movefile_write_through,
        ):
            error = ctypes.get_last_error()
            raise OSError(error, "MoveFileExW(MOVEFILE_WRITE_THROUGH) failed")
        method = "movefileex_write_through"
    else:
        os.replace(source, target)
        descriptor = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        method = "parent_directory_fsync"
    return method, time.perf_counter_ns() - started


def _durable_publication_summary(
    owned_root: Path,
    *,
    samples: int = 8,
) -> dict[str, Any]:
    """Write, file-fsync, atomically publish, and verify owned probe objects."""

    probe_root = owned_root / "durability-probe"
    probe_root.mkdir(parents=True, exist_ok=False)
    file_values: list[int] = []
    live_probe_values: list[int] = []
    parent_values: list[int] = []
    methods: set[str] = set()
    verified = 0
    with PRESSURE_PROBE.CapturedPaperPressureProbeClient(
        python_executable=sys.executable,
        probe_root=probe_root,
        response_timeout_seconds=5.0,
        helper_path=Path(PRESSURE_PROBE.__file__).resolve(strict=True),
        expected_helper_sha256=_PRESSURE_PROBE_EXECUTED_SHA256,
    ) as pressure_probe:
        pressure_probe_helper_sha256 = pressure_probe.helper_sha256
        pressure_probe_root_identity_sha256 = (
            pressure_probe.probe_root_identity_sha256
        )
        pressure_probe_volume_identity_sha256 = (
            capture_storage_volume_identity_sha256(probe_root)
        )
        for _index in range(samples):
            observed = pressure_probe.measure()
            if (
                observed.write_latency_profile
                != PRESSURE_PROBE.PRESSURE_WRITE_LATENCY_PROFILE
                or observed.bytes_written != 4096
                or observed.fsync_completed is not True
                or observed.cleanup_completed is not True
            ):
                raise RuntimeError(
                    "capture pressure probe returned incomplete benchmark evidence"
                )
            live_probe_values.append(int(observed.latency_ns))
    for index in range(samples):
        raw = canonical_json_bytes(
            {
                "benchmark_schema_version": BENCHMARK_SCHEMA_VERSION,
                "index": index,
                "nonce": uuid.uuid4().hex,
            }
        )
        digest = hashlib.sha256(raw).hexdigest()
        temporary = probe_root / f".{digest}.pending"
        published = probe_root / f"{digest}.probe"
        started = time.perf_counter_ns()
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        file_values.append(time.perf_counter_ns() - started)
        method, parent_ns = _publish_with_parent_durability(temporary, published)
        methods.add(method)
        parent_values.append(parent_ns)
        if (
            published.read_bytes() == raw
            and hashlib.sha256(published.read_bytes()).hexdigest() == digest
        ):
            verified += 1
    return {
        "sample_count": samples,
        "verified_count": verified,
        "all_verified": verified == samples,
        "live_pressure_probe": {
            **_latency_summary(live_probe_values),
            "all_verified": len(live_probe_values) == samples,
            "bytes_per_sample": 4096,
            "helper_sha256": pressure_probe_helper_sha256,
            "probe_root_identity_sha256": (
                pressure_probe_root_identity_sha256
            ),
            "probe_volume_identity_sha256": (
                pressure_probe_volume_identity_sha256
            ),
            "write_latency_profile": (
                PRESSURE_PROBE.PRESSURE_WRITE_LATENCY_PROFILE
            ),
        },
        "file_fsync": _latency_summary(file_values),
        "parent_publication": {
            **_latency_summary(parent_values),
            "methods": sorted(methods),
        },
    }


def _persist_content_addressed_report(directory: Path, raw: bytes) -> Path:
    """Durably retain the exact canonical stdout report under an owned root."""

    digest = hashlib.sha256(raw).hexdigest()
    report_root = directory / "reports"
    report_root.mkdir(parents=True, exist_ok=False)
    temporary = report_root / f".{digest}.pending"
    published = report_root / f"{digest}.json"
    with temporary.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    _publish_with_parent_durability(temporary, published)
    if published.read_bytes() != raw:
        raise RuntimeError("persisted benchmark report bytes changed after publication")
    return published


def _host_fingerprint(total_memory_bytes: int) -> str:
    material = {
        "logical_cpu_count": _logical_cpu_count(),
        "machine": platform.machine(),
        "node": platform.node(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "total_memory_bytes": int(total_memory_bytes),
    }
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def _capture_file_inventory(capture_root: Path) -> dict[str, Any]:
    event_files = tuple((capture_root / "events").rglob("*.jsonl.zlib")) + tuple(
        (capture_root / "events").rglob("*.jsonl.zst")
    )
    gap_files = tuple((capture_root / "gaps").rglob("*.jsonl.zlib")) + tuple(
        (capture_root / "gaps").rglob("*.jsonl.zst")
    )
    blob_files = tuple((capture_root / "blobs").rglob("*.json.zlib")) + tuple(
        (capture_root / "blobs").rglob("*.json.zst")
    )
    pack_files = tuple(
        path for path in blob_files if "packs" in path.relative_to(capture_root).parts
    )
    pack_file_set = set(pack_files)
    standalone_blob_files = tuple(
        path for path in blob_files if path not in pack_file_set
    )
    compressed_files = (*event_files, *gap_files, *blob_files)
    compressed_bytes = 0
    raw_bytes = 0
    payload_records = len(standalone_blob_files)
    for path in compressed_files:
        compressed = path.read_bytes()
        compressed_bytes += len(compressed)
        raw = ContentAddressedCaptureStore._decompress(path, compressed)
        raw_bytes += len(raw)
        if path in pack_file_set:
            try:
                pack = json.loads(raw.decode("utf-8"))
                rows = pack["payloads"]
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise RuntimeError(f"benchmark found malformed payload pack: {path}") from exc
            if not isinstance(rows, list):
                raise RuntimeError(f"benchmark found malformed payload pack: {path}")
            payload_records += len(rows)
    all_files = tuple(path for path in capture_root.rglob("*") if path.is_file())
    disk_bytes = sum(path.stat().st_size for path in all_files)
    return {
        "compression": {
            "compressed_bytes": compressed_bytes,
            "ratio_raw_to_compressed": round(
                raw_bytes / compressed_bytes if compressed_bytes else 0.0,
                6,
            ),
            "raw_bytes": raw_bytes,
            "saved_bytes": raw_bytes - compressed_bytes,
            "saved_percent": round(
                (1.0 - compressed_bytes / raw_bytes) * 100 if raw_bytes else 0.0,
                6,
            ),
        },
        "files": {
            "event_chunks": len(event_files),
            "gap_chunks": len(gap_files),
            "other": len(all_files) - len(compressed_files),
            "payload_packs": len(pack_files),
            "standalone_payload_blobs": len(standalone_blob_files),
            "logical_payload_records": payload_records,
            # Compatibility name: this is the number of physical payload
            # objects, now normally packs rather than one file per payload.
            "payload_blobs": len(blob_files),
            "physical_capture_objects": len(compressed_files),
            "total": len(all_files),
            "total_disk_bytes": disk_bytes,
        },
    }


def _verified_executed_source_hashes() -> dict[str, str]:
    errors_path, _, _ = _held_source("replay_errors")
    policy_path, _, _ = _held_source("first_dip_tape_policy")
    current = {
        "contract_sha256": hashlib.sha256(
            _stable_source_bytes(Path(str(CONTRACT.__file__)))
        ).hexdigest(),
        "runtime_sha256": hashlib.sha256(
            _stable_source_bytes(Path(str(RUNTIME.__file__)))
        ).hexdigest(),
        "pressure_probe_sha256": hashlib.sha256(
            _stable_source_bytes(Path(str(PRESSURE_PROBE.__file__)))
        ).hexdigest(),
        "replay_errors_sha256": hashlib.sha256(
            _stable_source_bytes(errors_path)
        ).hexdigest(),
        "first_dip_tape_policy_sha256": hashlib.sha256(
            _stable_source_bytes(policy_path)
        ).hexdigest(),
        "benchmark_script_sha256": hashlib.sha256(
            _stable_source_bytes(_BENCHMARK_SCRIPT_PATH)
        ).hexdigest(),
        "stage0_sha256": hashlib.sha256(
            _stable_source_bytes(_BENCHMARK_STAGE0_PATH)
        ).hexdigest(),
    }
    expected = {
        **_CAPTURE_EXECUTED_SOURCE_HASHES,
        "pressure_probe_sha256": _PRESSURE_PROBE_EXECUTED_SHA256,
        "benchmark_script_sha256": _BENCHMARK_SCRIPT_INITIAL_SHA256,
        "stage0_sha256": _BENCHMARK_STAGE0_SHA256,
    }
    if current != expected:
        raise RuntimeError("benchmark source bytes drifted during measurement")
    return expected


def _resolved_binding(
    args: argparse.Namespace,
    measurement: Any,
) -> Any:
    """Resolve finite validation budgets from this exact host measurement."""

    available = int(measurement.available_memory_bytes)
    disk_free = int(measurement.disk_free_bytes)
    memory_reserve = min(
        available - 1,
        max(64 * MEBIBYTE, available // 3),
    )
    disk_reserve = min(
        disk_free - 1,
        max(256 * MEBIBYTE, disk_free // 10),
    )
    policy = CaptureBudgetPolicy(
        memory_reserve_bytes=max(1, memory_reserve),
        disk_reserve_bytes=max(1, disk_reserve),
        capture_fraction_of_memory_headroom=0.35,
        ring_fraction_of_capture_memory=0.30,
        queue_fraction_of_capture_memory=0.30,
        capture_fraction_of_disk_headroom=0.25,
        capture_fraction_of_measured_write_bandwidth=0.50,
        max_average_cpu_percent=95.0,
        capture_fraction_of_cpu_headroom=0.90,
        calibrated_hot_symbol_bytes=max(1 * MEBIBYTE, args.payload_pack_mib * MEBIBYTE),
        max_queue_events=max(args.queue_events, 1_000),
        max_ring_events=max(args.queue_events, 1_000),
        max_gap_keys=max(args.gap_keys, 64),
        raw_retention_days=3,
        derived_retention_days=90,
        pressure_cpu_enter_percent=92.0,
        pressure_cpu_exit_percent=80.0,
        pressure_memory_enter_margin_bytes=max(1, memory_reserve // 8),
        pressure_memory_exit_margin_bytes=max(2, memory_reserve // 4),
        pressure_disk_enter_margin_bytes=max(1, disk_reserve // 8),
        pressure_disk_exit_margin_bytes=max(2, disk_reserve // 4),
        pressure_write_latency_enter_milliseconds=max(
            100.0, measurement.fsync_p95_milliseconds * 4.0
        ),
        pressure_write_latency_exit_milliseconds=max(
            25.0, measurement.fsync_p95_milliseconds * 1.5
        ),
        pressure_enter_samples=3,
        pressure_recovery_samples=3,
        pressure_sample_max_age_seconds=5.0,
        store_owner_lease_seconds=60.0,
        store_owner_heartbeat_seconds=10.0,
    )
    return CaptureResourceBinding.resolve(measurement, policy)


def _run_shared_store_validation(
    args: argparse.Namespace,
    directory: Path,
    *,
    binding: Any,
) -> dict[str, Any]:
    """Exercise multiple run identities through one exact quota/store runtime."""

    requested = int(args.writers)
    if binding.budget.max_writer_threads < requested:
        return {
            "executed": False,
            "requested_identity_count": requested,
            "identity_count": 0,
            "reason": "measured_writer_capacity_below_requested_concurrency",
        }
    shared_admission = SharedCaptureAdmissionBudget.from_resource_binding(binding)
    shared_root = directory / "shared-capture"
    manager = SharedCaptureStoreRuntime.create(
        shared_root,
        resource_binding=binding,
        shared_admission_budget=shared_admission,
        compression_codec=args.compression_codec,
        compression_level=args.compression_level,
        payload_pack_max_records=args.payload_pack_records,
        payload_pack_target_raw_bytes=args.payload_pack_mib * MEBIBYTE,
        payload_pack_read_cache_entries=args.payload_pack_read_cache,
    )
    identities = tuple(_identity(str(uuid.uuid4())) for _ in range(requested))
    ingresses = tuple(
        BoundedCaptureIngress.from_resource_binding(
            binding,
            shared_admission_budget=shared_admission,
        )
        for _ in identities
    )
    leases = tuple(manager.acquire(identity) for identity in identities)
    writers = tuple(
        lease.build_writer(
            ingress=ingress,
            batch_events=min(args.batch_events, binding.budget.max_queue_events),
            batch_bytes=min(args.batch_mib * MEBIBYTE, binding.budget.async_queue_bytes),
            poll_seconds=args.poll_ms / 1_000,
            flush_interval_seconds=args.flush_ms / 1_000,
        )
        for lease, ingress in zip(leases, ingresses, strict=True)
    )
    for writer in writers:
        writer.start()
    base = datetime.now(UTC).replace(microsecond=0)
    per_identity = max(500, min(5_000, args.events // requested))
    accepted_by_identity: dict[str, int] = {}
    canonical_bytes = 0
    for run_index, (identity, ingress) in enumerate(
        zip(identities, ingresses, strict=True)
    ):
        symbols = ("PLSM",) if run_index % 2 == 0 else ("VEEE",)
        accepted = 0
        for sequence in range(1, per_identity + 1):
            event = _event(
                identity=identity,
                sequence=sequence,
                symbols=symbols,
                base_time=base + timedelta(seconds=run_index),
            )
            if ingress.submit(event):
                accepted += 1
                canonical_bytes += int(event.canonical_size_bytes)
        accepted_by_identity[identity.identity_sha256] = accepted
    stopped = tuple(
        writer.stop(timeout_seconds=args.stop_timeout_s) for writer in writers
    )
    writer_health = tuple(writer.health() for writer in writers)
    before_release = manager.health()
    first_lease = leases[0]
    first_lease.release()
    after_first_release = manager.health()
    survivor_store_access = bool(
        leases[1].store is manager.store
        and after_first_release["lease_count"] == requested - 1
    )
    for lease in leases[1:]:
        lease.release()
    before_close = manager.health()
    inventory = _capture_file_inventory(shared_root)
    manager.close()
    aggregate = shared_admission.health()
    return {
        "executed": True,
        "requested_identity_count": requested,
        "identity_count": len(identities),
        "identity_sha256s": sorted(row.identity_sha256 for row in identities),
        "common_root": str(shared_root.resolve()),
        "resource_binding_sha256": binding.binding_sha256,
        "accepted_by_identity": dict(sorted(accepted_by_identity.items())),
        "accepted_events": sum(accepted_by_identity.values()),
        "accepted_canonical_bytes": canonical_bytes,
        "writers_stopped_cleanly": all(stopped),
        "writer_health": writer_health,
        "manager_before_release": before_release,
        "manager_after_first_release": after_first_release,
        "manager_before_close": before_close,
        "survivor_store_access_after_first_release": survivor_store_access,
        "aggregate_admission": aggregate,
        "storage": inventory,
        "closed": True,
    }


def _run_benchmark(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    _require_codec_available(args.compression_codec)
    capture_root = directory / "capture"
    measurement_started_at = datetime.now(UTC)
    host_sample_started = time.perf_counter()
    memory_before = _memory_snapshot()
    host_cpu_before = _host_cpu_snapshot()
    identity = _identity(str(uuid.uuid4()))
    symbols = _symbols(args.symbols)
    ingress = BoundedCaptureIngress(
        max_events=args.queue_events,
        max_bytes=args.queue_mib * MEBIBYTE,
        max_gap_keys=args.gap_keys,
    )
    store = ContentAddressedCaptureStore(
        capture_root,
        compression_codec=args.compression_codec,
        compression_level=args.compression_level,
        payload_pack_max_records=args.payload_pack_records,
        payload_pack_target_raw_bytes=args.payload_pack_mib * MEBIBYTE,
        payload_pack_read_cache_entries=args.payload_pack_read_cache,
    )
    writer_type = CaptureWriterWorker if args.writers == 1 else CaptureWriterPool
    writer_kwargs = {
        "ingress": ingress,
        "store": store,
        "batch_events": args.batch_events,
        "batch_bytes": args.batch_mib * MEBIBYTE,
        "poll_seconds": args.poll_ms / 1_000,
        "flush_interval_seconds": args.flush_ms / 1_000,
    }
    if args.writers > 1:
        writer_kwargs["workers"] = args.writers
    writer = writer_type(**writer_kwargs)
    rss = PeakRssSampler(interval_seconds=args.rss_sample_ms / 1_000)
    cpu_before = time.process_time()
    producer_clock: Callable[[], int]
    producer_cpu_clock = "thread_time_ns"
    if hasattr(time, "thread_time_ns"):
        producer_clock = time.thread_time_ns
    else:
        producer_clock = time.process_time_ns
        producer_cpu_clock = "process_time_ns_fallback"

    workload_base = datetime.now(UTC).replace(microsecond=0)
    stream_counts: Counter[str] = Counter()
    accepted_counts: Counter[str] = Counter()
    enqueue_latencies: list[int] = []
    enqueue_cpu_ns = 0
    accepted = 0
    accepted_canonical_bytes = 0

    rss.start()
    benchmark_started = time.perf_counter()
    writer.start()
    producer_started = time.perf_counter()
    for sequence in range(1, args.events + 1):
        event = _event(
            identity=identity,
            sequence=sequence,
            symbols=symbols,
            base_time=workload_base,
        )
        stream_counts[event.stream.value] += 1
        canonical_size = int(event.canonical_size_bytes)
        cpu_start = producer_clock()
        latency_start = time.perf_counter_ns()
        submitted = bool(ingress.submit(event))
        enqueue_latencies.append(time.perf_counter_ns() - latency_start)
        enqueue_cpu_ns += producer_clock() - cpu_start
        if submitted:
            accepted += 1
            accepted_canonical_bytes += canonical_size
            accepted_counts[event.stream.value] += 1
    producer_finished = time.perf_counter()
    stopped_cleanly = writer.stop(timeout_seconds=args.stop_timeout_s)
    writer_finished = time.perf_counter()
    rss.stop()
    cpu_after = time.process_time()
    worker_health = writer.health()
    if not stopped_cleanly:
        raise RuntimeError(f"capture writer did not stop cleanly: {worker_health}")
    if int(worker_health["events_written"]) != accepted:
        raise RuntimeError(
            "writer/ingress accepted-count mismatch: "
            f"accepted={accepted} health={worker_health}"
        )

    inventory = _capture_file_inventory(capture_root)
    durable_publication = _durable_publication_summary(directory)
    capture_volume_identity = capture_storage_volume_identity_sha256(capture_root)
    if (
        durable_publication["live_pressure_probe"][
            "probe_volume_identity_sha256"
        ]
        != capture_volume_identity
    ):
        raise RuntimeError("benchmark capture and live probe volumes differ")
    memory_after = _memory_snapshot()
    host_cpu_after = _host_cpu_snapshot()
    average_cpu_percent = _host_cpu_percent(host_cpu_before, host_cpu_after)
    disk_after = shutil.disk_usage(capture_root)
    host_sample_seconds = max(
        time.perf_counter() - host_sample_started, 1e-12
    )
    producer_seconds = producer_finished - producer_started
    writer_seconds = writer_finished - benchmark_started
    process_cpu_seconds = (
        cpu_after - cpu_before
    )
    compression = inventory["compression"]
    physical_capture_objects = int(
        inventory["files"]["physical_capture_objects"]
    )
    written = int(worker_health["events_written"])
    resource_measurement = CaptureResourceMeasurement(
        measured_at=datetime.now(UTC),
        sample_seconds=host_sample_seconds,
        total_memory_bytes=int(memory_after["total"]),
        available_memory_bytes=int(
            min(memory_before["available"], memory_after["available"])
        ),
        disk_free_bytes=int(disk_after.free),
        average_cpu_percent=average_cpu_percent,
        sustained_append_bytes_per_second=(
            float(accepted_canonical_bytes) / max(writer_seconds, 1e-12)
        ),
        fsync_p95_milliseconds=(
            float(durable_publication["live_pressure_probe"]["p95_ns"])
        )
        / 1_000_000,
        logical_cpu_count=_logical_cpu_count(),
        host_fingerprint_sha256=_host_fingerprint(int(memory_after["total"])),
        write_latency_measurement_profile=(
            PRESSURE_PROBE.PRESSURE_WRITE_LATENCY_PROFILE
        ),
        write_latency_probe_volume_identity_sha256=str(
            durable_publication["live_pressure_probe"][
                "probe_volume_identity_sha256"
            ]
        ),
    )
    store.close()
    measurement_ended_at = datetime.now(UTC)
    binding: Any | None = None
    binding_error: str | None = None
    try:
        binding = _resolved_binding(args, resource_measurement)
    except Exception as exc:
        binding_error = f"{type(exc).__name__}: {exc}"
    shared_validation = (
        _run_shared_store_validation(args, directory, binding=binding)
        if binding is not None
        else {
            "executed": False,
            "requested_identity_count": int(args.writers),
            "identity_count": 0,
            "reason": "resource_binding_unavailable",
        }
    )
    generated_at = datetime.now(UTC)
    current_host_fingerprint = _host_fingerprint(
        int(_memory_snapshot()["total"])
    )
    host_match = (
        current_host_fingerprint == resource_measurement.host_fingerprint_sha256
    )
    artifact_age_seconds = max(
        0.0, (generated_at - resource_measurement.measured_at).total_seconds()
    )
    acceptance_reasons: list[str] = []
    if accepted != args.events or int(worker_health["events_written"]) != accepted:
        acceptance_reasons.append("calibration_event_reconciliation_failed")
    if not durable_publication["all_verified"]:
        acceptance_reasons.append("durable_file_or_parent_publication_unverified")
    if not host_match:
        acceptance_reasons.append("measurement_host_fingerprint_mismatch")
    if artifact_age_seconds > args.artifact_max_age_s:
        acceptance_reasons.append("measurement_artifact_stale")
    if binding is None:
        acceptance_reasons.append("measured_resource_binding_unavailable")
    elif int(binding.budget.max_writer_threads) < 2:
        acceptance_reasons.append("measured_writer_capacity_below_two")
    if not shared_validation.get("executed"):
        acceptance_reasons.append(
            str(shared_validation.get("reason") or "shared_validation_not_executed")
        )
    else:
        aggregate = shared_validation["aggregate_admission"]
        if int(shared_validation["identity_count"]) < 2:
            acceptance_reasons.append("shared_validation_has_fewer_than_two_identities")
        if not shared_validation["writers_stopped_cleanly"]:
            acceptance_reasons.append("shared_writer_shutdown_not_clean")
        if not shared_validation["survivor_store_access_after_first_release"]:
            acceptance_reasons.append("shared_store_invalidated_by_single_release")
        if (
            int(aggregate["outstanding_events"]) != 0
            or int(aggregate["outstanding_bytes"]) != 0
        ):
            acceptance_reasons.append("shared_admission_reservations_not_drained")
        if aggregate["rejections"]:
            acceptance_reasons.append("shared_admission_rejected_representative_input")
        if int(aggregate["completed"]) != int(
            shared_validation["accepted_events"]
        ):
            acceptance_reasons.append("shared_admission_completion_mismatch")
    executed_source_hashes = _verified_executed_source_hashes()
    if capture_storage_volume_identity_sha256(capture_root) != capture_volume_identity:
        raise RuntimeError("benchmark capture volume changed during measurement")
    report = {
        "benchmark_schema_version": BENCHMARK_SCHEMA_VERSION,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "measurement_window": {
            "started_at": measurement_started_at.isoformat().replace("+00:00", "Z"),
            "ended_at": measurement_ended_at.isoformat().replace("+00:00", "Z"),
            "sample_seconds": resource_measurement.sample_seconds,
            "event_count": accepted,
        },
        "artifact_freshness": {
            "age_seconds_at_emit": artifact_age_seconds,
            "max_age_seconds": args.artifact_max_age_s,
            "fresh_at_emit": artifact_age_seconds <= args.artifact_max_age_s,
        },
        "acceptance": {
            "accepted": not acceptance_reasons,
            "reasons": acceptance_reasons,
        },
        "authority": {
            "capacity_authority": CAPACITY_AUTHORITY,
            "empirical_calibration_receipt_sha256": None,
            "hot_symbol_limit_authorized": False,
            "reasons": list(CAPACITY_AUTHORITY_REASONS),
            "watcher_limit_authorized": False,
            "writer_limit_authorized": False,
        },
        "capture_identity": {
            "generation": identity.generation,
            "identity_sha256": identity.identity_sha256,
            "run_id": identity.run_id,
        },
        "capture_runtime_source": {
            **executed_source_hashes,
        },
        "enqueue": {
            "accepted": accepted,
            "accepted_canonical_bytes": accepted_canonical_bytes,
            "accepted_by_stream": dict(sorted(accepted_counts.items())),
            "accepted_canonical_bytes_per_second": _rate(
                accepted_canonical_bytes, writer_seconds
            ),
            "cpu_clock": producer_cpu_clock,
            "cpu_ns": enqueue_cpu_ns,
            "latency": _latency_summary(enqueue_latencies),
            "producer_seconds": round(producer_seconds, 9),
            "submitted": args.events,
            "submitted_by_stream": dict(sorted(stream_counts.items())),
            "submitted_per_second": _rate(args.events, producer_seconds),
        },
        "environment": {
            "benchmark_authority_manifest_sha256": (
                _BENCHMARK_AUTHORITY_MANIFEST_SHA256
            ),
            "dependency_root_identity_sha256": (
                _BENCHMARK_DEPENDENCY_IDENTITY_SHA256
            ),
            "logical_cpu_count": _logical_cpu_count(),
            "platform": platform.platform(),
            "python_executable_sha256": _BENCHMARK_PYTHON_EXECUTABLE_SHA256,
            "resource_sampler_profile": _RESOURCE_SAMPLER_PROFILE,
            "python": platform.python_version(),
            "measurement_host_fingerprint_sha256": (
                resource_measurement.host_fingerprint_sha256
            ),
            "current_host_fingerprint_sha256": current_host_fingerprint,
            "host_fingerprint_matches": host_match,
        },
        "parameters": {
            "batch_bytes": args.batch_mib * MEBIBYTE,
            "batch_events": args.batch_events,
            "compression_level": args.compression_level,
            "compression_codec": args.compression_codec,
            "events": args.events,
            "flush_interval_seconds": args.flush_ms / 1_000,
            "gap_keys": args.gap_keys,
            "poll_seconds": args.poll_ms / 1_000,
            "payload_pack_max_records": args.payload_pack_records,
            "payload_pack_read_cache_entries": args.payload_pack_read_cache,
            "payload_pack_target_raw_bytes": args.payload_pack_mib * MEBIBYTE,
            "queue_bytes": args.queue_mib * MEBIBYTE,
            "queue_events": args.queue_events,
            "rss_sample_seconds": args.rss_sample_ms / 1_000,
            "symbols": args.symbols,
            "writers": args.writers,
        },
        "process": {
            "cpu_seconds": round(process_cpu_seconds, 9),
            "cpu_seconds_per_wall_second": round(
                process_cpu_seconds / max(writer_seconds, 1e-12),
                6,
            ),
            "peak_rss": {
                "baseline_bytes": rss.baseline_bytes,
                "delta_bytes": max(0, rss.peak_bytes - rss.baseline_bytes),
                "peak_bytes": rss.peak_bytes,
                "samples": rss.samples,
                "scope": "enqueue_through_writer_drain",
            },
        },
        "resource_measurement": {
            "measured_at": resource_measurement.measured_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "sample_seconds": resource_measurement.sample_seconds,
            "total_memory_bytes": resource_measurement.total_memory_bytes,
            "available_memory_bytes": resource_measurement.available_memory_bytes,
            "disk_free_bytes": resource_measurement.disk_free_bytes,
            "average_cpu_percent": resource_measurement.average_cpu_percent,
            "sustained_append_bytes_per_second": (
                resource_measurement.sustained_append_bytes_per_second
            ),
            "fsync_p95_milliseconds": resource_measurement.fsync_p95_milliseconds,
            "logical_cpu_count": resource_measurement.logical_cpu_count,
            "durable_publication": durable_publication,
            "host_fingerprint_sha256": (
                resource_measurement.host_fingerprint_sha256
            ),
            "write_latency_measurement_profile": (
                resource_measurement.write_latency_measurement_profile
            ),
            "write_latency_probe_volume_identity_sha256": (
                resource_measurement.write_latency_probe_volume_identity_sha256
            ),
            "measurement_sha256": resource_measurement.measurement_sha256,
        },
        "resolved_resource_binding": (
            {
                **binding.to_record(),
                "binding_sha256": binding.binding_sha256,
                "hashes": binding.hashes,
                "max_writer_threads": binding.budget.max_writer_threads,
            }
            if binding is not None
            else {"error": binding_error}
        ),
        "shared_store_validation": shared_validation,
        "storage": {
            **inventory,
            "object_amplification": {
                "physical_capture_objects_per_1000_events": round(
                    physical_capture_objects * 1_000 / max(written, 1), 6
                ),
                "logical_payload_records_per_physical_payload_object": round(
                    int(inventory["files"]["logical_payload_records"])
                    / max(int(inventory["files"]["payload_blobs"]), 1),
                    6,
                ),
            },
            "policy": {
                **store.storage_policy.to_record(),
                "policy_sha256": store.storage_policy.policy_sha256,
            },
            "resource_enforcement": {
                "calibration_mode": True,
                "enforced": bool(worker_health["resource"]["enforced"]),
                "resource_hashes": worker_health["resource"]["resource_hashes"],
                "fail_closed": bool(worker_health["resource"]["fail_closed"]),
                "failure_reasons": worker_health["resource"][
                    "resource_failure_reasons"
                ],
            },
        },
        "writer": {
            "compressed_bytes_per_second": _rate(
                compression["compressed_bytes"], writer_seconds
            ),
            "drain_seconds": round(writer_finished - producer_finished, 9),
            "events_per_second": _rate(written, writer_seconds),
            "health": worker_health,
            "raw_bytes_per_second": _rate(compression["raw_bytes"], writer_seconds),
            "wall_seconds": round(writer_seconds, 9),
        },
        "workload_base_utc": workload_base.isoformat().replace("+00:00", "Z"),
    }
    return report


def main(argv: Iterator[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    output_root = args.output_root.expanduser().resolve()
    directory, owner_token = _create_owned_directory(output_root)
    report: dict[str, Any] | None = None
    try:
        report = _run_benchmark(args, directory)
        if args.keep:
            retained = True
        else:
            _delete_verified_owned_directory(
                directory,
                output_root=output_root,
                owner_token=owner_token,
            )
            retained = False
        report["output"] = {
            "directory": str(directory),
            "retained": retained,
            "report_artifact_layout": "reports/<canonical-sha256>.json_when_retained",
            "safe_cleanup_verified": not args.keep,
        }
        emitted_source_hashes = _verified_executed_source_hashes()
        if report.get("capture_runtime_source") != emitted_source_hashes:
            raise RuntimeError(
                "benchmark source provenance changed before report emission"
            )
        raw = canonical_json_bytes(report)
        if args.keep:
            _persist_content_addressed_report(directory, raw)
        sys.stdout.buffer.write(raw + b"\n")
        return 0 if report["acceptance"]["accepted"] is True else 2
    finally:
        # Failure/interrupt cleanup follows the same marker and containment
        # verification.  Never attempt broad or best-effort recursive deletion.
        if not args.keep and directory.exists():
            _delete_verified_owned_directory(
                directory,
                output_root=output_root,
                owner_token=owner_token,
            )


if __name__ == "__main__":
    raise SystemExit(main())
