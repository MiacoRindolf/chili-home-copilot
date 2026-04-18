"""Cross-reference open trades against recovered Telegram IMMINENT alerts."""
import json
from collections import defaultdict
from pathlib import Path

DATA = Path(r"c:\dev\chili-home-copilot\scripts\sql\imminent_alerts_from_telegram.json")
OPEN_TICKERS = {
    "ABM", "ACHC", "ACHR", "AFJK", "AIDX", "AIFF", "AIXI", "AMUU",
    "AVR", "BNRG", "BULX", "CCCC", "CRDL", "DHC", "EKSO", "ELTX",
    "ETH-USD", "GENI", "GEO", "HAFN", "IMTX", "IMUX", "INTC", "JOB",
    "MRNA", "PED", "PFSI", "SOFX", "TLS", "VFS",
}

data = json.loads(DATA.read_text())
matches: dict[str, list[dict]] = defaultdict(list)
for r in data:
    if r["ticker"] in OPEN_TICKERS:
        matches[r["ticker"]].append(r)

print(f"{'TICKER':<10} {'#ALERTS':<8} {'LATEST DATE':<12} {'PID':<6} PATTERN NAME")
print("-" * 100)
matched = 0
for t in sorted(OPEN_TICKERS):
    rows = matches.get(t, [])
    if rows:
        latest = max(rows, key=lambda r: r["sent_at_utc"])
        date = latest["sent_at_utc"][:10]
        print(f"{t:<10} {len(rows):<8} {date:<12} #{latest['pattern_id']:<5} {latest['pattern_name']}")
        matched += 1
    else:
        print(f"{t:<10} --       --           --     (no telegram imminent)")

print("-" * 100)
print(f"Matched {matched} / {len(OPEN_TICKERS)} open tickers")
