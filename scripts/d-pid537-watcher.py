"""f-pattern-537-watcher — daily monitor for pid 537 (and Tier A health) until verdict.

Output contract — printed to stdout, parseable by the scheduled task:

    VERDICT_STATUS=<one of: IN_FLIGHT, COMPLETE_POSITIVE, COMPLETE_NEGATIVE, ALERT, REGRESSION>
    VERDICT_REASON=<short one-line reason>
    PID_537_N=<int>
    PID_537_WR=<float or NULL>
    PID_537_PAYOFF=<float or NULL>
    PID_537_STAGE=<string>
    PID_585_STAGE=<string>
    TIER_A_PROTECTED=<int — count of patterns with payoff>=1.5 AND n>=5>

Then human-readable details follow under '--- details ---' for the chat reader.

Exit codes:
  0 — IN_FLIGHT or COMPLETE_POSITIVE (healthy)
  2 — COMPLETE_NEGATIVE or ALERT (operator attention needed)
  3 — REGRESSION (Tier A protection silently disabled)
  1 — probe error
"""
from __future__ import annotations
import os
import sys
import traceback
from datetime import datetime, timezone

try:
    import psycopg2
    import psycopg2.extras
except Exception as e:
    print(f"VERDICT_STATUS=ALERT")
    print(f"VERDICT_REASON=psycopg2 import failed: {e}")
    sys.exit(1)


# Thresholds for pid 537 verdict
N_VERDICT_FLOOR = 15           # n at which we render a verdict
WR_DEGRADED_FLOOR = 0.50       # WR below this mid-flight = flag
PAYOFF_DEGRADED_FLOOR = 3.0    # payoff_ratio below this mid-flight = flag


