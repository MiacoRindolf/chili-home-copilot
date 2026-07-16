"""Momentum automation risk policy (config-backed; frozen on session snapshots — Phase 6)."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional

from sqlalchemy import and_, func

from ....config import settings
from ..execution_family_registry import (
    EXECUTION_FAMILY_COINBASE_SPOT,
    normalize_execution_family,
)

logger = logging.getLogger(__name__)

POLICY_VERSION = 1
RISK_SNAPSHOT_KEY = "momentum_risk"
POLICY_SNAPSHOT_KEY = "momentum_risk_policy_summary"

_REPLAY_RISK_NOW: contextvars.ContextVar[Optional[datetime]] = (
    contextvars.ContextVar("_chili_replay_risk_now", default=None)
)


def _risk_now_aware() -> datetime:
    value = _REPLAY_RISK_NOW.get()
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _risk_now_naive() -> datetime:
    return _risk_now_aware().replace(tzinfo=None)


@contextlib.contextmanager
def replay_risk_clock(ts: datetime) -> Iterator[None]:
    """Bind every risk-policy calendar decision to one replay tick instant."""

    if not isinstance(ts, datetime):
        raise TypeError("replay risk clock requires datetime")
    value = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(
        timezone.utc
    )
    token = _REPLAY_RISK_NOW.set(value)
    try:
        yield
    finally:
        _REPLAY_RISK_NOW.reset(token)

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
    abs_cap_em_scale_k: float | None = None,
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
                # STEP-E #15: EM-scale the abs cap so a legitimately-wide low-float (whose OWN
                # expected move justifies a wide ceiling, e.g. DSY adaptive 721bps) isn't
                # clamped to the fixed cap (300). effective_cap = max(cap, k * (ratio*EM)) with
                # k >= 1.0 the ONE documented base. Junk (small EM => small ratio*em) keeps the
                # fixed cap and still blocks. When k is None (legacy) the fixed cap applies.
                effective_cap = cap
                if abs_cap_em_scale_k is not None:
                    try:
                        k = float(abs_cap_em_scale_k)
                        if math.isfinite(k) and k >= 1.0:
                            effective_cap = max(cap, k * (r * em))
                    except (TypeError, ValueError):
                        effective_cap = cap
                adaptive = min(adaptive, max(base, effective_cap))  # never tolerate above the cap
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


_ALPACA_ACCT_CACHE: dict[str, Any] = {
    "scope": None,
    "expected_account_id": None,
    "observed_account_id": None,
    "equity": 0.0,
    "bp": 0.0,
    "ts": 0.0,
}


def _configured_alpaca_account_id() -> str:
    return str(
        getattr(settings, "chili_alpaca_expected_account_id", "") or ""
    ).strip()


def _clear_alpaca_account_caches() -> None:
    """Retire every capital-basis cache when the paper account generation changes."""
    _ALPACA_ACCT_CACHE.update({
        "scope": None,
        "expected_account_id": None,
        "observed_account_id": None,
        "equity": 0.0,
        "bp": 0.0,
        "ts": 0.0,
    })
    # Keep legacy family-only keys in the deletion set so a process upgraded in
    # place cannot resurrect a pre-generation last-good value.
    for key in list(_ACCOUNT_EQUITY_LAST_GOOD):
        if key in {"alpaca_spot", "alpaca_short"} or key.startswith(
            ("alpaca_spot|", "alpaca_short|")
        ):
            _ACCOUNT_EQUITY_LAST_GOOD.pop(key, None)


def _alpaca_cached_account_generation() -> str | None:
    expected = _configured_alpaca_account_id()
    cached_expected = str(
        _ALPACA_ACCT_CACHE.get("expected_account_id") or ""
    ).strip()
    cached_observed = str(
        _ALPACA_ACCT_CACHE.get("observed_account_id") or ""
    ).strip()
    if expected and cached_expected == expected and cached_observed == expected:
        return expected
    return None


def _alpaca_account_cached() -> tuple[float | None, float | None]:
    """(equity, buying_power) for the certified Alpaca paper account, short-TTL cached like the
    agentic reads so a burst of candidate sizings does not fire a get_account per name (rate
    limits). Fail-open to the last-good value on a transient miss. Alpaca's paper account reports
    ~4x day-trading buying power on its equity; the SIZING basis uses buying_power, the RISK cap
    uses equity. (2026-07-07, ALPACA_PAPER_ENABLE_PLAN.md)"""
    import time as _time

    # The adapter and runner are paper-only.  This check must precede every
    # cache read so a runtime paper→live posture flip cannot leak stale paper
    # equity/buying power into sizing or loss-cap decisions.
    if not bool(getattr(settings, "chili_alpaca_paper", True)):
        _clear_alpaca_account_caches()
        return None, None

    scope = "alpaca:paper"
    expected_account_id = _configured_alpaca_account_id()
    if not expected_account_id:
        _clear_alpaca_account_caches()
        return None, None
    if (
        _ALPACA_ACCT_CACHE.get("scope") != scope
        or _alpaca_cached_account_generation() != expected_account_id
    ):
        _clear_alpaca_account_caches()
    now = _time.monotonic()
    age = now - (_ALPACA_ACCT_CACHE.get("ts") or 0.0)
    _eq0 = _ALPACA_ACCT_CACHE.get("equity") or 0.0
    _bp0 = _ALPACA_ACCT_CACHE.get("bp") or 0.0
    if _eq0 > 0 and age < _AGENTIC_BP_TTL_SEC:
        return _eq0, _bp0
    try:
        from ..venue.alpaca_spot import AlpacaSpotAdapter

        snap = AlpacaSpotAdapter().get_account_snapshot() or {}
    except Exception:
        snap = {}
    if snap.get("ok") is True:
        observed_account_id = str(snap.get("account_id") or "").strip()
        if (
            snap.get("paper") is not True
            or observed_account_id != expected_account_id
        ):
            # A positive wrong-account read is not a transient data miss.  It
            # invalidates both the short account cache and the 180-second
            # last-good layer, with no fallback to the prior generation.
            _clear_alpaca_account_caches()
            return None, None
        try:
            eq = float(snap.get("equity") or 0.0)
            bp = float(snap.get("buying_power") or 0.0)
        except (TypeError, ValueError, OverflowError):
            eq = bp = 0.0
    else:
        observed_account_id = ""
        eq = bp = 0.0
    if eq > 0:
        _ALPACA_ACCT_CACHE["equity"] = eq
        _ALPACA_ACCT_CACHE["bp"] = bp
        _ALPACA_ACCT_CACHE["ts"] = now
        _ALPACA_ACCT_CACHE["scope"] = scope
        _ALPACA_ACCT_CACHE["expected_account_id"] = expected_account_id
        _ALPACA_ACCT_CACHE["observed_account_id"] = observed_account_id
        return eq, bp
    if _eq0 > 0 and age < _AGENTIC_BP_STALE_GRACE:
        return _eq0, _bp0  # transient miss → recent cached value
    return None, None


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


def _stabilize_account_equity(
    ef: str,
    eq: float | None,
    *,
    account_generation: str | None = None,
) -> float | None:
    """LOW/failed-read stabilizer for the per-family account-equity basis.

    Returns the live ``eq`` when it is a plausible positive value (and refreshes the
    last-good cache). When the live read is None/0/implausibly-tiny, returns the last-good
    cached value if it is within the short grace TTL, else None (caller -> fixed fallback).
    Never inflates above a real read; see the cache docstring above for the safety contract."""
    import time as _time

    now = _time.monotonic()
    cache_key = (
        f"{ef}|{account_generation}"
        if account_generation
        else ef
    )
    slot = _ACCOUNT_EQUITY_LAST_GOOD.get(cache_key)
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
        _ACCOUNT_EQUITY_LAST_GOOD[cache_key] = {"value": float(eq), "ts": now}
        return float(eq)

    # Degraded/failed/tiny read -> reuse the last REAL read within the grace window.
    if cached > 0 and age < _ACCOUNT_EQUITY_LAST_GOOD_TTL_SEC:
        logger.warning(
            "[momentum_neural] account-equity read DEGRADED for %s (live=%s) — reusing last-good "
            "$%.2f (age=%.0fs, ttl=%.0fs) to avoid a spurious daily-loss-cap collapse",
            cache_key, eq, cached, age, _ACCOUNT_EQUITY_LAST_GOOD_TTL_SEC,
        )
        return cached
    return None


# ── REPLAY v3 P2 — RECORDED ACCOUNT-EQUITY SEAM (the broker buying-power read) ────
# ``_account_equity_usd`` is the single source-of-truth for the equity-relative caps and
# the atomic risk-budget admission (``live_runner.py:9132`` reads it for the fill-boundary
# budget; ``live_runner.py:9252`` for the crypto ``equity_unavailable`` gate). In prod it
# hits the BROKER (``get_portfolio`` over the network) — a hidden real-time dep the sim
# clock / OHLCV seams do not reach (design R2). To replay the FSM hermetically the harness
# must serve a RECORDED/INJECTED equity basis instead of the network. We mirror the
# ``live_runner._REPLAY_OHLCV_PROVIDER`` ContextVar pattern EXACTLY: a process-global,
# async/thread-safe ``ContextVar`` holding an OPTIONAL provider callable. Default is
# ``None`` — and when it is ``None`` (ALWAYS in prod, since only the replay harness ever
# sets it) ``_account_equity_usd`` runs the REAL broker read on the EXACT same code path as
# before, so prod is BYTE-IDENTICAL. The ContextVar resets automatically on block/exception
# exit, so an injected equity can never leak into a real lane. The provider mirrors the
# function's own (execution_family, apply_margin_multiple, prefer_equity, prefer_cash_value)
# signature so a faithful replay can return a venue/flag-specific basis as-of t; a provider
# may return ``None`` to faithfully reproduce an equity outage (the ``equity_unavailable``
# reject path). See docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §3.2 / §6 (the equity seam, P2).
_REPLAY_EQUITY: contextvars.ContextVar[Optional[Callable[..., Optional[float]]]] = (
    contextvars.ContextVar("_chili_replay_account_equity", default=None)
)


def set_replay_account_equity(
    provider: Optional[Callable[..., Optional[float]]],
) -> "contextvars.Token[Optional[Callable[..., Optional[float]]]]":
    """Install a recorded-equity provider on the seam (replay harness only). Returns the
    ``contextvars.Token`` so the caller can ``reset_replay_account_equity(token)`` (prefer the
    ``replay_account_equity`` context manager, which does this for you)."""
    return _REPLAY_EQUITY.set(provider)


def reset_replay_account_equity(
    token: "contextvars.Token[Optional[Callable[..., Optional[float]]]]",
) -> None:
    """Pop the equity provider, restoring the value before the matching set."""
    _REPLAY_EQUITY.reset(token)


@contextlib.contextmanager
def replay_account_equity(
    provider: Optional[Callable[..., Optional[float]]],
) -> Iterator[None]:
    """Context manager installing ``provider`` as the account-equity source for the block.

    Pure ContextVar push/pop — async/thread-safe, auto-resets on normal exit AND on exception
    (the ``finally`` always restores the prior value), so an injected equity can never escape
    the block into prod. Nests correctly. Prod never enters this manager, so prod stays
    byte-identical. A constant equity is convenient: ``replay_account_equity(lambda **k: 1e5)``."""
    token = set_replay_account_equity(provider)
    try:
        yield
    finally:
        reset_replay_account_equity(token)


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
    # REPLAY v3 P2 seam (prod byte-identical): when a recorded-equity provider is installed
    # (replay harness only — ``None`` ALWAYS in prod) serve the injected basis as-of t with
    # ZERO broker/network I/O. A provider may return ``None`` to faithfully reproduce an
    # equity-outage (the ``equity_unavailable`` reject path). Prod never installs one, so the
    # real broker read below runs on the identical code path.
    _replay_equity = _REPLAY_EQUITY.get()
    if _replay_equity is not None:
        return _replay_equity(
            execution_family,
            apply_margin_multiple=apply_margin_multiple,
            prefer_equity=prefer_equity,
            prefer_cash_value=prefer_cash_value,
        )

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

    # Certified Alpaca paper rail: size against the ALPACA account, NOT the RH/Coinbase portfolio.
    # Without this branch alpaca_spot fell into the Coinbase `else` below and sized against the tiny
    # Coinbase balance (~$1.9k) => absurd ~$290 orders on a paper account with ~$100k equity / ~$400k
    # buying power. Alpaca's reported buying_power already includes its (paper 4x) margin, so use it
    # DIRECTLY with NO extra multiple (like the agentic cash rail); the RISK cap (prefer_equity) reads
    # the stabilized total EQUITY. (2026-07-07, ALPACA_PAPER_ENABLE_PLAN.md)
    from ..execution_family_registry import (
        EXECUTION_FAMILY_ALPACA_SHORT,
        EXECUTION_FAMILY_ALPACA_SPOT,
    )
    if ef in (EXECUTION_FAMILY_ALPACA_SPOT, EXECUTION_FAMILY_ALPACA_SHORT):
        if not bool(getattr(settings, "chili_alpaca_paper", True)):
            # Clear both cache layers before any stabilizer read. Otherwise a
            # posture flip could resurrect paper equity for 180s from the
            # family-scoped last-good cache even after the account cache cleared.
            _clear_alpaca_account_caches()
            return None
        _a_eq, _a_bp = _alpaca_account_cached()
        if prefer_equity:
            generation = _alpaca_cached_account_generation()
            if generation is None:
                return None
            return _stabilize_account_equity(
                ef,
                _a_eq if (_a_eq and _a_eq > 0) else None,
                account_generation=generation,
            )
        # Alpaca paper commonly exposes ~4x intraday buying power. The global
        # buying-power preference predates this venue and amplified both size and
        # max loss by that multiple. Alpaca therefore requires an explicit,
        # venue-scoped opt-in before leverage can become the sizing basis.
        _alpaca_use_bp = use_bp and bool(
            getattr(settings, "chili_momentum_alpaca_size_use_buying_power", False)
        )
        if _alpaca_use_bp and _a_bp and _a_bp > 0:
            return float(_a_bp)
        return float(_a_eq) if (_a_eq and _a_eq > 0) else None

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


def alpaca_paper_hard_loss_cap_usd(
    execution_family: str | None,
) -> float | None:
    """Deprecated activation-only cap hook.

    Adaptive paper sizing is owned by the content-addressed resolver packet and
    its structural-R/portfolio reservation.  Returning a dollar ceiling here
    would silently re-clamp that quantity and break replay/paper parity.
    """
    try:
        from ..execution_family_registry import normalize_execution_family

        family = normalize_execution_family(execution_family)
        if family not in {"alpaca_spot", "alpaca_short"}:
            return None
        if not bool(getattr(settings, "chili_alpaca_paper", True)):
            return None
        return None
    except (TypeError, ValueError, OverflowError):
        return None


def equity_relative_loss_cap(fixed_fallback_usd: float, execution_family: str | None = None) -> float:
    """Per-trade MAX-LOSS cap as a fraction of account equity (documented
    per-trade RISK knob). docs/DESIGN/MOMENTUM_LANE.md"""
    cap = _equity_relative_cap(
        fixed_fallback_usd,
        getattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01),
        execution_family,
    )
    return cap


def equity_relative_daily_loss_cap(fixed_fallback_usd: float, execution_family: str | None = None) -> float:
    """Daily-loss cap as a fraction of account equity (documented DAILY risk knob).
    Evaluated live so the daily circuit-breaker adapts to current equity.
    docs/DESIGN/MOMENTUM_LANE.md"""
    try:
        from ..execution_family_registry import normalize_execution_family

        family = normalize_execution_family(execution_family)
        if (
            family in {"alpaca_spot", "alpaca_short"}
            and bool(getattr(settings, "chili_alpaca_paper", True))
        ):
            fraction = float(
                getattr(
                    settings,
                    "chili_momentum_risk_daily_loss_fraction_of_equity",
                    0.0,
                )
                or 0.0
            )
            equity = _account_equity_usd(
                execution_family,
                prefer_equity=True,
            )
            if (
                not math.isfinite(fraction)
                or fraction <= 0.0
                or equity is None
                or not math.isfinite(float(equity))
                or float(equity) <= 0.0
            ):
                # Zero is an explicit fail-closed/unavailable result at callers;
                # no embedded dollar fallback may invent an adaptive paper budget.
                return 0.0
            return round(float(equity) * fraction, 2)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return _equity_relative_cap(
        fixed_fallback_usd,
        getattr(settings, "chili_momentum_risk_daily_loss_fraction_of_equity", 0.05),
        execution_family,
        prefer_equity=True,
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


def adaptive_watch_fanout(field_size: int | None = None) -> int:
    """Adaptive WATCH-fanout cap (CHUNK 2 engine core): how many $0-risk pre-fill
    watchers the lane may hold concurrently.

    A machine's edge is BREADTH — when 40 names are igniting it should watch more
    than when 3 are. The cap floats with the live field rather than a flat 15:

        cap = clamp(field_size, floor, watch_fanout_max)

    where ``field_size`` = the count of distinct live-eligible names right now
    (passed by the caller from the same source the arm queue ranks from), ``floor``
    = ``chili_momentum_watch_fanout_floor`` (the ONE documented base — never watch
    fewer than this so the lane is always primed for the first igniter), and
    ``watch_fanout_max`` is the hard upper bound (the documented per-tick
    processing-cost ceiling). Watchers are FREE ($0 risk); the atomic risk-budget
    governs real admission, so widening the watch field never widens risk.

    When the adaptive flag is OFF, or ``field_size`` is unavailable/unusable, this
    returns the flat ``chili_momentum_watch_fanout_max`` — BYTE-IDENTICAL to the
    legacy flat cap. docs/DESIGN/MOMENTUM_ENGINE.md §2."""
    try:
        hard_max = int(getattr(settings, "chili_momentum_watch_fanout_max", 15) or 15)
    except (TypeError, ValueError):
        hard_max = 15
    hard_max = max(1, hard_max)
    if not bool(getattr(settings, "chili_momentum_watch_fanout_adaptive_enabled", True)):
        return hard_max
    if field_size is None:
        return hard_max
    try:
        fs = int(field_size)
    except (TypeError, ValueError):
        return hard_max
    if fs < 0:
        return hard_max
    try:
        floor = int(getattr(settings, "chili_momentum_watch_fanout_floor", 5) or 5)
    except (TypeError, ValueError):
        floor = 5
    floor = max(1, min(floor, hard_max))
    return max(floor, min(fs, hard_max))


def admit_by_aggregate_risk(
    *,
    open_risk_usd: float,
    candidate_risk_usd: float,
    equity_usd: float | None,
    budget_fraction: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    """ATOMIC SHAPE-AWARE risk-budget admission decision (pure, zero-I/O, testable).

    Admit a new entry iff::

        open_risk_usd + candidate_risk_usd <= budget_fraction * equity_usd

    where ``candidate_risk_usd`` is the candidate's ACTUAL shape-aware dollars-at-
    risk = ``(entry_price - structural_stop_price) * fill_qty`` (so a tight-stop
    scalp consumes far less budget than a wide-stop trade of the same notional — the
    count would have treated them equally), ``open_risk_usd`` = the summed entry-to-
    stop $ across currently-OPEN positions (``aggregate_open_risk_usd``), and the
    budget = ``budget_fraction`` (defaults to the REUSED
    ``chili_momentum_max_aggregate_risk_pct_of_equity``, no new magic number) times
    equity.

    The caller MUST evaluate this INSIDE the per-(user,lane) advisory lock so two
    near-simultaneous fills cannot both pass against a stale ``open_risk_usd`` (the
    fill-burst race) — this function is the pure arithmetic; the lock is the
    serializer.

    FAIL-CLOSED on an unknown/unusable account or candidate risk: a non-positive /
    non-finite ``equity_usd`` returns ``admit=False`` (never size against an unknown
    account — [[feedback_adaptive_no_magic]]); a non-finite/negative candidate risk
    returns ``admit=False``. A budget_fraction <= 0 DISABLES the gate (admit=True,
    reason='budget_disabled') — the operator's documented kill of the dollar cap.

    Returns ``(admit, meta)``."""
    if budget_fraction is None:
        try:
            budget_fraction = float(getattr(
                settings, "chili_momentum_max_aggregate_risk_pct_of_equity", 0.03) or 0.0)
        except (TypeError, ValueError):
            budget_fraction = 0.0
    try:
        bf = float(budget_fraction)
    except (TypeError, ValueError):
        bf = 0.0
    if not math.isfinite(bf):
        return False, {
            "reason": "budget_fraction_invalid",
            "budget_fraction": budget_fraction,
        }
    if bf <= 0:
        return True, {"reason": "budget_disabled", "budget_fraction": bf}
    try:
        eq = float(equity_usd) if equity_usd is not None else 0.0
    except (TypeError, ValueError):
        eq = 0.0
    if eq <= 0 or not math.isfinite(eq):
        return False, {"reason": "equity_unavailable", "budget_fraction": bf}
    try:
        cand = float(candidate_risk_usd)
    except (TypeError, ValueError):
        cand = float("nan")
    if not math.isfinite(cand) or cand <= 0:
        return False, {"reason": "candidate_risk_invalid",
                       "candidate_risk_usd": candidate_risk_usd}
    try:
        opn = float(open_risk_usd)
    except (TypeError, ValueError):
        opn = float("nan")
    if not math.isfinite(opn) or opn < 0:
        return False, {
            "reason": "open_risk_invalid",
            "open_risk_usd": open_risk_usd,
        }
    cap = bf * eq
    projected = opn + cand
    admit = projected <= cap + 1e-9
    return admit, {
        "open_risk_usd": round(opn, 2),
        "candidate_risk_usd": round(cand, 2),
        "projected_usd": round(projected, 2),
        "cap_usd": round(cap, 2),
        "budget_fraction": bf,
        "equity_usd": round(eq, 2),
    }


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
            # Replay/live parity: never let a historical decision observe an outcome
            # that terminates after its causal frontier.  In production this is simply
            # ``now``; under ``replay_risk_clock`` it is the recorded tick instant.
            MomentumAutomationOutcome.terminal_at <= _risk_now_naive(),
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

        day = global_realized_pnl_today_et(db, as_of_utc=_risk_now_aware())
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


def _smoothstep(x: float) -> float:
    """Hermite smoothstep on [0,1]: 0 at/below 0, 1 at/above 1, smooth S in between.
    Used for the continuous (no fixed step) size ramps."""
    if not math.isfinite(x):
        return 1.0
    t = max(0.0, min(1.0, float(x)))
    return t * t * (3.0 - 2.0 * t)


def daily_room_size_down_multiplier(
    dist_to_sma_200_atr: float | None,
    dist_to_resistance_atr: float | None,
) -> tuple[float, dict[str, Any]]:
    """ROSS RISK GAP 1 — size-DOWN approaching the daily 200MA / overhead resistance.

    Ross cuts share size as price approaches a clear overhead wall (the daily 200MA from
    BELOW, or the nearest overhead resistance) — full size with lots of clear sky, smaller
    into the wall. A CONTINUOUS multiplier in ``[floor, 1.0]`` computed by a smoothstep over
    the signed daily-ATR distance to the nearest BINDING overhead level:

      room_atr = the closest overhead distance among {200MA-from-below, resistance}
                 (a NEGATIVE / zero distance means at-or-above the level ⇒ at the wall)
      mult     = floor + (1 - floor) * smoothstep(room_atr / band_atr)

    Only the SIGNED distance matters and only when the level is OVERHEAD (distance >= 0 = the
    wall is above/at price). When price is already comfortably ABOVE the 200MA (a large
    POSITIVE dist_to_sma_200_atr) that level is NOT overhead and contributes no size-down (a
    momentum name extended above its 200MA is exactly where Ross presses, not trims). The
    nearest overhead resistance ATR is always treated as a wall-from-below (it is computed as
    distance-to-overhead). SIZE-DOWN ONLY (mult <= 1.0 by construction; never sizes up). The
    band + floor are the ONE documented base each (FLOORS, not scattered caps). FAIL-OPEN:
    no usable distance / flag OFF (handled by the caller) ⇒ ``(1.0, ...)`` byte-identical.
    Pure (no I/O). docs/DESIGN/MOMENTUM_LANE.md [[project_ross_self_ref_daily_resistance]]"""
    band = float(getattr(settings, "chili_momentum_daily_room_band_atr", 2.0) or 2.0)
    floor = float(getattr(settings, "chili_momentum_daily_room_size_floor", 0.4) or 0.4)
    if not math.isfinite(band) or band <= 0:
        band = 2.0
    if not (0.05 <= floor <= 1.0):
        floor = 0.4
    # Collect the overhead-distance candidates (daily-ATR units). The resistance distance is
    # already a "distance to the nearest level ABOVE", so it is a wall whenever it is finite.
    # The 200MA is only a wall when price is BELOW it (a negative signed dist = above the MA =
    # not overhead). We take the SMALLEST overhead room (the binding, nearest wall).
    candidates: list[float] = []
    try:
        d_res = float(dist_to_resistance_atr) if dist_to_resistance_atr is not None else None
    except (TypeError, ValueError):
        d_res = None
    if d_res is not None and math.isfinite(d_res):
        candidates.append(max(0.0, d_res))
    try:
        d200 = float(dist_to_sma_200_atr) if dist_to_sma_200_atr is not None else None
    except (TypeError, ValueError):
        d200 = None
    # Signed daily-ATR units: + above the 200MA, - below. The MA is overhead only when price
    # is BELOW it (d200 < 0); the room-to-the-wall is then |d200|. Above it ⇒ not a wall.
    if d200 is not None and math.isfinite(d200) and d200 < 0:
        candidates.append(abs(d200))
    if not candidates:
        return 1.0, {"daily_room_mult": 1.0, "reason": "no_overhead_distance"}
    room_atr = min(candidates)
    mult = floor + (1.0 - floor) * _smoothstep(room_atr / band)
    # Numeric safety: clamp into [floor, 1.0] (size-DOWN only).
    mult = max(floor, min(1.0, mult))
    return float(mult), {
        "daily_room_mult": round(float(mult), 4),
        "room_atr": round(float(room_atr), 4),
        "band_atr": round(band, 4),
        "floor": round(floor, 4),
        "dist_to_sma_200_atr": (None if d200 is None else round(d200, 4)),
        "dist_to_resistance_atr": (None if d_res is None else round(d_res, 4)),
    }


def red_intraday_size_down_multiplier(
    db: Any, *, base_loss_usd: float, user_id: int | None = None
) -> tuple[float, dict[str, Any]]:
    """ROSS RISK GAP 2 — size-DOWN when down on the day (the cushion ladder, down side).

    Ross trades SMALLER when red on the day. ``cushion_risk_multiplier`` is the UP side
    (it climbs a ladder off banked GREEN cushion, floored at 1.0); this is the missing DOWN
    side. A CONTINUOUS multiplier in ``[floor, 1.0]`` keyed on today's REALIZED P&L when it is
    NEGATIVE, scaled by how deep the red is in UNITS of the day's per-trade risk budget
    (self-relative / adaptive — no fixed $):

      red_units = max(0, -realized_today) / base_loss_usd
      mult      = clamp(1 - (1 - floor) * red_units / full_down_units, floor, 1.0)

    Green / flat today ⇒ ``red_units == 0`` ⇒ mult 1.0 (byte-identical). At ``full_down_units``
    of red (e.g. down ~2x the per-trade loss budget) the size reaches the documented floor.
    SIZE-DOWN ONLY (never raises). The daily-loss cap + drawdown breaker remain the hard
    downside bound ABOVE this soft de-risk. ADDITIVE / FAIL-NEUTRAL: flag OFF (handled by the
    caller), no db, a degenerate base, or any error ⇒ ``(1.0, ...)`` (never increases risk,
    never blocks). Read-only. docs/DESIGN/MOMENTUM_LANE.md [[reference_ross_video_2026_06_11]]"""
    try:
        full_down = float(getattr(settings, "chili_momentum_red_intraday_full_down_units", 2.0) or 2.0)
        floor = float(getattr(settings, "chili_momentum_red_intraday_size_floor", 0.4) or 0.4)
        if not math.isfinite(full_down) or full_down <= 0:
            full_down = 2.0
        if not (0.05 <= floor <= 1.0):
            floor = 0.4
        base = float(base_loss_usd or 0.0)
        if base <= 0 or not math.isfinite(base):
            return 1.0, {"red_intraday_mult": 1.0, "reason": "no_base_loss"}
        from ..governance import global_realized_pnl_today_et

        day = global_realized_pnl_today_et(
            db, user_id, as_of_utc=_risk_now_aware()
        )
        realized = float(day.get("total_usd") or 0.0)
        if realized >= 0.0:
            return 1.0, {
                "red_intraday_mult": 1.0,
                "reason": "not_red",
                "day_realized_usd": round(realized, 2),
            }
        red_units = (-realized) / base
        mult = 1.0 - (1.0 - floor) * (red_units / full_down)
        mult = max(floor, min(1.0, mult))
        return float(mult), {
            "red_intraday_mult": round(float(mult), 4),
            "day_realized_usd": round(realized, 2),
            "red_units": round(float(red_units), 4),
            "full_down_units": round(full_down, 4),
            "floor": round(floor, 4),
            "base_loss_usd": round(base, 2),
        }
    except Exception:
        return 1.0, {"red_intraday_mult": 1.0, "reason": "error_fail_neutral"}


@dataclass(frozen=True)
class CurrentLiveLossHistoryEntry:
    """One broker-authoritative, causally available entered outcome."""

    session_id: int
    outcome_id: int
    symbol: str
    terminal_at: datetime
    outcome_class: str
    realized_pnl_usd: float
    return_bps: float | None
    broker_reconciled_at: datetime


CurrentLiveLossHistoryReceipt = tuple[
    tuple[CurrentLiveLossHistoryEntry, ...],
    dict[str, Any],
]


_LOSS_HISTORY_TERMINAL_STATES = frozenset(
    {
        "live_exited",
        "live_cooldown",
        "live_finished",
        "live_cancelled",
        "live_error",
        "expired",
        "archived",
    }
)
_LOSS_HISTORY_ENTRY_EVENTS = frozenset(
    {
        "live_entry_filled",
        "live_exit_filled",
        "live_partial_exit_filled",
    }
)
_LOSS_HISTORY_ENTRY_IMPLYING_TERMINAL_STATES = frozenset(
    {"live_exited", "live_cooldown", "live_finished"}
)
_LOSS_HISTORY_AMBIGUOUS_ENTRY_CLASSES = frozenset(
    {
        "cancelled_in_trade",
        "error_exit",
        "flat_unknown",
        "governance_exit",
        "stale_data_abort",
    }
)


def _loss_history_finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        resolved = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return resolved if math.isfinite(resolved) else None


def _loss_history_numeric_state(value: Any) -> str:
    """Classify an optional numeric label without treating corruption as proof."""

    if value is None:
        return "absent"
    if isinstance(value, bool):
        return "invalid"
    try:
        resolved = float(value)
    except (TypeError, ValueError, OverflowError):
        return "invalid"
    if not math.isfinite(resolved):
        return "invalid"
    return "zero" if abs(resolved) <= 1e-12 else "nonzero"


def _loss_history_notional_state(value: Any) -> str:
    if value is None:
        return "absent"
    if isinstance(value, bool):
        return "invalid"
    try:
        resolved = float(value)
    except (TypeError, ValueError, OverflowError):
        return "invalid"
    if not math.isfinite(resolved):
        return "invalid"
    return "positive" if resolved > 0.0 else "nonpositive"


def _loss_history_sign(value: float) -> int:
    if value > 1e-12:
        return 1
    if value < -1e-12:
        return -1
    return 0


def _loss_history_naive_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _loss_history_snapshot_entry_proof(session: Any) -> bool:
    snapshot = (
        session.risk_snapshot_json
        if isinstance(getattr(session, "risk_snapshot_json", None), dict)
        else {}
    )
    live = snapshot.get("momentum_live_execution")
    if not isinstance(live, dict):
        return False
    # Same durable proof as outcome_extract._entry_occurred_durable: unlike
    # position quantity, these survive terminal broker-zero reconciliation.
    return (
        live.get("realized_pnl_usd") is not None
        or live.get("last_exit_entry_price") is not None
    )


def _loss_history_generation_state(
    session: Any,
    *,
    execution_family: str,
    account_scope: str | None,
    account_identity: str,
) -> str:
    """Return ``current``, ``other``, or ``unknown`` account generation."""

    snapshot = (
        session.risk_snapshot_json
        if isinstance(getattr(session, "risk_snapshot_json", None), dict)
        else {}
    )
    if execution_family in {"alpaca_spot", "alpaca_short"}:
        stored_scope = str(snapshot.get("alpaca_account_scope") or "").strip().lower()
        stored_identity = str(snapshot.get("alpaca_account_id") or "").strip()
        if not stored_scope or not stored_identity:
            return "unknown"
        if stored_scope != str(account_scope or "").strip().lower():
            return "other"
        return "current" if stored_identity == account_identity else "other"
    stored_identity = str(
        snapshot.get("non_alpaca_account_identity") or ""
    ).strip()
    if not stored_identity:
        return "unknown"
    return "current" if stored_identity == account_identity else "other"


def _loss_history_entry_classification(
    outcome: Any,
    session: Any,
    *,
    entry_event_seen: bool,
) -> str:
    """Classify as ``entered``, explicit ``not_entered``, ``unknown``, or conflict."""

    from .outcome_labels import ALL_OUTCOME_CLASSES, NEVER_ENTERED_OUTCOMES

    outcome_class = str(getattr(outcome, "outcome_class", "") or "").strip().lower()
    durable_runtime = bool(entry_event_seen) or _loss_history_snapshot_entry_proof(
        session
    )
    economic_states = tuple(
        _loss_history_numeric_state(value)
        for value in (
            getattr(outcome, "realized_pnl_usd", None),
            getattr(outcome, "return_bps", None),
            getattr(outcome, "broker_realized_pnl_usd", None),
            getattr(outcome, "broker_return_bps", None),
        )
    )
    economic_nonzero = "nonzero" in economic_states
    economic_invalid = "invalid" in economic_states
    notional_state = _loss_history_notional_state(
        getattr(outcome, "broker_notional_basis_usd", None)
    )
    if notional_state in {"invalid", "nonpositive"}:
        return "conflict"
    durable_runtime = durable_runtime or notional_state == "positive"

    if outcome_class not in ALL_OUTCOME_CLASSES:
        return "unknown"
    if outcome_class in NEVER_ENTERED_OUTCOMES:
        if durable_runtime or economic_nonzero or economic_invalid:
            return "conflict"
        return "not_entered"
    if outcome_class in _LOSS_HISTORY_AMBIGUOUS_ENTRY_CLASSES:
        if durable_runtime or economic_nonzero:
            return "entered"
        if economic_invalid:
            return "conflict"
        return "unknown"
    # stop_loss/success/bailout/timed_exit/etc. explicitly describe economics.
    # Missing broker data makes coverage unavailable; it does not erase the trade.
    return "entered"


def load_current_live_loss_history(
    db: Any,
    *,
    user_id: int | None,
    execution_family: str | None,
    account_scope: str | None = None,
    account_identity: str | None = None,
    decision_as_of: datetime | None = None,
) -> CurrentLiveLossHistoryReceipt:
    """Load one ET day's complete broker-authoritative loss-guard ledger.

    The reader starts from the same-lane terminal-session inventory, not an
    inner join of whichever outcome rows happen to exist. Every durable entered
    session in the selected account generation must have an outcome and a finite
    broker P&L whose reconciliation availability clock crossed the frontier.

    This is mutable operational-DB history for a *current live* decision only.
    It is never a ReplayV3 receipt and never falls back to legacy self-report.
    """

    raw_family = str(execution_family or "").strip()
    try:
        uid = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        uid = None
    family = normalize_execution_family(raw_family) if raw_family else ""
    scope = str(account_scope or "").strip().lower()
    identity = str(account_identity or "").strip()
    meta: dict[str, Any] = {
        "schema_version": "chili.current-live-loss-history.v2",
        "user_id": uid,
        "execution_family": family or None,
        "account_scope": scope or None,
        "account_identity_bound": bool(identity),
        "history_authority": "broker_reconciled_current_live_db_only",
        "label_source": "momentum_automation_outcomes.broker_*",
        "legacy_pnl_fallback_used": False,
        "replay_certifiable": False,
    }
    if db is None:
        return (), {
            **meta,
            "reason": "loss_guard_db_unavailable",
            "history_unavailable": True,
        }
    if uid is None:
        return (), {
            **meta,
            "reason": "loss_guard_user_unavailable",
            "required_scope_unavailable": True,
        }
    if not family:
        return (), {
            **meta,
            "reason": "loss_guard_execution_family_unavailable",
            "required_scope_unavailable": True,
        }
    if family in {"alpaca_spot", "alpaca_short"} and scope != "alpaca:paper":
        return (), {
            **meta,
            "reason": "alpaca_loss_guard_scope_unavailable",
            "required_scope_unavailable": True,
        }
    if not identity:
        return (), {
            **meta,
            "reason": (
                "alpaca_loss_guard_identity_unavailable"
                if family in {"alpaca_spot", "alpaca_short"}
                else "non_alpaca_loss_guard_identity_unavailable"
            ),
            "required_scope_unavailable": True,
        }

    frontier = decision_as_of or _risk_now_aware()
    if frontier.tzinfo is None:
        frontier = frontier.replace(tzinfo=timezone.utc)
    else:
        frontier = frontier.astimezone(timezone.utc)
    frontier_utc = frontier.replace(tzinfo=None)
    day_start, day_end = _et_day_bounds_utc(as_of_utc=frontier)
    meta["decision_as_of_utc"] = frontier.isoformat()

    try:
        from ....models.trading import (
            MomentumAutomationOutcome,
            TradingAutomationEvent,
            TradingAutomationSession,
        )

        outcome_rows = (
            db.query(MomentumAutomationOutcome, TradingAutomationSession)
            .join(
                TradingAutomationSession,
                TradingAutomationSession.id
                == MomentumAutomationOutcome.session_id,
            )
            .filter(
                MomentumAutomationOutcome.mode == "live",
                TradingAutomationSession.mode == "live",
                MomentumAutomationOutcome.user_id == uid,
                TradingAutomationSession.user_id == uid,
                MomentumAutomationOutcome.execution_family == family,
                TradingAutomationSession.execution_family == family,
                MomentumAutomationOutcome.terminal_at >= day_start,
                MomentumAutomationOutcome.terminal_at < day_end,
                MomentumAutomationOutcome.terminal_at <= frontier_utc,
                MomentumAutomationOutcome.created_at <= frontier_utc,
            )
            .order_by(
                MomentumAutomationOutcome.terminal_at.desc(),
                MomentumAutomationOutcome.id.desc(),
            )
            .all()
        )

        terminal_clock = func.coalesce(
            TradingAutomationSession.ended_at,
            TradingAutomationSession.updated_at,
        )
        terminal_rows = (
            db.query(TradingAutomationSession, MomentumAutomationOutcome)
            .outerjoin(
                MomentumAutomationOutcome,
                and_(
                    MomentumAutomationOutcome.session_id
                    == TradingAutomationSession.id,
                    MomentumAutomationOutcome.created_at <= frontier_utc,
                ),
            )
            .filter(
                TradingAutomationSession.mode == "live",
                TradingAutomationSession.user_id == uid,
                TradingAutomationSession.execution_family == family,
                TradingAutomationSession.state.in_(
                    tuple(sorted(_LOSS_HISTORY_TERMINAL_STATES))
                ),
                terminal_clock >= day_start,
                terminal_clock < day_end,
                terminal_clock <= frontier_utc,
                TradingAutomationSession.created_at <= frontier_utc,
            )
            .order_by(
                terminal_clock.desc(),
                TradingAutomationSession.id.desc(),
            )
            .all()
        )

        session_ids = {
            int(session.id) for _outcome, session in outcome_rows
        } | {int(session.id) for session, _outcome in terminal_rows}
        event_bounds: dict[int, tuple[datetime, datetime]] = {}
        for outcome, session in outcome_rows:
            lower = _loss_history_naive_utc(session.created_at)
            outcome_terminal = _loss_history_naive_utc(outcome.terminal_at)
            session_terminal = _loss_history_naive_utc(
                session.ended_at or session.updated_at
            )
            if lower is not None and outcome_terminal is not None and session_terminal is not None:
                event_bounds[int(session.id)] = (
                    lower,
                    min(outcome_terminal, session_terminal, frontier_utc),
                )
        for session, _outcome in terminal_rows:
            session_id = int(session.id)
            if session_id in event_bounds:
                continue
            lower = _loss_history_naive_utc(session.created_at)
            upper = _loss_history_naive_utc(session.ended_at or session.updated_at)
            if lower is not None and upper is not None:
                event_bounds[session_id] = (lower, min(upper, frontier_utc))
        event_entry_session_ids: set[int] = set()
        if session_ids:
            event_rows = (
                db.query(
                    TradingAutomationEvent.session_id,
                    TradingAutomationEvent.ts,
                )
                .filter(
                    TradingAutomationEvent.session_id.in_(tuple(session_ids)),
                    TradingAutomationEvent.event_type.in_(
                        tuple(sorted(_LOSS_HISTORY_ENTRY_EVENTS))
                    ),
                    TradingAutomationEvent.ts <= frontier_utc,
                )
                .all()
            )
            for session_id, event_ts in event_rows:
                bounds = event_bounds.get(int(session_id))
                normalized_event_ts = _loss_history_naive_utc(event_ts)
                if (
                    bounds is not None
                    and normalized_event_ts is not None
                    and bounds[0] <= normalized_event_ts <= bounds[1]
                ):
                    event_entry_session_ids.add(int(session_id))
    except Exception:
        logger.debug(
            "[momentum_neural] current-live loss-history read failed",
            exc_info=True,
        )
        return (), {
            **meta,
            "reason": "loss_guard_history_unavailable",
            "history_unavailable": True,
        }

    gaps: dict[str, list[int]] = {}

    def _gap(reason: str, session_id: int) -> None:
        gaps.setdefault(reason, []).append(int(session_id))

    entries: list[CurrentLiveLossHistoryEntry] = []
    processed_session_ids: set[int] = set()
    bps_fallbacks = 0
    for outcome, session in outcome_rows:
        session_id = int(session.id)
        processed_session_ids.add(session_id)
        classification = _loss_history_entry_classification(
            outcome,
            session,
            entry_event_seen=session_id in event_entry_session_ids,
        )
        if classification == "not_entered":
            continue
        generation = _loss_history_generation_state(
            session,
            execution_family=family,
            account_scope=scope or None,
            account_identity=identity,
        )
        if generation == "other":
            continue
        if generation == "unknown":
            _gap("loss_guard_account_generation_unknown", session_id)
            continue
        if classification == "conflict":
            _gap("loss_guard_entry_classification_conflict", session_id)
            continue
        if classification == "unknown":
            _gap("loss_guard_entry_classification_unknown", session_id)
            continue
        if str(session.state or "") not in _LOSS_HISTORY_TERMINAL_STATES:
            _gap("loss_guard_outcome_session_state_mismatch", session_id)
            continue
        session_terminal_at = _loss_history_naive_utc(
            session.ended_at or session.updated_at
        )
        if session_terminal_at is None:
            _gap("loss_guard_session_terminal_clock_unavailable", session_id)
            continue
        if not (
            day_start <= session_terminal_at < day_end
            and session_terminal_at <= frontier_utc
        ):
            _gap("loss_guard_session_terminal_frontier_mismatch", session_id)
            continue
        terminal_at = _loss_history_naive_utc(outcome.terminal_at)
        if terminal_at is None:
            _gap("loss_guard_outcome_terminal_clock_unavailable", session_id)
            continue
        if terminal_at != session_terminal_at:
            _gap("loss_guard_outcome_session_terminal_clock_mismatch", session_id)
            continue

        # The legacy Alpaca session/outcome ledger cannot prove broker-NET cycle
        # economics.  Its order payload has no per-fill fee; the old runner
        # coerces that missing value to numeric zero before writing
        # ``momentum_fill_outcomes``.  The legacy reconciler can therefore label a
        # gross result ``reconciled`` merely because a fabricated zero is
        # non-NULL, and it also collapses recycled/multi-exit sessions into one
        # row.  Neither condition may authorize another PAPER entry.  Keep the
        # current session reader explicitly unavailable until the immutable
        # reservation-scoped fill/cycle settlement ledger supersedes this branch.
        if family in {"alpaca_spot", "alpaca_short"}:
            _gap("loss_guard_alpaca_cycle_settlement_unavailable", session_id)
            continue

        if str(outcome.broker_recon_status or "").strip().lower() != "reconciled":
            _gap("loss_guard_broker_reconciliation_unavailable", session_id)
            continue
        reconciled_at = outcome.broker_reconciled_at
        if not isinstance(reconciled_at, datetime):
            _gap("loss_guard_broker_reconciled_at_unavailable", session_id)
            continue
        reconciled_at = _loss_history_naive_utc(reconciled_at)
        if reconciled_at is None:
            _gap("loss_guard_broker_reconciled_at_unavailable", session_id)
            continue
        if reconciled_at > frontier_utc:
            _gap("loss_guard_broker_label_not_available_as_of", session_id)
            continue
        if reconciled_at < terminal_at:
            _gap("loss_guard_broker_label_precedes_terminal", session_id)
            continue
        broker_pnl = _loss_history_finite_float(outcome.broker_realized_pnl_usd)
        if broker_pnl is None:
            _gap("loss_guard_broker_pnl_nonfinite_or_unavailable", session_id)
            continue
        broker_bps = _loss_history_finite_float(outcome.broker_return_bps)
        if broker_bps is None:
            bps_fallbacks += 1
        elif _loss_history_sign(broker_pnl) != _loss_history_sign(broker_bps):
            _gap("loss_guard_broker_label_sign_mismatch", session_id)
            continue
        broker_notional = _loss_history_finite_float(
            outcome.broker_notional_basis_usd
        )
        if broker_notional is not None and broker_notional > 0.0 and broker_bps is not None:
            expected_bps = (broker_pnl / broker_notional) * 10_000.0
            if not math.isclose(
                broker_bps,
                expected_bps,
                rel_tol=1e-6,
                abs_tol=1e-6,
            ):
                _gap("loss_guard_broker_return_formula_mismatch", session_id)
                continue
        broker_win = outcome.broker_win
        if broker_win is not None:
            if not isinstance(broker_win, bool):
                _gap("loss_guard_broker_win_invalid", session_id)
                continue
            pnl_win = _loss_history_sign(broker_pnl) > 0
            bps_win = (
                _loss_history_sign(broker_bps) > 0
                if broker_bps is not None
                else pnl_win
            )
            if broker_win != pnl_win or broker_win != bps_win:
                _gap("loss_guard_broker_win_mismatch", session_id)
                continue
        outcome_symbol = str(outcome.symbol or "").strip().upper()
        session_symbol = str(session.symbol or "").strip().upper()
        if not outcome_symbol or not session_symbol:
            _gap("loss_guard_symbol_unavailable", session_id)
            continue
        if outcome_symbol != session_symbol:
            _gap("loss_guard_symbol_mismatch", session_id)
            continue
        symbol = outcome_symbol
        entries.append(
            CurrentLiveLossHistoryEntry(
                session_id=session_id,
                outcome_id=int(outcome.id),
                symbol=symbol,
                terminal_at=terminal_at,
                outcome_class=str(outcome.outcome_class or "").strip().lower(),
                realized_pnl_usd=broker_pnl,
                return_bps=broker_bps,
                broker_reconciled_at=reconciled_at,
            )
        )

    # A terminal/flat session can exist before its asynchronously emitted outcome.
    # Post-entry terminal states are themselves an incomplete-history signal even
    # if a crash lost the snapshot/events; they can never be treated as no-entry.
    for session, outcome in terminal_rows:
        session_id = int(session.id)
        if session_id in processed_session_ids:
            continue
        generation = _loss_history_generation_state(
            session,
            execution_family=family,
            account_scope=scope or None,
            account_identity=identity,
        )
        if generation == "other":
            continue
        if outcome is None:
            durable = (
                str(session.state or "")
                in _LOSS_HISTORY_ENTRY_IMPLYING_TERMINAL_STATES
                or _loss_history_snapshot_entry_proof(session)
                or session_id in event_entry_session_ids
            )
        else:
            durable = _loss_history_entry_classification(
                outcome,
                session,
                entry_event_seen=session_id in event_entry_session_ids,
            ) != "not_entered"
        if not durable:
            continue
        if generation == "unknown":
            _gap("loss_guard_account_generation_unknown", session_id)
        elif outcome is None:
            _gap("loss_guard_terminal_outcome_unavailable", session_id)
        else:
            _gap("loss_guard_outcome_frontier_mismatch", session_id)

    if gaps:
        first_reason = next(iter(gaps))
        return (), {
            **meta,
            "reason": first_reason,
            "history_unavailable": True,
            "coverage_grade": "COVERAGE_UNAVAILABLE",
            "coverage_gap_counts": {
                reason: len(ids) for reason, ids in gaps.items()
            },
            "coverage_gap_session_ids": sorted(
                {session_id for ids in gaps.values() for session_id in ids}
            ),
            "terminal_session_inventory_count": len(terminal_rows),
            "outcome_inventory_count": len(outcome_rows),
        }

    return tuple(entries), {
        **meta,
        "history_available": True,
        "coverage_grade": "CURRENT_LIVE_COMPLETE",
        "terminal_session_inventory_count": len(terminal_rows),
        "outcome_inventory_count": len(outcome_rows),
        "durable_entered_outcomes": len(entries),
        "broker_return_bps_fixed_cooldown_fallbacks": int(bps_fallbacks),
    }


def account_wide_consecutive_losses(
    db: Any,
    *,
    user_id: int | None,
    execution_family: str | None,
    account_scope: str | None = None,
    account_identity: str | None = None,
    decision_as_of: datetime | None = None,
    lookback: int = 40,
    _current_live_history: CurrentLiveLossHistoryReceipt | None = None,
) -> tuple[int, dict[str, Any]]:
    """Count consecutive realized losses for one account-scoped execution lane.

    The guard is account-wide across symbols, but never process-global across
    unrelated users, execution families, or broker accounts.  Alpaca paper rows
    are included for Alpaca paper decisions and must match the exact account scope
    and frozen non-secret account UUID persisted on their owning session.

    The ET-day window and terminal frontier use ``decision_as_of`` (or the bound
    risk clock).  This current-live-DB reader also requires ``created_at`` to have
    crossed the decision frontier, preventing a late/backfilled row from appearing
    in an earlier diagnostic decision.  That is necessary but not sufficient for
    ReplayV3 certification: sealed replay must use an append-only recorded history
    receipt with its own availability clock and must not call this DB reader.

    A missing account-generation identity or unreadable history is reported
    explicitly; the halt decision converts either condition to a fail-closed
    new-arm stop without fabricating a loss count.
    """
    if lookback <= 0:
        return 0, {
            "consecutive_losses": 0,
            "lookback": int(lookback),
            "reason": "loss_guard_lookback_invalid",
            "history_unavailable": True,
            "replay_certifiable": False,
        }
    history = _current_live_history or load_current_live_loss_history(
        db,
        user_id=user_id,
        execution_family=execution_family,
        account_scope=account_scope,
        account_identity=account_identity,
        decision_as_of=decision_as_of,
    )
    entries, history_meta = history
    meta = {
        **history_meta,
        "consecutive_losses": 0,
        "lookback": int(lookback),
    }
    if (
        history_meta.get("required_scope_unavailable") is True
        or history_meta.get("history_unavailable") is True
    ):
        return 0, meta

    consec = 0
    seen = 0
    # The shared classifier already removed true no-entry rows and sorted newest
    # first. Apply the bounded lookback only now so no-fill churn cannot push real
    # losses out of the window.
    for entry in entries[: int(lookback)]:
        seen += 1
        if entry.realized_pnl_usd < 0:
            consec += 1
        else:
            break  # a win (or break-even) ends the consecutive-loss run
    return consec, {
        **meta,
        "consecutive_losses": int(consec),
        "real_entries_today_seen": int(seen),
        "history_available": True,
    }


def consecutive_loss_halt_decision(
    db: Any,
    *,
    user_id: int | None,
    execution_family: str | None,
    account_scope: str | None = None,
    account_identity: str | None = None,
    decision_as_of: datetime | None = None,
    _current_live_history: CurrentLiveLossHistoryReceipt | None = None,
) -> tuple[bool, dict[str, Any]]:
    """ROSS RISK GAP 3 — account-wide consecutive-loss ARM HALT decision (HALTS ARMING ONLY).

    After ``chili_momentum_consecutive_loss_halt_count`` consecutive account-wide realized
    losses today (see ``account_wide_consecutive_losses``), HALT NEW ARMING for the rest of
    the ET session. This is an ADDITIONAL count-based halt that composes with the dollar-based
    daily-loss cap; it resets on a win or a new ET day, and is reversible.

    Returns ``(halted, meta)``. ⚠️ The caller MUST only gate NEW ARMS with this — open
    positions still manage + exit normally (this never runs on any exit path).
    Disabled remains a no-op, but missing scope/history fails CLOSED for new arms.
    It never blocks management or exits of an existing position."""
    if not bool(getattr(settings, "chili_momentum_consecutive_loss_halt_enabled", True)):
        return False, {"halted": False, "reason": "disabled"}
    try:
        threshold = int(getattr(settings, "chili_momentum_consecutive_loss_halt_count", 4) or 4)
        if threshold < 2:
            threshold = 2
        consec, meta = account_wide_consecutive_losses(
            db,
            user_id=user_id,
            execution_family=execution_family,
            account_scope=account_scope,
            account_identity=account_identity,
            decision_as_of=decision_as_of,
            _current_live_history=_current_live_history,
        )
        provenance = {
            "halt_count": {
                "value": int(threshold),
                "source": "settings.chili_momentum_consecutive_loss_halt_count",
            },
            "enabled": {
                "value": True,
                "source": "settings.chili_momentum_consecutive_loss_halt_enabled",
            },
            "validation_status": "offline_oos_required",
        }
        if (
            meta.get("required_scope_unavailable") is True
            or meta.get("history_unavailable") is True
        ):
            return True, {
                "halted": True,
                "consecutive_losses": 0,
                "halt_count": int(threshold),
                "reason": str(meta.get("reason") or "loss_guard_scope_unavailable"),
                "required_scope_unavailable": bool(
                    meta.get("required_scope_unavailable")
                ),
                "history_unavailable": bool(meta.get("history_unavailable")),
                "config_provenance": provenance,
            }
        halted = consec >= threshold
        return halted, {
            "halted": bool(halted),
            "consecutive_losses": int(consec),
            "halt_count": int(threshold),
            "config_provenance": provenance,
            **{k: v for k, v in meta.items() if k not in ("consecutive_losses",)},
        }
    except Exception:
        return True, {
            "halted": True,
            "consecutive_losses": 0,
            "reason": "loss_guard_history_unavailable",
            "history_unavailable": True,
        }


def _et_day_bounds_utc(
    *, days_ago: int = 0, as_of_utc: datetime | None = None
) -> tuple[datetime, datetime]:
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
    reference = as_of_utc or _risk_now_aware()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    today_et_date = reference.astimezone(et).date()
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
        frontier_utc = _risk_now_naive()
        rows = (
            db.query(MomentumAutomationOutcome.outcome_class)
            .filter(
                MomentumAutomationOutcome.execution_family == execution_family,
                MomentumAutomationOutcome.mode == "live",
                MomentumAutomationOutcome.terminal_at >= start_utc,
                MomentumAutomationOutcome.terminal_at < end_utc,
                MomentumAutomationOutcome.terminal_at <= frontier_utc,
            )
            .all()
        )
        return sum(1 for (oc,) in rows if is_real_entry_outcome(oc))
    except Exception:
        logger.debug("[momentum_neural] daily entry-count read failed", exc_info=True)
        return 0


def _count_symbol_episodes_today(
    db: Any, *, execution_family: str | None
) -> tuple[int, set[str], dict[str, Any]]:
    """A1(a) SYMBOL-EPISODE count for the quality-aware trade budget.

    Ross spent 3 trades on ONE name (CLRO) for +$8,917; CHILI's raw FIFO trade budget
    (``_count_real_entries_today``) charged each same-symbol churn as a separate 'trade',
    burning the 5/5 ceiling on B-names. The budget should measure DISTINCT-SYMBOL EPISODES,
    not raw entries: all same-day REAL entries into ONE symbol = ONE episode, and a symbol
    whose banked realized PnL today is > 0 costs 0 for a re-entry (a green round banked =>
    re-entry free — you EARNED the right to press the winner).

    Returns ``(episode_count, green_banked_symbols, meta)`` where:
      * ``episode_count`` = distinct symbols with >= 1 REAL entered outcome today, MINUS the
        green-banked symbols (green symbols cost 0),
      * ``green_banked_symbols`` = UPPER symbols whose today net realized PnL is > 0,
      * ``meta`` = instrumentation (raw distinct symbols, per-symbol banked pnl).

    Read-only, indexed (execution_family, terminal_at). Uses ``is_real_entry_outcome`` so
    never-entered churn (cancel/no-fill) is not counted. FAIL-OPEN: any error returns
    ``(0, set(), ...)`` so the gate never blocks on thin/bad data (byte-identical to today)."""
    if db is None or not execution_family:
        return 0, set(), {"reason": "no_input"}
    try:
        from ....models.trading import MomentumAutomationOutcome
        from .outcome_labels import is_real_entry_outcome

        start_utc, end_utc = _et_day_bounds_utc(days_ago=0)
        frontier_utc = _risk_now_naive()
        rows = (
            db.query(
                MomentumAutomationOutcome.symbol,
                MomentumAutomationOutcome.outcome_class,
                MomentumAutomationOutcome.realized_pnl_usd,
            )
            .filter(
                MomentumAutomationOutcome.execution_family == execution_family,
                MomentumAutomationOutcome.mode == "live",
                MomentumAutomationOutcome.terminal_at >= start_utc,
                MomentumAutomationOutcome.terminal_at < end_utc,
                MomentumAutomationOutcome.terminal_at <= frontier_utc,
            )
            .all()
        )
        # Sum today's REAL realized PnL per symbol (a green-banked symbol re-entry is free).
        banked: dict[str, float] = {}
        entered_syms: set[str] = set()
        for sym, oc, pnl in rows:
            if not is_real_entry_outcome(oc):
                continue
            s = str(sym or "").strip().upper()
            if not s:
                continue
            entered_syms.add(s)
            if pnl is not None:
                try:
                    banked[s] = banked.get(s, 0.0) + float(pnl)
                except (TypeError, ValueError):
                    continue
        green_banked = {s for s, p in banked.items() if p > 0.0}
        # Episodes charged = distinct entered symbols that are NOT green-banked. A symbol
        # that banked green today costs 0 (re-entry free); a red / flat symbol costs 1.
        episode_count = len(entered_syms - green_banked)
        return (
            episode_count,
            green_banked,
            {
                "distinct_symbols": len(entered_syms),
                "distinct_symbol_list": sorted(entered_syms),
                "green_banked_symbols": sorted(green_banked),
                "charged_episodes": episode_count,
            },
        )
    except Exception:
        logger.debug("[momentum_neural] symbol-episode count read failed", exc_info=True)
        return 0, set(), {"reason": "error_fail_open"}


def _wildcard_dominant_symbol(db: Any) -> str | None:
    """A3: the WILDCARD-regime dominant symbol (UPPER), or None when not in a wildcard regime /
    flag OFF / unreadable breadth (fail-closed to neutral). Reused by the A1/A2 top-rank
    predicate — in a wildcard regime the lone mover is the top-rank beneficiary. Never raises."""
    try:
        from .breadth_regime import compute_breadth_regime

        reg = compute_breadth_regime(db)
        if reg.is_wildcard and reg.dominant_symbol:
            return str(reg.dominant_symbol).upper()
    except Exception:
        logger.debug("[momentum_neural] wildcard dominant-symbol read failed", exc_info=True)
    return None


def _top_ranked_live_eligible_symbol(
    db: Any, *, crypto: bool = False
) -> tuple[str | None, float | None, float | None, dict[str, Any]]:
    """A1(b) the current #1 freshness-valid live_eligible symbol + its score + the within-day
    p90 of the live_eligible score distribution.

    The load-bearing half of A1: episode-counting alone still yields 5/5 on a churny day, so
    the #1-ranked mover (CLRO on 07-02) must be able to clear a full ceiling. Reads
    ``momentum_symbol_viability`` (scope=symbol, live_eligible, freshness-valid within the
    live risk gate), dedupes to the best score per distinct symbol, and returns:
      * ``top_symbol`` (UPPER) = the max-score fresh live-eligible symbol,
      * ``top_score``,
      * ``p90`` = the 90th percentile of the distinct-symbol score distribution (adaptive
        within-day percentile — NO new magic number),
      * ``meta``.

    FAIL-CLOSED for the exemption: any error / empty board => ``(None, None, None, ...)`` so
    the caller grants NO exemption (the ceiling stands). ``crypto`` filters to / out ``-USD``
    so the equity lane ranks against equities only."""
    meta: dict[str, Any] = {}
    if db is None:
        return None, None, None, {"reason": "no_db"}
    try:
        from ....models.trading import MomentumSymbolViability

        max_age = float(
            getattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0
        )
        frontier_utc = _risk_now_naive()
        cutoff = frontier_utc - timedelta(seconds=max_age)
        q = db.query(
            MomentumSymbolViability.symbol, MomentumSymbolViability.viability_score
        ).filter(
            MomentumSymbolViability.scope == "symbol",
            MomentumSymbolViability.live_eligible.is_(True),
            MomentumSymbolViability.freshness_ts >= cutoff,
            MomentumSymbolViability.freshness_ts <= frontier_utc,
        )
        if crypto:
            q = q.filter(MomentumSymbolViability.symbol.like("%-USD%"))
        else:
            q = q.filter(~MomentumSymbolViability.symbol.like("%-USD%"))
        rows = q.all()
        if not rows:
            return None, None, None, {"reason": "empty_board"}
        # Best score per distinct symbol (a symbol has many variant rows).
        best: dict[str, float] = {}
        for sym, score in rows:
            s = str(sym or "").strip().upper()
            if not s:
                continue
            try:
                sc = float(score or 0.0)
            except (TypeError, ValueError):
                continue
            if s not in best or sc > best[s]:
                best[s] = sc
        if not best:
            return None, None, None, {"reason": "no_scored_symbols"}
        top_symbol = max(best, key=lambda k: best[k])
        top_score = best[top_symbol]
        # Within-day p90 of the distinct-symbol live_eligible score distribution.
        scores = sorted(best.values())
        p90 = _percentile(scores, 0.90)
        meta = {
            "n_eligible": len(best),
            "top_symbol": top_symbol,
            "top_score": round(top_score, 4),
            "p90_score": None if p90 is None else round(p90, 4),
        }
        return top_symbol, top_score, p90, meta
    except Exception:
        logger.debug("[momentum_neural] top-rank read failed", exc_info=True)
        return None, None, None, {"reason": "error_fail_closed"}


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolated percentile of an ASCENDING-sorted list. ``q`` in [0,1]. None on
    empty input. A single value returns itself. Pure; no numpy dependency."""
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    pos = max(0.0, min(1.0, float(q))) * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = pos - lo
    return float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def daily_trade_count_budget_decision(
    db: Any,
    *,
    execution_family: str | None,
    open_entry_count: int = 0,
    symbol: str | None = None,
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
    window -> let the best regime run). DENY a NEW entry once today's charged count
    (terminated today + currently-open/in-flight) reaches the ceiling.

    A1 (Ross CLRO-lesson 2026-07-02): the count is now QUALITY-AWARE, not blind FIFO.
      * (a) EPISODE COUNTING — all same-day REAL entries into ONE symbol = ONE episode
        (``_count_symbol_episodes_today``); a symbol that BANKED GREEN today costs 0 for a
        re-entry (green round banked => press the winner free). This alone still yields 5/5
        on a churny day, so:
      * (b) TOP-RANK EXEMPTION — if ``symbol`` IS the current #1 freshness-valid live-eligible
        name AND its score >= today's within-day p90 of the live-eligible score distribution
        (adaptive percentile, no new magic number), ALLOW with reason ``top_rank_exempt`` — the
        #1 mover gets its OWN episode sub-budget = the SAME documented base. FAIL-CLOSED on the
        exemption: unreadable rank / not #1 / below p90 => no exemption, the ceiling stands.

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

            realized_today = float(
                global_realized_pnl_today_et(
                    db, as_of_utc=_risk_now_aware()
                ).get("total_usd")
                or 0.0
            )
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
        # A1(a) EPISODE COUNTING: distinct-symbol episodes (green-banked symbols free), NOT
        # raw FIFO entries — same-symbol churn no longer burns the ceiling. The green-banked
        # set also means THIS candidate's own symbol re-entry is free if it banked green today.
        episodes, green_banked, ep_meta = _count_symbol_episodes_today(
            db, execution_family=execution_family
        )
        try:
            open_ct = max(0, int(open_entry_count))
        except (TypeError, ValueError):
            open_ct = 0
        _cand_sym = str(symbol or "").strip().upper()
        # A green-banked re-entry into THIS candidate's own symbol costs 0 (the episode is
        # already banked green; pressing the winner is free). Open/in-flight for a green-banked
        # symbol is likewise already inside its free episode.
        _cand_is_green_banked = bool(_cand_sym) and _cand_sym in green_banked
        used = episodes + open_ct
        allowed = used < ceiling
        meta = {
            "allowed": allowed,
            "ceiling": ceiling,
            "base": base,
            "episodes_today": episodes,
            "open_inflight": open_ct,
            "used": used,
            "heat_mult": round(heat_mult, 3),
            "cushion_units": round(cushion_u, 3),
            "expectancy_mult": round(exp_mult, 3),
            "win_rate": round(win_rate, 3) if win_rate is not None else None,
            "n_expectancy": n_exp,
            "candidate_symbol": _cand_sym or None,
            "candidate_green_banked": _cand_is_green_banked,
            **ep_meta,
        }
        # A green-banked re-entry into the candidate's own symbol is always allowed (free).
        if _cand_is_green_banked:
            meta["allowed"] = True
            meta["reason"] = "green_banked_reentry_free"
            return True, meta
        if allowed:
            return True, meta
        # ── A1(b) TOP-RANK EXEMPTION (the load-bearing half) ──────────────────────────
        # The ceiling is reached, but the #1 freshness-valid live-eligible mover with a
        # top-percentile score gets its OWN episode sub-budget = the SAME documented base
        # (so the CLRO-class name is never denied while B-names churned the ceiling).
        # FAIL-CLOSED: any read failure / not-#1 / below-p90 => the plain block stands.
        meta["reason"] = "daily_trade_count_budget_reached"
        if not bool(
            getattr(settings, "chili_momentum_trade_budget_top_rank_exempt_enabled", True)
        ):
            return False, meta
        if not _cand_sym:
            meta["exempt"] = False
            meta["exempt_reason"] = "no_candidate_symbol"
            return False, meta
        _crypto = _cand_sym.endswith("-USD")
        top_sym, top_score, p90, rank_meta = _top_ranked_live_eligible_symbol(
            db, crypto=_crypto
        )
        meta["rank"] = rank_meta
        # Fail-closed: rank unreadable / no p90 / not the #1 name / below the within-day p90.
        if top_sym is None or top_score is None or p90 is None:
            meta["exempt"] = False
            meta["exempt_reason"] = "rank_unreadable"
            return False, meta
        if _cand_sym != top_sym:
            # A3 REUSE: the wildcard breadth regime's DOMINANT symbol IS the top-rank
            # beneficiary even when a razor-thin score gap makes another row the raw #1 —
            # in a wildcard regime the lone mover is the name to concentrate on. Fail-closed:
            # no wildcard / not the dominant name => the plain not-top-ranked block stands.
            _wc_dom = _wildcard_dominant_symbol(db)
            if _wc_dom and _cand_sym == _wc_dom:
                meta["exempt"] = True
                meta["exempt_reason"] = "wildcard_dominant_exempt"
                meta["exempt_sub_budget"] = base
                meta["reason"] = "top_rank_exempt"
                meta["allowed"] = True
                return True, meta
            meta["exempt"] = False
            meta["exempt_reason"] = "not_top_ranked"
            return False, meta
        if float(top_score) < float(p90):
            meta["exempt"] = False
            meta["exempt_reason"] = "below_within_day_p90"
            return False, meta
        # The #1 mover gets its OWN episode sub-budget = the SAME base. Its own charged
        # episodes = the count of PRIOR entered-but-not-green episodes into THIS symbol,
        # which is at most 1 (one distinct symbol) — always < base (base >= 1). So the #1
        # name always clears its own base sub-budget: allow. Instrumented for the live
        # binding read (report_binding_not_defaults).
        _own_prior_episode = 1 if _cand_sym in {
            str(s).strip().upper() for s in ep_meta.get("distinct_symbol_list", [])
        } else 0
        meta["exempt"] = True
        meta["exempt_reason"] = "top_rank_exempt"
        meta["exempt_sub_budget"] = base
        meta["exempt_sub_used"] = min(_own_prior_episode, base)
        meta["reason"] = "top_rank_exempt"
        meta["allowed"] = True
        return True, meta
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
        now_et = _risk_now_aware().astimezone(et)
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


