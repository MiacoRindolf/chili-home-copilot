"""Momentum automation risk policy (config-backed; frozen on session snapshots — Phase 6)."""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from ....config import settings
from ..execution_family_registry import EXECUTION_FAMILY_COINBASE_SPOT

logger = logging.getLogger(__name__)

POLICY_VERSION = 1
RISK_SNAPSHOT_KEY = "momentum_risk"
POLICY_SNAPSHOT_KEY = "momentum_risk_policy_summary"

# Per-trade cap keys subject to the rolling-median spike guard (both derive from the
# same per-venue account-equity read, so a single spiked read inflates both at once).
_PER_TRADE_CAP_KEYS = ("max_notional_per_trade_usd", "max_loss_per_trade_usd")
# Statistical sample-size floor before the rolling median is trusted to clamp — mirrors
# the brain's standing n>=5 evidence floor; below it we never clamp (use the raw cap).
_CAP_MEDIAN_MIN_HISTORY = 5


def policy_float_cap(policy: dict[str, Any], key: str, default: float) -> float:
    raw = policy.get(key, default)
    if isinstance(raw, bool) or raw is None:
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def policy_int_cap(policy: dict[str, Any], key: str, default: int) -> int:
    raw = policy.get(key, default)
    if isinstance(raw, bool) or raw is None:
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def adaptive_max_spread_bps(
    base_max_spread_bps: float,
    expected_move_bps: float | None,
    ratio: float,
    *,
    abs_cap_bps: float | None = None,
) -> float:
    """Volatility-relative spread tolerance, with an absolute safety cap.

    The BBO/quote spread is a round-trip execution cost; we tolerate
    proportionally more of it when the instrument's expected move (realized
    volatility) is larger — it loosens above ``base_max_spread_bps`` (the
    documented live floor) for explosive names while quiet/illiquid names keep the
    conservative floor. ``ratio`` is the single documented knob: the spread may be
    at most ``ratio`` x the expected per-bar move.

    BUT capped by ``abs_cap_bps`` — Ross's hard "if the spread is too wide, skip
    the trade entirely" rule. Uncapped, a name with a huge expected move (an
    explosive low-float runner) would tolerate an ~8% spread: you start down 8%
    AND can't exit at your stop on the reversal (the bid vanishes; a thin book
    gets cleared). Ross *steps back* from those (WHLR halt-resume 30c/$14 ≈ 2%).
    The cap never forces tolerance BELOW the floor. Falls back to the base floor
    when expected move / ratio is unusable.
    """
    base = float(base_max_spread_bps)
    try:
        em = float(expected_move_bps) if expected_move_bps is not None else None
    except (TypeError, ValueError):
        em = None
    if em is None or not math.isfinite(em) or em <= 0:
        return base
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return base
    if not math.isfinite(r) or r <= 0:
        return base
    adaptive = max(base, r * em)
    if abs_cap_bps is not None:
        try:
            cap = float(abs_cap_bps)
            if math.isfinite(cap) and cap > 0:
                adaptive = min(adaptive, max(base, cap))  # never tolerate above the cap
        except (TypeError, ValueError):
            pass
    return adaptive


# Short-TTL cache for the agentic cash-account buying power: a Monday burst of
# candidate sizings must NOT fire a fresh adapter + tools/list + get_accounts +
# get_portfolio per name (rate-limit / latency → missed fast Ross breaks). Serve a
# recent value on a transient read miss so the basis never drops to None mid-burst.
_AGENTIC_BP_CACHE: dict[str, float] = {"value": 0.0, "ts": 0.0}
_AGENTIC_BP_TTL_SEC = 10.0
_AGENTIC_BP_STALE_GRACE = 60.0


def _agentic_buying_power_cached() -> float | None:
    import time as _time

    now = _time.monotonic()
    cached = _AGENTIC_BP_CACHE.get("value") or 0.0
    age = now - (_AGENTIC_BP_CACHE.get("ts") or 0.0)
    if cached > 0 and age < _AGENTIC_BP_TTL_SEC:
        return cached
    try:
        from ..venue.robinhood_mcp import RobinhoodAgenticMcpAdapter

        bp = RobinhoodAgenticMcpAdapter().get_buying_power_usd()
    except Exception:
        bp = None
    if bp is not None and bp > 0:
        _AGENTIC_BP_CACHE["value"] = float(bp)
        _AGENTIC_BP_CACHE["ts"] = now
        return float(bp)
    if cached > 0 and age < _AGENTIC_BP_STALE_GRACE:
        return cached  # transient miss → recent cached value, not None
    return None


_AGENTIC_EQ_CACHE: dict[str, float] = {"value": 0.0, "ts": 0.0}


def _agentic_equity_cached() -> float | None:
    """Total agentic account EQUITY (total_value), short-TTL cached like the BP read. The
    daily-loss RISK cap uses THIS (stable cash+positions value) instead of the fluctuating
    buying power (operator 2026-06-22: "equity based naman dapat"). Fail-open."""
    import time as _time

    now = _time.monotonic()
    cached = _AGENTIC_EQ_CACHE.get("value") or 0.0
    age = now - (_AGENTIC_EQ_CACHE.get("ts") or 0.0)
    if cached > 0 and age < _AGENTIC_BP_TTL_SEC:
        return cached
    try:
        from ..venue.robinhood_mcp import RobinhoodAgenticMcpAdapter

        eq = RobinhoodAgenticMcpAdapter().get_account_equity_usd()
    except Exception:
        eq = None
    if eq is not None and eq > 0:
        _AGENTIC_EQ_CACHE["value"] = float(eq)
        _AGENTIC_EQ_CACHE["ts"] = now
        return float(eq)
    if cached > 0 and age < _AGENTIC_BP_STALE_GRACE:
        return cached
    return None


