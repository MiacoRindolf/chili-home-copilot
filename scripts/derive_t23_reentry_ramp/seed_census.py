# [23] re-entry ramp derivation (PR #1412). READ-ONLY against the live DB: point
# DATABASE_URL at it privately; every connection sets default_transaction_read_only.
"""[23] review fix: prior_day_rejection_seed, old rule (#1252 SQL ``LIKE '%stop%' OR
'%bailout%'``) vs new rule (``reentry_ramp_strike_class``), over every red
``live_exit_filled`` of the last 30 days grouped by ET day / symbol / reason.
READ-ONLY (default_transaction_read_only=on, statement_timeout 20 s); one GROUP BY query.
Result 2026-09-11: 40 symbol-days with a red live exit; old 35 seeded, new 36; the only
change is LBGJ 2026-09-11 (tape_accel_rollover -$40.00); none lost."""
import os
import sys
from collections import defaultdict

os.environ["CHILI_PYTEST"] = "1"
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.services.trading.momentum_neural.risk_policy import reentry_ramp_strike_class  # noqa: E402

eng = create_engine(
    os.environ["DATABASE_URL"],
    connect_args={"options": "-c statement_timeout=20000 -c default_transaction_read_only=on"},
)
SQL = text(
    """
    SELECT (e.ts AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')::date AS et_day,
           s.symbol, e.payload_json->>'reason' AS reason, count(*) AS n,
           round(sum((e.payload_json->>'pnl_usd')::numeric), 2) AS pnl
    FROM trading_automation_events e
    JOIN trading_automation_sessions s ON s.id = e.session_id
    WHERE e.event_type = 'live_exit_filled' AND e.ts >= now() - interval '30 days'
      AND (e.payload_json->>'pnl_usd')::float < 0
    GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
    """
)
by = defaultdict(list)
with eng.connect() as c:
    for d, sym, reason, n, pnl in c.execute(SQL):
        by[(str(d), sym)].append((reason, int(n), float(pnl)))

old_n = new_n = 0
flips_on, flips_off = [], []
for (d, sym), rs in sorted(by.items()):
    old = any(("stop" in (r or "")) or ("bailout" in (r or "")) for r, _, _ in rs)
    new = any(reentry_ramp_strike_class(r) is not None for r, _, _ in rs)
    old_n += old
    new_n += new
    if new and not old:
        flips_on.append((d, sym, rs))
    if old and not new:
        flips_off.append((d, sym, rs))
print("symbol-days with a red live exit:", len(by))
print("seeded old rule:", old_n, " new rule:", new_n)
print("new-only (seeded now, not before):", len(flips_on))
for f in flips_on:
    print("  ", f)
print("old-only (seeded before, not now):", len(flips_off))
for f in flips_off:
    print("  ", f)
