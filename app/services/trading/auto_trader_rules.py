"""Pure rule gates for AutoTrader v1 (testable without DB side effects)."""
from __future__ import annotations

import logging
import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Optional, Tuple

from sqlalchemy.orm import Session

from ...config import (
    AUTOTRADER_DIRECTIONAL_PROBABILITY_DEFAULT_MAX_ROWS,
    AUTOTRADER_DIRECTIONAL_PROBABILITY_DEFAULT_Z,
    AUTOTRADER_DIRECTIONAL_PROBABILITY_MAX_Z,
    AUTOTRADER_DIRECTIONAL_PROBABILITY_MIN_ROWS,
    AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_ASSET_TYPES,
    AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_ENABLED,
    AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_MAX_PCT,
    AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_SLIPPAGE_MULTIPLE,
    AUTOTRADER_FAVORABLE_ENTRY_DRIFT_MAX_SLIPPAGE_MULTIPLE,
    AUTOTRADER_FAVORABLE_ENTRY_DRIFT_MIN_SLIPPAGE_MULTIPLE,
    AUTOTRADER_FRACTIONAL_EQUITY_DEFAULT_ENABLED,
    AUTOTRADER_LEGACY_MAX_SYMBOL_PRICE_DEFAULT_USD,
    AUTOTRADER_MAX_ENTRY_SLIPPAGE_DEFAULT_PCT,
    AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_ASSET_TYPES,
    AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_ENABLED,
    AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_MINUTES,
    AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_THRESHOLD,
    AUTOTRADER_MANAGED_EDGE_DEFAULT_ADVERSE_BUFFER,
    AUTOTRADER_MANAGED_EDGE_DEFAULT_ASSET_TYPES,
    AUTOTRADER_MANAGED_EDGE_DEFAULT_CAPTURE_FRACTION,
    AUTOTRADER_MANAGED_EDGE_DEFAULT_MAX_REWARD_FRACTION,
    AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_DIRECTIONAL_SAMPLES,
    AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_EXPECTED_NET_PCT,
    AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_REWARD_FRACTION,
    AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_REWARD_RISK,
    AUTOTRADER_MANAGED_EDGE_DEFAULT_MODE,
    AUTOTRADER_MANAGED_EDGE_DEFAULT_STATIC_TO_MANAGED_REWARD_RATIO,
)
from ...models.trading import AutoTraderRun, BreakoutAlert, PaperTrade, ScanPattern, Trade
from .ops_log_prefixes import CHILI_RISK_CACHE
from .return_math import paper_trade_realized_pnl, trade_realized_pnl

logger = logging.getLogger(__name__)


# Learned confidence constants (Phase D).
#
# The rule gate still adapts the confidence floor from the M.1 pattern-regime
# ledger. Entry admission itself now uses expected net edge instead of a
# static projected-profit threshold.
#
# Confidence floor = env_floor, but replaceable by pattern hit rate times
# CONFIDENCE_LEARNING_FACTOR when a pattern has confident cells. 0.85 means
# "trust 85% of the observed win rate", leaving a safety margin vs a freshly
# promoted pattern over-claiming.
CONFIDENCE_LEARNING_FACTOR: float = 0.85

# Absolute confidence lower bound. Even a learned pattern with a great hit
# rate cannot drop the floor below this.
CONFIDENCE_ABSOLUTE_FLOOR: float = 0.55

MANAGED_EDGE_MODE_OFF = "off"
MANAGED_EDGE_MODE_SHADOW = "shadow"
MANAGED_EDGE_MODE_COMPARE = "compare"
MANAGED_EDGE_MODE_AUTHORITATIVE = "authoritative"
MANAGED_EDGE_ACTIVE_MODES = frozenset(
    {
        MANAGED_EDGE_MODE_SHADOW,
        MANAGED_EDGE_MODE_COMPARE,
        MANAGED_EDGE_MODE_AUTHORITATIVE,
    }
)
MANAGED_EDGE_GEOMETRY_SOURCE = "managed_directional_exit"
REALIZED_DYNAMIC_GEOMETRY_SOURCE = "realized_dynamic_exit_blend"
STATIC_TARGET_STOP_GEOMETRY_SOURCE = "static_target_stop"
AUTOTRADER_EDGE_MAX_STOCK_EXECUTION_STOP_LOSS_PCT = 30.0
AUTOTRADER_EDGE_MAX_CRYPTO_EXECUTION_STOP_LOSS_PCT = 60.0
AUTOTRADER_EDGE_MAX_OPTIONS_EXECUTION_STOP_LOSS_PCT = 0.0
AUTOTRADER_POSITIVE_REPRICE_DEFAULT_ENABLED = True
AUTOTRADER_POSITIVE_REPRICE_DEFAULT_ASSET_TYPES = "stock,crypto"


@dataclass
class RuleGateContext:
    """Inputs needed for rule evaluation (caller supplies quote + settings snapshot)."""

    current_price: float
    autotrader_open_count: int
    realized_loss_today_usd: float  # negative sum of closed autotrader PnL today (0 if none)
    # VV — per-lane open counts. Optional for backward compat; when present
    # the gate uses the lane-specific cap from StrategyParameter instead of
    # the legacy global ``max_concurrent`` cap. Keys: 'equity' | 'crypto' |
    # 'options'. Missing keys default to 0 — the gate also keeps the global
    # cap as a final-safety ceiling.
    autotrader_open_count_by_lane: Optional[dict] = None


@dataclass(frozen=True)
class RuleGateSettings:
    """Typed per-tick snapshot of the ``chili_autotrader_*`` settings.

    Phase D (tech-debt): the rule-gate code used to read ~13 settings via
    scattered ``getattr(settings, "chili_autotrader_...", default)`` calls
    — the defaults ended up duplicated at every callsite and drifting
    (capital fallback 25k vs 10k vs 100k in different paths). This
    dataclass is the single place those defaults live. ``passes_rule_gate``
    and ``resolve_effective_capital`` build one per call via
    :meth:`from_settings`.

    Defaults here MUST match the operator-facing defaults in
    ``app/config.py``; they exist as a belt-and-braces fallback when the
    config surface is bypassed (tests that pass a SimpleNamespace,
    paper-mode bootstraps, etc.).
    """

    # Capital / sizing
    assumed_capital_usd: float = 25_000.0

    # Session gating
    rth_only: bool = True
    allow_extended_hours: bool = False

    # Task KK — crypto path. When True, asset_type='crypto' alerts pass
    # the gate, the RTH check is skipped for them, and max_symbol_price
    # is not applied (crypto bases routinely exceed the equity $50 cap).
    crypto_enabled: bool = False

    # Task MM Phase 2 — options path. When True, asset_type='options'
    # alerts pass the gate. Most equity-specific checks are bypassed
    # (price cap, slippage tolerance, projected_profit_pct) because the
    # operator-driven option entry encodes its own limit price + sizing.
    # Kill-switch, drawdown breaker, concurrent-limit still apply.
    options_enabled: bool = False

    # Thresholds
    confidence_floor: float = 0.7
    # Deprecated fallback. Current admission uses expected net edge, so this
    # value should not act as a hidden 8%/9%/12% magic-profit threshold.
    min_projected_profit_pct: float = 0.0
    max_symbol_price_usd: float = AUTOTRADER_LEGACY_MAX_SYMBOL_PRICE_DEFAULT_USD
    fractional_equity_enabled: bool = AUTOTRADER_FRACTIONAL_EQUITY_DEFAULT_ENABLED
    max_entry_slippage_pct: float = AUTOTRADER_MAX_ENTRY_SLIPPAGE_DEFAULT_PCT
    options_min_underlying_reward_risk: float = 1.0
    options_min_option_reward_risk: float = 1.0
    options_min_expected_value_pct: float = 0.0

    # Daily loss caps (percent-of-equity preferred; dollar is fallback)
    daily_loss_cap_pct: float = 1.5
    daily_loss_cap_usd: float = 150.0

    # Concurrency
    # VV — legacy global cap; kept as outer-safety ceiling. The gate now
    # evaluates per-lane caps (equity/crypto/options) read from the
    # StrategyParameter ledger so the brain can adapt them. The global
    # cap only fires if a lane runs the brain's bookkeeping off the
    # rails. Default 60 = 3 lanes × 20 (lane bootstrap default).
    max_concurrent: int = 60
    max_concurrent_equity: int = 20
    max_concurrent_crypto: int = 20
    max_concurrent_options: int = 20

    # Broker-equity TTL cache (Phase B)
    broker_equity_cache_enabled: bool = False
    broker_equity_cache_ttl_seconds: int = 300
    broker_equity_cache_max_stale_seconds: int = 900

    @classmethod
    def from_settings(cls, source: Any) -> "RuleGateSettings":
        """Build a snapshot from any object exposing the ``chili_autotrader_*``
        attributes (``app.config.Settings``, pytest ``SimpleNamespace``,
        mock, etc.). Missing attributes fall back to the dataclass default.
        """
        def g(name: str, default: Any) -> Any:
            return getattr(source, name, default)

        return cls(
            assumed_capital_usd=float(g("chili_autotrader_assumed_capital_usd", cls.assumed_capital_usd)),
            rth_only=bool(g("chili_autotrader_rth_only", cls.rth_only)),
            allow_extended_hours=bool(g("chili_autotrader_allow_extended_hours", cls.allow_extended_hours)),
            crypto_enabled=bool(g("chili_autotrader_crypto_enabled", cls.crypto_enabled)),
            options_enabled=bool(g("chili_autotrader_options_enabled", cls.options_enabled)),
            confidence_floor=float(g("chili_autotrader_confidence_floor", cls.confidence_floor)),
            min_projected_profit_pct=float(
                g("chili_autotrader_min_projected_profit_pct", cls.min_projected_profit_pct)
            ),
            max_symbol_price_usd=float(g("chili_autotrader_max_symbol_price_usd", cls.max_symbol_price_usd)),
            fractional_equity_enabled=bool(
                g(
                    "chili_autotrader_fractional_equity_enabled",
                    cls.fractional_equity_enabled,
                )
            ),
            max_entry_slippage_pct=float(
                g("chili_autotrader_max_entry_slippage_pct", cls.max_entry_slippage_pct)
            ),
            options_min_underlying_reward_risk=float(
                g(
                    "chili_autotrader_options_min_underlying_reward_risk",
                    cls.options_min_underlying_reward_risk,
                )
            ),
            options_min_option_reward_risk=float(
                g(
                    "chili_autotrader_options_min_option_reward_risk",
                    cls.options_min_option_reward_risk,
                )
            ),
            options_min_expected_value_pct=float(
                g(
                    "chili_autotrader_options_min_expected_value_pct",
                    cls.options_min_expected_value_pct,
                )
            ),
            daily_loss_cap_pct=float(g("chili_autotrader_daily_loss_cap_pct", cls.daily_loss_cap_pct)),
            daily_loss_cap_usd=float(g("chili_autotrader_daily_loss_cap_usd", cls.daily_loss_cap_usd)),
            max_concurrent=int(g("chili_autotrader_max_concurrent", cls.max_concurrent)),
            max_concurrent_equity=int(
                g("chili_autotrader_max_concurrent_equity", cls.max_concurrent_equity)
            ),
            max_concurrent_crypto=int(
                g("chili_autotrader_max_concurrent_crypto", cls.max_concurrent_crypto)
            ),
            max_concurrent_options=int(
                g("chili_autotrader_max_concurrent_options", cls.max_concurrent_options)
            ),
            broker_equity_cache_enabled=bool(
                g("chili_autotrader_broker_equity_cache_enabled", cls.broker_equity_cache_enabled)
            ),
            broker_equity_cache_ttl_seconds=int(
                g("chili_autotrader_broker_equity_cache_ttl_seconds", cls.broker_equity_cache_ttl_seconds)
            ),
            broker_equity_cache_max_stale_seconds=int(
                g(
                    "chili_autotrader_broker_equity_cache_max_stale_seconds",
                    cls.broker_equity_cache_max_stale_seconds,
                )
            ),
        )


# ── Broker-equity TTL cache (Phase B) ────────────────────────────────
#
# ``resolve_effective_capital`` is called 3-4 times per auto-trader tick
# (inside ``resolve_brain_risk_context`` + twice inside ``passes_rule_gate``
# + once from the auto_trader entry point). Every call hit ``broker_service``
# directly — so when the broker flapped, a single tick amplified into
# 3-4 failing API calls, tripping rate limits and clobbering retry budget
# exactly when graceful degradation matters most.
#
# The cache is gated behind ``chili_autotrader_broker_equity_cache_enabled``
# (default False) so the rollout can soak in paper mode for 2+ sessions
# before being enabled on live accounts. When disabled, every call goes
# straight to the broker as before.
#
# Keyed on broker name ("robinhood" today; future multi-broker support
# slots in naturally). Stored fields: equity, source tag, cached_at ts.
# A ``cache:fresh`` value is returned whole; a ``cache:stale`` value is
# returned only when the broker is currently unreachable and the stale
# age is within ``max_stale_seconds``.
#
# Logged outcomes (prefix ``[chili_risk_cache]``):
#   - hit_fresh    — returned cache value within TTL
#   - miss_refresh — cache miss or expired; broker call succeeded
#   - miss_no_data — cache miss, broker returned None / disconnected / raised
#   - stale_serve  — broker unreachable; served aged cache value
#   - stale_expired — broker unreachable AND cache too old → fallback
#   - disabled     — feature flag is off; bypassing the cache entirely

_BrokerEquityEntry = tuple[float, str, float]  # (equity, source, cached_at)
_broker_equity_cache: dict[str, _BrokerEquityEntry] = {}
_broker_equity_cache_lock = threading.Lock()


