"""Settle the book against fills we already recorded, instead of waiting for the broker.

WHY THIS FILE EXISTS. Six legs carry 91.33% of the live book's net measured loss
(-$9,879.24 of -$10,817.06) and every one of them is UNCONFIRMED against broker truth.
Until they settle, no A/B this project runs is measured against a single-valued book —
including the benches run today. The operator's instruction was to MOCK the fills from our
own recorded history rather than wait for live broker traffic, and that turns out to be
possible: `trading_automation_events` and `momentum_fill_outcomes` already carry the order
ids, prices, quantities and timestamps a settlement needs.

THE CASE BUILT HERE — TKLF 2026-07-10, outcome 199025, session 12685, realized
-$2,116.32. It is the cleanest of the six: a complete round trip with a broker order id on
BOTH legs, unconfirmed for one reason only — its terminal_at sits far outside the batch
reconciler's 2.0-day lookback, so no pass will ever pick it up again. Every number below is
transcribed from the recorded rows, not constructed:

    entry  3f135f7e-71e2-49fc-9371-aab2d4209962  buy  13,227 @ 2.40  14:18:52.706973
    exit   183c5c10-166b-4164-96e8-3f49ebeed4e7  sell 13,227 @ 2.24  14:20:12.802512
    (13227 * 2.24) - (13227 * 2.40) = -2,116.32  — matches realized_pnl_usd to the cent

DB-free: the reconciler's `broker_orders_reader` kwarg replaces the live Alpaca reader, and
`_FakeDb` serves the three SQL shapes the attribution path issues. No production module is
edited and no live code path runs. Idiom copied from test_broker_truth_attribution_0902.py.

Runnable: pytest tests/test_broker_truth_recorded_fills_0908.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import outcome_reconcile as orc
from app.services.trading.venue.protocol import NormalizedOrder

SYMBOL = "TKLF"
ENTRY_OID = "3f135f7e-71e2-49fc-9371-aab2d4209962"
EXIT_OID = "183c5c10-166b-4164-96e8-3f49ebeed4e7"
QTY = 13227.0
ENTRY_PX = 2.40
EXIT_PX = 2.24
EXPECTED_PNL = QTY * EXIT_PX - QTY * ENTRY_PX
EXPECTED_NOTIONAL = QTY * ENTRY_PX
ENTRY_FILLED_AT = "2026-07-10 14:18:52.706973+00:00"
EXIT_FILLED_AT = "2026-07-10 14:20:12.802512+00:00"


def _o(oid, cid, side, status, filled, px, filled_at, order_type="limit"):
    """The precedent's helper, parameterised on product_id — the attribution path drops
    any order whose product_id differs from the session symbol."""
    return NormalizedOrder(
        order_id=oid,
        client_order_id=cid,
        product_id=SYMBOL,
        side=side,
        status=status,
        order_type=order_type,
        filled_size=float(filled),
        average_filled_price=(float(px) if px is not None else None),
        created_time=filled_at,
        raw={"filled_at": filled_at, "submitted_at": filled_at},
    )


def _recorded_orders():
    """Exactly what the broker's own order list showed, from the recorded events."""
    return [
        _o(ENTRY_OID, "chili_ml_e_12685_3b5cb2e8_a7a59ad867", "buy", "filled",
           QTY, ENTRY_PX, ENTRY_FILLED_AT),
        _o(EXIT_OID, None, "sell", "filled", QTY, EXIT_PX, EXIT_FILLED_AT,
           order_type="market"),
        # The superseded entry attempt and the scale-out that never filled. They are in
        # the recording, so they are in the fixture: an attribution that only works on a
        # pruned order list is not an attribution.
        _o("cb0a6c5a-88f6-4863-8544-17e890d95617",
           "chili_ml_e_12685_3b5cb2e8_28a8b31dbc", "buy", "canceled", 0.0, None, None),
        _o("e0806f93-5639-4e75-82b5-26149b155136", None, "sell", "canceled",
           0.0, None, None),
    ]


