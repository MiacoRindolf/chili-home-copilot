"""Portfolio-aware alpha promotion gate.

The existing promotion stack evaluates whether a single pattern has enough
evidence to enter shadow/pilot/promoted lifecycles. This layer adds the missing
portfolio question: does the candidate improve the alpha book, or does it add
more of the same exposure while stale promoted patterns have not been recerted?

The service is conservative by design:

* full broker-risk promotion is blocked while promoted/pilot patterns carry
  recert debt or execution-quality evidence is not clean;
* portfolio candidates can still move to ``shadow_promoted`` because that lane
  is broker-blocked and exists to collect fresh EV;
* all decisions are auditable through ``portfolio_gate_json`` and
  ``trading_alpha_portfolio_gate_audit``.
"""
from __future__ import annotations

import json
import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.trading import ScanPattern
from .realized_pnl_sql import trade_return_fraction_sql

logger = logging.getLogger(__name__)

RISK_LIFECYCLES = frozenset({"promoted", "live", "pilot_promoted"})
FULL_RISK_LIFECYCLES = frozenset({"promoted", "live"})
SHADOW_LIFECYCLE = "shadow_promoted"
CANDIDATE_LIFECYCLES = frozenset({"candidate", "backtested", "challenged"})
PILOT_BOOTSTRAP_RECERT_REASONS = frozenset({
    "missing_oos_recert",
    "thin_oos_recert",
    "missing_quality_composite_score",
    "thin_realized_ev",
})
BACKTEST_SOLVABLE_RECERT_REASONS = frozenset({
    "missing_oos_recert",
    "thin_oos_recert",
    "stale_oos_recert",
    "promotion_gate_not_currently_passed",
})
BROKER_RISK_PROBATION_RECERT_REASONS = frozenset({
    "missing_oos_recert",
    "thin_oos_recert",
    "stale_oos_recert",
})
BROKER_RISK_PROBATION_LIFECYCLES = frozenset({"promoted"})
BROKER_RISK_PROBATION_DEFAULT_MIN_CPCV_SHARPE = 1.0
BROKER_RISK_PROBATION_DEFAULT_MIN_REALIZED_TRADES = 5
PRIORITY_RECERT_PATTERN_IDS_SETTING = "brain_recert_queue_priority_pattern_ids"


@dataclass(frozen=True)
class AlphaPortfolioConfig:
    recert_stale_days: int = 30
    min_realized_trades: int = 5
    min_oos_trades: int = 5
    min_oos_avg_return_pct: float = 0.0
    min_oos_win_rate: float = 0.0
    min_risk_sleeves: int = 3
    min_shadow_score: float = 0.52
    max_shadow_total: int = 4
    max_shadow_per_sleeve: int = 1
    execution_lookback_days: int = 30
    execution_min_samples: int = 10
    execution_max_p90_slippage_pct: float = 0.75


def config_from_settings(settings_: Any) -> AlphaPortfolioConfig:
    return AlphaPortfolioConfig(
        recert_stale_days=int(getattr(
            settings_, "chili_alpha_portfolio_recert_stale_days", 30,
        )),
        min_realized_trades=int(getattr(
            settings_, "chili_alpha_portfolio_min_realized_trades", 5,
        )),
        min_oos_trades=int(getattr(
            settings_, "chili_alpha_portfolio_min_oos_trades", 5,
        )),
        min_oos_avg_return_pct=float(getattr(
            settings_, "chili_alpha_portfolio_min_oos_avg_return_pct", 0.0,
        )),
        min_oos_win_rate=float(getattr(
            settings_, "chili_alpha_portfolio_min_oos_win_rate", 0.0,
        )),
        min_risk_sleeves=int(getattr(
            settings_, "chili_alpha_portfolio_min_risk_sleeves", 3,
        )),
        min_shadow_score=float(getattr(
            settings_, "chili_alpha_portfolio_min_shadow_score", 0.52,
        )),
        max_shadow_total=int(getattr(
            settings_, "chili_alpha_portfolio_max_shadow_total", 4,
        )),
        max_shadow_per_sleeve=int(getattr(
            settings_, "chili_alpha_portfolio_max_shadow_per_sleeve", 1,
        )),
        execution_lookback_days=int(getattr(
            settings_, "chili_alpha_portfolio_execution_lookback_days", 30,
        )),
        execution_min_samples=int(getattr(
            settings_, "chili_alpha_portfolio_execution_min_samples", 10,
        )),
        execution_max_p90_slippage_pct=float(getattr(
            settings_,
            "chili_alpha_portfolio_execution_max_p90_slippage_pct",
            0.75,
        )),
    )


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _priority_recert_pattern_ids(settings_: Any) -> list[int]:
    raw = str(getattr(settings_, PRIORITY_RECERT_PATTERN_IDS_SETTING, "") or "")
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            logger.debug("[alpha_portfolio_gate] invalid priority pattern id: %s", token)
    return out


def _clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _lower_blob(pattern: Any) -> str:
    pieces = [
        _get(pattern, "name", ""),
        _get(pattern, "description", ""),
        _get(pattern, "hypothesis_family", ""),
        _get(pattern, "asset_class", ""),
        _get(pattern, "timeframe", ""),
        _get(pattern, "origin", ""),
    ]
    rules = _get(pattern, "rules_json", None)
    if isinstance(rules, dict):
        pieces.append(json.dumps(rules, sort_keys=True, default=str))
    elif rules is not None:
        pieces.append(str(rules))
    return " ".join(str(p or "") for p in pieces).lower()


def _timeframe_minutes(value: Any) -> int | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    unit = raw[-1]
    number = raw[:-1]
    try:
        qty = int(number)
    except Exception:
        return None
    if unit == "m":
        return qty
    if unit == "h":
        return qty * 60
    if unit == "d":
        return qty * 1440
    return None


