from __future__ import annotations

import re
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = REPO_ROOT / "app"

READ_SQL_RE = re.compile(r"\b(?:FROM|JOIN)\s+trading_trades\b", re.IGNORECASE)

# Exact current compatibility-view live-reader surface after Phase 5L-A. This
# is intentionally narrow: a new raw reader line against trading_trades must
# either move to trading_management_envelopes or make its contract explicit here.
ALLOWED_LINE_COUNTS: dict[tuple[str, str], int] = {
    (
        "app/services/trading/auto_trader.py",
        "FROM trading_trades t",
    ): 1,
    (
        "app/services/trading/auto_trader.py",
        "FROM trading_trades",
    ): 1,
    (
        "app/services/trading/auto_trader_rules.py",
        '"FROM trading_trades t "',
    ): 1,
    (
        "app/services/trading/bracket_reconciliation_service.py",
        "FROM trading_trades AS t",
    ): 2,
    (
        "app/services/trading/pattern_regime_ledger.py",
        "FROM trading_trades t",
    ): 4,
    (
        "app/services/trading/pattern_survival/features.py",
        "FROM trading_trades",
    ): 2,
    (
        "app/services/trading/pattern_survival/features.py",
        "FROM trading_trades t",
    ): 1,
    (
        "app/services/trading/venue/coinbase_orphan_adopt.py",
        "JOIN trading_trades t ON t.id = bi.trade_id",
    ): 1,
}

SKIP_FILES = {
    "app/migrations.py",
}


def _normalize_line(line: str) -> str:
    return " ".join(line.strip().split())


def test_no_new_raw_trading_trades_live_reader_sql() -> None:
    seen: Counter[tuple[str, str]] = Counter()
    for path in APP_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in SKIP_FILES:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if READ_SQL_RE.search(line):
                seen[(rel, _normalize_line(line))] += 1

    unexpected = {
        key: count
        for key, count in sorted(seen.items())
        if count > ALLOWED_LINE_COUNTS.get(key, 0)
    }
    stale_allowlist = {
        key: expected
        for key, expected in sorted(ALLOWED_LINE_COUNTS.items())
        if seen.get(key, 0) < expected
    }

    assert unexpected == {}
    assert stale_allowlist == {}
