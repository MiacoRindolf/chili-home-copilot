"""CI guard (Phase 4): no inline ``or 10000.0`` capital fallbacks.

Per the user's stated rule ("never `or 0.5` magic constants for missing
measurements"), capital placeholders that silently lie to the brain
about user equity must be channeled through
``stop_engine_fallback_constants.resolve_capital_with_critical_log``.

The previous pattern was scattered:

    capital = float(buying_power or 10000.0)   # alerts.py:332
    if quantity is None:
        quantity = max(1, int((buying_power or 10000.0) * ...))   # alerts.py:933
    ...

Each was a silent fallback. The Phase 4 fix routes all of them through
the resolver, which logs CRITICAL on every firing so the upstream broker
fetch can be fixed.

This guard catches new ``or 10000.0`` (or any 4+digit magic dollar
amount) patterns introduced anywhere except the resolver itself.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = REPO_ROOT / "app"

# The resolver IS the canonical fallback. Other test files quoting the
# pattern in docstrings are also exempt (they're documentation).
EXEMPT_PATHS = {
    APP_ROOT / "services" / "trading" / "stop_engine_fallback_constants.py",
}

# Match `or NNNN(.0)?` and `or NNN(.0)?` where the number is a likely
# dollar placeholder ($100+, no other digits adjacent). Captures things
# like ``or 10000``, ``or 10000.0``, ``or 100``, but not ``or 0.5``,
# ``or 0.95``, ``or 14`` (those are tunable multipliers, not capital).
PATTERN = re.compile(r"\bor\s+1\d{3,5}(?:\.\d+)?\b")


def test_no_inline_capital_or_fallback():
    failures: list[str] = []
    for py in APP_ROOT.rglob("*.py"):
        if py in EXEMPT_PATHS:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            stripped = line.split("#", 1)[0]
            if PATTERN.search(stripped):
                rel = py.relative_to(REPO_ROOT)
                failures.append(f"{rel}:{i}: {line.rstrip()}")

    if failures:
        pytest.fail(
            "Found inline `or NNNN` capital-fallback magic. Use\n"
            "stop_engine_fallback_constants.resolve_capital_with_critical_log()\n"
            "instead — it logs CRITICAL on every firing so upstream\n"
            "broker fetch failures are observable.\n\n"
            "Offending lines:\n" + "\n".join(failures)
        )
