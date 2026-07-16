"""Fail-closed paired out-of-sample benchmark mechanics.

This module is deliberately pure: it performs no provider, broker, database,
filesystem, or wall-clock access.  A study plan is content addressed before its
test sessions, complete ReplayV3 runs are paired on the same sealed input, and
the resulting report is only descriptive JSON.

The current implementation is mechanics-only and cannot mint an
``OosGateReceipt``.  ReplayV3 does not yet expose a receipt binding the full
counterfactual trade/risk/fill/fee/equity/quote ledger; this pure module also
cannot prove an append-only prospective registration or an authoritative
after-fact label.  Those missing authorities are explicit global blockers,
even when caller-supplied fixtures make every numeric rule pass.

The private issuers are integration seams, not public evidence loaders.  In
particular, dictionaries deserialized from a report cannot be converted back
into trusted plan, ledger, label, or gate authority.  A sealed ReplayV3
*reproduction* receipt is also not a counterfactual receipt: caller-supplied
trade rows and secondary roots may exercise metric mechanics, but they remain
``DIAGNOSTIC_ONLY`` and can never authorize promotion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import random
from typing import Any, ClassVar, Literal, Mapping, Sequence
import weakref


_HEX = frozenset("0123456789abcdef")
_PLAN_TOKEN = object()
_LEDGER_TOKEN = object()
_LABEL_TOKEN = object()
_SCOREBOARD_TOKEN = object()
# No promotion authority exists yet.  This remains ``None`` until the three
# owner-minted receipts listed in ``_compute_scoreboard`` are implemented and
# reviewed together; a private-looking Python object must not stand in for them.
_GATE_TOKEN: object | None = None
_AUTHORITY_RECORDS: dict[int, tuple[weakref.ReferenceType[Any], str, str]] = {}


class OosScoreboardError(ValueError):
    """The requested study is malformed or would weaken an evidence boundary."""


def _record_private_authority(value: Any, *, kind: str, digest: str) -> None:
    """Register one exact object identity; copied/replaced lookalikes are untrusted."""

    object_id = id(value)

    def remove(reference: weakref.ReferenceType[Any], *, expected_id: int = object_id) -> None:
        current = _AUTHORITY_RECORDS.get(expected_id)
        if current is not None and current[0] is reference:
            _AUTHORITY_RECORDS.pop(expected_id, None)

    reference = weakref.ref(value, remove)
    _AUTHORITY_RECORDS[object_id] = (reference, str(kind), _digest(digest, "authority_digest"))


def _has_private_authority(value: Any, *, kind: str, digest: str) -> bool:
    current = _AUTHORITY_RECORDS.get(id(value))
    return bool(
        current is not None
        and current[0]() is value
        and current[1] == kind
        and current[2] == str(digest or "").strip().lower()
    )


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise OosScoreboardError("all timestamps must be timezone-aware")
        return value.isoformat()
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, float) and not math.isfinite(value):
        raise OosScoreboardError("non-finite numeric value is not canonical")
    return value


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _digest(value: str, name: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in _HEX for char in result):
        raise OosScoreboardError(f"{name} must be a lowercase SHA-256 digest")
    return result


def _name(value: str, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise OosScoreboardError(f"{name} is required")
    return result


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OosScoreboardError(f"{name} must be timezone-aware")
    return value


def _finite(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise OosScoreboardError(f"{name} must be finite")
    return result


def _nonnegative(value: float, name: str) -> float:
    result = _finite(value, name)
    if result < 0:
        raise OosScoreboardError(f"{name} must be non-negative")
    return result


def _positive(value: float, name: str) -> float:
    result = _finite(value, name)
    if result <= 0:
        raise OosScoreboardError(f"{name} must be positive")
    return result


@dataclass(frozen=True)
class ArmProvenanceV1:
    """Every strategy artifact that may differ between paired arms."""

    schema_version: ClassVar[str] = "chili.oos-arm-provenance.v1"
    arm_id: str
    build_sha256: str
    variant_sha256: str
    config_sha256: str
    feature_flags_sha256: str
    model_sha256: str
    risk_policy_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "arm_id", _name(self.arm_id, "arm_id"))
        for attr in (
            "build_sha256",
            "variant_sha256",
            "config_sha256",
            "feature_flags_sha256",
            "model_sha256",
            "risk_policy_sha256",
        ):
            object.__setattr__(self, attr, _digest(getattr(self, attr), attr))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "arm_id": self.arm_id,
            "build_sha256": self.build_sha256,
            "variant_sha256": self.variant_sha256,
            "config_sha256": self.config_sha256,
            "feature_flags_sha256": self.feature_flags_sha256,
            "model_sha256": self.model_sha256,
            "risk_policy_sha256": self.risk_policy_sha256,
        }

    @property
    def arm_sha256(self) -> str:
        return _sha256_json(self.to_dict())


@dataclass(frozen=True)
class CostPolicyV1:
    schema_version: ClassVar[str] = "chili.oos-cost-policy.v1"
    policy_identity_sha256: str
    fee_schedule_sha256: str
    adverse_slippage_model_sha256: str
    executable_fill_policy_sha256: str
    provenance_sha256: str

    def __post_init__(self) -> None:
        for attr in (
            "policy_identity_sha256",
            "fee_schedule_sha256",
            "adverse_slippage_model_sha256",
            "executable_fill_policy_sha256",
            "provenance_sha256",
        ):
            object.__setattr__(self, attr, _digest(getattr(self, attr), attr))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_identity_sha256": self.policy_identity_sha256,
            "fee_schedule_sha256": self.fee_schedule_sha256,
            "adverse_slippage_model_sha256": self.adverse_slippage_model_sha256,
            "executable_fill_policy_sha256": self.executable_fill_policy_sha256,
            "provenance_sha256": self.provenance_sha256,
        }

    @property
    def cost_policy_sha256(self) -> str:
        return _sha256_json(self.to_dict())


@dataclass(frozen=True)
class AcceptanceRulesV1:
    """Prospectively bound thresholds; no metric has a numeric default."""

    schema_version: ClassVar[str] = "chili.oos-acceptance-rules.v1"
    minimum_paired_sessions: int
    minimum_complete_folds: int
    minimum_candidate_net_pnl_usd: float
    minimum_candidate_expectancy_r: float
    minimum_candidate_profit_factor: float
    minimum_mean_session_net_pnl_delta_usd: float
    minimum_mean_session_net_pnl_delta_r: float
    minimum_bootstrap_lower_delta_usd: float
    minimum_bootstrap_lower_delta_r: float
    maximum_candidate_drawdown_r: float
    maximum_drawdown_r_increase: float
    maximum_candidate_worst_loss_r: float
    maximum_worst_loss_r_increase: float
    minimum_risk_utilization: float
    maximum_risk_utilization: float
    maximum_candidate_missed_winners: int
    maximum_missed_winner_delta: int
    maximum_candidate_false_positives: int
    maximum_false_positive_delta: int
    minimum_candidate_mfe_capture_ratio: float
    minimum_mfe_capture_ratio_delta: float
    maximum_candidate_giveback_fraction: float
    maximum_giveback_fraction_delta: float
    minimum_candidate_positive_folds: int
    minimum_candidate_superior_folds: int
    minimum_fold_net_pnl_delta_usd: float
    confidence_level: float
    bootstrap_resamples: int
    random_seed: int
    threshold_provenance_sha256: str
    confidence_method_sha256: str
    margin_policy_sha256: str

    def __post_init__(self) -> None:
        positive_ints = (
            "minimum_paired_sessions",
            "minimum_complete_folds",
            "bootstrap_resamples",
        )
        nonnegative_ints = (
            "maximum_candidate_missed_winners",
            "maximum_candidate_false_positives",
            "minimum_candidate_positive_folds",
            "minimum_candidate_superior_folds",
        )
        signed_ints = ("maximum_missed_winner_delta", "maximum_false_positive_delta")
        for attr in positive_ints:
            value = getattr(self, attr)
            if isinstance(value, bool) or int(value) <= 0:
                raise OosScoreboardError(f"{attr} must be a positive integer")
            object.__setattr__(self, attr, int(value))
        for attr in nonnegative_ints:
            value = getattr(self, attr)
            if isinstance(value, bool) or int(value) < 0:
                raise OosScoreboardError(f"{attr} must be a non-negative integer")
            object.__setattr__(self, attr, int(value))
        for attr in signed_ints:
            value = getattr(self, attr)
            if isinstance(value, bool):
                raise OosScoreboardError(f"{attr} must be an integer")
            object.__setattr__(self, attr, int(value))
        if isinstance(self.random_seed, bool):
            raise OosScoreboardError("random_seed must be an integer")
        object.__setattr__(self, "random_seed", int(self.random_seed))

        nonnegative = (
            "minimum_candidate_profit_factor",
            "maximum_candidate_drawdown_r",
            "maximum_drawdown_r_increase",
            "maximum_candidate_worst_loss_r",
            "maximum_worst_loss_r_increase",
            "minimum_risk_utilization",
            "maximum_risk_utilization",
            "minimum_candidate_mfe_capture_ratio",
            "maximum_candidate_giveback_fraction",
            "maximum_giveback_fraction_delta",
        )
        signed = (
            "minimum_candidate_net_pnl_usd",
            "minimum_candidate_expectancy_r",
            "minimum_mean_session_net_pnl_delta_usd",
            "minimum_mean_session_net_pnl_delta_r",
            "minimum_bootstrap_lower_delta_usd",
            "minimum_bootstrap_lower_delta_r",
            "minimum_mfe_capture_ratio_delta",
            "minimum_fold_net_pnl_delta_usd",
        )
        for attr in nonnegative:
            object.__setattr__(self, attr, _nonnegative(getattr(self, attr), attr))
        for attr in signed:
            object.__setattr__(self, attr, _finite(getattr(self, attr), attr))
        confidence = _finite(self.confidence_level, "confidence_level")
        if not 0 < confidence < 1:
            raise OosScoreboardError("confidence_level must be strictly between zero and one")
        object.__setattr__(self, "confidence_level", confidence)
        if self.minimum_risk_utilization > self.maximum_risk_utilization:
            raise OosScoreboardError("risk utilization bounds are inverted")
        for attr in (
            "threshold_provenance_sha256",
            "confidence_method_sha256",
            "margin_policy_sha256",
        ):
            object.__setattr__(self, attr, _digest(getattr(self, attr), attr))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            **{
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "schema_version"
            },
        }

    @property
    def rules_sha256(self) -> str:
        return _sha256_json(self.to_dict())


@dataclass(frozen=True)
class SessionWindowV1:
    session_id: str
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _name(self.session_id, "session_id"))
        object.__setattr__(self, "starts_at", _aware(self.starts_at, "starts_at"))
        object.__setattr__(self, "ends_at", _aware(self.ends_at, "ends_at"))
        if self.ends_at <= self.starts_at:
            raise OosScoreboardError("session window must have positive duration")

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
        }


@dataclass(frozen=True)
class OosFoldV1:
    fold_id: str
    train_session_ids: tuple[str, ...]
    embargo_session_ids: tuple[str, ...]
    test_session_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fold_id", _name(self.fold_id, "fold_id"))
        for attr in ("train_session_ids", "embargo_session_ids", "test_session_ids"):
            values = tuple(_name(value, attr) for value in getattr(self, attr))
            if not values or len(values) != len(set(values)):
                raise OosScoreboardError(f"{attr} must be non-empty and unique")
            object.__setattr__(self, attr, values)
        train, embargo, test = map(
            set,
            (self.train_session_ids, self.embargo_session_ids, self.test_session_ids),
        )
        if train & embargo or train & test or embargo & test:
            raise OosScoreboardError("fold train, embargo, and test sessions must be disjoint")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold_id": self.fold_id,
            "train_session_ids": list(self.train_session_ids),
            "embargo_session_ids": list(self.embargo_session_ids),
            "test_session_ids": list(self.test_session_ids),
        }


@dataclass(frozen=True)
class OosStudyPlanV1:
    """A complete prospective study definition, before private registration."""

    schema_version: ClassVar[str] = "chili.paired-oos-study-plan.v1"
    study_id: str
    baseline_arm: ArmProvenanceV1
    candidate_arm: ArmProvenanceV1
    cost_policy: CostPolicyV1
    acceptance_rules: AcceptanceRulesV1
    session_windows: tuple[SessionWindowV1, ...]
    folds: tuple[OosFoldV1, ...]
    session_selector_sha256: str
    negative_control_selector_sha256: str
    start_state_policy_sha256: str
    ross_authority_sha256: str
    ross_certifiable_count: int
    ross_diagnostic_only_count: int
    ross_unavailable_count: int
    ross_unresolved_count: int
    label_join_policy_sha256: str
    plan_provenance_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "study_id", _name(self.study_id, "study_id"))
        if not isinstance(self.baseline_arm, ArmProvenanceV1) or not isinstance(
            self.candidate_arm, ArmProvenanceV1
        ):
            raise OosScoreboardError("study arms are malformed")
        if self.baseline_arm.arm_id == self.candidate_arm.arm_id:
            raise OosScoreboardError("baseline and candidate arm ids must differ")
        if not isinstance(self.cost_policy, CostPolicyV1) or not isinstance(
            self.acceptance_rules, AcceptanceRulesV1
        ):
            raise OosScoreboardError("study policy is malformed")
        windows = tuple(self.session_windows)
        folds = tuple(self.folds)
        if not windows or not folds:
            raise OosScoreboardError("study requires session windows and folds")
        if any(not isinstance(value, SessionWindowV1) for value in windows):
            raise OosScoreboardError("session window is malformed")
        if any(not isinstance(value, OosFoldV1) for value in folds):
            raise OosScoreboardError("fold is malformed")
        object.__setattr__(self, "session_windows", windows)
        object.__setattr__(self, "folds", folds)
        window_by_id = {window.session_id: window for window in windows}
        if len(window_by_id) != len(windows):
            raise OosScoreboardError("session ids must be globally unique")
        if len({fold.fold_id for fold in folds}) != len(folds):
            raise OosScoreboardError("fold ids must be unique")
        all_test: set[str] = set()
        test_windows: list[SessionWindowV1] = []
        for fold in folds:
            ids = fold.train_session_ids + fold.embargo_session_ids + fold.test_session_ids
            unknown = set(ids) - set(window_by_id)
            if unknown:
                raise OosScoreboardError(f"fold references unknown sessions: {sorted(unknown)}")
            if all_test.intersection(fold.test_session_ids):
                raise OosScoreboardError("a test session may belong to only one fold")
            all_test.update(fold.test_session_ids)
            train = [window_by_id[value] for value in fold.train_session_ids]
            embargo = [window_by_id[value] for value in fold.embargo_session_ids]
            test = [window_by_id[value] for value in fold.test_session_ids]
            if max(value.ends_at for value in train) >= min(value.starts_at for value in embargo):
                raise OosScoreboardError("train and embargo windows overlap or are unordered")
            if max(value.ends_at for value in embargo) >= min(value.starts_at for value in test):
                raise OosScoreboardError("embargo and test windows overlap or are unordered")
            test_windows.extend(test)
        ordered_tests = sorted(test_windows, key=lambda value: (value.starts_at, value.session_id))
        if any(left.ends_at > right.starts_at for left, right in zip(ordered_tests, ordered_tests[1:])):
            raise OosScoreboardError("test session windows overlap")
        used = {
            value
            for fold in folds
            for value in fold.train_session_ids + fold.embargo_session_ids + fold.test_session_ids
        }
        if used != set(window_by_id):
            raise OosScoreboardError("every registered session window must be assigned to a fold")
        for attr in (
            "session_selector_sha256",
            "negative_control_selector_sha256",
            "start_state_policy_sha256",
            "ross_authority_sha256",
            "label_join_policy_sha256",
            "plan_provenance_sha256",
        ):
            object.__setattr__(self, attr, _digest(getattr(self, attr), attr))
        for attr in (
            "ross_certifiable_count",
            "ross_diagnostic_only_count",
            "ross_unavailable_count",
            "ross_unresolved_count",
        ):
            value = getattr(self, attr)
            if isinstance(value, bool) or int(value) < 0:
                raise OosScoreboardError(f"{attr} must be a non-negative integer")
            object.__setattr__(self, attr, int(value))
        if (
            self.ross_certifiable_count
            + self.ross_diagnostic_only_count
            + self.ross_unavailable_count
            + self.ross_unresolved_count
            == 0
        ):
            raise OosScoreboardError("Ross authority inventory cannot be empty")

    @property
    def test_session_ids(self) -> tuple[str, ...]:
        return tuple(value for fold in self.folds for value in fold.test_session_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "study_id": self.study_id,
            "baseline_arm": self.baseline_arm.to_dict(),
            "candidate_arm": self.candidate_arm.to_dict(),
            "cost_policy": self.cost_policy.to_dict(),
            "acceptance_rules": self.acceptance_rules.to_dict(),
            "session_windows": [value.to_dict() for value in self.session_windows],
            "folds": [value.to_dict() for value in self.folds],
            "session_selector_sha256": self.session_selector_sha256,
            "negative_control_selector_sha256": self.negative_control_selector_sha256,
            "start_state_policy_sha256": self.start_state_policy_sha256,
            "ross_authority_sha256": self.ross_authority_sha256,
            "ross_certifiable_count": self.ross_certifiable_count,
            "ross_diagnostic_only_count": self.ross_diagnostic_only_count,
            "ross_unavailable_count": self.ross_unavailable_count,
            "ross_unresolved_count": self.ross_unresolved_count,
            "label_join_policy_sha256": self.label_join_policy_sha256,
            "plan_provenance_sha256": self.plan_provenance_sha256,
        }

    @property
    def plan_sha256(self) -> str:
        return _sha256_json(self.to_dict())


@dataclass(frozen=True)
class RegisteredOosStudyPlanV1:
    plan: OosStudyPlanV1
    registration_identity_sha256: str
    registered_at: datetime
    registration_receipt_sha256: str
    _authority_token: Any = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority_token is not _PLAN_TOKEN:
            raise OosScoreboardError("study plan lacks trusted prospective registration")
        if not isinstance(self.plan, OosStudyPlanV1):
            raise OosScoreboardError("registered study plan is malformed")
        object.__setattr__(
            self,
            "registration_identity_sha256",
            _digest(self.registration_identity_sha256, "registration_identity_sha256"),
        )
        object.__setattr__(self, "registered_at", _aware(self.registered_at, "registered_at"))
        expected = _sha256_json(
            {
                "plan_sha256": self.plan.plan_sha256,
                "registration_identity_sha256": self.registration_identity_sha256,
                "registered_at": self.registered_at,
            }
        )
        if _digest(self.registration_receipt_sha256, "registration_receipt_sha256") != expected:
            raise OosScoreboardError("registration receipt does not bind the study plan")
        windows = {value.session_id: value for value in self.plan.session_windows}
        earliest_test = min(windows[value].starts_at for value in self.plan.test_session_ids)
        if self.registered_at >= earliest_test:
            raise OosScoreboardError("study plan was not registered before the test frontier")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "chili.registered-paired-oos-study-plan.v1",
            "plan": self.plan.to_dict(),
            "plan_sha256": self.plan.plan_sha256,
            "registration_identity_sha256": self.registration_identity_sha256,
            "registered_at": self.registered_at.isoformat(),
            "registration_receipt_sha256": self.registration_receipt_sha256,
        }


def _register_oos_study_plan(
    plan: OosStudyPlanV1,
    *,
    registration_identity_sha256: str,
    registered_at: datetime,
) -> RegisteredOosStudyPlanV1:
    """Private integration seam for an append-only prospective registry."""

    identity = _digest(registration_identity_sha256, "registration_identity_sha256")
    observed = _aware(registered_at, "registered_at")
    receipt = _sha256_json(
        {
            "plan_sha256": plan.plan_sha256,
            "registration_identity_sha256": identity,
            "registered_at": observed,
        }
    )
    registered = RegisteredOosStudyPlanV1(plan, identity, observed, receipt, _PLAN_TOKEN)
    _record_private_authority(
        registered,
        kind="registered_plan",
        digest=_sha256_json(registered.to_dict()),
    )
    return registered


@dataclass(frozen=True)
class TradeLedgerRowV1:
    trade_id: str
    symbol: str
    entry_at: datetime
    exit_at: datetime
    quantity: float
    entry_reference_ask: float
    entry_fill_price: float
    exit_reference_bid: float
    exit_fill_price: float
    planned_risk_usd: float
    fees_usd: float
    modeled_adverse_slippage_usd: float
    executable_mfe_profit_usd: float
    executable_mae_r: float
    entry_favorable_broker_fill_sha256: str | None
    exit_favorable_broker_fill_sha256: str | None
    quote_path_through_exit: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "trade_id", _name(self.trade_id, "trade_id"))
        object.__setattr__(self, "symbol", _name(self.symbol, "symbol").upper())
        object.__setattr__(self, "entry_at", _aware(self.entry_at, "entry_at"))
        object.__setattr__(self, "exit_at", _aware(self.exit_at, "exit_at"))
        if self.exit_at < self.entry_at:
            raise OosScoreboardError("trade exit precedes entry")
        for attr in (
            "quantity",
            "entry_reference_ask",
            "entry_fill_price",
            "exit_reference_bid",
            "exit_fill_price",
            "planned_risk_usd",
        ):
            object.__setattr__(self, attr, _positive(getattr(self, attr), attr))
        for attr in (
            "fees_usd",
            "modeled_adverse_slippage_usd",
            "executable_mfe_profit_usd",
            "executable_mae_r",
        ):
            object.__setattr__(self, attr, _nonnegative(getattr(self, attr), attr))
        for attr in ("entry_favorable_broker_fill_sha256", "exit_favorable_broker_fill_sha256"):
            value = getattr(self, attr)
            if value is not None:
                object.__setattr__(self, attr, _digest(value, attr))
        if self.entry_fill_price < self.entry_reference_ask and self.entry_favorable_broker_fill_sha256 is None:
            raise OosScoreboardError("entry better than ask lacks exact broker-fill evidence")
        if self.exit_fill_price > self.exit_reference_bid and self.exit_favorable_broker_fill_sha256 is None:
            raise OosScoreboardError("exit better than bid lacks exact broker-fill evidence")
        if not self.quote_path_through_exit:
            raise OosScoreboardError("trade lacks executable quote path through exit")

    @property
    def gross_pnl_usd(self) -> float:
        return (self.exit_fill_price - self.entry_fill_price) * self.quantity

    @property
    def net_pnl_usd(self) -> float:
        return self.gross_pnl_usd - self.fees_usd - self.modeled_adverse_slippage_usd

    @property
    def net_pnl_r(self) -> float:
        return self.net_pnl_usd / self.planned_risk_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "entry_at": self.entry_at.isoformat(),
            "exit_at": self.exit_at.isoformat(),
            "quantity": self.quantity,
            "entry_reference_ask": self.entry_reference_ask,
            "entry_fill_price": self.entry_fill_price,
            "exit_reference_bid": self.exit_reference_bid,
            "exit_fill_price": self.exit_fill_price,
            "planned_risk_usd": self.planned_risk_usd,
            "fees_usd": self.fees_usd,
            "modeled_adverse_slippage_usd": self.modeled_adverse_slippage_usd,
            "executable_mfe_profit_usd": self.executable_mfe_profit_usd,
            "executable_mae_r": self.executable_mae_r,
            "entry_favorable_broker_fill_sha256": self.entry_favorable_broker_fill_sha256,
            "exit_favorable_broker_fill_sha256": self.exit_favorable_broker_fill_sha256,
            "quote_path_through_exit": self.quote_path_through_exit,
        }


@dataclass(frozen=True)
class ReplayV3BenchmarkLedgerV1:
    """DIAGNOSTIC_ONLY paired-metric fixture around a ReplayV3 reproduction.

    ReplayV3 currently proves reproduction of one captured decision/lifecycle.
    The trade rows and full-session economic roots accepted by the diagnostic
    builder below are still caller supplied.  This object is therefore useful
    for testing paired metric mechanics only and is not counterfactual or
    promotion authority.
    """

    schema_version: ClassVar[str] = "chili.replay-v3-benchmark-ledger.v1"
    evidence_grade: ClassVar[str] = "DIAGNOSTIC_ONLY"
    authority_scope: ClassVar[str] = "paired_metric_mechanics_only"
    session_id: str
    session_starts_at: datetime
    session_ends_at: datetime
    arm_id: str
    arm_sha256: str
    cost_policy_sha256: str
    initial_state_policy_sha256: str
    initial_state_sha256: str
    capture_identity_sha256: str
    final_capture_seal_sha256: str
    manifest_sha256: str
    complete_session_root_sha256: str
    release_order_root_sha256: str
    run_binding_sha256: str
    execution_receipt_sha256: str
    os_attestation_sha256: str
    decisions_root_sha256: str
    intents_root_sha256: str
    risk_root_sha256: str
    fills_root_sha256: str
    fees_root_sha256: str
    equity_root_sha256: str
    quote_path_root_sha256: str
    risk_budget_usd: float
    trades: tuple[TradeLedgerRowV1, ...]
    _authority_token: Any = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority_token is not _LEDGER_TOKEN:
            raise OosScoreboardError("ledger lacks trusted ReplayV3 provenance")
        object.__setattr__(self, "session_id", _name(self.session_id, "session_id"))
        object.__setattr__(self, "arm_id", _name(self.arm_id, "arm_id"))
        object.__setattr__(self, "session_starts_at", _aware(self.session_starts_at, "session_starts_at"))
        object.__setattr__(self, "session_ends_at", _aware(self.session_ends_at, "session_ends_at"))
        if self.session_ends_at <= self.session_starts_at:
            raise OosScoreboardError("ledger session window is invalid")
        for attr in (
            "arm_sha256",
            "cost_policy_sha256",
            "initial_state_policy_sha256",
            "initial_state_sha256",
            "capture_identity_sha256",
            "final_capture_seal_sha256",
            "manifest_sha256",
            "complete_session_root_sha256",
            "release_order_root_sha256",
            "run_binding_sha256",
            "execution_receipt_sha256",
            "os_attestation_sha256",
            "decisions_root_sha256",
            "intents_root_sha256",
            "risk_root_sha256",
            "fills_root_sha256",
            "fees_root_sha256",
            "equity_root_sha256",
            "quote_path_root_sha256",
        ):
            object.__setattr__(self, attr, _digest(getattr(self, attr), attr))
        object.__setattr__(self, "risk_budget_usd", _positive(self.risk_budget_usd, "risk_budget_usd"))
        trades = tuple(self.trades)
        if any(not isinstance(value, TradeLedgerRowV1) for value in trades):
            raise OosScoreboardError("ledger trade row is malformed")
        if len({value.trade_id for value in trades}) != len(trades):
            raise OosScoreboardError("ledger trade ids must be unique")
        if any(
            value.entry_at < self.session_starts_at or value.exit_at > self.session_ends_at
            for value in trades
        ):
            raise OosScoreboardError("ledger trade falls outside its complete session")
        object.__setattr__(self, "trades", trades)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_grade": self.evidence_grade,
            "authority_scope": self.authority_scope,
            "session_id": self.session_id,
            "session_starts_at": self.session_starts_at.isoformat(),
            "session_ends_at": self.session_ends_at.isoformat(),
            "arm_id": self.arm_id,
            "arm_sha256": self.arm_sha256,
            "cost_policy_sha256": self.cost_policy_sha256,
            "initial_state_policy_sha256": self.initial_state_policy_sha256,
            "initial_state_sha256": self.initial_state_sha256,
            "capture_identity_sha256": self.capture_identity_sha256,
            "final_capture_seal_sha256": self.final_capture_seal_sha256,
            "manifest_sha256": self.manifest_sha256,
            "complete_session_root_sha256": self.complete_session_root_sha256,
            "release_order_root_sha256": self.release_order_root_sha256,
            "run_binding_sha256": self.run_binding_sha256,
            "execution_receipt_sha256": self.execution_receipt_sha256,
            "os_attestation_sha256": self.os_attestation_sha256,
            "decisions_root_sha256": self.decisions_root_sha256,
            "intents_root_sha256": self.intents_root_sha256,
            "risk_root_sha256": self.risk_root_sha256,
            "fills_root_sha256": self.fills_root_sha256,
            "fees_root_sha256": self.fees_root_sha256,
            "equity_root_sha256": self.equity_root_sha256,
            "quote_path_root_sha256": self.quote_path_root_sha256,
            "risk_budget_usd": self.risk_budget_usd,
            "trades": [value.to_dict() for value in self.trades],
        }

    @property
    def ledger_sha256(self) -> str:
        return _sha256_json(self.to_dict())


@dataclass(frozen=True)
class UnavailableReplayV3LedgerV1:
    evidence_grade: ClassVar[str] = "UNAVAILABLE"
    session_id: str
    arm_id: str
    arm_sha256: str
    blockers: tuple[str, ...]
    _authority_token: Any = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority_token is not _LEDGER_TOKEN:
            raise OosScoreboardError("unavailable ledger lacks trusted ReplayV3 provenance")
        object.__setattr__(self, "session_id", _name(self.session_id, "session_id"))
        object.__setattr__(self, "arm_id", _name(self.arm_id, "arm_id"))
        object.__setattr__(self, "arm_sha256", _digest(self.arm_sha256, "arm_sha256"))
        blockers = tuple(dict.fromkeys(_name(value, "blocker") for value in self.blockers))
        if not blockers:
            raise OosScoreboardError("unavailable ledger requires a blocker")
        object.__setattr__(self, "blockers", blockers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "chili.replay-v3-unavailable-benchmark-ledger.v1",
            "evidence_grade": self.evidence_grade,
            "session_id": self.session_id,
            "arm_id": self.arm_id,
            "arm_sha256": self.arm_sha256,
            "blockers": list(self.blockers),
        }

    @property
    def ledger_sha256(self) -> str:
        return _sha256_json(self.to_dict())


ScoreboardLedger = ReplayV3BenchmarkLedgerV1 | UnavailableReplayV3LedgerV1
# Backward-compatible annotation alias.  It does not imply promotion authority;
# successful rows are explicitly DIAGNOSTIC_ONLY until a distinct sealed
# counterfactual receipt producer exists.
TrustedLedger = ScoreboardLedger


def _issue_diagnostic_replay_v3_benchmark_ledger(
    *,
    replay_result: Any,
    session_id: str,
    session_starts_at: datetime,
    session_ends_at: datetime,
    arm: ArmProvenanceV1,
    cost_policy: CostPolicyV1,
    initial_state_policy_sha256: str,
    initial_state_sha256: str,
    complete_session_root_sha256: str,
    decisions_root_sha256: str,
    intents_root_sha256: str,
    risk_root_sha256: str,
    fills_root_sha256: str,
    fees_root_sha256: str,
    equity_root_sha256: str,
    quote_path_root_sha256: str,
    risk_budget_usd: float,
    trades: Sequence[TradeLedgerRowV1],
) -> ReplayV3BenchmarkLedgerV1:
    """Build DIAGNOSTIC_ONLY metric input around a reproduction receipt."""

    # Lazy import keeps ordinary score computation independent of the replay driver.
    from . import replay_v3 as replay_module

    if not isinstance(replay_result, replay_module.ReplayResult):
        raise OosScoreboardError("benchmark ledger requires a ReplayV3 result")
    binding = replay_result.sealed_run_binding
    receipt = replay_result.sealed_execution_receipt
    attestation = replay_result.os_zero_egress_attestation
    if (
        not replay_result.certification_eligible
        or replay_result.certification_failures
        or not isinstance(binding, replay_module.ReplayV3RunBinding)
        or not isinstance(receipt, replay_module.ReplayV3ExecutionReceipt)
        or receipt._verification_token is not replay_module._REPLAY_V3_EXECUTION_RECEIPT_TOKEN
        or receipt.binding != binding
        or not isinstance(attestation, replay_module.ReplayOsZeroEgressAttestation)
        or attestation._verification_token is not replay_module._REPLAY_OS_ZERO_EGRESS_ATTESTATION_TOKEN
        or attestation.run_binding_sha256 != binding.run_binding_sha256
        or binding.adapter_network_attempt_count != 0
        or binding.python_network_attempt_count != 0
        or binding.adapter_rejected_provider_request_count != 0
    ):
        raise OosScoreboardError("ReplayV3 result is not sealed, hermetic, and certification eligible")
    ledger = ReplayV3BenchmarkLedgerV1(
        session_id=session_id,
        session_starts_at=session_starts_at,
        session_ends_at=session_ends_at,
        arm_id=arm.arm_id,
        arm_sha256=arm.arm_sha256,
        cost_policy_sha256=cost_policy.cost_policy_sha256,
        initial_state_policy_sha256=initial_state_policy_sha256,
        initial_state_sha256=initial_state_sha256,
        capture_identity_sha256=binding.identity_sha256,
        final_capture_seal_sha256=binding.final_capture_seal_sha256,
        manifest_sha256=binding.manifest_sha256,
        complete_session_root_sha256=complete_session_root_sha256,
        release_order_root_sha256=binding.release_order_root_sha256,
        run_binding_sha256=binding.run_binding_sha256,
        execution_receipt_sha256=_sha256_json(
            {
                "schema_version": "chili.replay-v3-private-execution-receipt-binding.v1",
                "run_binding_sha256": binding.run_binding_sha256,
                "binding": binding.to_dict(),
            }
        ),
        os_attestation_sha256=_sha256_json(attestation.to_dict()),
        decisions_root_sha256=decisions_root_sha256,
        intents_root_sha256=intents_root_sha256,
        risk_root_sha256=risk_root_sha256,
        fills_root_sha256=fills_root_sha256,
        fees_root_sha256=fees_root_sha256,
        equity_root_sha256=equity_root_sha256,
        quote_path_root_sha256=quote_path_root_sha256,
        risk_budget_usd=risk_budget_usd,
        trades=tuple(trades),
        _authority_token=_LEDGER_TOKEN,
    )
    _record_private_authority(
        ledger,
        kind="diagnostic_replay_ledger",
        digest=ledger.ledger_sha256,
    )
    return ledger


def _issue_replay_v3_benchmark_ledger(*args: Any, **kwargs: Any) -> None:
    """Refuse the obsolete authority-like name fail closed.

    A reproduction receipt cannot bind a changed candidate's orders, fills,
    fees, equity path, or quote path.  Callers that only need synthetic metric
    mechanics must use ``_issue_diagnostic_replay_v3_benchmark_ledger`` and
    retain the global counterfactual-authority blocker.
    """

    del args, kwargs
    raise OosScoreboardError(
        "ReplayV3 reproduction is not counterfactual benchmark authority"
    )


def _issue_unavailable_replay_v3_ledger(
    *, session_id: str, arm: ArmProvenanceV1, blockers: Sequence[str]
) -> UnavailableReplayV3LedgerV1:
    ledger = UnavailableReplayV3LedgerV1(
        session_id=session_id,
        arm_id=arm.arm_id,
        arm_sha256=arm.arm_sha256,
        blockers=tuple(blockers),
        _authority_token=_LEDGER_TOKEN,
    )
    _record_private_authority(
        ledger,
        kind="unavailable_replay_ledger",
        digest=ledger.ledger_sha256,
    )
    return ledger


@dataclass(frozen=True)
class CertifiedAfterFactLabelV1:
    """A CERTIFIABLE grading label joined only after both arm runs exist."""

    label_id: str
    session_id: str
    symbol: str
    starts_at: datetime
    ends_at: datetime
    expected_action: Literal["trade", "reject"]
    cohort: Literal["ross_transferable", "negative_control"]
    ross_authority_sha256: str
    label_evidence_sha256: str
    _authority_token: Any = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority_token is not _LABEL_TOKEN:
            raise OosScoreboardError("label lacks trusted CERTIFIABLE authority")
        object.__setattr__(self, "label_id", _name(self.label_id, "label_id"))
        object.__setattr__(self, "session_id", _name(self.session_id, "session_id"))
        object.__setattr__(self, "symbol", _name(self.symbol, "symbol").upper())
        object.__setattr__(self, "starts_at", _aware(self.starts_at, "starts_at"))
        object.__setattr__(self, "ends_at", _aware(self.ends_at, "ends_at"))
        if self.ends_at < self.starts_at:
            raise OosScoreboardError("label window is inverted")
        if self.expected_action not in {"trade", "reject"}:
            raise OosScoreboardError("label action is invalid")
        if self.cohort not in {"ross_transferable", "negative_control"}:
            raise OosScoreboardError("label cohort is invalid")
        if self.cohort == "ross_transferable" and self.expected_action != "trade":
            raise OosScoreboardError("Ross transferable label must expect a trade")
        if self.cohort == "negative_control" and self.expected_action != "reject":
            raise OosScoreboardError("negative control must expect rejection")
        object.__setattr__(self, "ross_authority_sha256", _digest(self.ross_authority_sha256, "ross_authority_sha256"))
        object.__setattr__(self, "label_evidence_sha256", _digest(self.label_evidence_sha256, "label_evidence_sha256"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "chili.certified-after-fact-label.v1",
            "label_id": self.label_id,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "expected_action": self.expected_action,
            "cohort": self.cohort,
            "ross_authority_sha256": self.ross_authority_sha256,
            "label_evidence_sha256": self.label_evidence_sha256,
        }


def _issue_certified_after_fact_label(
    *,
    label_id: str,
    session_id: str,
    symbol: str,
    starts_at: datetime,
    ends_at: datetime,
    expected_action: Literal["trade", "reject"],
    cohort: Literal["ross_transferable", "negative_control"],
    ross_authority_sha256: str,
    label_evidence_sha256: str,
) -> CertifiedAfterFactLabelV1:
    """Private seam for CHILI's sealed authoritative label grader."""

    label = CertifiedAfterFactLabelV1(
        label_id,
        session_id,
        symbol,
        starts_at,
        ends_at,
        expected_action,
        cohort,
        ross_authority_sha256,
        label_evidence_sha256,
        _LABEL_TOKEN,
    )
    _record_private_authority(
        label,
        kind="certified_label",
        digest=_sha256_json(label.to_dict()),
    )
    return label