def reset_broker_equity_cache_for_tests() -> None:
    """Clear the broker-equity TTL cache (pytest helper)."""
    with _broker_equity_cache_lock:
        _broker_equity_cache.clear()


def _broker_equity_cache_get(key: str) -> Optional[_BrokerEquityEntry]:
    with _broker_equity_cache_lock:
        return _broker_equity_cache.get(key)


def _broker_equity_cache_put(key: str, equity: float, source: str) -> None:
    with _broker_equity_cache_lock:
        _broker_equity_cache[key] = (equity, source, time.monotonic())


# XX - bound every broker call from the autotrader hot path with a hard
# wall-clock timeout. Hung-tick incident on 2026-04-27 had
# broker_service.get_portfolio() stuck for ~12 min which blocked every
# subsequent tick due to APScheduler max_instances=1.
# Combined with broker_equity_cache_enabled=true and max_instances>1
# this prevents one slow broker call from cascading into a stalled scheduler.

_BROKER_EQUITY_HARD_TIMEOUT_S = 10.0


def _call_with_timeout(fn, timeout_s, *args, **kwargs):
    """Run fn with hard wall-clock timeout. Returns (ok, result).

    On timeout/exception returns (False, None). ThreadPoolExecutor-based
    so it works on Windows / in threads / in async loops. Timed-out work
    continues in background until its socket times out.
    """
    import concurrent.futures
    ex = None
    try:
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = ex.submit(fn, *args, **kwargs)
        try:
            return True, future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return False, None
    except Exception as e:
        logger.debug("[autotrader] _call_with_timeout exception: %s", e)
        return False, None
    finally:
        if ex is not None:
            # Do not wait for a socket-stuck broker SDK call; the timeout's
            # entire job is to release the scheduler thread promptly.
            ex.shutdown(wait=False, cancel_futures=True)


def _fetch_broker_equity_once(
    fallback: float,
) -> tuple[float, str]:
    """Single uncached broker-equity lookup. Returns (equity, source).

    XX - every broker call wrapped in _call_with_timeout so a flapping
    broker can't stall the autotrader tick.
    """
    try:
        from .. import broker_service
        ok, connected = _call_with_timeout(
            broker_service.is_connected, _BROKER_EQUITY_HARD_TIMEOUT_S
        )
        if not ok:
            logger.warning(
                "[autotrader] is_connected timed out after %.1fs",
                _BROKER_EQUITY_HARD_TIMEOUT_S,
            )
            return fallback, "fallback:is_connected_timeout"
        if not connected:
            return fallback, "fallback:broker_disconnected"
        ok, portfolio = _call_with_timeout(
            broker_service.get_portfolio, _BROKER_EQUITY_HARD_TIMEOUT_S
        )
        if not ok:
            logger.warning(
                "[autotrader] get_portfolio timed out after %.1fs",
                _BROKER_EQUITY_HARD_TIMEOUT_S,
            )
            return fallback, "fallback:get_portfolio_timeout"
        if not isinstance(portfolio, dict):
            return fallback, "fallback:portfolio_unavailable"
        equity = portfolio.get("equity")
        try:
            equity_f = float(equity) if equity is not None else 0.0
        except (TypeError, ValueError):
            equity_f = 0.0
        if equity_f <= 0:
            return fallback, "fallback:equity_zero_or_missing"
        return equity_f, "broker_equity"
    except Exception:
        logger.debug("[autotrader] live capital resolve failed — using fallback", exc_info=True)
        return fallback, "fallback:exception"


def resolve_effective_capital(db: Session, settings: Any) -> tuple[float, str]:
    """Return (capital_usd, source).

    Phase 1: prefer live broker equity over the ``chili_autotrader_assumed_capital_usd``
    env default. The env value was a stale $25,000 assumption; real equity comes
    from ``broker_service.get_portfolio()`` which now works end-to-end (Phase 1
    of the phoenix-advisory fix). Fallback to the env value if the broker is
    unreachable or returns zero/missing equity — never go to 0, which would
    zero out every Kelly / notional calculation downstream.

    Phase B (tech-debt): a TTL cache in front of the broker lookup prevents a
    flapping broker from amplifying into a per-tick retry storm. Gated behind
    ``chili_autotrader_broker_equity_cache_enabled`` (default False).
    """
    # Phase D: read all knobs via the typed RuleGateSettings snapshot.
    # ``resolve_effective_capital`` is called 3-4x per tick (see the Phase B
    # comment above); building one snapshot per call keeps that cost
    # constant.
    gs = RuleGateSettings.from_settings(settings)
    fallback = gs.assumed_capital_usd
    cache_on = gs.broker_equity_cache_enabled
    if not cache_on:
        logger.debug(f"{CHILI_RISK_CACHE} disabled key=robinhood")
        return _fetch_broker_equity_once(fallback)

    ttl = gs.broker_equity_cache_ttl_seconds
    max_stale = gs.broker_equity_cache_max_stale_seconds
    key = "robinhood"  # single-broker today; key by broker name when multi-broker lands
    now = time.monotonic()
    entry = _broker_equity_cache_get(key)

    # Fresh cache hit.
    if entry is not None:
        equity, src, cached_at = entry
        age = now - cached_at
        if age < ttl and src == "broker_equity":
            logger.debug(
                f"{CHILI_RISK_CACHE} hit_fresh key=%s equity=%s age_s=%.1f ttl_s=%d",
                key, equity, age, ttl,
            )
            return equity, "cache:fresh"

    # Cache miss or expired — attempt a refresh.
    equity, src = _fetch_broker_equity_once(fallback)
    if src == "broker_equity":
        _broker_equity_cache_put(key, equity, src)
        logger.info(
            f"{CHILI_RISK_CACHE} miss_refresh key=%s equity=%s",
            key, equity,
        )
        return equity, src

    # Broker unreachable / returned unusable value. Serve stale if possible.
    if entry is not None:
        prev_equity, prev_src, prev_cached_at = entry
        stale_age = now - prev_cached_at
        if prev_src == "broker_equity" and stale_age <= (ttl + max_stale):
            logger.warning(
                f"{CHILI_RISK_CACHE} stale_serve key=%s equity=%s stale_age_s=%.1f "
                "budget_s=%d refresh_src=%s",
                key, prev_equity, stale_age, ttl + max_stale, src,
            )
            return prev_equity, "cache:stale"
        logger.warning(
            f"{CHILI_RISK_CACHE} stale_expired key=%s stale_age_s=%.1f budget_s=%d "
            "refresh_src=%s fallback=%s",
            key, stale_age, ttl + max_stale, src, equity,
        )
    else:
        logger.info(
            f"{CHILI_RISK_CACHE} miss_no_data key=%s refresh_src=%s fallback=%s",
            key, src, equity,
        )
    return equity, src


def resolve_brain_risk_context(
    db: Session,
    *,
    user_id: Optional[int],
    settings_override: Any | None = None,
) -> dict[str, Any]:
    """Return a snapshot of brain-driven risk inputs for a single tick.

    Phase 2: the autotrader consults the same regime + dial signals the rest
    of the brain uses, instead of acting on static env defaults. Returns a
    dict with keys:

    - ``regime``: ``"risk_on" | "cautious" | "risk_off" | None`` (from the
      regime runtime surface, same source as ``[bracket_intent_ops]`` logs)
    - ``drawdown_pct``: non-negative percent for dial computation (0 on
      uptrends, positive when equity is below the recent peak)
    - ``dial_value``: float output of ``risk_dial_model.compute_dial``,
      typically in ``[0, 1.5]``. ``1.0`` is baseline.
    - ``source``: short tag for audit rows (``"brain"`` or a fallback reason)

    Failures fall back to a neutral context (``dial_value=1.0``, no regime),
    so a transient failure can never make the gate paradoxically permissive
    or prevent the tick entirely.
    """
    ctx: dict[str, Any] = {
        "regime": None,
        "drawdown_pct": 0.0,
        "dial_value": 1.0,
        "source": "fallback:default",
    }
    try:
        from .runtime_surface_state import read_runtime_surface_state

        surface = read_runtime_surface_state(db, surface="regime")
        if surface:
            regime_raw = str(surface.get("regime") or "").strip().lower()
            if regime_raw in ("risk_on", "risk_off", "cautious"):
                ctx["regime"] = regime_raw
    except Exception:
        logger.debug("[autotrader] regime surface read failed", exc_info=True)

    # Drawdown: use unrealized + realized 5-day P&L vs current capital.
    try:
        from .portfolio_risk import _compute_unrealized_pnl  # type: ignore[attr-defined]
        from ...models.trading import Trade
        from datetime import datetime, timedelta

        cap, _ = resolve_effective_capital(db, settings_override or _get_settings())
        if user_id is not None and cap > 0:
            unreal = _compute_unrealized_pnl(db, user_id)
            cutoff = datetime.utcnow() - timedelta(days=5)
            real_5d = 0.0
            for t in (
                db.query(Trade)
                .filter(
                    Trade.user_id == user_id,
                    Trade.status == "closed",
                    Trade.exit_date.isnot(None),
                    Trade.exit_date >= cutoff,
                )
                .all()
            ):
                try:
                    pnl = trade_realized_pnl(t)
                    if pnl is None:
                        pnl = _finite_float(getattr(t, "pnl", None))
                    if pnl is not None:
                        real_5d += pnl
                except (TypeError, ValueError):
                    continue
            pnl_5d = float(unreal) + real_5d
            dd_pct = max(0.0, -(pnl_5d / cap * 100.0)) if pnl_5d < 0 else 0.0
            ctx["drawdown_pct"] = round(dd_pct, 3)
    except Exception:
        logger.debug("[autotrader] drawdown compute failed", exc_info=True)

    try:
        from .risk_dial_model import RiskDialConfig, RiskDialInput, compute_dial

        cfg = RiskDialConfig()
        out = compute_dial(
            RiskDialInput(
                regime=ctx["regime"],
                drawdown_pct=float(ctx["drawdown_pct"]),
                user_id=user_id,
            ),
            config=cfg,
        )
        ctx["dial_value"] = round(float(out.dial_value), 4)
        ctx["source"] = "brain"
    except Exception:
        logger.debug("[autotrader] dial compute failed — using neutral 1.0", exc_info=True)
        ctx["source"] = "fallback:dial_error"
    return ctx


def _get_settings():
    from ...config import settings
    return settings


def resolve_pattern_signal_context(
    db: Session,
    *,
    pattern_id: Optional[int],
    max_staleness_days: int = 14,
) -> dict[str, Any]:
    """Return learned (hit_rate, expectancy, profit_factor, n_cells) for a pattern.

    Phase 3: replaces hardcoded entry gates with values pulled from the
    brain's M.1 pattern-regime performance ledger. Each pattern's confident
    cells across the 8 regime dimensions are averaged to give a single
    signal-quality snapshot for confidence and expected-edge admission.

    When the pattern has no confident cells (new / under-traded), returns
    ``source="fallback:no_cells"`` and the caller uses the static env
    thresholds. Keeps behavior safe for cold-start patterns.
    """
    out: dict[str, Any] = {
        "pattern_id": pattern_id,
        "hit_rate": None,
        "expectancy": None,
        "profit_factor": None,
        "n_cells": 0,
        "n_trades_sum": 0,
        "n_trades_effective": 0,
        "source": "fallback:no_pattern_id",
    }
    if pattern_id is None:
        return out
    try:
        from datetime import date

        from .pattern_regime_ledger_lookup import load_resolved_context

        ctx = load_resolved_context(
            db,
            pattern_id=int(pattern_id),
            as_of_date=date.today(),
            max_staleness_days=int(max_staleness_days),
        )
        cells = list(ctx.cells_by_dimension.values())
        if not cells:
            out["source"] = "fallback:no_cells"
            return out

        def _mean(field: str) -> Optional[float]:
            vals = []
            for c in cells:
                v = getattr(c, field, None)
                if v is None:
                    continue
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                if f != f:  # NaN
                    continue
                vals.append(f)
            return sum(vals) / len(vals) if vals else None

        hit_rate = _mean("hit_rate")
        expectancy = _mean("expectancy")
        profit_factor = _mean("profit_factor")
        n_trades = sum(int(c.n_trades or 0) for c in cells)
        # ``n_trades`` is summed across regime dimensions, so the same
        # underlying trade can appear in multiple cells. For probability
        # shrinkage use a conservative effective count near the per-cell
        # average rather than overstating evidence by up to 8x.
        n_effective = int(math.ceil(n_trades / max(1, len(cells)))) if n_trades > 0 else 0

        out.update(
            hit_rate=round(hit_rate, 4) if hit_rate is not None else None,
            expectancy=round(expectancy, 4) if expectancy is not None else None,
            profit_factor=round(profit_factor, 4) if profit_factor is not None else None,
            n_cells=len(cells),
            n_trades_sum=n_trades,
            n_trades_effective=n_effective,
            source="brain_ledger",
        )
    except Exception:
        logger.debug("[autotrader] pattern signal context failed", exc_info=True)
        out["source"] = "fallback:exception"
    return out


