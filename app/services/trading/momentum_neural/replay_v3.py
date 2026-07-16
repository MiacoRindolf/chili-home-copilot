"""Replay v3 P1 — the live-FSM SIMULATOR driver.

Step ONE recorded momentum session END-TO-END through the *real*
``live_runner.tick_live_session`` FSM, on a SIMULATED clock, against a deterministic
``replay_mock_broker.MockBrokerAdapter`` (no real broker, no network), serving 15m/5m/1m
bars from a RECORDED-OHLCV provider via the ``live_runner`` seam. The FSM is NEVER
re-implemented — the driver only supplies INPUTS (clock + quote + bars + a seeded session)
and calls the unchanged ``tick_live_session`` once per grid step, letting the runner's own
state machine drive ``queued_live → watching_live → live_entry_candidate → live_pending_entry
→ live_entered → … → live_exited``.

This is the instrument Replay v2 structurally cannot be (v2 forks the arm→enter decision
inline over the tape; v3 runs the live gate verbatim). P1 wires the machinery on SYNTHETIC
recorded data; the real-data / chili_staging replay + the UPC recency-grace A/B are P2–P4.

Reuse map (docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §6):
  * clock      — ``live_runner.replay_clock`` (P0 ContextVar on ``_utcnow``)
  * broker     — ``replay_mock_broker.MockBrokerAdapter`` + ``make_mock_broker_factory``
  * OHLCV seam — ``live_runner.replay_ohlcv_provider`` (P1 ContextVar on the in-tick fetch)
  * FSM        — ``live_runner.tick_live_session(db, sid, adapter_factory=…)`` (verbatim)

See docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §2.2 / §4 (P1).
"""

from __future__ import annotations

import logging
import math
import json
import ipaddress
import socket
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from ....models.core import User
from ....models.trading import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from ..venue.account_identity import NON_ALPACA_ACCOUNT_IDENTITY_KEY
from ..execution_family_registry import (
    normalize_execution_family,
    venue_for_execution_family,
)
from . import live_runner as lr
from . import risk_evaluator as _risk_eval
from . import risk_policy as _rp
from .adaptive_risk_policy import (
    RISK_PACKET_SCHEMA_VERSION,
    ResolvedAdaptiveRisk,
    RiskInputEvidence,
    load_and_verify_adaptive_risk_decision_packet,
)
from .adaptive_risk_request_builder import (
    AdaptiveRiskDiagnosticCaptureBinding,
    AdaptiveRiskBuilderError,
    AdaptiveRiskBuilderSource,
    AdaptiveRiskRuntimeCaptureMaterial,
    _issue_sealed_replay_adaptive_risk_build_attestation,
    adaptive_risk_source_provider,
    rebuild_adaptive_risk_decision_packet,
)
from .live_fsm import (
    STATE_ARMED_PENDING_RUNNER,
    STATE_LIVE_ENTERED,
    STATE_LIVE_TRAILING,
    STATE_QUEUED_LIVE,
)
from .first_dip_tape_policy import (
    FirstDipTapeEvaluation,
    FirstDipTapePolicy,
    FirstDipTapeReadQuery,
    FirstDipTapeWindow,
    evaluate_first_dip_tape,
    first_dip_tape_window_from_capture,
)
from .first_dip_tape_decision import (
    FIRST_DIP_TAPE_PURPOSE_DETECTOR,
    FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION,
    FirstDipTapeDecisionResolution,
    FirstDipTapeDecisionRequest,
    _FIRST_DIP_TAPE_DECISION_AUTHORITY_ISSUER,
    _FirstDipPriorDetectorReference,
    _FirstDipTapeDecisionBinding,
    _VerifiedFirstDipTapeDecisionAuthority,
    _installed_sealed_replay_first_dip_final_authority_provider,
    _installed_sealed_replay_first_dip_tape_decision_authority,
    _issue_first_dip_final_authority_handoff,
    _prior_detector_reference_from_resolution,
)
from .live_replay_capture import (
    FirstDipFinalCaptureFrontier,
    LiveMicrostructureCaptureBridge,
)
from .replay_eligibility import EligibilityReplayer
from .replay_capture_contract import (
    ActiveCaptureContinuityEvidence,
    ActiveCaptureReadEvidence,
    CaptureContractError,
    CaptureBrokerOrderLifecycle,
    CaptureBrokerTransition,
    CaptureCoverageGrade,
    CaptureCoverageManifest,
    CaptureDecisionAction,
    CaptureDecisionCheckpoint,
    CaptureDecisionOutput,
    CaptureOrderIntent,
    CaptureOrderIntentRole,
    CaptureReadReceipt,
    CaptureRunOpen,
    CaptureScannerSnapshot,
    CaptureEvent,
    CaptureEventRef,
    CaptureIqfeedPrint,
    CaptureMicrostructureOperation,
    CaptureMicrostructureReadQuery,
    CaptureStream,
    CoverageMode,
    DeterministicDualClockLoader,
    FSMDependencyProfile,
    ReplayCoverageRequest,
    STREAM_POLICIES,
    PROVIDER_OHLCV_PAYLOAD_SCHEMA_VERSION,
    PROVIDER_OHLCV_QUERY_SCHEMA_VERSION,
    SCANNER_SNAPSHOT_PAYLOAD_SCHEMA_VERSION,
    StreamCoverage,
    IQFEED_L1_SOURCE_PROVENANCE_FIELD,
    IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
    NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION,
    VerifiedReplayCapture,
    captured_read_result_sha256,
    capture_prefix_root_sha256,
    capture_adaptive_order_artifacts_from_payload,
    capture_decision_output_from_payload,
    capture_final_decision_authority_sha256,
    grade_replay_coverage,
    resolve_capture_source_payload,
    sha256_json,
    validate_iqfeed_l1_source_provenance,
    verify_first_dip_receipt_inventory,
)
from .replay_mock_broker import (
    REPLAY_MOCK_ACCOUNT_IDENTITY,
    SEALED_REPLAY_SYNC_ACK_ARCHITECTURAL_BLOCKER,
    MockBrokerAdapter,
    RecordedBrokerTransition,
    VerifiedExactPrint,
    VerifiedExactPrintInventory,
    RecordedOrderIntent,
    RecordedQuote,
    _mint_verified_exact_print,
    _mint_verified_exact_print_inventory,
    make_mock_broker_factory,
)
from .replay_capture_runtime import ReplayNetworkAccessError, ReplayNetworkGuard
from .replay_errors import ReplayScannerSnapshotUnavailableError

_log = logging.getLogger(__name__)

REPLAY_ECONOMIC_SEED_KEY = "replay_economic_seed"
ADAPTIVE_RISK_DECISION_KEY = "adaptive_risk_decision"
PENDING_ADAPTIVE_RISK_DECISION_KEY = "pending_adaptive_risk_decision"
MUTABLE_DATABASE_CERTIFICATION_BLOCKER = (
    "mutable_database_dependencies_not_sealed"
)
CONTINUOUS_DECISION_READS_CERTIFICATION_BLOCKER = (
    "continuous_decision_reads_not_observed_by_fsm"
)
UNSEALED_CAUSAL_RUNTIME_INPUTS_CERTIFICATION_BLOCKER = (
    "l2_macro_governance_inputs_not_receipt_bound"
)
PROCESS_GLOBAL_STATE_CERTIFICATION_BLOCKER = (
    "process_global_decision_state_not_sealed"
)
SEALED_RUNTIME_INPUT_FAMILIES = (
    "governance",
    "microstructure",
    "macro",
    "selection_pipeline",
    "scanner_snapshot",
)

# Backward-compatible economics for synthetic P1/P2 FSM diagnostics only. The
# seed and every result carrying these values are explicitly non-certifying;
# activation, paper parity, Ross comparison, and OOS evidence must never consume
# them. Keeping this one named fixture preserves existing state-machine tests
# while the shared adaptive replay/paper runtime is migrated.
LEGACY_DIAGNOSTIC_POLICY_CAPS: Mapping[str, float] = {
    "max_notional_per_trade_usd": 100_000.0,
    "max_hold_seconds": 14_400.0,
    "max_loss_per_trade_usd": 50.0,
}


@dataclass(frozen=True)
class ReplayEconomicSeedEvidence:
    """Recorded evidence required for a certifiable ReplayV3 economic seed.

    This is deliberately narrower than whole-run certification. It verifies that
    a pending risk packet came from a valid adaptive decision over the same
    complete capture manifest. The packet is not active before its recorded
    availability clock, and it is not certification-eligible until the shared
    replay/paper runtime consumes it directly. Network denial, causal FSM parity,
    order-intent parity, and full-session OOS gates remain separate proofs.
    """

    coverage_request: ReplayCoverageRequest
    coverage_manifest: CaptureCoverageManifest
    risk_decision_packet: Mapping[str, Any]
    decision_available_at: datetime

    def validate_for(
        self, symbol: str, *, execution_family: str
    ) -> tuple[ResolvedAdaptiveRisk, dict[str, Any], CaptureCoverageGrade]:
        reasons: list[str] = []
        grade = grade_replay_coverage(
            self.coverage_request, self.coverage_manifest
        )
        risk = load_and_verify_adaptive_risk_decision_packet(
            self.risk_decision_packet
        )
        available_at = self.decision_available_at
        if not isinstance(available_at, datetime) or available_at.tzinfo is None:
            raise ValueError(
                "ReplayV3 economic seed is not certifiable: "
                "decision_available_at_not_timezone_aware"
            )
        available_at = available_at.astimezone(timezone.utc)
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_family = normalize_execution_family(execution_family)
        expected_venue = venue_for_execution_family(normalized_family)
        checkpoint = next(
            (
                row
                for row in self.coverage_manifest.decision_checkpoints
                if row.checkpoint_sha256
                == self.coverage_request.decision_checkpoint_sha256
            ),
            None,
        )

        if not grade.replayable or grade.grade != "complete" or grade.reasons:
            reasons.append("capture_coverage_not_complete")
        if risk.schema_version != RISK_PACKET_SCHEMA_VERSION:
            reasons.append("adaptive_risk_schema_mismatch")
        if not risk.valid or risk.rejection_reasons:
            reasons.append("adaptive_risk_resolution_invalid")
        if risk.quantity_shares <= 0:
            reasons.append("adaptive_risk_quantity_not_positive")
        if risk.planned_structural_risk_usd <= 0:
            reasons.append("adaptive_risk_planned_risk_not_positive")
        if risk.planned_notional_usd <= 0:
            reasons.append("adaptive_risk_planned_notional_not_positive")

        inputs = risk.input_snapshot
        if not isinstance(inputs, Mapping):
            reasons.append("adaptive_risk_input_snapshot_missing")
            inputs = {}
        surface = str(inputs.get("execution_surface") or "").strip().lower()
        broker_environment = str(
            inputs.get("broker_environment") or ""
        ).strip().lower()
        if broker_environment == "paper":
            expected_surface = "alpaca_paper" if expected_venue == "alpaca" else "paper"
        elif broker_environment == "live":
            expected_surface = "live"
        elif broker_environment == "replay":
            expected_surface = "replay"
        else:
            expected_surface = ""
        if not expected_surface or surface != expected_surface:
            reasons.append("adaptive_risk_surface_environment_mismatch")
        if str(inputs.get("execution_family") or "").strip().lower() != normalized_family:
            reasons.append("adaptive_risk_execution_family_mismatch")
        if str(inputs.get("venue") or "").strip().lower() != expected_venue:
            reasons.append("adaptive_risk_venue_mismatch")
        identity_broker = str(self.coverage_manifest.identity.broker or "").strip().lower()
        if identity_broker != expected_venue:
            reasons.append("capture_broker_execution_family_mismatch")
        if broker_environment != str(
            self.coverage_manifest.identity.broker_environment or ""
        ).strip().lower():
            reasons.append("adaptive_risk_broker_environment_mismatch")
        if str(inputs.get("symbol") or "").strip().upper() != normalized_symbol:
            reasons.append("adaptive_risk_symbol_mismatch")
        if str(inputs.get("side") or "").strip().lower() != "long":
            reasons.append("adaptive_risk_side_not_long")
        request = self.coverage_request
        identity = self.coverage_manifest.identity
        if checkpoint is None:
            reasons.append("adaptive_risk_decision_checkpoint_missing")
            prefix_root = ""
        else:
            prefix_root = checkpoint.input_prefix_root_sha256
            canonical_output: CaptureDecisionOutput | None = None
            try:
                canonical_output = capture_decision_output_from_payload(
                    checkpoint.decision_payload
                )
                capture_adaptive_order_artifacts_from_payload(
                    checkpoint.decision_payload,
                    canonical_output,
                    identity=identity,
                )
            except (CaptureContractError, TypeError, ValueError):
                reasons.append(
                    "adaptive_risk_canonical_decision_artifacts_invalid"
                )
            if canonical_output is not None:
                risk_increasing_intents = tuple(
                    intent
                    for intent in canonical_output.order_intents
                    if intent.risk_increasing
                )
                if canonical_output.action is not CaptureDecisionAction.ORDER_INTENT:
                    reasons.append(
                        "adaptive_risk_canonical_decision_not_order_intent"
                    )
                elif len(risk_increasing_intents) != 1:
                    reasons.append(
                        "adaptive_risk_canonical_entry_intent_ambiguous"
                    )
                else:
                    canonical_intent = risk_increasing_intents[0]
                    if (
                        canonical_intent.intent_role
                        is not CaptureOrderIntentRole.ENTRY
                        or canonical_intent.side != "buy"
                        or canonical_intent.symbol != normalized_symbol
                        or canonical_intent.quantity != int(risk.quantity_shares)
                        or canonical_intent.adaptive_decision_sha256
                        != risk.decision_packet_sha256
                        or canonical_intent.adaptive_resolution_sha256
                        != risk.economic_resolution_sha256
                        or canonical_intent.limit_price is None
                        or not math.isclose(
                            canonical_intent.limit_price,
                            float(risk.effective_entry_price),
                            rel_tol=0.0,
                            abs_tol=1e-12,
                        )
                    ):
                        reasons.append(
                            "adaptive_risk_canonical_entry_intent_mismatch"
                        )
            if available_at != checkpoint.available_at:
                reasons.append("adaptive_risk_available_clock_mismatch")
            committed_packet_sha256 = str(
                checkpoint.decision_payload.get(
                    "adaptive_risk_decision_packet_sha256"
                )
                or ""
            ).strip().lower()
            if committed_packet_sha256 != risk.decision_packet_sha256:
                reasons.append("adaptive_risk_packet_not_committed_by_fsm_decision")
            committed_economics = checkpoint.decision_payload.get(
                "resolved_economics"
            )
            expected_economics = {
                "economic_resolution_sha256": risk.economic_resolution_sha256,
                "quantity_shares": int(risk.quantity_shares),
                "effective_entry_price": float(risk.effective_entry_price),
                "effective_stop_exit_price": float(risk.effective_stop_exit_price),
                "risk_per_share_usd": float(risk.risk_per_share_usd),
                "planned_structural_risk_usd": float(
                    risk.planned_structural_risk_usd
                ),
                "planned_notional_usd": float(risk.planned_notional_usd),
                "planned_buying_power_impact_usd": float(
                    risk.planned_buying_power_impact_usd
                ),
            }
            if not isinstance(committed_economics, Mapping) or dict(
                committed_economics
            ) != expected_economics:
                reasons.append("adaptive_risk_economics_not_committed_by_fsm_decision")
            committed_order_intent = checkpoint.decision_payload.get("order_intent")
            expected_order_intent = {
                "symbol": normalized_symbol,
                "side": "long",
                "execution_family": normalized_family,
                "venue": expected_venue,
                "quantity_shares": int(risk.quantity_shares),
                "reference_entry_price": float(risk.effective_entry_price),
                "structural_stop_exit_price": float(
                    risk.effective_stop_exit_price
                ),
            }
            if not isinstance(committed_order_intent, Mapping) or dict(
                committed_order_intent
            ) != expected_order_intent:
                reasons.append("adaptive_risk_order_intent_not_committed_by_fsm_decision")
        if str(inputs.get("capture_prefix_root_sha256") or "").strip().lower() != prefix_root:
            reasons.append("adaptive_risk_capture_prefix_mismatch")
        if request.symbol != normalized_symbol:
            reasons.append("coverage_request_symbol_mismatch")
        if str(inputs.get("decision_id") or "").strip() != request.decision_id:
            reasons.append("adaptive_risk_decision_id_mismatch")
        if str(inputs.get("replay_or_paper_run_id") or "").strip() != identity.run_id:
            reasons.append("adaptive_risk_run_id_mismatch")
        if int(inputs.get("generation") or 0) != identity.generation:
            reasons.append("adaptive_risk_generation_mismatch")
        identity_hash_fields = {
            "account_identity_sha256": identity.account_identity_sha256,
            "code_build_sha256": identity.code_build_sha256,
            "effective_config_sha256": identity.config_sha256,
            "feature_flags_sha256": identity.feature_flags_sha256,
        }
        for input_name, expected_hash in identity_hash_fields.items():
            if str(inputs.get(input_name) or "").strip().lower() != expected_hash:
                reasons.append(f"adaptive_risk_{input_name}_mismatch")
        decision_at_raw = inputs.get("as_of")
        if not isinstance(decision_at_raw, datetime) or decision_at_raw.tzinfo is None:
            reasons.append("adaptive_risk_decision_clock_missing")
        else:
            decision_at = decision_at_raw.astimezone(timezone.utc)
            if decision_at != request.decision_at:
                reasons.append("adaptive_risk_decision_clock_mismatch")
            if available_at < decision_at:
                reasons.append("adaptive_risk_available_before_decision")
            if available_at > request.exit_end_at:
                reasons.append("adaptive_risk_available_after_replay_window")
        evidence = inputs.get("evidence", {})
        capture_evidence = evidence.get("capture_prefix")
        if not isinstance(capture_evidence, Mapping) or str(
            capture_evidence.get("content_sha256") or ""
        ).strip().lower() != prefix_root:
            reasons.append("adaptive_risk_capture_prefix_evidence_hash_mismatch")
        identity_evidence = {
            "code_build": identity.code_build_sha256,
            "effective_config": identity.config_sha256,
            "feature_flags": identity.feature_flags_sha256,
        }
        for evidence_name, expected_hash in identity_evidence.items():
            item = evidence.get(evidence_name)
            if not isinstance(item, Mapping) or str(
                item.get("content_sha256") or ""
            ).strip().lower() != expected_hash:
                reasons.append(f"adaptive_risk_{evidence_name}_evidence_mismatch")
        prefix_refs = [
            ref
            for ref in self.coverage_manifest.event_index.values()
            if checkpoint is not None and ref.sequence <= checkpoint.input_prefix_sequence
        ]
        captured_content_hashes = {
            value
            for ref in prefix_refs
            for value in (ref.event_sha256, ref.payload_sha256)
        }
        checkpoint_read_ids = set(checkpoint.required_read_ids if checkpoint else ())
        captured_content_hashes.update(
            receipt.result_sha256
            for receipt in self.coverage_manifest.read_receipts
            if receipt.read_id in checkpoint_read_ids
        )
        exempt_evidence = {
            "capture_prefix",
            "code_build",
            "effective_config",
            "feature_flags",
        }
        for evidence_name, item in evidence.items():
            if evidence_name in exempt_evidence or not isinstance(item, Mapping):
                continue
            if str(item.get("content_sha256") or "").strip().lower() not in captured_content_hashes:
                reasons.append(
                    f"adaptive_risk_evidence_not_captured:{evidence_name}"
                )

        quantity = int(risk.quantity_shares)
        expected_risk = quantity * float(risk.risk_per_share_usd)
        expected_notional = quantity * float(risk.effective_entry_price)
        if not math.isclose(
            float(risk.planned_structural_risk_usd),
            expected_risk,
            rel_tol=1e-10,
            abs_tol=1e-8,
        ):
            reasons.append("adaptive_risk_planned_risk_inconsistent")
        if not math.isclose(
            float(risk.planned_notional_usd),
            expected_notional,
            rel_tol=1e-10,
            abs_tol=1e-8,
        ):
            reasons.append("adaptive_risk_planned_notional_inconsistent")

        # Replay must enter through the same upstream builder as paper.  The
        # packet verifier above proves internal resolver consistency; this
        # second pass binds that exact raw policy/input snapshot to a diagnostic
        # prefix identity and requires byte-identical packet/quantity economics.
        # The coverage grade above independently validates the private-attested
        # sealed capture; this ordinary digest is never promoted to attestation.
        rebuilt = None
        if checkpoint is not None and isinstance(capture_evidence, Mapping):
            try:
                capture_binding = AdaptiveRiskDiagnosticCaptureBinding.create_diagnostic(
                    run_id=identity.run_id,
                    generation=identity.generation,
                    decision_id=request.decision_id,
                    input_prefix_sequence=checkpoint.input_prefix_sequence,
                    input_prefix_root_sha256=prefix_root,
                    identity_sha256=identity.identity_sha256,
                    observed_at=capture_evidence.get("observed_at"),
                    available_at=capture_evidence.get("available_at"),
                    verifier_generation=str(
                        capture_evidence.get("provider_generation") or ""
                    ),
                )
                rebuilt = rebuild_adaptive_risk_decision_packet(
                    self.risk_decision_packet,
                    capture_binding,
                )
                if rebuilt.parity_payload["quantity_shares"] != quantity:
                    reasons.append("adaptive_risk_builder_quantity_mismatch")
                if (
                    rebuilt.parity_payload["economic_resolution_sha256"]
                    != risk.economic_resolution_sha256
                ):
                    reasons.append("adaptive_risk_builder_economics_mismatch")
            except (AdaptiveRiskBuilderError, TypeError, ValueError) as exc:
                reason = getattr(exc, "reason", type(exc).__name__)
                reasons.append(f"adaptive_risk_builder_parity_failed:{reason}")
        else:
            reasons.append("adaptive_risk_builder_capture_binding_missing")

        if reasons:
            raise ValueError(
                "ReplayV3 economic seed is not certifiable: " + ",".join(reasons)
            )
        assert rebuilt is not None
        return rebuilt.resolution, dict(rebuilt.decision_packet), grade


# ── recorded inputs (the driver's data contract) ─────────────────────────────────
@dataclass(frozen=True)
class RecordedNbboTick:
    """One recorded NBBO snapshot at ``ts`` (naive-UTC). Mirrors ``momentum_nbbo_spread_tape``."""

    ts: datetime
    bid: float
    ask: float
    last: Optional[float] = None

    def as_quote(self) -> RecordedQuote:
        return RecordedQuote(bid=self.bid, ask=self.ask, last=self.last)


@dataclass
class RecordedArm:
    """A recorded live arm to seed: symbol + the confirm-time live-eligibility anchor.

    ``live_eligible_at_utc`` is the anchor ``confirm_live_arm`` stamps (the recency-grace
    keys off it) — seeded onto ``risk_snapshot_json['live_eligible_at_utc']`` so the grace is
    EXERCISABLE in P2 even though P1 enters via the happy (live_eligible=True) path."""

    symbol: str
    live_eligible_at_utc: str
    viability_score: float = 0.9
    atr_pct: float = 0.02
    user_id: Optional[int] = None
    variant_id: Optional[int] = None
    account_identity: str = REPLAY_MOCK_ACCOUNT_IDENTITY
    economic_seed_evidence: Optional[ReplayEconomicSeedEvidence] = None


@dataclass
class ReplaySeed:
    """The fully-seeded session handle the driver steps."""

    session_id: int
    symbol: str
    variant_id: int
    user_id: int
    economic_seed_mode: str = "legacy_config_diagnostic"
    adaptive_risk_decision_sha256: Optional[str] = None
    adaptive_risk_available_at: Optional[datetime] = None


@dataclass
class TickTrace:
    """One grid step's outcome (the per-tick decision trace, the parity-harness input)."""

    ts: datetime
    state_before: str
    state_after: str
    result: dict[str, Any]


@dataclass
class ReplayResult:
    """The end-to-end run trace: the FSM state path + the mock's fills + the event log."""

    states_visited: list[str] = field(default_factory=list)
    ticks: list[TickTrace] = field(default_factory=list)
    final_state: str = ""
    entry_fill_price: Optional[float] = None
    exit_fill_prices: list[float] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    economic_seed_mode: str = ""
    certification_eligible: bool = False
    certification_failures: list[str] = field(default_factory=list)
    sealed_run_binding: Optional["ReplayV3RunBinding"] = None
    sealed_execution_receipt: Optional["ReplayV3ExecutionReceipt"] = None
    os_zero_egress_attestation: Optional["ReplayOsZeroEgressAttestation"] = None


REPLAY_V3_RUN_BINDING_SCHEMA_VERSION = "chili.replay-v3-run-binding.v1"
REPLAY_OS_ZERO_EGRESS_ATTESTATION_SCHEMA_VERSION = (
    "chili.replay-v3-os-zero-egress-attestation.v1"
)
_REPLAY_V3_EXECUTION_RECEIPT_TOKEN = object()
_REPLAY_OS_ZERO_EGRESS_ATTESTATION_TOKEN = object()


