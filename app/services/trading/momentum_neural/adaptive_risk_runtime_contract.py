"""Fail-closed runtime contract for adaptive-risk surface parity.

The pure resolver is necessary but not sufficient for runtime parity.  A
broker path is not migrated until its last risk-increasing boundary strictly
recomputes the persisted decision packet and atomically reserves all three
economic dimensions (structural risk, gross notional, and broker buying-power
impact).  This module supplies the immutable reservation payload and the
readiness assessment used to keep an incompletely migrated runtime non-ready.

Nothing in this module enables a runner or places an order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
from typing import Any, Iterable, Mapping

from .adaptive_risk_policy import (
    AdaptiveRiskContractError,
    AdaptiveRiskInputs,
    RISK_PACKET_SCHEMA_VERSION,
    load_and_verify_adaptive_risk_decision_packet,
)


RUNTIME_PARITY_SCHEMA_VERSION = "chili.adaptive-risk-runtime-parity.v1"
RESERVATION_CLAIM_SCHEMA_VERSION = "chili.adaptive-risk-reservation-claim.v1"
ADAPTIVE_RISK_RESOLVER_ID = (
    "app.services.trading.momentum_neural.adaptive_risk_policy."
    "resolve_adaptive_risk"
)
REQUIRED_RUNTIME_SURFACES = frozenset(
    {"replay_v3", "db_paper", "alpaca_paper", "live"}
)
REQUIRED_ATOMIC_RESERVATION_DIMENSIONS = frozenset(
    {
        "structural_risk_usd",
        "gross_notional_usd",
        "buying_power_impact_usd",
    }
)


def _canonical_json(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskContractError(
            f"non-canonical adaptive-risk runtime payload: {exc}"
        ) from exc
    return encoded.encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


ADAPTIVE_RISK_INPUT_CONTRACT_SHA256 = _sha256_json(
    [field.name for field in fields(AdaptiveRiskInputs)]
)


@dataclass(frozen=True)
class AdaptiveRiskReservationClaim:
    """Exact economic dimensions a reservation transaction must persist."""

    schema_version: str
    claim_id: str
    decision_packet_sha256: str
    policy_sha256: str
    input_sha256: str
    account_identity_sha256: str
    capture_prefix_root_sha256: str
    decision_id: str
    run_id: str
    generation: int
    execution_surface: str
    execution_family: str
    venue: str
    broker_environment: str
    symbol: str
    correlation_cluster_id: str
    side: str
    quantity_shares: int
    structural_risk_usd: float
    gross_notional_usd: float
    buying_power_impact_usd: float
    reservation_ledger_content_sha256: str

    @property
    def claim_sha256(self) -> str:
        return _sha256_json(asdict(self))

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["claim_sha256"] = self.claim_sha256
        return payload


@dataclass(frozen=True)
class AdaptiveRiskLedgerSnapshot:
    """Atomic open+pending ledger projection observed under the account lock."""

    open_structural_risk_usd: float
    pending_reserved_risk_usd: float
    existing_same_symbol_structural_risk_usd: float
    pending_same_symbol_structural_risk_usd: float
    current_cluster_structural_risk_usd: float
    pending_correlation_cluster_risk_usd: float
    portfolio_gross_notional_usd: float
    pending_portfolio_gross_notional_usd: float
    open_buying_power_impact_usd: float
    pending_buying_power_impact_usd: float
    content_sha256: str

    @classmethod
    def from_dimensions(
        cls,
        *,
        open_structural_risk_usd: float,
        pending_reserved_risk_usd: float,
        existing_same_symbol_structural_risk_usd: float,
        pending_same_symbol_structural_risk_usd: float,
        current_cluster_structural_risk_usd: float,
        pending_correlation_cluster_risk_usd: float,
        portfolio_gross_notional_usd: float,
        pending_portfolio_gross_notional_usd: float,
        open_buying_power_impact_usd: float,
        pending_buying_power_impact_usd: float,
    ) -> "AdaptiveRiskLedgerSnapshot":
        values = {
            "open_structural_risk_usd": float(open_structural_risk_usd),
            "pending_reserved_risk_usd": float(pending_reserved_risk_usd),
            "existing_same_symbol_structural_risk_usd": float(
                existing_same_symbol_structural_risk_usd
            ),
            "pending_same_symbol_structural_risk_usd": float(
                pending_same_symbol_structural_risk_usd
            ),
            "current_cluster_structural_risk_usd": float(
                current_cluster_structural_risk_usd
            ),
            "pending_correlation_cluster_risk_usd": float(
                pending_correlation_cluster_risk_usd
            ),
            "portfolio_gross_notional_usd": float(portfolio_gross_notional_usd),
            "pending_portfolio_gross_notional_usd": float(
                pending_portfolio_gross_notional_usd
            ),
            "open_buying_power_impact_usd": float(open_buying_power_impact_usd),
            "pending_buying_power_impact_usd": float(
                pending_buying_power_impact_usd
            ),
        }
        return cls(**values, content_sha256=_sha256_json(values))


def build_adaptive_risk_reservation_claim(
    packet: Mapping[str, Any],
    *,
    claim_id: str,
) -> AdaptiveRiskReservationClaim:
    """Strictly recompute a packet and derive its atomic reservation payload."""

    claim_key = str(claim_id or "").strip()
    if not claim_key:
        raise AdaptiveRiskContractError("adaptive risk reservation claim_id is required")
    resolved = load_and_verify_adaptive_risk_decision_packet(packet)
    if not resolved.valid or resolved.quantity_shares <= 0:
        raise AdaptiveRiskContractError(
            "invalid adaptive risk decision cannot create a reservation claim"
        )
    inputs = resolved.input_snapshot
    evidence = inputs.get("evidence")
    ledger = evidence.get("reservation_ledger") if isinstance(evidence, Mapping) else None
    if not isinstance(ledger, Mapping):
        raise AdaptiveRiskContractError("reservation ledger provenance is missing")
    return AdaptiveRiskReservationClaim(
        schema_version=RESERVATION_CLAIM_SCHEMA_VERSION,
        claim_id=claim_key,
        decision_packet_sha256=resolved.decision_packet_sha256,
        policy_sha256=resolved.policy_sha256,
        input_sha256=resolved.input_sha256,
        account_identity_sha256=str(inputs["account_identity_sha256"]),
        capture_prefix_root_sha256=str(inputs["capture_prefix_root_sha256"]),
        decision_id=str(inputs["decision_id"]),
        run_id=str(inputs["replay_or_paper_run_id"]),
        generation=int(inputs["generation"]),
        execution_surface=str(inputs["execution_surface"]),
        execution_family=str(inputs["execution_family"]),
        venue=str(inputs["venue"]),
        broker_environment=str(inputs["broker_environment"]),
        symbol=str(inputs["symbol"]),
        correlation_cluster_id=str(inputs["correlation_cluster_id"]),
        side=str(inputs["side"]),
        quantity_shares=int(resolved.quantity_shares),
        structural_risk_usd=float(resolved.planned_structural_risk_usd),
        gross_notional_usd=float(resolved.planned_notional_usd),
        buying_power_impact_usd=float(resolved.planned_buying_power_impact_usd),
        reservation_ledger_content_sha256=str(ledger["content_sha256"]),
    )


def load_and_verify_adaptive_risk_reservation_claim(
    packet: Mapping[str, Any],
    claim_payload: Mapping[str, Any],
) -> AdaptiveRiskReservationClaim:
    """Reject any reservation dimension not derived exactly from ``packet``."""

    if not isinstance(claim_payload, Mapping):
        raise AdaptiveRiskContractError("adaptive risk reservation claim must be a mapping")
    raw = dict(claim_payload)
    supplied_sha256 = raw.pop("claim_sha256", None)
    if raw.get("schema_version") != RESERVATION_CLAIM_SCHEMA_VERSION:
        raise AdaptiveRiskContractError("unsupported adaptive risk reservation schema")
    try:
        candidate = AdaptiveRiskReservationClaim(**raw)
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskContractError(
            f"adaptive risk reservation claim is invalid: {exc}"
        ) from exc
    expected = build_adaptive_risk_reservation_claim(
        packet,
        claim_id=candidate.claim_id,
    )
    if supplied_sha256 != candidate.claim_sha256:
        raise AdaptiveRiskContractError("adaptive risk reservation claim hash mismatch")
    if _canonical_json(asdict(candidate)) != _canonical_json(asdict(expected)):
        raise AdaptiveRiskContractError(
            "adaptive risk reservation claim failed canonical recomputation"
        )
    return candidate


def verify_adaptive_risk_claim_against_atomic_ledger(
    packet: Mapping[str, Any],
    claim_payload: Mapping[str, Any],
    ledger: AdaptiveRiskLedgerSnapshot,
) -> AdaptiveRiskReservationClaim:
    """Bind a strict claim to the exact ledger read in its reservation tx."""

    claim = load_and_verify_adaptive_risk_reservation_claim(packet, claim_payload)
    resolved = load_and_verify_adaptive_risk_decision_packet(packet)
    inputs = resolved.input_snapshot
    field_names = (
        "open_structural_risk_usd",
        "pending_reserved_risk_usd",
        "existing_same_symbol_structural_risk_usd",
        "pending_same_symbol_structural_risk_usd",
        "current_cluster_structural_risk_usd",
        "pending_correlation_cluster_risk_usd",
        "portfolio_gross_notional_usd",
        "pending_portfolio_gross_notional_usd",
        "open_buying_power_impact_usd",
        "pending_buying_power_impact_usd",
    )
    for name in field_names:
        expected = float(getattr(ledger, name))
        supplied = float(inputs[name])
        tolerance = max(1e-9, max(abs(expected), abs(supplied)) * 1e-12)
        if abs(expected - supplied) > tolerance:
            raise AdaptiveRiskContractError(
                f"adaptive risk atomic ledger mismatch: {name}"
            )
    if claim.reservation_ledger_content_sha256 != ledger.content_sha256:
        raise AdaptiveRiskContractError(
            "adaptive risk reservation ledger content hash mismatch"
        )
    return claim


@dataclass(frozen=True)
class AdaptiveRiskRuntimeBinding:
    """Evidence a concrete runtime surface must supply to activation preflight.

    A registration is deliberately detailed: a module cannot claim parity just
    because it imports the resolver.  Its order boundary, durability, atomic
    ledger dimensions, and operational safeguards must all be wired.
    """

    surface: str
    resolver_id: str
    packet_schema_version: str
    input_contract_sha256: str
    policy_sha256: str
    code_build_sha256: str
    strict_packet_recomputed_at_last_risk_boundary: bool
    decision_packet_persisted_content_addressed: bool
    reservation_same_transaction_as_admission: bool
    atomic_reservation_dimensions: frozenset[str]
    account_identity_bound: bool
    order_idempotency_and_ownership_bound: bool
    reconciliation_bound: bool
    stale_data_fail_closed: bool
    kill_switch_bound: bool
    config_and_evidence_provenance_logged: bool
    activation_only_dollar_caps: tuple[str, ...] = ()
    fixed_symbol_concurrency_cap: int | None = None


@dataclass(frozen=True)
class AdaptiveRiskRuntimeReadiness:
    schema_version: str
    ready: bool
    reasons: tuple[str, ...]
    surface_reasons: Mapping[str, tuple[str, ...]]
    common_policy_sha256: str | None
    binding_manifest_sha256: str

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def assess_adaptive_risk_runtime_readiness(
    bindings: Iterable[AdaptiveRiskRuntimeBinding],
) -> AdaptiveRiskRuntimeReadiness:
    """Require parity receipts for every execution surface; missing is non-ready."""

    rows = list(bindings)
    grouped: dict[str, list[AdaptiveRiskRuntimeBinding]] = {}
    for binding in rows:
        grouped.setdefault(str(binding.surface or "").strip().lower(), []).append(binding)

    surface_reasons: dict[str, tuple[str, ...]] = {}
    policies: set[str] = set()
    for surface in sorted(REQUIRED_RUNTIME_SURFACES):
        matches = grouped.get(surface, [])
        reasons: list[str] = []
        if not matches:
            reasons.append("binding_missing")
        elif len(matches) != 1:
            reasons.append("binding_not_unique")
        else:
            binding = matches[0]
            if binding.resolver_id != ADAPTIVE_RISK_RESOLVER_ID:
                reasons.append("resolver_mismatch")
            if binding.packet_schema_version != RISK_PACKET_SCHEMA_VERSION:
                reasons.append("packet_schema_mismatch")
            if binding.input_contract_sha256 != ADAPTIVE_RISK_INPUT_CONTRACT_SHA256:
                reasons.append("input_contract_mismatch")
            if len(str(binding.policy_sha256 or "")) != 64:
                reasons.append("policy_hash_invalid")
            else:
                policies.add(binding.policy_sha256)
            if len(str(binding.code_build_sha256 or "")) != 64:
                reasons.append("code_build_hash_invalid")
            required_flags = {
                "strict_packet_recomputed_at_last_risk_boundary": binding.strict_packet_recomputed_at_last_risk_boundary,
                "decision_packet_persisted_content_addressed": binding.decision_packet_persisted_content_addressed,
                "reservation_same_transaction_as_admission": binding.reservation_same_transaction_as_admission,
                "account_identity_bound": binding.account_identity_bound,
                "order_idempotency_and_ownership_bound": binding.order_idempotency_and_ownership_bound,
                "reconciliation_bound": binding.reconciliation_bound,
                "stale_data_fail_closed": binding.stale_data_fail_closed,
                "kill_switch_bound": binding.kill_switch_bound,
                "config_and_evidence_provenance_logged": binding.config_and_evidence_provenance_logged,
            }
            reasons.extend(name for name, enabled in required_flags.items() if not enabled)
            missing_dimensions = REQUIRED_ATOMIC_RESERVATION_DIMENSIONS.difference(
                binding.atomic_reservation_dimensions
            )
            reasons.extend(
                f"atomic_reservation_dimension_missing:{name}"
                for name in sorted(missing_dimensions)
            )
            if binding.activation_only_dollar_caps:
                reasons.append("activation_only_dollar_cap_present")
            if binding.fixed_symbol_concurrency_cap is not None:
                reasons.append("fixed_symbol_concurrency_cap_present")
        surface_reasons[surface] = tuple(reasons)

    global_reasons: list[str] = []
    unexpected = sorted(set(grouped).difference(REQUIRED_RUNTIME_SURFACES))
    global_reasons.extend(f"unexpected_surface:{name}" for name in unexpected)
    if len(policies) > 1:
        global_reasons.append("policy_hash_differs_across_surfaces")
    for surface, reasons in surface_reasons.items():
        global_reasons.extend(f"{surface}:{reason}" for reason in reasons)

    manifest = [
        {
            **asdict(row),
            "atomic_reservation_dimensions": sorted(row.atomic_reservation_dimensions),
        }
        for row in sorted(rows, key=lambda item: item.surface)
    ]
    return AdaptiveRiskRuntimeReadiness(
        schema_version=RUNTIME_PARITY_SCHEMA_VERSION,
        ready=not global_reasons,
        reasons=tuple(global_reasons),
        surface_reasons=surface_reasons,
        common_policy_sha256=next(iter(policies)) if len(policies) == 1 else None,
        binding_manifest_sha256=_sha256_json(manifest),
    )


def require_adaptive_risk_runtime_ready(
    bindings: Iterable[AdaptiveRiskRuntimeBinding],
) -> AdaptiveRiskRuntimeReadiness:
    readiness = assess_adaptive_risk_runtime_readiness(bindings)
    if not readiness.ready:
        raise AdaptiveRiskContractError(
            "adaptive risk runtime parity is not ready: " + ",".join(readiness.reasons)
        )
    return readiness
