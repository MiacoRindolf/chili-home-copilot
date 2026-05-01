"""Phase 4 cleanup — ELTX intent (id=224) was incorrectly transitioned
to confirmed_at_broker on 2026-05-01 17:50:21 because the writer treated
"unconfirmed" API response as success. Robinhood actually cancelled the
order within 250ms (verified via direct order lookup). Mark the intent
terminal_reject so the writer stops retrying."""
import sys
sys.path.insert(0, "/app")

from sqlalchemy.orm import Session
from app.db import SessionLocal
from app.services.trading.bracket_intent_writer import (
    IntentState, transition,
)


def main():
    s: Session = SessionLocal()
    try:
        result = transition(
            s, 224,
            to_state=IntentState.TERMINAL_REJECT,
            reason="phase4_post_accept_verification:eltx_robinhood_auto_cancels",
        )
        print(f"transition result: ok={result.ok} prev={result.prev_state} new={result.new_state} reason={result.reason}")
        if result.ok:
            s.commit()
            print("committed.")
    finally:
        s.close()


if __name__ == "__main__":
    main()