def infer_alpha_sleeve(pattern: Any) -> str:
    """Infer a stable alpha sleeve from pattern metadata.

    The labels are intentionally coarse. The point is not perfect taxonomy; it
    is to prevent a promotion book made entirely of near-duplicate momentum or
    reversal patterns.
    """
    blob = _lower_blob(pattern)
    asset = str(_get(pattern, "asset_class", "") or "").lower()
    minutes = _timeframe_minutes(_get(pattern, "timeframe", ""))
    intraday = minutes is not None and minutes <= 60

    is_crypto = (
        "crypto" in asset
        or "crypto" in blob
        or any(tok in blob for tok in ("btc", "eth", "sol", "bnb", "avax"))
    )
    is_short = any(tok in blob for tok in (
        "sell signal", "short", "overbought", "macd histogram negative",
        "bearish",
    ))
    is_reversal = any(tok in blob for tok in (
        "mean_reversion", "mean reversion", "reversal", "oversold",
        "pullback", "capitulation", "divergence", "lower bollinger",
        "lower bb", "rsi<", "ibs",
    ))
    is_breakout = any(tok in blob for tok in (
        "breakout", "squeeze", "compression_expansion",
        "volatility expansion", "bb_squeeze", "trend reclaim",
    ))
    is_momentum = any(tok in blob for tok in (
        "momentum", "trend", "ema stack", "ema stacking", "adx",
        "macd histogram positive",
    ))

    if is_short:
        return "short_overbought"
    if intraday and is_reversal:
        return "crypto_intraday_reversal" if is_crypto else "intraday_reversal"
    if "ibs" in blob or ("mean reversion" in blob and not intraday):
        return "daily_mean_reversion"
    if is_breakout:
        return "volatility_breakout"
    if is_reversal:
        return "swing_reversal"
    if is_momentum:
        return "trend_momentum"
    if is_crypto:
        return "crypto_general"
    return "general_patterns"


def recert_reasons_for_pattern(
    pattern: Any,
    *,
    now: datetime | None = None,
    config: AlphaPortfolioConfig | None = None,
) -> list[str]:
    """Return why a broker-risk pattern needs re-certification."""
    cfg = config or AlphaPortfolioConfig()
    now = now or datetime.utcnow()
    lifecycle = str(_get(pattern, "lifecycle_stage", "") or "").strip().lower()
    promotion_status = str(_get(pattern, "promotion_status", "") or "").strip().lower()
    broker_risk = lifecycle in RISK_LIFECYCLES or promotion_status == "promoted"
    if not broker_risk:
        return []

    reasons: list[str] = []
    if _get(pattern, "promotion_gate_passed", None) is not True:
        reasons.append("promotion_gate_not_currently_passed")

    oos_at = _get(pattern, "oos_evaluated_at", None)
    if oos_at is None:
        reasons.append("missing_oos_recert")
    elif isinstance(oos_at, datetime) and oos_at < now - timedelta(days=cfg.recert_stale_days):
        reasons.append("stale_oos_recert")
    else:
        oos_n = _safe_int(_get(pattern, "oos_trade_count", None)) or 0
        if oos_n < cfg.min_oos_trades:
            reasons.append("thin_oos_recert")
        oos_avg = _safe_float(_get(pattern, "oos_avg_return_pct", None))
        if oos_avg is not None and oos_avg < cfg.min_oos_avg_return_pct:
            reasons.append("negative_oos_recert")
        oos_wr = _safe_float(_get(pattern, "oos_win_rate", None))
        if oos_wr is not None and oos_wr > 1.0:
            oos_wr = oos_wr / 100.0
        if (
            cfg.min_oos_win_rate > 0.0
            and oos_wr is not None
            and oos_wr < cfg.min_oos_win_rate
        ):
            reasons.append("weak_oos_win_rate_recert")

    if _get(pattern, "quality_composite_score", None) is None:
        reasons.append("missing_quality_composite_score")

    raw_n = _safe_int(_get(pattern, "raw_realized_trade_count", None)) or 0
    if raw_n < cfg.min_realized_trades:
        reasons.append("thin_realized_ev")

    raw_avg = _safe_float(_get(pattern, "raw_realized_avg_return_pct", None))
    payoff = _safe_float(_get(pattern, "payoff_ratio", None))
    payoff_n = _safe_int(_get(pattern, "payoff_ratio_n", None)) or 0
    payoff_protected = payoff is not None and payoff >= 1.5 and payoff_n >= cfg.min_realized_trades
    if raw_avg is not None and raw_avg < 0.0 and not payoff_protected:
        reasons.append("negative_realized_ev")

    return reasons


def _recert_reason_set(pattern: Any) -> set[str]:
    raw = _get(pattern, "recert_reason", None)
    reasons: set[str] = set()
    if isinstance(raw, str):
        reasons.update(x.strip() for x in raw.split(",") if x.strip())
    elif isinstance(raw, (list, tuple, set)):
        reasons.update(str(x).strip() for x in raw if str(x).strip())

    gate_json = _get(pattern, "portfolio_gate_json", None)
    if isinstance(gate_json, Mapping):
        nested = gate_json.get("recert_reasons")
        if isinstance(nested, str):
            reasons.update(x.strip() for x in nested.split(",") if x.strip())
        elif isinstance(nested, (list, tuple, set)):
            reasons.update(str(x).strip() for x in nested if str(x).strip())
    return reasons