@dataclass(frozen=True)
class ArmMetricsV1:
    trade_count: int
    net_pnl_usd: float
    net_pnl_r: float
    expectancy_usd: float | None
    expectancy_r: float | None
    profit_factor: float | None
    max_drawdown_usd: float
    max_drawdown_r: float
    mfe_capture_ratio: float | None
    mfe_giveback_usd: float
    mfe_giveback_fraction: float | None
    missed_winners: int
    false_positives: int
    worst_loss_r: float
    risk_utilization: float

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class PairedSessionRowV1:
    session_id: str
    fold_id: str
    status: Literal["complete", "unavailable"]
    blockers: tuple[str, ...]
    baseline_ledger_sha256: str | None
    candidate_ledger_sha256: str | None
    joined_label_set_sha256: str | None
    baseline: ArmMetricsV1 | None
    candidate: ArmMetricsV1 | None
    net_pnl_usd_delta: float | None
    net_pnl_r_delta: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "fold_id": self.fold_id,
            "status": self.status,
            "blockers": list(self.blockers),
            "baseline_ledger_sha256": self.baseline_ledger_sha256,
            "candidate_ledger_sha256": self.candidate_ledger_sha256,
            "joined_label_set_sha256": self.joined_label_set_sha256,
            "baseline": None if self.baseline is None else self.baseline.to_dict(),
            "candidate": None if self.candidate is None else self.candidate.to_dict(),
            "net_pnl_usd_delta": self.net_pnl_usd_delta,
            "net_pnl_r_delta": self.net_pnl_r_delta,
        }