def _account_equity_usd(
    execution_family: str | None = None, *, apply_margin_multiple: bool = True,
    prefer_equity: bool = False,
) -> float | None:
    """Best-effort account SIZING BASIS (USD) for equity-relative caps, PER VENUE.

    robinhood_spot -> Robinhood account (equities); else Coinbase portfolio (crypto).
    Basis = BUYING POWER when chili_momentum_risk_size_use_buying_power is True (default)
    so the lane utilizes available margin for sizing, NOT just settled cash/equity; falls
    back to equity if buying power is unavailable. Returns None when nothing is available
    so callers use the documented fixed cap (never size against an unknown account).

    apply_margin_multiple=False returns the RAW broker buying power (margin multiple
    forced to 1.0) — the basis for a daily-loss RISK cap. Operator 2026-06-15: "gamitin
    mo buying power, hindi lang cash" — but NOT the 2x-margin-inflated sizing number
    (a ~$2k Coinbase buying power must not read as $3,989 = bp*2.0). So the SIZING
    default applies the margin multiple; the RISK cap passes apply_margin_multiple=False
    to get the unlevered buying power (RH ~$13.4k / CB ~$2.0k).
    docs/DESIGN/MOMENTUM_LANE.md
    """
    from ..execution_family_registry import (
        EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
        normalize_execution_family,
    )

    ef = normalize_execution_family(execution_family)
    use_bp = bool(getattr(settings, "chili_momentum_risk_size_use_buying_power", True))

    # Agentic MCP rail: the isolated agentic account is a CASH account — its reported
    # buying_power IS the real, unleveraged spendable amount (no margin). Size against
    # it DIRECTLY with NO margin multiple (the 2x multiple exists only to recover
    # robin_stocks' under-reporting on the MARGIN main account; the MCP reports true BP).
    # Applying 2x here would submit orders exceeding the cash balance -> RH rejects.
    if ef == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP:
        # Cash account: reported BP IS the real spendable (NO margin multiple). Cached
        # (short TTL) so a burst of candidate sizings reuses one read. prefer_equity (the
        # daily-loss RISK cap) reads the STABLE total account value instead of fluctuating BP.
        if prefer_equity:
            eq = _agentic_equity_cached()
            if eq is not None and eq > 0:
                return float(eq)
        bp = _agentic_buying_power_cached()
        return float(bp) if (bp is not None and bp > 0) else None

    try:
        if ef == EXECUTION_FAMILY_ROBINHOOD_SPOT:
            from ...broker_service import get_portfolio as _rh_portfolio

            pf = _rh_portfolio() or {}
        else:
            from ...coinbase_service import get_portfolio

            pf = get_portfolio() or {}
        if use_bp:
            bp = float(pf.get("buying_power") or 0.0)
            if bp > 0:
                # SIZING applies the account's margin multiple (robin_stocks reports the
                # ~1x base; 2.0 recovers the 2x Gold margin the app shows). The RISK cap
                # passes apply_margin_multiple=False to use the unlevered buying power.
                mult = (
                    float(getattr(settings, "chili_momentum_risk_buying_power_margin_multiple", 1.0) or 1.0)
                    if apply_margin_multiple
                    else 1.0
                )
                return bp * max(1.0, mult)
        eq = float(pf.get("equity") or 0.0)
        return eq if eq > 0 else None
    except Exception:
        return None


def _equity_relative_cap(
    fixed_fallback_usd: float, fraction: Any, execution_family: str | None = None,
    *, prefer_equity: bool = False,
) -> float:
    """Cap = account_equity x fraction (equity-relative, not a fixed $), per venue.

    Scales UP as equity grows and DOWN in drawdown (auto-de-risk). Falls back to
    ``fixed_fallback_usd`` when equity or the fraction is unavailable (never size
    against unknown equity). A 0 / non-positive fixed cap is a deliberate operator
    disable/block and is preserved. docs/DESIGN/MOMENTUM_LANE.md
    """
    fixed = float(fixed_fallback_usd)
    if fixed <= 0:
        return fixed
    try:
        frac = float(fraction or 0.0)
    except (TypeError, ValueError):
        frac = 0.0
    if frac <= 0 or not math.isfinite(frac):
        return fixed
    eq = _account_equity_usd(execution_family, prefer_equity=prefer_equity)
    if eq is None or eq <= 0:
        return fixed
    return round(eq * frac, 2)


def equity_relative_notional_cap(fixed_fallback_usd: float, execution_family: str | None = None) -> float:
    """Per-trade NOTIONAL cap as a fraction of account equity (documented
    per-trade SIZE knob). docs/DESIGN/MOMENTUM_LANE.md"""
    return _equity_relative_cap(
        fixed_fallback_usd,
        getattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15),
        execution_family,
    )


def equity_relative_loss_cap(fixed_fallback_usd: float, execution_family: str | None = None) -> float:
    """Per-trade MAX-LOSS cap as a fraction of account equity (documented
    per-trade RISK knob). docs/DESIGN/MOMENTUM_LANE.md"""
    return _equity_relative_cap(
        fixed_fallback_usd,
        getattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01),
        execution_family,
    )