def pilot_bootstrap_recert_allows_live(
    pattern: Any,
    *,
    settings_: Any = None,
    now: datetime | None = None,
) -> bool:
    """Allow tiny pilot live ramps through bootstrap certification debt only.

    A ``pilot_promoted`` pattern is deliberately not full-risk certified yet.
    Missing OOS, missing quality score, and thin realized EV are the exact
    evidence gaps the pilot lane is designed to close. Hard failures like a
    failed promotion gate, negative realized EV, or stale certification still
    block broker orders.
    """
    if settings_ is None:
        from ...config import settings as _settings

        settings_ = _settings
    if not bool(getattr(settings_, "chili_pilot_promoted_allow_bootstrap_recert_live", True)):
        return False
    lifecycle = str(_get(pattern, "lifecycle_stage", "") or "").strip().lower()
    if lifecycle != "pilot_promoted":
        return False
    if not bool(_get(pattern, "recert_required", False)):
        return False

    reasons = _recert_reason_set(pattern)
    if not reasons:
        try:
            reasons = set(
                recert_reasons_for_pattern(
                    pattern,
                    now=now,
                    config=config_from_settings(settings_),
                )
            )
        except Exception:
            reasons = set()
    if not reasons:
        return False
    return reasons.issubset(PILOT_BOOTSTRAP_RECERT_REASONS)


def broker_risk_probation_allows_live(
    pattern: Any,
    *,
    settings_: Any = None,
    now: datetime | None = None,
) -> bool:
    """Allow reduced-risk live entries through soft OOS recert debt only."""
    if settings_ is None:
        from ...config import settings as _settings

        settings_ = _settings
    if not bool(getattr(settings_, "chili_autotrader_probation_live_enabled", True)):
        return False
    lifecycle = str(_get(pattern, "lifecycle_stage", "") or "").strip().lower()
    if lifecycle not in BROKER_RISK_PROBATION_LIFECYCLES:
        return False
    if not bool(_get(pattern, "recert_required", False)):
        return False
    if _get(pattern, "promotion_gate_passed", None) is not True:
        return False

    reasons = _recert_reason_set(pattern)
    if not reasons:
        try:
            reasons = set(
                recert_reasons_for_pattern(
                    pattern,
                    now=now,
                    config=config_from_settings(settings_),
                )
            )
        except Exception:
            reasons = set()
    if not reasons or not reasons.issubset(BROKER_RISK_PROBATION_RECERT_REASONS):
        return False

    cpcv = _safe_float(_get(pattern, "cpcv_median_sharpe", None))
    min_cpcv = float(
        getattr(
            settings_,
            "chili_autotrader_probation_min_cpcv_sharpe",
            BROKER_RISK_PROBATION_DEFAULT_MIN_CPCV_SHARPE,
        )
    )
    if cpcv is None or cpcv < min_cpcv:
        return False

    realized_n = (
        _safe_int(_get(pattern, "raw_realized_trade_count", None))
        or _safe_int(_get(pattern, "realized_n_trades", None))
        or 0
    )
    min_realized = int(
        getattr(
            settings_,
            "chili_autotrader_probation_min_realized_trades",
            BROKER_RISK_PROBATION_DEFAULT_MIN_REALIZED_TRADES,
        )
    )
    if realized_n < min_realized:
        return False

    realized_avg = _safe_float(_get(pattern, "raw_realized_avg_return_pct", None))
    if realized_avg is None:
        realized_avg = _safe_float(_get(pattern, "realized_avg_pnl_pct", None))
    return bool(realized_avg is not None and realized_avg > 0.0)


def _realized_edge_fraction(row: Mapping[str, Any]) -> tuple[float | None, int]:
    realized_n = _safe_int(row.get("realized_n_trades")) or 0
    avg = _safe_float(row.get("realized_avg_pnl_pct"))
    if avg is not None:
        return avg, realized_n

    raw_n = _safe_int(row.get("raw_realized_trade_count")) or 0
    raw_avg_pct = _safe_float(row.get("raw_realized_avg_return_pct"))
    if raw_avg_pct is None:
        return None, raw_n
    return raw_avg_pct / 100.0, raw_n


def candidate_base_score(
    row: Mapping[str, Any],
    *,
    config: AlphaPortfolioConfig | None = None,
) -> dict[str, Any]:
    """Compute single-pattern quality before portfolio crowding adjustment."""
    cfg = config or AlphaPortfolioConfig()
    components: dict[str, dict[str, float]] = {}

    quality = _safe_float(row.get("quality_composite_score"))
    if quality is not None:
        components["quality"] = {"value": _clip(quality), "weight": 0.35}

    cpcv = _safe_float(row.get("cpcv_median_sharpe"))
    if cpcv is not None:
        components["cpcv"] = {"value": _clip(cpcv / 4.0), "weight": 0.20}

    dsr = _safe_float(row.get("deflated_sharpe"))
    if dsr is not None:
        components["deflated_sharpe"] = {"value": _clip(dsr), "weight": 0.10}

    pbo = _safe_float(row.get("pbo"))
    if pbo is not None:
        components["pbo_inverse"] = {"value": 1.0 - _clip(pbo), "weight": 0.10}

    edge, edge_n = _realized_edge_fraction(row)
    if edge is not None and edge_n >= cfg.min_realized_trades:
        components["realized_edge"] = {
            "value": (_clip(edge / 0.01, -1.0, 1.0) + 1.0) / 2.0,
            "weight": 0.15,
        }

    payoff = _safe_float(row.get("payoff_ratio"))
    payoff_n = _safe_int(row.get("payoff_ratio_n")) or 0
    if payoff is not None and payoff_n >= cfg.min_realized_trades:
        components["payoff_ratio"] = {"value": _clip(payoff / 3.0), "weight": 0.10}

    total_weight = sum(c["weight"] for c in components.values())
    if total_weight <= 0:
        return {"score": None, "components": components, "penalties": []}

    score = sum(c["value"] * c["weight"] for c in components.values()) / total_weight
    penalties: list[str] = []
    if quality is None:
        score *= 0.94
        penalties.append("missing_quality_score")
    if edge_n < cfg.min_realized_trades:
        score *= 0.92
        penalties.append("thin_realized_ev")
    if row.get("promotion_gate_passed") is not True:
        score *= 0.50
        penalties.append("promotion_gate_not_passed")

    return {
        "score": _clip(score),
        "components": components,
        "penalties": penalties,
    }


