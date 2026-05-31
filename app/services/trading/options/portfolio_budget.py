"""Portfolio greeks budget enforcement.

Before any options trade is accepted, the proposed trade's net greeks
are summed with the operator's existing options portfolio greeks, and
the result is checked against ``options_greeks_budget``. If any limit
is breached, the trade is rejected and the reason is returned to the
caller.

This is a HARD rule — bypassing it requires ``CHILI_OPTIONS_BUDGET_BYPASS=true``,
which should ONLY be flipped during operator-supervised testing, never
in normal operation.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .contracts import (
    complete_greeks,
    finite_option_greek,
    finite_greek,
    missing_greeks,
    parse_contract_quantity,
)
from .strategies import StrategyProposal

logger = logging.getLogger(__name__)
GREEK_KEYS = ("delta", "gamma", "theta", "vega")
OPTION_ASSET_ALIASES_SQL = (
    "'option', 'options', 'option_contract', 'option_contracts', "
    "'options_contract', 'options_contracts', 'contract_option', "
    "'contract_options', 'equity_option', 'equity_options', "
    "'stock_option', 'stock_options', 'option_spread', "
    "'options_spread', 'option_spreads', 'options_spreads', "
    "'optionspread', 'optionspreads', 'robinhood_option', "
    "'robinhood_options'"
)


def _option_asset_marker_sql(expr: str) -> str:
    return (
        f"REPLACE(LOWER(COALESCE({expr}, '')), '-', '_') "
        f"IN ({OPTION_ASSET_ALIASES_SQL})"
    )


@dataclass
class BudgetCheckResult:
    accepted: bool
    reasons: list[str]
    current_portfolio: dict
    after_proposal: dict
    budget: dict


def _get_budget(db: Session, user_id: Optional[int]) -> dict:
    """Return the user's greeks budget, or sane defaults if no row exists."""
    try:
        row = db.execute(
            text(
                """
                SELECT max_abs_delta, max_abs_gamma, max_vega_per_tenor,
                       max_total_vega, max_theta_burn_per_day
                FROM options_greeks_budget
                WHERE user_id = :uid
                """
            ),
            {"uid": user_id},
        ).fetchone()
        if row:
            return _sanitize_budget({
                "max_abs_delta": row[0],
                "max_abs_gamma": row[1],
                "max_vega_per_tenor": row[2] or {},
                "max_total_vega": row[3],
                "max_theta_burn_per_day": row[4],
            })
    except Exception as e:
        logger.debug("[options.budget] _get_budget failed: %s", e)
    return _default_budget()


def _default_budget() -> dict:
    """Conservative fallback budget used when the persisted budget is absent."""
    return {
        "max_abs_delta": 0.50,
        "max_abs_gamma": 0.05,
        "max_vega_per_tenor": {"30d": 100, "60d": 80, "90d": 60},
        "max_total_vega": 200.0,
        "max_theta_burn_per_day": 50.0,
    }


def options_budget_bypass_enabled() -> bool:
    return os.environ.get("CHILI_OPTIONS_BUDGET_BYPASS", "").lower() in (
        "true",
        "1",
    )


def _zero_greeks() -> dict:
    return {
        "net_delta": 0.0,
        "net_gamma": 0.0,
        "net_theta": 0.0,
        "net_vega": 0.0,
        "vega_by_tenor": {},
        "missing_greeks_count": 0,
    }


def _unproven_greeks() -> dict:
    out = _zero_greeks()
    out["missing_greeks_count"] = 1
    return out


def _add_greek_totals(left: dict, right: dict) -> dict:
    out = {
        "net_delta": _finite_number_or_zero(left.get("net_delta"))
        + _finite_number_or_zero(right.get("net_delta")),
        "net_gamma": _finite_number_or_zero(left.get("net_gamma"))
        + _finite_number_or_zero(right.get("net_gamma")),
        "net_theta": _finite_number_or_zero(left.get("net_theta"))
        + _finite_number_or_zero(right.get("net_theta")),
        "net_vega": _finite_number_or_zero(left.get("net_vega"))
        + _finite_number_or_zero(right.get("net_vega")),
    }
    out["missing_greeks_count"] = _nonnegative_int_or_zero(
        left.get("missing_greeks_count")
    ) + _nonnegative_int_or_zero(
        right.get("missing_greeks_count")
    )
    out["vega_by_tenor"] = _add_vega_by_tenor(
        left.get("vega_by_tenor"),
        right.get("vega_by_tenor"),
    )
    return out