def equity_relative_daily_loss_cap(fixed_fallback_usd: float, execution_family: str | None = None) -> float:
    """Daily-loss cap as a fraction of account equity (documented DAILY risk knob).
    Evaluated live so the daily circuit-breaker adapts to current equity.
    docs/DESIGN/MOMENTUM_LANE.md"""
    return _equity_relative_cap(
        fixed_fallback_usd,
        getattr(settings, "chili_momentum_risk_daily_loss_fraction_of_equity", 0.05),
        execution_family,
        prefer_equity=True,  # daily-loss cap off STABLE account equity, not fluctuating BP
    )


def adaptive_max_concurrent_live_sessions() -> int:
    """Live-session concurrency cap = the simultaneous-open-risk BUDGET RATIO, bounded.
    N = clamp(round(frac / loss_fraction), base, 15): with the per-trade risk evaluated
    equity-relative (eq * loss_fraction), N = chili_momentum_risk_concurrent_open_risk_fraction
    / chili_momentum_risk_loss_fraction_of_equity — INDEPENDENT of account size/margin, so
    growing equity/buying-power scales per-trade SIZE, not the slot COUNT. Worst-case
    simultaneous loss across N sessions <= frac * basis. Falls back to the fixed base
    (``max_concurrent_live_sessions``) when the account is unavailable. Basis read per-venue:
    Coinbase when crypto-only, else Robinhood (the equity lane). docs/DESIGN/MOMENTUM_LANE.md"""
    base = max(1, int(getattr(settings, "chili_momentum_risk_max_concurrent_live_sessions", 5) or 5))
    try:
        frac = float(getattr(settings, "chili_momentum_risk_concurrent_open_risk_fraction", 0.05) or 0.0)
    except (TypeError, ValueError):
        frac = 0.0
    if frac <= 0 or not math.isfinite(frac):
        return base
    try:
        per_trade = float(getattr(settings, "chili_momentum_risk_max_loss_per_trade_usd", 50.0) or 50.0)
    except (TypeError, ValueError):
        per_trade = 50.0
    if per_trade <= 0:
        return base
    from ..execution_family_registry import (
        EXECUTION_FAMILY_COINBASE_SPOT,
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
    )
    ef = (
        EXECUTION_FAMILY_COINBASE_SPOT
        if bool(getattr(settings, "chili_momentum_auto_arm_crypto_only", True))
        else EXECUTION_FAMILY_ROBINHOOD_SPOT
    )
    eq = _account_equity_usd(ef)
    if not eq or eq <= 0:
        return base
    # Use the ACTUAL equity-relative per-trade risk (eq * loss_fraction), NOT the fixed $
    # cap, as the denominator — so N is the simultaneous-open-risk budget RATIO
    # (frac / loss_fraction), INDEPENDENT of account size/margin. Account/margin growth
    # scales the per-trade SIZE, not the COUNT: a 2x buying-power basis must NOT also double
    # the slot count (that would 4x simultaneous risk). 15 is a hard guardrail ceiling.
    risk = equity_relative_loss_cap(per_trade, ef)
    if not risk or risk <= 0:
        return base
    return max(base, min(15, int(round(eq * frac / risk))))


def effective_position_cap(*, crypto: bool = False) -> int:
    """OPEN-POSITION cap for decouple_watching. The adaptive risk-budget N binds
    first (≤15); the fixed ``chili_momentum_risk_max_concurrent_positions`` (5) is
    the fallback floor; the operator's ``chili_momentum_max_open_positions_ceiling``
    (20) is a hard backstop that only catches a misconfigured fraction (reference
    numbers are ceilings, not the active value — [[feedback_adaptive_no_magic]]).
    The crypto-specific bound is the super-bucket sub-cap enforced atomically at
    the fill boundary, so ``crypto`` does not change the gross cap here."""
    adaptive_n = adaptive_max_concurrent_live_sessions()
    try:
        pos_floor = int(getattr(settings, "chili_momentum_risk_max_concurrent_positions", 5) or 5)
    except (TypeError, ValueError):
        pos_floor = 5
    try:
        ceiling = int(getattr(settings, "chili_momentum_max_open_positions_ceiling", 20) or 20)
    except (TypeError, ValueError):
        ceiling = 20
    return max(1, min(max(adaptive_n, pos_floor), ceiling))


