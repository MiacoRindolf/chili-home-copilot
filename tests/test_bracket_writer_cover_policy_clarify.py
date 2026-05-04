"""bracket-writer-cover-policy-clarify (2026-05-03) — regression tests
for the comment + audit-label rewrite + startup warning + admin status
endpoint.

Six scenarios:

    1. Audit reason on terminal_reject persistence uses the new label
       'covered_by_existing_sell:no_stop_coverage'.
    2. Old label 'protected_by_limit' is not regenerated anywhere.
    3. WriterAction.reason stays 'covered_by_existing_sell' (unchanged).
    4. Startup warning fires on the silent-exposure flag combo.
    5. Startup warning does NOT fire for the other three flag combos.
    6. /api/admin/bracket/cover-policy-snapshot returns the expected
       shape with flags + advisory + per-row payload.

Tests use the chili_test conftest db fixture. Run with
``-p no:asyncio`` (workaround for pre-existing pytest-asyncio plugin
collection failure).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from app.services.trading.bracket_reconciler import ReconciliationDecision
from app.services.trading.bracket_writer_g2 import (
    place_missing_stop,
    warn_if_silent_exposure,
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
) -> None:
    db.execute(text("""
        INSERT INTO trading_trades (
            id, ticker, status, broker_source, direction, quantity,
            entry_price, entry_date
        ) VALUES (
            :id, :ticker, 'open', 'robinhood', 'long', :qty,
            1.0, NOW()
        )
        ON CONFLICT (id) DO NOTHING
    """), {"id": trade_id, "ticker": ticker, "qty": qty})

    db.execute(text("""
        INSERT INTO trading_bracket_intents (
            id, trade_id, ticker, direction, quantity, entry_price,
            stop_price, intent_state, shadow_mode, broker_source,
            created_at, updated_at, payload_json
        ) VALUES (
            :id, :tid, :ticker, 'long', :qty, 1.0,
            :stop, 'intent', false, 'robinhood',
            NOW(), NOW(), '{}'::jsonb
        )
        ON CONFLICT (id) DO NOTHING
    """), {
        "id": intent_id, "tid": trade_id, "ticker": ticker, "qty": qty,
        "stop": stop_price,
    })
    db.commit()


def _stub_adapter_with_qty(qty: float):
    """Return a fake adapter whose get_products yields a single matching
    ticker with the given quantity. Mirrors how the real adapter exposes
    raw broker product rows."""
    adapter = MagicMock()
    product = MagicMock()
    product.product_id = "TEST"
    product.raw = {"ticker": "TEST", "quantity": qty}
    adapter.get_products.return_value = ([product], True)
    return adapter


def _decision_missing_stop() -> ReconciliationDecision:
    return ReconciliationDecision(
        kind="missing_stop", severity="warn", delta_payload={},
    )


def _enable_writer_flag():
    """Patch the top-level writer flag + place_missing_stop flag so the
    place_missing_stop function reaches the FIX-55 branch (otherwise
    early-return on disabled)."""
    from app.config import settings
    return [
        patch.object(settings, "chili_bracket_writer_g2_enabled", True, create=True),
        patch.object(
            settings, "chili_bracket_writer_g2_place_missing_stop", True,
            create=True,
        ),
        # Ensure DEFAULT POLICY (skip placement) is in effect for tests 1-3.
        patch.object(
            settings, "chili_bracket_writer_cancel_covering_sell", False,
            create=True,
        ),
    ]


# ── Tests ──────────────────────────────────────────────────────────────


def test_audit_reason_uses_new_no_stop_coverage_label(db):
    """Scenario 1 (rewritten 2026-05-04 for bracket-writer-respect-
    upside-targets): the covered-by-existing-sell branch no longer
    routes to ``mark_terminal_reject`` with a ``:no_stop_coverage``
    label. It now writes a structured ``pending_decision`` row and
    returns ``reason='existing_target_present_no_stop'``. This test
    asserts the new contract; ``last_diff_reason`` carries the new
    audit label, and the persisted JSON describes the conflict."""
    _seed_trade_and_intent(
        db, trade_id=7001, intent_id=77001, ticker="TEST",
        qty=10.0, stop_price=5.0,
    )

    patches = _enable_writer_flag()
    for p in patches:
        p.start()
    try:
        with (
            patch(
                "app.services.broker_service.get_position_held_for_sells",
                return_value=10.0,  # held_for_sells == broker_qty → covered
            ),
            patch(
                "app.services.broker_service.list_open_sell_orders_for_ticker",
                return_value=[],
            ),
            patch(
                "app.services.trading.market_data.fetch_quote",
                return_value={"last_price": 5.5},
            ),
        ):
            result = place_missing_stop(
                db,
                trade_id=7001,
                bracket_intent_id=77001,
                ticker="TEST",
                broker_source="robinhood",
                decision=_decision_missing_stop(),
                local_quantity=10.0,
                stop_price=5.0,
                adapter_factory=lambda src: _stub_adapter_with_qty(10.0),
            )
    finally:
        for p in patches:
            p.stop()

    # New WriterAction reason.
    assert result.ok is False
    assert result.reason == "existing_target_present_no_stop"

    # New audit label + intent_state stays at whatever it was (no
    # mark_terminal_reject call).
    row = db.execute(text(
        "SELECT intent_state, last_diff_reason, payload_json "
        "FROM trading_bracket_intents WHERE id=77001"
    )).first()
    # intent_state remains 'intent' (no terminal_reject transition).
    assert row[0] == "intent"
    assert row[1] == "existing_target_present_no_stop"
    # pending_decision row written.
    payload = row[2] or {}
    assert payload.get("pending_decision") is not None


def test_old_protected_by_limit_label_not_regenerated(db):
    """Scenario 2: same seed as #1. Assert the old misleading label
    'protected_by_limit' is nowhere in the persisted row's reason."""
    _seed_trade_and_intent(
        db, trade_id=7002, intent_id=77002, ticker="TEST",
        qty=10.0, stop_price=5.0,
    )

    patches = _enable_writer_flag()
    for p in patches:
        p.start()
    try:
        with patch(
            "app.services.broker_service.get_position_held_for_sells",
            return_value=10.0,
        ):
            place_missing_stop(
                db,
                trade_id=7002,
                bracket_intent_id=77002,
                ticker="TEST",
                broker_source="robinhood",
                decision=_decision_missing_stop(),
                local_quantity=10.0,
                stop_price=5.0,
                adapter_factory=lambda src: _stub_adapter_with_qty(10.0),
            )
    finally:
        for p in patches:
            p.stop()

    last_diff = db.execute(text(
        "SELECT last_diff_reason FROM trading_bracket_intents WHERE id=77002"
    )).scalar()
    assert last_diff is not None
    assert "protected_by_limit" not in last_diff


