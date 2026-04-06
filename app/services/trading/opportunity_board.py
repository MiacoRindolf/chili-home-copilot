"""Trading Brain opportunity board: tiered manual-trading view (shared scoring with imminent)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from sqlalchemy import desc

from ...config import settings
from ...models.trading import PrescreenCandidate, ScanResult
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
from .trading_source_freshness import collect_source_freshness, compute_board_data_as_of

logger = logging.getLogger(__name__)


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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Recent AI scanner picks as Tier B (stronger score) or C; no pattern composite."""
    tier_b: list[dict[str, Any]] = []
    tier_c: list[dict[str, Any]] = []
    if max_rows <= 0:
        return tier_b, tier_c
    try:
        rows = (
            db.query(ScanResult)
            .order_by(desc(ScanResult.scanned_at))
            .limit(max(30, max_rows * 5))
            .all()
        )
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
        if sr.scanned_at:
            dt = sr.scanned_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            scanned_iso = dt.isoformat()
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
    data_as_of, data_as_of_min_keys = compute_board_data_as_of(source_freshness)

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
    scan_b, scan_c = _scanner_fallback_rows(db, seen, max_rows=sb_max, min_score_b=min_sb)
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
    freshness_unknown = data_as_of is None
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

    op_sum = {
        "actionable_count": len(tier_a),
        "watch_soon_count": len(tier_b),
        "watch_today_count": len(tier_c),
        "no_trade_now": no_trade,
        "last_refresh_utc": iso,
        "session_line": sess.get("label", "") + " · " + crypto_ctx.get("label", ""),
        "data_freshness_unknown": freshness_unknown,
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
        "age_seconds": round(age_sec, 3) if age_sec is not None else None,
        "is_stale": is_stale,
        "stale_threshold_seconds": stale_sec,
        "freshness_degraded": freshness_unknown,
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
        "source_stats": meta.get("universe_by_source", {}),
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