def streak_risk_multiplier(db, *, execution_family: str | None = None) -> tuple[float, dict]:
    """Streak-adaptive risk dial (Ross: 'coming out of the gates swinging' on a
    hot streak; 'size down' when cold). A multiplier on the per-trade max loss
    derived from the lane's OWN recent closed LIVE outcomes -- self-relative, no
    market magic numbers; only the bounds are fixed and documented:

      mult = clamp(0.5 + recent_win_rate, 0.5, 1.5)   # 50% wins -> 1.0 neutral
      >=3 consecutive losses -> hard floor 0.5         # Ross's stop-digging rule
      <5 closed outcomes      -> 1.0                   # not enough evidence

    The window is the last 10 REAL ENTERED trades in THE SAME lane:
      * execution_family (when given) segregates the lane -- without it the window
        mixed crypto (Coinbase), equity (Robinhood) AND paper-soak twins (Alpaca)
        into one dial, so a crypto loss spuriously de-risked the equity lane.
      * is_real_entry_outcome() drops never-entered rows -- a $0.00
        cancelled_pre_entry (realized=0.0, NOT NULL) was being miscounted as a loss
        and inflating the consecutive-loss run. Entered-then-force-closed losses
        (stop_loss, bailout, stale_data_abort, governance_exit, ...) still count.

    Bounds/formula are UNCHANGED; only the input set is corrected. execution_family
    defaults to None for byte-identical legacy behaviour. The daily-loss cap and
    drawdown breaker still bound everything above this. Fail-neutral (returns 1.0)."""
    try:
        from ....models.trading import MomentumAutomationOutcome
        from .outcome_labels import is_real_entry_outcome

        q = db.query(
            MomentumAutomationOutcome.realized_pnl_usd,
            MomentumAutomationOutcome.outcome_class,
        ).filter(
            MomentumAutomationOutcome.mode == "live",
            MomentumAutomationOutcome.realized_pnl_usd.isnot(None),
        )
        if execution_family:
            q = q.filter(MomentumAutomationOutcome.execution_family == execution_family)
        # Fetch headroom (NOT a risk parameter): pull more than the 10-window so the
        # post-filter prune of never-entered rows still yields the newest 10 REAL
        # entries; the verified deepest real entry within the non-null set sits well
        # inside this cap. Bounded + indexed (mode, terminal_at desc).
        raw = q.order_by(MomentumAutomationOutcome.terminal_at.desc()).limit(40).all()
        pnls = [float(p) for (p, oc) in raw if is_real_entry_outcome(oc)][:10]
        if len(pnls) < 5:
            return 1.0, {"streak_mult": 1.0, "reason": "insufficient_history", "n": len(pnls)}
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
        consec_losses = 0
        for p in pnls:  # newest first
            if p <= 0:
                consec_losses += 1
            else:
                break
        mult = max(0.5, min(1.5, 0.5 + win_rate))
        if consec_losses >= 3:
            mult = 0.5
        return mult, {
            "streak_mult": round(mult, 2), "win_rate": round(win_rate, 2),
            "consecutive_losses": consec_losses, "n": len(pnls),
        }
    except Exception:
        return 1.0, {"streak_mult": 1.0, "reason": "error_fail_neutral"}


def cushion_risk_multiplier(db, *, base_loss_usd: float) -> tuple[float, dict]:
    """Ross's day-cushion risk ladder (2026-06-11 recap video: "I am NOT taking
    full risk until I first have a cushion on the day" — his −$17k FGL stop-out
    landed on a +$65k banked cushion, so the day stayed well green).

      mult = clamp(0.5 + 0.5 * cushion / base_loss, 0.5, 2.0)

      no banked day P&L   -> 1.0  (full base risk — first triggers are the
                                   highest-EV pool; floor raised from 0.5 on
                                   2026-06-12 quant-pass-v2 replay evidence)
      cushion = 1x base   -> 1.0  (ladder begins climbing past 1x cushion)
      cushion >= 3x base  -> 2.0  (aggression ceiling)

    Green guarantee by construction: a max-risk stop-out gives back at most
    0.5*cushion + 0.5*base, so with >= 1x base of cushion the day stays green.
    Self-relative (cushion measured in units of the CURRENT equity-relative
    per-trade loss — scales with the account, no fixed dollars); only the
    bounds are fixed and documented. Composes with streak_risk_multiplier
    (streak = multi-day form; cushion = today's ladder). Daily-loss cap and
    drawdown breaker still bound everything above this. Fail-neutral 1.0."""
    try:
        from ..governance import global_realized_pnl_today_et

        day = global_realized_pnl_today_et(db)
        realized = float(day.get("total_usd") or 0.0)
        cushion = max(0.0, realized)
        base = float(base_loss_usd or 0.0)
        if base <= 0:
            return 1.0, {"cushion_mult": 1.0, "reason": "no_base_loss"}
        # Floor raised 0.5 -> 1.0 (2026-06-12 quant pass v2, +$1,015/3d
        # replay-validated): FIRST triggers are the highest-EV pool (+1.45R) —
        # the half-size start was a stealth de-risk of the day's best trades.
        # The daily-loss cap + drawdown breaker remain the downside bound;
        # the ladder still EARNS the climb to 2x from banked cushion.
        mult = max(1.0, min(2.0, 0.5 + 0.5 * (cushion / base)))
        return mult, {
            "cushion_mult": round(mult, 2),
            "day_realized_usd": round(realized, 2),
            "cushion_usd": round(cushion, 2),
            "base_loss_usd": round(base, 2),
        }
    except Exception:
        return 1.0, {"cushion_mult": 1.0, "reason": "error_fail_neutral"}