def _add_vega_by_tenor(left: Any, right: Any) -> dict[str, float]:
    totals: dict[str, float] = {}
    for source in (left, right):
        if not isinstance(source, dict):
            continue
        for tenor, raw_value in source.items():
            value = _finite_number_or_none(raw_value)
            if value is None:
                continue
            key = str(tenor or "").strip().lower()
            if not key:
                continue
            totals[key] = totals.get(key, 0.0) + value
    return totals


def _budget_tenor_label_set(budget: dict[str, Any]) -> set[str]:
    return {tenor for tenor, _days, _limit in _budget_tenor_limits(budget)}


def _budget_known_vega_by_tenor(source: Any, budget: dict[str, Any]) -> dict[str, float]:
    totals = _add_vega_by_tenor(source, {})
    labels = _budget_tenor_label_set(budget)
    if not labels:
        return totals
    return {tenor: value for tenor, value in totals.items() if tenor in labels}


def _finite_number_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _finite_number_or_zero(value: Any) -> float:
    out = _finite_number_or_none(value)
    return out if out is not None else 0.0


def _nonnegative_int_or_zero(value: Any) -> int:
    out = _finite_number_or_none(value)
    if out is None or out < 0:
        return 0
    return int(out)


def _signed_contract_quantity_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    out = _finite_number_or_none(value)
    if out is None or out == 0.0 or not float(out).is_integer():
        return None
    return int(out)


def _quantity_value_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _open_trade_contract_quantity_or_none(
    qty_raw: Any,
    meta: dict[str, Any],
) -> int | None:
    if not _quantity_value_missing(qty_raw):
        return parse_contract_quantity(qty_raw)
    return parse_contract_quantity(meta.get("quantity"))


def _positive_limit_or_default(value: Any, default: float) -> float:
    out = _finite_number_or_none(value)
    return out if out is not None and out > 0.0 else default


def _tenor_days(label: Any) -> int | None:
    raw = str(label or "").strip().lower()
    if not raw.endswith("d"):
        return None
    number = _finite_number_or_none(raw[:-1])
    if number is None or number <= 0 or not float(number).is_integer():
        return None
    return int(number)


def _sanitize_vega_per_tenor(value: Any, default: dict) -> dict:
    out = dict(default)
    if not isinstance(value, dict):
        return out
    for tenor, raw_limit in value.items():
        if _tenor_days(tenor) is None:
            continue
        limit = _finite_number_or_none(raw_limit)
        if limit is not None and limit > 0:
            out[str(tenor).strip().lower()] = limit
    return out


def _budget_tenor_limits(budget: dict[str, Any]) -> list[tuple[str, int, float]]:
    raw = budget.get("max_vega_per_tenor")
    if not isinstance(raw, dict):
        return []
    limits: list[tuple[str, int, float]] = []
    for tenor, raw_limit in raw.items():
        days = _tenor_days(tenor)
        limit = _finite_number_or_none(raw_limit)
        if days is not None and limit is not None and limit > 0.0:
            limits.append((str(tenor).strip().lower(), days, limit))
    return sorted(limits, key=lambda item: item[1])


def _expiration_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _tenor_bucket_for_expiration(
    expiration: Any,
    budget: dict[str, Any],
    *,
    today: date | None = None,
) -> str | None:
    exp = _expiration_date(expiration)
    limits = _budget_tenor_limits(budget)
    if exp is None or not limits:
        return None
    today = today or datetime.now(timezone.utc).date()
    dte = max(0, (exp - today).days)
    for tenor, days, _limit in limits:
        if dte <= days:
            return tenor
    return limits[-1][0]


def _add_vega_to_tenor(
    target: dict[str, float],
    *,
    expiration: Any,
    vega: Any,
    quantity: Any = 1,
    budget: dict[str, Any],
) -> bool:
    vega_f = _finite_number_or_none(vega)
    qty = _finite_number_or_none(quantity)
    if vega_f is None or qty is None or vega_f == 0.0 or qty == 0.0:
        return True
    if not _budget_tenor_limits(budget):
        return True
    bucket = _tenor_bucket_for_expiration(expiration, budget)
    if bucket is None:
        return False
    target[bucket] = target.get(bucket, 0.0) + vega_f * qty
    return True


