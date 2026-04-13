"""Container-aware thread caps for brain I/O (market data, snapshots, predictions).

``os.cpu_count()`` inside Docker often reflects the host, not cgroup CPU limits.
We combine cgroup quota (when present) with optional settings overrides so
snapshot / mining / prediction pools do not open dozens of concurrent provider
connections on 2-CPU Compose services.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _read_cgroup_v2_cpus() -> float | None:
    try:
        p = Path("/sys/fs/cgroup/cpu.max")
        if not p.is_file():
            return None
        line = p.read_text(encoding="utf-8").strip()
        if line == "max" or not line:
            return None
        parts = line.split()
        if len(parts) != 2:
            return None
        quota, period = int(parts[0]), int(parts[1])
        if period <= 0 or quota < 0:
            return None
        return quota / period
    except Exception:
        return None


def _read_cgroup_v1_cpus() -> float | None:
    try:
        qf = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        pf = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if not qf.is_file() or not pf.is_file():
            return None
        quota = int(qf.read_text(encoding="utf-8").strip())
        period = int(pf.read_text(encoding="utf-8").strip())
        if period <= 0 or quota <= 0:
            return None
        return quota / period
    except Exception:
        return None


def cgroup_cpu_limit() -> float | None:
    """Return cgroup CPU limit if set, else None (unlimited / unknown)."""
    v2 = _read_cgroup_v2_cpus()
    if v2 is not None:
        return v2
    return _read_cgroup_v1_cpus()


def effective_cpu_budget(settings: Any | None = None) -> float:
    """Logical CPUs this process should size pools against."""
    if settings is not None:
        o = getattr(settings, "brain_io_effective_cpus_override", None)
        if o is not None:
            return max(1.0, float(o))
    host = float(os.cpu_count() or 4)
    cg = cgroup_cpu_limit()
    if cg is not None:
        return max(1.0, min(host, float(cg)))
    # Docker Desktop often exposes the host CPU count without a readable cgroup quota.
    _dockerish = Path("/.dockerenv").exists() or os.environ.get(
        "CHILI_RUNNING_IN_DOCKER", ""
    ).strip().lower() in ("1", "true", "yes")
    if _dockerish:
        raw = os.environ.get("CHILI_CONTAINER_CPU_LIMIT", "").strip()
        if raw:
            try:
                return max(1.0, min(host, float(raw)))
            except ValueError:
                pass
        return max(1.0, min(host, 4.0))
    return max(1.0, host)


def _capped_vs_host(eff: float, host: float) -> bool:
    """True when cgroup (or override) is materially below visible CPU count."""
    return eff + 0.49 < host


def io_workers_high(settings: Any) -> int:
    o = getattr(settings, "brain_io_workers_high", None)
    if o is not None:
        return max(1, int(o))
    host = float(os.cpu_count() or 4)
    eff = effective_cpu_budget(settings)
    capped = _capped_vs_host(eff, host)
    mult = 2.0 if capped else 3.0
    floor = 4 if capped else 24
    return min(80, max(floor, int(eff * mult)))


def io_workers_med(settings: Any) -> int:
    o = getattr(settings, "brain_io_workers_med", None)
    if o is not None:
        return max(1, int(o))
    host = float(os.cpu_count() or 4)
    eff = effective_cpu_budget(settings)
    capped = _capped_vs_host(eff, host)
    floor = 8 if capped else 16
    return min(48, max(floor, int(eff * 2)))


def io_workers_low(settings: Any) -> int:
    o = getattr(settings, "brain_io_workers_low", None)
    if o is not None:
        return max(1, int(o))
    host = float(os.cpu_count() or 4)
    eff = effective_cpu_budget(settings)
    capped = _capped_vs_host(eff, host)
    floor = 4 if capped else 10
    return min(32, max(floor, int(eff)))


def io_workers_for_snapshot_batch(settings: Any) -> int:
    o = getattr(settings, "brain_snapshot_io_workers", None)
    if o is not None:
        return max(1, int(o))
    return io_workers_high(settings)


def io_workers_for_predictions(settings: Any) -> int:
    o = getattr(settings, "brain_prediction_io_workers", None)
    if o is not None:
        return max(1, int(o))
    return io_workers_high(settings)


def log_brain_io_profile(log: logging.Logger | None = None) -> None:
    """One startup line: effective CPUs + default tier sizes (uses live settings)."""
    _log = log or logger
    try:
        from ...config import settings as s

        eff = effective_cpu_budget(s)
        host = float(os.cpu_count() or 4)
        cg = cgroup_cpu_limit()
        _log.info(
            "[chili_brain_io] concurrency profile host_cpus=%.1f cgroup_cpus=%s effective=%.1f "
            "snapshot_workers=%s prediction_workers=%s high/med/low=%s/%s/%s",
            host,
            f"{cg:.2f}" if cg is not None else "none",
            eff,
            io_workers_for_snapshot_batch(s),
            io_workers_for_predictions(s),
            io_workers_high(s),
            io_workers_med(s),
            io_workers_low(s),
        )
    except Exception as e:
        _log.warning("[chili_brain_io] could not log concurrency profile: %s", e)


class BrainIoCycleStats:
    """Lightweight per-batch counters (thread-safe)."""

    __slots__ = ("_lock", "ohlcv_fetches", "snapshot_threads")

    def __init__(self) -> None:
        self._lock = __import__("threading").Lock()
        self.ohlcv_fetches = 0
        self.snapshot_threads = 0

    def add_ohlcv(self, n: int = 1) -> None:
        with self._lock:
            self.ohlcv_fetches += n

    def set_snapshot_threads(self, n: int) -> None:
        with self._lock:
            self.snapshot_threads = n

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "ohlcv_fetches": self.ohlcv_fetches,
                "snapshot_threads": self.snapshot_threads,
            }

    def reset(self) -> None:
        with self._lock:
            self.ohlcv_fetches = 0
            self.snapshot_threads = 0


_SNAPSHOT_BATCH_STATS = BrainIoCycleStats()


def snapshot_batch_stats() -> BrainIoCycleStats:
    return _SNAPSHOT_BATCH_STATS