def compute_risk_first_quantity(
    *,
    entry_price: float,
    atr_pct: float,
    max_loss_usd: float,
    max_notional_ceiling_usd: float,
    base_increment: float | None = None,
    base_min_size: float | None = None,
    stop_atr_mult: float = 0.60,
) -> tuple[float, dict[str, Any]]:
    """Risk-first sizing (Ross-style): qty = max_loss_usd / stop_distance, capped at
    the notional ceiling.

    A TIGHTER stop buys MORE size at constant risk (Ross's core sizing edge) — vs
    notional-first where stop distance doesn't drive size. Stop distance uses the
    same ATR formula as ``stop_target_prices`` (max(0.003, atr_pct x stop_atr_mult)).
    Returns ``(qty, meta)``; qty=0 with a ``reason`` when inputs are unusable.
    docs/DESIGN/MOMENTUM_LANE.md
    """
    e = float(entry_price or 0.0)
    if e <= 0 or not math.isfinite(e):
        return 0.0, {"reason": "invalid_entry"}
    loss = float(max_loss_usd or 0.0)
    if loss <= 0 or not math.isfinite(loss):
        return 0.0, {"reason": "max_loss_nonpositive"}
    stop_pct = max(0.003, float(atr_pct or 0.0) * float(stop_atr_mult or 0.60))
    stop_distance = e * stop_pct
    if stop_distance <= 0 or not math.isfinite(stop_distance):
        return 0.0, {"reason": "stop_distance_invalid"}
    qty = loss / stop_distance
    capped_by = None
    ceiling = float(max_notional_ceiling_usd or 0.0)
    if ceiling > 0 and qty * e > ceiling:
        qty = ceiling / e
        capped_by = "notional_ceiling"
    inc = float(base_increment) if base_increment and base_increment > 0 else None
    if inc:
        qty = math.floor(qty / inc) * inc
    mn = float(base_min_size) if base_min_size and base_min_size > 0 else None
    if mn and qty < mn:
        return 0.0, {"reason": "below_min_size", "stop_distance": round(stop_distance, 8)}
    return float(qty), {
        "model": "risk_first",
        "stop_distance": round(stop_distance, 8),
        "risk_usd": round(loss, 2),
        "notional_usd": round(qty * e, 2),
        "capped_by": capped_by,
    }


def max_loss_circuit_decision(
    *,
    avg: float,
    qty: float,
    stop_distance: float,
    bid: float | None,
    k: float,
    risk_anchor_usd: float | None = None,
) -> dict[str, Any]:
    """Hard max-loss-per-trade circuit (pure, zero-I/O, unit-testable).

    The threshold basis is the REALIZED STRUCTURAL RISK = ``stop_distance * qty``
    (the per-share structural stop distance frozen in the position's entry sizing),
    NOT the frozen ``risk_usd`` budget — verified live, ``risk_usd``=$19.30 vs
    structural=$1.61, a 12x overstatement that would let a $38 hole open on a
    $1.61-stop name. The flatten anchor ``floor_price = avg - k*stop_distance`` is an
    ABSOLUTE loss floor (not a falling-bid ladder), so a deep gap-through fill is
    mechanically impossible.

    FAIL-CLOSED-SAFE: any unusable basis (non-positive/non-finite stop_distance, qty,
    avg, or bid; bid None) returns ``breach=False`` with ``reason='insufficient_basis'``
    — the circuit NEVER fires on bad basis.

    Returns a dict: ``breach`` (bool), ``structural_risk_usd``, ``threshold_usd``,
    ``unrealized_pnl``, ``floor_price``, ``reason``.
    """
    a = float(avg or 0.0)
    q = float(qty or 0.0)
    sd = float(stop_distance or 0.0)
    kk = float(k or 0.0)
    b = None
    try:
        b = float(bid) if bid is not None else None
    except (TypeError, ValueError):
        b = None
    if (
        sd <= 0
        or not math.isfinite(sd)
        or q <= 0
        or not math.isfinite(q)
        or a <= 0
        or not math.isfinite(a)
        or b is None
        or not math.isfinite(b)
        or b <= 0
    ):
        return {
            "breach": False,
            "structural_risk_usd": None,
            "threshold_usd": None,
            "unrealized_pnl": None,
            "floor_price": None,
            "reason": "insufficient_basis",
        }
    structural_risk_usd = sd * q
    threshold_usd = kk * structural_risk_usd
    # GUARD #1 (risk-neutral pyramid): when a frozen risk anchor is supplied (the
    # STARTER's original structural risk R0), the circuit threshold may only TIGHTEN
    # to it — never sit above R0. This keeps an ENLARGED (pyramided) position's
    # worst-case realized loss <= the starter's original risk, since the #769 floor
    # would otherwise re-base on the bigger qty (k*sd*q1 ~ 3-4.5x R0). A TIGHTEN of
    # the circuit, never a weaken (Hard-Rule compliant). None => byte-identical legacy
    # (threshold_usd/q == k*sd, so floor_price == a - k*sd exactly as before).
    try:
        _anchor = float(risk_anchor_usd) if risk_anchor_usd is not None else None
    except (TypeError, ValueError):
        _anchor = None
    if _anchor is not None and math.isfinite(_anchor) and _anchor > 0:
        threshold_usd = min(threshold_usd, _anchor)
    unrealized_pnl = (b - a) * q
    floor_price = a - threshold_usd / q
    breach = unrealized_pnl <= -threshold_usd
    return {
        "breach": bool(breach),
        "structural_risk_usd": structural_risk_usd,
        "threshold_usd": threshold_usd,
        "unrealized_pnl": unrealized_pnl,
        "floor_price": floor_price,
        "reason": "max_loss_circuit_breach" if breach else "within_threshold",
    }