def resolve_effective_slippage_pct(
    db: Session,
    *,
    user_id: Optional[int],
    settings: Any,
) -> tuple[float, str]:
    """Return (slippage_pct, source).

    Phase 1: prefer P90 historical slippage from ``execution_quality`` over the
    env default. The env value (``chili_autotrader_max_entry_slippage_pct``, 1.0%)
    is a static guess; the brain already measures actual slippage per-user and
    emits a P90 via ``suggest_adaptive_spread``. When <10 measurable trades exist
    the function returns ``insufficient_data`` — in that case we fall back to
    the env default rather than mistakenly locking in some arbitrary value.
    """
    # Phase D: typed snapshot so the default (1.0%) lives in exactly one place.
    fallback = RuleGateSettings.from_settings(settings).max_entry_slippage_pct
    if user_id is None:
        return fallback, "fallback:no_user"
    try:
        from .execution_quality import suggest_adaptive_spread

        suggestion = suggest_adaptive_spread(db, user_id=user_id, lookback_days=60)
        if not isinstance(suggestion, dict):
            return fallback, "fallback:no_suggestion"
        if suggestion.get("reason") == "insufficient_data":
            return fallback, "fallback:insufficient_data"
        p90 = suggestion.get("p90_slippage_pct")
        try:
            p90_f = float(p90) if p90 is not None else 0.0
        except (TypeError, ValueError):
            p90_f = 0.0
        if p90_f <= 0:
            return fallback, "fallback:p90_zero"
        # Cap adaptive slippage to a sane ceiling so a spell of bad fills
        # doesn't open the entry window to arbitrary drift. 3% absolute max.
        return min(3.0, p90_f), "adaptive_p90"
    except Exception:
        logger.debug("[autotrader] adaptive slippage resolve failed — using fallback", exc_info=True)
        return fallback, "fallback:exception"


def alert_confidence_from_score(alert: BreakoutAlert) -> float:
    """Match dispatch_alert mapping: min(0.95, 0.55 + 0.5 * composite)."""
    comp = float(alert.score_at_alert or 0.0)
    return min(0.95, 0.55 + 0.5 * comp)


def projected_profit_pct(entry: Optional[float], target: Optional[float]) -> Optional[float]:
    if entry is None or target is None:
        return None
    e = float(entry)
    t = float(target)
    if e <= 0:
        return None
    return round((t - e) / e * 100.0, 4)


@dataclass(frozen=True)
class EntryEdgeDecision:
    """Economic admission decision for a long autotrader entry.

    Percent target size is only geometry. This decision turns the full entry
    shape into expected net edge:

        p(win) * reward - p(loss) * stop_loss - empirical_costs

    and admits only when that value is positive. The snapshot is intentionally
    verbose because this replaces the old static projected-profit threshold.
    """

    allowed: bool
    reason: str
    snapshot: dict[str, Any]


def _fraction01(value: Any, default: float | None = None) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    if v > 1.0 and v <= 100.0:
        v = v / 100.0
    return max(0.0, min(1.0, v))


