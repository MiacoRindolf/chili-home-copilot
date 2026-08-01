"""In-process heap fingerprint logger (FIX 49 / FIX 50 lift).

The function ``run_memory_watcher_tick`` was previously inline in
``app/services/trading_scheduler.py``. f-leak-2 lifts it here so the
chili web container can call it from its lifespan startup path
without importing the whole scheduler module. trading_scheduler.py
now imports from here too.

Why an in-process tick (not a host-side probe): docker exec python
spawns a fresh child interpreter, so it can never see PID 1's heap.
The watcher must run inside the live process. APScheduler in
scheduler-worker calls this function every 5 min; chili's lifespan
calls it via a daemon thread every 60s.

Cheap (~50ms typical): one gc.collect() + one gc.get_objects() pass
+ a dict tally. Logs:
  - VmRSS / VmSize / Threads from /proc/self/status (Linux only)
  - py_objects total
  - top_abs: 6 most-numerous types
  - top_delta_since_last: 5 types whose count grew most since the
    previous tick on the SAME process (so we see the leak signature
    over time)
  - top_qualnames: 5 most-numerous functions by __qualname__ (FIX 50;
    a __qualname__ with 1000s of survivors is a closure being
    created in a hot loop and pinned somewhere)

Each caller passes its own ``prev_counts_ref`` (a single-element
list serving as a mutable reference) so the watcher's per-process
delta state is isolated; the same module imported into different
processes gets its own state per process by default.
"""
from __future__ import annotations

import gc as _gc
import logging
import os as _os
import sys as _sys
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# glibc-only: malloc_trim(0) walks ALL arenas (glibc >= 2.8) and returns
# free pages to the OS. Without it, per-thread arenas retain the high-water
# mark of every large transient pandas/numpy allocation forever — the
# 2026-07-31 scheduler-worker 9.9GB RSS incident was this retention, not a
# Python-object leak (object counts were flat while RSS ratcheted).
# Set CHILI_MEM_WATCHER_MALLOC_TRIM=0 to disable.
_libc = None
if _sys.platform.startswith("linux"):
    try:
        import ctypes as _ctypes

        _candidate = _ctypes.CDLL("libc.so.6")
        if hasattr(_candidate, "malloc_trim"):
            _libc = _candidate
    except Exception:
        _libc = None


def _malloc_trim_enabled() -> bool:
    raw = _os.environ.get("CHILI_MEM_WATCHER_MALLOC_TRIM", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _read_vm_rss_kb() -> int:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return 0


def _run_malloc_trim() -> str:
    """Trim all glibc arenas; return a log fragment (never raises)."""
    if _libc is None or not _malloc_trim_enabled():
        return ""
    try:
        pre_kb = _read_vm_rss_kb()
        _libc.malloc_trim(0)
        reclaimed_mb = max(0, pre_kb - _read_vm_rss_kb()) // 1024
        return f"malloc_trim_reclaimed={reclaimed_mb}MB "
    except Exception as e:
        return f"malloc_trim_failed={e.__class__.__name__} "


def run_memory_watcher_tick(
    prev_counts_ref: list[dict[str, int]],
    *,
    log_prefix: str = "[mem_watcher]",
) -> None:
    """Run one mem_watcher tick. Logs at INFO; never raises.

    ``prev_counts_ref`` is a single-element list used as a mutable
    container for the per-caller previous-snapshot dict. Callers
    initialize as ``[{}]`` and pass the same list across ticks.
    """
    try:
        _gc.collect()
        # Trim AFTER collect so freshly-freed chunks are returnable, BEFORE
        # the /proc status read so the logged vm_rss reflects post-trim truth.
        _trim_note = _run_malloc_trim()
        try:
            with open("/proc/self/status") as f:
                _status = f.read()
            _vm_rss_kb = 0
            _vm_size_kb = 0
            _threads = 0
            for line in _status.splitlines():
                if line.startswith("VmRSS:"):
                    _vm_rss_kb = int(line.split()[1])
                elif line.startswith("VmSize:"):
                    _vm_size_kb = int(line.split()[1])
                elif line.startswith("Threads:"):
                    _threads = int(line.split()[1])
        except Exception:
            _vm_rss_kb = _vm_size_kb = _threads = 0

        counts: dict[str, int] = {}
        qualname_counts: dict[str, int] = {}
        for obj in _gc.get_objects():
            t = type(obj).__name__
            counts[t] = counts.get(t, 0) + 1
            if t == "function":
                qn = getattr(obj, "__qualname__", None)
                if qn:
                    qualname_counts[qn] = qualname_counts.get(qn, 0) + 1
        total = sum(counts.values())

        top_abs = sorted(counts.items(), key=lambda x: -x[1])[:12]
        prev = prev_counts_ref[0] if prev_counts_ref else {}
        deltas: list[tuple[int, str, int]] = []
        for t, n in counts.items():
            d = n - prev.get(t, n)
            if d > 0:
                deltas.append((d, t, n))
        deltas.sort(reverse=True)
        top_delta = deltas[:5]
        top_qualnames = sorted(
            qualname_counts.items(), key=lambda x: -x[1]
        )[:5]

        logger.info(
            "%s vm_rss=%dMB vm_size=%dMB threads=%d %spy_objects=%d "
            "top_abs=%s top_delta_since_last=%s top_qualnames=%s",
            log_prefix,
            _vm_rss_kb // 1024, _vm_size_kb // 1024, _threads, _trim_note, total,
            [(t, n) for t, n in top_abs[:6]],
            [(t, f"+{d}", f"now={n}") for d, t, n in top_delta],
            top_qualnames,
        )

        if prev_counts_ref:
            prev_counts_ref[0] = counts
    except Exception as e:
        logger.warning("%s tick failed: %s", log_prefix, e)


def start_thread_watcher(
    *,
    interval_s: float = 60.0,
    log_prefix: str = "[mem_watcher]",
    name: str = "chili-mem-watcher",
) -> threading.Thread:
    """Spawn a daemon thread that calls run_memory_watcher_tick every
    ``interval_s`` seconds. Returns the started thread.

    Used by chili's lifespan (where APScheduler isn't running). The
    scheduler-worker continues to use APScheduler's cron registration
    for the same function -- both wire-paths share this implementation.
    """
    prev_counts_ref: list[dict[str, int]] = [{}]

    def _loop() -> None:
        # Slight initial delay so startup logs aren't drowned out.
        time.sleep(min(60.0, interval_s))
        while True:
            try:
                run_memory_watcher_tick(
                    prev_counts_ref, log_prefix=log_prefix,
                )
            except Exception:
                logger.exception("%s loop iter failed", log_prefix)
            time.sleep(interval_s)

    t = threading.Thread(target=_loop, daemon=True, name=name)
    t.start()
    return t


__all__ = ["run_memory_watcher_tick", "start_thread_watcher"]