# (side, leg_seq, fill_source, broker_fill_price, qty, fees_usd, settled_pnl,
#  settled_fees, realized_pnl, entry_price, broker_order_id, fill_ts)
_LEDGER = [
    ("entry", 0, "broker_confirmed", ENTRY_PX, QTY, 0.0, None, None, None, None,
     ENTRY_OID, "2026-07-10 14:18:52.706973"),
    ("exit", 0, "broker_confirmed", EXIT_PX, QTY, 0.0, None, None, EXPECTED_PNL,
     ENTRY_PX, EXIT_OID, "2026-07-10 14:20:12.802512"),
]


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    def __init__(self, ledger_rows, *, collisions=None):
        self.ledger_rows = ledger_rows
        self.collisions = collisions or []
        self.sql = []

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.sql.append(sql)
        if "broker_order_id IS NOT NULL" in sql:
            return _Res([(r[10], r[11]) for r in self.ledger_rows if r[10]])
        if "FROM momentum_fill_outcomes" in sql:
            return _Res([r[:10] for r in self.ledger_rows])
        if "momentum_automation_outcomes" in sql:
            return _Res(list(self.collisions))
        raise AssertionError("unexpected SQL in DB-free test: " + sql)


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(
        orc.settings,
        "chili_momentum_outcome_recon_broker_attribution_enabled", True, raising=False)
    monkeypatch.setattr(
        orc.settings,
        "chili_momentum_outcome_recon_broker_attribution_grace_seconds", 900,
        raising=False)
    monkeypatch.setattr(
        orc.settings,
        "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 20,
        raising=False)


#: The envelope as it ACTUALLY survives on this session — and its emptiness is the
#: fixture, not an omission. `live_recycled` fired at 14:20:30, ten seconds after the
#: exit filled and before the outcome row existed, and it cleared every key
#: `_has_entry_evidence` reads: entry_order_id, entry_client_order_id, position,
#: exit_order_id, scale_limit_order_id. Verified against the live row: of those keys and
#: `last_exit_reason`, only `last_exit_reason` is still present.
#:
#: That is deliberately kept here, because settling from a recycle-wiped envelope is the
#: harder case and the one the six real legs actually present. The reconciler is already
#: built for it — it spends ONE proof read rather than skipping permanently
#: (outcome_reconcile.py:635-641, the CANF 19471 lesson) — and these tests pin that the
#: proof read, fed our own recorded orders, is enough to settle the leg.
_POST_RECYCLE_ENVELOPE = {"last_exit_reason": "bailout"}


def _sess():
    return SimpleNamespace(
        id=12685, symbol=SYMBOL, execution_family="alpaca_spot",
        mode="live", state="live_cancelled",
        started_at=orc._naive_utc("2026-07-10 14:07:36.274680+00:00"),
        ended_at=orc._naive_utc("2026-07-10 14:23:21.297198+00:00"),
        risk_snapshot_json={"momentum_live_execution": dict(_POST_RECYCLE_ENVELOPE)},
    )


def _outcome():
    return SimpleNamespace(
        id=199025, session_id=12685, symbol=SYMBOL, mode="live",
        realized_pnl_usd=EXPECTED_PNL, return_bps=None,
        outcome_class="bailout", exit_reason="bailout",
        execution_family="alpaca_spot",
        broker_recon_status=None, broker_realized_pnl_usd=None,
        broker_notional_basis_usd=None, broker_return_bps=None,
        broker_win=None, broker_reconciled_at=None, broker_divergence_usd=None,
        broker_recon_detail_json=None,
        terminal_at=orc._naive_utc("2026-07-10 14:23:21.297198+00:00"),
    )


def test_the_envelope_really_is_evidence_free_after_the_recycle():
    """Pins the premise of the whole file. If a future change repopulated the envelope
    this fixture would silently start testing the EASY path instead of the real one."""
    assert orc._has_entry_evidence(_POST_RECYCLE_ENVELOPE) is False


def _reconcile(orders=None, *, readable=True, truncated=False, ledger=None):
    db = _FakeDb(_LEDGER if ledger is None else ledger)
    outcome, sess = _outcome(), _sess()
    seen = {}

    def reader(symbol, after, until):
        seen["symbol"], seen["after"], seen["until"] = symbol, after, until
        return {
            "readable": readable,
            "orders": _recorded_orders() if orders is None else orders,
            "truncated": truncated,
        }

    res = orc.reconcile_one_outcome(db, outcome, sess, broker_orders_reader=reader)
    return outcome, res, seen


def _attribution(outcome, res):
    detail = outcome.broker_recon_detail_json or res.get("detail") or {}
    return (detail or {}).get("broker_attribution") or {}


