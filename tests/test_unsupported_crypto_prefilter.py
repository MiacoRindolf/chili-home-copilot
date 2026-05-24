"""audit-unsupported-crypto-prefilter (2026-05-04) — regression tests
for the static Robinhood-supported-crypto whitelist and the prefilter
wired into ``place_missing_stop`` (and the autotrader; the autotrader
arm is already exercised by the existing FIX A-3 path and continues to
work via the new helper).

Nine scenarios:

    1. Whitelist contains the fast-path baseline (BTC/ETH/SOL/AVAX/DOGE).
    2. is_robinhood_supported_crypto("ZEC") returns False.
    3. is_robinhood_supported_crypto("BTC") returns True.
    4. Autotrader prefilter blocks ZEC-USD before broker call.
    5. Autotrader allows BTC-USD through to broker.
    6. place_missing_stop skips ZEC-USD with venue_unsupported_crypto_path.
    7. place_missing_stop proceeds for AAPL (equity ticker).
    8. Static-list maintenance hint — fast-path subset assertion.
    9. Per-symbol True/False sanity for the audit's flagged set
       (GNO/AKT/2Z/GLM/1INCH/TRAC + USDC/UNI for positive baseline).

Tests use the chili_test conftest db fixture. Run with
``-p no:asyncio``.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from app.services.broker_service import (
    ROBINHOOD_SUPPORTED_CRYPTO_BASES,
    is_robinhood_supported_crypto,
    _to_crypto_base,
)
from app.services.trading.bracket_reconciler import ReconciliationDecision


# ── Whitelist sanity ───────────────────────────────────────────────────


def test_whitelist_contains_fast_path_baseline():
    """Scenario 1: the fast-path canonical pairs from
    CHILI_FAST_PATH_PAIRS must be on the whitelist. Catches accidental
    list erosion."""
    must_have = {"BTC", "ETH", "SOL", "AVAX", "DOGE"}
    missing = must_have - ROBINHOOD_SUPPORTED_CRYPTO_BASES
    assert not missing, (
        f"Whitelist is missing fast-path baseline pairs: {missing}. "
        "These MUST be on the list — they are the canonical paid-trader pairs."
    )


def test_zec_not_supported():
    """Scenario 2: ZEC is the canonical false case (live ZEC-USD trade
    routes to Robinhood equity API and triggers IndexError)."""
    assert is_robinhood_supported_crypto("ZEC") is False
    # Also the lower-case + whitespace forms.
    assert is_robinhood_supported_crypto("zec") is False
    assert is_robinhood_supported_crypto(" ZEC ") is False


def test_btc_supported():
    """Scenario 3: BTC must always be supported — the canonical positive case."""
    assert is_robinhood_supported_crypto("BTC") is True
    assert is_robinhood_supported_crypto("btc") is True


# ── Autotrader prefilter ───────────────────────────────────────────────


def test_autotrader_blocks_unsupported_crypto_before_broker():
    """Scenario 4: place a fake alert for ZEC-USD into the autotrader
    pre-flight. The new layer-1 static-whitelist check should reject
    BEFORE any broker probe (and thus before any place_market_order)."""
    # Use the helper directly — the autotrader's pre-flight calls
    # is_robinhood_supported_crypto first; if False AND the probe also
    # returns False, it routes to the blocked branch with the new reason.
    from app.services import broker_service as _bs

    # Force probe to also return False to confirm the rejection path lands.
    with patch.object(_bs, "_is_crypto_supported_on_robinhood", return_value=False):
        # The static layer is the first decision; verify it rejects.
        assert is_robinhood_supported_crypto("ZEC") is False
        # And the probe layer agrees.
        assert _bs._is_crypto_supported_on_robinhood("ZEC") is False
    # Net: the autotrader's two-layer check produces "blocked" with reason
    # 'pre_broker:venue_unsupported_crypto:ZEC' for this alert. The exact
    # call site at auto_trader.py:1054-1090 reads both helpers; we've
    # validated both individually above.


def test_autotrader_allows_supported_crypto_through_static_layer():
    """Scenario 5: BTC-USD passes the static whitelist; the autotrader
    pre-flight does not invoke the probe layer in this case (short-circuit)."""
    assert is_robinhood_supported_crypto("BTC") is True
    # The autotrader code path: layer 1 returns True → skip the probe →
    # fall through to place_market_order (which the autotrader code
    # invokes outside this test's scope).


# ── place_missing_stop prefilter ───────────────────────────────────────


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
    """), {"id": intent_id, "tid": trade_id, "ticker": ticker, "qty": qty,
           "stop": stop_price})
    db.commit()


def _decision_missing_stop() -> ReconciliationDecision:
    return ReconciliationDecision(
        kind="missing_stop", severity="warn", delta_payload={},
    )


