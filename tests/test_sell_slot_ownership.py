"""CI guard (Phase 3.6): only the canonical placement modules submit
SELL orders to the broker.

Rationale: on 2026-05-01 the audit identified the SELL-slot fight
between ``bracket_writer_g2`` and ``live_exit_engine`` as the underlying
reason multiple subsystems could submit SELL orders for the same shares
without coordinating. Phase 3.3 funneled bracket-stop placement through
``bracket_writer_g2``; this guard makes that exclusivity explicit.

Allowed SELL placement sites:

* ``app/services/broker_service.py`` (the boundary — wraps robin_stocks)
* ``app/services/coinbase_service.py`` (the boundary — wraps Coinbase SDK)
* ``app/services/trading/venue/*`` (venue adapters that call the boundary)
* ``app/services/trading/bracket_writer_g2.py`` (stop placement)
* ``app/services/trading/robinhood_exit_execution.py`` (exit submissions)
* ``app/services/trading/momentum_neural/live_runner.py`` (autopilot — separate scope, see audit)

Anywhere else calling something that looks like a SELL submission is a
bug class — submit through one of the above instead, which routes
through ``tick_normalizer`` and gets recorded on the execution event bus.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = REPO_ROOT / "app"

# Allowed SELL-placement files / directories.
EXEMPT_PATHS = {
    APP_ROOT / "services" / "broker_service.py",
    APP_ROOT / "services" / "coinbase_service.py",
    APP_ROOT / "services" / "trading" / "venue",
    APP_ROOT / "services" / "trading" / "bracket_writer_g2.py",
    APP_ROOT / "services" / "trading" / "robinhood_exit_execution.py",
    APP_ROOT / "services" / "trading" / "momentum_neural" / "live_runner.py",
    APP_ROOT / "services" / "trading" / "momentum_neural" / "automation_query.py",
    APP_ROOT / "services" / "broker_account_repair.py",  # cleanup utility
}


def _is_exempt(path: Path) -> bool:
    for ex in EXEMPT_PATHS:
        try:
            path.relative_to(ex)
            return True
        except ValueError:
            continue
    return path in EXEMPT_PATHS


# Patterns that indicate a SELL submission. We match call shapes that pass
# ``side="sell"`` or ``side='sell'`` to a broker call, and a few specific
# function-name idioms.
SELL_PATTERNS = (
    re.compile(r'\bside\s*=\s*["\']sell["\']'),
    re.compile(r'\bplace_sell_(?:order|stop_loss_order|crypto_order)\b'),
    re.compile(r'\border_sell_(?:limit|crypto_limit|crypto_by_quantity|option_limit)\b'),
    re.compile(r'\blimit_order_gtc_sell\b'),
    re.compile(r'\bmarket_order_sell\b'),
)


def _scan(path: Path) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return findings
    in_docstring = False
    docstring_quote = None
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # Cheap docstring tracking — skip lines inside ''' or """ blocks.
        if not in_docstring:
            for q in ('"""', "'''"):
                if stripped.startswith(q):
                    if stripped.count(q) == 2:  # single-line docstring
                        break
                    in_docstring = True
                    docstring_quote = q
                    break
        else:
            if docstring_quote and docstring_quote in stripped:
                in_docstring = False
                docstring_quote = None
            continue
        # Strip line comments — text-search false positives in comments.
        code = line.split("#", 1)[0]
        if not code.strip():
            continue
        for pat in SELL_PATTERNS:
            if pat.search(code):
                findings.append((i, line.rstrip()))
                break
    return findings


def test_sell_placement_only_in_canonical_modules():
    failures: list[str] = []
    for py in APP_ROOT.rglob("*.py"):
        if _is_exempt(py):
            continue
        for lineno, line in _scan(py):
            rel = py.relative_to(REPO_ROOT)
            failures.append(f"{rel}:{lineno}: {line}")

    if failures:
        pytest.fail(
            "Found SELL-placement sites outside the canonical modules.\n"
            "If you need to submit a SELL, do it through one of:\n"
            "  - bracket_writer_g2 (stop placement)\n"
            "  - robinhood_exit_execution (exit submissions)\n"
            "  - momentum_neural live_runner (autopilot)\n"
            "Each of those routes through broker_service / venue_adapter\n"
            "which applies tick_normalizer and records the execution event.\n\n"
            "Offending lines:\n" + "\n".join(failures)
        )
