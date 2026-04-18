"""Parse Chili Telegram chat export HTML and extract every IMMINENT PATTERN
alert as structured rows: (ticker, pattern_id, pattern_name, sent_at_utc).

The chat export uses two message body formats depending on the formatter
version that was live at the time:

  Legacy (plain-text):
    IMMINENT PATTERN: <pattern name> (#<id>)<br><TICKER> @ $price | ...

  Rich HTML (current):
    ?? <strong>IMMINENT PATTERN</strong>  <code>TICKER</code>  #ID<br>
    <em>Pattern Name</em><br>...

Writes the normalised list to scripts/sql/imminent_alerts_from_telegram.json.
"""
from __future__ import annotations

import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

EXPORT_DIR = Path(r"C:\Users\rindo\Downloads\Telegram Desktop\ChatExport_2026-04-18")
OUT_PATH = Path(__file__).parent / "sql" / "imminent_alerts_from_telegram.json"

# Regex to split each message block. Each message has a pull_right date div
# with an ISO-ish title, followed (somewhere below) by a text div. We capture
# the title timestamp + the first text div after it.
MSG_RE = re.compile(
    r'class="pull_right date details"\s+title="([^"]+)"'
    r'[\s\S]*?<div class="text">([\s\S]*?)</div>',
    re.IGNORECASE,
)

# Legacy body (plain text)
LEGACY_RE = re.compile(
    r'IMMINENT PATTERN:\s*(?P<name>.+?)\s*\(#(?P<pid>\d+)\)\s*<br>\s*'
    r'(?P<ticker>[A-Z0-9.\-]+)\s*@',
    re.IGNORECASE,
)

# Rich HTML body
RICH_RE = re.compile(
    r'<strong>IMMINENT PATTERN</strong>\s*'
    r'<code>(?P<ticker>[A-Z0-9.\-]+)</code>\s*#(?P<pid>\d+)\s*<br>\s*'
    r'<em>(?P<name>.+?)</em>',
    re.IGNORECASE,
)


def parse_ts(title: str) -> str:
    """Telegram title looks like '02.04.2026 06:34:31 UTC-08:00'.
    Return UTC ISO-8601."""
    m = re.match(
        r'(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2}):(\d{2})\s+UTC([+-])(\d{2}):(\d{2})',
        title.strip(),
    )
    if not m:
        raise ValueError(f"Unrecognised timestamp format: {title}")
    d, mo, y, H, M, S, sign, oh, om = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(H), int(M), int(S))
    offset = timedelta(hours=int(oh), minutes=int(om))
    if sign == "-":
        dt_utc = dt + offset
    else:
        dt_utc = dt - offset
    return dt_utc.replace(tzinfo=timezone.utc).isoformat()


def extract_alerts(body: str) -> list[dict]:
    out: list[dict] = []
    for m in LEGACY_RE.finditer(body):
        out.append({
            "ticker": m.group("ticker").upper(),
            "pattern_id": int(m.group("pid")),
            "pattern_name": html.unescape(m.group("name").strip()),
            "format": "legacy",
        })
    for m in RICH_RE.finditer(body):
        out.append({
            "ticker": m.group("ticker").upper(),
            "pattern_id": int(m.group("pid")),
            "pattern_name": html.unescape(m.group("name").strip()),
            "format": "rich",
        })
    return out


def main() -> None:
    files = sorted(EXPORT_DIR.glob("messages*.html"))
    if not files:
        print(f"No HTML files found in {EXPORT_DIR}", file=sys.stderr)
        sys.exit(1)

    all_rows: list[dict] = []
    for fp in files:
        text = fp.read_text(encoding="utf-8", errors="replace")
        count_before = len(all_rows)
        for msg in MSG_RE.finditer(text):
            ts_raw, body = msg.group(1), msg.group(2)
            alerts = extract_alerts(body)
            if not alerts:
                continue
            ts = parse_ts(ts_raw)
            for a in alerts:
                a["sent_at_utc"] = ts
                all_rows.append(a)
        print(f"{fp.name}: +{len(all_rows) - count_before} imminent alerts")

    # Sort by timestamp ascending so the DB update picks the latest alert
    # per (ticker) via ORDER BY ... LIMIT 1 in SQL.
    all_rows.sort(key=lambda r: r["sent_at_utc"])

    # Stats
    by_ticker: dict[str, int] = {}
    by_pattern: dict[int, int] = {}
    for r in all_rows:
        by_ticker[r["ticker"]] = by_ticker.get(r["ticker"], 0) + 1
        by_pattern[r["pattern_id"]] = by_pattern.get(r["pattern_id"], 0) + 1

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_rows, indent=2), encoding="utf-8")

    print()
    print(f"Total alerts:            {len(all_rows)}")
    print(f"Distinct tickers:        {len(by_ticker)}")
    print(f"Distinct pattern IDs:    {len(by_pattern)}")
    if all_rows:
        print(f"Earliest:                {all_rows[0]['sent_at_utc']}")
        print(f"Latest:                  {all_rows[-1]['sent_at_utc']}")
    print()
    top_tickers = sorted(by_ticker.items(), key=lambda kv: -kv[1])[:15]
    print("Top 15 tickers by alert count:")
    for t, n in top_tickers:
        print(f"  {t:<12} {n}")
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