def day_open_risk_ramp_multiplier(
    db: Any, *, execution_family: str | None
) -> tuple[float, dict[str, Any]]:
    """FIX-17 — DAY-OPEN RISK RAMP (ENTRIES ONLY): the first N real entries of the ET day
    share an ADAPTIVE fraction of the day's risk envelope, so the first shots can't pre-spend
    what the red-day reducer would only later claw back (IPW -$137: the first trades consumed
    2.4x the later allowance).

      entries_today = real ENTERED live trades that terminated today (this lane)
      released      = entries_today >= N  OR  today's realized start is already GREEN
                      (then the cushion ladder owns the climb — this is a no-op)
      mult          = frac + (1 - frac) * (entries_today / N)      # linear ramp to 1.0
                      clamp to [frac, 1.0] (size-DOWN only; never sizes up)

    Both the starting ``frac`` and the span ``N`` TILT off the recent daily-PnL volatility
    (the SAME trailing daily PnL/equity sample the prior-day damper uses): a HIGH-variance
    lane opens more conservatively (lower frac, longer N); a calm lane barely throttles.
    ``chili_momentum_day_open_ramp_fraction_base`` and ``..._entries_base`` are the ONE
    documented base each (FLOORS the ramp climbs from, not scattered caps).

    Sizing-only — this is the ENTRY-fill path (held states never consult it), so it can NEVER
    delay/shrink an exit. Composes multiplicatively under the runner's base*3.0 clamp; the
    daily-loss cap + drawdown breaker still bound the downside. FAIL-OPEN: flag OFF / no
    history / any error => ``(1.0, ...)`` (full size, byte-identical). Read-only, lookahead-
    free. docs/DESIGN/MOMENTUM_LANE.md [[project_profitability_levers]]"""
    if not bool(getattr(settings, "chili_momentum_day_open_risk_ramp_enabled", True)):
        return 1.0, {"reason": "disabled"}
    if db is None or not execution_family:
        return 1.0, {"reason": "no_input"}
    try:
        frac_base = float(getattr(settings, "chili_momentum_day_open_ramp_fraction_base", 0.5) or 0.5)
        n_base = int(getattr(settings, "chili_momentum_day_open_ramp_entries_base", 3) or 3)
        if not (0.1 <= frac_base <= 1.0):
            frac_base = 0.5
        if n_base < 1:
            n_base = 3

        # RELEASE EARLY on a green realized start — the cushion ladder then owns the climb.
        try:
            from ..governance import global_realized_pnl_today_et

            day = global_realized_pnl_today_et(
                db, as_of_utc=_risk_now_aware()
            )
            realized_today = float(day.get("total_usd") or 0.0)
        except Exception:
            realized_today = 0.0
        if realized_today > 0.0:
            return 1.0, {"reason": "green_start_released", "day_realized_usd": round(realized_today, 2)}

        # ADAPTIVE tilt off recent daily-PnL volatility (trailing daily PnL/equity stdev).
        # High variance => open MORE conservatively (lower frac, longer N). Neutral tilt (1.0)
        # when history is too thin to measure — the documented base then binds (fail-open to base).
        lookback = int(getattr(settings, "chili_momentum_prior_day_damper_lookback_days", 20) or 20)
        _prior, sample = _prior_session_pnl_over_equity(
            db, execution_family=execution_family, lookback_days=lookback
        )
        vol = None
        if sample and len(sample) >= 5:
            try:
                _s = statistics.pstdev(sample)
                vol = _s if (math.isfinite(_s) and _s > 0) else None
            except statistics.StatisticsError:
                vol = None
        # Tilt: scale by vol relative to a documented per-trade risk reference (loss fraction
        # of equity). vol_ratio > 1 => throttle harder; < 1 => barely throttle. Bounded so a
        # single wild day can't zero out the ramp.
        loss_frac_ref = float(getattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01) or 0.01)
        frac = frac_base
        n = n_base
        vol_ratio = None
        if vol is not None and loss_frac_ref > 0:
            vol_ratio = max(0.5, min(2.0, vol / loss_frac_ref))
            # More vol -> lower starting fraction (down to half the base) and a longer ramp.
            frac = max(0.1, min(1.0, frac_base / vol_ratio))
            n = max(1, min(20, int(round(n_base * vol_ratio))))

        entries_today = _count_real_entries_today(db, execution_family=execution_family)
        if entries_today >= n:
            return 1.0, {
                "reason": "ramp_complete",
                "entries_today": entries_today,
                "n": n,
                "frac": round(frac, 4),
            }
        mult = frac + (1.0 - frac) * (float(entries_today) / float(n))
        mult = max(frac, min(1.0, mult))
        return mult, {
            "ramp_mult": round(mult, 4),
            "entries_today": entries_today,
            "n": n,
            "frac_base": round(frac_base, 4),
            "frac": round(frac, 4),
            "vol": round(vol, 6) if vol is not None else None,
            "vol_ratio": round(vol_ratio, 4) if vol_ratio is not None else None,
            "day_realized_usd": round(realized_today, 2),
            "n_sample": len(sample) if sample else 0,
        }
    except Exception:
        return 1.0, {"ramp_mult": 1.0, "reason": "error_fail_open"}


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


