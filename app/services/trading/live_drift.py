"""Live / paper drift-to-null monitoring for repeatable-edge ScanPatterns (Phase 3 v1).

Compares recent realized win rate (single primary runtime source: live **or** paper) to
research baseline. Idempotent confidence nudge uses a stored ``confidence_reference`` anchor.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from math import comb
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DRIFT_VERSION = 1

REPEATABLE_EDGE_ORIGINS = frozenset({"web_discovered", "brain_discovered"})

APPROXIMATION_NOTE = (
    "CHILI v3 live drift v1: single-stream primary (live if n≥threshold else paper); "
    "p_like is an exact binomial tail under a fixed baseline when not suppressed — not "
    "sequential-testing corrected; paper ≠ live execution."
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def binomial_two_sided_p_value(n: int, k: int, p0: float) -> float | None:
    """Two-sided binomial p-value; None if inputs invalid."""
    if n <= 0 or p0 <= 0.0 or p0 >= 1.0:
        return None
    k = max(0, min(n, k))
    # P(X <= k) and P(X >= k) using exact PMF sums (n modest in trading contexts).
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
    """Fresh skip-shaped contract so stale tier/delta/p_like are not left looking current."""
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


def _baseline_research_win_rate_pct(pattern: Any, oos_val: dict[str, Any]) -> tuple[float | None, list[str]]:
    """Research WR % for drift comparison; None if unavailable."""
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


def baseline_degenerate(
    baseline_pct: float,
    *,
    low: float,
    high: float,
) -> tuple[bool, str | None]:
    """True if p_like must not be reported (pathological p0 near 0 or 1)."""
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
    """Closed live trades + closed paper trades in the window."""
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

    paper_q = db.query(PaperTrade).filter(
        PaperTrade.scan_pattern_id == int(scan_pattern_id),
        PaperTrade.status == "closed",
        PaperTrade.exit_date.isnot(None),
        PaperTrade.exit_date >= since,
    )
    paper_rows = paper_q.filter(
        or_(PaperTrade.user_id == int(user_id), PaperTrade.user_id.is_(None))
    ).all()
    paper_wins = sum(1 for t in paper_rows if float(t.pnl or 0) > 0)

    return {
        "n_live": len(live_rows),
        "wins_live": int(live_wins),
        "n_paper": len(paper_rows),
        "wins_paper": int(paper_wins),
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
    """Return (primary_source, n, wins, mixed_sparse_live, skip_reason)."""
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
    """Full evaluation; may return skip-shaped dict."""
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

    # Tier: delta-first; p_like sharpens only when present
    tier = "healthy"
    if drift_delta <= -crit_pp or (p_like is not None and p_like <= strong_p and drift_delta < -warn_pp):
        tier = "critical"
    elif drift_delta <= -warn_pp or (p_like is not None and p_like <= 0.05):
        tier = "warning"

    # Window count: coarse buckets by trade count (deterministic)
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
    """Merge contract, idempotent confidence from anchor, hygiene flags, optional challenged."""
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

    # Idempotent confidence: always derived from confidence_reference × tier multiplier
    if (
        not skip
        and tier in ("healthy", "warning", "critical")
        and bool(getattr(settings, "brain_live_drift_confidence_nudge_enabled", True))
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

    # Auto challenged: default-off; critical + enough sample + strong p_like only
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
    if (
        auto
        and not skip
        and tier == "critical"
        and n_s >= min_tr
        and p_ok
        and str(contract.get("primary_runtime_source") or "") != ""
    ):
        from .lifecycle import LifecycleError, transition

        cur = (pattern.lifecycle_stage or "").strip().lower()
        if cur in ("promoted", "live"):
            try:
                transition(
                    db,
                    pattern,
                    "challenged",
                    reason="live_drift_critical_strong_p_like",
                    commit=False,
                )
                db.flush()
            except LifecycleError as e:
                logger.debug("[live_drift] skip auto challenged: %s", e)

    pattern.oos_validation_json = ov
    return contract


def run_live_drift_refresh(db: Session) -> dict[str, Any]:
    """Batch refresh ``live_drift`` for repeatable-edge patterns in promoted/live."""
    from ...config import settings
    from ...models.trading import ScanPattern

    if not getattr(settings, "brain_live_drift_enabled", False):
        return {"ok": True, "skipped": True, "reason": "disabled", "updated": 0}

    uid = getattr(settings, "brain_default_user_id", None)
    if uid is None:
        return {"ok": True, "skipped": True, "reason": "no_brain_default_user_id", "updated": 0}

    window_days = int(getattr(settings, "brain_live_drift_window_days", 120) or 120)

    q = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.origin.in_(tuple(REPEATABLE_EDGE_ORIGINS)),
            ScanPattern.lifecycle_stage.in_(("promoted", "live")),
        )
    )
    rows = q.all()
    updated = 0
    for pattern in rows:
        try:
            prev = None
            ov0 = pattern.oos_validation_json or {}
            if isinstance(ov0, dict):
                prev = ov0.get("live_drift") if isinstance(ov0.get("live_drift"), dict) else None

            rt = aggregate_runtime_samples(
                db,
                scan_pattern_id=int(pattern.id),
                user_id=int(uid),
                window_days=window_days,
            )
            contract = compute_live_drift_contract(
                pattern=pattern,
                oos_val=dict(ov0) if isinstance(ov0, dict) else {},
                runtime=rt,
                prev_live_drift=prev,
                settings=settings,
            )

            apply_live_drift_to_pattern(db, pattern, contract, settings)
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
    """Compact server-side shape for API/templates."""
    if not contract or not isinstance(contract, dict):
        return None
    return {
        "drift_tier": contract.get("drift_tier"),
        "drift_delta": contract.get("drift_delta"),
        "sample_count": contract.get("sample_count"),
        "primary_runtime_source": contract.get("primary_runtime_source"),
        "skip_reason": contract.get("skip_reason"),
    }
