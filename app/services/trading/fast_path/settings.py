"""Fast-path runtime settings — env-only, no DB, no broker.

Kept intentionally minimal: this module is imported by every fast-path
component, so it must not transitively import broker SDKs, the database,
or anything else heavy. If you need DB-backed config, add a separate
loader; settings here are pure env reads.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field


DEFAULT_UNIVERSE_HYSTERESIS_RANKS = 3
"""Default rank buffer used by universe rotation and learning floors."""

DEFAULT_UNIVERSE_LEARNING_RETENTION_HORIZON_S = 300
"""Default short-horizon learning retention window.

This is the largest scalp-decay bucket before the multi-hour observation
horizons. It lets the rotator collect 1s/5s/30s/60s/300s evidence without
pinning a symbol for the 30m/1h/4h research horizons.
"""

DEFAULT_MAKER_TICK_FRACTION_OF_MID = 1e-4
"""Default maker fallback tick offset as a fraction of mid-price."""

MAX_MAKER_TICK_FRACTION_OF_MID = 0.01
"""Largest accepted fallback maker tick offset fraction (1% of mid)."""

FAST_PATH_MODES = frozenset({"paper", "live"})
"""Supported fast-path operating modes."""

FAST_PATH_EXECUTION_MODES = frozenset({
    "taker",
    "maker_only",
    "maker_first_then_taker",
})
"""Supported fast-path execution modes."""


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


def _env_positive_int(
    name: str,
    default: int,
    *,
    max_value: int | None = None,
) -> int:
    value = _env_int(name, default)
    if isinstance(value, bool) or value <= 0:
        return default
    if max_value is not None and value > max_value:
        return default
    return value


def _env_nonnegative_int(name: str, default: int) -> int:
    value = _env_int(name, default)
    if isinstance(value, bool) or value < 0:
        return default
    return value


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


def _env_choice(name: str, default: str, choices: frozenset[str]) -> str:
    raw = (os.environ.get(name) or default).strip().lower()
    return raw if raw in choices else default


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

    pairs: list[str] = field(default_factory=list)
    """Explicit operator-configured fallback pairs.

    The fast-path scalp lane should normally use the data-driven universe
    rotator. Leaving this empty is intentional: an unset env var must not
    resurrect a stale baked-in coin list.
    """

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
    (default), the executor + ws_client read from ``pairs`` (the explicit
    configured pair list). When True, ws_client reads
    ``fast_path_universe WHERE status='active'`` and the rotator runs
    hourly. Rollback path: flip this False and provide
    ``CHILI_FAST_PATH_PAIRS``; with no configured pairs, ingestion stays
    paused instead of trading a baked-in symbol list."""

    universe_empty_fallback_enabled: bool = False
    """When universe rotation is enabled but ``fast_path_universe`` has
    no active/shadow rows, keep the feed paused instead of silently
    subscribing to ``pairs``. The explicit configured-pair fallback can
    be re-enabled for emergency continuity via
    ``CHILI_FAST_PATH_UNIVERSE_EMPTY_FALLBACK_ENABLED=true``."""

    universe_shadow_paper_fills_enabled: bool = False
    """When False, shadow pairs may still place simulated maker probes
    and feed maker-filled decay, but a filled shadow probe does not
    become an open dashboard paper position. Active pairs keep normal
    paper-fill behavior. Override with
    ``CHILI_FAST_PATH_UNIVERSE_SHADOW_PAPER_FILLS_ENABLED=true``."""

    universe_shadow_terminal_reprobe_enabled: bool = False
    """When True, paper-mode shadow pairs may place maker probes even
    when learned gates report terminal negative-edge, below-cost, or
    adverse-selection verdicts. Live trading remains active-only; this
    only refreshes paper evidence for the learner. Override with
    ``CHILI_FAST_PATH_UNIVERSE_SHADOW_TERMINAL_REPROBE_ENABLED=true``."""

    universe_shadow_capacity_probe_enabled: bool = False
    """When True, paper-mode shadow pairs may place observe-only maker
    probes when the only operational block is an existing paper
    position for that ticker. Fills update maker-attempt/decay evidence
    but do not create another dashboard paper position. Override with
    ``CHILI_FAST_PATH_UNIVERSE_SHADOW_CAPACITY_PROBE_ENABLED=true``."""

    universe_top_n: int = 25
    """Top-N pairs by composite_score that the rotator promotes per
    pass. Mid-tier sweet spot per the 2026-05-07 alpha replay; tighten
    for volatility, loosen for coverage."""

    universe_hysteresis_ranks: int = DEFAULT_UNIVERSE_HYSTERESIS_RANKS
    """A pair must drop ≥ this many ranks below the top-N cut to be
    demoted. Avoids subscription churn on rank-edge oscillation."""

    universe_shadow_window_h: int = 24
    """Cold-start window length (hours) before a newly-promoted pair
    becomes eligible for ``status='active'`` review. Promotion still
    requires learned decay evidence to clear execution costs; the
    window alone is not enough. ``decay_miner`` accumulates
    ``fast_signal_decay`` rows during this window."""

    # Admission gate thresholds (settings-tunable per the brief's
    # no-magic-numbers rule). Cited from
    # docs/STRATEGY/RESEARCH/2026-05-07_fastpath-universe-alpha-replay.md.
    universe_min_volume_24h_usd: float = 10_000_000.0
    """Lower bound for 24h-volume filter (USD). $10M filters out
    illiquid pairs whose round-trip cost exceeds reasonable alpha."""

    universe_max_spread_bps: float = 8.0
    """Upper bound for top-of-book spread (bps). Defaults to the same
    cap used by the executor's spread-sanity gate so the rotator does
    not subscribe symbols that execution will reject immediately."""

    universe_min_top_of_book_usd: float = 5_000.0
    """Minimum top-of-book size (USD) on each side. Below this, market
    impact dominates the predicted alpha at typical fast-path order
    sizes."""

    universe_shadow_min_top_of_book_usd: float = 25.0
    """Minimum top-of-book size (USD) for shadow-only exploration.
    Defaults to the configured fast-path execution notional at load
    time. Shadow candidates may be thinner than active candidates, but
    they still need enough visible touch liquidity to make a probe fill
    observation meaningful."""

    universe_min_range_24h_bps: float = 150.0
    """Minimum Coinbase 24h high-low range (bps). This keeps
    stable/near-stable quote products out of the scalp universe while
    leaving the threshold operator-configurable via
    ``CHILI_FAST_PATH_UNIVERSE_MIN_RANGE_24H_BPS``."""

    universe_adaptive_range_floor_enabled: bool = True
    """When True, raise the configured 24h-range floor to the weakest
    range in the current top-N plus hysteresis volatility cohort among
    otherwise liquid candidates. This keeps the scalp universe relative
    to today's market instead of relying only on a static floor."""

    universe_missing_grace_passes: int = 2
    """Number of consecutive rotator passes a previously subscribed
    symbol may miss due to transient data/depth/spread issues before it
    is demoted inactive. Hard volatility/volume failures still demote
    immediately."""

    universe_min_shadow_exploration_n: int = 3
    """Minimum number of probe-eligible shadow subscriptions to keep
    alive for learning when learned-edge or market-velocity filters
    would otherwise empty the universe. These rows are shadow-only and
    cannot promote to active without the normal edge evidence. Override
    with ``CHILI_FAST_PATH_UNIVERSE_MIN_SHADOW_EXPLORATION_N``; set to
    0 to disable the exploration floor."""

    universe_market_velocity_cost_parity_ratio: float = 1.0
    """Minimum recent realized-move / round-trip-cost ratio required before
    the rotator backfills shadow exploration slots that learned filters would
    otherwise skip. ``1.0`` means recent 1m movement must at least cover
    estimated fees + spread. Override via
    ``CHILI_FAST_PATH_UNIVERSE_MARKET_VELOCITY_COST_PARITY_RATIO``."""

    universe_market_velocity_deadlock_probe_enabled: bool = True
    """When True, a below-cost market-velocity regime may still keep the
    configured shadow exploration floor alive when the alternative is an
    empty websocket universe. The escape lane is shadow-only, uses
    ``universe_min_shadow_exploration_n`` for its bound, and can select
    only candidates blocked by missing fresh velocity evidence, not
    learned negative-edge candidates. Override via
    ``CHILI_FAST_PATH_UNIVERSE_MARKET_VELOCITY_DEADLOCK_PROBE_ENABLED``."""

    universe_learning_retention_horizon_s: int = (
        DEFAULT_UNIVERSE_LEARNING_RETENTION_HORIZON_S
    )
    """Seconds to keep a recently alerting/probed shadow ticker eligible
    for subscription so the decay miner can observe short-horizon outcomes.
    Set to 0 to disable. Override via
    ``CHILI_FAST_PATH_UNIVERSE_LEARNING_RETENTION_HORIZON_S``."""

    universe_learning_retention_max_n: int = DEFAULT_UNIVERSE_HYSTERESIS_RANKS
    """Maximum recently-learning shadow tickers to prioritize per rotation.
    The loader defaults this to ``universe_min_shadow_exploration_n`` so
    the retention lane reuses the configured learning budget instead of
    introducing an independent hard cap. Override via
    ``CHILI_FAST_PATH_UNIVERSE_LEARNING_RETENTION_MAX_N``."""

    universe_snapshot_fetch_concurrency: int = 4
    """Bounded worker count for Coinbase REST snapshot collection during
    universe rotation. Workers share a global request pacer, so increasing
    this hides network latency without raising aggregate request cadence.
    Override via
    ``CHILI_FAST_PATH_UNIVERSE_SNAPSHOT_FETCH_CONCURRENCY``."""

    universe_rest_request_pacing_s: float = 0.12
    """Minimum spacing between Coinbase REST requests across all rotator
    snapshot workers. The default is roughly 8 requests/sec, below the
    documented public limit. Override via
    ``CHILI_FAST_PATH_UNIVERSE_REST_REQUEST_PACING_S``."""

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

    cost_aware_live_fee_enabled: bool = False
    """When True, cost-aware fast-path components ask Coinbase for the
    account's current maker/taker fee tier and fall back to the static
    fee settings only if the broker lookup is unavailable. Override via
    ``CHILI_FAST_PATH_COST_AWARE_LIVE_FEE_ENABLED``."""

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

    negative_edge_filter_ttl_s: int = 30
    """Seconds to cache learned negative-edge suppression decisions in the
    websocket alert path. The executor remains the source of truth; this
    cache only avoids inserting alerts that the same learned gate would
    immediately reject. Override via
    ``CHILI_FAST_PATH_NEGATIVE_EDGE_FILTER_TTL_S``."""

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

    maker_tick_fraction_of_mid: float = DEFAULT_MAKER_TICK_FRACTION_OF_MID
    """Fallback maker tick offset as a fraction of mid-price when Coinbase
    ``quote_increment`` is unavailable. Bounded in ``load`` so operator
    typos cannot turn a passive probe into a coarse, always-join limit.
    Override via ``CHILI_FAST_PATH_MAKER_TICK_FRACTION_OF_MID``."""

    # ── Short-alert gate (2026-05-17) ─────────────────────────────────
    maker_attempt_adverse_filter_enabled: bool = True
    """When True, maker modes reject lanes whose recent maker-attempt
    lifecycle proves adverse selection: passive fills happen after
    adverse mid movement, or unfilled terminal attempts miss favorable
    movement. The gate uses confidence intervals instead of a fixed
    attempt-count quota."""

    maker_attempt_adverse_filter_window_h: int = 24
    """Lookback window for maker-attempt adverse-selection evidence."""

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

    emit_raw_imbalance_alerts: bool = False
    """When False (default), the scanner does not emit standalone
    ``imbalance_long`` / ``imbalance_short`` alerts. Raw top-book
    imbalance remains available as an input to stricter confirmed
    signals such as ``book_pressure_reclaim_long``. The maker-filled
    evidence observed on 2026-05-24 showed raw ``imbalance_long`` was
    negative at scalp horizons across score buckets, so production
    should not keep generating probes from that lane unless the
    operator explicitly re-enables it via
    ``CHILI_FAST_PATH_EMIT_RAW_IMBALANCE_ALERTS=true`` for a controlled
    experiment."""

    scanner_vol_breakout_lookback: int = 20
    """Number of closed 1m bars used for scanner volume baselines."""

    scanner_vol_breakout_mult: float = 2.0
    """Volume multiple over baseline required for ``volume_breakout_long``."""

    scanner_imbalance_long_threshold: float = 0.65
    """Order-book imbalance threshold for ``imbalance_long`` candidates."""

    scanner_imbalance_short_threshold: float = 0.35
    """Order-book imbalance threshold for optional ``imbalance_short`` candidates."""

    scanner_imbalance_cooldown_s: float = 30.0
    """Per-ticker cooldown for imbalance signals."""

    scanner_spread_squeeze_bps: float = 1.5
    """Maximum latest top-of-book spread for ``spread_squeeze`` candidates."""

    scanner_spread_squeeze_vol_mult: float = 1.2
    """Volume multiple over baseline required for ``spread_squeeze``."""

    scanner_spread_squeeze_cooldown_s: float = 60.0
    """Per-ticker cooldown for ``spread_squeeze`` candidates."""

    scanner_book_pressure_enabled: bool = True
    """When True, emit ``book_pressure_reclaim_long`` candidates from
    persistent book pressure that also has microprice + mid confirmation."""

    scanner_book_pressure_window: int = 5
    """Number of sampled book emits that must agree before
    ``book_pressure_reclaim_long`` can fire."""

    scanner_book_pressure_min_avg_imbalance: float = 0.65
    """Minimum average top-N book imbalance across the pressure window."""

    scanner_book_pressure_min_microprice_bps: float = 0.25
    """Minimum average microprice lead over mid, in bps."""

    scanner_book_pressure_max_spread_bps: float = 3.0
    """Maximum top-of-book spread allowed anywhere in the pressure window."""

    scanner_book_pressure_min_mid_move_bps: float = 0.25
    """Minimum mid and best-bid move from the start to end of the pressure window."""

    scanner_book_pressure_cooldown_s: float = 30.0
    """Per-ticker cooldown for ``book_pressure_reclaim_long`` candidates."""

    scanner_book_pressure_min_touch_notional_usd: float = 25.0
    """Minimum visible best bid and best ask notional for book-pressure
    confirmation. Defaults to the configured execution notional so a
    dust best level cannot manufacture fake microprice pressure."""

    scanner_max_pending_deferred: int = 1000
    """Global cap on pending pullback-deferred emits. Prevents scanner
    heap growth when book channels go quiet after volume-breakout spikes."""


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value):
        return default
    return value