def _kelly_fraction_from_conviction(conviction: float) -> float:
    """HALF-KELLY bet fraction from a [0,1] conviction percentile (no hardcoded win-rate).

    Kelly: f* = edge/odds = p - (1-p)/b. Even-money proxy (b=1) => f* = 2p-1. We use the
    BLENDED triple-confluence percentile as the win-probability proxy p. Full Kelly is
    over-aggressive (operator warned: sizing up before proven expectancy is risky) => HALF
    Kelly: f = 0.5*max(0, 2p-1). p<=0.5 => 0 (no size-up); p=1 => 0.5. Bounded [0,0.5]."""
    try:
        p = float(conviction)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(p):
        return 0.0
    p = max(0.0, min(1.0, p))
    return 0.5 * max(0.0, 2.0 * p - 1.0)


def triple_confluence_kelly_multiplier(
    *,
    squeeze_pct: float | None,
    ofi: float | None,
    news_grade_rank: int | None,
) -> tuple[float, dict[str, Any]]:
    """FRACTIONAL-KELLY TRIPLE-CONFLUENCE size-up multiplier (NOT a hard live-block).

    Sizes UP — and ONLY up — when ALL THREE pillars AGREE: SQUEEZE squeeze_pct>0.5, OFI
    ofi>0, NEWS news_grade_rank>0. Any leg missing/sub-neutral => 1.0 (fail-open). All agree:
      c=clamp((w_sq*sq_lift+w_ofi*ofi_lift+w_news*news_lift)/sum_w,0,1); f_half=0.5*max(0,2c-1)
      mult=clamp(1.0+gain*f_half,1.0,max_multiplier)
    sq_lift=(squeeze_pct-0.5)/0.5; ofi_lift=ofi; news_lift=1.0. max_multiplier (default 1.5,
    clamped [1,2]) is the ONE documented HALF-KELLY ceiling; the #769 circuit still bounds the
    realized worst case. Composes under the runner's min(.., base*3.0) clamp + hard notional
    ceiling + the unchanged #769 circuit. NEVER a veto/shrink (>=1.0). Flag OFF / leg missing /
    error => (1.0, ...). Pure; settings only. [momentum_neural] kelly-conviction."""
    if not bool(getattr(settings, "chili_momentum_kelly_conviction_enabled", True)):
        return 1.0, {"reason": "disabled", "kelly_mult": 1.0}
    try:
        sq = None if squeeze_pct is None else float(squeeze_pct)
        of = None if ofi is None else float(ofi)
        nr = 0 if news_grade_rank is None else int(news_grade_rank)
        sq_ok = sq is not None and math.isfinite(sq) and sq > 0.5
        ofi_ok = of is not None and math.isfinite(of) and of > 0.0
        news_ok = nr > 0
        if not (sq_ok and ofi_ok and news_ok):
            return 1.0, {"kelly_mult": 1.0, "reason": "confluence_incomplete",
                         "squeeze_ok": bool(sq_ok), "ofi_ok": bool(ofi_ok), "news_ok": bool(news_ok)}
        sq_lift = max(0.0, min(1.0, (sq - 0.5) / 0.5))
        ofi_lift = max(0.0, min(1.0, of))
        news_lift = 1.0
        def _w(name: str, default: float) -> float:
            raw = getattr(settings, name, default)
            try:
                v = float(raw if raw is not None else default)
            except (TypeError, ValueError):
                return default
            return v if (math.isfinite(v) and v >= 0) else default
        w_sq = _w("chili_momentum_kelly_conviction_w_squeeze", 0.4)
        w_ofi = _w("chili_momentum_kelly_conviction_w_ofi", 0.4)
        w_news = _w("chili_momentum_kelly_conviction_w_news", 0.2)
        wsum = w_sq + w_ofi + w_news
        if wsum <= 0:
            return 1.0, {"kelly_mult": 1.0, "reason": "weights_disabled"}
        conviction = max(0.0, min(1.0, (w_sq*sq_lift + w_ofi*ofi_lift + w_news*news_lift) / wsum))
        f_half = _kelly_fraction_from_conviction(conviction)
        _gain_raw = getattr(settings, "chili_momentum_kelly_conviction_gain", 1.0)
        kelly_gain = float(_gain_raw if _gain_raw is not None else 1.0)
        if not math.isfinite(kelly_gain) or kelly_gain < 0:
            kelly_gain = 1.0
        _max_raw = getattr(settings, "chili_momentum_kelly_conviction_max_multiplier", 1.5)
        max_mult = float(_max_raw if _max_raw is not None else 1.5)
        if not math.isfinite(max_mult) or max_mult < 1.0:
            max_mult = 1.0
        mult = max(1.0, min(max_mult, 1.0 + kelly_gain * f_half))
        return mult, {"kelly_mult": round(mult, 4), "conviction": round(conviction, 4),
                      "kelly_fraction_half": round(f_half, 4), "squeeze_pct": round(sq, 4),
                      "ofi": round(of, 4), "news_grade_rank": nr,
                      "weights": {"squeeze": w_sq, "ofi": w_ofi, "news": w_news},
                      "max_multiplier": max_mult, "gain": kelly_gain}
    except Exception:
        return 1.0, {"reason": "error_fail_neutral", "kelly_mult": 1.0}


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
        # STEP-E #15: use the SAME EM-scaled tolerance the admission gate used, so a wider spread
        # accepted via the EM-scaled cap is priced as a proportional SIZE-DOWN (a DSY-class name
        # at its 721bps EM ceiling shrinks toward the floor, not admitted at full size).
        _em_scale_k: float | None = None
        if bool(getattr(settings, "chili_momentum_risk_spread_abs_cap_em_scale_enabled", True)):
            try:
                _em_scale_k = float(getattr(settings, "chili_momentum_risk_spread_abs_cap_em_scale_k", 1.0) or 1.0)
            except (TypeError, ValueError):
                _em_scale_k = 1.0
        tol = adaptive_max_spread_bps(
            base, expected_move_bps, ratio, abs_cap_bps=abs_cap_bps, abs_cap_em_scale_k=_em_scale_k
        )
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


