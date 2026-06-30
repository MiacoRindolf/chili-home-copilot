"""CHUNK 3 — the adaptive RAIL RATE GOVERNOR (the single load-bearing new safety
component of the momentum ENGINE).

Why it exists
-------------
Chunk 2 deleted the slot COUNT as the primary admission governor (replaced by a
continuous dollars-at-risk budget). The count was *also*, accidentally, a free crude
RATE limiter: with at most N concurrent live sessions, at most ~N order *places* could
hit the broker rail per burst. Remove it and the genuinely-new risk is **execution
flooding** — M admitted names (plus S4 fast-poll ``get_order`` calls, which spend the
*same* per-account rail budget because ``get_order`` is a LIST endpoint,
``robinhood_mcp.py:382``) hammering the broker at once -> a 429 blow-out (the exact
failure ``project_crypto_live`` already hit). This governor bounds the rail call rate
so multi-admission can never flood / 429 the broker.

Design (no magic numbers — the rate SELF-DISCOVERS)
---------------------------------------------------
A process-local, thread-safe **token bucket** shared by EVERY rail call in the lane
(both order *places* and ``get_order`` *polls*):

- **One documented conservative STARTING bound** (``refill_rps`` seed). Everything
  else is adaptive: WIDEN the refill rate on a run of successes, HALVE it the instant
  the rail returns a 429 / rate-limit error. The steady-state rate is whatever the
  rail actually tolerates, discovered live — never a hardcoded RPS.
- **acquire()** takes one token. If the bucket is empty it WAITS up to a bounded
  ``max_wait_s`` for a refill; if still empty it returns ``acquired=False`` so the
  caller DEFERS to the next tick (a fill is NEVER dropped silently — the caller logs
  the deferral and retries; the resting order / pending poll persists).
- **note_429()** halves the refill rate (multiplicative decrease) and drains the
  bucket so the next call backs off immediately.
- **note_success()** counts toward a streak; after ``widen_after_successes`` clean
  calls the refill rate steps up (additive-ish increase toward ``refill_rps_max``).

Bounded + safe for the live-runner ThreadPool
---------------------------------------------
- **Process-local singleton per (lane_key)** in a module dict guarded by a lock, with
  a HARD CAP on the number of distinct buckets (``_MAX_BUCKETS``) and a TTL sweep
  (idle buckets older than ``_BUCKET_TTL_S`` are evicted) — caches must have a hard
  max size + TTL (CLAUDE.md concurrency rule).
- Each bucket has its own ``threading.Lock``; all token math is under it. The
  live-runner batch pool AND the WS event loop both land here, so it must be
  thread-safe; it holds no DB session and no broker handle, so it can never wedge a
  worker (the auto_trader orphan-lock lesson).

Kill-switch
-----------
``chili_momentum_entry_placement_governor_enabled`` (default True). When OFF,
``acquire()`` returns ``acquired=True`` immediately with zero wait and the bucket is
never touched — byte-identical to the deployed order path (no governor in the loop).

docs/DESIGN/MOMENTUM_ENGINE.md §2 (shared) / §3.C / Phase 5.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Bounds (caches MUST have a hard max size + TTL — CLAUDE.md) ───────────────
_MAX_BUCKETS = 64          # hard cap on distinct lane buckets held in-process
_BUCKET_TTL_S = 3600.0     # evict a bucket idle longer than this (sweep on access)

# ── The ONE documented conservative STARTING bound (everything else adaptive) ─
# A deliberately cautious seed so the FIRST burst on a cold process cannot flood the
# rail before the rate self-discovers. ~2 rail calls/sec is well under any plausible
# broker per-account budget; note_success() WIDENS from here, note_429() HALVES.
_DEFAULT_REFILL_RPS = 2.0
_DEFAULT_REFILL_RPS_MIN = 0.25   # floor: never throttle to a dead stop
_DEFAULT_REFILL_RPS_MAX = 20.0   # ceiling: never widen past a sane absolute
_DEFAULT_BURST = 4.0             # bucket capacity (max tokens) — small burst headroom
_DEFAULT_MAX_WAIT_S = 1.5        # how long acquire() blocks for a token before deferring
_DEFAULT_WIDEN_AFTER = 8         # clean-call streak before a widen step
_DEFAULT_WIDEN_FACTOR = 1.25     # multiplicative increase on widen
_DEFAULT_HALVE_FACTOR = 0.5      # multiplicative decrease on a 429


@dataclass
class GovernorConfig:
    """Resolved knobs for a bucket. Defaults are the conservative seed; the live
    runner passes the operator-tunable values from settings (one documented start)."""

    refill_rps: float = _DEFAULT_REFILL_RPS
    refill_rps_min: float = _DEFAULT_REFILL_RPS_MIN
    refill_rps_max: float = _DEFAULT_REFILL_RPS_MAX
    burst: float = _DEFAULT_BURST
    max_wait_s: float = _DEFAULT_MAX_WAIT_S
    widen_after_successes: int = _DEFAULT_WIDEN_AFTER
    widen_factor: float = _DEFAULT_WIDEN_FACTOR
    halve_factor: float = _DEFAULT_HALVE_FACTOR

    def sanitized(self) -> "GovernorConfig":
        """Clamp to sane, positive, internally-consistent values (fail-safe vs a
        misconfigured env). Never returns a config that could divide-by-zero or
        widen below its own floor."""
        rps_min = max(1e-3, float(self.refill_rps_min))
        rps_max = max(rps_min, float(self.refill_rps_max))
        rps = min(max(float(self.refill_rps), rps_min), rps_max)
        return GovernorConfig(
            refill_rps=rps,
            refill_rps_min=rps_min,
            refill_rps_max=rps_max,
            burst=max(1.0, float(self.burst)),
            max_wait_s=max(0.0, float(self.max_wait_s)),
            widen_after_successes=max(1, int(self.widen_after_successes)),
            widen_factor=max(1.0, float(self.widen_factor)),
            halve_factor=min(max(1e-3, float(self.halve_factor)), 1.0),
        )


@dataclass
class _Counters:
    """Lifetime observability for one bucket (emitted under [momentum_s4])."""

    waits: int = 0              # acquire() that had to block for a refill
    wait_ms_total: float = 0.0  # cumulative blocked time
    defers: int = 0             # acquire() that gave up -> caller deferred a call
    rate_limit_events: int = 0  # note_429() calls (rail pushed back)
    widens: int = 0             # adaptive rate increases
    grants: int = 0             # tokens granted (calls let through)


@dataclass
class AcquireResult:
    acquired: bool
    waited_s: float = 0.0
    deferred: bool = False
    refill_rps: float = 0.0


class _TokenBucket:
    """A single adaptive token bucket. Thread-safe; all state under ``_lock``."""

    def __init__(self, cfg: GovernorConfig) -> None:
        self._cfg = cfg.sanitized()
        self._rps = self._cfg.refill_rps
        self._tokens = self._cfg.burst  # start full so the first call is instant
        self._last_refill = time.monotonic()
        self._success_streak = 0
        self._lock = threading.Lock()
        self.counters = _Counters()
        self.last_access = time.monotonic()

    # ── internal: lazy refill (must hold the lock) ───────────────────────────
    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._cfg.burst, self._tokens + elapsed * self._rps)
            self._last_refill = now

    def acquire(self) -> AcquireResult:
        """Take one token. Block up to ``max_wait_s`` for a refill; on timeout return
        ``acquired=False`` (caller defers — never a silent drop)."""
        deadline = time.monotonic() + self._cfg.max_wait_s
        waited = 0.0
        with self._lock:
            self.last_access = time.monotonic()
            while True:
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self.counters.grants += 1
                    if waited > 0.0:
                        self.counters.waits += 1
                        self.counters.wait_ms_total += waited * 1000.0
                    return AcquireResult(
                        acquired=True, waited_s=waited, refill_rps=self._rps
                    )
                now = time.monotonic()
                if now >= deadline:
                    self.counters.defers += 1
                    if waited > 0.0:
                        self.counters.waits += 1
                        self.counters.wait_ms_total += waited * 1000.0
                    return AcquireResult(
                        acquired=False, waited_s=waited, deferred=True,
                        refill_rps=self._rps,
                    )
                # Sleep just long enough for the next token (or until the deadline),
                # releasing the lock so other callers/refills proceed.
                need = (1.0 - self._tokens) / self._rps if self._rps > 0 else 0.05
                sleep_s = max(0.005, min(need, deadline - now))
                self._lock.release()
                try:
                    time.sleep(sleep_s)
                    waited += sleep_s
                finally:
                    self._lock.acquire()

    def note_success(self) -> None:
        """A clean rail call. After a streak, WIDEN the refill rate toward the max."""
        with self._lock:
            self.last_access = time.monotonic()
            self._success_streak += 1
            if self._success_streak >= self._cfg.widen_after_successes:
                self._success_streak = 0
                new_rps = min(
                    self._cfg.refill_rps_max, self._rps * self._cfg.widen_factor
                )
                if new_rps > self._rps:
                    self._rps = new_rps
                    self.counters.widens += 1

    def note_429(self) -> None:
        """The rail pushed back (429 / rate-limit). HALVE the refill rate and drain
        the bucket so the next call immediately backs off."""
        with self._lock:
            self.last_access = time.monotonic()
            self._success_streak = 0
            self._rps = max(self._cfg.refill_rps_min, self._rps * self._cfg.halve_factor)
            self._tokens = 0.0
            self._last_refill = time.monotonic()
            self.counters.rate_limit_events += 1

    def snapshot(self) -> dict:
        with self._lock:
            self._refill_locked()
            c = self.counters
            return {
                "refill_rps": round(self._rps, 4),
                "tokens": round(self._tokens, 3),
                "grants": c.grants,
                "waits": c.waits,
                "wait_ms_total": round(c.wait_ms_total, 1),
                "defers": c.defers,
                "rate_limit_events": c.rate_limit_events,
                "widens": c.widens,
            }


# ── Process-local registry: one bucket per lane_key, bounded + TTL-swept ──────
_REGISTRY: dict[str, _TokenBucket] = {}
_REGISTRY_LOCK = threading.Lock()


def _sweep_locked(*, reserve: int = 0) -> None:
    """Evict idle buckets (TTL) and, if still at/over the hard cap, the least-recently
    used. ``reserve`` leaves room for that many about-to-be-inserted buckets so the
    registry NEVER exceeds ``_MAX_BUCKETS`` AFTER the caller's insert. Must hold
    ``_REGISTRY_LOCK``."""
    now = time.monotonic()
    stale = [k for k, b in _REGISTRY.items() if (now - b.last_access) > _BUCKET_TTL_S]
    for k in stale:
        _REGISTRY.pop(k, None)
    target = max(0, _MAX_BUCKETS - max(0, reserve))
    if len(_REGISTRY) > target:
        # Evict oldest-accessed down to the target (bounded memory).
        for k, _ in sorted(_REGISTRY.items(), key=lambda kv: kv[1].last_access)[
            : len(_REGISTRY) - target
        ]:
            _REGISTRY.pop(k, None)


def get_bucket(lane_key: str, cfg: Optional[GovernorConfig] = None) -> _TokenBucket:
    """Process-local singleton bucket for ``lane_key`` (created on first use). Bounded
    + TTL-swept. Thread-safe."""
    with _REGISTRY_LOCK:
        b = _REGISTRY.get(lane_key)
        if b is None:
            # Reserve one slot for the about-to-be-inserted bucket so the registry
            # never exceeds _MAX_BUCKETS after the insert.
            _sweep_locked(reserve=1)
            b = _TokenBucket(cfg or GovernorConfig())
            _REGISTRY[lane_key] = b
        else:
            _sweep_locked()
        return b


def reset_for_tests() -> None:
    """Clear the process-local registry (test isolation only)."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def _governor_enabled(settings) -> bool:
    return bool(
        getattr(settings, "chili_momentum_entry_placement_governor_enabled", True)
    )


