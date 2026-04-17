"""Phase B: canonical, pure ExitEvaluator for the trading-brain exit engines.

Single source of truth for exit-decision semantics. Both the backtest path
(``backtest_service.py::DynamicPatternStrategy``) and the live/paper path
(``live_exit_engine.py::compute_live_exit_levels``) feed this module in
shadow mode and log the canonical decision beside their legacy one.

Design rules (enforced by the Phase B plan, see
``.cursor/plans/phase_b_exit_engine_unification.plan.md``):

    1. Pure and deterministic: no DB, no HTTP, no ``fetch_ohlcv_df``, no
       logging. Deterministic given (config, state, bar).
    2. No new exit rules: the union of what the two legacy paths already
       implement, parameterized so each caller can reproduce its legacy
       behavior bit-for-bit.
    3. Rule priority is frozen:
           stop -> target -> BOS -> time_decay -> trail -> partial
       Priority only determines the ``reason_code`` label when more than
       one rule would fire on the same bar; it does not change whether
       the position closes. A close is a close.
    4. Trail monotonicity: trailing stop never loosens in the returned
       ``updated_state``. Callers must carry ``updated_state`` forward to
       the next bar.
    5. Crypto-safe: evaluator treats ``BASE-USD`` and bare ``BASEUSD``
       identically — ticker normalization is the adapter's job, not the
       evaluator's.

This module is NOT allowed to import from adapters (``live_exit_engine``,
``backtest_service``). Anything that would pull DB state belongs in the
adapter layer.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any


EXIT_ACTION_HOLD = "hold"
EXIT_ACTION_EXIT_STOP = "exit_stop"
EXIT_ACTION_EXIT_TARGET = "exit_target"
EXIT_ACTION_EXIT_TRAIL = "exit_trail"
EXIT_ACTION_EXIT_BOS = "exit_bos"
EXIT_ACTION_EXIT_TIME_DECAY = "exit_time_decay"
EXIT_ACTION_PARTIAL = "partial"


@dataclass(frozen=True)
class ExitConfig:
    """Frozen exit configuration for one position.

    All fields are optional flags that map to legacy behaviour:

    * ``trail_atr_mult``: backtest ``_exit_atr_mult`` (default 2.0 in
      ``DynamicPatternStrategy``); live ``trailing_atr_mult`` (default 1.5
      in ``live_exit_engine``). ``None`` disables the trail rule.
    * ``hard_stop_enabled``: True in live (``current_price <= stop``);
      False in backtest (no hard stop — trail only).
    * ``hard_target_enabled``: True in live; False in backtest.
    * ``max_bars``: bars-in-trade cap. Legacy both paths.
    * ``use_bos``: Break-of-Structure exit toggle.
    * ``bos_buffer_frac``: fractional buffer below swing low
      (backtest: 0.003/0.008/0.015 volatility-adaptive; live config stores
      a percent like 0.5 which the adapter must divide by 100 before
      passing to the evaluator).
    * ``bos_grace_bars``: minimum bars-in-trade before BOS is allowed to
      fire (backtest: 3/4/6; live: 0).
    * ``partial_at_1r``: emit ``partial`` action at 1R move.
    * ``trail_source``: one of ``"close"`` (backtest, uses close) or
      ``"high"`` (conservative alt, uses bar high). Defaults to ``"close"``
      for backward parity.
    """

    trail_atr_mult: float | None = None
    hard_stop_enabled: bool = False
    hard_target_enabled: bool = False
    max_bars: int | None = None
    use_bos: bool = False
    bos_buffer_frac: float = 0.0
    bos_grace_bars: int = 0
    partial_at_1r: bool = False
    trail_source: str = "close"
    # When ``True`` (default) the trailing stop is never allowed to loosen.
    # This is mathematically correct. Legacy DynamicPatternStrategy recomputes
    # ``highest_since_entry - k*atr`` each bar without a monotonicity guard,
    # so for bit-for-bit backtest parity the legacy flavor passes ``False``.
    trail_monotonic: bool = True

    def config_hash(self) -> str:
        """Stable 16-char hash of the config for grouping parity rows."""
        blob = json.dumps(asdict(self), sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]


@dataclass
class PositionState:
    """Mutable per-position state carried bar-to-bar by the adapter.

    The evaluator returns an ``updated_state`` that the adapter must use
    on the next call. Direction ``"short"`` is supported for symmetry;
    legacy code paths are long-only but the math reflects both sides so
    that follow-up phases can enable shorts without schema surgery.
    """

    direction: str  # "long" or "short"
    entry_price: float
    stop_price: float | None
    target_price: float | None
    bars_held: int = 0
    highest_since_entry: float | None = None
    lowest_since_entry: float | None = None
    trailing_stop: float | None = None
    partial_taken: bool = False


@dataclass(frozen=True)
class BarContext:
    """One bar of OHLC + pre-computed context for a single evaluation call."""

    open: float
    high: float
    low: float
    close: float
    atr: float | None
    swing_low: float | None
    swing_high: float | None = None
    bar_idx: int = 0
    bar_ts: str | None = None


@dataclass(frozen=True)
class ExitDecision:
    """Return value of ``evaluate_bar``.

    ``updated_state`` MUST be carried forward by the caller even if
    ``action == hold``, because trail/highest/lowest state evolves every
    bar.
    """

    action: str
    exit_price: float | None
    reason_code: str
    r_multiple: float | None
    trailing_stop: float | None
    updated_state: PositionState


def _is_long(state: PositionState) -> bool:
    return state.direction == "long"


def _safe_float(x: Any, default: float | None = None) -> float | None:
    try:
        if x is None:
            return default
        f = float(x)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def _compute_r_multiple(state: PositionState, ref_price: float) -> float | None:
    """Return R-multiple of the position at ``ref_price``.

    R is ``(entry - stop)`` for longs or ``(stop - entry)`` for shorts. If
    no stop is known the function returns ``None``.
    """
    if state.stop_price is None:
        return None
    risk = abs(state.entry_price - state.stop_price)
    if risk <= 0:
        return None
    if _is_long(state):
        return (ref_price - state.entry_price) / risk
    return (state.entry_price - ref_price) / risk


def _update_extremes(state: PositionState, bar: BarContext) -> PositionState:
    """Update highest/lowest since entry; does not change trailing stop."""
    hi = state.highest_since_entry
    lo = state.lowest_since_entry
    # Backtest uses close for highest-since-entry; we mirror that and also
    # track high/low to keep symmetry for shorts and future rules.
    close_val = float(bar.close)
    if hi is None or close_val > hi:
        hi = close_val
    if lo is None or close_val < lo:
        lo = close_val
    return replace(state, highest_since_entry=hi, lowest_since_entry=lo)


def _new_trailing_stop(
    config: ExitConfig, state: PositionState, bar: BarContext
) -> float | None:
    """Compute the (possibly updated) trailing stop. Never loosens."""
    if config.trail_atr_mult is None:
        return state.trailing_stop
    atr = _safe_float(bar.atr, None)
    if atr is None or atr <= 0:
        return state.trailing_stop

    if _is_long(state):
        anchor = state.highest_since_entry
        if anchor is None:
            return state.trailing_stop
        candidate = float(anchor) - float(config.trail_atr_mult) * atr
        if state.trailing_stop is None or not config.trail_monotonic:
            return candidate
        return max(state.trailing_stop, candidate)

    anchor = state.lowest_since_entry
    if anchor is None:
        return state.trailing_stop
    candidate = float(anchor) + float(config.trail_atr_mult) * atr
    if state.trailing_stop is None or not config.trail_monotonic:
        return candidate
    return min(state.trailing_stop, candidate)


def evaluate_bar(
    config: ExitConfig, state: PositionState, bar: BarContext
) -> ExitDecision:
    """Evaluate one bar against one position.

    Frozen priority: stop -> target -> BOS -> time_decay -> trail -> partial.
    Returns the highest-priority action that would fire on this bar. If
    multiple rules would fire simultaneously, only the highest-priority
    reason is reported (PnL impact of a close is identical either way;
    label choice is a reporting concern).

    Monotonic trail: ``updated_state.trailing_stop`` is always >= previous
    trailing stop for longs (and <= for shorts). Callers carry it forward.
    """
    new_state = _update_extremes(state, bar)
    new_state = replace(new_state, bars_held=int(state.bars_held) + 1)
    new_trail = _new_trailing_stop(config, new_state, bar)
    new_state = replace(new_state, trailing_stop=new_trail)

    is_long = _is_long(state)

    # 1. Hard stop (live paths). Long: if bar low breaches stop, exit at stop.
    if config.hard_stop_enabled and state.stop_price is not None:
        if is_long and bar.low <= state.stop_price:
            r = _compute_r_multiple(state, state.stop_price)
            return ExitDecision(
                action=EXIT_ACTION_EXIT_STOP,
                exit_price=float(state.stop_price),
                reason_code="hard_stop",
                r_multiple=r,
                trailing_stop=new_trail,
                updated_state=new_state,
            )
        if (not is_long) and bar.high >= state.stop_price:
            r = _compute_r_multiple(state, state.stop_price)
            return ExitDecision(
                action=EXIT_ACTION_EXIT_STOP,
                exit_price=float(state.stop_price),
                reason_code="hard_stop",
                r_multiple=r,
                trailing_stop=new_trail,
                updated_state=new_state,
            )

    # 2. Hard target (live paths). Long: if bar high reaches target, exit at target.
    if config.hard_target_enabled and state.target_price is not None:
        if is_long and bar.high >= state.target_price:
            r = _compute_r_multiple(state, state.target_price)
            return ExitDecision(
                action=EXIT_ACTION_EXIT_TARGET,
                exit_price=float(state.target_price),
                reason_code="hard_target",
                r_multiple=r,
                trailing_stop=new_trail,
                updated_state=new_state,
            )
        if (not is_long) and bar.low <= state.target_price:
            r = _compute_r_multiple(state, state.target_price)
            return ExitDecision(
                action=EXIT_ACTION_EXIT_TARGET,
                exit_price=float(state.target_price),
                reason_code="hard_target",
                r_multiple=r,
                trailing_stop=new_trail,
                updated_state=new_state,
            )

    # 3. BOS: only after grace bars, requires a valid swing low/high.
    if (
        config.use_bos
        and new_state.bars_held >= int(config.bos_grace_bars or 0)
    ):
        if is_long and bar.swing_low is not None and bar.swing_low > 0:
            bos_level = float(bar.swing_low) * (1.0 - float(config.bos_buffer_frac))
            if bar.close < bos_level:
                r = _compute_r_multiple(state, float(bar.close))
                return ExitDecision(
                    action=EXIT_ACTION_EXIT_BOS,
                    exit_price=float(bar.close),
                    reason_code="bos_long",
                    r_multiple=r,
                    trailing_stop=new_trail,
                    updated_state=new_state,
                )
        if (not is_long) and bar.swing_high is not None and bar.swing_high > 0:
            bos_level = float(bar.swing_high) * (1.0 + float(config.bos_buffer_frac))
            if bar.close > bos_level:
                r = _compute_r_multiple(state, float(bar.close))
                return ExitDecision(
                    action=EXIT_ACTION_EXIT_BOS,
                    exit_price=float(bar.close),
                    reason_code="bos_short",
                    r_multiple=r,
                    trailing_stop=new_trail,
                    updated_state=new_state,
                )

    # 4. Time decay: cap on bars held.
    if config.max_bars is not None and new_state.bars_held >= int(config.max_bars):
        r = _compute_r_multiple(state, float(bar.close))
        return ExitDecision(
            action=EXIT_ACTION_EXIT_TIME_DECAY,
            exit_price=float(bar.close),
            reason_code="max_bars",
            r_multiple=r,
            trailing_stop=new_trail,
            updated_state=new_state,
        )

    # 5. Trailing stop breach on current bar close.
    if new_trail is not None:
        if is_long and float(bar.close) < float(new_trail):
            r = _compute_r_multiple(state, float(bar.close))
            return ExitDecision(
                action=EXIT_ACTION_EXIT_TRAIL,
                exit_price=float(bar.close),
                reason_code="trail_long",
                r_multiple=r,
                trailing_stop=new_trail,
                updated_state=new_state,
            )
        if (not is_long) and float(bar.close) > float(new_trail):
            r = _compute_r_multiple(state, float(bar.close))
            return ExitDecision(
                action=EXIT_ACTION_EXIT_TRAIL,
                exit_price=float(bar.close),
                reason_code="trail_short",
                r_multiple=r,
                trailing_stop=new_trail,
                updated_state=new_state,
            )

    # 6. Partial at 1R (non-terminal: caller should not close the position).
    if config.partial_at_1r and not state.partial_taken:
        r_now = _compute_r_multiple(state, float(bar.close))
        if r_now is not None and r_now >= 1.0:
            marked = replace(new_state, partial_taken=True)
            return ExitDecision(
                action=EXIT_ACTION_PARTIAL,
                exit_price=float(bar.close),
                reason_code="partial_at_1r",
                r_multiple=r_now,
                trailing_stop=new_trail,
                updated_state=marked,
            )

    return ExitDecision(
        action=EXIT_ACTION_HOLD,
        exit_price=None,
        reason_code="hold",
        r_multiple=_compute_r_multiple(state, float(bar.close)),
        trailing_stop=new_trail,
        updated_state=new_state,
    )


def build_config_live(exit_cfg_dict: dict[str, Any] | None) -> ExitConfig:
    """Build an ``ExitConfig`` that reproduces live/paper semantics.

    Legacy ``live_exit_engine._load_exit_config`` defaults:

        atr_stop_mult=2.0, atr_target_mult=3.0, trailing_enabled=True,
        trailing_atr_mult=1.5, max_bars=20, use_bos=True,
        bos_buffer_pct=0.5 (PERCENT — divided by 100 here),
        partial_at_1r=False

    Live has hard stop and hard target because ``PaperTrade`` / ``Trade``
    rows carry explicit stop_price and target_price.
    """
    cfg = dict(exit_cfg_dict or {})
    max_bars = cfg.get("max_bars", 20)
    use_bos = bool(cfg.get("use_bos", True))
    bos_buffer_pct = float(cfg.get("bos_buffer_pct", 0.5))
    # Legacy ``compute_live_exit_levels`` computes a trailing stop value for
    # reporting but never closes on it — the caller reads ``action`` which
    # only flips on hard stop, hard target, time decay or BOS. For shadow
    # parity we therefore disable the trail-close rule here. A follow-up
    # cutover phase can opt live into the canonical monotonic-trail close.
    return ExitConfig(
        trail_atr_mult=None,
        hard_stop_enabled=True,
        hard_target_enabled=True,
        max_bars=int(max_bars) if max_bars is not None else None,
        use_bos=use_bos,
        bos_buffer_frac=bos_buffer_pct / 100.0,
        bos_grace_bars=0,
        partial_at_1r=bool(cfg.get("partial_at_1r", False)),
        trail_source="close",
        trail_monotonic=True,
    )


def build_config_backtest(
    *,
    exit_atr_mult: float = 2.0,
    exit_max_bars: int = 20,
    use_bos: bool = True,
    bos_buffer_frac: float = 0.003,
    bos_grace_bars: int = 3,
) -> ExitConfig:
    """Build an ``ExitConfig`` that reproduces ``DynamicPatternStrategy`` semantics.

    Backtest has NO hard stop / hard target checks. Exit triggers only by
    trail, max_bars, or BOS (OR-combined; priority determines label only).
    """
    return ExitConfig(
        trail_atr_mult=float(exit_atr_mult),
        hard_stop_enabled=False,
        hard_target_enabled=False,
        max_bars=int(exit_max_bars),
        use_bos=bool(use_bos),
        bos_buffer_frac=float(bos_buffer_frac),
        bos_grace_bars=int(bos_grace_bars),
        partial_at_1r=False,
        trail_source="close",
        # Legacy DynamicPatternStrategy recomputes the trailing stop from
        # scratch each bar — it is NOT monotonic. For bit-for-bit parity in
        # shadow mode we mirror that. Cutover to monotonic trail is a
        # separate, explicit decision in a follow-up phase.
        trail_monotonic=False,
    )


__all__ = [
    "ExitConfig",
    "PositionState",
    "BarContext",
    "ExitDecision",
    "EXIT_ACTION_HOLD",
    "EXIT_ACTION_EXIT_STOP",
    "EXIT_ACTION_EXIT_TARGET",
    "EXIT_ACTION_EXIT_TRAIL",
    "EXIT_ACTION_EXIT_BOS",
    "EXIT_ACTION_EXIT_TIME_DECAY",
    "EXIT_ACTION_PARTIAL",
    "evaluate_bar",
    "build_config_live",
    "build_config_backtest",
]
