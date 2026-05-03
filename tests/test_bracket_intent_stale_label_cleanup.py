"""bracket-intent-stale-label-cleanup (2026-05-03) — regression tests for
the sweep-loop hook in
``app.services.trading.bracket_reconciliation_service._apply_intent_mirror_writes``
that mirrors broker stop-order id into the local advisory cache and
auto-transitions stale ``terminal_reject`` rows to ``reconciled`` when
the classifier subsequently reports ``kind=agree``.

Nine scenarios:

    1. Mirror write on kind=agree when local NULL, broker has order.
    2. Mirror write when local stale, broker has different order.
    3. Mirror clear when local has order, broker NULL.
    4. No mirror write when broker.available is False.
    5. No-op when both sides agree (local == broker).
    6. Auto-transition idempotency (already reconciled, no further writes).
    7. Flag OFF preserves prior behavior.
    8. Authority contract canary — static check that no decision-time
       code reads ``bracket_intents.broker_stop_order_id``.
    9. No auto-transition for kind=agree rows that were never terminal_reject.

Tests use the ``db`` fixture from conftest (``chili_test`` enforced).
Run with ``-p no:asyncio`` to work around the pre-existing
pytest-asyncio plugin AttributeError on collection.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.services.trading.bracket_reconciler import (
    BrokerView,
    LocalView,
    ReconciliationDecision,
)


# ── Test seed helpers ──────────────────────────────────────────────────


def _seed_trade_and_intent(
    db,
    *,
    trade_id: int,
    intent_id: int,
    ticker: str,
    qty: float = 10.0,
    stop_price: float = 5.0,
    intent_state: str = "terminal_reject",
    trade_status: str = "open",
    broker_stop_order_id: str | None = None,
) -> None:
    """Insert one Trade + one BracketIntent row matching the schema in
    use by the chili_test database. See
    ``test_bracket_emergency_terminal_reject_repair._seed_trade_and_intent``
    for schema notes (direction not side, exit_reason not closed_reason,
    etc.).
    """
    db.execute(text("""
        INSERT INTO trading_trades (
            id, ticker, status, broker_source, direction, quantity,
            entry_price, entry_date
        ) VALUES (
            :id, :ticker, :status, 'robinhood', 'long', :qty,
            1.0, NOW()
        )
        ON CONFLICT (id) DO NOTHING
    """), {"id": trade_id, "ticker": ticker, "status": trade_status, "qty": qty})

    db.execute(text("""
        INSERT INTO trading_bracket_intents (
            id, trade_id, ticker, direction, quantity, entry_price,
            stop_price, intent_state, shadow_mode, broker_source,
            broker_stop_order_id,
            created_at, updated_at, payload_json
        ) VALUES (
            :id, :tid, :ticker, 'long', :qty, 1.0,
            :stop, :state, false, 'robinhood',
            :bsoi,
            NOW(), NOW(), '{}'::jsonb
        )
        ON CONFLICT (id) DO NOTHING
    """), {
        "id": intent_id, "tid": trade_id, "ticker": ticker, "qty": qty,
        "stop": stop_price, "state": intent_state,
        "bsoi": broker_stop_order_id,
    })
    db.commit()


def _local(
    *,
    trade_id: int,
    intent_id: int,
    ticker: str,
    intent_state: str = "terminal_reject",
    trade_status: str = "open",
) -> LocalView:
    return LocalView(
        trade_id=trade_id,
        bracket_intent_id=intent_id,
        ticker=ticker,
        direction="long",
        quantity=10.0,
        intent_state=intent_state,
        stop_price=5.0,
        target_price=None,
        broker_source="robinhood",
        trade_status=trade_status,
    )


def _broker(
    *,
    available: bool = True,
    ticker: str = "TEST",
    stop_order_id: str | None = None,
    stop_order_state: str | None = None,
) -> BrokerView:
    return BrokerView(
        available=available,
        ticker=ticker,
        broker_source="robinhood",
        stop_order_id=stop_order_id,
        stop_order_state=stop_order_state,
        position_quantity=10.0,
    )


def _decision(kind: str = "agree", severity: str = "info") -> ReconciliationDecision:
    return ReconciliationDecision(kind=kind, severity=severity, delta_payload={})


def _settings_with_flag(value: bool):
    from app.config import settings
    return patch.object(
        settings, "chili_bracket_intent_mirror_enabled", value, create=True,
    )


def _apply(db, *, local, broker, decision, sweep_id: str = "test-sweep"):
    from app.services.trading.bracket_reconciliation_service import (
        _apply_intent_mirror_writes,
    )
    return _apply_intent_mirror_writes(
        db,
        sweep_id=sweep_id,
        mode="authoritative",
        local=local,
        broker=broker,
        decision=decision,
    )


# ── Scenarios ─────────────────────────────────────────────────────────


def test_mirror_write_on_agree_with_null_local_and_broker_value(db):
    """Scenario 1: terminal_reject intent, local broker_stop_order_id NULL,
    broker reports a working stop. Expect: local mirror populated AND
    auto-transition to reconciled fires (because kind=agree).
    """
    _seed_trade_and_intent(
        db, trade_id=8001, intent_id=88001, ticker="MIR1",
        broker_stop_order_id=None, intent_state="terminal_reject",
    )
    local = _local(trade_id=8001, intent_id=88001, ticker="MIR1",
                   intent_state="terminal_reject")
    broker = _broker(stop_order_id="abc-new", stop_order_state="working",
                     ticker="MIR1")

    with _settings_with_flag(True):
        _apply(db, local=local, broker=broker, decision=_decision("agree"))
        db.commit()

    row = db.execute(text(
        "SELECT broker_stop_order_id, intent_state, last_diff_reason "
        "FROM trading_bracket_intents WHERE id=88001"
    )).first()
    assert row[0] == "abc-new"
    assert row[1] == "reconciled"
    assert row[2] == "auto_reconciled_after_terminal_reject"


def test_mirror_write_when_local_stale_broker_different(db):
    """Scenario 2: local has 'old', broker reports 'new'. Mirror updates
    to 'new'. Auto-transition still fires (state was terminal_reject)."""
    _seed_trade_and_intent(
        db, trade_id=8002, intent_id=88002, ticker="MIR2",
        broker_stop_order_id="old", intent_state="terminal_reject",
    )
    local = _local(trade_id=8002, intent_id=88002, ticker="MIR2",
                   intent_state="terminal_reject")
    broker = _broker(stop_order_id="new", stop_order_state="working",
                     ticker="MIR2")

    with _settings_with_flag(True):
        _apply(db, local=local, broker=broker, decision=_decision("agree"))
        db.commit()

    row = db.execute(text(
        "SELECT broker_stop_order_id, intent_state "
        "FROM trading_bracket_intents WHERE id=88002"
    )).first()
    assert row[0] == "new"
    assert row[1] == "reconciled"


def test_mirror_clear_on_missing_stop_with_broker_null(db):
    """Scenario 3: local has 'dead' order id, broker reports NULL,
    decision=missing_stop (not agree). Mirror clears to NULL. State
    stays terminal_reject (auto-transition only fires on kind=agree)."""
    _seed_trade_and_intent(
        db, trade_id=8003, intent_id=88003, ticker="MIR3",
        broker_stop_order_id="dead", intent_state="terminal_reject",
    )
    local = _local(trade_id=8003, intent_id=88003, ticker="MIR3",
                   intent_state="terminal_reject")
    broker = _broker(stop_order_id=None, stop_order_state=None, ticker="MIR3")

    with _settings_with_flag(True):
        _apply(db, local=local, broker=broker,
               decision=_decision("missing_stop", "warn"))
        db.commit()

    row = db.execute(text(
        "SELECT broker_stop_order_id, intent_state "
        "FROM trading_bracket_intents WHERE id=88003"
    )).first()
    assert row[0] is None
    assert row[1] == "terminal_reject"


def test_no_mirror_write_when_broker_unavailable(db):
    """Scenario 4: broker.available=False. No mirror write, no transition.
    The local row stays exactly as seeded."""
    _seed_trade_and_intent(
        db, trade_id=8004, intent_id=88004, ticker="MIR4",
        broker_stop_order_id="keep-me", intent_state="terminal_reject",
    )
    local = _local(trade_id=8004, intent_id=88004, ticker="MIR4",
                   intent_state="terminal_reject")
    broker = _broker(available=False, ticker="MIR4")

    with _settings_with_flag(True):
        _apply(db, local=local, broker=broker, decision=_decision("agree"))
        db.commit()

    row = db.execute(text(
        "SELECT broker_stop_order_id, intent_state "
        "FROM trading_bracket_intents WHERE id=88004"
    )).first()
    assert row[0] == "keep-me"
    assert row[1] == "terminal_reject"


def test_noop_when_both_sides_agree(db):
    """Scenario 5: local 'abc' == broker 'abc', state already reconciled.
    No UPDATE issued (verify by reading updated_at before/after).
    The mirror writer no-ops; the auto-transition no-ops."""
    _seed_trade_and_intent(
        db, trade_id=8005, intent_id=88005, ticker="MIR5",
        broker_stop_order_id="abc", intent_state="reconciled",
    )
    before = db.execute(text(
        "SELECT updated_at FROM trading_bracket_intents WHERE id=88005"
    )).scalar()

    local = _local(trade_id=8005, intent_id=88005, ticker="MIR5",
                   intent_state="reconciled")
    broker = _broker(stop_order_id="abc", stop_order_state="working",
                     ticker="MIR5")

    with _settings_with_flag(True):
        _apply(db, local=local, broker=broker, decision=_decision("agree"))
        db.commit()

    after = db.execute(text(
        "SELECT updated_at FROM trading_bracket_intents WHERE id=88005"
    )).scalar()
    assert after == before, "no-op should not bump updated_at"


def test_auto_transition_idempotent_on_already_reconciled(db):
    """Scenario 6: state already reconciled, kind=agree. The
    mark_auto_reconciled_after_terminal_reject writer's WHERE clause
    requires intent_state='terminal_reject', so no UPDATE runs.
    Mirror-write may still fire if there's a value diff (separate concern)."""
    _seed_trade_and_intent(
        db, trade_id=8006, intent_id=88006, ticker="MIR6",
        broker_stop_order_id="abc", intent_state="reconciled",
    )
    before = db.execute(text(
        "SELECT updated_at, last_diff_reason "
        "FROM trading_bracket_intents WHERE id=88006"
    )).first()

    local = _local(trade_id=8006, intent_id=88006, ticker="MIR6",
                   intent_state="reconciled")
    broker = _broker(stop_order_id="abc", stop_order_state="working",
                     ticker="MIR6")

    with _settings_with_flag(True):
        _apply(db, local=local, broker=broker, decision=_decision("agree"))
        db.commit()

    after = db.execute(text(
        "SELECT updated_at, last_diff_reason "
        "FROM trading_bracket_intents WHERE id=88006"
    )).first()
    # No state move means no last_diff_reason update (it stays whatever
    # it was, or NULL); critically NOT 'auto_reconciled_after_terminal_reject'.
    assert after[1] != "auto_reconciled_after_terminal_reject"


