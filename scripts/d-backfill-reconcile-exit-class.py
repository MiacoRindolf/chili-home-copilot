"""Backfill: re-label historical live_cancelled/cancelled momentum outcome rows
that completed a REAL round-trip under the reconcile-exit-class fix (#518), and
re-ingest them so the strategy learner finally sees the win/loss.

Why a backfill: #518 fixed the GO-FORWARD labeling, but rows persisted before the
deploy still carry the old cancelled_* label (+ contributes_to_evolution=False).
This re-derives outcome_class with the current logic (composing #517's durable
entry + #518's reconcile reroute), flips contributes where it now qualifies, and
re-ingests via the normal ingest_session_outcome path.

⚠️ CONSEQUENTIAL: re-ingesting feeds these real losses into the viability nudge
AND maybe_kill_underperforming_variant / maybe_publish_refined_variant. A bulk
load of historical losses can trip the variant-kill gate (wr<0.35 AND
mean_return_bps<-30 AND n>=5 over 90d). The dry-run prints a KILL PROJECTION so
the effect is visible BEFORE applying; --apply logs which variants get
killed/refined and how to reactivate.

Usage:
  conda run -n chili-env python scripts/d-backfill-reconcile-exit-class.py            # dry-run (census + kill projection)
  conda run -n chili-env python scripts/d-backfill-reconcile-exit-class.py --apply    # relabel + re-ingest (option c)
  conda run -n chili-env python scripts/d-backfill-reconcile-exit-class.py --apply --no-reingest  # relabel only (option b)
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("CHILI_APP_NAME", "chili-reconcile-backfill")

from app.db import SessionLocal  # noqa: E402
from app.models.trading import (  # noqa: E402
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    MomentumSymbolViability,
)
from app.services.trading.momentum_neural.evolution import ingest_session_outcome  # noqa: E402
from app.services.trading.momentum_neural.outcome_extract import (  # noqa: E402
    derive_outcome_class,
    outcome_evolution_credit_from_extracted,
)

# Kill-gate constants (mirror maybe_kill_underperforming_variant in evolution.py).
_KILL_MIN_N = 5
_KILL_WR = 0.35
_KILL_MEAN_BPS = -30.0
_KILL_LOOKBACK_DAYS = 90

_TERMINAL = ("live_cancelled", "cancelled")


def _effective_entry(stored_entry: bool, summary: dict, realized_pnl) -> bool:
    # Mirror _entry_occurred_durable (#517): realized P&L or a recorded exit-entry
    # price is durable proof of a real entry fill.
    if stored_entry:
        return True
    if realized_pnl is not None:
        return True
    if summary.get("last_exit_entry_price") is not None:
        return True
    return False


def _new_label_for(row: MomentumAutomationOutcome) -> tuple[str, bool, bool]:
    """Return (new_outcome_class, new_contributes, eff_entry) under current logic."""
    summary = row.extracted_summary_json if isinstance(row.extracted_summary_json, dict) else {}
    credit = summary.get("evolution_credit") if isinstance(summary.get("evolution_credit"), dict) else {}
    stored_entry = bool(summary.get("entry_occurred"))
    partial = bool(summary.get("partial_exit_occurred"))
    gov = row.governance_context_json if isinstance(row.governance_context_json, dict) else {}
    eff_entry = _effective_entry(stored_entry, summary, row.realized_pnl_usd)
    new_class = derive_outcome_class(
        mode=row.mode,
        terminal_state=row.terminal_state,
        entry_occurred=eff_entry,
        partial_exit=partial,
        realized_pnl_usd=row.realized_pnl_usd,
        return_bps=row.return_bps,
        exit_reason=row.exit_reason,
        governance_context=gov,
        events=[],
    )
    extracted = {
        "entry_occurred": eff_entry,
        "entry_decision_packet_id": credit.get("entry_decision_packet_id"),
        "return_bps": row.return_bps,
        "realized_pnl_usd": row.realized_pnl_usd,
        "outcome_class": new_class,
        "mode": row.mode,
        "quote_source_at_entry": summary.get("quote_source_at_entry"),
    }
    new_contrib = bool(outcome_evolution_credit_from_extracted(extracted)["contributes_to_evolution"])
    return new_class, new_contrib, eff_entry


def _plan(db) -> list[tuple[MomentumAutomationOutcome, str, bool]]:
    rows = (
        db.query(MomentumAutomationOutcome)
        .filter(MomentumAutomationOutcome.terminal_state.in_(_TERMINAL))
        .order_by(MomentumAutomationOutcome.session_id)
        .all()
    )
    plan = []
    for row in rows:
        new_class, new_contrib, _ = _new_label_for(row)
        # Scope: rows whose outcome_class actually changes (the mislabeled reconcile
        # round-trips). A row whose class is unchanged but whose contributes flag
        # would re-derive differently is a pre-existing anomaly outside this fix —
        # leave it untouched.
        if new_class != (row.outcome_class or ""):
            plan.append((row, new_class, new_contrib))
    return plan


def _kill_projection(db, affected_variants: set[int], plan) -> None:
    """For each affected variant, project the post-backfill 90d contributing
    sample and whether maybe_kill_underperforming_variant would fire."""
    since = datetime.utcnow() - timedelta(days=_KILL_LOOKBACK_DAYS)
    # session_ids that will newly contribute
    newly_contrib = {int(r.session_id) for r, _nc, ncontrib in plan if ncontrib and not bool(r.contributes_to_evolution)}
    print("\n## KILL / REFINE PROJECTION (post-backfill 90d contributing sample)")
    print(f"   gate: n>={_KILL_MIN_N} AND win_rate<{_KILL_WR} AND mean_return_bps<{_KILL_MEAN_BPS}")
    for vid in sorted(affected_variants):
        var = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == vid).one_or_none()
        active = bool(var.is_active) if var else None
        # current contributing sample (return_bps not null, contributes True, 90d)
        existing = (
            db.query(MomentumAutomationOutcome)
            .filter(
                MomentumAutomationOutcome.variant_id == vid,
                MomentumAutomationOutcome.contributes_to_evolution.is_(True),
                MomentumAutomationOutcome.return_bps.isnot(None),
                MomentumAutomationOutcome.terminal_at >= since,
            )
            .all()
        )
        existing_bps = [float(o.return_bps) for o in existing]
        # add the newly-contributing backfilled rows for this variant
        add = [
            float(r.return_bps)
            for r, _nc, ncontrib in plan
            if ncontrib and int(r.variant_id) == vid and int(r.session_id) in newly_contrib and r.return_bps is not None
        ]
        sample = existing_bps + add
        n = len(sample)
        if n == 0:
            print(f"   variant {vid} (active={active}): no contributing sample")
            continue
        wins = sum(1 for b in sample if b > 0)
        wr = wins / n
        mean_bps = sum(sample) / n
        would_kill = n >= _KILL_MIN_N and wr < _KILL_WR and mean_bps < _KILL_MEAN_BPS
        flag = "  <== WOULD KILL" if would_kill else ""
        print(
            f"   variant {vid} (active={active}): n {len(existing_bps)}->{n} (+{len(add)}), "
            f"wr={wr:.2f}, mean_bps={mean_bps:.1f}{flag}"
        )


def _variant_active_census(db, variant_ids: set[int]) -> dict[int, bool]:
    out = {}
    for vid in variant_ids:
        v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == vid).one_or_none()
        out[vid] = bool(v.is_active) if v else None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="persist relabel + re-ingest (default: dry-run)")
    ap.add_argument("--no-reingest", action="store_true", help="relabel only; skip the learner re-ingest (option b)")
    ap.add_argument("--trial", action="store_true", help="run the FULL apply path against live data but ROLL BACK at the end (validation; persists nothing)")
    args = ap.parse_args()
    commit = not args.trial

    with SessionLocal() as db:
        plan = _plan(db)
        affected_variants = {int(r.variant_id) for r, _nc, _co in plan}
        class_flips = sum(1 for r, nc, _co in plan if nc != (r.outcome_class or ""))
        contrib_flips = sum(1 for r, _nc, co in plan if co != bool(r.contributes_to_evolution))
        newly_contrib = sum(1 for r, _nc, co in plan if co and not bool(r.contributes_to_evolution))

        print(f"## PLAN: {len(plan)} rows change "
              f"(outcome_class flips={class_flips}, contributes flips={contrib_flips}, "
              f"newly-contributing={newly_contrib})")
        dist = defaultdict(int)
        for r, nc, co in plan:
            dist[f"{r.outcome_class}->{nc} contrib={bool(r.contributes_to_evolution)}->{co}"] += 1
        for k, n in sorted(dist.items(), key=lambda kv: -kv[1]):
            print(f"   {n:>3}  {k}")
        print(f"\naffected variants: {sorted(affected_variants)}")
        print(f"active BEFORE: {_variant_active_census(db, affected_variants)}")

        _kill_projection(db, affected_variants, plan)

        if not args.apply and not args.trial:
            print("\n[dry-run] no changes written. Re-run with --apply to relabel"
                  + (" (relabel only)" if args.no_reingest else " + re-ingest").rstrip())
            return 0

        if not plan:
            print("\nnothing to backfill.")
            return 0

        mode_tag = "trial" if args.trial else "apply"

        # Pass 1: relabel all rows (outcome_class + contributes + evolution_credit summary).
        for row, new_class, new_contrib in plan:
            prev_class = row.outcome_class
            prev_contrib = bool(row.contributes_to_evolution)
            summary = dict(row.extracted_summary_json or {})
            _nc, _nco, eff_entry = _new_label_for(row)
            credit = dict(summary.get("evolution_credit") or {})
            extracted = {
                "entry_occurred": eff_entry,
                "entry_decision_packet_id": credit.get("entry_decision_packet_id"),
                "return_bps": row.return_bps,
                "realized_pnl_usd": row.realized_pnl_usd,
                "outcome_class": new_class,
                "mode": row.mode,
                "quote_source_at_entry": summary.get("quote_source_at_entry"),
            }
            fresh_credit = outcome_evolution_credit_from_extracted(extracted)
            row.outcome_class = new_class
            row.contributes_to_evolution = bool(new_contrib)
            summary["evolution_credit"] = fresh_credit
            summary["reconcile_exit_class_backfill"] = {
                "at_utc": datetime.utcnow().isoformat() + "Z",
                "prev_outcome_class": prev_class,
                "prev_contributes": prev_contrib,
                "new_outcome_class": new_class,
                "new_contributes": bool(new_contrib),
            }
            row.extracted_summary_json = summary
        if commit:
            db.commit()
        else:
            db.flush()
        print(f"\n[{mode_tag}] relabeled {len(plan)} rows ({'committed' if commit else 'in-transaction'}).")

        if args.no_reingest:
            print(f"[{mode_tag}] --no-reingest: skipped learner re-ingest (option b).")
            print(f"active AFTER: {_variant_active_census(db, affected_variants)}")
            if not commit:
                db.rollback()
                print(f"[{mode_tag}] ROLLED BACK — nothing persisted.")
            return 0

        # Pass 2: re-ingest each newly/again-contributing row through the normal path.
        ingested = 0
        for row, _nc, new_contrib in plan:
            if not new_contrib:
                continue
            res = ingest_session_outcome(db, row, force=False, source="reconcile_exit_class_backfill")
            if commit:
                db.commit()
            else:
                db.flush()
            ingested += 1
            print(f"   ingest sess={row.session_id} variant={row.variant_id} "
                  f"class={row.outcome_class} applied={res.get('contribution_applied')}")
        print(f"\n[{mode_tag}] re-ingested {ingested} contributing rows.")

        after = _variant_active_census(db, affected_variants)
        print(f"active AFTER: {after}")
        killed = [vid for vid, a in after.items() if a is False]
        if killed:
            print(f"\n⚠️ variants DEACTIVATED by re-ingest: {killed}")
            print("   reactivate one with:")
            print("   UPDATE momentum_strategy_variants SET is_active=true, updated_at=now() WHERE id=<vid>;")
            print("   UPDATE momentum_symbol_viability SET paper_eligible=true, live_eligible=true WHERE variant_id=<vid>;")
        else:
            print("   (no variants deactivated — consistent with the kill projection)")

        if not commit:
            db.rollback()
            print(f"\n[{mode_tag}] ROLLED BACK — nothing persisted. This validated the full apply path against live data.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
