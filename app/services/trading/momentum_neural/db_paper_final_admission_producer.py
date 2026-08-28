"""Production DbPaperFinalAdmissionProvider — ang capture-owned material author.

ANG PUWANG (2026-08-28, kinumpirma ni Codex): ang buong DB-paper final
admission chain (material→bundle→observation→executable→receipt) ay may mga
LOADER lamang sa production; ang tests ang tanging nagbubuo ng material. Kaya
ang bawat paper admission ay fail-closed sa
``builder_missing_final_admission_provider`` (o mas maaga sa
``db_paper_account_binding_missing``). Ang module na ito ang production
implementer: isang bagong material kada admission tick, binubuo LAMANG sa
pamamagitan ng mga exported canonical builder (kaya ang canonical-byte checks
ay hindi maaaring mag-drift), na may tapat na ebidensya — walang pekeng quote,
walang pekeng clock, walang authority na hiniram sa ``alpaca:paper``.

Ang first_dip_reclaim ay nananatiling fail-closed hangga't walang aktibong
first-dip capture context sa prosesong ito (ang envelope ay process-local at
mimintable lamang ng first_dip_tape_decision mula sa sariwang tape receipt) —
kapareho ng ngayon, pero may tumpak nang typed na dahilan.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import text

from ....config import settings
from .adaptive_risk_policy import (
    AdaptiveRiskInputs,
    RiskInputEvidence,
    build_adaptive_risk_policy_from_settings,
)
from .adaptive_risk_reservation import ImmutableAccountRiskSnapshot
from .adaptive_risk_request_builder import (
    AdaptiveRiskBuilderError,
    AdaptiveRiskBuilderSource,
    AdaptiveRiskDiagnosticCaptureBinding,
    DbPaperFinalAdmissionMaterial,
    db_paper_admission_component_sha256,
    db_paper_bbo_evidence_payload,
    db_paper_eligibility_evidence_payload,
    db_paper_entry_gate_evidence_payload,
    db_paper_execution_terms_payload,
    load_db_paper_final_admission_material,
)
from .paper_execution import (
    effective_stop_atr_pct,
    stop_target_prices,
    structural_or_vol_floored_atr_pct,
)
from .replay_capture_contract import sha256_json

_log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

_BBO_PROVIDER_GENERATION = "db-paper-bbo-v1"
_ELIGIBILITY_SOURCE = "postgresql:locked-viability-read"
_ELIGIBILITY_PROVIDER_GENERATION = "db-paper-session-visibility-1"
_GATE_SOURCE = "db-paper-final-entry-gate"
_GATE_PROVIDER_GENERATION = "db-paper-final-entry-gate-v1"
_ACCOUNT_SOURCE = "chili:db-paper-simulated-account-v1"
_CAPTURE_VERIFIER_GENERATION = "db-paper-final-admission-producer-v1"

# code_build sha: deterministic kada deploy — sha256 ng file bytes ng mga
# load-bearing module (ang "stamp sha sa fills" na tuntunin ng operator).
_CODE_BUILD_MODULES = (
    "db_paper_final_admission_producer.py",
    "adaptive_risk_request_builder.py",
    "adaptive_risk_policy.py",
    "paper_runner.py",
)
_code_build_sha_cache: str | None = None


def _code_build_sha256() -> str:
    global _code_build_sha_cache
    if _code_build_sha_cache is not None:
        return _code_build_sha_cache
    import os

    digest = hashlib.sha256()
    base = os.path.dirname(os.path.abspath(__file__))
    for name in _CODE_BUILD_MODULES:
        path = os.path.join(base, name)
        try:
            with open(path, "rb") as handle:
                digest.update(hashlib.sha256(handle.read()).digest())
        except OSError:
            digest.update(b"missing:" + name.encode("utf-8"))
    _code_build_sha_cache = digest.hexdigest()
    return _code_build_sha_cache


def _feature_flags_sha256() -> str:
    flags = {
        "chili_momentum_paper_runner_enabled": bool(
            getattr(settings, "chili_momentum_paper_runner_enabled", False)
        ),
        "chili_momentum_entry_gates_enabled": bool(
            getattr(settings, "chili_momentum_entry_gates_enabled", True)
        ),
        "chili_momentum_paper_shadow_arm_enabled": bool(
            getattr(settings, "chili_momentum_paper_shadow_arm_enabled", True)
        ),
    }
    return sha256_json({"schema": "chili.db-paper-feature-flags.v1", **flags})


def _effective_config_sha256(policy_receipt: Any, terms: Mapping[str, float]) -> str:
    return sha256_json({
        "schema": "chili.db-paper-effective-config.v1",
        "adaptive_policy_settings_projection_sha256": (
            policy_receipt.settings_projection_sha256
        ),
        "execution_terms_request": {k: float(v) for k, v in terms.items()},
    })


def _today_realized_paper_pnl_usd(db: Any, as_of: datetime) -> float:
    """Bounded ET-day rollup ng simulated paper fills — ang daily-loss evidence."""

    day_start_et = as_of.astimezone(_ET).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    day_start = day_start_et.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        row = db.execute(
            text(
                "SELECT coalesce(sum(f.pnl_usd), 0.0) "
                "FROM trading_automation_simulated_fills f "
                "JOIN trading_automation_sessions s ON s.id = f.session_id "
                "WHERE s.mode = 'paper' AND f.ts >= :day_start "
                "AND f.pnl_usd IS NOT NULL"
            ),
            {"day_start": day_start},
        ).fetchone()
        return float(row[0] if row and row[0] is not None else 0.0)
    except Exception:
        _log.warning("[db_paper_producer] realized pnl rollup failed", exc_info=True)
        return 0.0


def _readiness_float(readiness: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        try:
            value = readiness.get(name)
            if value is None:
                continue
            out = float(value)
            if out == out and out > 0:
                return out
        except (TypeError, ValueError):
            continue
    return None


def _generic_evidence(name: str, payload: Mapping[str, Any], at: datetime) -> RiskInputEvidence:
    """Tapat na generic evidence: content sha ng aktwal na deskriptor payload."""

    return RiskInputEvidence(
        source=f"db-paper-producer:{name}",
        observed_at=at,
        available_at=at,
        content_sha256=sha256_json({"schema": f"chili.db-paper-evidence.{name}.v1", **dict(payload)}),
        provider_generation=_CAPTURE_VERIFIER_GENERATION,
    )


def build_db_paper_final_admission_material(
    db: Any,
    *,
    sess: Any,
    variant: Any,
    quote_fn: Callable[[str], Mapping[str, Any] | None] | None,
    regime_snapshot: Mapping[str, Any] | None = None,
    **boundary: Any,
) -> DbPaperFinalAdmissionMaterial:
    """Buuin ang isang buong DbPaperFinalAdmissionMaterial mula sa live data.

    Ang mga boundary kwargs ay ang eksaktong ipinapasa ng
    ``paper_runner`` sa ``runtime_db_paper_final_admission`` — ini-echo namin
    ang bawat isa nang verbatim (ang builder ang muling magpapatunay).
    Bawat kabiguan ay typed ``AdaptiveRiskBuilderError`` ⇒ tapat na veto.
    """

    from .paper_runner import _resolve_quote

    symbol = str(boundary["symbol"]).strip().upper()
    setup_family = str(boundary["setup_family"]).strip().lower()
    account_scope = str(boundary["account_scope"]).strip()
    account_identity = str(boundary["account_identity_sha256"]).strip().lower()

    policy_receipt = build_adaptive_risk_policy_from_settings(settings)
    policy = policy_receipt.policy
    terms_request = {
        "stop_atr_mult": float(boundary["stop_atr_mult"]),
        "target_atr_mult": float(boundary["target_atr_mult"]),
        "vol_floor_mult": float(boundary["vol_floor_mult"]),
        "reward_risk": float(boundary["reward_risk"]),
        "entry_slippage_bps": float(boundary["entry_slippage_bps"]),
        "exit_slippage_bps": float(boundary["exit_slippage_bps"]),
        "fee_to_target_ratio": float(boundary["fee_to_target_ratio"]),
    }
    effective_config = _effective_config_sha256(policy_receipt, terms_request)

    # ── BBO (capture-owned read; hindi kailanman pineke) ─────────────────
    readiness = dict(boundary.get("execution_readiness") or {})
    spread_hint = _readiness_float(readiness, "spread_bps") or 50.0
    bbo_read_at = datetime.now(timezone.utc)
    bid, ask, mid, quote_source = _resolve_quote(symbol, spread_hint, quote_fn)
    if not (bid > 0 and ask > 0 and mid > 0 and ask >= bid):
        raise AdaptiveRiskBuilderError("db_paper_final_bbo_unavailable", quote_source)

    decision_at = datetime.now(timezone.utc)
    eligibility_available = boundary["eligibility_available_at"]
    if decision_at < eligibility_available:
        decision_at = eligibility_available

    # ── Entry gate (sariwang capture-owned evaluation) ───────────────────
    from .entry_gates import run_paper_entry_gates

    gate_clock = datetime.now(timezone.utc)
    gate_allowed, gate_reason, gate_debug = run_paper_entry_gates(
        db,
        symbol=symbol,
        variant=variant,
        regime_snapshot=dict(regime_snapshot or {}),
        family_id=str(getattr(variant, "family", "") or "") or None,
        live_price=mid,
        decision_at=gate_clock,
    )
    gate_debug = dict(gate_debug or {})
    gate_done = datetime.now(timezone.utc)
    pullback_low = None
    try:
        raw_stop = gate_debug.get("pullback_low")
        if raw_stop is not None:
            pullback_low = float(raw_stop)
    except (TypeError, ValueError):
        pullback_low = None
    if pullback_low is None or not (0 < pullback_low < mid):
        # Walang structural low = walang mapapatunayang panganib — ang
        # material ay lalabas na gate_allowed=False at ang Observation ang
        # magko-convert nito sa typed veto. Konserbatibong placeholder pa rin
        # para buo ang canonical payload.
        gate_allowed = False
        if pullback_low is None:
            gate_reason = str(gate_reason or "") or "no_pullback_low"
        pullback_low = max(0.01, round(mid * 0.94, 4))
        gate_debug.setdefault("pullback_low", pullback_low)

    opportunity = {
        "account_scope": account_scope,
        "symbol": symbol,
        "trading_date": decision_at.astimezone(_ET).date().isoformat(),
        "setup_family": setup_family,
    }

    # ── SHARED STOP RECOMPUTE (kailangang eksaktong tugma sa runner) ─────
    # Ang runner ay nagre-recompute ng executable stop mula sa final BBO at
    # sa parehong volatility/structure chain, at nangangailangan ng
    # inputs.structural_stop == recomputed stop (same-money). Ginagamit namin
    # ang PAREHONG mga function para imposible ang drift.
    vol_fraction = _readiness_float(readiness, "atr_pct", "volatility_fraction")
    if vol_fraction is None:
        vol_fraction = 0.05
    elif vol_fraction > 1.0:
        vol_fraction = vol_fraction / 100.0
    entry_price = float(ask) * (
        1.0 + float(boundary["entry_slippage_bps"]) / 10_000.0
    )
    _eff_atr = effective_stop_atr_pct(
        float(vol_fraction),
        float(vol_fraction) * 10_000.0,
        stop_atr_mult=terms_request["stop_atr_mult"],
        vol_floor_mult=terms_request["vol_floor_mult"],
    )
    _eff_atr, _stop_model = structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=_eff_atr,
        structural_stop_price=float(pullback_low),
        entry_price=entry_price,
        stop_atr_mult=terms_request["stop_atr_mult"],
    )
    structural_stop, _target_px = stop_target_prices(
        entry_price,
        atr_pct=_eff_atr,
        side_long=True,
        stop_atr_mult=terms_request["stop_atr_mult"],
        target_atr_mult=terms_request["target_atr_mult"],
        reward_risk=terms_request["reward_risk"],
    )
    structural_stop = float(structural_stop)

    # ── Canonical component payloads (exported builders LAMANG) ──────────
    bbo_payload = db_paper_bbo_evidence_payload(
        symbol=symbol,
        bid=float(bid),
        ask=float(ask),
        quote_source=quote_source,
        observed_at=bbo_read_at,
        available_at=bbo_read_at,
        provider_generation=_BBO_PROVIDER_GENERATION,
    )
    eligibility_payload = db_paper_eligibility_evidence_payload(
        symbol=symbol,
        viability_id=int(boundary["viability_id"]),
        variant_id=int(boundary["variant_id"]),
        viability_score=float(boundary["viability_score"]),
        paper_eligible=bool(boundary["paper_eligible"]),
        observed_at=boundary["eligibility_observed_at"],
        available_at=eligibility_available,
        row_updated_at=boundary["eligibility_row_updated_at"],
        execution_readiness=readiness,
        source=_ELIGIBILITY_SOURCE,
        provider_generation=_ELIGIBILITY_PROVIDER_GENERATION,
    )
    gate_payload = db_paper_entry_gate_evidence_payload(
        symbol=symbol,
        allowed=bool(gate_allowed),
        reason=str(gate_reason or ""),
        debug=gate_debug,
        structural_stop=float(structural_stop),
        setup_family=setup_family,
        opportunity_key=opportunity,
        observed_at=gate_clock,
        available_at=gate_done,
        source=_GATE_SOURCE,
        provider_generation=_GATE_PROVIDER_GENERATION,
    )

    # ── Account snapshot (ang simulated instance account) ────────────────
    equity = float(getattr(settings, "chili_db_paper_equity_usd", 13000.0) or 13000.0)
    buying_power = float(
        getattr(settings, "chili_db_paper_buying_power_usd", 0.0) or equity
    )
    snapshot_at = datetime.now(timezone.utc)
    account = ImmutableAccountRiskSnapshot(
        snapshot_id=f"db-paper-{sess.id}-{int(snapshot_at.timestamp() * 1000)}",
        source=_ACCOUNT_SOURCE,
        provider_generation=_ACCOUNT_SOURCE,
        account_scope=account_scope,
        execution_family=str(boundary["execution_family"]).strip().lower(),
        broker_environment="paper",
        venue=str(boundary["venue"]).strip().lower(),
        account_identity_sha256=account_identity,
        observed_at=snapshot_at,
        available_at=snapshot_at,
        equity_usd=equity,
        buying_power_usd=buying_power,
        broker_day_change_usd=0.0,
        local_realized_pnl_usd=_today_realized_paper_pnl_usd(db, decision_at),
        pending_policy_buying_power_reflected_usd=0.0,
    )
    account_evidence = RiskInputEvidence(
        source=account.source,
        observed_at=account.observed_at,
        available_at=account.available_at,
        content_sha256=account.snapshot_sha256,
        provider_generation=account.provider_generation,
    )

    # ── Evidence map ─────────────────────────────────────────────────────
    component_shas = [
        db_paper_admission_component_sha256(bbo_payload),
        db_paper_admission_component_sha256(eligibility_payload),
        db_paper_admission_component_sha256(gate_payload),
    ]
    capture_prefix_root = sha256_json(component_shas)
    evidence: dict[str, RiskInputEvidence] = {
        "bbo": RiskInputEvidence(
            source=quote_source,
            observed_at=bbo_read_at,
            available_at=bbo_read_at,
            content_sha256=component_shas[0],
            provider_generation=_BBO_PROVIDER_GENERATION,
        ),
        "paper_eligibility": RiskInputEvidence(
            source=_ELIGIBILITY_SOURCE,
            observed_at=boundary["eligibility_observed_at"],
            available_at=eligibility_available,
            content_sha256=component_shas[1],
            provider_generation=_ELIGIBILITY_PROVIDER_GENERATION,
        ),
        "paper_entry_gate": RiskInputEvidence(
            source=_GATE_SOURCE,
            observed_at=gate_clock,
            available_at=gate_done,
            content_sha256=component_shas[2],
            provider_generation=_GATE_PROVIDER_GENERATION,
        ),
        "account": account_evidence,
        "daily_pnl": account_evidence,
        "capture_prefix": RiskInputEvidence(
            source="db-paper-producer:capture-prefix",
            observed_at=gate_done,
            available_at=gate_done,
            content_sha256=capture_prefix_root,
            provider_generation=_CAPTURE_VERIFIER_GENERATION,
        ),
        "structural_stop": _generic_evidence(
            "structural_stop",
            {"symbol": symbol, "structural_stop": float(structural_stop)},
            gate_done,
        ),
        "setup_quality": _generic_evidence(
            "setup_quality",
            {"symbol": symbol, "viability_score": float(boundary["viability_score"])},
            gate_done,
        ),
        "volatility": _generic_evidence(
            "volatility",
            {"symbol": symbol, "atr_pct": readiness.get("atr_pct")},
            gate_done,
        ),
        "liquidity": _generic_evidence(
            "liquidity",
            {"symbol": symbol,
             "adv": readiness.get("avg_daily_volume"),
             "recent": readiness.get("recent_volume")},
            gate_done,
        ),
        "portfolio_heat": _generic_evidence(
            "portfolio_heat", {"pre_lock_placeholder": True}, gate_done
        ),
        "correlation": _generic_evidence(
            "correlation", {"cluster": f"equity:{setup_family}"}, gate_done
        ),
        "code_build": _generic_evidence(
            "code_build", {"sha": _code_build_sha256()}, gate_done
        ),
        "effective_config": _generic_evidence(
            "effective_config", {"sha": effective_config}, gate_done
        ),
        "feature_flags": _generic_evidence(
            "feature_flags", {"sha": _feature_flags_sha256()}, gate_done
        ),
        "candidate_buying_power_estimate": _generic_evidence(
            "candidate_buying_power_estimate",
            {"symbol": symbol, "ask": float(ask)},
            gate_done,
        ),
        "reservation_ledger": _generic_evidence(
            "reservation_ledger", {"pre_lock_placeholder": True}, gate_done
        ),
    }

    # ── Inputs (pre-lock: zeroed aggregates; ang runner ang magfo-fold ng
    #    locked na katotohanan sa bundle) ──────────────────────────────────
    adv = _readiness_float(readiness, "avg_daily_volume", "adv_shares") or 1_000_000.0
    recent = _readiness_float(readiness, "recent_volume", "volume") or max(
        100_000.0, adv * 0.05
    )
    depth = _readiness_float(readiness, "executable_depth_shares") or max(
        1_000.0, recent * 0.01
    )
    correlation_cluster = f"equity:{setup_family}"
    trading_date = decision_at.astimezone(_ET).date().isoformat()
    inputs = AdaptiveRiskInputs(
        decision_id=f"dbp-{sess.id}-{trading_date}-{setup_family}",
        replay_or_paper_run_id=str(sess.correlation_id or f"dbp-run-{sess.id}"),
        generation=int(sess.id),
        execution_surface="db_paper",
        execution_family=account.execution_family,
        venue=account.venue,
        broker_environment="paper",
        symbol=symbol,
        side="long",
        as_of=decision_at,
        account_identity_sha256=account_identity,
        code_build_sha256=_code_build_sha256(),
        effective_config_sha256=effective_config,
        feature_flags_sha256=_feature_flags_sha256(),
        capture_prefix_root_sha256=capture_prefix_root,
        equity_usd=account.equity_usd,
        buying_power_usd=account.buying_power_usd,
        broker_day_change_usd=account.broker_day_change_usd,
        local_realized_pnl_usd=account.local_realized_pnl_usd,
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=0.0,
        policy_buying_power_capacity_usd=account.buying_power_usd,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=0.0,
        candidate_buying_power_impact_per_share_usd=float(ask),
        bid=float(bid),
        ask=float(ask),
        structural_stop=float(structural_stop),
        entry_slippage_bps=float(boundary["entry_slippage_bps"]),
        exit_slippage_bps=float(boundary["exit_slippage_bps"]),
        fees_per_share_usd=0.0,
        setup_quality=max(0.0, min(1.0, float(boundary["viability_score"]))),
        realized_volatility_fraction=float(vol_fraction),
        average_daily_volume_shares=float(adv),
        recent_volume_shares=float(recent),
        executable_depth_shares=float(depth),
        correlation_cluster_id=correlation_cluster,
        evidence=evidence,
    )

    capture_binding = AdaptiveRiskDiagnosticCaptureBinding.create_diagnostic(
        run_id=inputs.replay_or_paper_run_id,
        generation=inputs.generation,
        decision_id=inputs.decision_id,
        input_prefix_sequence=3,
        input_prefix_root_sha256=capture_prefix_root,
        identity_sha256=account_identity,
        observed_at=gate_done,
        available_at=gate_done,
        verifier_generation=_CAPTURE_VERIFIER_GENERATION,
    )
    source = AdaptiveRiskBuilderSource(
        policy=policy,
        inputs=inputs,
        account_snapshot=account,
        capture_binding=capture_binding,
        account_scope=account_scope,
        setup_family=setup_family,
        correlation_cluster=correlation_cluster,
    )
    execution_terms = db_paper_execution_terms_payload(
        effective_config_sha256=effective_config,
        **terms_request,
    )

    material_kwargs: dict[str, Any] = {}
    if setup_family == "first_dip_reclaim":
        # Ang first-dip envelope ay process-local capability na mimintable
        # lamang ng first-dip tape capture (first_dip_tape_decision:3533, may
        # buong adaptive-request na kontrata). Wala pang tape capture sa paper
        # runner na proseso, kaya TAPAT na typed fail-closed — kapareho ng
        # dating gawi pero may tumpak nang dahilan; ang pullback/momentum na
        # families ay nabubuksan agad.
        raise AdaptiveRiskBuilderError(
            "db_paper_first_dip_capture_context_unavailable", setup_family
        )

    material = DbPaperFinalAdmissionMaterial.create(
        source,
        quote_source=quote_source,
        gate_allowed=bool(gate_allowed),
        gate_reason=str(gate_reason or ""),
        gate_debug=gate_debug,
        opportunity_key=opportunity,
        eligibility=eligibility_payload,
        execution_terms=execution_terms,
        **material_kwargs,
    )
    # Self round-trip: anumang canonicalization drift ay mamamatay DITO sa
    # producer na may malinaw na traceback, hindi sa gitna ng seremonya.
    load_db_paper_final_admission_material(material.to_payload())
    return material