def adaptive_reentry_cooldown_seconds(
    *,
    base_seconds: int,
    last_exit_reason: str | None,
    last_exit_return_bps: float | None,
    entry_stop_atr_pct: float | None,
    profit_factor: float = 0.25,
    vol_ref_atr_pct: float = 0.03,
    vol_span: float = 1.5,
) -> tuple[int, dict[str, Any]]:
    """Adaptive after-exit cooldown (pure, zero-I/O). Replaces the FIXED magic
    `chili_momentum_risk_cooldown_after_stopout_seconds` with a regime/vol- and
    exit-reason-scaled value. ONE documented base (`base_seconds`, the env floor);
    everything else is derived, no scattered magic.

    Two adaptive factors, multiplied onto the base:
      * reason_mult: a clean PROFIT/target exit (return_bps > 0 OR reason in the
        profit set) -> `profit_factor` (a SHORT cooldown so a winner can be
        re-scalped immediately — the TNMG re-enter-after-the-pop case). A stop-out
        / loss -> 1.0 (the full base cooldown — sit out the chop).
      * vol_mult: scale by the name's realized vol relative to a reference. A
        higher-ATR name needs a LONGER pause to let the next structure form;
        clamp to [1/vol_span, vol_span] so it never explodes or vanishes.
          vol_mult = clamp((atr_pct / vol_ref_atr_pct), 1/vol_span, vol_span)

    Returns (seconds:int >= 0, debug). Fail-NEUTRAL: any unusable input falls back
    to `base_seconds` (never a shorter-than-base loss-side cooldown). The result is
    floored at 0 and never raises. docs/DESIGN/MOMENTUM_LANE.md"""
    dbg: dict[str, Any] = {}
    try:
        base = max(0, int(base_seconds or 0))
    except (TypeError, ValueError):
        base = 0
    # reason_mult — profit/target => short re-arm; stop/loss => full base.
    #
    # WAVE-1 FIX-6 (N3): the realized-return SIGN is AUTHORITATIVE. The prior code
    # classified via a SUBSTRING reason match ("trail" in "trail_stop") that ALWAYS ran
    # — so a LOSING trail-stop exit (rb<0) was tagged is_profit=True and got the 0.25x
    # short cooldown (~112s) instead of the full base loss cooldown, letting the lane
    # re-arm the same loser seconds later (IPW re-armed 3s after a wrongly-shortened
    # cooldown -> -$78.62). Now: when rb is known, its sign decides (rb<0 => NOT profit,
    # full base; rb>0 => profit). Reason-token matching (EXACT token equality on the
    # underscore-split reason, never substring) is a FALLBACK used ONLY when rb is None.
    _profit_reasons = {"target", "first_target", "scale_out", "runner_target", "profit"}
    is_profit = False
    try:
        rb = float(last_exit_return_bps) if last_exit_return_bps is not None else None
    except (TypeError, ValueError):
        rb = None
    _reason = (str(last_exit_reason or "").strip().lower())
    if rb is not None:
        # SIGN authoritative: strictly-positive realized return => profit; else full base.
        is_profit = rb > 0
    else:
        # No realized return available: fall back to EXACT reason-token equality (split on
        # "_" so "trail_stop" yields {"trail","stop"} and never matches a profit token; a
        # bare "target"/"scale_out"/"profit"/"first_target"/"runner_target" still counts).
        _tokens = set(_reason.split("_"))
        is_profit = bool(_profit_reasons & _tokens) or _reason in _profit_reasons
    reason_mult = float(profit_factor) if is_profit else 1.0
    # vol_mult — clamp(atr_pct / vol_ref, 1/span, span); higher vol => longer.
    vol_mult = 1.0
    try:
        ap = float(entry_stop_atr_pct) if entry_stop_atr_pct is not None else None
        ref = float(vol_ref_atr_pct)
        span = float(vol_span)
        if ap is not None and math.isfinite(ap) and ap > 0 and ref > 0 and span >= 1.0:
            raw = ap / ref
            vol_mult = max(1.0 / span, min(span, raw))
    except (TypeError, ValueError):
        vol_mult = 1.0
    secs = int(round(base * reason_mult * vol_mult))
    secs = max(0, secs)
    dbg.update(
        base_seconds=base, reason_mult=reason_mult, vol_mult=round(vol_mult, 4),
        is_profit=is_profit, last_exit_reason=_reason, secs=secs,
    )
    return secs, dbg


