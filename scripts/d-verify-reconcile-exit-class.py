"""Read-only verification: re-derive outcome_class for live_cancelled/cancelled
momentum outcome rows under the reconcile-exit-class fix and show the before/
after delta — including which rows flip contributes_to_evolution.

DRY-RUN ONLY. This does not mutate any row. Backfilling persisted history is a
separate, operator-gated decision (it would re-ingest historical losses into the
viability/kill/refine learner — see the CC report's "kill-risk" note).

Usage:
    set DATABASE_URL=postgresql://chili:chili@localhost:5433/chili
    conda run -n chili-env python scripts/d-verify-reconcile-exit-class.py
"""

from __future__ import annotations

import os

os.environ.setdefault("CHILI_PYTEST", "1")

from sqlalchemy import create_engine, text  # noqa: E402

from app.services.trading.momentum_neural.outcome_extract import (  # noqa: E402
    derive_outcome_class,
    outcome_evolution_credit_from_extracted,
)

DB = os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")


def _effective_entry(stored_entry: bool, realized_pnl, last_exit_entry_price) -> bool:
    # Mirror _entry_occurred_durable (#517): a realized P&L or recorded exit-entry
    # price is durable proof of a real entry fill that survives event-aging and
    # position-zeroing.
    if stored_entry:
        return True
    if realized_pnl is not None:
        return True
    if last_exit_entry_price is not None:
        return True
    return False


def main() -> None:
    eng = create_engine(DB)
    flips_class = 0
    flips_contrib = 0
    rows_seen = 0
    by_new_class: dict[str, int] = {}
    print(f"{'sess':>5} {'mode':>5} {'old_class':>20} -> {'new_class':>12} "
          f"{'contrib':>14} reason")
    with eng.connect() as c:
        rows = c.execute(text(
            """
            SELECT session_id, mode, terminal_state, outcome_class, exit_reason,
                   realized_pnl_usd, return_bps, contributes_to_evolution,
                   governance_context_json, extracted_summary_json
            FROM momentum_automation_outcomes
            WHERE terminal_state IN ('live_cancelled','cancelled')
            ORDER BY session_id
            """
        ))
        for r in rows:
            m = r._mapping
            rows_seen += 1
            summary = m["extracted_summary_json"] or {}
            credit = summary.get("evolution_credit") or {}
            stored_entry = bool(summary.get("entry_occurred"))
            partial = bool(summary.get("partial_exit_occurred"))
            gov = m["governance_context_json"] or {}
            eff_entry = _effective_entry(
                stored_entry, m["realized_pnl_usd"], summary.get("last_exit_entry_price")
            )
            new_class = derive_outcome_class(
                mode=m["mode"],
                terminal_state=m["terminal_state"],
                entry_occurred=eff_entry,
                partial_exit=partial,
                realized_pnl_usd=m["realized_pnl_usd"],
                return_bps=m["return_bps"],
                exit_reason=m["exit_reason"],
                governance_context=gov,
                events=[],
            )
            extracted = {
                "entry_occurred": eff_entry,
                "entry_decision_packet_id": credit.get("entry_decision_packet_id"),
                "return_bps": m["return_bps"],
                "realized_pnl_usd": m["realized_pnl_usd"],
                "outcome_class": new_class,
                "mode": m["mode"],
                "quote_source_at_entry": summary.get("quote_source_at_entry"),
            }
            new_contrib = outcome_evolution_credit_from_extracted(extracted)["contributes_to_evolution"]
            old_class = m["outcome_class"]
            old_contrib = bool(m["contributes_to_evolution"])
            class_changed = new_class != old_class
            contrib_changed = new_contrib != old_contrib
            if class_changed:
                flips_class += 1
                by_new_class[new_class] = by_new_class.get(new_class, 0) + 1
            if contrib_changed:
                flips_contrib += 1
            mark = " *" if class_changed else "  "
            print(
                f"{m['session_id']:>5} {m['mode']:>5} {old_class:>20} -> "
                f"{new_class:>12} {str(old_contrib)+'->'+str(new_contrib):>14} "
                f"{m['exit_reason']!r}{mark}"
            )
    print("\n--- summary ---")
    print(f"rows examined:            {rows_seen}")
    print(f"outcome_class flips:      {flips_class}")
    print(f"contributes flips:        {flips_contrib}")
    print(f"new class distribution:   {by_new_class}")


if __name__ == "__main__":
    main()