def _sanitize_budget(raw: dict[str, Any]) -> dict:
    default = _default_budget()
    theta_raw = raw.get("max_theta_burn_per_day")
    theta_limit = None
    if theta_raw is not None:
        theta_limit = _positive_limit_or_default(
            theta_raw,
            float(default["max_theta_burn_per_day"]),
        )
    return {
        "max_abs_delta": _positive_limit_or_default(
            raw.get("max_abs_delta"),
            float(default["max_abs_delta"]),
        ),
        "max_abs_gamma": _positive_limit_or_default(
            raw.get("max_abs_gamma"),
            float(default["max_abs_gamma"]),
        ),
        "max_vega_per_tenor": _sanitize_vega_per_tenor(
            raw.get("max_vega_per_tenor"),
            default["max_vega_per_tenor"],
        ),
        "max_total_vega": _positive_limit_or_default(
            raw.get("max_total_vega"),
            float(default["max_total_vega"]),
        ),
        "max_theta_burn_per_day": theta_limit,
    }



def _proposal_greek(proposal: StrategyProposal, name: str) -> float | None:
    return finite_greek(getattr(proposal, f"net_{name}", None))


def _proposal_missing_greeks(proposal: StrategyProposal) -> list[str]:
    missing: list[str] = []
    for name in ("delta", "gamma", "theta", "vega"):
        if _proposal_greek(proposal, name) is None:
            missing.append(name)
    return missing


def _meta_greek(meta: dict[str, Any], key: str) -> float:
    parsed = finite_option_greek(meta.get(key), key)
    if parsed is None and isinstance(meta.get("quote_snapshot"), dict):
        parsed = finite_option_greek(meta["quote_snapshot"].get(key), key)
    if parsed is None:
        raise ValueError(f"invalid_greek:{key}")
    return parsed


def _position_leg_greek(source: dict[str, Any], key: str) -> float | None:
    parsed = finite_option_greek(source.get(key), key)
    if parsed is None and isinstance(source.get("quote_snapshot"), dict):
        parsed = finite_option_greek(source["quote_snapshot"].get(key), key)
    return parsed


def _sum_open_position_greeks(
    db: Session,
    user_id: Optional[int],
    *,
    budget: dict[str, Any] | None = None,
) -> dict:
    """Aggregate net greeks across all open option positions.

    For phase-1 scaffolding, we trust the persisted leg greeks; a follow-up
    will recompute from current quotes. Since we haven't placed any options
    yet (Q2.T1 is shipping the foundation), this returns zeros in steady
    state but the code path is in place for when positions exist.
    """
    try:
        rows = db.execute(
            text(
                """
                SELECT legs_json
                FROM options_position
                WHERE (user_id = :uid OR :uid IS NULL)
                  AND closed_at IS NULL
                """
            ),
            {"uid": user_id},
        ).fetchall()
    except Exception as e:
        logger.debug("[options.budget] open positions fetch failed: %s", e)
        return _sum_open_trade_greeks(db, user_id, budget=budget)

    budget = budget or _default_budget()
    net_d = net_g = net_t = net_v = 0.0
    vega_by_tenor: dict[str, float] = {}
    missing_count = 0
    for r in rows or []:
        try:
            legs = r[0]
            if isinstance(legs, str):
                legs = json.loads(legs)
            if not isinstance(legs, list) or not legs:
                missing_count += 1
                continue
            for leg in legs or []:
                if not isinstance(leg, dict):
                    missing_count += 1
                    continue
                qty = _signed_contract_quantity_or_none(leg.get("qty"))
                if qty is None:
                    missing_count += 1
                    continue
                vals = {
                    "delta": _position_leg_greek(leg, "delta"),
                    "gamma": _position_leg_greek(leg, "gamma"),
                    "theta": _position_leg_greek(leg, "theta"),
                    "vega": _position_leg_greek(leg, "vega"),
                }
                if any(value is None for value in vals.values()):
                    missing_count += 1
                if vals["delta"] is not None:
                    net_d += vals["delta"] * qty
                if vals["gamma"] is not None:
                    net_g += vals["gamma"] * qty
                if vals["theta"] is not None:
                    net_t += vals["theta"] * qty
                if vals["vega"] is not None:
                    net_v += vals["vega"] * qty
                    if not _add_vega_to_tenor(
                        vega_by_tenor,
                        expiration=leg.get("expiration"),
                        vega=vals["vega"],
                        quantity=qty,
                        budget=budget,
                    ):
                        missing_count += 1
        except Exception:
            missing_count += 1
            continue
    position_totals = {
        "net_delta": net_d,
        "net_gamma": net_g,
        "net_theta": net_t,
        "net_vega": net_v,
        "vega_by_tenor": vega_by_tenor,
        "missing_greeks_count": missing_count,
    }
    return _add_greek_totals(
        position_totals,
        _sum_open_trade_greeks(db, user_id, budget=budget),
    )


