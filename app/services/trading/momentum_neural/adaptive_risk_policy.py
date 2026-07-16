"""Pure adaptive risk resolver shared by replay and broker-paper execution.

This is an offline-first policy boundary.  It does not fetch account data,
market data, configuration, or place orders.  Callers must provide one fully
timestamped/provenanced input packet.  The same packet therefore resolves to
the same economic decision in ReplayV3 and Alpaca paper.

No activation-only dollar cap or one-symbol cap exists here.  Position
concurrency emerges from remaining daily, portfolio, symbol, correlation-cluster,
liquidity, buying-power, and gross-exposure budgets after causal reservations.
Operational correctness safeguards
(account identity, freshness, ownership, reconciliation, kill switches) remain
outside this sizing function and are not strategy opportunity caps.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from functools import cached_property
import hashlib
import json
import math
import re
from typing import Any, Mapping
import uuid


UTC = timezone.utc
RISK_PACKET_SCHEMA_VERSION = "chili.adaptive-risk-decision.v2"
ADAPTIVE_RISK_POLICY_SETTINGS_SCHEMA_VERSION = (
    "chili.adaptive-risk-policy-settings.v1"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# This is the single public policy-field -> Settings-field contract shared by
# ReplayV3 and captured Alpaca PAPER.  Keeping it immutable and beside the pure
# resolver prevents either execution surface from quietly introducing its own
# dollar cap, symbol-count limit, or alternate defaults.
ADAPTIVE_RISK_POLICY_SETTING_BINDINGS: tuple[tuple[str, str], ...] = (
    (
        "policy_version",
        "chili_momentum_adaptive_risk_policy_version",
    ),
    (
        "policy_source",
        "chili_momentum_adaptive_risk_policy_source",
    ),
    (
        "risk_fraction_of_equity",
        "chili_momentum_risk_loss_fraction_of_equity",
    ),
    (
        "daily_risk_fraction_of_equity",
        "chili_momentum_risk_daily_loss_fraction_of_equity",
    ),
    (
        "portfolio_risk_fraction_of_equity",
        "chili_momentum_risk_concurrent_open_risk_fraction",
    ),
    (
        "cluster_risk_fraction_of_equity",
        "chili_momentum_adaptive_risk_cluster_fraction_of_equity",
    ),
    (
        "symbol_risk_fraction_of_equity",
        "chili_momentum_adaptive_risk_symbol_fraction_of_equity",
    ),
    (
        "daily_gap_reserve_fraction_of_equity",
        "chili_momentum_adaptive_risk_daily_gap_reserve_fraction_of_equity",
    ),
    (
        "max_notional_fraction_of_equity",
        "chili_momentum_risk_notional_fraction_of_equity",
    ),
    (
        "max_buying_power_fraction_for_notional",
        "chili_momentum_adaptive_risk_max_buying_power_fraction_for_notional",
    ),
    (
        "max_portfolio_gross_fraction_of_equity",
        "chili_momentum_adaptive_risk_max_portfolio_gross_fraction_of_equity",
    ),
    (
        "quality_multiplier_floor",
        "chili_momentum_adaptive_risk_quality_multiplier_floor",
    ),
    (
        "quality_multiplier_ceiling",
        "chili_momentum_adaptive_risk_quality_multiplier_ceiling",
    ),
    (
        "volatility_reference_fraction",
        "chili_momentum_adaptive_risk_volatility_reference_fraction",
    ),
    (
        "volatility_multiplier_floor",
        "chili_momentum_adaptive_risk_volatility_multiplier_floor",
    ),
    (
        "spread_reserve_multiple",
        "chili_momentum_adaptive_risk_spread_reserve_multiple",
    ),
    (
        "per_share_gap_reserve_volatility_multiple",
        "chili_momentum_adaptive_risk_gap_reserve_volatility_multiple",
    ),
    (
        "max_adv_participation",
        "chili_momentum_risk_liquidity_participation_fraction",
    ),
    (
        "max_recent_volume_participation",
        "chili_momentum_adaptive_risk_recent_volume_participation",
    ),
    (
        "max_executable_depth_participation",
        "chili_momentum_adaptive_risk_executable_depth_participation",
    ),
    (
        "market_data_max_age_seconds",
        "chili_momentum_adaptive_risk_market_data_max_age_seconds",
    ),
    (
        "account_data_max_age_seconds",
        "chili_momentum_adaptive_risk_account_data_max_age_seconds",
    ),
    (
        "reservation_data_max_age_seconds",
        "chili_momentum_adaptive_risk_reservation_data_max_age_seconds",
    ),
    (
        "context_data_max_age_seconds",
        "chili_momentum_adaptive_risk_context_data_max_age_seconds",
    ),
)


class AdaptiveRiskContractError(ValueError):
    pass


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AdaptiveRiskContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value, "datetime").isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, field_name)
    if not isinstance(value, str) or not value.strip():
        raise AdaptiveRiskContractError(f"{field_name} must be ISO-8601 text")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdaptiveRiskContractError(
            f"{field_name} must be ISO-8601 text"
        ) from exc
    return _utc(parsed, field_name)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _canonical_json(value: Any) -> bytes:
    try:
        raw = json.dumps(
            value,
            default=_json_default,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskContractError(f"non-canonical risk packet: {exc}") from exc
    return raw.encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _is_sha256(value: Any) -> bool:
    return _SHA256_RE.fullmatch(str(value or "").strip().lower()) is not None


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(
        float(value)
    )


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _materially_exceeds(value: float, limit: float) -> bool:
    """Deterministic USD comparison tolerant only of float aggregation dust."""

    tolerance = max(1e-9, max(abs(float(value)), abs(float(limit))) * 1e-12)
    return float(value) > float(limit) + tolerance


@dataclass(frozen=True)
class RiskInputEvidence:
    """Provenance for one coherent group of resolver inputs."""

    source: str
    observed_at: datetime
    available_at: datetime
    content_sha256: str
    provider_generation: str

    def __post_init__(self) -> None:
        observed = _utc(self.observed_at, "evidence.observed_at")
        available = _utc(self.available_at, "evidence.available_at")
        if available < observed:
            raise AdaptiveRiskContractError(
                "evidence available_at cannot precede observed_at"
            )
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        if not str(self.source or "").strip():
            raise AdaptiveRiskContractError("evidence source is required")
        if not _is_sha256(self.content_sha256):
            raise AdaptiveRiskContractError(
                "evidence content_sha256 must be a full lowercase SHA256"
            )
        object.__setattr__(self, "content_sha256", self.content_sha256.lower())
        if not str(self.provider_generation or "").strip():
            raise AdaptiveRiskContractError("provider_generation is required")


@dataclass(frozen=True)
class AdaptiveRiskPolicy:
    """All strategy limits are explicit fractions/multipliers with provenance."""

    policy_version: str
    policy_source: str
    risk_fraction_of_equity: float
    daily_risk_fraction_of_equity: float
    portfolio_risk_fraction_of_equity: float
    cluster_risk_fraction_of_equity: float
    symbol_risk_fraction_of_equity: float
    daily_gap_reserve_fraction_of_equity: float
    max_notional_fraction_of_equity: float
    max_buying_power_fraction_for_notional: float
    max_portfolio_gross_fraction_of_equity: float
    quality_multiplier_floor: float
    quality_multiplier_ceiling: float
    volatility_reference_fraction: float
    volatility_multiplier_floor: float
    spread_reserve_multiple: float
    per_share_gap_reserve_volatility_multiple: float
    max_adv_participation: float
    max_recent_volume_participation: float
    max_executable_depth_participation: float
    market_data_max_age_seconds: float
    account_data_max_age_seconds: float
    reservation_data_max_age_seconds: float
    context_data_max_age_seconds: float

    def __post_init__(self) -> None:
        if not str(self.policy_version or "").strip():
            raise AdaptiveRiskContractError("policy_version is required")
        if not str(self.policy_source or "").strip():
            raise AdaptiveRiskContractError("policy_source is required")
        fraction_fields = (
            "risk_fraction_of_equity",
            "daily_risk_fraction_of_equity",
            "portfolio_risk_fraction_of_equity",
            "cluster_risk_fraction_of_equity",
            "symbol_risk_fraction_of_equity",
            "max_buying_power_fraction_for_notional",
            "max_adv_participation",
            "max_recent_volume_participation",
            "max_executable_depth_participation",
        )
        for name in fraction_fields:
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0 < value <= 1:
                raise AdaptiveRiskContractError(f"{name} must be in (0, 1]")
            object.__setattr__(self, name, value)
        nonnegative_fraction_fields = (
            "daily_gap_reserve_fraction_of_equity",
            "max_notional_fraction_of_equity",
            "max_portfolio_gross_fraction_of_equity",
        )
        for name in nonnegative_fraction_fields:
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise AdaptiveRiskContractError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)
        positive_fields = (
            "quality_multiplier_floor",
            "quality_multiplier_ceiling",
            "volatility_reference_fraction",
            "volatility_multiplier_floor",
            "market_data_max_age_seconds",
            "account_data_max_age_seconds",
            "reservation_data_max_age_seconds",
            "context_data_max_age_seconds",
        )
        for name in positive_fields:
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise AdaptiveRiskContractError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        for name in (
            "spread_reserve_multiple",
            "per_share_gap_reserve_volatility_multiple",
        ):
            reserve_multiple = float(getattr(self, name))
            if not math.isfinite(reserve_multiple) or reserve_multiple < 0:
                raise AdaptiveRiskContractError(f"{name} must be non-negative")
            object.__setattr__(self, name, reserve_multiple)
        if self.quality_multiplier_ceiling < self.quality_multiplier_floor:
            raise AdaptiveRiskContractError(
                "quality multiplier ceiling cannot be below its floor"
            )
        if self.volatility_multiplier_floor > 1:
            raise AdaptiveRiskContractError(
                "volatility multiplier floor cannot exceed one"
            )

    @cached_property
    def policy_sha256(self) -> str:
        return _sha256_json(asdict(self))


@dataclass(frozen=True)
class AdaptiveRiskPolicySettingsReceipt:
    """Canonical proof of the named settings used to build one policy.

    The receipt is safe to include in replay/captured-paper configuration
    provenance: it contains only normalized non-secret policy values.  Its
    digest binds the field-to-setting contract, the normalized settings, and
    the resulting policy snapshot/hash.
    """

    policy: AdaptiveRiskPolicy
    setting_values: tuple[tuple[str, Any], ...]
    schema_version: str = ADAPTIVE_RISK_POLICY_SETTINGS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ADAPTIVE_RISK_POLICY_SETTINGS_SCHEMA_VERSION:
            raise AdaptiveRiskContractError(
                "unsupported adaptive risk settings schema"
            )
        expected_names = tuple(
            setting_name
            for _policy_field, setting_name in ADAPTIVE_RISK_POLICY_SETTING_BINDINGS
        )
        actual_names = tuple(name for name, _value in self.setting_values)
        if actual_names != expected_names:
            raise AdaptiveRiskContractError(
                "adaptive risk settings receipt does not match the binding contract"
            )
        normalized_policy = asdict(self.policy)
        for (policy_field, _setting_name), (_name, value) in zip(
            ADAPTIVE_RISK_POLICY_SETTING_BINDINGS,
            self.setting_values,
            strict=True,
        ):
            if normalized_policy[policy_field] != value:
                raise AdaptiveRiskContractError(
                    "adaptive risk settings receipt value does not match policy"
                )

    def _unsigned_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_field_bindings": {
                policy_field: setting_name
                for policy_field, setting_name in ADAPTIVE_RISK_POLICY_SETTING_BINDINGS
            },
            "settings": dict(self.setting_values),
            "policy_snapshot": asdict(self.policy),
            "policy_sha256": self.policy.policy_sha256,
        }

    @cached_property
    def settings_projection_sha256(self) -> str:
        return _sha256_json(self._unsigned_projection())

    def to_settings_projection(self) -> dict[str, Any]:
        projection = self._unsigned_projection()
        projection["settings_projection_sha256"] = self.settings_projection_sha256
        return projection


def build_adaptive_risk_policy_from_settings(
    settings_obj: Any,
) -> AdaptiveRiskPolicySettingsReceipt:
    """Build the one ReplayV3/captured-PAPER policy from named settings.

    The function intentionally has no execution-surface argument.  Replay and
    PAPER callers therefore cannot select different defaults or inject an
    activation-only size clamp.  Pydantic ``Settings`` validates environment
    input first; the typed policy validates the normalized cross-field
    contract again here.
    """

    if settings_obj is None:
        raise AdaptiveRiskContractError("adaptive risk settings are required")
    policy_values: dict[str, Any] = {}
    for policy_field, setting_name in ADAPTIVE_RISK_POLICY_SETTING_BINDINGS:
        if not hasattr(settings_obj, setting_name):
            raise AdaptiveRiskContractError(
                f"adaptive risk setting is missing: {setting_name}"
            )
        policy_values[policy_field] = getattr(settings_obj, setting_name)
    try:
        policy = AdaptiveRiskPolicy(**policy_values)
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskContractError(
            f"adaptive risk settings failed policy validation: {exc}"
        ) from exc

    normalized_policy = asdict(policy)
    setting_values = tuple(
        (setting_name, normalized_policy[policy_field])
        for policy_field, setting_name in ADAPTIVE_RISK_POLICY_SETTING_BINDINGS
    )
    return AdaptiveRiskPolicySettingsReceipt(
        policy=policy,
        setting_values=setting_values,
    )


def adaptive_risk_policy_settings_projection(settings_obj: Any) -> dict[str, Any]:
    """Return the public canonical non-secret settings projection."""

    return build_adaptive_risk_policy_from_settings(
        settings_obj
    ).to_settings_projection()


@dataclass(frozen=True)
class AdaptiveRiskInputs:
    """One complete, causal risk decision input packet.

    ``open_structural_risk_usd`` and ``pending_reserved_risk_usd`` are the
    aggregate portfolio values used by the daily and portfolio-risk budgets.
    The same-symbol and correlation-cluster fields below are explicit subsets
    used by their narrower limits; callers must not omit pending claims merely
    because they are also present in the aggregate value.

    ``portfolio_gross_notional_usd`` is policy-owned filled/open gross exposure,
    while ``pending_portfolio_gross_notional_usd`` covers all policy-owned
    gross-increasing pending claims not yet in that value.  Each phase's
    structural risk, gross notional, and broker BP impact must be jointly
    present or jointly zero.  ``policy_buying_power_capacity_usd`` is the stable
    account capacity before all policy-owned open and pending claims.  The corresponding
    ``*_buying_power_impact_usd`` fields are broker-estimated buying-power impact,
    not assumed share notional, and remain claimed through pending-to-filled
    transitions.  The separate causal ``buying_power_usd`` snapshot is an
    absolute executable ceiling.  All reservation aggregates must be derived
    atomically from the content-addressed ``reservation_ledger`` evidence.
    """

    decision_id: str
    replay_or_paper_run_id: str
    generation: int
    execution_surface: str
    execution_family: str
    venue: str
    broker_environment: str
    symbol: str
    side: str
    as_of: datetime
    account_identity_sha256: str
    code_build_sha256: str
    effective_config_sha256: str
    feature_flags_sha256: str
    capture_prefix_root_sha256: str
    equity_usd: float
    buying_power_usd: float
    broker_day_change_usd: float
    local_realized_pnl_usd: float
    open_structural_risk_usd: float
    pending_reserved_risk_usd: float
    existing_same_symbol_structural_risk_usd: float
    pending_same_symbol_structural_risk_usd: float
    current_cluster_structural_risk_usd: float
    pending_correlation_cluster_risk_usd: float
    portfolio_gross_notional_usd: float
    pending_portfolio_gross_notional_usd: float
    policy_buying_power_capacity_usd: float
    open_buying_power_impact_usd: float
    pending_buying_power_impact_usd: float
    candidate_buying_power_impact_per_share_usd: float
    bid: float
    ask: float
    structural_stop: float
    entry_slippage_bps: float
    exit_slippage_bps: float
    fees_per_share_usd: float
    setup_quality: float
    realized_volatility_fraction: float
    average_daily_volume_shares: float
    recent_volume_shares: float
    executable_depth_shares: float
    correlation_cluster_id: str
    evidence: Mapping[str, RiskInputEvidence]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))
        object.__setattr__(self, "symbol", str(self.symbol or "").strip().upper())
        object.__setattr__(
            self,
            "correlation_cluster_id",
            str(self.correlation_cluster_id or "").strip().lower(),
        )
        object.__setattr__(self, "side", str(self.side or "").strip().lower())
        object.__setattr__(
            self, "execution_surface", str(self.execution_surface or "").strip().lower()
        )
        for name in ("execution_family", "venue", "broker_environment"):
            object.__setattr__(
                self,
                name,
                str(getattr(self, name) or "").strip().lower(),
            )
        for name in (
            "existing_same_symbol_structural_risk_usd",
            "pending_same_symbol_structural_risk_usd",
            "pending_correlation_cluster_risk_usd",
            "pending_portfolio_gross_notional_usd",
            "policy_buying_power_capacity_usd",
            "open_buying_power_impact_usd",
            "pending_buying_power_impact_usd",
        ):
            value = getattr(self, name)
            if not _finite(value) or float(value) < 0:
                raise AdaptiveRiskContractError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, float(value))
        candidate_impact = self.candidate_buying_power_impact_per_share_usd
        if not _finite(candidate_impact) or float(candidate_impact) <= 0:
            raise AdaptiveRiskContractError(
                "candidate_buying_power_impact_per_share_usd must be finite and positive"
            )
        object.__setattr__(
            self,
            "candidate_buying_power_impact_per_share_usd",
            float(candidate_impact),
        )

    @cached_property
    def input_sha256(self) -> str:
        return _sha256_json(asdict(self))

    @cached_property
    def economic_input_sha256(self) -> str:
        """Parity fingerprint excluding replay/paper transport identity."""

        raw = asdict(self)
        raw.pop("decision_id", None)
        raw.pop("replay_or_paper_run_id", None)
        raw.pop("execution_surface", None)
        return _sha256_json(raw)


@dataclass(frozen=True)
class ResolvedAdaptiveRisk:
    schema_version: str
    valid: bool
    rejection_reasons: tuple[str, ...]
    policy_sha256: str
    input_sha256: str
    economic_input_sha256: str
    effective_entry_price: float
    effective_stop_exit_price: float
    risk_per_share_usd: float
    base_r_usd: float
    setup_quality_multiplier: float
    volatility_multiplier: float
    candidate_risk_budget_usd: float
    risk_budget_caps_usd: Mapping[str, float]
    buying_power_caps_usd: Mapping[str, float]
    notional_caps_usd: Mapping[str, float]
    quantity_caps_shares: Mapping[str, int]
    binding_constraints: tuple[str, ...]
    quantity_shares: int
    planned_structural_risk_usd: float
    planned_notional_usd: float
    planned_buying_power_impact_usd: float
    remaining_daily_risk_after_candidate_usd: float
    remaining_portfolio_risk_after_candidate_usd: float
    remaining_cluster_risk_after_candidate_usd: float
    policy_snapshot: Mapping[str, Any]
    input_snapshot: Mapping[str, Any]

    @cached_property
    def economic_resolution_sha256(self) -> str:
        raw = asdict(self)
        raw.pop("input_sha256", None)
        input_snapshot = dict(raw.pop("input_snapshot", {}))
        input_snapshot.pop("decision_id", None)
        input_snapshot.pop("replay_or_paper_run_id", None)
        input_snapshot.pop("execution_surface", None)
        raw["input_snapshot"] = input_snapshot
        return _sha256_json(raw)

    @cached_property
    def decision_packet_sha256(self) -> str:
        return _sha256_json(asdict(self))

    def to_decision_packet(self) -> dict[str, Any]:
        # Round-trip through the same canonical encoder used by the hashes.  A
        # plain ``asdict`` leaves timezone-aware ``datetime`` objects nested in
        # ``input_snapshot.evidence``; PostgreSQL JSON columns (and the capture
        # store) cannot serialize those objects.  The canonical round-trip is
        # JSON-safe while remaining byte-equivalent for hash verification.
        packet = json.loads(_canonical_json(asdict(self)).decode("utf-8"))
        packet["decision_packet_sha256"] = self.decision_packet_sha256
        packet["economic_resolution_sha256"] = self.economic_resolution_sha256
        return packet


def load_and_verify_adaptive_risk_decision_packet(
    packet: Mapping[str, Any],
) -> ResolvedAdaptiveRisk:
    """Strictly reconstruct and recompute one persisted decision packet.

    A ``ResolvedAdaptiveRisk`` instance can be constructed directly, so an
    ``isinstance`` check is not evidence that its nested snapshots or derived
    fields are genuine.  Certification callers must enter through this loader:
    it rebuilds the typed policy/evidence/input packet, reruns the pure resolver,
    and requires the complete canonical packet (including both hashes) to match.
    Unknown, omitted, stale, or tampered fields therefore fail closed.
    """

    if not isinstance(packet, Mapping):
        raise AdaptiveRiskContractError("adaptive risk decision packet must be a mapping")
    raw_packet = dict(packet)
    if raw_packet.get("schema_version") != RISK_PACKET_SCHEMA_VERSION:
        raise AdaptiveRiskContractError("unsupported adaptive risk decision schema")
    policy_raw = raw_packet.get("policy_snapshot")
    inputs_raw = raw_packet.get("input_snapshot")
    if not isinstance(policy_raw, Mapping) or not isinstance(inputs_raw, Mapping):
        raise AdaptiveRiskContractError("adaptive risk decision snapshots are missing")

    try:
        policy = AdaptiveRiskPolicy(**dict(policy_raw))
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskContractError(
            f"adaptive risk policy snapshot is invalid: {exc}"
        ) from exc

    input_values = dict(inputs_raw)
    evidence_raw = input_values.get("evidence")
    if not isinstance(evidence_raw, Mapping):
        raise AdaptiveRiskContractError("adaptive risk evidence snapshot is missing")
    evidence: dict[str, RiskInputEvidence] = {}
    for name, value in evidence_raw.items():
        if not isinstance(value, Mapping):
            raise AdaptiveRiskContractError(f"adaptive risk evidence is invalid: {name}")
        evidence_values = dict(value)
        evidence_values["observed_at"] = _parse_utc(
            evidence_values.get("observed_at"), f"evidence.{name}.observed_at"
        )
        evidence_values["available_at"] = _parse_utc(
            evidence_values.get("available_at"), f"evidence.{name}.available_at"
        )
        try:
            evidence[str(name)] = RiskInputEvidence(**evidence_values)
        except (TypeError, ValueError) as exc:
            raise AdaptiveRiskContractError(
                f"adaptive risk evidence is invalid: {name}: {exc}"
            ) from exc
    input_values["evidence"] = evidence
    input_values["as_of"] = _parse_utc(input_values.get("as_of"), "input.as_of")
    try:
        inputs = AdaptiveRiskInputs(**input_values)
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskContractError(
            f"adaptive risk input snapshot is invalid: {exc}"
        ) from exc

    recomputed = resolve_adaptive_risk(policy, inputs)
    expected_packet = recomputed.to_decision_packet()
    if _canonical_json(raw_packet) != _canonical_json(expected_packet):
        raise AdaptiveRiskContractError(
            "adaptive risk decision packet failed canonical recomputation"
        )
    return recomputed


_REQUIRED_EVIDENCE = frozenset(
    {
        "account",
        "daily_pnl",
        "bbo",
        "structural_stop",
        "setup_quality",
        "volatility",
        "liquidity",
        "portfolio_heat",
        "correlation",
        "code_build",
        "effective_config",
        "feature_flags",
        "capture_prefix",
        "candidate_buying_power_estimate",
        "reservation_ledger",
    }
)
_ACCOUNT_EVIDENCE = frozenset({"account", "daily_pnl"})
_RESERVATION_EVIDENCE = frozenset(
    {
        "candidate_buying_power_estimate",
        "portfolio_heat",
        "reservation_ledger",
    }
)
_MARKET_EVIDENCE = frozenset(
    {"bbo", "structural_stop", "volatility", "liquidity"}
)


def _zero_resolution(
    policy: AdaptiveRiskPolicy,
    inputs: AdaptiveRiskInputs,
    reasons: list[str],
) -> ResolvedAdaptiveRisk:
    return ResolvedAdaptiveRisk(
        schema_version=RISK_PACKET_SCHEMA_VERSION,
        valid=False,
        rejection_reasons=tuple(dict.fromkeys(reasons)),
        policy_sha256=policy.policy_sha256,
        input_sha256=inputs.input_sha256,
        economic_input_sha256=inputs.economic_input_sha256,
        effective_entry_price=0.0,
        effective_stop_exit_price=0.0,
        risk_per_share_usd=0.0,
        base_r_usd=0.0,
        setup_quality_multiplier=0.0,
        volatility_multiplier=0.0,
        candidate_risk_budget_usd=0.0,
        risk_budget_caps_usd={},
        buying_power_caps_usd={},
        notional_caps_usd={},
        quantity_caps_shares={},
        binding_constraints=(),
        quantity_shares=0,
        planned_structural_risk_usd=0.0,
        planned_notional_usd=0.0,
        planned_buying_power_impact_usd=0.0,
        remaining_daily_risk_after_candidate_usd=0.0,
        remaining_portfolio_risk_after_candidate_usd=0.0,
        remaining_cluster_risk_after_candidate_usd=0.0,
        policy_snapshot=asdict(policy),
        input_snapshot=asdict(inputs),
    )


def _validate_inputs(
    policy: AdaptiveRiskPolicy,
    inputs: AdaptiveRiskInputs,
) -> list[str]:
    reasons: list[str] = []
    if not inputs.decision_id:
        reasons.append("decision_id_missing")
    try:
        uuid.UUID(inputs.replay_or_paper_run_id)
    except (ValueError, AttributeError):
        reasons.append("run_id_invalid")
    try:
        generation = int(inputs.generation)
    except (TypeError, ValueError):
        generation = 0
    if isinstance(inputs.generation, bool) or generation <= 0:
        reasons.append("generation_invalid")
    if inputs.execution_surface not in {
        "replay",
        "db_paper",
        "alpaca_paper",
        "live",
    }:
        reasons.append("execution_surface_invalid")
    for name in ("execution_family", "venue", "broker_environment"):
        if not str(getattr(inputs, name) or "").strip():
            reasons.append(f"{name}_missing")
    if not inputs.symbol:
        reasons.append("symbol_missing")
    if not inputs.correlation_cluster_id:
        reasons.append("correlation_cluster_id_missing")
    if inputs.side != "long":
        reasons.append("unsupported_side")
    for name in (
        "account_identity_sha256",
        "code_build_sha256",
        "effective_config_sha256",
        "feature_flags_sha256",
        "capture_prefix_root_sha256",
    ):
        if not _is_sha256(getattr(inputs, name)):
            reasons.append(f"{name}_invalid")

    numeric_names = (
        "equity_usd",
        "buying_power_usd",
        "broker_day_change_usd",
        "local_realized_pnl_usd",
        "open_structural_risk_usd",
        "pending_reserved_risk_usd",
        "existing_same_symbol_structural_risk_usd",
        "pending_same_symbol_structural_risk_usd",
        "current_cluster_structural_risk_usd",
        "pending_correlation_cluster_risk_usd",
        "portfolio_gross_notional_usd",
        "pending_portfolio_gross_notional_usd",
        "policy_buying_power_capacity_usd",
        "open_buying_power_impact_usd",
        "pending_buying_power_impact_usd",
        "candidate_buying_power_impact_per_share_usd",
        "bid",
        "ask",
        "structural_stop",
        "entry_slippage_bps",
        "exit_slippage_bps",
        "fees_per_share_usd",
        "setup_quality",
        "realized_volatility_fraction",
        "average_daily_volume_shares",
        "recent_volume_shares",
        "executable_depth_shares",
    )
    for name in numeric_names:
        if not _finite(getattr(inputs, name)):
            reasons.append(f"{name}_nonfinite")

    if reasons:
        return reasons
    if inputs.equity_usd <= 0:
        reasons.append("equity_not_positive")
    nonnegative = (
        "buying_power_usd",
        "open_structural_risk_usd",
        "pending_reserved_risk_usd",
        "existing_same_symbol_structural_risk_usd",
        "pending_same_symbol_structural_risk_usd",
        "current_cluster_structural_risk_usd",
        "pending_correlation_cluster_risk_usd",
        "portfolio_gross_notional_usd",
        "pending_portfolio_gross_notional_usd",
        "policy_buying_power_capacity_usd",
        "open_buying_power_impact_usd",
        "pending_buying_power_impact_usd",
        "entry_slippage_bps",
        "exit_slippage_bps",
        "fees_per_share_usd",
        "average_daily_volume_shares",
        "recent_volume_shares",
        "executable_depth_shares",
    )
    for name in nonnegative:
        if float(getattr(inputs, name)) < 0:
            reasons.append(f"{name}_negative")
    if _materially_exceeds(
        inputs.existing_same_symbol_structural_risk_usd,
        inputs.open_structural_risk_usd,
    ):
        reasons.append("same_symbol_existing_risk_exceeds_open_risk")
    if _materially_exceeds(
        inputs.current_cluster_structural_risk_usd,
        inputs.open_structural_risk_usd,
    ):
        reasons.append("cluster_existing_risk_exceeds_open_risk")
    if _materially_exceeds(
        inputs.existing_same_symbol_structural_risk_usd,
        inputs.current_cluster_structural_risk_usd,
    ):
        reasons.append("same_symbol_existing_risk_exceeds_cluster_risk")
    if _materially_exceeds(
        inputs.pending_same_symbol_structural_risk_usd,
        inputs.pending_reserved_risk_usd,
    ):
        reasons.append("same_symbol_pending_risk_exceeds_pending_risk")
    if _materially_exceeds(
        inputs.pending_correlation_cluster_risk_usd,
        inputs.pending_reserved_risk_usd,
    ):
        reasons.append("cluster_pending_risk_exceeds_pending_risk")
    if _materially_exceeds(
        inputs.pending_same_symbol_structural_risk_usd,
        inputs.pending_correlation_cluster_risk_usd,
    ):
        reasons.append("same_symbol_pending_risk_exceeds_cluster_pending_risk")
    open_dimensions_present = (
        inputs.open_structural_risk_usd > 0,
        inputs.portfolio_gross_notional_usd > 0,
        inputs.open_buying_power_impact_usd > 0,
    )
    if any(open_dimensions_present) and not all(open_dimensions_present):
        reasons.append("open_reservation_dimensions_incomplete")
    pending_dimensions_present = (
        inputs.pending_reserved_risk_usd > 0,
        inputs.pending_portfolio_gross_notional_usd > 0,
        inputs.pending_buying_power_impact_usd > 0,
    )
    if any(pending_dimensions_present) and not all(pending_dimensions_present):
        reasons.append("pending_reservation_dimensions_incomplete")
    if (
        inputs.portfolio_gross_notional_usd > 0
        and inputs.open_buying_power_impact_usd <= 0
    ):
        reasons.append("open_buying_power_impact_missing")
    if (
        inputs.pending_portfolio_gross_notional_usd > 0
        and inputs.pending_buying_power_impact_usd <= 0
    ):
        reasons.append("pending_buying_power_impact_missing")
    reflected_floor = inputs.buying_power_usd + inputs.open_buying_power_impact_usd
    if _materially_exceeds(
        reflected_floor,
        inputs.policy_buying_power_capacity_usd,
    ):
        reasons.append("policy_buying_power_capacity_below_available")
    if _materially_exceeds(
        inputs.policy_buying_power_capacity_usd,
        reflected_floor + inputs.pending_buying_power_impact_usd,
    ):
        reasons.append("policy_buying_power_capacity_exceeds_reconstructable")
    if inputs.bid <= 0 or inputs.ask <= 0 or inputs.ask < inputs.bid:
        reasons.append("bbo_invalid")
    if inputs.structural_stop <= 0 or inputs.structural_stop >= inputs.ask:
        reasons.append("structural_stop_invalid")
    if not 0 <= inputs.setup_quality <= 1:
        reasons.append("setup_quality_out_of_range")
    if inputs.realized_volatility_fraction <= 0:
        reasons.append("volatility_not_positive")
    if inputs.candidate_buying_power_impact_per_share_usd <= 0:
        reasons.append("candidate_buying_power_impact_not_positive")

    evidence_keys = set(inputs.evidence)
    for key in sorted(_REQUIRED_EVIDENCE - evidence_keys):
        reasons.append(f"evidence_missing:{key}")
    for key in sorted(_REQUIRED_EVIDENCE & evidence_keys):
        evidence = inputs.evidence[key]
        if not isinstance(evidence, RiskInputEvidence):
            reasons.append(f"evidence_invalid:{key}")
            continue
        if evidence.available_at > inputs.as_of:
            reasons.append(f"evidence_from_future:{key}")
            continue
        available_age = (inputs.as_of - evidence.available_at).total_seconds()
        observed_age = (inputs.as_of - evidence.observed_at).total_seconds()
        if key in _RESERVATION_EVIDENCE:
            max_age = policy.reservation_data_max_age_seconds
        elif key in _ACCOUNT_EVIDENCE:
            max_age = policy.account_data_max_age_seconds
        elif key in _MARKET_EVIDENCE:
            max_age = policy.market_data_max_age_seconds
        else:
            max_age = policy.context_data_max_age_seconds
        if available_age > max_age or observed_age > max_age:
            reasons.append(f"evidence_stale:{key}")
        if available_age > max_age:
            reasons.append(f"evidence_availability_clock_stale:{key}")
        if observed_age > max_age:
            reasons.append(f"evidence_observed_clock_stale:{key}")
    return reasons


def resolve_adaptive_risk(
    policy: AdaptiveRiskPolicy,
    inputs: AdaptiveRiskInputs,
) -> ResolvedAdaptiveRisk:
    """Resolve causal long-side quantity and all binding constraints."""

    reasons = _validate_inputs(policy, inputs)
    if reasons:
        return _zero_resolution(policy, inputs, reasons)

    equity = float(inputs.equity_usd)
    effective_entry = float(inputs.ask) * (
        1.0 + float(inputs.entry_slippage_bps) / 10_000.0
    )
    effective_stop_exit = float(inputs.structural_stop) * (
        1.0 - float(inputs.exit_slippage_bps) / 10_000.0
    )
    gap_reserve_per_share = (
        effective_entry
        * float(inputs.realized_volatility_fraction)
        * policy.per_share_gap_reserve_volatility_multiple
    )
    spread_reserve_per_share = max(0.0, float(inputs.ask) - float(inputs.bid)) * (
        policy.spread_reserve_multiple
    )
    risk_per_share = (
        effective_entry
        - effective_stop_exit
        + float(inputs.fees_per_share_usd)
        + spread_reserve_per_share
        + gap_reserve_per_share
    )
    if not math.isfinite(risk_per_share) or risk_per_share <= 0:
        return _zero_resolution(policy, inputs, ["risk_per_share_invalid"])

    quality_multiplier = policy.quality_multiplier_floor + float(
        inputs.setup_quality
    ) * (policy.quality_multiplier_ceiling - policy.quality_multiplier_floor)
    volatility_multiplier = _clamp(
        policy.volatility_reference_fraction
        / float(inputs.realized_volatility_fraction),
        policy.volatility_multiplier_floor,
        1.0,
    )
    base_r = equity * policy.risk_fraction_of_equity
    quality_adjusted_r = base_r * quality_multiplier * volatility_multiplier

    authoritative_drawdown = max(
        0.0,
        -float(inputs.broker_day_change_usd),
        -float(inputs.local_realized_pnl_usd),
    )
    daily_budget = equity * policy.daily_risk_fraction_of_equity
    daily_gap_reserve = equity * policy.daily_gap_reserve_fraction_of_equity
    daily_remaining = max(
        0.0,
        daily_budget
        - authoritative_drawdown
        - float(inputs.open_structural_risk_usd)
        - float(inputs.pending_reserved_risk_usd)
        - daily_gap_reserve,
    )
    portfolio_remaining = max(
        0.0,
        equity * policy.portfolio_risk_fraction_of_equity
        - float(inputs.open_structural_risk_usd)
        - float(inputs.pending_reserved_risk_usd),
    )
    symbol_remaining = max(
        0.0,
        equity * policy.symbol_risk_fraction_of_equity
        - float(inputs.existing_same_symbol_structural_risk_usd)
        - float(inputs.pending_same_symbol_structural_risk_usd),
    )
    cluster_remaining = max(
        0.0,
        equity * policy.cluster_risk_fraction_of_equity
        - float(inputs.current_cluster_structural_risk_usd)
        - float(inputs.pending_correlation_cluster_risk_usd),
    )
    risk_budget_caps = {
        "quality_and_volatility_adjusted_r": quality_adjusted_r,
        "symbol_remaining_after_existing_and_pending": symbol_remaining,
        "daily_remaining_after_open_pending_and_reserve": daily_remaining,
        "portfolio_remaining_after_open_and_pending": portfolio_remaining,
        "correlation_cluster_remaining": cluster_remaining,
    }
    candidate_risk = max(0.0, min(risk_budget_caps.values()))
    if candidate_risk <= 0:
        return _zero_resolution(policy, inputs, ["risk_budget_exhausted"])

    buying_power_caps = {
        "buying_power_policy_remaining_after_reservations": max(
            0.0,
            float(inputs.policy_buying_power_capacity_usd)
            * policy.max_buying_power_fraction_for_notional
            - float(inputs.open_buying_power_impact_usd)
            - float(inputs.pending_buying_power_impact_usd),
        ),
        "broker_available_buying_power": float(inputs.buying_power_usd),
    }
    notional_caps = {
        "equity_notional_cap": equity * policy.max_notional_fraction_of_equity,
        "portfolio_gross_remaining_after_pending": max(
            0.0,
            equity * policy.max_portfolio_gross_fraction_of_equity
            - float(inputs.portfolio_gross_notional_usd)
            - float(inputs.pending_portfolio_gross_notional_usd),
        ),
    }
    notional_budget = min(notional_caps.values())
    quantity_caps = {
        "structural_risk": math.floor(candidate_risk / risk_per_share),
        "notional": math.floor(notional_budget / effective_entry),
        "buying_power_policy": math.floor(
            buying_power_caps[
                "buying_power_policy_remaining_after_reservations"
            ]
            / float(inputs.candidate_buying_power_impact_per_share_usd)
        ),
        "broker_available_buying_power": math.floor(
            buying_power_caps["broker_available_buying_power"]
            / float(inputs.candidate_buying_power_impact_per_share_usd)
        ),
        "adv_participation": math.floor(
            float(inputs.average_daily_volume_shares)
            * policy.max_adv_participation
        ),
        "recent_volume_participation": math.floor(
            float(inputs.recent_volume_shares)
            * policy.max_recent_volume_participation
        ),
        "executable_depth_participation": math.floor(
            float(inputs.executable_depth_shares)
            * policy.max_executable_depth_participation
        ),
    }
    quantity = min(quantity_caps.values())
    if quantity <= 0:
        rejected = _zero_resolution(policy, inputs, ["no_executable_quantity"])
        return ResolvedAdaptiveRisk(
            **{
                **asdict(rejected),
                "effective_entry_price": effective_entry,
                "effective_stop_exit_price": effective_stop_exit,
                "risk_per_share_usd": risk_per_share,
                "base_r_usd": base_r,
                "setup_quality_multiplier": quality_multiplier,
                "volatility_multiplier": volatility_multiplier,
                "candidate_risk_budget_usd": candidate_risk,
                "risk_budget_caps_usd": risk_budget_caps,
                "buying_power_caps_usd": buying_power_caps,
                "notional_caps_usd": notional_caps,
                "quantity_caps_shares": quantity_caps,
            }
        )

    tolerance = max(1e-9, candidate_risk * 1e-9)
    bindings = [
        f"risk_budget:{name}"
        for name, value in risk_budget_caps.items()
        if abs(value - candidate_risk) <= tolerance
    ]
    bindings.extend(
        f"quantity:{name}" for name, value in quantity_caps.items() if value == quantity
    )
    planned_risk = quantity * risk_per_share
    planned_notional = quantity * effective_entry
    planned_buying_power_impact = (
        quantity * float(inputs.candidate_buying_power_impact_per_share_usd)
    )
    # Integer rounding must never allow the candidate to exceed any budget.
    if planned_risk > candidate_risk + tolerance:
        return _zero_resolution(policy, inputs, ["integer_risk_budget_violation"])
    if planned_notional > notional_budget + max(1e-9, notional_budget * 1e-9):
        return _zero_resolution(policy, inputs, ["integer_notional_budget_violation"])
    buying_power_budget = min(buying_power_caps.values())
    if planned_buying_power_impact > buying_power_budget + max(
        1e-9, buying_power_budget * 1e-9
    ):
        return _zero_resolution(policy, inputs, ["integer_buying_power_violation"])

    return ResolvedAdaptiveRisk(
        schema_version=RISK_PACKET_SCHEMA_VERSION,
        valid=True,
        rejection_reasons=(),
        policy_sha256=policy.policy_sha256,
        input_sha256=inputs.input_sha256,
        economic_input_sha256=inputs.economic_input_sha256,
        effective_entry_price=effective_entry,
        effective_stop_exit_price=effective_stop_exit,
        risk_per_share_usd=risk_per_share,
        base_r_usd=base_r,
        setup_quality_multiplier=quality_multiplier,
        volatility_multiplier=volatility_multiplier,
        candidate_risk_budget_usd=candidate_risk,
        risk_budget_caps_usd=risk_budget_caps,
        buying_power_caps_usd=buying_power_caps,
        notional_caps_usd=notional_caps,
        quantity_caps_shares=quantity_caps,
        binding_constraints=tuple(bindings),
        quantity_shares=int(quantity),
        planned_structural_risk_usd=planned_risk,
        planned_notional_usd=planned_notional,
        planned_buying_power_impact_usd=planned_buying_power_impact,
        remaining_daily_risk_after_candidate_usd=max(
            0.0, daily_remaining - planned_risk
        ),
        remaining_portfolio_risk_after_candidate_usd=max(
            0.0, portfolio_remaining - planned_risk
        ),
        remaining_cluster_risk_after_candidate_usd=max(
            0.0, cluster_remaining - planned_risk
        ),
        policy_snapshot=asdict(policy),
        input_snapshot=asdict(inputs),
    )