def test_writer_action_reason_unchanged(db):
    """Scenario 3 (rewritten 2026-05-04): covered-by-sell condition
    now returns ``reason='existing_target_present_no_stop'`` (the
    pending-decision contract). The prior ``covered_by_existing_sell``
    reason was retired alongside the auto-cancel branch."""
    _seed_trade_and_intent(
        db, trade_id=7003, intent_id=77003, ticker="TEST",
        qty=20.0, stop_price=4.0,
    )

    patches = _enable_writer_flag()
    for p in patches:
        p.start()
    try:
        with (
            patch(
                "app.services.broker_service.get_position_held_for_sells",
                return_value=20.0,
            ),
            patch(
                "app.services.broker_service.list_open_sell_orders_for_ticker",
                return_value=[],
            ),
            patch(
                "app.services.trading.market_data.fetch_quote",
                return_value={"last_price": 5.0},
            ),
        ):
            result = place_missing_stop(
                db,
                trade_id=7003,
                bracket_intent_id=77003,
                ticker="TEST",
                broker_source="robinhood",
                decision=_decision_missing_stop(),
                local_quantity=20.0,
                stop_price=4.0,
                adapter_factory=lambda src: _stub_adapter_with_qty(20.0),
            )
    finally:
        for p in patches:
            p.stop()

    assert result.action == "place_missing_stop"
    assert result.ok is False
    assert result.reason == "existing_target_present_no_stop"