def _config_from_settings(settings) -> GovernorConfig:
    """Build a GovernorConfig from the operator-tunable settings, falling back to the
    conservative seed for any missing knob (the ONE documented start)."""
    def _f(name: str, default: float) -> float:
        try:
            return float(getattr(settings, name, default))
        except (TypeError, ValueError):
            return default

    def _i(name: str, default: int) -> int:
        try:
            return int(getattr(settings, name, default))
        except (TypeError, ValueError):
            return default

    return GovernorConfig(
        refill_rps=_f("chili_momentum_rail_governor_start_rps", _DEFAULT_REFILL_RPS),
        refill_rps_min=_f("chili_momentum_rail_governor_min_rps", _DEFAULT_REFILL_RPS_MIN),
        refill_rps_max=_f("chili_momentum_rail_governor_max_rps", _DEFAULT_REFILL_RPS_MAX),
        burst=_f("chili_momentum_rail_governor_burst", _DEFAULT_BURST),
        max_wait_s=_f("chili_momentum_rail_governor_max_wait_s", _DEFAULT_MAX_WAIT_S),
        widen_after_successes=_i(
            "chili_momentum_rail_governor_widen_after_successes", _DEFAULT_WIDEN_AFTER
        ),
        widen_factor=_f("chili_momentum_rail_governor_widen_factor", _DEFAULT_WIDEN_FACTOR),
        halve_factor=_f("chili_momentum_rail_governor_halve_factor", _DEFAULT_HALVE_FACTOR),
    )