@dataclass(frozen=True)
class ReplayV3RunBinding:
    """Content address of one exact sealed-input execution of the real FSM."""

    identity_sha256: str
    final_capture_seal_sha256: str
    manifest_sha256: str
    release_order_root_sha256: str
    decision_checkpoint_sha256: str
    result_trace_sha256: str
    broker_lifecycle_root_sha256: str
    adapter_network_attempt_count: int
    python_network_attempt_count: int
    adapter_rejected_provider_request_count: int
    schema_version: str = REPLAY_V3_RUN_BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != REPLAY_V3_RUN_BINDING_SCHEMA_VERSION:
            raise SealedReplayInputError("ReplayV3 run binding schema is invalid")
        for name in (
            "identity_sha256",
            "final_capture_seal_sha256",
            "manifest_sha256",
            "release_order_root_sha256",
            "decision_checkpoint_sha256",
            "result_trace_sha256",
            "broker_lifecycle_root_sha256",
        ):
            value = str(getattr(self, name) or "").strip().lower()
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise SealedReplayInputError(f"ReplayV3 run binding {name} is invalid")
            object.__setattr__(self, name, value)
        for name in (
            "adapter_network_attempt_count",
            "python_network_attempt_count",
            "adapter_rejected_provider_request_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) < 0:
                raise SealedReplayInputError(f"ReplayV3 run binding {name} is invalid")
            object.__setattr__(self, name, int(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity_sha256": self.identity_sha256,
            "final_capture_seal_sha256": self.final_capture_seal_sha256,
            "manifest_sha256": self.manifest_sha256,
            "release_order_root_sha256": self.release_order_root_sha256,
            "decision_checkpoint_sha256": self.decision_checkpoint_sha256,
            "result_trace_sha256": self.result_trace_sha256,
            "broker_lifecycle_root_sha256": self.broker_lifecycle_root_sha256,
            "adapter_network_attempt_count": self.adapter_network_attempt_count,
            "python_network_attempt_count": self.python_network_attempt_count,
            "adapter_rejected_provider_request_count": (
                self.adapter_rejected_provider_request_count
            ),
        }

    @property
    def run_binding_sha256(self) -> str:
        return sha256_json(self.to_dict())

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReplayV3RunBinding":
        expected = {
            "schema_version",
            "identity_sha256",
            "final_capture_seal_sha256",
            "manifest_sha256",
            "release_order_root_sha256",
            "decision_checkpoint_sha256",
            "result_trace_sha256",
            "broker_lifecycle_root_sha256",
            "adapter_network_attempt_count",
            "python_network_attempt_count",
            "adapter_rejected_provider_request_count",
        }
        if set(raw) != expected:
            raise SealedReplayInputError("ReplayV3 run binding fields do not match schema")
        return cls(**dict(raw))


@dataclass(frozen=True)
class ReplayV3ExecutionReceipt:
    """Non-serializable in-process receipt issued only by ``ReplayV3Driver``."""

    binding: ReplayV3RunBinding
    _verification_token: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _REPLAY_V3_EXECUTION_RECEIPT_TOKEN:
            raise SealedReplayInputError(
                "ReplayV3 execution receipt token is invalid"
            )
        if not isinstance(self.binding, ReplayV3RunBinding):
            raise SealedReplayInputError("ReplayV3 execution receipt is malformed")


@dataclass(frozen=True)
class ReplayOsZeroEgressAttestation:
    """OS namespace proof bound to one already-completed exact replay run.

    This object is produced only after the replay process has run inside the
    isolated namespace.  A generic/synthetic namespace check cannot be attached
    to another capture because the exact ``run_binding_sha256`` is committed.
    """

    run_binding_sha256: str
    network_namespace: str
    non_loopback_interfaces: tuple[str, ...]
    non_loopback_routes: tuple[str, ...]
    blocked_connect_ex: int
    database_transport: str
    adapter_network_attempt_count: int
    python_network_attempt_count: int
    schema_version: str = REPLAY_OS_ZERO_EGRESS_ATTESTATION_SCHEMA_VERSION
    _verification_token: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema_version != REPLAY_OS_ZERO_EGRESS_ATTESTATION_SCHEMA_VERSION:
            raise SealedReplayInputError("OS zero-egress attestation schema is invalid")
        if self._verification_token is not _REPLAY_OS_ZERO_EGRESS_ATTESTATION_TOKEN:
            raise SealedReplayInputError(
                "OS zero-egress attestation lacks trusted in-process provenance"
            )
        digest = str(self.run_binding_sha256 or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise SealedReplayInputError("OS zero-egress run binding hash is invalid")
        object.__setattr__(self, "run_binding_sha256", digest)
        object.__setattr__(
            self,
            "non_loopback_interfaces",
            tuple(str(value) for value in self.non_loopback_interfaces),
        )
        object.__setattr__(
            self,
            "non_loopback_routes",
            tuple(str(value) for value in self.non_loopback_routes),
        )
        if (
            self.network_namespace != "none"
            or self.non_loopback_interfaces
            or self.non_loopback_routes
            or self.database_transport != "unix_domain_socket"
            or isinstance(self.blocked_connect_ex, bool)
            or int(self.blocked_connect_ex) == 0
            or isinstance(self.adapter_network_attempt_count, bool)
            or int(self.adapter_network_attempt_count) != 0
            or isinstance(self.python_network_attempt_count, bool)
            or int(self.python_network_attempt_count) != 0
        ):
            raise SealedReplayInputError("OS zero-egress attestation is not fail-closed")
        object.__setattr__(self, "blocked_connect_ex", int(self.blocked_connect_ex))
        object.__setattr__(
            self,
            "adapter_network_attempt_count",
            int(self.adapter_network_attempt_count),
        )
        object.__setattr__(
            self,
            "python_network_attempt_count",
            int(self.python_network_attempt_count),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_binding_sha256": self.run_binding_sha256,
            "network_namespace": self.network_namespace,
            "non_loopback_interfaces": list(self.non_loopback_interfaces),
            "non_loopback_routes": list(self.non_loopback_routes),
            "blocked_connect_ex": self.blocked_connect_ex,
            "database_transport": self.database_transport,
            "adapter_network_attempt_count": self.adapter_network_attempt_count,
            "python_network_attempt_count": self.python_network_attempt_count,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReplayOsZeroEgressAttestation":
        expected = {
            "schema_version",
            "run_binding_sha256",
            "network_namespace",
            "non_loopback_interfaces",
            "non_loopback_routes",
            "blocked_connect_ex",
            "database_transport",
            "adapter_network_attempt_count",
            "python_network_attempt_count",
        }
        if set(raw) != expected:
            raise SealedReplayInputError(
                "OS zero-egress attestation fields do not match schema"
            )
        interfaces = raw.get("non_loopback_interfaces")
        routes = raw.get("non_loopback_routes")
        if not isinstance(interfaces, list) or not isinstance(routes, list):
            raise SealedReplayInputError("OS zero-egress route inventory is malformed")
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            run_binding_sha256=str(raw.get("run_binding_sha256") or ""),
            network_namespace=str(raw.get("network_namespace") or ""),
            non_loopback_interfaces=tuple(str(value) for value in interfaces),
            non_loopback_routes=tuple(str(value) for value in routes),
            blocked_connect_ex=raw.get("blocked_connect_ex"),
            database_transport=str(raw.get("database_transport") or ""),
            adapter_network_attempt_count=raw.get("adapter_network_attempt_count"),
            python_network_attempt_count=raw.get("python_network_attempt_count"),
        )


def apply_os_zero_egress_attestation(
    result: ReplayResult,
    attestation: ReplayOsZeroEgressAttestation,
) -> ReplayResult:
    """Attach OS proof only when it names this exact sealed run."""

    binding = result.sealed_run_binding
    receipt = result.sealed_execution_receipt
    if binding is None or receipt is None:
        raise SealedReplayInputError(
            "OS zero-egress attestation cannot certify a diagnostic replay"
        )
    if not isinstance(attestation, ReplayOsZeroEgressAttestation):
        raise SealedReplayInputError("OS zero-egress attestation is malformed")
    if (
        receipt._verification_token is not _REPLAY_V3_EXECUTION_RECEIPT_TOKEN
        or receipt.binding != binding
        or
        attestation.run_binding_sha256 != binding.run_binding_sha256
        or attestation.adapter_network_attempt_count
        != binding.adapter_network_attempt_count
        or binding.adapter_network_attempt_count != 0
        or attestation.python_network_attempt_count
        != binding.python_network_attempt_count
        or binding.python_network_attempt_count != 0
    ):
        raise SealedReplayInputError(
            "OS zero-egress attestation does not bind the exact replay run"
        )
    result.os_zero_egress_attestation = attestation
    result.certification_failures = [
        reason
        for reason in result.certification_failures
        if reason != "os_level_external_network_denial_not_proven"
    ]
    result.certification_eligible = not result.certification_failures
    return result


def publish_replay_v3_run_binding(
    result: ReplayResult,
    path: str | Path,
) -> Path:
    """Publish only an exact sealed run binding for the OS gate to attest.

    Diagnostic runs and runs that attempted an uncaptured provider path cannot
    create the handoff file consumed by ``run_replay_zero_egress_gate.py``.
    """

    binding = result.sealed_run_binding
    receipt = result.sealed_execution_receipt
    if (
        binding is None
        or receipt is None
        or receipt._verification_token is not _REPLAY_V3_EXECUTION_RECEIPT_TOKEN
        or receipt.binding != binding
    ):
        raise SealedReplayInputError(
            "replay has no trusted exact execution receipt to publish"
        )
    if (
        binding.adapter_network_attempt_count != 0
        or binding.python_network_attempt_count != 0
        or binding.adapter_rejected_provider_request_count != 0
    ):
        raise SealedReplayInputError(
            "replay with provider/network fallback evidence cannot be published"
        )
    destination = Path(path)
    destination.write_text(
        json.dumps(binding.to_dict(), sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return destination


# Strict, versioned payload schemas for the sealed ReplayV3 path. Producers
# translate provider responses into these envelopes before capture; replay never
# guesses that an arbitrary mapping is equivalent evidence.
SEALED_REPLAY_NBBO_SCHEMA_VERSION = NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION
SEALED_REPLAY_IQFEED_PRINT_SCHEMA_VERSION = IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION
SEALED_REPLAY_OHLCV_QUERY_SCHEMA_VERSION = (
    PROVIDER_OHLCV_QUERY_SCHEMA_VERSION
)
SEALED_REPLAY_OHLCV_SCHEMA_VERSION = PROVIDER_OHLCV_PAYLOAD_SCHEMA_VERSION
SEALED_REPLAY_ELIGIBILITY_SCHEMA_VERSION = (
    "chili.replay-v3-input.admission-eligibility.v1"
)
SEALED_REPLAY_ACCOUNT_RISK_SCHEMA_VERSION = (
    "chili.replay-v3-input.account-risk.v1"
)
SEALED_REPLAY_ACCOUNT_RISK_QUERY_SCHEMA_VERSION = (
    "chili.replay-v3-input.account-risk-query.v1"
)
SEALED_REPLAY_BROKER_LIFECYCLE_SCHEMA_VERSION = (
    "chili.replay-v3-input.broker-lifecycle.v1"
)
SEALED_REPLAY_SCANNER_SNAPSHOT_SCHEMA_VERSION = (
    SCANNER_SNAPSHOT_PAYLOAD_SCHEMA_VERSION
)

_SEALED_REPLAY_SUPPORTED_STREAMS = frozenset(
    {
        CaptureStream.NBBO_QUOTE,
        CaptureStream.IQFEED_PRINT,
        CaptureStream.PROVIDER_OHLCV,
        CaptureStream.ADMISSION_ELIGIBILITY,
        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
        CaptureStream.SCANNER_SNAPSHOT,
        CaptureStream.BROKER_ORDER_LIFECYCLE,
    }
)
_SEALED_REPLAY_QUERY_STREAMS = frozenset(
    {
        CaptureStream.PROVIDER_OHLCV,
        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
        CaptureStream.SCANNER_SNAPSHOT,
    }
)


class SealedReplayInputError(CaptureContractError):
    """A sealed fact cannot be used without ambiguity or fallback."""


@dataclass(frozen=True)
class SealedReplayInputProof:
    """Content roots for the exact sealed input schedule admitted by ReplayV3.

    ``adapter_network_attempt_count`` describes this adapter only. It is not an
    OS-egress proof, and parsing broker lifecycle facts is not the same as
    replaying their transitions through the broker/FSM boundary.
    """

    identity_sha256: str
    final_capture_seal_sha256: str
    manifest_sha256: str
    release_order_root_sha256: str
    decision_checkpoint_sha256: str
    decision_id: str
    decision_at: datetime
    checkpoint_available_at: datetime
    input_prefix_sequence: int
    input_prefix_root_sha256: str
    adapter_network_attempt_count: int = 0
    os_level_external_network_denial_proven: bool = False
    broker_lifecycle_replayed: bool = False


@dataclass(frozen=True)
class SealedReplayInputRelease:
    """Facts that became visible at one monotonic ``available_at`` boundary."""

    available_at: datetime
    event_sha256s: tuple[str, ...]
    streams: tuple[CaptureStream, ...]


@dataclass(frozen=True)
class _SealedNbboInput:
    event: CaptureEvent
    quote: RecordedQuote


@dataclass(frozen=True)
class _SealedIqfeedPrintInput:
    """One exact IQFeed trade print, never a Q-frame time proxy."""

    event: CaptureEvent
    price: float
    size: float
    bid: float | None
    ask: float | None

    def tape_row(self) -> tuple[float, float, float | None, float | None, float]:
        event_at = self.event.clocks.provider_event_at
        if event_at is None:  # defensive; the stream contract already requires it
            raise SealedReplayInputError(
                "sealed IQFeed print exact provider clock is missing"
            )
        return (self.price, self.size, self.bid, self.ask, event_at.timestamp())


@dataclass(frozen=True)
class _SealedOhlcvInput:
    event: CaptureEvent
    call_key: tuple[str, str, str]
    rows: tuple[tuple[datetime, float, float, float, float, float], ...]

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Open": [row[1] for row in self.rows],
                "High": [row[2] for row in self.rows],
                "Low": [row[3] for row in self.rows],
                "Close": [row[4] for row in self.rows],
                "Volume": [row[5] for row in self.rows],
            },
            index=pd.DatetimeIndex([row[0] for row in self.rows]),
        )


@dataclass(frozen=True)
class _SealedEligibilityInput:
    event: CaptureEvent
    eligible: bool
    freshness_at: datetime


@dataclass(frozen=True)
class _SealedAccountRiskInput:
    event: CaptureEvent


@dataclass(frozen=True)
class _SealedScannerSnapshotInput:
    """One exact symbol projection consumed by the Ross universe risk seam."""

    event: CaptureEvent
    snapshot: CaptureScannerSnapshot
    snapshot_row: Mapping[str, Any]


@dataclass(frozen=True)
class _SealedBrokerLifecycleInput:
    event: CaptureEvent
    broker_order_id: str
    event_type: str
    canonical: Optional[CaptureBrokerOrderLifecycle] = None
    intent: Optional[CaptureOrderIntent] = None


@dataclass(frozen=True)
class _SealedDecisionRead:
    """One exact query receipt consumed once by one captured FSM tick."""

    receipt: CaptureReadReceipt
    source_event_sha256s: tuple[str, ...]


@dataclass(frozen=True)
class _SealedFirstDipFinalFrontier:
    """Independently re-inventoried final tape/request evidence for one tick."""

    frontier: FirstDipFinalCaptureFrontier
    dependency_profile: FSMDependencyProfile
    policy: FirstDipTapePolicy
    evaluation: FirstDipTapeEvaluation
    adaptive_request: Any
    prior_detector_reference: _FirstDipPriorDetectorReference
    tape_read: _SealedDecisionRead
    tape_read_evidence: ActiveCaptureReadEvidence
    tape_continuity_evidence: ActiveCaptureContinuityEvidence


@dataclass(frozen=True)
class _SealedDecisionTick:
    """One captured invocation of the real FSM.

    Market/provider facts are released on every causal frontier, but they do not
    themselves prove that the live loop invoked ``tick_live_session``.  Only an
    exact, manifest-bound FSM decision checkpoint may schedule a replay tick.
    """

    checkpoint: CaptureDecisionCheckpoint
    event: CaptureEvent
    output: CaptureDecisionOutput
    # Diagnostic evidence for validating that every causal source fact was
    # available by decision_at. Durable receipt/watermark/health proof controls
    # may publish later, through checkpoint.available_at. This is not the replay
    # clock: the real FSM is always invoked at checkpoint.decision_at.
    input_prefix_available_at: datetime
    required_streams: frozenset[CaptureStream]
    receipt_source_event_sha256s: tuple[
        tuple[CaptureStream, tuple[str, ...]], ...
    ] = ()
    decision_read_plan: tuple[_SealedDecisionRead, ...] = ()
    query_read_plan: tuple[_SealedDecisionRead, ...] = ()
    first_dip_final_frontier: _SealedFirstDipFinalFrontier | None = None

    @property
    def frontier(self) -> tuple[datetime, int | None]:
        return (
            self.checkpoint.decision_at,
            self.checkpoint.input_prefix_sequence,
        )

    def receipt_sources_for(self, stream: CaptureStream) -> frozenset[str]:
        for candidate, values in self.receipt_source_event_sha256s:
            if candidate is stream:
                return frozenset(values)
        return frozenset()

    def receipt_reads_for(
        self, stream: CaptureStream
    ) -> tuple[_SealedDecisionRead, ...]:
        return tuple(
            row for row in self.decision_read_plan if row.receipt.stream is stream
        )


def _sealed_payload_fields(
    payload: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    description: str,
) -> None:
    keys = frozenset(str(key) for key in payload)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise SealedReplayInputError(
            f"{description} payload fields do not match the sealed ReplayV3 schema"
        )


def _sealed_finite_number(
    value: Any,
    field_name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> float:
    if isinstance(value, bool):
        raise SealedReplayInputError(f"{field_name} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SealedReplayInputError(
            f"{field_name} must be a finite number"
        ) from exc
    if not math.isfinite(parsed):
        raise SealedReplayInputError(f"{field_name} must be a finite number")
    if positive and parsed <= 0.0:
        raise SealedReplayInputError(f"{field_name} must be positive")
    if nonnegative and parsed < 0.0:
        raise SealedReplayInputError(f"{field_name} cannot be negative")
    return parsed


def _sealed_payload_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SealedReplayInputError(f"{field_name} must be an ISO-8601 UTC instant")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise SealedReplayInputError(
            f"{field_name} must be an ISO-8601 UTC instant"
        ) from exc
    if parsed.tzinfo is None:
        raise SealedReplayInputError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _sealed_first_dip_receipt_inventory(
    *,
    receipt: CaptureReadReceipt,
    checkpoint: CaptureDecisionCheckpoint,
    manifest: CaptureCoverageManifest,
) -> tuple[CaptureEventRef, ...]:
    """Defense-in-depth wrapper over the coverage grader's shared verifier."""

    try:
        return verify_first_dip_receipt_inventory(
            receipt=receipt,
            checkpoint=checkpoint,
            manifest=manifest,
        )
    except CaptureContractError as exc:
        raise SealedReplayInputError(str(exc)) from exc


def _sealed_microstructure_receipt_inventory(
    *,
    receipt: CaptureReadReceipt,
    checkpoint: CaptureDecisionCheckpoint,
    manifest: CaptureCoverageManifest,
) -> tuple[CaptureMicrostructureReadQuery, tuple[CaptureEventRef, ...]]:
    """Re-inventory one runtime-owned microstructure window from sealed refs."""

    try:
        query = CaptureMicrostructureReadQuery.from_dict(receipt.query or {})
    except (CaptureContractError, TypeError, ValueError) as exc:
        raise SealedReplayInputError(
            "sealed microstructure receipt query is malformed"
        ) from exc
    if (
        receipt.identity_sha256 != manifest.identity.identity_sha256
        or receipt.stream is not query.stream
        or receipt.provider != query.provider
        or receipt.symbol != query.symbol
        or receipt.decision_id != checkpoint.decision_id
        or receipt.read_id not in checkpoint.required_read_ids
        or query.symbol != checkpoint.symbol
        or query.decision_at != checkpoint.decision_at
        or query.available_at_most != receipt.returned_at
        or receipt.query_sha256 != sha256_json(query.to_dict())
    ):
        raise SealedReplayInputError(
            "sealed microstructure receipt escaped its exact decision query"
        )
    clock_name = query.source_clock_basis
    visible = tuple(
        ref
        for ref in manifest.event_index.values()
        if ref.stream is query.stream
        and ref.provider == query.provider
        and ref.symbol == query.symbol
        and ref.sequence <= checkpoint.input_prefix_sequence
        and ref.available_at <= query.available_at_most
    )
    if any(getattr(ref, clock_name) is None for ref in visible):
        raise SealedReplayInputError(
            "sealed microstructure source lacks its exact event clock"
        )
    actual_frontier = max((ref.sequence for ref in visible), default=0)
    selected = tuple(
        sorted(
            (
                ref
                for ref in visible
                if getattr(ref, clock_name) > query.event_start_exclusive
                and getattr(ref, clock_name) <= query.event_end_inclusive
            ),
            key=lambda ref: (getattr(ref, clock_name), ref.sequence),
        )
    )
    if (
        actual_frontier != query.source_frontier_sequence
        or tuple(ref.event_sha256 for ref in selected)
        != receipt.source_event_sha256s
        or receipt.empty_result != (not selected)
        or captured_read_result_sha256(selected) != receipt.result_sha256
    ):
        raise SealedReplayInputError(
            "sealed microstructure receipt is not the complete source window"
        )
    return query, selected


def _sealed_first_dip_final_frontier_inventory(
    *,
    checkpoint: CaptureDecisionCheckpoint,
    decision_event: CaptureEvent,
    capture: VerifiedReplayCapture,
    manifest: CaptureCoverageManifest,
) -> _SealedFirstDipFinalFrontier | None:
    """Recompute a final first-dip frontier from sealed bytes, never its claim.

    The live frontier is an ordinary content-addressed record.  This loader does
    not trust its hashes as authority: it re-inventories the exact receipt
    commits, source events, cumulative continuity checkpoints, prefix root,
    adaptive request, detector lineage, policy, and evaluation.  The returned
    object is still evidence-only; a fresh sealed-replay capability must be
    minted later at the active FSM boundary.
    """

    raw = checkpoint.decision_payload.get("first_dip_final_capture_frontier")
    supplied_sha = checkpoint.decision_payload.get(
        "first_dip_final_capture_frontier_sha256"
    )
    if raw is None and supplied_sha is None:
        return None
    if not isinstance(raw, Mapping) or not isinstance(supplied_sha, str):
        raise SealedReplayInputError(
            "sealed first-dip final frontier payload/hash is incomplete"
        )
    try:
        frontier = FirstDipFinalCaptureFrontier.from_dict(raw)
        profile_raw = json.loads(frontier.dependency_profile_canonical_json)
        profile = FSMDependencyProfile.from_dict(profile_raw)
        policy = FirstDipTapePolicy.from_dict(
            json.loads(frontier.policy_canonical_json)
        )
        recorded_evaluation = FirstDipTapeEvaluation.from_dict(
            json.loads(frontier.evaluation_canonical_json)
        )
        from .adaptive_risk_reservation import (  # noqa: PLC0415
            load_adaptive_risk_reservation_request,
        )

        adaptive_request = load_adaptive_risk_reservation_request(
            json.loads(frontier.adaptive_request_canonical_json)
        )
        prior_reference = _FirstDipPriorDetectorReference.from_dict(
            json.loads(frontier.prior_detector_reference_canonical_json)
        )
        decision_output = capture_decision_output_from_payload(
            checkpoint.decision_payload
        )
    except (CaptureContractError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SealedReplayInputError(
            "sealed first-dip final frontier is malformed"
        ) from exc
    if supplied_sha.strip().lower() != frontier.frontier_sha256:
        raise SealedReplayInputError(
            "sealed first-dip final frontier content hash mismatch"
        )
    if decision_output.setup_role != "first_dip_reclaim":
        raise SealedReplayInputError(
            "sealed first-dip final frontier belongs to another setup"
        )

    identity = manifest.identity
    decision_ref = manifest.event_index.get(checkpoint.decision_event_sha256)
    if decision_ref is None or decision_ref.event_sha256 != decision_event.event_sha256:
        raise SealedReplayInputError(
            "sealed first-dip final frontier decision event is missing"
        )
    try:
        expected_prefix = capture_prefix_root_sha256(
            manifest.event_index.values(),
            identity_sha256=identity.identity_sha256,
            through_sequence=frontier.input_prefix_sequence,
        )
    except CaptureContractError as exc:
        raise SealedReplayInputError(
            "sealed first-dip final frontier prefix is incomplete"
        ) from exc
    prefix_refs = tuple(
        ref
        for ref in manifest.event_index.values()
        if ref.sequence <= frontier.input_prefix_sequence
    )
    if (
        frontier.run_id != identity.run_id
        or frontier.generation != identity.generation
        or frontier.identity_sha256 != identity.identity_sha256
        or expected_prefix != frontier.input_prefix_root_sha256
        or frontier.input_prefix_sequence <= checkpoint.input_prefix_sequence
        or frontier.input_prefix_sequence >= decision_event.sequence
        or frontier.attested_available_at > checkpoint.available_at
        or frontier.final_boundary_available_at > checkpoint.available_at
        or checkpoint.available_at > frontier.expires_at
        or not prefix_refs
        or max(ref.available_at for ref in prefix_refs)
        > frontier.attested_available_at
    ):
        raise SealedReplayInputError(
            "sealed first-dip final frontier escaped its decision/prefix boundary"
        )

    request_inputs = adaptive_request.inputs
    if (
        adaptive_request.request_sha256 != frontier.adaptive_request_sha256
        or adaptive_request.setup_family != "first_dip_reclaim"
        or request_inputs.execution_surface != "alpaca_paper"
        or adaptive_request.opportunity_key is None
        or adaptive_request.opportunity_key.key_sha256
        != frontier.opportunity_key_sha256
        or request_inputs.symbol != checkpoint.symbol
        or request_inputs.decision_id != checkpoint.decision_id
        or request_inputs.replay_or_paper_run_id != identity.run_id
        or request_inputs.generation != identity.generation
        or request_inputs.account_identity_sha256
        != identity.account_identity_sha256
        or request_inputs.code_build_sha256 != identity.code_build_sha256
        or request_inputs.effective_config_sha256 != identity.config_sha256
        or request_inputs.feature_flags_sha256
        != identity.feature_flags_sha256
        or identity.broker != "alpaca"
        or identity.broker_environment != "paper"
        or request_inputs.capture_prefix_root_sha256
        != checkpoint.input_prefix_root_sha256
        or request_inputs.as_of < checkpoint.decision_at
    ):
        raise SealedReplayInputError(
            "sealed first-dip adaptive request escaped detector provenance"
        )

    receipts_by_id = {row.read_id: row for row in manifest.read_receipts}
    detector_read_id = str(
        checkpoint.decision_payload.get("first_dip_tape_read_id") or ""
    ).strip()
    detector_receipt = receipts_by_id.get(detector_read_id)
    if detector_receipt is None:
        raise SealedReplayInputError(
            "sealed first-dip prior detector receipt is missing"
        )

    event_by_hash = {event.event_sha256: event for event in capture.events}
    run_open_rows = tuple(
        event
        for event in capture.events
        if event.payload.get("kind") == "RUN_OPEN"
    )
    if len(run_open_rows) != 1:
        raise SealedReplayInputError(
            "sealed first-dip final frontier producer roster is unavailable"
        )
    try:
        run_open = CaptureRunOpen.from_dict(run_open_rows[0].payload)
    except (CaptureContractError, TypeError, ValueError) as exc:
        raise SealedReplayInputError(
            "sealed first-dip final frontier producer roster is malformed"
        ) from exc
    owners: dict[CaptureStream, tuple[str, int]] = {}
    for stream in profile.required_streams:
        matches = tuple(row for row in run_open.producers if stream in row.streams)
        if len(matches) != 1:
            raise SealedReplayInputError(
                "sealed first-dip final frontier stream owner is ambiguous: "
                + stream.value
            )
        owners[stream] = (matches[0].producer_id, matches[0].generation)

    def receipt_commit(receipt: CaptureReadReceipt) -> CaptureEvent:
        receipt_sha = sha256_json(receipt.to_dict())
        matches = tuple(
            event
            for event in capture.events
            if event.stream is CaptureStream.READ_RECEIPT
            and event.payload_sha256 == receipt_sha
            and event.sequence <= frontier.input_prefix_sequence
        )
        if len(matches) != 1 or dict(matches[0].payload) != receipt.to_dict():
            raise SealedReplayInputError(
                "sealed first-dip read receipt commit is missing or ambiguous: "
                + receipt.read_id
            )
        return matches[0]

    read_evidence: list[ActiveCaptureReadEvidence] = []
    read_evidence_by_id: dict[str, ActiveCaptureReadEvidence] = {}
    final_reads: dict[str, _SealedDecisionRead] = {}
    for read_id in profile.required_read_ids:
        receipt = receipts_by_id.get(read_id)
        if receipt is None:
            raise SealedReplayInputError(
                "sealed first-dip final read receipt is missing: " + read_id
            )
        owner = owners.get(receipt.stream)
        commit = receipt_commit(receipt)
        source_events: list[CaptureEvent] = []
        source_refs: list[CaptureEventRef] = []
        for source_sha in receipt.source_event_sha256s:
            source = event_by_hash.get(source_sha)
            if source is None:
                raise SealedReplayInputError(
                    "sealed first-dip final receipt source is missing: " + read_id
                )
            ref = CaptureEventRef.from_event(source)
            if (
                source.sequence > frontier.input_prefix_sequence
                or source.stream is not receipt.stream
                or source.provider != receipt.provider
                or source.symbol != receipt.symbol
                or source.clocks.available_at > receipt.returned_at
                or (
                    STREAM_POLICIES[receipt.stream].query_parameters_required
                    and source.query_sha256 != receipt.query_sha256
                )
            ):
                raise SealedReplayInputError(
                    "sealed first-dip final receipt source escaped its prefix: "
                    + read_id
                )
            source_events.append(source)
            source_refs.append(ref)
        if (
            owner is None
            or receipt.decision_id != frontier.decision_id
            or receipt.identity_sha256 != identity.identity_sha256
            or commit.provider != receipt.provider
            or commit.symbol != receipt.symbol
            or commit.clocks.available_at > frontier.attested_available_at
            or captured_read_result_sha256(source_refs) != receipt.result_sha256
        ):
            raise SealedReplayInputError(
                "sealed first-dip final receipt binding is invalid: " + read_id
            )
        try:
            evidence = ActiveCaptureReadEvidence(
                receipt=receipt,
                receipt_sha256=sha256_json(receipt.to_dict()),
                receipt_event_sha256=commit.event_sha256,
                receipt_event_sequence=commit.sequence,
                receipt_committed_available_at=commit.clocks.available_at,
                producer_id=owner[0],
                producer_generation=owner[1],
                source_event_refs=tuple(source_refs),
            )
        except CaptureContractError as exc:
            raise SealedReplayInputError(
                "sealed first-dip final read inventory is invalid: " + read_id
            ) from exc
        read_evidence.append(evidence)
        read_evidence_by_id[read_id] = evidence
        final_reads[read_id] = _SealedDecisionRead(
            receipt=receipt,
            source_event_sha256s=receipt.source_event_sha256s,
        )
    if sha256_json(
        {
            "read_evidence": [
                row.to_evidence_dict()
                for row in sorted(
                    read_evidence,
                    key=lambda item: item.receipt.read_id,
                )
            ]
        }
    ) != frontier.read_evidence_inventory_sha256:
        raise SealedReplayInputError(
            "sealed first-dip final read inventory digest mismatch"
        )

    continuity_evidence: list[ActiveCaptureContinuityEvidence] = []
    continuity_evidence_by_stream: dict[
        CaptureStream, ActiveCaptureContinuityEvidence
    ] = {}
    continuity_streams = tuple(
        stream
        for stream in profile.required_streams
        if STREAM_POLICIES[stream].coverage_mode
        in {CoverageMode.CONTINUOUS, CoverageMode.CHANGE_LOG}
    )
    for stream in continuity_streams:
        candidates: list[tuple[CaptureEvent, StreamCoverage]] = []
        for event in capture.events:
            if (
                event.stream is not CaptureStream.CAPTURE_HEALTH
                or event.sequence > frontier.input_prefix_sequence
                or set(event.payload) != {"live_continuity_checkpoint", "coverage"}
                or event.payload.get("live_continuity_checkpoint") is not True
                or not isinstance(event.payload.get("coverage"), Mapping)
            ):
                continue
            try:
                coverage = StreamCoverage.from_dict(event.payload["coverage"])
            except (CaptureContractError, TypeError, ValueError):
                continue
            if coverage.stream is stream:
                candidates.append((event, coverage))
        if not candidates:
            raise SealedReplayInputError(
                "sealed first-dip final continuity checkpoint is missing: "
                + stream.value
            )
        coverage_event, coverage = max(
            candidates,
            key=lambda item: item[0].sequence,
        )
        watermark = coverage.watermark
        if watermark is None:
            raise SealedReplayInputError(
                "sealed first-dip final continuity watermark is missing: "
                + stream.value
            )
        watermark_rows = tuple(
            event
            for event in capture.events
            if event.stream is CaptureStream.PROVIDER_WATERMARK
            and event.sequence + 1 == coverage_event.sequence
            and dict(event.payload) == watermark.to_dict()
        )
        owner = owners.get(stream)
        source_rows = tuple(
            event
            for event in capture.events
            if event.stream is stream
            and event.provider == coverage.provider
            and event.symbol == coverage.symbol
            and event.sequence < coverage_event.sequence
        )
        if len(watermark_rows) != 1 or owner is None or not source_rows:
            raise SealedReplayInputError(
                "sealed first-dip final continuity evidence is ambiguous: "
                + stream.value
            )
        watermark_event = watermark_rows[0]
        first_available = min(row.clocks.available_at for row in source_rows)
        last_available = max(row.clocks.available_at for row in source_rows)
        exact_complete = all(
            row.clocks.provider_event_at is not None for row in source_rows
        )
        dependency = profile.dependency_for(stream)
        if (
            coverage.identity_sha256 != identity.identity_sha256
            or coverage.provider != source_rows[0].provider
            or coverage.symbol != checkpoint.symbol
            or coverage.event_count != len(source_rows)
            or coverage.first_available_at != first_available
            or coverage.last_available_at != last_available
            or coverage.exact_event_clock_complete != exact_complete
            or not coverage.content_verified
            or not coverage.continuity_complete
            or coverage.first_available_at > dependency.coverage_start_at
            or watermark.generation != owner[1]
            or watermark.event_watermark_at < recorded_evaluation.decision_at
            or watermark_event.clocks.market_reference_at
            != watermark.event_watermark_at
        ):
            raise SealedReplayInputError(
                "sealed first-dip final continuity coverage is incomplete: "
                + stream.value
            )
        try:
            continuity_row = ActiveCaptureContinuityEvidence(
                coverage=coverage,
                producer_id=owner[0],
                producer_generation=owner[1],
                source_frontier_sequence=max(row.sequence for row in source_rows),
                watermark_event_sha256=watermark_event.event_sha256,
                watermark_event_sequence=watermark_event.sequence,
                watermark_committed_available_at=(
                    watermark_event.clocks.available_at
                ),
                coverage_event_sha256=coverage_event.event_sha256,
                coverage_event_sequence=coverage_event.sequence,
                coverage_committed_available_at=(
                    coverage_event.clocks.available_at
                ),
            )
            continuity_evidence.append(continuity_row)
            continuity_evidence_by_stream[stream] = continuity_row
        except CaptureContractError as exc:
            raise SealedReplayInputError(
                "sealed first-dip final continuity inventory is invalid: "
                + stream.value
            ) from exc
    if sha256_json(
        {
            "continuity_evidence": [
                row.to_evidence_dict()
                for row in sorted(
                    continuity_evidence,
                    key=lambda item: item.coverage.stream.value,
                )
            ]
        }
    ) != frontier.continuity_evidence_inventory_sha256:
        raise SealedReplayInputError(
            "sealed first-dip final continuity inventory digest mismatch"
        )

    final_read = final_reads.get(frontier.first_dip_tape_read_id)
    if final_read is None or final_read.receipt.stream is not CaptureStream.IQFEED_PRINT:
        raise SealedReplayInputError(
            "sealed first-dip final tape receipt is missing"
        )
    final_receipt = final_read.receipt
    final_read_evidence = read_evidence_by_id.get(
        frontier.first_dip_tape_read_id
    )
    final_continuity_evidence = continuity_evidence_by_stream.get(
        CaptureStream.IQFEED_PRINT
    )
    if final_read_evidence is None or final_continuity_evidence is None:
        raise SealedReplayInputError(
            "sealed first-dip final tape evidence is incomplete"
        )
    if final_receipt.query is None:
        raise SealedReplayInputError(
            "sealed first-dip final tape query is missing"
        )
    try:
        query = FirstDipTapeReadQuery.from_dict(final_receipt.query)
        query.validate_for_policy(policy)
        causal_stream_events = tuple(
            event
            for event in capture.events
            if event.stream is CaptureStream.IQFEED_PRINT
            and event.provider == query.provider
            and event.symbol == query.symbol
            and event.sequence < final_read_evidence.receipt_event_sequence
            and event.clocks.available_at <= query.available_at_most
        )
        if (
            not causal_stream_events
            or max(event.sequence for event in causal_stream_events)
            != query.source_frontier_sequence
        ):
            raise CaptureContractError(
                "final tape source frontier differs from the receipt-time stream"
            )
        final_source_events = tuple(
            event_by_hash[value] for value in final_read.source_event_sha256s
        )
        window = first_dip_tape_window_from_capture(
            final_receipt,
            final_source_events,
        )
        evaluation = evaluate_first_dip_tape(
            window,
            policy=policy,
            decision_at=query.decision_at,
            symbol=checkpoint.symbol,
        )
    except (CaptureContractError, TypeError, ValueError, KeyError) as exc:
        raise SealedReplayInputError(
            "sealed first-dip final tape evaluation cannot be reproduced"
        ) from exc
    ordered_sources = tuple(
        sorted(
            final_source_events,
            key=lambda event: (
                event.clocks.provider_event_at,
                event.sequence,
            ),
        )
    )
    exact_query_sources = tuple(
        sorted(
            (
                event
                for event in causal_stream_events
                if event.clocks.provider_event_at is not None
                and query.event_start_exclusive
                < event.clocks.provider_event_at
                <= query.event_end_inclusive
            ),
            key=lambda event: (
                event.clocks.provider_event_at,
                event.sequence,
            ),
        )
    )
    final_elapsed = (
        frontier.final_boundary_available_at - evaluation.decision_at
    ).total_seconds()
    newest_age = evaluation.newest_source_age_seconds
    if (
        query.symbol != checkpoint.symbol
        or query.provider != final_receipt.provider
        or query.decision_at != final_receipt.returned_at
        or query.decision_at > frontier.attested_available_at
        or request_inputs.as_of > query.decision_at
        or tuple(final_source_events) != ordered_sources
        or ordered_sources != exact_query_sources
        or evaluation.to_dict() != recorded_evaluation.to_dict()
        or evaluation.evaluation_sha256 != frontier.evaluation_sha256
        or final_elapsed < 0.0
        or newest_age is None
        or float(newest_age) + final_elapsed > policy.max_source_age_seconds
    ):
        raise SealedReplayInputError(
            "sealed first-dip final tape evaluation differs from captured facts"
        )

    detector_commit = receipt_commit(detector_receipt)
    detector_source_refs = tuple(
        manifest.event_index[value]
        for value in detector_receipt.source_event_sha256s
    )
    detector_policy_raw = checkpoint.decision_payload.get(
        "first_dip_tape_policy"
    )
    detector_evaluation_raw = checkpoint.decision_payload.get(
        "first_dip_tape_evaluation"
    )
    try:
        detector_policy = FirstDipTapePolicy.from_dict(detector_policy_raw)
        detector_evaluation = FirstDipTapeEvaluation.from_dict(
            detector_evaluation_raw
        )
    except (CaptureContractError, TypeError, ValueError) as exc:
        raise SealedReplayInputError(
            "sealed first-dip detector lineage policy is malformed"
        ) from exc
    expected_detector_inventory = sha256_json(
        {
            "read_id": detector_receipt.read_id,
            "source_event_sha256s": list(
                detector_receipt.source_event_sha256s
            ),
        }
    )
    if (
        prior_reference.run_id != identity.run_id
        or prior_reference.authority_source != "captured_db_paper"
        or prior_reference.generation != identity.generation
        or prior_reference.symbol != checkpoint.symbol
        or prior_reference.decision_id != checkpoint.decision_id
        or prior_reference.decision_at != checkpoint.decision_at
        or prior_reference.input_prefix_root_sha256
        != checkpoint.input_prefix_root_sha256
        or prior_reference.decision_checkpoint_sha256 is not None
        or prior_reference.active_input_attestation_sha256 is None
        or prior_reference.read_receipt_sha256
        != sha256_json(detector_receipt.to_dict())
        or prior_reference.receipt_event_sha256 != detector_commit.event_sha256
        or prior_reference.source_event_inventory_sha256
        != expected_detector_inventory
        or prior_reference.policy_sha256 != detector_policy.policy_sha256
        or prior_reference.evaluation_sha256
        != detector_evaluation.evaluation_sha256
        or prior_reference.opportunity_key_sha256
        != frontier.opportunity_key_sha256
        or sha256_json(prior_reference.to_dict())
        != frontier.prior_detector_reference_sha256
        or captured_read_result_sha256(detector_source_refs)
        != detector_receipt.result_sha256
    ):
        raise SealedReplayInputError(
            "sealed first-dip prior detector lineage differs from captured facts"
        )

    return _SealedFirstDipFinalFrontier(
        frontier=frontier,
        dependency_profile=profile,
        policy=policy,
        evaluation=evaluation,
        adaptive_request=adaptive_request,
        prior_detector_reference=prior_reference,
        tape_read=final_read,
        tape_read_evidence=final_read_evidence,
        tape_continuity_evidence=final_continuity_evidence,
    )


class SealedReplayV3InputAdapter:
    """Release a verified capture to ReplayV3 without provider fallback.

    This is an input adapter, not a second FSM. Construction admits only the
    exact verified capture/manifest/request triple and only when coverage is an
    unqualified ``complete`` grade. Facts are validated up front, then released
    by ``DeterministicDualClockLoader`` strictly on ``available_at``. Market and
    provider timestamps remain feature data and can never make a fact visible.

    The current synthetic ``ReplayV3Driver`` remains unchanged. Until broker
    lifecycle transitions and OS-level zero-egress are integrated and proven,
    their existing certification blockers remain honest.
    """

    def __init__(
        self,
        capture: VerifiedReplayCapture,
        manifest: CaptureCoverageManifest,
        request: ReplayCoverageRequest,
    ) -> None:
        if type(capture) is not VerifiedReplayCapture:
            raise SealedReplayInputError(
                "sealed ReplayV3 input requires an exact VerifiedReplayCapture"
            )
        if type(manifest) is not CaptureCoverageManifest:
            raise SealedReplayInputError(
                "sealed ReplayV3 input requires an exact CaptureCoverageManifest"
            )
        if type(request) is not ReplayCoverageRequest:
            raise SealedReplayInputError(
                "sealed ReplayV3 input requires an exact ReplayCoverageRequest"
            )
        if not request.symbol:
            raise SealedReplayInputError("sealed ReplayV3 request symbol is required")
        if request.expected_identity_sha256 != capture.identity.identity_sha256:
            raise SealedReplayInputError(
                "sealed ReplayV3 request must pin the exact capture identity"
            )
        if manifest.identity != capture.identity:
            raise SealedReplayInputError("sealed ReplayV3 capture/manifest identity mismatch")

        binding = manifest.seal_binding
        if binding is None:
            raise SealedReplayInputError("sealed ReplayV3 manifest binding is missing")
        exact_binding_pairs = (
            (binding.expected_final_seal_sha256, capture.expected_final_seal_sha256),
            (binding.final_seal_sha256, capture.final_seal_sha256),
            (binding.seal_content_root_sha256, capture.seal_content_root_sha256),
            (binding.close_proof_sha256, capture.close_proof_sha256),
            (binding.event_accumulator_sha256, capture.event_accumulator_sha256),
            (binding.gap_accumulator_sha256, capture.gap_accumulator_sha256),
        )
        if any(left != right for left, right in exact_binding_pairs):
            raise SealedReplayInputError(
                "sealed ReplayV3 capture/manifest seal binding mismatch"
            )

        capture_refs = {
            event.event_sha256: CaptureEventRef.from_event(event)
            for event in capture.events
        }
        if capture_refs != dict(manifest.event_index):
            raise SealedReplayInputError(
                "sealed ReplayV3 manifest does not index the exact capture inventory"
            )

        grade = grade_replay_coverage(request, manifest)
        if (
            not grade.replayable
            or grade.grade != "complete"
            or grade.reasons
            or grade.manifest_sha256 != manifest.manifest_sha256
        ):
            reasons = grade.reasons or ("coverage_grade_not_complete",)
            raise SealedReplayInputError(
                "sealed ReplayV3 coverage is not complete: " + ",".join(reasons)
            )

        checkpoints = [
            row
            for row in manifest.decision_checkpoints
            if row.checkpoint_sha256 == request.decision_checkpoint_sha256
        ]
        if len(checkpoints) != 1:
            raise SealedReplayInputError(
                "sealed ReplayV3 exact decision checkpoint is missing or ambiguous"
            )
        checkpoint = checkpoints[0]
        dependency_profile = checkpoint.decision_payload.get(
            "fsm_dependency_profile"
        )
        if not isinstance(dependency_profile, Mapping):
            raise SealedReplayInputError(
                "sealed ReplayV3 dependency profile is malformed"
            )
        try:
            profile_streams = frozenset(
                CaptureStream(str(value))
                for value in dependency_profile.get("required_streams", ())
            )
        except ValueError as exc:
            raise SealedReplayInputError(
                "sealed ReplayV3 dependency profile contains an unknown stream"
            ) from exc
        replay_streams = request.required_streams
        if not profile_streams or not profile_streams.issubset(replay_streams):
            raise SealedReplayInputError(
                "sealed ReplayV3 dependency profile escapes its replay request"
            )
        unsupported = replay_streams - _SEALED_REPLAY_SUPPORTED_STREAMS
        if unsupported:
            raise SealedReplayInputError(
                "sealed ReplayV3 dependency stream is not implemented: "
                + ",".join(sorted(stream.value for stream in unsupported))
            )

        # Decision time selects invocations in the replay window.  The later
        # checkpoint/event availability clock proves durable publication only;
        # it must never move or suppress an FSM invocation.
        checkpoints_in_window = tuple(
            row
            for row in manifest.decision_checkpoints
            if row.symbol == request.symbol
            and request.warmup_start_at <= row.decision_at <= request.exit_end_at
        )
        checkpoint_event_sha256s = {
            row.decision_event_sha256 for row in checkpoints_in_window
        }

        # Canonical order intents are outputs of the real FSM and authority for
        # replaying the broker lifecycle.  They are deliberately not fed back as
        # market inputs, but every canonical broker transition must bind one
        # exact intent from a decision available within this replay window.
        canonical_order_intents: dict[str, CaptureOrderIntent] = {}
        canonical_intent_decisions: dict[str, str] = {}
        canonical_decision_outputs: dict[str, CaptureDecisionOutput] = {}
        canonical_decision_authorities: dict[str, str] = {}
        decision_events: list[CaptureEvent] = []
        malformed_decision_sequences: list[int] = []
        for event in capture.events:
            if (
                event.stream is not CaptureStream.FSM_DECISION
                or event.symbol != request.symbol
                or event.event_sha256 not in checkpoint_event_sha256s
            ):
                continue
            decision_events.append(event)
            if "decision_output" not in event.payload:
                malformed_decision_sequences.append(event.sequence)
                continue
            try:
                output = capture_decision_output_from_payload(event.payload)
            except (CaptureContractError, TypeError, ValueError):
                malformed_decision_sequences.append(event.sequence)
                continue
            if output.decision_id in {
                row.decision_id for row in canonical_decision_outputs.values()
            }:
                raise SealedReplayInputError(
                    "sealed ReplayV3 FSM decision id is duplicated"
                )
            canonical_decision_outputs[event.event_sha256] = output
            canonical_decision_authorities[output.decision_id] = (
                capture_final_decision_authority_sha256(event, output)
            )
            for intent in output.order_intents:
                prior = canonical_order_intents.get(intent.order_intent_sha256)
                prior_cid = next(
                    (
                        row
                        for row in canonical_order_intents.values()
                        if row.client_order_id == intent.client_order_id
                    ),
                    None,
                )
                if prior is not None:
                    if (
                        prior != intent
                        or canonical_intent_decisions.get(
                            intent.order_intent_sha256
                        )
                        != output.decision_id
                    ):
                        raise SealedReplayInputError(
                            "sealed ReplayV3 canonical order intent is duplicated"
                        )
                    continue
                if prior_cid is not None:
                    raise SealedReplayInputError(
                        "sealed ReplayV3 canonical order intent is duplicated"
                    )
                canonical_order_intents[intent.order_intent_sha256] = intent
                canonical_intent_decisions[intent.order_intent_sha256] = (
                    output.decision_id
                )

        if malformed_decision_sequences:
            raise SealedReplayInputError(
                "sealed ReplayV3 FSM decision output is missing or malformed"
            )

        checkpoints_by_event: dict[str, list[CaptureDecisionCheckpoint]] = {}
        for row in checkpoints_in_window:
            checkpoints_by_event.setdefault(row.decision_event_sha256, []).append(row)
        decision_ticks: list[_SealedDecisionTick] = []
        scheduled_event_hashes: set[str] = set()
        for event in decision_events:
            output = canonical_decision_outputs[event.event_sha256]
            matches = checkpoints_by_event.get(event.event_sha256, [])
            if len(matches) != 1:
                raise SealedReplayInputError(
                    "sealed ReplayV3 FSM decision checkpoint is missing or ambiguous"
                )
            row = matches[0]
            if (
                row.decision_id != output.decision_id
                or row.symbol != output.symbol
                or row.available_at != event.clocks.available_at
                or event.clocks.market_reference_at != row.decision_at
                or row.decision_payload != event.payload
                or row.input_prefix_sequence >= event.sequence
            ):
                raise SealedReplayInputError(
                    "sealed ReplayV3 FSM decision/checkpoint binding is invalid"
                )
            profile = row.decision_payload.get("fsm_dependency_profile")
            if not isinstance(profile, Mapping):
                raise SealedReplayInputError(
                    "sealed ReplayV3 FSM decision dependency profile is malformed"
                )
            try:
                row_streams = frozenset(
                    CaptureStream(str(value))
                    for value in profile.get("required_streams", ())
                )
            except ValueError as exc:
                raise SealedReplayInputError(
                    "sealed ReplayV3 FSM decision dependency stream is unknown"
                ) from exc
            if not row_streams or not row_streams.issubset(request.required_streams):
                raise SealedReplayInputError(
                    "sealed ReplayV3 FSM decision escaped requested stream coverage"
                )
            profile_read_ids = frozenset(
                str(value) for value in profile.get("required_read_ids", ())
            )
            if profile_read_ids != frozenset(row.required_read_ids):
                raise SealedReplayInputError(
                    "sealed ReplayV3 FSM decision read set is inconsistent"
                )
            row_grade = grade_replay_coverage(
                ReplayCoverageRequest(
                    warmup_start_at=request.warmup_start_at,
                    decision_at=row.decision_at,
                    exit_end_at=request.exit_end_at,
                    required_streams=row_streams,
                    decision_id=row.decision_id,
                    decision_checkpoint_sha256=row.checkpoint_sha256,
                    required_read_ids=frozenset(row.required_read_ids),
                    symbol=row.symbol,
                    expected_identity_sha256=(
                        request.expected_identity_sha256
                    ),
                ),
                manifest,
            )
            if (
                not row_grade.replayable
                or row_grade.grade != "complete"
                or row_grade.reasons
                or row_grade.manifest_sha256 != manifest.manifest_sha256
            ):
                reasons = row_grade.reasons or (
                    "secondary_checkpoint_coverage_not_complete",
                )
                raise SealedReplayInputError(
                    "sealed ReplayV3 FSM checkpoint coverage is not complete: "
                    + ",".join(reasons)
                )
            scheduled_event_hashes.add(event.event_sha256)
            prefix_refs = tuple(
                ref
                for ref in manifest.event_index.values()
                if ref.sequence <= row.input_prefix_sequence
            )
            if not prefix_refs or max(ref.sequence for ref in prefix_refs) != (
                row.input_prefix_sequence
            ):
                raise SealedReplayInputError(
                    "sealed ReplayV3 FSM input prefix is incomplete"
                )
            final_first_dip = _sealed_first_dip_final_frontier_inventory(
                checkpoint=row,
                decision_event=event,
                capture=capture,
                manifest=manifest,
            )
            if (
                output.setup_role == "first_dip_reclaim"
                and output.action is CaptureDecisionAction.ORDER_INTENT
                and final_first_dip is None
            ):
                raise SealedReplayInputError(
                    "sealed first-dip order decision lacks a verified final frontier"
                )
            if final_first_dip is not None and output.setup_role != "first_dip_reclaim":
                raise SealedReplayInputError(
                    "sealed first-dip final frontier is attached to another setup"
                )
            decision_ticks.append(
                _SealedDecisionTick(
                    checkpoint=row,
                    event=event,
                    output=output,
                    # Replaced below after every required read receipt has been
                    # bound to its exact selected source facts.  Using every
                    # non-control event in the durable prefix would incorrectly
                    # treat an unselected post-receipt callback as a decision
                    # input merely because it was persisted before publication.
                    input_prefix_available_at=row.decision_at,
                    required_streams=row_streams,
                    first_dip_final_frontier=final_first_dip,
                )
            )
        if set(checkpoints_by_event) != scheduled_event_hashes:
            raise SealedReplayInputError(
                "sealed ReplayV3 manifest contains an unbound FSM checkpoint"
            )
        if not any(
            row.checkpoint.checkpoint_sha256 == checkpoint.checkpoint_sha256
            for row in decision_ticks
        ):
            raise SealedReplayInputError(
                "sealed ReplayV3 requested FSM checkpoint is not scheduled"
            )
        decision_ticks.sort(
            key=lambda row: (
                row.checkpoint.decision_at,
                row.checkpoint.input_prefix_sequence,
                row.checkpoint.decision_id,
            )
        )
        if len({row.frontier for row in decision_ticks}) != len(decision_ticks):
            raise SealedReplayInputError(
                "sealed ReplayV3 schedules multiple FSM decisions at one frontier"
            )
        for earlier, later in zip(decision_ticks, decision_ticks[1:]):
            if (
                later.checkpoint.input_prefix_sequence
                <= earlier.checkpoint.input_prefix_sequence
                or later.checkpoint.decision_at < earlier.checkpoint.decision_at
            ):
                raise SealedReplayInputError(
                    "sealed ReplayV3 FSM decision schedule regresses"
                )

        receipts_by_id = {row.read_id: row for row in manifest.read_receipts}
        receipt_source_hashes: set[str] = set()
        query_source_hashes: set[str] = set()
        scoped_decision_ticks: list[_SealedDecisionTick] = []
        for decision_tick in decision_ticks:
            checkpoint_row = decision_tick.checkpoint
            first_dip_tape_read_id = str(
                checkpoint_row.decision_payload.get("first_dip_tape_read_id")
                or ""
            ).strip()
            first_dip_tape_seen = False
            if first_dip_tape_read_id and (
                CaptureStream.IQFEED_PRINT not in decision_tick.required_streams
                or first_dip_tape_read_id not in checkpoint_row.required_read_ids
            ):
                raise SealedReplayInputError(
                    "sealed ReplayV3 first-dip read id is outside its dependency profile"
                )
            iqfeed_typed_read_seen = False
            query_read_ids: set[str] = set()
            sources_by_stream: dict[CaptureStream, set[str]] = {}
            selected_source_refs: list[CaptureEventRef] = []
            decision_read_plan: list[_SealedDecisionRead] = []
            query_read_plan: list[_SealedDecisionRead] = []
            for read_id in checkpoint_row.required_read_ids:
                receipt = receipts_by_id.get(read_id)
                if receipt is None:
                    raise SealedReplayInputError(
                        f"sealed ReplayV3 required read receipt is missing: {read_id}"
                    )
                if (
                    receipt.decision_id != checkpoint_row.decision_id
                    or receipt.stream not in decision_tick.required_streams
                    or receipt.symbol
                    != (
                        None
                        if receipt.stream is CaptureStream.ACCOUNT_RISK_SNAPSHOT
                        else checkpoint_row.symbol
                    )
                ):
                    raise SealedReplayInputError(
                        "sealed ReplayV3 receipt escaped its exact decision profile: "
                        + read_id
                    )
                is_first_dip_read = False
                is_microstructure_read = False
                if receipt.stream is CaptureStream.IQFEED_PRINT:
                    if len(receipt.source_event_sha256s) != len(
                        set(receipt.source_event_sha256s)
                    ):
                        raise SealedReplayInputError(
                            "sealed ReplayV3 IQFeed receipt contains duplicate sources"
                        )
                    if read_id == first_dip_tape_read_id:
                        if first_dip_tape_seen:
                            raise SealedReplayInputError(
                                "sealed ReplayV3 first-dip tape receipt is ambiguous"
                            )
                        first_dip_tape_seen = True
                        is_first_dip_read = True
                        _sealed_first_dip_receipt_inventory(
                            receipt=receipt,
                            checkpoint=checkpoint_row,
                            manifest=manifest,
                        )
                    else:
                        _sealed_microstructure_receipt_inventory(
                            receipt=receipt,
                            checkpoint=checkpoint_row,
                            manifest=manifest,
                        )
                        is_microstructure_read = True
                        query_read_ids.add(read_id)
                    iqfeed_typed_read_seen = True
                elif receipt.stream in _SEALED_REPLAY_QUERY_STREAMS:
                    query_read_ids.add(read_id)
                empty_typed_iqfeed_receipt = (
                    receipt.stream is CaptureStream.IQFEED_PRINT
                    and (is_first_dip_read or is_microstructure_read)
                    and receipt.empty_result
                    and not receipt.source_event_sha256s
                )
                if (
                    (receipt.empty_result or not receipt.source_event_sha256s)
                    and not empty_typed_iqfeed_receipt
                ):
                    raise SealedReplayInputError(
                        "sealed ReplayV3 required read returned no replayable facts: "
                        + read_id
                    )
                if (
                    len(receipt.source_event_sha256s) != 1
                    and receipt.stream is not CaptureStream.IQFEED_PRINT
                ):
                    raise SealedReplayInputError(
                        "sealed ReplayV3 read seam cannot reproduce an ambiguous "
                        "multi-event result: "
                        + read_id
                    )
                source_refs: list[CaptureEventRef] = []
                for event_sha256 in receipt.source_event_sha256s:
                    source_ref = manifest.event_index.get(event_sha256)
                    if (
                        source_ref is None
                        or source_ref.stream is not receipt.stream
                        or source_ref.symbol != receipt.symbol
                        or source_ref.sequence
                        > checkpoint_row.input_prefix_sequence
                        or source_ref.available_at > receipt.returned_at
                        or source_ref.available_at > checkpoint_row.decision_at
                    ):
                        raise SealedReplayInputError(
                            "sealed ReplayV3 query receipt source escaped its "
                            f"decision prefix: {read_id}"
                        )
                    if (
                        source_ref.identity_sha256
                        != capture.identity.identity_sha256
                        or source_ref.provider != receipt.provider
                        or (
                            STREAM_POLICIES[receipt.stream].query_parameters_required
                            and source_ref.query_sha256 != receipt.query_sha256
                        )
                    ):
                        raise SealedReplayInputError(
                            "sealed ReplayV3 query receipt/source provenance "
                            f"mismatch: {read_id}"
                        )
                    source_refs.append(source_ref)
                    selected_source_refs.append(source_ref)
                    receipt_source_hashes.add(event_sha256)
                    if read_id in query_read_ids:
                        query_source_hashes.add(event_sha256)
                    sources_by_stream.setdefault(receipt.stream, set()).add(
                        event_sha256
                    )
                if receipt.stream is CaptureStream.IQFEED_PRINT:
                    if any(ref.provider_event_at is None for ref in source_refs):
                        raise SealedReplayInputError(
                            "sealed ReplayV3 first-dip print lacks an exact provider clock"
                        )
                    ordered_refs = tuple(
                        sorted(
                            source_refs,
                            key=lambda ref: (
                                ref.provider_event_at,
                                ref.sequence,
                            ),
                        )
                    )
                    if tuple(source_refs) != ordered_refs:
                        raise SealedReplayInputError(
                            "sealed ReplayV3 first-dip prints are not in exact event order"
                        )
                if captured_read_result_sha256(source_refs) != receipt.result_sha256:
                    raise SealedReplayInputError(
                        "sealed ReplayV3 query result digest mismatch: " + read_id
                    )
                decision_read = _SealedDecisionRead(
                    receipt=receipt,
                    source_event_sha256s=tuple(receipt.source_event_sha256s),
                )
                decision_read_plan.append(decision_read)
                if read_id in query_read_ids:
                    query_read_plan.append(decision_read)
            if first_dip_tape_read_id and not first_dip_tape_seen:
                raise SealedReplayInputError(
                    "sealed ReplayV3 first-dip typed receipt is missing"
                )
            predecision_streams = decision_tick.required_streams - {
                CaptureStream.BROKER_ORDER_LIFECYCLE
            }
            for stream in predecision_streams:
                if not sources_by_stream.get(stream) and not (
                    stream is CaptureStream.IQFEED_PRINT
                    and iqfeed_typed_read_seen
                    and all(
                        receipts_by_id[read_id].empty_result
                        for read_id in checkpoint_row.required_read_ids
                        if receipts_by_id[read_id].stream
                        is CaptureStream.IQFEED_PRINT
                    )
                ):
                    raise SealedReplayInputError(
                        "sealed ReplayV3 stream has no exact decision receipt "
                        f"source: {stream.value}"
                    )
            for stream in {
                CaptureStream.NBBO_QUOTE,
                CaptureStream.ADMISSION_ELIGIBILITY,
            } & predecision_streams:
                matching_reads = [
                    row
                    for row in decision_read_plan
                    if row.receipt.stream is stream
                ]
                if len(matching_reads) != 1:
                    raise SealedReplayInputError(
                        "sealed ReplayV3 scalar decision read is ambiguous: "
                        + stream.value
                    )
            read_order_keys = [
                row.receipt.requested_at for row in decision_read_plan
            ]
            if len(read_order_keys) != len(set(read_order_keys)):
                raise SealedReplayInputError(
                    "sealed ReplayV3 decision read order is ambiguous at one decision"
                )
            ordered_decision_reads = tuple(
                sorted(
                    decision_read_plan,
                    key=lambda row: (
                        row.receipt.requested_at,
                        row.receipt.returned_at,
                        row.receipt.read_id,
                    ),
                )
            )
            if not selected_source_refs:
                raise SealedReplayInputError(
                    "sealed ReplayV3 FSM decision has no receipt-selected causal facts"
                )
            input_prefix_available_at = max(
                ref.available_at for ref in selected_source_refs
            )
            if input_prefix_available_at > checkpoint_row.decision_at:
                raise SealedReplayInputError(
                    "sealed ReplayV3 FSM receipt selected a future causal fact"
                )
            scoped_decision_ticks.append(
                replace(
                    decision_tick,
                    input_prefix_available_at=input_prefix_available_at,
                    receipt_source_event_sha256s=tuple(
                        (stream, tuple(sorted(values)))
                        for stream, values in sorted(
                            sources_by_stream.items(),
                            key=lambda item: item[0].value,
                        )
                    ),
                    decision_read_plan=ordered_decision_reads,
                    query_read_plan=tuple(
                        row
                        for row in ordered_decision_reads
                        if row.receipt.read_id in query_read_ids
                    ),
                )
            )
        decision_ticks = scoped_decision_ticks

        typed_by_hash: dict[
            str,
            _SealedNbboInput
            | _SealedIqfeedPrintInput
            | _SealedOhlcvInput
            | _SealedEligibilityInput
            | _SealedAccountRiskInput
            | _SealedScannerSnapshotInput
            | _SealedBrokerLifecycleInput,
        ] = {}
        replay_events: list[CaptureEvent] = []
        logical_fact_keys: set[tuple[Any, ...]] = set()
        present_streams: set[CaptureStream] = set()
        symbol = request.symbol
        for event in capture.events:
            if event.stream not in replay_streams:
                continue
            if event.clocks.available_at > request.exit_end_at:
                continue
            if event.stream in _SEALED_REPLAY_QUERY_STREAMS and (
                event.event_sha256 not in query_source_hashes
            ):
                continue
            if event.stream is CaptureStream.ACCOUNT_RISK_SNAPSHOT:
                if event.symbol is not None:
                    raise SealedReplayInputError(
                        "sealed account-risk fact must be account-scoped"
                    )
            elif event.symbol != symbol:
                continue

            parsed, logical_key = self._parse_event(
                event,
                capture,
                canonical_order_intents=canonical_order_intents,
                canonical_intent_decisions=canonical_intent_decisions,
            )
            if logical_key in logical_fact_keys:
                raise SealedReplayInputError(
                    f"sealed ReplayV3 duplicate or ambiguous fact: {event.stream.value}"
                )
            logical_fact_keys.add(logical_key)
            typed_by_hash[event.event_sha256] = parsed
            replay_events.append(event)
            present_streams.add(event.stream)

        decision_ticks_by_id = {
            row.checkpoint.decision_id: row for row in decision_ticks
        }
        for parsed in typed_by_hash.values():
            if (
                not isinstance(parsed, _SealedBrokerLifecycleInput)
                or parsed.canonical is None
            ):
                continue
            originating_tick = decision_ticks_by_id.get(
                parsed.canonical.decision_id
            )
            if originating_tick is None:
                raise SealedReplayInputError(
                    "sealed broker lifecycle has no originating FSM checkpoint"
                )
            expected_authority = canonical_decision_authorities.get(
                parsed.canonical.decision_id
            )
            if (
                expected_authority is None
                or parsed.canonical.final_decision_attestation_sha256
                != expected_authority
            ):
                raise SealedReplayInputError(
                    "sealed broker lifecycle decision authority is invalid"
                )
            if (
                parsed.event.sequence
                <= originating_tick.checkpoint.input_prefix_sequence
                or parsed.event.clocks.available_at
                < originating_tick.checkpoint.decision_at
            ):
                raise SealedReplayInputError(
                    "sealed broker lifecycle precedes its originating decision"
                )

        missing_streams = replay_streams - present_streams
        if missing_streams:
            raise SealedReplayInputError(
                "sealed ReplayV3 dependency facts are missing: "
                + ",".join(sorted(stream.value for stream in missing_streams))
            )
        missing_receipt_sources = receipt_source_hashes - set(typed_by_hash)
        if missing_receipt_sources:
            raise SealedReplayInputError(
                "sealed ReplayV3 receipt source is outside the exact replay scope"
            )
        replay_events_by_hash = {
            event.event_sha256: event for event in replay_events
        }
        for decision_tick in decision_ticks:
            decision_checkpoint = decision_tick.checkpoint
            predecision_streams = decision_tick.required_streams - {
                CaptureStream.BROKER_ORDER_LIFECYCLE
            }
            missing_at_decision: set[CaptureStream] = set()
            for stream in predecision_streams:
                source_hashes = decision_tick.receipt_sources_for(stream)
                facts = [
                    replay_events_by_hash.get(event_sha256)
                    for event_sha256 in source_hashes
                ]
                first_dip_read_id = str(
                    decision_checkpoint.decision_payload.get(
                        "first_dip_tape_read_id"
                    )
                    or ""
                ).strip()
                empty_first_dip_available = (
                    stream is CaptureStream.IQFEED_PRINT
                    and bool(first_dip_read_id)
                    and receipts_by_id[first_dip_read_id].empty_result
                    and not source_hashes
                )
                available = empty_first_dip_available or (
                    bool(facts)
                    and all(
                        event is not None
                        and event.stream is stream
                        and event.clocks.available_at
                        <= decision_checkpoint.decision_at
                        and event.sequence
                        <= decision_checkpoint.input_prefix_sequence
                        for event in facts
                    )
                )
                if not available:
                    missing_at_decision.add(stream)
            if missing_at_decision:
                raise SealedReplayInputError(
                    "sealed ReplayV3 dependency was unavailable at decision "
                    f"{decision_checkpoint.decision_id}:"
                    + ",".join(
                        sorted(stream.value for stream in missing_at_decision)
                    )
                )

        loader = DeterministicDualClockLoader(replay_events)
        release_order = tuple(loader.iter_release_order())
        release_order_root = sha256_json(
            {
                "identity_sha256": capture.identity.identity_sha256,
                "decision_checkpoint_sha256": checkpoint.checkpoint_sha256,
                "release_order": [
                    {
                        "available_at": event.clocks.available_at,
                        "sequence": event.sequence,
                        "event_sha256": event.event_sha256,
                        "payload_sha256": event.payload_sha256,
                        "query_sha256": event.query_sha256,
                    }
                    for event in release_order
                ],
            }
        )

        self._request = request
        self._manifest = manifest
        self._coverage_grade = grade
        self._loader = loader
        self._release_cursor = 0
        self._pending_release: list[CaptureEvent] = []
        self._max_released_sequence = 0
        self._advanced_frontier: tuple[datetime, int | None] | None = None
        self._typed_by_hash = typed_by_hash
        self._release_order = release_order
        self._checkpoint = checkpoint
        self._decision_ticks = tuple(decision_ticks)
        self._decision_ticks_by_frontier = {
            row.frontier: row for row in self._decision_ticks
        }
        self._canonical_order_intents = canonical_order_intents
        self._malformed_decision_sequences: tuple[int, ...] = ()
        self._current_nbbo: dict[str, _SealedNbboInput] = {}
        self._current_ohlcv: dict[tuple[str, str, str], _SealedOhlcvInput] = {}
        self._current_eligibility: dict[str, _SealedEligibilityInput] = {}
        self._current_account_risk: _SealedAccountRiskInput | None = None
        self._current_scanner_snapshot: dict[
            str, _SealedScannerSnapshotInput
        ] = {}
        self._released_broker_lifecycle: list[_SealedBrokerLifecycleInput] = []
        self._released_counterfactual_exact_prints: list[VerifiedExactPrint] = []
        self._released_event_sha256s: set[str] = set()
        self._active_decision_tick: _SealedDecisionTick | None = None
        self._active_query_reads: tuple[_SealedDecisionRead, ...] = ()
        self._active_query_read_cursor = 0
        self._active_first_dip_tape_consumed = False
        self._active_first_dip_tape_authority: (
            _VerifiedFirstDipTapeDecisionAuthority | None
        ) = None
        self._active_first_dip_final_tape_consumed = False
        self._active_first_dip_final_authority: (
            _VerifiedFirstDipTapeDecisionAuthority | None
        ) = None
        self._active_sealed_adaptive_material: (
            AdaptiveRiskRuntimeCaptureMaterial | None
        ) = None
        self._terminal_drain_complete = False
        last_decision = self._decision_ticks[-1]
        self._broker_events_unobserved_by_fsm = frozenset(
            parsed.event.event_sha256
            for parsed in typed_by_hash.values()
            if isinstance(parsed, _SealedBrokerLifecycleInput)
            and (
                parsed.event.clocks.available_at
                > last_decision.checkpoint.decision_at
                or parsed.event.sequence
                > last_decision.checkpoint.input_prefix_sequence
            )
        )
        self._rejected_provider_requests = 0
        self._network_attempt_count = 0
        self._advanced_to: datetime | None = None
        self._proof = SealedReplayInputProof(
            identity_sha256=capture.identity.identity_sha256,
            final_capture_seal_sha256=capture.final_seal_sha256,
            manifest_sha256=manifest.manifest_sha256,
            release_order_root_sha256=release_order_root,
            decision_checkpoint_sha256=checkpoint.checkpoint_sha256,
            decision_id=checkpoint.decision_id,
            decision_at=checkpoint.decision_at,
            checkpoint_available_at=checkpoint.available_at,
            input_prefix_sequence=checkpoint.input_prefix_sequence,
            input_prefix_root_sha256=checkpoint.input_prefix_root_sha256,
        )
        self._counterfactual_exact_print_inventory = (
            _mint_verified_exact_print_inventory(
                capture_identity_sha256=self._proof.identity_sha256,
                final_capture_seal_sha256=self._proof.final_capture_seal_sha256,
                release_order_root_sha256=self._proof.release_order_root_sha256,
                event_sha256s=tuple(
                    event.event_sha256
                    for event in self._release_order
                    if isinstance(
                        self._typed_by_hash[event.event_sha256],
                        _SealedIqfeedPrintInput,
                    )
                ),
            )
        )

    @staticmethod
    def _parse_event(
        event: CaptureEvent,
        capture: VerifiedReplayCapture,
        *,
        canonical_order_intents: Mapping[str, CaptureOrderIntent],
        canonical_intent_decisions: Mapping[str, str],
    ) -> tuple[
        _SealedNbboInput
        | _SealedIqfeedPrintInput
        | _SealedOhlcvInput
        | _SealedEligibilityInput
        | _SealedAccountRiskInput
        | _SealedScannerSnapshotInput
        | _SealedBrokerLifecycleInput,
        tuple[Any, ...],
    ]:
        try:
            payload = resolve_capture_source_payload(event).payload
        except CaptureContractError as exc:
            raise SealedReplayInputError(str(exc)) from exc
        stream = event.stream
        if stream is CaptureStream.IQFEED_PRINT:
            try:
                normalized_print = CaptureIqfeedPrint.from_event(event)
            except CaptureContractError as exc:
                raise SealedReplayInputError(str(exc)) from exc
            event_at = event.clocks.provider_event_at
            assert event_at is not None
            return (
                _SealedIqfeedPrintInput(
                    event=event,
                    price=normalized_print.price,
                    size=normalized_print.size,
                    bid=normalized_print.bid,
                    ask=normalized_print.ask,
                ),
                (stream.value, event.symbol, event_at, event.sequence),
            )

        if stream is CaptureStream.NBBO_QUOTE:
            _sealed_payload_fields(
                payload,
                required=frozenset({"schema_version", "symbol", "bid", "ask"}),
                optional=frozenset({"last", IQFEED_L1_SOURCE_PROVENANCE_FIELD}),
                description="NBBO",
            )
            if payload.get("schema_version") != SEALED_REPLAY_NBBO_SCHEMA_VERSION:
                raise SealedReplayInputError("sealed NBBO schema version is unsupported")
            if str(payload.get("symbol") or "").strip().upper() != event.symbol:
                raise SealedReplayInputError("sealed NBBO payload/event symbol mismatch")
            source_provenance = payload.get(IQFEED_L1_SOURCE_PROVENANCE_FIELD)
            if source_provenance is not None:
                try:
                    validate_iqfeed_l1_source_provenance(
                        source_provenance,
                        symbol=str(event.symbol or ""),
                        clocks=event.clocks,
                    )
                except CaptureContractError as exc:
                    raise SealedReplayInputError(str(exc)) from exc
            bid = _sealed_finite_number(payload.get("bid"), "NBBO bid", positive=True)
            ask = _sealed_finite_number(payload.get("ask"), "NBBO ask", positive=True)
            if ask < bid:
                raise SealedReplayInputError("sealed NBBO ask cannot be below bid")
            last_raw = payload.get("last")
            last = (
                None
                if last_raw is None
                else _sealed_finite_number(last_raw, "NBBO last", positive=True)
            )
            event_at = event.clocks.provider_event_at
            if event_at is None:  # defensive; CaptureEvent already enforces this
                raise SealedReplayInputError("sealed NBBO exact event clock is missing")
            return (
                _SealedNbboInput(
                    event=event,
                    quote=RecordedQuote(bid=bid, ask=ask, last=last),
                ),
                (stream.value, event.symbol, event_at),
            )

        if stream is CaptureStream.PROVIDER_OHLCV:
            query = event.query
            if not isinstance(query, Mapping) or set(query) != {
                "schema_version",
                "call",
                "provider_parameters",
            }:
                raise SealedReplayInputError(
                    "sealed OHLCV query fields do not match the ReplayV3 provider seam"
                )
            if query.get("schema_version") != SEALED_REPLAY_OHLCV_QUERY_SCHEMA_VERSION:
                raise SealedReplayInputError(
                    "sealed OHLCV query schema version is unsupported"
                )
            call = query.get("call")
            provider_parameters = query.get("provider_parameters")
            if not isinstance(call, Mapping) or set(call) != {
                "symbol",
                "interval",
                "period",
            }:
                raise SealedReplayInputError(
                    "sealed OHLCV live-runner call is malformed"
                )
            if not isinstance(provider_parameters, Mapping):
                raise SealedReplayInputError(
                    "sealed OHLCV provider parameters are malformed"
                )
            query_symbol = str(call.get("symbol") or "").strip().upper()
            interval = str(call.get("interval") or "").strip()
            period = str(call.get("period") or "").strip()
            if not query_symbol or not interval or not period:
                raise SealedReplayInputError("sealed OHLCV query is incomplete")
            if query_symbol != event.symbol:
                raise SealedReplayInputError("sealed OHLCV query/event symbol mismatch")
            _sealed_payload_fields(
                payload,
                required=frozenset({"schema_version", "query_sha256", "rows"}),
                description="OHLCV",
            )
            if payload.get("schema_version") != SEALED_REPLAY_OHLCV_SCHEMA_VERSION:
                raise SealedReplayInputError("sealed OHLCV schema version is unsupported")
            if str(payload.get("query_sha256") or "").strip().lower() != event.query_sha256:
                raise SealedReplayInputError("sealed OHLCV payload/query mismatch")
            raw_rows = payload.get("rows")
            if not isinstance(raw_rows, (list, tuple)) or not raw_rows:
                raise SealedReplayInputError("sealed OHLCV result rows are missing")
            rows: list[tuple[datetime, float, float, float, float, float]] = []
            last_bar_at: datetime | None = None
            for raw_row in raw_rows:
                if not isinstance(raw_row, Mapping):
                    raise SealedReplayInputError("sealed OHLCV row is malformed")
                _sealed_payload_fields(
                    raw_row,
                    required=frozenset(
                        {
                            "market_reference_at",
                            "open",
                            "high",
                            "low",
                            "close",
                            "volume",
                        }
                    ),
                    description="OHLCV row",
                )
                bar_at = _sealed_payload_utc(
                    raw_row.get("market_reference_at"),
                    "OHLCV market_reference_at",
                )
                if last_bar_at is not None and bar_at <= last_bar_at:
                    raise SealedReplayInputError(
                        "sealed OHLCV rows must be strictly ordered and unique"
                    )
                open_px = _sealed_finite_number(
                    raw_row.get("open"), "OHLCV open", positive=True
                )
                high_px = _sealed_finite_number(
                    raw_row.get("high"), "OHLCV high", positive=True
                )
                low_px = _sealed_finite_number(
                    raw_row.get("low"), "OHLCV low", positive=True
                )
                close_px = _sealed_finite_number(
                    raw_row.get("close"), "OHLCV close", positive=True
                )
                volume = _sealed_finite_number(
                    raw_row.get("volume"), "OHLCV volume", nonnegative=True
                )
                if high_px < max(open_px, close_px, low_px) or low_px > min(
                    open_px, close_px, high_px
                ):
                    raise SealedReplayInputError("sealed OHLCV price bounds are invalid")
                rows.append((bar_at, open_px, high_px, low_px, close_px, volume))
                last_bar_at = bar_at
            reference_at = event.clocks.market_reference_at
            if reference_at is None or (
                last_bar_at is not None and last_bar_at > reference_at
            ):
                raise SealedReplayInputError(
                    "sealed OHLCV rows exceed the captured market reference clock"
                )
            call_key = (query_symbol, interval, period)
            return (
                _SealedOhlcvInput(event=event, call_key=call_key, rows=tuple(rows)),
                (stream.value, event.query_sha256, event.clocks.available_at),
            )

        if stream is CaptureStream.ADMISSION_ELIGIBILITY:
            _sealed_payload_fields(
                payload,
                required=frozenset(
                    {"schema_version", "symbol", "live_eligible", "freshness_at"}
                ),
                description="admission/eligibility",
            )
            if (
                payload.get("schema_version")
                != SEALED_REPLAY_ELIGIBILITY_SCHEMA_VERSION
            ):
                raise SealedReplayInputError(
                    "sealed admission/eligibility schema version is unsupported"
                )
            if str(payload.get("symbol") or "").strip().upper() != event.symbol:
                raise SealedReplayInputError(
                    "sealed admission/eligibility payload/event symbol mismatch"
                )
            eligible = payload.get("live_eligible")
            if type(eligible) is not bool:
                raise SealedReplayInputError(
                    "sealed admission/eligibility verdict must be boolean"
                )
            freshness_at = _sealed_payload_utc(
                payload.get("freshness_at"), "eligibility freshness_at"
            )
            if (
                event.clocks.market_reference_at != freshness_at
                or freshness_at > event.clocks.available_at
            ):
                raise SealedReplayInputError(
                    "sealed admission/eligibility clock binding mismatch"
                )
            return (
                _SealedEligibilityInput(
                    event=event,
                    eligible=eligible,
                    freshness_at=freshness_at,
                ),
                (stream.value, event.symbol, freshness_at),
            )

        if stream is CaptureStream.ACCOUNT_RISK_SNAPSHOT:
            _sealed_payload_fields(
                payload,
                required=frozenset(
                    {
                        "schema_version",
                        "account_identity_sha256",
                        "equity_usd",
                        "buying_power_usd",
                        "cash_usd",
                    }
                ),
                optional=frozenset(
                    {"daily_risk_budget_usd", "portfolio_heat_usd"}
                ),
                description="account-risk",
            )
            if (
                payload.get("schema_version")
                != SEALED_REPLAY_ACCOUNT_RISK_SCHEMA_VERSION
            ):
                raise SealedReplayInputError(
                    "sealed account-risk schema version is unsupported"
                )
            account_hash = str(
                payload.get("account_identity_sha256") or ""
            ).strip().lower()
            if account_hash != capture.identity.account_identity_sha256:
                raise SealedReplayInputError(
                    "sealed account-risk identity does not match capture identity"
                )
            _sealed_finite_number(
                payload.get("equity_usd"), "account equity", positive=True
            )
            _sealed_finite_number(
                payload.get("buying_power_usd"),
                "account buying power",
                nonnegative=True,
            )
            _sealed_finite_number(payload.get("cash_usd"), "account cash")
            for optional_name in ("daily_risk_budget_usd", "portfolio_heat_usd"):
                if optional_name in payload:
                    _sealed_finite_number(
                        payload.get(optional_name),
                        f"account {optional_name}",
                        nonnegative=True,
                    )
            query = event.query
            if not isinstance(query, Mapping) or set(query) != {
                "schema_version",
                "account_identity_sha256",
                "fields",
            }:
                raise SealedReplayInputError(
                    "sealed account-risk query fields do not match schema"
                )
            if (
                query.get("schema_version")
                != SEALED_REPLAY_ACCOUNT_RISK_QUERY_SCHEMA_VERSION
            ):
                raise SealedReplayInputError(
                    "sealed account-risk query schema version is unsupported"
                )
            if (
                str(query.get("account_identity_sha256") or "").strip().lower()
                != account_hash
            ):
                raise SealedReplayInputError(
                    "sealed account-risk payload/query identity mismatch"
                )
            fields = query.get("fields")
            expected_fields = sorted(key for key in payload if key.endswith("_usd"))
            if not isinstance(fields, (list, tuple)) or list(fields) != expected_fields:
                raise SealedReplayInputError(
                    "sealed account-risk payload/query field mismatch"
                )
            return (
                _SealedAccountRiskInput(event=event),
                (stream.value, event.query_sha256, event.clocks.available_at),
            )

        if stream is CaptureStream.SCANNER_SNAPSHOT:
            try:
                snapshot = CaptureScannerSnapshot.from_event(event)
            except (CaptureContractError, TypeError, ValueError) as exc:
                raise SealedReplayInputError(
                    "sealed scanner snapshot is malformed"
                ) from exc
            source = snapshot.source_projection
            # ``CaptureScannerSnapshot`` recursively freezes its canonical
            # projection.  The legacy universe helper accepts a plain dict, so
            # expose a detached copy while retaining the typed object for exact
            # query/profile/provenance checks at consumption time.
            snapshot_row = {
                "ticker": str(source["ticker"]),
                "todaysChangePerc": source["todaysChangePerc"],
                "lastTrade": dict(source["lastTrade"]),
                "day": dict(source["day"]),
                "min": dict(source["min"]),
            }
            return (
                _SealedScannerSnapshotInput(
                    event=event,
                    snapshot=snapshot,
                    snapshot_row=snapshot_row,
                ),
                (stream.value, event.query_sha256, event.clocks.available_at),
            )

        if stream is CaptureStream.BROKER_ORDER_LIFECYCLE:
            if payload.get("schema_version") == "chili.broker-order-lifecycle.v1":
                try:
                    lifecycle = CaptureBrokerOrderLifecycle.from_dict(payload)
                except (CaptureContractError, TypeError, ValueError) as exc:
                    raise SealedReplayInputError(
                        "sealed canonical broker lifecycle is malformed"
                    ) from exc
                intent = canonical_order_intents.get(
                    lifecycle.order_intent_sha256
                )
                if (
                    intent is None
                    or canonical_intent_decisions.get(
                        lifecycle.order_intent_sha256
                    )
                    != lifecycle.decision_id
                    or intent.client_order_id != lifecycle.client_order_id
                    or intent.quantity != lifecycle.order_quantity
                    or intent.symbol != event.symbol
                ):
                    raise SealedReplayInputError(
                        "sealed canonical broker lifecycle/order intent mismatch"
                    )
                return (
                    _SealedBrokerLifecycleInput(
                        event=event,
                        broker_order_id=str(lifecycle.broker_order_id or ""),
                        event_type=lifecycle.transition.value,
                        canonical=lifecycle,
                        intent=intent,
                    ),
                    (
                        stream.value,
                        lifecycle.order_intent_sha256,
                        lifecycle.transition.value,
                        lifecycle.cumulative_filled_quantity,
                        lifecycle.prior_transition_event_sha256,
                    ),
                )
            _sealed_payload_fields(
                payload,
                required=frozenset(
                    {
                        "schema_version",
                        "broker_order_id",
                        "client_order_id",
                        "event_type",
                        "side",
                        "status",
                        "quantity_shares",
                        "cumulative_filled_quantity",
                        "average_fill_price",
                    }
                ),
                optional=frozenset({"reject_reason"}),
                description="broker lifecycle",
            )
            if (
                payload.get("schema_version")
                != SEALED_REPLAY_BROKER_LIFECYCLE_SCHEMA_VERSION
            ):
                raise SealedReplayInputError(
                    "sealed broker-lifecycle schema version is unsupported"
                )
            broker_order_id = str(payload.get("broker_order_id") or "").strip()
            client_order_id = str(payload.get("client_order_id") or "").strip()
            event_type = str(payload.get("event_type") or "").strip().lower()
            side = str(payload.get("side") or "").strip().lower()
            status = str(payload.get("status") or "").strip().lower()
            if not broker_order_id or not client_order_id or not status:
                raise SealedReplayInputError(
                    "sealed broker-lifecycle order identity/status is missing"
                )
            if event_type not in {
                "submitted",
                "acknowledged",
                "partially_filled",
                "filled",
                "canceled",
                "rejected",
            }:
                raise SealedReplayInputError(
                    "sealed broker-lifecycle event type is unsupported"
                )
            expected_status = {
                "submitted": "submitted",
                "acknowledged": "open",
                "partially_filled": "partially_filled",
                "filled": "filled",
                "canceled": "canceled",
                "rejected": "rejected",
            }[event_type]
            if status != expected_status:
                raise SealedReplayInputError(
                    "sealed broker-lifecycle event/status mismatch"
                )
            if side not in {"buy", "sell"}:
                raise SealedReplayInputError("sealed broker-lifecycle side is invalid")
            quantity = _sealed_finite_number(
                payload.get("quantity_shares"),
                "broker order quantity",
                positive=True,
            )
            cumulative = _sealed_finite_number(
                payload.get("cumulative_filled_quantity"),
                "broker cumulative fill",
                nonnegative=True,
            )
            if cumulative > quantity:
                raise SealedReplayInputError(
                    "sealed broker cumulative fill exceeds order quantity"
                )
            fill_price = payload.get("average_fill_price")
            if fill_price is not None:
                _sealed_finite_number(
                    fill_price, "broker average fill price", positive=True
                )
            if cumulative > 0.0 and fill_price is None:
                raise SealedReplayInputError(
                    "sealed broker filled quantity has no average fill price"
                )
            if event_type in {"submitted", "acknowledged", "rejected"} and (
                cumulative != 0.0 or fill_price is not None
            ):
                raise SealedReplayInputError(
                    "sealed broker pre-fill/reject event carries fill economics"
                )
            if event_type == "partially_filled" and not 0.0 < cumulative < quantity:
                raise SealedReplayInputError(
                    "sealed broker partial-fill quantity is not partial"
                )
            if event_type == "filled" and cumulative != quantity:
                raise SealedReplayInputError(
                    "sealed broker filled event is not cumulatively complete"
                )
            reject_reason = payload.get("reject_reason")
            if event_type == "rejected":
                if not isinstance(reject_reason, str) or not reject_reason.strip():
                    raise SealedReplayInputError(
                        "sealed broker rejection reason is missing"
                    )
            elif reject_reason is not None:
                raise SealedReplayInputError(
                    "sealed broker non-rejection carries a rejection reason"
                )
            event_at = event.clocks.provider_event_at
            if event_at is None:
                raise SealedReplayInputError(
                    "sealed broker lifecycle exact event clock is missing"
                )
            return (
                _SealedBrokerLifecycleInput(
                    event=event,
                    broker_order_id=broker_order_id,
                    event_type=event_type,
                ),
                (stream.value, broker_order_id, event_type, event_at),
            )

        raise SealedReplayInputError(
            f"sealed ReplayV3 cannot parse stream: {stream.value}"
        )

    @property
    def proof(self) -> SealedReplayInputProof:
        return replace(
            self._proof,
            adapter_network_attempt_count=self._network_attempt_count,
        )

    @property
    def network_attempt_count(self) -> int:
        return self._network_attempt_count

    @property
    def rejected_provider_request_count(self) -> int:
        return self._rejected_provider_requests

    @property
    def terminal_drain_complete(self) -> bool:
        return self._terminal_drain_complete

    @property
    def broker_events_unobserved_by_fsm(self) -> frozenset[str]:
        return self._broker_events_unobserved_by_fsm

    @property
    def continuous_decision_reads_observed_by_fsm(self) -> bool:
        """Whether NBBO/eligibility receipt consumption occurs at the real read seam.

        Replay currently pins the exact receipt-selected facts before invoking the
        unchanged FSM, but the broker quote getter and ORM eligibility read do not
        yet emit consumption acknowledgements.  This must remain false—and a
        certification blocker—until those two production seams are instrumented.
        """

        return False

    @property
    def runtime_input_capabilities(self) -> frozenset[str]:
        """Receipt-bound causal families the real FSM can consume in this adapter."""

        capabilities: set[str] = set()
        # Scanner support is declared only when every captured FSM invocation
        # carries exactly one typed scanner query receipt.  Merely having the
        # implementation in this class—or a caller-supplied flag—is not enough.
        scanner_ready = bool(self._decision_ticks) and all(
            CaptureStream.SCANNER_SNAPSHOT in tick.required_streams
            and len(
                tuple(
                    read
                    for read in tick.query_read_plan
                    if read.receipt.stream is CaptureStream.SCANNER_SNAPSHOT
                )
            )
            == 1
            for tick in self._decision_ticks
        )
        if scanner_ready:
            capabilities.add("scanner_snapshot")
        # Macro features now use the same exact OHLCV provider seam as the
        # rest of the real FSM, inside a replay-local cache.  Every captured
        # tick must carry at least one ordered, single-source typed OHLCV read;
        # extra/missing/out-of-order macro reads are rejected at consumption
        # and the end-of-tick plan proves none were silently skipped.
        macro_ready = bool(self._decision_ticks) and all(
            CaptureStream.PROVIDER_OHLCV in tick.required_streams
            and bool(
                tuple(
                    read
                    for read in tick.query_read_plan
                    if read.receipt.stream is CaptureStream.PROVIDER_OHLCV
                )
            )
            and all(
                len(read.source_event_sha256s) == 1
                and isinstance(
                    self._typed_by_hash.get(read.source_event_sha256s[0]),
                    _SealedOhlcvInput,
                )
                for read in tick.query_read_plan
                if read.receipt.stream is CaptureStream.PROVIDER_OHLCV
            )
            for tick in self._decision_ticks
        )
        if macro_ready:
            capabilities.add("macro")
        return frozenset(capabilities)

    def assert_runtime_input_capabilities(self) -> None:
        """Fail before the FSM can observe an unsealed P&L-changing input.

        A post-run certification blocker is not sufficient: governance state,
        L2/tape state, or macro features may already have changed the diagnostic
        trace.  Until each family is routed through an exact captured read seam,
        sealed ReplayV3 execution is intentionally unavailable.
        """

        missing = tuple(
            family
            for family in SEALED_RUNTIME_INPUT_FAMILIES
            if family not in self.runtime_input_capabilities
        )
        if missing:
            raise SealedReplayInputError(
                "sealed_runtime_input_family_unavailable:" + ",".join(missing)
            )

    @property
    def advanced_to(self) -> datetime | None:
        return self._advanced_to

    @property
    def replay_boundaries(self) -> tuple[datetime, ...]:
        """All causal release instants plus exact decision/window boundaries."""

        return tuple(
            sorted(
                {
                    self._request.warmup_start_at,
                    self._request.exit_end_at,
                    *(row.checkpoint.decision_at for row in self._decision_ticks),
                    *(event.clocks.available_at for event in self._release_order),
                }
            )
        )

    @property
    def replay_frontiers(self) -> tuple[tuple[datetime, int | None], ...]:
        """Exact FSM decision frontiers followed by one terminal input drain.

        Inputs do not independently invoke the FSM.  Each captured invocation
        releases only facts satisfying both ``available_at <= decision_at`` and
        ``sequence <= input_prefix_sequence``.  Coalescing intervening arrivals
        at the next real invocation also prevents a post-prefix event with an
        earlier availability clock from leaking into that decision.  The final
        unrestricted frontier drains post-decision broker/output facts through
        the exact requested window.
        """

        frontiers = {row.frontier for row in self._decision_ticks}
        frontiers.add((self._request.exit_end_at, None))
        return tuple(
            sorted(
                frontiers,
                key=lambda value: (
                    value[0],
                    value[1] if value[1] is not None else 2**63 - 1,
                ),
            )
        )

    def decision_tick_for_frontier(
        self,
        available_at: datetime,
        sequence_at_most: int | None,
    ) -> _SealedDecisionTick | None:
        """Return the exact captured FSM invocation for one causal frontier."""

        if available_at.tzinfo is None:
            raise SealedReplayInputError(
                "sealed ReplayV3 decision frontier must be timezone-aware"
            )
        frontier = (available_at.astimezone(timezone.utc), sequence_at_most)
        return self._decision_ticks_by_frontier.get(frontier)

    @property
    def decision_tick_count(self) -> int:
        return len(self._decision_ticks)

    def ready_for_decision(self, decision_tick: _SealedDecisionTick) -> bool:
        """Whether this exact tick's receipt-bound dependencies are released."""

        if decision_tick not in self._decision_ticks:
            return False
        for stream in decision_tick.required_streams:
            if stream is CaptureStream.BROKER_ORDER_LIFECYCLE:
                continue
            source_hashes = decision_tick.receipt_sources_for(stream)
            empty_first_dip_ready = (
                stream is CaptureStream.IQFEED_PRINT
                and any(
                    row.receipt.stream is stream
                    and row.receipt.empty_result
                    and not row.source_event_sha256s
                    for row in decision_tick.decision_read_plan
                )
            )
            if (
                not source_hashes
                and not empty_first_dip_ready
            ) or not source_hashes.issubset(self._released_event_sha256s):
                return False
        return True

    def begin_decision_read_plan(
        self, decision_tick: _SealedDecisionTick
    ) -> None:
        """Activate one exact receipt plan before invoking the real FSM."""

        if self._active_decision_tick is not None:
            raise SealedReplayInputError(
                "sealed ReplayV3 decision read plan is already active"
            )
        if self._advanced_frontier != decision_tick.frontier:
            raise SealedReplayInputError(
                "sealed ReplayV3 decision read plan/frontier mismatch"
            )
        if not self.ready_for_decision(decision_tick):
            raise SealedReplayInputError(
                "sealed ReplayV3 decision dependencies are not exactly released"
            )
        read_streams: set[CaptureStream] = set()
        for row in decision_tick.decision_read_plan:
            if not set(row.source_event_sha256s).issubset(
                self._released_event_sha256s
            ):
                raise SealedReplayInputError(
                    "sealed ReplayV3 decision read source is not released"
                )
            read_streams.add(row.receipt.stream)
        for stream in decision_tick.required_streams - {
            CaptureStream.BROKER_ORDER_LIFECYCLE
        }:
            if stream not in read_streams:
                raise SealedReplayInputError(
                    "sealed ReplayV3 decision receipt plan is incomplete: "
                    + stream.value
                )
        self._active_decision_tick = decision_tick
        self._active_query_reads = decision_tick.query_read_plan
        self._active_query_read_cursor = 0
        self._active_first_dip_tape_consumed = False
        self._active_first_dip_tape_authority = None
        self._active_first_dip_final_tape_consumed = False
        self._active_first_dip_final_authority = None
        self._active_sealed_adaptive_material = None

    def complete_decision_read_plan(self) -> None:
        """Require every captured query read to have been consumed exactly once."""

        if self._active_decision_tick is None:
            raise SealedReplayInputError(
                "sealed ReplayV3 decision read plan is not active"
            )
        missing = [
            row.receipt.read_id
            for row in self._active_query_reads[
                self._active_query_read_cursor :
            ]
        ]
        first_dip_read_id = str(
            self._active_decision_tick.checkpoint.decision_payload.get(
                "first_dip_tape_read_id"
            )
            or ""
        ).strip()
        if first_dip_read_id and not self._active_first_dip_tape_consumed:
            missing.append(first_dip_read_id)
        final_frontier = self._active_decision_tick.first_dip_final_frontier
        if final_frontier is not None:
            if not self._active_first_dip_final_tape_consumed:
                missing.append(final_frontier.tape_read.receipt.read_id)
            material = self._active_sealed_adaptive_material
            sealed_attestation = (
                None if material is None else material.sealed_replay_attestation
            )
            if sealed_attestation is None or not sealed_attestation.consumed:
                missing.append("sealed_adaptive_risk_request")
            if sealed_attestation is not None:
                sealed_attestation.revoke()
        self._active_decision_tick = None
        self._active_query_reads = ()
        self._active_query_read_cursor = 0
        self._active_first_dip_tape_consumed = False
        self._active_first_dip_tape_authority = None
        self._active_first_dip_final_tape_consumed = False
        self._active_first_dip_final_authority = None
        self._active_sealed_adaptive_material = None
        if missing:
            raise SealedReplayInputError(
                "sealed ReplayV3 FSM did not consume its exact query reads: "
                + ",".join(missing)
            )

    def abort_decision_read_plan(self) -> None:
        material = self._active_sealed_adaptive_material
        if material is not None and material.sealed_replay_attestation is not None:
            material.sealed_replay_attestation.revoke()
        self._active_decision_tick = None
        self._active_query_reads = ()
        self._active_query_read_cursor = 0
        self._active_first_dip_tape_consumed = False
        self._active_first_dip_tape_authority = None
        self._active_first_dip_final_tape_consumed = False
        self._active_first_dip_final_authority = None
        self._active_sealed_adaptive_material = None

    def _first_dip_tape_window(
        self,
        symbol: str | None = None,
        *,
        mark_consumed: bool,
    ) -> FirstDipTapeWindow:
        """Rebuild the one exact receipt-bound IQFeed window.

        Authority preparation peeks without satisfying the FSM read plan.  Only
        a direct consuming read or an accepted, fully validated decision receipt
        marks the captured read consumed.
        """

        decision_tick = self._active_decision_tick
        if decision_tick is None:
            raise SealedReplayInputError(
                "sealed first-dip tape read occurred outside a captured FSM tick"
            )
        if self._active_first_dip_tape_consumed:
            raise SealedReplayInputError(
                "sealed first-dip tape receipt was consumed more than once"
            )
        read_id = str(
            decision_tick.checkpoint.decision_payload.get(
                "first_dip_tape_read_id"
            )
            or ""
        ).strip()
        reads = decision_tick.receipt_reads_for(CaptureStream.IQFEED_PRINT)
        if len(reads) != 1 or not read_id or reads[0].receipt.read_id != read_id:
            raise SealedReplayInputError(
                "sealed first-dip tape receipt provenance is missing or ambiguous"
            )
        receipt = reads[0].receipt
        normalized = str(symbol or self._request.symbol or "").strip().upper()
        if receipt.symbol != normalized or normalized != decision_tick.checkpoint.symbol:
            raise SealedReplayInputError(
                "sealed first-dip tape receipt symbol does not match the decision"
            )
        parsed_prints: list[_SealedIqfeedPrintInput] = []
        for source_sha256 in reads[0].source_event_sha256s:
            if source_sha256 not in self._released_event_sha256s:
                raise SealedReplayInputError(
                    "sealed first-dip tape source is not causally released"
                )
            parsed = self._typed_by_hash.get(source_sha256)
            if not isinstance(parsed, _SealedIqfeedPrintInput):
                raise SealedReplayInputError(
                    "sealed first-dip tape receipt references a non-print fact"
                )
            parsed_prints.append(parsed)
        provider_event_ats = tuple(
            parsed.event.clocks.provider_event_at for parsed in parsed_prints
        )
        if any(value is None for value in provider_event_ats):
            raise SealedReplayInputError(
                "sealed first-dip tape source lacks an exact provider clock"
            )
        typed_event_ats = tuple(value for value in provider_event_ats if value is not None)
        if tuple(
            sorted(
                zip(
                    typed_event_ats,
                    (parsed.event.sequence for parsed in parsed_prints),
                )
            )
        ) != tuple(
            (value, parsed.event.sequence)
            for value, parsed in zip(typed_event_ats, parsed_prints)
        ):
            raise SealedReplayInputError(
                "sealed first-dip tape facts regressed in provider event order"
            )
        if mark_consumed:
            self._active_first_dip_tape_consumed = True
        return FirstDipTapeWindow(
            read_id=read_id,
            symbol=normalized,
            requested_at=receipt.requested_at,
            returned_at=receipt.returned_at,
            result_sha256=receipt.result_sha256,
            source_event_sha256s=reads[0].source_event_sha256s,
            provider_event_ats=typed_event_ats,
            rows=tuple(parsed.tape_row() for parsed in parsed_prints),
        )

    def consume_first_dip_tape_read(
        self, symbol: str | None = None
    ) -> FirstDipTapeWindow:
        """Consume the exact active IQFeed print window once, without fallback."""

        return self._first_dip_tape_window(symbol, mark_consumed=True)

    def _evaluate_first_dip_tape(
        self,
        *,
        policy: FirstDipTapePolicy,
        symbol: str | None = None,
        mark_consumed: bool,
    ) -> FirstDipTapeEvaluation:
        """Run the shared replay/paper policy at the captured decision clock."""

        decision_tick = self._active_decision_tick
        if decision_tick is None:
            raise SealedReplayInputError(
                "sealed first-dip evaluation occurred outside a captured FSM tick"
            )
        normalized = str(symbol or self._request.symbol or "").strip().upper()
        payload = decision_tick.checkpoint.decision_payload
        raw_policy = payload.get("first_dip_tape_policy")
        if (
            not isinstance(raw_policy, Mapping)
            or dict(raw_policy) != policy.to_dict()
            or payload.get("first_dip_tape_policy_sha256")
            != policy.policy_sha256
        ):
            raise SealedReplayInputError(
                "sealed first-dip policy differs from captured provenance"
            )
        window = self._first_dip_tape_window(
            normalized,
            mark_consumed=mark_consumed,
        )
        evaluation = evaluate_first_dip_tape(
            window,
            policy=policy,
            decision_at=decision_tick.checkpoint.decision_at,
            symbol=normalized,
        )
        raw_evaluation = payload.get("first_dip_tape_evaluation")
        if (
            not isinstance(raw_evaluation, Mapping)
            or dict(raw_evaluation) != evaluation.to_dict()
            or payload.get("first_dip_tape_evaluation_sha256")
            != evaluation.evaluation_sha256
        ):
            raise SealedReplayInputError(
                "sealed first-dip evaluation differs from exact captured prints"
            )
        return evaluation

    def evaluate_first_dip_tape(
        self,
        *,
        policy: FirstDipTapePolicy,
        symbol: str | None = None,
    ) -> FirstDipTapeEvaluation:
        """Consume and evaluate the shared policy for direct adapter callers."""

        return self._evaluate_first_dip_tape(
            policy=policy,
            symbol=symbol,
            mark_consumed=True,
        )

    def prepare_first_dip_tape_decision_authority(
        self,
    ) -> _VerifiedFirstDipTapeDecisionAuthority:
        """Prepare one exact private authority from the active sealed boundary.

        Preparation verifies all retained-prefix, receipt, coverage, query, and
        evaluation provenance but deliberately does *not* satisfy the captured
        FSM read plan.  The read is marked consumed only after the real detector
        presents the exact request and the resolver validates and atomically
        consumes the receipt lineage.
        """

        decision_tick = self._active_decision_tick
        if decision_tick is None:
            raise SealedReplayInputError(
                "sealed first-dip authority preparation occurred outside an active tick"
            )
        if self._active_first_dip_tape_authority is not None:
            return self._active_first_dip_tape_authority
        checkpoint = decision_tick.checkpoint
        raw_policy = checkpoint.decision_payload.get("first_dip_tape_policy")
        if not isinstance(raw_policy, Mapping):
            raise SealedReplayInputError(
                "sealed first-dip authority lacks an exact captured policy"
            )
        try:
            policy = FirstDipTapePolicy.from_dict(raw_policy)
            purpose = str(
                checkpoint.decision_payload.get(
                    "first_dip_tape_purpose",
                    FIRST_DIP_TAPE_PURPOSE_DETECTOR,
                )
                or ""
            ).strip().lower()
            request = FirstDipTapeDecisionRequest(
                symbol=checkpoint.symbol,
                decision_at=checkpoint.decision_at,
                policy=policy,
                purpose=purpose,
            )
        except (CaptureContractError, TypeError, ValueError) as exc:
            raise SealedReplayInputError(
                "sealed first-dip authority policy is malformed"
            ) from exc
        if (
            checkpoint.decision_payload.get("first_dip_tape_policy_sha256")
            != policy.policy_sha256
        ):
            raise SealedReplayInputError(
                "sealed first-dip authority policy digest is inconsistent"
            )
        if request.purpose == FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION:
            # A final-purpose receipt must prove inclusion of the earlier
            # detector receipt/opportunity under this later prefix.  The
            # current fixture/adapter has no typed cross-checkpoint reference;
            # never synthesize one from serialized debug.
            raise SealedReplayInputError(
                "sealed first-dip pre-reservation prior-detector proof is unavailable"
            )
        reads = decision_tick.receipt_reads_for(CaptureStream.IQFEED_PRINT)
        if len(reads) != 1:
            raise SealedReplayInputError(
                "sealed first-dip detector receipt is missing or ambiguous"
            )
        capture_receipt = reads[0].receipt
        if capture_receipt.query is None:
            raise SealedReplayInputError(
                "sealed first-dip detector receipt lacks its typed query"
            )
        try:
            query = FirstDipTapeReadQuery.from_dict(capture_receipt.query)
            query.validate_for_policy(request.policy)
        except (CaptureContractError, TypeError, ValueError) as exc:
            raise SealedReplayInputError(
                "sealed first-dip detector query is malformed"
            ) from exc
        if (
            query.symbol != request.symbol
            or query.decision_at != request.decision_at
            or query.available_at_most != request.decision_at
            or query.policy_sha256 != request.policy.policy_sha256
        ):
            raise SealedReplayInputError(
                "sealed first-dip detector query escaped its exact decision"
            )

        # Defense in depth: bind the receipt to the complete retained prefix
        # again at the point where the private detector capability is issued.
        _sealed_first_dip_receipt_inventory(
            receipt=capture_receipt,
            checkpoint=checkpoint,
            manifest=self._manifest,
        )
        receipt_payload_sha256 = sha256_json(capture_receipt.to_dict())
        receipt_commit_refs = tuple(
            ref
            for ref in self._manifest.event_index.values()
            if ref.stream is CaptureStream.READ_RECEIPT
            and ref.payload_sha256 == receipt_payload_sha256
            and ref.sequence <= checkpoint.input_prefix_sequence
        )
        if len(receipt_commit_refs) != 1:
            raise SealedReplayInputError(
                "sealed first-dip detector receipt commit is ambiguous"
            )
        receipt_commit_ref = receipt_commit_refs[0]
        coverage = self._manifest.stream_coverage.get(CaptureStream.IQFEED_PRINT)
        if (
            coverage is None
            or coverage.identity_sha256 != self._manifest.identity.identity_sha256
            or coverage.symbol != request.symbol
            or coverage.provider.strip().lower() != query.provider
            or coverage.watermark is None
            or coverage.watermark.generation != self._manifest.identity.generation
            or coverage.watermark.event_watermark_at < request.decision_at
            or coverage.watermark.emitted_available_at
            < receipt_commit_ref.available_at
        ):
            raise SealedReplayInputError(
                "sealed first-dip detector coverage proof is unavailable"
            )

        evaluation = self._evaluate_first_dip_tape(
            policy=request.policy,
            symbol=request.symbol,
            mark_consumed=False,
        )
        grade = self._coverage_grade
        binding = _FirstDipTapeDecisionBinding(
            run_id=self._manifest.identity.run_id,
            authority_source="sealed_replay",
            purpose=request.purpose,
            generation=self._manifest.identity.generation,
            identity_sha256=self._manifest.identity.identity_sha256,
            symbol=request.symbol,
            decision_id=checkpoint.decision_id,
            decision_at=checkpoint.decision_at,
            boundary_attested_available_at=checkpoint.available_at,
            boundary_expires_at=(
                checkpoint.decision_at
                + timedelta(seconds=request.policy.max_source_age_seconds)
            ),
            input_prefix_sequence=checkpoint.input_prefix_sequence,
            input_prefix_root_sha256=checkpoint.input_prefix_root_sha256,
            admission_handoff_sha256=None,
            adaptive_request_sha256=None,
            opportunity_key_sha256=None,
            decision_checkpoint_sha256=checkpoint.checkpoint_sha256,
            final_capture_seal_sha256=self._proof.final_capture_seal_sha256,
            coverage_manifest_sha256=self._manifest.manifest_sha256,
            coverage_grade_sha256=sha256_json(
                {
                    "replayable": grade.replayable,
                    "grade": grade.grade,
                    "reasons": list(grade.reasons),
                    "manifest_sha256": grade.manifest_sha256,
                }
            ),
            stream_coverage_sha256=sha256_json(coverage.to_dict()),
            active_input_attestation_sha256=None,
            active_continuity_inventory_sha256=None,
            active_producer_generations_sha256=None,
            active_resource_binding_sha256=None,
            read_receipt_sha256=receipt_payload_sha256,
            receipt_event_sha256=receipt_commit_ref.event_sha256,
            receipt_event_sequence=receipt_commit_ref.sequence,
            receipt_committed_available_at=receipt_commit_ref.available_at,
            source_frontier_sequence=query.source_frontier_sequence,
            source_event_inventory_sha256=sha256_json(
                {
                    "read_id": capture_receipt.read_id,
                    "source_event_sha256s": list(
                        capture_receipt.source_event_sha256s
                    ),
                }
            ),
            watermark_event_at=coverage.watermark.event_watermark_at,
            watermark_emitted_available_at=(
                coverage.watermark.emitted_available_at
            ),
            evaluation_sha256=evaluation.evaluation_sha256,
            prior_detector_reference=None,
        )
        authority_holder: list[_VerifiedFirstDipTapeDecisionAuthority] = []

        def accept_exact_active_read() -> None:
            authority = authority_holder[0] if authority_holder else None
            if (
                authority is None
                or self._active_decision_tick is not decision_tick
                or self._active_first_dip_tape_authority is not authority
                or self._active_first_dip_tape_consumed
                or checkpoint.decision_payload.get("first_dip_tape_read_id")
                != capture_receipt.read_id
                or checkpoint.decision_payload.get(
                    "first_dip_tape_evaluation_sha256"
                )
                != evaluation.evaluation_sha256
                or str(
                    checkpoint.decision_payload.get(
                        "first_dip_tape_purpose",
                        FIRST_DIP_TAPE_PURPOSE_DETECTOR,
                    )
                    or ""
                ).strip().lower()
                != request.purpose
            ):
                raise SealedReplayInputError(
                    "sealed first-dip accepted receipt is not the active boundary"
                )
            self._active_first_dip_tape_consumed = True

        authority = (
            _FIRST_DIP_TAPE_DECISION_AUTHORITY_ISSUER.issue_sealed_replay(
                request,
                binding,
                evaluation,
                accept_exact_active_read,
            )
        )
        authority_holder.append(authority)
        self._active_first_dip_tape_authority = authority
        return authority

    def prepare_first_dip_adaptive_risk_material(
        self,
    ) -> AdaptiveRiskRuntimeCaptureMaterial:
        """Rebuild one exact recorded paper source under a sealed capability.

        The persisted request is not treated as a source or attestation.  Its
        raw policy/input/account fields are rebound to the detector prefix and
        sealed identity, then the ordinary builder must reproduce the exact
        request hash before its one-shot proof is considered consumed.
        """

        decision_tick = self._active_decision_tick
        if decision_tick is None or decision_tick.first_dip_final_frontier is None:
            raise SealedReplayInputError(
                "sealed first-dip adaptive material has no active final frontier"
            )
        if self._active_sealed_adaptive_material is not None:
            return self._active_sealed_adaptive_material
        final = decision_tick.first_dip_final_frontier
        expected = final.adaptive_request
        inputs = expected.inputs
        capture_evidence = inputs.evidence.get("capture_prefix")
        if not isinstance(capture_evidence, RiskInputEvidence):
            raise SealedReplayInputError(
                "sealed first-dip adaptive request lacks capture-prefix evidence"
            )
        checkpoint = decision_tick.checkpoint
        identity = self._manifest.identity
        if (
            inputs.execution_surface != "alpaca_paper"
            or inputs.replay_or_paper_run_id != identity.run_id
            or inputs.generation != identity.generation
            or inputs.decision_id != checkpoint.decision_id
            or inputs.symbol != checkpoint.symbol
            or inputs.capture_prefix_root_sha256
            != checkpoint.input_prefix_root_sha256
            or capture_evidence.content_sha256
            != checkpoint.input_prefix_root_sha256
        ):
            raise SealedReplayInputError(
                "sealed first-dip adaptive request escaped its recorded paper prefix"
            )
        try:
            binding = AdaptiveRiskDiagnosticCaptureBinding.create_diagnostic(
                run_id=identity.run_id,
                generation=identity.generation,
                decision_id=checkpoint.decision_id,
                input_prefix_sequence=checkpoint.input_prefix_sequence,
                input_prefix_root_sha256=checkpoint.input_prefix_root_sha256,
                identity_sha256=identity.identity_sha256,
                observed_at=capture_evidence.observed_at,
                available_at=capture_evidence.available_at,
                verifier_generation=capture_evidence.provider_generation,
            )
            source = AdaptiveRiskBuilderSource(
                policy=expected.policy,
                inputs=expected.inputs,
                account_snapshot=expected.account_snapshot,
                capture_binding=binding,
                account_scope=expected.account_scope,
                setup_family=expected.setup_family,
                correlation_cluster=expected.correlation_cluster,
            )
            attestation = _issue_sealed_replay_adaptive_risk_build_attestation(
                source=source,
                expected_request=expected,
                identity_sha256=identity.identity_sha256,
                final_capture_seal_sha256=self._proof.final_capture_seal_sha256,
                coverage_manifest_sha256=self._manifest.manifest_sha256,
                decision_checkpoint_sha256=checkpoint.checkpoint_sha256,
            )
            material = AdaptiveRiskRuntimeCaptureMaterial(
                source=source,
                sealed_replay_attestation=attestation,
            )
        except (AdaptiveRiskBuilderError, TypeError, ValueError) as exc:
            raise SealedReplayInputError(
                "sealed first-dip adaptive request cannot be reconstructed"
            ) from exc
        self._active_sealed_adaptive_material = material
        return material

    def prepare_first_dip_final_tape_decision_handoff(
        self,
        *,
        adaptive_request: object,
        detector_policy: FirstDipTapePolicy,
        final_boundary_available_at: datetime,
    ) -> object:
        """Mint the fresh sealed final authority from one verified frontier."""

        decision_tick = self._active_decision_tick
        if decision_tick is None or decision_tick.first_dip_final_frontier is None:
            raise SealedReplayInputError(
                "sealed first-dip final authority has no active frontier"
            )
        final = decision_tick.first_dip_final_frontier
        frontier = final.frontier
        checkpoint = decision_tick.checkpoint
        if (
            not isinstance(final_boundary_available_at, datetime)
            or final_boundary_available_at.tzinfo is None
        ):
            raise SealedReplayInputError(
                "sealed first-dip final caller clock is malformed"
            )
        caller_boundary = final_boundary_available_at.astimezone(timezone.utc)
        if (
            not isinstance(detector_policy, FirstDipTapePolicy)
            or detector_policy.to_dict() != final.policy.to_dict()
            or type(adaptive_request) is not type(final.adaptive_request)
            or adaptive_request.to_payload() != final.adaptive_request.to_payload()
            or caller_boundary < checkpoint.decision_at
            or caller_boundary > frontier.final_boundary_available_at
        ):
            raise SealedReplayInputError(
                "sealed first-dip final authority request escaped its frontier"
            )
        material = self._active_sealed_adaptive_material
        sealed_attestation = (
            None if material is None else material.sealed_replay_attestation
        )
        detector_authority = self._active_first_dip_tape_authority
        if (
            material is None
            or sealed_attestation is None
            or not sealed_attestation.consumed
            or detector_authority is None
            or not self._active_first_dip_tape_consumed
        ):
            raise SealedReplayInputError(
                "sealed first-dip final authority lacks consumed detector/request lineage"
            )
        detector_resolution = FirstDipTapeDecisionResolution(
            evaluation=detector_authority.receipt.evaluation,
            receipt=detector_authority.receipt,
        )
        replay_prior = _prior_detector_reference_from_resolution(
            detector_resolution,
            opportunity_key_sha256=frontier.opportunity_key_sha256,
        )
        captured_prior = final.prior_detector_reference
        if (
            replay_prior.symbol != captured_prior.symbol
            or replay_prior.decision_id != captured_prior.decision_id
            or replay_prior.decision_at != captured_prior.decision_at
            or replay_prior.input_prefix_root_sha256
            != captured_prior.input_prefix_root_sha256
            or replay_prior.policy_sha256 != captured_prior.policy_sha256
            or replay_prior.evaluation_sha256
            != captured_prior.evaluation_sha256
            or replay_prior.opportunity_key_sha256
            != captured_prior.opportunity_key_sha256
        ):
            raise SealedReplayInputError(
                "sealed first-dip detector replay differs from captured lineage"
            )
        if self._active_first_dip_final_authority is not None:
            return _issue_first_dip_final_authority_handoff(
                authority=self._active_first_dip_final_authority,
                final_boundary_available_at=frontier.final_boundary_available_at,
                source="sealed_replay",
            )

        read = final.tape_read_evidence
        continuity = final.tape_continuity_evidence
        receipt = read.receipt
        watermark = continuity.coverage.watermark
        if receipt.query is None or watermark is None:
            raise SealedReplayInputError(
                "sealed first-dip final receipt/continuity proof is incomplete"
            )
        query = FirstDipTapeReadQuery.from_dict(receipt.query)
        grade = self._coverage_grade
        binding = _FirstDipTapeDecisionBinding(
            run_id=self._manifest.identity.run_id,
            authority_source="sealed_replay",
            purpose=FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION,
            generation=self._manifest.identity.generation,
            identity_sha256=self._manifest.identity.identity_sha256,
            symbol=query.symbol,
            decision_id=frontier.decision_id,
            decision_at=query.decision_at,
            boundary_attested_available_at=frontier.attested_available_at,
            boundary_expires_at=frontier.expires_at,
            input_prefix_sequence=frontier.input_prefix_sequence,
            input_prefix_root_sha256=frontier.input_prefix_root_sha256,
            admission_handoff_sha256=None,
            adaptive_request_sha256=frontier.adaptive_request_sha256,
            opportunity_key_sha256=frontier.opportunity_key_sha256,
            decision_checkpoint_sha256=checkpoint.checkpoint_sha256,
            final_capture_seal_sha256=self._proof.final_capture_seal_sha256,
            coverage_manifest_sha256=self._manifest.manifest_sha256,
            coverage_grade_sha256=sha256_json(
                {
                    "replayable": grade.replayable,
                    "grade": grade.grade,
                    "reasons": list(grade.reasons),
                    "manifest_sha256": grade.manifest_sha256,
                }
            ),
            stream_coverage_sha256=sha256_json(continuity.coverage.to_dict()),
            active_input_attestation_sha256=None,
            active_continuity_inventory_sha256=None,
            active_producer_generations_sha256=None,
            active_resource_binding_sha256=None,
            read_receipt_sha256=read.receipt_sha256,
            receipt_event_sha256=read.receipt_event_sha256,
            receipt_event_sequence=read.receipt_event_sequence,
            receipt_committed_available_at=read.receipt_committed_available_at,
            source_frontier_sequence=query.source_frontier_sequence,
            source_event_inventory_sha256=sha256_json(
                {
                    "read_id": receipt.read_id,
                    "source_event_sha256s": list(receipt.source_event_sha256s),
                }
            ),
            watermark_event_at=watermark.event_watermark_at,
            watermark_emitted_available_at=watermark.emitted_available_at,
            evaluation_sha256=final.evaluation.evaluation_sha256,
            prior_detector_reference=replay_prior,
        )
        authority_holder: list[_VerifiedFirstDipTapeDecisionAuthority] = []

        def accept_exact_final_read() -> None:
            authority = authority_holder[0] if authority_holder else None
            if (
                authority is None
                or self._active_decision_tick is not decision_tick
                or self._active_first_dip_final_authority is not authority
                or self._active_first_dip_final_tape_consumed
            ):
                raise SealedReplayInputError(
                    "sealed first-dip final receipt is not the active boundary"
                )
            self._active_first_dip_final_tape_consumed = True

        request = FirstDipTapeDecisionRequest(
            symbol=query.symbol,
            decision_at=query.decision_at,
            policy=final.policy,
            purpose=FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION,
        )
        authority = _FIRST_DIP_TAPE_DECISION_AUTHORITY_ISSUER.issue_sealed_replay(
            request,
            binding,
            final.evaluation,
            accept_exact_final_read,
        )
        authority_holder.append(authority)
        self._active_first_dip_final_authority = authority
        return _issue_first_dip_final_authority_handoff(
            authority=authority,
            final_boundary_available_at=frontier.final_boundary_available_at,
            source="sealed_replay",
        )

    @property
    def network_fallback_allowed(self) -> bool:
        """Sealed providers are process-local and can never fetch missing data."""

        return False

    def _peek_decision_query_window(
        self, stream: CaptureStream
    ) -> tuple[int, _SealedDecisionRead, tuple[Any, ...]]:
        """Validate one ordered multi-event read without consuming it yet."""

        if self._active_decision_tick is None:
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 provider read occurred outside a captured FSM tick"
            )
        cursor = self._active_query_read_cursor
        if cursor >= len(self._active_query_reads):
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 FSM made an extra or out-of-order provider read: "
                + stream.value
            )
        read = self._active_query_reads[cursor]
        if read.receipt.stream is not stream:
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 FSM made an extra or out-of-order provider read: "
                + stream.value
            )
        parsed_rows: list[Any] = []
        for source_sha256 in read.source_event_sha256s:
            if source_sha256 not in self._released_event_sha256s:
                self._rejected_provider_requests += 1
                raise SealedReplayInputError(
                    "sealed ReplayV3 provider read source is not causally released"
                )
            parsed = self._typed_by_hash.get(source_sha256)
            if parsed is None:
                self._rejected_provider_requests += 1
                raise SealedReplayInputError(
                    "sealed ReplayV3 provider read selected an untyped source"
                )
            parsed_rows.append(parsed)
        if read.receipt.empty_result != (not parsed_rows):
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 provider read empty-result claim is inconsistent"
            )
        return cursor, read, tuple(parsed_rows)

    def _consume_decision_query(
        self, stream: CaptureStream
    ) -> tuple[_SealedDecisionRead, Any]:
        if self._active_decision_tick is None:
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 provider read occurred outside a captured FSM tick"
            )
        cursor = self._active_query_read_cursor
        if cursor >= len(self._active_query_reads):
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 FSM made an extra or out-of-order provider read: "
                + stream.value
            )
        read = self._active_query_reads[cursor]
        if read.receipt.stream is not stream:
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 FSM made an extra or out-of-order provider read: "
                + stream.value
            )
        if len(read.source_event_sha256s) != 1:
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 provider read has ambiguous source facts"
            )
        source_sha256 = read.source_event_sha256s[0]
        if source_sha256 not in self._released_event_sha256s:
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 provider read source is not causally released"
            )
        parsed = self._typed_by_hash.get(source_sha256)
        self._active_query_read_cursor = cursor + 1
        return read, parsed

    def _receipt_selected_scalar_fact(self, stream: CaptureStream) -> Any:
        decision_tick = self._active_decision_tick
        if decision_tick is None:
            raise SealedReplayInputError(
                "sealed ReplayV3 scalar decision read occurred outside a captured FSM tick"
            )
        reads = decision_tick.receipt_reads_for(stream)
        if len(reads) != 1 or len(reads[0].source_event_sha256s) != 1:
            raise SealedReplayInputError(
                "sealed ReplayV3 scalar decision read has ambiguous receipt provenance: "
                + stream.value
            )
        source_sha256 = reads[0].source_event_sha256s[0]
        if source_sha256 not in self._released_event_sha256s:
            raise SealedReplayInputError(
                "sealed ReplayV3 scalar receipt source is not causally released: "
                + stream.value
            )
        parsed = self._typed_by_hash.get(source_sha256)
        if parsed is None:
            raise SealedReplayInputError(
                "sealed ReplayV3 scalar receipt source is outside the typed input set: "
                + stream.value
            )
        return parsed

    @property
    def canonical_order_intents(self) -> tuple[CaptureOrderIntent, ...]:
        return tuple(
            sorted(
                self._canonical_order_intents.values(),
                key=lambda row: (row.client_order_id, row.order_intent_sha256),
            )
        )

    @property
    def malformed_decision_sequences(self) -> tuple[int, ...]:
        return self._malformed_decision_sequences

    def _canonical_broker_inputs(
        self, *, released_only: bool
    ) -> tuple[_SealedBrokerLifecycleInput, ...]:
        values = (
            self._released_broker_lifecycle
            if released_only
            else [
                parsed
                for parsed in self._typed_by_hash.values()
                if isinstance(parsed, _SealedBrokerLifecycleInput)
            ]
        )
        return tuple(row for row in values if row.canonical is not None)

    @staticmethod
    def _to_recorded_intent(intent: CaptureOrderIntent) -> RecordedOrderIntent:
        return RecordedOrderIntent(
            order_intent_sha256=intent.order_intent_sha256,
            client_order_id=intent.client_order_id,
            product_id=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            base_size=float(intent.quantity),
            time_in_force=intent.time_in_force,
            extended_hours=intent.extended_hours,
            limit_price=intent.limit_price,
        )

    @staticmethod
    def _to_recorded_transition(
        value: _SealedBrokerLifecycleInput,
    ) -> RecordedBrokerTransition:
        row = value.canonical
        if row is None:
            raise SealedReplayInputError(
                "legacy broker lifecycle cannot drive sealed ReplayV3"
            )
        return RecordedBrokerTransition(
            event_sha256=value.event.event_sha256,
            sequence=value.event.sequence,
            available_at=value.event.clocks.available_at,
            order_intent_sha256=row.order_intent_sha256,
            client_order_id=row.client_order_id,
            broker_order_id=row.broker_order_id,
            transition=row.transition.value,
            order_quantity=float(row.order_quantity),
            cumulative_filled_quantity=float(row.cumulative_filled_quantity),
            last_fill_quantity=float(row.last_fill_quantity),
            last_fill_price=row.last_fill_price,
            reject_or_cancel_reason=row.reject_or_cancel_reason,
        )

    @property
    def canonical_broker_lifecycle_complete(self) -> bool:
        if self._malformed_decision_sequences:
            return False
        values = self._canonical_broker_inputs(released_only=False)
        if not self._canonical_order_intents:
            return not values
        by_intent: dict[str, list[CaptureBrokerOrderLifecycle]] = {}
        for value in values:
            assert value.canonical is not None
            by_intent.setdefault(value.canonical.order_intent_sha256, []).append(
                value.canonical
            )
        if set(by_intent) != set(self._canonical_order_intents):
            return False
        return all(rows and rows[-1].terminal for rows in by_intent.values())

    def configure_recorded_broker(self, mock: MockBrokerAdapter) -> None:
        if not self.canonical_broker_lifecycle_complete:
            raise SealedReplayInputError(
                "canonical terminal broker lifecycle is incomplete"
            )
        try:
            mock.configure_recorded_lifecycle(
                intents=tuple(
                    self._to_recorded_intent(intent)
                    for intent in self.canonical_order_intents
                ),
                transitions=tuple(
                    self._to_recorded_transition(value)
                    for value in self._canonical_broker_inputs(released_only=False)
                ),
            )
        except (TypeError, ValueError) as exc:
            raise SealedReplayInputError(
                f"canonical broker lifecycle cannot drive the FSM: {exc}"
            ) from exc

    def released_recorded_broker_transitions(
        self,
    ) -> tuple[RecordedBrokerTransition, ...]:
        return tuple(
            self._to_recorded_transition(value)
            for value in self._canonical_broker_inputs(released_only=True)
        )

    def released_counterfactual_exact_prints(
        self,
    ) -> tuple[VerifiedExactPrint, ...]:
        """Exact prints released by the verified dual-clock frontier so far.

        These private-token objects may drive deterministic fill-allocation
        mechanics in a future counterfactual arm. They are intentionally not a
        counterfactual receipt and reproduction mode does not consume them.
        """

        return tuple(self._released_counterfactual_exact_prints)

    def counterfactual_exact_print_inventory(
        self,
    ) -> VerifiedExactPrintInventory:
        """Private-token complete print inventory bound to this sealed capture."""

        return self._counterfactual_exact_print_inventory

    @property
    def broker_lifecycle_inventory_root_sha256(self) -> str:
        values = self._canonical_broker_inputs(released_only=False)
        return sha256_json(
            {
                "identity_sha256": self._proof.identity_sha256,
                "decision_checkpoint_sha256": (
                    self._proof.decision_checkpoint_sha256
                ),
                "order_intents": [
                    intent.to_dict() for intent in self.canonical_order_intents
                ],
                "transitions": [
                    {
                        "event_sha256": value.event.event_sha256,
                        "available_at": value.event.clocks.available_at,
                        "sequence": value.event.sequence,
                        "payload_sha256": value.event.payload_sha256,
                    }
                    for value in values
                ],
            }
        )

    def apply_current_eligibility(
        self,
        db: Session,
        *,
        symbol: str,
        variant_id: int,
    ) -> None:
        normalized = str(symbol or "").strip().upper()
        if self._active_decision_tick is not None:
            selected = self._receipt_selected_scalar_fact(
                CaptureStream.ADMISSION_ELIGIBILITY
            )
            fact = selected if isinstance(selected, _SealedEligibilityInput) else None
        else:
            fact = self._current_eligibility.get(normalized)
        if fact is None:
            raise SealedReplayInputError(
                "no captured admission/eligibility fact is released; fallback is forbidden"
            )
        row = (
            db.query(MomentumSymbolViability)
            .filter(
                MomentumSymbolViability.symbol == normalized,
                MomentumSymbolViability.variant_id == int(variant_id),
            )
            .one_or_none()
        )
        if row is None:
            raise SealedReplayInputError(
                "sealed eligibility target viability row is missing"
            )
        row.live_eligible = fact.eligible
        row.freshness_ts = fact.freshness_at.astimezone(timezone.utc).replace(
            tzinfo=None
        )
        db.flush()

    def account_equity_provider(
        self, _execution_family: Any = None, **_kwargs: Any
    ) -> Optional[float]:
        read, fact = self._consume_decision_query(
            CaptureStream.ACCOUNT_RISK_SNAPSHOT
        )
        if not isinstance(fact, _SealedAccountRiskInput):
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 account receipt selected a non-account fact"
            )
        prefer_cash = bool(_kwargs.get("prefer_cash_value"))
        prefer_equity = bool(_kwargs.get("prefer_equity")) or prefer_cash
        field_name = (
            "cash_usd"
            if prefer_cash
            else ("equity_usd" if prefer_equity else "buying_power_usd")
        )
        query = fact.event.query
        fields = query.get("fields") if isinstance(query, Mapping) else None
        if (
            read.receipt.query_sha256 != fact.event.query_sha256
            or not isinstance(fields, (list, tuple))
            or field_name not in fields
        ):
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 account provider call differs from its receipt"
            )
        value = fact.event.payload.get(field_name)
        return float(value) if value is not None else None

    def read_microstructure(
        self,
        *,
        operation: CaptureMicrostructureOperation,
        symbol: str,
        decision_at: datetime,
        parameters: Mapping[str, Any],
    ) -> Any:
        """Recompute one exact print-window result from its sealed receipt."""

        if operation not in LiveMicrostructureCaptureBridge._PRINT_OPERATIONS:
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed IQFeed L2 microstructure remains coverage-unavailable"
            )
        cursor, read, parsed_rows = self._peek_decision_query_window(
            CaptureStream.IQFEED_PRINT
        )
        try:
            query = CaptureMicrostructureReadQuery.from_dict(
                read.receipt.query or {}
            )
        except (CaptureContractError, TypeError, ValueError) as exc:
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed microstructure receipt query is malformed"
            ) from exc
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_decision = (
            decision_at.astimezone(timezone.utc)
            if isinstance(decision_at, datetime) and decision_at.tzinfo is not None
            else decision_at
        )
        if (
            normalized_symbol != query.symbol
            or query.operation is not operation
            or query.stream is not CaptureStream.IQFEED_PRINT
            or query.provider != "iqfeed"
            or normalized_decision != query.decision_at
            or dict(query.parameters) != dict(parameters)
            or read.receipt.query_sha256 != sha256_json(query.to_dict())
            or read.receipt.returned_at != query.available_at_most
        ):
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed microstructure provider call differs from its exact receipt"
            )
        if any(not isinstance(row, _SealedIqfeedPrintInput) for row in parsed_rows):
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed microstructure receipt selected a non-print source"
            )
        decision_tick = self._active_decision_tick
        assert decision_tick is not None
        visible = tuple(
            sorted(
                (
                    row
                    for row in self._typed_by_hash.values()
                    if isinstance(row, _SealedIqfeedPrintInput)
                    and row.event.provider == query.provider
                    and row.event.symbol == query.symbol
                    and row.event.clocks.available_at <= query.available_at_most
                    and row.event.sequence
                    <= decision_tick.checkpoint.input_prefix_sequence
                ),
                key=lambda row: (
                    row.event.clocks.provider_event_at,
                    row.event.sequence,
                ),
            )
        )
        actual_frontier = max(
            (row.event.sequence for row in visible),
            default=0,
        )
        expected = tuple(
            row
            for row in visible
            if row.event.clocks.provider_event_at is not None
            and row.event.clocks.provider_event_at > query.event_start_exclusive
            and row.event.clocks.provider_event_at <= query.event_end_inclusive
        )
        if (
            actual_frontier != query.source_frontier_sequence
            or tuple(row.event.event_sha256 for row in expected)
            != read.source_event_sha256s
        ):
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed microstructure receipt is not the complete source window"
            )
        rows = tuple(
            (
                row.price,
                row.size,
                row.bid,
                row.ask,
                row.event.clocks.provider_event_at,
            )
            for row in expected
        )
        if any(row[4] is None for row in rows):
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed microstructure source lacks exact event time"
            )
        try:
            result = LiveMicrostructureCaptureBridge._compute_print_result(
                operation,
                rows,
                decision_at=query.decision_at,
                parameters=query.parameters,
            )
        except (CaptureContractError, KeyError, TypeError, ValueError) as exc:
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed microstructure result could not be reproduced"
            ) from exc
        self._active_query_read_cursor = cursor + 1
        return result

    def scanner_snapshot_provider(
        self,
        ticker: str,
        *,
        include_otc: bool,
        max_age_seconds: float,
        profile_id: str,
        asset_class: str,
        price_min: float | None,
        price_max: float | None,
        min_dollar_volume: float | None,
        min_change_pct: float | None,
    ) -> Mapping[str, Any]:
        """Consume one exact Massive scanner projection from its read receipt."""

        read, fact = self._consume_decision_query(
            CaptureStream.SCANNER_SNAPSHOT
        )
        if not isinstance(fact, _SealedScannerSnapshotInput):
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 scanner receipt selected a non-scanner fact"
            )
        query = fact.snapshot.query
        supplied_profile = {
            "profile_id": str(profile_id or "").strip(),
            "asset_class": str(asset_class or "").strip(),
            "price_min": price_min,
            "price_max": price_max,
            "min_dollar_volume": min_dollar_volume,
            "min_change_pct": min_change_pct,
            "snapshot_max_age_seconds": max_age_seconds,
        }
        if (
            str(ticker or "").strip().upper() != query.symbol
            or type(include_otc) is not bool
            or include_otc != query.include_otc
            or supplied_profile != query.profile.to_dict()
            or read.receipt.query_sha256 != query.query_sha256
            or fact.event.query_sha256 != query.query_sha256
        ):
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 scanner provider call differs from its exact receipt"
            )
        row = fact.snapshot_row
        return {
            "ticker": row["ticker"],
            "todaysChangePerc": row["todaysChangePerc"],
            "lastTrade": dict(row["lastTrade"]),
            "day": dict(row["day"]),
            "min": dict(row["min"]),
        }

    @property
    def ready_for_fsm(self) -> bool:
        required = self._request.required_streams
        readiness = {
            CaptureStream.NBBO_QUOTE: bool(self._current_nbbo),
            CaptureStream.PROVIDER_OHLCV: bool(self._current_ohlcv),
            CaptureStream.ADMISSION_ELIGIBILITY: bool(
                self._current_eligibility
            ),
            CaptureStream.ACCOUNT_RISK_SNAPSHOT: (
                self._current_account_risk is not None
            ),
            CaptureStream.SCANNER_SNAPSHOT: bool(
                self._current_scanner_snapshot
            ),
            # Broker lifecycle is an execution response/output stream and is
            # not a prerequisite for the first decision tick.
            CaptureStream.BROKER_ORDER_LIFECYCLE: True,
        }
        return all(readiness.get(stream, False) for stream in required)

    def mark_broker_lifecycle_replayed(self) -> None:
        expected = {
            value.event.event_sha256
            for value in self._canonical_broker_inputs(released_only=False)
        }
        released = {
            value.event.event_sha256
            for value in self._canonical_broker_inputs(released_only=True)
        }
        if (
            released != expected
            or not self._terminal_drain_complete
        ):
            raise SealedReplayInputError(
                "broker lifecycle was not fully released through the exact window"
            )
        self._proof = replace(self._proof, broker_lifecycle_replayed=True)

    def advance_to(self, available_at: datetime) -> SealedReplayInputRelease:
        """Release all exact facts available at or before ``available_at``."""

        normalized = (
            available_at.astimezone(timezone.utc)
            if isinstance(available_at, datetime) and available_at.tzinfo is not None
            else available_at
        )
        for decision_tick in self._decision_ticks:
            row = decision_tick.checkpoint
            if normalized == row.decision_at and any(
                event.clocks.available_at <= row.decision_at
                and event.sequence > row.input_prefix_sequence
                for event in self._release_order
            ):
                raise SealedReplayInputError(
                    "decision-time advance requires the exact checkpoint sequence frontier"
                )
        return self.advance_to_frontier(available_at, sequence_at_most=None)

    def advance_to_frontier(
        self,
        available_at: datetime,
        *,
        sequence_at_most: int | None,
    ) -> SealedReplayInputRelease:
        """Release through an exact ``(available_at, sequence)`` frontier."""

        if not isinstance(available_at, datetime) or available_at.tzinfo is None:
            raise SealedReplayInputError(
                "sealed ReplayV3 advance boundary must be timezone-aware"
            )
        boundary = available_at.astimezone(timezone.utc)
        if boundary < self._request.warmup_start_at:
            raise SealedReplayInputError(
                "sealed ReplayV3 cannot advance before the exact coverage window"
            )
        if boundary > self._request.exit_end_at:
            raise SealedReplayInputError(
                "sealed ReplayV3 cannot advance beyond the exact coverage window"
            )
        if sequence_at_most is not None and (
            isinstance(sequence_at_most, bool) or int(sequence_at_most) <= 0
        ):
            raise SealedReplayInputError(
                "sealed ReplayV3 sequence frontier must be positive"
            )
        sequence_frontier = (
            None if sequence_at_most is None else int(sequence_at_most)
        )
        if (
            sequence_frontier is not None
            and self._max_released_sequence > sequence_frontier
        ):
            raise SealedReplayInputError(
                "sealed ReplayV3 exact prefix was already crossed"
            )
        if self._advanced_frontier is not None:
            prior_at, prior_sequence = self._advanced_frontier
            if boundary < prior_at or (
                boundary == prior_at
                and prior_sequence is None
                and sequence_frontier is not None
            ) or (
                boundary == prior_at
                and prior_sequence is not None
                and sequence_frontier is not None
                and sequence_frontier < prior_sequence
            ):
                raise SealedReplayInputError(
                    "sealed ReplayV3 frontier cannot move backwards"
                )
        # Admit every fact whose availability clock has elapsed into a pending
        # causal set.  A decision frontier then selects the exact global capture
        # prefix from that set; the sequence fence is not limited to same-clock
        # ties.  Facts outside the prefix stay pending for a later checkpoint or
        # the terminal unrestricted drain.
        while self._release_cursor < len(self._release_order):
            event = self._release_order[self._release_cursor]
            if event.clocks.available_at > boundary:
                break
            self._pending_release.append(event)
            self._release_cursor += 1
        if sequence_frontier is None:
            released_rows = self._pending_release
            self._pending_release = []
        else:
            released_rows = [
                event
                for event in self._pending_release
                if event.sequence <= sequence_frontier
            ]
            self._pending_release = [
                event
                for event in self._pending_release
                if event.sequence > sequence_frontier
            ]
        released = tuple(released_rows)
        for event in released:
            self._released_event_sha256s.add(event.event_sha256)
            parsed = self._typed_by_hash[event.event_sha256]
            if isinstance(parsed, _SealedNbboInput):
                assert event.symbol is not None
                self._current_nbbo[event.symbol] = parsed
            elif isinstance(parsed, _SealedIqfeedPrintInput):
                # Continuous prints are selected through the exact decision
                # receipt, not a mutable latest-value cache.  Retain a private,
                # non-serializable release object as the only future input seam
                # for counterfactual FIFO fill allocation; reproduction mode
                # never feeds it into the recorded-lifecycle broker.
                normalized = CaptureIqfeedPrint.from_event(parsed.event)
                provider_event_at = parsed.event.clocks.provider_event_at
                assert provider_event_at is not None
                self._released_counterfactual_exact_prints.append(
                    _mint_verified_exact_print(
                        event_sha256=parsed.event.event_sha256,
                        sequence=parsed.event.sequence,
                        release_ordinal=(
                            len(self._released_counterfactual_exact_prints) + 1
                        ),
                        capture_identity_sha256=self._proof.identity_sha256,
                        final_capture_seal_sha256=(
                            self._proof.final_capture_seal_sha256
                        ),
                        release_order_root_sha256=(
                            self._proof.release_order_root_sha256
                        ),
                        product_id=str(parsed.event.symbol or ""),
                        provider_event_at=provider_event_at,
                        received_at=parsed.event.clocks.received_at,
                        available_at=parsed.event.clocks.available_at,
                        price=normalized.price,
                        size=normalized.size,
                        bid=normalized.bid,
                        ask=normalized.ask,
                        conditions=normalized.conditions,
                    )
                )
            elif isinstance(parsed, _SealedOhlcvInput):
                self._current_ohlcv[parsed.call_key] = parsed
            elif isinstance(parsed, _SealedEligibilityInput):
                assert event.symbol is not None
                self._current_eligibility[event.symbol] = parsed
            elif isinstance(parsed, _SealedAccountRiskInput):
                self._current_account_risk = parsed
            elif isinstance(parsed, _SealedScannerSnapshotInput):
                assert event.symbol is not None
                self._current_scanner_snapshot[event.symbol] = parsed
            elif isinstance(parsed, _SealedBrokerLifecycleInput):
                self._released_broker_lifecycle.append(parsed)
            else:  # pragma: no cover - constructor owns the closed union
                raise AssertionError("unknown sealed ReplayV3 parsed fact")
        self._advanced_to = boundary
        self._advanced_frontier = (boundary, sequence_frontier)
        if released:
            self._max_released_sequence = max(
                self._max_released_sequence,
                max(event.sequence for event in released),
            )
        if boundary == self._request.exit_end_at and sequence_frontier is None:
            if (
                self._release_cursor != len(self._release_order)
                or self._pending_release
            ):
                raise SealedReplayInputError(
                    "sealed ReplayV3 terminal drain did not exhaust its inputs"
                )
            self._terminal_drain_complete = True
        return SealedReplayInputRelease(
            available_at=boundary,
            event_sha256s=tuple(event.event_sha256 for event in released),
            streams=tuple(event.stream for event in released),
        )

    def current_quote(self, symbol: str | None = None) -> RecordedQuote:
        normalized = str(symbol or self._request.symbol or "").strip().upper()
        if self._active_decision_tick is not None:
            selected = self._receipt_selected_scalar_fact(CaptureStream.NBBO_QUOTE)
            fact = selected if isinstance(selected, _SealedNbboInput) else None
        else:
            fact = self._current_nbbo.get(normalized)
        if fact is None:
            raise SealedReplayInputError(
                "no captured NBBO is released; network/provider fallback is forbidden"
            )
        return fact.quote

    def ohlcv_provider(
        self,
        ticker: str,
        *,
        interval: str = "1d",
        period: str = "6mo",
    ) -> pd.DataFrame:
        """Replay the exact captured result for the live-runner provider call."""

        key = (
            str(ticker or "").strip().upper(),
            str(interval or "").strip(),
            str(period or "").strip(),
        )
        read, fact = self._consume_decision_query(
            CaptureStream.PROVIDER_OHLCV
        )
        if (
            not isinstance(fact, _SealedOhlcvInput)
            or fact.call_key != key
            or read.receipt.query_sha256 != fact.event.query_sha256
        ):
            self._rejected_provider_requests += 1
            raise SealedReplayInputError(
                "sealed ReplayV3 OHLCV provider call differs from its exact receipt"
            )
        return fact.to_frame()

    def current_eligibility(self, symbol: str | None = None) -> tuple[bool, datetime]:
        normalized = str(symbol or self._request.symbol or "").strip().upper()
        if self._active_decision_tick is not None:
            selected = self._receipt_selected_scalar_fact(
                CaptureStream.ADMISSION_ELIGIBILITY
            )
            fact = selected if isinstance(selected, _SealedEligibilityInput) else None
        else:
            fact = self._current_eligibility.get(normalized)
        if fact is None:
            raise SealedReplayInputError(
                "no captured admission/eligibility fact is released; fallback is forbidden"
            )
        return fact.eligible, fact.freshness_at

    def current_account_risk(self) -> Mapping[str, Any]:
        fact = self._current_account_risk
        if fact is None:
            raise SealedReplayInputError(
                "no captured account-risk fact is released; fallback is forbidden"
            )
        return fact.event.payload

    def released_broker_lifecycle(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(fact.event.payload for fact in self._released_broker_lifecycle)


# ── the recorded-OHLCV provider (the fetch_ohlcv_df replacement) ─────────────────
class RecordedOhlcvProvider:
    """Serve OHLCV bars from RECORDED data (a per-interval frame) instead of the network.

    Installed on ``live_runner``'s ``_REPLAY_OHLCV_PROVIDER`` seam for the run. The runner
    calls it as ``provider(ticker, interval=…, period=…)`` — the exact ``fetch_ohlcv_df``
    signature. Bars are keyed by ``interval`` (15m/5m/1m); an unknown interval is
    unavailable and returns an empty frame. It must never substitute a different cadence,
    including in diagnostic mode. Datetime-indexed or clock-column frames are sliced against
    the replay clock on every call. The current, incomplete interval is excluded
    so its eventual full-day high/low/close/volume cannot leak into a decision."""

    def __init__(
        self,
        frames_by_interval: dict[str, pd.DataFrame],
        *,
        clock: Callable[[], datetime] | None = None,
        certification_mode: bool = False,
    ) -> None:
        self._frames = {str(k): v for k, v in frames_by_interval.items()}
        self._clock = clock or lr._utcnow
        self._certification_mode = bool(certification_mode)
        self.call_log: list[tuple[str, str, str]] = []
        self.rejection_log: list[tuple[str, str]] = []

    @staticmethod
    def _interval_seconds(interval: str) -> float:
        raw = str(interval or "").strip().lower()
        units = (("min", 60.0), ("m", 60.0), ("h", 3600.0), ("d", 86400.0))
        for suffix, multiplier in units:
            if not raw.endswith(suffix):
                continue
            number = raw[: -len(suffix)] or "1"
            try:
                return max(0.0, float(number) * multiplier)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    @staticmethod
    def _utc_timestamp(value: datetime) -> pd.Timestamp:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            return ts.tz_localize("UTC")
        return ts.tz_convert("UTC")

    def _slice_as_of(
        self,
        df: pd.DataFrame,
        *,
        interval: str,
        as_of: datetime,
    ) -> pd.DataFrame:
        if df.empty:
            return df.copy()
        cutoff = self._utc_timestamp(as_of) - pd.Timedelta(
            seconds=self._interval_seconds(interval)
        )
        if isinstance(df.index, pd.DatetimeIndex):
            index_utc = pd.to_datetime(df.index, utc=True, errors="coerce")
            return df.loc[index_utc <= cutoff].copy()
        for column in ("ts", "timestamp", "observed_at", "Datetime", "datetime", "date"):
            if column not in df.columns:
                continue
            values = pd.to_datetime(df[column], utc=True, errors="coerce")
            return df.loc[values <= cutoff].copy()
        # Synthetic/unit frames with no clock axis retain their historical
        # behavior outside certification.  Certification fails closed because
        # there is no way to prove which rows existed at ``as_of``.
        if self._certification_mode:
            self.rejection_log.append((str(interval), "clock_axis_missing"))
            return df.iloc[0:0].copy()
        return df.copy()

    def __call__(
        self, ticker: str, *, interval: str = "1d", period: str = "6mo"
    ) -> pd.DataFrame:
        self.call_log.append((str(ticker), str(interval), str(period)))
        requested_interval = str(interval)
        df = self._frames.get(requested_interval)
        if df is None:
            self.rejection_log.append((requested_interval, "interval_missing"))
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume"]
            )
        return self._slice_as_of(
            df,
            interval=requested_interval,
            as_of=self._clock(),
        )