def _finite_float(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _load_scan_pattern_for_edge(db: Session, pattern_id: Any) -> ScanPattern | None:
    try:
        pid = int(pattern_id)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        pat = (
            db.query(ScanPattern)
            .filter(ScanPattern.id == pid)
            .one_or_none()
        )
    except Exception:
        return None
    if pat is None or pat.__class__.__module__.startswith("unittest.mock"):
        return None
    return pat


def _probability_sample_count(
    pattern: Any,
    sample_n: int | None,
) -> tuple[int | None, str | None]:
    """Return a closed-trade sample count suitable for probability shrinkage."""
    try:
        n = int(sample_n) if sample_n is not None else None
    except (TypeError, ValueError):
        n = None
    raw_n = getattr(pattern, "raw_realized_trade_count", None)
    try:
        raw_int = int(raw_n) if raw_n is not None else None
    except (TypeError, ValueError):
        raw_int = None
    if n is not None and raw_int is not None and raw_int >= 0 and n > raw_int:
        return raw_int, "closed_sample_count_guard"
    return n, None


def _settings_int(settings: Any, name: str, default: int, *, minimum: int = 0) -> int:
    raw = getattr(settings, name, default)
    if _looks_like_mock_setting(raw):
        raw = default
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(minimum, value)


def _settings_float(
    settings: Any,
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = getattr(settings, name, default)
    if _looks_like_mock_setting(raw):
        raw = default
    try:
        value = float(raw)
    except Exception:
        value = default
    if not math.isfinite(value):
        value = default
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def _max_execution_stop_loss_fraction(settings: Any, asset_type: Any) -> float | None:
    """Asset-aware cap for the stop distance the live executor will actually use."""
    asset = str(asset_type or "stock").strip().lower()
    if asset in {"crypto", "cryptocurrency"}:
        setting_name = "chili_autotrader_crypto_max_execution_stop_loss_pct"
        default_pct = AUTOTRADER_EDGE_MAX_CRYPTO_EXECUTION_STOP_LOSS_PCT
    elif asset in {"option", "options"}:
        setting_name = "chili_autotrader_options_max_execution_stop_loss_pct"
        default_pct = AUTOTRADER_EDGE_MAX_OPTIONS_EXECUTION_STOP_LOSS_PCT
    else:
        setting_name = "chili_autotrader_stock_max_execution_stop_loss_pct"
        default_pct = AUTOTRADER_EDGE_MAX_STOCK_EXECUTION_STOP_LOSS_PCT
    cap_pct = _settings_float(
        settings,
        setting_name,
        default_pct,
        minimum=0.0,
        maximum=100.0,
    )
    if cap_pct <= 0.0:
        return None
    return cap_pct / 100.0


_TRUE_SETTING_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_SETTING_TOKENS = frozenset({"0", "false", "no", "off"})


def _looks_like_mock_setting(value: Any) -> bool:
    return value.__class__.__module__.startswith("unittest.mock")


def _settings_bool(settings: Any, name: str, default: bool) -> bool:
    raw = getattr(settings, name, default)
    if _looks_like_mock_setting(raw):
        raw = default
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in _TRUE_SETTING_TOKENS:
            return True
        if token in _FALSE_SETTING_TOKENS:
            return False
        return bool(default)
    return bool(raw)


def _settings_csv_set(settings: Any, name: str, default: str) -> set[str]:
    raw = getattr(settings, name, default)
    if _looks_like_mock_setting(raw):
        raw = default
    if isinstance(raw, (set, tuple, list)):
        values = raw
    else:
        values = str(raw or default).split(",")
    return {str(v).strip().lower() for v in values if str(v).strip()}


def _bayes_lower_probability(
    *,
    successes: float,
    observations: float,
    prior_n: float,
    z: float,
    prior_mean: float = 0.5,
) -> dict[str, Any] | None:
    if observations <= 0.0:
        return None
    successes = max(0.0, min(float(observations), float(successes)))
    prior_mean = max(0.0, min(1.0, float(prior_mean)))
    prior_n = max(0.0, float(prior_n))
    alpha = successes + prior_mean * prior_n
    beta = (float(observations) - successes) + (1.0 - prior_mean) * prior_n
    total = alpha + beta
    if total <= 0.0:
        return None
    mean = alpha / total
    variance = (alpha * beta) / ((total * total) * (total + 1.0))
    lower = max(0.0, min(1.0, mean - max(0.0, float(z)) * math.sqrt(max(0.0, variance))))
    return {
        "mean_probability": mean,
        "lower_probability": lower,
        "alpha": alpha,
        "beta": beta,
        "observations": observations,
        "successes": successes,
        "prior_n": prior_n,
        "prior_mean": prior_mean,
        "z": z,
    }


def _directional_row_value(row: Any, key: str, idx: int) -> Any:
    if hasattr(row, "get"):
        return row.get(key)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping.get(key)
    try:
        return row[idx]
    except Exception:
        return None


def _load_directional_outcome_rows(
    db: Session,
    *,
    pattern_id: int,
    ticker: str,
    exact_ticker: bool,
    limit: int,
) -> list[Any]:
    if pattern_id <= 0 or limit <= 0:
        return []
    try:
        from sqlalchemy import text

        ticker_clause = "AND UPPER(ticker) = UPPER(:ticker)" if exact_ticker else (
            "AND (:ticker = '' OR UPPER(ticker) <> UPPER(:ticker))"
        )
        result = db.execute(
            text(
                f"""
                SELECT ticker, alert_at,
                       window_max_favorable_pct,
                       window_max_adverse_pct,
                       directional_correct
                FROM pattern_alert_directional_outcome
                WHERE scan_pattern_id = :pattern_id
                  AND directional_correct IS NOT NULL
                  AND window_max_favorable_pct IS NOT NULL
                  AND window_max_adverse_pct IS NOT NULL
                  {ticker_clause}
                ORDER BY alert_at DESC
                LIMIT :limit
                """
            ),
            {
                "pattern_id": int(pattern_id),
                "ticker": str(ticker or "").upper(),
                "limit": int(limit),
            },
        )
        mapped = result.mappings()
        if hasattr(mapped, "all"):
            return list(mapped.all())
        return list(mapped)
    except Exception:
        return []


def _score_directional_rows_for_edge(
    rows: list[Any],
    *,
    reward: float,
    loss: float,
    prior_n: int,
    z: float,
) -> dict[str, Any] | None:
    if not rows or reward <= 0.0 or loss <= 0.0:
        return None
    reward_pct = float(reward) * 100.0
    loss_pct = float(loss) * 100.0
    successes = 0.0
    observations = 0.0
    reward_hits = 0
    stop_breaches = 0
    ambiguous = 0
    directional_correct = 0
    fav_vals: list[float] = []
    adv_vals: list[float] = []

    for row in rows:
        fav = _finite_float(_directional_row_value(row, "window_max_favorable_pct", 2))
        adv = _finite_float(_directional_row_value(row, "window_max_adverse_pct", 3))
        if fav is None or adv is None:
            continue
        observations += 1.0
        fav_vals.append(float(fav))
        adv_vals.append(float(adv))
        if bool(_directional_row_value(row, "directional_correct", 4)):
            directional_correct += 1
        hit_reward = float(fav) >= reward_pct
        hit_stop = float(adv) <= -loss_pct
        if hit_reward:
            reward_hits += 1
        if hit_stop:
            stop_breaches += 1
        if hit_reward and hit_stop:
            # With MFE/MAE bars but no intrabar path, sequence is unknown.
            # Count it as half a win instead of pretending target came first.
            ambiguous += 1
            successes += 0.5
        elif hit_reward:
            successes += 1.0

    if observations <= 0.0:
        return None
    posterior = _bayes_lower_probability(
        successes=successes,
        observations=observations,
        prior_n=float(prior_n),
        z=float(z),
    )
    if posterior is None:
        return None
    return {
        **posterior,
        "sample_n": int(observations),
        "reward_hits": reward_hits,
        "stop_breaches": stop_breaches,
        "ambiguous_path_count": ambiguous,
        "directional_correct_count": directional_correct,
        "directional_wr": directional_correct / observations,
        "avg_max_favorable_pct": sum(fav_vals) / len(fav_vals) if fav_vals else None,
        "avg_max_adverse_pct": sum(adv_vals) / len(adv_vals) if adv_vals else None,
        "reward_threshold_pct": reward_pct,
        "stop_threshold_pct": loss_pct,
    }


def _directional_edge_probability(
    db: Session,
    *,
    alert: BreakoutAlert,
    reward: float,
    loss: float,
    settings: Any,
) -> dict[str, Any] | None:
    try:
        pattern_id = int(alert.scan_pattern_id or 0)
    except (TypeError, ValueError):
        pattern_id = 0
    if pattern_id <= 0:
        return None

    prior_n = _settings_int(settings, "chili_realized_ev_min_trades", 5, minimum=0)
    z = _settings_float(
        settings,
        "chili_autotrader_directional_probability_z",
        AUTOTRADER_DIRECTIONAL_PROBABILITY_DEFAULT_Z,
        minimum=0.0,
        maximum=AUTOTRADER_DIRECTIONAL_PROBABILITY_MAX_Z,
    )
    limit = _settings_int(
        settings,
        "chili_autotrader_directional_probability_max_rows",
        AUTOTRADER_DIRECTIONAL_PROBABILITY_DEFAULT_MAX_ROWS,
        minimum=AUTOTRADER_DIRECTIONAL_PROBABILITY_MIN_ROWS,
    )
    ticker_rows = _load_directional_outcome_rows(
        db,
        pattern_id=pattern_id,
        ticker=alert.ticker or "",
        exact_ticker=True,
        limit=limit,
    )
    pattern_rows = _load_directional_outcome_rows(
        db,
        pattern_id=pattern_id,
        ticker=alert.ticker or "",
        exact_ticker=False,
        limit=limit,
    )
    ticker_ev = _score_directional_rows_for_edge(
        ticker_rows, reward=reward, loss=loss, prior_n=prior_n, z=z,
    )
    pattern_ev = _score_directional_rows_for_edge(
        pattern_rows, reward=reward, loss=loss, prior_n=prior_n, z=z,
    )
    if ticker_ev is None and pattern_ev is None:
        return None
    if ticker_ev is not None and pattern_ev is not None:
        # Ticker-specific evidence should matter more as it accumulates, but
        # until then the pattern-wide bucket is the safer base rate.
        ticker_n = float(ticker_ev["observations"])
        ticker_weight = ticker_n / max(1.0, ticker_n + float(prior_n))
        prob = (
            ticker_weight * float(ticker_ev["lower_probability"])
            + (1.0 - ticker_weight) * float(pattern_ev["lower_probability"])
        )
        source = "directional_mfe_mae_pattern_ticker_blend"
    elif ticker_ev is not None:
        ticker_weight = 1.0
        prob = float(ticker_ev["lower_probability"])
        source = "directional_mfe_mae_ticker"
    else:
        ticker_weight = 0.0
        prob = float(pattern_ev["lower_probability"])  # type: ignore[index]
        source = "directional_mfe_mae_pattern"

    sample_n = int(
        (ticker_ev or {}).get("sample_n", 0) + (pattern_ev or {}).get("sample_n", 0)
    )
    return {
        "probability": max(0.0, min(1.0, prob)),
        "source": source,
        "sample_n": sample_n,
        "prior_n": prior_n,
        "z": z,
        "ticker_weight": round(float(ticker_weight), 6),
        "ticker": _round_directional_evidence(ticker_ev),
        "pattern": _round_directional_evidence(pattern_ev),
    }


def _round_directional_evidence(evidence: dict[str, Any] | None) -> dict[str, Any] | None:
    if evidence is None:
        return None
    out: dict[str, Any] = {}
    for key, value in evidence.items():
        if isinstance(value, float):
            out[key] = round(value, 6)
        else:
            out[key] = value
    return out


def _directional_component_for_managed_edge(
    evidence: dict[str, Any] | None,
    *,
    scope: str,
) -> dict[str, Any] | None:
    if not evidence:
        return None
    try:
        sample_n = int(evidence.get("sample_n") or evidence.get("observations") or 0)
    except (TypeError, ValueError):
        sample_n = 0
    fav_pct = _finite_float(evidence.get("avg_max_favorable_pct"))
    adv_pct = _finite_float(evidence.get("avg_max_adverse_pct"))
    if sample_n <= 0 or fav_pct is None or adv_pct is None:
        return None
    if fav_pct <= 0.0:
        return None
    return {
        "scope": scope,
        "sample_n": sample_n,
        "avg_max_favorable_pct": float(fav_pct),
        "avg_max_adverse_pct": float(adv_pct),
    }


def _managed_exit_geometry_from_directional(
    *,
    alert: BreakoutAlert,
    entry_price: float,
    static_reward: float,
    base_reward: float,
    base_loss: float,
    directional: dict[str, Any] | None,
    settings: Any,
) -> tuple[float, float, dict[str, Any]]:
    mode = str(
        getattr(
            settings,
            "chili_autotrader_managed_edge_mode",
            AUTOTRADER_MANAGED_EDGE_DEFAULT_MODE,
        )
        or AUTOTRADER_MANAGED_EDGE_DEFAULT_MODE
    ).strip().lower()
    snap: dict[str, Any] = {
        "used": False,
        "selected": False,
        "mode": mode,
        "reason": "inactive",
        "static_reward_fraction": round(static_reward, 8),
        "base_reward_fraction": round(base_reward, 8),
        "base_stop_loss_fraction": round(base_loss, 8),
    }
    if mode not in MANAGED_EDGE_ACTIVE_MODES:
        snap["reason"] = "mode_inactive"
        return base_reward, base_loss, snap

    asset_type = str(getattr(alert, "asset_type", None) or "stock").strip().lower()
    allowed_assets = _settings_csv_set(
        settings,
        "chili_autotrader_managed_edge_asset_types",
        AUTOTRADER_MANAGED_EDGE_DEFAULT_ASSET_TYPES,
    )
    snap["asset_type"] = asset_type
    snap["allowed_asset_types"] = sorted(allowed_assets)
    if "all" not in allowed_assets and asset_type not in allowed_assets:
        snap["reason"] = "asset_type_not_enabled"
        return base_reward, base_loss, snap

    if not directional:
        snap["reason"] = "missing_directional_evidence"
        return base_reward, base_loss, snap

    components = [
        comp
        for comp in (
            _directional_component_for_managed_edge(
                directional.get("ticker") if isinstance(directional, dict) else None,
                scope="ticker",
            ),
            _directional_component_for_managed_edge(
                directional.get("pattern") if isinstance(directional, dict) else None,
                scope="pattern",
            ),
        )
        if comp is not None
    ]
    min_samples = _settings_int(
        settings,
        "chili_autotrader_managed_edge_min_directional_samples",
        AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_DIRECTIONAL_SAMPLES,
        minimum=1,
    )
    sample_n = sum(int(comp["sample_n"]) for comp in components)
    snap["sample_n"] = sample_n
    snap["min_directional_samples"] = min_samples
    snap["components"] = components
    if sample_n < min_samples:
        snap["reason"] = "insufficient_directional_samples"
        return base_reward, base_loss, snap

    fav_pct = (
        sum(float(comp["avg_max_favorable_pct"]) * int(comp["sample_n"]) for comp in components)
        / float(sample_n)
    )
    adv_pct = (
        sum(float(comp["avg_max_adverse_pct"]) * int(comp["sample_n"]) for comp in components)
        / float(sample_n)
    )
    observed_fav_fraction = max(0.0, fav_pct / 100.0)
    observed_adv_fraction = abs(adv_pct) / 100.0
    capture_fraction = _settings_float(
        settings,
        "chili_autotrader_managed_edge_capture_fraction",
        AUTOTRADER_MANAGED_EDGE_DEFAULT_CAPTURE_FRACTION,
        minimum=0.0,
        maximum=1.0,
    )
    adverse_buffer = _settings_float(
        settings,
        "chili_autotrader_managed_edge_adverse_buffer",
        AUTOTRADER_MANAGED_EDGE_DEFAULT_ADVERSE_BUFFER,
        minimum=1.0,
    )
    min_reward = _settings_float(
        settings,
        "chili_autotrader_managed_edge_min_reward_fraction",
        AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_REWARD_FRACTION,
        minimum=0.0,
    )
    max_reward = _settings_float(
        settings,
        "chili_autotrader_managed_edge_max_reward_fraction",
        AUTOTRADER_MANAGED_EDGE_DEFAULT_MAX_REWARD_FRACTION,
        minimum=0.0,
    )
    if max_reward > 0.0:
        reward_ceiling = min(max_reward, base_reward)
    else:
        reward_ceiling = base_reward
    managed_reward = min(reward_ceiling, max(min_reward, observed_fav_fraction * capture_fraction))
    managed_loss = observed_adv_fraction * adverse_buffer
    static_to_managed_ratio = (
        static_reward / managed_reward
        if managed_reward > 0.0
        else 0.0
    )
    min_static_ratio = _settings_float(
        settings,
        "chili_autotrader_managed_edge_static_to_managed_reward_ratio",
        AUTOTRADER_MANAGED_EDGE_DEFAULT_STATIC_TO_MANAGED_REWARD_RATIO,
        minimum=1.0,
    )
    min_reward_risk = _settings_float(
        settings,
        "chili_autotrader_managed_edge_min_reward_risk",
        AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_REWARD_RISK,
        minimum=0.0,
    )
    snap.update(
        observed_avg_max_favorable_pct=round(fav_pct, 6),
        observed_avg_max_adverse_pct=round(adv_pct, 6),
        observed_favorable_fraction=round(observed_fav_fraction, 8),
        observed_adverse_fraction=round(observed_adv_fraction, 8),
        capture_fraction=round(capture_fraction, 6),
        adverse_buffer=round(adverse_buffer, 6),
        min_reward_fraction=round(min_reward, 8),
        max_reward_fraction=round(max_reward, 8),
        min_static_to_managed_reward_ratio=round(min_static_ratio, 6),
        min_reward_risk=round(min_reward_risk, 6),
        candidate_reward_fraction=round(managed_reward, 8),
        candidate_stop_loss_fraction=round(managed_loss, 8),
        static_to_managed_reward_ratio=round(static_to_managed_ratio, 6),
    )
    if managed_reward <= 0.0 or managed_loss <= 0.0:
        snap["reason"] = "invalid_managed_geometry"
        return base_reward, base_loss, snap
    if managed_reward >= base_reward:
        snap["reason"] = "managed_reward_not_tighter_than_base"
        return base_reward, base_loss, snap
    if managed_loss >= base_loss:
        snap["reason"] = "managed_stop_not_tighter_than_base"
        return base_reward, base_loss, snap
    if static_to_managed_ratio < min_static_ratio:
        snap["reason"] = "static_bracket_not_overextended"
        return base_reward, base_loss, snap
    reward_risk = managed_reward / managed_loss if managed_loss > 0.0 else None
    snap["reward_risk"] = round(float(reward_risk), 6) if reward_risk is not None else None
    if reward_risk is None or reward_risk < min_reward_risk:
        snap["reason"] = "managed_reward_risk_below_floor"
        return base_reward, base_loss, snap

    managed_target = entry_price * (1.0 + managed_reward)
    managed_stop = entry_price * (1.0 - managed_loss)
    snap.update(
        used=True,
        reason=MANAGED_EDGE_GEOMETRY_SOURCE,
        managed_reward_fraction=round(managed_reward, 8),
        managed_stop_loss_fraction=round(managed_loss, 8),
        managed_target_price=round(managed_target, 8),
        managed_stop_price=round(managed_stop, 8),
    )
    return managed_reward, managed_loss, snap


def _expected_edge_components(
    *,
    probability: float,
    reward: float,
    loss: float,
    cost_fraction: float,
) -> dict[str, float | None]:
    expected_reward = probability * reward
    expected_loss = (1.0 - probability) * loss
    expected_net = expected_reward - expected_loss - cost_fraction
    breakeven_denom = reward + loss
    breakeven_probability = (
        (loss + cost_fraction) / breakeven_denom
        if breakeven_denom > 0.0
        else None
    )
    return {
        "expected_reward": expected_reward,
        "expected_loss": expected_loss,
        "expected_net": expected_net,
        "breakeven_probability": breakeven_probability,
        "probability_edge": (
            probability - breakeven_probability
            if breakeven_probability is not None
            else None
        ),
        "reward_risk": reward / loss if loss > 0.0 else None,
    }


def _alert_confidence_probability(confidence: float, settings: Any) -> tuple[float, dict[str, Any]]:
    raw = _fraction01(confidence, 0.5)
    if raw is None:
        raw = 0.5
    weight = _settings_float(
        settings,
        "chili_autotrader_alert_confidence_probability_weight",
        0.25,
        minimum=0.0,
        maximum=1.0,
    )
    p = 0.5 + (float(raw) - 0.5) * weight
    return max(0.0, min(1.0, p)), {
        "raw_alert_confidence": round(float(raw), 6),
        "weight": round(float(weight), 6),
        "reason": "score_confidence_is_not_a_calibrated_win_probability",
    }


def _pattern_probability(
    db: Session,
    *,
    alert: BreakoutAlert,
    pat_ctx: dict[str, Any],
    confidence: float,
    settings: Any,
    pattern: ScanPattern | None = None,
    reward: float,
    loss: float,
) -> tuple[float, str, int | None, dict[str, Any]]:
    directional = _directional_edge_probability(
        db,
        alert=alert,
        reward=reward,
        loss=loss,
        settings=settings,
    )
    details: dict[str, Any] = {
        "directional_evidence": directional,
        "alert_confidence": None,
    }
    p = _fraction01(pat_ctx.get("hit_rate"))
    n: int | None = None
    if p is not None:
        try:
            n = int(
                pat_ctx.get("n_trades_effective")
                if pat_ctx.get("n_trades_effective") is not None
                else pat_ctx.get("n_trades_sum") or 0
            )
        except (TypeError, ValueError):
            n = None
        source = "pattern_regime_hit_rate"
    else:
        source = "missing"
        if alert.scan_pattern_id:
            try:
                from .pattern_stats_accessor import get_corrected_pattern_stats

                pat = pattern or _load_scan_pattern_for_edge(db, alert.scan_pattern_id)
                if pat is not None:
                    stats = get_corrected_pattern_stats(pat)
                    p2 = _fraction01(stats.win_rate)
                    n2, n_guard = _probability_sample_count(pat, stats.trade_count)
                    if p2 is not None and n2 is not None and n2 > 0:
                        p = p2
                        n = n2
                        source = f"pattern_{stats.source_win_rate}_win_rate"
                        if n_guard:
                            source = f"{source}_{n_guard}"
            except Exception:
                pass

    if source == "pattern_regime_hit_rate" and (n is None or n <= 0):
        details["ignored_regime_hit_rate_reason"] = "missing_effective_sample_n"
        p = None
        n = None
        source = "missing"

    if p is None:
        if directional is not None:
            p = float(directional["probability"])
            n = int(directional.get("sample_n") or 0)
            source = str(directional.get("source") or "directional_mfe_mae")
        else:
            p, alert_details = _alert_confidence_probability(confidence, settings)
            details["alert_confidence"] = alert_details
            source = "alert_confidence_shrunk"

    # Shrink pattern-derived probabilities toward break-even until the
    # realized-EV evidence count matures. The prior size reuses CHILI's
    # existing promotion evidence knob instead of inventing a new threshold.
    if (
        n is not None
        and n > 0
        and not source.startswith("alert_")
        and not source.startswith("directional_")
    ):
        prior_n = _settings_int(settings, "chili_realized_ev_min_trades", 5, minimum=0)
        if prior_n > 0:
            p = (p * n + 0.5 * prior_n) / max(1, n + prior_n)
            source = f"{source}_shrunk"
            details["neutral_prior_n"] = prior_n

    # Imminent-alert outcomes are gate-chain-free observations. Use them as a
    # cold-start prior when closed-trade evidence is thin, but let actual
    # closed trades dominate as their count grows.
    if (
        directional is not None
        and not source.startswith("directional_")
        and not source.startswith("alert_")
    ):
        prior_n = _settings_int(settings, "chili_realized_ev_min_trades", 5, minimum=0)
        closed_n = max(0, int(n or 0))
        trade_weight = (
            closed_n / max(1.0, float(closed_n + prior_n))
            if prior_n > 0
            else 1.0
        )
        if trade_weight < 1.0:
            p = trade_weight * float(p) + (1.0 - trade_weight) * float(
                directional["probability"]
            )
            source = f"{source}_directional_cold_start_blend"
            details["trade_evidence_weight"] = round(float(trade_weight), 6)

    p = max(0.0, min(1.0, p))
    details["final_probability"] = round(float(p), 6)
    details["final_source"] = source
    details["final_sample_n"] = n
    return p, source, n, details


def _exit_config_dict(pattern: ScanPattern | None) -> dict[str, Any]:
    if pattern is None:
        return {}
    raw = getattr(pattern, "exit_config", None)
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _edge_learned_exit_config_geometry(
    *,
    pattern: ScanPattern | None,
    static_reward: float,
    static_loss: float,
) -> tuple[float, float, dict[str, Any]]:
    snap: dict[str, Any] = {
        "used": False,
        "reason": "missing_edge_learned_exit_config",
        "static_reward_fraction": round(static_reward, 8),
        "static_stop_loss_fraction": round(static_loss, 8),
    }
    cfg = _exit_config_dict(pattern)
    if not cfg:
        return static_reward, static_loss, snap
    payload = cfg.get("edge_learned_exit_v1")
    if isinstance(payload, dict):
        learned = payload
    elif payload is True or cfg.get("source") == "autotrader_edge_debt_v1":
        learned = cfg
    else:
        return static_reward, static_loss, snap

    reward = _finite_float(
        learned.get("target_reward_fraction")
        or learned.get("reward_fraction")
        or learned.get("target_fraction")
    )
    loss = _finite_float(
        learned.get("stop_loss_fraction")
        or learned.get("loss_fraction")
        or learned.get("stop_fraction")
    )
    if reward is None or loss is None or reward <= 0.0 or loss <= 0.0:
        snap["reason"] = "invalid_edge_learned_geometry"
        return static_reward, static_loss, snap
    if reward >= static_reward and loss >= static_loss:
        snap["reason"] = "edge_learned_geometry_not_tighter_than_static"
        snap["target_reward_fraction"] = round(float(reward), 8)
        snap["stop_loss_fraction"] = round(float(loss), 8)
        return static_reward, static_loss, snap

    snap.update(
        used=True,
        reason="scan_pattern_edge_learned_exit_config",
        source=learned.get("source") or cfg.get("source") or "autotrader_edge_debt_v1",
        basis=learned.get("basis"),
        target_reward_fraction=round(float(reward), 8),
        stop_loss_fraction=round(float(loss), 8),
        reward_risk=round(float(reward / loss), 6),
        sample_n=learned.get("sample_n"),
        total_edge_rejects=learned.get("total_edge_rejects"),
        parent_pattern_id=learned.get("parent_pattern_id"),
    )
    return float(reward), float(loss), snap


def _realized_exit_geometry(
    *,
    pattern: ScanPattern | None,
    static_reward: float,
    static_loss: float,
    settings: Any,
) -> tuple[float, float, dict[str, Any]]:
    """Blend alert bracket geometry with realized dynamic-exit payoff stats.

    The displayed target/stop is the hard plan, but CHILI's open-position
    monitor often exits before either bracket. For patterns with materialized
    avg winner/loser stats, use a Bayesian blend so mature dynamic-exit
    evidence can rescue low-win-rate/high-payoff edges without giving thin
    samples a free pass.
    """
    snap: dict[str, Any] = {
        "used": False,
        "reason": "no_pattern",
        "static_reward_fraction": round(static_reward, 8),
        "static_stop_loss_fraction": round(static_loss, 8),
    }
    if pattern is None:
        return static_reward, static_loss, snap

    cfg_reward, cfg_loss, cfg_snap = _edge_learned_exit_config_geometry(
        pattern=pattern,
        static_reward=static_reward,
        static_loss=static_loss,
    )
    if cfg_snap.get("used"):
        return cfg_reward, cfg_loss, cfg_snap
    snap["edge_learned_exit_config"] = cfg_snap

    winner = _finite_float(getattr(pattern, "avg_winner_pct", None))
    loser = _finite_float(getattr(pattern, "avg_loser_pct", None))
    if winner is None or loser is None:
        snap["reason"] = "missing_realized_winner_loser"
        return static_reward, static_loss, snap
    if (abs(winner) > 1.0 and abs(winner) <= 100.0) or (abs(loser) > 1.0 and abs(loser) <= 100.0):
        winner = winner / 100.0
        loser = loser / 100.0
    realized_reward = winner if winner > 0.0 else None
    realized_loss = abs(loser) if loser < 0.0 else None
    if realized_reward is None or realized_loss is None or realized_loss <= 0.0:
        snap["reason"] = "invalid_realized_winner_loser"
        return static_reward, static_loss, snap

    try:
        from .pattern_stats_accessor import get_corrected_pattern_stats

        stats = get_corrected_pattern_stats(pattern)
        avg_return = _finite_float(stats.avg_return_pct)
        n = int(stats.trade_count or 0)
    except Exception:
        avg_return = _finite_float(getattr(pattern, "corrected_avg_return_pct", None))
        try:
            n = int(
                getattr(pattern, "corrected_trade_count", None)
                or 0
            )
        except (TypeError, ValueError):
            n = 0
    try:
        payoff_n = int(getattr(pattern, "payoff_ratio_n", None) or 0)
    except (TypeError, ValueError):
        payoff_n = 0
    guarded_n, n_guard = _probability_sample_count(pattern, n)
    n_candidates = [
        int(x)
        for x in (guarded_n, payoff_n)
        if x is not None and int(x) > 0
    ]
    n = min(n_candidates) if n_candidates else int(guarded_n or payoff_n or 0)
    if avg_return is not None and avg_return <= 0.0:
        snap["reason"] = "non_positive_realized_avg_return"
        snap["corrected_avg_return_pct"] = round(avg_return, 6)
        return static_reward, static_loss, snap
    if n <= 0:
        snap["reason"] = "missing_realized_sample_n"
        return static_reward, static_loss, snap

    try:
        prior_n = max(0, int(getattr(settings, "chili_realized_ev_min_trades", 5)))
    except Exception:
        prior_n = 5
    evidence_weight = n / max(1, n + prior_n)
    reward = evidence_weight * realized_reward + (1.0 - evidence_weight) * static_reward
    loss = evidence_weight * realized_loss + (1.0 - evidence_weight) * static_loss
    if reward <= 0.0 or loss <= 0.0:
        snap["reason"] = "invalid_blended_geometry"
        return static_reward, static_loss, snap

    snap.update(
        used=True,
        reason="scan_pattern_realized_dynamic_exit_blend",
        realized_reward_fraction=round(realized_reward, 8),
        realized_loss_fraction=round(realized_loss, 8),
        realized_sample_n=n,
        realized_sample_n_guard=n_guard,
        realized_prior_n=prior_n,
        realized_evidence_weight=round(evidence_weight, 6),
        corrected_avg_return_pct=round(avg_return, 6) if avg_return is not None else None,
        payoff_ratio=_finite_float(getattr(pattern, "payoff_ratio", None)),
        payoff_ratio_n=getattr(pattern, "payoff_ratio_n", None),
        blended_reward_fraction=round(reward, 8),
        blended_stop_loss_fraction=round(loss, 8),
    )
    return reward, loss, snap


def _empirical_entry_cost_fraction(
    db: Session,
    *,
    ticker: str,
    settings: Any,
) -> tuple[float, dict[str, Any]]:
    """Return empirical entry cost fraction from TCA rows.

    Missing cost data returns zero and says why; the Coinbase venue cost gate
    still adds explicit fee protection downstream.
    """
    try:
        from sqlalchemy import text

        row = db.execute(text("""
            SELECT sample_trades, p90_spread_bps, p90_slippage_bps,
                   median_spread_bps, median_slippage_bps, last_updated_at
            FROM trading_execution_cost_estimates
            WHERE UPPER(ticker) = UPPER(:ticker)
              AND LOWER(side) = 'buy'
            ORDER BY last_updated_at DESC
            LIMIT 1
        """), {"ticker": ticker}).mappings().first()
    except Exception:
        return 0.0, {"used": False, "reason": "query_failed"}
    if (
        not row
        or not hasattr(row, "get")
        or row.__class__.__module__.startswith("unittest.mock")
    ):
        return 0.0, {"used": False, "reason": "no_estimate"}

    try:
        samples = int(row.get("sample_trades") or 0)
    except (TypeError, ValueError):
        samples = 0
    try:
        min_samples = max(
            1, int(getattr(settings, "chili_coinbase_cost_gate_min_tca_samples", 5))
        )
    except Exception:
        min_samples = 5
    if samples < min_samples:
        return 0.0, {
            "used": False,
            "reason": "insufficient_samples",
            "sample_trades": samples,
            "min_samples": min_samples,
        }

    def _bps(name: str) -> float:
        try:
            v = float(row.get(name) or 0.0)
            return v if v > 0.0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    p90_spread = _bps("p90_spread_bps")
    p90_slip = _bps("p90_slippage_bps")
    total_bps = p90_spread + p90_slip
    last_updated = row.get("last_updated_at")
    return total_bps / 10000.0, {
        "used": True,
        "sample_trades": samples,
        "p90_spread_bps": p90_spread,
        "p90_slippage_bps": p90_slip,
        "median_spread_bps": _bps("median_spread_bps"),
        "median_slippage_bps": _bps("median_slippage_bps"),
        "total_cost_bps": round(total_bps, 3),
        "last_updated_at": (
            last_updated.isoformat() if hasattr(last_updated, "isoformat") else last_updated
        ),
    }


def _entry_price_adjusted_alert(alert: BreakoutAlert, entry_price: float) -> Any:
    """Lightweight alert view for re-checking edge at the actual entry quote."""
    return SimpleNamespace(
        id=getattr(alert, "id", None),
        ticker=getattr(alert, "ticker", None),
        asset_type=getattr(alert, "asset_type", None),
        alert_tier=getattr(alert, "alert_tier", None),
        scan_pattern_id=getattr(alert, "scan_pattern_id", None),
        score_at_alert=getattr(alert, "score_at_alert", None),
        price_at_alert=getattr(alert, "price_at_alert", None),
        entry_price=entry_price,
        stop_loss=getattr(alert, "stop_loss", None),
        target_price=getattr(alert, "target_price", None),
        user_id=getattr(alert, "user_id", None),
    )


def _favorable_entry_drift_limit_pct(settings: Any, base_slippage_pct: float) -> float:
    multiple = _settings_float(
        settings,
        "chili_autotrader_favorable_entry_drift_slippage_multiple",
        AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_SLIPPAGE_MULTIPLE,
        minimum=AUTOTRADER_FAVORABLE_ENTRY_DRIFT_MIN_SLIPPAGE_MULTIPLE,
        maximum=AUTOTRADER_FAVORABLE_ENTRY_DRIFT_MAX_SLIPPAGE_MULTIPLE,
    )
    cap_pct = _settings_float(
        settings,
        "chili_autotrader_favorable_entry_drift_max_pct",
        AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_MAX_PCT,
        minimum=0.0,
    )
    if cap_pct <= 0.0:
        return float(base_slippage_pct)
    return max(
        float(base_slippage_pct),
        min(cap_pct, float(base_slippage_pct) * multiple),
    )


def _favorable_entry_drift_enabled_for(settings: Any, asset_type: str) -> bool:
    if not _settings_bool(
        settings,
        "chili_autotrader_favorable_entry_drift_enabled",
        AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_ENABLED,
    ):
        return False
    allowed_assets = _settings_csv_set(
        settings,
        "chili_autotrader_favorable_entry_drift_asset_types",
        AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_ASSET_TYPES,
    )
    asset = str(asset_type or "stock").strip().lower()
    return "all" in allowed_assets or asset in allowed_assets


def _positive_reprice_entry_enabled_for(settings: Any, asset_type: str) -> bool:
    if not _settings_bool(
        settings,
        "chili_autotrader_positive_reprice_entry_enabled",
        AUTOTRADER_POSITIVE_REPRICE_DEFAULT_ENABLED,
    ):
        return False
    allowed_assets = _settings_csv_set(
        settings,
        "chili_autotrader_positive_reprice_entry_asset_types",
        AUTOTRADER_POSITIVE_REPRICE_DEFAULT_ASSET_TYPES,
    )
    asset = str(asset_type or "stock").strip().lower()
    return "all" in allowed_assets or asset in allowed_assets


def _slippage_reprice_cooldown_enabled_for(settings: Any, asset_type: str) -> bool:
    if not _settings_bool(
        settings,
        "chili_autotrader_slippage_reprice_cooldown_enabled",
        AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_ENABLED,
    ):
        return False
    allowed_assets = _settings_csv_set(
        settings,
        "chili_autotrader_slippage_reprice_cooldown_asset_types",
        AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_ASSET_TYPES,
    )
    asset = str(asset_type or "stock").strip().lower()
    return "all" in allowed_assets or asset in allowed_assets


def _non_positive_reprice_marker(snapshot: Any) -> bool:
    snap = snapshot if isinstance(snapshot, dict) else {}
    positive = snap.get("slippage_reprice_positive_edge")
    if positive is False:
        return True
    expected = _safe_float(snap.get("slippage_reprice_expected_net_pct"))
    if expected is not None and expected <= 0.0:
        return True
    reason = str(snap.get("slippage_reprice_edge_reason") or "").strip().lower()
    return reason == "non_positive_expected_edge"


def _slippage_reprice_cooldown_snapshot(
    db: Session | None,
    alert: BreakoutAlert,
    *,
    settings: Any,
    asset_type: str,
) -> dict[str, Any] | None:
    if db is None or not _slippage_reprice_cooldown_enabled_for(settings, asset_type):
        return None
    minutes = _settings_int(
        settings,
        "chili_autotrader_slippage_reprice_cooldown_minutes",
        AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_MINUTES,
        minimum=1,
    )
    threshold = _settings_int(
        settings,
        "chili_autotrader_slippage_reprice_cooldown_threshold",
        AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_THRESHOLD,
        minimum=1,
    )
    ticker = str(getattr(alert, "ticker", "") or "").strip()
    if not ticker:
        return None
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    try:
        q = (
            db.query(AutoTraderRun)
            .filter(AutoTraderRun.reason == "missed_entry_slippage")
            .filter(AutoTraderRun.ticker == ticker)
            .filter(AutoTraderRun.created_at >= cutoff)
        )
        pid = getattr(alert, "scan_pattern_id", None)
        if pid is not None:
            q = q.filter(AutoTraderRun.scan_pattern_id == int(pid))
        q = q.order_by(AutoTraderRun.created_at.desc()).limit(max(threshold * 3, threshold))
        rows = list(q.all() or [])
    except Exception:
        logger.debug("[autotrader] slippage cooldown lookup failed", exc_info=True)
        return None

    bad_rows = [row for row in rows if _non_positive_reprice_marker(getattr(row, "rule_snapshot", None))]
    if len(bad_rows) < threshold:
        return None
    latest = max((getattr(row, "created_at", None) for row in bad_rows), default=None)
    until = latest + timedelta(minutes=minutes) if isinstance(latest, datetime) else None
    return {
        "slippage_reprice_cooldown_active": True,
        "slippage_reprice_cooldown_count": len(bad_rows),
        "slippage_reprice_cooldown_threshold": threshold,
        "slippage_reprice_cooldown_minutes": minutes,
        "slippage_reprice_cooldown_until": until.isoformat() if until else None,
        "slippage_reprice_cooldown_reason": "repeated_non_positive_reprice_edge",
    }


def evaluate_entry_edge(
    db: Session,
    alert: BreakoutAlert,
    *,
    settings: Any,
    pat_ctx: dict[str, Any],
    confidence: float,
) -> EntryEdgeDecision:
    entry = alert.entry_price
    target = alert.target_price
    stop = alert.stop_loss
    ppp = projected_profit_pct(entry, target)
    snap: dict[str, Any] = {
        "method": "expected_net_edge_v1",
        "projected_profit_pct": ppp,
    }
    if entry is None or target is None:
        return EntryEdgeDecision(False, "missing_entry_or_target", snap)
    try:
        e = float(entry)
        t = float(target)
        s = float(stop) if stop is not None else 0.0
    except (TypeError, ValueError):
        return EntryEdgeDecision(False, "bad_entry_geometry", snap)
    if e <= 0 or t <= 0:
        return EntryEdgeDecision(False, "bad_entry_geometry", snap)
    if t <= e:
        snap["reward_fraction"] = 0.0
        return EntryEdgeDecision(False, "target_not_above_entry", snap)
    if s <= 0 or s >= e:
        snap["stop_loss_fraction"] = None
        return EntryEdgeDecision(False, "missing_or_invalid_stop_for_edge", snap)

    static_reward = (t - e) / e
    static_loss = (e - s) / e
    pattern = _load_scan_pattern_for_edge(db, alert.scan_pattern_id)
    reward, loss, realized_geometry = _realized_exit_geometry(
        pattern=pattern,
        static_reward=static_reward,
        static_loss=static_loss,
        settings=settings,
    )
    prob, prob_source, sample_n, probability_details = _pattern_probability(
        db,
        alert=alert,
        pat_ctx=pat_ctx,
        confidence=confidence,
        settings=settings,
        pattern=pattern,
        reward=reward,
        loss=loss,
    )
    cost_fraction, cost_snapshot = _empirical_entry_cost_fraction(
        db, ticker=alert.ticker, settings=settings,
    )

    edge_math = _expected_edge_components(
        probability=prob,
        reward=reward,
        loss=loss,
        cost_fraction=cost_fraction,
    )
    expected_reward = float(edge_math["expected_reward"] or 0.0)
    expected_loss = float(edge_math["expected_loss"] or 0.0)
    expected_net = float(edge_math["expected_net"] or 0.0)
    breakeven_probability = edge_math["breakeven_probability"]
    geometry_source = (
        REALIZED_DYNAMIC_GEOMETRY_SOURCE
        if realized_geometry.get("used")
        else STATIC_TARGET_STOP_GEOMETRY_SOURCE
    )
    snap.update(
        probability=round(prob, 6),
        probability_source=prob_source,
        probability_sample_n=sample_n,
        probability_details=probability_details,
        edge_geometry_source=geometry_source,
        dynamic_exit_geometry=realized_geometry,
        target_reward_fraction=round(static_reward, 8),
        hard_stop_loss_fraction=round(static_loss, 8),
        reward_fraction=round(reward, 8),
        stop_loss_fraction=round(loss, 8),
        expected_reward_fraction=round(expected_reward, 8),
        expected_loss_fraction=round(expected_loss, 8),
        empirical_cost_fraction=round(cost_fraction, 8),
        cost_fraction=round(cost_fraction, 8),
        empirical_cost=cost_snapshot,
        expected_net_fraction=round(expected_net, 8),
        expected_net_pct=round(expected_net * 100.0, 4),
        breakeven_probability=(
            round(float(breakeven_probability), 6)
            if breakeven_probability is not None
            else None
        ),
        probability_edge=(
            round(float(edge_math["probability_edge"]), 6)
            if breakeven_probability is not None
            else None
        ),
        reward_risk=(
            round(float(edge_math["reward_risk"]), 6)
            if edge_math["reward_risk"] is not None
            else None
        ),
    )
    full_bracket_edge = {
        "probability": snap.get("probability"),
        "probability_source": snap.get("probability_source"),
        "probability_sample_n": snap.get("probability_sample_n"),
        "probability_details": snap.get("probability_details"),
        "edge_geometry_source": snap.get("edge_geometry_source"),
        "reward_fraction": snap.get("reward_fraction"),
        "stop_loss_fraction": snap.get("stop_loss_fraction"),
        "expected_net_fraction": snap.get("expected_net_fraction"),
        "expected_net_pct": snap.get("expected_net_pct"),
        "reward_risk": snap.get("reward_risk"),
    }

    managed_reward, managed_loss, managed_geometry = _managed_exit_geometry_from_directional(
        alert=alert,
        entry_price=e,
        static_reward=static_reward,
        base_reward=reward,
        base_loss=loss,
        directional=(
            probability_details.get("directional_evidence")
            if isinstance(probability_details, dict)
            else None
        ),
        settings=settings,
    )
    managed_edge: dict[str, Any] = {
        "selected": False,
        "geometry": managed_geometry,
    }
    if managed_geometry.get("used"):
        managed_prob, managed_source, managed_sample_n, managed_details = _pattern_probability(
            db,
            alert=alert,
            pat_ctx=pat_ctx,
            confidence=confidence,
            settings=settings,
            pattern=pattern,
            reward=managed_reward,
            loss=managed_loss,
        )
        managed_math = _expected_edge_components(
            probability=managed_prob,
            reward=managed_reward,
            loss=managed_loss,
            cost_fraction=cost_fraction,
        )
        managed_expected_reward = float(managed_math["expected_reward"] or 0.0)
        managed_expected_loss = float(managed_math["expected_loss"] or 0.0)
        managed_expected_net = float(managed_math["expected_net"] or 0.0)
        managed_breakeven = managed_math["breakeven_probability"]
        min_managed_net = (
            _settings_float(
                settings,
                "chili_autotrader_managed_edge_min_expected_net_pct",
                AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_EXPECTED_NET_PCT,
            )
            / 100.0
        )
        managed_edge.update(
            probability=round(managed_prob, 6),
            probability_source=managed_source,
            probability_sample_n=managed_sample_n,
            probability_details=managed_details,
            reward_fraction=round(managed_reward, 8),
            stop_loss_fraction=round(managed_loss, 8),
            expected_reward_fraction=round(managed_expected_reward, 8),
            expected_loss_fraction=round(managed_expected_loss, 8),
            expected_net_fraction=round(managed_expected_net, 8),
            expected_net_pct=round(managed_expected_net * 100.0, 4),
            min_expected_net_pct=round(min_managed_net * 100.0, 4),
            breakeven_probability=(
                round(float(managed_breakeven), 6)
                if managed_breakeven is not None
                else None
            ),
            probability_edge=(
                round(float(managed_math["probability_edge"]), 6)
                if managed_breakeven is not None
                else None
            ),
            reward_risk=(
                round(float(managed_math["reward_risk"]), 6)
                if managed_math["reward_risk"] is not None
                else None
            ),
        )
        managed_mode = str(managed_geometry.get("mode") or "").strip().lower()
        managed_selectable = (
            managed_mode == MANAGED_EDGE_MODE_AUTHORITATIVE
            and managed_expected_net > expected_net
            and managed_expected_net > min_managed_net
        )
        if managed_selectable:
            managed_edge["selected"] = True
            managed_geometry["selected"] = True
            snap["full_bracket_edge"] = full_bracket_edge
            snap.update(
                probability=round(managed_prob, 6),
                probability_source=managed_source,
                probability_sample_n=managed_sample_n,
                probability_details=managed_details,
                edge_geometry_source=MANAGED_EDGE_GEOMETRY_SOURCE,
                reward_fraction=round(managed_reward, 8),
                stop_loss_fraction=round(managed_loss, 8),
                expected_reward_fraction=round(managed_expected_reward, 8),
                expected_loss_fraction=round(managed_expected_loss, 8),
                expected_net_fraction=round(managed_expected_net, 8),
                expected_net_pct=round(managed_expected_net * 100.0, 4),
                breakeven_probability=(
                    round(float(managed_breakeven), 6)
                    if managed_breakeven is not None
                    else None
                ),
                probability_edge=(
                    round(float(managed_math["probability_edge"]), 6)
                    if managed_breakeven is not None
                    else None
                ),
                reward_risk=(
                    round(float(managed_math["reward_risk"]), 6)
                    if managed_math["reward_risk"] is not None
                    else None
                ),
            )
            expected_net = managed_expected_net
        else:
            managed_edge["selection_reason"] = (
                "mode_not_authoritative"
                if managed_mode != MANAGED_EDGE_MODE_AUTHORITATIVE
                else (
                    "not_better_than_full_bracket"
                    if managed_expected_net <= expected_net
                    else "non_positive_managed_expected_edge"
                )
            )
    snap["managed_exit_edge"] = managed_edge
    execution_loss = (
        float(snap.get("stop_loss_fraction") or 0.0)
        if snap.get("edge_geometry_source") == MANAGED_EDGE_GEOMETRY_SOURCE
        else static_loss
    )
    max_execution_loss = _max_execution_stop_loss_fraction(
        settings,
        getattr(alert, "asset_type", None),
    )
    if max_execution_loss is not None and execution_loss > max_execution_loss:
        snap.update(
            execution_stop_loss_fraction=round(float(execution_loss), 8),
            max_execution_stop_loss_fraction=round(float(max_execution_loss), 8),
            max_execution_stop_loss_pct=round(float(max_execution_loss) * 100.0, 4),
            execution_stop_loss_source=(
                MANAGED_EDGE_GEOMETRY_SOURCE
                if snap.get("edge_geometry_source") == MANAGED_EDGE_GEOMETRY_SOURCE
                else STATIC_TARGET_STOP_GEOMETRY_SOURCE
            ),
            entry_price=round(float(e), 8),
            stop_price=round(float(s), 8),
            target_price=round(float(t), 8),
        )
        return EntryEdgeDecision(False, "execution_stop_loss_too_wide", snap)
    if expected_net <= 0.0:
        return EntryEdgeDecision(False, "non_positive_expected_edge", snap)
    return EntryEdgeDecision(True, "positive_expected_edge", snap)


def passes_rule_gate(
    db: Session,
    alert: BreakoutAlert,
    *,
    settings: Any,
    ctx: RuleGateContext,
    for_new_entry: bool,
    fallback_user_id: Optional[int] = None,
) -> Tuple[bool, str, dict[str, Any]]:
    """Return (ok, reason, snapshot_dict).

    When *for_new_entry* is True, enforces check_new_trade_allowed and max concurrent.
    When False (scale-in path), caller should enforce synergy / notional separately.

    *fallback_user_id* is used for ``portfolio_risk`` checks when the alert is
    system-scope (``alert.user_id is None`` — pattern_imminent alerts are written
    this way by the imminent scanner in single-tenant mode). The autotrader
    resolves its owning user from settings; pass it here so the rule gate can
    attribute the portfolio check to the right account.
    """
    # Phase D: snapshot the autotrader settings once per gate call. Every
    # field below references ``gs.<name>`` instead of getattr; defaults
    # live in RuleGateSettings.from_settings so a typo at a single callsite
    # can no longer silently change the threshold.
    gs = RuleGateSettings.from_settings(settings)

    snap: dict[str, Any] = {
        "ticker": alert.ticker,
        "alert_id": alert.id,
        "for_new_entry": for_new_entry,
    }

    # Task KK — crypto trades 24/7 with no PDT. When the operator has
    # opted into the crypto path, alerts with asset_type='crypto' bypass
    # the US-equity session gate entirely. Equity alerts still go through
    # the existing RTH / extended-hours check unchanged.
    asset_type_l = (alert.asset_type or "").lower()
    is_crypto_alert = asset_type_l == "crypto"
    crypto_path = bool(gs.crypto_enabled) and is_crypto_alert
    # Task MM Phase 2 — options path. Operator-driven via the manual
    # entry endpoint that creates the alert; the option metadata
    # (strike/expiration/type) lives in alert.indicator_snapshot.option_meta.
    is_options_alert = asset_type_l == "options"
    options_path = bool(gs.options_enabled) and is_options_alert
    snap["asset_type"] = asset_type_l
    snap["crypto_path"] = crypto_path
    snap["options_path"] = options_path

    if gs.rth_only and not crypto_path and not options_path:
        from .pattern_imminent_alerts import (
            us_stock_extended_session_open,
            us_stock_session_open,
        )

        allow_ext = gs.allow_extended_hours
        session_open = (
            us_stock_extended_session_open() if allow_ext else us_stock_session_open()
        )
        if not session_open:
            return False, (
                "outside_extended_hours" if allow_ext else "outside_rth"
            ), snap

    if asset_type_l != "stock" and not crypto_path and not options_path:
        # Crypto alert without flag, options alert without flag, or some
        # asset class we don't support (forex). Same audit reason for
        # historical continuity; the snapshot's asset_type and *_path
        # fields tell the operator which flag would unblock.
        return False, "not_stock", snap

    # For options, the operator-driven entry has already chosen a strike,
    # expiration, qty, and limit price. Validate that the alert carries
    # the required option metadata here; after the confidence gate the
    # dedicated options entry-quality model handles payoff/EV in the
    # correct price domain.
    if options_path:
        from .options.contracts import (
            normalize_option_meta,
            option_price_domains_snapshot,
            validate_single_leg_option_meta,
        )

        snap_meta = alert.indicator_snapshot if isinstance(alert.indicator_snapshot, dict) else {}
        opt_meta = snap_meta.get("option_meta") or {}
        if not isinstance(opt_meta, dict):
            return False, "options_meta_missing:option_meta", snap
        # Phase 4 — accept either single-leg metadata or a multi-leg
        # ``legs`` list. Single-leg requires (strike, expiration,
        # option_type); multi-leg requires `legs` to be a non-empty
        # list of dicts each with (strike, expiration, option_type,
        # action). The autotrader's _execute_broker_buy will branch
        # on the presence of `legs` to call place_spread vs
        # place_option_buy.
        legs = opt_meta.get("legs")
        if isinstance(legs, list) and len(legs) >= 2:
            normalized_legs: list[dict[str, Any]] = []
            for i, leg in enumerate(legs):
                miss = [k for k in ("strike", "expiration", "option_type", "action")
                        if not (isinstance(leg, dict) and leg.get(k))]
                if miss:
                    return False, f"options_meta_leg_{i}_missing:{','.join(miss)}", snap
                normalized_legs.append(
                    normalize_option_meta(
                        leg,
                        underlying=getattr(alert, "ticker", None),
                        current_underlying_price=getattr(ctx, "current_price", None),
                    )
                )
            opt_meta = dict(opt_meta)
            opt_meta["legs"] = normalized_legs
        else:
            opt_meta = normalize_option_meta(
                opt_meta,
                underlying=getattr(alert, "ticker", None),
                current_underlying_price=getattr(ctx, "current_price", None),
            )
            missing = validate_single_leg_option_meta(opt_meta)
            if missing:
                return False, f"options_meta_missing:{','.join(missing)}", snap
        snap["option_meta"] = opt_meta
        snap["option_contract_key"] = opt_meta.get("contract_key")
        snap["price_domains"] = option_price_domains_snapshot()

    # Phase 3: pull learned per-pattern signal quality from the M.1 ledger.
    # When the pattern has confident cells we can derive confidence and
    # probability evidence from history instead of static entry thresholds.
    pat_ctx = resolve_pattern_signal_context(db, pattern_id=alert.scan_pattern_id)
    snap["pattern_signal"] = pat_ctx

    conf = alert_confidence_from_score(alert)
    snap["confidence"] = conf

    # Q2 Task H — confidence_floor sourced through the StrategyParameter
    # registry when available, falling back to the operator's env setting.
    # Registers (idempotent) on first call with the env value as initial.
    # When chili_strategy_parameter_learning_enabled is True, the learning
    # pass adapts this value from realized outcomes; reads always work
    # regardless of the flag state.
    env_floor = gs.confidence_floor
    try:
        from .strategy_parameter import (
            ParameterSpec, get_parameter, register_parameter,
        )
        register_parameter(
            db,
            ParameterSpec(
                strategy_family="autotrader",
                parameter_key="confidence_floor",
                initial_value=float(env_floor),
                min_value=0.40,
                max_value=0.95,
                description=(
                    "Minimum signal confidence to allow a new entry. Adapts "
                    "from realized hit-rate outcomes when the learning flag "
                    "is on."
                ),
            ),
        )
        adaptive_floor = get_parameter(
            db,
            "autotrader",
            "confidence_floor",
            default=float(env_floor),
        )
        if adaptive_floor is not None and adaptive_floor != env_floor:
            snap["confidence_floor_adaptive"] = round(float(adaptive_floor), 4)
            snap["confidence_floor_env"] = round(float(env_floor), 4)
            env_floor = float(adaptive_floor)
    except Exception:
        # Read path is best-effort; never raise into the gate decision.
        pass

    # Learned floor (per-pattern, from M.1 ledger): 85% of historical
    # hit_rate, clamped to [0.55, env_floor]. Clamping to env_floor as
    # an upper bound means the brain can LOWER the floor below env (when a
    # pattern is genuinely strong) but never raise it above what the
    # operator configured — keeps the operator in charge of the outer
    # envelope.
    if pat_ctx.get("hit_rate") is not None:
        learned_floor = max(
            CONFIDENCE_ABSOLUTE_FLOOR,
            min(env_floor, float(pat_ctx["hit_rate"]) * CONFIDENCE_LEARNING_FACTOR),
        )
        floor = learned_floor
        snap["confidence_floor_source"] = "pattern_hit_rate"
    else:
        floor = env_floor
        snap["confidence_floor_source"] = (
            "strategy_parameter_adaptive"
            if "confidence_floor_adaptive" in snap
            else "env_default"
        )
    snap["confidence_floor_effective"] = round(floor, 4)
    if conf < floor:
        return False, "confidence_below_floor", snap

    entry = alert.entry_price
    target = alert.target_price
    px = float(ctx.current_price)
    snap["current_price"] = px

    if options_path:
        from .options.entry_quality import evaluate_long_option_entry

        option_entry = evaluate_long_option_entry(
            db,
            alert=alert,
            option_meta=opt_meta,
            current_underlying_price=px,
            confidence=conf,
            settings=gs,
        )
        snap["projected_profit_pct"] = None
        snap["projected_profit_pct_source"] = "options_entry_quality"
        snap["min_profit_source"] = "not_applicable_options"
        snap["min_profit_pct_effective"] = None
        snap["option_entry_quality"] = option_entry.snapshot
        if not option_entry.accepted:
            return False, f"options_entry_quality:{option_entry.reason}", snap
        try:
            from .options.contracts import missing_greeks
            from .options.portfolio_budget import (
                check_proposal_against_budget,
                options_budget_bypass_enabled,
                single_leg_proposal_from_option_meta,
            )

            missing = missing_greeks(opt_meta)
            if missing:
                reasons = ["missing_complete_greeks:" + ",".join(missing)]
                if options_budget_bypass_enabled():
                    snap["options_budget_check"] = {
                        "ok": True,
                        "reasons": [
                            "BYPASS_VIA_CHILI_OPTIONS_BUDGET_BYPASS",
                            *reasons,
                        ],
                    }
                else:
                    snap["options_budget_check"] = {
                        "ok": False,
                        "reasons": reasons,
                    }
                    return False, "options_budget:missing_complete_greeks", snap
            elif db is not None:
                proposal = single_leg_proposal_from_option_meta(
                    opt_meta,
                    confidence=conf,
                )
                budget_check = check_proposal_against_budget(
                    db,
                    alert.user_id if alert.user_id is not None else fallback_user_id,
                    proposal,
                )
                snap["options_budget_check"] = {
                    "ok": budget_check.accepted,
                    "reasons": budget_check.reasons,
                    "current_portfolio": budget_check.current_portfolio,
                    "after_proposal": budget_check.after_proposal,
                    "budget": budget_check.budget,
                }
                if not budget_check.accepted:
                    return False, "options_budget:" + ",".join(budget_check.reasons), snap
            else:
                snap["options_budget_check"] = {
                    "ok": None,
                    "reason": "no_db",
                }
        except Exception as exc:
            reason = f"budget_error:{type(exc).__name__}"
            try:
                from .options.portfolio_budget import (
                    options_budget_bypass_enabled as _options_budget_bypass_enabled,
                )
                bypass = _options_budget_bypass_enabled()
            except Exception:
                bypass = False
            snap["options_budget_check"] = {
                "ok": True if bypass else False,
                "reasons": (
                    ["BYPASS_VIA_CHILI_OPTIONS_BUDGET_BYPASS", reason]
                    if bypass else [reason]
                ),
                "reason": "error",
                "error": type(exc).__name__,
            }
            if not bypass:
                return False, f"options_budget:{reason}", snap
    else:
        ppp = projected_profit_pct(entry, target)
        snap["projected_profit_pct"] = ppp
        snap["projected_profit_pct_source"] = "stock_entry_target"
        edge_decision = evaluate_entry_edge(
            db,
            alert,
            settings=settings,
            pat_ctx=pat_ctx,
            confidence=conf,
        )
        snap["entry_edge"] = edge_decision.snapshot
        snap["entry_edge_reason"] = edge_decision.reason
        snap["entry_edge_expected_net_pct"] = edge_decision.snapshot.get(
            "expected_net_pct"
        )
        if not edge_decision.allowed:
            return False, edge_decision.reason, snap
        snap["min_profit_source"] = "expected_net_edge"
        snap["min_profit_pct_effective"] = None

    ref = float(entry) if entry is not None else float(alert.price_at_alert or 0)
    if ref <= 0:
        return False, "bad_reference_price", snap

    max_px = gs.max_symbol_price_usd
    fractional_equity_enabled = bool(gs.fractional_equity_enabled)
    snap["max_symbol_price_usd"] = max_px
    snap["fractional_equity_enabled"] = fractional_equity_enabled
    # Crypto and options never use the legacy stock share-price cap. Crypto
    # bases are often high-priced assets, and the option path sees underlying
    # spot here rather than the option premium.
    # When fractional equity is enabled, this legacy cap is informational only:
    # risk notional and fractional quantity normalization own stock sizing.
    if not crypto_path and not options_path and fractional_equity_enabled and px > max_px:
        snap["symbol_price_cap_skipped_reason"] = "fractional_equity_enabled"
    if not crypto_path and not options_path and not fractional_equity_enabled and px > max_px:
        return False, "symbol_price_above_cap", snap

    # WW — for options, ``ref`` is the option PREMIUM (e.g. $4.01) but
    # ``px = ctx.current_price`` is the UNDERLYING price (e.g. SPY at
    # $714). Computing ``abs(px - ref) / ref`` here gives nonsensical
    # ~17,000% "slippage" that blocks every option entry. The Phase 2
    # comment at the options_path validation block above explicitly
    # called for short-circuiting the equity-shape gates; this is the
    # missing implementation. Operator-driven option entries already
    # encode their own limit price + sizing — the broker's limit order
    # will simply not fill if the market moves past it, so there's
    # nothing for this gate to add.
    uid_for_slip = alert.user_id if alert.user_id is not None else fallback_user_id
    slip_pct, slip_source = resolve_effective_slippage_pct(db, user_id=uid_for_slip, settings=settings)
    snap["slippage_tolerance_pct"] = round(slip_pct, 4)
    snap["slippage_source"] = slip_source
    if not options_path:
        signed_slip = (px - ref) / ref * 100.0
        slip = abs(signed_slip)
        snap["entry_slippage_pct"] = round(slip, 4)
        snap["entry_slippage_signed_pct"] = round(signed_slip, 4)
        if signed_slip < 0.0:
            snap["entry_slippage_direction"] = "favorable"
        elif signed_slip > 0.0:
            snap["entry_slippage_direction"] = "adverse"
        else:
            snap["entry_slippage_direction"] = "flat"
        if slip > slip_pct:
            favorable_limit = _favorable_entry_drift_limit_pct(settings, slip_pct)
            favorable_enabled = (
                signed_slip < 0.0
                and not crypto_path
                and _favorable_entry_drift_enabled_for(
                    settings,
                    alert.asset_type or "stock",
                )
            )
            snap["favorable_entry_drift_enabled"] = favorable_enabled
            snap["favorable_entry_drift_max_pct"] = round(favorable_limit, 4)
            if favorable_enabled and slip <= favorable_limit:
                adjusted_alert = _entry_price_adjusted_alert(alert, px)
                adjusted_edge = evaluate_entry_edge(
                    db,
                    adjusted_alert,
                    settings=settings,
                    pat_ctx=pat_ctx,
                    confidence=conf,
                )
                snap["favorable_entry_drift_edge"] = adjusted_edge.snapshot
                snap["favorable_entry_drift_edge_reason"] = adjusted_edge.reason
                snap["favorable_entry_drift_original_entry_price"] = round(ref, 8)
                snap["favorable_entry_drift_rechecked_entry_price"] = round(px, 8)
                if adjusted_edge.allowed:
                    snap["entry_edge"] = adjusted_edge.snapshot
                    snap["entry_edge_reason"] = adjusted_edge.reason
                    snap["entry_edge_expected_net_pct"] = adjusted_edge.snapshot.get(
                        "expected_net_pct"
                    )
                    snap["entry_reference_price_adjusted"] = True
                    ref = px
                else:
                    return False, "missed_entry_slippage", snap
            else:
                reprice_enabled = _positive_reprice_entry_enabled_for(
                    settings,
                    alert.asset_type or "stock",
                )
                snap["slippage_reprice_positive_edge_enabled"] = reprice_enabled
                snap["slippage_reprice_max_pct"] = round(favorable_limit, 4)
                cooldown = _slippage_reprice_cooldown_snapshot(
                    db,
                    alert,
                    settings=settings,
                    asset_type=alert.asset_type or "stock",
                )
                if cooldown:
                    snap.update(cooldown)
                    return False, "slippage_reprice_cooldown", snap
                try:
                    adjusted_alert = _entry_price_adjusted_alert(alert, px)
                    adjusted_edge = evaluate_entry_edge(
                        db,
                        adjusted_alert,
                        settings=settings,
                        pat_ctx=pat_ctx,
                        confidence=conf,
                    )
                    snap["slippage_reprice_edge"] = adjusted_edge.snapshot
                    snap["slippage_reprice_edge_reason"] = adjusted_edge.reason
                    snap["slippage_reprice_original_entry_price"] = round(ref, 8)
                    snap["slippage_reprice_current_price"] = round(px, 8)
                    snap["slippage_reprice_expected_net_pct"] = (
                        adjusted_edge.snapshot.get("expected_net_pct")
                    )
                    snap["slippage_reprice_positive_edge"] = bool(adjusted_edge.allowed)
                    if adjusted_edge.allowed and reprice_enabled and slip <= favorable_limit:
                        snap["entry_edge"] = adjusted_edge.snapshot
                        snap["entry_edge_reason"] = adjusted_edge.reason
                        snap["entry_edge_expected_net_pct"] = adjusted_edge.snapshot.get(
                            "expected_net_pct"
                        )
                        snap["entry_reference_price_adjusted"] = True
                        snap["slippage_reprice_accepted"] = True
                        ref = px
                    else:
                        return False, "missed_entry_slippage", snap
                except Exception as exc:
                    snap["slippage_reprice_error"] = type(exc).__name__
                    return False, "missed_entry_slippage", snap
    else:
        snap["entry_slippage_pct"] = None
        snap["slippage_skipped_reason"] = "options_path"

    # Long viability: stock alerts express stop/target in the same price
    # domain as entry/current price. Options substitutions keep the
    # underlying stop/target from the source alert while ref is the option
    # premium, so validating those against each other blocks every option.
    # Premium exits are handled by the options exit monitor.
    if options_path:
        snap["stop_target_validation_skipped_reason"] = "options_underlying_levels"
    else:
        if alert.stop_loss is not None:
            sl = float(alert.stop_loss)
            if sl >= ref or sl >= px:
                return False, "stop_not_below_entry", snap
        if target is not None and float(target) <= ref:
            return False, "target_not_above_entry", snap

    if bool(getattr(alert, "_chili_shadow_observation_only", False)):
        snap["shadow_observation_risk_authority_skipped"] = True
        snap["shadow_observation_risk_authority_skip_reason"] = (
            "shadow_observation_only"
        )
        snap["daily_loss_cap_source"] = "shadow_observation_not_live"
        snap["portfolio_check"] = {
            "ok": None,
            "reason": "shadow_observation_only",
        }
        return True, "ok", snap

    # Phase 2: pull brain-driven risk context (regime + dial + drawdown).
    # dial_value = 1.0 is baseline. risk_off tightens it (lower notional,
    # fewer concurrent); risk_on loosens it up to the configured ceiling.
    uid_for_brain = alert.user_id if alert.user_id is not None else fallback_user_id
    brain_ctx = resolve_brain_risk_context(
        db, user_id=uid_for_brain, settings_override=settings,
    )
    snap["brain_context"] = brain_ctx
    dial = float(brain_ctx.get("dial_value", 1.0))

    # Daily loss cap: prefer a percent-of-equity cap only when equity is
    # proven. Assumed fallback capital is telemetry, not loss authority.
    cap_pct = gs.daily_loss_cap_pct  # 1.5% of equity default
    equity_for_cap, equity_cap_source = resolve_effective_capital(db, settings)
    snap["daily_loss_cap_capital_source"] = equity_cap_source
    equity_cap_proven = (
        equity_for_cap > 0
        and cap_pct > 0
        and not str(equity_cap_source or "").startswith("fallback:")
    )
    if equity_cap_proven:
        cap_loss = equity_for_cap * (cap_pct / 100.0) * dial
        snap["daily_loss_cap_source"] = "equity_pct_dial"
    else:
        cap_loss = gs.daily_loss_cap_usd * dial
        snap["daily_loss_cap_source"] = "env_dollar_dial"
        if equity_for_cap > 0 and cap_pct > 0:
            snap["daily_loss_cap_unproven_equity_usd"] = round(equity_for_cap, 2)
    snap["daily_loss_cap_usd"] = round(cap_loss, 2)
    snap["realized_loss_today_usd"] = ctx.realized_loss_today_usd
    if cap_loss > 0 and ctx.realized_loss_today_usd <= -cap_loss:
        return False, "daily_loss_cap_already_hit", snap

    if for_new_entry:
        # VV — per-lane concurrency caps. Each lane (equity / crypto / options)
        # has its own cap registered in the StrategyParameter ledger so the
        # brain can adapt them from realized outcomes. The legacy global
        # cap (gs.max_concurrent) acts as an outer-safety ceiling on the
        # *sum* of all lanes.
        if crypto_path:
            lane = "crypto"
            base_lane_cap = gs.max_concurrent_crypto
        elif options_path:
            lane = "options"
            base_lane_cap = gs.max_concurrent_options
        else:
            lane = "equity"
            base_lane_cap = gs.max_concurrent_equity

        # Resolve the lane cap from StrategyParameter (registers idempotently
        # on first call). Falls back to the env-bootstrapped value above if
        # the ledger is unavailable.
        try:
            from .strategy_parameter import (
                ParameterSpec, get_parameter, register_parameter,
            )
            register_parameter(
                db,
                ParameterSpec(
                    strategy_family="autotrader_concurrency",
                    parameter_key=f"max_concurrent_{lane}",
                    initial_value=float(base_lane_cap),
                    min_value=1.0,
                    max_value=200.0,
                    param_type="int",
                    description=(
                        f"Max simultaneous autotrader-v1 open positions in the "
                        f"{lane} lane. Adapts from realized outcomes when the "
                        f"learner is enabled."
                    ),
                ),
            )
            learned = get_parameter(
                db,
                strategy_family="autotrader_concurrency",
                parameter_key=f"max_concurrent_{lane}",
                default=float(base_lane_cap),
            )
            base_lane_cap_eff = int(round(float(learned))) if learned is not None else base_lane_cap
        except Exception:
            base_lane_cap_eff = base_lane_cap

        # Dial-scaled cap. Floor at 1 so a deeply defensive dial still
        # allows one probe position per lane instead of fully muting it.
        lane_cap = max(1, int(round(base_lane_cap_eff * dial)))

        # Per-lane open count. Falls back to the legacy single counter when
        # the caller didn't supply a per-lane breakdown (e.g. older test
        # paths) — preserves equality with the prior single-cap behavior.
        if ctx.autotrader_open_count_by_lane is not None:
            lane_open = int(ctx.autotrader_open_count_by_lane.get(lane, 0))
        else:
            lane_open = int(ctx.autotrader_open_count)

        snap["concurrency_lane"] = lane
        snap["max_concurrent_lane"] = lane_cap
        snap["max_concurrent_lane_base"] = base_lane_cap_eff
        snap["max_concurrent_lane_env"] = base_lane_cap
        snap["autotrader_open_count_lane"] = lane_open
        snap["autotrader_open_count"] = ctx.autotrader_open_count
        if lane_open >= lane_cap:
            return False, f"max_concurrent_{lane}", snap

        # Outer-safety ceiling — the sum across all lanes. Default 60 from
        # 3 × 20; bumped via env if the operator wants a different ceiling.
        # This only fires if a lane miscount or a flood of unattributed
        # trades pushes the global total past the outer cap.
        global_cap_base = gs.max_concurrent
        global_cap = max(1, int(round(global_cap_base * dial)))
        snap["max_concurrent_global"] = global_cap
        if ctx.autotrader_open_count >= global_cap:
            return False, "max_concurrent_global", snap

        uid = alert.user_id if alert.user_id is not None else fallback_user_id
        if uid is None:
            return False, "missing_user_id_on_alert", snap

        from .portfolio_risk import check_new_trade_allowed

        cap, cap_source = resolve_effective_capital(db, settings)
        snap["capital_usd"] = round(cap, 2)
        snap["capital_source"] = cap_source
        capital_source_is_fallback = str(cap_source or "").startswith("fallback:")
        snap["capital_proven"] = not capital_source_is_fallback
        if capital_source_is_fallback:
            reason = f"capital_unavailable:{cap_source}"
            snap["portfolio_check"] = {"ok": False, "reason": reason}
            return False, reason, snap
        portfolio_asset_type = (
            "options" if options_path else ("crypto" if crypto_path else "stock")
        )
        ok, reason = check_new_trade_allowed(
            db,
            uid,
            alert.ticker.upper(),
            capital=cap,
            asset_type=portfolio_asset_type,
        )
        snap["portfolio_check"] = {"ok": ok, "reason": reason}
        snap["portfolio_asset_type"] = portfolio_asset_type
        if not ok:
            return False, f"portfolio_blocked:{reason}", snap

    return True, "ok", snap


def count_autotrader_v1_open(db: Session, user_id: Optional[int], *, paper_mode: bool = False) -> int:
    if paper_mode:
        q = db.query(PaperTrade).filter(PaperTrade.status == "open")
        if user_id is not None:
            q = q.filter(PaperTrade.user_id == user_id)
        n = 0
        for row in q.all():
            sj = row.signal_json or {}
            if sj.get("auto_trader_v1"):
                n += 1
        return n
    q = db.query(Trade).filter(
        Trade.auto_trader_version == "v1",
        Trade.status.in_(("open", "working")),
    )
    if user_id is not None:
        q = q.filter(Trade.user_id == user_id)
    return int(q.count())


# VV — per-lane open counts. JOINs trading_trades ↔ trading_breakout_alerts
# via related_alert_id. Prefer Trade.asset_kind because option substitution
# mutates the in-memory alert for execution but can leave the stored alert row
# as stock; fall back to alert.asset_type for legacy rows without asset_kind.
def count_autotrader_v1_open_by_lane(
    db: Session,
    user_id: Optional[int],
    *,
    paper_mode: bool = False,
) -> dict:
    """Return ``{'equity': int, 'crypto': int, 'options': int}``.

    Counts active AutoTrader-v1 trades per asset-class lane. Live rows include
    both filled positions (open) and acknowledged-but-not-yet-filled entries
    (working), because a resting entry order still consumes exposure budget.
    """
    out = {"equity": 0, "crypto": 0, "options": 0}
    try:
        if paper_mode:
            q = db.query(PaperTrade).filter(PaperTrade.status == "open")
            if user_id is not None:
                q = q.filter(PaperTrade.user_id == user_id)
            for row in q.all():
                sj = row.signal_json or {}
                if not sj.get("auto_trader_v1"):
                    continue
                # PaperTrade may not link an alert; rely on signal_json
                # markers when present. Falls through to 'equity'.
                lane = "equity"
                if sj.get("options_path") or sj.get("asset_type") == "options":
                    lane = "options"
                elif sj.get("crypto_path") or sj.get("asset_type") == "crypto":
                    lane = "crypto"
                out[lane] = out.get(lane, 0) + 1
            return out

        # Live trades — JOIN to BreakoutAlert via related_alert_id.
        from sqlalchemy import text as _text
        params = {}
        sql = (
            "SELECT COALESCE(LOWER(NULLIF(t.asset_kind, '')), "
            "              LOWER(NULLIF(a.asset_type, '')), 'stock') AS at, "
            "       COUNT(*) AS n "
            "FROM trading_trades t "
            "LEFT JOIN trading_breakout_alerts a ON a.id = t.related_alert_id "
            "WHERE t.auto_trader_version = 'v1' "
            "  AND t.status IN ('open', 'working') "
        )
        if user_id is not None:
            sql += " AND t.user_id = :uid"
            params["uid"] = user_id
        sql += " GROUP BY at"
        rows = db.execute(_text(sql), params).fetchall()
        for at, n in rows or []:
            at_l = (at or "stock").lower()
            if at_l == "crypto":
                out["crypto"] += int(n)
            elif at_l in ("option", "options"):
                out["options"] += int(n)
            else:
                # 'stock', NULL/empty, forex, anything unrecognized → equity bucket
                out["equity"] += int(n)
    except Exception as e:
        logger.debug("[autotrader] count_open_by_lane failed (returning zeros): %s", e)
    return out


def autotrader_paper_realized_pnl_today_et(db: Session, user_id: Optional[int]) -> float:
    """Sum paper autotrader realized P&L closed today (US/Eastern)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    end_et = start_et + timedelta(days=1)
    start_utc = start_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    q = db.query(PaperTrade).filter(
        PaperTrade.status == "closed",
        PaperTrade.exit_date.isnot(None),
        PaperTrade.exit_date >= start_utc,
        PaperTrade.exit_date < end_utc,
    )
    if user_id is not None:
        q = q.filter(PaperTrade.user_id == user_id)
    total = 0.0
    for row in q.all():
        sj = row.signal_json or {}
        if not sj.get("auto_trader_v1"):
            continue
        pnl = paper_trade_realized_pnl(row)
        if pnl is None:
            pnl = _finite_float(getattr(row, "pnl", None))
        if pnl is not None:
            total += pnl
    return total


def autotrader_realized_pnl_today_et(db: Session, user_id: Optional[int]) -> float:
    """Sum live autotrader realized P&L closed today (US/Eastern)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    end_et = start_et + timedelta(days=1)
    start_utc = start_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    q = (
        db.query(Trade)
        .filter(
            Trade.auto_trader_version == "v1",
            Trade.status == "closed",
            Trade.exit_date.isnot(None),
            Trade.exit_date >= start_utc,
            Trade.exit_date < end_utc,
        )
    )
    if user_id is not None:
        q = q.filter(Trade.user_id == user_id)
    rows = q.all()
    total = 0.0
    for t in rows:
        pnl = trade_realized_pnl(t)
        if pnl is None:
            pnl = _finite_float(getattr(t, "pnl", None))
        if pnl is not None:
            total += pnl
    return total


def breakout_alert_already_processed(db: Session, breakout_alert_id: int) -> bool:
    return (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == breakout_alert_id)
        .first()
        is not None
    )
