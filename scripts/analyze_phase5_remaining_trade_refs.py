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

RAW_READER_SQL_RE = re.compile(r"\b(?:FROM|JOIN)\s+trading_trades\b", re.IGNORECASE)
RAW_MUTATION_SQL_RE = re.compile(
    r"\b(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+trading_trades\b",
    re.IGNORECASE,
)
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
        re.compile(r"^docs/", re.IGNORECASE),
        Classification(
            "docs_runbooks",
            "SSWE / Operator",
            "Documentation-only references; keep as historical/operator context.",
        ),
    ),
    (
        re.compile(r"^(?:tests|app/migrations\.py)", re.IGNORECASE),
        Classification(
            "compatibility_migration_test_history",
            "SSWE / QA",
            "Keep only when tied to compatibility, migration, or historical proof.",
        ),
    ),
    (
        re.compile(r"^scripts/", re.IGNORECASE),
        Classification(
            "compatibility_migration_test_history",
            "SSWE / QA",
            "Probe or setup script; keep only when tied to compatibility or historical proof.",
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
    raw_readers = _unique_matches(RAW_READER_SQL_RE, source)
    raw_mutations = _unique_matches(RAW_MUTATION_SQL_RE, source)
    table_symbols = _unique_matches(TABLE_SYMBOL_RE, source)
    model_symbols = _unique_matches(MODEL_SYMBOL_RE, source)
    covered = set(raw_readers + raw_mutations)
    return {
        "raw_readers": raw_readers,
        "raw_mutations": raw_mutations,
        "table_symbols": [symbol for symbol in table_symbols if symbol not in covered],
        "model_symbols": model_symbols,
    }


def _has_any_reference(matches: dict[str, list[str]]) -> bool:
    return any(matches.values())


def classify_path(relative_path: str) -> Classification:
    normalized = relative_path.replace("\\", "/")
    for pattern, classification in CLASSIFICATIONS:
        if pattern.search(normalized):
            return classification
    return DEFAULT_CLASSIFICATION


def classify_reference_contract(
    relative_path: str,
    matches: dict[str, list[str]],
) -> Classification:
    """Classify a path + reference shape into an explicit Phase 5L contract."""
    normalized = relative_path.replace("\\", "/")
    path_classification = classify_path(normalized)

    if path_classification.bucket in {
        "docs_runbooks",
        "compatibility_migration_test_history",
    }:
        return path_classification

    if normalized.startswith("app/") and matches["raw_readers"]:
        return Classification(
            "unexpected_runtime_reader",
            "Needs owner",
            "Runtime app code must not add raw FROM/JOIN trading_trades readers; use a semantic helper.",
        )

    if normalized.startswith("app/") and matches["raw_mutations"]:
        if path_classification.bucket == "live_writer_order_broker_reconcile":
            return Classification(
                "allowed_compatibility_writer_update",
                path_classification.owner,
                "Allowed compatibility-view write/update path; do not rename mechanically.",
            )
        return Classification(
            "unexpected_runtime_mutation",
            "Needs owner",
            "Runtime app code is mutating the compatibility view outside an owned writer path.",
        )

    if matches["table_symbols"]:
        return Classification(
            "compatibility_relation_symbol",
            path_classification.owner
            if path_classification.bucket != DEFAULT_CLASSIFICATION.bucket
            else "SSWE / Algo Trader Architect",
            "Literal trading_trades symbol remains as a compatibility contract; audit before changing.",
        )

    if matches["model_symbols"]:
        return Classification(
            "orm_trade_symbol_compat",
            "SSWE / Algo Trader Architect",
            "Legacy ORM class symbol. Keep until a deliberate ORM rename phase.",
        )

    return path_classification


def build_inventory(
    root: Path = REPO_ROOT,
    include_dirs: Iterable[str] = DEFAULT_INCLUDE_DIRS,
    *,
    raw_sql_only: bool = False,
) -> dict:
    entries: list[dict] = []
    buckets: dict[str, int] = {}
    raw_reader_buckets: dict[str, int] = {}
    unexpected_runtime_readers: list[str] = []
    unexpected_runtime_mutations: list[str] = []
    unclassified: list[str] = []

    for path in sorted(_iter_source_files(root, include_dirs)):
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        matches = _reference_matches(source)
        if raw_sql_only and not matches["raw_readers"]:
            continue
        if not _has_any_reference(matches):
            continue
        relative_path = path.relative_to(root).as_posix()
        classification = classify_reference_contract(relative_path, matches)
        buckets[classification.bucket] = buckets.get(classification.bucket, 0) + 1
        if matches["raw_readers"]:
            raw_reader_buckets[classification.bucket] = (
                raw_reader_buckets.get(classification.bucket, 0) + 1
            )
        if classification.bucket == "unexpected_runtime_reader":
            unexpected_runtime_readers.append(relative_path)
        if classification.bucket == "unexpected_runtime_mutation":
            unexpected_runtime_mutations.append(relative_path)
        if classification.bucket == DEFAULT_CLASSIFICATION.bucket:
            unclassified.append(relative_path)
        entries.append(
            {
                "path": relative_path,
                "bucket": classification.bucket,
                "owner": classification.owner,
                "decision": classification.decision,
                "reference_kind": (
                    "raw_sql_reader"
                    if matches["raw_readers"]
                    else "raw_sql_mutation"
                    if matches["raw_mutations"]
                    else "relation_symbol"
                    if matches["table_symbols"]
                    else "orm_symbol"
                ),
                "raw_sql_references": matches["raw_readers"],
                "raw_mutation_references": matches["raw_mutations"],
                "table_symbol_references": matches["table_symbols"],
                "model_symbol_references": matches["model_symbols"],
                "references": (
                    matches["raw_readers"]
                    + matches["raw_mutations"]
                    + matches["table_symbols"]
                    + matches["model_symbols"]
                ),
            }
        )

    ok = (
        not unexpected_runtime_readers
        and not unexpected_runtime_mutations
        and not unclassified
    )
    return {
        "ok": ok,
        "root": str(root),
        "file_count": len(entries),
        "raw_sql_file_count": sum(1 for entry in entries if entry["raw_sql_references"]),
        "buckets": dict(sorted(buckets.items())),
        "raw_reader_buckets": dict(sorted(raw_reader_buckets.items())),
        "unexpected_runtime_readers": unexpected_runtime_readers,
        "unexpected_runtime_mutations": unexpected_runtime_mutations,
        "unclassified": unclassified,
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
        "--fail-on-unexpected-runtime",
        action="store_true",
        help="Exit non-zero when runtime app code has unexpected raw readers/mutations or unclassified references.",
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
    if args.fail_on_unexpected_runtime and not report["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