def synthetic_uptrend_ohlcv(
    *, n: int = 48, start_close: float = 10.0, step: float = 0.05, surge_mult: float = 3.0
) -> pd.DataFrame:
    """A clean rising OHLCV frame whose LAST bar carries a volume surge — passes the shared
    momentum/volume entry confirmation (``entry_gates.momentum_volume_confirmation`` and the
    pullback-break fallback). Deterministic; no RNG. Used to seed a synthetic recorded day so
    the e2e test does not depend on prod ``chili`` data."""
    closes = [start_close + i * step for i in range(n)]
    base_vol = 1000.0
    vols = [base_vol for _ in range(n - 1)] + [base_vol * surge_mult]
    return pd.DataFrame(
        {
            "Open": [c - step * 0.4 for c in closes],
            "High": [c + step * 0.6 for c in closes],
            "Low": [c - step * 0.6 for c in closes],
            "Close": closes,
            "Volume": vols,
        }
    )


# ── seeding (the recorded arm → a queued_live session in the replay DB) ──────────
def _ensure_user(db: Session, *, name: Optional[str] = None) -> int:
    # ``users.name`` is UNIQUE — make the replay user name collision-proof across runs.
    uname = name or f"ReplayV3_{uuid.uuid4().hex[:10]}"
    u = User(name=uname)
    db.add(u)
    db.flush()
    return int(u.id)


