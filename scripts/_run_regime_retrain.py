"""One-shot regime retrain. Reports stats before + after."""
from __future__ import annotations
from sqlalchemy import text
from app.db import SessionLocal


def main() -> int:
    sess = SessionLocal()
    try:
        r = sess.execute(text(
            "SELECT count(*) AS n, max(as_of) AS most_recent FROM regime_snapshot"
        )).fetchone()
        print(f"BEFORE: rows={r.n}  most_recent={r.most_recent}")
        sess.rollback()

        from app.services.trading.regime_classifier import run_weekly_regime_retrain
        out = run_weekly_regime_retrain(sess)
        print(f"RETRAIN result: {out}")

        r = sess.execute(text(
            "SELECT count(*) AS n, max(as_of) AS most_recent FROM regime_snapshot"
        )).fetchone()
        print(f"AFTER: rows={r.n}  most_recent={r.most_recent}")
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
