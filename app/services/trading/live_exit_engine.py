"""Live exit engine — mirrors DynamicPatternStrategy exit logic for real/paper positions.

Supports:
- ATR trailing stops (tighten only)
- Time-decay exits (reduce after N bars with no move)
- Partial profit-taking at R-multiples
- Break-of-structure (BOS) exits via swing-low breach
- Pattern-specific exit_config from ScanPattern.exit_config

Phase B (shadow): every call also runs the canonical ExitEvaluator
(``app.services.trading.exit_evaluator``) in parallel and logs parity
against the legacy decision into ``trading_exit_parity_log``. In any mode
other than ``authoritative`` the legacy dict is what callers act on.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import PaperTrade, ScanPattern, Trade

logger = logging.getLogger(__name__)


def _compute_bars_held(db: Session, trade: PaperTrade | Trade) -> int:
    """Bars elapsed since ``trade.entry_date``, sized to the position's
    pattern timeframe.

    Migration 227 fix: pre-fix the legacy time-decay path computed
    ``(now - entry_date).days`` regardless of timeframe, so a 1m position
    only "ages" by 1 bar after a full day instead of 1440. Survey at
    fix time: 181 × 1m, 116 × 5m, 84 × 15m, 170 × 1h, 74 × 4h, 144 × 1d
    -- 625 of 769 patterns silently affected.

    Falls back to ``"1d"`` when:
      * the position has no associated ScanPattern (orphan / direct-entry);
      * the pattern's timeframe value is missing or unknown to
        ``timeframe_utils._TIMEFRAME_SECONDS`` (logged WARNING).

    The fallback preserves legacy semantics for the orphan case so this
    fix is forward-only -- existing time-decay decisions on 1d positions
    keep their pre-fix behaviour.
    """
    from .timeframe_utils import timeframe_to_seconds

    if not trade.entry_date:
        return 0
    tf = "1d"
    sp_id = getattr(trade, "scan_pattern_id", None)
    if sp_id:
        try:
            pat = db.query(ScanPattern).filter(ScanPattern.id == sp_id).first()
            if pat and pat.timeframe:
                tf = pat.timeframe
        except Exception:
            pass
    try:
        tf_seconds = timeframe_to_seconds(tf)
    except ValueError:
        logger.warning(
            "[exit_engine] Unknown timeframe %r for trade_id=%s; "
            "defaulting to 1d. Add to timeframe_utils._TIMEFRAME_SECONDS.",
            tf, getattr(trade, "id", None),
        )
        tf_seconds = 86400
    elapsed_s = (datetime.utcnow() - trade.entry_date).total_seconds()
    return max(0, int(elapsed_s // tf_seconds))


def compute_live_exit_levels(
    db: Session,
    trade: PaperTrade | Trade,
    current_price: float,
) -> dict[str, Any]:
    """Compute adaptive exit levels for an open position based on its pattern's config."""
    from .market_data import fetch_ohlcv_df
    from .indicator_core import compute_atr

    result: dict[str, Any] = {"action": "hold"}

    exit_cfg = _load_exit_config(db, getattr(trade, "scan_pattern_id", None))
    entry = trade.entry_price
    # Phase 4 (2026-05-01): consolidated fallback (was inline 0.97).
    # Single source of truth in stop_engine_fallback_constants.
    from .stop_engine_fallback_constants import (
        FALLBACK_INITIAL_STOP_PCT, FALLBACK_DEFAULT_RISK_PCT,
    )
    stop = trade.stop_price or entry * (1.0 - FALLBACK_INITIAL_STOP_PCT)
    is_long = getattr(trade, "direction", "long") == "long"
    risk = abs(entry - stop) if entry and stop else entry * FALLBACK_DEFAULT_RISK_PCT

    try:
        df = fetch_ohlcv_df(trade.ticker, period="3mo", interval="1d")
        if df is not None and len(df) >= 14:
            atr_arr = compute_atr(df["High"].values, df["Low"].values, df["Close"].values, period=14)
            atr = float(atr_arr[-1]) if len(atr_arr) > 0 and not math.isnan(atr_arr[-1]) else None
        else:
            atr = None
    except Exception:
        atr = None

    result["atr"] = atr
    result["exit_config"] = exit_cfg

    if atr and exit_cfg.get("trailing_enabled", True):
        # Pattern-specific trailing_atr_mult overrides the global one. When
        # the pattern config doesn't pin the value (most don't), Q2 Task J
        # routes through the StrategyParameter registry so the learner can
        # adapt the trailing-stop tightness from realized exit outcomes.
        cfg_trail = exit_cfg.get("trailing_atr_mult")
        if cfg_trail is None:
            trail_mult = _resolve_trailing_atr_mult(db)
        else:
            trail_mult = float(cfg_trail)
        result["trailing_atr_mult_used"] = trail_mult
        if is_long:
            trail_stop = current_price - (atr * trail_mult)
            result["trailing_stop"] = round(trail_stop, 4)
        else:
            trail_stop = current_price + (atr * trail_mult)
            result["trailing_stop"] = round(trail_stop, 4)

    if is_long and current_price <= stop:
        result["action"] = "exit_stop"
        result["exit_price"] = stop
    elif not is_long and current_price >= stop:
        result["action"] = "exit_stop"
        result["exit_price"] = stop

    target = getattr(trade, "target_price", None)
    if target:
        if is_long and current_price >= target:
            result["action"] = "exit_target"
            result["exit_price"] = target
        elif not is_long and current_price <= target:
            result["action"] = "exit_target"
            result["exit_price"] = target

    max_bars = exit_cfg.get("max_bars")
    if max_bars and trade.entry_date:
        # Migration 227: bars-held is computed unit-aware via the
        # position's pattern timeframe. Pre-fix this was wall-clock
        # ``.days``, which silently broke time-decay on every non-1d
        # pattern (181 1m + 116 5m + 84 15m + 170 1h + 74 4h patterns
        # in production survey).
        bars_held = _compute_bars_held(db, trade)
        if bars_held >= max_bars and result["action"] == "hold":
            result["action"] = "exit_time_decay"
            result["exit_price"] = current_price
            result["bars_held"] = bars_held

    swing_low_val: float | None = None
    if atr and exit_cfg.get("use_bos", True):
        try:
            df_recent = fetch_ohlcv_df(trade.ticker, period="1mo", interval="1d")
            if df_recent is not None and len(df_recent) >= 5:
                lows = df_recent["Low"].values[-5:]
                swing_low_val = float(min(lows))
                bos_buffer = exit_cfg.get("bos_buffer_pct", 0.5) / 100
                bos_level = swing_low_val * (1 - bos_buffer) if is_long else swing_low_val * (1 + bos_buffer)
                result["bos_level"] = round(bos_level, 4)
                if is_long and current_price < bos_level:
                    result["action"] = "exit_bos"
                    result["exit_price"] = current_price
        except Exception:
            pass

    # Partial-profit emission (migration 226 wired this up). Priority
    # discipline: partial only fires when no terminal exit would, so the
    # action is checked AFTER stop/target/time_decay/BOS have had their
    # chance. ``partial_taken`` gates re-fire (single partial per trade).
    # The legacy ``partial_profit_eligible`` flag was dead (zero readers
    # confirmed via grep); replaced with an actual ``action="partial"``
    # that ``run_exit_engine`` routes into the partial_actions bucket.
    if (
        risk > 0
        and exit_cfg.get("partial_at_1r", False)
        and not getattr(trade, "partial_taken", False)
        and result["action"] == "hold"
    ):
        r_move = (current_price - entry) / risk if is_long else (entry - current_price) / risk
        if r_move >= 1.0:
            result["action"] = "partial"
            result["exit_price"] = current_price
            result["r_multiple"] = round(r_move, 2)
            result["partial_close_fraction"] = float(
                exit_cfg.get("partial_close_fraction", 0.5)
            )

    _phase_b_shadow_parity(
        db=db,
        trade=trade,
        exit_cfg=exit_cfg,
        current_price=current_price,
        atr=atr,
        swing_low_val=swing_low_val,
        legacy_result=result,
    )

    return result