def _ensure_variant(
    db: Session, *, execution_family: str = "robinhood_spot"
) -> MomentumStrategyVariant:
    """A minimal impulse_breakout variant (the family the params normalize against).

    ``(family, variant_key, version)`` is UNIQUE — use a per-call ``variant_key`` so repeated
    seeds (and a non-truncated DB) never collide."""
    v = MomentumStrategyVariant(
        family="impulse_breakout",
        variant_key=f"replay_v3_{uuid.uuid4().hex[:8]}",
        version=1,
        label="Replay v3 impulse_breakout",
        params_json={},
        is_active=True,
        execution_family=execution_family,
    )
    db.add(v)
    db.flush()
    return v


def _recorded_naive_utc(value: str, field_name: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def seed_replay_session(
    db: Session,
    arm: RecordedArm,
    *,
    execution_family: str = "robinhood_spot",
    state: str = STATE_QUEUED_LIVE,
) -> ReplaySeed:
    """Seed ONE ``queued_live`` (or ``armed``) live momentum session from a recorded arm.

    Writes: a user, an impulse_breakout variant, a ``MomentumSymbolViability`` row
    (``live_eligible=True``, fresh ``freshness_ts``, ``regime_snapshot_json`` carrying the
    ATR so the stop sizes), and a ``TradingAutomationSession`` whose ``risk_snapshot_json``
    carries the frozen risk gate, the live-execution block, and the
    ``live_eligible_at_utc`` recency-grace anchor. A certifiable economic seed
    additionally requires ``RecordedArm.economic_seed_evidence``; otherwise the
    session is explicitly diagnostic and inherits the caller's/configured legacy
    risk path without inventing ReplayV3-only dollar constants. Self-contained —
    no prod data."""
    recorded_freshness_at = _recorded_naive_utc(
        arm.live_eligible_at_utc, "live_eligible_at_utc"
    )
    normalized_family = normalize_execution_family(execution_family)
    if normalized_family not in {"alpaca_spot", "alpaca_short"}:
        account_identity = str(arm.account_identity or "").strip()
        if not account_identity:
            raise ValueError("replay non-Alpaca account identity is required")
    if arm.user_id is not None:
        existing_user = db.get(User, int(arm.user_id))
        if existing_user is None:
            raise ValueError("recorded replay user_id does not exist")
    existing_variant: MomentumStrategyVariant | None = None
    if arm.variant_id is not None:
        existing_variant = db.get(MomentumStrategyVariant, int(arm.variant_id))
        if existing_variant is None:
            raise ValueError("recorded replay variant_id does not exist")
        if normalize_execution_family(existing_variant.execution_family) != normalized_family:
            raise ValueError("recorded replay variant execution family mismatch")
    resolution: ResolvedAdaptiveRisk | None = None
    adaptive_packet: dict[str, Any] | None = None
    adaptive_available_at: datetime | None = None
    coverage_grade: CaptureCoverageGrade | None = None
    if arm.economic_seed_evidence is not None:
        # Validate before the first ORM write/flush so malformed certification
        # evidence cannot leave a partially seeded fixture in the transaction.
        (
            resolution,
            adaptive_packet,
            coverage_grade,
        ) = arm.economic_seed_evidence.validate_for(
            arm.symbol,
            execution_family=normalized_family,
        )
        adaptive_available_at = arm.economic_seed_evidence.decision_available_at
        adaptive_available_at = adaptive_available_at.astimezone(timezone.utc)

    uid = int(arm.user_id) if arm.user_id is not None else _ensure_user(db)
    if existing_variant is None:
        existing_variant = _ensure_variant(
            db, execution_family=normalized_family
        )
    vid = int(existing_variant.id)

    # Viability: live-eligible + fresh as-of the seed (the recency-grace happy path; the
    # P2 eligibility-replayer flips this per-tick to reproduce the flicker).
    via = MomentumSymbolViability(
        symbol=arm.symbol,
        scope="symbol",
        variant_id=vid,
        viability_score=float(arm.viability_score),
        paper_eligible=True,
        live_eligible=True,
        freshness_ts=recorded_freshness_at,
        regime_snapshot_json={"atr_pct": float(arm.atr_pct), "meta": {"atr_pct": float(arm.atr_pct)}},
        execution_readiness_json={"spread_bps": 8.0},
        explain_json={},
        evidence_window_json={},
    )
    db.add(via)
    db.flush()

    risk_snapshot: dict[str, Any] = {
        lr.RISK_SNAPSHOT_KEY: {"allowed": True, "evaluated_at_utc": arm.live_eligible_at_utc},
        # The recency-grace anchor (confirm_live_arm stamps this top-level). REQUIRED so the
        # grace is exercisable later (P2); present-but-unused on the P1 happy path.
        "live_eligible_at_utc": arm.live_eligible_at_utc,
        "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
        lr.KEY_LIVE_EXEC: {"tick_count": 0},
    }
    economic_seed_mode = "legacy_config_diagnostic"
    adaptive_packet_sha256: str | None = None
    if resolution is None or adaptive_packet is None:
        # Legacy P1/P2 fixtures remain useful for FSM diagnostics, but their
        # configured/default risk economics cannot receive replay/paper parity or
        # performance-certification credit.
        risk_snapshot[REPLAY_ECONOMIC_SEED_KEY] = {
            "mode": economic_seed_mode,
            "economic_seed_certifiable": False,
            "reason": "recorded_adaptive_risk_evidence_missing",
        }
        risk_snapshot["momentum_policy_caps"] = dict(
            LEGACY_DIAGNOSTIC_POLICY_CAPS
        )
    else:
        adaptive_packet_sha256 = resolution.decision_packet_sha256
        economic_seed_mode = "recorded_adaptive_risk_pending"
        assert adaptive_available_at is not None and coverage_grade is not None
        assert arm.economic_seed_evidence is not None
        seal_binding = (
            arm.economic_seed_evidence.coverage_manifest.seal_binding
        )
        if seal_binding is None:
            raise ValueError(
                "ReplayV3 economic seed is not certifiable: "
                "sealed_capture_binding_missing"
            )
        # Keep the verified packet under a PENDING key. Translating it into the
        # legacy caps would double-apply the old sizing stack and, on Alpaca,
        # permit the fixed-dollar clamps to override it. A future dual-clock
        # release plus direct shared replay/paper consumer must activate the exact
        # quantity/R/notional; until then this seed remains non-certifying.
        risk_snapshot[PENDING_ADAPTIVE_RISK_DECISION_KEY] = {
            "available_at": adaptive_available_at.isoformat().replace("+00:00", "Z"),
            "decision_packet": adaptive_packet,
        }
        risk_snapshot[REPLAY_ECONOMIC_SEED_KEY] = {
            "mode": economic_seed_mode,
            "adaptive_packet_recomputed": True,
            "adaptive_request_builder_diagnostic_parity": True,
            "economic_seed_certifiable": False,
            "reason": "adaptive_risk_alpaca_lifecycle_not_migrated",
            "post_exit_capture_manifest_sha256": coverage_grade.manifest_sha256,
            "final_capture_seal_sha256": seal_binding.final_seal_sha256,
            "capture_seal_content_root_sha256": (
                seal_binding.seal_content_root_sha256
            ),
            "capture_close_proof_sha256": seal_binding.close_proof_sha256,
            "capture_prefix_root_sha256": resolution.input_snapshot.get(
                "capture_prefix_root_sha256"
            ),
            "adaptive_risk_decision_sha256": adaptive_packet_sha256,
            "economic_resolution_sha256": resolution.economic_resolution_sha256,
            "quantity_shares": int(resolution.quantity_shares),
            "planned_structural_risk_usd": float(
                resolution.planned_structural_risk_usd
            ),
            "planned_notional_usd": float(resolution.planned_notional_usd),
        }
    if normalized_family not in {
        "alpaca_spot",
        "alpaca_short",
    }:
        risk_snapshot[NON_ALPACA_ACCOUNT_IDENTITY_KEY] = account_identity
    correlation_id = "replay-v3-diagnostic"
    if resolution is not None:
        correlation_id = str(
            resolution.input_snapshot.get("decision_id") or ""
        ).strip()
        if not correlation_id:
            raise ValueError("recorded adaptive risk decision_id is required")
    sess = TradingAutomationSession(
        user_id=uid,
        venue=venue_for_execution_family(normalized_family),
        execution_family=normalized_family,
        mode="live",
        symbol=arm.symbol,
        variant_id=vid,
        state=state,
        risk_snapshot_json=risk_snapshot,
        correlation_id=correlation_id,
    )
    db.add(sess)
    db.flush()
    return ReplaySeed(
        session_id=int(sess.id),
        symbol=arm.symbol,
        variant_id=vid,
        user_id=uid,
        economic_seed_mode=economic_seed_mode,
        adaptive_risk_decision_sha256=adaptive_packet_sha256,
        adaptive_risk_available_at=adaptive_available_at,
    )


def seed_replay_position(
    db: Session,
    seed: ReplaySeed,
    *,
    entry_price: float,
    stop_price: float,
    target_price: float,
    high_water_mark: float,
    quantity: float,
    opened_at: datetime,
    state: str = STATE_LIVE_ENTERED,
) -> ReplaySeed:
    """Seed a truthful long position directly into the replay-only live FSM.

    This is the narrow exit-validation seam: it mutates only the already-seeded
    replay session row and never constructs an adapter, places an entry order, or
    emits a synthetic broker fill.  The next replay grid tick therefore enters the
    unchanged production exit manager in ``live_entered`` or ``live_trailing`` with
    the caller's exact economic inputs.

    The helper deliberately refuses to overwrite an existing position or any
    in-flight order ownership.  That keeps it suitable for fresh replay fixtures,
    not as a way to rewrite a partially executed session.
    """

    if state not in {STATE_LIVE_ENTERED, STATE_LIVE_TRAILING}:
        raise ValueError("replay position state must be live_entered or live_trailing")
    if not isinstance(opened_at, datetime):
        raise TypeError("opened_at must be a datetime")

    values = {
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "high_water_mark": high_water_mark,
        "quantity": quantity,
    }
    parsed: dict[str, float] = {}
    for name, raw in values.items():
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a finite positive number") from exc
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be a finite positive number")
        parsed[name] = value

    if parsed["high_water_mark"] < parsed["entry_price"]:
        raise ValueError("high_water_mark must be at or above entry_price for a long")
    if parsed["stop_price"] >= parsed["high_water_mark"]:
        raise ValueError("stop_price must be below high_water_mark for a long")
    if parsed["target_price"] <= parsed["entry_price"]:
        raise ValueError("target_price must be above entry_price for a long")
    if state == STATE_LIVE_ENTERED and parsed["stop_price"] >= parsed["entry_price"]:
        raise ValueError("an entered long must have its stop below entry_price")

    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(seed.session_id))
        .one_or_none()
    )
    if sess is None:
        raise ValueError("replay session not found")
    if not (
        str(sess.mode) == "live"
        and str(sess.symbol) == str(seed.symbol)
        and int(sess.variant_id) == int(seed.variant_id)
        and int(sess.user_id) == int(seed.user_id)
    ):
        raise ValueError("replay seed does not match the persisted session")
    snap = dict(sess.risk_snapshot_json or {})
    le = dict(snap.get(lr.KEY_LIVE_EXEC) or {})
    unsafe_keys = {
        "entry_order_id",
        "entry_client_order_id",
        "exit_order_id",
        "exit_client_order_id",
        "pending_exit_reason",
        "scale_limit_order_id",
    }
    if isinstance(le.get("position"), dict) or any(le.get(key) for key in unsafe_keys):
        raise ValueError("replay session already owns a position or in-flight order")
    if sess.state not in {STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE}:
        raise ValueError("replay position can only replace a fresh seeded session")

    opened_utc = opened_at
    if opened_utc.tzinfo is not None:
        opened_utc = opened_utc.astimezone(timezone.utc).replace(tzinfo=None)
    le["position"] = {
        "product_id": str(seed.symbol).upper().strip(),
        "side": "long",
        "quantity": parsed["quantity"],
        "original_quantity": parsed["quantity"],
        "avg_entry_price": parsed["entry_price"],
        "notional_usd": parsed["quantity"] * parsed["entry_price"],
        "opened_at_utc": opened_utc.isoformat(),
        "high_water_mark": parsed["high_water_mark"],
        "stop_price": parsed["stop_price"],
        "target_price": parsed["target_price"],
    }
    snap[lr.KEY_LIVE_EXEC] = le
    sess.risk_snapshot_json = snap
    sess.state = state
    db.flush()
    return seed


