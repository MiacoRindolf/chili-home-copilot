"""CI guard (Phase 3.2): broker SDKs must only be imported in the
canonical adapter set.

Rationale: on 2026-05-01 we found ``round(price, 2)`` scattered across
9 broker-submission sites, silently destroying crypto and sub-dollar
equity prices. The Phase 1 fix (``tick_normalizer``) handled the round
calls, but the underlying problem is that any module can ``import
robin_stocks.robinhood`` directly and then call ``rh.orders.order(...)``
without going through the precision-aware boundary. This test makes the
boundary explicit:

* ``robin_stocks`` → only ``broker_service.py`` and ``trading/venue/*``
* ``coinbase`` (advanced trade) → only ``coinbase_service.py`` and
  ``trading/venue/*``

If you need a new broker call, add a wrapper in ``broker_service.py``
or extend the venue adapter — don't import the SDK directly.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = REPO_ROOT / "app"

# Allowed importers — these ARE the boundary.
EXEMPT_PATHS = {
    APP_ROOT / "services" / "broker_service.py",
    APP_ROOT / "services" / "coinbase_service.py",
    # Everything under trading/venue/* is the venue adapter set.
    APP_ROOT / "services" / "trading" / "venue",
}


def _is_exempt(path: Path) -> bool:
    for ex in EXEMPT_PATHS:
        try:
            path.relative_to(ex)
            return True
        except ValueError:
            continue
        # Also match exact-file exemptions.
    return path in EXEMPT_PATHS


# Regex covers the common forms:
#   import robin_stocks
#   import robin_stocks.robinhood as rh
#   from robin_stocks import ...
#   from robin_stocks.robinhood import ...
ROBIN_PATTERNS = (
    re.compile(r"^\s*import\s+robin_stocks(\s|\.|$)", re.MULTILINE),
    re.compile(r"^\s*from\s+robin_stocks", re.MULTILINE),
)
COINBASE_PATTERNS = (
    re.compile(r"^\s*import\s+coinbase(\s|\.|$)", re.MULTILINE),
    re.compile(r"^\s*from\s+coinbase", re.MULTILINE),
)


def _scan(path: Path, patterns) -> list[tuple[int, str]]:
    """Return list of (lineno, line) of import lines matching any pattern."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    findings: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        # Skip comments & docstring contents — text-search false positives
        # for "robin_stocks" inside a comment shouldn't trip the guard.
        # (We only check the import-line shape — a docstring saying "uses
        # robin_stocks" lacks the import keyword.)
        if not (line.lstrip().startswith("import ") or line.lstrip().startswith("from ")):
            continue
        for pat in patterns:
            if pat.search(line):
                findings.append((i, line.rstrip()))
                break
    return findings


def test_robin_stocks_only_imported_in_adapter_set():
    failures: list[str] = []
    for py in APP_ROOT.rglob("*.py"):
        if _is_exempt(py):
            continue
        for lineno, line in _scan(py, ROBIN_PATTERNS):
            rel = py.relative_to(REPO_ROOT)
            failures.append(f"{rel}:{lineno}: {line}")

    if failures:
        pytest.fail(
            "Found `import robin_stocks` outside the canonical adapter set.\n"
            "Allowed locations: app/services/broker_service.py, "
            "app/services/trading/venue/*.\n"
            "If you need a new broker call, add a wrapper in broker_service.py "
            "(see e.g. get_open_stock_orders, get_market_hours).\n\n"
            "Offending lines:\n" + "\n".join(failures)
        )


def test_coinbase_only_imported_in_adapter_set():
    failures: list[str] = []
    for py in APP_ROOT.rglob("*.py"):
        if _is_exempt(py):
            continue
        for lineno, line in _scan(py, COINBASE_PATTERNS):
            rel = py.relative_to(REPO_ROOT)
            failures.append(f"{rel}:{lineno}: {line}")

    if failures:
        pytest.fail(
            "Found `import coinbase` outside the canonical adapter set.\n"
            "Allowed locations: app/services/coinbase_service.py, "
            "app/services/trading/venue/*.\n\n"
            "Offending lines:\n" + "\n".join(failures)
        )