def main() -> int:
    db_url = os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=10)
    except Exception as e:
        print(f"VERDICT_STATUS=ALERT")
        print(f"VERDICT_REASON=DB connect failed: {e}")
        return 1
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ---------- pid 537 state ----------
    try:
        cur.execute("""
            SELECT id, lifecycle_stage, demoted_at, promotion_demote_reason,
                   promotion_status, payoff_ratio, payoff_ratio_n,
                   payoff_ratio_updated_at,
                   raw_realized_trade_count, raw_realized_win_rate,
                   raw_realized_avg_return_pct,
                   cpcv_median_sharpe, promotion_gate_passed
            FROM scan_patterns WHERE id=537
        """)
        p537 = cur.fetchone()
    except Exception as e:
        print(f"VERDICT_STATUS=ALERT")
        print(f"VERDICT_REASON=pid 537 SELECT failed: {e}")
        return 1

    if not p537:
        print(f"VERDICT_STATUS=ALERT")
        print(f"VERDICT_REASON=pid 537 row not found (was the pattern deleted?)")
        return 2

    # ---------- pid 585 state ----------
    cur.execute("SELECT lifecycle_stage FROM scan_patterns WHERE id=585")
    p585 = cur.fetchone()
    p585_stage = (p585 or {}).get("lifecycle_stage") or "(missing)"

    # ---------- Tier A coverage ----------
    cur.execute("""
        SELECT
          COUNT(*) FILTER (WHERE payoff_ratio IS NOT NULL) AS with_payoff,
          COUNT(*) FILTER (WHERE payoff_ratio_n >= 5) AS with_n5,
          COUNT(*) FILTER (WHERE payoff_ratio >= 1.5 AND payoff_ratio_n >= 5)
              AS protected,
          COUNT(*) FILTER (WHERE quality_composite_score IS NOT NULL) AS scored
        FROM scan_patterns
    """)
    cov = cur.fetchone() or {}

    # ---------- 7d realized PnL ----------
    cur.execute("""
        SELECT COUNT(*) AS n, SUM(pnl)::numeric(12,2) AS pnl
        FROM trading_management_envelopes
        WHERE status='closed' AND pnl IS NOT NULL
          AND entry_date > NOW() - INTERVAL '7 days'
    """)
    last7 = cur.fetchone() or {}

    cur.execute("""
        SELECT COUNT(*) AS n, SUM(pnl)::numeric(12,2) AS pnl
        FROM trading_management_envelopes
        WHERE status='closed' AND pnl IS NOT NULL
          AND scan_pattern_id=537
    """)
    p537_lifetime = cur.fetchone() or {}

    # ---------- verdict logic ----------
    n = int(p537.get("raw_realized_trade_count") or 0)
    wr = p537.get("raw_realized_win_rate")
    payoff = p537.get("payoff_ratio")
    payoff_n = int(p537.get("payoff_ratio_n") or 0)
    stage = (p537.get("lifecycle_stage") or "").strip()

    # Use the larger of raw_realized_trade_count and payoff_ratio_n for the n
    # check — both should agree but realized_stats_sync is the more frequently
    # refreshed counter on a healthy system.
    n_for_verdict = max(n, payoff_n)

    verdict = "IN_FLIGHT"
    reason = f"pid 537 n={n_for_verdict}; awaiting n>={N_VERDICT_FLOOR}"

    # 1. Regression check first — if Tier A protections appear disabled,
    #    that's the most urgent thing to surface.
    if int(cov.get("protected", 0) or 0) < 1:
        verdict = "REGRESSION"
        reason = (
            "Tier A protection count is 0 — payoff-ratio gate may be silently "
            "disabled. Expected >=1 (pattern 585 alone qualifies)."
        )
    elif int(cov.get("scored", 0) or 0) > int(cov.get("with_n5", 0) or 0) + 5:
        # Composite floor: scored should approximate with_n5 (within tolerance).
        # If scored explodes relative to with_n5, the floor isn't firing.
        verdict = "REGRESSION"
        reason = (
            f"composite scored={cov.get('scored')} vs with_n5={cov.get('with_n5')} — "
            "composite n>=5 floor may have been disabled."
        )

    # 2. Pattern 585 sanity — should be promoted or pilot_promoted, NOT decayed.
    if verdict == "IN_FLIGHT" and p585_stage in ("decayed", "retired", "challenged"):
        verdict = "REGRESSION"
        reason = (
            f"pattern 585 lifecycle_stage='{p585_stage}' — was 'promoted' at "
            "watcher creation. Demote should not happen under Tier A gates."
        )

    # 3. Pid 537 stays in pilot_promoted/promoted, not demoted back.
    if verdict == "IN_FLIGHT" and stage in ("challenged", "decayed", "retired"):
        verdict = "ALERT"
        reason = (
            f"pid 537 was demoted to '{stage}' since promotion. "
            f"Check promotion_demote_reason='{p537.get('promotion_demote_reason')}'."
        )

    # 4. Mid-flight degradation (before n reaches 15).
    if verdict == "IN_FLIGHT" and n_for_verdict < N_VERDICT_FLOOR:
        if wr is not None and float(wr) < WR_DEGRADED_FLOOR:
            verdict = "ALERT"
            reason = (
                f"pid 537 WR={float(wr):.2f} below {WR_DEGRADED_FLOOR} floor "
                f"at n={n_for_verdict}. Mid-flight degradation."
            )
        elif payoff is not None and float(payoff) < PAYOFF_DEGRADED_FLOOR:
            verdict = "ALERT"
            reason = (
                f"pid 537 payoff_ratio={float(payoff):.2f} below "
                f"{PAYOFF_DEGRADED_FLOOR} floor at n={n_for_verdict}. "
                "Substantive payoff collapsing."
            )

    # 5. Verdict at n >= 15.
    if verdict == "IN_FLIGHT" and n_for_verdict >= N_VERDICT_FLOOR:
        wrf = float(wr) if wr is not None else None
        pf = float(payoff) if payoff is not None else None
        if wrf is not None and wrf >= WR_DEGRADED_FLOOR \
                and pf is not None and pf >= PAYOFF_DEGRADED_FLOOR:
            verdict = "COMPLETE_POSITIVE"
            reason = (
                f"pid 537 reached n={n_for_verdict} with WR={wrf:.2f}, "
                f"payoff_ratio={pf:.2f}. Edge confirmed; consider elevating "
                "to 'shadow_promoted' or 'promoted'."
            )
        else:
            verdict = "COMPLETE_NEGATIVE"
            reason = (
                f"pid 537 reached n={n_for_verdict} but stats degraded "
                f"(WR={wrf}, payoff={pf}). Re-demote candidate; original "
                "promotion was thin-sample artifact."
            )

    # ---------- emit machine-readable status ----------
    def fmt(v):
        if v is None:
            return "NULL"
        if isinstance(v, float):
            return f"{v:.4f}"
        return str(v)

    print(f"VERDICT_STATUS={verdict}")
    print(f"VERDICT_REASON={reason}")
    print(f"PID_537_N={n_for_verdict}")
    print(f"PID_537_WR={fmt(wr)}")
    print(f"PID_537_PAYOFF={fmt(payoff)}")
    print(f"PID_537_STAGE={stage}")
    print(f"PID_585_STAGE={p585_stage}")
    print(f"TIER_A_PROTECTED={cov.get('protected', 0)}")
    print(f"TIER_A_SCORED={cov.get('scored', 0)}")
    print(f"TIER_A_WITH_N5={cov.get('with_n5', 0)}")

    # ---------- human-readable detail ----------
    print()
    print("--- details ---")
    print(f"Timestamp (UTC): {datetime.now(timezone.utc).isoformat()}")
    print(f"pid 537 full state:")
    for k, v in p537.items():
        print(f"  {k}: {v}")
    print(f"\npid 585 lifecycle_stage: {p585_stage}")
    print(f"\nTier A coverage: {dict(cov)}")
    print(f"\n7d total: n={last7.get('n')} pnl={last7.get('pnl')}")
    print(f"pid 537 lifetime: n={p537_lifetime.get('n')} pnl={p537_lifetime.get('pnl')}")

    # Exit code mapping
    if verdict == "REGRESSION":
        return 3
    if verdict in ("ALERT", "COMPLETE_NEGATIVE"):
        return 2
    # IN_FLIGHT or COMPLETE_POSITIVE
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"VERDICT_STATUS=ALERT")
        print(f"VERDICT_REASON=watcher crashed: {e}")
        traceback.print_exc()
        sys.exit(1)
