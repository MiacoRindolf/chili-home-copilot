"""Weekly TCA probe for the maker-only paper-soak.

Compares avg entry slippage bps before vs after the flag flip.

Output schema (machine-readable header):

    BASELINE_AVG_BPS=153.93   # Coinbase, 30d ending at flip
    SINCE_FLIP_AVG_BPS=<float>
    SINCE_FLIP_N=<int>
    DELTA_BPS=<float>          # negative = improvement
    MAKER_ROUTED_COUNT=<int>   # rows tagged _chili_maker_only=True
    MARKET_ROUTED_COUNT=<int>  # rows without the tag
    VERDICT=<one of: IN_FLIGHT, IMPROVED, NO_CHANGE, REGRESSED>

Exit codes:
  0 — IN_FLIGHT (n < threshold OR no rows yet)
  0 — IMPROVED (avg < 30 bps)
  2 — NO_CHANGE (delta within ±20 bps of baseline)
  2 — REGRESSED (avg > baseline + 20 bps)
  1 — probe error
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone

try:
    import psycopg2
    import psycopg2.extras
except Exception as e:
    print(f"VERDICT=ALERT")
    print(f"ERROR=psycopg2 import failed: {e}")
    sys.exit(1)

DB = os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")

# Pre-flip baseline (recorded at flip time, frozen — operator may update
# manually if a new baseline window becomes more authoritative).
BASELINE_AVG_BPS = 153.93
BASELINE_N = 158
BASELINE_WINDOW = "30 days ending 2026-05-19 06:57Z"

# Flag-flip time (UTC). Update if the flag is ever rotated.
FLIP_TS_UTC = "2026-05-19 13:57:00"  # ~06:57 PT

# Minimum sample size before we render a non-IN_FLIGHT verdict.
MIN_N = 10
# Target: avg bps should drop materially. Improvement threshold = baseline−30 bps.
IMPROVEMENT_THRESHOLD_BPS = 30.0

print(f"# maker-only TCA probe — {datetime.now(timezone.utc).isoformat()}")

try:
    conn = psycopg2.connect(DB, connect_timeout=10)
except Exception as e:
    print(f"VERDICT=ALERT")
    print(f"ERROR=DB connect failed: {e}")
    sys.exit(1)

conn.set_session(readonly=True, autocommit=True)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

# 1. Post-flip Coinbase avg entry bps
cur.execute(
    """
    SELECT
      COUNT(*) AS n,
      AVG(tca_entry_slippage_bps) AS avg_bps,
      STDDEV(tca_entry_slippage_bps) AS sd_bps,
      MIN(tca_entry_slippage_bps) AS min_bps,
      MAX(tca_entry_slippage_bps) AS max_bps
    FROM trading_management_envelopes
    WHERE broker_source = 'coinbase'
      AND tca_entry_slippage_bps IS NOT NULL
      AND entry_date > %s::timestamp
    """,
    (FLIP_TS_UTC,),
)
row = cur.fetchone() or {}
n_post = int(row.get("n") or 0)
avg_post = float(row["avg_bps"]) if row.get("avg_bps") is not None else None
sd_post = float(row["sd_bps"]) if row.get("sd_bps") is not None else None

# 2. Maker-routed vs market-routed count in trading_execution_events
cur.execute(
    """
    SELECT
      COUNT(*) FILTER (WHERE payload_json->>'_chili_maker_only' = 'True'
                         OR (payload_json ? '_chili_maker_only' AND (payload_json->>'_chili_maker_only')::text = 'true'))
        AS maker_routed,
      COUNT(*) FILTER (WHERE payload_json->>'_chili_maker_only' IS NULL)
        AS not_tagged
    FROM trading_execution_events
    WHERE broker_source = 'coinbase'
      AND recorded_at > %s::timestamp
    """,
    (FLIP_TS_UTC,),
)
tagrow = cur.fetchone() or {}
maker_n = int(tagrow.get("maker_routed") or 0)
market_n = int(tagrow.get("not_tagged") or 0)

# 3. Verdict
verdict = "IN_FLIGHT"
delta = None
if avg_post is not None and n_post >= MIN_N:
    delta = avg_post - BASELINE_AVG_BPS
    if avg_post < IMPROVEMENT_THRESHOLD_BPS:
        verdict = "IMPROVED"
    elif delta > 20.0:
        verdict = "REGRESSED"
    elif abs(delta) <= 20.0:
        verdict = "NO_CHANGE"
    else:
        # delta < -20 but avg >= 30 — partial improvement
        verdict = "IMPROVED"

print(f"BASELINE_AVG_BPS={BASELINE_AVG_BPS}")
print(f"BASELINE_N={BASELINE_N}")
print(f"BASELINE_WINDOW={BASELINE_WINDOW}")
print(f"FLIP_TS_UTC={FLIP_TS_UTC}")
print(f"SINCE_FLIP_N={n_post}")
print(f"SINCE_FLIP_AVG_BPS={avg_post if avg_post is not None else 'NULL'}")
print(f"SINCE_FLIP_SD_BPS={sd_post if sd_post is not None else 'NULL'}")
print(f"DELTA_BPS={delta if delta is not None else 'NULL'}")
print(f"MAKER_ROUTED_COUNT={maker_n}")
print(f"MARKET_ROUTED_COUNT={market_n}")
print(f"MIN_N_FOR_VERDICT={MIN_N}")
print(f"VERDICT={verdict}")
print()
print("# details")
print(f"  baseline source: 30d pre-flip Coinbase trades with TCA")
print(f"  improvement target: avg < {IMPROVEMENT_THRESHOLD_BPS} bps")
print(f"  raw: {dict(row)}")
print(f"  routing tag counts: {dict(tagrow)}")

conn.close()

if verdict == "IN_FLIGHT" or verdict == "IMPROVED":
    sys.exit(0)
if verdict == "REGRESSED" or verdict == "NO_CHANGE":
    sys.exit(2)
sys.exit(0)
