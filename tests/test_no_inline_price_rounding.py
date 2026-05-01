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

# Phase 1 scope — files that actually submit orders to the broker. These
# are where tick-size correctness is non-negotiable. Storage-side rounding
# (Trade rows, signal records, paper trades, backtest results, snapshots)
# is Phase 2 territory and a separate guard will be added there.
#
# Each path is checked recursively (you can list a directory).
PHASE1_BROKER_BOUNDARY_PATHS = [
    APP_ROOT / "services" / "broker_service.py",
    APP_ROOT / "services" / "coinbase_service.py",
    APP_ROOT / "services" / "trading" / "robinhood_exit_execution.py",
    APP_ROOT / "services" / "trading" / "venue",
    APP_ROOT / "services" / "trading" / "bracket_writer_g2.py",
    APP_ROOT / "services" / "trading" / "options" / "synthesis.py",
]

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
    # 2026-05-01 additions after the first audit pass — these names appear
    # in price-like idioms but the rounded value is not a broker submission:
    "probability",        # p_fill / p_partial / p_missed — 0..1 scalars
    "p_fill", "p_partial", "p_missed",
    "complexity",         # code analysis metric
    "notional",           # money amount derived from price * qty (Phase 2)
    "projected_profit",   # display amount, not a broker price
    "near_resistance",    # display level, not submission
    "fib",                # fibonacci level (display/diagnostic)
    "recommended_spread", # measured statistic, not order price
    "latency", "ack_to_fill", "ms",  # timing metrics
    "rl_rate",            # rate-limit metric
    "synthesis_spread",   # diagnostic
    "synthesis_spot",     # diagnostic
    "synthesis_target_dte",
    "spread_pct",         # diagnostic
    "alt = round",        # one-off in synthesis fallback strike search
    "rr =",               # R:R ratio
    "rr_",
    "near_resistance",
    "spread_bps",         # diagnostic
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


def _iter_phase1_files():
    """Yield every .py inside the Phase 1 scope (file or recursive dir)."""
    for path in PHASE1_BROKER_BOUNDARY_PATHS:
        if not path.exists():
            continue
        if path.is_file() and path.suffix == ".py":
            yield path
        elif path.is_dir():
            yield from path.rglob("*.py")


def test_no_inline_price_rounding_in_broker_boundary():
    """Phase 1 scope: every file that submits orders to a broker must go
    through tick_normalizer. Any inline ``round(price, N)`` in a broker-
    submission file is forbidden — the next venue (sub-dollar equity,
    crypto, options) has a different tick rule.

    Storage-side rounding (Trade rows, snapshots, signals, paper trades,
    backtests) is Phase 2 scope — a separate guard will catch those.
    """
    failures: list[str] = []
    for py in _iter_phase1_files():
        for lineno, line in _scan_file(py):
            rel = py.relative_to(REPO_ROOT)
            failures.append(f"{rel}:{lineno}: {line}")

    if failures:
        pytest.fail(
            "Found inline price-like round() calls in Phase 1 broker-boundary code.\n"
            "Use app.services.trading.tick_normalizer.normalize_price instead.\n"
            "If the value is genuinely not a broker-bound price, name it "
            "'display_*' or use f-string formatting.\n\n"
            "Offending lines:\n" + "\n".join(failures)
        )
