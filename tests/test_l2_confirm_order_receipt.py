"""The confirm that PLACED an order is on the order — driven through the REAL runner.

[2] review, 2026-09-11. ``live_l2_confirm_decision`` is emitted ON CHANGE OF REASON, and
its key (``le["l2_confirm_last_reason"]``) survives recycles, so a pass that repeats its
reason writes nothing. Measured on the live book: 35 of 99 ``live_entry_submitted``
(the 72 h to 2026-09-11 12:00Z) had no decision receipt between their latest
``live_entry_pending_place`` and the submit — TNON session 22141 on 09-11 has ONE receipt
(``l2_confirm_tape_thrust`` at 10:40:46) and then four orders at 10:41:47, 10:43:38,
10:44:30 and 10:48:18, each with nothing of its own. PSIG 21640 (09-10) submitted on a
confirm whose last booked decision was a DEFER.

The first fix stamped only the reason's NAME on the order and was pinned by a test that
searched the source text of ``tick_live_session`` — green if the lines were commented out
or if a later gate rebound ``_l2c_reason`` with another value. These tests instead drive
``tick_live_session`` from ``live_pending_entry`` to a real broker ``place`` call and read
the ``live_entry_submitted`` ROW it wrote: the order must carry the value that decided
(``buy_share_delta`` and its halves), the book legs that could release a defer, the tape
read's own latency (``tape_read_ms`` — the confirmer runs before ``place_profile``'s clock
starts, so nothing else times it), and a fail-open's cause.

DB-backed (``TEST_DATABASE_URL`` must end in ``_test``). Reuses the pending-entry fixture
chain of tests/test_momentum_limit_entry.py (a Coinbase SOL-USD session, a mocked adapter).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.trading import TradingAutomationEvent
import app.services.trading.momentum_neural.live_runner as lr
from app.services.trading.momentum_neural.entry_gates import L2_CONFIRM_ORDER_RECEIPT_KEYS
from app.services.trading.momentum_neural.live_runner import tick_live_session
from app.services.trading.venue.coinbase_spot import reset_duplicate_client_order_guard_for_tests

from tests.test_momentum_limit_entry import _mk_pending_entry_session
from tests.test_momentum_live_runner import _mk_adapter


@pytest.fixture(autouse=True)
def _runner_boundaries(monkeypatch, stable_non_alpaca_account_identity):
    """The same boundaries test_entry_places_marketable_limit_not_market runs under."""
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda _symbol: "regular",
    )
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda _ef: True)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *_a, **_k: 10_000.0,
    )
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", False)
    monkeypatch.setattr(settings, "chili_coinbase_maker_only_enabled", False)
    monkeypatch.setattr(settings, "brain_decision_packet_required_for_runners", False)
    monkeypatch.setattr(settings, "chili_momentum_l2_confirm_enabled", True)


def _tick(db: Session, sess, ad):
    with patch(
        "app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
        return_value=False,
    ):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    return out


def _events(db: Session, sess_id: int, event_type: str) -> list[TradingAutomationEvent]:
    return (
        db.query(TradingAutomationEvent)
        .filter(TradingAutomationEvent.session_id == sess_id,
                TradingAutomationEvent.event_type == event_type)
        .order_by(TradingAutomationEvent.id)
        .all()
    )


def _seed_last_reason(db: Session, sess, reason: str) -> None:
    """Put the session in the TNON 22141 / PSIG 21640 state: the on-change key already
    holds the reason the submitting pass is about to produce."""
    snap = dict(sess.risk_snapshot_json or {})
    le = dict(snap.get("momentum_live_execution") or {})
    le["l2_confirm_last_reason"] = reason
    snap["momentum_live_execution"] = le
    sess.risk_snapshot_json = snap
    db.commit()
    db.refresh(sess)


# What a real `l2_confirm_secondary_override` pass knows (values from a live-shaped
# receipt; `selection_contract` / `gap_trim_basis` are window plumbing that belongs on the
# decision receipt, not the order).
_OVERRIDE_DBG = {
    "reason": "l2_confirm_secondary_override",
    "buy_share_delta": -0.0421,
    "front_buy_share": 0.61,
    "back_buy_share": 0.5679,
    "n_ticks": 255,
    "window_prints": 255,
    "span_s": 35.4,
    "print_age_s": 0.8,
    "print_stale": False,
    "tape_read_ms": 2286.4,
    "book_readable": True,
    "book_unreadable_why": None,
    "book_would_agree": True,
    "ofi_agrees": False,
    "depth_rising": True,
    "depth_rising_at": 0.5,
    "ofi": 0.031,
    "micro_edge": -0.4,
    "depth_imbal_pctile": 0.667,
    "ofi_threshold": 0.25,
    "n_snaps": 6,
    "n_ranked": 6,
    "snapshot_age_s": 1.2,
    "book_window_s": 36.2,
    "book_current_within_s": 18.1,
    "book_k": 6,
    "selection_contract": "recorded_publication_v1",
    "gap_trim_basis": "window_gap_p90 x measured_p99_over_p90",
}


def test_a_suppressed_decision_receipt_still_leaves_the_deciding_value_on_the_order(
        db: Session, monkeypatch):
    """THE HOLE, reproduced end to end: the submitting pass repeats its reason, so NO
    `live_l2_confirm_decision` is written for it — and the order it places still carries
    buy_share_delta, both halves, the book legs, and the read's latency, value for value."""
    calls: list[str] = []

    def _confirm(symbol, **_kw):
        calls.append(symbol)
        return "confirm", dict(_OVERRIDE_DBG)

    monkeypatch.setattr(lr, "_l2_entry_confirm", _confirm)
    sess = _mk_pending_entry_session(db, "SOL-USD")
    _seed_last_reason(db, sess, "l2_confirm_secondary_override")
    ad = _mk_adapter()

    out = _tick(db, sess, ad)

    assert calls == ["SOL-USD"], "the confirmer seam ran for this pass"
    assert ad.place_limit_order_gtc.call_count == 1, out
    assert _events(db, sess.id, "live_l2_confirm_decision") == [], (
        "the on-change receipt is suppressed for this pass — the hole being fixed")
    (sub,) = _events(db, sess.id, "live_entry_submitted")
    payload = sub.payload_json
    assert payload["l2_confirm_reason"] == "l2_confirm_secondary_override"
    assert payload["l2_confirm_fallback"] is None
    rec = payload["l2_confirm"]
    assert rec["decision"] == "confirm"
    for k in L2_CONFIRM_ORDER_RECEIPT_KEYS:
        if k in _OVERRIDE_DBG:
            assert rec[k] == _OVERRIDE_DBG[k], k
    # the VALUE that decided, what it was made of, and what the order waited on
    assert rec["buy_share_delta"] == -0.0421
    assert (rec["front_buy_share"], rec["back_buy_share"]) == (0.61, 0.5679)
    assert rec["depth_rising"] is True and rec["ofi_agrees"] is False
    assert rec["tape_read_ms"] == 2286.4
    # compact: window plumbing stays on the decision receipt
    assert "selection_contract" not in rec and "gap_trim_basis" not in rec


