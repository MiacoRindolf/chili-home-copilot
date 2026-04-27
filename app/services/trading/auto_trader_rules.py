"""Pure rule gates for AutoTrader v1 (testable without DB side effects)."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from sqlalchemy.orm import Session

from ...models.trading import AutoTraderRun, BreakoutAlert, PaperTrade, Trade
from .ops_log_prefixes import CHILI_RISK_CACHE

logger = logging.getLogger(__name__)


# ── Learned-threshold constants (Phase D) ────────────────────────────
#
# These numeric factors translate a pattern's historical record from the
# M.1 regime-performance ledger into a per-alert threshold override. Each
# constant has an operator-configurable env counterpart that acts as an
# upper or lower guard — the operator stays in charge of the envelope;
# the brain can only tighten inside it (lower the confidence floor when
# the pattern is genuinely strong, or raise the minimum-profit floor to
# keep mean-reverters out of the book).
#
# The specific factors were calibrated during the M.1 cold-start rollout
# (2025-Q4). Changing any of them moves every live gate, so treat them
# as a configuration surface — add a regression test when you touch one.

# Confidence floor = env_floor, but replaceable by
# (pattern.hit_rate × CONFIDENCE_LEARNING_FACTOR) when a pattern has
# confident cells. 0.85 means "trust 85% of the observed win rate" —
# leaves ~15 percentage points of safety margin vs a freshly-promoted
# pattern over-claiming.
CONFIDENCE_LEARNING_FACTOR: float = 0.85

# Absolute confidence lower bound — even a learned pattern with a great
# hit rate can't drop the floor below this. Keeps the gate from
# silently trusting one lucky hot streak.
CONFIDENCE_ABSOLUTE_FLOOR: float = 0.55

# Minimum-projected-profit floor = env_min_pp, but replaceable by
# (pattern.expectancy × 100 × LEARNED_PROFIT_MULTIPLIER) when the
# pattern has confident expectancy. 0.7 means "require 70% of
# historical expected move" — conservative bias for live entries.
LEARNED_PROFIT_MULTIPLIER: float = 0.7

# Absolute floor on the minimum-projected-profit gate when the learned
# value would otherwise fall below this. 6.0% covers half a typical
# spread-and-slippage round trip on a $20-$100 ticker; anything below
# that is within noise.
MIN_PROFIT_PCT_FLOOR: float = 6.0


@dataclass
class RuleGateContext:
    """Inputs needed for rule evaluation (caller supplies quote + settings snapshot)."""

    current_price: float
    autotrader_open_count: int
    realized_loss_today_usd: float  # negative sum of closed autotrader PnL today (0 if none)


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
    min_projected_profit_pct: float = 12.0
    max_symbol_price_usd: float = 50.0
    max_entry_slippage_pct: float = 1.0

    # Daily loss caps (percent-of-equity preferred; dollar is fallback)
    daily_loss_cap_pct: float = 1.5
    daily_loss_cap_usd: float = 150.0

    # Concurrency
    max_concurrent: int = 3

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
            max_entry_slippage_pct=float(
                g("chili_autotrader_max_entry_slippage_pct", cls.max_entry_slippage_pct)
            ),
            daily_loss_cap_pct=float(g("chili_autotrader_daily_loss_cap_pct", cls.daily_loss_cap_pct)),
            daily_loss_cap_usd=float(g("chili_autotrader_daily_loss_cap_usd", cls.daily_loss_cap_usd)),
            max_concurrent=int(g("chili_autotrader_max_concurrent", cls.max_concurrent)),
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


def _fetch_broker_equity_once(
    fallback: float,
) -> tuple[float, str]:
    """Single uncached broker-equity lookup. Returns (equity, source)."""
    try:
        from .. import broker_service
        if not broker_service.is_connected():
            return fallback, "fallback:broker_disconnected"
        portfolio = broker_service.get_portfolio()
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

        cap, _ = resolve_effective_capital(db, _get_settings())
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
                    real_5d += float(t.pnl or 0.0)
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

    Phase 3: replaces the hardcoded ``confidence_floor=0.7`` and
    ``min_projected_profit_pct=12.0`` static thresholds with values pulled
    from the brain's M.1 pattern-regime performance ledger. Each pattern's
    confident cells across the 8 regime dimensions are averaged to give a
    single signal-quality snapshot.

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

        out.update(
            hit_rate=round(hit_rate, 4) if hit_rate is not None else None,
            expectancy=round(expectancy, 4) if expectancy is not None else None,
            profit_factor=round(profit_factor, 4) if profit_factor is not None else None,
            n_cells=len(cells),
            n_trades_sum=n_trades,
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
    # expiration, qty, and limit price. The equity-shaped gates below
    # (confidence floor, projected profit %, slippage on equity-spread,
    # symbol price cap) don't apply cleanly. Validate that the alert
    # carries the required option metadata, then short-circuit through
    # the kill-switch / drawdown / concurrent-limit checks the same way
    # crypto does, and return ok.
    if options_path:
        snap_meta = alert.indicator_snapshot if isinstance(alert.indicator_snapshot, dict) else {}
        opt_meta = snap_meta.get("option_meta") or {}
        # Phase 4 — accept either single-leg metadata or a multi-leg
        # ``legs`` list. Single-leg requires (strike, expiration,
        # option_type); multi-leg requires `legs` to be a non-empty
        # list of dicts each with (strike, expiration, option_type,
        # action). The autotrader's _execute_broker_buy will branch
        # on the presence of `legs` to call place_spread vs
        # place_option_buy.
        legs = opt_meta.get("legs")
        if isinstance(legs, list) and len(legs) >= 2:
            for i, leg in enumerate(legs):
                miss = [k for k in ("strike", "expiration", "option_type", "action")
                        if not (isinstance(leg, dict) and leg.get(k))]
                if miss:
                    return False, f"options_meta_leg_{i}_missing:{','.join(miss)}", snap
        else:
            required = ("strike", "expiration", "option_type")
            missing = [k for k in required if not opt_meta.get(k)]
            if missing:
                return False, f"options_meta_missing:{','.join(missing)}", snap
        snap["option_meta"] = opt_meta

    # Phase 3: pull learned per-pattern signal quality from the M.1 ledger.
    # When the pattern has confident cells we can derive confidence_floor and
    # min_projected_profit from history instead of using the static env values.
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
    ppp = projected_profit_pct(entry, target)
    snap["projected_profit_pct"] = ppp
    env_min_pp = gs.min_projected_profit_pct
    # Learned min profit: 70% of historical expectancy-pct, floored at 6% so a
    # mean-reversion pattern with a tiny expectancy doesn't trigger entries
    # that can't clear real spreads. Expectancy is signed — patterns with
    # non-positive expectancy keep the env floor.
    if pat_ctx.get("expectancy") is not None and float(pat_ctx["expectancy"]) > 0:
        learned_min_pp = max(
            MIN_PROFIT_PCT_FLOOR,
            float(pat_ctx["expectancy"]) * 100 * LEARNED_PROFIT_MULTIPLIER,
        )
        min_pp = min(env_min_pp, learned_min_pp)
        snap["min_profit_source"] = "pattern_expectancy"
    else:
        min_pp = env_min_pp
        snap["min_profit_source"] = "env_default"
    snap["min_profit_pct_effective"] = round(min_pp, 3)
    if ppp is None:
        return False, "missing_entry_or_target", snap
    if ppp < min_pp:
        return False, "projected_profit_below_min", snap

    ref = float(entry) if entry is not None else float(alert.price_at_alert or 0)
    if ref <= 0:
        return False, "bad_reference_price", snap

    px = float(ctx.current_price)
    snap["current_price"] = px
    max_px = gs.max_symbol_price_usd
    # Task KK — the equity max-symbol-price cap (default $50) was meant to
    # avoid sub-1-share fractional rounding traps on stocks like NVDA. It
    # makes no sense for crypto: BTC is routinely > $50k, and Robinhood
    # supports fractional crypto natively, so a literal price cap would
    # block every crypto alert by construction.
    # Task MM — same reasoning for options. The "symbol price" the gate
    # sees here is the underlying's price, not the option premium. A
    # call on AAPL at $180 spot is fine even though 180 > 50.
    if not crypto_path and not options_path and px > max_px:
        return False, "symbol_price_above_cap", snap

    uid_for_slip = alert.user_id if alert.user_id is not None else fallback_user_id
    slip_pct, slip_source = resolve_effective_slippage_pct(db, user_id=uid_for_slip, settings=settings)
    snap["slippage_tolerance_pct"] = round(slip_pct, 4)
    snap["slippage_source"] = slip_source
    slip = abs(px - ref) / ref * 100.0
    snap["entry_slippage_pct"] = round(slip, 4)
    if slip > slip_pct:
        return False, "missed_entry_slippage", snap

    # Long viability: stop below entry, target above entry
    if alert.stop_loss is not None:
        sl = float(alert.stop_loss)
        if sl >= ref or sl >= px:
            return False, "stop_not_below_entry", snap
    if target is not None and float(target) <= ref:
        return False, "target_not_above_entry", snap

    # Phase 2: pull brain-driven risk context (regime + dial + drawdown).
    # dial_value = 1.0 is baseline. risk_off tightens it (lower notional,
    # fewer concurrent); risk_on loosens it up to the configured ceiling.
    uid_for_brain = alert.user_id if alert.user_id is not None else fallback_user_id
    brain_ctx = resolve_brain_risk_context(db, user_id=uid_for_brain)
    snap["brain_context"] = brain_ctx
    dial = float(brain_ctx.get("dial_value", 1.0))

    # Daily loss cap: prefer a percent-of-equity cap (dial-scaled) over the
    # static dollar cap. Falls back to the env dollar cap when equity is
    # unavailable — preserves current behavior in degraded environments.
    cap_pct = gs.daily_loss_cap_pct  # 1.5% of equity default
    equity_for_cap, _ = resolve_effective_capital(db, settings)
    if equity_for_cap > 0 and cap_pct > 0:
        cap_loss = equity_for_cap * (cap_pct / 100.0) * dial
        snap["daily_loss_cap_source"] = "equity_pct_dial"
    else:
        cap_loss = gs.daily_loss_cap_usd * dial
        snap["daily_loss_cap_source"] = "env_dollar_dial"
    snap["daily_loss_cap_usd"] = round(cap_loss, 2)
    snap["realized_loss_today_usd"] = ctx.realized_loss_today_usd
    if cap_loss > 0 and ctx.realized_loss_today_usd <= -cap_loss:
        return False, "daily_loss_cap_already_hit", snap

    if for_new_entry:
        base_max_c = gs.max_concurrent
        # Dial-scaled concurrency. Floor at 1 so a deeply defensive dial still
        # allows one probe position instead of fully muting the autotrader.
        max_c = max(1, int(round(base_max_c * dial)))
        snap["max_concurrent_effective"] = max_c
        snap["max_concurrent_base"] = base_max_c
        snap["autotrader_open_count"] = ctx.autotrader_open_count
        if ctx.autotrader_open_count >= max_c:
            return False, "max_concurrent_autotrader", snap

        uid = alert.user_id if alert.user_id is not None else fallback_user_id
        if uid is None:
            return False, "missing_user_id_on_alert", snap

        from .portfolio_risk import check_new_trade_allowed

        cap, cap_source = resolve_effective_capital(db, settings)
        snap["capital_usd"] = round(cap, 2)
        snap["capital_source"] = cap_source
        ok, reason = check_new_trade_allowed(db, uid, alert.ticker.upper(), capital=cap)
        snap["portfolio_check"] = {"ok": ok, "reason": reason}
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
        Trade.status == "open",
    )
    if user_id is not None:
        q = q.filter(Trade.user_id == user_id)
    return int(q.count())


def autotrader_paper_realized_pnl_today_et(db: Session, user_id: Optional[int]) -> float:
    """Sum PaperTrade.pnl for autotrader-tagged rows closed today (US/Eastern)."""
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
        if row.pnl is not None:
            total += float(row.pnl)
    return total


def autotrader_realized_pnl_today_et(db: Session, user_id: Optional[int]) -> float:
    """Sum Trade.pnl for autotrader v1 positions closed on current US/Eastern calendar day."""
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
        if t.pnl is not None:
            total += float(t.pnl)
    return total


def breakout_alert_already_processed(db: Session, breakout_alert_id: int) -> bool:
    return (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == breakout_alert_id)
        .first()
        is not None
    )
