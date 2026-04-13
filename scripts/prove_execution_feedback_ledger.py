#!/usr/bin/env python3
"""One-shot proof: paper close → ledger outcomes → debounced digest → dispatch → execution_quality_updated.

Safe use:
  - Point DATABASE_URL at a **dev/staging** Postgres (not production) unless you accept a synthetic
    paper row and durable ledger rows on that database.
  - Requires ``CHILI_PROVE_EXEC_FEEDBACK_LEDGER=1`` to mutate anything.

The debounced ``execution_feedback_digest`` work row schedules ``next_run_at`` in the future; this
script **backdates** that row so ``run_brain_work_dispatch_round`` can claim it immediately (same
pattern as a soak test, not normal product behavior).

Usage (repo root, conda env chili-env):
  CHILI_PROVE_EXEC_FEEDBACK_LEDGER=1 conda run -n chili-env python scripts/prove_execution_feedback_ledger.py
  CHILI_PROVE_EXEC_FEEDBACK_LEDGER=1 python scripts/prove_execution_feedback_ledger.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROOF_TICKER = "CHILI-PROOF-USD"
ENV_FLAG = "CHILI_PROVE_EXEC_FEEDBACK_LEDGER"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan only (no DB writes, no env flag required).",
    )
    ap.add_argument(
        "--keep-paper-row",
        action="store_true",
        help="Do not delete the synthetic PaperTrade after success (default: delete).",
    )
    args = ap.parse_args()

    if args.dry_run:
        print("[prove_exec_feedback] dry-run: would require", ENV_FLAG, "=1")
        print(f"[prove_exec_feedback] would create/close PaperTrade ticker={PROOF_TICKER}")
        print("[prove_exec_feedback] would backdate execution_feedback_digest next_run_at")
        print("[prove_exec_feedback] would run run_brain_work_dispatch_round once")
        return 0

    if os.environ.get(ENV_FLAG) != "1":
        print(f"Refusing to run: set {ENV_FLAG}=1", file=sys.stderr)
        return 2

    from app.config import settings
    from app.db import SessionLocal
    from app.models.trading import BrainWorkEvent, PaperTrade
    from app.services.trading.brain_work.dispatcher import run_brain_work_dispatch_round
    from app.services.trading.brain_work.ledger import brain_work_ledger_enabled
    from app.services.trading.paper_trading import _close_paper_trade
    from app.services.trading.brain_work.execution_hooks import on_paper_trade_closed

    if not brain_work_ledger_enabled():
        print("brain_work_ledger_enabled is False — enable ledger in settings.", file=sys.stderr)
        return 3

    uid = getattr(settings, "brain_default_user_id", None)
    if uid is None:
        print("brain_default_user_id must be set (same as worker) for digest handler.", file=sys.stderr)
        return 4

    db = SessionLocal()
    paper_id: int | None = None
    try:
        pt = PaperTrade(
            user_id=int(uid),
            scan_pattern_id=None,
            ticker=PROOF_TICKER,
            direction="long",
            entry_price=100.0,
            stop_price=90.0,
            target_price=110.0,
            quantity=1,
            status="open",
            entry_date=datetime.utcnow(),
            signal_json={"source": "prove_execution_feedback_ledger.py"},
        )
        db.add(pt)
        db.flush()
        paper_id = int(pt.id)

        _close_paper_trade(pt, exit_price=100.25, reason="proof_script")
        on_paper_trade_closed(db, pt)
        db.commit()
        print(f"[prove_exec_feedback] closed paper_trade id={paper_id} ticker={PROOF_TICKER}")

        dedupe = f"exec_fb_digest:user:{int(uid)}"
        digest = (
            db.query(BrainWorkEvent)
            .filter(
                BrainWorkEvent.dedupe_key == dedupe,
                BrainWorkEvent.event_type == "execution_feedback_digest",
                BrainWorkEvent.status.in_(("pending", "retry_wait", "processing")),
            )
            .order_by(BrainWorkEvent.id.desc())
            .first()
        )
        if not digest:
            print("No execution_feedback_digest work row — check on_paper_trade_closed.", file=sys.stderr)
            return 5
        digest.next_run_at = datetime.utcnow() - timedelta(seconds=10)
        db.add(digest)
        db.commit()
        print(f"[prove_exec_feedback] backdated digest work id={digest.id} dedupe={dedupe}")

        summary = run_brain_work_dispatch_round(db, user_id=int(uid))
        db.commit()
        print(f"[prove_exec_feedback] dispatch_round {summary}")

        recent = (
            db.query(BrainWorkEvent)
            .filter(
                BrainWorkEvent.event_type.in_(
                    (
                        "paper_trade_closed",
                        "execution_quality_updated",
                        "execution_feedback_digest",
                    )
                )
            )
            .order_by(BrainWorkEvent.id.desc())
            .limit(12)
            .all()
        )
        print("[prove_exec_feedback] recent ledger rows (newest first):")
        for r in recent:
            print(
                f"  id={r.id} kind={r.event_kind} type={r.event_type} status={r.status} "
                f"dedupe={r.dedupe_key[:48]}..."
            )

        if not args.keep_paper_row and paper_id is not None:
            db.query(PaperTrade).filter(PaperTrade.id == paper_id).delete()
            db.commit()
            print(f"[prove_exec_feedback] deleted synthetic paper_trade id={paper_id}")

        print("[prove_exec_feedback] done — check GET /api/trading/scan/status brain_runtime.work_ledger")
        return 0
    except Exception as e:
        print(f"[prove_exec_feedback] failed: {e}", file=sys.stderr)
        try:
            db.rollback()
        except Exception:
            pass
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