_DEFAULT_TRAILING_ATR_MULT = 1.5
_TRAILING_ATR_MULT_BOUNDS = (0.5, 5.0)


def _resolve_trailing_atr_mult(db: Session | None) -> float:
    """Q2 Task J — adaptive trailing-stop ATR multiple.

    Default 1.5 ATR (current behavior). Bounds [0.5, 5.0] keep the
    learner from setting a trailing stop tighter than half an ATR (gets
    stopped out by noise) or looser than five ATR (gives back too much
    open profit).
    """
    if db is None:
        return _DEFAULT_TRAILING_ATR_MULT
    try:
        from .strategy_parameter import (
            ParameterSpec, get_parameter, register_parameter,
        )
        register_parameter(
            db,
            ParameterSpec(
                strategy_family="exit_engine",
                parameter_key="trailing_atr_mult",
                initial_value=_DEFAULT_TRAILING_ATR_MULT,
                min_value=_TRAILING_ATR_MULT_BOUNDS[0],
                max_value=_TRAILING_ATR_MULT_BOUNDS[1],
                description=(
                    "ATR multiple for live trailing stops on positions "
                    "that don't pin a pattern-specific trailing_atr_mult. "
                    "The learner adapts this from realized exit outcomes "
                    "(stopped-by-noise vs gave-back-profit)."
                ),
            ),
        )
        v = get_parameter(
            db, "exit_engine", "trailing_atr_mult",
            default=_DEFAULT_TRAILING_ATR_MULT,
        )
        if v is None:
            return _DEFAULT_TRAILING_ATR_MULT
        return float(max(_TRAILING_ATR_MULT_BOUNDS[0],
                         min(_TRAILING_ATR_MULT_BOUNDS[1], v)))
    except Exception:
        return _DEFAULT_TRAILING_ATR_MULT