def _extract_option_meta(snapshot: Any) -> dict[str, Any]:
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except Exception:
            snapshot = {}
    if not isinstance(snapshot, dict):
        return {}
    meta = snapshot.get("option_meta")
    if isinstance(meta, dict):
        return meta
    breakout = snapshot.get("breakout_alert")
    if isinstance(breakout, str):
        try:
            breakout = json.loads(breakout)
        except Exception:
            breakout = {}
    if isinstance(breakout, dict) and isinstance(breakout.get("option_meta"), dict):
        return breakout["option_meta"]
    return {}


def _sum_open_trade_greeks(
    db: Session,
    user_id: Optional[int],
    *,
    budget: dict[str, Any] | None = None,
) -> dict:
    """Fallback Greek aggregation from open Trade snapshots.

    The original budget path only read ``options_position``. Live AutoTrader
    entries currently create ``trading_trades`` first, so this fallback keeps
    the budget aware of option trades even before a separate option-position
    projection is populated.
    """
    try:
        rows = db.execute(
            text(
                f"""
                SELECT quantity, indicator_snapshot
                FROM trading_trades
                WHERE (user_id = :uid OR :uid IS NULL)
                  AND status IN ('open', 'working')
                  AND (
                    {_option_asset_marker_sql('asset_kind')}
                    OR indicator_snapshot::jsonb ? 'option_meta'
                    OR indicator_snapshot::jsonb ? 'options_path'
                    OR {_option_asset_marker_sql("indicator_snapshot::jsonb ->> 'asset_kind'")}
                    OR {_option_asset_marker_sql("indicator_snapshot::jsonb ->> 'asset_type'")}
                    OR {_option_asset_marker_sql("indicator_snapshot::jsonb ->> 'asset_class'")}
                    OR (indicator_snapshot::jsonb -> 'breakout_alert') ? 'option_meta'
                    OR {_option_asset_marker_sql("(indicator_snapshot::jsonb -> 'breakout_alert') ->> 'asset_kind'")}
                    OR {_option_asset_marker_sql("(indicator_snapshot::jsonb -> 'breakout_alert') ->> 'asset_type'")}
                    OR {_option_asset_marker_sql("(indicator_snapshot::jsonb -> 'breakout_alert') ->> 'asset_class'")}
                  )
                """
            ),
            {"uid": user_id},
        ).fetchall()
    except Exception as e:
        logger.debug("[options.budget] open trade greeks fetch failed: %s", e)
        return _unproven_greeks()

    budget = budget or _default_budget()
    net_d = net_g = net_t = net_v = 0.0
    vega_by_tenor: dict[str, float] = {}
    missing_count = 0
    for qty_raw, snapshot in rows or []:
        meta = _extract_option_meta(snapshot)
        if not meta:
            missing_count += 1
            continue
        qty = _open_trade_contract_quantity_or_none(qty_raw, meta)
        if qty is None:
            missing_count += 1
            continue
        if not complete_greeks(meta):
            missing_count += 1
        for key, acc in (
            ("delta", "net_d"),
            ("gamma", "net_g"),
            ("theta", "net_t"),
            ("vega", "net_v"),
        ):
            f = _position_leg_greek(meta, key)
            if f is None:
                continue
            if acc == "net_d":
                net_d += f * qty
            elif acc == "net_g":
                net_g += f * qty
            elif acc == "net_t":
                net_t += f * qty
            elif acc == "net_v":
                net_v += f * qty
                if not _add_vega_to_tenor(
                    vega_by_tenor,
                    expiration=meta.get("expiration"),
                    vega=f,
                    quantity=qty,
                    budget=budget,
                ):
                    missing_count += 1
    return {
        "net_delta": net_d,
        "net_gamma": net_g,
        "net_theta": net_t,
        "net_vega": net_v,
        "vega_by_tenor": vega_by_tenor,
        "missing_greeks_count": missing_count,
    }


