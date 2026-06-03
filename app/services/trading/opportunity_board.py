"""Trading Brain opportunity board: tiered manual-trading view (shared scoring with imminent)."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from sqlalchemy import desc

from ...config import settings
from ...models.trading import PrescreenCandidate, ScanResult
from .decision_ledger import attach_shadow_signal_packets
from .learning import get_current_predictions
from .market_data import is_crypto
from .opportunity_scoring import scan_pattern_eligible_main_imminent
from .pattern_imminent_alerts import (
    describe_us_session_context,
    format_eta_range,
    gather_imminent_candidate_rows,
    us_stock_session_open,
)
from .prescreen_job import load_active_global_candidate_tickers
from .speculative_momentum_engine import build_speculative_momentum_slice
from .trading_source_freshness import collect_source_freshness, compute_board_freshness_status

logger = logging.getLogger(__name__)

# Explicit engine ids (UI + future mesh hooks). Core tiers use the pattern-imminent plane.
OPPORTUNITY_ENGINE_CORE = "core_repeatable_edge"
OPPORTUNITY_ENGINE_PREDICTION = "prediction_context"
OPPORTUNITY_ENGINE_SCANNER = "scanner_context"
OPPORTUNITY_ENGINE_UNIVERSE = "universe_context"
OPPORTUNITY_ENGINE_PATTERN_RESEARCH = "pattern_research"


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if isinstance(value, bool) or value is None:
            return default
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _risk_level(score: float, *, good: bool = False) -> str:
    score = max(0.0, min(1.0, float(score)))
    if good:
        if score >= 0.72:
            return "strong"
        if score >= 0.48:
            return "partial"
        return "weak"
    if score >= 0.72:
        return "high"
    if score >= 0.38:
        return "medium"
    return "low"


def _price_level_geometry(candidate: dict[str, Any]) -> dict[str, float | None]:
    entry = _safe_float(candidate.get("entry") or candidate.get("price"))
    stop = _safe_float(candidate.get("stop"))
    target = _safe_float(candidate.get("target"))
    if not entry or entry <= 0:
        return {"entry": entry, "stop": stop, "target": target, "risk_bps": None, "reward_bps": None, "rr": None}
    risk_bps = abs(entry - stop) / entry * 10_000.0 if stop and stop > 0 else None
    reward_bps = abs(target - entry) / entry * 10_000.0 if target and target > 0 else None
    rr = (reward_bps / risk_bps) if risk_bps and reward_bps else None
    return {
        "entry": entry,
        "stop": stop,
        "target": target,
        "risk_bps": risk_bps,
        "reward_bps": reward_bps,
        "rr": rr,
    }


def _extension_risk(candidate: dict[str, Any]) -> dict[str, Any]:
    breakdown = candidate.get("score_breakdown") if isinstance(candidate.get("score_breakdown"), dict) else {}
    penalty = _safe_float(breakdown.get("overextension_penalty"), 0.0) or 0.0
    score = max(0.0, min(1.0, penalty / 0.12 if penalty else 0.0))
    return {
        "level": _risk_level(score),
        "score": round(score, 4),
        "overextension_penalty": round(penalty, 4),
        "reason": (
            "RSI/extension penalty is priced into the setup score."
            if penalty
            else "No overextension penalty detected."
        ),
    }


def _execution_risk(candidate: dict[str, Any], extension: dict[str, Any]) -> dict[str, Any]:
    geom = _price_level_geometry(candidate)
    rr = geom["rr"]
    missing_levels = geom["entry"] is None or geom["stop"] is None or geom["target"] is None
    score = 0.18
    reasons: list[str] = []
    if missing_levels:
        score += 0.32
        reasons.append("entry/stop/target incomplete")
    if rr is not None and rr < 1.25:
        score += 0.22
        reasons.append("thin reward-to-risk")
    risk_bps = geom["risk_bps"]
    if risk_bps is not None:
        if risk_bps < 35:
            score += 0.2
            reasons.append("tight stop vulnerable to spread/slippage")
        elif risk_bps > 900:
            score += 0.1
            reasons.append("wide stop increases capital at risk")
    score += 0.18 * float(extension.get("score") or 0.0)
    if candidate.get("asset_class") == "crypto":
        score += 0.04
    score = max(0.0, min(1.0, score))
    expected_slippage = 4.0 + score * (12.0 if candidate.get("asset_class") == "crypto" else 8.0)
    return {
        "level": _risk_level(score),
        "score": round(score, 4),
        "expected_slippage_bps": round(expected_slippage, 4),
        "expected_fill_probability": round(max(0.35, min(0.98, 0.94 - score * 0.45)), 4),
        "risk_bps": round(risk_bps, 4) if risk_bps is not None else None,
        "reward_bps": round(geom["reward_bps"], 4) if geom["reward_bps"] is not None else None,
        "reward_risk": round(rr, 4) if rr is not None else None,
        "reasons": reasons or ["levels and extension look executable"],
    }


def _structural_confirmation(candidate: dict[str, Any]) -> dict[str, Any]:
    coverage = _safe_float(candidate.get("feature_coverage"), 0.0) or 0.0
    readiness = _safe_float(candidate.get("readiness"), 0.0) or 0.0
    comp = _safe_float(candidate.get("composite"), 0.0) or 0.0
    source_strength = str(candidate.get("source_strength") or "").lower()
    score = 0.35 * coverage + 0.35 * readiness + 0.2 * comp
    if "pattern_imminent" in (candidate.get("sources") or []):
        score += 0.08
    if source_strength == "strong":
        score += 0.05
    if candidate.get("prediction_support"):
        score += 0.03
    score = max(0.0, min(1.0, score))
    return {
        "level": _risk_level(score, good=True),
        "score": round(score, 4),
        "feature_coverage": round(coverage, 4) if candidate.get("feature_coverage") is not None else None,
        "readiness": round(readiness, 4) if candidate.get("readiness") is not None else None,
        "confirmation_sources": candidate.get("sources") or [],
    }


def _liquidity_quality(candidate: dict[str, Any]) -> dict[str, Any]:
    sources = candidate.get("sources") or []
    strength = str(candidate.get("source_strength") or "").lower()
    score = 0.55
    if candidate.get("asset_class") == "crypto":
        score += 0.08
    if "prescreener" in sources:
        score += 0.1
    if "scanner" in sources:
        score += 0.04
    if strength == "strong":
        score += 0.08
    elif strength == "weak":
        score -= 0.12
    if candidate.get("price") is None and candidate.get("entry") is None:
        score -= 0.14
    score = max(0.0, min(1.0, score))
    return {
        "level": _risk_level(score, good=True),
        "score": round(score, 4),
        "proxy": "source/freshness/level completeness",
        "note": "Proxy until venue-order-book liquidity is attached to board rows.",
    }


def _net_edge_estimate(
    candidate: dict[str, Any],
    execution: dict[str, Any],
    extension: dict[str, Any],
) -> dict[str, Any]:
    geom = _price_level_geometry(candidate)
    entry = geom["entry"]
    stop = geom["stop"]
    target = geom["target"]
    if not entry or not stop or not target or entry <= 0:
        return {
            "available": False,
            "expected_net_edge": None,
            "reason": "entry/stop/target required",
            "capital_lane": "shadow_only",
        }
    if target <= entry or stop >= entry:
        return {
            "available": False,
            "expected_net_edge": None,
            "reason": "long-side payoff geometry invalid",
            "capital_lane": "shadow_only",
        }
    raw_prob = _safe_float(candidate.get("repeatability_confidence"))
    prob = 0.35 if raw_prob is None else raw_prob
    prob = max(0.05, min(0.95, prob))
    payoff = (target - entry) / entry
    loss = (entry - stop) / entry
    costs = (
        (_safe_float(execution.get("expected_slippage_bps"), 0.0) or 0.0) / 10_000.0
        + (0.003 if candidate.get("asset_class") == "crypto" else 0.0005)
        + 0.0005 * float(extension.get("score") or 0.0)
    )
    edge = prob * payoff - (1.0 - prob) * loss - costs
    return {
        "available": True,
        "expected_net_edge": round(edge, 6),
        "probability_proxy": round(prob, 4),
        "payoff_fraction": round(payoff, 6),
        "loss_fraction": round(loss, 6),
        "cost_fraction": round(costs, 6),
        "capital_lane": "shadow_only",
    }


def _board_data_quality_gate(
    *,
    data_as_of: str | None,
    age_sec: float | None,
    stale_threshold_seconds: int,
    freshness_unknown: bool,
    is_stale: bool,
    board_truncated: bool,
    data_as_of_min_keys: list[str],
    source_freshness: dict[str, Any],
    missing_source_keys: list[str] | None = None,
    invalid_source_keys: list[str] | None = None,
    source_status: dict[str, str] | None = None,
) -> dict[str, Any]:
    reasons: list[dict[str, Any]] = []
    hard_block_reason_code: str | None = None
    if freshness_unknown:
        hard_block_reason_code = "board_freshness_unknown"
        reasons.append(
            {
                "code": hard_block_reason_code,
                "severity": "block",
                "message": "Board data_as_of could not be established from source timestamps.",
            }
        )
    elif is_stale:
        hard_block_reason_code = "board_data_stale"
        reasons.append(
            {
                "code": hard_block_reason_code,
                "severity": "block",
                "message": "Board data_as_of is older than the configured freshness threshold.",
            }
        )
    if board_truncated:
        reasons.append(
            {
                "code": "board_candidate_pool_truncated",
                "severity": "warn",
                "message": "Candidate scoring hit the per-request evaluation budget.",
            }
        )

    capital_lane_eligible = hard_block_reason_code is None
    status = "pass" if capital_lane_eligible and not board_truncated else "warn"
    if hard_block_reason_code:
        status = "block"

    missing_keys = sorted(
        set(missing_source_keys or [k for k, v in (source_freshness or {}).items() if not v])
    )
    invalid_keys = sorted(set(invalid_source_keys or []))
    return {
        "gate": "opportunity_board_data_quality",
        "status": status,
        "capital_lane_eligible": capital_lane_eligible,
        "learning_lane_enabled": True,
        "hard_block_reason_code": hard_block_reason_code,
        "data_as_of": data_as_of,
        "data_as_of_min_keys": data_as_of_min_keys,
        "age_seconds": round(age_sec, 3) if age_sec is not None else None,
        "stale_threshold_seconds": int(stale_threshold_seconds),
        "freshness_unknown": bool(freshness_unknown),
        "is_stale": bool(is_stale),
        "board_truncated": bool(board_truncated),
        "missing_source_keys": missing_keys,
        "invalid_source_keys": invalid_keys,
        "source_status": dict(source_status or {}),
        "reasons": reasons,
    }


def _apply_board_data_quality_gate(candidates: list[dict[str, Any]], gate: dict[str, Any]) -> None:
    candidate_gate = dict(gate or {})
    capital_ok = bool(candidate_gate.get("capital_lane_eligible"))
    hard_reason = candidate_gate.get("hard_block_reason_code")
    for it in candidates:
        it["data_quality_gate"] = dict(candidate_gate)
        it["learning_lane"] = {
            "enabled": True,
            "records_decision_packet": True,
            "reason": "Record the observation even when it is not capital-approved.",
        }
        it["capital_lane"] = {
            "board_data_quality_passed": capital_ok,
            "requires_runner_decision_packet": True,
            "approved_for_direct_execution": False,
            "hard_block_reason_code": hard_reason,
        }
        ne = it.get("net_edge_estimate")
        if isinstance(ne, dict):
            ne["data_quality_status"] = candidate_gate.get("status")
            if capital_ok:
                if ne.get("capital_lane") == "shadow_only":
                    ne["capital_lane"] = "requires_runner_decision_packet"
            else:
                ne["capital_lane"] = "blocked_data_quality"
                ne["data_quality_reason_code"] = hard_reason


def _annotate_desk_fields(candidates: list[dict[str, Any]]) -> None:
    """Add operator-desk metadata without changing ranking or tier membership."""
    for it in candidates:
        srcs = it.get("sources") or []
        tier = str(it.get("tier") or "")
        if "pattern_imminent" in srcs:
            eng = OPPORTUNITY_ENGINE_CORE
            badge = "Core Edge"
        elif "pattern_research" in srcs:
            eng = OPPORTUNITY_ENGINE_PATTERN_RESEARCH
            badge = "Pattern incubation"
        elif "live_predictions" in srcs:
            eng = OPPORTUNITY_ENGINE_PREDICTION
            badge = "Prediction context"
        elif "scanner" in srcs:
            eng = OPPORTUNITY_ENGINE_SCANNER
            badge = "Scanner snapshot"
        elif "prescreener" in srcs:
            eng = OPPORTUNITY_ENGINE_UNIVERSE
            badge = "Universe watch"
        else:
            eng = "context_unknown"
            badge = "Context"

        it["opportunity_engine"] = eng
        it["setup_type_badge"] = badge
        it["next_action_label"] = {
            "A": "Act now",
            "B": "Watch soon",
            "C": "Watch today",
            "D": "Incubate",
        }.get(tier, "Watch")

        comp = it.get("composite")
        it["core_edge_score"] = round(float(comp), 4) if comp is not None else None
        scn = it.get("scanner_score")
        it["speculative_momentum_score"] = (
            round(min(1.0, float(scn) / 10.0), 4) if scn is not None else None
        )
        if eng == OPPORTUNITY_ENGINE_CORE:
            it["repeatability_confidence"] = round(0.75 + 0.02 * min(5.0, float(comp or 0)), 4)
            it["primary_scoring_plane"] = "core_repeatable_edge"
        else:
            it["repeatability_confidence"] = 0.35
            it["primary_scoring_plane"] = "auxiliary_context"
        extension = _extension_risk(it)
        execution = _execution_risk(it, extension)
        it["extension_risk"] = extension
        it["execution_risk"] = execution
        it["structural_confirmation"] = _structural_confirmation(it)
        it["liquidity_quality"] = _liquidity_quality(it)
        it["net_edge_estimate"] = _net_edge_estimate(it, execution, extension)


def _attach_board_decision_packets(
    db: Session,
    *,
    user_id: int | None,
    generated_at: datetime,
    data_as_of: str | None,
    tiers: list[list[dict[str, Any]]],
) -> dict[str, int]:
    if not isinstance(db, Session):
        return {"created": 0, "reused": 0}
    candidates = [item for tier in tiers for item in tier]
    if not candidates:
        return {"created": 0, "reused": 0}
    try:
        return attach_shadow_signal_packets(
            db,
            user_id=user_id,
            candidates=candidates,
            source_surface="opportunity_board",
            generated_at=generated_at,
            data_as_of=data_as_of,
            ttl_seconds=int(getattr(settings, "opportunity_board_stale_seconds", 180)),
        )
    except Exception as exc:
        logger.warning("[opportunity_board] decision packet attach failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return {"created": 0, "reused": 0}


def _prediction_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        t = (r.get("ticker") or "").strip().upper()
        if t:
            out[t] = r
    return out


def _pattern_row_to_candidate(
    row: dict[str, Any],
    *,
    tier: str,
    why_here: str,
    why_not_higher: str,
    main_risk: str,
    sources: list[str],
    pred: dict[str, Any] | None,
    source_strength: str = "strong",
) -> dict[str, Any]:
    pat = row["pattern"]
    sc = row["score"]
    ticker = row["ticker"]
    ps = pred or {}
    return {
        "ticker": ticker,
        "asset_class": "crypto" if is_crypto(ticker) else "stocks",
        "tier": tier,
        "sources": sources,
        "source_strength": source_strength,
        "scan_pattern_id": pat.id,
        "pattern_name": pat.name,
        "lifecycle_stage": getattr(pat, "lifecycle_stage", None),
        "promotion_status": getattr(pat, "promotion_status", None),
        "timeframe": pat.timeframe,
        "composite": round(float(row["composite"]), 4),
        "score_breakdown": {k: round(v, 4) for k, v in row["score_breakdown"].items()},
        "readiness": round(float(row["readiness"]), 4),
        "feature_coverage": round(float(row["coverage_ratio"]), 4),
        "eta_hours": [round(row["eta_lo"], 3), round(row["eta_hi"], 3)],
        "eta_label": format_eta_range(row["eta_lo"], row["eta_hi"]),
        "entry": sc.get("entry_price"),
        "stop": sc.get("stop_loss"),
        "target": sc.get("take_profit"),
        "price": sc.get("price"),
        "why_here": why_here,
        "why_not_higher_tier": why_not_higher,
        "main_risk": main_risk,
        "also_in_live_predictions": bool(pred),
        "prediction_support": {
            "direction": ps.get("direction"),
            "confidence": ps.get("confidence"),
        } if pred else None,
        "missing_indicators": row.get("missing_indicators") or [],
    }


def _tier_a_b_c_from_pattern_rows(
    rows: list[dict[str, Any]],
    *,
    pred_by_ticker: dict[str, dict[str, Any]],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split pattern×ticker rows into A/B/C using shared composite + coverage (thresholds only)."""
    min_a_c = float(getattr(settings, "opportunity_tier_a_min_composite", 0.48))
    cov_a = float(getattr(settings, "opportunity_tier_a_min_coverage", 0.5))
    min_b_c = float(getattr(settings, "opportunity_tier_b_min_composite", 0.38))
    cov_b = float(getattr(settings, "opportunity_tier_b_min_coverage", 0.35))
    max_b_eta = float(getattr(settings, "opportunity_tier_b_max_eta_hours", 4.0))
    min_c_c = float(getattr(settings, "opportunity_tier_c_min_composite", 0.28))

    tier_a: list[dict] = []
    tier_b: list[dict] = []
    tier_c: list[dict] = []

    for row in rows:
        pat = row["pattern"]
        comp = float(row["composite"])
        cov = float(row["coverage_ratio"])
        eta_hi = float(row["eta_hi"])
        main_eligible = scan_pattern_eligible_main_imminent(pat)

        pred = pred_by_ticker.get(row["ticker"].upper())

        if (
            main_eligible
            and comp >= min_a_c
            and cov >= cov_a
            and eta_hi <= float(settings.pattern_imminent_max_eta_hours)
        ):
            wh = (
                f"Promoted/live pattern “{pat.name}” is close to firing: readiness "
                f"{row['readiness']:.0%}, ETA {format_eta_range(row['eta_lo'], row['eta_hi'])}."
            )
            wnh = "Already at the highest tier for manual review."
            risk = "Heuristic ETA and partial rules — not a guaranteed breakout."
            tier_a.append(
                _pattern_row_to_candidate(
                    row,
                    tier="A",
                    why_here=wh,
                    why_not_higher=wnh,
                    main_risk=risk,
                    sources=["pattern_imminent", "scan_pattern"],
                    pred=pred,
                )
            )
            continue

        if comp >= min_b_c and cov >= cov_b and eta_hi <= max_b_eta:
            wh = f"Strong setup forming on “{pat.name}” — watch for confirmation."
            wnh = (
                "Below Tier A composite/coverage/ETA bar, or pattern not promoted/live."
                if not main_eligible
                else "Composite or coverage below Tier A threshold."
            )
            risk = "May take longer to resolve or fail if context shifts."
            tier_b.append(
                _pattern_row_to_candidate(
                    row,
                    tier="B",
                    why_here=wh,
                    why_not_higher=wnh,
                    main_risk=risk,
                    sources=["pattern_imminent", "scan_pattern"],
                    pred=pred,
                )
            )
            continue

        if comp >= min_c_c:
            wh = f"Worth monitoring: “{pat.name}” on {row['ticker']} (swing/context)."
            wnh = "Weaker score or wider ETA vs Watch Soon tier."
            risk = "Lower conviction; use smaller size or wait."
            tier_c.append(
                _pattern_row_to_candidate(
                    row,
                    tier="C",
                    why_here=wh,
                    why_not_higher=wnh,
                    main_risk=risk,
                    sources=["pattern_imminent", "scan_pattern"],
                    pred=pred,
                )
            )

    return tier_a, tier_b, tier_c


