"""Live / paper drift-to-null monitoring for repeatable-edge ScanPatterns."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from math import comb
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .pattern_validation_projection import write_validation_contract

logger = logging.getLogger(__name__)

DRIFT_VERSION = 1
DRIFT_V2_VERSION = 2

REPEATABLE_EDGE_ORIGINS = frozenset({"web_discovered", "brain_discovered"})

APPROXIMATION_NOTE = (
    "CHILI v3 live drift v1: single-stream primary (live if n≥threshold else paper); "
    "p_like is an exact binomial tail under a fixed baseline when not suppressed — not "
    "sequential-testing corrected; paper ≠ live execution."
)

V2_APPROXIMATION_NOTE = (
    "CHILI v5 live drift v2: separate live and paper runtime scorecards with composite "
    "expectancy/distribution checks. Research baseline still uses OOS aggregates, not a fully "
    "matched live microstructure benchmark."
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _outcome_pct_from_trade(trade: Any) -> float | None:
    direct = _safe_float(getattr(trade, "pnl_pct", None))
    if direct is not None:
        return direct
    entry = _safe_float(getattr(trade, "entry_price", None))
    qty = _safe_float(getattr(trade, "quantity", None))
    pnl = _safe_float(getattr(trade, "pnl", None))
    if entry is None or entry <= 0 or qty is None or qty <= 0 or pnl is None:
        return None
    return (pnl / (entry * qty)) * 100.0


def binomial_two_sided_p_value(n: int, k: int, p0: float) -> float | None:
    """Two-sided binomial p-value; None if inputs invalid."""
    if n <= 0 or p0 <= 0.0 or p0 >= 1.0:
        return None
    k = max(0, min(n, k))
    if n > 512:
        return None

    def pmf(i: int) -> float:
        return comb(n, i) * (p0**i) * ((1.0 - p0) ** (n - i))

    p_le = sum(pmf(i) for i in range(0, k + 1))
    p_ge = sum(pmf(i) for i in range(k, n + 1))
    return min(1.0, 2.0 * min(p_le, p_ge))


def _carry_confidence_reference(prev: dict[str, Any] | None) -> float | None:
    if not prev or not isinstance(prev, dict):
        return None
    v = prev.get("confidence_reference")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_skip_contract(
    *,
    skip_reason: str,
    prev_live_drift: dict[str, Any] | None = None,
    evaluation_window_days: int | None = None,
) -> dict[str, Any]:
    ref = _carry_confidence_reference(prev_live_drift)
    return {
        "drift_version": DRIFT_VERSION,
        "observed_live_score": None,
        "baseline_research_score": None,
        "drift_delta": None,
        "drift_p_like": None,
        "sample_count": 0,
        "sample_count_live": 0,
        "sample_count_paper": 0,
        "window_count": 0,
        "drift_tier": "n/a",
        "drift_flags": [],
        "primary_runtime_source": None,
        "runtime_mixed_context": False,
        "evaluation_window": {"days": int(evaluation_window_days or 0)} if evaluation_window_days else {},
        "last_evaluated_at": _utc_iso(),
        "approximation_note": APPROXIMATION_NOTE,
        "skip_reason": skip_reason,
        "confidence_reference": ref,
        "p_like_suppressed": False,
        "degenerate_baseline": False,
    }


def build_skip_contract_v2(
    *,
    skip_reason: str,
    evaluation_window_days: int | None = None,
) -> dict[str, Any]:
    return {
        "drift_version": DRIFT_V2_VERSION,
        "runtime_scorecards": {"live": None, "paper": None},
        "research_baseline": None,
        "primary_runtime_source": None,
        "fallback_used": False,
        "sample_count": 0,
        "sample_count_live": 0,
        "sample_count_paper": 0,
        "comparisons": {},
        "composite_flags": [],
        "composite_tier": "n/a",
        "critical_for_auto_challenge": False,
        "shadow_mode": True,
        "evaluation_window": {"days": int(evaluation_window_days or 0)},
        "last_evaluated_at": _utc_iso(),
        "approximation_note": V2_APPROXIMATION_NOTE,
        "skip_reason": skip_reason,
    }


def _baseline_research_win_rate_pct(pattern: Any, oos_val: dict[str, Any]) -> tuple[float | None, list[str]]:
    from .backtest_metrics import backtest_win_rate_db_to_display_pct

    flags: list[str] = []
    if pattern is not None and getattr(pattern, "oos_win_rate", None) is not None:
        p = backtest_win_rate_db_to_display_pct(pattern.oos_win_rate)
        if p is not None:
            return float(p), flags
    ee = oos_val.get("edge_evidence") if isinstance(oos_val.get("edge_evidence"), dict) else {}
    oos_m = ee.get("oos_mean_wr_pct")
    if oos_m is not None:
        try:
            return float(oos_m), flags
        except (TypeError, ValueError):
            pass
    flags.append("no_baseline_wr")
    return None, flags


def _baseline_research_expectancy_pct(pattern: Any) -> float | None:
    for attr in ("oos_avg_return_pct", "avg_return_pct"):
        v = _safe_float(getattr(pattern, attr, None))
        if v is not None:
            return v
    return None


def baseline_degenerate(
    baseline_pct: float,
    *,
    low: float,
    high: float,
) -> tuple[bool, str | None]:
    if baseline_pct <= low * 100.0:
        return True, "baseline_near_zero"
    if baseline_pct >= high * 100.0:
        return True, "baseline_near_one"
    return False, None


def aggregate_runtime_samples(
    db: Session,
    *,
    scan_pattern_id: int,
    user_id: int,
    window_days: int,
) -> dict[str, Any]:
    from ...models.trading import PaperTrade, Trade

    since = datetime.utcnow() - timedelta(days=max(1, int(window_days)))
    live_rows = (
        db.query(Trade)
        .filter(
            Trade.user_id == int(user_id),
            Trade.status == "closed",
            Trade.scan_pattern_id == int(scan_pattern_id),
            Trade.exit_date.isnot(None),
            Trade.exit_date >= since,
        )
        .all()
    )
    live_wins = sum(1 for t in live_rows if float(t.pnl or 0) > 0)

    paper_rows = (
        db.query(PaperTrade)
        .filter(
            PaperTrade.scan_pattern_id == int(scan_pattern_id),
            PaperTrade.status == "closed",
            PaperTrade.exit_date.isnot(None),
            PaperTrade.exit_date >= since,
        )
        .filter(or_(PaperTrade.user_id == int(user_id), PaperTrade.user_id.is_(None)))
        .all()
    )
    paper_wins = sum(1 for t in paper_rows if float(t.pnl or 0) > 0)

    return {
        "n_live": len(live_rows),
        "wins_live": int(live_wins),
        "n_paper": len(paper_rows),
        "wins_paper": int(paper_wins),
    }


def _scorecard(rows: list[Any], *, source: str) -> dict[str, Any] | None:
    if not rows:
        return None
    outcomes = [v for row in rows if (v := _outcome_pct_from_trade(row)) is not None]
    winners = [v for v in outcomes if v > 0]
    losers = [v for v in outcomes if v < 0]
    gross_wins = sum(winners)
    gross_losses = abs(sum(losers))
    expectancy = round(sum(outcomes) / len(outcomes), 4) if outcomes else None
    avg_winner = round(sum(winners) / len(winners), 4) if winners else None
    avg_loser = round(sum(losers) / len(losers), 4) if losers else None
    profit_factor = round(gross_wins / gross_losses, 4) if gross_losses > 0 else (999.0 if gross_wins > 0 else None)
    ordered = sorted(outcomes)
    p25 = ordered[max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.25))))] if ordered else None
    slips: list[float] = []
    freshness = None
    for row in rows:
        for attr in ("tca_entry_slippage_bps", "tca_exit_slippage_bps"):
            v = _safe_float(getattr(row, attr, None))
            if v is not None:
                slips.append(abs(v))
        last_at = getattr(row, "exit_date", None) or getattr(row, "updated_at", None) or getattr(row, "created_at", None)
        if isinstance(last_at, datetime):
            freshness = max(freshness, last_at) if freshness else last_at
    win_rate_pct = round((len(winners) / max(1, len(outcomes))) * 100.0, 4)
    return {
        "source": source,
        "sample_count": len(outcomes),
        "win_rate_pct": win_rate_pct,
        "expectancy_per_trade_pct": expectancy,
        "avg_winner_pct": avg_winner,
        "avg_loser_pct": avg_loser,
        "profit_factor": profit_factor,
        "p25_trade_outcome_pct": round(float(p25), 4) if p25 is not None else None,
        "slippage_burden_bps": round(sum(slips) / len(slips), 2) if slips else None,
        "freshness_at": freshness.isoformat() if freshness else None,
    }


def aggregate_runtime_scorecards(
    db: Session,
    *,
    scan_pattern_id: int,
    user_id: int,
    window_days: int,
) -> dict[str, Any]:
    from ...models.trading import PaperTrade, Trade

    since = datetime.utcnow() - timedelta(days=max(1, int(window_days)))
    live_rows = (
        db.query(Trade)
        .filter(
            Trade.user_id == int(user_id),
            Trade.status == "closed",
            Trade.scan_pattern_id == int(scan_pattern_id),
            Trade.exit_date.isnot(None),
            Trade.exit_date >= since,
        )
        .all()
    )
    paper_rows = (
        db.query(PaperTrade)
        .filter(
            PaperTrade.scan_pattern_id == int(scan_pattern_id),
            PaperTrade.status == "closed",
            PaperTrade.exit_date.isnot(None),
            PaperTrade.exit_date >= since,
        )
        .filter(or_(PaperTrade.user_id == int(user_id), PaperTrade.user_id.is_(None)))
        .all()
    )
    return {
        "live": _scorecard(live_rows, source="live"),
        "paper": _scorecard(paper_rows, source="paper"),
        "n_live": len(live_rows),
        "n_paper": len(paper_rows),
    }


def select_primary_runtime(
    *,
    n_live: int,
    n_paper: int,
    wins_live: int,
    wins_paper: int,
    live_min_primary: int,
    min_trades: int,
) -> tuple[str | None, int, int, bool, str | None]:
    if n_live >= live_min_primary:
        return "live", n_live, wins_live, False, None
    if n_paper >= min_trades:
        mixed = 0 < n_live < live_min_primary
        return "paper", n_paper, wins_paper, mixed, None
    if n_live > 0 or n_paper > 0:
        return None, 0, 0, False, "insufficient_runtime_sample"
    return None, 0, 0, False, "no_runtime_trades_in_window"


def compute_live_drift_contract(
    *,
    pattern: Any,
    oos_val: dict[str, Any],
    runtime: dict[str, Any],
    prev_live_drift: dict[str, Any] | None,
    settings: Any,
) -> dict[str, Any]:
    window_days = int(getattr(settings, "brain_live_drift_window_days", 120) or 120)
    live_min = int(getattr(settings, "brain_live_drift_live_min_primary", 8) or 8)
    min_trades = int(getattr(settings, "brain_live_drift_min_trades", 8) or 8)
    eps_lo = float(getattr(settings, "brain_live_drift_baseline_p0_low", 0.05) or 0.05)
    eps_hi = float(getattr(settings, "brain_live_drift_baseline_p0_high", 0.95) or 0.95)
    warn_pp = float(getattr(settings, "brain_live_drift_warning_delta_pp", 8.0) or 8.0)
    crit_pp = float(getattr(settings, "brain_live_drift_critical_delta_pp", 18.0) or 18.0)
    strong_p = float(getattr(settings, "brain_live_drift_strong_p_like", 0.02) or 0.02)

    n_live = int(runtime.get("n_live") or 0)
    n_paper = int(runtime.get("n_paper") or 0)
    wins_live = int(runtime.get("wins_live") or 0)
    wins_paper = int(runtime.get("wins_paper") or 0)

    primary, n_use, wins, mixed_sparse, sk = select_primary_runtime(
        n_live=n_live,
        n_paper=n_paper,
        wins_live=wins_live,
        wins_paper=wins_paper,
        live_min_primary=live_min,
        min_trades=min_trades,
    )
    if sk is not None or primary is None:
        return build_skip_contract(
            skip_reason=sk or "insufficient_runtime_sample",
            prev_live_drift=prev_live_drift,
            evaluation_window_days=window_days,
        )

    observed_wr_pct = round(100.0 * wins / n_use, 4) if n_use else 0.0

    baseline_pct, bflags = _baseline_research_win_rate_pct(pattern, oos_val)
    drift_flags: list[str] = list(bflags)
    if baseline_pct is None:
        return build_skip_contract(
            skip_reason="no_research_baseline",
            prev_live_drift=prev_live_drift,
            evaluation_window_days=window_days,
        )

    degenerate, deg_reason = baseline_degenerate(baseline_pct, low=eps_lo, high=eps_hi)
    if degenerate:
        drift_flags.append(deg_reason or "degenerate_baseline")

    suppress_p_like = mixed_sparse or degenerate
    if mixed_sparse:
        drift_flags.append("sparse_live_while_paper_primary_p_like_suppressed")

    drift_delta = round(observed_wr_pct - baseline_pct, 4)

    p_like: float | None = None
    p0 = baseline_pct / 100.0
    if not suppress_p_like and n_use >= min_trades and not degenerate:
        p_like = binomial_two_sided_p_value(n_use, wins, p0)

    tier = "healthy"
    if drift_delta <= -crit_pp or (p_like is not None and p_like <= strong_p and drift_delta < -warn_pp):
        tier = "critical"
    elif drift_delta <= -warn_pp or (p_like is not None and p_like <= 0.05):
        tier = "warning"

    bucket = max(1, min(8, n_use // max(1, min_trades)))
    window_count = min(bucket, 4) if n_use >= min_trades else 1

    try:
        pat_conf = float(getattr(pattern, "confidence", 0.5) or 0.5)
    except (TypeError, ValueError):
        pat_conf = 0.5
    ref = _carry_confidence_reference(prev_live_drift)
    if ref is None:
        ref = pat_conf

    return {
        "drift_version": DRIFT_VERSION,
        "observed_live_score": observed_wr_pct,
        "baseline_research_score": round(baseline_pct, 4),
        "drift_delta": drift_delta,
        "drift_p_like": round(p_like, 6) if p_like is not None else None,
        "sample_count": int(n_use),
        "sample_count_live": int(n_live),
        "sample_count_paper": int(n_paper),
        "window_count": int(window_count),
        "drift_tier": tier,
        "drift_flags": drift_flags,
        "primary_runtime_source": primary,
        "runtime_mixed_context": bool(mixed_sparse),
        "evaluation_window": {"days": window_days},
        "last_evaluated_at": _utc_iso(),
        "approximation_note": APPROXIMATION_NOTE,
        "skip_reason": None,
        "confidence_reference": ref,
        "p_like_suppressed": bool(suppress_p_like),
        "degenerate_baseline": bool(degenerate),
    }


def compute_live_drift_v2_contract(
    *,
    pattern: Any,
    oos_val: dict[str, Any],
    scorecards: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    window_days = int(getattr(settings, "brain_live_drift_window_days", 120) or 120)
    live_min = int(getattr(settings, "brain_live_drift_live_min_primary", 8) or 8)
    min_trades = int(getattr(settings, "brain_live_drift_min_trades", 8) or 8)
    warn_expectancy_ratio = float(getattr(settings, "brain_live_drift_v2_warn_expectancy_ratio", 0.7) or 0.7)
    crit_expectancy_ratio = float(getattr(settings, "brain_live_drift_v2_critical_expectancy_ratio", 0.4) or 0.4)
    warn_pf = float(getattr(settings, "brain_live_drift_v2_warn_profit_factor", 1.0) or 1.0)
    crit_pf = float(getattr(settings, "brain_live_drift_v2_critical_profit_factor", 0.8) or 0.8)
    warn_slip = float(getattr(settings, "brain_live_drift_v2_warn_slippage_bps", 25.0) or 25.0)
    crit_slip = float(getattr(settings, "brain_live_drift_v2_critical_slippage_bps", 45.0) or 45.0)

    live = scorecards.get("live")
    paper = scorecards.get("paper")
    n_live = int(scorecards.get("n_live") or 0)
    n_paper = int(scorecards.get("n_paper") or 0)

    primary = None
    fallback_used = False
    if live and n_live >= live_min:
        primary = live
    elif paper and n_paper >= min_trades:
        primary = paper
        fallback_used = n_live > 0
    if primary is None:
        return build_skip_contract_v2(
            skip_reason="insufficient_runtime_sample" if (n_live or n_paper) else "no_runtime_trades_in_window",
            evaluation_window_days=window_days,
        )

    baseline_wr, _ = _baseline_research_win_rate_pct(pattern, oos_val)
    baseline_expectancy = _baseline_research_expectancy_pct(pattern)
    if baseline_wr is None and baseline_expectancy is None:
        return build_skip_contract_v2(skip_reason="no_research_baseline", evaluation_window_days=window_days)

    comparisons: dict[str, Any] = {}
    flags: list[str] = []
    tier = "healthy"

    runtime_wr = _safe_float(primary.get("win_rate_pct"))
    runtime_expectancy = _safe_float(primary.get("expectancy_per_trade_pct"))
    runtime_pf = _safe_float(primary.get("profit_factor"))
    runtime_p25 = _safe_float(primary.get("p25_trade_outcome_pct"))
    slippage_burden = _safe_float(primary.get("slippage_burden_bps"))

    if baseline_wr is not None and runtime_wr is not None:
        wr_delta = round(runtime_wr - baseline_wr, 4)
        comparisons["win_rate_delta_pp"] = wr_delta
        warn_pp = float(getattr(settings, "brain_live_drift_warning_delta_pp", 8.0) or 8.0)
        crit_pp = float(getattr(settings, "brain_live_drift_critical_delta_pp", 18.0) or 18.0)
        if wr_delta <= -crit_pp:
            flags.append("win_rate_critical")
        elif wr_delta <= -warn_pp:
            flags.append("win_rate_warning")

    if baseline_expectancy is not None and runtime_expectancy is not None:
        denom = abs(baseline_expectancy) if abs(baseline_expectancy) > 1e-6 else 1.0
        expectancy_ratio = round(runtime_expectancy / denom, 4)
        comparisons["expectancy_ratio"] = expectancy_ratio
        comparisons["expectancy_delta_pct"] = round(runtime_expectancy - baseline_expectancy, 4)
        if expectancy_ratio <= crit_expectancy_ratio:
            flags.append("expectancy_critical")
        elif expectancy_ratio <= warn_expectancy_ratio:
            flags.append("expectancy_warning")

    if runtime_pf is not None:
        comparisons["profit_factor"] = runtime_pf
        if runtime_pf <= crit_pf:
            flags.append("profit_factor_critical")
        elif runtime_pf <= warn_pf:
            flags.append("profit_factor_warning")

    if runtime_p25 is not None:
        comparisons["p25_trade_outcome_pct"] = runtime_p25
        if runtime_p25 <= -2.0:
            flags.append("tail_loss_critical")
        elif runtime_p25 <= -0.75:
            flags.append("tail_loss_warning")

    if slippage_burden is not None:
        comparisons["slippage_burden_bps"] = slippage_burden
        if slippage_burden >= crit_slip:
            flags.append("slippage_burden_critical")
        elif slippage_burden >= warn_slip:
            flags.append("slippage_burden_warning")

    if any(flag.endswith("critical") for flag in flags):
        tier = "critical"
    elif flags:
        tier = "warning"

    return {
        "drift_version": DRIFT_V2_VERSION,
        "runtime_scorecards": {"live": live, "paper": paper},
        "research_baseline": {
            "win_rate_pct": baseline_wr,
            "expectancy_per_trade_pct": baseline_expectancy,
        },
        "primary_runtime_source": primary.get("source"),
        "fallback_used": fallback_used,
        "sample_count": int(primary.get("sample_count") or 0),
        "sample_count_live": n_live,
        "sample_count_paper": n_paper,
        "comparisons": comparisons,
        "composite_flags": flags,
        "composite_tier": tier,
        "critical_for_auto_challenge": tier == "critical" and not fallback_used,
        "shadow_mode": bool(getattr(settings, "brain_live_drift_shadow_mode", True)),
        "evaluation_window": {"days": window_days},
        "last_evaluated_at": _utc_iso(),
        "approximation_note": V2_APPROXIMATION_NOTE,
        "skip_reason": None,
    }


def _tier_confidence_multiplier(tier: str, settings: Any) -> float:
    if tier == "critical":
        return float(getattr(settings, "brain_live_drift_confidence_mult_critical", 0.88) or 0.88)
    if tier == "warning":
        return float(getattr(settings, "brain_live_drift_confidence_mult_warning", 0.94) or 0.94)
    return float(getattr(settings, "brain_live_drift_confidence_mult_healthy", 1.0) or 1.0)


def apply_live_drift_to_pattern(
    db: Session,
    pattern: Any,
    contract: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    ov = dict(pattern.oos_validation_json or {}) if isinstance(pattern.oos_validation_json, dict) else {}
    flags = list(ov.get("research_hygiene_flags") or [])
    for f in list(flags):
        if isinstance(f, str) and f.startswith("live_drift_"):
            flags.remove(f)
    tier = str(contract.get("drift_tier") or "n/a")
    skip = bool(contract.get("skip_reason"))

    if not skip and tier == "warning":
        flags.append("live_drift_warning")
    elif not skip and tier == "critical":
        flags.append("live_drift_critical")
    ov["research_hygiene_flags"] = flags

    ov["live_drift"] = contract

    if not skip and tier in ("healthy", "warning", "critical") and bool(
        getattr(settings, "brain_live_drift_confidence_nudge_enabled", True)
    ):
        try:
            anchor_f = float(contract.get("confidence_reference"))
        except (TypeError, ValueError):
            anchor_f = float(pattern.confidence or 0.5)
        contract["confidence_reference"] = anchor_f
        mult = _tier_confidence_multiplier(tier, settings)
        lo = float(getattr(settings, "brain_live_drift_confidence_floor", 0.1) or 0.1)
        hi = float(getattr(settings, "brain_live_drift_confidence_cap", 0.95) or 0.95)
        new_c = max(lo, min(hi, anchor_f * mult))
        pattern.confidence = round(new_c, 4)

    auto = bool(getattr(settings, "brain_live_drift_auto_challenged_enabled", False))
    n_s = int(contract.get("sample_count") or 0)
    min_tr = int(getattr(settings, "brain_live_drift_min_trades", 8) or 8)
    p_like = contract.get("drift_p_like")
    p_ok = (
        p_like is not None
        and not contract.get("p_like_suppressed")
        and not contract.get("degenerate_baseline")
        and float(p_like) <= float(getattr(settings, "brain_live_drift_auto_challenged_max_p_like", 0.02) or 0.02)
    )
    if auto and not skip and tier == "critical" and n_s >= min_tr and p_ok:
        from .lifecycle import LifecycleError, transition

        cur = (pattern.lifecycle_stage or "").strip().lower()
        if cur in ("promoted", "live"):
            try:
                transition(db, pattern, "challenged", reason="live_drift_critical_strong_p_like", commit=False)
                db.flush()
            except LifecycleError as e:
                logger.debug("[live_drift] skip auto challenged: %s", e)

    pattern.oos_validation_json = ov
    return contract


def apply_live_drift_v2_to_pattern(
    db: Session,
    pattern: Any,
    contract: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    write_validation_contract(pattern, "live_drift_v2", contract)
    if (
        bool(getattr(settings, "brain_live_drift_auto_challenged_enabled", False))
        and not contract.get("skip_reason")
        and bool(contract.get("critical_for_auto_challenge"))
    ):
        from .lifecycle import LifecycleError, transition

        cur = (pattern.lifecycle_stage or "").strip().lower()
        if cur in ("promoted", "live"):
            try:
                transition(db, pattern, "challenged", reason="live_drift_v2_composite_critical", commit=False)
                db.flush()
            except LifecycleError as e:
                logger.debug("[live_drift_v2] skip auto challenged: %s", e)
    return contract


def run_live_drift_refresh(db: Session) -> dict[str, Any]:
    from ...config import settings
    from ...models.trading import ScanPattern

    v2_on = bool(getattr(settings, "brain_live_drift_v2_enabled", True))
    if not v2_on:
        return {"ok": True, "skipped": True, "reason": "disabled", "updated": 0}

    uid = getattr(settings, "brain_default_user_id", None)
    if uid is None:
        return {"ok": True, "skipped": True, "reason": "no_brain_default_user_id", "updated": 0}

    window_days = int(getattr(settings, "brain_live_drift_window_days", 120) or 120)

    rows = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.origin.in_(tuple(REPEATABLE_EDGE_ORIGINS)),
            ScanPattern.lifecycle_stage.in_(("promoted", "live")),
        )
        .all()
    )
    updated = 0
    for pattern in rows:
        try:
            ov0 = pattern.oos_validation_json or {}
            prev = ov0.get("live_drift") if isinstance(ov0, dict) and isinstance(ov0.get("live_drift"), dict) else None

            if v2_on:
                scorecards = aggregate_runtime_scorecards(
                    db,
                    scan_pattern_id=int(pattern.id),
                    user_id=int(uid),
                    window_days=window_days,
                )
                contract_v2 = compute_live_drift_v2_contract(
                    pattern=pattern,
                    oos_val=dict(ov0) if isinstance(ov0, dict) else {},
                    scorecards=scorecards,
                    settings=settings,
                )
                apply_live_drift_v2_to_pattern(db, pattern, contract_v2, settings)

            updated += 1
        except Exception as e:
            logger.warning("[live_drift] pattern id=%s failed: %s", getattr(pattern, "id", "?"), e)

    if updated:
        try:
            db.commit()
        except Exception as e:
            logger.warning("[live_drift] commit failed: %s", e)
            db.rollback()
            return {"ok": False, "error": str(e), "updated": 0}

    return {"ok": True, "updated": updated, "candidates": len(rows)}


def live_drift_summary(contract: dict[str, Any] | None) -> dict[str, Any] | None:
    if not contract or not isinstance(contract, dict):
        return None
    return {
        "drift_tier": contract.get("drift_tier"),
        "drift_delta": contract.get("drift_delta"),
        "sample_count": contract.get("sample_count"),
        "primary_runtime_source": contract.get("primary_runtime_source"),
        "skip_reason": contract.get("skip_reason"),
    }


def live_drift_v2_summary(contract: dict[str, Any] | None) -> dict[str, Any] | None:
    if not contract or not isinstance(contract, dict):
        return None
    return {
        "composite_tier": contract.get("composite_tier"),
        "primary_runtime_source": contract.get("primary_runtime_source"),
        "sample_count": contract.get("sample_count"),
        "sample_count_live": contract.get("sample_count_live"),
        "sample_count_paper": contract.get("sample_count_paper"),
        "comparisons": contract.get("comparisons"),
        "skip_reason": contract.get("skip_reason"),
        "shadow_mode": contract.get("shadow_mode"),
    }


# ── Feature / indicator distribution drift (PSI) ─────────────────────

def _compute_psi(
    expected: list[float],
    actual: list[float],
    n_bins: int = 10,
) -> float:
    """Compute Population Stability Index between two distributions.

    PSI < 0.10  → stable
    PSI 0.10-0.25 → moderate drift
    PSI > 0.25 → significant drift
    """
    import numpy as np

    e = np.array(expected, dtype=float)
    a = np.array(actual, dtype=float)
    if len(e) < 10 or len(a) < 10:
        return 0.0

    # Build bins from expected distribution
    breakpoints = np.percentile(e, np.linspace(0, 100, n_bins + 1))
    breakpoints[0] = -np.inf
    breakpoints[-1] = np.inf
    # Deduplicate (can happen if many identical values)
    breakpoints = np.unique(breakpoints)
    if len(breakpoints) < 3:
        return 0.0

    e_counts = np.histogram(e, bins=breakpoints)[0].astype(float)
    a_counts = np.histogram(a, bins=breakpoints)[0].astype(float)

    # Avoid division by zero with small floor
    e_pct = np.maximum(e_counts / e_counts.sum(), 1e-4)
    a_pct = np.maximum(a_counts / a_counts.sum(), 1e-4)

    psi = float(np.sum((a_pct - e_pct) * np.log(a_pct / e_pct)))
    return round(max(0.0, psi), 6)


def check_feature_drift(
    training_feature_stats: dict[str, dict[str, float]],
    recent_feature_rows: list[dict[str, float]],
    *,
    psi_threshold_moderate: float = 0.10,
    psi_threshold_significant: float = 0.25,
) -> dict[str, Any]:
    """Check whether ML input features have drifted from their training distribution.

    Parameters
    ----------
    training_feature_stats : dict
        Per-feature stats captured at training time (from
        ``PatternMetaLearner.get_training_feature_stats()``).  Each entry
        must contain ``mean`` and ``std``.
    recent_feature_rows : list[dict]
        Recent feature vectors (one dict per snapshot) using the same
        feature names as training.

    Returns
    -------
    dict with ``ok``, ``features_checked``, ``per_feature`` PSI values,
    ``drifted_features`` list, and ``composite_tier``.
    """
    import numpy as np

    if not training_feature_stats or not recent_feature_rows:
        return {"ok": True, "skip_reason": "insufficient_data", "features_checked": 0}

    per_feature: dict[str, dict[str, Any]] = {}
    drifted: list[str] = []

    for fname, stats in training_feature_stats.items():
        # Reconstruct an approximate training distribution from stats
        # (mean, std) via normal sampling — this is an approximation
        mean = stats.get("mean", 0.0)
        std = stats.get("std", 1.0)
        if std <= 0:
            std = 1e-6

        # Synthetic expected from stored quantiles (more accurate than normal approx)
        q_keys = ["q10", "q25", "q50", "q75", "q90"]
        quantiles = [stats.get(k) for k in q_keys]
        if all(q is not None for q in quantiles):
            # Build a simple representative sample from quantiles
            expected = np.concatenate([
                np.random.default_rng(42).normal(quantiles[0], std * 0.3, 20),
                np.random.default_rng(42).normal(quantiles[1], std * 0.2, 20),
                np.random.default_rng(42).normal(quantiles[2], std * 0.1, 20),
                np.random.default_rng(42).normal(quantiles[3], std * 0.2, 20),
                np.random.default_rng(42).normal(quantiles[4], std * 0.3, 20),
            ]).tolist()
        else:
            expected = np.random.default_rng(42).normal(mean, std, 100).tolist()

        actual = [row.get(fname, 0.0) for row in recent_feature_rows]
        psi = _compute_psi(expected, actual)

        tier = "stable"
        if psi >= psi_threshold_significant:
            tier = "significant"
            drifted.append(fname)
        elif psi >= psi_threshold_moderate:
            tier = "moderate"
            drifted.append(fname)

        per_feature[fname] = {"psi": psi, "tier": tier}

    composite = "stable"
    if any(v["tier"] == "significant" for v in per_feature.values()):
        composite = "significant"
    elif any(v["tier"] == "moderate" for v in per_feature.values()):
        composite = "moderate"

    result = {
        "ok": composite == "stable",
        "features_checked": len(per_feature),
        "drifted_features": drifted,
        "composite_tier": composite,
        "per_feature": per_feature,
        "evaluated_at": _utc_iso(),
    }

    if drifted:
        logger.warning(
            "[feature_drift] %d/%d features drifted (composite=%s): %s",
            len(drifted), len(per_feature), composite,
            ", ".join(drifted[:5]),
        )

    return result

