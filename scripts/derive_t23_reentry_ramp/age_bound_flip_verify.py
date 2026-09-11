# [23] re-entry ramp derivation (PR #1412). READ-ONLY against the live DB: point
# DATABASE_URL at it privately; every connection sets default_transaction_read_only.
"""[23] review fix verification (READ-ONLY, bounded): at the three tape+ instants the
reviewer found flipping stale->fresh under count_v1's self-raised bound, read the SHIPPED
helper (signed_tape_accel_features, count_v1, window_prints=255, as_of=<instant>) and report
the stamp the fixed G4 bar now decides on. default_transaction_read_only=on, 20 s timeout,
every tick read symbol-scoped and LIMIT 255 (the helper's own query)."""
import os
import sys
from datetime import datetime

os.environ["CHILI_PYTEST"] = "1"
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.services.trading.momentum_neural.entry_gates import signed_tape_accel_features  # noqa: E402

eng = create_engine(
    os.environ["DATABASE_URL"],
    connect_args={"options": "-c statement_timeout=20000 -c default_transaction_read_only=on"},
)
db = sessionmaker(bind=eng)()
FLOOR = 14.69
INSTANTS = [
    ("DPU", datetime(2026, 9, 9, 21, 36, 2, 550000)),
    ("WYHG", datetime(2026, 9, 8, 22, 12, 2)),
    ("DLTH", datetime(2026, 9, 3, 13, 24, 55)),
]
for sym, ts in INSTANTS:
    out = signed_tape_accel_features(sym, db=db, as_of=ts, window_prints=255, feature_contract="count_v1")
    if out is None:
        print(sym, ts.isoformat(), "no tape")
        continue
    old_bound = max(FLOOR, out.get("gap_p99_s") or 0.0)
    age = out.get("print_age_s")
    print(
        sym, ts.isoformat(),
        "n", out.get("n_ticks"),
        "age", round(age, 2) if age is not None else None,
        "old_59_bound", round(old_bound, 2),
        "old_verdict", ("stale" if age is not None and age > old_bound else "fresh"),
        "helper_bound", out.get("print_age_bound_s"),
        "helper_stale", out.get("print_stale"),
        "gap_p99", round(out.get("gap_p99_s") or 0, 2),
        "gap_trim_s", round(out.get("gap_trim_s") or 0, 2),
        "p90", round(out.get("gap_trim_window_p90_s") or 0, 3),
        "span_s", round(out.get("span_s") or 0, 1),
        "accel", round(out.get("signed_tape_accel") or 0, 1),
        "bsd", round(out.get("buy_share_delta") or 0, 4),
        flush=True,
    )
db.close()
