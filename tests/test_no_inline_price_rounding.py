"""CI guard: forbid inline price/stop/limit rounding outside tick_normalizer.

Rationale
---------
On 2026-05-01 we found that ``broker_service.py`` had ``round(price, 2)`` at
nine submission sites, silently destroying crypto prices and causing
Robinhood to flag rounded equity stops as invalid. The fix was a venue-aware
``tick_normalizer`` module. This test is the regression net: any future code
that re-introduces ``round(price, N)`` (or its variants) without going through
the normalizer will fail this test.

What this test does NOT block
-----------------------------
* ``round(pnl, 2)`` — P&L is a money amount, not a tick-aligned price. (Phase 2
  will address P&L storage precision separately.)
* ``round(R, 6)`` / ``round(ratio, *)`` — risk metrics are dimensionless,
  not broker-bound.
* ``round`` calls in test files, scripts, or the normalizer itself.

If you legitimately need to round a price-like value outside the normalizer
and you have a good reason (operator-display formatting, log line, JSON
payload), prefix the variable name with ``display_`` or use ``f"{x:.2f}"``
formatting — both are accepted patterns this test won't trip on.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = REPO_ROOT / "app"

# Paths that are allowed to use raw round() because they ARE the
# normalizer (or are out of scope for this guard).
EXEMPT_PATHS = {
    APP_ROOT / "services" / "trading" / "tick_normalizer.py",
}

# Substrings in the matched line that indicate the rounded value is NOT a
# broker-bound price (so the rounding is fine).
ALLOW_SUBSTRINGS = (
    "pnl",                # money amount, Phase 2 territory
    "ratio",              # dimensionless
    "_r ",                # risk metric (R-multiple)
    " r,",                # risk multiple
    "pct",                # percentage display
    "percent",
    "delta",              # bps_diff and friends
    "bps",
    "win_rate",           # 0-1 scalar, not a price
    "score",              # rank score, not a price
    "weight",
    "confidence",
    "median",
    "atr",                # vol metric — equity tick is finer-grained
    "elapsed",
    "duration",
    "seconds",
    "minutes",
    "hours",
    "days",
)

# Forbidden patterns: round() with a literal small N applied to something
# that looks price-like.
PRICE_LIKE_NAMES = (
    "price",
    "stop",
    "limit",
    "target",
    "trigger",
    "entry",
    "exit",
    "fill",
    "tp",
    "sl",
    "premium",
    "strike",
)


def _is_price_like(line: str) -> bool:
    lower = line.lower()
    if any(allow in lower for allow in ALLOW_SUBSTRINGS):
        return False
    return any(name in lower for name in PRICE_LIKE_NAMES)


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return list of (lineno, line) flagged in *path*."""
    if path in EXEMPT_PATHS:
        return []
    if not path.suffix == ".py":
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    findings: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        # Strip comments — round() inside a docstring or # ... is not real code
        stripped = line.split("#", 1)[0]
        if "round(" not in stripped:
            continue
        # Must be round(<expr>, <small_int>)
        if not re.search(r"\bround\s*\([^,)]+,\s*[0-9]+\s*\)", stripped):
            continue
        if _is_price_like(stripped):
            findings.append((i, line.rstrip()))
    return findings


def test_no_inline_price_rounding_in_app_services():
    """Every price-like value crossing the broker boundary must go through
    tick_normalizer.normalize_price. Any inline round(price, N) — even if
    it produces the same answer in a specific case — is forbidden because
    the next venue (sub-dollar equity, crypto, options) has a different
    tick.
    """
    services_dir = APP_ROOT / "services"
    failures: list[str] = []
    for py in services_dir.rglob("*.py"):
        for lineno, line in _scan_file(py):
            rel = py.relative_to(REPO_ROOT)
            failures.append(f"{rel}:{lineno}: {line}")

    if failures:
        pytest.fail(
            "Found inline price-like round() calls outside tick_normalizer.\n"
            "Use app.services.trading.tick_normalizer.normalize_price instead.\n"
            "If the value is genuinely not a broker-bound price, name it "
            "'display_*' or use f-string formatting to avoid this guard.\n\n"
            "Offending lines:\n" + "\n".join(failures)
        )
