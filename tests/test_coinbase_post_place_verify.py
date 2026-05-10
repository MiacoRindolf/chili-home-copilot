"""f-coinbase-post-place-verify-routing-fix (2026-05-10).

Pin the post-place-verify routing in
``bracket_writer_g2.place_missing_stop``:

  * Coinbase orders verify via ``adapter.get_order_status``
    (Coinbase-side mirror of the polling loop).
  * Robinhood orders verify via
    ``broker_service.verify_order_landed`` — byte-identical to the
    pre-fix behaviour.
  * Adapter state-vocabulary mapping covers the Coinbase Advanced
    Trade order states (PENDING / OPEN / FILLED / CANCELLED /
    EXPIRED / FAILED).
  * Orphan recovery: when a prior sweep marked the intent
    'unverified' but the broker order is still OPEN at Coinbase,
    the writer adopts the order instead of placing a duplicate.

Pre-fix bug: ``broker_service.verify_order_landed`` is hard-wired to
``api.robinhood.com``. For Coinbase order UUIDs that returned 404 and
the writer's verdict timed out to "unknown" every cycle, leaving
intents permanently 'unverified' while 4 real stop orders sat
orphaned at Coinbase (b3c14ef6 AERGO-USD, 545eeffe 1INCH-USD,
d1b91a9c ACX-USD, b13e8058 RARE-USD).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.trading import bracket_writer_g2 as bw
from app.services.trading.bracket_reconciler import ReconciliationDecision


def _decision_missing_stop():
    return ReconciliationDecision(
        kind="missing_stop", severity="warn", delta_payload={},
    )


@pytest.fixture()
def reset_cooldowns():
    bw._intent_reject_cooldown.clear()
    bw._intent_post_place_cooldown.clear()
    bw._intent_exception_cooldown.clear()
    yield
    bw._intent_reject_cooldown.clear()
    bw._intent_post_place_cooldown.clear()
    bw._intent_exception_cooldown.clear()


def _common_monkeypatches(monkeypatch):
    """Stub heavy plumbing so the verify/orphan path is the only thing
    under test."""
    from app.config import settings
    monkeypatch.setattr(
        settings, "chili_bracket_writer_g2_enabled", True, raising=False,
    )
    monkeypatch.setattr(
        settings, "chili_bracket_writer_g2_place_missing_stop", True,
        raising=False,
    )
    monkeypatch.setattr(
        settings, "chili_coinbase_stop_limit_buffer_pct", 0.005,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.broker_service.get_position_held_for_sells",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.trading.bracket_writer_g2._g2_event",
        lambda *a, **kw: None,
    )


def _no_orphan_db():
    """A MagicMock db whose execute().fetchone() returns None.

    Matches the 'no prior unverified event' lookup result, so the
    orphan-recovery hook falls through to normal placement.
    """
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = None
    return db


# ── 1. Coinbase verify routes to adapter, not broker_service ────────


def test_coinbase_verify_uses_adapter_not_robinhood(
    reset_cooldowns, monkeypatch,
):
    """For a Coinbase order, the writer must call
    ``adapter.get_order_status``, NOT ``broker_service.verify_order_landed``.
    """
    _common_monkeypatches(monkeypatch)

    def _raise_if_called(*a, **kw):
        raise AssertionError(
            "broker_service.verify_order_landed must NOT be called for "
            "a Coinbase order — the 404 against api.robinhood.com is "
            "the exact bug this brief fixes."
        )

    monkeypatch.setattr(
        "app.services.broker_service.verify_order_landed",
        _raise_if_called,
    )

    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_limit_order_gtc.return_value = {
        "ok": True, "order_id": "CB-NEW-1",
    }
    adapter.get_order_status.return_value = {
        "ok": True, "state": "confirmed", "raw": {},
    }

    res = bw.place_missing_stop(
        db=_no_orphan_db(),
        trade_id=42,
        bracket_intent_id=42,
        ticker="ADA-USD",
        broker_source="coinbase",
        decision=_decision_missing_stop(),
        local_quantity=100.0,
        stop_price=0.45,
        adapter_factory=lambda src: adapter,
    )

    assert adapter.get_order_status.called, (
        "Coinbase verify path must invoke adapter.get_order_status"
    )
    assert adapter.get_order_status.call_args.args[0] == "CB-NEW-1"
    assert res.ok is True
    assert res.new_stop_order_id == "CB-NEW-1"


# ── 2. Coinbase OPEN/confirmed → resting → ok=True ──────────────────


def test_coinbase_verify_confirmed_state_returns_ok(
    reset_cooldowns, monkeypatch,
):
    """Coinbase get_order_status returning state='confirmed' must
    surface as a successful WriterAction with the new order id."""
    _common_monkeypatches(monkeypatch)

    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_limit_order_gtc.return_value = {
        "ok": True, "order_id": "CB-OPEN-1",
    }
    adapter.get_order_status.return_value = {
        "ok": True, "state": "confirmed", "raw": {"status": "OPEN"},
    }

    res = bw.place_missing_stop(
        db=_no_orphan_db(),
        trade_id=1, bracket_intent_id=1,
        ticker="AERGO-USD", broker_source="coinbase",
        decision=_decision_missing_stop(),
        local_quantity=50.0, stop_price=0.123,
        adapter_factory=lambda src: adapter,
    )

    assert res.ok is True
    assert res.reason == "ok"
    assert res.new_stop_order_id == "CB-OPEN-1"


# ── 3. Coinbase CANCELLED → rejected → terminal-class failure ───────


def test_coinbase_verify_cancelled_state_is_terminal_reject(
    reset_cooldowns, monkeypatch,
):
    """Coinbase get_order_status returning state='cancelled' must map
    to verdict='rejected' and surface as post_accept_rejected, with
    the intent NOT transitioning to confirmed."""
    _common_monkeypatches(monkeypatch)

    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_limit_order_gtc.return_value = {
        "ok": True, "order_id": "CB-CANCELLED-1",
    }
    adapter.get_order_status.return_value = {
        "ok": True, "state": "cancelled", "raw": {"status": "CANCELLED"},
    }

    res = bw.place_missing_stop(
        db=_no_orphan_db(),
        trade_id=2, bracket_intent_id=2,
        ticker="1INCH-USD", broker_source="coinbase",
        decision=_decision_missing_stop(),
        local_quantity=10.0, stop_price=0.5,
        adapter_factory=lambda src: adapter,
    )

    assert res.ok is False
    assert res.reason == "post_accept_rejected"
    assert res.new_stop_order_id == "CB-CANCELLED-1"


# ── 4. Coinbase API unreachable → unknown → unverified, NOT confirmed


def test_coinbase_verify_unreachable_is_unverified_not_falsely_confirmed(
    reset_cooldowns, monkeypatch,
):
    """Coinbase get_order_status raising / returning ok=False must NOT
    fabricate a 'confirmed' state. The writer's verdict times out to
    'unknown' and the intent stays unverified (post-place cooldown
    armed)."""
    _common_monkeypatches(monkeypatch)

    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_limit_order_gtc.return_value = {
        "ok": True, "order_id": "CB-DOWN-1",
    }
    # Adapter is reachable for place but down for status (e.g. 503).
    adapter.get_order_status.return_value = {
        "ok": False, "error": "503 transport_error", "state": None,
    }
    # Speed up the poll loop — default 3s / 0.5s would slow the suite.
    # Swap the helper for a fast variant that calls the real one with
    # tight timing so we still exercise the production verdict logic.
    _real_verify = bw._verify_via_coinbase
    monkeypatch.setattr(
        bw, "_verify_via_coinbase",
        lambda a, oid, **kw: _real_verify(
            a, oid, max_wait_s=0.05, poll_interval_s=0.01,
        ),
    )

    res = bw.place_missing_stop(
        db=_no_orphan_db(),
        trade_id=3, bracket_intent_id=3,
        ticker="ACX-USD", broker_source="coinbase",
        decision=_decision_missing_stop(),
        local_quantity=5.0, stop_price=0.25,
        adapter_factory=lambda src: adapter,
    )

    assert res.ok is False
    assert res.reason == "unverified"
    assert res.new_stop_order_id == "CB-DOWN-1"


# ── 5. Robinhood verify routing unchanged (regression) ──────────────


def test_robinhood_verify_routing_unchanged(
    reset_cooldowns, monkeypatch,
):
    """The RH path must still go through broker_service.verify_order_landed.
    Coinbase get_order_status must NOT be touched for a RH order."""
    _common_monkeypatches(monkeypatch)

    rh_verify_calls: list[str] = []

    def _rh_verify(oid, **kw):
        rh_verify_calls.append(oid)
        return ("resting", "confirmed")

    monkeypatch.setattr(
        "app.services.broker_service.verify_order_landed", _rh_verify,
    )

    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_loss_sell_order.return_value = {
        "ok": True, "order_id": "RH-1",
    }
    # RH adapter doesn't have get_order_status — assert it's never called.
    adapter.get_order_status.side_effect = AssertionError(
        "adapter.get_order_status must NOT be invoked for a Robinhood "
        "order — the verify path is broker_service.verify_order_landed."
    )

    res = bw.place_missing_stop(
        db=_no_orphan_db(),
        trade_id=10, bracket_intent_id=10,
        ticker="AAPL", broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=10.0, stop_price=145.5,
        adapter_factory=lambda src: adapter,
    )

    assert rh_verify_calls == ["RH-1"], (
        "Robinhood path must call broker_service.verify_order_landed "
        f"exactly once; got calls={rh_verify_calls}"
    )
    assert res.ok is True
    assert res.reason == "ok"


# ── 6. Orphan recovery adopts the prior unverified order ────────────


def test_coinbase_orphan_recovery_adopts_open_order(
    reset_cooldowns, monkeypatch,
):
    """If a prior g2_place_missing_stop_unverified event exists and the
    recorded broker order is still OPEN at Coinbase, the writer must
    adopt it (transition to confirmed_at_broker, return
    reason='orphan_recovered') WITHOUT calling place_stop_limit_order_gtc.
    """
    _common_monkeypatches(monkeypatch)

    db = MagicMock()
    db.execute.return_value.fetchone.return_value = ("ORPHAN-OID-ABC",)

    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.get_order_status.return_value = {
        "ok": True, "state": "confirmed", "raw": {"status": "OPEN"},
    }
    # Sentinel: must NOT place a fresh stop.
    adapter.place_stop_limit_order_gtc.side_effect = AssertionError(
        "orphan recovery must not place a duplicate stop"
    )

    res = bw.place_missing_stop(
        db=db,
        trade_id=99, bracket_intent_id=99,
        ticker="RARE-USD", broker_source="coinbase",
        decision=_decision_missing_stop(),
        local_quantity=10.0, stop_price=0.05,
        adapter_factory=lambda src: adapter,
    )

    assert res.ok is True
    assert res.reason == "orphan_recovered"
    assert res.new_stop_order_id == "ORPHAN-OID-ABC"
    adapter.get_order_status.assert_called_once_with("ORPHAN-OID-ABC")
    assert not adapter.place_stop_limit_order_gtc.called


# ── 7. Orphan recovery falls through when prior order is cancelled ──


def test_coinbase_orphan_recovery_skipped_when_prev_cancelled(
    reset_cooldowns, monkeypatch,
):
    """If the prior unverified order is no longer resting at Coinbase
    (state='cancelled'), recovery must fall through to a fresh
    placement attempt."""
    _common_monkeypatches(monkeypatch)

    db = MagicMock()
    db.execute.return_value.fetchone.return_value = ("ORPHAN-DEAD",)

    # First call (orphan lookup) → cancelled. Second call (post-place
    # verify) → confirmed. Use side_effect to model the sequence.
    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.get_order_status.side_effect = [
        {"ok": True, "state": "cancelled", "raw": {}},
        {"ok": True, "state": "confirmed", "raw": {}},
    ]
    adapter.place_stop_limit_order_gtc.return_value = {
        "ok": True, "order_id": "CB-FRESH-1",
    }

    res = bw.place_missing_stop(
        db=db,
        trade_id=100, bracket_intent_id=100,
        ticker="ACX-USD", broker_source="coinbase",
        decision=_decision_missing_stop(),
        local_quantity=5.0, stop_price=0.2,
        adapter_factory=lambda src: adapter,
    )

    adapter.place_stop_limit_order_gtc.assert_called_once()
    assert res.ok is True
    assert res.reason == "ok"
    assert res.new_stop_order_id == "CB-FRESH-1"


# ── 8. Coinbase state-vocabulary mapping (unit) ─────────────────────


class _FakeCoinbaseClient:
    """Stand-in for the Coinbase SDK client used by the adapter.

    Returns a get_order response keyed on the canonical raw status."""
    def __init__(self, status_raw):
        self._status_raw = status_raw

    def get_order(self, *, order_id):
        # The real SDK returns an object whose .to_dict() yields
        # {"order": {"order_id": ..., "status": "<RAW>"}, ...}.
        return {"order": {"order_id": order_id, "status": self._status_raw}}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("PENDING", "unconfirmed"),
        ("OPEN", "confirmed"),
        ("FILLED", "filled"),
        ("CANCELLED", "cancelled"),
        ("CANCELED", "cancelled"),    # US-spelling variant
        ("EXPIRED", "cancelled"),
        ("FAILED", "failed"),
        ("REJECTED", "rejected"),
    ],
)
def test_coinbase_get_order_status_state_vocabulary(
    monkeypatch, raw, expected,
):
    """Each Coinbase Advanced Trade order state must normalize to the
    Robinhood-compatible vocabulary the writer's verify state machine
    consumes."""
    from app.services.trading.venue.coinbase_spot import CoinbaseSpotAdapter
    from app.services.trading.venue import rate_limiter

    rate_limiter.reset_for_tests()
    monkeypatch.setattr(
        CoinbaseSpotAdapter, "is_enabled", lambda self: True,
    )
    monkeypatch.setattr(
        CoinbaseSpotAdapter, "_require_client",
        lambda self: _FakeCoinbaseClient(raw),
    )

    adapter = CoinbaseSpotAdapter()
    out = adapter.get_order_status("any-oid")

    assert out["ok"] is True, f"raw={raw!r} → {out!r}"
    assert out["state"] == expected, (
        f"Coinbase raw {raw!r} should normalize to {expected!r}, got "
        f"{out['state']!r}"
    )


def test_coinbase_get_order_status_404_returns_not_found(monkeypatch):
    """SDK errors that look like 404s must come back as not_found, not
    a fabricated state."""
    from app.services.trading.venue.coinbase_spot import CoinbaseSpotAdapter
    from app.services.trading.venue import rate_limiter

    rate_limiter.reset_for_tests()

    class _NotFoundClient:
        def get_order(self, *, order_id):
            raise RuntimeError("404 Not Found: order does not exist")

    monkeypatch.setattr(
        CoinbaseSpotAdapter, "is_enabled", lambda self: True,
    )
    monkeypatch.setattr(
        CoinbaseSpotAdapter, "_require_client", lambda self: _NotFoundClient(),
    )

    adapter = CoinbaseSpotAdapter()
    out = adapter.get_order_status("ghost-oid")

    assert out["ok"] is False
    assert out["error"] == "not_found"
    assert out["state"] is None


def test_coinbase_get_order_status_empty_order_id():
    from app.services.trading.venue.coinbase_spot import CoinbaseSpotAdapter
    adapter = CoinbaseSpotAdapter()
    out = adapter.get_order_status("")
    assert out["ok"] is False
    assert out["state"] is None


# ── 9. _verify_via_coinbase helper unit tests ───────────────────────


def test_verify_via_coinbase_resting_on_confirmed():
    adapter = MagicMock()
    adapter.get_order_status.return_value = {
        "ok": True, "state": "confirmed", "raw": {},
    }
    verdict, observed = bw._verify_via_coinbase(
        adapter, "oid", max_wait_s=0.1, poll_interval_s=0.01,
    )
    assert verdict == "resting"
    assert observed == "confirmed"


def test_verify_via_coinbase_rejected_on_cancelled():
    adapter = MagicMock()
    adapter.get_order_status.return_value = {
        "ok": True, "state": "cancelled", "raw": {},
    }
    verdict, observed = bw._verify_via_coinbase(
        adapter, "oid", max_wait_s=0.1, poll_interval_s=0.01,
    )
    assert verdict == "rejected"
    assert observed == "cancelled"


def test_verify_via_coinbase_unknown_on_persistent_404():
    adapter = MagicMock()
    adapter.get_order_status.return_value = {
        "ok": False, "error": "not_found", "state": None,
    }
    verdict, observed = bw._verify_via_coinbase(
        adapter, "oid", max_wait_s=0.05, poll_interval_s=0.01,
    )
    assert verdict == "unknown"
    assert observed is None


def test_verify_via_coinbase_unknown_on_persistent_exception():
    """get_order_status raising every poll must surface as unknown, not
    silently confirmed."""
    adapter = MagicMock()
    adapter.get_order_status.side_effect = RuntimeError("transport down")
    verdict, observed = bw._verify_via_coinbase(
        adapter, "oid", max_wait_s=0.05, poll_interval_s=0.01,
    )
    assert verdict == "unknown"
    assert observed is None


def test_verify_via_coinbase_empty_oid():
    verdict, observed = bw._verify_via_coinbase(MagicMock(), "")
    assert verdict == "unknown"
    assert observed is None
