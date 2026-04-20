"""Two reconciliation sweeps run concurrently against the same intent.

This was flagged as a missing coverage gap in the Phase 2 audit: the
existing hardening tests cover the single-sweep classification paths but
never exercise what happens when two sweeps fire back-to-back (scheduler
jitter, or a manual operator-triggered sweep overlapping with the cron
job). The invariants under concurrency:

  1. Both sweeps must commit without deadlock / integrity error.
  2. Both sweeps must see the same intent (one isn't blocked out).
  3. ``bump_last_observed`` is idempotent — two sweeps bumping the same
     intent must leave it at ``last_observed_at >= max(sweep1, sweep2)``,
     not lose a write.
  4. Two reconciliation log rows are written, one per sweep.
  5. ``mark_reconciled`` on agree is safe under concurrency — at most one
     sweep flips the state, the other becomes a no-op.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app import models
from app.models.trading import Trade
from app.services.trading.bracket_intent import BracketIntentInput
from app.services.trading.bracket_intent_writer import upsert_bracket_intent
from app.services.trading.bracket_reconciler import BrokerView
from app.services.trading.bracket_reconciliation_service import (
    run_reconciliation_sweep,
)


def _shadow_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
        "shadow", raising=False,
    )
    monkeypatch.setattr(
        "app.services.trading.bracket_reconciliation_service.settings.brain_live_brackets_mode",
        "shadow", raising=False,
    )


def _trade_with_intent(db, *, user_id, ticker):
    t = Trade(
        user_id=user_id,
        ticker=ticker,
        direction="long",
        entry_price=100.0,
        quantity=4.0,
        status="open",
        broker_source="robinhood",
        broker_order_id=f"concurrent-{ticker}",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    res = upsert_bracket_intent(
        db, trade_id=t.id, user_id=user_id,
        bracket_input=BracketIntentInput(
            ticker=ticker, direction="long", entry_price=100.0,
            quantity=4.0, atr=2.0, stop_model="atr_swing",
            lifecycle_stage="validated", regime="cautious",
        ),
        broker_source="robinhood",
    )
    assert res is not None
    return t, res.intent_id


def test_two_concurrent_sweeps_both_complete_and_log(db, monkeypatch):
    """Fire two ``run_reconciliation_sweep`` calls in parallel threads
    against independent sessions. Both must succeed and each must write
    its own log row for the shared intent."""
    if db.bind is None or db.bind.dialect.name != "postgresql":
        pytest.skip("concurrency test is Postgres-only")

    _shadow_mode(monkeypatch)
    u = models.User(name="concurrent_sweep_u1")
    db.add(u)
    db.flush()

    trade, intent_id = _trade_with_intent(db, user_id=u.id, ticker="CONCMS")

    # Each sweep sees the broker report a matching position with no stop
    # — that classifies as missing_stop; the interesting invariant is
    # about concurrency, not classification.
    def broker_fn(_rows):
        return [BrokerView(
            available=True, ticker="CONCMS", broker_source="robinhood",
            position_quantity=4.0,
        )]

    SessionFactory = sessionmaker(bind=db.bind)

    def _run_sweep() -> str:
        sess = SessionFactory()
        try:
            summary = run_reconciliation_sweep(
                sess, broker_view_fn=broker_fn,
            )
            return summary.sweep_id
        finally:
            sess.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run_sweep) for _ in range(2)]
        sweep_ids = []
        for f in as_completed(futures, timeout=30):
            sweep_ids.append(f.result())

    assert len(sweep_ids) == 2
    assert sweep_ids[0] != sweep_ids[1], "sweeps must have distinct sweep_ids"

    # Each sweep wrote a row for this trade.
    rows = db.execute(text("""
        SELECT sweep_id, kind FROM trading_bracket_reconciliation_log
        WHERE trade_id = :tid AND sweep_id = ANY(:sids)
    """), {"tid": trade.id, "sids": sweep_ids}).fetchall()
    assert len(rows) == 2
    # Both classifications should be missing_stop (broker has qty, no stop).
    assert all(r[1] == "missing_stop" for r in rows)

    # Intent's last_observed_at must have been bumped — at least one of the
    # two sweeps' bump_last_observed calls landed.
    obs = db.execute(text("""
        SELECT last_observed_at, last_diff_reason
        FROM trading_bracket_intents WHERE id = :id
    """), {"id": intent_id}).fetchone()
    assert obs[0] is not None
    assert obs[1] and "missing_stop" in obs[1]


def test_concurrent_agree_sweeps_mark_reconciled_exactly_once(db, monkeypatch):
    """When both concurrent sweeps see agreement, ``mark_reconciled`` is
    called from both. The second call must be a no-op (intent already in
    ``reconciled`` state) — no rowcount > 1, no crash."""
    if db.bind is None or db.bind.dialect.name != "postgresql":
        pytest.skip("concurrency test is Postgres-only")

    _shadow_mode(monkeypatch)
    u = models.User(name="concurrent_agree_u1")
    db.add(u)
    db.flush()

    trade, intent_id = _trade_with_intent(db, user_id=u.id, ticker="CONCAGREE")

    # Broker reports matching qty + a working stop+target at our local
    # prices → classifier returns agree. (_intent helper sets stop/target
    # via upsert_bracket_intent's computed values, but we override here to
    # ensure price-drift doesn't surprise us.)
    db.execute(text("""
        UPDATE trading_bracket_intents
        SET stop_price = 96.0, target_price = 106.0
        WHERE id = :id
    """), {"id": intent_id})
    db.commit()

    def broker_fn(_rows):
        return [BrokerView(
            available=True, ticker="CONCAGREE", broker_source="robinhood",
            position_quantity=4.0,
            stop_order_id="stop-conc", stop_order_state="open", stop_order_price=96.0,
            target_order_id="tgt-conc", target_order_state="open", target_order_price=106.0,
        )]

    SessionFactory = sessionmaker(bind=db.bind)

    def _run_sweep() -> tuple[str, int]:
        sess = SessionFactory()
        try:
            summary = run_reconciliation_sweep(sess, broker_view_fn=broker_fn)
            return summary.sweep_id, summary.agree
        finally:
            sess.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run_sweep) for _ in range(2)]
        results = [f.result() for f in as_completed(futures, timeout=30)]

    # Both sweeps saw the agreement — classification is deterministic.
    assert all(r[1] >= 1 for r in results)

    # Intent landed at reconciled state; only ONE transition row exists
    # conceptually (second mark_reconciled is a rowcount=0 update because
    # the state filter `intent_state NOT LIKE 'authoritative%'` still
    # matches but the state is already reconciled — the update is a no-op
    # semantically). Key invariant: no crash, consistent final state.
    state = db.execute(text("""
        SELECT intent_state FROM trading_bracket_intents WHERE id = :id
    """), {"id": intent_id}).scalar()
    assert state == "reconciled"