def portfolio_gate_score(
    row: Mapping[str, Any],
    *,
    broker_risk_by_sleeve: Mapping[str, int],
    shadow_by_sleeve: Mapping[str, int],
    config: AlphaPortfolioConfig | None = None,
) -> dict[str, Any]:
    cfg = config or AlphaPortfolioConfig()
    sleeve = str(row["alpha_sleeve"])
    base = candidate_base_score(row, config=cfg)
    base_score = base["score"]
    if base_score is None:
        return {**base, "portfolio_score": None, "diversity": {}}

    risk_n = int(broker_risk_by_sleeve.get(sleeve, 0) or 0)
    shadow_n = int(shadow_by_sleeve.get(sleeve, 0) or 0)
    crowding_penalty = 1.0 / (1.0 + 0.18 * risk_n + 0.08 * shadow_n)
    diversity_bonus = 0.08 if risk_n == 0 else 0.0
    portfolio_score = _clip(float(base_score) * crowding_penalty + diversity_bonus)
    return {
        **base,
        "portfolio_score": portfolio_score,
        "diversity": {
            "broker_risk_in_sleeve": risk_n,
            "shadow_in_sleeve": shadow_n,
            "crowding_penalty": crowding_penalty,
            "diversity_bonus": diversity_bonus,
        },
    }


def _candidate_floor_blocks(row: Mapping[str, Any], cfg: AlphaPortfolioConfig) -> list[str]:
    reasons: list[str] = []
    if row.get("promotion_gate_passed") is not True:
        reasons.append("promotion_gate_not_passed")
    for key in ("cpcv_median_sharpe", "deflated_sharpe", "pbo"):
        if _safe_float(row.get(key)) is None:
            reasons.append(f"missing_{key}")

    realized_avg, realized_n = _realized_edge_fraction(row)
    if realized_n >= cfg.min_realized_trades and realized_avg is not None and realized_avg <= 0.0:
        reasons.append("negative_realized_floor")
    oos_at = row.get("oos_evaluated_at")
    if oos_at is not None:
        oos_n = _safe_int(row.get("oos_trade_count")) or 0
        oos_avg = _safe_float(row.get("oos_avg_return_pct"))
        if oos_n < cfg.min_oos_trades:
            reasons.append("thin_oos_floor")
        elif oos_avg is not None and oos_avg < cfg.min_oos_avg_return_pct:
            reasons.append("negative_oos_floor")
    return reasons


def _load_pattern_rows(
    db: Session,
    *,
    pattern_id: int | None = None,
    realized_window_days: int = 90,
) -> list[dict[str, Any]]:
    rows = db.execute(
        text(f"""
            WITH realized AS (
                SELECT scan_pattern_id,
                       COUNT(*) AS n_trades,
                       AVG({trade_return_fraction_sql()}) AS avg_pnl_pct,
                       SUM(pnl) AS total_pnl
                FROM trading_trades
                WHERE scan_pattern_id IS NOT NULL
                  AND scan_pattern_id != -1
                  AND status = 'closed'
                  AND pnl IS NOT NULL
                  AND entry_price > 0
                  AND quantity > 0
                  AND exit_date > NOW() - make_interval(days => :window_days)
                GROUP BY scan_pattern_id
            )
            SELECT
                sp.id,
                sp.name,
                sp.description,
                sp.rules_json,
                sp.origin,
                sp.asset_class,
                sp.timeframe,
                sp.active,
                sp.lifecycle_stage,
                sp.promotion_status,
                sp.hypothesis_family,
                sp.oos_evaluated_at,
                sp.oos_trade_count,
                sp.oos_win_rate,
                sp.oos_avg_return_pct,
                sp.cpcv_n_paths,
                sp.cpcv_median_sharpe,
                sp.deflated_sharpe,
                sp.pbo,
                sp.promotion_gate_passed,
                sp.quality_composite_score,
                sp.raw_realized_trade_count,
                sp.raw_realized_win_rate,
                sp.raw_realized_avg_return_pct,
                sp.payoff_ratio,
                sp.payoff_ratio_n,
                sp.updated_at,
                COALESCE(r.n_trades, 0) AS realized_n_trades,
                r.avg_pnl_pct AS realized_avg_pnl_pct,
                COALESCE(r.total_pnl, 0) AS realized_total_pnl,
                q.rolling_sample_n,
                q.rolling_directional_wr
            FROM scan_patterns sp
            LEFT JOIN realized r ON r.scan_pattern_id = sp.id
            LEFT JOIN pattern_directional_quality_v q ON q.scan_pattern_id = sp.id
            WHERE sp.active IS TRUE
              AND (:pattern_id IS NULL OR sp.id = :pattern_id)
            ORDER BY sp.id ASC
            """
        ),
        {
            "pattern_id": pattern_id,
            "window_days": int(realized_window_days),
        },
    ).mappings().all()
    return [dict(r) for r in rows]