def test_startup_warning_fires_on_silent_exposure_combo(caplog):
    """Scenario 4: emergency-repair ON, cancel-covering-sell OFF →
    WARNING-level log line emitted naming both flags."""
    from app.config import settings
    with (
        patch.object(
            settings, "chili_bracket_missing_stop_repair_enabled", True,
            create=True,
        ),
        patch.object(
            settings, "chili_bracket_writer_cancel_covering_sell", False,
            create=True,
        ),
        caplog.at_level(logging.WARNING, logger="app.services.trading.bracket_writer_g2"),
    ):
        emitted = warn_if_silent_exposure()

    assert emitted is True
    matching = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "SILENT-EXPOSURE COMBO ACTIVE" in r.getMessage()
    ]
    assert matching, "expected one WARNING line; got none"
    msg = matching[0].getMessage()
    assert "CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED=1" in msg
    assert "CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=0" in msg


@pytest.mark.parametrize("repair, cancel", [
    (True, True),    # both ON: covered, no silent exposure
    (False, False),  # both OFF: emergency-repair off, no exposure
    (False, True),   # only cancel ON: nothing to cancel
])
def test_startup_warning_silent_for_non_exposure_combos(repair, cancel, caplog):
    """Scenario 5: warning does NOT fire for the other three combos."""
    from app.config import settings
    with (
        patch.object(
            settings, "chili_bracket_missing_stop_repair_enabled", repair,
            create=True,
        ),
        patch.object(
            settings, "chili_bracket_writer_cancel_covering_sell", cancel,
            create=True,
        ),
        caplog.at_level(logging.WARNING, logger="app.services.trading.bracket_writer_g2"),
    ):
        emitted = warn_if_silent_exposure()

    assert emitted is False
    matching = [
        r for r in caplog.records
        if "SILENT-EXPOSURE COMBO ACTIVE" in r.getMessage()
    ]
    assert not matching, (
        f"warning should NOT fire for repair={repair}, cancel={cancel}"
    )


def test_admin_cover_policy_snapshot_endpoint_shape(db):
    """Scenario 6: seed two intent rows whose last_diff_reason starts with
    'covered_by_existing_sell'. Call the admin handler directly with a
    paired-context shim and assert the JSON shape includes flag snapshot,
    row count, and per-row advisory."""
    # Seed two trades + intents.
    _seed_trade_and_intent(
        db, trade_id=7004, intent_id=77004, ticker="ALPHA",
        qty=10.0, stop_price=5.0,
    )
    _seed_trade_and_intent(
        db, trade_id=7005, intent_id=77005, ticker="BETA",
        qty=20.0, stop_price=4.0,
    )
    # Set their last_diff_reason to the new label so the endpoint picks them up.
    db.execute(text("""
        UPDATE trading_bracket_intents
        SET last_diff_reason = 'covered_by_existing_sell:no_stop_coverage',
            intent_state = 'terminal_reject'
        WHERE id IN (77004, 77005)
    """))
    db.commit()

    from app.routers.admin import api_admin_bracket_cover_policy_snapshot

    # Build the same ctx shape that require_paired produces. _guard checks
    # ctx is not None; the handler reads ctx["db"]. Anything else passes.
    ctx = {"db": db}
    response = api_admin_bracket_cover_policy_snapshot(ctx=ctx)

    # JSONResponse exposes its body via .body (bytes); decode + parse.
    import json as _json
    payload = _json.loads(response.body.decode("utf-8"))

    assert "as_of" in payload
    assert "flags" in payload
    assert set(payload["flags"].keys()) >= {
        "chili_bracket_missing_stop_repair_enabled",
        "chili_bracket_writer_cancel_covering_sell",
    }
    assert payload["row_count"] >= 2  # may include other test seeds

    seeded = [r for r in payload["rows"] if r["intent_id"] in (77004, 77005)]
    assert len(seeded) == 2
    for r in seeded:
        assert r["intent_state"] == "terminal_reject"
        assert r["last_diff_reason"].startswith("covered_by_existing_sell")
        assert "advisory" in r
        assert "no downside protection" in r["advisory"]