def single_leg_proposal_from_option_meta(
    meta: dict[str, Any],
    *,
    confidence: float = 0.5,
) -> StrategyProposal:
    """Build a minimal StrategyProposal from normalized single-leg metadata."""
    from .contracts import normalize_expiration, normalize_option_meta
    from .strategies import Leg

    opt = normalize_option_meta(meta)
    exp_raw = normalize_expiration(opt.get("expiration"))
    if not exp_raw:
        raise ValueError("missing_expiration")
    exp = datetime.strptime(exp_raw, "%Y-%m-%d").date()
    qty = parse_contract_quantity(opt.get("quantity"))
    if qty is None:
        raise ValueError("invalid_quantity")
    missing = missing_greeks(opt)
    if missing:
        raise ValueError("missing_greeks:" + ",".join(missing))
    leg = Leg(
        occ_symbol=str(opt.get("occ_symbol") or opt.get("contract_key") or ""),
        underlying=str(opt.get("underlying") or ""),
        expiration=exp,
        strike=float(opt.get("strike") or 0.0),
        opt_type=str(opt.get("option_type") or ""),
        qty=qty,
        entry_price=float(opt.get("limit_price") or 0.0),
    )
    return StrategyProposal(
        underlying=leg.underlying,
        strategy_family="single_long_option",
        legs=[leg],
        net_debit=leg.entry_price * 100.0 * qty,
        net_credit=None,
        max_loss=leg.entry_price * 100.0 * qty,
        max_profit=None,
        breakevens=[],
        net_delta=_meta_greek(opt, "delta") * qty,
        net_gamma=_meta_greek(opt, "gamma") * qty,
        net_theta=_meta_greek(opt, "theta") * qty,
        net_vega=_meta_greek(opt, "vega") * qty,
        confidence=confidence,
        rationale="single-leg option entry from AutoTrader option_meta",
        meta=opt,
    )


def _proposal_vega_by_tenor(
    proposal: StrategyProposal,
    budget: dict[str, Any],
) -> dict[str, float]:
    raw = proposal.meta.get("vega_by_tenor") if isinstance(proposal.meta, dict) else None
    if isinstance(raw, dict):
        return _budget_known_vega_by_tenor(raw, budget)

    net_vega = _proposal_greek(proposal, "vega")
    if net_vega is None:
        return {}

    expirations = {
        exp
        for leg in (proposal.legs or [])
        for exp in (_expiration_date(getattr(leg, "expiration", None)),)
        if exp is not None
    }
    if len(expirations) != 1:
        return {}
    bucket = _tenor_bucket_for_expiration(next(iter(expirations)), budget)
    if bucket is None:
        return {}
    return {bucket: net_vega}