def test_the_recorded_fills_settle_the_leg():
    """The whole point: a leg the batch reconciler can never reach again settles from our
    own recording, and the number it produces is the broker's, not the book's."""
    outcome, res, _ = _reconcile()
    assert outcome.broker_recon_status == orc.STATUS_RECONCILED, res
    assert outcome.broker_realized_pnl_usd == pytest.approx(EXPECTED_PNL, abs=1e-6)
    assert outcome.broker_notional_basis_usd == pytest.approx(EXPECTED_NOTIONAL, abs=1e-6)
    assert outcome.broker_win is False


def test_the_broker_number_agrees_with_the_book_for_this_leg():
    """TKLF is a case where the two books AGREE, so a divergence here would mean the
    fixture or the attribution is wrong — not that the book was."""
    outcome, _, _ = _reconcile()
    assert outcome.broker_realized_pnl_usd == pytest.approx(
        outcome.realized_pnl_usd, abs=1e-6)


def test_the_attribution_is_flat_and_covers_the_whole_position():
    outcome, res, _ = _reconcile()
    ba = _attribution(outcome, res)
    assert ba.get("attr_status") == orc.ATTR_FLAT, ba
    assert ba.get("open_qty") == pytest.approx(QTY)
    assert ba.get("close_qty") == pytest.approx(QTY)
    assert ba.get("ledger_ids_missing_from_broker") == [], (
        "the listing must provably cover the session, or the settlement is not evidence")


def test_the_window_is_the_session_plus_the_binding_grace():
    """started_at - 120 s, ended_at + the attribution grace. A window narrower than the
    fills would make the leg read as unattributable rather than settled."""
    _, _, seen = _reconcile()
    assert seen["after"] <= orc._naive_utc(ENTRY_FILLED_AT)
    assert seen["until"] >= orc._naive_utc(EXIT_FILLED_AT)
    assert seen["symbol"] == SYMBOL


def _source(outcome, res):
    detail = outcome.broker_recon_detail_json or res.get("detail") or {}
    return (detail or {}).get("source"), (detail or {}).get("attribution_version")


def test_a_settled_leg_records_that_the_BROKER_produced_the_number():
    """`reconciled` alone does not mean the broker was consulted — the reconciler also
    settles from the ledger when the listing is unusable, and that fallback is deliberate.
    What separates the two is `source` and `attribution_version`, so those are what a
    broker-attributed settlement must be asserted on."""
    outcome, res, _ = _reconcile()
    source, version = _source(outcome, res)
    assert source == "broker_orders_attributed"
    assert version == orc.ATTRIBUTION_VERSION


def test_an_unreadable_broker_never_claims_broker_attribution():
    """The failure that matters most: an unread source must never masquerade as a read
    one. The ledger fallback may still settle the row — that is by design — but it must
    say so, and it must not stamp the attribution version."""
    outcome, res, _ = _reconcile(readable=False)
    source, version = _source(outcome, res)
    assert source == "ledger_confirmed"
    assert version is None, "an unread listing must never earn the attribution version"
    ba = _attribution(outcome, res)
    assert ba.get("attr_status") == orc.ATTR_UNREADABLE


def test_a_truncated_listing_never_claims_broker_attribution():
    """A partial listing can attribute a partial position and call it flat. It must be
    refused BEFORE attribution, not reconciled from."""
    outcome, res, _ = _reconcile(truncated=True)
    source, version = _source(outcome, res)
    assert source == "ledger_confirmed"
    assert version is None
    ba = _attribution(outcome, res)
    assert ba.get("attr_status") == orc.ATTR_TRUNCATED


def test_a_listing_missing_the_ledgers_own_order_never_claims_attribution():
    """If the broker list lacks an order our ledger says filled, the listing does not
    cover the session — so it may not be attributed from, however complete it looks."""
    partial = [o for o in _recorded_orders() if o.order_id != EXIT_OID]
    outcome, res, _ = _reconcile(orders=partial)
    ba = _attribution(outcome, res)
    assert ba.get("attr_status") == orc.ATTR_LISTING_INCOMPLETE, ba
    assert EXIT_OID in (ba.get("ledger_ids_missing_from_broker") or []), ba
    # `source` only names the path that RAN; the version is what marks a certification,
    # and no number may be written from a listing that does not cover the session.
    _, version = _source(outcome, res)
    assert version is None
    assert outcome.broker_realized_pnl_usd is None