def _prediction_only_candidates(
    predictions: list[dict[str, Any]],
    seen_tickers: set[str],
    *,
    max_add: int,
) -> list[dict[str, Any]]:
    out: list[dict] = []
    for p in predictions:
        t = (p.get("ticker") or "").strip().upper()
        if not t or t in seen_tickers:
            continue
        seen_tickers.add(t)
        conf = p.get("confidence")
        direction = p.get("direction")
        score = p.get("score")
        wh = f"Live prediction: {direction} bias (score {score}, confidence {conf})."
        out.append({
            "ticker": t,
            "asset_class": "crypto" if is_crypto(t) else "stocks",
            "tier": "C",
            "sources": ["live_predictions"],
            "source_strength": "moderate",
            "scan_pattern_id": None,
            "pattern_name": None,
            "lifecycle_stage": None,
            "promotion_status": None,
            "timeframe": None,
            "composite": None,
            "score_breakdown": None,
            "readiness": None,
            "feature_coverage": None,
            "eta_hours": None,
            "eta_label": None,
            "entry": p.get("price"),
            "stop": p.get("suggested_stop"),
            "target": p.get("suggested_target"),
            "price": p.get("price"),
            "why_here": wh,
            "why_not_higher_tier": "No active pattern×ticker imminent row tied to this ticker.",
            "main_risk": "Prediction-only — no pattern rule coverage check on this row.",
            "also_in_live_predictions": True,
            "prediction_support": {"direction": direction, "confidence": conf},
            "missing_indicators": [],
        })
        if len(out) >= max_add:
            break
    return out