def liquidity_capped_notional(
    equity_notional_cap: float, dollar_volume: float | None, *, fraction: float | None = None
) -> float:
    """Cap the per-trade notional at a fraction of the NAME's dollar-volume, so the position
    never exceeds what can be EXITED cleanly (Ross's "you can't move 500,000 shares in 1-2
    minutes" rule).

    As the account COMPOUNDS, the equity-relative cap grows — but this liquidity cap binds on
    THIN names, so CHILI scales up only as far as each name's liquidity allows instead of
    outgrowing the small-cap universe. Without it, a 15%-of-$1M notional = $150k = ~30,000
    shares of a thin $5 low-float that cannot be exited on a stop-out (the thin-book sweep /
    0-fills root cause). At a small account the equity cap binds (unchanged behavior); as the
    account grows the LIQUIDITY cap binds on thin names. The participation fraction is the ONE
    documented knob (~1% of daily $-volume ~= a few minutes of an active name's exitable
    volume). Fail-OPEN: returns the equity cap unchanged when the dollar-volume is unavailable
    or the fraction is disabled (<=0). Pure + side-effect-free. (docs/DESIGN/SCALING_ENGINE.md)
    """
    cap = float(equity_notional_cap or 0.0)
    if cap <= 0:
        return cap
    try:
        dv = float(dollar_volume or 0.0)
    except (TypeError, ValueError):
        return cap
    if dv <= 0 or not math.isfinite(dv):
        return cap  # no liquidity data -> fail open (unchanged)
    if fraction is None:
        try:
            fraction = float(getattr(settings, "chili_momentum_risk_liquidity_participation_fraction", 0.01) or 0.0)
        except (TypeError, ValueError):
            fraction = 0.0
    try:
        frac = float(fraction or 0.0)
    except (TypeError, ValueError):
        return cap
    if frac <= 0 or not math.isfinite(frac):
        return cap  # disabled -> no liquidity cap
    liq_cap = frac * dv
    return min(cap, liq_cap) if liq_cap > 0 else cap


@dataclass(frozen=True)
class MomentumAutomationRiskPolicy:
    """Conservative defaults for short-horizon crypto momentum (pre-runner gates)."""

    execution_family_default: str = EXECUTION_FAMILY_COINBASE_SPOT
    mode_scope: str = "both"  # paper | live | both (informational)
    max_daily_loss_usd: float = 250.0
    max_loss_per_trade_usd: float = 50.0
    max_concurrent_sessions: int = 10
    max_concurrent_live_sessions: int = 5
    max_concurrent_positions: int = 5
    max_notional_per_trade_usd: float = 500.0
    max_position_size_base: float = 1_000_000.0
    max_spread_bps_paper: float = 28.0
    max_spread_bps_live: float = 12.0
    # Adaptive spread tolerance. The BBO/quote spread is a round-trip cost, so we
    # gate it RELATIVE to how far the instrument actually moves (its realized 15m
    # volatility), never below the live floor above. This single documented knob is
    # the max spread as a fraction of that expected per-bar move (0.5 => the spread
    # may be at most half a typical bar's range). Lets Ross-style explosive names
    # (wide absolute spread, tiny vs. their move) trade without a magic fixed cap.
    spread_to_expected_move_ratio: float = 0.5
    # Absolute spread cap (Ross "skip if the spread is too wide") — the adaptive
    # tolerance never exceeds this, blocking the catastrophic-cost wide-spread entry.
    max_spread_bps_abs_cap: float = 300.0
    max_estimated_slippage_bps: float = 18.0
    max_fee_to_target_ratio: float = 0.35
    max_hold_seconds: int = 86_400
    cooldown_after_stopout_seconds: int = 300
    cooldown_after_cancel_seconds: int = 60
    viability_max_age_seconds: float = 600.0
    stale_market_data_max_age_sec: float = 30.0
    require_live_eligible_for_live: bool = True
    require_fresh_viability: bool = True
    require_strict_coinbase_freshness: bool = False
    disable_live_if_governance_inhibit: bool = True
    block_paper_when_kill_switch: bool = False
    auto_expire_pending_live_arm_seconds: float = 900.0

    @classmethod
    def from_settings(cls) -> MomentumAutomationRiskPolicy:
        s = settings
        return cls(
            max_daily_loss_usd=float(getattr(s, "chili_momentum_risk_max_daily_loss_usd", 250.0)),
            max_loss_per_trade_usd=float(getattr(s, "chili_momentum_risk_max_loss_per_trade_usd", 50.0)),
            max_concurrent_sessions=int(getattr(s, "chili_momentum_risk_max_concurrent_sessions", 10)),
            max_concurrent_live_sessions=adaptive_max_concurrent_live_sessions(),
            max_concurrent_positions=int(getattr(s, "chili_momentum_risk_max_concurrent_positions", 5)),
            max_notional_per_trade_usd=float(getattr(s, "chili_momentum_risk_max_notional_per_trade_usd", 500.0)),
            max_position_size_base=float(getattr(s, "chili_momentum_risk_max_position_size_base", 1_000_000.0)),
            max_spread_bps_paper=float(getattr(s, "chili_momentum_risk_max_spread_bps_paper", 28.0)),
            max_spread_bps_live=float(getattr(s, "chili_momentum_risk_max_spread_bps_live", 12.0)),
            spread_to_expected_move_ratio=float(
                getattr(s, "chili_momentum_risk_spread_to_expected_move_ratio", 0.5)
            ),
            max_spread_bps_abs_cap=float(
                getattr(s, "chili_momentum_risk_max_spread_bps_abs_cap", 300.0)
            ),
            max_estimated_slippage_bps=float(getattr(s, "chili_momentum_risk_max_estimated_slippage_bps", 18.0)),
            max_fee_to_target_ratio=float(getattr(s, "chili_momentum_risk_max_fee_to_target_ratio", 0.35)),
            max_hold_seconds=int(getattr(s, "chili_momentum_risk_max_hold_seconds", 86_400)),
            cooldown_after_stopout_seconds=int(getattr(s, "chili_momentum_risk_cooldown_after_stopout_seconds", 300)),
            cooldown_after_cancel_seconds=int(getattr(s, "chili_momentum_risk_cooldown_after_cancel_seconds", 60)),
            viability_max_age_seconds=float(getattr(s, "chili_momentum_risk_viability_max_age_seconds", 600.0)),
            stale_market_data_max_age_sec=float(
                getattr(s, "chili_momentum_risk_stale_market_data_max_age_sec", 30.0)
            ),
            require_live_eligible_for_live=bool(getattr(s, "chili_momentum_risk_require_live_eligible", True)),
            require_fresh_viability=bool(getattr(s, "chili_momentum_risk_require_fresh_viability", True)),
            require_strict_coinbase_freshness=bool(
                getattr(s, "chili_momentum_risk_require_strict_coinbase_freshness", False)
            ),
            disable_live_if_governance_inhibit=bool(
                getattr(s, "chili_momentum_risk_disable_live_if_governance_inhibit", True)
            ),
            block_paper_when_kill_switch=bool(getattr(s, "chili_momentum_risk_block_paper_when_kill_switch", False)),
            auto_expire_pending_live_arm_seconds=float(
                getattr(s, "chili_momentum_risk_auto_expire_pending_live_arm_seconds", 900.0)
            ),
        )


