"""Manually invoke c_secondary learning steps that haven't run in 24h+.

Runs:
  - learning.refine_patterns(db, user_id) — tunes existing patterns
  - learning.learn_exit_optimization(db, user_id) — refines exit configs
  - learning.evolve_pattern_strategies(db) — strategy-level evolution

These normally run inside the full reconcile cycle but get starved when
brain-worker is busy. Calling directly to bypass the queue.
"""
from __future__ import annotations
from app.db import SessionLocal
from app.config import settings


def main() -> int:
    sess = SessionLocal()
    try:
        from app.services.trading import learning as L
        uid = getattr(settings, "brain_default_user_id", None)
        try:
            r = L.refine_patterns(sess, uid)
            print(f"refine_patterns: {r}")
        except Exception as e:
            print(f"refine_patterns FAILED: {type(e).__name__}: {e}")
        try:
            r = L.learn_exit_optimization(sess, uid)
            print(f"learn_exit_optimization: {r}")
        except Exception as e:
            print(f"learn_exit_optimization FAILED: {type(e).__name__}: {e}")
        try:
            r = L.evolve_pattern_strategies(sess)
            print(f"evolve_pattern_strategies: {r}")
        except Exception as e:
            print(f"evolve_pattern_strategies FAILED: {type(e).__name__}: {e}")
    finally:
        # FIX 46 pattern (rollback before close).
        try:
            sess.rollback()
        except Exception:
            pass
        sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
