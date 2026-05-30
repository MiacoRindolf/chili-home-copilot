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

This guard catches new inline capital placeholders in the executable
proposal path (``alerts.py``): ``or 10000.0``, ``return 10000.0``,
``risk_capital = 100000.0``, etc.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = REPO_ROOT / "app"
TARGET_PATHS = [APP_ROOT / "services" / "trading" / "alerts.py"]

# Match likely executable capital placeholders. Captures things like
# ``or 10000``, ``return 10000.0``, ``cap = 100000``, and underscored
# variants, but not small tunable multipliers such as ``0.95`` or ``14``.
PATTERN = re.compile(
    r"(?:\bor\s+|\breturn\s+|=\s*)1(?:_?\d){3,5}(?:\.\d+)?\b"
)


def test_no_inline_capital_or_fallback():
    failures: list[str] = []
    for py in TARGET_PATHS:
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
            "Found inline capital-fallback magic. Use\n"
            "stop_engine_fallback_constants.resolve_capital_with_critical_log()\n"
            "instead — it logs CRITICAL on every firing so upstream\n"
            "broker fetch failures are observable.\n\n"
            "Offending lines:\n" + "\n".join(failures)
        )