def _load_exit_config(db: Session, scan_pattern_id: int | None) -> dict:
    """Load exit config from the ScanPattern, with sensible defaults.

    Note: ``trailing_atr_mult`` defaults to ``None`` so the engine's
    StrategyParameter resolver kicks in when the pattern doesn't pin the
    value. Setting a numeric default here would shadow the registry.
    """
    defaults = {
        "atr_stop_mult": 2.0,
        "atr_target_mult": 3.0,
        "trailing_enabled": True,
        "trailing_atr_mult": None,
        "max_bars": 20,
        "use_bos": True,
        "bos_buffer_pct": 0.5,
        "partial_at_1r": False,
        # Fraction of position to close on the partial fire. 0.5 = "take
        # half off at 1R, let the rest run". Pattern can override via
        # ``exit_config.partial_close_fraction``. Bounds [0, 1] enforced
        # at place_partial_close call time (the consumer).
        "partial_close_fraction": 0.5,
    }
    if not scan_pattern_id:
        return defaults
    try:
        pat = db.query(ScanPattern).filter(ScanPattern.id == scan_pattern_id).first()
        if pat and pat.exit_config:
            cfg = pat.exit_config if isinstance(pat.exit_config, dict) else json.loads(pat.exit_config)
            defaults.update({k: v for k, v in cfg.items() if v is not None})
    except Exception:
        pass
    return defaults


def run_exit_engine(db: Session, user_id: int | None = None) -> dict[str, Any]:
    """Evaluate all open positions through the exit engine. Returns action recommendations."""
    from .market_data import fetch_quote

    open_paper = db.query(PaperTrade).filter(PaperTrade.status == "open")
    if user_id is not None:
        open_paper = open_paper.filter(PaperTrade.user_id == user_id)
    positions = open_paper.all()

    results = []
    for pos in positions:
        try:
            q = fetch_quote(pos.ticker)
            if not q or not q.get("price"):
                continue
            price = float(q["price"])
            exit_rec = compute_live_exit_levels(db, pos, price)
            exit_rec["ticker"] = pos.ticker
            exit_rec["position_id"] = pos.id
            exit_rec["current_price"] = price
            results.append(exit_rec)
        except Exception as e:
            logger.debug("[exit_engine] Error evaluating %s: %s", pos.ticker, e)

    # Split non-hold actions into terminal vs partial buckets. The
    # ``actions`` key keeps the legacy meaning (terminal closes only) so
    # existing consumers don't change behaviour. ``partial_actions`` is
    # a new bucket the auto-trader / paper-runner consumes separately to
    # call ``place_partial_close`` without closing the whole position.
    terminal_actions = [
        r for r in results
        if r.get("action") not in ("hold", "partial")
    ]
    partial_actions = [r for r in results if r.get("action") == "partial"]
    logger.info(
        "[exit_engine] Evaluated %d positions: %d terminal + %d partial actions recommended",
        len(results), len(terminal_actions), len(partial_actions),
    )

    return {
        "ok": True,
        "evaluated": len(results),
        "actions": terminal_actions,
        "partial_actions": partial_actions,
        "all": results,
    }


