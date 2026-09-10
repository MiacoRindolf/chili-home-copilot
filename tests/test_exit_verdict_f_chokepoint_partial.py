"""EXIT VERDICT F -- the f sell beside the resting R stop, and the deadman head guard
sizing the re-arm to R (2026-09-10, [21]/[44]/[47]). Real claim tables.

WHAT THE CODE PROVED (deviation from the spec's §6.4 chokepoint bypass, stated in the PR):
`_release_deadman_at_literal_submit` is a WHOLE-close protocol (phase 2 resizes the close
to the whole broker remainder) -- and even past a bypass of it, the owner-transport OUTBOX
is single-slot: the resting deadman IS the active transport, so the ordinary-exit lease
for f is refused (`alpaca_owner_transport_kind_mismatch`; the first version of this test
hit exactly that). The shipped precedent for a partial sell resting BESIDE the deadman is
the OCO tranche (`_place_scale_out_limit`): a SIBLING order POSTed straight to the adapter
with its cid written before the POST, its fill adopted on a later tick, and cancelled by
every whole exit before the handoff. That is what the f sell now is.

FACT 2 (unchanged): the resting deadman reserves `qty_available`, so the stop must cover
exactly R = Q - f before f can rest. The shrink is cancel + re-arm through the existing
terminal -> re-arm recursion with the head guard subtracting `pending_qty` (I2) -- never
`replace_order_qty` (tests/test_partial_exit_path_b_unwired.py stays green).

Runnable: pytest tests/test_exit_verdict_f_chokepoint_partial.py -v
"""
from __future__ import annotations

import uuid
from collections import deque
from datetime import datetime
from typing import Any

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import optional_db_read as ODR
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    advance_owner_transport,
    lease_owner_transport,
    read_action_claim,
)
from app.services.trading.venue.protocol import NormalizedTicker

from tests.test_alpaca_deadman_close_handoff import (  # noqa: F401  (fixture import)
    _DeadmanLifecycleAdapter,
    _deadman_settings,
    _ensure_initial_deadman,
    _install_truth_order,
    _request,
    _seed_owner,
    _write_live_state,
)
from tests.test_momentum_emergency_exit_recovery import (
    TEST_ALPACA_ACCOUNT_ID,
    _ScriptedAlpaca,
    _ensure_retained_entry_owner,
    _fresh,
    _order,
    _seed_session,
)

NOW = datetime(2026, 9, 10, 14, 0, 41)