def _env_positive_float(
    name: str,
    default: float,
    *,
    max_value: float | None = None,
) -> float:
    value = _env_float(name, default)
    if not math.isfinite(value) or value <= 0.0:
        return default
    if max_value is not None and value > max_value:
        return default
    return value


def _env_nonnegative_float(name: str, default: float) -> float:
    value = _env_float(name, default)
    if not math.isfinite(value) or value < 0.0:
        return default
    return value


def _env_unit_interval_float(name: str, default: float) -> float:
    value = _env_float(name, default)
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        return default
    return value


def load() -> FastPathSettings:
    """Read settings from the process environment. Called once at
    container boot by ``scripts/fast_data_worker.py``."""
    exec_notional_usd = _env_positive_float(
        "CHILI_FAST_PATH_EXEC_NOTIONAL_USD",
        25.0,
    )
    universe_hysteresis_ranks = _env_nonnegative_int(
        "CHILI_FAST_PATH_UNIVERSE_HYSTERESIS_RANKS",
        DEFAULT_UNIVERSE_HYSTERESIS_RANKS,
    )
    universe_min_shadow_exploration_n = _env_nonnegative_int(
        "CHILI_FAST_PATH_UNIVERSE_MIN_SHADOW_EXPLORATION_N",
        universe_hysteresis_ranks,
    )
    universe_learning_retention_max_n = _env_nonnegative_int(
        "CHILI_FAST_PATH_UNIVERSE_LEARNING_RETENTION_MAX_N",
        universe_min_shadow_exploration_n,
    )
    return FastPathSettings(
        enabled=_env_bool("CHILI_FAST_PATH_ENABLED", False),
        mode=_env_choice("CHILI_FAST_PATH_MODE", "paper", FAST_PATH_MODES),
        pairs=_env_pairs("CHILI_FAST_PATH_PAIRS", []),
        bar_window=_env_positive_int("CHILI_FAST_PATH_BAR_WINDOW", 500),
        book_depth=_env_positive_int("CHILI_FAST_PATH_BOOK_DEPTH", 25),
        queue_max=_env_positive_int("CHILI_FAST_PATH_QUEUE_MAX", 10_000),
        batch_size=_env_positive_int("CHILI_FAST_PATH_BATCH_SIZE", 50),
        batch_interval_ms=_env_positive_int(
            "CHILI_FAST_PATH_BATCH_INTERVAL_MS", 200),
        cb_threshold=_env_positive_int("CHILI_FAST_PATH_CB_THRESHOLD", 5),
        healthz_port=_env_positive_int(
            "CHILI_FAST_PATH_HEALTHZ_PORT",
            8090,
            max_value=65535,
        ),
        metrics_log_interval_s=_env_positive_int(
            "CHILI_FAST_PATH_METRICS_INTERVAL_S", 60),
        # f-fastpath-universe-rotation (2026-05-07)
        universe_rotation_enabled=_env_bool(
            "CHILI_FAST_PATH_UNIVERSE_ROTATION_ENABLED", False),
        universe_empty_fallback_enabled=_env_bool(
            "CHILI_FAST_PATH_UNIVERSE_EMPTY_FALLBACK_ENABLED", False),
        universe_shadow_paper_fills_enabled=_env_bool(
            "CHILI_FAST_PATH_UNIVERSE_SHADOW_PAPER_FILLS_ENABLED", False),
        universe_shadow_terminal_reprobe_enabled=_env_bool(
            "CHILI_FAST_PATH_UNIVERSE_SHADOW_TERMINAL_REPROBE_ENABLED", False),
        universe_shadow_capacity_probe_enabled=_env_bool(
            "CHILI_FAST_PATH_UNIVERSE_SHADOW_CAPACITY_PROBE_ENABLED", False),
        universe_top_n=_env_positive_int("CHILI_FAST_PATH_UNIVERSE_TOP_N", 25),
        universe_hysteresis_ranks=universe_hysteresis_ranks,
        universe_shadow_window_h=_env_positive_int(
            "CHILI_FAST_PATH_UNIVERSE_SHADOW_WINDOW_H", 24),
        universe_min_volume_24h_usd=_env_nonnegative_float(
            "CHILI_FAST_PATH_UNIVERSE_MIN_VOLUME_24H_USD", 10_000_000.0),
        universe_max_spread_bps=_env_nonnegative_float(
            "CHILI_FAST_PATH_UNIVERSE_MAX_SPREAD_BPS",
            _env_nonnegative_float("CHILI_FAST_PATH_EXEC_MAX_SPREAD_BPS", 8.0),
        ),
        universe_min_top_of_book_usd=_env_nonnegative_float(
            "CHILI_FAST_PATH_UNIVERSE_MIN_TOP_OF_BOOK_USD", 5_000.0),
        universe_shadow_min_top_of_book_usd=_env_nonnegative_float(
            "CHILI_FAST_PATH_UNIVERSE_SHADOW_MIN_TOP_OF_BOOK_USD",
            exec_notional_usd,
        ),
        universe_min_range_24h_bps=_env_nonnegative_float(
            "CHILI_FAST_PATH_UNIVERSE_MIN_RANGE_24H_BPS", 150.0),
        universe_adaptive_range_floor_enabled=_env_bool(
            "CHILI_FAST_PATH_UNIVERSE_ADAPTIVE_RANGE_FLOOR_ENABLED", True),
        universe_missing_grace_passes=_env_nonnegative_int(
            "CHILI_FAST_PATH_UNIVERSE_MISSING_GRACE_PASSES", 2),
        universe_min_shadow_exploration_n=universe_min_shadow_exploration_n,
        universe_market_velocity_cost_parity_ratio=_env_nonnegative_float(
            "CHILI_FAST_PATH_UNIVERSE_MARKET_VELOCITY_COST_PARITY_RATIO",
            1.0,
        ),
        universe_market_velocity_deadlock_probe_enabled=_env_bool(
            "CHILI_FAST_PATH_UNIVERSE_MARKET_VELOCITY_DEADLOCK_PROBE_ENABLED",
            True,
        ),
        universe_learning_retention_horizon_s=_env_nonnegative_int(
            "CHILI_FAST_PATH_UNIVERSE_LEARNING_RETENTION_HORIZON_S",
            DEFAULT_UNIVERSE_LEARNING_RETENTION_HORIZON_S,
        ),
        universe_learning_retention_max_n=universe_learning_retention_max_n,
        universe_snapshot_fetch_concurrency=_env_int(
            "CHILI_FAST_PATH_UNIVERSE_SNAPSHOT_FETCH_CONCURRENCY",
            4,
        ),
        universe_rest_request_pacing_s=_env_float(
            "CHILI_FAST_PATH_UNIVERSE_REST_REQUEST_PACING_S",
            0.12,
        ),
        universe_min_trades_24h=_env_nonnegative_int(
            "CHILI_FAST_PATH_UNIVERSE_MIN_TRADES_24H", 1_000),
        cost_aware_admission_enabled=_env_bool(
            "CHILI_FAST_PATH_COST_AWARE_ADMISSION_ENABLED", False),
        cost_aware_taker_fee_bps=_env_nonnegative_float(
            "CHILI_FAST_PATH_COST_AWARE_TAKER_FEE_BPS", 60.0),
        cost_aware_live_fee_enabled=_env_bool(
            "CHILI_FAST_PATH_COST_AWARE_LIVE_FEE_ENABLED", False),
        # f-fastpath-maker-only (2026-05-08)
        execution_mode=_env_choice(
            "CHILI_FAST_PATH_EXECUTION_MODE",
            "taker",
            FAST_PATH_EXECUTION_MODES,
        ),
        cost_aware_maker_fee_bps=_env_nonnegative_float(
            "CHILI_FAST_PATH_COST_AWARE_MAKER_FEE_BPS", 40.0),
        live_alpha_evidence_gate_enabled=_env_bool(
            "CHILI_FAST_PATH_LIVE_ALPHA_EVIDENCE_GATE_ENABLED", True),
        live_alpha_min_samples=_env_nonnegative_int(
            "CHILI_FAST_PATH_LIVE_ALPHA_MIN_SAMPLES", 50),
        live_alpha_min_net_bps=_env_nonnegative_float(
            "CHILI_FAST_PATH_LIVE_ALPHA_MIN_NET_BPS", 0.0),
        negative_edge_filter_ttl_s=_env_int(
            "CHILI_FAST_PATH_NEGATIVE_EDGE_FILTER_TTL_S", 30),
        maker_cancel_on_timeout_s=_env_int(
            "CHILI_FAST_PATH_MAKER_CANCEL_ON_TIMEOUT_S", 10),
        maker_first_taker_fallback_s=_env_int(
            "CHILI_FAST_PATH_MAKER_FIRST_TAKER_FALLBACK_S", 5),
        maker_tick_fraction_of_mid=_env_positive_float(
            "CHILI_FAST_PATH_MAKER_TICK_FRACTION_OF_MID",
            DEFAULT_MAKER_TICK_FRACTION_OF_MID,
            max_value=MAX_MAKER_TICK_FRACTION_OF_MID,
        ),
        maker_attempt_adverse_filter_enabled=_env_bool(
            "CHILI_FAST_PATH_MAKER_ATTEMPT_ADVERSE_FILTER_ENABLED", True),
        maker_attempt_adverse_filter_window_h=_env_int(
            "CHILI_FAST_PATH_MAKER_ATTEMPT_ADVERSE_FILTER_WINDOW_H", 24),
        # Short-alert gate (2026-05-17)
        emit_short_alerts=_env_bool(
            "CHILI_FAST_PATH_EMIT_SHORT_ALERTS", False),
        emit_raw_imbalance_alerts=_env_bool(
            "CHILI_FAST_PATH_EMIT_RAW_IMBALANCE_ALERTS", False),
        scanner_vol_breakout_lookback=_env_positive_int(
            "CHILI_FAST_PATH_SCANNER_VOL_BREAKOUT_LOOKBACK", 20),
        scanner_vol_breakout_mult=_env_positive_float(
            "CHILI_FAST_PATH_SCANNER_VOL_BREAKOUT_MULT", 2.0),
        scanner_imbalance_long_threshold=_env_unit_interval_float(
            "CHILI_FAST_PATH_SCANNER_IMBALANCE_LONG_THRESHOLD", 0.65),
        scanner_imbalance_short_threshold=_env_unit_interval_float(
            "CHILI_FAST_PATH_SCANNER_IMBALANCE_SHORT_THRESHOLD", 0.35),
        scanner_imbalance_cooldown_s=_env_nonnegative_float(
            "CHILI_FAST_PATH_SCANNER_IMBALANCE_COOLDOWN_S", 30.0),
        scanner_spread_squeeze_bps=_env_nonnegative_float(
            "CHILI_FAST_PATH_SCANNER_SPREAD_SQUEEZE_BPS", 1.5),
        scanner_spread_squeeze_vol_mult=_env_positive_float(
            "CHILI_FAST_PATH_SCANNER_SPREAD_SQUEEZE_VOL_MULT", 1.2),
        scanner_spread_squeeze_cooldown_s=_env_nonnegative_float(
            "CHILI_FAST_PATH_SCANNER_SPREAD_SQUEEZE_COOLDOWN_S", 60.0),
        scanner_book_pressure_enabled=_env_bool(
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_ENABLED", True),
        scanner_book_pressure_window=_env_positive_int(
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_WINDOW", 5),
        scanner_book_pressure_min_avg_imbalance=_env_unit_interval_float(
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_MIN_AVG_IMBALANCE", 0.65),
        scanner_book_pressure_min_microprice_bps=_env_nonnegative_float(
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_MIN_MICROPRICE_BPS", 0.25),
        scanner_book_pressure_max_spread_bps=_env_nonnegative_float(
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_MAX_SPREAD_BPS", 3.0),
        scanner_book_pressure_min_mid_move_bps=_env_nonnegative_float(
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_MIN_MID_MOVE_BPS", 0.25),
        scanner_book_pressure_cooldown_s=_env_nonnegative_float(
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_COOLDOWN_S", 30.0),
        scanner_book_pressure_min_touch_notional_usd=_env_nonnegative_float(
            "CHILI_FAST_PATH_SCANNER_BOOK_PRESSURE_MIN_TOUCH_NOTIONAL_USD",
            exec_notional_usd,
        ),
        scanner_max_pending_deferred=_env_positive_int(
            "CHILI_FAST_PATH_SCANNER_MAX_PENDING_DEFERRED", 1000,
        ),
    )


__all__ = [
    "DEFAULT_UNIVERSE_HYSTERESIS_RANKS",
    "DEFAULT_UNIVERSE_LEARNING_RETENTION_HORIZON_S",
    "FAST_PATH_EXECUTION_MODES",
    "FAST_PATH_MODES",
    "FastPathSettings",
    "load",
]
