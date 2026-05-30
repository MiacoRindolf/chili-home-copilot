"""Classify remaining Phase 5 trade-surface references.

This source-only report helps the position-identity cleanup separate expected
legacy compatibility/live-writer references from analytics, model, and capital
readers that may still need migration or explicit ownership.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]

RAW_SQL_RE = re.compile(r"\b(?:FROM|JOIN)\s+trading_trades\b", re.IGNORECASE)
TABLE_SYMBOL_RE = re.compile(r"\btrading_trades\b", re.IGNORECASE)
MODEL_SYMBOL_RE = re.compile(r"\bTrade\b")

SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "htmlcov",
    "node_modules",
    "project_ws",
}

DEFAULT_INCLUDE_DIRS = ("app", "scripts", "tests", "docs")


@dataclass(frozen=True)
class Classification:
    bucket: str
    owner: str
    decision: str


CLASSIFICATIONS = (
    (
        re.compile(
            r"^(?:tests|docs|app/migrations\.py|scripts/(?:analyze_phase5_|d-|dispatch-|setup-)|scripts/_)",
            re.IGNORECASE,
        ),
        Classification(
            "compatibility_migration_test_history",
            "SSWE / QA",
            "Keep only when tied to compatibility, migration, or historical proof.",
        ),
    ),
    (
        re.compile(
            r"^(?:app/services/(?:broker_service|coinbase_service)\.py|"
            r"app/services/trading/(?:auto_trader|broker_quotes|bracket_reconciliation_service|"
            r"emergency_liquidation|execution_audit|live_exit_engine|paper_trading|"
            r"robinhood_exit_execution|scanner|venue/|crypto/exit_monitor|options/exit_monitor))",
            re.IGNORECASE,
        ),
        Classification(
            "live_writer_order_broker_reconcile",
            "Algo Trader Architect / Risk",
            "Allowed only when the path owns live order, broker truth, reconciliation, or exit execution.",
        ),
    ),
    (
        re.compile(
            r"^(?:app/services/trading/(?:attribution_service|cost_aware_gate|"
            r"management_envelopes|pattern_regime|pattern_survival|portfolio_allocator|"
            r"portfolio_risk|position_sizer|promotion_gate|realized_pnl_sql|return_math|"
            r"triple_barrier|options/portfolio_budget)|scripts/brain_worker\.py)",
            re.IGNORECASE,
        ),
        Classification(
            "evidence_model_capital_reader",
            "Data Science / Algo Trader Architect",
            "Needs reader-surface migration, parity evidence, or an explicit compatibility waiver.",
        ),
    ),
)

DEFAULT_CLASSIFICATION = Classification(
    "unclassified_trade_surface_reference",
    "Needs owner",
    "Review before Phase 5L acceptance.",
)


def _is_skipped(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def _iter_source_files(root: Path, include_dirs: Iterable[str]) -> Iterable[Path]:
    for include_dir in include_dirs:
        start = root / include_dir
        if not start.exists():
            continue
        if start.is_file():
            yield start
            continue
        for path in start.rglob("*"):
            if path.is_file() and not _is_skipped(path.relative_to(root)):
                yield path


def _unique_matches(pattern: re.Pattern[str], source: str) -> list[str]:
    found: list[str] = []
    for match in pattern.finditer(source):
        text = match.group(0)
        if text not in found:
            found.append(text)
    return found


def _reference_matches(source: str) -> dict[str, list[str]]:
    raw_sql = _unique_matches(RAW_SQL_RE, source)
    symbols = _unique_matches(TABLE_SYMBOL_RE, source) + _unique_matches(MODEL_SYMBOL_RE, source)
    return {
        "raw_sql": raw_sql,
        "symbols": [symbol for symbol in symbols if symbol not in raw_sql],
    }


def classify_path(relative_path: str) -> Classification:
    normalized = relative_path.replace("\\", "/")
    for pattern, classification in CLASSIFICATIONS:
        if pattern.search(normalized):
            return classification
    return DEFAULT_CLASSIFICATION


def build_inventory(
    root: Path = REPO_ROOT,
    include_dirs: Iterable[str] = DEFAULT_INCLUDE_DIRS,
    *,
    raw_sql_only: bool = False,
) -> dict:
    entries: list[dict] = []
    buckets: dict[str, int] = {}
    raw_reader_buckets: dict[str, int] = {}

    for path in sorted(_iter_source_files(root, include_dirs)):
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        matches = _reference_matches(source)
        if raw_sql_only and not matches["raw_sql"]:
            continue
        if not matches["raw_sql"] and not matches["symbols"]:
            continue
        relative_path = path.relative_to(root).as_posix()
        classification = classify_path(relative_path)
        buckets[classification.bucket] = buckets.get(classification.bucket, 0) + 1
        if matches["raw_sql"]:
            raw_reader_buckets[classification.bucket] = raw_reader_buckets.get(classification.bucket, 0) + 1
        entries.append(
            {
                "path": relative_path,
                "bucket": classification.bucket,
                "owner": classification.owner,
                "decision": classification.decision,
                "reference_kind": "raw_sql_reader" if matches["raw_sql"] else "symbol_or_text_reference",
                "raw_sql_references": matches["raw_sql"],
                "symbol_references": matches["symbols"],
                "references": matches["raw_sql"] + matches["symbols"],
            }
        )

    return {
        "ok": True,
        "root": str(root),
        "file_count": len(entries),
        "raw_sql_file_count": sum(1 for entry in entries if entry["raw_sql_references"]),
        "buckets": dict(sorted(buckets.items())),
        "raw_reader_buckets": dict(sorted(raw_reader_buckets.items())),
        "entries": entries,
    }


def _print_table(report: dict) -> None:
    print("bucket | files")
    print("-------+------")
    for bucket, count in report["buckets"].items():
        print(f"{bucket} | {count}")
    print()
    print("raw reader bucket | files")
    print("------------------+------")
    if report["raw_reader_buckets"]:
        for bucket, count in report["raw_reader_buckets"].items():
            print(f"{bucket} | {count}")
    else:
        print("(none) | 0")
    print()
    print("path | kind | bucket | owner")
    print("-----+------+--------+------")
    for entry in report["entries"]:
        print(f"{entry['path']} | {entry['reference_kind']} | {entry['bucket']} | {entry['owner']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--raw-sql-only",
        action="store_true",
        help="Only include files with raw FROM/JOIN trading_trades readers.",
    )
    parser.add_argument(
        "--include",
        action="append",
        dest="include_dirs",
        help="Directory or file to scan. May be passed more than once.",
    )
    args = parser.parse_args()

    report = build_inventory(
        REPO_ROOT,
        args.include_dirs or DEFAULT_INCLUDE_DIRS,
        raw_sql_only=args.raw_sql_only,
    )
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