def reentry_after_stop_allowed(
    *,
    enabled: bool,
    stopout_cycles: int,
    max_stopout_reentries: int,
) -> tuple[bool, str]:
    """Pure bound on RE-ENTRIES after a STOP-OUT for a single session/name (no I/O).

    `stopout_cycles` is the count of prior LOSS recycles for this name today (not
    profit recycles — those are free to re-scalp). When it reaches
    `max_stopout_reentries` the session must TERMINALIZE (FINISHED) instead of
    recycling to WATCHING, so a chopper cannot bleed via unlimited re-arms. Flag OFF
    => always allowed (byte-identical legacy unlimited recycle).
    Returns (allowed, reason)."""
    if not enabled:
        return True, "flag_off"
    try:
        c = int(stopout_cycles or 0)
        m = int(max_stopout_reentries or 0)
    except (TypeError, ValueError):
        return True, "bad_basis_fail_open"
    if m <= 0:
        return True, "uncapped"
    if c >= m:
        return False, "max_stopout_reentries_reached"
    return True, "allowed"


def reentry_escalation_decision(
    *,
    enabled: bool,
    escalation_level: int,
    structural_trigger: bool,
    live_price: float | None,
    prior_hwm: float | None,
    prior_exit_price: float | None,
    prior_risk_dist: float | None,
    tape_accel: float | None,
    is_day_leader: bool | None = None,
) -> tuple[bool, dict[str, Any]]:
    """G4 P2 — SAME-SYMBOL re-entry escalation after a stop-out (PURE, no I/O).

    The bleed-stopping half of the losers-eat-the-winner fix (CLRO 07-02: two earlier
    full-risk stops ate the +$285 leg to +$13). After each stop-out on a name within
    the day, the NEXT entry on that same name must be HIGHER-QUALITY — a confirmation
    RAISE, never a lockout (the decision is a WAIT that clears the moment the market
    proves the level; the day-leader stays re-enterable by design).

    ``escalation_level`` = consecutive-loss pressure for this name today (incremented
    per loss recycle, decayed on a profit recycle, RESET on a green banked round —
    green_banked_reentry_free parity). Level <= 0 ⇒ no escalation (byte-identical).

    At level >= 1, ALL of (adaptive; no hard counts, margins in the trade's OWN units):
      * STRUCTURAL trigger class — the fired trigger must carry real structure
        (pullback_low; the same class set the structural-stop machinery trusts). The
        weak fallbacks (momentum_continuation / score_only) no longer qualify.
        DAY-LEADER SUBSTITUTE (review m2): the #1 name (``is_day_leader``) must never
        be permanently WAIT-blocked just because its entries fire via non-structural
        (volume-confirmation) reasons. When the leader's trigger is non-structural it
        may substitute a STRICT high-quality equivalent for the structural class:
        readable POSITIVE tape AND an ACTUAL price reclaim above the prior failure
        (both actively satisfied — NO skip-on-missing, so this is a HIGHER bar, not a
        hole). Non-leaders keep the strict structural requirement;
      * STRUCTURE RECLAIM — live price must exceed the level where the LAST attempt
        FAILED: the prior trade's high-water mark (fallback: its exit price when no
        HWM was recorded), plus ``(level - 1) * prior_risk_dist`` — each successive
        failure demands one more full R of proof. Missing BOTH references ⇒ the
        reclaim check is skipped (structural-trigger + tape still required — partial
        raise rather than a starving block on absent bookkeeping);
      * TAPE HOLD — when the executed tape is readable, ``tape_accel`` must be > 0
        (buyers lifting). An unreadable tape (None) skips this check (the reclaim
        requirement still stands) so a thin-tape name is not starved.

    Returns ``(allowed, debug)``. Fail-OPEN on unusable numeric basis (current
    behavior — the standard trigger already fired). docs/DESIGN/MOMENTUM_LANE.md"""
    dbg: dict[str, Any] = {
        "escalation_level": escalation_level,
        "structural_trigger": bool(structural_trigger),
        "is_day_leader": bool(is_day_leader) if is_day_leader is not None else None,
        "prior_hwm": prior_hwm,
        "prior_exit_price": prior_exit_price,
        "prior_risk_dist": prior_risk_dist,
        "tape_accel": tape_accel,
        "required_reclaim": None,
    }
    if not enabled:
        dbg["reason"] = "flag_off"
        return True, dbg
    try:
        lvl = int(escalation_level or 0)
    except (TypeError, ValueError):
        dbg["reason"] = "bad_level_fail_open"
        return True, dbg
    if lvl <= 0:
        dbg["reason"] = "no_escalation"
        return True, dbg

    # Shared reclaim math (prior-failure reference + per-level margin) — used by both
    # the leader substitute below and the reclaim gate (step 2). Returns
    # (ref, required_price) with ref None when no usable prior reference exists.
    def _reclaim_required() -> tuple[float | None, float | None]:
        _ref = None
        for _cand in (prior_hwm, prior_exit_price):
            try:
                if _cand is not None and math.isfinite(float(_cand)) and float(_cand) > 0:
                    _ref = float(_cand)
                    break
            except (TypeError, ValueError):
                continue
        if _ref is None:
            return None, None
        _m = 0.0
        try:
            if (
                prior_risk_dist is not None
                and math.isfinite(float(prior_risk_dist))
                and float(prior_risk_dist) > 0
            ):
                _m = max(0, lvl - 1) * float(prior_risk_dist)
        except (TypeError, ValueError):
            _m = 0.0
        return _ref, _ref + _m

    def _price_ge(target: float | None) -> bool:
        if target is None:
            return False
        try:
            return (
                live_price is not None
                and math.isfinite(float(live_price))
                and float(live_price) > 0
                and float(live_price) >= float(target)
            )
        except (TypeError, ValueError):
            return False

    def _tape_positive() -> bool:
        try:
            return (
                tape_accel is not None
                and math.isfinite(float(tape_accel))
                and float(tape_accel) > 0.0
            )
        except (TypeError, ValueError):
            return False

    # 1) structural trigger class required at any escalation level.
    if not structural_trigger:
        # Day-leader substitute (review m2): the leader may replace the structural
        # class with a STRICT equivalent — readable POSITIVE tape AND an actual
        # reclaim above the prior failure, BOTH actively satisfied (no skip). A
        # non-leader, or a leader without that confirmation, still blocks.
        _sub_ok = False
        if is_day_leader:
            _, _sub_req = _reclaim_required()
            _sub_ok = bool(_tape_positive() and _sub_req is not None and _price_ge(_sub_req))
            dbg["leader_structural_substitute"] = _sub_ok
        if not _sub_ok:
            dbg["reason"] = "non_structural_trigger"
            return False, dbg
    # 2) structure reclaim: price must prove the prior failure wrong.
    ref, required = _reclaim_required()
    if ref is not None:
        dbg["required_reclaim"] = round(required, 6)
        px = None
        try:
            if live_price is not None and math.isfinite(float(live_price)) and float(live_price) > 0:
                px = float(live_price)
        except (TypeError, ValueError):
            px = None
        if px is None:
            # No readable live price: the downstream quote gates own that failure
            # mode; do not double-block here (fail toward current behavior).
            dbg["reason"] = "no_live_price_fail_open"
        elif px < required:
            # DAY-LEADER IGNITION BYPASS (2026-07-09, JEM 06-30 replay forensic):
            # demanding the PRIOR attempt's HWM back is the wrong proof when the
            # market re-based LOWER and is now IGNITING off the new base — on JEM
            # the structural trigger fired at 3.57-3.61 with tape_accel +75k..+110k
            # (the +37%-in-1-min squeeze's first seconds) while this gate demanded
            # 3.82; by the time the vertical crossed 3.82 the anti-chase cap owned
            # the block — between the two, the re-entry window was ~1-2s wide and
            # the day's biggest winner was forfeited (Ross re-enters on the NEW
            # structure's break, not the old failure's price). For the DAY-LEADER
            # with a STRUCTURAL trigger and actively POSITIVE tape, the structural
            # break itself is the reclaim proof. Non-leaders, non-structural
            # triggers, and weak tape keep the full price-reclaim bar (the
            # CLRO-07-02 loss-chase class this gate exists for), and the anti-chase
            # cap + adaptive cooldown + per-name loss caps all still apply.
            if is_day_leader and structural_trigger and _tape_positive():
                dbg["reason"] = "leader_ignition_bypass"
            else:
                dbg["reason"] = "reclaim_not_met"
                return False, dbg
        else:
            dbg["reason"] = "reclaim_met"
            # NOTE: the anti-chase-the-top CAP (do not re-buy far above where the last
            # attempt failed) lives in live_runner's standalone re-entry chase gate, not
            # here. It anchors to the prior losing tranche's high-water-mark and measures
            # the ceiling in the name's ATR (robust to a pathologically wide prior stop),
            # and fires at ANY escalation level — a strict superset of what a `required`-
            # anchored, prior_risk_dist-scaled cap could do from inside this helper.
    else:
        dbg["reason"] = "no_reclaim_reference"
    # 3) tape hold when readable.
    try:
        if tape_accel is not None and math.isfinite(float(tape_accel)) and float(tape_accel) <= 0.0:
            dbg["reason"] = "tape_not_confirming"
            return False, dbg
    except (TypeError, ValueError):
        pass
    return True, dbg


