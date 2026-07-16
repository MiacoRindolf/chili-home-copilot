"""Standalone, no-DB benchmark for ReplayV3 capture runtime primitives.

The benchmark drives the real ``BoundedCaptureIngress``,
``CaptureWriterWorker``, and ``ContentAddressedCaptureStore`` with a
deterministic representative mix of NBBO, IQFeed prints, L2 updates, query
receipts, and change-log events.  It does not import the broad trading package,
read runtime configuration, connect to a database, or contact a provider.

Example::

    python scripts/benchmark_replay_capture_runtime.py \
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
import importlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import threading
import time
from types import ModuleType
from typing import Any, Callable, Iterator
import uuid

import psutil


UTC = timezone.utc
BENCHMARK_SCHEMA_VERSION = "chili.replay-capture-benchmark.v4"
OWNERSHIP_MARKER = ".chili-replay-capture-benchmark-owner.json"
OWNED_DIRECTORY_PREFIX = "chili-replay-capture-benchmark-"
MEBIBYTE = 1024**2
CAPACITY_AUTHORITY = "diagnostic_only"
CAPACITY_AUTHORITY_REASONS = (
    "empirical_hot_symbol_calibration_receipt_unavailable",
    "full_runner_watcher_resource_calibration_unavailable",
    "writer_scaling_calibration_unavailable",
)


def _load_capture_modules() -> tuple[ModuleType, ModuleType]:
    """Load only the two capture modules, bypassing trading package side effects."""

    module_dir = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "trading"
        / "momentum_neural"
    )
    package_name = "_chili_replay_capture_benchmark"
    package = ModuleType(package_name)
    package.__path__ = [str(module_dir)]  # type: ignore[attr-defined]
    package.__package__ = package_name
    sys.modules[package_name] = package
    contract = importlib.import_module(f"{package_name}.replay_capture_contract")
    runtime = importlib.import_module(f"{package_name}.replay_capture_runtime")
    return contract, runtime


CONTRACT, RUNTIME = _load_capture_modules()
CaptureClocks = CONTRACT.CaptureClocks
CaptureEvent = CONTRACT.CaptureEvent
CaptureRunIdentity = CONTRACT.CaptureRunIdentity
CaptureStream = CONTRACT.CaptureStream
canonical_json_bytes = CONTRACT.canonical_json_bytes
BoundedCaptureIngress = RUNTIME.BoundedCaptureIngress
CaptureResourceMeasurement = RUNTIME.CaptureResourceMeasurement
CaptureBudgetPolicy = RUNTIME.CaptureBudgetPolicy
CaptureResourceBinding = RUNTIME.CaptureResourceBinding
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


class PeakRssSampler:
    """Low-overhead process RSS sampler covering enqueue through writer drain."""

    def __init__(self, *, interval_seconds: float) -> None:
        self._process = psutil.Process()
        self._interval = float(interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.baseline_bytes = int(self._process.memory_info().rss)
        self.peak_bytes = self.baseline_bytes
        self.samples = 1

    def _sample(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                rss = int(self._process.memory_info().rss)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return
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
        try:
            self.peak_bytes = max(
                self.peak_bytes,
                int(self._process.memory_info().rss),
            )
            self.samples += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


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
    parent_values: list[int] = []
    methods: set[str] = set()
    verified = 0
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
        "logical_cpu_count": psutil.cpu_count(logical=True),
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


def _source_hash(module: ModuleType) -> str:
    source = Path(str(module.__file__)).read_bytes()
    return hashlib.sha256(source).hexdigest()


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
    memory_before = psutil.virtual_memory()
    psutil.cpu_percent(interval=None)
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
    process = psutil.Process()
    cpu_before = process.cpu_times()
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
    cpu_after = process.cpu_times()
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
    memory_after = psutil.virtual_memory()
    average_cpu_percent = float(psutil.cpu_percent(interval=None))
    disk_after = shutil.disk_usage(capture_root)
    host_sample_seconds = max(
        time.perf_counter() - host_sample_started, 1e-12
    )
    producer_seconds = producer_finished - producer_started
    writer_seconds = writer_finished - benchmark_started
    process_cpu_seconds = (
        (cpu_after.user + cpu_after.system) - (cpu_before.user + cpu_before.system)
    )
    compression = inventory["compression"]
    physical_capture_objects = int(
        inventory["files"]["physical_capture_objects"]
    )
    written = int(worker_health["events_written"])
    resource_measurement = CaptureResourceMeasurement(
        measured_at=datetime.now(UTC),
        sample_seconds=host_sample_seconds,
        total_memory_bytes=int(memory_after.total),
        available_memory_bytes=int(
            min(memory_before.available, memory_after.available)
        ),
        disk_free_bytes=int(disk_after.free),
        average_cpu_percent=average_cpu_percent,
        sustained_append_bytes_per_second=(
            float(accepted_canonical_bytes) / max(writer_seconds, 1e-12)
        ),
        fsync_p95_milliseconds=(
            float(durable_publication["file_fsync"]["p95_ns"])
            + float(durable_publication["parent_publication"]["p95_ns"])
        )
        / 1_000_000,
        logical_cpu_count=int(psutil.cpu_count(logical=True) or 1),
        host_fingerprint_sha256=_host_fingerprint(int(memory_after.total)),
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
    current_host_fingerprint = _host_fingerprint(int(psutil.virtual_memory().total))
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
            "contract_sha256": _source_hash(CONTRACT),
            "runtime_sha256": _source_hash(RUNTIME),
            "benchmark_script_sha256": hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest(),
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
            "logical_cpu_count": psutil.cpu_count(logical=True),
            "platform": platform.platform(),
            "psutil_version": psutil.__version__,
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
