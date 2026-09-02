"""BUDGET / LIVENESS review of the broker-truth attribution seam (2026-09-02).

The lens: can any row shape still starve a newly terminal FILLED session out of
the per-pass broker-read budget, does the prioritisation actually order the
batch, and does the backoff ever permanently stop a row that needs reconciling?

Each test below is a measured failure of the first hardening cut (ba33924 +
a0d0817), re-derived from the live outcome census on 2026-09-02:

    118 live Alpaca outcomes in the scheduler's 2-day lookback
    115 `unreconciled_no_fills` / `cancelled_pre_entry` + 3 `reconciled`
    116 of 118 carry NO entry evidence on the envelope
    ledger legs with broker_order_id: 29 in 14 days, 0 with a NULL fill_ts

Fakes only — no DB, no broker, no network.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import outcome_reconcile as orc

SID = 19471
T_TERMINAL = datetime(2026, 9, 2, 13, 41, 8)


# ── fixtures ─────────────────────────────────────────────────────────────────
def _pair(
    *,
    sid=SID,
    recon_status=None,
    detail=None,
    outcome_class="stop_loss",
    le=None,
    realized=-78.13,
    terminal_at=T_TERMINAL,
    started=datetime(2026, 9, 2, 11, 3, 22),
    ended=datetime(2026, 9, 2, 13, 41, 8),
):
    envelope = {"entry_order_id": "entry-oid-1", "trade_cycles": 1}
    if le is not None:
        envelope = le
    sess = SimpleNamespace(
        id=sid, symbol="CANF", execution_family="alpaca_spot", mode="live",
        state="live_finished", started_at=started, ended_at=ended,
        risk_snapshot_json={"momentum_live_execution": envelope},
    )
    outcome = SimpleNamespace(
        id=200000 + sid, session_id=sid, symbol="CANF", mode="live",
        execution_family="alpaca_spot", outcome_class=outcome_class,
        terminal_at=terminal_at, realized_pnl_usd=realized, return_bps=None,
        broker_recon_status=recon_status, broker_realized_pnl_usd=None,
        broker_return_bps=None, broker_notional_basis_usd=None, broker_win=None,
        broker_divergence_usd=None, broker_reconciled_at=None,
        broker_recon_detail_json=detail,
    )
    return outcome, sess


class _FakeDb:
    """Ledger rows in the `_aggregate_ledger` / `_ledger_legs_for_attribution` shape."""

    def __init__(self, legs=(), fill_legs=()):
        self.legs = list(legs)
        self.fill_legs = list(fill_legs)
        self.sql = []

    def execute(self, stmt, params=None):
        sql = str(getattr(stmt, "text", stmt))
        self.sql.append(sql)
        upper = sql.upper()
        assert "INSERT" not in upper and "UPDATE" not in upper and "DELETE" not in upper
        if "broker_order_id, fill_ts" in sql:
            return SimpleNamespace(fetchall=lambda: list(self.fill_legs))
        if "momentum_fill_outcomes" in sql:
            return SimpleNamespace(fetchall=lambda: list(self.legs))
        return SimpleNamespace(fetchall=lambda: [], fetchone=lambda: None)


def _readable_empty(symbol, after, until):
    return {"readable": True, "orders": [], "truncated": False}


def _unreadable(symbol, after, until):
    return {"readable": False, "orders": [], "error": "credentials_rotated"}


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_enabled", True, raising=False)
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_grace_seconds", 900, raising=False)
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 20, raising=False)
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_no_fill_backoff_seconds", 1800, raising=False)
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_blocking_retry_seconds", 120, raising=False)


# ═════════════════════════════════════════════════════════════════════════════
# (L1) THE BACKOFF MUST NOT MINT A TERMINAL, NEVER-ATTRIBUTED ROW
# ═════════════════════════════════════════════════════════════════════════════
def test_a_backoff_skip_never_carries_an_attribution_version_onto_a_settled_row():
    """THE TRAP: the skip branch copied `attribution_version` forward verbatim.

    Sequence (all reachable — the exit leg is written asynchronously by
    `_record_fill_outcome_safe`, so a pass can run between terminalization and
    the ledger settling):
      pass 1  ledger empty, listing readable-but-empty → `no_owned_fills`
              → attribution_version stamped + a retry horizon armed
      pass 2  inside the horizon → SKIP. But the ledger has since settled into a
              closed round trip, so the ledger verdict is now `reconciled` —
              written together with the INHERITED version.
      → `needs_reconcile` is False forever. The row is terminal, carries the
        LEDGER-ONLY number, and was never attributed: exactly the CANF-19471
        −78.13-instead-of-−186.98 undercount, minted by the mechanism that was
        added to protect the read budget.
    """
    o, s = _pair()
    orc.reconcile_one_outcome(_FakeDb([]), o, s, broker_orders_reader=_readable_empty)
    d1 = o.broker_recon_detail_json
    assert d1["broker_attribution"]["attr_status"] == orc.ATTR_NO_OWNED_FILLS
    assert d1["attribution_version"] == orc.ATTRIBUTION_VERSION
    assert orc.broker_read_plan(o, s)["read"] is False  # backed off

    # the ledger settles into a clean 355/355 round trip inside the horizon
    # (side, leg_seq, fill_source, fill_price, qty, fees, settled_pnl,
    #  settled_fees, lane_pnl, entry_price) — the `_aggregate_ledger` shape.
    settled = [("entry", 0, "broker_confirmed", 4.34, 355.0, 0.0, None, None, None, None),
               ("exit", 1, "broker_confirmed", 4.119915, 355.0, 0.0, -78.130175, 0.0,
                -78.130175, 4.34)]
    calls = []
    orc.reconcile_one_outcome(
        _FakeDb(settled, fill_legs=[("entry-oid-1", datetime(2026, 9, 2, 11, 10, 19)),
                                    ("exit-oid-1", datetime(2026, 9, 2, 11, 11, 5))]),
        o, s, broker_orders_reader=lambda *a: calls.append(a) or _readable_empty(*a))
    assert calls == [], "the skip really did spend no broker read"
    d2 = o.broker_recon_detail_json
    assert o.broker_recon_status in orc._TERMINAL_RECON_STATUSES
    assert "attribution_version" not in d2, "a skip attributes NOTHING — it may not stamp a version"
    assert orc.needs_reconcile(o, s) is True, "the row is still owed a broker read"
    assert d2["attribution_backoff_released"] == "ledger_settled_terminal"
    # …and the released horizon means the very next pass reads it.
    assert orc.broker_read_plan(o, s)["read"] is True


def test_a_skip_that_is_not_a_settle_still_carries_its_horizon_verbatim():
    """The release must be narrow: an ordinary skip may never push its own
    horizon out (that would make the backoff a treadmill that never expires)."""
    o, s = _pair(outcome_class="cancelled_pre_entry", realized=None)
    orc.reconcile_one_outcome(_FakeDb([]), o, s, broker_orders_reader=_readable_empty)
    horizon = o.broker_recon_detail_json["attribution_next_retry_utc"]
    for _ in range(3):
        orc.reconcile_one_outcome(_FakeDb([]), o, s, broker_orders_reader=_readable_empty)
        assert o.broker_recon_detail_json["attribution_next_retry_utc"] == horizon


# ═════════════════════════════════════════════════════════════════════════════
# (L2) THE BACKOFF LENGTH IS A LOSS-GUARD OUTAGE LENGTH
# ═════════════════════════════════════════════════════════════════════════════
def test_a_row_the_loss_guard_is_blocked_behind_gets_the_SHORT_horizon():
    """`risk_policy.load_current_live_loss_history` skips exactly one class of
    row: `not_entered`. Everything else gaps the day and disarms the account.

    So a 30-minute backoff on an ENTERED row is a 30-minute account-wide arming
    outage by construction — the 2026-09-02 landmine re-armed with a shorter
    fuse. The horizon must be keyed on whether the row can block the guard."""
    blocking, s_b = _pair(outcome_class="stop_loss", realized=-78.13)
    skipped, s_s = _pair(sid=19999, outcome_class="cancelled_pre_entry", realized=None)

    assert orc._loss_guard_can_block(blocking, s_b) is True
    assert orc._loss_guard_can_block(skipped, s_s) is False

    orc.reconcile_one_outcome(_FakeDb([]), blocking, s_b, broker_orders_reader=_readable_empty)
    orc.reconcile_one_outcome(_FakeDb([]), skipped, s_s, broker_orders_reader=_readable_empty)

    now = datetime.utcnow()
    b_at = datetime.fromisoformat(blocking.broker_recon_detail_json["attribution_next_retry_utc"])
    s_at = datetime.fromisoformat(skipped.broker_recon_detail_json["attribution_next_retry_utc"])
    assert (b_at - now).total_seconds() <= 130, "a blocked account may not wait 30 minutes"
    assert (s_at - now).total_seconds() > 600, "a row the guard ignores must not burn the budget"
    assert blocking.broker_recon_detail_json["attribution_retry_blocking"] is True
    assert skipped.broker_recon_detail_json["attribution_retry_blocking"] is False


def test_an_unrecognised_outcome_class_is_treated_as_blocking():
    """Fail-closed: `unknown` and `conflict` both gap the guard's day, so only a
    PROVABLE `not_entered` earns the long backoff."""
    o, s = _pair(outcome_class="some_future_class", realized=None)
    assert orc._loss_guard_can_block(o, s) is True
    # a never-entered class carrying economics is a `conflict` → blocking
    o2, s2 = _pair(outcome_class="cancelled_pre_entry", realized=-12.0)
    assert orc._loss_guard_can_block(o2, s2) is True


# ═════════════════════════════════════════════════════════════════════════════
# (L3) EVERY NON-CONVERGED VERDICT MUST ARM A HORIZON
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("reader,expect_status", [
    (_unreadable, orc.ATTR_UNREADABLE),
    (lambda *a: {"readable": True, "orders": [], "truncated": True}, orc.ATTR_TRUNCATED),
])
def test_unreadable_and_truncated_verdicts_arm_a_retry_horizon(reader, expect_status):
    """These stamp no `attribution_version`, so `needs_reconcile` re-admits them
    every 60 s. Under the first cut they therefore re-listed the broker EVERY
    pass forever — one credential rotation and every entered row in the lookback
    holds a budget slot permanently."""
    o, s = _pair()
    orc.reconcile_one_outcome(_FakeDb([]), o, s, broker_orders_reader=reader)
    d = o.broker_recon_detail_json
    assert d["broker_attribution"]["attr_status"] == expect_status
    assert "attribution_version" not in d, "an unreadable pass never certifies"
    assert d["attribution_next_retry_utc"] > datetime.utcnow().isoformat()
    plan = orc.broker_read_plan(o, s)
    assert plan["read"] is False and plan["reason"] == orc.ATTR_SKIPPED_BACKOFF


def test_a_never_closing_residual_open_row_does_not_re_read_every_pass():
    """`residual_open` means "the close is not visible yet", so the row is
    re-read until it is. Unbounded, that is a permanent budget slot: an OCO leg
    the walker cannot see, or a genuinely never-closed session, holds it for the
    whole lookback."""
    o, s = _pair()
    entry = SimpleNamespace(order_id="E1", client_order_id=f"chili_ml_e_{SID}_a", side="buy",
                            status="filled", filled_size=355.0, average_filled_price=4.34,
                            product_id="CANF", raw={"filled_at": "2026-09-02T11:10:19+00:00"})
    orc.reconcile_one_outcome(_FakeDb([]), o, s,
                              broker_orders_reader=lambda *a: {"readable": True, "orders": [entry],
                                                               "truncated": False})
    assert o.broker_recon_status == orc.STATUS_RESIDUAL_OPEN
    assert orc.broker_read_plan(o, s)["read"] is False, "bounded, not every-60s forever"
    # the horizon is the SHORT one: the guard is disarmed behind this row
    at = datetime.fromisoformat(o.broker_recon_detail_json["attribution_next_retry_utc"])
    assert (at - datetime.utcnow()).total_seconds() <= 130


def test_a_flat_verdict_clears_a_stale_horizon():
    o, s = _pair(le={"entry_order_id": "E1", "trade_cycles": 1},
                 detail={"attribution_next_retry_utc":
                         (datetime.utcnow() - timedelta(seconds=5)).isoformat(),
                         "attribution_attempts": 3})
    assert orc.broker_read_plan(o, s)["read"] is True  # the horizon has expired
    entry = SimpleNamespace(order_id="E1", client_order_id=f"chili_ml_e_{SID}_a", side="buy",
                            status="filled", filled_size=100.0, average_filled_price=4.0,
                            product_id="CANF", raw={"filled_at": "2026-09-02T11:10:19+00:00"})
    exit_ = SimpleNamespace(order_id="X1", client_order_id=f"chili_ml_s_{SID}_a", side="sell",
                            status="filled", filled_size=100.0, average_filled_price=3.5,
                            product_id="CANF", raw={"filled_at": "2026-09-02T11:20:19+00:00"})
    orc.reconcile_one_outcome(_FakeDb([]), o, s,
                              broker_orders_reader=lambda *a: {"readable": True,
                                                               "orders": [entry, exit_],
                                                               "truncated": False})
    assert o.broker_recon_status == orc.STATUS_RECONCILED
    assert "attribution_next_retry_utc" not in o.broker_recon_detail_json


# ═════════════════════════════════════════════════════════════════════════════
# (L4) THE BUDGET IS NEVER CHARGED FOR A READ THAT CANNOT HAPPEN
# ═════════════════════════════════════════════════════════════════════════════
def test_a_row_with_no_derivable_window_never_charges_the_budget():
    """`reconcile_one_outcome` bails with `unreadable` WITHOUT calling the reader
    when the window is null — but the batch loop had already charged a slot, and
    the resulting verdict stamps no version, so the leak repeated every pass."""
    o, s = _pair(started=None, ended=None, terminal_at=None)
    plan = orc.broker_read_plan(o, s)
    assert plan["read"] is False
    assert plan["reason"] == orc.ATTR_SKIPPED_NO_WINDOW
    calls = []
    orc.reconcile_one_outcome(_FakeDb([]), o, s, read_plan=plan,
                              broker_orders_reader=lambda *a: calls.append(a) or _readable_empty(*a))
    assert calls == []
    assert o.broker_recon_detail_json["attribution_read_skipped"] == orc.ATTR_SKIPPED_NO_WINDOW


# ═════════════════════════════════════════════════════════════════════════════
# (L5) ORDERING: THE STARVATION THE FIRST CUT LEFT OPEN
# ═════════════════════════════════════════════════════════════════════════════
def _batch_db(rows):
    class _Q:
        def join(self, *a, **k): return self
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def all(self): return list(rows)

    class _Db:
        def query(self, *a, **k): return _Q()
        def commit(self): pass
        def rollback(self): pass

    return _Db()


def test_a_blind_row_outranks_an_unbounded_backfill_queue(monkeypatch):
    """THE MEASURED STARVATION. On a cold deploy every already-`reconciled` row
    in the lookback lacks an `attribution_version`. Ranking by the stamp put all
    of them in the TOP class; a newly terminal FILLED row that lost its first
    read to a transient Alpaca error is `unreconciled_broker_unavailable` and sat
    BELOW every one of them — with no way back up, because the backfill queue is
    refilled by every pass that cannot drain it.

    Budget 5 against 40 backfill rows: the blind row must still be served."""
    monkeypatch.setattr(orc.settings, "chili_momentum_broker_truth_reconciliation_enabled", True, raising=False)
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 5, raising=False)
    rows = []
    for i in range(40):
        o, s = _pair(sid=18000 + i, recon_status="reconciled", detail={"status": "reconciled"},
                     terminal_at=datetime(2026, 9, 2, 12, 0, 0) + timedelta(seconds=i))
        # already loss-guard usable — the guard has its number, only the stamp is old
        o.broker_reconciled_at = datetime(2026, 9, 2, 12, 30, 0)
        o.broker_realized_pnl_usd = -5.0
        o.broker_notional_basis_usd = 100.0
        rows.append((o, s))
    blind, blind_s = _pair(sid=19471, recon_status=orc.STATUS_BROKER_UNAVAILABLE, detail={},
                           terminal_at=datetime(2026, 9, 2, 11, 0, 0))  # OLDEST of them all
    rows.insert(20, (blind, blind_s))

    served = []

    def _one(db, outcome, sess, read_plan=None, **kw):
        served.append(outcome.session_id)
        outcome.broker_recon_status = outcome.broker_recon_status or orc.STATUS_NO_FILLS
        return {"status": str(outcome.broker_recon_status)}

    monkeypatch.setattr(orc, "reconcile_one_outcome", _one)
    out = orc.reconcile_momentum_outcomes_to_broker_truth(_batch_db(rows), lookback_days=2.0,
                                                          day_net_advisory=False)
    assert out["ok"] is True
    assert served[0] == 19471, "the row the loss guard is blind on is served FIRST"
    assert out["loss_guard_blind_starved"] == 0


def test_the_starvation_gauge_counts_only_loss_guard_blind_rows(monkeypatch):
    """`skipped_broker_budget` alone is benign — the backfill class queues there
    by design. The alertable number is how many BLIND rows lost their read."""
    monkeypatch.setattr(orc.settings, "chili_momentum_broker_truth_reconciliation_enabled", True, raising=False)
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 1, raising=False)
    rows = [_pair(sid=19471, terminal_at=datetime(2026, 9, 2, 13, 0, 0)),
            _pair(sid=19472, terminal_at=datetime(2026, 9, 2, 12, 0, 0))]

    def _one(db, outcome, sess, read_plan=None, **kw):
        outcome.broker_recon_status = orc.STATUS_NO_FILLS
        return {"status": orc.STATUS_NO_FILLS}

    monkeypatch.setattr(orc, "reconcile_one_outcome", _one)
    out = orc.reconcile_momentum_outcomes_to_broker_truth(_batch_db(rows), lookback_days=2.0,
                                                          day_net_advisory=False)
    assert out["skipped_broker_budget"] == 1
    assert out["loss_guard_blind_starved"] == 1


def test_the_sort_key_is_total_and_cannot_silently_degrade(monkeypatch):
    """The prioritisation used to sit inside `try/except: pass`, so one bad row
    reverted the WHOLE pass to SQL order with no log line — the fix disappearing
    exactly when the data is weird. The key must be total instead."""
    monkeypatch.setattr(orc.settings, "chili_momentum_broker_truth_reconciliation_enabled", True, raising=False)
    rows = [
        _pair(sid=1, terminal_at=None),                                # no clock
        _pair(sid=2, terminal_at=datetime(2026, 9, 2, 13, 0, 0)),
        _pair(sid=3, terminal_at=datetime(1969, 6, 1, 0, 0, 0)),       # pre-epoch
    ]

    def _one(db, outcome, sess, read_plan=None, **kw):
        outcome.broker_recon_status = orc.STATUS_NO_FILLS
        return {"status": orc.STATUS_NO_FILLS}

    monkeypatch.setattr(orc, "reconcile_one_outcome", _one)
    out = orc.reconcile_momentum_outcomes_to_broker_truth(_batch_db(rows), lookback_days=2.0,
                                                          day_net_advisory=False)
    assert out["ok"] is True and out["checked"] == 3