def execution_health_summary(
    db: Session,
    *,
    config: AlphaPortfolioConfig,
) -> dict[str, Any]:
    try:
        from .execution_quality import compute_execution_stats

        stats = compute_execution_stats(
            db, user_id=None, lookback_days=config.execution_lookback_days,
        )
    except Exception as exc:
        logger.warning("[alpha_portfolio_gate] execution health failed: %s", exc)
        return {
            "clean": False,
            "reasons": ["execution_health_query_failed"],
            "error": type(exc).__name__,
        }

    measurable = int(stats.get("measurable", 0) or 0)
    p90 = _safe_float(stats.get("p90_slippage_pct"))
    reasons: list[str] = []
    if measurable < config.execution_min_samples:
        reasons.append("insufficient_execution_quality_samples")
    if p90 is not None and p90 > config.execution_max_p90_slippage_pct:
        reasons.append("p90_slippage_above_limit")
    return {
        "clean": not reasons,
        "reasons": reasons,
        "lookback_days": config.execution_lookback_days,
        "min_samples": config.execution_min_samples,
        "max_p90_slippage_pct": config.execution_max_p90_slippage_pct,
        "stats": stats,
    }


def _pattern_update_payload(row: Mapping[str, Any], score_info: Mapping[str, Any]) -> dict[str, Any]:
    recert_reasons = row.get("recert_reasons") or []
    payload = {
        "alpha_sleeve": row["alpha_sleeve"],
        "portfolio_score": score_info.get("portfolio_score"),
        "base_score": score_info.get("score"),
        "components": score_info.get("components", {}),
        "penalties": score_info.get("penalties", []),
        "diversity": score_info.get("diversity", {}),
        "recert_reasons": recert_reasons,
        "lifecycle_stage": row.get("lifecycle_stage"),
        "promotion_status": row.get("promotion_status"),
    }
    return payload


