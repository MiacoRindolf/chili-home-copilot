"""bracket-intent-stop-price-live-sync (2026-05-03) — regression tests
for the new ``sync_bracket_intent_stop_from_trade`` writer in
``app.services.trading.bracket_intent_writer`` and its sweep-loop hook
``_sync_bracket_intent_stop_unconditional`` in
``app.services.trading.stop_engine``.

Eight scenarios + 2 prior-suite regression checks (#9 and #10 are
exercised via separate pytest invocations by the operator/CI):

    1. Sync fires on every sweep when trade.stop_loss != bi.stop_price.
    2. No-op when values match (no UPDATE issued, updated_at stays).
    3. terminal_reject does NOT block sync.
    4. CLOSED state DOES block sync.
    5. authoritative_* prefix DOES block sync.
    6. brain_live_brackets_mode='off' blocks sync at the call site.
    7. Authority contract canary — static grep for new
       bracket_intents.stop_price reads in decision-making code.
    8. Sync continues to fire across multiple sweeps as
       trade.stop_loss moves.

Tests use the chili_test conftest db fixture. Run with
``-p no:asyncio``.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from app.services.trading.bracket_intent_writer import (
    sync_bracket_intent_stop_from_trade,
)


# ── Test seed helpers ──────────────────────────────────────────────────


def _seed_trade_and_intent(
    db,
    *,
    trade_id: int,
    intent_id: int,
    ticker: str,
    qty: float = 10.0,
    trade_stop_loss: float = 5.0,
    bi_stop_price: float = 5.0,
    intent_state: str = "intent",
    trade_status: str = "open",
) -> None:
    db.execute(text("""
        INSERT INTO trading_trades (
            id, ticker, status, broker_source, direction, quantity,
            entry_price, stop_loss, entry_date
        ) VALUES (
            :id, :ticker, :status, 'robinhood', 'long', :qty,
            1.0, :sl, NOW()
        )
        ON CONFLICT (id) DO NOTHING
    """), {"id": trade_id, "ticker": ticker, "status": trade_status,
           "qty": qty, "sl": trade_stop_loss})

    db.execute(text("""
        INSERT INTO trading_bracket_intents (
            id, trade_id, ticker, direction, quantity, entry_price,
            stop_price, intent_state, shadow_mode, broker_source,
            created_at, updated_at, payload_json
        ) VALUES (
            :id, :tid, :ticker, 'long', :qty, 1.0,
            :stop, :state, false, 'robinhood',
            NOW(), NOW(), '{}'::jsonb
        )
        ON CONFLICT (id) DO NOTHING
    """), {
        "id": intent_id, "tid": trade_id, "ticker": ticker, "qty": qty,
        "stop": bi_stop_price, "state": intent_state,
    })
    db.commit()


# ── Tests ──────────────────────────────────────────────────────────────


def test_sync_fires_when_drift_exists(db):
    """Scenario 1: trade.stop_loss=2.0, bi.stop_price=1.5. Sync makes
    bi.stop_price=2.0 and reports changed=True with prev=1.5."""
    _seed_trade_and_intent(
        db, trade_id=6001, intent_id=66001, ticker="DRFT",
        trade_stop_loss=2.0, bi_stop_price=1.5,
        intent_state="intent",
    )

    changed, prev = sync_bracket_intent_stop_from_trade(
        db, 6001, trade_stop_loss=2.0,
    )
    db.commit()

    assert changed is True
    assert prev == 1.5
    new = db.execute(text(
        "SELECT stop_price FROM trading_bracket_intents WHERE id=66001"
    )).scalar()
    assert float(new) == 2.0


def test_sync_noop_when_values_match(db):
    """Scenario 2: both at 2.0. No UPDATE issued; updated_at unchanged."""
    _seed_trade_and_intent(
        db, trade_id=6002, intent_id=66002, ticker="MTCH",
        trade_stop_loss=2.0, bi_stop_price=2.0,
        intent_state="reconciled",
    )
    before = db.execute(text(
        "SELECT updated_at FROM trading_bracket_intents WHERE id=66002"
    )).scalar()

    changed, prev = sync_bracket_intent_stop_from_trade(
        db, 6002, trade_stop_loss=2.0,
    )
    db.commit()

    assert changed is False
    assert prev == 2.0
    after = db.execute(text(
        "SELECT updated_at FROM trading_bracket_intents WHERE id=66002"
    )).scalar()
    assert after == before, "no-op should not bump updated_at"


def test_sync_works_for_terminal_reject(db):
    """Scenario 3: intent_state='terminal_reject', drift exists. Sync
    updates stop_price; intent_state stays terminal_reject."""
    _seed_trade_and_intent(
        db, trade_id=6003, intent_id=66003, ticker="TRJC",
        trade_stop_loss=3.5, bi_stop_price=2.0,
        intent_state="terminal_reject",
    )

    changed, prev = sync_bracket_intent_stop_from_trade(
        db, 6003, trade_stop_loss=3.5,
    )
    db.commit()

    assert changed is True
    assert prev == 2.0
    row = db.execute(text(
        "SELECT stop_price, intent_state FROM trading_bracket_intents WHERE id=66003"
    )).first()
    assert float(row[0]) == 3.5
    assert row[1] == "terminal_reject", (
        "intent_state must be preserved; auto-transition is a separate concern"
    )


def test_sync_blocked_for_closed_state(db):
    """Scenario 4: CLOSED state + drift. Sync does NOT update."""
    _seed_trade_and_intent(
        db, trade_id=6004, intent_id=66004, ticker="CLSD",
        trade_stop_loss=4.0, bi_stop_price=2.0,
        intent_state="closed",
    )

    changed, prev = sync_bracket_intent_stop_from_trade(
        db, 6004, trade_stop_loss=4.0,
    )
    db.commit()

    assert changed is False
    assert prev == 2.0
    new = db.execute(text(
        "SELECT stop_price FROM trading_bracket_intents WHERE id=66004"
    )).scalar()
    assert float(new) == 2.0, "CLOSED row must not be touched"


def test_sync_blocked_for_authoritative_prefix(db):
    """Scenario 5: intent_state='authoritative_submitted' (Phase G.2
    legacy). Sync does NOT update — frozen-authority contract."""
    _seed_trade_and_intent(
        db, trade_id=6005, intent_id=66005, ticker="AUTH",
        trade_stop_loss=5.0, bi_stop_price=2.0,
        intent_state="authoritative_submitted",
    )

    changed, prev = sync_bracket_intent_stop_from_trade(
        db, 6005, trade_stop_loss=5.0,
    )
    db.commit()

    assert changed is False
    assert prev == 2.0
    new = db.execute(text(
        "SELECT stop_price FROM trading_bracket_intents WHERE id=66005"
    )).scalar()
    assert float(new) == 2.0, "authoritative_* row must not be touched"


def test_call_site_skips_when_brain_live_brackets_mode_off(db):
    """Scenario 6: mode='off' at the stop_engine call-site wrapper.
    The wrapper short-circuits before invoking the writer."""
    _seed_trade_and_intent(
        db, trade_id=6006, intent_id=66006, ticker="OFFM",
        trade_stop_loss=6.0, bi_stop_price=2.0,
        intent_state="intent",
    )

    from app.services.trading.stop_engine import (
        _sync_bracket_intent_stop_unconditional,
    )

    fake_trade = MagicMock()
    fake_trade.id = 6006
    fake_trade.ticker = "OFFM"
    fake_trade.broker_source = "robinhood"
    fake_trade.stop_loss = 6.0

    from app.config import settings
    with patch.object(settings, "brain_live_brackets_mode", "off", create=True):
        _sync_bracket_intent_stop_unconditional(db, fake_trade)
    db.commit()

    new = db.execute(text(
        "SELECT stop_price FROM trading_bracket_intents WHERE id=66006"
    )).scalar()
    assert float(new) == 2.0, "mode=off must skip the sync"


def test_authority_contract_canary_no_decision_time_reads():
    """Scenario 7: static check that decision-time code does not read
    ``bracket_intents.stop_price`` for decisions. The mirror is
    advisory cache. Acceptable readers:
      * bracket_intent_writer (the writer module — reads to detect change).
      * bracket_writer_g2.place_missing_stop — reads at PLACEMENT time
        because the broker call needs a stop price; this is the
        intended consumer of the cache. The check below scopes to
        forbidden modules only.

    Forbidden in:
      * bracket_reconciliation_service.py
      * bracket_reconciler.py (classifier)

    The classifier already references ``broker.stop_order_id`` etc; it
    must continue to read from the broker truth, not from this cache.
    """
    repo_root = Path(__file__).resolve().parent.parent
    column_pattern = re.compile(r"bi\.stop_price|bracket_intents\.stop_price")

    # Whitelist: each entry is a (relpath, expected line count) baseline.
    # Existing reads are tolerated (frozen at the count below); a NEW
    # read in any of these files trips the canary by raising the count.
    # Add new entries when extending the watch surface; do not adjust
    # baselines without recording the rationale in the CC report.
    baseline_counts = {
        "app/services/trading/bracket_reconciliation_service.py": 1,
        "app/services/trading/bracket_reconciler.py": 0,
    }

    for relpath, expected in baseline_counts.items():
        body = (repo_root / relpath).read_text(encoding="utf-8")
        hits = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if column_pattern.search(line):
                hits.append(line)
        assert len(hits) == expected, (
            f"Decision-time read count for bracket_intents.stop_price in "
            f"{relpath} is {len(hits)}; expected baseline {expected}. "
            f"If this is intentional, update the baseline in this test "
            f"and document why in the CC report. Hits:\n  "
            + "\n  ".join(hits)
        )


def test_sync_catches_up_across_multiple_sweeps(db):
    """Scenario 8: drift exists, sync once → matches. Mutate
    trade.stop_loss to a new value. Sync again → catches up."""
    _seed_trade_and_intent(
        db, trade_id=6008, intent_id=66008, ticker="MULT",
        trade_stop_loss=2.0, bi_stop_price=1.0,
        intent_state="reconciled",
    )

    # First sweep: drift 1.0 -> 2.0
    changed, prev = sync_bracket_intent_stop_from_trade(
        db, 6008, trade_stop_loss=2.0,
    )
    db.commit()
    assert changed is True
    assert prev == 1.0
    cur = db.execute(text(
        "SELECT stop_price FROM trading_bracket_intents WHERE id=66008"
    )).scalar()
    assert float(cur) == 2.0

    # Engine moves trade.stop_loss to 3.0 (simulated; not actually mutating
    # the trade row -- the writer reads its arg directly).
    # Second sweep: drift 2.0 -> 3.0
    changed, prev = sync_bracket_intent_stop_from_trade(
        db, 6008, trade_stop_loss=3.0,
    )
    db.commit()
    assert changed is True
    assert prev == 2.0
    cur = db.execute(text(
        "SELECT stop_price FROM trading_bracket_intents WHERE id=66008"
    )).scalar()
    assert float(cur) == 3.0

    # Third sweep: same value -> no-op
    changed, prev = sync_bracket_intent_stop_from_trade(
        db, 6008, trade_stop_loss=3.0,
    )
    db.commit()
    assert changed is False
    assert prev == 3.0