@dataclass(frozen=True)
class FoldScoreV1:
    fold_id: str
    status: Literal["complete", "unavailable"]
    baseline_net_pnl_usd: float | None
    candidate_net_pnl_usd: float | None
    delta_usd: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold_id": self.fold_id,
            "status": self.status,
            "baseline_net_pnl_usd": self.baseline_net_pnl_usd,
            "candidate_net_pnl_usd": self.candidate_net_pnl_usd,
            "delta_usd": self.delta_usd,
        }


@dataclass(frozen=True)
class OosScoreboardV1:
    """Serializable report; private fields alone can authorize gate recomputation."""

    schema_version: ClassVar[str] = "chili.paired-oos-scoreboard.v1"
    plan_sha256: str
    registration_receipt_sha256: str
    status: Literal["unavailable", "rejected", "accepted"]
    rows: tuple[PairedSessionRowV1, ...]
    folds: tuple[FoldScoreV1, ...]
    baseline: ArmMetricsV1 | None
    candidate: ArmMetricsV1 | None
    mean_session_net_pnl_delta_usd: float | None
    mean_session_net_pnl_delta_r: float | None
    bootstrap_lower_delta_usd: float | None
    bootstrap_lower_delta_r: float | None
    global_blockers: tuple[str, ...]
    passed_rules: tuple[str, ...]
    failed_rules: tuple[str, ...]
    _registered_plan: RegisteredOosStudyPlanV1 | None = field(repr=False, compare=False)
    _source_ledgers: tuple[TrustedLedger, ...] = field(repr=False, compare=False)
    _source_labels: tuple[CertifiedAfterFactLabelV1, ...] = field(repr=False, compare=False)
    _authority_token: Any = field(repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "plan_sha256": self.plan_sha256,
            "registration_receipt_sha256": self.registration_receipt_sha256,
            "status": self.status,
            "rows": [value.to_dict() for value in self.rows],
            "folds": [value.to_dict() for value in self.folds],
            "baseline": None if self.baseline is None else self.baseline.to_dict(),
            "candidate": None if self.candidate is None else self.candidate.to_dict(),
            "mean_session_net_pnl_delta_usd": self.mean_session_net_pnl_delta_usd,
            "mean_session_net_pnl_delta_r": self.mean_session_net_pnl_delta_r,
            "bootstrap_lower_delta_usd": self.bootstrap_lower_delta_usd,
            "bootstrap_lower_delta_r": self.bootstrap_lower_delta_r,
            "global_blockers": list(self.global_blockers),
            "passed_rules": list(self.passed_rules),
            "failed_rules": list(self.failed_rules),
        }
        payload["scoreboard_sha256"] = _sha256_json(payload)
        return payload

    @property
    def scoreboard_sha256(self) -> str:
        return str(self.to_dict()["scoreboard_sha256"])