def scan_alpha_portfolio(
    db: Session,
    *,
    settings_: Any = None,
    now: datetime | None = None,
    limit: int = 50,
    pattern_id: int | None = None,
) -> dict[str, Any]:
    """Read the alpha portfolio state and compute recommended actions."""
    if settings_ is None:
        from ...config import settings as _settings

        settings_ = _settings

    cfg = config_from_settings(settings_)
    now = now or datetime.utcnow()
    run_id = f"alpha_portfolio_gate_{now.strftime('%Y%m%dT%H%M%S')}"
    rows = _load_pattern_rows(
        db,
        pattern_id=pattern_id,
        realized_window_days=int(getattr(
            settings_, "chili_cohort_score_realized_window_days", 90,
        )),
    )

    lifecycle_counts = Counter(str(r.get("lifecycle_stage") or "unknown") for r in rows)
    broker_risk_rows: list[dict[str, Any]] = []
    shadow_rows: list[dict[str, Any]] = []
    exact_promoted_status_rows: list[dict[str, Any]] = []

    for row in rows:
        row["alpha_sleeve"] = infer_alpha_sleeve(row)
        lifecycle = str(row.get("lifecycle_stage") or "").strip().lower()
        promotion_status = str(row.get("promotion_status") or "").strip().lower()
        if lifecycle in RISK_LIFECYCLES:
            broker_risk_rows.append(row)
        if lifecycle == SHADOW_LIFECYCLE:
            shadow_rows.append(row)
        if promotion_status == "promoted":
            exact_promoted_status_rows.append(row)

    broker_risk_by_sleeve = Counter(r["alpha_sleeve"] for r in broker_risk_rows)
    shadow_by_sleeve = Counter(r["alpha_sleeve"] for r in shadow_rows)

    recert_required: list[dict[str, Any]] = []
    pattern_updates: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    blocked_candidate_rows: list[dict[str, Any]] = []

    for row in rows:
        recert_reasons = recert_reasons_for_pattern(row, now=now, config=cfg)
        row["recert_reasons"] = recert_reasons
        score_info = portfolio_gate_score(
            row,
            broker_risk_by_sleeve=broker_risk_by_sleeve,
            shadow_by_sleeve=shadow_by_sleeve,
            config=cfg,
        )
        row["portfolio_score_info"] = score_info
        score = score_info.get("portfolio_score")
        update_payload = _pattern_update_payload(row, score_info)
        pattern_updates.append({
            "scan_pattern_id": int(row["id"]),
            "alpha_sleeve": row["alpha_sleeve"],
            "portfolio_gate_score": score,
            "portfolio_gate_json": update_payload,
            "recert_required": bool(recert_reasons),
            "recert_reason": ",".join(recert_reasons) if recert_reasons else None,
        })
        if recert_reasons:
            recert_required.append({
                "scan_pattern_id": int(row["id"]),
                "name": row.get("name"),
                "alpha_sleeve": row["alpha_sleeve"],
                "lifecycle_stage": row.get("lifecycle_stage"),
                "promotion_status": row.get("promotion_status"),
                "reasons": recert_reasons,
                "portfolio_gate_score": score,
            })

        lifecycle = str(row.get("lifecycle_stage") or "").strip().lower()
        if lifecycle in CANDIDATE_LIFECYCLES:
            floor_blocks = _candidate_floor_blocks(row, cfg)
            candidate = {
                "scan_pattern_id": int(row["id"]),
                "name": row.get("name"),
                "alpha_sleeve": row["alpha_sleeve"],
                "lifecycle_stage": row.get("lifecycle_stage"),
                "promotion_status": row.get("promotion_status"),
                "quality_composite_score": row.get("quality_composite_score"),
                "cpcv_median_sharpe": row.get("cpcv_median_sharpe"),
                "deflated_sharpe": row.get("deflated_sharpe"),
                "pbo": row.get("pbo"),
                "raw_realized_trade_count": row.get("raw_realized_trade_count"),
                "raw_realized_avg_return_pct": row.get("raw_realized_avg_return_pct"),
                "payoff_ratio": row.get("payoff_ratio"),
                "payoff_ratio_n": row.get("payoff_ratio_n"),
                "portfolio_gate_score": score,
                "base_score": score_info.get("score"),
                "components": score_info.get("components", {}),
                "penalties": score_info.get("penalties", []),
                "diversity": score_info.get("diversity", {}),
                "floor_blocks": floor_blocks,
            }
            if floor_blocks:
                blocked_candidate_rows.append(candidate)
            elif score is not None:
                candidate_rows.append(candidate)

    candidate_rows.sort(
        key=lambda c: (
            c["portfolio_gate_score"] if c["portfolio_gate_score"] is not None else -1.0,
            c["cpcv_median_sharpe"] if c["cpcv_median_sharpe"] is not None else -1.0,
            -int(c["scan_pattern_id"]),
        ),
        reverse=True,
    )

    selected_shadow: list[dict[str, Any]] = []
    selected_by_sleeve: dict[str, int] = defaultdict(int)
    for cand in candidate_rows:
        if len(selected_shadow) >= cfg.max_shadow_total:
            break
        score = cand.get("portfolio_gate_score")
        if score is None or float(score) < cfg.min_shadow_score:
            continue
        sleeve = str(cand["alpha_sleeve"])
        if selected_by_sleeve[sleeve] >= cfg.max_shadow_per_sleeve:
            continue
        selected_by_sleeve[sleeve] += 1
        selected_shadow.append({
            **cand,
            "decision": "shadow_candidate",
            "decision_reason": "portfolio_score_and_sleeve_cap_passed",
        })

    execution = execution_health_summary(db, config=cfg)
    risk_sleeves = sorted(k for k, v in broker_risk_by_sleeve.items() if v > 0)
    diversification_reasons: list[str] = []
    if len(risk_sleeves) < cfg.min_risk_sleeves:
        diversification_reasons.append("broker_risk_sleeves_below_target")

    full_blocks = []
    if recert_required:
        full_blocks.append("recert_required")
    if not execution.get("clean"):
        full_blocks.extend(execution.get("reasons") or ["execution_health_not_clean"])

    priority_recert_patterns: list[dict[str, Any]] = []
    for priority_pattern_id in _priority_recert_pattern_ids(settings_):
        priority_row = next(
            (
                r for r in recert_required
                if int(r["scan_pattern_id"]) == int(priority_pattern_id)
            ),
            None,
        )
        if priority_row is None:
            maybe_priority = next(
                (r for r in rows if int(r.get("id") or 0) == int(priority_pattern_id)),
                None,
            )
            if maybe_priority is not None:
                priority_row = {
                    "scan_pattern_id": int(priority_pattern_id),
                    "name": maybe_priority.get("name"),
                    "alpha_sleeve": maybe_priority.get("alpha_sleeve"),
                    "lifecycle_stage": maybe_priority.get("lifecycle_stage"),
                    "promotion_status": maybe_priority.get("promotion_status"),
                    "reasons": maybe_priority.get("recert_reasons") or [],
                    "portfolio_gate_score": (
                        maybe_priority.get("portfolio_score_info") or {}
                    ).get("portfolio_score"),
                }
        if priority_row is not None:
            priority_recert_patterns.append(priority_row)
    primary_priority_recert_pattern = (
        priority_recert_patterns[0] if priority_recert_patterns else None
    )

    return {
        "ok": True,
        "run_id": run_id,
        "generated_at": now,
        "config": cfg.__dict__,
        "pattern_id_filter": pattern_id,
        "active_pattern_count": len(rows),
        "lifecycle_counts": dict(lifecycle_counts),
        "exact_promoted_status_count": len(exact_promoted_status_rows),
        "broker_risk_count": len(broker_risk_rows),
        "full_risk_count": sum(
            1 for r in broker_risk_rows
            if str(r.get("lifecycle_stage") or "").strip().lower() in FULL_RISK_LIFECYCLES
        ),
        "broker_risk_by_sleeve": dict(sorted(broker_risk_by_sleeve.items())),
        "shadow_by_sleeve": dict(sorted(shadow_by_sleeve.items())),
        "risk_sleeves": risk_sleeves,
        "portfolio_diversified": not diversification_reasons,
        "diversification_reasons": diversification_reasons,
        "recert_required_count": len(recert_required),
        "recert_required_patterns": recert_required,
        "priority_recert_patterns": priority_recert_patterns,
        "pattern_585": primary_priority_recert_pattern,
        "execution_health": execution,
        "full_promotion_blocked": bool(full_blocks),
        "full_promotion_block_reasons": sorted(set(full_blocks)),
        "candidate_count": len(candidate_rows),
        "candidates": candidate_rows[: max(0, int(limit))],
        "blocked_candidates": blocked_candidate_rows[: max(0, int(limit))],
        "shadow_candidates": selected_shadow,
        "pattern_updates": pattern_updates,
    }


