"""Run the alpha portfolio promotion gate.

Default is dry-run: inspect promotion concentration, stale recert debt,
portfolio candidate scores, and broker-risk blockers without writing.

Examples:
  python scripts/run_alpha_portfolio_gate.py
  python scripts/run_alpha_portfolio_gate.py --execute
  python scripts/run_alpha_portfolio_gate.py --execute --queue-recert
  python scripts/run_alpha_portfolio_gate.py --execute --promote-shadow
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("CHILI_APP_NAME", "chili-alpha-portfolio-gate")

from app.db import SessionLocal  # noqa: E402
from app.services.trading.alpha_portfolio_gate import (  # noqa: E402
    persist_alpha_portfolio_snapshot,
    queue_recert_for_required,
    scan_alpha_portfolio,
    stage_shadow_candidates,
)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def _brief(snapshot: dict) -> dict:
    return {
        "run_id": snapshot.get("run_id"),
        "active_pattern_count": snapshot.get("active_pattern_count"),
        "exact_promoted_status_count": snapshot.get("exact_promoted_status_count"),
        "broker_risk_count": snapshot.get("broker_risk_count"),
        "broker_risk_by_sleeve": snapshot.get("broker_risk_by_sleeve"),
        "shadow_by_sleeve": snapshot.get("shadow_by_sleeve"),
        "portfolio_diversified": snapshot.get("portfolio_diversified"),
        "diversification_reasons": snapshot.get("diversification_reasons"),
        "recert_required_count": snapshot.get("recert_required_count"),
        "recert_required_patterns": snapshot.get("recert_required_patterns"),
        "pattern_585": snapshot.get("pattern_585"),
        "execution_health": snapshot.get("execution_health"),
        "full_promotion_blocked": snapshot.get("full_promotion_blocked"),
        "full_promotion_block_reasons": snapshot.get("full_promotion_block_reasons"),
        "candidate_count": snapshot.get("candidate_count"),
        "top_candidates": (snapshot.get("candidates") or [])[:10],
        "shadow_candidates": snapshot.get("shadow_candidates"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Persist gate scores/audit rows")
    parser.add_argument(
        "--queue-recert",
        action="store_true",
        help="Queue manual recert proposals for recert-required broker-risk patterns",
    )
    parser.add_argument(
        "--promote-shadow",
        action="store_true",
        help="Move selected portfolio candidates to broker-blocked shadow_promoted",
    )
    parser.add_argument("--limit", type=int, default=50, help="Candidate rows to print")
    parser.add_argument("--pattern-id", type=int, default=None, help="Restrict scan to one pattern")
    parser.add_argument("--recert-mode", default=None, help="Override recert queue mode")
    parser.add_argument("--json", action="store_true", help="Print full JSON snapshot")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    _configure_logging(args.verbose)

    db = SessionLocal()
    try:
        snapshot = scan_alpha_portfolio(
            db,
            limit=max(1, int(args.limit)),
            pattern_id=args.pattern_id,
        )

        persist_result = persist_alpha_portfolio_snapshot(
            db, snapshot, execute=bool(args.execute),
        )

        recert_result = None
        if args.queue_recert:
            recert_result = queue_recert_for_required(
                db,
                snapshot,
                execute=bool(args.execute),
                mode_override=args.recert_mode,
            )

        shadow_result = None
        if args.promote_shadow:
            shadow_result = stage_shadow_candidates(
                db,
                snapshot,
                execute=bool(args.execute),
            )

        output = {
            "dry_run": not bool(args.execute),
            "snapshot": snapshot if args.json else _brief(snapshot),
            "persist_result": persist_result,
            "recert_result": recert_result,
            "shadow_result": shadow_result,
        }
        print(json.dumps(output, default=str, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logging.exception("alpha portfolio gate failed")
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
