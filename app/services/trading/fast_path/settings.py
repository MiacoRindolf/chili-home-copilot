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
    # Be tolerant of accidentally-inline notes in .env files, e.g.
    # "1 until soak completes". Compose/dotenv can pass that whole
    # string through, and treating it as False silently disables gates.
    raw = raw.split()[0] if raw else raw
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

    # ── Universe rotation (f-fastpath-universe-rotation, 2026-05-07) ─
    universe_rotation_enabled: bool = False
    """Master flag for the data-driven universe rotation. When False
    (default), the executor + ws_client read from ``pairs`` (the
    hardcoded 5-pair list). When True, ws_client reads
    ``fast_path_universe WHERE status='active'`` and the rotator runs
    hourly. Rollback path: flip this False and the system reverts to
    the 5-pair fallback bit-identically."""

    universe_top_n: int = 25
    """Top-N pairs by composite_score that the rotator promotes per
    pass. Mid-tier sweet spot per the 2026-05-07 alpha replay; tighten
    for volatility, loosen for coverage."""

    universe_hysteresis_ranks: int = 3
    """A pair must drop ≥ this many ranks below the top-N cut to be
    demoted. Avoids subscription churn on rank-edge oscillation."""

    universe_shadow_window_h: int = 24
    """Cold-start window length (hours) before a newly-promoted pair
    transitions from ``status='shadow'`` to ``status='active'`` and
    becomes admission-eligible. ``decay_miner`` accumulates
    ``fast_signal_decay`` rows during this window."""

    # Admission gate thresholds (settings-tunable per the brief's
    # no-magic-numbers rule). Cited from
    # docs/STRATEGY/RESEARCH/2026-05-07_fastpath-universe-alpha-replay.md.
    universe_min_volume_24h_usd: float = 10_000_000.0
    """Lower bound for 24h-volume filter (USD). $10M filters out
    illiquid pairs whose round-trip cost exceeds reasonable alpha."""

    universe_max_spread_bps: float = 10.0
    """Upper bound for top-of-book spread (bps). 10 bps is the
    economic-line consensus from the alpha replay; pairs above are
    cost-prohibitive for alpha extraction at fast horizons."""

    universe_min_top_of_book_usd: float = 5_000.0
    """Minimum top-of-book size (USD) on each side. Below this, market
    impact dominates the predicted alpha at typical fast-path order
    sizes."""

    universe_min_trades_24h: int = 1_000
    """Minimum 24h trade count. Below this, the order book is too
    thin / discontinuous for the rotator's price snapshots to be
    reliable."""

    # Cost-aware admission gate (Step 5 of the brief). Off-by-default
    # so behavior at switchover is bit-identical to current.
    cost_aware_admission_enabled: bool = False
    """When True, the executor applies ``gate_cost_aware_admission``:
    rejects any signal whose ``mean_return < 2 × (taker_fee_bps +
    median_spread_bps_for_ticker)`` at the best-Sharpe horizon. When
    False (default), the gate is a no-op."""

    cost_aware_taker_fee_bps: float = 60.0
    """Coinbase Advanced Trade taker fee, **per-side, in bps**. Default
    is **60 bps** = retail volume tier 1 (>=$10k 30d volume), per
    https://docs.cdp.coinbase.com/exchange/docs/fees. The cost-aware
    gate's formula ``2 * (taker_fee_bps + spread_bps)`` multiplies by 2
    for the round-trip, so this value MUST be per-side, not round-trip.

    Operators on a higher volume tier should override via
    ``CHILI_FAST_PATH_COST_AWARE_TAKER_FEE_BPS``. Reference values:
    tier 1 = 60, tier 2 = 40, tier 3 = 25, tier 4 = 15, tier 5 = 10,
    tier 6 = 8, tier 7 = 5, tier 8 = 4, tier 9 = 4. Coinbase One
    subscribers may have different rates -- check the live fee schedule
    on the operator's account."""

    # ── Maker-only execution mode (f-fastpath-maker-only, 2026-05-08) ─
    execution_mode: str = "taker"
    """Fast-path execution mode. One of:
      * ``taker`` (default) -- crosses the spread with market orders.
        Existing behaviour; retained as benchmark.
      * ``maker_only`` -- places ``post_only=true`` limit orders inside
        the spread. Per the 2026-05-07 alpha replay, the only economic
        path on Coinbase at retail tier (taker round-trip cost
        dominates the realized edge).
      * ``maker_first_then_taker`` -- tries maker for
        ``maker_first_taker_fallback_s`` seconds, then crosses to taker
        if unfilled. Operator-controlled compromise.

    Default is ``taker`` so behaviour at switchover is bit-identical
    to today. Override via ``CHILI_FAST_PATH_EXECUTION_MODE``."""

    cost_aware_maker_fee_bps: float = 40.0
    """Coinbase Advanced Trade **maker** fee, per-side, in bps. Default
    is **40 bps** = retail volume tier 1 maker, per the same fee schedule
    as the taker fee above. Maker rebate eligibility (POST_ONLY orders
    that don't cross) can effectively bring this to 0-10 bps in
    practice, but the worst-case retail tier 1 maker is the safe
    default for the cost-aware gate.

    The cost-aware gate uses this when ``execution_mode == 'maker_only'``
    or ``'maker_first_then_taker'``; uses ``cost_aware_taker_fee_bps``
    otherwise. Override via ``CHILI_FAST_PATH_COST_AWARE_MAKER_FEE_BPS``.
    Reference values: tier 1 = 40, tier 2 = 25, tier 3 = 15, tier 4 = 8,
    tier 5 = 6, tier 6 = 4, tier 7 = 0, tier 8 = 0, tier 9 = 0
    (rebate-eligible at higher tiers; check the live schedule for the
    operator's account)."""

    live_alpha_evidence_gate_enabled: bool = True
    """When True, live fast-path execution requires calibrated decay evidence
    for the exact ticker / alert_type / score bucket. Paper can continue to
    explore no-data buckets; live cannot. This prevents a mode flip from
    turning cold-start or decayed signals into real orders."""

    live_alpha_min_samples: int = 50
    """Minimum decay samples required before a live fast-path signal can pass
    ``gate_live_alpha_evidence``."""

    live_alpha_min_net_bps: float = 0.0
    """Minimum edge left after the estimated round-trip fee + spread cost.
    Keep at 0 by default because ``gate_cost_aware_admission`` already owns
    the economic line; raise this for a larger live safety margin."""

    maker_cancel_on_timeout_s: int = 10
    """Cancel a resting maker order after this many seconds if unfilled.
    The trade-off: longer = higher fill rate but more adverse-selection
    risk; shorter = lower fill rate but cleaner signal. 10s is a
    starting point per the alpha-replay's mean signal half-life;
    operators tune via ``CHILI_FAST_PATH_MAKER_CANCEL_ON_TIMEOUT_S``."""

    maker_first_taker_fallback_s: int = 5
    """Under ``execution_mode='maker_first_then_taker'``, after this
    many seconds with no fill, cancel the maker order and place a
    taker (market) order. Default 5s -- shorter than
    ``maker_cancel_on_timeout_s`` because the fallback path commits to
    paying the taker fee, so the operator wants the maker chance brief.
    Override via ``CHILI_FAST_PATH_MAKER_FIRST_TAKER_FALLBACK_S``."""

    # ── Short-alert gate (2026-05-17) ─────────────────────────────────
    emit_short_alerts: bool = False
    """When False (default), the scanner skips ``imbalance_short`` alert
    emission entirely. Coinbase spot — the only fast-path venue today —
    cannot short, so the executor rejects every short alert with
    ``short_unsupported_in_spot`` regardless of edge or sizing. Pre-gate
    diagnostic (2026-05-16): 2,546 of 6,595 alerts/24h (39%) were
    imbalance_short and 100% rejected, burning DB writes + executor
    compute for zero strategy gain.

    Operators on a perp venue (Hyperliquid / dYdX / Drift) that supports
    shorts should override via ``CHILI_FAST_PATH_EMIT_SHORT_ALERTS=true``.
    Tests can override via the ``emit_short_alerts`` constructor arg on
    ``MomentumScanner`` directly (default True for backwards-compat with
    pre-gate fixtures)."""


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


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
        # f-fastpath-universe-rotation (2026-05-07)
        universe_rotation_enabled=_env_bool(
            "CHILI_FAST_PATH_UNIVERSE_ROTATION_ENABLED", False),
        universe_top_n=_env_int("CHILI_FAST_PATH_UNIVERSE_TOP_N", 25),
        universe_hysteresis_ranks=_env_int(
            "CHILI_FAST_PATH_UNIVERSE_HYSTERESIS_RANKS", 3),
        universe_shadow_window_h=_env_int(
            "CHILI_FAST_PATH_UNIVERSE_SHADOW_WINDOW_H", 24),
        universe_min_volume_24h_usd=_env_float(
            "CHILI_FAST_PATH_UNIVERSE_MIN_VOLUME_24H_USD", 10_000_000.0),
        universe_max_spread_bps=_env_float(
            "CHILI_FAST_PATH_UNIVERSE_MAX_SPREAD_BPS", 10.0),
        universe_min_top_of_book_usd=_env_float(
            "CHILI_FAST_PATH_UNIVERSE_MIN_TOP_OF_BOOK_USD", 5_000.0),
        universe_min_trades_24h=_env_int(
            "CHILI_FAST_PATH_UNIVERSE_MIN_TRADES_24H", 1_000),
        cost_aware_admission_enabled=_env_bool(
            "CHILI_FAST_PATH_COST_AWARE_ADMISSION_ENABLED", False),
        cost_aware_taker_fee_bps=_env_float(
            "CHILI_FAST_PATH_COST_AWARE_TAKER_FEE_BPS", 60.0),
        # f-fastpath-maker-only (2026-05-08)
        execution_mode=(
            os.environ.get("CHILI_FAST_PATH_EXECUTION_MODE") or "taker"
        ).strip().lower(),
        cost_aware_maker_fee_bps=_env_float(
            "CHILI_FAST_PATH_COST_AWARE_MAKER_FEE_BPS", 40.0),
        live_alpha_evidence_gate_enabled=_env_bool(
            "CHILI_FAST_PATH_LIVE_ALPHA_EVIDENCE_GATE_ENABLED", True),
        live_alpha_min_samples=_env_int(
            "CHILI_FAST_PATH_LIVE_ALPHA_MIN_SAMPLES", 50),
        live_alpha_min_net_bps=_env_float(
            "CHILI_FAST_PATH_LIVE_ALPHA_MIN_NET_BPS", 0.0),
        maker_cancel_on_timeout_s=_env_int(
            "CHILI_FAST_PATH_MAKER_CANCEL_ON_TIMEOUT_S", 10),
        maker_first_taker_fallback_s=_env_int(
            "CHILI_FAST_PATH_MAKER_FIRST_TAKER_FALLBACK_S", 5),
        # Short-alert gate (2026-05-17)
        emit_short_alerts=_env_bool(
            "CHILI_FAST_PATH_EMIT_SHORT_ALERTS", False),
    )


__all__ = ["FastPathSettings", "load"]
