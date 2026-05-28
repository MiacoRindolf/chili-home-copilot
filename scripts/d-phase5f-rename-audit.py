"""Phase 5F dependency audit for the trading_trades physical rename.

The physical rename from ``trading_trades`` to
``trading_management_envelopes`` is high blast-radius because the ORM, raw SQL,
tests, scripts, and docs still mention the legacy table/model. This read-only
script produces a small inventory that can be rerun before a rename dry-run.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
INCLUDE_DIRS = ("app", "tests", "scripts", "docs")
EXCLUDE_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "_claude_history",
    "_pg_stat_log",
    "watcher-out",
}
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".ps1",
    ".sql",
    ".txt",
    ".yml",
    ".yaml",
    ".json",
}

PATTERNS = {
    "literal_trading_trades": re.compile(r"\btrading_trades\b"),
    "literal_trading_management_envelopes": re.compile(r"\btrading_management_envelopes\b"),
    "orm_trade_import": re.compile(r"\bfrom\s+app\.models\.trading\s+import\b.*\bTrade\b"),
    "orm_trade_symbol": re.compile(r"\bTrade\b"),
    "trade_id": re.compile(r"\btrade_id\b"),
    "source_trade_id": re.compile(r"\bsource_trade_id\b"),
}


def _iter_files() -> Iterable[Path]:
    for base in INCLUDE_DIRS:
        root = ROOT / base
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel_parts = set(path.relative_to(ROOT).parts)
            if rel_parts & EXCLUDE_PARTS:
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if path.name.endswith(("-out.txt", "-output.txt")):
                continue
            yield path


def _kind(path: Path) -> str:
    rel = path.relative_to(ROOT).as_posix()
    if rel.startswith("app/models/"):
        return "orm_model"
    if rel.startswith("app/migrations"):
        return "migration"
    if rel.startswith("app/services/") or rel.startswith("app/routers/"):
        return "runtime_code"
    if rel.startswith("tests/"):
        return "test"
    if rel.startswith("scripts/"):
        return "script"
    if rel.startswith("docs/"):
        return "doc"
    return "other"


def main() -> int:
    file_hits: dict[str, dict[str, int]] = {}
    kind_counts: dict[str, Counter[str]] = defaultdict(Counter)
    total_counts: Counter[str] = Counter()

    for path in _iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        hits = {name: len(pattern.findall(text)) for name, pattern in PATTERNS.items()}
        hits = {name: n for name, n in hits.items() if n}
        if not hits:
            continue
        rel = path.relative_to(ROOT).as_posix()
        file_hits[rel] = hits
        k = _kind(path)
        for name, n in hits.items():
            kind_counts[k][name] += n
            total_counts[name] += n

    literal_files = [
        rel for rel, hits in file_hits.items()
        if hits.get("literal_trading_trades")
    ]
    runtime_literal_files = [
        rel for rel in literal_files
        if _kind(ROOT / rel) in {"runtime_code", "orm_model", "migration"}
    ]
    runtime_trade_symbol_files = [
        rel for rel, hits in file_hits.items()
        if hits.get("orm_trade_symbol") and _kind(ROOT / rel) in {"runtime_code", "orm_model"}
    ]

    payload = {
        "ok": True,
        "root": str(ROOT),
        "summary": {
            "files_with_any_hit": len(file_hits),
            "files_with_literal_trading_trades": len(literal_files),
            "runtime_files_with_literal_trading_trades": len(runtime_literal_files),
            "runtime_files_with_Trade_symbol": len(runtime_trade_symbol_files),
            "total_counts": dict(total_counts),
            "counts_by_kind": {
                kind: dict(counter)
                for kind, counter in sorted(kind_counts.items())
            },
        },
        "top_files_by_literal_trading_trades": sorted(
            [
                {
                    "path": rel,
                    "kind": _kind(ROOT / rel),
                    "literal_trading_trades": hits.get("literal_trading_trades", 0),
                    "orm_trade_symbol": hits.get("orm_trade_symbol", 0),
                }
                for rel, hits in file_hits.items()
                if hits.get("literal_trading_trades")
            ],
            key=lambda row: (-row["literal_trading_trades"], row["path"]),
        )[:80],
        "runtime_literal_files": sorted(runtime_literal_files),
        "runtime_trade_symbol_files": sorted(runtime_trade_symbol_files)[:120],
    }

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
