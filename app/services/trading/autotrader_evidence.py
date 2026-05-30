"""Evidence hygiene helpers for AutoTrader audit rows.

The helpers are intentionally pure: callers pass already-read rows and receive
clean/quarantined buckets without changing runtime state or database rows.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable


PHASE5K_D_PDT_RUNTIME_WRITE_QUARANTINE = "phase5k_d_pdt_runtime_write_churn_quarantine"

_QUARANTINE_MARKERS = (
    "phase5k_d_pdt_runtime_write_churn_quarantine",
    "phase5k_c_reflip_runtime_recreate",
    "runtime_write_churn",
    "runtime_write_evidence",
    "quarantined_target",
    "pm025",
    "pm057",
    "pm070",
    "pm_025",
    "pm_057",
    "pm_070",
    "incident_evidence",
    "clean_evidence_blocked",
)

_FIELD_NAMES = (
    "decision",
    "reason",
    "management_scope",
    "ticker",
    "rule_snapshot",
    "llm_snapshot",
)


@dataclass(frozen=True)
class AutoTraderEvidenceClassification:
    clean: bool
    reason: str | None = None


@dataclass(frozen=True)
class AutoTraderEvidencePartition:
    clean: list[Any]
    quarantined: list[Any]
    reason_counts: dict[str, int]

    @property
    def clean_count(self) -> int:
        return len(self.clean)

    @property
    def quarantined_count(self) -> int:
        return len(self.quarantined)

    @property
    def total_count(self) -> int:
        return self.clean_count + self.quarantined_count

    def to_summary(self) -> dict[str, Any]:
        return {
            "total_count": self.total_count,
            "clean_count": self.clean_count,
            "quarantined_count": self.quarantined_count,
            "reason_counts": dict(self.reason_counts),
        }


def classify_autotrader_run_evidence(run: Any) -> AutoTraderEvidenceClassification:
    """Return whether an AutoTrader audit row is clean evidence.

    Quarantine markers may live in the explicit reason/decision strings or in
    JSON snapshots produced by governance reports and diagnostics.
    """
    for field_name in _FIELD_NAMES:
        marker = _find_quarantine_marker(getattr(run, field_name, None))
        if marker is not None:
            return AutoTraderEvidenceClassification(clean=False, reason=marker)
    return AutoTraderEvidenceClassification(clean=True)


def partition_autotrader_runs(runs: Iterable[Any]) -> AutoTraderEvidencePartition:
    clean: list[Any] = []
    quarantined: list[Any] = []
    reasons: Counter[str] = Counter()
    for run in runs:
        classification = classify_autotrader_run_evidence(run)
        if classification.clean:
            clean.append(run)
            continue
        quarantined.append(run)
        reasons[classification.reason or "unknown_quarantine"] += 1
    return AutoTraderEvidencePartition(
        clean=clean,
        quarantined=quarantined,
        reason_counts=dict(reasons),
    )


def clean_autotrader_runs(runs: Iterable[Any]) -> list[Any]:
    return partition_autotrader_runs(runs).clean


def _find_quarantine_marker(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = _normalize(value)
        for marker in _QUARANTINE_MARKERS:
            if _normalize(marker) in normalized:
                return marker
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            key_marker = _find_quarantine_marker(str(key))
            if key_marker is not None:
                return key_marker
            item_marker = _find_quarantine_marker(item)
            if item_marker is not None:
                return item_marker
        return None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            marker = _find_quarantine_marker(item)
            if marker is not None:
                return marker
        return None
    return _find_quarantine_marker(str(value))


def _normalize(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())