@pytest.mark.parametrize("bsd, read_ms", [(0.0133, 41.0), (0.2071, 7530.2)])
def test_each_order_carries_its_own_pass_not_a_stamped_constant(
        db: Session, monkeypatch, bsd, read_ms):
    """TNON 22141 placed four orders on ONE decision receipt. Same reason, suppressed
    receipt, different deciding values: the order carries the value of ITS pass."""

    def _confirm(symbol, **_kw):
        return "confirm", {"reason": "l2_confirm_tape_thrust",
                           "buy_share_delta": bsd, "tape_read_ms": read_ms}

    monkeypatch.setattr(lr, "_l2_entry_confirm", _confirm)
    sess = _mk_pending_entry_session(db, "SOL-USD")
    _seed_last_reason(db, sess, "l2_confirm_tape_thrust")
    ad = _mk_adapter()
    _tick(db, sess, ad)
    assert ad.place_limit_order_gtc.call_count == 1
    assert _events(db, sess.id, "live_l2_confirm_decision") == []
    (sub,) = _events(db, sess.id, "live_entry_submitted")
    rec = sub.payload_json["l2_confirm"]
    assert rec == {"decision": "confirm", "reason": "l2_confirm_tape_thrust",
                   "buy_share_delta": bsd, "tape_read_ms": read_ms}


def test_the_real_confirmers_fail_open_is_named_on_the_order(db: Session):
    """No fake: the REAL `_l2_entry_confirm` on a crypto name (no equity tape) is a named
    fail-open, and the order says so — reason, fallback, and the (trivial) read latency."""
    sess = _mk_pending_entry_session(db, "SOL-USD")
    ad = _mk_adapter()

    out = _tick(db, sess, ad)

    assert ad.place_limit_order_gtc.call_count == 1, out
    (sub,) = _events(db, sess.id, "live_entry_submitted")
    rec = sub.payload_json["l2_confirm"]
    assert rec["decision"] == "confirm"
    assert rec["reason"] == "l2_confirm_no_tape"
    assert rec["fallback"] == "fail_open_confirm"
    assert isinstance(rec["tape_read_ms"], float) and rec["tape_read_ms"] >= 0.0
    assert sub.payload_json["l2_confirm_reason"] == "l2_confirm_no_tape"
    assert sub.payload_json["l2_confirm_fallback"] == "fail_open_confirm"
    # …and the on-change receipt for this first pass exists and agrees with the order
    (dec,) = _events(db, sess.id, "live_l2_confirm_decision")
    assert dec.payload_json["reason"] == rec["reason"]
    assert dec.payload_json["tape_read_ms"] == rec["tape_read_ms"]