def _is_stop_class_exit_reason(reason: str | None) -> bool:
    """G4 P2 (review M1) — TRUE iff the exit reason is a genuine STOP-class exit.

    Token membership on the ``_``-split reason (the SAME convention the outcome
    classifier and the adaptive-cooldown token fallback use), so decorated reasons
    (``stop_broker_zero_reconcile``, ``trail_stop_retry_cap_broker_zero_reconcile``)
    still classify while ``kill_switch_flatten`` / ``bailout`` / ``max_hold`` /
    ``target`` / ``scale_out_limit`` do NOT. Unknown/None ⇒ False (fail toward the
    pre-G4 behavior: no escalation on an unconfirmed class)."""
    try:
        tokens = set(str(reason or "").lower().split("_"))
    except Exception:
        return False
    return "stop" in tokens


def reentry_escalation_level_update(
    *,
    current_level: int,
    was_loss: bool,
    exit_reason: str | None,
    green_banked: bool,
) -> tuple[int, str]:
    """G4 P2 (review M1) — the escalation-level bookkeeping rule (PURE, no I/O).

    The level measures CONSECUTIVE STOP pressure on the name today, so only a genuine
    STOP-class loss raises it (``_is_stop_class_exit_reason``): a kill-switch flatten,
    a bailout, a max-hold timeout, or a target/scale exit that happens to close red is
    NOT evidence the entry level failed — those exits do not increment even when
    pnl <= 0 (level unchanged). A profit recycle DECAYS the level by one; a GREEN
    BANKED round (the symbol's banked realized PnL > 0 — the caller supplies the
    basis) RESETS it to zero (green_banked_reentry_free parity).

    Returns ``(new_level, reason)``. Unusable ``current_level`` ⇒ treated as 0."""
    try:
        lvl = max(0, int(current_level or 0))
    except (TypeError, ValueError):
        lvl = 0
    if was_loss:
        if _is_stop_class_exit_reason(exit_reason):
            return lvl + 1, "stop_class_loss_increment"
        return lvl, "non_stop_loss_unchanged"
    if green_banked:
        return 0, "green_banked_reset"
    return max(0, lvl - 1), "profit_recycle_decay"


