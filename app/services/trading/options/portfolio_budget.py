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
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .contracts import (
    complete_greeks,
    finite_greek,
    missing_greeks,
    parse_contract_quantity,
)
from .strategies import StrategyProposal

logger = logging.getLogger(__name__)
GREEK_KEYS = ("delta", "gamma", "theta", "vega")


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


def _nonnegative_finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out >= 0.0 else None


def _sanitize_budget(raw: Any) -> dict:
    """Return finite budget caps plus audit fields for malformed persisted caps."""
    defaults = _default_budget()
    budget = {
        **defaults,
        "max_vega_per_tenor": dict(defaults["max_vega_per_tenor"]),
    }
    invalid: list[str] = []
    if not isinstance(raw, dict):
        budget["_invalid_fields"] = ["budget"]
        return budget

    for key in ("max_abs_delta", "max_abs_gamma", "max_total_vega"):
        parsed = _nonnegative_finite_number(raw.get(key))
        if parsed is None:
            invalid.append(key)
        else:
            budget[key] = parsed

    theta = raw.get("max_theta_burn_per_day")
    if theta is None:
        budget["max_theta_burn_per_day"] = None
    else:
        parsed_theta = _nonnegative_finite_number(theta)
        if parsed_theta is None:
            invalid.append("max_theta_burn_per_day")
        else:
            budget["max_theta_burn_per_day"] = parsed_theta

    tenor_caps = raw.get("max_vega_per_tenor")
    if isinstance(tenor_caps, str):
        try:
            tenor_caps = json.loads(tenor_caps)
        except Exception:
            tenor_caps = None
    if isinstance(tenor_caps, dict):
        clean_tenors: dict[str, float] = {}
        for tenor, cap in tenor_caps.items():
            parsed_cap = _nonnegative_finite_number(cap)
            if parsed_cap is None:
                invalid.append(f"max_vega_per_tenor:{tenor}")
                continue
            clean_tenors[str(tenor)] = parsed_cap
        budget["max_vega_per_tenor"] = clean_tenors
    elif tenor_caps is not None:
        invalid.append("max_vega_per_tenor")

    prior_invalid = raw.get("_invalid_fields")
    if isinstance(prior_invalid, list):
        invalid.extend(str(field) for field in prior_invalid)
    budget["_invalid_fields"] = sorted(set(invalid))
    return budget


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
    out["missing_greeks_count"] = int(left.get("missing_greeks_count") or 0) + int(
        right.get("missing_greeks_count") or 0
    )
    return out


