"""Fast-path runtime settings — env-only, no DB, no broker.

Kept intentionally minimal: this module is imported by every fast-path
component, so it must not transitively import broker SDKs, the database,
or anything else heavy. If you need DB-backed config, add a separate
loader; settings here are pure env reads.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_pairs(name: str, default: list[str]) -> list[str]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return list(default)
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        p = part.strip().upper()
        if p:
            out.append(p)
    return out or list(default)


@dataclass(frozen=True)
class FastPathSettings:
    """Frozen at process startup — never mutate at runtime.

    All hot-path code reads these fields directly; the dataclass is
    safer than scattered ``os.environ`` reads because the bounds are
    enforced once at boot.
    """

    enabled: bool = False
    """Master kill switch. Default OFF. When False, the supervisor still
    starts the container but parks every pair in ``state='paused'`` and
    opens the healthz endpoint. Safe to deploy without consuming any
    Coinbase WS quota or persisting any rows."""

    mode: str = "paper"
    """``paper`` or ``live``. F1 ingestion is read-only by definition;
    this flag is read by F4+ execution code only. Surfaced here so the
    operator can flip the whole fast lane mode in one place."""

    pairs: list[str] = field(default_factory=lambda: [
        "BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "DOGE-USD",
    ])

    # ── Memory / queue bounds (see architecture doc) ─────────────────
    bar_window: int = 500
    """In-memory sliding window of recent bars per (ticker, interval).
    Older bars only live in Postgres."""

    book_depth: int = 25
    """Top-N L2 levels per side held in memory (F2)."""

    queue_max: int = 10_000
    """DB write queue capacity. Items beyond this are dropped per the
    backpressure rules — bar-close events are NEVER dropped, only
    sub-second tick-level updates."""

    batch_size: int = 50
    """Max rows per INSERT batch."""

    batch_interval_ms: int = 200
    """Max time a row waits in the queue before its batch is flushed."""

    # ── Resilience ────────────────────────────────────────────────────
    cb_threshold: int = 5
    """Per-pair circuit-breaker: errors per 60s before the pair is
    moved to ``state='paused'``."""

    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 30.0
    """Exponential backoff bounds for WS reconnect."""

    # ── Coinbase WS ───────────────────────────────────────────────────
    coinbase_ws_url: str = "wss://advanced-trade-ws.coinbase.com"

    # ── Observability ─────────────────────────────────────────────────
    healthz_port: int = 8090
    metrics_log_interval_s: int = 60


def load() -> FastPathSettings:
    """Read settings from the process environment. Called once at
    container boot by ``scripts/fast_data_worker.py``."""
    return FastPathSettings(
        enabled=_env_bool("CHILI_FAST_PATH_ENABLED", False),
        mode=(os.environ.get("CHILI_FAST_PATH_MODE") or "paper").strip().lower(),
        pairs=_env_pairs("CHILI_FAST_PATH_PAIRS", [
            "BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "DOGE-USD",
        ]),
        bar_window=_env_int("CHILI_FAST_PATH_BAR_WINDOW", 500),
        book_depth=_env_int("CHILI_FAST_PATH_BOOK_DEPTH", 25),
        queue_max=_env_int("CHILI_FAST_PATH_QUEUE_MAX", 10_000),
        batch_size=_env_int("CHILI_FAST_PATH_BATCH_SIZE", 50),
        batch_interval_ms=_env_int("CHILI_FAST_PATH_BATCH_INTERVAL_MS", 200),
        cb_threshold=_env_int("CHILI_FAST_PATH_CB_THRESHOLD", 5),
        healthz_port=_env_int("CHILI_FAST_PATH_HEALTHZ_PORT", 8090),
        metrics_log_interval_s=_env_int("CHILI_FAST_PATH_METRICS_INTERVAL_S", 60),
    )


__all__ = ["FastPathSettings", "load"]