def _phase_b_shadow_parity(
    *,
    db: Session,
    trade: PaperTrade | Trade,
    exit_cfg: dict,
    current_price: float,
    atr: float | None,
    swing_low_val: float | None,
    legacy_result: dict[str, Any],
) -> None:
    """Phase B shadow hook: run the canonical ExitEvaluator and log parity.

    Side-effect only. The canonical decision MUST NOT influence ``legacy_result``
    while ``brain_exit_engine_mode`` is not ``authoritative``. Failures are
    swallowed so the legacy path is never broken by a parity log issue.
    """
    try:
        from ...config import settings
        mode = str(getattr(settings, "brain_exit_engine_mode", "off") or "off").lower()
        if mode == "off":
            return
        if mode == "authoritative":
            logger.warning(
                "[exit_engine_ops] authoritative mode reached in live adapter but "
                "cutover is not part of Phase B; treating as shadow."
            )
            mode = "shadow"

        sample_pct = float(getattr(settings, "brain_exit_engine_parity_sample_pct", 1.0) or 1.0)
        ops_log_enabled = bool(getattr(settings, "brain_exit_engine_ops_log_enabled", True))

        from . import exit_evaluator as ev
        from ...models.trading import ExitParityLog
        from ...trading_brain.infrastructure.exit_engine_ops_log import (
            format_exit_engine_ops_line,
        )

        cfg = ev.build_config_live(exit_cfg)

        is_long = getattr(trade, "direction", "long") == "long"
        entry = float(trade.entry_price)
        stop = float(trade.stop_price) if trade.stop_price else entry * (0.97 if is_long else 1.03)
        target = getattr(trade, "target_price", None)
        target_f = float(target) if target else None
        # Migration 227: keep legacy and canonical adapters in sync via the
        # same unit-aware bars-held helper. Pre-fix this branch read
        # ``.days``, identical to the legacy bug above; canonical inherited
        # the wrong unit through this adapter.
        try:
            bars_held = _compute_bars_held(db, trade)
        except Exception:
            bars_held = 0

        state = ev.PositionState(
            direction="long" if is_long else "short",
            entry_price=entry,
            stop_price=stop,
            target_price=target_f,
            bars_held=max(0, bars_held - 1),  # evaluate_bar increments
            highest_since_entry=max(entry, current_price) if is_long else entry,
            lowest_since_entry=min(entry, current_price) if is_long else entry,
            trailing_stop=None,
            partial_taken=False,
        )
        bar = ev.BarContext(
            open=current_price,
            high=current_price,
            low=current_price,
            close=current_price,
            atr=atr,
            swing_low=swing_low_val,
            swing_high=None,
            bar_idx=bars_held,
            bar_ts=None,
        )

        decision = ev.evaluate_bar(cfg, state, bar)
        legacy_action = str(legacy_result.get("action") or "hold")
        canonical_action = decision.action
        agree = (legacy_action == canonical_action)
        # Live's existing ``agree`` is already strict label equality, so
        # ``agree_strict_bool`` mirrors ``agree_bool`` for live rows. The
        # column exists so verdict queries can apply a single definition
        # across live and backtest sources (backtest's ``agree_bool`` is
        # the looser "both engines closed" definition).
        agree_strict = (legacy_action == canonical_action)
        config_hash = cfg.config_hash()

        legacy_xp = legacy_result.get("exit_price")
        canonical_xp = decision.exit_price
        pnl_diff_pct: float | None = None
        # Long-only sign convention. ``compute_live_exit_levels`` is
        # long-only today; if shorts are added later, negate for short rows.
        if (
            legacy_xp is not None
            and canonical_xp is not None
            and float(legacy_xp) > 0
        ):
            pnl_diff_pct = float(
                (float(canonical_xp) - float(legacy_xp))
                / float(legacy_xp)
                * 100.0
            )

        # f-exit-parity-metric-v2 (Migration 230): compute the four new
        # parity-decomposition fields via the shared pure helper so the
        # live and backtest paths stay byte-identical on this logic.
        from .exit_parity_metric import compute_parity_v2_fields, should_persist_parity_row
        v2 = compute_parity_v2_fields(
            legacy_action=legacy_action,
            canonical_action=canonical_action,
            legacy_exit_price=legacy_xp,
            canonical_exit_price=canonical_xp,
            canonical_reason_code=decision.reason_code,
            direction="long" if is_long else "short",
        )

        row_kwargs = dict(
            source="live",
            position_id=int(getattr(trade, "id", 0) or 0) or None,
            scan_pattern_id=getattr(trade, "scan_pattern_id", None),
            ticker=str(trade.ticker),
            bar_ts=None,
            legacy_action=legacy_action,
            legacy_exit_price=legacy_xp,
            canonical_action=canonical_action,
            canonical_exit_price=canonical_xp,
            pnl_diff_pct=pnl_diff_pct,
            agree_bool=bool(agree),
            agree_strict_bool=bool(agree_strict),
            action_class=v2.action_class,
            label_match=v2.label_match,
            exit_price_drift_bps=v2.exit_price_drift_bps,
            priority_winner=v2.priority_winner,
            mode=mode,
            config_hash=config_hash,
            provenance_json={
                "current_price": float(current_price),
                "atr": atr,
                "swing_low": swing_low_val,
                "bars_held_estimate": bars_held,
                "reason_code": decision.reason_code,
            },
        )

        persisted = should_persist_parity_row(
            sample_pct=sample_pct,
            action_class=v2.action_class,
            agree_bool=bool(agree),
            legacy_action=legacy_action,
            canonical_action=canonical_action,
            source="live",
            ticker=str(trade.ticker),
            position_id=row_kwargs["position_id"],
            scan_pattern_id=getattr(trade, "scan_pattern_id", None),
            config_hash=config_hash,
            sample_salt=f"{bars_held}:{current_price}",
        )
        if persisted:
            # Use a fresh SessionLocal so the parity write commits independently
            # of the caller's transaction. The caller chain (trading_scheduler ->
            # _run_paper_trade_check_job) wraps ``check_paper_exits`` writes in
            # the same db, and a ``db.commit()`` here would prematurely flush
            # those. ``db.flush()`` (the previous behaviour) sends to server
            # state but rolls back if the caller never commits -- which is why
            # 0 live parity rows ever landed.
            from ...db import SessionLocal as _SL
            with _SL() as parity_db:
                parity_db.add(ExitParityLog(**row_kwargs))
                parity_db.commit()

        if ops_log_enabled:
            line = format_exit_engine_ops_line(
                mode=mode,
                source="live",
                position_id=row_kwargs["position_id"],
                ticker=str(trade.ticker),
                legacy_action=legacy_action,
                canonical_action=canonical_action,
                agree=agree,
                config_hash=config_hash,
                sample_pct=sample_pct,
            )
            # Parity rows are sampled for boring hold/hold agreement. Only escalate
            # the INFO line for interesting cases (disagreements or actual exits).
            # Per-bar hold+hold+agree is ~90% of all lines and pure noise.
            boring = (
                bool(agree)
                and legacy_action == "hold"
                and canonical_action == "hold"
            )
            if boring:
                logger.debug(line)
            else:
                logger.info(line)
    except Exception as exc:  # pragma: no cover - defensive; legacy path must not break
        logger.debug("[exit_engine] shadow parity failed: %s", exc)