@dataclass(frozen=True)
class OosGateReceipt:
    plan_sha256: str
    registration_receipt_sha256: str
    scoreboard_sha256: str
    passed_rules: tuple[str, ...]
    _authority_token: Any = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if _GATE_TOKEN is None or self._authority_token is not _GATE_TOKEN:
            raise OosScoreboardError(
                "OOS gate receipt authority integration is unavailable"
            )
        for attr in ("plan_sha256", "registration_receipt_sha256", "scoreboard_sha256"):
            object.__setattr__(self, attr, _digest(getattr(self, attr), attr))
        if not self.passed_rules:
            raise OosScoreboardError("accepted gate receipt must bind passed rules")

    @property
    def receipt_sha256(self) -> str:
        return _sha256_json(
            {
                "plan_sha256": self.plan_sha256,
                "registration_receipt_sha256": self.registration_receipt_sha256,
                "scoreboard_sha256": self.scoreboard_sha256,
                "passed_rules": list(self.passed_rules),
            }
        )


def _drawdown(values: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def _labels_for_session(
    labels: Sequence[CertifiedAfterFactLabelV1], session_id: str
) -> tuple[CertifiedAfterFactLabelV1, ...]:
    return tuple(value for value in labels if value.session_id == session_id)


def _metrics(
    ledgers: Sequence[ReplayV3BenchmarkLedgerV1],
    labels: Sequence[CertifiedAfterFactLabelV1],
) -> ArmMetricsV1:
    ordered_ledgers = sorted(ledgers, key=lambda value: (value.session_starts_at, value.session_id))
    trades = sorted(
        (trade for ledger in ordered_ledgers for trade in ledger.trades),
        key=lambda value: (value.entry_at, value.exit_at, value.trade_id),
    )
    net_usd = [value.net_pnl_usd for value in trades]
    net_r = [value.net_pnl_r for value in trades]
    gains = sum(value for value in net_usd if value > 0)
    losses = -sum(value for value in net_usd if value < 0)
    total_mfe = sum(value.executable_mfe_profit_usd for value in trades)
    captured = sum(
        max(0.0, min(value.net_pnl_usd, value.executable_mfe_profit_usd))
        for value in trades
    )
    giveback = sum(max(0.0, value.executable_mfe_profit_usd - value.net_pnl_usd) for value in trades)
    by_session = {value.session_id: value for value in ordered_ledgers}
    missed = 0
    false_positive = 0
    for label in labels:
        ledger = by_session.get(label.session_id)
        if ledger is None:
            continue
        matches = [
            trade
            for trade in ledger.trades
            if trade.symbol == label.symbol and label.starts_at <= trade.entry_at <= label.ends_at
        ]
        if label.expected_action == "trade" and not matches:
            missed += 1
        elif label.expected_action == "reject" and matches:
            false_positive += 1
    total_budget = sum(value.risk_budget_usd for value in ordered_ledgers)
    used_risk = sum(value.planned_risk_usd for value in trades)
    return ArmMetricsV1(
        trade_count=len(trades),
        net_pnl_usd=sum(net_usd),
        net_pnl_r=sum(net_r),
        expectancy_usd=None if not trades else sum(net_usd) / len(trades),
        expectancy_r=None if not trades else sum(net_r) / len(trades),
        profit_factor=None if losses == 0 else gains / losses,
        max_drawdown_usd=_drawdown(net_usd),
        max_drawdown_r=_drawdown(net_r),
        mfe_capture_ratio=None if total_mfe == 0 else captured / total_mfe,
        mfe_giveback_usd=giveback,
        mfe_giveback_fraction=None if total_mfe == 0 else giveback / total_mfe,
        missed_winners=missed,
        false_positives=false_positive,
        worst_loss_r=max((max(0.0, -value) for value in net_r), default=0.0),
        risk_utilization=0.0 if total_budget == 0 else used_risk / total_budget,
    )


def _bootstrap_lower(values: Sequence[float], *, rules: AcceptanceRulesV1) -> float | None:
    if not values:
        return None
    generator = random.Random(rules.random_seed)
    size = len(values)
    samples = [
        sum(values[generator.randrange(size)] for _ in range(size)) / size
        for _ in range(rules.bootstrap_resamples)
    ]
    samples.sort()
    index = max(0, min(len(samples) - 1, math.floor((1.0 - rules.confidence_level) * len(samples))))
    return samples[index]


def _pair_blockers(
    baseline: ReplayV3BenchmarkLedgerV1,
    candidate: ReplayV3BenchmarkLedgerV1,
    *,
    window: SessionWindowV1,
    plan: OosStudyPlanV1,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if baseline.arm_sha256 != plan.baseline_arm.arm_sha256:
        blockers.append("baseline_arm_provenance_mismatch")
    if candidate.arm_sha256 != plan.candidate_arm.arm_sha256:
        blockers.append("candidate_arm_provenance_mismatch")
    if baseline.cost_policy_sha256 != plan.cost_policy.cost_policy_sha256 or candidate.cost_policy_sha256 != plan.cost_policy.cost_policy_sha256:
        blockers.append("cost_policy_mismatch")
    if (
        baseline.initial_state_policy_sha256 != plan.start_state_policy_sha256
        or candidate.initial_state_policy_sha256 != plan.start_state_policy_sha256
    ):
        blockers.append("start_state_policy_mismatch")
    if (
        baseline.session_starts_at != window.starts_at
        or candidate.session_starts_at != window.starts_at
        or baseline.session_ends_at != window.ends_at
        or candidate.session_ends_at != window.ends_at
    ):
        blockers.append("planned_session_window_mismatch")
    exact = (
        "capture_identity_sha256",
        "final_capture_seal_sha256",
        "manifest_sha256",
        "complete_session_root_sha256",
        "release_order_root_sha256",
        "initial_state_policy_sha256",
        "initial_state_sha256",
        "cost_policy_sha256",
        "quote_path_root_sha256",
    )
    blockers.extend(
        f"paired_{attr}_mismatch"
        for attr in exact
        if getattr(baseline, attr) != getattr(candidate, attr)
    )
    return tuple(dict.fromkeys(blockers))


def _evaluate_rules(
    *,
    rules: AcceptanceRulesV1,
    rows: Sequence[PairedSessionRowV1],
    folds: Sequence[FoldScoreV1],
    baseline: ArmMetricsV1 | None,
    candidate: ArmMetricsV1 | None,
    mean_delta_usd: float | None,
    mean_delta_r: float | None,
    lower_usd: float | None,
    lower_r: float | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    outcomes: dict[str, bool] = {}
    complete_rows = [value for value in rows if value.status == "complete"]
    complete_folds = [value for value in folds if value.status == "complete"]
    outcomes["sample.minimum_paired_sessions"] = len(complete_rows) >= rules.minimum_paired_sessions
    outcomes["sample.minimum_complete_folds"] = len(complete_folds) >= rules.minimum_complete_folds
    if baseline is None or candidate is None:
        for name in (
            "absolute.net_pnl_usd",
            "absolute.expectancy_r",
            "absolute.profit_factor_defined",
            "absolute.profit_factor",
            "paired.mean_delta_usd",
            "paired.mean_delta_r",
            "paired.bootstrap_lower_usd",
            "paired.bootstrap_lower_r",
            "risk.drawdown",
            "risk.drawdown_noninferiority",
            "risk.worst_loss",
            "risk.worst_loss_noninferiority",
            "risk.utilization",
            "ross.missed_winners",
            "ross.missed_winner_noninferiority",
            "control.false_positives",
            "control.false_positive_noninferiority",
            "exit.mfe_capture",
            "exit.mfe_capture_superiority",
            "exit.giveback",
            "exit.giveback_noninferiority",
            "fold.positive",
            "fold.superior",
        ):
            outcomes[name] = False
    else:
        outcomes["absolute.net_pnl_usd"] = candidate.net_pnl_usd >= rules.minimum_candidate_net_pnl_usd
        outcomes["absolute.expectancy_r"] = candidate.expectancy_r is not None and candidate.expectancy_r >= rules.minimum_candidate_expectancy_r
        outcomes["absolute.profit_factor_defined"] = candidate.profit_factor is not None
        outcomes["absolute.profit_factor"] = candidate.profit_factor is not None and candidate.profit_factor >= rules.minimum_candidate_profit_factor
        outcomes["paired.mean_delta_usd"] = mean_delta_usd is not None and mean_delta_usd >= rules.minimum_mean_session_net_pnl_delta_usd
        outcomes["paired.mean_delta_r"] = mean_delta_r is not None and mean_delta_r >= rules.minimum_mean_session_net_pnl_delta_r
        outcomes["paired.bootstrap_lower_usd"] = lower_usd is not None and lower_usd >= rules.minimum_bootstrap_lower_delta_usd
        outcomes["paired.bootstrap_lower_r"] = lower_r is not None and lower_r >= rules.minimum_bootstrap_lower_delta_r
        outcomes["risk.drawdown"] = candidate.max_drawdown_r <= rules.maximum_candidate_drawdown_r
        outcomes["risk.drawdown_noninferiority"] = candidate.max_drawdown_r - baseline.max_drawdown_r <= rules.maximum_drawdown_r_increase
        outcomes["risk.worst_loss"] = candidate.worst_loss_r <= rules.maximum_candidate_worst_loss_r
        outcomes["risk.worst_loss_noninferiority"] = candidate.worst_loss_r - baseline.worst_loss_r <= rules.maximum_worst_loss_r_increase
        outcomes["risk.utilization"] = rules.minimum_risk_utilization <= candidate.risk_utilization <= rules.maximum_risk_utilization
        outcomes["ross.missed_winners"] = candidate.missed_winners <= rules.maximum_candidate_missed_winners
        outcomes["ross.missed_winner_noninferiority"] = candidate.missed_winners - baseline.missed_winners <= rules.maximum_missed_winner_delta
        outcomes["control.false_positives"] = candidate.false_positives <= rules.maximum_candidate_false_positives
        outcomes["control.false_positive_noninferiority"] = candidate.false_positives - baseline.false_positives <= rules.maximum_false_positive_delta
        outcomes["exit.mfe_capture"] = candidate.mfe_capture_ratio is not None and candidate.mfe_capture_ratio >= rules.minimum_candidate_mfe_capture_ratio
        outcomes["exit.mfe_capture_superiority"] = candidate.mfe_capture_ratio is not None and baseline.mfe_capture_ratio is not None and candidate.mfe_capture_ratio - baseline.mfe_capture_ratio >= rules.minimum_mfe_capture_ratio_delta
        outcomes["exit.giveback"] = candidate.mfe_giveback_fraction is not None and candidate.mfe_giveback_fraction <= rules.maximum_candidate_giveback_fraction
        outcomes["exit.giveback_noninferiority"] = candidate.mfe_giveback_fraction is not None and baseline.mfe_giveback_fraction is not None and candidate.mfe_giveback_fraction - baseline.mfe_giveback_fraction <= rules.maximum_giveback_fraction_delta
        positive = sum((value.candidate_net_pnl_usd or 0.0) > 0 for value in complete_folds)
        superior = sum((value.delta_usd or 0.0) >= rules.minimum_fold_net_pnl_delta_usd for value in complete_folds)
        outcomes["fold.positive"] = positive >= rules.minimum_candidate_positive_folds
        outcomes["fold.superior"] = superior >= rules.minimum_candidate_superior_folds
    passed = tuple(name for name, result in outcomes.items() if result)
    failed = tuple(name for name, result in outcomes.items() if not result)
    return passed, failed


def _compute_scoreboard(
    registered: RegisteredOosStudyPlanV1,
    ledgers: Sequence[TrustedLedger],
    labels: Sequence[CertifiedAfterFactLabelV1],
) -> dict[str, Any]:
    if (
        registered._authority_token is not _PLAN_TOKEN
        or not _has_private_authority(
            registered,
            kind="registered_plan",
            digest=_sha256_json(registered.to_dict()),
        )
    ):
        raise OosScoreboardError("study registration authority is invalid")
    plan = registered.plan
    windows = {value.session_id: value for value in plan.session_windows}
    fold_by_test = {value: fold.fold_id for fold in plan.folds for value in fold.test_session_ids}
    # These are architectural facts, not caller-configurable policy flags.
    # The private helpers below exercise deterministic mechanics, but their
    # timestamps, secondary roots, trade rows, and label digests are supplied
    # by the caller.  Until the owning subsystems mint receipts that bind those
    # exact values, no in-process object from this module may become promotion
    # authority.  Keep the report useful for diagnostics while making a false
    # acceptance categorically impossible.
    global_blockers: list[str] = [
        "append_only_prospective_registration_receipt_unavailable",
        "sealed_replay_v3_counterfactual_ledger_receipt_unavailable",
        "sealed_replay_v3_reproduction_receipt_not_counterfactual_authority",
        "authoritative_after_fact_label_receipt_unavailable",
    ]

    ledger_by_key: dict[tuple[str, str], TrustedLedger] = {}
    for ledger in ledgers:
        if (
            not isinstance(
                ledger,
                (ReplayV3BenchmarkLedgerV1, UnavailableReplayV3LedgerV1),
            )
            or ledger._authority_token is not _LEDGER_TOKEN
            or not _has_private_authority(
                ledger,
                kind=(
                    "diagnostic_replay_ledger"
                    if isinstance(ledger, ReplayV3BenchmarkLedgerV1)
                    else "unavailable_replay_ledger"
                ),
                digest=ledger.ledger_sha256,
            )
        ):
            global_blockers.append("untrusted_replay_ledger")
            continue
        key = (ledger.session_id, ledger.arm_id)
        if key in ledger_by_key:
            global_blockers.append(f"duplicate_ledger:{ledger.session_id}:{ledger.arm_id}")
        else:
            ledger_by_key[key] = ledger
        if ledger.session_id not in plan.test_session_ids:
            global_blockers.append(f"unplanned_ledger_session:{ledger.session_id}")
        if ledger.arm_id not in {plan.baseline_arm.arm_id, plan.candidate_arm.arm_id}:
            global_blockers.append(f"unplanned_ledger_arm:{ledger.arm_id}")

    trusted_labels: list[CertifiedAfterFactLabelV1] = []
    seen_labels: set[str] = set()
    for label in labels:
        if (
            not isinstance(label, CertifiedAfterFactLabelV1)
            or label._authority_token is not _LABEL_TOKEN
            or not _has_private_authority(
                label,
                kind="certified_label",
                digest=_sha256_json(label.to_dict()),
            )
        ):
            global_blockers.append("untrusted_after_fact_label")
            continue
        if label.label_id in seen_labels:
            global_blockers.append(f"duplicate_label:{label.label_id}")
            continue
        seen_labels.add(label.label_id)
        if label.ross_authority_sha256 != plan.ross_authority_sha256:
            global_blockers.append(f"ross_authority_mismatch:{label.label_id}")
            continue
        if label.session_id not in plan.test_session_ids:
            global_blockers.append(f"label_outside_test_plan:{label.label_id}")
            continue
        window = windows[label.session_id]
        if label.starts_at < window.starts_at or label.ends_at > window.ends_at:
            global_blockers.append(f"label_outside_complete_session:{label.label_id}")
            continue
        trusted_labels.append(label)
    for index, left in enumerate(trusted_labels):
        for right in trusted_labels[index + 1 :]:
            if (
                left.session_id == right.session_id
                and left.symbol == right.symbol
                and left.starts_at <= right.ends_at
                and right.starts_at <= left.ends_at
            ):
                global_blockers.append(f"overlapping_label_windows:{left.label_id}:{right.label_id}")
    trusted_ross_count = sum(value.cohort == "ross_transferable" for value in trusted_labels)
    if plan.ross_certifiable_count == 0 or trusted_ross_count == 0:
        # The currently verified Ross authority (0/4/2/6) necessarily lands here,
        # even if a caller tries to attach a synthetic label to its digest.
        global_blockers.append("ross_certifiable_labels_unavailable")
    if trusted_ross_count > plan.ross_certifiable_count:
        global_blockers.append("ross_certifiable_label_count_exceeds_authority")
    if not any(value.cohort == "negative_control" for value in trusted_labels):
        global_blockers.append("negative_control_labels_unavailable")

    rows: list[PairedSessionRowV1] = []
    paired_baseline: list[ReplayV3BenchmarkLedgerV1] = []
    paired_candidate: list[ReplayV3BenchmarkLedgerV1] = []
    paired_labels: list[CertifiedAfterFactLabelV1] = []
    for session_id in plan.test_session_ids:
        baseline = ledger_by_key.get((session_id, plan.baseline_arm.arm_id))
        candidate = ledger_by_key.get((session_id, plan.candidate_arm.arm_id))
        baseline_ledger_sha256 = None if baseline is None else baseline.ledger_sha256
        candidate_ledger_sha256 = None if candidate is None else candidate.ledger_sha256
        blockers: list[str] = []
        if baseline is None:
            blockers.append("baseline_ledger_missing")
        elif isinstance(baseline, UnavailableReplayV3LedgerV1):
            blockers.extend(f"baseline:{value}" for value in baseline.blockers)
        if candidate is None:
            blockers.append("candidate_ledger_missing")
        elif isinstance(candidate, UnavailableReplayV3LedgerV1):
            blockers.extend(f"candidate:{value}" for value in candidate.blockers)
        if isinstance(baseline, ReplayV3BenchmarkLedgerV1) and isinstance(candidate, ReplayV3BenchmarkLedgerV1):
            blockers.extend(_pair_blockers(baseline, candidate, window=windows[session_id], plan=plan))
        if blockers:
            rows.append(
                PairedSessionRowV1(
                    session_id,
                    fold_by_test[session_id],
                    "unavailable",
                    tuple(dict.fromkeys(blockers)),
                    baseline_ledger_sha256,
                    candidate_ledger_sha256,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
            continue
        if not isinstance(baseline, ReplayV3BenchmarkLedgerV1) or not isinstance(
            candidate, ReplayV3BenchmarkLedgerV1
        ):
            # Defensive explicit check: this remains fail-closed under ``python -O``.
            rows.append(
                PairedSessionRowV1(
                    session_id,
                    fold_by_test[session_id],
                    "unavailable",
                    ("paired_ledger_authority_unreachable",),
                    baseline_ledger_sha256,
                    candidate_ledger_sha256,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
            continue
        session_labels = _labels_for_session(trusted_labels, session_id)
        joined_label_set_sha256 = _sha256_json(
            {
                "labels": [
                    value.to_dict()
                    for value in sorted(session_labels, key=lambda item: item.label_id)
                ]
            }
        )
        baseline_metrics = _metrics((baseline,), session_labels)
        candidate_metrics = _metrics((candidate,), session_labels)
        paired_baseline.append(baseline)
        paired_candidate.append(candidate)
        paired_labels.extend(session_labels)
        rows.append(
            PairedSessionRowV1(
                session_id,
                fold_by_test[session_id],
                "complete",
                (),
                baseline.ledger_sha256,
                candidate.ledger_sha256,
                joined_label_set_sha256,
                baseline_metrics,
                candidate_metrics,
                candidate_metrics.net_pnl_usd - baseline_metrics.net_pnl_usd,
                candidate_metrics.net_pnl_r - baseline_metrics.net_pnl_r,
            )
        )

    fold_scores: list[FoldScoreV1] = []
    for fold in plan.folds:
        fold_rows = [value for value in rows if value.fold_id == fold.fold_id]
        if not fold_rows or any(value.status != "complete" for value in fold_rows):
            fold_scores.append(FoldScoreV1(fold.fold_id, "unavailable", None, None, None))
        else:
            baseline_total = sum(value.baseline.net_pnl_usd for value in fold_rows if value.baseline is not None)
            candidate_total = sum(value.candidate.net_pnl_usd for value in fold_rows if value.candidate is not None)
            fold_scores.append(FoldScoreV1(fold.fold_id, "complete", baseline_total, candidate_total, candidate_total - baseline_total))

    baseline_metrics = _metrics(paired_baseline, paired_labels) if paired_baseline else None
    candidate_metrics = _metrics(paired_candidate, paired_labels) if paired_candidate else None
    usd_deltas = [value.net_pnl_usd_delta for value in rows if value.status == "complete" and value.net_pnl_usd_delta is not None]
    r_deltas = [value.net_pnl_r_delta for value in rows if value.status == "complete" and value.net_pnl_r_delta is not None]
    mean_usd = None if not usd_deltas else sum(usd_deltas) / len(usd_deltas)
    mean_r = None if not r_deltas else sum(r_deltas) / len(r_deltas)
    lower_usd = _bootstrap_lower(usd_deltas, rules=plan.acceptance_rules)
    lower_r = _bootstrap_lower(r_deltas, rules=plan.acceptance_rules)
    passed, failed = _evaluate_rules(
        rules=plan.acceptance_rules,
        rows=rows,
        folds=fold_scores,
        baseline=baseline_metrics,
        candidate=candidate_metrics,
        mean_delta_usd=mean_usd,
        mean_delta_r=mean_r,
        lower_usd=lower_usd,
        lower_r=lower_r,
    )
    if global_blockers or any(value.status != "complete" for value in rows):
        status: Literal["unavailable", "rejected", "accepted"] = "unavailable"
    elif failed:
        status = "rejected"
    else:
        status = "accepted"
    return {
        "plan_sha256": plan.plan_sha256,
        "registration_receipt_sha256": registered.registration_receipt_sha256,
        "status": status,
        "rows": tuple(rows),
        "folds": tuple(fold_scores),
        "baseline": baseline_metrics,
        "candidate": candidate_metrics,
        "mean_session_net_pnl_delta_usd": mean_usd,
        "mean_session_net_pnl_delta_r": mean_r,
        "bootstrap_lower_delta_usd": lower_usd,
        "bootstrap_lower_delta_r": lower_r,
        "global_blockers": tuple(dict.fromkeys(global_blockers)),
        "passed_rules": passed,
        "failed_rules": failed,
    }


def build_paired_oos_scoreboard(
    registered_plan: RegisteredOosStudyPlanV1,
    *,
    ledgers: Sequence[TrustedLedger],
    labels: Sequence[CertifiedAfterFactLabelV1],
) -> OosScoreboardV1:
    """Build a report from private authorities while retaining every test session."""

    values = _compute_scoreboard(registered_plan, tuple(ledgers), tuple(labels))
    scoreboard = OosScoreboardV1(
        **values,
        _registered_plan=registered_plan,
        _source_ledgers=tuple(ledgers),
        _source_labels=tuple(labels),
        _authority_token=_SCOREBOARD_TOKEN,
    )
    _record_private_authority(
        scoreboard,
        kind="scoreboard",
        digest=scoreboard.scoreboard_sha256,
    )
    return scoreboard


def issue_oos_gate_receipt(scoreboard: OosScoreboardV1) -> OosGateReceipt | None:
    """Recompute all rows/rules and mint authority only for the unchanged passing study."""

    if _GATE_TOKEN is None:
        return None
    if (
        not isinstance(scoreboard, OosScoreboardV1)
        or scoreboard._authority_token is not _SCOREBOARD_TOKEN
        or not _has_private_authority(
            scoreboard,
            kind="scoreboard",
            digest=scoreboard.scoreboard_sha256,
        )
        or scoreboard._registered_plan is None
        or scoreboard._registered_plan._authority_token is not _PLAN_TOKEN
        or not _has_private_authority(
            scoreboard._registered_plan,
            kind="registered_plan",
            digest=_sha256_json(scoreboard._registered_plan.to_dict()),
        )
    ):
        return None
    recomputed_values = _compute_scoreboard(
        scoreboard._registered_plan,
        scoreboard._source_ledgers,
        scoreboard._source_labels,
    )
    recomputed = OosScoreboardV1(
        **recomputed_values,
        _registered_plan=None,
        _source_ledgers=(),
        _source_labels=(),
        _authority_token=None,
    )
    if recomputed.to_dict() != scoreboard.to_dict() or recomputed.status != "accepted" or recomputed.failed_rules:
        return None
    receipt = OosGateReceipt(
        plan_sha256=scoreboard.plan_sha256,
        registration_receipt_sha256=scoreboard.registration_receipt_sha256,
        scoreboard_sha256=scoreboard.scoreboard_sha256,
        passed_rules=scoreboard.passed_rules,
        _authority_token=_GATE_TOKEN,
    )
    _record_private_authority(
        receipt,
        kind="gate_receipt",
        digest=receipt.receipt_sha256,
    )
    return receipt


__all__ = [
    "AcceptanceRulesV1",
    "ArmMetricsV1",
    "ArmProvenanceV1",
    "CostPolicyV1",
    "FoldScoreV1",
    "OosFoldV1",
    "OosGateReceipt",
    "OosScoreboardError",
    "OosScoreboardV1",
    "OosStudyPlanV1",
    "PairedSessionRowV1",
    "SessionWindowV1",
    "TradeLedgerRowV1",
    "build_paired_oos_scoreboard",
    "issue_oos_gate_receipt",
]
