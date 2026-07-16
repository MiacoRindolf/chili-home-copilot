"""Paper automation runner — batch/tick service (Phase 7).

Risk snapshot contract (do not violate in future phases):
- ``risk_snapshot_json["momentum_risk"]`` and other admission-time keys are frozen audit
  baseline; this module never overwrites them.
- Mutable execution state lives under ``risk_snapshot_json["momentum_paper_execution"]`` only.
- Runner may re-check governance / freshness / policy via ``evaluate_proposed_momentum_automation``;
  on mismatch, emit ``paper_blocked_by_risk`` or ``paper_policy_drift`` and take a safe action
  (stall, error, or exit) — never silently rewrite historical snapshot fields.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ....config import settings
from ....models.trading import (
    MomentumSymbolViability,
    MomentumStrategyVariant,
    TradingAutomationSession,
    TradingAutomationSimulatedFill,
)
from ..execution_family_registry import normalize_execution_family, momentum_runner_supports_execution_family
from .persistence import (
    append_trading_automation_event,
    append_trading_automation_simulated_fill,
    variant_for_id,
    build_runtime_snapshot_values,
    default_session_binding,
    upsert_trading_automation_runtime_snapshot,
    upsert_trading_automation_session_binding,
)
from .risk_evaluator import evaluate_proposed_momentum_automation
from .risk_policy import RISK_SNAPSHOT_KEY, policy_float_cap, policy_int_cap
from .paper_execution import (
    cushion_adaptive_trail_stop,
    breakeven_stop_after_partial,
    build_synthetic_quote,
    class_aware_reward_risk,
    crypto_paper_roundtrip_bps,
    default_reference_mid,
    effective_stop_atr_pct,
    long_exit_fill_price,
    regime_atr_pct,
    roundtrip_fee_usd,
    runner_trail_stop,
    scale_out_fraction,
    scale_out_quantity,
    stop_target_prices,
    structural_or_vol_floored_atr_pct,
    utc_iso,
)
from .paper_fsm import (
    STATE_BAILOUT,
    STATE_COOLDOWN,
    STATE_ENTERED,
    STATE_ENTRY_CANDIDATE,
    STATE_ERROR,
    STATE_EXITED,
    STATE_FINISHED,
    STATE_PENDING_ENTRY,
    STATE_QUEUED,
    STATE_SCALING_OUT,
    STATE_TRAILING,
    STATE_WATCHING,
    assert_transition,
    is_live_intent_state,
    PAPER_RUNNER_RUNNABLE_STATES,
)
from .session_lifecycle import is_operator_paused
from .strategy_params import normalize_strategy_params
from ..decision_ledger import (
    finalize_packet_after_simulated_exit,
    mark_packet_executed,
    run_momentum_entry_decision,
)
from ..deployment_ladder_service import record_trade_outcome_metrics
from ..market_data import fetch_ohlcv_df
from .entry_gates import bos_exit_triggered_long, run_paper_entry_gates
from .adaptive_risk_policy import (
    AdaptiveRiskContractError,
    ResolvedAdaptiveRisk,
    RiskInputEvidence,
)
from .adaptive_risk_reservation import (
    AdaptiveReservationError,
    AdaptiveReservationIdempotencyConflict,
    AdaptiveRiskPendingSettlement,
    AdaptiveRiskReservationStore,
    DurableOrderLifecycleEvidence,
    LockedAdaptiveRiskAdmissionSnapshot,
    RESERVATION_LEDGER_GENERATION,
    canonical_db_paper_fill_content_sha256,
    load_adaptive_risk_reservation_request,
)
from .adaptive_risk_request_builder import (
    AdaptiveRiskBuilderError,
    AdaptiveRiskBuilderSource,
    DbPaperAdmissionReceipt,
    DbPaperExecutableAdmission,
    DbPaperFinalAdmissionBundle,
    DbPaperFinalAdmissionObservation,
    KEY_ADAPTIVE_RISK_BUILDER_SOURCE,
    KEY_DB_PAPER_EXECUTABLE_ADMISSION,
    KEY_DB_PAPER_FINAL_ADMISSION_RECEIPT,
    build_adaptive_risk_request,
    db_paper_execution_terms_payload,
    load_db_paper_admission_receipt,
    load_adaptive_risk_builder_source,
    load_db_paper_executable_admission,
    load_db_paper_final_admission_bundle,
    load_db_paper_final_admission_observation,
    resolve_detector_setup_family,
    runtime_db_paper_final_admission,
)

_log = logging.getLogger(__name__)

KEY_PAPER_EXEC = "momentum_paper_execution"
KEY_ADAPTIVE_RISK_REQUEST = "adaptive_risk_reservation_request"

QuoteFn = Callable[[str], dict[str, Any]]


def _utcnow() -> datetime:
    return datetime.utcnow()


def _finite_float_or_default(value: Any, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(out):
        return default
    return out


def _execution_readiness_costs(execution_readiness: Any) -> tuple[float, float, float]:
    ex = execution_readiness if isinstance(execution_readiness, dict) else {}
    return (
        _finite_float_or_default(ex.get("spread_bps"), 12.0),
        _finite_float_or_default(ex.get("slippage_estimate_bps"), 10.0),
        _finite_float_or_default(ex.get("fee_to_target_ratio"), 0.08),
    )


def _effective_viability(via: MomentumSymbolViability, max_age_sec: float) -> float:
    """Linear decay of score when viability row is stale past half of policy max age."""
    raw = float(via.viability_score or 0.0)
    ft = getattr(via, "freshness_ts", None)
    if ft is None:
        return raw
    try:
        ft_naive = ft.replace(tzinfo=None) if getattr(ft, "tzinfo", None) else ft
        age = (_utcnow() - ft_naive).total_seconds()
    except Exception:
        return raw
    half = max(60.0, float(max_age_sec) / 2.0)
    if age <= half:
        return raw
    decay = min(0.25, (age - half) / max(float(max_age_sec), 1.0) * 0.25)
    return max(0.0, raw - decay)


def _via_entry_paused(via: MomentumSymbolViability) -> bool:
    ex = via.explain_json if isinstance(via.explain_json, dict) else {}
    until_raw = ex.get("variant_symbol_pause_until_utc")
    if not until_raw:
        return False
    try:
        until = datetime.fromisoformat(str(until_raw).replace("Z", "+00:00")).replace(tzinfo=None)
        return _utcnow() < until
    except Exception:
        return False


def _policy_caps(snap: dict[str, Any]) -> dict[str, Any]:
    caps = snap.get("momentum_policy_caps")
    return caps if isinstance(caps, dict) else {}


def _paper_exec(snap: dict[str, Any]) -> dict[str, Any]:
    pe = snap.get(KEY_PAPER_EXEC)
    return dict(pe) if isinstance(pe, dict) else {}


def _commit_pe(sess: TradingAutomationSession, pe: dict[str, Any]) -> None:
    snap = dict(sess.risk_snapshot_json or {})
    snap[KEY_PAPER_EXEC] = pe
    sess.risk_snapshot_json = snap
    # Force the JSON column dirty so a second in-tick mutation around an intervening
    # flush is never silently dropped (mirrors live _commit_le; see note there).
    try:
        flag_modified(sess, "risk_snapshot_json")
    except Exception:
        pass


def _same_money(left: Any, right: Any) -> bool:
    try:
        lhs = float(left)
        rhs = float(right)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(lhs) or not math.isfinite(rhs):
        return False
    return abs(lhs - rhs) <= max(1e-9, max(abs(lhs), abs(rhs)) * 1e-12)


def _db_paper_account_binding(
    sess: TradingAutomationSession,
) -> tuple[str, str]:
    snapshot = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    binding = snapshot.get("db_paper_account_binding")
    binding = binding if isinstance(binding, dict) else {}
    account_scope = str(binding.get("account_scope") or "").strip()
    account_identity = str(
        binding.get("account_identity_sha256") or ""
    ).strip().lower()
    if not account_scope.startswith("db-paper:"):
        raise AdaptiveRiskBuilderError(
            "db_paper_account_binding_missing", "account_scope"
        )
    if len(account_identity) != 64 or any(
        char not in "0123456789abcdef" for char in account_identity
    ):
        raise AdaptiveRiskBuilderError(
            "db_paper_account_binding_missing", "account_identity_sha256"
        )
    return account_scope, account_identity


def _adaptive_paper_setup_family(
    pe: dict[str, Any],
    variant: MomentumStrategyVariant | None,
    *,
    symbol: Any,
) -> str:
    """Recover setup identity without treating diagnostic booleans as proof."""

    debug = pe.get("entry_trigger_debug")
    debug = debug if isinstance(debug, dict) else {}
    variant_family = str(
        getattr(variant, "family", "") or "unknown"
    ).strip().lower()
    setup = resolve_detector_setup_family(
        debug,
        fallback_setup_family=variant_family,
        expected_symbol=symbol,
    )
    if setup == "first_dip_reclaim":
        return setup
    if variant_family == "first_dip_reclaim":
        # A first-dip variant without the detector's exact opportunity identity
        # must not fall through as an ordinary family and bypass final receipt
        # verification.  A veto here precedes reservation and leaves the
        # once-per-day opportunity reusable.
        raise AdaptiveRiskBuilderError(
            "adaptive_risk_builder_boundary_mismatch",
            "first_dip_opportunity_key_missing",
        )
    return setup


def _final_revalidate_adaptive_db_paper_entry(
    db: Session,
    sess: TradingAutomationSession,
    pe: dict[str, Any],
    *,
    via: MomentumSymbolViability,
    variant: MomentumStrategyVariant | None,
    stop_atr_mult: float,
    target_atr_mult: float,
    vol_floor_mult: float,
) -> dict[str, Any]:
    """Consume one producer-owned final prefix without creating a reservation.

    The tick's earlier quote and ORM viability object are detector hints only.
    This boundary locks/reloads eligibility, records the DB visibility clock,
    then asks the capture producer for one immutable bundle containing the
    latest exact BBO, final entry-gate result, and adaptive source.  No mutable
    market/provider read is permitted after this helper succeeds.
    """

    try:
        final_via = (
            db.query(MomentumSymbolViability)
            .filter(MomentumSymbolViability.id == int(via.id))
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
    except Exception:
        return {
            "ok": False,
            "veto": True,
            "reason": "db_paper_final_eligibility_read_failed",
        }
    eligibility_available_at = datetime.now(timezone.utc)
    if final_via is None:
        return {
            "ok": False,
            "veto": True,
            "reason": "db_paper_final_eligibility_missing",
            "decision_at": eligibility_available_at,
        }
    if (
        str(final_via.symbol or "").strip().upper()
        != str(sess.symbol or "").strip().upper()
        or int(final_via.variant_id) != int(sess.variant_id)
    ):
        return {
            "ok": False,
            "veto": True,
            "reason": "db_paper_final_eligibility_identity_mismatch",
            "decision_at": eligibility_available_at,
        }
    if final_via.paper_eligible is not True:
        return {
            "ok": False,
            "veto": True,
            "reason": "db_paper_final_eligibility_veto",
            "decision_at": eligibility_available_at,
        }
    try:
        eligibility_observed_at = _aware_db_utc(
            final_via.freshness_ts, "viability.freshness_ts"
        )
        eligibility_row_updated_at = _aware_db_utc(
            final_via.updated_at, "viability.updated_at"
        )
    except AdaptiveRiskContractError:
        return {
            "ok": False,
            "veto": True,
            "reason": "db_paper_final_eligibility_clock_missing",
            "decision_at": eligibility_available_at,
        }
    if eligibility_observed_at > eligibility_available_at:
        return {
            "ok": False,
            "veto": True,
            "reason": "db_paper_final_eligibility_from_future",
            "decision_at": eligibility_available_at,
        }

    try:
        expected_account_scope, expected_account_identity = (
            _db_paper_account_binding(sess)
        )
        detector_setup_family = _adaptive_paper_setup_family(
            pe, variant, symbol=sess.symbol
        )
        _spread_bps, requested_slip_bps, requested_fee_ratio = (
            _execution_readiness_costs(final_via.execution_readiness_json)
        )
        # These process settings are read before the capture producer creates
        # its immutable material.  The producer must echo them under the exact
        # effective-config digest; no global setting is read after finalization.
        requested_reward_risk = class_aware_reward_risk(sess.symbol)
        material = runtime_db_paper_final_admission(
            execution_surface="db_paper",
            execution_family=normalize_execution_family(sess.execution_family),
            venue=str(sess.venue or "").strip().lower(),
            broker_environment="paper",
            symbol=str(sess.symbol or "").strip().upper(),
            setup_family=detector_setup_family,
            account_scope=expected_account_scope,
            account_identity_sha256=expected_account_identity,
            viability_id=int(final_via.id),
            variant_id=int(final_via.variant_id),
            viability_score=float(final_via.viability_score),
            paper_eligible=bool(final_via.paper_eligible),
            eligibility_observed_at=eligibility_observed_at,
            # This is the honest DB visibility clock for the locked refresh,
            # not the row's metadata update timestamp.
            eligibility_available_at=eligibility_available_at,
            eligibility_row_updated_at=eligibility_row_updated_at,
            execution_readiness=dict(final_via.execution_readiness_json or {}),
            stop_atr_mult=float(stop_atr_mult),
            target_atr_mult=float(target_atr_mult),
            vol_floor_mult=float(vol_floor_mult),
            reward_risk=float(requested_reward_risk),
            entry_slippage_bps=float(requested_slip_bps),
            exit_slippage_bps=float(requested_slip_bps),
            fee_to_target_ratio=float(requested_fee_ratio),
        )
    except AdaptiveRiskBuilderError as exc:
        return {
            "ok": False,
            "veto": True,
            "reason": exc.reason,
            "detail": exc.detail,
            "decision_at": eligibility_available_at,
        }

    source = material.source
    if (
        source.account_scope != expected_account_scope
        or source.account_snapshot.account_scope != expected_account_scope
        or source.inputs.account_identity_sha256 != expected_account_identity
        or source.account_snapshot.account_identity_sha256
        != expected_account_identity
    ):
        return {
            "ok": False,
            "veto": True,
            "reason": "db_paper_account_binding_mismatch",
            "decision_at": eligibility_available_at,
        }
    decision_at = source.inputs.as_of
    final_reason = material.gate_reason
    final_debug = dict(material.gate_debug)
    prior_debug = pe.get("entry_trigger_debug")
    prior_debug = prior_debug if isinstance(prior_debug, dict) else {}
    prior_opportunity = prior_debug.get("opportunity_key")
    final_opportunity = dict(material.opportunity_key)
    comparable_final_opportunity = {
        key: value
        for key, value in final_opportunity.items()
        if key != "account_scope"
    }
    if (
        isinstance(prior_opportunity, dict)
        and prior_opportunity != comparable_final_opportunity
    ):
        return {
            "ok": False,
            "veto": True,
            "reason": "db_paper_final_opportunity_changed",
            "gate_reason": str(final_reason or ""),
            "gate_debug": final_debug,
            "decision_at": decision_at,
        }

    locked_snapshot: LockedAdaptiveRiskAdmissionSnapshot | None = None
    try:
        if not material.gate_allowed:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_entry_gate_veto",
                str(final_reason or "unknown"),
            )
        final_pe = dict(pe)
        final_pe["entry_trigger_debug"] = final_debug
        setup_family = _adaptive_paper_setup_family(
            final_pe, variant, symbol=sess.symbol
        )
        if setup_family != source.setup_family:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_opportunity_mismatch", "setup_family"
            )
        if decision_at < eligibility_available_at:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_evidence_from_future", "eligibility_db_read"
            )

        terms = dict(material.execution_terms)
        requested_terms = {
            "stop_atr_mult": float(stop_atr_mult),
            "target_atr_mult": float(target_atr_mult),
            "vol_floor_mult": float(vol_floor_mult),
            "reward_risk": float(requested_reward_risk),
            "entry_slippage_bps": float(requested_slip_bps),
            "exit_slippage_bps": float(requested_slip_bps),
            "fee_to_target_ratio": float(requested_fee_ratio),
        }
        changed_terms = sorted(
            name
            for name, expected in requested_terms.items()
            if not _same_money(terms.get(name), expected)
        )
        if changed_terms:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_execution_terms_changed",
                ",".join(changed_terms),
            )
        final_slip_bps = float(terms["entry_slippage_bps"])
        final_exit_slip_bps = float(terms["exit_slippage_bps"])
        final_fee_ratio = float(terms["fee_to_target_ratio"])
        if not _same_money(source.inputs.entry_slippage_bps, final_slip_bps):
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_mismatch", "entry_slippage_bps"
            )
        if not _same_money(source.inputs.exit_slippage_bps, final_exit_slip_bps):
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_mismatch", "exit_slippage_bps"
            )

        captured_eligibility = dict(material.eligibility)
        exact_eligibility = {
            "symbol": str(final_via.symbol or "").strip().upper(),
            "viability_id": int(final_via.id),
            "variant_id": int(final_via.variant_id),
            "viability_score": float(final_via.viability_score),
            "paper_eligible": bool(final_via.paper_eligible),
            "observed_at": eligibility_observed_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "available_at": eligibility_available_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "row_updated_at": eligibility_row_updated_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "execution_readiness": dict(
                final_via.execution_readiness_json or {}
            ),
        }
        eligibility_changed = sorted(
            name
            for name, expected in exact_eligibility.items()
            if (
                not _same_money(captured_eligibility.get(name), expected)
                if name == "viability_score"
                else captured_eligibility.get(name) != expected
            )
        )
        if eligibility_changed:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_eligibility_changed",
                ",".join(eligibility_changed),
            )

        # The provider's captured material tells us which adaptive account and
        # correlation cluster to lock.  The resulting DB snapshot is folded into
        # the final bundle before any economics are resolved.
        store = AdaptiveRiskReservationStore(db.get_bind())
        locked_snapshot = store.lock_admission_snapshot(
            account_scope=source.account_scope,
            symbol=source.inputs.symbol,
            correlation_cluster=source.correlation_cluster,
            account_snapshot=source.account_snapshot,
            session=db,
        )
        evidence = dict(source.inputs.evidence)
        evidence["reservation_ledger"] = RiskInputEvidence(
            source="postgresql:adaptive_risk_reservations",
            observed_at=locked_snapshot.observed_at,
            available_at=locked_snapshot.observed_at,
            content_sha256=locked_snapshot.ledger_sha256,
            provider_generation=RESERVATION_LEDGER_GENERATION,
        )
        aggregates = locked_snapshot.aggregates
        exact_inputs = replace(
            source.inputs,
            as_of=locked_snapshot.observed_at,
            open_structural_risk_usd=aggregates["open_structural_risk_usd"],
            pending_reserved_risk_usd=aggregates["pending_reserved_risk_usd"],
            existing_same_symbol_structural_risk_usd=aggregates[
                "existing_same_symbol_structural_risk_usd"
            ],
            pending_same_symbol_structural_risk_usd=aggregates[
                "pending_same_symbol_structural_risk_usd"
            ],
            current_cluster_structural_risk_usd=aggregates[
                "current_cluster_structural_risk_usd"
            ],
            pending_correlation_cluster_risk_usd=aggregates[
                "pending_correlation_cluster_risk_usd"
            ],
            portfolio_gross_notional_usd=aggregates[
                "portfolio_gross_notional_usd"
            ],
            pending_portfolio_gross_notional_usd=aggregates[
                "pending_portfolio_gross_notional_usd"
            ],
            policy_buying_power_capacity_usd=(
                locked_snapshot.policy_buying_power_capacity_usd
            ),
            open_buying_power_impact_usd=aggregates[
                "open_buying_power_impact_usd"
            ],
            pending_buying_power_impact_usd=aggregates[
                "pending_buying_power_impact_usd"
            ],
            evidence=evidence,
        )
        source = replace(source, inputs=exact_inputs)
        bundle = DbPaperFinalAdmissionBundle.create(
            material,
            source,
            locked_risk_snapshot=locked_snapshot,
        )
        # Everything below reads only the immutable finalized bundle.
        source = bundle.source
        decision_at = source.inputs.as_of
        final_reason = bundle.gate_reason
        final_debug = dict(bundle.gate_debug)
        final_opportunity = dict(bundle.opportunity_key)
        terms = dict(bundle.execution_terms)
        final_bid = float(source.inputs.bid)
        final_ask = float(source.inputs.ask)
        final_mid = (final_bid + final_ask) / 2.0
        entry_price = final_ask * (
            1.0 + float(source.inputs.entry_slippage_bps) / 10_000.0
        )
        pullback_low = final_debug.get("pullback_low")
        if pullback_low is None:
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_invalid", "pullback_low_missing"
            )
        # Recompute the executable stop/target from the final BBO and the
        # final-prefix volatility/structure.  The producer's risk source must
        # agree exactly; it cannot smuggle in a stop calculated on the old tick.
        effective_atr = effective_stop_atr_pct(
            float(source.inputs.realized_volatility_fraction),
            float(source.inputs.realized_volatility_fraction) * 10_000.0,
            stop_atr_mult=float(terms["stop_atr_mult"]),
            vol_floor_mult=float(terms["vol_floor_mult"]),
        )
        effective_atr, stop_model = structural_or_vol_floored_atr_pct(
            vol_floored_atr_pct=effective_atr,
            structural_stop_price=float(pullback_low),
            entry_price=entry_price,
            stop_atr_mult=float(terms["stop_atr_mult"]),
        )
        stop_price, target_price = stop_target_prices(
            entry_price,
            atr_pct=effective_atr,
            side_long=True,
            stop_atr_mult=float(terms["stop_atr_mult"]),
            target_atr_mult=float(terms["target_atr_mult"]),
            reward_risk=float(terms["reward_risk"]),
        )
        if not _same_money(source.inputs.structural_stop, stop_price):
            raise AdaptiveRiskBuilderError(
                "db_paper_final_boundary_mismatch", "recomputed_structural_stop"
            )
        eligibility = dict(bundle.eligibility)

        def eligibility_clock(name: str) -> datetime:
            raw = eligibility.get(name)
            if not isinstance(raw, str) or not raw.strip():
                raise AdaptiveRiskBuilderError(
                    "db_paper_final_eligibility_clock_missing", name
                )
            try:
                value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise AdaptiveRiskBuilderError(
                    "db_paper_final_eligibility_clock_invalid", name
                ) from exc
            if value.tzinfo is None:
                raise AdaptiveRiskBuilderError(
                    "db_paper_final_eligibility_clock_invalid", name
                )
            return value.astimezone(timezone.utc)

        observation = DbPaperFinalAdmissionObservation.create(
            source,
            decision_at=decision_at,
            bid=final_bid,
            ask=final_ask,
            quote_source=bundle.quote_source,
            viability_id=int(eligibility["viability_id"]),
            variant_id=int(eligibility["variant_id"]),
            viability_score=float(eligibility["viability_score"]),
            paper_eligible=eligibility.get("paper_eligible") is True,
            eligibility_observed_at=eligibility_clock("observed_at"),
            eligibility_available_at=eligibility_clock("available_at"),
            eligibility_row_updated_at=eligibility_clock("row_updated_at"),
            execution_readiness=dict(eligibility.get("execution_readiness") or {}),
            gate_allowed=bundle.gate_allowed,
            gate_reason=str(final_reason or ""),
            gate_debug=final_debug,
            structural_stop=stop_price,
            opportunity_key=final_opportunity,
            first_dip_final_admission_envelope=(
                material._first_dip_final_admission_envelope
            ),
            first_dip_final_admission_expectation=(
                material._first_dip_final_admission_expectation
            ),
        )
    except AdaptiveRiskBuilderError as exc:
        return {
            "ok": False,
            "veto": True,
            "reason": exc.reason,
            "detail": exc.detail,
            "gate_reason": str(final_reason or ""),
            "gate_debug": final_debug,
            "decision_at": decision_at,
        }
    except AdaptiveRiskPendingSettlement as exc:
        return {
            "ok": False,
            "veto": True,
            "reason": exc.reason,
            "detail": "flat_cycle_net_economics_unresolved",
            "settlement_provenance": exc.provenance,
            "gate_reason": str(final_reason or ""),
            "gate_debug": final_debug,
            "decision_at": decision_at,
        }
    except (AdaptiveRiskContractError, AdaptiveReservationError) as exc:
        return {
            "ok": False,
            "veto": True,
            "reason": "db_paper_locked_admission_snapshot_failed",
            "detail": type(exc).__name__,
            "gate_reason": str(final_reason or ""),
            "gate_debug": final_debug,
            "decision_at": decision_at,
        }

    pe["entry_trigger_debug"] = final_debug
    pe["entry_trigger_reason"] = str(final_reason or "")
    pe[KEY_ADAPTIVE_RISK_BUILDER_SOURCE] = source.to_payload()
    pe["adaptive_risk_final_admission_bundle"] = bundle.to_payload()
    pe["adaptive_risk_final_observation"] = observation.to_payload()
    return {
        "ok": True,
        "source": source,
        "observation": observation,
        "bundle": bundle,
        "locked_snapshot": locked_snapshot,
        "setup_family": setup_family,
        "decision_at": decision_at,
        "gate_reason": str(final_reason or ""),
        "gate_debug": final_debug,
        "via": final_via,
        "bid": final_bid,
        "ask": final_ask,
        "mid": final_mid,
        "quote_source": bundle.quote_source,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "effective_atr": effective_atr,
        "stop_model": stop_model,
        "slippage_bps": final_slip_bps,
        "fee_ratio": final_fee_ratio,
    }


def _reserve_adaptive_db_paper_entry(
    db: Session,
    sess: TradingAutomationSession,
    pe: dict[str, Any],
    *,
    bid: float,
    ask: float,
    entry_price: float,
    structural_stop: float,
    setup_family: str,
    builder_source: AdaptiveRiskBuilderSource | None = None,
    final_observation: DbPaperFinalAdmissionObservation | None = None,
    final_bundle: DbPaperFinalAdmissionBundle | None = None,
    locked_snapshot: LockedAdaptiveRiskAdmissionSnapshot | None = None,
    reference_price: float | None = None,
    target_price: float | None = None,
    effective_atr: float | None = None,
    fee_ratio: float | None = None,
) -> dict[str, Any]:
    """Consume one strict request at the DB-paper fill boundary.

    The caller has already assembled the final bundle while holding the
    account-scoped PostgreSQL lock.  This boundary resolves exactly once,
    content-verifies that resolution in the store, and persists reservation +
    executable receipt in one rollback-safe savepoint.
    """

    payload: dict[str, Any] | None = None
    try:
        builder_audit: dict[str, Any] | None = None
        source: AdaptiveRiskBuilderSource | None = builder_source
        if (
            source is None
            or final_observation is None
            or final_bundle is None
            or locked_snapshot is None
            or reference_price is None
            or target_price is None
            or effective_atr is None
            or fee_ratio is None
        ):
            return {
                "ok": False,
                "reason": "db_paper_final_admission_receipt_required",
            }
        # Every new DB-paper reservation rebuilds from the just-observed final
        # bundle.  There is no request-only/legacy bypass: without the source +
        # observation we cannot create the mandatory fill receipt.
        reloaded_bundle = load_db_paper_final_admission_bundle(
            final_bundle.to_payload()
        )
        reloaded_observation = load_db_paper_final_admission_observation(
            final_observation.to_payload()
        )
        if (
            reloaded_bundle.source.source_sha256 != source.source_sha256
            or reloaded_observation.source_sha256 != source.source_sha256
            or reloaded_bundle.locked_risk_snapshot.get("content_sha256")
            != locked_snapshot.content_sha256
        ):
            raise AdaptiveRiskBuilderError(
                "db_paper_final_bundle_mismatch", "reservation_boundary"
            )
        source = reloaded_bundle.source
        final_bundle = reloaded_bundle
        final_observation = reloaded_observation
        expected_account_scope, expected_account_identity = (
            _db_paper_account_binding(sess)
        )
        if (
            source.account_scope != expected_account_scope
            or source.account_snapshot.account_scope != expected_account_scope
            or source.inputs.account_identity_sha256 != expected_account_identity
            or source.account_snapshot.account_identity_sha256
            != expected_account_identity
        ):
            raise AdaptiveRiskBuilderError("db_paper_account_binding_mismatch")
        payload = None
        pe[KEY_ADAPTIVE_RISK_BUILDER_SOURCE] = source.to_payload()
        pe["adaptive_risk_final_observation"] = final_observation.to_payload()
        pe["adaptive_risk_final_admission_bundle"] = final_bundle.to_payload()
        if payload is None:
            source_matches = bool(
                source.inputs.execution_surface == "db_paper"
                and source.inputs.broker_environment == "paper"
                and source.inputs.symbol == str(sess.symbol or "").strip().upper()
                and source.inputs.side == "long"
                and source.inputs.execution_family
                == normalize_execution_family(sess.execution_family)
                and source.inputs.venue == str(sess.venue or "").strip().lower()
                and source.setup_family == str(setup_family or "").strip().lower()
                and source.correlation_cluster
                == source.inputs.correlation_cluster_id
                and source.account_scope == expected_account_scope
                and source.inputs.account_identity_sha256
                == expected_account_identity
                and _same_money(source.inputs.bid, bid)
                and _same_money(source.inputs.ask, ask)
                and _same_money(source.inputs.structural_stop, structural_stop)
            )
            if not source_matches:
                raise AdaptiveRiskBuilderError(
                    "adaptive_risk_builder_boundary_mismatch",
                    "db_paper_entry_boundary",
                )
            client_order_id = (
                f"db-paper-{int(sess.id)}-{source.inputs.decision_id}"
            )
            built = build_adaptive_risk_request(
                source,
                client_order_id=client_order_id,
                entry_limit_price=entry_price,
                opportunity_key=final_bundle.opportunity_key,
            )
            request = built.request
            payload = request.to_payload()
            pe[KEY_ADAPTIVE_RISK_REQUEST] = payload
            builder_audit = built.audit_payload()
            pe["adaptive_risk_builder_audit"] = builder_audit
        else:  # pragma: no cover - payload is deliberately reset above
            request = load_adaptive_risk_reservation_request(payload)
        if pe.get("adaptive_risk_request_consumed_sha256") == request.request_sha256:
            return {"ok": False, "reason": "adaptive_risk_binding_stale"}
        session_family = normalize_execution_family(sess.execution_family)
        session_venue = str(sess.venue or "").strip().lower()
        request_matches = bool(
            request.inputs.execution_surface == "db_paper"
            and request.inputs.broker_environment == "paper"
            and request.inputs.symbol == str(sess.symbol or "").strip().upper()
            and request.inputs.side == "long"
            and request.inputs.execution_family == session_family
            and request.inputs.venue == session_venue
            and request.account_scope == expected_account_scope
            and request.inputs.account_identity_sha256
            == expected_account_identity
            and request.setup_family == str(setup_family or "").strip().lower()
            and request.correlation_cluster
            == request.inputs.correlation_cluster_id
            and _same_money(request.inputs.bid, bid)
            and _same_money(request.inputs.ask, ask)
            and _same_money(request.inputs.structural_stop, structural_stop)
            and _same_money(request.entry_limit_price, entry_price)
        )
        if not request_matches:
            raise AdaptiveRiskContractError(
                "DB-paper adaptive request does not match the current fill boundary"
            )
        # Explicitly begin/retain the caller-owned transaction.  The adaptive
        # reservation and its later canonical fill must share this exact unit
        # of work; the store must not commit an independent DB-paper claim.
        if not db.in_transaction():
            db.connection()
        store = AdaptiveRiskReservationStore(db.get_bind())
        connection_generation = f"db-paper-session:{int(sess.id)}"
        with db.begin_nested():
            decision = store.reserve(
                request,
                session=db,
                locked_snapshot=locked_snapshot,
                prepared_resolution=built.resolution,
                prepared_decision_packet=built.decision_packet,
            )
            if not decision.admission_accepted or decision.reservation_id is None:
                return {
                    "ok": False,
                    "reason": "adaptive_risk_admission_rejected",
                    "rejection_reasons": list(decision.rejection_reasons),
                    "decision_packet_sha256": decision.decision_packet_sha256,
                }
            expected_decision = {
                "decision_packet_sha256": (
                    decision.decision_packet_sha256,
                    built.resolution.decision_packet_sha256,
                ),
                "quantity_shares": (
                    int(decision.quantity_shares),
                    int(built.resolution.quantity_shares),
                ),
                "structural_risk_usd": (
                    float(decision.structural_risk_usd),
                    float(built.resolution.planned_structural_risk_usd),
                ),
                "gross_notional_usd": (
                    float(decision.gross_notional_usd),
                    float(built.resolution.planned_notional_usd),
                ),
                "buying_power_impact_usd": (
                    float(decision.buying_power_impact_usd),
                    float(built.resolution.planned_buying_power_impact_usd),
                ),
            }
            changed_decision = sorted(
                name
                for name, (actual, expected) in expected_decision.items()
                if (
                    actual != expected
                    if isinstance(actual, str)
                    else not _same_money(actual, expected)
                )
            )
            if changed_decision:
                raise AdaptiveRiskContractError(
                    "DB-paper reservation changed the prepared resolution: "
                    + ",".join(changed_decision)
                )
            expected_gross = float(entry_price) * int(decision.quantity_shares)
            if not _same_money(decision.gross_notional_usd, expected_gross):
                raise AdaptiveRiskContractError(
                    "DB-paper fill notional differs from the reserved executable notional"
                )
            venue_roundtrip_bps = None
            if str(sess.symbol or "").upper().endswith("-USD"):
                venue_roundtrip_bps = crypto_paper_roundtrip_bps()
            executable_fees = roundtrip_fee_usd(
                float(decision.gross_notional_usd),
                float(fee_ratio),
                entry=float(entry_price),
                target=float(target_price),
                venue_rt_bps=venue_roundtrip_bps,
            )
            executable = DbPaperExecutableAdmission.create(
                final_bundle,
                final_observation,
                request,
                built.resolution,
                reservation_id=decision.reservation_id,
                structural_risk_usd=float(decision.structural_risk_usd),
                gross_notional_usd=float(decision.gross_notional_usd),
                buying_power_impact_usd=float(
                    decision.buying_power_impact_usd
                ),
                entry_price=float(entry_price),
                reference_price=float(reference_price),
                stop_price=float(structural_stop),
                target_price=float(target_price),
                fees_usd=float(executable_fees),
                effective_atr=float(effective_atr),
            )
            receipt = DbPaperAdmissionReceipt.create(
                source,
                final_observation,
                request,
                executable,
                decision_packet_sha256=decision.decision_packet_sha256,
                reservation_id=decision.reservation_id,
                connection_generation=connection_generation,
            )
        pe[KEY_DB_PAPER_EXECUTABLE_ADMISSION] = executable.to_payload()
        pe[KEY_DB_PAPER_FINAL_ADMISSION_RECEIPT] = receipt.to_payload()
        result = {
            "ok": True,
            "quantity_shares": int(decision.quantity_shares),
            "structural_risk_usd": float(decision.structural_risk_usd),
            "gross_notional_usd": float(decision.gross_notional_usd),
            "buying_power_impact_usd": float(decision.buying_power_impact_usd),
            "decision_packet_sha256": decision.decision_packet_sha256,
            "reservation_id": str(decision.reservation_id),
            "request_sha256": request.request_sha256,
            "client_order_id": request.client_order_id,
            "account_scope": request.account_scope,
            "connection_generation": connection_generation,
            "policy_sha256": request.policy.policy_sha256,
            "account_identity_sha256": request.inputs.account_identity_sha256,
            "effective_config_sha256": request.inputs.effective_config_sha256,
            "feature_flags_sha256": request.inputs.feature_flags_sha256,
            "code_build_sha256": request.inputs.code_build_sha256,
            "capture_prefix_root_sha256": (
                request.inputs.capture_prefix_root_sha256
            ),
            "correlation_cluster_id": request.inputs.correlation_cluster_id,
            "fees_usd": float(executable.fees_usd),
            "effective_atr": float(executable.effective_atr),
            # In-memory only: the canonical fill uses this exact single
            # resolution to reconstruct every persisted executable field.  It
            # is deliberately not copied into risk_snapshot_json.
            "_prepared_resolution": built.resolution,
            "builder_audit": builder_audit,
        }
        result[KEY_DB_PAPER_EXECUTABLE_ADMISSION] = executable.to_payload()
        result["executable_admission_sha256"] = executable.content_sha256
        result[KEY_DB_PAPER_FINAL_ADMISSION_RECEIPT] = receipt.to_payload()
        result["final_admission_receipt_sha256"] = receipt.content_sha256
        return result
    except AdaptiveRiskBuilderError as exc:
        return {"ok": False, "reason": exc.reason}
    except (AdaptiveRiskContractError, AdaptiveReservationError, TypeError, ValueError):
        return {"ok": False, "reason": "adaptive_risk_request_invalid"}
    except Exception:
        _log.exception(
            "DB-paper adaptive reservation unavailable session=%s symbol=%s",
            getattr(sess, "id", None),
            getattr(sess, "symbol", None),
        )
        # This store participates in the caller's outer transaction.  An
        # unexpected persistence/programming failure must reach that owner so
        # it can roll back; returning a soft rejection could leave the Session
        # failed or conceal a partially executed critical path.
        raise


def _aware_db_utc(value: datetime | None, field: str) -> datetime:
    if value is None:
        raise AdaptiveRiskContractError(f"canonical DB-paper fill missing {field}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _adaptive_db_paper_request_from_pe(
    pe: dict[str, Any],
) -> Any:
    payload = pe.get(KEY_ADAPTIVE_RISK_REQUEST)
    if not isinstance(payload, dict):
        raise AdaptiveRiskContractError(
            "DB-paper adaptive request is missing at the canonical fill boundary"
        )
    return load_adaptive_risk_reservation_request(payload)


def _adaptive_lifecycle_event_id(
    reservation_id: uuid.UUID,
    *,
    remaining_open_quantity: int | None,
) -> str:
    if remaining_open_quantity is None:
        return f"db-paper-entry:{reservation_id}"
    return f"db-paper-position:{reservation_id}:remaining:{remaining_open_quantity}"


def _adaptive_lifecycle_marker(
    *,
    request: Any,
    reservation_id: uuid.UUID,
    decision_packet_sha256: str,
    lifecycle_event_id: str,
    cumulative_filled_quantity: int,
    remaining_open_quantity: int | None,
    connection_generation: str,
    marker_json: dict[str, Any] | None,
) -> dict[str, Any]:
    marker = dict(marker_json or {})
    marker.update(
        {
            "adaptive_risk_lifecycle_event_id": lifecycle_event_id,
            "adaptive_risk_reservation_id": str(reservation_id),
            "adaptive_risk_decision_packet_sha256": decision_packet_sha256,
            "adaptive_risk_client_order_id": request.client_order_id,
            "adaptive_risk_account_scope": request.account_scope,
            "adaptive_risk_connection_generation": connection_generation,
            "adaptive_risk_cumulative_fill_quantity": int(
                cumulative_filled_quantity
            ),
        }
    )
    if remaining_open_quantity is not None:
        marker["adaptive_risk_remaining_open_quantity"] = int(
            remaining_open_quantity
        )
    return marker


def _adaptive_fill_by_lifecycle_id(
    db: Session,
    lifecycle_event_id: str,
) -> TradingAutomationSimulatedFill | None:
    return (
        db.query(TradingAutomationSimulatedFill)
        .filter(
            TradingAutomationSimulatedFill.marker_json[
                "adaptive_risk_lifecycle_event_id"
            ].astext
            == lifecycle_event_id
        )
        .with_for_update()
        .one_or_none()
    )


def _require_exact_canonical_fill(
    row: TradingAutomationSimulatedFill,
    *,
    sess: TradingAutomationSession,
    action: str,
    fill_type: str,
    price: float,
    quantity: int,
    reference_price: float,
    fees_usd: float | None,
    pnl_usd: float | None,
    position_state_before: str,
    position_state_after: str,
    reason: str,
    marker: dict[str, Any],
    decision_packet_id: int | None,
) -> None:
    comparisons = {
        "session_id": (int(row.session_id), int(sess.id)),
        "symbol": (row.symbol, sess.symbol),
        "lane": (row.lane, "simulation"),
        "side": (row.side, "long"),
        "action": (row.action, action),
        "fill_type": (row.fill_type, fill_type),
        "price": (float(row.price), float(price)),
        "quantity": (float(row.quantity), float(quantity)),
        "reference_price": (float(row.reference_price), float(reference_price)),
        "fees_usd": (
            float(row.fees_usd) if row.fees_usd is not None else None,
            float(fees_usd) if fees_usd is not None else None,
        ),
        "pnl_usd": (
            float(row.pnl_usd) if row.pnl_usd is not None else None,
            float(pnl_usd) if pnl_usd is not None else None,
        ),
        "position_state_before": (row.position_state_before, position_state_before),
        "position_state_after": (row.position_state_after, position_state_after),
        "reason": (row.reason, reason),
        "marker_json": (dict(row.marker_json or {}), marker),
        "decision_packet_id": (row.decision_packet_id, decision_packet_id),
    }
    changed = sorted(
        name for name, (persisted, supplied) in comparisons.items() if persisted != supplied
    )
    if changed:
        raise AdaptiveReservationIdempotencyConflict(
            "canonical DB-paper lifecycle retry changed: " + ",".join(changed)
        )


def _durable_db_paper_fill_evidence(
    *,
    request: Any,
    row: TradingAutomationSimulatedFill,
    lifecycle_event_id: str,
    connection_generation: str,
    event_kind: str,
    order_status: str,
    cumulative_filled_quantity: int,
    remaining_open_quantity: int | None = None,
) -> DurableOrderLifecycleEvidence:
    return DurableOrderLifecycleEvidence(
        event_kind=event_kind,
        durability_kind="committed_db_paper_fill",
        provider_event_id=lifecycle_event_id,
        broker_source="db_paper",
        connection_generation=connection_generation,
        account_scope=request.account_scope,
        execution_family=request.inputs.execution_family,
        broker_environment=request.inputs.broker_environment,
        account_identity_sha256=request.inputs.account_identity_sha256,
        client_order_id=request.client_order_id,
        # CID is persisted separately and can legitimately occupy its full
        # VARCHAR(160).  Use the fixed-length content address for this synthetic
        # broker id so adding a prefix can never overflow broker_order_id.
        broker_order_id=f"db-paper-order:{request.request_sha256}",
        observed_at=_aware_db_utc(row.ts, "ts"),
        available_at=_aware_db_utc(row.created_at, "created_at"),
        event_content_sha256=canonical_db_paper_fill_content_sha256(row),
        cumulative_filled_quantity=int(cumulative_filled_quantity),
        remaining_open_quantity=remaining_open_quantity,
        source_record_table="trading_automation_simulated_fills",
        source_record_id=str(row.id),
        order_status=order_status,
    )


def _record_adaptive_db_paper_entry_fill(
    db: Session,
    sess: TradingAutomationSession,
    pe: dict[str, Any],
    admission: dict[str, Any],
    *,
    price: float,
    reference_price: float,
    fees_usd: float,
    stop_price: float,
    target_price: float,
    effective_atr: float,
    decision_packet_id: int | None,
) -> TradingAutomationSimulatedFill:
    """Create canonical entry fill and consume risk in one outer transaction."""

    request = _adaptive_db_paper_request_from_pe(pe)
    reservation_id = uuid.UUID(str(admission["reservation_id"]))
    quantity = int(admission["quantity_shares"])
    if float(admission["quantity_shares"]) != quantity or quantity <= 0:
        raise AdaptiveRiskContractError("DB-paper adaptive entry quantity is invalid")
    connection_generation = str(admission["connection_generation"])
    expected_connection_generation = f"db-paper-session:{int(sess.id)}"
    if connection_generation != expected_connection_generation:
        raise AdaptiveRiskContractError(
            "DB-paper connection generation changed before canonical fill"
        )
    lifecycle_event_id = _adaptive_lifecycle_event_id(
        reservation_id, remaining_open_quantity=None
    )
    marker = _adaptive_lifecycle_marker(
        request=request,
        reservation_id=reservation_id,
        decision_packet_sha256=str(admission["decision_packet_sha256"]),
        lifecycle_event_id=lifecycle_event_id,
        cumulative_filled_quantity=quantity,
        remaining_open_quantity=None,
        connection_generation=connection_generation,
        marker_json={"entry": price, "stop": stop_price, "target": target_price},
    )
    receipt_payload = admission.get(KEY_DB_PAPER_FINAL_ADMISSION_RECEIPT)
    if not isinstance(receipt_payload, dict):
        raise AdaptiveRiskContractError(
            "DB-paper canonical entry requires final admission receipt"
        )
    receipt = load_db_paper_admission_receipt(receipt_payload)
    executable_payload = admission.get(KEY_DB_PAPER_EXECUTABLE_ADMISSION)
    persisted_executable_payload = pe.get(KEY_DB_PAPER_EXECUTABLE_ADMISSION)
    if not isinstance(executable_payload, dict) or not isinstance(
        persisted_executable_payload, dict
    ):
        raise AdaptiveRiskContractError(
            "DB-paper canonical entry requires executable admission"
        )
    executable = load_db_paper_executable_admission(executable_payload)
    persisted_executable = load_db_paper_executable_admission(
        persisted_executable_payload
    )
    if persisted_executable != executable:
        raise AdaptiveRiskContractError(
            "DB-paper executable admission changed before canonical fill"
        )
    source_payload = pe.get(KEY_ADAPTIVE_RISK_BUILDER_SOURCE)
    observation_payload = pe.get("adaptive_risk_final_observation")
    bundle_payload = pe.get("adaptive_risk_final_admission_bundle")
    if (
        not isinstance(source_payload, dict)
        or not isinstance(observation_payload, dict)
        or not isinstance(bundle_payload, dict)
    ):
        raise AdaptiveRiskContractError(
            "DB-paper canonical entry missing final bundle binding"
        )
    observation = load_db_paper_final_admission_observation(observation_payload)
    bundle = load_db_paper_final_admission_bundle(bundle_payload)
    source = load_adaptive_risk_builder_source(source_payload)
    prepared_resolution = admission.get("_prepared_resolution")
    if not isinstance(prepared_resolution, ResolvedAdaptiveRisk):
        raise AdaptiveRiskContractError(
            "DB-paper canonical entry lost its prepared resolution"
        )
    expected_executable = DbPaperExecutableAdmission.create(
        bundle,
        observation,
        request,
        prepared_resolution,
        reservation_id=reservation_id,
        structural_risk_usd=float(admission["structural_risk_usd"]),
        gross_notional_usd=float(admission["gross_notional_usd"]),
        buying_power_impact_usd=float(
            admission["buying_power_impact_usd"]
        ),
        entry_price=float(price),
        reference_price=float(reference_price),
        stop_price=float(stop_price),
        target_price=float(target_price),
        fees_usd=float(fees_usd),
        effective_atr=float(effective_atr),
    )
    if executable.content_sha256 != expected_executable.content_sha256:
        raise AdaptiveRiskContractError(
            "DB-paper executable admission failed full canonical reconstruction"
        )
    expected_receipt = DbPaperAdmissionReceipt.create(
        source,
        observation,
        request,
        expected_executable,
        decision_packet_sha256=prepared_resolution.decision_packet_sha256,
        reservation_id=reservation_id,
        connection_generation=expected_connection_generation,
    )
    if receipt.content_sha256 != expected_receipt.content_sha256:
        raise AdaptiveRiskContractError(
            "DB-paper final receipt failed full canonical reconstruction"
        )
    exact_receipt = {
        "reservation_id": (receipt.reservation_id, str(reservation_id)),
        "request_sha256": (receipt.request_sha256, request.request_sha256),
        "decision_packet_sha256": (
            receipt.decision_packet_sha256,
            str(admission["decision_packet_sha256"]),
        ),
        "connection_generation": (
            receipt.connection_generation,
            connection_generation,
        ),
        "source_sha256": (
            receipt.source_sha256,
            source_payload.get("source_sha256"),
        ),
        "final_observation_sha256": (
            receipt.final_observation_sha256,
            observation.content_sha256,
        ),
        "final_bundle_sha256": (
            receipt.final_bundle_sha256,
            bundle.content_sha256,
        ),
        "locked_risk_snapshot_sha256": (
            receipt.locked_risk_snapshot_sha256,
            bundle.locked_risk_snapshot.get("content_sha256"),
        ),
        "executable_admission_sha256": (
            receipt.executable_admission_sha256,
            executable.content_sha256,
        ),
        "opportunity_sha256": (
            receipt.opportunity_sha256,
            observation.opportunity_sha256,
        ),
        "client_order_id": (receipt.client_order_id, request.client_order_id),
        "account_scope": (receipt.account_scope, request.account_scope),
        "account_identity_sha256": (
            receipt.account_identity_sha256,
            request.inputs.account_identity_sha256,
        ),
        "broker_environment": (
            receipt.broker_environment,
            request.inputs.broker_environment,
        ),
        "execution_family": (
            receipt.execution_family,
            request.inputs.execution_family,
        ),
        "venue": (receipt.venue, request.inputs.venue),
        "replay_or_paper_run_id": (
            receipt.replay_or_paper_run_id,
            request.inputs.replay_or_paper_run_id,
        ),
        "generation": (receipt.generation, request.inputs.generation),
        "decision_id": (receipt.decision_id, request.inputs.decision_id),
        "decision_at": (receipt.decision_at, request.inputs.as_of),
    }
    changed = sorted(
        name
        for name, (persisted, expected) in exact_receipt.items()
        if persisted != expected
    )
    if changed:
        raise AdaptiveRiskContractError(
            "DB-paper final admission receipt differs from canonical fill: "
            + ",".join(changed)
        )
    exact_executable = {
        "reservation_id": (executable.reservation_id, str(reservation_id)),
        "request_sha256": (executable.request_sha256, request.request_sha256),
        "decision_packet_sha256": (
            executable.decision_packet_sha256,
            str(admission["decision_packet_sha256"]),
        ),
        "source_sha256": (
            executable.source_sha256,
            source_payload.get("source_sha256"),
        ),
        "final_observation_sha256": (
            executable.final_observation_sha256,
            observation.content_sha256,
        ),
        "final_bundle_sha256": (
            executable.final_bundle_sha256,
            bundle.content_sha256,
        ),
        "locked_risk_snapshot_sha256": (
            executable.locked_risk_snapshot_sha256,
            bundle.locked_risk_snapshot.get("content_sha256"),
        ),
        "reservation_ledger_sha256": (
            executable.reservation_ledger_sha256,
            bundle.locked_risk_snapshot.get("ledger_sha256"),
        ),
        "account_scope": (executable.account_scope, request.account_scope),
        "account_identity_sha256": (
            executable.account_identity_sha256,
            request.inputs.account_identity_sha256,
        ),
        "symbol": (executable.symbol, request.inputs.symbol),
        "setup_family": (executable.setup_family, request.setup_family),
        "quantity_shares": (executable.quantity_shares, quantity),
        "entry_price": (executable.entry_price, float(price)),
        "reference_price": (
            executable.reference_price,
            float(reference_price),
        ),
        "stop_price": (executable.stop_price, float(stop_price)),
        "target_price": (executable.target_price, float(target_price)),
        "fees_usd": (executable.fees_usd, float(fees_usd)),
    }
    executable_changed = sorted(
        name
        for name, (approved, supplied) in exact_executable.items()
        if (
            approved != supplied
            if isinstance(approved, (str, int))
            else not _same_money(approved, supplied)
        )
    )
    if executable_changed:
        raise AdaptiveRiskContractError(
            "DB-paper executable admission differs from canonical fill: "
            + ",".join(executable_changed)
        )
    marker["adaptive_risk_final_admission_receipt_sha256"] = (
        receipt.content_sha256
    )
    marker["adaptive_risk_executable_admission_sha256"] = (
        executable.content_sha256
    )
    store = AdaptiveRiskReservationStore(db.get_bind())
    state_before = store.lock_reservation(reservation_id, session=db)
    reservation_binding_changed = sorted(
        name
        for name, (actual, expected) in {
            "decision_packet_sha256": (
                state_before.decision_packet_sha256,
                executable.decision_packet_sha256,
            ),
            "account_scope": (state_before.account_scope, executable.account_scope),
            "symbol": (state_before.symbol, executable.symbol),
            "setup_family": (state_before.setup_family, executable.setup_family),
            "correlation_cluster": (
                state_before.correlation_cluster,
                request.correlation_cluster,
            ),
            "planned_quantity_shares": (
                state_before.planned_quantity_shares,
                executable.quantity_shares,
            ),
        }.items()
        if actual != expected
    )
    if reservation_binding_changed:
        raise AdaptiveRiskContractError(
            "canonical entry differs from the locked reservation: "
            + ",".join(reservation_binding_changed)
        )
    with db.begin_nested():
        row = _adaptive_fill_by_lifecycle_id(db, lifecycle_event_id)
        if row is None:
            row = _record_sim_fill(
                db,
                sess,
                action="enter_long",
                fill_type="entry",
                price=price,
                quantity=quantity,
                reference_price=reference_price,
                fees_usd=fees_usd,
                position_state_before="flat",
                position_state_after="long",
                reason="entry_fill",
                marker_json=marker,
                decision_packet_id=decision_packet_id,
                strict=True,
            )
            assert row is not None
        else:
            _require_exact_canonical_fill(
                row,
                sess=sess,
                action="enter_long",
                fill_type="entry",
                price=price,
                quantity=quantity,
                reference_price=reference_price,
                fees_usd=fees_usd,
                pnl_usd=None,
                position_state_before="flat",
                position_state_after="long",
                reason="entry_fill",
                marker=marker,
                decision_packet_id=decision_packet_id,
            )
        evidence = _durable_db_paper_fill_evidence(
            request=request,
            row=row,
            lifecycle_event_id=lifecycle_event_id,
            connection_generation=connection_generation,
            event_kind="cumulative_fill",
            order_status="filled",
            cumulative_filled_quantity=quantity,
        )
        state = store.apply_cumulative_fill(
            reservation_id,
            evidence=evidence,
            session=db,
        )
        if (
            state.state != "filled"
            or state.pending_structural_risk_usd != 0
            or state.open_quantity_shares != quantity
        ):
            raise AdaptiveReservationError(
                "DB-paper entry fill did not atomically open its reservation"
            )
    return row


def _record_db_paper_position_fill(
    db: Session,
    sess: TradingAutomationSession,
    pe: dict[str, Any],
    *,
    action: str,
    price: float,
    quantity: float,
    remaining_open_quantity: float,
    reference_price: float,
    pnl_usd: float,
    reason: str,
    marker_json: dict[str, Any],
    decision_packet_id: int | None,
) -> TradingAutomationSimulatedFill:
    if pe.get("adaptive_risk_reservation_id"):
        return _record_adaptive_db_paper_position_fill(
            db,
            sess,
            pe,
            action=action,
            price=price,
            quantity=quantity,
            remaining_open_quantity=remaining_open_quantity,
            reference_price=reference_price,
            pnl_usd=pnl_usd,
            reason=reason,
            marker_json=marker_json,
            decision_packet_id=decision_packet_id,
        )
    row = _record_sim_fill(
        db,
        sess,
        action=action,
        fill_type="exit",
        price=price,
        quantity=quantity,
        reference_price=reference_price,
        pnl_usd=pnl_usd,
        position_state_before="long",
        position_state_after=("flat" if remaining_open_quantity == 0 else "long"),
        reason=reason,
        marker_json=marker_json,
        decision_packet_id=decision_packet_id,
        strict=True,
    )
    assert row is not None
    return row


def _record_adaptive_db_paper_position_fill(
    db: Session,
    sess: TradingAutomationSession,
    pe: dict[str, Any],
    *,
    action: str,
    price: float,
    quantity: float,
    remaining_open_quantity: float,
    reference_price: float,
    pnl_usd: float,
    reason: str,
    marker_json: dict[str, Any],
    decision_packet_id: int | None,
) -> TradingAutomationSimulatedFill:
    """Persist a partial/flat fill and update heat in the same transaction."""

    request = _adaptive_db_paper_request_from_pe(pe)
    reservation_id = uuid.UUID(str(pe["adaptive_risk_reservation_id"]))
    exit_quantity = int(quantity)
    remaining = int(remaining_open_quantity)
    if float(quantity) != exit_quantity or float(remaining_open_quantity) != remaining:
        raise AdaptiveRiskContractError(
            "DB-paper adaptive position quantities must be whole shares"
        )
    if exit_quantity <= 0 or remaining < 0:
        raise AdaptiveRiskContractError("DB-paper adaptive position quantity is invalid")
    connection_generation = str(
        pe.get("adaptive_risk_connection_generation")
        or f"db-paper-session:{int(sess.id)}"
    )
    store = AdaptiveRiskReservationStore(db.get_bind())
    locked = store.lock_reservation(reservation_id, session=db)
    cumulative = int(locked.cumulative_filled_quantity_shares)
    lifecycle_event_id = _adaptive_lifecycle_event_id(
        reservation_id, remaining_open_quantity=remaining
    )
    marker = _adaptive_lifecycle_marker(
        request=request,
        reservation_id=reservation_id,
        decision_packet_sha256=locked.decision_packet_sha256,
        lifecycle_event_id=lifecycle_event_id,
        cumulative_filled_quantity=cumulative,
        remaining_open_quantity=remaining,
        connection_generation=connection_generation,
        marker_json=marker_json,
    )
    after = "flat" if remaining == 0 else "long"
    with db.begin_nested():
        row = _adaptive_fill_by_lifecycle_id(db, lifecycle_event_id)
        if row is None:
            if int(locked.open_quantity_shares) - remaining != exit_quantity:
                raise AdaptiveRiskContractError(
                    "DB-paper exit quantity differs from locked open quantity"
                )
            row = _record_sim_fill(
                db,
                sess,
                action=action,
                fill_type="exit",
                price=price,
                quantity=exit_quantity,
                reference_price=reference_price,
                pnl_usd=pnl_usd,
                position_state_before="long",
                position_state_after=after,
                reason=reason,
                marker_json=marker,
                decision_packet_id=decision_packet_id,
                strict=True,
            )
            assert row is not None
        else:
            _require_exact_canonical_fill(
                row,
                sess=sess,
                action=action,
                fill_type="exit",
                price=price,
                quantity=exit_quantity,
                reference_price=reference_price,
                fees_usd=None,
                pnl_usd=pnl_usd,
                position_state_before="long",
                position_state_after=after,
                reason=reason,
                marker=marker,
                decision_packet_id=decision_packet_id,
            )
        evidence = _durable_db_paper_fill_evidence(
            request=request,
            row=row,
            lifecycle_event_id=lifecycle_event_id,
            connection_generation=connection_generation,
            event_kind="position_flat" if remaining == 0 else "position_reduced",
            order_status="flat" if remaining == 0 else "partially_exited",
            cumulative_filled_quantity=cumulative,
            remaining_open_quantity=remaining,
        )
        if remaining == 0:
            state = store.close_open_exposure(
                reservation_id,
                evidence=evidence,
                reason=reason,
                session=db,
            )
            if state.state != "closed" or state.open_quantity_shares != 0:
                raise AdaptiveReservationError(
                    "DB-paper flat fill did not atomically close its reservation"
                )
        else:
            state = store.reduce_open_exposure(
                reservation_id,
                evidence=evidence,
                reason=reason,
                session=db,
            )
            if state.state == "closed" or state.open_quantity_shares != remaining:
                raise AdaptiveReservationError(
                    "DB-paper partial fill did not atomically reduce its reservation"
                )
    if remaining == 0:
        pe["adaptive_risk_reservation_closed"] = True
        pe["adaptive_risk_reservation_close_reason"] = str(reason)
    return row


def _close_adaptive_db_paper_exposure(
    db: Session,
    sess: TradingAutomationSession,
    pe: dict[str, Any],
    *,
    reason: str,
) -> bool:
    """Reconcile only from an already-canonical flat fill; never invent one."""

    reservation_id = pe.get("adaptive_risk_reservation_id")
    if not reservation_id:
        return False
    if pe.get("adaptive_risk_reservation_closed") is True:
        return True
    try:
        request = _adaptive_db_paper_request_from_pe(pe)
        reservation_uuid = uuid.UUID(str(reservation_id))
        store = AdaptiveRiskReservationStore(db.get_bind())
        locked = store.lock_reservation(reservation_uuid, session=db)
        lifecycle_event_id = _adaptive_lifecycle_event_id(
            reservation_uuid, remaining_open_quantity=0
        )
        row = _adaptive_fill_by_lifecycle_id(db, lifecycle_event_id)
        if row is None:
            raise AdaptiveReservationError(
                "canonical flat fill is unavailable for reconciliation"
            )
        evidence = _durable_db_paper_fill_evidence(
            request=request,
            row=row,
            lifecycle_event_id=lifecycle_event_id,
            connection_generation=str(
                pe.get("adaptive_risk_connection_generation")
                or f"db-paper-session:{int(sess.id)}"
            ),
            event_kind="position_flat",
            order_status="flat",
            cumulative_filled_quantity=int(
                locked.cumulative_filled_quantity_shares
            ),
            remaining_open_quantity=0,
        )
        state = store.close_open_exposure(
            reservation_uuid,
            evidence=evidence,
            reason=str(reason or "position_flat_confirmed"),
            session=db,
        )
        if state.state != "closed":
            raise AdaptiveReservationError(
                "DB-paper adaptive exposure did not close"
            )
        pe["adaptive_risk_reservation_closed"] = True
        pe["adaptive_risk_reservation_close_reason"] = str(reason)
        return True
    except (AdaptiveRiskContractError, AdaptiveReservationError, TypeError, ValueError):
        pe["adaptive_risk_reconciliation_required"] = True
        pe["adaptive_risk_reconciliation_reason"] = "close_exposure_failed"
        return False
    except Exception:
        _log.exception(
            "DB-paper adaptive exposure close unavailable reservation=%s",
            reservation_id,
        )
        # Do not swallow an unexpected caller-transaction failure and then try
        # to flush more reconciliation state through the same Session.
        raise


def _emit(
    db: Session,
    sess: TradingAutomationSession,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    append_trading_automation_event(
        db,
        sess.id,
        event_type,
        payload,
        correlation_id=sess.correlation_id,
        source_node_id="momentum_paper_runner",
    )


def _sync_runtime_snapshot(
    db: Session,
    sess: TradingAutomationSession,
    *,
    via: MomentumSymbolViability | None = None,
) -> None:
    try:
        variant = (
            db.query(MomentumStrategyVariant)
            .filter(MomentumStrategyVariant.id == int(sess.variant_id))
            .one_or_none()
        )
        trade_count = int(
            db.query(TradingAutomationSimulatedFill)
            .filter(TradingAutomationSimulatedFill.session_id == int(sess.id))
            .count()
        )
        values = build_runtime_snapshot_values(
            sess,
            variant=variant,
            viability=via,
            trade_count=trade_count,
        )
        pe = (sess.risk_snapshot_json or {}).get(KEY_PAPER_EXEC) if isinstance(sess.risk_snapshot_json, dict) else {}
        quote_source = pe.get("last_quote_source") if isinstance(pe, dict) else None
        upsert_trading_automation_runtime_snapshot(db, session_id=int(sess.id), values=values)
        upsert_trading_automation_session_binding(
            db,
            session_id=int(sess.id),
            values=default_session_binding(
                venue=sess.venue,
                mode=sess.mode,
                execution_family=sess.execution_family,
                quote_source=quote_source,
            ),
        )
    except Exception:
        _log.debug("paper runtime snapshot sync skipped for session %s", sess.id, exc_info=True)


def _record_sim_fill(
    db: Session,
    sess: TradingAutomationSession,
    *,
    action: str,
    fill_type: str,
    price: float | None,
    quantity: float | None,
    reference_price: float | None = None,
    fees_usd: float | None = None,
    pnl_usd: float | None = None,
    position_state_before: str | None = None,
    position_state_after: str | None = None,
    reason: str | None = None,
    marker_json: Optional[dict[str, Any]] = None,
    decision_packet_id: int | None = None,
    strict: bool = False,
) -> TradingAutomationSimulatedFill | None:
    try:
        row = append_trading_automation_simulated_fill(
            db,
            session_id=int(sess.id),
            symbol=sess.symbol,
            lane="simulation",
            action=action,
            fill_type=fill_type,
            side="long",
            quantity=quantity,
            price=price,
            reference_price=reference_price,
            fees_usd=fees_usd,
            pnl_usd=pnl_usd,
            position_state_before=position_state_before,
            position_state_after=position_state_after,
            reason=reason,
            marker_json=marker_json,
            decision_packet_id=decision_packet_id,
        )
        _record_sim_fill_ledger_safe(
            db,
            sess,
            simulated_fill_id=int(row.id),
            fill_type=fill_type,
            quantity=quantity,
            price=price,
            pnl_usd=pnl_usd,
            position_state_after=position_state_after,
            reason=reason,
            marker_json=marker_json,
            decision_packet_id=decision_packet_id,
            strict=strict,
        )
        return row
    except Exception:
        if strict:
            raise
        _log.debug("paper simulated fill audit skipped for session %s", sess.id, exc_info=True)
        return None


def _scan_pattern_id_for_session(db: Session, sess: TradingAutomationSession) -> int | None:
    try:
        variant = variant_for_id(db, int(sess.variant_id))
        sid = getattr(variant, "scan_pattern_id", None) if variant is not None else None
        return int(sid) if sid is not None else None
    except Exception:
        return None


def _record_sim_fill_ledger_safe(
    db: Session,
    sess: TradingAutomationSession,
    *,
    simulated_fill_id: int,
    fill_type: str | None,
    quantity: float | None,
    price: float | None,
    pnl_usd: float | None,
    position_state_after: str | None,
    reason: str | None,
    marker_json: Optional[dict[str, Any]],
    decision_packet_id: int | None,
    strict: bool = False,
) -> None:
    """Mirror momentum paper fills into the canonical economic ledger."""
    try:
        from .. import economic_ledger as _ledger

        if not _ledger.mode_is_active():
            return
        marker = marker_json if isinstance(marker_json, dict) else {}
        mode = (sess.mode or "paper").lower()
        scan_pattern_id = _scan_pattern_id_for_session(db, sess)
        common = {
            "session_id": int(sess.id),
            "user_id": sess.user_id,
            "scan_pattern_id": scan_pattern_id,
            "ticker": sess.symbol,
            "quantity": float(quantity) if quantity is not None else 0.0,
            "fill_price": float(price) if price is not None else 0.0,
            "venue": sess.venue,
            "mode": mode,
            "decision_packet_id": decision_packet_id,
            "provenance": {
                "runner": "momentum_paper_runner",
                "simulated_fill_id": simulated_fill_id,
                "reason": reason,
            },
        }
        if fill_type == "entry" and position_state_after == "long":
            _ledger.record_automation_session_entry_fill(db, fee=0.0, **common)
            return
        if fill_type != "exit":
            return
        entry = marker.get("entry")
        if entry is None:
            return
        if position_state_after == "long":
            _ledger.record_automation_session_partial_exit_fill(
                db,
                entry_price=float(entry),
                realized_pnl_usd=pnl_usd,
                **common,
            )
            return
        if position_state_after != "flat":
            return
        _ledger.record_automation_session_exit_fill(
            db,
            entry_price=float(entry),
            realized_pnl_usd=pnl_usd,
            **common,
        )
        # The fill row is flushed before this hook.  Derive cumulative paper
        # P&L from canonical exit rows inside the same transaction rather than
        # from ``pe``: strict exits intentionally persist their row before
        # mutating the runner snapshot, and a snapshot read here would omit the
        # just-recorded final leg.
        cumulative = (
            db.query(
                func.coalesce(
                    func.sum(TradingAutomationSimulatedFill.pnl_usd),
                    0.0,
                )
            )
            .filter(
                TradingAutomationSimulatedFill.session_id == int(sess.id),
                TradingAutomationSimulatedFill.fill_type == "exit",
            )
            .scalar()
        )
        _ledger.reconcile_automation_session(
            db,
            session_id=int(sess.id),
            user_id=sess.user_id,
            scan_pattern_id=scan_pattern_id,
            ticker=sess.symbol,
            legacy_pnl=float(cumulative),
            mode=mode,
            provenance={
                "runner": "momentum_paper_runner",
                "simulated_fill_id": simulated_fill_id,
                "reason": reason,
            },
        )
    except Exception:
        if strict:
            raise
        _log.debug("paper economic ledger automation hook skipped for session %s", sess.id, exc_info=True)


def _record_paper_exit_basis(
    pe: dict[str, Any],
    *,
    quantity: float,
    entry_price: float,
    exit_price: float,
    pnl_usd: float,
    reason: str,
) -> None:
    try:
        qty = float(quantity)
        entry = float(entry_price)
        exit_px = float(exit_price)
        pnl = float(pnl_usd)
    except (TypeError, ValueError):
        return
    notional_basis = abs(entry * qty)
    pe["last_exit_quantity"] = qty
    pe["last_exit_entry_price"] = entry
    pe["last_exit_price"] = exit_px
    pe["last_exit_notional_basis_usd"] = notional_basis
    pe["last_exit_return_bps"] = (pnl / notional_basis) * 10_000.0 if notional_basis > 1e-12 else None
    pe["last_exit_reason"] = reason


def _finalize_paper_decision_after_exit(
    db: Session,
    sess: TradingAutomationSession,
    *,
    pe: dict[str, Any],
    realized_pnl_usd: float,
    slip_bps: float,
) -> None:
    pid = pe.get("last_entry_decision_packet_id")
    if not pid:
        return
    try:
        finalize_packet_after_simulated_exit(
            db,
            packet_id=int(pid),
            realized_pnl_usd=realized_pnl_usd,
            slippage_bps=slip_bps,
        )
        record_trade_outcome_metrics(
            db,
            session_id=int(sess.id),
            variant_id=int(sess.variant_id),
            user_id=sess.user_id,
            mode="paper",
            realized_pnl_usd=realized_pnl_usd,
            slippage_bps=slip_bps,
            missed_fill=False,
            partial_fill=False,
            cumulative_session_pnl_usd=float(pe.get("realized_pnl_usd") or 0.0),
        )
    except Exception:
        _log.debug("decision packet finalize skipped session=%s", sess.id, exc_info=True)


def _safe_transition(db: Session, sess: TradingAutomationSession, new_state: str) -> None:
    old = sess.state
    if old == new_state:
        return
    assert_transition(old, new_state)
    sess.state = new_state
    sess.updated_at = _utcnow()
    from .feedback_emit import emit_feedback_after_terminal_transition
    from .outcome_extract import session_terminal_for_feedback

    if session_terminal_for_feedback(sess.mode or "paper", new_state):
        emit_feedback_after_terminal_transition(db, sess)


def runner_boundary_risk_ok(
    db: Session,
    sess: TradingAutomationSession,
) -> tuple[bool, dict[str, Any]]:
    """Re-check policy at tick boundary; does not mutate snapshot."""
    if sess.user_id is None:
        return False, {"reason": "no_user"}
    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=int(sess.user_id),
        symbol=sess.symbol,
        variant_id=int(sess.variant_id),
        mode="paper",
        execution_family=normalize_execution_family(sess.execution_family),
        exclude_session_id=int(sess.id),
    )
    return bool(ev.get("allowed", False)), ev


def _default_quote_fn(symbol: str) -> dict[str, Any]:
    try:
        from ..market_data import fetch_quote

        q = fetch_quote(symbol)
    except Exception as ex:
        _log.debug("paper_runner quote fetch failed %s: %s", symbol, ex)
        return {}
    if not q:
        return {}
    mid = q.get("price")
    try:
        mf = float(mid) if mid is not None else 0.0
    except (TypeError, ValueError):
        mf = 0.0
    if mf <= 0:
        return {}
    return {"mid": mf, "bid": q.get("bid"), "ask": q.get("ask"), "source": "fetch_quote"}


def _resolve_quote(
    symbol: str,
    spread_bps: float,
    quote_fn: Optional[QuoteFn],
    *,
    raw_quote: dict[str, Any] | None = None,
) -> tuple[float, float, float, str]:
    if raw_quote is None:
        fn = quote_fn or _default_quote_fn
        raw = fn(symbol) or {}
    else:
        raw = dict(raw_quote)
    mid = raw.get("mid")
    try:
        mid_f = float(mid) if mid is not None else 0.0
    except (TypeError, ValueError):
        mid_f = 0.0
    bid_r = raw.get("bid")
    ask_r = raw.get("ask")
    try:
        bid_f = float(bid_r) if bid_r is not None else 0.0
        ask_f = float(ask_r) if ask_r is not None else 0.0
    except (TypeError, ValueError):
        bid_f = ask_f = 0.0
    if mid_f > 0 and bid_f > 0 and ask_f > 0:
        return bid_f, ask_f, mid_f, str(raw.get("source") or "quote")
    if mid_f > 0:
        # mid known but one side missing: synthesize the spread around the REAL mid
        syn = build_synthetic_quote(mid_f, spread_bps, source="synthetic_spread")
        return syn.bid, syn.ask, syn.mid, syn.source
    # NO REAL PRICE AT ALL: never fabricate one. The old $100.0 placeholder
    # "filled" a ROBO-USD ($0.022) partial exit at $99.84 and minted +$555,963
    # of fiction into realized PnL (2026-06-12). Zeros = the tick SKIPS.
    return 0.0, 0.0, 0.0, "quote_unavailable"


def list_runnable_paper_sessions(db: Session, *, limit: int = 25) -> list[TradingAutomationSession]:
    lim = max(1, min(int(limit), 200))
    rows = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.mode == "paper",
            TradingAutomationSession.state.in_(PAPER_RUNNER_RUNNABLE_STATES),
        )
        .order_by(TradingAutomationSession.updated_at.asc())
        .limit(lim)
        .all()
    )
    return [r for r in rows if not is_live_intent_state(r.state) and not is_operator_paused(r.risk_snapshot_json)]


def run_paper_runner_batch(
    db: Session,
    *,
    limit: int = 25,
    quote_fn: Optional[QuoteFn] = None,
) -> list[dict[str, Any]]:
    """Scheduler/worker entry: tick several paper sessions."""
    out: list[dict[str, Any]] = []
    for sess in list_runnable_paper_sessions(db, limit=limit):
        try:
            out.append(tick_paper_session(db, int(sess.id), quote_fn=quote_fn))
        except Exception:
            _log.warning("[paper_runner] tick failed session=%s", sess.id, exc_info=True)
            out.append({"ok": False, "session_id": sess.id, "error": "tick_exception"})
    return out


def tick_paper_session(
    db: Session,
    session_id: int,
    *,
    quote_fn: Optional[QuoteFn] = None,
) -> dict[str, Any]:
    """Advance one paper automation session by one step."""
    if not settings.chili_momentum_paper_runner_enabled:
        return {"ok": True, "skipped": "paper_runner_disabled"}

    try:
        sess = (
            db.query(TradingAutomationSession)
            .filter(
                TradingAutomationSession.id == int(session_id),
                TradingAutomationSession.mode == "paper",
            )
            .with_for_update(nowait=True)
            .one_or_none()
        )
    except Exception:
        return {"ok": True, "skipped": "concurrent_tick"}
    if sess is None:
        return {"ok": False, "error": "not_found"}
    if is_live_intent_state(sess.state):
        return {"ok": True, "skipped": "live_intent_session"}
    if sess.state not in PAPER_RUNNER_RUNNABLE_STATES:
        return {"ok": True, "skipped": "not_runnable", "state": sess.state}
    if is_operator_paused(sess.risk_snapshot_json):
        return {"ok": True, "skipped": "operator_paused", "state": sess.state}

    ef = normalize_execution_family(sess.execution_family)
    if not momentum_runner_supports_execution_family(ef):
        return {"ok": True, "skipped": "execution_family_not_implemented", "execution_family": ef}

    snap = dict(sess.risk_snapshot_json or {})
    if RISK_SNAPSHOT_KEY not in snap:
        _emit(
            db,
            sess,
            "paper_error",
            {"reason": "missing_frozen_risk_snapshot", "hint": "admit_session_without_risk"},
        )
        _safe_transition(db, sess, STATE_ERROR)
        db.flush()
        return {"ok": False, "error": "missing_risk_snapshot"}

    via = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sess.symbol,
            MomentumSymbolViability.variant_id == int(sess.variant_id),
        )
        .one_or_none()
    )
    if not via:
        _emit(db, sess, "paper_error", {"reason": "viability_missing"})
        _safe_transition(db, sess, STATE_ERROR)
        db.flush()
        return {"ok": False, "error": "no_viability"}

    variant = variant_for_id(db, int(sess.variant_id))
    params = normalize_strategy_params(
        variant.params_json if variant is not None else {},
        family_id=variant.family if variant is not None else None,
    )

    spread_bps, slip_bps, fee_ratio = _execution_readiness_costs(via.execution_readiness_json)

    caps = _policy_caps(snap)
    try:
        cap_max_hold = int(caps.get("max_hold_seconds") or settings.chili_momentum_risk_max_hold_seconds)
    except (TypeError, ValueError):
        cap_max_hold = int(settings.chili_momentum_risk_max_hold_seconds)
    max_hold = min(int(params.get("max_hold_seconds") or cap_max_hold), cap_max_hold)
    max_notional = policy_float_cap(
        caps,
        "max_notional_per_trade_usd",
        settings.chili_momentum_risk_max_notional_per_trade_usd,
    )

    try:
        max_age_sec = float(getattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0)
    except (TypeError, ValueError):
        max_age_sec = 600.0

    raw_quote = (quote_fn or _default_quote_fn)(sess.symbol) or {}
    try:
        qmid = float(raw_quote["mid"]) if raw_quote.get("mid") is not None else None
    except (TypeError, ValueError):
        qmid = None
    ref_mid = default_reference_mid(
        viability_score=float(via.viability_score or 0.0),
        symbol=sess.symbol,
        quote_mid=qmid,
    )
    bid, ask, mid, quote_src = _resolve_quote(
        sess.symbol, spread_bps, quote_fn, raw_quote=raw_quote
    )
    # QUOTE SANITY (2026-06-12 ROBO +$555k fiction): no price -> no tick; and a
    # mid that JUMPED beyond the guard fraction vs the session's own last mid in
    # ONE tick is vendor garbage, not a market — quarantine the tick (skip, keep
    # state). Real explosive moves arrive across ticks; the guard fraction is
    # ONE documented knob.
    if mid <= 0:
        _emit(db, sess, "paper_quote_unavailable", {"source": quote_src})
        db.flush()
        return {"ok": True, "skipped": "quote_unavailable"}
    _last_mid_guard = None
    try:
        _last_mid_guard = float(_paper_exec(dict(sess.risk_snapshot_json or {})).get("last_mid") or 0.0)
    except (TypeError, ValueError):
        _last_mid_guard = None
    if _last_mid_guard and _last_mid_guard > 0:
        _jump = abs(mid - _last_mid_guard) / _last_mid_guard
        _jump_cap = float(getattr(settings, "chili_momentum_paper_quote_jump_guard_frac", 0.5) or 0.5)
        if _jump_cap > 0 and _jump > _jump_cap:
            _emit(db, sess, "paper_quote_quarantined", {
                "mid": mid, "last_mid": _last_mid_guard,
                "jump_frac": round(_jump, 4), "guard_frac": _jump_cap, "source": quote_src,
            })
            db.flush()
            return {"ok": True, "skipped": "quote_quarantined"}

    ok_boundary, ev = runner_boundary_risk_ok(db, sess)
    if not ok_boundary:
        _emit(
            db,
            sess,
            "paper_blocked_by_risk",
            {
                "severity": ev.get("severity"),
                "errors": ev.get("errors"),
                "evaluated_at_utc": ev.get("evaluated_at_utc"),
            },
        )
        if sess.state == STATE_QUEUED:
            _safe_transition(db, sess, STATE_ERROR)
        elif sess.state == STATE_ENTERED and _paper_exec(snap).get("position"):
            pe = _paper_exec(snap)
            pos = pe.get("position")
            if isinstance(pos, dict):
                entry = float(pos["entry_price"])
                qty = float(pos["quantity"])
                exit_px = long_exit_fill_price(bid, mid, slip_bps)
                pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
                dpid = pe.get("last_entry_decision_packet_id")
                _record_db_paper_position_fill(
                    db,
                    sess,
                    pe,
                    action="forced_exit",
                    price=exit_px,
                    quantity=qty,
                    remaining_open_quantity=0,
                    reference_price=mid,
                    pnl_usd=pnl,
                    reason="risk_block_forced_exit",
                    marker_json={
                        "entry": entry,
                        "stop": pos.get("stop_price"),
                        "target": pos.get("target_price"),
                    },
                    decision_packet_id=int(dpid) if dpid else None,
                )
                pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
                _record_paper_exit_basis(
                    pe,
                    quantity=qty,
                    entry_price=entry,
                    exit_price=exit_px,
                    pnl_usd=pnl,
                    reason="risk_block_forced_exit",
                )
                pe["position"] = None
                _finalize_paper_decision_after_exit(db, sess, pe=pe, realized_pnl_usd=pnl, slip_bps=slip_bps)
            pe["last_tick_utc"] = utc_iso()
            _commit_pe(sess, pe)
            _safe_transition(db, sess, STATE_EXITED)
            _emit(
                db,
                sess,
                "paper_exit_filled",
                {"reason": "risk_block", "price": pe.get("last_exit_price")},
            )
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "blocked": True, "risk_evaluation": ev}

    pe = _paper_exec(snap)
    pe["tick_count"] = int(pe.get("tick_count") or 0) + 1
    pe["last_mid"] = mid
    pe["last_quote_source"] = quote_src
    pe["last_tick_utc"] = utc_iso()
    _commit_pe(sess, pe)
    snap = dict(sess.risk_snapshot_json or {})
    pe = _paper_exec(snap)

    st = sess.state

    if st == STATE_QUEUED:
        _safe_transition(db, sess, STATE_WATCHING)
        _emit(db, sess, "paper_runner_started", {"symbol": sess.symbol, "variant_id": sess.variant_id})
        _emit(db, sess, "paper_watch_started", {"mid": mid, "quote_source": quote_src})
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_WATCHING:
        if _via_entry_paused(via):
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state, "variant_pause": True}
        eff_v = _effective_viability(via, max_age_sec)
        if eff_v >= float(params["entry_viability_min"]) and via.paper_eligible:
            _safe_transition(db, sess, STATE_ENTRY_CANDIDATE)
            _emit(
                db,
                sess,
                "paper_entry_candidate_detected",
                {"viability_score": via.viability_score, "effective_viability": eff_v, "mid": mid},
            )
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_ENTRY_CANDIDATE:
        eff_v = _effective_viability(via, max_age_sec)
        if eff_v < float(params["entry_revalidate_floor"]) or not via.paper_eligible:
            _safe_transition(db, sess, STATE_WATCHING)
            _emit(db, sess, "paper_watch_started", {"reason": "candidate_regressed"})
        else:
            regime_pre = via.regime_snapshot_json if isinstance(via.regime_snapshot_json, dict) else {}
            _entry_gate_decision_at = _utcnow()
            ok_g, reason_g, dbg = run_paper_entry_gates(
                db,
                symbol=sess.symbol,
                variant=variant,
                regime_snapshot=regime_pre,
                family_id=variant.family if variant is not None else None,
                live_price=mid,
                decision_at=_entry_gate_decision_at,
            )
            if not ok_g:
                _safe_transition(db, sess, STATE_WATCHING)
                _emit(
                    db,
                    sess,
                    "paper_entry_gates_blocked",
                    {"reason": reason_g, "debug": dbg, "mid": mid},
                )
            else:
                # Stash the structural stop (pullback low) + breakout level from the
                # SHARED trigger so PENDING_ENTRY's stop mirrors live's structural stop
                # (parity). Cleared when the trigger had no structure.
                _pblow = dbg.get("pullback_low")
                if _pblow:
                    pe["structural_stop_price"] = float(_pblow)
                    _pbhigh = dbg.get("pullback_high")
                    if _pbhigh:
                        pe["breakout_level_price"] = float(_pbhigh)
                    else:
                        pe.pop("breakout_level_price", None)
                else:
                    pe.pop("structural_stop_price", None)
                    pe.pop("breakout_level_price", None)
                pe["entry_trigger_debug"] = dict(dbg or {})
                pe["entry_trigger_reason"] = str(reason_g or "")
                _commit_pe(sess, pe)
                _safe_transition(db, sess, STATE_PENDING_ENTRY)
                _emit(db, sess, "paper_entry_submitted",
                      {"mid": mid, "structural_stop": pe.get("structural_stop_price")})
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_PENDING_ENTRY:
        eff_v = _effective_viability(via, max_age_sec)
        if eff_v < float(params["entry_revalidate_floor"]) or not via.paper_eligible:
            _safe_transition(db, sess, STATE_WATCHING)
            _emit(db, sess, "paper_watch_started", {"reason": "entry_aborted"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        regime = via.regime_snapshot_json if isinstance(via.regime_snapshot_json, dict) else {}
        decision_packet_id: int | None = None
        if bool(getattr(settings, "brain_enable_decision_ledger", True)):
            dec = run_momentum_entry_decision(
                db,
                session=sess,
                viability=via,
                variant=variant,
                user_id=sess.user_id,
                max_notional_policy=float(max_notional),
                quote_mid=mid,
                spread_bps=spread_bps,
                execution_mode="paper",
                regime_snapshot=regime,
            )
            alloc = dec.get("allocation") or {}
            pe["legacy_pre_admission_audit"] = {
                "decision_packet_id": dec.get("packet_id"),
                "legacy_proceed": bool(dec.get("proceed")),
                "legacy_abstain_reason_code": alloc.get("abstain_reason_code"),
                "legacy_abstain_reason_text": alloc.get("abstain_reason_text"),
                "legacy_max_notional_policy_usd": float(max_notional),
                "suppression_authority": False,
                "economic_sizing_authority": "adaptive_risk_policy",
            }
            if not dec.get("proceed"):
                _emit(
                    db,
                    sess,
                    "paper_legacy_abstain_observed",
                    {
                        "packet_id": dec.get("packet_id"),
                        "reason": alloc.get("abstain_reason_code"),
                        "detail": alloc.get("abstain_reason_text"),
                        "suppression_authority": False,
                    },
                )
            decision_packet_id = dec.get("packet_id")
            # The legacy allocator remains selection/audit evidence only.  The
            # shared adaptive resolver below owns every economic sizing value.
        if bool(getattr(settings, "brain_decision_packet_required_for_runners", True)) and decision_packet_id is None:
            pe["decision_packet_provenance_gate"] = {
                "classification": "operational_provenance",
                "suppression_authority": True,
                "strategy_or_sizing_authority": False,
                "activation_blocker": True,
                "reason": "decision_packet_required_missing",
            }
            _commit_pe(sess, pe)
            _emit(
                db,
                sess,
                "paper_error",
                {
                    "reason": "decision_packet_required_missing",
                    "classification": "operational_provenance",
                    "strategy_or_sizing_authority": False,
                    "activation_blocker": True,
                },
            )
            _safe_transition(db, sess, STATE_ERROR)
            db.flush()
            return {"ok": False, "error": "decision_packet_missing"}
        # Stop PARITY with live: vol-floored + 0.15-capped ATR (effective_stop_atr_pct),
        # then the structural pullback stop (take the WIDER) — the SAME chain the live
        # runner uses, NOT the raw regime ATR. The 2:1 target auto-scales off the
        # actual stop distance. docs/DESIGN/MOMENTUM_LANE.md
        _sam = float(params["stop_atr_mult"])
        final_admission = _final_revalidate_adaptive_db_paper_entry(
            db,
            sess,
            pe,
            via=via,
            variant=variant,
            stop_atr_mult=_sam,
            target_atr_mult=float(params["target_atr_mult"]),
            vol_floor_mult=float(
                getattr(
                    settings,
                    "chili_momentum_risk_stop_vol_floor_mult",
                    0.5,
                )
                or 0.5
            ),
        )
        if not final_admission.get("ok"):
            final_reason = str(
                final_admission.get("reason")
                or "db_paper_final_admission_unavailable"
            )
            pe["adaptive_risk_runtime_ready"] = False
            pe["adaptive_risk_runtime_block_reason"] = final_reason
            pe["adaptive_risk_final_revalidation"] = {
                "allowed": False,
                "reason": final_reason,
                "detail": final_admission.get("detail"),
                "gate_reason": final_admission.get("gate_reason"),
                "decision_at": (
                    final_admission["decision_at"].isoformat()
                    if isinstance(final_admission.get("decision_at"), datetime)
                    else None
                ),
                "reservation_created": False,
                "opportunity_consumed": False,
            }
            _commit_pe(sess, pe)
            _safe_transition(db, sess, STATE_WATCHING)
            _emit(
                db,
                sess,
                "paper_entry_adaptive_risk_blocked",
                {
                    "reason": final_reason,
                    "gate_reason": final_admission.get("gate_reason"),
                    "reservation_created": False,
                    "opportunity_consumed": False,
                },
            )
            db.flush()
            return {
                "ok": True,
                "session_id": sess.id,
                "state": sess.state,
                "skipped": final_reason,
            }
        # From here to the canonical fill there is no market/provider read.
        # The tick's earlier BBO and ORM viability are no longer authoritative.
        via = final_admission["via"]
        bid = float(final_admission["bid"])
        ask = float(final_admission["ask"])
        mid = float(final_admission["mid"])
        quote_src = str(final_admission["quote_source"])
        entry_px = float(final_admission["entry_price"])
        stop_px = float(final_admission["stop_price"])
        target_px = float(final_admission["target_price"])
        _eff_atr = float(final_admission["effective_atr"])
        _stop_model = str(final_admission["stop_model"])
        slip_bps = float(final_admission["slippage_bps"])
        fee_ratio = float(final_admission["fee_ratio"])
        spread_bps = (
            ((ask - bid) / mid) * 10_000.0 if mid > 0.0 else float("inf")
        )
        ref_mid = mid
        setup_family = str(final_admission["setup_family"])
        pe["adaptive_risk_final_revalidation"] = {
            "allowed": True,
            "reason": "capture_bound_final_revalidation_passed",
            "gate_reason": final_admission.get("gate_reason"),
            "decision_at": final_admission["decision_at"].isoformat(),
            "observation_sha256": final_admission["observation"].content_sha256,
        }
        adaptive_admission = _reserve_adaptive_db_paper_entry(
            db,
            sess,
            pe,
            bid=float(final_admission["bid"]),
            ask=float(final_admission["ask"]),
            entry_price=entry_px,
            structural_stop=stop_px,
            setup_family=setup_family,
            builder_source=final_admission["source"],
            final_observation=final_admission["observation"],
            final_bundle=final_admission["bundle"],
            locked_snapshot=final_admission["locked_snapshot"],
            reference_price=ref_mid,
            target_price=target_px,
            effective_atr=_eff_atr,
            fee_ratio=fee_ratio,
        )
        if not adaptive_admission.get("ok"):
            pe["adaptive_risk_runtime_ready"] = False
            pe["adaptive_risk_runtime_block_reason"] = adaptive_admission.get(
                "reason"
            )
            if adaptive_admission.get("rejection_reasons"):
                pe["adaptive_risk_rejection_reasons"] = list(
                    adaptive_admission["rejection_reasons"]
                )
            _commit_pe(sess, pe)
            _emit(
                db,
                sess,
                "paper_error",
                {
                    "reason": adaptive_admission.get("reason"),
                    "adaptive_risk": adaptive_admission,
                },
            )
            _safe_transition(db, sess, STATE_ERROR)
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {
                "ok": False,
                "error": adaptive_admission.get("reason"),
                "adaptive_risk": adaptive_admission,
            }
        qty = float(adaptive_admission["quantity_shares"])
        notional = float(adaptive_admission["gross_notional_usd"])
        pe["adaptive_risk_runtime_ready"] = True
        pe["adaptive_risk_decision_packet_sha256"] = adaptive_admission[
            "decision_packet_sha256"
        ]
        pe["adaptive_risk_reservation_id"] = adaptive_admission[
            "reservation_id"
        ]
        pe["adaptive_risk_account_scope"] = adaptive_admission["account_scope"]
        pe["adaptive_risk_request_sha256"] = adaptive_admission[
            "request_sha256"
        ]
        pe["adaptive_risk_connection_generation"] = adaptive_admission[
            "connection_generation"
        ]
        pe["adaptive_risk_reservation_closed"] = False
        pe["adaptive_risk_resolved"] = {
            key: adaptive_admission[key]
            for key in (
                "quantity_shares",
                "structural_risk_usd",
                "gross_notional_usd",
                "buying_power_impact_usd",
                "policy_sha256",
                "account_identity_sha256",
                "effective_config_sha256",
                "feature_flags_sha256",
                "code_build_sha256",
                "capture_prefix_root_sha256",
                "correlation_cluster_id",
            )
        }
        # Fee economics were calculated inside the locked executable admission
        # and are no longer recomputed after reservation.
        fees = float(adaptive_admission["fees_usd"])
        opened = _utcnow()
        new_position = {
            "side": "long",
            "entry_price": entry_px,
            "quantity": qty,
            "original_quantity": qty,
            "notional_usd": notional,
            "opened_at_utc": opened.isoformat(),
            "stop_price": stop_px,
            "target_price": target_px,
            "spread_bps": spread_bps,
            "slippage_bps_used": slip_bps,
            "fee_to_target_ratio": fee_ratio,
            "fees_est_usd": fees,
            # Ross asymmetric exit: freeze the entry ATR-pct (the runner trail rides
            # the same ATR distance the initial stop used) + seed the high-water mark.
            # = the effective stop ATR-pct from the live-parity stop chain above
            # (matches the live runner's entry_stop_atr_pct).
            "entry_atr_pct": _eff_atr,
            "high_water_mark": entry_px,
        }
        try:
            _record_adaptive_db_paper_entry_fill(
                db,
                sess,
                pe,
                adaptive_admission,
                price=entry_px,
                reference_price=ref_mid,
                fees_usd=fees,
                stop_price=stop_px,
                target_price=target_px,
                effective_atr=_eff_atr,
                decision_packet_id=decision_packet_id,
            )
        except Exception:
            # The nested canonical-fill unit has rolled back.  DB paper has no
            # external order ambiguity, so the still-zero reservation can be
            # released in this same caller-owned transaction.
            AdaptiveRiskReservationStore(db.get_bind()).release_zero_fill(
                uuid.UUID(str(adaptive_admission["reservation_id"])),
                reason="pre_post_release",
                session=db,
            )
            pe["adaptive_risk_runtime_ready"] = False
            pe["adaptive_risk_runtime_block_reason"] = (
                "adaptive_canonical_entry_fill_failed"
            )
            _commit_pe(sess, pe)
            _safe_transition(db, sess, STATE_ERROR)
            _emit(
                db,
                sess,
                "paper_error",
                {"reason": "adaptive_canonical_entry_fill_failed"},
            )
            db.flush()
            return {
                "ok": False,
                "error": "adaptive_canonical_entry_fill_failed",
            }
        pe["adaptive_risk_request_consumed_sha256"] = adaptive_admission[
            "request_sha256"
        ]
        pe["position"] = new_position
        pe["entry_regime_snapshot_json"] = dict(regime)
        pe["reference_mid_at_entry"] = ref_mid
        pe["entry_quote_source"] = quote_src
        pe["last_entry_decision_packet_id"] = decision_packet_id
        _safe_transition(db, sess, STATE_ENTERED)
        _commit_pe(sess, pe)
        if decision_packet_id:
            try:
                mark_packet_executed(db, int(decision_packet_id))
            except Exception:
                _log.debug("mark_packet_executed skipped session=%s", sess.id, exc_info=True)
        _emit(
            db,
            sess,
            "paper_entry_filled",
            {
                "entry_price": entry_px,
                "qty": qty,
                "notional_usd": notional,
                "fees_est_usd": fees,
                "stop": stop_px,
                "target": target_px,
            },
        )
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st in (STATE_ENTERED, STATE_SCALING_OUT, STATE_TRAILING, STATE_BAILOUT):
        pos = pe.get("position")
        if not isinstance(pos, dict):
            _safe_transition(db, sess, STATE_ERROR)
            _emit(db, sess, "paper_error", {"reason": "position_missing"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": False, "error": "position_missing"}

        entry = float(pos["entry_price"])
        qty = float(pos["quantity"])
        stop_px = float(pos["stop_price"])
        target_px = float(pos["target_price"])
        exit_px = long_exit_fill_price(bid, mid, slip_bps)
        opened_at = pos.get("opened_at_utc")
        try:
            t0 = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            t0 = _utcnow()
        held = (_utcnow() - t0).total_seconds()
        regime_live = via.regime_snapshot_json if isinstance(via.regime_snapshot_json, dict) else {}
        atrp = regime_atr_pct(regime_live)
        mult_trail = 1.0 + min(0.5, max(-0.2, (atrp - 0.015) / 0.03))
        base_act = float(params["trail_activate_return_bps"]) * mult_trail
        trail_activate_return = 1.0 + base_act / 10_000.0

        # Ross runner: track the high-water mark (peak bid) for the chandelier trail.
        _hwm_prev = pos.get("high_water_mark")
        try:
            _hwm_prev_f = float(_hwm_prev) if _hwm_prev is not None else entry
        except (TypeError, ValueError):
            _hwm_prev_f = entry
        _hwm = max(_hwm_prev_f, float(bid))
        if _hwm_prev is None or _hwm > _hwm_prev_f:
            pos["high_water_mark"] = _hwm
            pe["position"] = pos
            _commit_pe(sess, pe)

        if st == STATE_BAILOUT:
            pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
            dpid = pe.get("last_entry_decision_packet_id")
            _record_db_paper_position_fill(
                db,
                sess,
                pe,
                action="exit_long",
                price=exit_px,
                quantity=qty,
                remaining_open_quantity=0,
                reference_price=mid,
                pnl_usd=pnl,
                reason="bailout",
                marker_json={"entry": entry, "stop": stop_px, "target": target_px},
                decision_packet_id=int(dpid) if dpid else None,
            )
            pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
            _record_paper_exit_basis(
                pe,
                quantity=qty,
                entry_price=entry,
                exit_price=exit_px,
                pnl_usd=pnl,
                reason="bailout",
            )
            pe["position"] = None
            _safe_transition(db, sess, STATE_EXITED)
            _commit_pe(sess, pe)
            _finalize_paper_decision_after_exit(db, sess, pe=pe, realized_pnl_usd=pnl, slip_bps=slip_bps)
            _emit(db, sess, "paper_exit_filled", {"price": exit_px, "pnl_usd": pnl, "reason": "bailout"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # Time-based stop tightening (no meaningful progress toward target)
        progress_mid = entry + 0.25 * (target_px - entry)
        progress_ok = mid >= progress_mid
        if held >= 0.5 * max_hold and not progress_ok:
            new_s = max(float(pos["stop_price"]), entry)
            if new_s > float(pos["stop_price"]):
                pos["stop_price"] = new_s
        if held >= 0.75 * max_hold and not progress_ok:
            new_s = max(float(pos["stop_price"]), entry * 1.0015)
            if new_s > float(pos["stop_price"]):
                pos["stop_price"] = new_s
        stop_px = float(pos["stop_price"])
        pe["position"] = pos
        _commit_pe(sess, pe)

        # Break of structure (last closed bar vs swing low).  Only market-data
        # evaluation is best-effort here.  Once an exit is selected, canonical
        # fill persistence and adaptive-ledger transition are strict and must
        # never be swallowed by this path.
        bos_triggered = False
        try:
            df_bos = fetch_ohlcv_df(sess.symbol, interval="15m", period="5d")
            if df_bos is not None and not df_bos.empty:
                last_close = float(df_bos["Close"].astype(float).iloc[-1])
                bos_triggered = bos_exit_triggered_long(
                    df_bos, current_close=last_close
                )
        except Exception:
            _log.debug(
                "paper_runner BOS evaluation skipped session=%s",
                sess.id,
                exc_info=True,
            )
        if bos_triggered:
            pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
            dpid = pe.get("last_entry_decision_packet_id")
            _record_db_paper_position_fill(
                db,
                sess,
                pe,
                action="exit_long",
                price=exit_px,
                quantity=qty,
                remaining_open_quantity=0,
                reference_price=mid,
                pnl_usd=pnl,
                reason="bos",
                marker_json={
                    "entry": entry,
                    "stop": stop_px,
                    "target": target_px,
                },
                decision_packet_id=int(dpid) if dpid else None,
            )
            pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
            _record_paper_exit_basis(
                pe,
                quantity=qty,
                entry_price=entry,
                exit_price=exit_px,
                pnl_usd=pnl,
                reason="bos",
            )
            pe["position"] = None
            _safe_transition(db, sess, STATE_EXITED)
            _commit_pe(sess, pe)
            _finalize_paper_decision_after_exit(
                db,
                sess,
                pe=pe,
                realized_pnl_usd=pnl,
                slip_bps=slip_bps,
            )
            _emit(
                db,
                sess,
                "paper_exit_filled",
                {"price": exit_px, "pnl_usd": pnl, "reason": "bos"},
            )
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # bailout: viability collapse
        eff_bail = _effective_viability(via, max_age_sec)
        if eff_bail < float(params["bailout_viability_floor"]):
            _safe_transition(db, sess, STATE_BAILOUT)
            _emit(
                db,
                sess,
                "paper_bailout",
                {"viability_score": via.viability_score, "effective_viability": eff_bail, "bid": bid},
            )
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if held >= max_hold:
            pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
            dpid = pe.get("last_entry_decision_packet_id")
            _record_db_paper_position_fill(
                db,
                sess,
                pe,
                action="exit_long",
                price=exit_px,
                quantity=qty,
                remaining_open_quantity=0,
                reference_price=mid,
                pnl_usd=pnl,
                reason="max_hold",
                marker_json={"entry": entry, "stop": stop_px, "target": target_px},
                decision_packet_id=int(dpid) if dpid else None,
            )
            pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
            _record_paper_exit_basis(
                pe,
                quantity=qty,
                entry_price=entry,
                exit_price=exit_px,
                pnl_usd=pnl,
                reason="max_hold",
            )
            pe["position"] = None
            _safe_transition(db, sess, STATE_EXITED)
            _commit_pe(sess, pe)
            _finalize_paper_decision_after_exit(db, sess, pe=pe, realized_pnl_usd=pnl, slip_bps=slip_bps)
            _emit(db, sess, "paper_exit_filled", {"price": exit_px, "pnl_usd": pnl, "reason": "max_hold"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # Ross runner trail: in TRAILING, ratchet the stop UP to a chandelier off the
        # high-water mark (same ATR distance the initial stop used), floored at
        # breakeven once the first-target partial de-risked the runner. The stop check
        # below enforces it SAME tick. Derived from the frozen entry ATR — not a
        # static floor. (docs/DESIGN/MOMENTUM_LANE.md)
        if st == STATE_TRAILING:
            _atr_pct_trail = pos.get("entry_atr_pct")
            try:
                _atr_pct_trail = float(_atr_pct_trail) if _atr_pct_trail is not None else atrp
            except (TypeError, ValueError):
                _atr_pct_trail = atrp
            _be_floor = entry if pos.get("partial_taken") else stop_px
            _sm = float(params.get("stop_atr_mult") or 0.60)
            _q0 = float(pos.get("original_quantity") or pos.get("quantity") or 0.0)
            # day_realized 0.0 in paper (no real account cushion) = tightest
            # patience — mirrors a fresh small account; the cushion dial still
            # widens with THIS position's unrealized R (parity with live).
            # 5m EMA9 structural anchor — same as the live caller (parity),
            # refreshed at most once per minute per session, fail-open.
            _ema5 = None
            try:
                from datetime import datetime as _dt, timezone as _tz

                _min_key = _dt.now(_tz.utc).strftime("%Y%m%d%H%M")
                if pe.get("ema5m_min") == _min_key:
                    _ema5 = pe.get("ema5m_val")
                    _ema5 = float(_ema5) if _ema5 is not None else None
                else:
                    from ..market_data import fetch_ohlcv_df as _e5_fetch

                    _df5 = _e5_fetch(sess.symbol, interval="5m", period="1d")
                    if _df5 is not None and len(_df5) >= 9:
                        _ema5 = float(_df5["Close"].ewm(span=9, adjust=False).mean().iloc[-1])
                    pe["ema5m_min"] = _min_key
                    pe["ema5m_val"] = _ema5
                    _commit_pe(sess, pe)
            except Exception:
                _ema5 = None
            _trailed = cushion_adaptive_trail_stop(
                high_water_mark=float(pos.get("high_water_mark") or entry),
                entry_price=float(entry),
                atr_pct=_atr_pct_trail,
                stop_atr_mult=_sm,
                day_realized_usd=0.0,
                position_risk_usd=(float(entry) * max(0.003, float(_atr_pct_trail or 0.0) * _sm)) * _q0,
                breakeven_floor=_be_floor,
                current_stop=stop_px,
                side_long=True,
                ema_5m=_ema5,
            )
            if _trailed > stop_px:
                pos["stop_price"] = _trailed
                stop_px = _trailed
                pe["position"] = pos
                _commit_pe(sess, pe)
                _emit(db, sess, "paper_trail_ratchet", {
                    "new_stop": _trailed,
                    "high_water_mark": pos.get("high_water_mark"),
                    "partial_taken": bool(pos.get("partial_taken")),
                })

        if exit_px <= stop_px:
            # A stop hit while TRAILING (or after the first-target partial) IS the
            # runner's trailing stop; before that it's the initial protective stop.
            _stop_reason = "trail_stop" if (st == STATE_TRAILING or pos.get("partial_taken")) else "stop"
            pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
            dpid = pe.get("last_entry_decision_packet_id")
            _record_db_paper_position_fill(
                db,
                sess,
                pe,
                action="exit_long",
                price=exit_px,
                quantity=qty,
                remaining_open_quantity=0,
                reference_price=mid,
                pnl_usd=pnl,
                reason=_stop_reason,
                marker_json={"entry": entry, "stop": stop_px, "target": target_px},
                decision_packet_id=int(dpid) if dpid else None,
            )
            pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
            _record_paper_exit_basis(
                pe,
                quantity=qty,
                entry_price=entry,
                exit_price=exit_px,
                pnl_usd=pnl,
                reason=_stop_reason,
            )
            pe["position"] = None
            _safe_transition(db, sess, STATE_EXITED)
            _commit_pe(sess, pe)
            _finalize_paper_decision_after_exit(db, sess, pe=pe, realized_pnl_usd=pnl, slip_bps=slip_bps)
            _emit(db, sess, "paper_exit_filled", {"price": exit_px, "pnl_usd": pnl, "reason": _stop_reason})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # First-target (2:1) reached and not yet scaled — take the Ross partial.
        # Fires from ENTERED or TRAILING (price drifted up past trail-activate before
        # reaching the target); the partial_taken guard ensures it fires once.
        if (
            st in (STATE_ENTERED, STATE_TRAILING)
            and not pos.get("partial_taken")
            and exit_px >= target_px * 0.995
        ):
            _safe_transition(db, sess, STATE_SCALING_OUT)
            _emit(db, sess, "paper_partial_exit", {"price": exit_px, "note": "target_zone"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_SCALING_OUT:
            # Ross asymmetric exit: sell `scale_out_fraction` of the ORIGINAL size into
            # the first (2:1) target, move the balance stop to breakeven, and HOLD the
            # runner (-> TRAILING). A position too small to leave a sellable runner is
            # flattened whole at target (the old flat exit). (docs/DESIGN/MOMENTUM_LANE.md)
            orig_qty = float(pos.get("original_quantity") or qty)
            frac = scale_out_fraction(symbol=sess.symbol)
            scale_qty, runner_qty, can_split = scale_out_quantity(
                current_qty=qty,
                original_qty=orig_qty,
                fraction=frac,
            )
            if can_split and not pos.get("partial_taken"):
                total_fees = float(pos.get("fees_est_usd") or 0.0)
                fee_part = total_fees * (scale_qty / orig_qty) if orig_qty > 0 else 0.0
                pnl_p = (exit_px - entry) * scale_qty - fee_part
                new_stop = breakeven_stop_after_partial(
                    entry, float(pos["stop_price"]), side_long=True
                )
                dpid = pe.get("last_entry_decision_packet_id")
                _record_db_paper_position_fill(
                    db,
                    sess,
                    pe,
                    action="exit_long",
                    price=exit_px,
                    quantity=scale_qty,
                    remaining_open_quantity=runner_qty,
                    reference_price=mid,
                    pnl_usd=pnl_p,
                    reason="scale_out_target",
                    marker_json={
                        "entry": entry,
                        "partial": True,
                        "runner_qty": runner_qty,
                        "breakeven_stop": new_stop,
                    },
                    decision_packet_id=int(dpid) if dpid else None,
                )
                pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl_p
                pos["fees_est_usd"] = max(0.0, total_fees - fee_part)
                pos["quantity"] = runner_qty
                pos["partial_taken"] = True
                pos["stop_price"] = new_stop
                pos["scaled_out_at_utc"] = utc_iso()
                pos["scale_out_fraction"] = frac
                pe["position"] = pos
                _safe_transition(db, sess, STATE_TRAILING)
                _commit_pe(sess, pe)
                _emit(
                    db,
                    sess,
                    "paper_scaled_out_to_runner",
                    {"qty": scale_qty, "runner_qty": runner_qty, "breakeven_stop": pos["stop_price"], "pnl_usd": pnl_p},
                )
                _sync_runtime_snapshot(db, sess, via=via)
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}
            # Un-splittable (tiny) position: flatten whole at target.
            pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
            dpid = pe.get("last_entry_decision_packet_id")
            _record_db_paper_position_fill(
                db,
                sess,
                pe,
                action="exit_long",
                price=exit_px,
                quantity=qty,
                remaining_open_quantity=0,
                reference_price=mid,
                pnl_usd=pnl,
                reason="target",
                marker_json={"entry": entry, "stop": stop_px, "target": target_px},
                decision_packet_id=int(dpid) if dpid else None,
            )
            pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
            _record_paper_exit_basis(
                pe,
                quantity=qty,
                entry_price=entry,
                exit_price=exit_px,
                pnl_usd=pnl,
                reason="target",
            )
            pe["position"] = None
            _safe_transition(db, sess, STATE_EXITED)
            _commit_pe(sess, pe)
            _finalize_paper_decision_after_exit(db, sess, pe=pe, realized_pnl_usd=pnl, slip_bps=slip_bps)
            _emit(db, sess, "paper_exit_filled", {"price": exit_px, "pnl_usd": pnl, "reason": "target"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_ENTERED and exit_px >= entry * trail_activate_return:
            _safe_transition(db, sess, STATE_TRAILING)
            _emit(db, sess, "paper_runner_started", {"note": "trail_armed", "bid": bid})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # TRAILING runs the chandelier ratchet above; the shared stop check enforces
        # the trailed stop. No dedicated static-floor trail exit remains.

        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_EXITED:
        if pe.get("adaptive_risk_reservation_id") and not _close_adaptive_db_paper_exposure(
            db, sess, pe, reason="position_flat_reconciliation"
        ):
            _commit_pe(sess, pe)
            _emit(
                db,
                sess,
                "paper_error",
                {"reason": "adaptive_risk_close_reconciliation_required"},
            )
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {
                "ok": False,
                "error": "adaptive_risk_close_reconciliation_required",
            }
        cd_sec = policy_int_cap(
            caps,
            "cooldown_after_stopout_seconds",
            settings.chili_momentum_risk_cooldown_after_stopout_seconds,
        )
        until = _utcnow() + timedelta(seconds=max(0, cd_sec))
        pe["cooldown_until_utc"] = until.isoformat()
        _safe_transition(db, sess, STATE_COOLDOWN)
        _commit_pe(sess, pe)
        _emit(db, sess, "paper_cooldown_started", {"until_utc": pe["cooldown_until_utc"]})
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_COOLDOWN:
        until_raw = pe.get("cooldown_until_utc")
        try:
            until = datetime.fromisoformat(str(until_raw).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            until = _utcnow()
        if _utcnow() >= until:
            pe.pop("cooldown_until_utc", None)
            pe["trade_cycles"] = int(pe.get("trade_cycles") or 0) + 1
            _commit_pe(sess, pe)
            _safe_transition(db, sess, STATE_WATCHING)
            _emit(db, sess, "paper_recycled", {
                "realized_pnl_usd": pe.get("realized_pnl_usd"),
                "trade_cycles": pe["trade_cycles"],
            })
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    _sync_runtime_snapshot(db, sess, via=via)
    db.flush()
    return {"ok": True, "session_id": sess.id, "state": sess.state}


def summarize_paper_execution(snap: Any) -> dict[str, Any]:
    """Read-model helper for API/UI."""
    if not isinstance(snap, dict):
        return {}
    pe = snap.get(KEY_PAPER_EXEC)
    if not isinstance(pe, dict):
        return {}
    pos = pe.get("position")
    out: dict[str, Any] = {
        "tick_count": pe.get("tick_count"),
        "last_tick_utc": pe.get("last_tick_utc"),
        "last_mid": pe.get("last_mid"),
        "last_quote_source": pe.get("last_quote_source"),
        "realized_pnl_usd": pe.get("realized_pnl_usd"),
        "last_exit_reason": pe.get("last_exit_reason"),
        "cooldown_until_utc": pe.get("cooldown_until_utc"),
    }
    if isinstance(pos, dict):
        out["in_position"] = True
        out["entry_price"] = pos.get("entry_price")
        out["quantity"] = pos.get("quantity")
        out["original_quantity"] = pos.get("original_quantity")
        out["notional_usd"] = pos.get("notional_usd")
        out["stop_price"] = pos.get("stop_price")
        out["target_price"] = pos.get("target_price")
        out["high_water_mark"] = pos.get("high_water_mark")
        # Ross asymmetric exit state: first-target partial taken yet + runner info.
        out["partial_taken"] = bool(pos.get("partial_taken"))
        out["scaled_out_at_utc"] = pos.get("scaled_out_at_utc")
        out["scale_out_fraction"] = pos.get("scale_out_fraction")
    else:
        out["in_position"] = False
    return out