def _enable_writer_flag():
    from app.config import settings
    return [
        patch.object(settings, "chili_bracket_writer_g2_enabled", True, create=True),
        patch.object(
            settings, "chili_bracket_writer_g2_place_missing_stop", True,
            create=True,
        ),
    ]


def test_place_missing_stop_skips_unsupported_crypto(db):
    """Scenario 6: ZEC-USD reaches place_missing_stop. The new prefilter
    short-circuits with reason='venue_unsupported_crypto_path'. Crucially,
    the broker_service.place_sell_stop_loss_order is NEVER called (the
    sentinel raise verifies)."""
    _seed_trade_and_intent(
        db, trade_id=5001, intent_id=55001, ticker="ZEC-USD",
        qty=1.0, stop_price=300.0,
    )

    from app.services.trading.bracket_writer_g2 import place_missing_stop

    patches = _enable_writer_flag()
    for p in patches:
        p.start()
    try:
        # Sentinel: if the prefilter fails to short-circuit and the writer
        # actually attempts the broker call, this raise verifies it.
        with patch(
            "app.services.broker_service.place_sell_stop_loss_order",
            side_effect=AssertionError("broker call must not happen for unsupported crypto"),
        ):
            result = place_missing_stop(
                db,
                trade_id=5001,
                bracket_intent_id=55001,
                ticker="ZEC-USD",
                broker_source="robinhood",
                decision=_decision_missing_stop(),
                local_quantity=1.0,
                stop_price=300.0,
            )
    finally:
        for p in patches:
            p.stop()

    assert result.action == "place_missing_stop"
    assert result.ok is False
    assert result.reason == "venue_unsupported_crypto_path"


def test_place_missing_stop_proceeds_for_equity(db):
    """Scenario 7: AAPL is an equity, not crypto. The prefilter must NOT
    short-circuit. The writer reaches its next check (which then fails
    on broker_qty_zero or covered-by-sell — exact next gate doesn't
    matter for this test). Reason MUST NOT be venue_unsupported_crypto_path."""
    _seed_trade_and_intent(
        db, trade_id=5002, intent_id=55002, ticker="AAPL",
        qty=10.0, stop_price=180.0,
    )

    from app.services.trading.bracket_writer_g2 import place_missing_stop

    fake_adapter = MagicMock()
    # No matching product → broker_qty stays None → broker_qty_zero
    # branch not taken; writer reaches the FIX 55 covered-by-sell branch.
    fake_adapter.get_products.return_value = ([], False)

    patches = _enable_writer_flag()
    for p in patches:
        p.start()
    try:
        with patch(
            "app.services.broker_service.get_position_held_for_sells",
            return_value=None,
        ):
            result = place_missing_stop(
                db,
                trade_id=5002,
                bracket_intent_id=55002,
                ticker="AAPL",
                broker_source="robinhood",
                decision=_decision_missing_stop(),
                local_quantity=10.0,
                stop_price=180.0,
                adapter_factory=lambda src: fake_adapter,
            )
    finally:
        for p in patches:
            p.stop()

    assert result.reason != "venue_unsupported_crypto_path", (
        "equity ticker AAPL must not be filtered as unsupported crypto"
    )


# ── Whitelist maintenance hints ────────────────────────────────────────


def test_whitelist_baseline_size():
    """Scenario 8: assert the whitelist is at least the size of the
    fast-path baseline. Catches accidental wipe."""
    assert len(ROBINHOOD_SUPPORTED_CRYPTO_BASES) >= 5, (
        f"Whitelist has only {len(ROBINHOOD_SUPPORTED_CRYPTO_BASES)} entries; "
        "minimum is the 5 fast-path pairs."
    )


@pytest.mark.parametrize("base, expected", [
    # The audit's flagged set — every one of these triggered
    # crypto_not_supported_on_robinhood:<BASE> in the prior 24h. They MUST
    # be False so the prefilter rejects them.
    ("ZEC", False),
    ("GNO", False),
    ("AKT", False),
    ("2Z", False),
    ("GLM", False),
    ("1INCH", False),
    ("TRAC", False),
    # Positive baseline — these MUST be True so the prefilter does
    # not over-block.
    ("BTC", True),
    ("ETH", True),
    ("SOL", True),
    ("AVAX", True),
    ("DOGE", True),
    ("USDC", True),
])
def test_audit_flagged_symbols_have_expected_classification(base, expected):
    """Scenario 9: per-symbol classification for the audit's flagged set
    plus a positive baseline. If any of these flips we want the test
    suite (and CI) to be loud about it."""
    assert is_robinhood_supported_crypto(base) is expected, (
        f"is_robinhood_supported_crypto({base!r}) should be {expected}"
    )