def acquire_rail(settings, *, lane_key: str = "momentum") -> AcquireResult:
    """Shared entry point for BOTH the place path and the poll path. Takes one rail
    token (bounded wait) so multi-admission cannot flood / 429 the broker.

    Flag OFF ⇒ returns ``acquired=True`` instantly without touching any bucket
    (byte-identical to the deployed order path). On flag ON, blocks up to ``max_wait_s``
    for a token; if none, returns ``acquired=False`` so the caller DEFERS (logs +
    retries next tick — never a silent drop)."""
    if not _governor_enabled(settings):
        return AcquireResult(acquired=True)
    bucket = get_bucket(lane_key, _config_from_settings(settings))
    return bucket.acquire()


def note_rail_outcome(settings, result, *, lane_key: str = "momentum") -> None:
    """Feed a rail call's outcome back to the adaptive rate. ``result`` is the dict the
    adapter returns from a place (``{"ok":..., "error":...}``) or any object with an
    ``error``/status the caller maps to a rate-limit. A 429 / rate-limit HALVES the
    rate; a clean call counts toward a WIDEN. Flag OFF ⇒ no-op."""
    if not _governor_enabled(settings):
        return
    bucket = get_bucket(lane_key, _config_from_settings(settings))
    if is_rate_limit_outcome(result):
        bucket.note_429()
    else:
        bucket.note_success()