# ── the event grid ───────────────────────────────────────────────────────────────
def build_event_grid(
    nbbo: list[RecordedNbboTick], *, step_seconds: float = 0.0
) -> list[RecordedNbboTick]:
    """Build the ordered time/event grid the driver steps. With ``step_seconds<=0`` the grid
    is the recorded NBBO ticks themselves (true tick granularity — a sub-minute flicker is
    hit). With a coarse step it down-samples to one tick per ``step_seconds`` bucket (the
    existing tick-cadence option). Always sorted by ``ts``."""
    ticks = sorted(nbbo, key=lambda t: t.ts)
    if step_seconds <= 0 or not ticks:
        return ticks
    out: list[RecordedNbboTick] = []
    next_at: Optional[datetime] = None
    for t in ticks:
        if next_at is None or t.ts >= next_at:
            out.append(t)
            next_at = t.ts + timedelta(seconds=float(step_seconds))
    return out


# ── the driver ───────────────────────────────────────────────────────────────────
class ReplayV3Driver:
    """Step the REAL ``tick_live_session`` across an event grid with a mock broker + sim clock.

    Per grid step (in order, mirroring docs §2.2):
      1. ``eligibility.apply(db, t)`` — write ``live_eligible`` + ``freshness_ts`` AS-OF t (P2;
         reproduces the flicker) so the unchanged viability read + the real gate see the
         as-of-t state.
      2. ``mock.set_clock(t)`` + ``mock.set_quote(symbol, quote@t)`` — broker BBO/fill as-of t.
      3. ``replay_clock(t)`` — freeze the runner's ``_utcnow()`` chokepoint at t.
      4. ``replay_ohlcv_provider(provider)`` + ``replay_account_equity(equity_provider)`` —
         serve recorded bars + a recorded equity basis for the in-tick reads (P2 equity seam).
      5. ``tick_live_session(db, sid, adapter_factory=make_mock_broker_factory(mock))``.

    The FSM advances itself; the driver only records the per-tick state transition + result.

    ``risk_gate_allows`` controls the entry-instant risk path:
      * ``None`` (P2 default when an ``eligibility`` replayer is supplied): run the GENUINE
        ``runner_boundary_risk_ok`` → ``evaluate_proposed_momentum_automation`` gate — the
        live_eligible check + the recency-grace are EXERCISED (the whole point of P2).
      * ``True`` (P1 back-compat): short-circuit the gate to always-allow (its full DB-seeded
        eval was out of P1 scope). Only this ONE pre-entry gate is short-circuited; the FSM
        transitions always run the real code.
    """

    def __init__(
        self,
        db: Session,
        seed: ReplaySeed,
        *,
        mock: MockBrokerAdapter,
        ohlcv_provider: Optional[Callable[..., Any]] = None,
        grid: Optional[list[RecordedNbboTick]] = None,
        risk_gate_allows: Optional[bool] = None,
        eligibility: Optional[EligibilityReplayer] = None,
        equity_provider: Optional[Callable[..., Optional[float]]] = None,
        scanner_snapshot_provider: Optional[Callable[..., Mapping[str, Any]]] = None,
        sealed_inputs: Optional[SealedReplayV3InputAdapter] = None,
    ) -> None:
        self.db = db
        self.seed = seed
        self.mock = mock
        self.sealed_inputs = sealed_inputs
        self._decision_runtime_state = lr.DecisionRuntimeState(
            clock_domain="replay_utc"
        )
        self._macro_feature_cache: dict[str, Any] = {}
        if sealed_inputs is not None:
            if type(sealed_inputs) is not SealedReplayV3InputAdapter:
                raise SealedReplayInputError(
                    "sealed driver requires an exact SealedReplayV3InputAdapter"
                )
            if (
                ohlcv_provider is not None
                or grid
                or eligibility is not None
                or equity_provider is not None
                or scanner_snapshot_provider is not None
                or risk_gate_allows is not None
            ):
                raise SealedReplayInputError(
                    "sealed driver forbids legacy provider/grid/gate overrides"
                )
            if mock.freshness_mode != "sim":
                raise SealedReplayInputError(
                    "sealed driver forbids wall-clock broker freshness"
                )
            sealed_inputs.configure_recorded_broker(mock)
            self.ohlcv_provider = sealed_inputs.ohlcv_provider
            self.grid = []
            self.risk_gate_allows = None
            self.eligibility = None
            self.equity_provider = sealed_inputs.account_equity_provider
            self.scanner_snapshot_provider = sealed_inputs.scanner_snapshot_provider
            self.microstructure_provider = sealed_inputs
        else:
            if ohlcv_provider is None:
                raise ValueError("diagnostic ReplayV3 requires an OHLCV provider")
            self.ohlcv_provider = ohlcv_provider
            self.grid = list(grid or [])
            self.scanner_snapshot_provider = scanner_snapshot_provider
            self.microstructure_provider = None
        # P2: when an eligibility replayer is supplied and the caller didn't force the
        # short-circuit, run the REAL gate (risk_gate_allows stays None). P1 callers pass
        # risk_gate_allows=True explicitly to keep the short-circuit.
        if sealed_inputs is None and risk_gate_allows is None and eligibility is None:
            # No eligibility replayer + unspecified gate ⇒ preserve the P1 default (allow).
            risk_gate_allows = True
        if sealed_inputs is None:
            self.risk_gate_allows = risk_gate_allows
            self.eligibility = eligibility
            self.equity_provider = equity_provider
        self._factory = make_mock_broker_factory(mock)
        self._released_recorded_lifecycle_count = 0
        self._python_network_attempt_count = 0
        self._sealed_run_active = False
        self._run_network_guard_active = False
        self._reproduced_decision_output_sha256s: list[str] = []
        self._broker_lifecycle_architectural_blockers: list[str] = []

    @classmethod
    def from_sealed_inputs(
        cls,
        db: Session,
        seed: ReplaySeed,
        *,
        mock: MockBrokerAdapter,
        sealed_inputs: SealedReplayV3InputAdapter,
    ) -> "ReplayV3Driver":
        """The only certification-capable ReplayV3 constructor."""

        return cls(
            db,
            seed,
            mock=mock,
            sealed_inputs=sealed_inputs,
        )

    def _session(self) -> Optional[TradingAutomationSession]:
        return (
            self.db.query(TradingAutomationSession)
            .filter(TradingAutomationSession.id == self.seed.session_id)
            .one_or_none()
        )

    def _state(self) -> str:
        s = self._session()
        return str(s.state) if s is not None else "<gone>"

    def _database_guard_endpoints(self) -> tuple[tuple[str, int], ...]:
        """Resolve only the already-configured replay DB before fencing sockets."""

        bind = self.db.get_bind()
        url = getattr(bind, "url", None)
        host = str(getattr(url, "host", "") or "").strip().lower()
        if not host:
            return ()
        port = int(getattr(url, "port", None) or 5432)
        endpoints: set[tuple[str, int]] = {(host, port)}
        for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            endpoints.add((str(row[4][0]).strip().lower(), port))
        return tuple(sorted(endpoints))

    @property
    def python_network_attempt_count(self) -> int:
        return self._python_network_attempt_count

    def _assert_diagnostic_runtime_input_capabilities(self) -> None:
        """Fail before diagnostic ORM mutation when the real gate needs scanner truth."""

        if (
            getattr(self, "sealed_inputs", None) is not None
            or getattr(self, "scanner_snapshot_provider", None) is not None
            or getattr(self, "risk_gate_allows", True) is True
        ):
            return
        with self.db.no_autoflush:
            session = self._session()
        if session is None:
            return
        from .risk_evaluator import _ross_lane_universe_required

        if _ross_lane_universe_required(
            mode=str(session.mode or ""),
            execution_family=str(session.execution_family or ""),
            symbol=str(session.symbol or ""),
        ):
            raise ReplayScannerSnapshotUnavailableError(
                "replay scanner_snapshot input is unavailable: "
                "recorded Ross universe snapshot not bound"
            )

    def step(self, t: datetime, quote: Optional[RecordedQuote]) -> TickTrace:
        """Run one tick using this driver's isolated warmed decision state."""

        if self.sealed_inputs is not None and not self._sealed_run_active:
            raise SealedReplayInputError(
                "sealed ReplayV3 step requires run() capability preflight"
            )
        if not self._run_network_guard_active:
            # ``run`` preflights once before activating its shared guard.  A
            # standalone direct step needs the same zero-mutation preflight.
            self._assert_diagnostic_runtime_input_capabilities()
            network_guard = ReplayNetworkGuard(
                allowed_endpoints=self._database_guard_endpoints()
            )
            try:
                with network_guard, lr.decision_runtime_state(
                    self._decision_runtime_state
                ):
                    trace = self._step_with_bound_decision_runtime_state(t, quote)
                    if network_guard.attempt_count:
                        raise ReplayNetworkAccessError(
                            "direct diagnostic ReplayV3 step swallowed a forbidden "
                            "network attempt"
                        )
                    return trace
            finally:
                self._python_network_attempt_count += network_guard.attempt_count
        with lr.decision_runtime_state(self._decision_runtime_state):
            return self._step_with_bound_decision_runtime_state(t, quote)

    def _step_with_bound_decision_runtime_state(
        self, t: datetime, quote: Optional[RecordedQuote]
    ) -> TickTrace:
        """Run ONE tick at instant ``t`` with the recorded ``quote`` (None ⇒ no_bbo)."""
        # 0) eligibility AS-OF t — write live_eligible + freshness_ts before the tick reads it
        #    (P2: this is what reproduces the flicker for the REAL gate to evaluate).
        if self.sealed_inputs is not None:
            self.sealed_inputs.apply_current_eligibility(
                self.db,
                symbol=self.seed.symbol,
                variant_id=self.seed.variant_id,
            )
        elif self.eligibility is not None:
            self.eligibility.apply(self.db, t)
        # 1) broker clock + quote
        self.mock.set_clock(t)
        if quote is None:
            self.mock.clear_quote(self.seed.symbol)
        else:
            self.mock.set_quote(self.seed.symbol, quote)
        state_before = self._state()
        # 2) sim clock + 3) recorded OHLCV provider + (P2) recorded equity, around the FSM tick.
        scanner_provider = self.scanner_snapshot_provider
        from .entry_features import macro_feature_cache
        from .pipeline import microstructure_read_provider

        with lr.replay_clock(t), _rp.replay_risk_clock(
            t
        ), lr.replay_ohlcv_provider(self.ohlcv_provider), _rp.replay_account_equity(
            self.equity_provider
        ), _risk_eval.replay_scanner_snapshot_provider(
            scanner_provider
        ), macro_feature_cache(
            self._macro_feature_cache
        ), microstructure_read_provider(self.microstructure_provider):
            result = lr.tick_live_session(
                self.db, self.seed.session_id, adapter_factory=self._factory
            )
        self.db.flush()
        state_after = self._state()
        return TickTrace(
            ts=t, state_before=state_before, state_after=state_after, result=dict(result)
        )

    def _advance_sealed_boundary(
        self,
        boundary: datetime,
        *,
        sequence_at_most: int | None,
    ) -> Optional[TickTrace]:
        adapter = self.sealed_inputs
        if adapter is None:  # pragma: no cover - private branch guard
            raise AssertionError("sealed boundary used by diagnostic driver")
        adapter.advance_to_frontier(
            boundary,
            sequence_at_most=sequence_at_most,
        )
        released = adapter.released_recorded_broker_transitions()
        new_rows = released[self._released_recorded_lifecycle_count :]
        for transition in new_rows:
            try:
                self.mock.release_recorded_transition(transition)
            except (TypeError, ValueError) as exc:
                sync_ack_violation = (
                    f"{transition.client_order_id}:"
                    f"{SEALED_REPLAY_SYNC_ACK_ARCHITECTURAL_BLOCKER}"
                )
                if (
                    "before its PLACE request" in str(exc)
                    and sync_ack_violation
                    in self.mock.recorded_request_violations
                ):
                    self._broker_lifecycle_architectural_blockers.append(
                        sync_ack_violation
                    )
                    continue
                raise SealedReplayInputError(
                    f"recorded broker lifecycle replay diverged: {exc}"
                ) from exc
        self._released_recorded_lifecycle_count = len(released)
        decision_tick = adapter.decision_tick_for_frontier(
            boundary,
            sequence_at_most,
        )
        # Input arrival is not an FSM invocation.  Release it causally and wait
        # for the exact checkpoint captured from the live loop.
        if decision_tick is None:
            return None
        if not adapter.ready_for_decision(decision_tick):
            raise SealedReplayInputError(
                "captured FSM tick reached before its exact dependencies"
            )
        bound_before = set(self.mock.recorded_bound_client_ids)
        adapter.begin_decision_read_plan(decision_tick)
        try:
            first_dip_read_id = str(
                decision_tick.checkpoint.decision_payload.get(
                    "first_dip_tape_read_id"
                )
                or ""
            ).strip()
            if first_dip_read_id:
                authority = adapter.prepare_first_dip_tape_decision_authority()
                if decision_tick.first_dip_final_frontier is not None:
                    material = adapter.prepare_first_dip_adaptive_risk_material()

                    def sealed_adaptive_source(**_boundary: Any) -> object:
                        return material

                    with adaptive_risk_source_provider(
                        sealed_adaptive_source,
                        one_shot=True,
                    ), _installed_sealed_replay_first_dip_final_authority_provider(
                        adapter.prepare_first_dip_final_tape_decision_handoff
                    ), _installed_sealed_replay_first_dip_tape_decision_authority(
                        authority
                    ):
                        trace = self.step(
                            boundary, adapter.current_quote(self.seed.symbol)
                        )
                else:
                    with _installed_sealed_replay_first_dip_tape_decision_authority(
                        authority
                    ):
                        trace = self.step(
                            boundary, adapter.current_quote(self.seed.symbol)
                        )
            else:
                trace = self.step(
                    boundary, adapter.current_quote(self.seed.symbol)
                )
            adapter.complete_decision_read_plan()
        except Exception:
            adapter.abort_decision_read_plan()
            raise
        expected = decision_tick.output
        if str(trace.state_after or "").strip().lower() != expected.fsm_state:
            raise SealedReplayInputError(
                "sealed ReplayV3 FSM state differs from captured decision output"
            )
        expected_cids = {
            intent.client_order_id for intent in expected.order_intents
        }
        bound_after = set(self.mock.recorded_bound_client_ids)
        newly_bound = bound_after - bound_before
        if expected_cids:
            if not expected_cids.issubset(bound_after) or not newly_bound.issubset(
                expected_cids
            ):
                raise SealedReplayInputError(
                    "sealed ReplayV3 order output differs from captured decision"
                )
        elif newly_bound:
            raise SealedReplayInputError(
                "sealed ReplayV3 emitted an order for a captured no-order decision"
            )
        # Do not copy the captured expected hash into the "reproduced" side.
        # The current unchanged FSM seam does not yet emit a canonical
        # CaptureDecisionOutput carrying exact action/reason/setup/economics.
        # State/CID subset checks above remain useful diagnostics, but they are
        # not output parity and therefore cannot clear the certification gate.
        return trace

    @staticmethod
    def _trace_sha256(result: ReplayResult) -> str:
        return sha256_json(
            {
                "states_visited": result.states_visited,
                "ticks": [
                    {
                        "ts": trace.ts,
                        "state_before": trace.state_before,
                        "state_after": trace.state_after,
                        "result": trace.result,
                    }
                    for trace in result.ticks
                ],
                "final_state": result.final_state,
                "entry_fill_price": result.entry_fill_price,
                "exit_fill_prices": result.exit_fill_prices,
                "events": result.events,
                "economic_seed_mode": result.economic_seed_mode,
            }
        )

    def run(self) -> ReplayResult:
        """Step the whole grid and return the end-to-end trace."""
        if self.sealed_inputs is not None:
            # This must precede adapter release, ORM mutation, broker requests,
            # network fencing, and the first call into tick_live_session().
            self.sealed_inputs.assert_runtime_input_capabilities()
        else:
            # Diagnostic replay still must not mutate eligibility/session rows and
            # only then discover that its Ross scanner decision input is absent.
            self._assert_diagnostic_runtime_input_capabilities()
        self._decision_runtime_state = lr.DecisionRuntimeState(
            clock_domain="replay_utc"
        )
        self._macro_feature_cache = {}
        self._sealed_run_active = self.sealed_inputs is not None
        try:
            with lr.decision_runtime_state(self._decision_runtime_state):
                return self._run_with_bound_decision_runtime_state()
        finally:
            self._sealed_run_active = False

    def _run_with_bound_decision_runtime_state(self) -> ReplayResult:
        """Run with one fresh state container that persists for this replay only."""

        seed_failure = (
            "adaptive_risk_alpaca_lifecycle_not_migrated"
            if self.seed.economic_seed_mode == "recorded_adaptive_risk_pending"
            else "legacy_or_missing_recorded_adaptive_risk_economics"
        )
        # ReplayV3 still executes the unchanged FSM against mutable ORM state.
        # Until that initial database/read set and authorized mutation log are
        # content-addressed, this blocker is intentionally non-removable.  The
        # OS zero-egress attestation removes only its own network blocker.
        certification_failures = [
            seed_failure,
            MUTABLE_DATABASE_CERTIFICATION_BLOCKER,
        ]
        if self.sealed_inputs is None:
            certification_failures.extend(
                [
                    "sealed_dual_clock_capture_driver_not_migrated",
                    "os_level_external_network_denial_not_proven",
                    "recorded_broker_lifecycle_not_replayed",
                ]
            )
        else:
            certification_failures.extend(
                [
                    "os_level_external_network_denial_not_proven",
                    "recorded_broker_lifecycle_not_replayed",
                    "recorded_fsm_decision_output_not_reproduced",
                    "captured_runtime_identity_not_enforced",
                    CONTINUOUS_DECISION_READS_CERTIFICATION_BLOCKER,
                    UNSEALED_CAUSAL_RUNTIME_INPUTS_CERTIFICATION_BLOCKER,
                    PROCESS_GLOBAL_STATE_CERTIFICATION_BLOCKER,
                ]
            )
        if self.risk_gate_allows is True:
            certification_failures.append("entry_risk_gate_bypassed")
        if self.sealed_inputs is None and self.eligibility is None:
            certification_failures.append("recorded_eligibility_stream_missing")
        res = ReplayResult(
            economic_seed_mode=self.seed.economic_seed_mode,
            certification_eligible=False,
            certification_failures=certification_failures,
        )
        # P1 back-compat: when risk_gate_allows is True, neutralize the full risk gate (its
        # full DB-seeded eval was out of P1 scope). P2 leaves it None ⇒ the REAL
        # runner_boundary_risk_ok → evaluate_proposed_momentum_automation runs, so the
        # live_eligible check + the recency-grace are genuinely exercised. The driver wraps the
        # REAL FSM either way — only this ONE pre-entry gate is ever short-circuited.
        _orig_gate = lr.runner_boundary_risk_ok
        if self.risk_gate_allows is True:
            lr.runner_boundary_risk_ok = lambda *a, **k: (True, {"allowed": True, "replay": True})  # type: ignore[assignment]
        network_guard = ReplayNetworkGuard(
            allowed_endpoints=self._database_guard_endpoints()
        )
        try:
            with network_guard:
                self._run_network_guard_active = True
                res.states_visited.append(self._state())
                steps: list[TickTrace] = []
                if self.sealed_inputs is not None:
                    for boundary, sequence_at_most in self.sealed_inputs.replay_frontiers:
                        trace = self._advance_sealed_boundary(
                            boundary,
                            sequence_at_most=sequence_at_most,
                        )
                        if (
                            network_guard is not None
                            and network_guard.attempt_count > 0
                        ):
                            raise ReplayNetworkAccessError(
                                "sealed ReplayV3 provider swallowed a forbidden network attempt"
                            )
                        if trace is not None:
                            steps.append(trace)
                else:
                    for tk in self.grid:
                        attempts_before = network_guard.attempt_count
                        trace = self.step(tk.ts, tk.as_quote())
                        if network_guard.attempt_count > attempts_before:
                            raise ReplayNetworkAccessError(
                                "diagnostic ReplayV3 provider swallowed a forbidden "
                                "network attempt"
                            )
                        steps.append(trace)
                for trace in steps:
                    res.ticks.append(trace)
                    if trace.state_after != res.states_visited[-1]:
                        res.states_visited.append(trace.state_after)
        except ReplayNetworkAccessError as exc:
            self._python_network_attempt_count = network_guard.attempt_count
            if self.sealed_inputs is not None:
                raise SealedReplayInputError(
                    "sealed ReplayV3 external provider/network fallback attempted"
                ) from exc
            raise
        finally:
            self._run_network_guard_active = False
            self._python_network_attempt_count = network_guard.attempt_count
            lr.runner_boundary_risk_ok = _orig_gate  # type: ignore[assignment]

        res.final_state = self._state()
        # Mine fills off the mock + the event log off the DB (the runner persisted them).
        fills, _ = self.mock.get_fills(limit=1000)
        for f in fills:
            if f.side in ("buy", "bid", "long") and res.entry_fill_price is None:
                res.entry_fill_price = float(f.price)
            elif f.side in ("sell", "ask", "short"):
                res.exit_fill_prices.append(float(f.price))
        evs = (
            self.db.query(TradingAutomationEvent)
            .filter(TradingAutomationEvent.session_id == self.seed.session_id)
            .order_by(TradingAutomationEvent.id.asc())
            .all()
        )
        res.events = [str(e.event_type) for e in evs]
        if self.sealed_inputs is not None:
            adapter = self.sealed_inputs
            try:
                adapter.mark_broker_lifecycle_replayed()
            except SealedReplayInputError:
                pass
            proof = adapter.proof
            expected_applied = {
                row.event_sha256
                for row in adapter.released_recorded_broker_transitions()
            }
            actual_applied = set(self.mock.recorded_applied_event_sha256s)
            expected_cids = {
                intent.client_order_id for intent in adapter.canonical_order_intents
            }
            actual_cids = set(self.mock.recorded_bound_client_ids)
            broker_replayed = (
                proof.broker_lifecycle_replayed
                and expected_applied == actual_applied
                and expected_cids == actual_cids
                and self.mock.recorded_cancel_request_complete
                and not self.mock.recorded_request_violations
            )
            if broker_replayed:
                res.certification_failures = [
                    reason
                    for reason in res.certification_failures
                    if reason != "recorded_broker_lifecycle_not_replayed"
                ]
            else:
                if expected_cids != actual_cids:
                    res.certification_failures.append(
                        "recorded_order_intent_not_reproduced"
                    )
                if self.mock.recorded_request_violations:
                    res.certification_failures.append(
                        "recorded_order_request_parity_failed"
                    )
            expected_output_hashes = [
                row.output.decision_output_sha256
                for row in adapter._decision_ticks
            ]
            if self._reproduced_decision_output_sha256s == expected_output_hashes:
                res.certification_failures = [
                    reason
                    for reason in res.certification_failures
                    if reason
                    != "recorded_fsm_decision_output_not_reproduced"
                ]
            if adapter.rejected_provider_request_count:
                res.certification_failures.append(
                    "sealed_provider_request_not_captured"
                )
            if self._broker_lifecycle_architectural_blockers:
                res.certification_failures.append(
                    "synchronous_broker_ack_not_causally_replayable"
                )
            if adapter.broker_events_unobserved_by_fsm:
                res.certification_failures.append(
                    "terminal_broker_fact_not_observed_by_fsm"
                )
            if adapter.continuous_decision_reads_observed_by_fsm:
                res.certification_failures = [
                    reason
                    for reason in res.certification_failures
                    if reason != CONTINUOUS_DECISION_READS_CERTIFICATION_BLOCKER
                ]
            if not adapter.terminal_drain_complete:
                res.certification_failures.append(
                    "sealed_terminal_input_drain_incomplete"
                )
            if adapter.network_attempt_count:
                res.certification_failures.append(
                    "sealed_adapter_network_attempted"
                )
            res.certification_failures = list(
                dict.fromkeys(res.certification_failures)
            )
            res.sealed_run_binding = ReplayV3RunBinding(
                identity_sha256=proof.identity_sha256,
                final_capture_seal_sha256=proof.final_capture_seal_sha256,
                manifest_sha256=proof.manifest_sha256,
                release_order_root_sha256=proof.release_order_root_sha256,
                decision_checkpoint_sha256=proof.decision_checkpoint_sha256,
                result_trace_sha256=self._trace_sha256(res),
                broker_lifecycle_root_sha256=(
                    adapter.broker_lifecycle_inventory_root_sha256
                ),
                adapter_network_attempt_count=adapter.network_attempt_count,
                python_network_attempt_count=self._python_network_attempt_count,
                adapter_rejected_provider_request_count=(
                    adapter.rejected_provider_request_count
                ),
            )
            res.sealed_execution_receipt = ReplayV3ExecutionReceipt(
                binding=res.sealed_run_binding,
                _verification_token=_REPLAY_V3_EXECUTION_RECEIPT_TOKEN,
            )
            res.certification_eligible = not res.certification_failures
        return res