def resolve_effective_risk_policy() -> dict[str, Any]:
    """Full policy as JSON-safe dict (for snapshots and read APIs)."""
    p = MomentumAutomationRiskPolicy.from_settings()
    d = asdict(p)
    d["policy_version"] = POLICY_VERSION
    d["resolved_at_utc"] = datetime.now(timezone.utc).isoformat()
    return d


def effective_policy_summary() -> dict[str, Any]:
    """Compact summary for UI / automation strip."""
    p = MomentumAutomationRiskPolicy.from_settings()
    return {
        "policy_version": POLICY_VERSION,
        "max_concurrent_sessions": p.max_concurrent_sessions,
        "max_concurrent_live_sessions": p.max_concurrent_live_sessions,
        "max_spread_bps_paper": p.max_spread_bps_paper,
        "max_spread_bps_live": p.max_spread_bps_live,
        "max_estimated_slippage_bps": p.max_estimated_slippage_bps,
        "max_fee_to_target_ratio": p.max_fee_to_target_ratio,
        "viability_max_age_seconds": p.viability_max_age_seconds,
        "disable_live_if_governance_inhibit": p.disable_live_if_governance_inhibit,
    }


def _recent_frozen_per_trade_caps(
    db: Any, *, execution_family: str | None, lookback: int
) -> dict[str, list[float]]:
    """Recent FROZEN per-trade caps for the same venue (rolling-median spike-guard
    input). Best-effort, read-only: any failure returns empty lists so the caller
    simply skips clamping — a history-read error never blocks an admission."""
    out: dict[str, list[float]] = {k: [] for k in _PER_TRADE_CAP_KEYS}
    if db is None or lookback <= 0:
        return out
    try:
        from ....models.trading import TradingAutomationSession

        q = db.query(TradingAutomationSession.risk_snapshot_json)
        if execution_family:
            q = q.filter(TradingAutomationSession.execution_family == execution_family)
        rows = q.order_by(TradingAutomationSession.id.desc()).limit(int(lookback)).all()
    except Exception:
        logger.debug("[momentum_neural] rolling-median cap history read failed", exc_info=True)
        return out
    for (row_snap,) in rows:
        caps = row_snap.get("momentum_policy_caps") if isinstance(row_snap, dict) else None
        if not isinstance(caps, dict):
            continue
        for key in _PER_TRADE_CAP_KEYS:
            try:
                fv = float(caps.get(key))
            except (TypeError, ValueError):
                continue
            if math.isfinite(fv) and fv > 0:
                out[key].append(fv)
    return out


def bounded_by_rolling_median(
    raw_cap: float,
    recent_caps: list[float],
    *,
    multiple: float,
    min_history: int = _CAP_MEDIAN_MIN_HISTORY,
) -> tuple[float, dict[str, Any]]:
    """Clamp a per-trade cap DOWN to ``multiple x rolling_median`` of recent caps.

    Stops a transient spiked equity read from inflating the cap (and, via the shared
    notional ceiling, position size + risk). Only ever clamps DOWNWARD; legitimate
    equity growth trails the median so the bound rises with it and is not clamped. A
    non-positive raw cap (a deliberate operator disable/block) is preserved. Below
    ``min_history`` samples the median is untrusted and the raw cap passes through.
    Pure (no I/O); returns ``(value, derivation)``.
    docs/DESIGN/MOMENTUM_LANE_ENTRY_STOP_REALIGNMENT.md
    """
    raw = float(raw_cap)
    deriv: dict[str, Any] = {"raw": round(raw, 4), "n": len(recent_caps), "clamped": False}
    if raw <= 0 or not math.isfinite(raw):
        deriv["reason"] = "nonpositive_or_disabled"
        return raw, deriv
    try:
        mult = float(multiple)
    except (TypeError, ValueError):
        mult = 1.0
    if not math.isfinite(mult) or mult < 1.0:
        mult = 1.0
    deriv["multiple"] = round(mult, 4)
    if len(recent_caps) < int(min_history):
        deriv["reason"] = "thin_history"
        return raw, deriv
    median = float(statistics.median(recent_caps))
    deriv["median"] = round(median, 4)
    if median <= 0:
        deriv["reason"] = "nonpositive_median"
        return raw, deriv
    bound = mult * median
    deriv["bound"] = round(bound, 4)
    if raw > bound:
        deriv["clamped"] = True
        return round(bound, 2), deriv
    deriv["reason"] = "within_bound"
    return raw, deriv