def is_rate_limit_outcome(result) -> bool:
    """True iff a rail result represents a 429 / rate-limit push-back. Robust to the
    several shapes the lane sees: an order-result dict ({"ok": False, "error": "..."}),
    a raw status int/str, an exception, or a NormalizedOrder-ish object with a status.
    Conservative: only the explicit rate-limit signals count (never a generic error,
    which must NOT widen OR halve falsely)."""
    text = ""
    status_code = None
    code = ""
    try:
        if result is None:
            return False
        if isinstance(result, dict):
            err = result.get("error")
            text = str(err or "")
            status_code = result.get("status_code") or result.get("status")
            code = str(result.get("code") or "")
        elif isinstance(result, (int,)):
            status_code = result
        elif isinstance(result, BaseException):
            # An exception surfaced from the rail (e.g. RhMcpError "MCP HTTP 429 ..."
            # with code="http_429"). Its str() carries the rate-limit text and it may
            # carry a typed .code — match both. [poll-path 429 unmasking, 2026-06-27]
            text = str(result or "")
            code = str(getattr(result, "code", "") or "")
            status_code = getattr(result, "status_code", None)
        else:
            text = str(getattr(result, "error", "") or getattr(result, "status", "") or "")
            status_code = getattr(result, "status_code", None)
            code = str(getattr(result, "code", "") or "")
    except Exception:
        return False
    try:
        if status_code is not None and int(status_code) == 429:
            return True
    except (TypeError, ValueError):
        pass
    code_low = code.lower()
    if code_low in ("http_429",) or code_low.endswith("_429"):
        return True
    low = text.lower()
    return (
        "429" in low
        or "rate limit" in low
        or "rate-limit" in low
        or "ratelimit" in low
        or "too many requests" in low
    )