def symbol_day_banked_pnl_other_sessions(
    db: Any,
    *,
    symbol: str,
    exclude_session_id: int | None = None,
    execution_family: str | None = None,
) -> float | None:
    """G4 P2 (review m1) — the SYMBOL's today-ET net realized PnL across its OTHER
    (already-terminal) live sessions.

    The green-banked reset must key on the symbol's DAY-WIDE net (the
    ``_count_symbol_episodes_today`` precedent: green-banked = today NET realized PnL
    across ALL sessions), not one session's local ledger — a symbol that banked green
    in an earlier session and then recycles red in a fresh session is still a
    green-banked name. Outcome rows are one-per-terminal-session (the current live
    session has none yet), so the caller composes: day_net = THIS session's cumulative
    ``le["realized_pnl_usd"]`` + this other-sessions sum. CHEAP by construction — the
    caller runs it once per loss-recycle transition (not per tick) over the indexed
    (symbol, mode) outcome table with today's terminal_at bounds.

    Returns the sum (0.0 when no qualifying rows) or ``None`` on any read error —
    the caller then falls back to the session-local basis (current behavior)."""
    s = str(symbol or "").strip().upper()
    if db is None or not s:
        return None
    try:
        from ....models.trading import MomentumAutomationOutcome
        from .outcome_labels import is_real_entry_outcome

        start_utc, end_utc = _et_day_bounds_utc(days_ago=0)
        frontier_utc = _risk_now_naive()
        q = (
            db.query(
                MomentumAutomationOutcome.session_id,
                MomentumAutomationOutcome.outcome_class,
                MomentumAutomationOutcome.realized_pnl_usd,
            )
            .filter(
                MomentumAutomationOutcome.symbol == s,
                MomentumAutomationOutcome.mode == "live",
                MomentumAutomationOutcome.terminal_at >= start_utc,
                MomentumAutomationOutcome.terminal_at < end_utc,
                MomentumAutomationOutcome.terminal_at <= frontier_utc,
            )
        )
        if execution_family:
            q = q.filter(MomentumAutomationOutcome.execution_family == execution_family)
        total = 0.0
        for sid, oc, pnl in q.all():
            if exclude_session_id is not None and sid == exclude_session_id:
                continue
            if not is_real_entry_outcome(oc):
                continue
            if pnl is None:
                continue
            try:
                total += float(pnl)
            except (TypeError, ValueError):
                continue
        return total
    except Exception:
        logger.debug("[momentum_neural] symbol day-banked pnl read failed", exc_info=True)
        return None


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
    # Live-eligibility TOCTOU recency grace (2026-06-29 UPC +500% miss). On a fast/thin
    # premarket vertical the neural re-scoring can FLICKER live_eligible False at the exact
    # entry instant even though the name armed+confirmed live-eligible seconds earlier. When
    # the session was live-eligible at ARM/CONFIRM within this grace window AND there is live
    # forward momentum, the eligibility block is DOWNGRADED to a warn so a transient flicker
    # cannot terminally veto a just-confirmed active mover. The grace ONLY relaxes on positive
    # evidence (recent eligibility + forward momentum); a name never live-eligible, or whose
    # ineligibility is older than the window, still BLOCKS. ONE documented base (seconds);
    # flag OFF => byte-identical (no downgrade). Composes with — never widens — the separate
    # freshness check, and never touches the drawdown / kill-switch / max-loss hard blocks.
    live_eligible_recency_grace_enabled: bool = True
    live_eligible_recency_grace_seconds: float = 90.0
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
            live_eligible_recency_grace_enabled=bool(
                getattr(s, "chili_momentum_live_eligible_recency_grace_enabled", True)
            ),
            live_eligible_recency_grace_seconds=float(
                getattr(s, "chili_momentum_live_eligible_recency_grace_seconds", 90.0)
            ),
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
    d["resolved_at_utc"] = _risk_now_aware().isoformat()
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