def _finite_number_or_zero(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _signed_contract_quantity(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out == 0.0 or not out.is_integer():
        return None
    return int(out)


def _proposal_greek(proposal: StrategyProposal, name: str) -> float | None:
    return finite_greek(getattr(proposal, f"net_{name}", None))


def _proposal_missing_greeks(proposal: StrategyProposal) -> list[str]:
    missing: list[str] = []
    for name in ("delta", "gamma", "theta", "vega"):
        if _proposal_greek(proposal, name) is None:
            missing.append(name)
    return missing


def _meta_greek(meta: dict[str, Any], key: str) -> float:
    value = meta.get(key)
    if value is None and isinstance(meta.get("quote_snapshot"), dict):
        value = meta["quote_snapshot"].get(key)
    parsed = finite_greek(value)
    if parsed is None:
        raise ValueError(f"missing_greek:{key}")
    return parsed


def _sum_open_position_greeks(db: Session, user_id: Optional[int]) -> dict:
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
        return _sum_open_trade_greeks(db, user_id)

    net_d = net_g = net_t = net_v = 0.0
    missing_count = 0
    for r in rows or []:
        try:
            legs = r[0]
            if isinstance(legs, str):
                legs = json.loads(legs)
            if not isinstance(legs, list) or not legs:
                missing_count += 1
                continue
            for leg in legs:
                if not isinstance(leg, dict):
                    missing_count += 1
                    continue
                qty = _signed_contract_quantity(leg.get("qty"))
                if qty is None:
                    missing_count += 1
                    continue
                d = leg.get("delta")
                g = leg.get("gamma")
                t = leg.get("theta")
                v = leg.get("vega")
                vals = {
                    "delta": finite_greek(d),
                    "gamma": finite_greek(g),
                    "theta": finite_greek(t),
                    "vega": finite_greek(v),
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
        except Exception:
            missing_count += 1
            continue
    position_totals = {
        "net_delta": net_d,
        "net_gamma": net_g,
        "net_theta": net_t,
        "net_vega": net_v,
        "missing_greeks_count": missing_count,
    }
    return _add_greek_totals(position_totals, _sum_open_trade_greeks(db, user_id))


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


def _sum_open_trade_greeks(db: Session, user_id: Optional[int]) -> dict:
    """Fallback Greek aggregation from open Trade snapshots.

    The original budget path only read ``options_position``. Live AutoTrader
    entries currently create ``trading_trades`` first, so this fallback keeps
    the budget aware of option trades even before a separate option-position
    projection is populated.
    """
    try:
        rows = db.execute(
            text(
                """
                SELECT quantity, indicator_snapshot
                FROM trading_trades
                WHERE (user_id = :uid OR :uid IS NULL)
                  AND status IN ('open', 'working')
                  AND (
                    LOWER(COALESCE(asset_kind, '')) IN ('option', 'options')
                    OR indicator_snapshot::jsonb ? 'option_meta'
                    OR indicator_snapshot::jsonb ? 'options_path'
                    OR (indicator_snapshot::jsonb -> 'breakout_alert') ? 'option_meta'
                  )
                """
            ),
            {"uid": user_id},
        ).fetchall()
    except Exception as e:
        logger.debug("[options.budget] open trade greeks fetch failed: %s", e)
        return _unproven_greeks()

    net_d = net_g = net_t = net_v = 0.0
    missing_count = 0
    for qty_raw, snapshot in rows or []:
        meta = _extract_option_meta(snapshot)
        if not meta:
            missing_count += 1
            continue
        qty_source = qty_raw if qty_raw is not None else meta.get("quantity")
        qty = parse_contract_quantity(qty_source)
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
            value = meta.get(key)
            if value is None and isinstance(meta.get("quote_snapshot"), dict):
                value = meta["quote_snapshot"].get(key)
            f = finite_greek(value)
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
    return {
        "net_delta": net_d,
        "net_gamma": net_g,
        "net_theta": net_t,
        "net_vega": net_v,
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
        budget = _sanitize_budget(_get_budget(db, user_id))
        current = _sum_open_position_greeks(db, user_id)

        after_totals = _add_greek_totals(
            current,
            {
                "net_delta": _proposal_greek(proposal, "delta"),
                "net_gamma": _proposal_greek(proposal, "gamma"),
                "net_theta": _proposal_greek(proposal, "theta"),
                "net_vega": _proposal_greek(proposal, "vega"),
                "missing_greeks_count": 0,
            },
        )
        after = {key: after_totals[key] for key in ("net_delta", "net_gamma", "net_theta", "net_vega")}

        reasons: list[str] = []
        invalid_budget_fields = budget.get("_invalid_fields") or []
        if invalid_budget_fields:
            reasons.append("budget_invalid:" + ",".join(map(str, invalid_budget_fields)))
        if int(current.get("missing_greeks_count") or 0) > 0:
            reasons.append(
                f"missing_complete_greeks:open_positions:{int(current.get('missing_greeks_count') or 0)}"
            )
        missing = _proposal_missing_greeks(proposal)
        if missing:
            reasons.append("missing_complete_greeks:" + ",".join(missing))
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
    try:
        clean_budget = _sanitize_budget({
            "max_abs_delta": max_abs_delta,
            "max_abs_gamma": max_abs_gamma,
            "max_vega_per_tenor": max_vega_per_tenor or {},
            "max_total_vega": max_total_vega,
            "max_theta_burn_per_day": max_theta_burn_per_day,
        })
        invalid_budget_fields = clean_budget.get("_invalid_fields") or []
        if invalid_budget_fields:
            logger.warning(
                "[options.budget] refusing invalid budget caps: %s",
                invalid_budget_fields,
            )
            return False

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
                "d": clean_budget["max_abs_delta"],
                "g": clean_budget["max_abs_gamma"],
                "vt": json.dumps(clean_budget["max_vega_per_tenor"]),
                "tv": clean_budget["max_total_vega"],
                "tb": clean_budget["max_theta_burn_per_day"],
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
