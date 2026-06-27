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


# ── LAST-GOOD account-equity guard (FIX: spurious daily-loss-cap collapse) ───────────
# _account_equity_usd does a LIVE broker portfolio read on every cap evaluation. Robinhood
# reads are FLAKY (phoenix.robinhood.com SSL handshake failures fall back to api.robinhood.com,
# which can return a tiny/partial equity, or the lane family can momentarily resolve to a
# near-empty account). When the basis collapses to ~$20, 5% collapses to ~$1 and any small
# realized loss (-$44) trips a SPURIOUS daily-loss HALT (the recurring "$1 cap" bug, same class
# as the 06-15 Coinbase-basis freeze). The existing rolling-median spike guard
# (bounded_by_rolling_median) only catches HIGH spikes (inflation); there was no LOW/failed-read
# guard. This module-level cache, keyed by execution_family, holds the LAST REAL POSITIVE read.
# On a None/0/implausibly-tiny live read we reuse the last-good value for a SHORT grace window
# instead of collapsing the cap.
#
# SAFETY (load-bearing — do NOT let this mask a real drawdown):
#   * The cache is updated ONLY from a successful positive live read. It is the LAST REAL READ,
#     never an invented floor — it can NEVER inflate the cap above what the account actually had.
#   * It is used ONLY when the live read is missing/degraded; a normal read always wins, so a
#     genuine sustained drawdown lowers the cap on the very next good read (and fully expires
#     within the grace window).
#   * Past the grace window the cache is discarded and the caller falls back to the documented
#     fixed cap — a persistent broker outage is NOT hidden indefinitely.
#   * The "implausibly tiny" guard fires ONLY when a fresh read is < _ACCOUNT_EQUITY_TINY_FRAC of
#     a still-fresh last-good (the legacy-account bleed-through case); a true ~90%+ drawdown
#     within one TTL is rare, and even then the next-tick good read corrects it.
_ACCOUNT_EQUITY_LAST_GOOD: dict[str, dict[str, float]] = {}
_ACCOUNT_EQUITY_LAST_GOOD_TTL_SEC = 180.0  # reuse last-good across transient read misses (~3min)
_ACCOUNT_EQUITY_TINY_FRAC = 0.10  # a live read < 10% of a fresh last-good == implausible flake


def _stabilize_account_equity(ef: str, eq: float | None) -> float | None:
    """LOW/failed-read stabilizer for the per-family account-equity basis.

    Returns the live ``eq`` when it is a plausible positive value (and refreshes the
    last-good cache). When the live read is None/0/implausibly-tiny, returns the last-good
    cached value if it is within the short grace TTL, else None (caller -> fixed fallback).
    Never inflates above a real read; see the cache docstring above for the safety contract."""
    import time as _time

    now = _time.monotonic()
    slot = _ACCOUNT_EQUITY_LAST_GOOD.get(ef)
    cached = float(slot["value"]) if slot else 0.0
    age = (now - float(slot["ts"])) if slot else 1e9

    live_ok = eq is not None and eq > 0
    # "Implausibly tiny" = a fresh read that is a tiny fraction of a STILL-FRESH last-good
    # (the near-empty legacy account bleeding through an RH fallback read). Treat as a flake.
    tiny_flake = (
        live_ok
        and cached > 0
        and age < _ACCOUNT_EQUITY_LAST_GOOD_TTL_SEC
        and float(eq) < cached * _ACCOUNT_EQUITY_TINY_FRAC
    )

    if live_ok and not tiny_flake:
        _ACCOUNT_EQUITY_LAST_GOOD[ef] = {"value": float(eq), "ts": now}
        return float(eq)

    # Degraded/failed/tiny read -> reuse the last REAL read within the grace window.
    if cached > 0 and age < _ACCOUNT_EQUITY_LAST_GOOD_TTL_SEC:
        logger.warning(
            "[momentum_neural] account-equity read DEGRADED for %s (live=%s) — reusing last-good "
            "$%.2f (age=%.0fs, ttl=%.0fs) to avoid a spurious daily-loss-cap collapse",
            ef, eq, cached, age, _ACCOUNT_EQUITY_LAST_GOOD_TTL_SEC,
        )
        return cached
    return None