def build_session_risk_snapshot(
    *,
    policy_full: dict[str, Any],
    evaluation: dict[str, Any],
    viability_brief: dict[str, Any] | None,
    readiness_subset: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
    execution_family: str | None = None,
    db: Any = None,
) -> dict[str, Any]:
    """Merge operator keys (e.g. arm_token) with frozen policy + evaluation.

    When ``db`` is supplied the two equity-relative per-trade caps are passed through
    the rolling-median spike guard (``bounded_by_rolling_median``) before freezing, so
    a transient bad equity read cannot 4-6x size + risk for the life of the session."""
    snap: dict[str, Any] = dict(extra or {})
    snap[POLICY_SNAPSHOT_KEY] = effective_policy_summary()
    snap["momentum_risk_policy_resolved_utc"] = policy_full.get("resolved_at_utc")
    snap[RISK_SNAPSHOT_KEY] = {
        "policy_version": POLICY_VERSION,
        "evaluated_at_utc": evaluation.get("evaluated_at_utc"),
        "allowed": evaluation.get("allowed"),
        "severity": evaluation.get("severity"),
        "checks": evaluation.get("checks", []),
        "warnings": evaluation.get("warnings", []),
        "errors": evaluation.get("errors", []),
        "governance_state": evaluation.get("governance_state"),
        "freshness_state": evaluation.get("freshness_state"),
        "viability_state": evaluation.get("viability_state"),
    }
    if viability_brief is not None:
        snap["viability_brief"] = viability_brief
    if readiness_subset is not None:
        snap["execution_readiness_subset"] = readiness_subset
    # Frozen caps for runner enforcement (Phase 7+); do not overwrite after admission.
    snap["momentum_policy_caps"] = {
        "max_hold_seconds": int(policy_full.get("max_hold_seconds") or 86_400),
        "cooldown_after_stopout_seconds": policy_int_cap(policy_full, "cooldown_after_stopout_seconds", 300),
        # Equity-relative per-trade notional (no fixed-$ magic): a fraction of
        # account equity, frozen at admission; falls back to the fixed cap when
        # equity is unavailable. [[feedback_adaptive_no_magic]]
        "max_notional_per_trade_usd": equity_relative_notional_cap(
            policy_float_cap(policy_full, "max_notional_per_trade_usd", 500.0),
            execution_family,
        ),
        # Equity-relative per-trade max-loss (no fixed-$ magic); same fallback rules.
        "max_loss_per_trade_usd": equity_relative_loss_cap(
            policy_float_cap(policy_full, "max_loss_per_trade_usd", 50.0),
            execution_family,
        ),
    }
    # Rolling-median spike guard: a transient bad per-venue equity read inflates BOTH
    # per-trade caps at once (they share the equity input), releasing the notional
    # ceiling and 4-6x-ing size + risk. Clamp each frozen cap DOWN to a bounded
    # multiple of its rolling median across recent same-venue admissions, and persist
    # the derivation for audit. Read-only/best-effort: only active when a db is
    # supplied; history-read failure leaves caps unclamped (never blocks admission).
    if db is not None:
        caps = snap["momentum_policy_caps"]
        multiple = float(getattr(settings, "chili_momentum_risk_cap_max_median_multiple", 2.0) or 2.0)
        lookback = int(getattr(settings, "chili_momentum_risk_cap_median_lookback", 40) or 40)
        history = _recent_frozen_per_trade_caps(db, execution_family=execution_family, lookback=lookback)
        derivation: dict[str, Any] = {}
        for key in _PER_TRADE_CAP_KEYS:
            bounded, d = bounded_by_rolling_median(caps[key], history.get(key, []), multiple=multiple)
            d["execution_family"] = execution_family
            caps[key] = bounded
            derivation[key] = d
        snap["momentum_policy_caps_derivation"] = derivation
        clamped = {k: derivation[k] for k in _PER_TRADE_CAP_KEYS if derivation[k].get("clamped")}
        logger.info(
            "[momentum_neural] per-trade cap derivation venue=%s clamped=%s detail=%s",
            execution_family, list(clamped.keys()) or None, derivation,
        )
        for k, d in clamped.items():
            logger.warning(
                "[momentum_neural] per-trade cap spike CLAMPED key=%s raw=%.4f -> %.4f "
                "(median=%.4f x%.2f n=%d venue=%s)",
                k, d["raw"], caps[k], d.get("median", 0.0), d.get("multiple", 0.0),
                d.get("n", 0), execution_family,
            )
    return snap