_TOD_CACHE: dict[str, Any] = {}


def time_of_day_risk_multiplier(db: Any, *, now_et_hour_frac: float | None = None) -> tuple[float, dict[str, Any]]:
    """ADAPTIVE time-of-day risk multiplier (2026-07-10, greenlit #1): scale per-trade
    risk by the LANE'S OWN measured per-hour expectancy, shrunk toward the
    Ross-verified discovery-window prior. Both the prior AND our 30-day data agree (2026-07-10 pull:
    09:00 ET +25.1R, 10:00 +6.8R, 11:00+ ALL negative — the midday churn that ate the
    JZXN/CANF mornings), so this is loss-subtraction on a measured curve, not a bet.

    mult(hour) = w * data_mult + (1 - w) * prior_mult
      * data_mult  — the trailing-30d avg realized-R for the ET hour bucket mapped to
        [floor, 1]: avg_r >= 0 -> 1.0; avg_r <= -1R -> floor; linear between (the
        name's own outcomes decide — no hand-tuned schedule).
      * prior_mult — the Ross discipline as the SMALL-SAMPLE anchor: 1.0 through the
        discovery window (premarket-10:30 ET), 0.5 to 11:30, else the floor.
      * w = n/(n + K) — shrinkage by the bucket's own sample count; K is THE one
        documented base (samples for a bucket to earn half its own weight).
      * floor — reuses chili_momentum_frontside_strength_floor's convention (0.25):
        floors are floors; a proven-toxic hour still gets a probe-sized budget so the
        curve can keep LEARNING (a 0x hour could never update itself).

    MARKET-STRUCTURE derate (applied AFTER the paper full-size floor, like the spread
    derate): a validated discipline, not capital-preservation psychology — it binds on
    paper too. Crypto (24/7) is exempt at the CALLER (no equity session clock).
    10-minute process cache (one aggregate query). Fail-open 1.0 on any error."""
    try:
        if not bool(getattr(settings, "chili_momentum_time_of_day_risk_enabled", True)):
            return 1.0, {"reason": "flag_off"}
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo

        if now_et_hour_frac is None:
            _et = _risk_now_aware().astimezone(ZoneInfo("America/New_York"))
            now_et_hour_frac = _et.hour + _et.minute / 60.0
        floor = 0.25
        # PRIOR: the documented Ross discipline (discovery window full-risk).
        if now_et_hour_frac < 10.5:
            prior = 1.0
        elif now_et_hour_frac < 11.5:
            prior = 0.5
        else:
            prior = floor
        # DATA: trailing-30d per-hour realized-R buckets (cached 10 min).
        import time as _time

        _now_mono = _time.monotonic()
        _as_of_utc = _risk_now_naive()
        _is_replay = _REPLAY_RISK_NOW.get() is not None
        # A wall-TTL cache is valid for a live lane, but not for replay: a later A/B
        # run could otherwise poison an earlier run with future-derived buckets.
        cached = None if _is_replay else _TOD_CACHE.get("buckets")
        if cached is None or (_now_mono - _TOD_CACHE.get("at", 0.0)) > 600.0:
            from sqlalchemy import text as _sql

            rows = db.execute(_sql(
                "SELECT extract(hour FROM (ts AT TIME ZONE 'UTC') AT TIME ZONE 'America/New_York')::int AS h, "
                "       count(*) AS n, avg((payload_json->>'realized_r')::numeric) AS avg_r "
                "FROM trading_automation_events "
                "WHERE event_type='momentum_mfe_realized' "
                "AND ts > :as_of_utc - interval '30 days' AND ts <= :as_of_utc "
                "GROUP BY 1"
            ), {"as_of_utc": _as_of_utc}).fetchall()
            cached = {int(r[0]): (int(r[1]), float(r[2])) for r in rows}
            if not _is_replay:
                _TOD_CACHE["buckets"] = cached
                _TOD_CACHE["at"] = _now_mono
        hour = int(now_et_hour_frac)
        n, avg_r = cached.get(hour, (0, 0.0))
        # avg_r >= 0 -> 1.0; avg_r <= -1 -> floor; linear in between.
        if avg_r >= 0:
            data_mult = 1.0
        else:
            data_mult = max(floor, 1.0 + (1.0 - floor) * float(avg_r))
        K = float(getattr(settings, "chili_momentum_time_of_day_shrinkage_samples", 20) or 20)
        w = float(n) / (float(n) + max(1.0, K))
        mult = w * data_mult + (1.0 - w) * prior
        mult = max(floor, min(1.0, mult))
        return mult, {
            "et_hour_frac": round(float(now_et_hour_frac), 2), "bucket_n": n,
            "bucket_avg_r": round(float(avg_r), 3), "data_mult": round(data_mult, 3),
            "prior_mult": prior, "w": round(w, 3), "mult": round(mult, 3),
        }
    except Exception:
        return 1.0, {"reason": "error_fail_open"}


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
            .filter(MomentumAutomationOutcome.terminal_at <= _risk_now_naive())
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
        # ALPACA PAPER (2026-07-07): the rolling-median guard clamps a cap to 2x the median of recent
        # SAME-VENUE caps — it exists to catch a transient BAD equity READ inflating size. The Alpaca
        # paper equity (~$100k / ~$400k BP) is AUTHORITATIVE (get_account_snapshot), but the recent-cap
        # history is contaminated by the pre-fix wrong-basis era (Coinbase ~$1.9k), so the median would
        # under-clamp a legitimate $400k account down to ~$1k. Skip the guard for the paper alpaca lane
        # so it sizes against its REAL buying power (fake money; the equity-relative cap + BP + per-trade
        # max-loss still bound it). Default ON; flag restores the guard. (ALPACA_PAPER_ENABLE_PLAN.md)
        _skip_median_guard = str(execution_family or "").lower() in ("alpaca_spot", "alpaca_short") and bool(
            getattr(settings, "chili_momentum_alpaca_skip_cap_median_guard", True)
        )
        history = (
            {}
            if _skip_median_guard
            else _recent_frozen_per_trade_caps(db, execution_family=execution_family, lookback=lookback)
        )
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