def persist_alpha_portfolio_snapshot(
    db: Session,
    snapshot: Mapping[str, Any],
    *,
    execute: bool = False,
) -> dict[str, Any]:
    updates = list(snapshot.get("pattern_updates") or [])
    decisions: list[dict[str, Any]] = []

    for recert in snapshot.get("recert_required_patterns") or []:
        decisions.append({
            "scan_pattern_id": recert["scan_pattern_id"],
            "alpha_sleeve": recert.get("alpha_sleeve"),
            "lifecycle_stage": recert.get("lifecycle_stage"),
            "portfolio_gate_score": recert.get("portfolio_gate_score"),
            "decision": "recert_required",
            "reasons_json": {"reasons": recert.get("reasons") or []},
        })
    for cand in snapshot.get("shadow_candidates") or []:
        decisions.append({
            "scan_pattern_id": cand["scan_pattern_id"],
            "alpha_sleeve": cand.get("alpha_sleeve"),
            "lifecycle_stage": cand.get("lifecycle_stage"),
            "portfolio_gate_score": cand.get("portfolio_gate_score"),
            "decision": "shadow_candidate",
            "reasons_json": {
                "decision_reason": cand.get("decision_reason"),
                "components": cand.get("components", {}),
                "diversity": cand.get("diversity", {}),
            },
        })

    if not execute:
        return {
            "ok": True,
            "dry_run": True,
            "updates_planned": len(updates),
            "audit_rows_planned": len(decisions),
        }

    now = datetime.utcnow()
    for upd in updates:
        db.execute(
            text(
                """
                UPDATE scan_patterns
                SET alpha_sleeve = :alpha_sleeve,
                    portfolio_gate_score = :portfolio_gate_score,
                    portfolio_gate_json = CAST(:portfolio_gate_json AS JSONB),
                    portfolio_gate_updated_at = :now,
                    recert_required = :recert_required,
                    recert_reason = :recert_reason
                WHERE id = :scan_pattern_id
                """
            ),
            {
                "scan_pattern_id": int(upd["scan_pattern_id"]),
                "alpha_sleeve": upd["alpha_sleeve"],
                "portfolio_gate_score": upd["portfolio_gate_score"],
                "portfolio_gate_json": json.dumps(
                    upd["portfolio_gate_json"], default=str, separators=(",", ":"),
                ),
                "now": now,
                "recert_required": bool(upd["recert_required"]),
                "recert_reason": upd["recert_reason"],
            },
        )

    run_id = str(snapshot.get("run_id") or f"alpha_portfolio_gate_{now:%Y%m%dT%H%M%S}")
    for decision in decisions:
        db.execute(
            text(
                """
                INSERT INTO trading_alpha_portfolio_gate_audit (
                    run_id, scan_pattern_id, alpha_sleeve, lifecycle_stage,
                    portfolio_gate_score, decision, reasons_json, created_at
                ) VALUES (
                    :run_id, :scan_pattern_id, :alpha_sleeve, :lifecycle_stage,
                    :portfolio_gate_score, :decision,
                    CAST(:reasons_json AS JSONB), :created_at
                )
                """
            ),
            {
                "run_id": run_id,
                "scan_pattern_id": decision.get("scan_pattern_id"),
                "alpha_sleeve": decision.get("alpha_sleeve"),
                "lifecycle_stage": decision.get("lifecycle_stage"),
                "portfolio_gate_score": decision.get("portfolio_gate_score"),
                "decision": decision["decision"],
                "reasons_json": json.dumps(
                    decision.get("reasons_json") or {},
                    default=str,
                    separators=(",", ":"),
                ),
                "created_at": now,
            },
        )

    db.commit()
    return {
        "ok": True,
        "dry_run": False,
        "updates_written": len(updates),
        "audit_rows_written": len(decisions),
    }


def queue_recert_for_required(
    db: Session,
    snapshot: Mapping[str, Any],
    *,
    execute: bool = False,
    mode_override: str | None = None,
) -> dict[str, Any]:
    recerts = list(snapshot.get("recert_required_patterns") or [])
    actionable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for recert in recerts:
        reasons = [str(x) for x in (recert.get("reasons") or [])]
        if set(reasons).intersection(BACKTEST_SOLVABLE_RECERT_REASONS):
            actionable.append(recert)
        else:
            skipped.append({
                "scan_pattern_id": int(recert["scan_pattern_id"]),
                "reasons": reasons,
                "skip_reason": "no_backtest_solvable_recert_reason",
            })
    if not execute:
        return {
            "ok": True,
            "dry_run": True,
            "recert_planned": [int(r["scan_pattern_id"]) for r in actionable],
            "recert_skipped": skipped,
        }

    from .recert_queue_service import queue_scheduler

    queued: list[dict[str, Any]] = []
    for recert in actionable:
        reasons = [str(x) for x in (recert.get("reasons") or [])]
        reason = (
            "alpha_portfolio_gate:"
            + ",".join(reasons)
        )[:256]
        result = queue_scheduler(
            db,
            scan_pattern_id=int(recert["scan_pattern_id"]),
            pattern_name=recert.get("name"),
            as_of_date=date.today(),
            reason=reason,
            severity="red",
            payload={
                "origin": "alpha_portfolio_gate",
                "alpha_sleeve": recert.get("alpha_sleeve"),
                "lifecycle_stage": recert.get("lifecycle_stage"),
                "promotion_status": recert.get("promotion_status"),
                "recert_reasons": reasons,
            },
            mode_override=mode_override,
        )
        if result is not None:
            queued.append({
                "scan_pattern_id": result.scan_pattern_id,
                "recert_id": result.recert_id,
                "log_id": result.log_id,
                "status": result.status,
                "mode": result.mode,
            })
    return {
        "ok": True,
        "dry_run": False,
        "queued": queued,
        "skipped": skipped,
    }