def _scanner_fallback_rows(
    db: Session,
    seen: set[str],
    *,
    max_rows: int,
    min_score_b: float,
    max_age_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Recent AI scanner picks as Tier B (stronger score) or C; no pattern composite."""
    tier_b: list[dict[str, Any]] = []
    tier_c: list[dict[str, Any]] = []
    if max_rows <= 0:
        return tier_b, tier_c
    try:
        query = db.query(ScanResult)
        if max_age_seconds is not None and float(max_age_seconds) > 0:
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                seconds=float(max_age_seconds)
            )
            query = query.filter(ScanResult.scanned_at.isnot(None))
            query = query.filter(ScanResult.scanned_at >= cutoff)
        rows = query.order_by(desc(ScanResult.scanned_at)).limit(max(30, max_rows * 5)).all()
    except Exception as e:
        logger.warning("[opportunity_board] scanner fallback query failed: %s", e)
        return tier_b, tier_c

    used_tickers: set[str] = set()
    for sr in rows:
        if len(tier_b) + len(tier_c) >= max_rows:
            break
        t = (sr.ticker or "").strip().upper()
        if not t or t in seen or t in used_tickers:
            continue
        used_tickers.add(t)
        seen.add(t)
        tier_label = "B" if float(sr.score or 0) >= float(min_score_b) else "C"
        scanned_iso = None
        scanned_age_seconds = None
        if sr.scanned_at:
            dt = sr.scanned_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            scanned_iso = dt.isoformat()
            scanned_age_seconds = max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
        wh = (
            f"Scanner pick ({sr.signal or 'n/a'}) with confluence score {sr.score:.1f}/10"
            + (f", as of {scanned_iso}" if scanned_iso else "")
            + "."
        )
        wnh = (
            "Not derived from ScanPattern imminent rules — Tier A requires promoted/live pattern context."
            if tier_label == "B"
            else "Lower scanner score or older context vs Watch Soon bar."
        )
        risk = "Scanner snapshot only; no live pattern rule evaluation on this row."
        row = {
            "ticker": t,
            "asset_class": "crypto" if is_crypto(t) else "stocks",
            "tier": tier_label,
            "sources": ["scanner"],
            "source_strength": "moderate" if tier_label == "B" else "weak",
            "scan_pattern_id": None,
            "pattern_name": None,
            "lifecycle_stage": None,
            "promotion_status": None,
            "timeframe": None,
            "composite": None,
            "score_breakdown": None,
            "readiness": None,
            "feature_coverage": None,
            "eta_hours": None,
            "eta_label": None,
            "entry": sr.entry_price,
            "stop": sr.stop_loss,
            "target": sr.take_profit,
            "price": None,
            "scanner_score": float(sr.score) if sr.score is not None else None,
            "scanner_signal": sr.signal,
            "scanner_scanned_at_utc": scanned_iso,
            "scanner_age_seconds": round(scanned_age_seconds, 3) if scanned_age_seconds is not None else None,
            "why_here": wh,
            "why_not_higher_tier": wnh,
            "main_risk": risk,
            "also_in_live_predictions": False,
            "prediction_support": None,
            "missing_indicators": [],
        }
        if tier_label == "B":
            tier_b.append(row)
        else:
            tier_c.append(row)
    return tier_b, tier_c


def _prescreener_fallback_rows(
    db: Session,
    seen: set[str],
    *,
    max_rows: int,
) -> list[dict[str, Any]]:
    """Active global prescreen universe — Tier C context only."""
    out: list[dict[str, Any]] = []
    if max_rows <= 0:
        return out
    try:
        rows = (
            db.query(PrescreenCandidate)
            .filter(PrescreenCandidate.user_id.is_(None))
            .filter(PrescreenCandidate.active.is_(True))
            .order_by(desc(PrescreenCandidate.last_seen_at))
            .limit(max_rows * 3)
            .all()
        )
    except Exception as e:
        logger.warning("[opportunity_board] prescreener fallback query failed: %s", e)
        return _prescreener_fallback_list_only(db, seen, max_rows=max_rows)

    for pc in rows:
        if len(out) >= max_rows:
            break
        t = (pc.ticker_norm or pc.ticker or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        ls = pc.last_seen_at
        ls_iso = None
        if ls:
            if ls.tzinfo is None:
                ls = ls.replace(tzinfo=timezone.utc)
            ls_iso = ls.isoformat()
        wh = (
            "Global prescreen candidate (liquidity/universe filter) — worth watching for future pattern alignment."
            + (f" Last seen in prescreen data: {ls_iso}." if ls_iso else "")
        )
        out.append({
            "ticker": t,
            "asset_class": "crypto" if is_crypto(t) else "stocks",
            "tier": "C",
            "sources": ["prescreener"],
            "source_strength": "weak",
            "scan_pattern_id": None,
            "pattern_name": None,
            "lifecycle_stage": None,
            "promotion_status": None,
            "timeframe": None,
            "composite": None,
            "score_breakdown": None,
            "readiness": None,
            "feature_coverage": None,
            "eta_hours": None,
            "eta_label": None,
            "entry": None,
            "stop": None,
            "target": None,
            "price": None,
            "prescreen_last_seen_utc": ls_iso,
            "why_here": wh,
            "why_not_higher_tier": "No ScanPattern×ticker imminent evaluation — not actionable Tier A/B.",
            "main_risk": "Universe listing only; may never match a promoted pattern.",
            "also_in_live_predictions": False,
            "prediction_support": None,
            "missing_indicators": [],
        })
    return out


def _prescreener_fallback_list_only(
    db: Session,
    seen: set[str],
    *,
    max_rows: int,
) -> list[dict[str, Any]]:
    """Fallback when PrescreenCandidate ORM query fails — ticker strings only."""
    out: list[dict[str, Any]] = []
    try:
        tickers = load_active_global_candidate_tickers(db)[: max_rows * 2]
    except Exception:
        return out
    for t_raw in tickers:
        if len(out) >= max_rows:
            break
        t = (t_raw or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append({
            "ticker": t,
            "asset_class": "crypto" if is_crypto(t) else "stocks",
            "tier": "C",
            "sources": ["prescreener"],
            "source_strength": "weak",
            "scan_pattern_id": None,
            "pattern_name": None,
            "lifecycle_stage": None,
            "promotion_status": None,
            "timeframe": None,
            "composite": None,
            "score_breakdown": None,
            "readiness": None,
            "feature_coverage": None,
            "eta_hours": None,
            "eta_label": None,
            "entry": None,
            "stop": None,
            "target": None,
            "price": None,
            "why_here": "Global prescreen candidate (ticker list only; timestamps unavailable).",
            "why_not_higher_tier": "No pattern imminent row — Tier C context.",
            "main_risk": "Universe listing only.",
            "also_in_live_predictions": False,
            "prediction_support": None,
            "missing_indicators": [],
        })
    return out


def get_trading_opportunity_board(
    db: Session,
    user_id: int | None,
    *,
    include_research: bool = False,
    include_debug: bool = False,
    max_per_tier: dict[str, int] | None = None,
) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc)
    iso = generated_at.isoformat()

    eq_open = us_stock_session_open()
    sess = describe_us_session_context()
    crypto_ctx = {"active": True, "label": "Crypto evaluates 24/7 when patterns allow"}

    predictions: list[dict[str, Any]] = []
    try:
        predictions = get_current_predictions(db, None) or []
    except Exception as e:
        logger.warning("[opportunity_board] predictions failed: %s", e)

    pred_by_ticker = _prediction_index(predictions)

    # Freshness: grounded on DB + prediction cache times, not HTTP request duration.
    source_freshness = collect_source_freshness(db)
    freshness_status = compute_board_freshness_status(source_freshness)
    data_as_of = freshness_status.get("data_as_of")
    data_as_of_min_keys = list(freshness_status.get("data_as_of_min_keys") or [])

    rows, meta = gather_imminent_candidate_rows(
        db,
        user_id,
        equity_session_open=eq_open,
        all_active_patterns=True,
        apply_main_dispatch_filters=False,
        for_opportunity_board=True,
    )

    tier_a, tier_b, tier_c = _tier_a_b_c_from_pattern_rows(
        rows, pred_by_ticker=pred_by_ticker,
    )

    seen = {r["ticker"].upper() for r in tier_a + tier_b + tier_c}
    pred_only = _prediction_only_candidates(
        predictions,
        seen,
        max_add=max(0, int(settings.opportunity_max_tier_c)),
    )
    tier_c.extend(pred_only)

    sb_max = int(getattr(settings, "opportunity_board_max_scanner_fallback", 6))
    ps_max = int(getattr(settings, "opportunity_board_max_prescreener_fallback", 8))
    min_sb = float(getattr(settings, "opportunity_board_scanner_fallback_min_score_b", 6.5))
    scan_b, scan_c = _scanner_fallback_rows(
        db,
        seen,
        max_rows=sb_max,
        min_score_b=min_sb,
        max_age_seconds=float(settings.opportunity_board_stale_seconds),
    )
    tier_b.extend(scan_b)
    tier_c.extend(scan_c)
    tier_c.extend(_prescreener_fallback_rows(db, seen, max_rows=ps_max))

    # Re-sort tiers so stronger pattern rows stay first; fallbacks follow.
    tier_b.sort(key=lambda x: (0 if x.get("composite") is not None else 1, -(x.get("composite") or 0)))
    tier_c.sort(key=lambda x: (0 if x.get("composite") is not None else 1, -(x.get("composite") or 0)))

    caps = {
        "A": int(settings.opportunity_max_tier_a),
        "B": int(settings.opportunity_max_tier_b),
        "C": int(settings.opportunity_max_tier_c),
        "D": int(settings.opportunity_max_tier_d),
    }
    if max_per_tier:
        for key, val in max_per_tier.items():
            if val is None or int(val) <= 0:
                continue
            ks = str(key).upper()
            if ks in caps:
                caps[ks] = int(val)
    max_a, max_b, max_c, max_d = caps["A"], caps["B"], caps["C"], caps["D"]

    def cap(lst: list, n: int) -> tuple[list, bool]:
        if len(lst) <= n:
            return lst, False
        return lst[:n], True

    tier_a, more_a = cap(tier_a, max_a)
    tier_b, more_b = cap(tier_b, max_b)
    tier_c, more_c = cap(tier_c, max_c)

    tier_d: list[dict] = []
    if include_research:
        for row in rows[: max_d * 3]:
            pat = row["pattern"]
            if scan_pattern_eligible_main_imminent(pat):
                continue
            t = row["ticker"].upper()
            if any(x["ticker"].upper() == t for x in tier_a + tier_b + tier_c):
                continue
            tier_d.append(
                _pattern_row_to_candidate(
                    row,
                    tier="D",
                    why_here=f"Research context: candidate/backtested pattern “{pat.name}”.",
                    why_not_higher_tier="Not promoted/live — not actionable tier.",
                    main_risk="Experimental; do not treat as production signal.",
                    sources=["pattern_research"],
                    pred=pred_by_ticker.get(t),
                    source_strength="moderate",
                )
            )
            if len(tier_d) >= max_d:
                break

    no_trade = len(tier_a) == 0
    reason_codes: list[str] = []
    summary_lines: list[str] = []

    if meta["skip_reasons"].get("excluded_promotion_lifecycle", 0) and len(tier_a) == 0:
        reason_codes.append("no_promoted_live_patterns_qualified")
        summary_lines.append(
            "No promoted/live patterns produced a Tier A setup after scoring and session gates."
        )
    if not eq_open:
        reason_codes.append("us_stocks_session_closed_or_extended")
        summary_lines.append(
            f"US cash equities: {sess.get('label', 'session limited')} — stock-pattern rows may be sparse."
        )
    elif not sess.get("equity_evaluation_active"):
        reason_codes.append("us_equity_extended_hours")
        summary_lines.append(
            "US stocks are not in regular session — actionable stock setups are intentionally conservative."
        )
    if meta["skip_reasons"].get("readiness_unusable", 0) > 5 and len(rows) < 3:
        reason_codes.append("weak_feature_coverage_common")
        summary_lines.append("Many patterns lack indicator coverage in the swing snapshot — fewer honest setups.")
    if no_trade and rows:
        reason_codes.append("below_tier_a_threshold")
        summary_lines.append("Candidates exist but none cleared Tier A score + coverage + ETA bar.")
    if no_trade and not rows:
        reason_codes.append("no_evaluable_candidates")
        summary_lines.append("No pattern×ticker rows passed readiness and ETA filters.")
    if meta.get("board_eval_budget_hit"):
        reason_codes.append("board_eval_budget_truncated")
        summary_lines.append(
            "Board scoring stopped early to stay within latency caps — some patterns were not fully evaluated."
        )

    stale_sec = int(settings.opportunity_board_stale_seconds)
    now_utc = datetime.now(timezone.utc)
    freshness_unknown = bool(freshness_status.get("freshness_unknown"))
    if data_as_of:
        try:
            das = data_as_of.replace("Z", "+00:00")
            dao = datetime.fromisoformat(das)
            if dao.tzinfo is None:
                dao = dao.replace(tzinfo=timezone.utc)
            else:
                dao = dao.astimezone(timezone.utc)
            age_sec = max(0.0, (now_utc - dao).total_seconds())
            is_stale = age_sec > float(stale_sec)
        except (ValueError, TypeError):
            age_sec = None
            is_stale = True
    else:
        age_sec = None
        is_stale = True

    data_quality_gate = _board_data_quality_gate(
        data_as_of=data_as_of,
        age_sec=age_sec,
        stale_threshold_seconds=stale_sec,
        freshness_unknown=freshness_unknown,
        is_stale=is_stale,
        board_truncated=bool(meta.get("board_eval_budget_hit")),
        data_as_of_min_keys=data_as_of_min_keys,
        source_freshness=source_freshness,
        missing_source_keys=list(freshness_status.get("missing_source_keys") or []),
        invalid_source_keys=list(freshness_status.get("invalid_source_keys") or []),
        source_status=dict(freshness_status.get("source_status") or {}),
    )

    op_sum = {
        "actionable_count": len(tier_a),
        "watch_soon_count": len(tier_b),
        "watch_today_count": len(tier_c),
        "no_trade_now": no_trade,
        "last_refresh_utc": iso,
        "session_line": sess.get("label", "") + " · " + crypto_ctx.get("label", ""),
        "data_freshness_unknown": freshness_unknown,
        "data_quality_status": data_quality_gate.get("status"),
        "capital_lane_eligible": data_quality_gate.get("capital_lane_eligible"),
        "missing_source_keys": data_quality_gate.get("missing_source_keys"),
        "invalid_source_keys": data_quality_gate.get("invalid_source_keys"),
    }

    _annotate_desk_fields(tier_a)
    _annotate_desk_fields(tier_b)
    _annotate_desk_fields(tier_c)
    _annotate_desk_fields(tier_d)
    _apply_board_data_quality_gate(tier_a, data_quality_gate)
    _apply_board_data_quality_gate(tier_b, data_quality_gate)
    _apply_board_data_quality_gate(tier_c, data_quality_gate)
    _apply_board_data_quality_gate(tier_d, data_quality_gate)
    decision_packet_stats = _attach_board_decision_packets(
        db,
        user_id=user_id,
        generated_at=generated_at,
        data_as_of=data_as_of,
        tiers=[tier_a, tier_b, tier_c, tier_d],
    )

    speculative_envelope: dict[str, Any]
    try:
        speculative_envelope = build_speculative_momentum_slice(
            db,
            limit=12,
            max_scan_age_seconds=float(settings.opportunity_board_stale_seconds),
        )
    except Exception as e:
        logger.warning("[opportunity_board] speculative slice failed: %s", e)
        speculative_envelope = {
            "ok": False,
            "engine": "speculative_momentum",
            "items": [],
            "generated_at": iso,
            "error": str(e),
            "methodology": "heuristic_scan_result_inference",
            "methodology_note": "Speculative momentum slice failed — see error.",
        }

    out: dict[str, Any] = {
        "ok": True,
        "generated_at": iso,
        # data_as_of: conservative UTC instant — board narrative cannot be newer than this.
        "data_as_of": data_as_of,
        "data_as_of_explanation": (
            "Minimum (stalest) of non-null source timestamps in source_freshness; "
            "the composite board is not fresher than its weakest feed."
        ),
        "data_as_of_min_keys": data_as_of_min_keys,
        "source_freshness": source_freshness,
        "source_status": freshness_status.get("source_status"),
        "missing_source_keys": freshness_status.get("missing_source_keys"),
        "invalid_source_keys": freshness_status.get("invalid_source_keys"),
        "age_seconds": round(age_sec, 3) if age_sec is not None else None,
        "is_stale": is_stale,
        "stale_threshold_seconds": stale_sec,
        "freshness_degraded": freshness_unknown,
        "data_quality_gate": data_quality_gate,
        # True when board gather hit per-request score/universe caps (not an error; UI should say "sampled").
        "board_truncated": bool(meta.get("board_eval_budget_hit")),
        "session_context": {**sess, "crypto_context": crypto_ctx},
        "operator_summary": op_sum,
        "no_trade_now": no_trade,
        "no_trade_reason_codes": reason_codes,
        "no_trade_summary_lines": summary_lines,
        "counts": {
            "tier_a": len(tier_a),
            "tier_b": len(tier_b),
            "tier_c": len(tier_c),
            "tier_d": len(tier_d),
        },
        "tiers": {
            "actionable_now": tier_a,
            "watch_soon": tier_b,
            "watch_today": tier_c,
            "research_only": tier_d,
        },
        "has_more": {"A": more_a, "B": more_b, "C": more_c},
        "applied_tier_caps": {"A": max_a, "B": max_b, "C": max_c, "D": max_d},
        "decision_packet_stats": decision_packet_stats,
        "source_stats": meta.get("universe_by_source", {}),
        # Isolated plane for explosive / speculative movers (not merged into tier scoring).
        "speculative_movers": speculative_envelope,
    }
    if include_debug:
        out["debug"] = {
            "skip_reasons": meta.get("skip_reasons"),
            "top_suppressed": meta.get("top_suppressed"),
            "tickers_scored": meta.get("tickers_scored"),
            "board_eval_budget_hit": meta.get("board_eval_budget_hit"),
            "board_per_pattern_cap": meta.get("board_per_pattern_cap"),
            "board_score_budget": meta.get("board_score_budget"),
        }
    return out