def _account_equity_usd(
    execution_family: str | None = None, *, apply_margin_multiple: bool = True,
    prefer_equity: bool = False, prefer_cash_value: bool = False,
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

    prefer_cash_value (operator 2026-06-25) returns the account CASH VALUE / total
    equity REGARDLESS of the buying-power flag — the basis the per-broker daily-loss cap
    now uses (a 5% cap off the $13.6k agentic CASH value, not off margin-inflated BP).
    For robinhood_agentic_mcp the cash value is the stable total account value
    (_agentic_equity_cached); for robinhood_spot / coinbase it is pf["equity"]. Routes
    through the last-good stabilizer so a flaky read cannot collapse the cap to ~$1
    (the documented failure mode, lines 264-266). Implies prefer_equity semantics
    (stabilized, never margin-inflated). docs/DESIGN/MOMENTUM_LANE.md
    """
    from ..execution_family_registry import (
        EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
        normalize_execution_family,
    )

    ef = normalize_execution_family(execution_family)
    # prefer_cash_value forces the stabilized total-equity path (never BP, never margin)
    # for the daily-loss RISK cap; it implies prefer_equity's last-good stabilization.
    use_bp = bool(getattr(settings, "chili_momentum_risk_size_use_buying_power", True))
    if prefer_cash_value:
        use_bp = False
        prefer_equity = True

    # Agentic MCP rail: the isolated agentic account is a CASH account — its reported
    # buying_power IS the real, unleveraged spendable amount (no margin). Size against
    # it DIRECTLY with NO margin multiple (the 2x multiple exists only to recover
    # robin_stocks' under-reporting on the MARGIN main account; the MCP reports true BP).
    # Applying 2x here would submit orders exceeding the cash balance -> RH rejects.
    if ef == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP:
        # Cash account: reported BP IS the real spendable (NO margin multiple). Cached
        # (short TTL) so a burst of candidate sizings reuses one read. prefer_equity (the
        # daily-loss RISK cap) reads the STABLE total account value instead of fluctuating BP.
        # prefer_equity reads route through the last-good stabilizer so a transient RH-MCP
        # read miss (SSL flake / partial response) reuses the last real equity instead of
        # collapsing the daily-loss cap to ~$1. (Sizing/BP keeps its own short-TTL cache.)
        if prefer_equity:
            eq = _agentic_equity_cached()
            stable = _stabilize_account_equity(ef, eq)
            if stable is not None and stable > 0:
                return float(stable)
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
                # RISK-cap reads (prefer_equity, unlevered) get the last-good LOW guard so a
                # flaky RH portfolio read can't collapse the daily-loss cap. SIZING reads
                # (apply_margin_multiple=True) keep raw fail-to-None behaviour (never size
                # against a stale basis); the guard is risk-cap-only.
                basis = bp * max(1.0, mult)
                if prefer_equity:
                    return _stabilize_account_equity(ef, basis)
                return basis
        eq = float(pf.get("equity") or 0.0)
        if prefer_equity:
            return _stabilize_account_equity(ef, eq if eq > 0 else None)
        return eq if eq > 0 else None
    except Exception:
        # On a hard read failure the RISK-cap path still tries the last-good cache so a
        # transient broker outage does not collapse the daily-loss cap; sizing fails to None.
        if prefer_equity:
            return _stabilize_account_equity(ef, None)
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


def _et_day_bounds_utc(*, days_ago: int = 0) -> tuple[datetime, datetime]:
    """[start_utc, end_utc) (naive UTC) for the US/Eastern calendar day ``days_ago`` back.

    Mirrors ``governance.global_realized_pnl_today_et``'s ET-session windowing so the
    daily-trade-count budget and the prior-day damper bucket trades on the SAME calendar
    boundary the daily-loss cap uses (no off-by-one between the gate and the breaker)."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    utc = ZoneInfo("UTC")
    # MED-4 fail-SAFE: do the day arithmetic in ET CALENDAR-DATE space, not by subtracting
    # an ABSOLUTE 24h timedelta from a DST-aware datetime. An ET calendar day is 23h/25h
    # across a DST transition, so `now_et.replace(hour=0) - timedelta(days=N)` drifted the
    # window an hour on transition days. Subtract days on the DATE, then build the aware ET
    # midnight from that date via zoneinfo so each [start,end) is a true ET calendar day.
    today_et_date = datetime.now(et).date()
    start_date = today_et_date - _td(days=days_ago)
    end_date = start_date + _td(days=1)
    start_et = _dt(start_date.year, start_date.month, start_date.day, 0, 0, 0, 0, tzinfo=et)
    end_et = _dt(end_date.year, end_date.month, end_date.day, 0, 0, 0, 0, tzinfo=et)
    start_utc = start_et.astimezone(utc).replace(tzinfo=None)
    end_utc = end_et.astimezone(utc).replace(tzinfo=None)
    return start_utc, end_utc


def _count_real_entries_today(db: Any, *, execution_family: str | None) -> int:
    """REAL ENTERED live trades that terminated in today's ET session for THIS lane.

    Read-only, indexed (execution_family, terminal_at). Uses ``is_real_entry_outcome`` so
    the lane's heavy churn of never-entered cancel/no-fill rows (realized_pnl=0.0, NOT NULL)
    is NOT counted as a 'trade' — the budget measures ENTRIES, not arms. Fail-open: any
    error returns 0 (the gate then never blocks)."""
    if db is None or not execution_family:
        return 0
    try:
        from ....models.trading import MomentumAutomationOutcome
        from .outcome_labels import is_real_entry_outcome

        start_utc, end_utc = _et_day_bounds_utc(days_ago=0)
        rows = (
            db.query(MomentumAutomationOutcome.outcome_class)
            .filter(
                MomentumAutomationOutcome.execution_family == execution_family,
                MomentumAutomationOutcome.mode == "live",
                MomentumAutomationOutcome.terminal_at >= start_utc,
                MomentumAutomationOutcome.terminal_at < end_utc,
            )
            .all()
        )
        return sum(1 for (oc,) in rows if is_real_entry_outcome(oc))
    except Exception:
        logger.debug("[momentum_neural] daily entry-count read failed", exc_info=True)
        return 0


def daily_trade_count_budget_decision(
    db: Any,
    *,
    execution_family: str | None,
    open_entry_count: int = 0,
) -> tuple[bool, dict[str, Any]]:
    """ADAPTIVE per-day entry-COUNT budget (SCAL101 '5 trades/day A+ cap', generalized).

    Ross/Max use a fixed 5-trades-a-day rule as a DISCIPLINE FLOOR-reference (don't
    over-trade a quiet tape into churn); we generalize it to a ceiling that FLOATS with
    regime heat + the lane's recent realized expectancy, distinct from the slot/position
    COUNT (that bounds simultaneous open risk; this bounds NEW entries across the session):

      base       = chili_momentum_daily_trade_count_base (the ONE documented floor-ref, 5)
      ceiling    = round(base * heat_mult * expectancy_mult), clamped to [base, base*ceil_x]
      heat_mult  = clamp(1 + cushion/(2*base_loss), 1.0, ...)   # banked GREEN today => loosen
      exp_mult   = clamp(0.5 + recent_win_rate, 0.5, 1.5)       # cold lane => tighten

    TIGHTEN when expectancy degrades (a losing recent window halves toward 0.5 -> fewer
    entries -> stop bleeding into a bad regime), LOOSEN when hot (banked cushion + a winning
    window -> let the best regime run). DENY a NEW entry once today's REAL ENTERED count
    (terminated today + currently-open/in-flight) reaches the ceiling.

    Returns ``(allowed, meta)``. ADDITIVE / FAIL-OPEN: flag OFF, no db, no execution_family,
    a degenerate base, or any error => ``(True, ...)`` so the caller is byte-identical to
    today (the gate NEVER blocks on thin/bad data). Read-only; lookahead-free (only past
    terminated trades + the live open count). [momentum_neural] SCAL101"""
    if not bool(getattr(settings, "chili_momentum_daily_trade_count_budget_enabled", True)):
        return True, {"reason": "disabled"}
    try:
        base = int(getattr(settings, "chili_momentum_daily_trade_count_base", 5) or 5)
        if base <= 0:
            return True, {"reason": "base_disabled"}
        # Heat: today's banked realized cushion (units of the per-trade loss budget).
        heat_mult = 1.0
        cushion_u = 0.0
        try:
            from ..governance import global_realized_pnl_today_et

            realized_today = float(global_realized_pnl_today_et(db).get("total_usd") or 0.0)
            base_loss = equity_relative_loss_cap(
                float(getattr(settings, "chili_momentum_risk_max_loss_per_trade_usd", 50.0) or 50.0),
                execution_family,
            )
            if base_loss and base_loss > 0:
                cushion_u = max(0.0, realized_today) / base_loss
                # Each banked unit of risk loosens the day by 1/(2*base) of the ceiling —
                # a 2x-base-loss cushion adds ~1 trade of headroom. Bounded by the clamp below.
                heat_mult = 1.0 + cushion_u / (2.0 * base)
        except Exception:
            heat_mult = 1.0
        # Expectancy: the lane's recent live win rate (same dial bounds as the streak risk).
        exp_mult, win_rate, n_exp = 1.0, None, 0
        try:
            rr = _recent_realized_r(db, execution_family=execution_family, lookback=10)
            n_exp = len(rr)
            if n_exp >= 5:
                win_rate = sum(1 for r in rr if r > 0) / n_exp
                exp_mult = max(0.5, min(1.5, 0.5 + win_rate))
        except Exception:
            exp_mult = 1.0
        ceil_x = float(getattr(settings, "chili_momentum_daily_trade_count_max_multiple", 2.0) or 2.0)
        if not math.isfinite(ceil_x) or ceil_x < 1.0:
            ceil_x = 1.0
        raw_ceiling = base * heat_mult * exp_mult
        ceiling = int(max(base, min(round(raw_ceiling), int(round(base * ceil_x)))))
        entered = _count_real_entries_today(db, execution_family=execution_family)
        try:
            open_ct = max(0, int(open_entry_count))
        except (TypeError, ValueError):
            open_ct = 0
        used = entered + open_ct
        allowed = used < ceiling
        meta = {
            "allowed": allowed,
            "ceiling": ceiling,
            "base": base,
            "entered_today": entered,
            "open_inflight": open_ct,
            "used": used,
            "heat_mult": round(heat_mult, 3),
            "cushion_units": round(cushion_u, 3),
            "expectancy_mult": round(exp_mult, 3),
            "win_rate": round(win_rate, 3) if win_rate is not None else None,
            "n_expectancy": n_exp,
        }
        if not allowed:
            meta["reason"] = "daily_trade_count_budget_reached"
        return allowed, meta
    except Exception:
        return True, {"reason": "error_fail_open"}


def _minutes_since_rth_open_et() -> float | None:
    """Minutes since the 09:30 ET RTH open for TODAY (clamped >= 0), or None.

    Returns None BEFORE 09:30 ET (premarket — the time-fatigue leg is neutral there;
    the early window has its own clock policy) and None on any error. Pure read of the
    wall clock — no I/O. Used ONLY by the GAP-2 fatigue derate (size-down)."""
    try:
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        now_et = datetime.now(et)
        open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        if now_et < open_et:
            return None
        return max(0.0, (now_et - open_et).total_seconds() / 60.0)
    except Exception:
        return None


def fatigue_derate_multiplier(
    *,
    trade_count_today: int,
    max_trades_per_day: int,
    minutes_since_open: float | None = None,
    is_crypto: bool = False,
) -> tuple[float, dict[str, Any]]:
    """TIME + DECISION-FATIGUE size-down multiplier in [floor, 1.0] (GAP 2, PSY101).

    Ross trades best EARLY: decision quality degrades as the session lengthens and as the
    trade count climbs. This derate REDUCES the per-trade risk budget the deeper into the
    session / the more trades taken today — it is bounded to ``(floor, 1.0]`` and is NEVER
    > 1.0, so it can ONLY shrink size (the caller composes it multiplicatively under the
    existing 3x clamp; the equity-relative notional ceiling + liquidity cap still bound qty).

        time_frac  = clamp(minutes_since_open / full_session_minutes, 0, 1)   # 0 at open
        trade_frac = clamp(trade_count_today / max(max_trades_per_day, 1), 0, 1)
        derate     = 1.0 - 0.5*(1-floor as weight)... -> implemented as:
        derate     = 1.0 - (1.0 - floor) * (0.5*time_frac + 0.5*trade_frac)
        result     = clamp(derate, floor, 1.0)

    The TWO legs are weighted equally and TOGETHER can pull the multiplier all the way to
    ``floor`` (both maxed). Crypto (24/7, no RTH open) zeroes the TIME leg (``minutes_since_open``
    is None) so only the trade-count leg applies — the time clock is meaningless there.

    FAIL-NEUTRAL (returns 1.0): the flag is checked by the CALLER, so this helper is only
    invoked when enabled; any bad/degenerate input here still returns 1.0 (never a derate
    smaller than warranted, never > 1.0). Pure; no I/O. docs/DESIGN/MOMENTUM_LANE.md"""
    meta: dict[str, Any] = {"fatigue_mult": 1.0}
    try:
        floor = float(getattr(settings, "chili_momentum_fatigue_derate_floor", 0.5) or 0.5)
        if not math.isfinite(floor) or floor <= 0:
            floor = 0.5
        floor = max(0.1, min(1.0, floor))
        full_min = float(getattr(settings, "chili_momentum_fatigue_full_session_minutes", 240.0) or 240.0)
        if not math.isfinite(full_min) or full_min <= 0:
            full_min = 240.0
        # TIME leg (equities only; crypto has no RTH open -> neutral).
        time_frac = 0.0
        if not is_crypto and minutes_since_open is not None:
            try:
                time_frac = max(0.0, min(1.0, float(minutes_since_open) / full_min))
            except (TypeError, ValueError):
                time_frac = 0.0
        # TRADE-COUNT leg.
        try:
            tc = max(0, int(trade_count_today))
            mx = max(1, int(max_trades_per_day))
            trade_frac = max(0.0, min(1.0, tc / mx))
        except (TypeError, ValueError):
            trade_frac = 0.0
        fatigue = 0.5 * time_frac + 0.5 * trade_frac  # in [0, 1]
        derate = 1.0 - (1.0 - floor) * fatigue
        mult = max(floor, min(1.0, derate))
        meta = {
            "fatigue_mult": round(mult, 4),
            "time_frac": round(time_frac, 4),
            "trade_frac": round(trade_frac, 4),
            "minutes_since_open": (round(float(minutes_since_open), 1) if minutes_since_open is not None else None),
            "trade_count_today": int(max(0, int(trade_count_today))) if isinstance(trade_count_today, (int, float)) else 0,
            "floor": round(floor, 3),
        }
        return mult, meta
    except Exception:
        return 1.0, {"fatigue_mult": 1.0, "reason": "error_fail_neutral"}


def _prior_session_pnl_over_equity(
    db: Any, *, execution_family: str | None, lookback_days: int
) -> tuple[float | None, list[float]]:
    """(prior_session PnL/equity, trailing daily PnL/equity sample) for the lane.

    Buckets terminated live outcomes by ET calendar day (skipping empty days), normalizes
    each day's net realized PnL by the CURRENT equity basis (equity-relative — a fixed-$
    outlier means nothing without the account size), and returns the MOST-RECENT PAST day's
    normalized PnL plus the trailing sample (excluding today). Best-effort/read-only; thin
    or failed => ``(None, [])`` so the damper is neutral."""
    if db is None or not execution_family or lookback_days <= 0:
        return None, []
    try:
        from ....models.trading import MomentumAutomationOutcome

        # Window: from the start of `lookback_days` ago up to the start of TODAY (exclude
        # today — the damper is a PRIOR-session reset, lookahead-free).
        far_start, _ = _et_day_bounds_utc(days_ago=int(lookback_days))
        today_start, _ = _et_day_bounds_utc(days_ago=0)
        rows = (
            db.query(
                MomentumAutomationOutcome.terminal_at,
                MomentumAutomationOutcome.realized_pnl_usd,
            )
            .filter(
                MomentumAutomationOutcome.execution_family == execution_family,
                MomentumAutomationOutcome.mode == "live",
                MomentumAutomationOutcome.realized_pnl_usd.isnot(None),
                MomentumAutomationOutcome.terminal_at >= far_start,
                MomentumAutomationOutcome.terminal_at < today_start,
            )
            .all()
        )
    except Exception:
        logger.debug("[momentum_neural] prior-day pnl read failed", exc_info=True)
        return None, []
    if not rows:
        return None, []
    eq = _account_equity_usd(execution_family, prefer_equity=True)
    if not eq or eq <= 0:
        return None, []
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    utc = ZoneInfo("UTC")
    by_day: dict[Any, float] = {}
    for ts, pnl in rows:
        try:
            if ts is None or pnl is None:
                continue
            d = ts.replace(tzinfo=utc).astimezone(et).date()
            by_day[d] = by_day.get(d, 0.0) + float(pnl)
        except Exception:
            continue
    if not by_day:
        return None, []
    days_sorted = sorted(by_day.keys())
    sample = [by_day[d] / eq for d in days_sorted]
    prior = by_day[days_sorted[-1]] / eq  # most-recent PAST session
    return prior, sample


def prior_day_pnl_damper_multiplier(
    db: Any, *, execution_family: str | None
) -> tuple[float, dict[str, Any]]:
    """OUTLIER prior-session size DAMPER (HVM101 / SCAL101 emotional/variance reset).

    After a statistically OUTLIER prior session — a BIG win OR a BIG loss (|PnL|/equity
    z-scored over a trailing window of daily normalized PnL) — apply a size multiplier
    < 1 for the next session: a tilt/variance reset (Ross + Mike's 'green on the day,
    don't give it back' / 'don't revenge-trade a red day' discipline). Symmetric on the
    sign — both a euphoric over-confidence day and a tilted blow-up day revert toward
    baseline risk.

      z = (prior_norm - mean) / stdev            # over the trailing daily sample
      damper = clamp(1 - slope * (|z| - thresh), floor, 1.0)  when |z| >= thresh, else 1.0

    Distinct from cushion_risk_multiplier (which reads TODAY's intraday banked cushion to
    climb a ladder) — this reads the COMPLETED PRIOR session and only ever SIZES DOWN.
    Composes multiplicatively with the other size-down levers (bounded by the runner's 3x
    combined clamp). Equity-relative, adaptive: the threshold/slope/floor are the only fixed
    knobs (all documented config defaults). ADDITIVE / FAIL-NEUTRAL: flag OFF, thin/degenerate
    history, zero-variance, or any error => ``(1.0, ...)`` (never increases risk, never blocks).
    Read-only; lookahead-free (prior days only). [momentum_neural] HVM101/SCAL101"""
    if not bool(getattr(settings, "chili_momentum_prior_day_pnl_damper_enabled", True)):
        return 1.0, {"reason": "disabled"}
    try:
        lookback_days = int(getattr(settings, "chili_momentum_prior_day_damper_lookback_days", 20) or 20)
        z_thresh = float(getattr(settings, "chili_momentum_prior_day_damper_z_threshold", 1.5) or 1.5)
        floor = float(getattr(settings, "chili_momentum_prior_day_damper_floor", 0.5) or 0.5)
        slope = float(getattr(settings, "chili_momentum_prior_day_damper_slope", 0.25) or 0.25)
        if not (0.0 < floor <= 1.0):
            floor = 0.5
        prior, sample = _prior_session_pnl_over_equity(
            db, execution_family=execution_family, lookback_days=lookback_days
        )
        if prior is None or len(sample) < 5:
            return 1.0, {"reason": "thin_history", "n": len(sample)}
        mean = statistics.fmean(sample)
        try:
            stdev = statistics.pstdev(sample)
        except statistics.StatisticsError:
            stdev = 0.0
        if not math.isfinite(stdev) or stdev <= 0:
            return 1.0, {"reason": "zero_variance", "n": len(sample)}
        z = (prior - mean) / stdev
        meta = {
            "prior_norm": round(prior, 6),
            "mean": round(mean, 6),
            "stdev": round(stdev, 6),
            "z": round(z, 3),
            "z_threshold": z_thresh,
            "n": len(sample),
        }
        if abs(z) < z_thresh:
            return 1.0, {**meta, "damper_mult": 1.0, "reason": "within_band", "outlier": False}
        damper = max(floor, min(1.0, 1.0 - slope * (abs(z) - z_thresh)))
        return damper, {
            **meta,
            "damper_mult": round(damper, 4),
            "floor": floor,
            "slope": slope,
            "outlier": True,
            "outlier_sign": "win" if prior > 0 else "loss",
        }
    except Exception:
        return 1.0, {"damper_mult": 1.0, "reason": "error_fail_neutral"}


def consecutive_green_days(
    db: Any, *, execution_family: str | None, lookback_days: int = 30
) -> tuple[int, dict[str, Any]]:
    """Count consecutive GREEN ET calendar days (net realized PnL > 0) for the lane,
    walking BACKWARDS from the most-recent PAST day (today excluded — today's session is
    incomplete; including it would let an intraday red flicker collapse the streak mid-day).

    Buckets terminated live outcomes by ET calendar day, sums net realized PnL per day, and
    counts how many of the most-recent CONTIGUOUS past days closed green, stopping at the
    first red (or zero) day. Read-only, ephemeral (recomputed each call — never persisted),
    lookahead-free. Returns ``(streak, meta)``; thin/failed history => ``(0, ...)`` (neutral).
    """
    meta: dict[str, Any] = {"streak": 0, "lookback_days": int(lookback_days)}
    if db is None or not execution_family or lookback_days <= 0:
        return 0, {**meta, "reason": "no_input"}
    try:
        from ....models.trading import MomentumAutomationOutcome
        from .outcome_labels import is_real_entry_outcome

        far_start, _ = _et_day_bounds_utc(days_ago=int(lookback_days))
        today_start, _ = _et_day_bounds_utc(days_ago=0)
        rows = (
            db.query(
                MomentumAutomationOutcome.terminal_at,
                MomentumAutomationOutcome.realized_pnl_usd,
                MomentumAutomationOutcome.outcome_class,
            )
            .filter(
                MomentumAutomationOutcome.execution_family == execution_family,
                MomentumAutomationOutcome.mode == "live",
                MomentumAutomationOutcome.realized_pnl_usd.isnot(None),
                MomentumAutomationOutcome.terminal_at >= far_start,
                MomentumAutomationOutcome.terminal_at < today_start,
            )
            .all()
        )
    except Exception:
        logger.debug("[momentum_neural] green-day streak read failed", exc_info=True)
        return 0, {**meta, "reason": "read_failed"}
    if not rows:
        return 0, {**meta, "reason": "no_history"}
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    utc = ZoneInfo("UTC")
    by_day: dict[Any, float] = {}
    for ts, pnl, oc in rows:
        try:
            # Only REAL entered trades carry strategy P&L. A never-entered row
            # (cancelled_pre_entry / no_fill / risk_block) carries realized_pnl_usd=0.0
            # (NOT NULL — slips past the not-null filter); a day of ONLY such rows would
            # sum to 0.0 and spuriously BREAK the streak (0.0 is not > 0.0) even though no
            # real trade happened. Mirror _count_real_entries_today: exclude them so the
            # daily green/red verdict is the REAL realized-PnL sum. [momentum_neural]
            if ts is None or pnl is None or not is_real_entry_outcome(oc):
                continue
            d = ts.replace(tzinfo=utc).astimezone(et).date()
            by_day[d] = by_day.get(d, 0.0) + float(pnl)
        except Exception:
            continue
    if not by_day:
        return 0, {**meta, "reason": "no_buckets"}
    days_sorted = sorted(by_day.keys(), reverse=True)  # most-recent first
    streak = 0
    green_usd = 0.0
    for d in days_sorted:
        if by_day[d] > 0.0:
            streak += 1
            green_usd += by_day[d]
        else:
            break
    return streak, {
        **meta,
        "streak": int(streak),
        "green_usd": round(green_usd, 2),
        "days_seen": len(by_day),
    }


def green_day_graduation_multiplier(
    db: Any, *, execution_family: str | None
) -> tuple[float, dict[str, Any]]:
    """GREEN-DAY GRADUATION size multiplier (NOT a hard live-block).

    After a consecutive green-day streak (realized daily PnL > 0, ET calendar, auto-derived
    from history), scale the per-trade risk basis UP a bounded amount so the lane graduates
    to bigger size only once it has PROVEN consistency — Ross/Mike's "earn the size" rule.

      mult = clamp(1.0 + step * max(0, streak - 1), 1.0, max_multiplier)

    Day-1 (streak<=1) => 1.0 (no graduation off a single green day). Composes multiplicatively
    into the runner's existing combined-multiplier ceiling, applied at entry-quantity compute
    time — it is NEVER a veto and never blocks an entry. ADDITIVE / FAIL-NEUTRAL: flag OFF,
    thin history, or any error => ``(1.0, ...)`` (never changes sizing). Read-only; ephemeral
    (the streak is recomputed each call, never persisted). [momentum_neural] graduation."""
    if not bool(getattr(settings, "chili_momentum_green_day_graduation_enabled", False)):
        return 1.0, {"reason": "disabled", "graduation_mult": 1.0}
    try:
        step = float(getattr(settings, "chili_momentum_green_day_step_per_day", 0.1) or 0.1)
        max_mult = float(getattr(settings, "chili_momentum_green_day_max_multiplier", 2.0) or 2.0)
        lookback = int(getattr(settings, "chili_momentum_green_day_lookback_days", 30) or 30)
        if max_mult < 1.0:
            max_mult = 1.0
        streak, s_meta = consecutive_green_days(
            db, execution_family=execution_family, lookback_days=lookback
        )
        mult = max(1.0, min(max_mult, 1.0 + step * max(0, int(streak) - 1)))
        return mult, {
            "graduation_mult": round(mult, 4),
            "consecutive_green_days": int(streak),
            "step_per_day": step,
            "max_multiplier": max_mult,
            **{k: v for k, v in s_meta.items() if k in ("green_usd", "days_seen")},
        }
    except Exception:
        return 1.0, {"reason": "error_fail_neutral", "graduation_mult": 1.0}


def catalyst_conviction_size_multiplier(
    symbol: str,
    *,
    strong_symbols: set[str] | None = None,
    weak_symbols: set[str] | None = None,
    fake_symbols: set[str] | None = None,
) -> tuple[float, dict[str, Any]]:
    """CATALYST-CONVICTION size multiplier (NOT a hard live-block).

    When the name carries a STRONG, credible catalyst (the DEPLOYED strong/weak/fake news
    grade — FDA/trial/M&A/contract/beat, not also diluting/rumored/hacked) scale the per-trade
    risk basis UP a bounded amount — Ross's "a real reason a low-float runs earns the size".

      mult = clamp(1.0 + step * grade_rank, 1.0, max_multiplier)

    ``grade_rank`` comes from ``catalyst_grade_rank`` (STRONG=3, weak/fake/none=0), so weak and
    fake DOMINATE (suppress the boost to rank 0). Mirrors ``green_day_graduation_multiplier``:
    composes multiplicatively into the runner's existing 3x combined-multiplier ceiling +
    downstream hard notional ceiling, applied at entry-quantity compute time — it is NEVER a
    veto and NEVER shrinks a trade (a catalyst only ADDS; the no-news shrink lives elsewhere).
    ADDITIVE / FAIL-NEUTRAL: flag OFF, no/weak/fake catalyst, or any error => ``(1.0, ...)``
    (never changes sizing). Read-only; reuses the SAME news accessors (no new feed). The
    grade sets may be passed in (fetched once upstream); omitted => fetched fresh here.
    [momentum_neural] catalyst-conviction."""
    if not bool(getattr(settings, "chili_momentum_catalyst_conviction_enabled", False)):
        return 1.0, {"reason": "disabled", "conviction_mult": 1.0}
    try:
        from .catalyst import catalyst_grade_rank

        # None-aware defaults (NOT `or` — a legit step=0.0 is falsy and would wrongly fall back)
        _step_raw = getattr(settings, "chili_momentum_catalyst_conviction_step", 0.15)
        step = float(_step_raw if _step_raw is not None else 0.15)
        _max_raw = getattr(settings, "chili_momentum_catalyst_conviction_max_multiplier", 1.5)
        max_mult = float(_max_raw if _max_raw is not None else 1.5)
        if max_mult < 1.0:
            max_mult = 1.0
        rank = int(
            catalyst_grade_rank(
                symbol,
                strong_symbols=strong_symbols,
                weak_symbols=weak_symbols,
                fake_symbols=fake_symbols,
            )
        )
        # A catalyst only ADDS: clamp floor 1.0 (rank<=0 / negative step => no boost), ceiling
        # max_mult. The runner's min(..., base*3.0) clamp + the hard notional ceiling further
        # contain the COMBINED multiplier — this factor can never push past any ceiling.
        mult = max(1.0, min(max_mult, 1.0 + step * max(0, rank)))
        return mult, {
            "conviction_mult": round(mult, 4),
            "grade_rank": rank,
            "step": step,
            "max_multiplier": max_mult,
        }
    except Exception:
        return 1.0, {"reason": "error_fail_neutral", "conviction_mult": 1.0}


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


def spread_liquidity_risk_multiplier(
    spread_bps: float | None,
    expected_move_bps: float | None,
    *,
    floor: float = 0.5,
    ratio: float | None = None,
    abs_cap_bps: float | None = None,
) -> tuple[float, dict[str, Any]]:
    """Shrink per-trade RISK as the live spread consumes the name's ADAPTIVE spread
    tolerance — wide-spread / illiquid names (the −$697 low-float gap-through tail; e.g.
    QXL −$229 on a 119bps name, 2026-06-22) get SIZED DOWN, never REJECTED. This is the
    surgical fix the failed L3 entry filter was NOT: it cuts the loser tail without
    killing a single trade or winner (an entry filter can't tell winner from loser at
    fire-time; SIZE can — the risky-liquidity names are systematically over-sized).

    ``mult = clamp(1 − spread/tolerance, floor, 1.0)``: a tight name → 1.0; a name eating
    its full allowable spread → ``floor``. ``tolerance`` = ``adaptive_max_spread_bps``
    (the SAME gate that admitted the name) which scales UP for explosive movers, so a
    high-move runner with a proportionate spread is NOT shrunk. Returns ``(1.0, …)``
    fail-NEUTRAL on unusable inputs (never increases risk). Reads settings only.
    [momentum_neural] project_profitability_levers / docs/DESIGN/SCALING_ENGINE.md P1"""
    try:
        sb = float(spread_bps) if spread_bps is not None else None
        if sb is None or not math.isfinite(sb) or sb <= 0:
            return 1.0, {"reason": "no_spread"}
        if ratio is None:
            ratio = float(getattr(settings, "chili_momentum_risk_spread_to_expected_move_ratio", 0.5) or 0.5)
        if abs_cap_bps is None:
            abs_cap_bps = float(getattr(settings, "chili_momentum_risk_max_spread_bps_abs_cap", 800.0) or 800.0)
        base = float(getattr(settings, "chili_momentum_risk_max_spread_bps_live", 60.0) or 60.0)
        tol = adaptive_max_spread_bps(base, expected_move_bps, ratio, abs_cap_bps=abs_cap_bps)
        if not math.isfinite(tol) or tol <= 0:
            return 1.0, {"reason": "no_tolerance"}
        flo = float(floor)
        if not (0.0 < flo <= 1.0):
            flo = 0.5
        mult = max(flo, min(1.0, 1.0 - (sb / tol)))
        return mult, {"spread_bps": round(sb, 1), "tolerance_bps": round(tol, 1), "mult": round(mult, 4), "floor": flo}
    except (TypeError, ValueError):
        return 1.0, {"reason": "error_fail_neutral"}


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


def _recent_realized_r(
    db: Any, *, execution_family: str | None, lookback: int
) -> list[float]:
    """Recent per-trade realized-R for the lane, MOST-RECENT FIRST, REAL ENTERED trades only.

    realized_R = realized_pnl_usd / frozen max_loss_per_trade_usd (the admission risk
    budget the lane sizes qty against, so it ~= the structural stop_distance*qty) — a
    clean R-multiple computable from data that ALWAYS exists (no MFE persistence needed).

    Mirrors streak_risk_multiplier's discipline (is_real_entry_outcome): a $0.00
    cancelled_pre_entry carries realized_pnl_usd=0.0 (NOT NULL) and would slip past a
    realized-not-null filter — and this lane churns FAR more cancels than fills, so those
    never-entered 0.0-R rows would dominate the window and dilute both means toward 0,
    neutering the breaker. Prune them so the metric measures ENTERED-trade follow-through.

    Best-effort, read-only: any failure / a missing cap -> that trade is skipped; an empty
    list -> the caller applies no bump. LIVE mode only. [momentum_neural]"""
    if db is None or lookback <= 0 or not execution_family:
        return []
    try:
        from ....models.trading import MomentumAutomationOutcome, TradingAutomationSession
        from .outcome_labels import is_real_entry_outcome

        # Fetch headroom (NOT a risk parameter): pull more than `lookback` so the post-filter
        # prune of never-entered (cancel / no-fill / risk-block) rows still yields ~lookback
        # REAL entries in this churn-heavy lane. Bounded + indexed (execution_family, terminal_at desc).
        fetch = max(int(lookback) * 5, 80)
        rows = (
            db.query(
                MomentumAutomationOutcome.realized_pnl_usd,
                MomentumAutomationOutcome.outcome_class,
                TradingAutomationSession.risk_snapshot_json,
            )
            .join(TradingAutomationSession, MomentumAutomationOutcome.session_id == TradingAutomationSession.id)
            .filter(MomentumAutomationOutcome.execution_family == execution_family)
            .filter(MomentumAutomationOutcome.mode == "live")
            .filter(MomentumAutomationOutcome.realized_pnl_usd.isnot(None))
            .order_by(MomentumAutomationOutcome.terminal_at.desc())
            .limit(fetch)
            .all()
        )
    except Exception:
        logger.debug("[momentum_neural] run-R history read failed", exc_info=True)
        return []
    out: list[float] = []
    for pnl, oc, snap in rows:
        if not is_real_entry_outcome(oc):
            continue  # never-entered (cancel / no-fill / risk-block) — not real follow-through
        caps = snap.get("momentum_policy_caps") if isinstance(snap, dict) else None
        if not isinstance(caps, dict):
            continue
        try:
            cap = float(caps.get("max_loss_per_trade_usd"))
            pv = float(pnl)
        except (TypeError, ValueError):
            continue
        if math.isfinite(cap) and cap > 0 and math.isfinite(pv):
            out.append(pv / cap)
        if len(out) >= int(lookback):
            break
    return out


def run_r_viability_bump(
    db: Any, execution_family: str | None
) -> tuple[float, dict[str, Any]]:
    """MACRO run-R breaker (L2.1): a SOFT, regime-RELATIVE entry-bar raise.

    Returns ``(bump, meta)``. The lane's recent realized-R (a follow-through proxy — the
    2026-06-22 decomposition found winners thrust, losers fade) is taken as a SHORT recent
    window mean vs the full-lookback baseline mean. When the recent stretch is BOTH
    loss-making in R AND below the lane's own baseline (a no-follow-through regime), raise
    entry_viability_min by the configured bump so fewer marginal setups arm. RELATIVE +
    graduated => it releases the moment the recent stretch recovers to baseline, so it can
    NEVER permanently freeze the lane (the failure mode an absolute floor would have).

    Entry-side ONLY: the result is consumed by ``_effective_entry_viability_min``; it never
    reads or mutates a position/order and is never called from an exit path. Disabled /
    thin-history => ``(0.0, ...)`` so the caller's ``_score_ok`` is byte-identical.
    [momentum_neural] project_profitability_levers"""
    if not bool(getattr(settings, "chili_momentum_run_r_breaker_enabled", True)):
        return 0.0, {"reason": "disabled"}
    bump_cfg = float(getattr(settings, "chili_momentum_run_r_breaker_viability_bump", 0.05) or 0.0)
    if bump_cfg <= 0:
        return 0.0, {"reason": "bump_disabled"}
    n = int(getattr(settings, "chili_momentum_run_r_breaker_lookback", 40) or 40)
    short_k = int(getattr(settings, "chili_momentum_run_r_breaker_short_window", 10) or 10)
    min_hist = int(getattr(settings, "chili_momentum_run_r_breaker_min_history", 8) or 8)
    rr = _recent_realized_r(db, execution_family=execution_family, lookback=n)
    meta: dict[str, Any] = {"n": len(rr), "lookback": n, "short_window": short_k}
    if len(rr) < max(1, min_hist):
        return 0.0, {**meta, "reason": "thin_history", "triggered": False}
    short = rr[: max(1, min(short_k, len(rr)))]
    long_mean = statistics.fmean(rr)
    short_mean = statistics.fmean(short)
    meta.update({"short_mean_r": round(short_mean, 3), "long_mean_r": round(long_mean, 3)})
    if short_mean < 0.0 and short_mean < long_mean:
        return round(bump_cfg, 4), {**meta, "reason": "below_baseline_and_losing", "triggered": True}
    return 0.0, {**meta, "reason": "ok", "triggered": False}


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