@pytest.fixture(autouse=True)
def _boundaries(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_alpaca_expected_account_id", TEST_ALPACA_ACCOUNT_ID, raising=False)
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda _family: True)
    monkeypatch.setattr(lr, "_record_live_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_exit_intent_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda sid: True)
    monkeypatch.setattr(lr, "_utcnow", lambda: NOW)


def _emit_log(monkeypatch) -> list[tuple[str, dict]]:
    log: list[tuple[str, dict]] = []
    monkeypatch.setattr(lr, "_emit", lambda db, sess, ev, payload: log.append((ev, dict(payload))))
    return log


def _session_hours(monkeypatch, session: str) -> None:
    monkeypatch.setattr("app.services.trading.momentum_neural.market_profile.market_session_now",
                        lambda _symbol, **_k: session)


def _resting_deadman_session(db, *, symbol: str, deadman_qty: float, marker: dict | None,
                             positions: list[float] | None = None):
    """A held Alpaca session (Q = 10) with ONE resting deadman generation of `deadman_qty`
    leased on the real owner claim (the outbox's active transport), and the verdict marker."""
    sess = _seed_session(db, symbol=symbol, quantity=10.0, avg_entry_price=10.0)
    le: dict[str, Any] = {
        "side_long": True,
        "entry_filled_at_utc": "2026-09-10T14:00:00",
        "position": {"product_id": symbol, "side": "long", "quantity": 10.0,
                     "avg_entry_price": 10.0, "stop_price": 9.0, "original_quantity": 10.0},
    }
    if marker is not None:
        le["exit_verdict"] = marker
    snapshot = dict(sess.risk_snapshot_json or {})
    snapshot["momentum_live_execution"] = le
    sess.risk_snapshot_json = snapshot
    db.add(sess)
    db.commit()
    owner_context = _ensure_retained_entry_owner(db, sess)
    deadman_cid = f"chili_dm_{sess.id}_1_{uuid.uuid4().hex[:6]}"
    deadman_oid = f"deadman-{uuid.uuid4().hex[:6]}"
    request = _request(symbol=symbol, cid=deadman_cid, qty=deadman_qty, kind="deadman")
    request["base_size"] = lr._fmt_base_size(deadman_qty)
    lease_token = f"vt-deadman-{uuid.uuid4().hex}"
    leased = lease_owner_transport(db, **owner_context, transport_kind="deadman",
                                   client_order_id=deadman_cid, order_request=request, lease_token=lease_token)
    assert leased["ok"] is True, leased
    assert advance_owner_transport(db, **owner_context, client_order_id=deadman_cid,
                                   lease_token=lease_token, phase="submitted", broker_order_id=deadman_oid)
    readable, claim = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and claim is not None
    transport = dict(claim["metadata"]["owner_transport"])
    le["deadman_stop"] = {"order_id": deadman_oid, "client_order_id": deadman_cid, "stop_price": 7.5,
                          "qty": deadman_qty, "phase": "submitted", "owner_transport": transport}
    snapshot = dict(sess.risk_snapshot_json or {})
    snapshot["momentum_live_execution"] = le
    sess.risk_snapshot_json = snapshot
    db.add(sess)
    db.commit()
    adapter = _ScriptedAlpaca(positions=positions or [10.0])
    active = _order(oid=deadman_oid, cid=deadman_cid, symbol=symbol, side="sell", status="open",
                    filled=0.0, avg=None, qty=deadman_qty, order_type="stop", time_in_force="gtc",
                    position_intent="sell_to_close", raw_overrides={"stop_price": 7.5})
    terminal = _order(oid=deadman_oid, cid=deadman_cid, symbol=symbol, side="sell", status="cancelled",
                      filled=0.0, avg=None, qty=deadman_qty, order_type="stop", time_in_force="gtc",
                      position_intent="sell_to_close", raw_overrides={"stop_price": 7.5})
    adapter.truth[deadman_cid] = deque([active, terminal])
    adapter.cid_orders[deadman_cid] = terminal
    adapter.orders[deadman_oid] = terminal
    meta = _fresh()
    adapter.execution_bbo = (
        NormalizedTicker(product_id=symbol, bid=9.95, ask=9.97, mid=9.96, freshness=meta,
                         raw={"feed": "iqfeed_l1", "tape_row_id": 9901}),
        meta,
    )
    return sess, le, adapter, deadman_oid, deadman_cid


def _marker(f: float = 4.0, R: float = 6.0, phase: str = "partial_sell_pending", sell: dict | None = None) -> dict:
    partial = {"pending_qty": f, "f": f, "R": R, "Q": 10.0, "fraction": 0.4,
               "decision_as_of": "2026-09-10T14:00:37", "decision_bid": 9.95,
               "attempts": {"shrink": 1, "sell": 0}, "shrink": {"path": "cancel_rearm"}}
    if sell is not None:
        partial["sell"] = sell
        partial["attempts"]["sell"] = int(sell.get("attempt") or 1)
    return {"phase": phase, "partial": partial}


def _place(db, sess, le, adapter, *, symbol):
    return lr._exit_verdict_place_partial_sell(
        db, sess, adapter, le, as_of=NOW, bid=9.95, ask=9.97, mid=9.96,
        product_id=symbol, prod=None, verdict={"fired": True, "binding": "all_three"},
    )


# ── the f sibling: POSTed beside the R stop, no handoff, no outbox lease ────────

def test_the_f_sibling_posts_beside_the_r_stop_without_the_handoff_or_an_outbox_lease(db, monkeypatch):
    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    sess, le, adapter, oid, cid = _resting_deadman_session(db, symbol="EVTA", deadman_qty=6.0, marker=_marker())
    out = _place(db, sess, le, adapter, symbol="EVTA")
    assert out["ok"] is True and out["attempt"] == 1, out
    assert adapter.cancel_calls == []                        # the R stop is NOT touched
    assert adapter.market_calls == []
    assert len(adapter.limit_calls) == 1                      # rung 1: marketable limit
    kw = adapter.limit_calls[0]
    assert kw["base_size"] == "4" and kw["side"] == "sell" and kw["position_intent"] == "sell_to_close"
    assert kw["time_in_force"] == "day" and kw["extended_hours"] is False
    assert kw["client_order_id"].startswith(f"chili_ml_tv_{sess.id}_")
    assert float(kw["limit_price"]) <= 9.95
    sell = le["exit_verdict"]["partial"]["sell"]
    assert sell["order_id"] == out["order_id"] and sell["phase"] == "submitted" and sell["qty"] == 4.0
    assert sell["client_order_id"] == kw["client_order_id"]
    assert le["deadman_stop"]["order_id"] == oid and float(le["deadman_stop"]["qty"]) == 6.0
    assert "deadman_released_for_close" not in le            # no whole-close handoff
    assert lr._exit_verdict_pending_partial_qty(le) == 4.0    # cleared only by the fill
    # the outbox was never leased for the f sell: the deadman is still the active transport
    readable, claim = read_action_claim(db, symbol="EVTA", account_scope="alpaca:paper")
    assert readable and claim["metadata"]["owner_transport"]["client_order_id"] == cid
    assert claim["metadata"]["owner_transport"]["phase"] == "submitted"
    submitted = [p for e, p in log if e == "live_exit_verdict_partial_submitted"]
    assert len(submitted) == 1 and submitted[0]["qty"] == 4.0 and submitted[0]["R"] == 6.0
    assert submitted[0]["order_type"] == "limit" and submitted[0]["attempt"] == 1


@pytest.mark.parametrize("session", ["premarket", "afterhours"])
def test_an_extended_hours_f_sibling_is_a_day_limit_that_crosses_hard(db, monkeypatch, session):
    _emit_log(monkeypatch)
    _session_hours(monkeypatch, session)
    sess, le, adapter, _oid, _cid = _resting_deadman_session(
        db, symbol=f"EVT{session[:2].upper()}", deadman_qty=6.0, marker=_marker(),
    )
    out = _place(db, sess, le, adapter, symbol=sess.symbol)
    assert out["ok"] is True, out
    assert len(adapter.limit_calls) == 1 and adapter.market_calls == [] and adapter.cancel_calls == []
    kw = adapter.limit_calls[0]
    assert kw["base_size"] == "4" and kw["extended_hours"] is True and kw["time_in_force"] == "day"
    g = lr._notional_guard_multiplier() - 1.0
    assert float(kw["limit_price"]) == pytest.approx(float(lr._fmt_limit_price_sell(9.95 * (1.0 - 8.0 * g))))


def test_rung_three_is_a_market_order_in_rth_and_a_limit_out_of_hours():
    assert lr._exit_verdict_sell_rung(attempt=3, bid=9.95, mid=9.96, extended=False) == ("market", None)
    t, px = lr._exit_verdict_sell_rung(attempt=3, bid=9.95, mid=9.96, extended=True)
    assert t == "limit" and px < 9.95
    t1, px1 = lr._exit_verdict_sell_rung(attempt=1, bid=9.95, mid=9.96, extended=False)
    t2, px2 = lr._exit_verdict_sell_rung(attempt=2, bid=9.95, mid=9.96, extended=False)
    assert t1 == t2 == "limit" and px2 < px1 < 9.95


def test_the_cid_is_durable_before_the_post_and_an_indeterminate_submit_keeps_it(db, monkeypatch):
    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    sess, le, adapter, _oid, _cid = _resting_deadman_session(db, symbol="EVTI", deadman_qty=6.0, marker=_marker())
    seen: dict[str, Any] = {}

    def _flaky(**kwargs):
        seen["at_post"] = dict(le["exit_verdict"]["partial"]["sell"])
        return {"ok": False, "error": "ReadTimeout", "submit_outcome": "indeterminate"}

    monkeypatch.setattr(adapter, "place_limit_order_gtc", _flaky)
    out = _place(db, sess, le, adapter, symbol="EVTI")
    assert out["ok"] is False and out["error"] == "submit_indeterminate"
    assert seen["at_post"]["client_order_id"].startswith("chili_ml_tv_")   # written BEFORE the POST
    sell = le["exit_verdict"]["partial"]["sell"]
    assert sell["phase"] == "indeterminate" and sell["order_id"] is None
    assert [p["why"] for e, p in log if e == "live_exit_verdict_partial_failed"] == ["submit_indeterminate"]
    # the sell service resolves it by exact CID truth: found => adopt the order id
    found = _order(oid="tv-oid-1", cid=sell["client_order_id"], symbol="EVTI", side="sell", status="open",
                   filled=0.0, avg=None, qty=4.0, order_type="limit")
    adapter.truth[sell["client_order_id"]] = found
    adapter.orders["tv-oid-1"] = found
    svc = lr._exit_verdict_service_partial_sell(db, sess, adapter, le, as_of=NOW, bid=9.95, avg=10.0, product_id="EVTI")
    assert svc == {"pending": True, "age_s": 0.0, "patience_s": 5.0}
    assert le["exit_verdict"]["partial"]["sell"]["order_id"] == "tv-oid-1"


# ── the sell service: the fill starts the runner; a zero fill walks the ladder ──

def _sibling(oid="tv-oid-1", cid="chili_ml_tv_x_abc", attempt=1, submitted_at="2026-09-10T14:00:40"):
    return {"client_order_id": cid, "order_id": oid, "qty": 4.0, "order_type": "limit", "limit_price": 9.92,
            "attempt": attempt, "extended_hours": False, "submitted_at_utc": submitted_at, "phase": "submitted"}


def test_the_sell_service_adopts_the_fill_and_starts_the_runner(db, monkeypatch):
    log = _emit_log(monkeypatch)
    sess, le, adapter, oid, _cid = _resting_deadman_session(db, symbol="EVTF", deadman_qty=6.0,
                                                            marker=_marker(sell=_sibling()))
    adapter.orders["tv-oid-1"] = _order(oid="tv-oid-1", cid="chili_ml_tv_x_abc", symbol="EVTF", side="sell",
                                        status="filled", filled=4.0, avg=9.94, qty=4.0, order_type="limit")
    svc = lr._exit_verdict_service_partial_sell(db, sess, adapter, le, as_of=NOW, bid=9.95, avg=10.0, product_id="EVTF")
    assert svc["filled"] is True and svc["filled_size"] == 4.0
    assert svc["pnl_usd"] == pytest.approx((9.94 - 10.0) * 4.0)
    ev = le["exit_verdict"]
    assert ev["phase"] == "runner" and ev["partial"]["pending_qty"] == 0.0
    assert "sell" not in ev["partial"] and ev["partial"]["sell_done"]["filled_size"] == 4.0
    assert le["position"]["quantity"] == 6.0 and le["position"]["partial_taken"] is True
    assert le["position"]["stop_price"] == 9.0                 # no breakeven move
    assert sess.state == "live_entered"                        # no state transition
    assert float(le["deadman_stop"]["qty"]) == 6.0             # the R stop is the runner's floor
    names = [e for e, _ in log]
    assert names.index("live_partial_exit_filled") < names.index("live_exit_verdict_runner_started")
    started = [p for e, p in log if e == "live_exit_verdict_runner_started"][0]
    assert started["runner_qty"] == 6.0 and started["trail_authority"] == "tick_deadman"
    assert started["frontier_rewound_to"] == "2026-09-10T14:00:37"


def test_a_terminal_partial_fill_starts_the_runner_and_re_covers_the_rest(db, monkeypatch):
    _emit_log(monkeypatch)
    sess, le, adapter, _oid, _cid = _resting_deadman_session(db, symbol="EVTP", deadman_qty=6.0,
                                                             marker=_marker(sell=_sibling()))
    adapter.orders["tv-oid-1"] = _order(oid="tv-oid-1", cid="chili_ml_tv_x_abc", symbol="EVTP", side="sell",
                                        status="cancelled", filled=1.0, avg=9.93, qty=4.0, order_type="limit")
    svc = lr._exit_verdict_service_partial_sell(db, sess, adapter, le, as_of=NOW, bid=9.95, avg=10.0, product_id="EVTP")
    assert svc["filled"] is True and svc["filled_size"] == 1.0
    ev = le["exit_verdict"]
    assert ev["phase"] == "runner" and le["position"]["quantity"] == 9.0
    # the R = 6 stop now under-covers the 9 held: the cover pulse re-arms Q - k
    assert ev["recover"]["want_qty"] == 9.0 and ev["recover"]["reason"] == "sibling_terminal_partial_fill"


def test_a_zero_fill_terminal_sibling_retires_and_the_next_rung_is_placed(db, monkeypatch):
    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    sess, le, adapter, _oid, _cid = _resting_deadman_session(db, symbol="EVTZ", deadman_qty=6.0,
                                                             marker=_marker(sell=_sibling()))
    adapter.orders["tv-oid-1"] = _order(oid="tv-oid-1", cid="chili_ml_tv_x_abc", symbol="EVTZ", side="sell",
                                        status="cancelled", filled=0.0, avg=None, qty=4.0, order_type="limit")
    svc = lr._exit_verdict_service_partial_sell(db, sess, adapter, le, as_of=NOW, bid=9.95, avg=10.0, product_id="EVTZ")
    assert svc == {"failed": True, "why": "terminal_no_fill"}
    assert "sell" not in le["exit_verdict"]["partial"] and le["exit_verdict"]["phase"] == "partial_sell_pending"
    assert [p["fallback"] for e, p in log if e == "live_exit_verdict_partial_failed"] == ["retry_next_rung"]
    # the elif asks for the next rung: attempt 2 = 4x guard, still a limit
    out = _place(db, sess, le, adapter, symbol="EVTZ")
    assert out["ok"] is True and out["attempt"] == 2
    g = lr._notional_guard_multiplier() - 1.0
    assert float(adapter.limit_calls[-1]["limit_price"]) == pytest.approx(
        float(lr._fmt_limit_price_sell(9.95 * (1.0 - 4.0 * g))))


def test_past_its_patience_an_open_sibling_is_cancelled_and_a_race_fill_is_adopted(db, monkeypatch):
    _emit_log(monkeypatch)
    sess, le, adapter, _oid, _cid = _resting_deadman_session(
        db, symbol="EVTR", deadman_qty=6.0,
        marker=_marker(sell=_sibling(submitted_at="2026-09-10T14:00:30")),   # 11 s ago > 5 s patience
    )
    open_order = _order(oid="tv-oid-1", cid="chili_ml_tv_x_abc", symbol="EVTR", side="sell",
                        status="open", filled=0.0, avg=None, qty=4.0, order_type="limit")
    raced = _order(oid="tv-oid-1", cid="chili_ml_tv_x_abc", symbol="EVTR", side="sell",
                   status="cancelled", filled=4.0, avg=9.91, qty=4.0, order_type="limit")
    adapter.orders["tv-oid-1"] = deque([open_order, raced])
    svc = lr._exit_verdict_service_partial_sell(db, sess, adapter, le, as_of=NOW, bid=9.95, avg=10.0, product_id="EVTR")
    assert adapter.cancel_calls == ["tv-oid-1"]
    assert svc["filled"] is True and svc["cancel_race"] is True
    assert le["exit_verdict"]["phase"] == "runner" and le["position"]["quantity"] == 6.0


def test_the_sell_cap_forces_the_whole_position(db, monkeypatch):
    log = _emit_log(monkeypatch)
    sess, le, adapter, _oid, _cid = _resting_deadman_session(
        db, symbol="EVTC", deadman_qty=6.0,
        marker=_marker(sell=_sibling(attempt=lr._EXIT_SUBMIT_MAX_ATTEMPTS)),
    )
    adapter.orders["tv-oid-1"] = _order(oid="tv-oid-1", cid="chili_ml_tv_x_abc", symbol="EVTC", side="sell",
                                        status="expired", filled=0.0, avg=None, qty=4.0, order_type="limit")
    svc = lr._exit_verdict_service_partial_sell(db, sess, adapter, le, as_of=NOW, bid=9.95, avg=10.0, product_id="EVTC")
    assert svc == {"failed": True, "forced_whole": True}
    ev = le["exit_verdict"]
    assert ev["phase"] == "armed" and ev["force_whole"] == "whole_partial_failed" and ev["partial"]["pending_qty"] == 0.0
    assert [p["fallback"] for e, p in log if e == "live_exit_verdict_partial_failed"] == ["whole_partial_failed"]


# ── the chokepoint head: any whole exit releases the sibling FIRST (I1) ─────────

def test_a_whole_exit_cancels_the_resting_sibling_at_the_head_then_hands_off_the_whole_position(db, monkeypatch):
    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    sess, le, adapter, oid, _cid = _resting_deadman_session(db, symbol="EVTW", deadman_qty=6.0,
                                                            marker=_marker(sell=_sibling()))
    adapter.orders["tv-oid-1"] = _order(oid="tv-oid-1", cid="chili_ml_tv_x_abc", symbol="EVTW", side="sell",
                                        status="cancelled", filled=0.0, avg=None, qty=4.0, order_type="limit")
    out = lr._submit_live_market_exit(
        db, sess, adapter, le=le, product_id="EVTW", quantity=10.0,
        client_order_id=f"chili_ml_b_{sess.id}_{uuid.uuid4().hex[:12]}", reason="bailout",
        bid=9.95, ask=9.97, mid=9.96,
    )
    # the sibling went first
    assert adapter.cancel_calls[0] == "tv-oid-1"
    assert "sell" not in le["exit_verdict"]["partial"]
    assert lr._exit_verdict_pending_partial_qty(le) == 0.0
    assert le["exit_verdict"]["partial"]["abandoned_by"] == "bailout"
    names = [e for e, _ in log]
    assert names.index("live_exit_verdict_sibling_released") < names.index("live_exit_verdict_partial_failed")
    failed = [p for e, p in log if e == "live_exit_verdict_partial_failed"][0]
    assert failed["fallback"] == "whole_by_bailout" and failed["pending_qty_was"] == 4.0
    # ...then the WHOLE handoff for Q (I1): phase 1 freeze of a 10-share successor
    assert out.get("error") == "deadman_successor_intent_frozen_for_next_pulse", out
    assert le["deadman_released_for_close"]["successor_intent_pre_cancel"]["base_size"] == "10"
    assert adapter.limit_calls == [] and adapter.market_calls == []


def test_a_whole_exit_adopts_a_sibling_fill_caught_in_the_cancel_race_and_closes_the_rest(db, monkeypatch):
    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    sess, le, adapter, _oid, _cid = _resting_deadman_session(db, symbol="EVTX", deadman_qty=6.0,
                                                             marker=_marker(sell=_sibling()), positions=[6.0])
    adapter.orders["tv-oid-1"] = _order(oid="tv-oid-1", cid="chili_ml_tv_x_abc", symbol="EVTX", side="sell",
                                        status="cancelled", filled=4.0, avg=9.93, qty=4.0, order_type="limit")
    out = lr._submit_live_market_exit(
        db, sess, adapter, le=le, product_id="EVTX", quantity=10.0,
        client_order_id=f"chili_ml_b_{sess.id}_{uuid.uuid4().hex[:12]}", reason="bailout",
        bid=9.95, ask=9.97, mid=9.96,
    )
    assert adapter.cancel_calls[0] == "tv-oid-1"
    assert le["position"]["quantity"] == 6.0                   # the 4 booked as a partial
    assert le["exit_verdict"]["phase"] == "partial_sell_pending"   # NO runner: the whole exit owns the leg
    assert [e for e, _ in log if e == "live_exit_verdict_runner_started"] == []
    assert [p["reason"] for e, p in log if e == "live_partial_exit_filled"] == ["tape_sellers_took_it"]
    assert out.get("error") == "deadman_successor_intent_frozen_for_next_pulse", out
    assert le["deadman_released_for_close"]["successor_intent_pre_cancel"]["base_size"] == "6"   # Q - k


def test_a_sibling_whose_cancel_is_not_terminal_blocks_the_whole_exit_before_any_post(db, monkeypatch):
    _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    sess, le, adapter, _oid, _cid = _resting_deadman_session(db, symbol="EVTN", deadman_qty=6.0,
                                                             marker=_marker(sell=_sibling()))
    adapter.orders["tv-oid-1"] = _order(oid="tv-oid-1", cid="chili_ml_tv_x_abc", symbol="EVTN", side="sell",
                                        status="pending_cancel", filled=0.0, avg=None, qty=4.0, order_type="limit")
    out = lr._submit_live_market_exit(
        db, sess, adapter, le=le, product_id="EVTN", quantity=10.0,
        client_order_id=f"chili_ml_b_{sess.id}_{uuid.uuid4().hex[:12]}", reason="bailout",
        bid=9.95, ask=9.97, mid=9.96,
    )
    assert out == {"ok": False, "error": "verdict_sibling_cancel_not_terminal", "deferred": True,
                   "pre_place_blocked": True}
    assert le["exit_verdict"]["partial"]["sell"]["order_id"] == "tv-oid-1"   # still tracked
    assert lr._exit_verdict_pending_partial_qty(le) == 4.0
    assert "deadman_released_for_close" not in le


def test_the_verdicts_own_whole_exit_does_not_release_anything(db, monkeypatch):
    """`tape_sellers_took_it` with pending_qty 0 (cannot split / partial failed) is a plain
    whole close through the normal handoff -- the head hook is reason-gated."""
    _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    marker = {"phase": "armed", "partial": {"pending_qty": 0.0}, "whole": {"kind": "whole_cannot_split"}}
    sess, le, adapter, oid, _cid = _resting_deadman_session(db, symbol="EVTY", deadman_qty=10.0, marker=marker)
    out = lr._submit_live_market_exit(
        db, sess, adapter, le=le, product_id="EVTY", quantity=10.0,
        client_order_id=f"chili_ml_tv_{sess.id}_{uuid.uuid4().hex[:12]}", reason="tape_sellers_took_it",
        bid=9.95, ask=9.97, mid=9.96,
    )
    assert out.get("error") == "deadman_successor_intent_frozen_for_next_pulse", out
    assert le["exit_verdict"]["partial"].get("abandoned_by") is None


# ── I2: the head guard sizes the re-arm to Q - f through the terminal recursion ──

def test_the_terminal_canceled_predecessor_re_arms_for_r_through_the_head_guard(db, _deadman_settings):
    """The shrink pulse cancelled the Q generation; the :43928 maintenance call finds it
    terminal (zero fill), books it, and recurses (:12190) with `quantity = remaining = Q`;
    the head guard subtracts `pending_qty = f` exactly ONCE => the new generation is R."""
    symbol = f"I2{uuid.uuid4().hex[:4].upper()}"
    sess, context = _seed_owner(db, symbol=symbol, quantity=10.0)
    cid = f"chili_dm_{sess.id}_1_focused"
    oid = f"oid-{uuid.uuid4().hex[:8]}"
    request = _request(symbol=symbol, cid=cid, qty=10.0, kind="deadman")
    lease_token = f"lease-{uuid.uuid4().hex}"
    leased = lease_owner_transport(db, **context, transport_kind="deadman", client_order_id=cid,
                                   order_request=request, lease_token=lease_token)
    assert leased["ok"] is True
    assert advance_owner_transport(db, **context, client_order_id=cid, lease_token=lease_token,
                                   phase="submitted", broker_order_id=oid)
    db.commit()
    readable, claim = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and claim is not None
    transport = dict(claim["metadata"]["owner_transport"])
    le = dict(sess.risk_snapshot_json["momentum_live_execution"])
    le["deadman_stop"] = {"order_id": oid, "client_order_id": cid, "stop_price": 7.5, "qty": 10.0,
                          "phase": "submitted", "owner_transport": transport}
    le["exit_verdict"] = _marker(phase="partial_shrink_pending")
    _write_live_state(db, sess, le)
    adapter = _DeadmanLifecycleAdapter(lifecycle="accepted", broker_position=10.0)
    _install_truth_order(adapter, transport=transport, order_id=oid, status="canceled",
                         lifecycle="canceled", filled=0.0, average_fill=None)

    result = _ensure_initial_deadman(db, sess, adapter)      # quantity = Q = 10 (position truth)

    assert result["ok"] is True and result["protected"] is True, result
    assert len(adapter.place_calls) == 1                     # ONE new generation
    assert float(adapter.place_calls[0]["base_size"]) == 6.0  # = Q - f, subtracted ONCE
    le2 = sess.risk_snapshot_json["momentum_live_execution"]
    assert float(le2["deadman_stop"]["qty"]) == 6.0
    assert le2["position"]["quantity"] == pytest.approx(10.0)  # the position is still Q
    assert le2["deadman_stop_history"][-1]["terminal_status"] == "canceled"
    assert le2["exit_verdict"]["partial"]["pending_qty"] == 4.0   # cleared only by the fill


def test_the_head_guard_refuses_impossible_arithmetic_with_a_full_close(db, _deadman_settings):
    symbol = f"I3{uuid.uuid4().hex[:4].upper()}"
    sess, _context = _seed_owner(db, symbol=symbol, quantity=10.0)
    le = dict(sess.risk_snapshot_json["momentum_live_execution"])
    le["exit_verdict"] = _marker(f=10.0, R=0.0, phase="partial_shrink_pending")
    _write_live_state(db, sess, le)
    adapter = _DeadmanLifecycleAdapter(lifecycle="accepted", broker_position=10.0)
    result = _ensure_initial_deadman(db, sess, adapter)
    assert result["ok"] is False and result["unprotected"] is True
    assert result["error"] == "verdict_partial_split_arithmetic_invalid"
    assert adapter.place_calls == []


def test_the_first_placement_is_also_sized_by_the_marker(db, _deadman_settings):
    """Every placement path crosses the head guard, not only the re-arm."""
    symbol = f"I4{uuid.uuid4().hex[:4].upper()}"
    sess, _context = _seed_owner(db, symbol=symbol, quantity=10.0)
    le = dict(sess.risk_snapshot_json["momentum_live_execution"])
    le["exit_verdict"] = _marker(phase="partial_shrink_pending")
    _write_live_state(db, sess, le)
    adapter = _DeadmanLifecycleAdapter(lifecycle="accepted", broker_position=10.0)
    result = _ensure_initial_deadman(db, sess, adapter)
    assert result["protected"] is True
    assert float(adapter.place_calls[0]["base_size"]) == 6.0


# ── the bounded read: SET LOCAL inside a savepoint that is rolled back ──────────

class _Nested:
    def __init__(self, log):
        self.log = log

    def rollback(self):
        self.log.append("rollback")


class _BoundedFakeDb:
    def __init__(self, rows, *, raise_on_statement: Exception | None = None):
        self.rows = rows
        self.log: list[str] = []
        self.raise_on_statement = raise_on_statement

    def begin_nested(self):
        self.log.append("begin_nested")
        return _Nested(self.log)

    def execute(self, stmt, params=None):
        text = str(stmt)
        self.log.append(f"execute:{text}")
        if "statement_timeout" not in text and self.raise_on_statement is not None:
            raise self.raise_on_statement
        rows = self.rows

        class _R:
            def fetchall(self_inner):
                return rows

        return _R()


def test_bounded_fetchall_sets_the_timeout_inside_a_savepoint_and_rolls_it_back():
    from sqlalchemy import text

    db = _BoundedFakeDb([(1,), (2,)])
    rows = ODR.bounded_fetchall(db, text("SELECT 1"), {}, timeout_ms=2000)
    assert rows == [(1,), (2,)]
    assert db.log == ["begin_nested", "execute:SET LOCAL statement_timeout = 2000", "execute:SELECT 1", "rollback"]


def test_bounded_fetchall_rolls_back_on_a_timeout_and_the_reader_names_it():
    from sqlalchemy import text

    class _QueryCanceled(Exception):
        pass

    db = _BoundedFakeDb([], raise_on_statement=_QueryCanceled("canceling statement due to statement timeout"))
    with pytest.raises(_QueryCanceled):
        ODR.bounded_fetchall(db, text("SELECT 1"), {}, timeout_ms=2000)
    assert db.log[-1] == "rollback"
    # the reader maps it to `unreadable{timeout}` (fail-open: no verdict, no walk)
    from app.services.trading.momentum_neural import entry_gates as EG

    err: dict = {}
    out = EG.leg_prints_since_high("SKYQ", db=db, hi_at=datetime(2026, 9, 10, 14, 0), hi_id=1,
                                   as_of=datetime(2026, 9, 10, 14, 1), err=err)
    assert out is None and err["why"] == "timeout"


def test_bounded_fetchall_falls_back_to_a_direct_execute_on_a_fake_without_begin_nested():
    from sqlalchemy import text

    class _Plain:
        def __init__(self):
            self.stmts = []

        def execute(self, stmt, params=None):
            self.stmts.append(str(stmt))
            rows = [(7,)]

            class _R:
                def fetchall(self_inner):
                    return rows

            return _R()

    db = _Plain()
    assert ODR.bounded_fetchall(db, text("SELECT 7"), {}, timeout_ms=2000) == [(7,)]
    assert db.stmts == ["SELECT 7"]


def test_the_read_timeout_is_the_loops_event_tick_spacing():
    from app.services.trading.momentum_neural.live_runner_loop import _EVENT_TICK_MIN_SPACING_S

    assert lr._exit_verdict_read_timeout_ms() == int(_EVENT_TICK_MIN_SPACING_S * 1000) == 2000
