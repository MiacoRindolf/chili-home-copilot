"""Unified Autopilot opportunity feed for stocks and crypto."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumStrategyVariant, MomentumSymbolViability
from ..scanner import get_crypto_breakout_cache, run_momentum_scanner
from .market_profile import asset_class_for_symbol, is_coinbase_spot_symbol, market_open_now
from .operator_readiness import build_momentum_operator_readiness
from .strategy_params import summarize_strategy_params
from .viability_health import get_viability_pipeline_health
from .viability_scope import VIABILITY_SCOPE_SYMBOL


def _fresh_cutoff() -> datetime:
    fresh_sec = max(1800.0, float(settings.chili_momentum_risk_viability_max_age_seconds) * 6.0)
    return datetime.utcnow() - timedelta(seconds=fresh_sec)


def _scan_score(row: dict[str, Any]) -> float:
    for key in ("score", "composite_score", "scanner_score"):
        try:
            val = row.get(key)
            if val is not None:
                return float(val)
        except (TypeError, ValueError):
            continue
    return 0.0


def _scan_symbol(row: dict[str, Any]) -> str:
    return str(row.get("ticker") or row.get("symbol") or "").strip().upper()


def _scan_has_signal(row: dict[str, Any]) -> bool:
    signal = str(row.get("signal") or "").strip().lower()
    return bool(signal and signal not in ("hold", "none"))


def _scan_context(row: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "source": source,
        "score": _scan_score(row),
        "signal": row.get("signal"),
        "label": row.get("label") or row.get("name"),
        "signals": list(row.get("signals") or [])[:4] if isinstance(row.get("signals"), list) else [],
        "raw": {
            "price": row.get("price") or row.get("entry_price"),
            "risk_reward": row.get("risk_reward"),
            "volume_ratio": row.get("vol_ratio") or row.get("relative_volume"),
        },
    }


def _top_variant_payload(
    symbol: str,
    viability: MomentumSymbolViability | None,
    variant: MomentumStrategyVariant | None,
) -> dict[str, Any] | None:
    if viability is None or variant is None:
        return None
    exec_r = viability.execution_readiness_json if isinstance(viability.execution_readiness_json, dict) else {}
    asset_class = asset_class_for_symbol(symbol)
    open_now = market_open_now(symbol)
    paper_ready = bool(viability.paper_eligible) and (open_now if asset_class == "stock" else True)
    live_ready = (
        bool(viability.live_eligible)
        and is_coinbase_spot_symbol(symbol)
        and exec_r.get("product_tradable") is not False
        and (open_now if asset_class == "stock" else True)
    )
    return {
        "variant_id": int(variant.id),
        "label": variant.label,
        "family": variant.family,
        "version": int(variant.version),
        "execution_family": variant.execution_family or "coinbase_spot",
        "viability_scope": getattr(viability, "scope", VIABILITY_SCOPE_SYMBOL),
        "viability_score": round(float(viability.viability_score or 0.0), 4),
        "paper_ready": paper_ready,
        "live_ready": live_ready,
        "paper_eligible": bool(viability.paper_eligible),
        "live_eligible": bool(viability.live_eligible),
        "strategy_params_summary": summarize_strategy_params(variant.params_json),
        "refinement_info": {
            "is_refined": bool(getattr(variant, "parent_variant_id", None)),
            "parent_variant_id": getattr(variant, "parent_variant_id", None),
            "meta": variant.refinement_meta_json if isinstance(variant.refinement_meta_json, dict) else {},
        },
    }


def _paper_action_payload(
    *,
    symbol: str,
    top_variant: dict[str, Any] | None,
    paper_ready: bool,
) -> dict[str, Any]:
    execution_family = str((top_variant or {}).get("execution_family") or "coinbase_spot")
    readiness = build_momentum_operator_readiness(execution_family=execution_family, symbol=symbol)
    enabled = bool(
        top_variant
        and paper_ready
        and readiness.get("execution_family_implemented")
        and readiness.get("momentum_neural_enabled")
        and not readiness.get("governance_blocks_paper")
    )
    can_run_paper = enabled and bool(readiness.get("runnable_paper_now"))
    blocked_reason = None
    detail = None
    if enabled and not can_run_paper:
        if not readiness.get("paper_runner_enabled"):
            detail = "Creates a draft only until the paper runner is enabled."
        elif not readiness.get("paper_scheduler_would_run"):
            detail = "Creates a draft only until paper scheduling is healthy."
        else:
            detail = "Creates a draft only; simulation execution is not runnable yet."
    elif not top_variant:
        blocked_reason = "no_fresh_viability"
    elif not paper_ready:
        blocked_reason = "market_closed" if asset_class_for_symbol(symbol) == "stock" and not market_open_now(symbol) else "paper_not_ready"
    elif not readiness.get("execution_family_implemented"):
        blocked_reason = "execution_family_not_implemented"
    elif not readiness.get("momentum_neural_enabled"):
        blocked_reason = "momentum_neural_disabled"
    elif readiness.get("governance_blocks_paper"):
        blocked_reason = "governance_kill_switch"

    return {
        "label": "Run paper" if can_run_paper else "Create draft",
        "enabled": enabled,
        "can_create_paper_draft": enabled,
        "can_run_paper": can_run_paper,
        "blocked_reason": blocked_reason,
        "detail": detail,
        "runner_runnable_now": bool(readiness.get("runnable_paper_now")),
    }


def _live_action_payload(
    *,
    symbol: str,
    top_variant: dict[str, Any] | None,
    live_ready: bool,
) -> dict[str, Any]:
    execution_family = str((top_variant or {}).get("execution_family") or "coinbase_spot")
    readiness = build_momentum_operator_readiness(execution_family=execution_family, symbol=symbol)
    enabled = bool(
        top_variant
        and live_ready
        and readiness.get("execution_family_implemented")
        and readiness.get("momentum_neural_enabled")
        and not readiness.get("governance_blocks_live")
        and readiness.get("broker_ready_for_live")
        and readiness.get("execution_ready")
    )
    armed_only = enabled and not bool(readiness.get("runnable_live_now"))
    blocked_reason = None
    detail = None
    if armed_only:
        detail = "Live arm is available, but execution stays armed-only until the live runner is enabled."
    elif not top_variant:
        blocked_reason = "no_fresh_viability"
    elif not live_ready:
        if asset_class_for_symbol(symbol) == "stock" and not market_open_now(symbol):
            blocked_reason = "market_closed"
        elif not is_coinbase_spot_symbol(symbol):
            blocked_reason = "live_symbol_not_coinbase"
        else:
            blocked_reason = "live_not_ready"
    elif not readiness.get("execution_family_implemented"):
        blocked_reason = "execution_family_not_implemented"
    elif not readiness.get("momentum_neural_enabled"):
        blocked_reason = "momentum_neural_disabled"
    elif readiness.get("governance_blocks_live"):
        blocked_reason = "governance_kill_switch"
    elif not readiness.get("broker_ready_for_live"):
        blocked_reason = "broker_not_ready"
    elif not readiness.get("execution_ready"):
        blocked_reason = "execution_not_ready"

    return {
        "label": "Arm live",
        "enabled": enabled,
        "can_arm_live": enabled,
        "blocked_reason": blocked_reason,
        "detail": detail,
        "armed_only": armed_only,
        "runner_runnable_now": bool(readiness.get("runnable_live_now")),
    }


def _blocked_reasons(
    *,
    symbol: str,
    asset_class: str,
    market_open: bool,
    paper_ready: bool,
    live_ready: bool,
    top_variant: dict[str, Any] | None,
) -> list[str]:
    reasons: list[str] = []
    if asset_class == "stock" and not market_open:
        reasons.append("market_closed")
    if top_variant is None:
        reasons.append("no_fresh_viability")
        return reasons
    if not paper_ready:
        reasons.append("paper_not_ready")
    if not live_ready:
        if asset_class != "crypto":
            reasons.append("live_asset_not_supported")
        elif not is_coinbase_spot_symbol(symbol):
            reasons.append("live_symbol_not_coinbase")
        else:
            reasons.append("live_not_ready")
    return reasons


def list_momentum_opportunities(
    db: Session,
    *,
    mode: str = "paper",
    asset_filter: str = "all",
    limit: int = 60,
) -> dict[str, Any]:
    selected_mode = "live" if (mode or "").strip().lower() == "live" else "paper"
    selected_asset = (asset_filter or "all").strip().lower()
    fresh_cutoff = _fresh_cutoff()

    rows = (
        db.query(MomentumSymbolViability, MomentumStrategyVariant)
        .join(MomentumStrategyVariant, MomentumStrategyVariant.id == MomentumSymbolViability.variant_id)
        .filter(MomentumStrategyVariant.is_active.is_(True))
        .filter(MomentumSymbolViability.scope == VIABILITY_SCOPE_SYMBOL)
        .filter(MomentumSymbolViability.freshness_ts >= fresh_cutoff)
        .order_by(MomentumSymbolViability.symbol.asc(), MomentumSymbolViability.viability_score.desc())
        .all()
    )

    viability_by_symbol: dict[str, tuple[MomentumSymbolViability, MomentumStrategyVariant]] = {}
    for viability, variant in rows:
        sym = str(viability.symbol or "").strip().upper()
        current = viability_by_symbol.get(sym)
        if current is None or float(viability.viability_score or 0.0) > float(current[0].viability_score or 0.0):
            viability_by_symbol[sym] = (viability, variant)

    stock_scan = run_momentum_scanner(max_results=20)
    stock_rows = list(stock_scan.get("results") or [])
    crypto_scan = get_crypto_breakout_cache()
    crypto_rows = list(crypto_scan.get("results") or [])

    scan_map: dict[str, dict[str, Any]] = {}
    for row in stock_rows:
        sym = _scan_symbol(row)
        if sym:
            scan_map[sym] = _scan_context(row, "momentum_scanner")
    for row in crypto_rows:
        sym = _scan_symbol(row)
        if sym and sym not in scan_map:
            scan_map[sym] = _scan_context(row, "crypto_breakout")

    filtered_scan_symbols: set[str] = set()
    hidden_scan_only_count = 0
    visible: list[dict[str, Any]] = []
    discovered: list[dict[str, Any]] = []
    hidden_market_closed_count = 0
    hidden_non_actionable_count = 0

    for sym in set(scan_map) | set(viability_by_symbol):
        asset_class = asset_class_for_symbol(sym)
        if selected_asset in ("stock", "crypto") and asset_class != selected_asset:
            continue
        if sym in scan_map:
            filtered_scan_symbols.add(sym)
        viability_pair = viability_by_symbol.get(sym)
        if viability_pair is None and sym in scan_map:
            hidden_scan_only_count += 1
            discovered.append({
                "symbol": sym,
                "asset_class": asset_class,
                "market_open_now": market_open_now(sym),
                "needs_viability_assessment": True,
                "scan_context": scan_map[sym],
            })
            continue

        market_open = market_open_now(sym)
        top_variant = _top_variant_payload(sym, viability_pair[0], viability_pair[1]) if viability_pair else None
        paper_ready = bool(top_variant and top_variant.get("paper_ready"))
        live_ready = bool(top_variant and top_variant.get("live_ready"))
        paper_action = _paper_action_payload(symbol=sym, top_variant=top_variant, paper_ready=paper_ready)
        live_action = _live_action_payload(symbol=sym, top_variant=top_variant, live_ready=live_ready)
        actionable_now = bool(live_action.get("enabled")) if selected_mode == "live" else bool(paper_action.get("enabled"))
        if not actionable_now:
            hidden_non_actionable_count += 1
            if asset_class == "stock" and not market_open:
                hidden_market_closed_count += 1
            continue

        scan_context = scan_map.get(sym)
        blocked = _blocked_reasons(
            symbol=sym,
            asset_class=asset_class,
            market_open=market_open,
            paper_ready=paper_ready,
            live_ready=live_ready,
            top_variant=top_variant,
        )
        visible.append(
            {
                "symbol": sym,
                "asset_class": asset_class,
                "market_open_now": market_open,
                "compatible_now": actionable_now,
                "paper_ready": paper_ready,
                "live_ready": live_ready,
                "can_create_paper_draft": bool(paper_action.get("can_create_paper_draft")),
                "can_run_paper": bool(paper_action.get("can_run_paper")),
                "can_arm_live": bool(live_action.get("can_arm_live")),
                "paper_action": paper_action,
                "live_action": live_action,
                "blocked_reasons": blocked,
                "scan_context": scan_context,
                "top_variant": top_variant,
                "freshness_ts": (
                    viability_pair[0].freshness_ts.isoformat()
                    if viability_pair and viability_pair[0].freshness_ts
                    else None
                ),
            }
        )

    def _sort_key(row: dict[str, Any]) -> tuple[float, float, float, float, float]:
        scan = row.get("scan_context") or {}
        top_variant = row.get("top_variant") or {}
        freshness = row.get("freshness_ts")
        try:
            freshness_ts = datetime.fromisoformat(str(freshness).replace("Z", "+00:00")).timestamp() if freshness else 0.0
        except Exception:
            freshness_ts = 0.0
        return (
            1.0 if row.get("compatible_now") else 0.0,
            1.0 if _scan_has_signal(scan) else 0.0,
            float(scan.get("score") or 0.0),
            float(top_variant.get("viability_score") or 0.0),
            freshness_ts,
        )

    visible.sort(key=_sort_key, reverse=True)
    discovered.sort(key=lambda d: float((d.get("scan_context") or {}).get("score") or 0), reverse=True)
    pipeline = get_viability_pipeline_health(db)
    return {
        "ok": True,
        "mode": selected_mode,
        "asset_filter": selected_asset,
        "opportunities": visible[: max(1, min(int(limit), 200))],
        "discovered": discovered[:20],
        "metadata": {
            "fresh_cutoff_utc": fresh_cutoff.isoformat(),
            "stock_scan_count": len(stock_rows),
            "crypto_scan_count": len(crypto_rows),
            "scan_symbol_count": len(filtered_scan_symbols),
            "viability_symbol_count": len(viability_by_symbol),
            "visible_opportunity_count": len(visible),
            "discovered_count": len(discovered),
            "hidden_scan_only_count": hidden_scan_only_count,
            "hidden_non_actionable_count": hidden_non_actionable_count,
            "market_closed_hidden_count": hidden_market_closed_count,
            **pipeline,
        },
    }