def test_flag_off_preserves_prior_behavior(db):
    """Scenario 7: flag OFF. Even with kind=agree + terminal_reject +
    broker reporting a stop, neither mirror nor transition fires.
    Local row stays exactly as seeded."""
    _seed_trade_and_intent(
        db, trade_id=8007, intent_id=88007, ticker="MIR7",
        broker_stop_order_id=None, intent_state="terminal_reject",
    )
    local = _local(trade_id=8007, intent_id=88007, ticker="MIR7",
                   intent_state="terminal_reject")
    broker = _broker(stop_order_id="would-mirror", stop_order_state="working",
                     ticker="MIR7")

    with _settings_with_flag(False):
        _apply(db, local=local, broker=broker, decision=_decision("agree"))
        db.commit()

    row = db.execute(text(
        "SELECT broker_stop_order_id, intent_state "
        "FROM trading_bracket_intents WHERE id=88007"
    )).first()
    assert row[0] is None
    assert row[1] == "terminal_reject"


def test_authority_contract_canary_no_decision_time_reads():
    """Scenario 8: static check that decision-time code paths do not
    read ``bracket_intents.broker_stop_order_id``. The mirror is
    advisory cache; promoting it to authority would silently regress
    the broker-truth-first contract that the reconciler depends on.

    Allowed reads (informational):
      - bracket_reconciler.py:154 — debug delta_payload only.
      - bracket_intent_writer.sync_broker_stop_order_id_mirror — the
        writer itself reads to detect change.

    Disallowed: any new read in
        bracket_reconciliation_service.py
        bracket_writer_g2.py
    """
    repo_root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r"\bbroker_stop_order_id\b")

    for relpath in (
        "app/services/trading/bracket_reconciliation_service.py",
        "app/services/trading/bracket_writer_g2.py",
    ):
        body = (repo_root / relpath).read_text(encoding="utf-8")
        # Strip comments/docstrings? Cheap heuristic: just find SELECT-ish reads.
        # We forbid any line that LOOKS like a SQL or attribute READ from
        # the column. WRITES (UPDATE ... SET broker_stop_order_id = ...)
        # are not in scope of this canary -- they're allowed.
        offenders = []
        for line in body.splitlines():
            if not pattern.search(line):
                continue
            stripped = line.strip()
            # Allow: comments, string literals describing the column,
            # the writer module's own UPDATE/SELECT (lives in
            # bracket_intent_writer.py, not in scope here).
            if stripped.startswith("#"):
                continue
            # Allow appearance in delta_payload dict construction
            # (bracket_reconciler.py:154 already exists; not in this list).
            # Disallow if the line LOOKS like a column read in a query
            # or an attribute access.
            if "SELECT" in line.upper() and "broker_stop_order_id" in line:
                offenders.append(line)
            elif re.search(r"\.broker_stop_order_id\b", line):
                offenders.append(line)
        assert not offenders, (
            f"Decision-time code in {relpath} reads broker_stop_order_id, "
            f"violating the advisory-cache contract:\n  "
            + "\n  ".join(offenders)
        )


def test_no_auto_transition_for_non_terminal_reject_state(db):
    """Scenario 9: state='intent' (or any non-terminal_reject), kind=agree.
    Auto-transition guard requires intent_state='terminal_reject', so it
    does not fire. Mirror still writes if applicable (covered by
    scenarios 1+2)."""
    _seed_trade_and_intent(
        db, trade_id=8009, intent_id=88009, ticker="MIR9",
        broker_stop_order_id=None, intent_state="intent",
    )
    local = _local(trade_id=8009, intent_id=88009, ticker="MIR9",
                   intent_state="intent")
    broker = _broker(stop_order_id="abc", stop_order_state="working",
                     ticker="MIR9")

    with _settings_with_flag(True):
        _apply(db, local=local, broker=broker, decision=_decision("agree"))
        db.commit()

    row = db.execute(text(
        "SELECT broker_stop_order_id, intent_state, last_diff_reason "
        "FROM trading_bracket_intents WHERE id=88009"
    )).first()
    # Mirror DID write (broker had a value, local was NULL)
    assert row[0] == "abc"
    # Auto-transition did NOT fire (state was 'intent', not 'terminal_reject')
    assert row[1] == "intent"
    assert row[2] != "auto_reconciled_after_terminal_reject"