def stage_shadow_candidates(
    db: Session,
    snapshot: Mapping[str, Any],
    *,
    execute: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    candidates = list(snapshot.get("shadow_candidates") or [])
    planned_ids = [int(c["scan_pattern_id"]) for c in candidates]
    if not execute:
        return {"ok": True, "dry_run": True, "shadow_planned": planned_ids}

    now = now or datetime.utcnow()
    staged: list[int] = []
    for cand in candidates:
        pid = int(cand["scan_pattern_id"])
        pattern = db.get(ScanPattern, pid)
        if pattern is None:
            continue
        lifecycle = str(pattern.lifecycle_stage or "").strip().lower()
        if lifecycle not in CANDIDATE_LIFECYCLES:
            continue
        old_lifecycle = pattern.lifecycle_stage
        old_status = pattern.promotion_status
        pattern.lifecycle_stage = SHADOW_LIFECYCLE
        pattern.promotion_status = "shadow_collecting_ev"
        pattern.lifecycle_changed_at = now
        pattern.active = True
        staged.append(pid)
        try:
            from .brain_work.promotion_surface import emit_promotion_surface_change

            emit_promotion_surface_change(
                db,
                scan_pattern_id=pid,
                old_promotion_status=old_status,
                old_lifecycle_stage=old_lifecycle,
                new_promotion_status=pattern.promotion_status,
                new_lifecycle_stage=pattern.lifecycle_stage,
                source="alpha_portfolio_gate_shadow",
                extra={
                    "alpha_sleeve": cand.get("alpha_sleeve"),
                    "portfolio_gate_score": cand.get("portfolio_gate_score"),
                },
            )
        except Exception:
            logger.debug(
                "[alpha_portfolio_gate] promotion_surface emit failed",
                exc_info=True,
            )

    db.commit()
    return {"ok": True, "dry_run": False, "shadow_staged": staged}


def run_alpha_portfolio_maintenance(
    db: Session,
    *,
    settings_: Any = None,
    now: datetime | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    """Persist alpha-gate state and automatically feed certification work.

    This is the scheduled "make it part of the system" loop: scan the book,
    write current recert flags/gate audit, queue required recerts as scheduler
    work, and stage portfolio-approved candidates into broker-blocked shadow.
    """
    if settings_ is None:
        from ...config import settings as _settings

        settings_ = _settings
    if not bool(getattr(settings_, "chili_alpha_portfolio_gate_enabled", False)):
        return {"ok": True, "skipped": "alpha_portfolio_gate_disabled"}
    if not bool(getattr(settings_, "chili_alpha_portfolio_maintenance_enabled", True)):
        return {"ok": True, "skipped": "maintenance_disabled"}

    now = now or datetime.utcnow()
    realized_sync_result: dict[str, Any] = {
        "ok": True,
        "skipped": "realized_sync_disabled",
    }
    if (
        execute
        and bool(getattr(settings_, "chili_alpha_portfolio_sync_realized_on_maintenance", True))
    ):
        try:
            from .realized_stats_sync import sync_realized_stats

            realized_sync_result = {"ok": True, **sync_realized_stats(db, dry_run=False)}
        except Exception as exc:
            db.rollback()
            realized_sync_result = {
                "ok": False,
                "error": f"realized_sync_failed:{type(exc).__name__}",
            }
            logger.warning(
                "[alpha_portfolio_gate] maintenance realized sync failed: %s",
                exc,
                exc_info=True,
            )
    elif not execute:
        realized_sync_result = {
            "ok": True,
            "skipped": "dry_run_write_free",
        }

    quality_result: dict[str, Any] = {
        "ok": True,
        "skipped": "quality_refresh_disabled",
    }
    if (
        execute
        and bool(getattr(settings_, "chili_alpha_portfolio_refresh_quality_on_maintenance", True))
    ):
        try:
            from .pattern_quality_score import compute_and_persist_scores

            quality_result = compute_and_persist_scores(db, settings_=settings_)
        except Exception as exc:
            db.rollback()
            quality_result = {
                "ok": False,
                "error": f"quality_refresh_failed:{type(exc).__name__}",
            }
            logger.warning(
                "[alpha_portfolio_gate] maintenance quality refresh failed: %s",
                exc,
                exc_info=True,
            )
    elif not execute:
        quality_result = {
            "ok": True,
            "skipped": "dry_run_write_free",
        }

    snapshot = scan_alpha_portfolio(db, settings_=settings_, now=now)
    persist_result = persist_alpha_portfolio_snapshot(
        db, snapshot, execute=execute,
    )

    queue_result: dict[str, Any] = {
        "ok": True,
        "skipped": "auto_queue_recert_disabled",
    }
    if bool(getattr(settings_, "chili_alpha_portfolio_auto_queue_recert_enabled", True)):
        queue_result = queue_recert_for_required(
            db,
            snapshot,
            execute=execute,
            mode_override=getattr(settings_, "brain_recert_queue_mode", None),
        )

    stage_result: dict[str, Any] = {
        "ok": True,
        "skipped": "auto_stage_shadow_disabled",
    }
    if bool(getattr(settings_, "chili_alpha_portfolio_auto_stage_shadow_enabled", True)):
        stage_result = stage_shadow_candidates(
            db,
            snapshot,
            execute=execute,
            now=now,
        )

    return {
        "ok": True,
        "dry_run": not execute,
        "run_id": snapshot.get("run_id"),
        "generated_at": snapshot.get("generated_at"),
        "active_pattern_count": snapshot.get("active_pattern_count"),
        "recert_required_count": snapshot.get("recert_required_count"),
        "shadow_candidate_count": len(snapshot.get("shadow_candidates") or []),
        "full_promotion_blocked": snapshot.get("full_promotion_blocked"),
        "full_promotion_block_reasons": snapshot.get("full_promotion_block_reasons") or [],
        "realized_sync": realized_sync_result,
        "quality_refresh": quality_result,
        "persist": persist_result,
        "recert_queue": queue_result,
        "shadow_stage": stage_result,
    }


def broker_risk_allowed(
    db: Session,
    *,
    settings_: Any = None,
) -> tuple[bool, dict[str, Any]]:
    """Return whether full/pilot broker-risk promotion may proceed."""
    if settings_ is None:
        from ...config import settings as _settings

        settings_ = _settings
    if not bool(getattr(settings_, "chili_alpha_portfolio_gate_enabled", False)):
        return True, {"ok": True, "skipped": "flag_disabled"}
    snapshot = scan_alpha_portfolio(db, settings_=settings_, limit=10)
    return not bool(snapshot.get("full_promotion_blocked")), snapshot