def check_proposal_against_budget(
    db: Session,
    user_id: Optional[int],
    proposal: StrategyProposal,
) -> BudgetCheckResult:
    """Hard check: would this proposal breach any greeks limit?

    Returns ``BudgetCheckResult(accepted=False, reasons=[...])`` if any
    limit is exceeded. Caller MUST refuse to place the trade if
    ``accepted=False``.

    Bypass: ``CHILI_OPTIONS_BUDGET_BYPASS=true`` env override skips the
    check (returns accepted=True with a warning reason). For
    operator-supervised testing only.
    """
    bypass = options_budget_bypass_enabled()
    budget = _default_budget()
    current = _zero_greeks()
    after = {
        "net_delta": 0.0,
        "net_gamma": 0.0,
        "net_theta": 0.0,
        "net_vega": 0.0,
    }
    try:
        budget = _get_budget(db, user_id)
        current = _sum_open_position_greeks(db, user_id, budget=budget)
        proposal_vega_by_tenor = _proposal_vega_by_tenor(proposal, budget)

        after_totals = _add_greek_totals(
            current,
            {
                "net_delta": _proposal_greek(proposal, "delta"),
                "net_gamma": _proposal_greek(proposal, "gamma"),
                "net_theta": _proposal_greek(proposal, "theta"),
                "net_vega": _proposal_greek(proposal, "vega"),
                "vega_by_tenor": proposal_vega_by_tenor,
                "missing_greeks_count": 0,
            },
        )
        after = {
            key: after_totals[key]
            for key in ("net_delta", "net_gamma", "net_theta", "net_vega")
        }
        after["vega_by_tenor"] = after_totals.get("vega_by_tenor") or {}

        reasons: list[str] = []
        if int(current.get("missing_greeks_count") or 0) > 0:
            reasons.append(
                f"missing_complete_greeks:open_positions:{int(current.get('missing_greeks_count') or 0)}"
            )
        missing = _proposal_missing_greeks(proposal)
        if missing:
            reasons.append("missing_complete_greeks:" + ",".join(missing))
        proposal_net_vega = _proposal_greek(proposal, "vega")
        if (
            _budget_tenor_limits(budget)
            and proposal_net_vega is not None
            and abs(proposal_net_vega) > 0.0
            and not proposal_vega_by_tenor
        ):
            reasons.append("missing_proposal_vega_tenor")
        elif (
            _budget_tenor_limits(budget)
            and proposal_net_vega is not None
            and abs(proposal_net_vega) > 0.0
            and abs(sum(proposal_vega_by_tenor.values()) - proposal_net_vega) > 1e-9
        ):
            reasons.append("incomplete_proposal_vega_tenor")
        if abs(after["net_delta"]) > budget["max_abs_delta"]:
            reasons.append(
                f"abs_delta_breach: |{after['net_delta']:.4f}| > {budget['max_abs_delta']}"
            )
        if abs(after["net_gamma"]) > budget["max_abs_gamma"]:
            reasons.append(
                f"abs_gamma_breach: |{after['net_gamma']:.6f}| > {budget['max_abs_gamma']}"
            )
        if abs(after["net_vega"]) > budget["max_total_vega"]:
            reasons.append(
                f"total_vega_breach: |{after['net_vega']:.4f}| > {budget['max_total_vega']}"
            )
        for tenor, _days, limit in _budget_tenor_limits(budget):
            tenor_vega = _finite_number_or_none(after["vega_by_tenor"].get(tenor))
            if tenor_vega is not None and abs(tenor_vega) > limit:
                reasons.append(
                    f"tenor_vega_breach:{tenor}: |{tenor_vega:.4f}| > {limit}"
                )
        if (
            budget["max_theta_burn_per_day"] is not None
            and after["net_theta"] < -budget["max_theta_burn_per_day"]
        ):
            reasons.append(
                f"theta_burn_breach: {after['net_theta']:.4f} < "
                f"-{budget['max_theta_burn_per_day']}/day"
            )
    except Exception as e:
        logger.warning("[options.budget] check failed closed: %s", e)
        reasons = [f"budget_error:{type(e).__name__}"]

    accepted = len(reasons) == 0
    if not accepted and bypass:
        reasons.insert(0, "BYPASS_VIA_CHILI_OPTIONS_BUDGET_BYPASS")
        accepted = True

    return BudgetCheckResult(
        accepted=accepted,
        reasons=reasons,
        current_portfolio=current,
        after_proposal=after,
        budget=budget,
    )


def upsert_budget(
    db: Session,
    user_id: int,
    *,
    max_abs_delta: float = 0.50,
    max_abs_gamma: float = 0.05,
    max_vega_per_tenor: Optional[dict] = None,
    max_total_vega: float = 200.0,
    max_theta_burn_per_day: Optional[float] = 50.0,
) -> bool:
    """Idempotent upsert of the per-user options greeks budget."""
    clean = _sanitize_budget(
        {
            "max_abs_delta": max_abs_delta,
            "max_abs_gamma": max_abs_gamma,
            "max_vega_per_tenor": max_vega_per_tenor or {},
            "max_total_vega": max_total_vega,
            "max_theta_burn_per_day": max_theta_burn_per_day,
        }
    )
    try:
        db.execute(
            text(
                """
                INSERT INTO options_greeks_budget
                    (user_id, max_abs_delta, max_abs_gamma, max_vega_per_tenor,
                     max_total_vega, max_theta_burn_per_day, updated_at)
                VALUES (:uid, :d, :g, :vt, :tv, :tb, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    max_abs_delta = EXCLUDED.max_abs_delta,
                    max_abs_gamma = EXCLUDED.max_abs_gamma,
                    max_vega_per_tenor = EXCLUDED.max_vega_per_tenor,
                    max_total_vega = EXCLUDED.max_total_vega,
                    max_theta_burn_per_day = EXCLUDED.max_theta_burn_per_day,
                    updated_at = NOW()
                """
            ),
            {
                "uid": user_id,
                "d": clean["max_abs_delta"],
                "g": clean["max_abs_gamma"],
                "vt": json.dumps(clean["max_vega_per_tenor"]),
                "tv": clean["max_total_vega"],
                "tb": clean["max_theta_burn_per_day"],
            },
        )
        db.commit()
        return True
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[options.budget] upsert_budget failed: %s", e)
        return False
