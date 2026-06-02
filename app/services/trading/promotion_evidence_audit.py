"""Promotion-evidence completeness audit (Codex stabilization plan #6).

Background
----------
Codex's audit on 2026-04-27 reported that of 31 patterns with
``promotion_status='promoted'``, 20 were missing OOS win rate, 18 had
zero OOS trades, 30 didn't have ``promotion_gate_passed=true``, and 29
had zero deflated Sharpe. That's near-total absence of the evidence the
promotion gate is supposed to enforce.

Most of those rows are leftover from the legacy ``promotion_status``
column that pre-dates the canonical ``lifecycle_stage`` FSM; the
canonical-column count of "promoted" is much smaller (~10) — but even
within that smaller set, several rows are missing CPCV / deflated Sharpe.

This module:
  * Computes a snapshot of promoted-pattern evidence completeness (per
    both columns, since they don't agree)
  * Logs the summary every run (so operators can track drift)
  * If ``chili_pattern_evidence_auto_demote`` is True, demotes
    evidence-incomplete patterns (lifecycle_stage -> ``challenged``) and
    records the actions

The auto-demote flag is OFF by default. The audit-only mode is safe to
run on a schedule (no side effects). Enabling auto-demote is an
operator-grade decision under the kill-switch / promotion runbook; do
not flip it on without reviewing the report first.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings

logger = logging.getLogger(__name__)
LOG_PREFIX = "[promotion_evidence_audit]"
PROMOTION_EVIDENCE_FEATURE_SCHEMA_VERSION = "promotion_evidence_eligibility_v1"
PROMOTED_PATTERN_AUDIT_EVIDENCE_SOURCE = "promoted_pattern_audit"

_PROVENANCE_ACCEPTED_STATES = frozenset({"accepted", "complete", "verified"})
_QUARANTINE_CLEAR_STATES = frozenset({"clear", "none", "not_quarantined", "inactive"})
_BROKER_TRUTH_ACCEPTED_STATES = frozenset(
    {"accepted", "authoritative", "broker_accepted", "verified"}
)
_LIVE_FALLBACK_BLOCKER_FIELDS = (
    "broker_reconcile_no_exit_price",
    "missing_stop",
    "qty_drift",
    "over_fill",
    "unprotected_position",
    "pm070_protective_order_blocker",
    "protective_order_blocker",
    "runtime_source_trust_blocker",
    "source_trust_blocker",
)
_REQUIRED_CLASSIFIER_METADATA = (
    "code_version",
    "feature_schema_version",
    "backtest_result_id",
)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _state_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "blocked"}


def _row_or_default(row: Any, key: str, default: Any) -> Any:
    value = _row_get(row, key)
    return value if _has_value(value) else default


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _family_trial_value(row: Any, default: Any) -> int | None:
    for key in ("family_trial_burden", "n_effective_trials", "family_size"):
        value = _row_get(row, key)
        parsed = _nonnegative_int(value)
        if parsed is not None:
            return parsed
    return _nonnegative_int(default)


def classify_promotion_evidence_rows(
    rows: list[Any] | tuple[Any, ...],
    *,
    evidence_source: str | None = None,
    broker_truth_state: str | None = None,
    quarantine_state: str | None = None,
    provenance_status: str | None = None,
    code_version: str | None = None,
    feature_schema_version: str | None = PROMOTION_EVIDENCE_FEATURE_SCHEMA_VERSION,
    backtest_result_id: Any = None,
    family_trial_burden: Any = None,
    owner_override: bool = False,
) -> dict[str, Any]:
    """Pure PM-20260601-136 raw-to-eligible promotion evidence classifier.

    This helper does not read the database and does not mutate promotion
    state. It turns row-like evidence into an auditable classifier packet
    before statistical gates consume realized rows.
    """
    row_list = list(rows or [])
    warnings: list[str] = []
    excluded_rows_by_reason: dict[str, int] = {}
    row_results: list[dict[str, Any]] = []
    eligible_rows = 0

    def _add_reason(reasons: list[str], reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)
            excluded_rows_by_reason[reason] = excluded_rows_by_reason.get(reason, 0) + 1

    def _single_row_value_or_default(key: str, default: Any) -> Any:
        if _has_value(default):
            return default
        raw_values = [
            _row_get(row, key)
            for row in row_list
            if _has_value(_row_get(row, key))
        ]
        if len({str(value) for value in raw_values}) == 1:
            return raw_values[0]
        return default

    effective_code_version = _single_row_value_or_default("code_version", code_version)
    effective_feature_schema_version = _single_row_value_or_default(
        "feature_schema_version",
        feature_schema_version,
    )
    effective_backtest_result_id = _single_row_value_or_default(
        "backtest_result_id",
        backtest_result_id,
    )

    effective_metadata = {
        "code_version": effective_code_version,
        "feature_schema_version": effective_feature_schema_version,
        "backtest_result_id": effective_backtest_result_id,
    }
    required_missing = [
        key for key in _REQUIRED_CLASSIFIER_METADATA if not _has_value(effective_metadata[key])
    ]

    def _distinct_values(key: str, default: Any) -> set[str]:
        values: set[str] = set()
        for row in row_list:
            value = _row_or_default(row, key, default)
            if _has_value(value):
                values.add(str(value))
        return values

    mixed_fields: list[str] = []
    for key, default in (
        ("evidence_source", evidence_source),
        ("code_version", effective_code_version),
        ("feature_schema_version", effective_feature_schema_version),
        ("backtest_result_id", effective_backtest_result_id),
    ):
        if len(_distinct_values(key, default)) > 1:
            mixed_fields.append(key)
            warnings.append(f"mixed_{key}")

    max_family_trial_burden = _nonnegative_int(family_trial_burden) or 0
    for row in row_list:
        row_burden = _family_trial_value(row, family_trial_burden)
        if row_burden is not None:
            max_family_trial_burden = max(max_family_trial_burden, row_burden)

    for idx, row in enumerate(row_list):
        reasons: list[str] = []
        if required_missing and not owner_override:
            for key in required_missing:
                _add_reason(reasons, f"missing_{key}")

        row_source = _row_or_default(row, "evidence_source", evidence_source)
        row_broker_truth = _row_or_default(row, "broker_truth_state", broker_truth_state)
        row_quarantine = _row_or_default(row, "quarantine_state", quarantine_state)
        row_provenance = _row_or_default(row, "provenance_status", provenance_status)
        row_code_version = _row_or_default(row, "code_version", effective_code_version)
        row_feature_schema = _row_or_default(
            row,
            "feature_schema_version",
            effective_feature_schema_version,
        )
        row_backtest_id = _row_or_default(
            row,
            "backtest_result_id",
            effective_backtest_result_id,
        )

        if not _has_value(row_source):
            _add_reason(reasons, "missing_evidence_source")
        if _state_key(row_provenance) not in _PROVENANCE_ACCEPTED_STATES:
            _add_reason(reasons, "provenance_not_accepted")
        if _state_key(row_quarantine) not in _QUARANTINE_CLEAR_STATES:
            _add_reason(reasons, "quarantine_not_clear")
        if _row_get(row, "research_integrity") is False or _state_key(
            _row_get(row, "research_integrity")
        ) in {"failed", "fail", "false", "0"}:
            _add_reason(reasons, "research_integrity_failed")
        if not _has_value(row_code_version):
            _add_reason(reasons, "missing_code_version")
        if not _has_value(row_feature_schema):
            _add_reason(reasons, "missing_feature_schema_version")
        if not _has_value(row_backtest_id):
            _add_reason(reasons, "missing_backtest_result_id")
        for field in mixed_fields:
            _add_reason(reasons, f"mixed_{field}")

        is_live_fallback = _truthy(_row_get(row, "live_fallback")) or _state_key(
            row_source
        ) in {"live_fallback", "live-fallback", "broker_live_fallback"}
        if is_live_fallback:
            if _state_key(row_broker_truth) not in _BROKER_TRUTH_ACCEPTED_STATES:
                _add_reason(reasons, "broker_truth_not_accepted")
            for blocker in _LIVE_FALLBACK_BLOCKER_FIELDS:
                if _truthy(_row_get(row, blocker)):
                    _add_reason(reasons, blocker)

        eligible = not reasons
        if eligible:
            eligible_rows += 1
        row_results.append(
            {
                "row_index": idx,
                "eligible": eligible,
                "excluded_reasons": reasons,
                "evidence_source": row_source,
                "broker_truth_state": row_broker_truth,
                "quarantine_state": row_quarantine,
                "provenance_status": row_provenance,
                "code_version": row_code_version,
                "feature_schema_version": row_feature_schema,
                "backtest_result_id": row_backtest_id,
                "family_trial_burden": _family_trial_value(row, family_trial_burden),
            }
        )

    return {
        "raw_rows": len(row_list),
        "eligible_rows": eligible_rows,
        "excluded_rows_by_reason": excluded_rows_by_reason,
        "warnings": warnings,
        "evidence_source": evidence_source,
        "broker_truth_state": broker_truth_state,
        "quarantine_state": quarantine_state,
        "provenance_status": provenance_status,
        "required_metadata_missing": required_missing,
        "code_version": effective_code_version,
        "feature_schema_version": effective_feature_schema_version,
        "backtest_result_id": effective_backtest_result_id,
        "family_trial_burden": max_family_trial_burden,
        "owner_override": bool(owner_override),
        "rows": row_results,
    }


def _promotion_audit_classifier_rows(
    rows: list[Any] | tuple[Any, ...],
) -> list[dict[str, Any]]:
    """Map promoted-pattern audit rows into conservative classifier inputs.

    The legacy audit table scan only proves whether coarse OOS/CPCV fields
    are present on ``scan_patterns``. It does not prove provenance,
    quarantine clearance, code version, or backtest identity. Keep that
    distinction explicit so the audit report cannot accidentally promote
    row counts as statistically eligible evidence.
    """
    classifier_rows: list[dict[str, Any]] = []
    for row in rows or []:
        classifier_row: dict[str, Any] = {
            "evidence_source": PROMOTED_PATTERN_AUDIT_EVIDENCE_SOURCE,
            "broker_truth_state": "not_applicable",
            "feature_schema_version": PROMOTION_EVIDENCE_FEATURE_SCHEMA_VERSION,
        }
        row_id = _row_get(row, "id")
        if _has_value(row_id):
            classifier_row["pattern_id"] = row_id
        classifier_rows.append(classifier_row)
    return classifier_rows


def _criteria() -> dict[str, str]:
    """Operator-readable description of what 'evidence-complete' means."""
    return {
        "oos_win_rate": "must be IS NOT NULL",
        "oos_trade_count": "must be > 0",
        "promotion_gate_passed": "must be TRUE",
        "deflated_sharpe": "must be IS NOT NULL",
        "cpcv_median_sharpe": "must be IS NOT NULL",
    }


def audit_promoted_pattern_evidence(db: Session) -> dict[str, Any]:
    """Return a snapshot of promoted-pattern evidence completeness.

    Pure-read; never mutates. ``run_promotion_evidence_audit`` wraps this
    and adds the optional auto-demote action when the env flag is set.
    """
    audited_at = datetime.utcnow().isoformat()

    # Both column conventions (canonical + legacy) so we can show drift.
    promoted_lifecycle = db.execute(
        text(
            "SELECT count(*) FROM scan_patterns "
            "WHERE lifecycle_stage IN ('promoted', 'live')"
        )
    ).scalar() or 0
    promoted_legacy = db.execute(
        text(
            "SELECT count(*) FROM scan_patterns "
            "WHERE promotion_status = 'promoted'"
        )
    ).scalar() or 0

    # The audit set is the UNION of both — anything currently treated as
    # "promoted" by either convention should have evidence on file.
    rows = db.execute(
        text(
            """
            SELECT
                id,
                name,
                lifecycle_stage,
                promotion_status,
                oos_win_rate,
                oos_trade_count,
                promotion_gate_passed,
                deflated_sharpe,
                cpcv_median_sharpe
            FROM scan_patterns
            WHERE lifecycle_stage IN ('promoted', 'live')
               OR promotion_status = 'promoted'
            """
        )
    ).fetchall()
    classifier = classify_promotion_evidence_rows(
        _promotion_audit_classifier_rows(rows),
        evidence_source=PROMOTED_PATTERN_AUDIT_EVIDENCE_SOURCE,
        broker_truth_state="not_applicable",
        feature_schema_version=PROMOTION_EVIDENCE_FEATURE_SCHEMA_VERSION,
    )

    by_missing: dict[str, int] = {
        "oos_win_rate_null": 0,
        "oos_trade_count_zero_or_null": 0,
        "promotion_gate_not_passed": 0,
        "deflated_sharpe_null": 0,
        "cpcv_median_sharpe_null": 0,
    }
    incomplete_ids: list[int] = []
    incomplete_details: list[dict[str, Any]] = []
    complete = 0

    for r in rows:
        missing: list[str] = []
        if r.oos_win_rate is None:
            by_missing["oos_win_rate_null"] += 1
            missing.append("oos_win_rate_null")
        if r.oos_trade_count is None or (r.oos_trade_count or 0) <= 0:
            by_missing["oos_trade_count_zero_or_null"] += 1
            missing.append("oos_trade_count_zero_or_null")
        if not (r.promotion_gate_passed is True):
            by_missing["promotion_gate_not_passed"] += 1
            missing.append("promotion_gate_not_passed")
        if r.deflated_sharpe is None:
            by_missing["deflated_sharpe_null"] += 1
            missing.append("deflated_sharpe_null")
        if r.cpcv_median_sharpe is None:
            by_missing["cpcv_median_sharpe_null"] += 1
            missing.append("cpcv_median_sharpe_null")
        if missing:
            incomplete_ids.append(int(r.id))
            incomplete_details.append({
                "id": int(r.id),
                "name": r.name,
                "lifecycle_stage": r.lifecycle_stage,
                "promotion_status": r.promotion_status,
                "missing": missing,
                # f-promotion-pipeline-rebalance Phase 1 (2026-05-09):
                # surface CPCV median sharpe so the auto-demote
                # filter can protect CPCV-passing patterns even when
                # their OOS evidence is missing.
                "cpcv_median_sharpe": (
                    float(r.cpcv_median_sharpe)
                    if r.cpcv_median_sharpe is not None
                    else None
                ),
            })
        else:
            complete += 1

    return {
        "audited_at": audited_at,
        "criteria": _criteria(),
        "promoted_count_lifecycle": int(promoted_lifecycle),
        "promoted_count_legacy": int(promoted_legacy),
        "audit_universe_size": len(rows),
        "evidence_complete": complete,
        "evidence_incomplete": len(incomplete_ids),
        "by_missing_field": by_missing,
        "incomplete_ids": incomplete_ids,
        "incomplete_details": incomplete_details,
        "promotion_evidence_classifier": classifier,
    }


def _auto_demote_enabled() -> bool:
    return bool(getattr(settings, "chili_pattern_evidence_auto_demote", False))


def _auto_demote_dry_run() -> bool:
    """If True, log the demotions that would happen without applying them."""
    return bool(getattr(settings, "chili_pattern_evidence_auto_demote_dry_run", False))


# f-promotion-pipeline-rebalance Phase 1 (2026-05-09): CPCV-passing
# threshold parallel to learning.THIN_EVIDENCE_CPCV_PASSING_SHARPE_FLOOR.
# A pattern with CPCV median sharpe >= this value is protected from
# the 02:15 PT auto-demote audit even if its OOS evidence rows are
# NULL — CPCV is the higher-information signal and the missing-OOS
# state is a separate evidence-completeness gap, not a CPCV-degrade.
_CPCV_PASSING_SHARPE_FLOOR_FOR_AUDIT = 1.0


def _filter_cpcv_passing(incomplete_details: list[dict[str, Any]]) -> tuple[
    list[int], list[dict[str, Any]],
]:
    """Strip out patterns whose CPCV median sharpe is still passing
    (>= 1.0). Returns ``(actionable_ids, retained_details)`` where
    ``actionable_ids`` is the set the audit may demote, and
    ``retained_details`` is the full row payload (with a per-row
    ``cpcv_protected: bool`` flag) for the surfaced report.

    Reads ``chili_pattern_demote_require_cpcv_degrade`` (default True);
    when False, returns the input untouched (legacy semantics).
    """
    require = bool(
        getattr(
            settings, "chili_pattern_demote_require_cpcv_degrade", True,
        )
    )
    actionable_ids: list[int] = []
    retained: list[dict[str, Any]] = []
    for row in incomplete_details:
        cpcv_sharpe = row.get("cpcv_median_sharpe")
        protected = False
        if require and cpcv_sharpe is not None:
            try:
                if float(cpcv_sharpe) >= _CPCV_PASSING_SHARPE_FLOOR_FOR_AUDIT:
                    protected = True
            except (TypeError, ValueError):
                protected = False
        row_with_flag = dict(row)
        row_with_flag["cpcv_protected"] = protected
        retained.append(row_with_flag)
        if not protected:
            actionable_ids.append(int(row["id"]))
    return actionable_ids, retained


def run_promotion_evidence_audit(db: Session) -> dict[str, Any]:
    """Scheduler entrypoint. Runs the audit; optionally auto-demotes."""
    summary = audit_promoted_pattern_evidence(db)

    if summary["evidence_incomplete"] == 0:
        logger.info(
            "%s OK: %d promoted patterns, all evidence-complete",
            LOG_PREFIX,
            summary["audit_universe_size"],
        )
    else:
        logger.warning(
            "%s INCOMPLETE: %d/%d promoted patterns missing evidence; by_field=%s",
            LOG_PREFIX,
            summary["evidence_incomplete"],
            summary["audit_universe_size"],
            summary["by_missing_field"],
        )

    summary["auto_demote_enabled"] = _auto_demote_enabled()
    summary["auto_demote_dry_run"] = _auto_demote_dry_run()
    summary["auto_demote_actions"] = []

    # f-promotion-pipeline-rebalance Phase 1 (2026-05-09): filter the
    # actionable demote set through the CPCV-passing protection. The
    # full incomplete set is preserved in summary["incomplete_ids"] +
    # summary["incomplete_details"] (now annotated with
    # cpcv_protected); the actionable set is what we'd actually
    # demote.
    actionable_ids, annotated_details = _filter_cpcv_passing(
        summary["incomplete_details"]
    )
    summary["incomplete_details"] = annotated_details
    summary["cpcv_protected_count"] = sum(
        1 for r in annotated_details if r.get("cpcv_protected")
    )
    summary["actionable_demote_ids"] = actionable_ids

    if _auto_demote_enabled() and actionable_ids:
        ids = actionable_ids
        if _auto_demote_dry_run():
            logger.warning(
                "%s DRY-RUN: would demote %d patterns to 'challenged' (ids=%s)",
                LOG_PREFIX,
                len(ids),
                ids[:50],
            )
            summary["auto_demote_actions"] = [
                {"id": pid, "applied": False, "reason": "dry_run"} for pid in ids
            ]
        else:
            now = datetime.utcnow()
            db.execute(
                text(
                    """
                    UPDATE scan_patterns
                    SET lifecycle_stage = 'challenged',
                        lifecycle_changed_at = :now,
                        promotion_status = 'demoted_evidence_gap'
                    WHERE id = ANY(:ids)
                      AND (lifecycle_stage IN ('promoted', 'live')
                           OR promotion_status = 'promoted')
                    """
                ),
                {"now": now, "ids": ids},
            )
            db.commit()
            logger.error(
                "%s AUTO-DEMOTED %d patterns to 'challenged' for evidence gap (ids=%s)",
                LOG_PREFIX,
                len(ids),
                ids[:50],
            )
            summary["auto_demote_actions"] = [
                {"id": pid, "applied": True, "reason": "evidence_incomplete"} for pid in ids
            ]

    return summary
