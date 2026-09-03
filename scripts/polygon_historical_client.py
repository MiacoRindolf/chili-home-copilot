"""Polygon/Massive v3 historical TICK client — trades and NBBO quotes.

WHY THIS EXISTS
---------------
``app/services/massive_client.py`` speaks the aggregates/snapshot/reference
surface the live lane needs.  It does not speak ``/v3/trades`` or ``/v3/quotes``,
and it inherits ``settings.polygon_max_rps = 5`` (app/config.py) — a self-imposed
cap roughly 45x below what the key actually sustains, tuned for a live loop that
must never starve the lane, not for a bulk backfill.  Hydrating history is a
different workload with different constraints, so it gets its own client rather
than a new mode bolted onto the live one.

WHAT PHASE 1b ESTABLISHED (empirically, not from docs)
------------------------------------------------------
  * ``GET {base}/v3/trades/{symbol}`` and ``{base}/v3/quotes/{symbol}`` both
    return HTTP 200 on historical dates for both keys.
  * Massive is a Polygon front door: identical request -> byte-identical
    ``results`` payload (sha256 match on n=2000 trades and n=1180 quotes).
    They are SEPARATE rate-limit buckets, so requests may be sharded across
    both front doors.
  * Max page size is 50,000 (``limit=100000`` -> HTTP 400 on the ``max`` tag).
  * Pagination is by ``next_url`` cursor, and the cursor URL does NOT carry the
    key — it must be re-attached per page or the page 401s.
  * Trade rows carry NO bid/ask.  NBBO lives only on ``/v3/quotes``.
  * Extended hours are fully covered (04:00 ET first premarket print present).

RATE LIMITING
-------------
Phase 1b measured ~200-227 rps per front door with zero 429s and deliberately
stopped short of hunting the actual ceiling, because the codebase already
carries a circuit breaker written after a 2026-04-19 abuse-denylist incident.
This client therefore defaults to a *conservative* token bucket well under the
measured comfort level, and treats 429 as authoritative regardless: it honours
``Retry-After`` and backs off exponentially with jitter.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import requests

MASSIVE_DEFAULT_BASE = "https://api.massive.com"
POLYGON_DEFAULT_BASE = "https://api.polygon.io"

# Provider-enforced page ceiling (measured: 50001+ -> HTTP 400).
MAX_PAGE_LIMIT = 50_000

# Deliberately far below the ~200 rps Phase 1b saw answer cleanly. See module
# docstring: this is a bulk backfill running alongside a LIVE trading lane that
# shares these credentials, so headroom for the lane matters more than speed.
DEFAULT_RPS = 12.0
DEFAULT_MAX_RETRIES = 6
DEFAULT_TIMEOUT_S = 60.0

NS_PER_S = 1_000_000_000


class PolygonHTTPError(RuntimeError):
    """A non-retryable HTTP failure from a Polygon-family front door."""

    def __init__(self, status: int, url: str, body: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:400]}")
        self.status = status
        self.url = url
        self.body = body


def env_file_candidates(env_path: str | os.PathLike[str] | None = None) -> list[Path]:
    """Where to look for ``.env``, in priority order.

    A git WORKTREE does not contain the gitignored ``.env`` from the main
    checkout, so "next to this script" is not sufficient when these scripts run
    from a worktree.  ``CHILI_ENV_FILE`` is the explicit escape hatch; otherwise
    the worktree's ``.git`` pointer file is followed back to the main checkout.
    """
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    if os.environ.get("CHILI_ENV_FILE"):
        candidates.append(Path(os.environ["CHILI_ENV_FILE"]))
    root = Path(__file__).resolve().parents[1]
    candidates.append(root / ".env")
    dot_git = root / ".git"
    try:
        if dot_git.is_file():
            marker = dot_git.read_text(encoding="utf-8", errors="replace").strip()
            if marker.startswith("gitdir:"):
                gitdir = Path(marker.split(":", 1)[1].strip())
                if gitdir.parent.name == "worktrees":
                    candidates.append(gitdir.parent.parent.parent / ".env")
    except OSError:
        pass
    return [p for p in candidates if p.exists()]


def load_provider_credentials(env_path: str | os.PathLike[str] | None = None) -> dict[str, dict[str, str]]:
    """Resolve keys/base URLs from the process env, falling back to the .env file.

    Values are never logged or returned in any diagnostic structure; callers
    refer to credentials by env-var NAME only.
    """
    values: dict[str, str] = {}
    for path in env_file_candidates(env_path):
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values.setdefault(k.strip(), v.strip())
        if values:
            break
    # Real process env wins over the file.
    for name in ("MASSIVE_API_KEY", "POLYGON_API_KEY", "MASSIVE_BASE_URL", "POLYGON_BASE_URL"):
        if os.environ.get(name):
            values[name] = os.environ[name]
    return {
        "massive": {
            "key": values.get("MASSIVE_API_KEY", ""),
            "base": (values.get("MASSIVE_BASE_URL") or MASSIVE_DEFAULT_BASE).rstrip("/"),
            "key_env": "MASSIVE_API_KEY",
        },
        "polygon": {
            "key": values.get("POLYGON_API_KEY", ""),
            "base": (values.get("POLYGON_BASE_URL") or POLYGON_DEFAULT_BASE).rstrip("/"),
            "key_env": "POLYGON_API_KEY",
        },
    }


class _TokenBucket:
    """Simple thread-safe token bucket; ``take()`` blocks until a slot frees."""

    def __init__(self, rps: float) -> None:
        self.interval = 1.0 / max(0.01, float(rps))
        self._lock = threading.Lock()
        self._next_at = 0.0

    def take(self) -> float:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_at)
            self._next_at = start + self.interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        return max(0.0, delay)


@dataclass
class FetchStats:
    requests: int = 0
    pages: int = 0
    rows: int = 0
    bytes_received: int = 0
    retries: int = 0
    rate_limited: int = 0
    throttle_sleep_s: float = 0.0
    wall_s: float = 0.0
    front_doors: dict[str, int] = field(default_factory=dict)

    def merge(self, other: "FetchStats") -> None:
        self.requests += other.requests
        self.pages += other.pages
        self.rows += other.rows
        self.bytes_received += other.bytes_received
        self.retries += other.retries
        self.rate_limited += other.rate_limited
        self.throttle_sleep_s += other.throttle_sleep_s
        self.wall_s += other.wall_s
        for k, v in other.front_doors.items():
            self.front_doors[k] = self.front_doors.get(k, 0) + v


class PolygonHistoricalClient:
    """Paged reader for ``/v3/trades`` and ``/v3/quotes``.

    ``front_doors`` names which credential sets to use.  With both present the
    client alternates between them, because Phase 1b proved they are separate
    rate-limit buckets serving byte-identical payloads.
    """

    def __init__(
        self,
        *,
        front_doors: tuple[str, ...] = ("massive", "polygon"),
        rps: float = DEFAULT_RPS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        credentials: dict[str, dict[str, str]] | None = None,
        session: Any | None = None,
    ) -> None:
        creds = credentials if credentials is not None else load_provider_credentials()
        usable = [d for d in front_doors if creds.get(d, {}).get("key")]
        if not usable:
            missing = ", ".join(creds.get(d, {}).get("key_env", d) for d in front_doors)
            raise RuntimeError(f"no usable provider credentials; set one of: {missing}")
        self.creds = creds
        self.front_doors = tuple(usable)
        self.max_retries = int(max_retries)
        self.timeout_s = float(timeout_s)
        self._bucket = _TokenBucket(rps)
        self._rr = 0
        self.session = session or requests.Session()
        if hasattr(self.session, "headers"):
            self.session.headers.update({"Accept": "application/json"})

    # -- transport ---------------------------------------------------------
    def _next_front_door(self) -> str:
        door = self.front_doors[self._rr % len(self.front_doors)]
        self._rr += 1
        return door

    def _get(self, url: str, params: dict[str, Any], door: str, stats: FetchStats) -> dict[str, Any]:
        """One GET with the door's key attached, retried on 429/5xx."""
        merged = dict(params)
        merged["apiKey"] = self.creds[door]["key"]
        attempt = 0
        while True:
            stats.throttle_sleep_s += self._bucket.take()
            stats.requests += 1
            stats.front_doors[door] = stats.front_doors.get(door, 0) + 1
            try:
                resp = self.session.get(url, params=merged, timeout=self.timeout_s)
            except Exception as exc:  # network-level failure is retryable
                if attempt >= self.max_retries:
                    raise
                attempt += 1
                stats.retries += 1
                self._sleep_backoff(attempt, None, exc)
                continue
            status = int(getattr(resp, "status_code", 0))
            body = resp.text or ""
            stats.bytes_received += len(body)
            if status == 200:
                return json.loads(body) if body else {}
            retryable = status == 429 or 500 <= status < 600
            if status == 429:
                stats.rate_limited += 1
            if not retryable or attempt >= self.max_retries:
                # Never let the key reach a log line: `url` here is the bare
                # endpoint, and `params` (which holds apiKey) is not included.
                raise PolygonHTTPError(status, url, body)
            attempt += 1
            stats.retries += 1
            retry_after = None
            headers = getattr(resp, "headers", None) or {}
            try:
                retry_after = float(headers.get("Retry-After")) if headers.get("Retry-After") else None
            except (TypeError, ValueError):
                retry_after = None
            self._sleep_backoff(attempt, retry_after, None)

    @staticmethod
    def _sleep_backoff(attempt: int, retry_after: float | None, _exc: Exception | None) -> None:
        if retry_after is not None and retry_after >= 0:
            time.sleep(min(60.0, retry_after))
            return
        # Exponential with full jitter, capped.
        time.sleep(min(30.0, random.uniform(0.0, 2.0 ** attempt * 0.25)))

    # -- paged reads -------------------------------------------------------
    def iter_records(
        self,
        dataset: str,
        symbol: str,
        start_ns: int,
        end_ns: int,
        *,
        limit: int = MAX_PAGE_LIMIT,
        stats: FetchStats | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every ``/v3/{dataset}`` record in ``[start_ns, end_ns)``, ascending.

        ``dataset`` is ``"trades"`` or ``"quotes"``.  Bounds are nanoseconds since
        the Unix epoch, half-open, matching the provider's own
        ``timestamp.gte``/``timestamp.lt`` semantics — so adjacent windows tile
        without overlap and without gaps.
        """
        if dataset not in ("trades", "quotes"):
            raise ValueError(f"dataset must be 'trades' or 'quotes', got {dataset!r}")
        st = stats if stats is not None else FetchStats()
        t0 = time.monotonic()
        door = self._next_front_door()
        url = f"{self.creds[door]['base']}/v3/{dataset}/{symbol.upper()}"
        params: dict[str, Any] = {
            "timestamp.gte": int(start_ns),
            "timestamp.lt": int(end_ns),
            "limit": int(min(limit, MAX_PAGE_LIMIT)),
            "order": "asc",
            "sort": "timestamp",
        }
        while True:
            payload = self._get(url, params, door, st)
            st.pages += 1
            results = payload.get("results") or []
            for rec in results:
                st.rows += 1
                yield rec
            nxt = payload.get("next_url")
            if not nxt:
                break
            # The cursor URL carries its own query string but NOT the key; a bare
            # next_url 401s.  Re-attach via params on a fresh front door so the
            # paging cost is spread across both rate-limit buckets.
            door = self._next_front_door()
            url = nxt
            params = {}
        st.wall_s += time.monotonic() - t0
        if stats is None:
            self.last_stats = st


def day_bounds_ns(day_start_utc, day_end_utc) -> tuple[int, int]:
    """Convert two aware UTC datetimes to the provider's nanosecond bounds."""
    return (
        int(day_start_utc.timestamp() * NS_PER_S),
        int(day_end_utc.timestamp() * NS_PER_S),
    )
