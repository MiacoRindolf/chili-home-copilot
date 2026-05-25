"""CI guard: only ``bracket_intent_writer`` writes to ``trading_bracket_intents``.

Phase 3.1 (2026-05-01) makes ``bracket_intent_writer.py`` the single
authority for the bracket intent table. Any UPDATE / INSERT / SQLAlchemy
ORM ``session.add(BracketIntent)`` outside this module must be flagged.

The motivation: on 2026-05-01, the audit identified the bracket-lifecycle
race-condition class as the underlying reason every fix (51, 52, 53, 55,
56, 57) generated a new bug. Funnelling all mutations through the state
machine in ``bracket_intent_writer.transition()`` makes the race
impossible.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = REPO_ROOT / "app"

# Files that ARE the authority. They're allowed to write the table.
EXEMPT_PATHS = {
    APP_ROOT / "services" / "trading" / "bracket_intent_writer.py",
    APP_ROOT / "migrations.py",  # schema lifecycle — not runtime mutation
    APP_ROOT / "models" / "trading.py",  # ORM table declaration
}

# Narrowly sanctioned sidecar writes that predate this guard and are not
# generic lifecycle ownership. Keep these context fragments specific so new
# direct writes in the same file still fail this test.
ALLOWED_WRITE_CONTEXTS = {
    "app/routers/admin.py": ("pending_decision,operator_choice",),
    "app/services/broker_service.py": ("inverse_reconcile_reopen",),
    "app/services/coinbase_service.py": (
        "position_id = :pid",
        "coinbase_inverse_reconcile_reopen",
    ),
    "app/services/trading/bracket_reconciliation_service.py": (
        "terminal_reject_repair_last_attempt_at",
        "pending_decision",
        "adopted_broker_tighter_stop",
    ),
    "app/services/trading/bracket_writer_g2.py": ("pending_decision",),
}

# Patterns that indicate a write to the table.
FORBIDDEN_PATTERNS = (
    re.compile(r"UPDATE\s+trading_bracket_intents", re.IGNORECASE),
    re.compile(r"INSERT\s+INTO\s+trading_bracket_intents", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM\s+trading_bracket_intents", re.IGNORECASE),
    re.compile(r"session\.add\s*\(\s*BracketIntent\s*\("),
    re.compile(r"session\.merge\s*\(\s*BracketIntent\s*\("),
)


def _is_allowed_sidecar_write(py: Path, lines: list[str], line_idx: int) -> bool:
    rel = py.relative_to(REPO_ROOT).as_posix()
    fragments = ALLOWED_WRITE_CONTEXTS.get(rel)
    if not fragments:
        return False
    context = "\n".join(lines[max(0, line_idx - 4): line_idx + 12])
    return any(fragment in context for fragment in fragments)


def test_only_bracket_intent_writer_mutates_table():
    failures: list[str] = []
    for py in APP_ROOT.rglob("*.py"):
        if py in EXEMPT_PATHS:
            continue
        if py.suffix != ".py":
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except Exception:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines, start=1):
            stripped = line.split("#", 1)[0]
            for pat in FORBIDDEN_PATTERNS:
                if pat.search(stripped):
                    if _is_allowed_sidecar_write(py, lines, i - 1):
                        continue
                    rel = py.relative_to(REPO_ROOT)
                    failures.append(f"{rel}:{i}: {line.rstrip()}")
                    break

    if failures:
        pytest.fail(
            "Found unauthorized writes to trading_bracket_intents.\n"
            "Phase 3.1 makes bracket_intent_writer.py the lifecycle writer.\n"
            "Use transition(), upsert_bracket_intent(), mark_reconciled(),\n"
            "mark_terminal_reject(), or mark_closed(); add a narrow sidecar\n"
            "context here only for reviewed metadata/repair writes.\n\n"
            "Offending lines:\n" + "\n".join(failures)
        )
