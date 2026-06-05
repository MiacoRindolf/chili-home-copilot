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

from .ops_log_prefixes import CHILI_BRAIN_IO

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
            f"{CHILI_BRAIN_IO} concurrency profile host_cpus=%.1f cgroup_cpus=%s effective=%.1f "
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
        _log.warning(f"{CHILI_BRAIN_IO} could not log concurrency profile: %s", e)


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


# ===========================================================================
# CPU-bound compute sizing
# ---------------------------------------------------------------------------
# Pools that crunch already-fetched data (indicator math, pattern detection,
# backtests) are bound by CPU, so size them to the cgroup-effective CPU budget.
# Prefer this over raw ``os.cpu_count() * k`` formulas: inside Docker, cpu_count
# reflects the host, so a host-based pool oversubscribes a CPU-limited container.
# ===========================================================================
def cpu_workers(
    settings: Any | None = None,
    *,
    multiplier: float = 1.0,
    floor: int = 1,
    ceiling: int | None = None,
) -> int:
    """Worker count for CPU-bound compute, sized to the cgroup-effective CPUs.

    ``multiplier`` allows mild oversubscription for work that still blocks on
    occasional I/O (e.g. scanner scoring that also fetches a quote); keep it ~1
    for pure compute. ``ceiling`` caps the result; ``floor`` is the minimum.
    """
    eff = effective_cpu_budget(settings)
    n = max(int(floor), int(round(eff * float(multiplier))))
    if ceiling is not None:
        n = min(int(ceiling), n)
    return max(1, n)


