# [23] re-entry ramp derivation (PR #1412). READ-ONLY against the live DB: point
# DATABASE_URL at it privately; every connection sets default_transaction_read_only.
"""Read-only probe: does the G4 bar's [59] print-age bound max(14.69, window gap_p99)
self-raise above 14.69 under count_v1 (it cannot under legacy_time_split, whose 7.5-s
trim caps every in-window gap)? Bounded: symbol + time window + LIMIT on every tick read.
"""
import os
import sys
from collections import defaultdict

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.dont_write_bytecode = True
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.services.trading.momentum_neural.entry_gates import _signed_tape_features  # noqa: E402

URL = os.environ["DATABASE_URL"]
eng = create_engine(
    URL,
    connect_args={"options": "-c default_transaction_read_only=on -c statement_timeout=20000"},
)
FLOOR = 14.69
MULT = 7.82

EV_SQL = text(
    """
    SELECT DISTINCT ON (s.symbol, date_trunc('minute', e.ts)) s.symbol, e.ts, e.event_type
    FROM trading_automation_events e JOIN trading_automation_sessions s ON s.id = e.session_id
    WHERE e.event_type IN ('g4_reentry_escalation_blocked','g4_reentry_pass_unproven',
                           'g4_reentry_reclaim_proven')
      AND e.ts > now() - interval '8 days'
      AND s.symbol NOT LIKE '%-USD'
    ORDER BY s.symbol, date_trunc('minute', e.ts), e.ts
    """
)
TAPE_SQL = text(
    """
    SELECT price, size, bid, ask, EXTRACT(EPOCH FROM observed_at) FROM (
      SELECT price, size, bid, ask, observed_at, id FROM iqfeed_trade_ticks
      WHERE symbol = :s AND observed_at <= :as_of AND observed_at > :lo
        AND received_at <= :as_of AND available_at <= :as_of AND available_at >= received_at
        AND isfinite(observed_at) AND isfinite(received_at) AND isfinite(available_at)
      ORDER BY observed_at DESC, id DESC LIMIT 255
    ) t ORDER BY observed_at ASC
    """
)

stats = defaultdict(lambda: defaultdict(int))
examples = []
with eng.connect() as c:
    evs = c.execute(EV_SQL).fetchall()
    for sym, ts, et in evs:
        from datetime import timedelta, timezone

        try:
            rows = c.execute(TAPE_SQL, {"s": sym, "as_of": ts, "lo": ts - timedelta(hours=2)}).fetchall()
        except Exception:
            c.rollback()
            stats[sym]["read_timeout"] += 1
            continue
        rows = [tuple(r) for r in rows]
        as_of_ts = ts.replace(tzinfo=timezone.utc).timestamp()
        leg = _signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=0.0, split="time",
                                    window_mode="prints")
        cnt = _signed_tape_features(rows, window_s=None, tick_rate_floor_pctile=0.0, split="count",
                                    gap_trim_s=FLOOR, gap_discontinuity_mult=MULT,
                                    as_of_ts=as_of_ts, window_mode="prints")
        st = stats[sym]
        st["n"] += 1
        def _age(f):
            return (max(0.0, as_of_ts - float(f["last_ts"])) if f and f.get("last_ts") is not None else None)
        if cnt is not None:
            _ca = _age(cnt); _cb = max(FLOOR, cnt.get("gap_p99_s") or 0.0)
            _tp = (cnt["signed_tape_accel"] > 0 and (cnt.get("buy_share_delta") or 0) > 0)
            if _ca is not None and FLOOR < _ca <= _cb:
                st["count_selfraise_fresh_and_tape_plus"] += int(_tp)
                if leg is not None and _age(leg) is not None and _age(leg) > max(FLOOR, leg.get("gap_p99_s") or 0.0):
                    st["legacy_stale_count_fresh"] += 1
                if leg is None:
                    st["legacy_none_count_selfraise_fresh"] += 1
        for name, f in (("legacy", leg), ("count", cnt)):
            if f is None:
                st[f"{name}_none"] += 1
                continue
            p99 = f.get("gap_p99_s")
            bound = max(FLOOR, p99 or 0.0)
            age = max(0.0, as_of_ts - float(f["last_ts"])) if f.get("last_ts") is not None else None
            if bound > FLOOR + 1e-9:
                st[f"{name}_bound_above_floor"] += 1
            if age is not None:
                if age > bound:
                    st[f"{name}_stale"] += 1
                if FLOOR < age <= bound:
                    st[f"{name}_fresh_only_by_self_raise"] += 1
                    if name == "count" and len(examples) < 20:
                        examples.append((sym, ts.isoformat(), et, round(age, 2), round(bound, 2),
                                         round(f.get("gap_trim_s") or 0, 2), f.get("n_ticks"),
                                         f.get("print_stale"), f.get("print_age_bound_s")))
print("symbol n | legacy: none bound>floor stale fresh_by_selfraise | count: none bound>floor stale fresh_by_selfraise")
tot = defaultdict(int)
for sym, st in sorted(stats.items(), key=lambda kv: -kv[1]["n"]):
    for k, v in st.items():
        tot[k] += v
    print(sym, st["n"], "|", st["legacy_none"], st["legacy_bound_above_floor"], st["legacy_stale"],
          st["legacy_fresh_only_by_self_raise"], "|", st["count_none"], st["count_bound_above_floor"],
          st["count_stale"], st["count_fresh_only_by_self_raise"])
print("TOTAL", dict(tot))
for sym, st in stats.items():
    if st["count_selfraise_fresh_and_tape_plus"] or st["legacy_stale_count_fresh"]:
        print("  ", sym, "selfraise_fresh&tape+", st["count_selfraise_fresh_and_tape_plus"], "legacy_stale->count_fresh", st["legacy_stale_count_fresh"])
print("examples (sym, ts, event, age_s, bar_bound_s, trim_s, n_ticks, helper_print_stale, helper_bound):")
for e in examples:
    print(e)
