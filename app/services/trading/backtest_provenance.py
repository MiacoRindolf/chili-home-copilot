"""Helpers to normalize and repair backtest provenance metadata."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import BacktestResult, ScanPattern
from .scan_pattern_label_alignment import strategy_label_aligns_scan_pattern_name

_WINDOW_KEYS = ("period", "interval", "ohlc_bars", "chart_time_from", "chart_time_to")


def _coerce_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def canonical_strategy_name(
    scan_pattern: ScanPattern | None,
    fallback_strategy_name: str | None,
) -> str:
    if scan_pattern is not None and scan_pattern.name:
        return str(scan_pattern.name)[:100]
    return str(fallback_strategy_name or "")[:100]


def normalize_backtest_params(
    params_obj: dict[str, Any] | None,
    *,
    scan_pattern: ScanPattern | None,
    strategy_name: str | None,
) -> tuple[dict[str, Any], str, list[str]]:
    params = _coerce_json_dict(params_obj)
    data_provenance = _coerce_json_dict(params.get("data_provenance"))

    for key in _WINDOW_KEYS:
        if params.get(key) is None and data_provenance.get(key) is not None:
            params[key] = data_provenance[key]
        if data_provenance.get(key) is None and params.get(key) is not None:
            data_provenance[key] = params[key]

    if scan_pattern is not None:
        data_provenance["scan_pattern_id"] = int(scan_pattern.id)
    elif params.get("scan_pattern_id") is not None:
        data_provenance.setdefault("scan_pattern_id", params.get("scan_pattern_id"))

    params["data_provenance"] = data_provenance

    issues: list[str] = []
    expected_strategy = canonical_strategy_name(scan_pattern, strategy_name)
    if scan_pattern is not None and not strategy_label_aligns_scan_pattern_name(
        strategy_name or "",
        expected_strategy,
    ):
        issues.append("strategy_scan_pattern_mismatch")

    missing_window = [key for key in _WINDOW_KEYS if params.get(key) is None]
    issues.extend(f"missing_{key}" for key in missing_window)

    if not missing_window:
        provenance_status = "complete"
    elif len(missing_window) >= 3:
        provenance_status = "quarantined"
    else:
        provenance_status = "incomplete"

    params["provenance_status"] = provenance_status
    params["provenance_issues"] = issues

    integrity = _coerce_json_dict(params.get("research_integrity"))
    integrity["provenance_status"] = provenance_status
    integrity["provenance_issues"] = issues
    params["research_integrity"] = integrity
    return params, provenance_status, issues


def normalize_backtest_storage_metadata(
    db: Session,
    *,
    resolved_scan_pattern_id: int | None,
    strategy_name: str | None,
    params_obj: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], str, list[str], ScanPattern | None]:
    scan_pattern = db.get(ScanPattern, int(resolved_scan_pattern_id)) if resolved_scan_pattern_id else None
    canonical_name = canonical_strategy_name(scan_pattern, strategy_name)
    normalized_params, status, issues = normalize_backtest_params(
        params_obj,
        scan_pattern=scan_pattern,
        strategy_name=canonical_name,
    )
    return canonical_name, normalized_params, status, issues, scan_pattern


def repair_backtest_provenance(
    db: Session,
    *,
    apply: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    q = db.query(BacktestResult).order_by(BacktestResult.id.asc())
    if limit is not None:
        q = q.limit(max(1, int(limit)))
    rows = q.all()

    strategy_fixed = 0
    params_fixed = 0
    complete = 0
    incomplete = 0
    quarantined = 0

    for row in rows:
        original_strategy = str(row.strategy_name or "")
        original_params = _coerce_json_dict(row.params)
        strategy_name, params_obj, status, _issues, _pattern = normalize_backtest_storage_metadata(
            db,
            resolved_scan_pattern_id=int(row.scan_pattern_id) if row.scan_pattern_id is not None else None,
            strategy_name=original_strategy,
            params_obj=original_params,
        )
        if strategy_name != original_strategy:
            strategy_fixed += 1
        if params_obj != original_params:
            params_fixed += 1
        if status == "complete":
            complete += 1
        elif status == "quarantined":
            quarantined += 1
        else:
            incomplete += 1
        if apply:
            row.strategy_name = strategy_name
            row.params = params_obj

    if apply and rows:
        db.commit()

    return {
        "ok": True,
        "rows_scanned": len(rows),
        "strategy_fixed": strategy_fixed,
        "params_fixed": params_fixed,
        "provenance_complete": complete,
        "provenance_incomplete": incomplete,
        "provenance_quarantined": quarantined,
        "applied": bool(apply),
    }