# ===========================================================================
# Provider-aware I/O concurrency
# ---------------------------------------------------------------------------
# Network fetches are bound by the *provider's* rate budget + connection pool,
# NOT by CPU. Sizing provider fetches off the CPU budget (the legacy
# ``io_workers_*`` pattern) is the root cause of two failures:
#   1. A mixed-provider batch sharing one CPU-sized pool throttles a FAST
#      provider (Massive: 100 rps, 512-conn pool) down to the pace dictated by
#      a SLOW one — equity mining strangled by crypto's Coinbase limit.
#   2. The same pool simultaneously *hammers* the slow provider (Coinbase),
#      tripping its 429 rate-limit + 60s backoff (which stalls the whole fetch).
# Fix: size each provider to ITSELF, and split mixed universes by provider so
# each group runs at its own safe concurrency.
# ===========================================================================
def massive_fetch_concurrency(settings: Any) -> int:
    """Concurrent Massive OHLCV fetches. Mirrors the proven massive_client batch
    sizer: rate-governed (massive_max_rps, which the client paces internally) and
    bounded by half the urllib3 pool so overlapping scans don't exhaust it."""
    rps = max(1, int(getattr(settings, "massive_max_rps", 100) or 100))
    pool_cap = max(16, int(getattr(settings, "massive_http_pool_maxsize", 512) or 512) // 2)
    return max(1, min(80, max(30, rps), pool_cap))


def polygon_fetch_concurrency(settings: Any) -> int:
    """Concurrent Polygon OHLCV fetches (dedicated batch worker setting)."""
    return max(1, int(getattr(settings, "market_data_polygon_batch_workers", 48) or 48))


def coinbase_fetch_concurrency(settings: Any) -> int:
    """Concurrent Coinbase REST fetches — GENTLE. Coinbase public OHLCV is
    429-prone (opens a 60s backoff on rate-limit), so match the fast-path's
    proven snapshot concurrency rather than a CPU-derived number."""
    o = getattr(settings, "coinbase_fetch_concurrency", None)
    if o is not None:
        return max(1, int(o))
    return max(1, int(getattr(settings, "universe_snapshot_fetch_concurrency", 4) or 4))


def yfinance_fetch_concurrency(settings: Any) -> int:
    """Concurrent yfinance fetches — GENTLE. yfinance is fragile and already
    globally paced (yf_session.acquire); a small pool avoids tripping it."""
    o = getattr(settings, "yfinance_fetch_concurrency", None)
    if o is not None:
        return max(1, int(o))
    return 4


_PROVIDER_FETCH_SIZERS = {
    "massive": massive_fetch_concurrency,
    "polygon": polygon_fetch_concurrency,
    "coinbase": coinbase_fetch_concurrency,
    "yfinance": yfinance_fetch_concurrency,
    "yf": yfinance_fetch_concurrency,
}


def io_fanout_workers(n_tasks: int, settings: Any | None = None, *, ceiling: int = 32) -> int:
    """Workers for a HETEROGENEOUS I/O fan-out — a fixed handful of *independent*
    network/DB calls run together (e.g. assembling AI context from several
    sources, or a prescreener pulling candidates from multiple feeds).

    These are bound by the NUMBER OF CONCURRENT TASKS, not by CPU and not by any
    single provider's rate, so sizing them off the CPU budget would wrongly
    serialize independent I/O. Size to the task count up to a safety ceiling
    (tunable via ``brain_io_fanout_ceiling``)."""
    o = getattr(settings, "brain_io_fanout_ceiling", None) if settings is not None else None
    cap = max(1, int(o)) if o is not None else int(ceiling)
    return max(1, min(cap, int(n_tasks)))


def io_workers_for_provider(provider: str, n_items: int, settings: Any) -> int:
    """Worker count for fetching ``n_items`` from a single named provider.

    Provider one of: massive | polygon | coinbase | yfinance. Unknown/mixed
    falls back to the gentlest (Coinbase) so we never accidentally hammer a
    rate-limited API. Always clamped to ``n_items`` (no idle threads)."""
    n = max(1, int(n_items))
    sizer = _PROVIDER_FETCH_SIZERS.get((provider or "").strip().lower())
    cap = sizer(settings) if sizer is not None else coinbase_fetch_concurrency(settings)
    return max(1, min(cap, n))


def ohlcv_provider_for_ticker(ticker: str) -> str:
    """Route a ticker to the provider that actually serves its OHLCV history.

    Crypto (``-USD``) is served by Coinbase (Massive is dead for crypto, so the
    real rate pressure is Coinbase); everything else is equity → Massive primary.
    This determines which provider's rate budget bounds the fetch."""
    return "coinbase" if str(ticker or "").upper().endswith("-USD") else "massive"


def split_tickers_by_provider(tickers: "list[str]") -> "dict[str, list[str]]":
    """Partition a mixed universe into ``{provider: [tickers]}`` groups so each
    group can be fetched at its own provider-safe concurrency."""
    groups: "dict[str, list[str]]" = {}
    for t in tickers:
        groups.setdefault(ohlcv_provider_for_ticker(t), []).append(t)
    return groups


def parallel_fetch_by_provider(
    items: "list",
    worker_fn: "Any",
    settings: Any,
    *,
    ticker_of: "Any" = None,
    shutdown_event: "Any" = None,
    cap: "int | None" = None,
) -> "list":
    """Provider-split parallel map: run ``worker_fn(item)`` over ``items``,
    partitioned by each item's OHLCV provider so a FAST provider (Massive) is
    never throttled to a SLOW one's pace (Coinbase) and the slow one is never
    hammered into 429s. Provider groups run concurrently; within a group the
    pool is sized to that provider's rate budget.

    ``ticker_of(item) -> ticker`` routes each item (default: the item itself, for
    plain ticker strings). ``cap`` optionally bounds each provider group's worker
    count (e.g. honor a caller-tuned ``max_workers`` while still splitting).
    Returns the list of ``worker_fn`` results in completion order (exceptions
    dropped). Callers decide whether to ``extend`` (worker returns a list) or
    filter-truthy (worker returns a scalar/None).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _key = ticker_of if ticker_of is not None else (lambda x: x)
    groups: "dict[str, list]" = {}
    for it in items:
        groups.setdefault(ohlcv_provider_for_ticker(_key(it)), []).append(it)

    def _run_group(provider: str, group: "list") -> "list":
        if not group:
            return []
        workers = io_workers_for_provider(provider, len(group), settings)
        if cap is not None:
            workers = max(1, min(int(cap), workers))
        out: "list" = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(worker_fn, it): it for it in group}
            for f in as_completed(futs):
                if shutdown_event is not None and shutdown_event.is_set():
                    break
                try:
                    out.append(f.result())
                except Exception:
                    continue
        return out

    results: "list" = []
    if len(groups) <= 1:
        for provider, group in groups.items():
            results.extend(_run_group(provider, group))
    else:
        with ThreadPoolExecutor(max_workers=len(groups)) as outer:
            gfuts = [
                outer.submit(_run_group, provider, group)
                for provider, group in groups.items()
            ]
            for gf in as_completed(gfuts):
                if shutdown_event is not None and shutdown_event.is_set():
                    break
                try:
                    results.extend(gf.result())
                except Exception:
                    continue
    return results