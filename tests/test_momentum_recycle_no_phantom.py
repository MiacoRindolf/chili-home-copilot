"""SAFETY: momentum-lane duplicate-fill root cause (2026-06-27, AREC sid 9331).

Two independent guards against a recycled live watcher re-adopting its OWN already-
filled entry order (-> phantom 2x long + stuck live_bailout spin):

FIX A — _record_fill_outcome_safe is IDEMPOTENT by broker_order_id. Re-polling the
        SAME real broker order (recycle / repeg / late-fill sweep) logs ONE row, not
        two. A DIFFERENT broker order id logs a second row. broker_order_id IS NULL
        (paper / synthetic) keeps the leg_seq behavior.

FIX B — at the COOLDOWN -> WATCHING_LIVE recycle the runner RESETS every per-trade
        entry-order / position lifecycle key (entry_order_id, entry_order_ids_all,
        entry_orders_resolved, entry_submitted, position, + the per-trade exit/scale/
        pyramid/anticipation/micropullback/stop/halt markers) so the recycled watcher
        starts CLEAN. Identity / cooldown / trade_cycles / cumulative PnL+fees /
        discipline counters PERSIST. With the kill-switch OFF the recycle is byte-
        identical (state retained).

Uses the truncating ``db`` fixture (TEST_DATABASE_URL, _test DB) and reuses the
live-runner tick harness (_mk_adapter / _seed_live_eligible_row / _uid).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_COOLDOWN,
    STATE_WATCHING_LIVE,
)
from app.services.trading.momentum_neural.live_runner import tick_live_session
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY

# Reuse the existing live-runner harness pieces verbatim (connected venue + valid BBO
# adapter, viability seeding, user id). Importing the autouse _venue_connected_by_default
# fixture makes the tick reach the real state-handler logic in THIS module too.
from tests.test_momentum_live_runner import (  # noqa: F401
    _mk_adapter,
    _uid,
    _venue_connected_by_default,
)
from tests.test_momentum_paper_runner import _seed_live_eligible_row


# ── seeding for the Fix-A direct writer test ─────────────────────────────────
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession

_variant_seq = 0


def _variant(db):
    global _variant_seq
    _variant_seq += 1
    v = MomentumStrategyVariant(
        family="test_family",
        variant_key=f"recyc_{_variant_seq}",
        label="recycle test variant",
        params_json={},
    )
    db.add(v)
    db.flush()
    return v


def _fill_log_session(db, *, symbol="AREC", execution_family="robinhood_spot"):
    v = _variant(db)
    sess = TradingAutomationSession(
        user_id=None,
        venue="test",
        execution_family=execution_family,
        mode="live",
        symbol=symbol,
        variant_id=v.id,
        state="live_entered",
        risk_snapshot_json={"momentum_live_execution": {}},
        correlation_id="corr-recyc",
    )
    db.add(sess)
    db.flush()
    return sess


def _count_rows(db, session_id) -> int:
    return int(
        db.execute(
            text("SELECT COUNT(*) FROM momentum_fill_outcomes WHERE session_id = :s"),
            {"s": session_id},
        ).scalar()
        or 0
    )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ FIX A — idempotent fill-log by broker_order_id                            ║
# ╚══════════════════════════════════════════════════════════════════════════╝
def test_fix_a_same_broker_order_id_logs_one_row(db, monkeypatch):
    """The smoking gun: re-ingesting the SAME broker order id (a recycled watcher
    re-polling its own filled entry) must insert exactly ONE row — the second call
    is the idempotent skip, NOT a fresh leg_seq+1 row."""
    sess = _fill_log_session(db)
    monkeypatch.setattr(lr.settings, "chili_momentum_fill_log_enabled", True, raising=False)

    common = dict(
        side="entry", fill_source="broker_confirmed", broker_order_id="AREC-ENTRY-1",
        fill_price=2.21, qty=221.0, fees_usd=0.0, order_status="filled",
        intended_price=2.20, spread_bps_at_decision=30.0,
    )
    lr._record_fill_outcome_safe(db, sess, **common)
    lr._record_fill_outcome_safe(db, sess, **common)  # re-poll of the SAME order
    db.commit()

    assert _count_rows(db, sess.id) == 1, "same broker_order_id must log exactly one row"


def test_fix_a_different_broker_order_id_logs_second_row(db, monkeypatch):
    """A genuinely DIFFERENT broker order (a real second leg) must still log."""
    sess = _fill_log_session(db)
    monkeypatch.setattr(lr.settings, "chili_momentum_fill_log_enabled", True, raising=False)

    base = dict(
        side="entry", fill_source="broker_confirmed",
        fill_price=2.21, qty=221.0, fees_usd=0.0, order_status="filled",
        intended_price=2.20, spread_bps_at_decision=30.0,
    )
    lr._record_fill_outcome_safe(db, sess, broker_order_id="AREC-ENTRY-1", **base)
    lr._record_fill_outcome_safe(db, sess, broker_order_id="AREC-ENTRY-2", **base)
    db.commit()

    assert _count_rows(db, sess.id) == 2, "distinct broker_order_ids must each log a row"


def test_fix_a_null_broker_order_id_keeps_leg_seq_behavior(db, monkeypatch):
    """broker_order_id IS NULL (paper / synthetic / broker-zero escape) is exempt
    from the dedupe pre-check and keeps the leg_seq path — two NULL-order calls for
    the same (session, side) get distinct leg_seq and BOTH rows land."""
    sess = _fill_log_session(db)
    monkeypatch.setattr(lr.settings, "chili_momentum_fill_log_enabled", True, raising=False)

    base = dict(
        side="exit", fill_source="reconstructed", broker_order_id=None,
        fill_price=2.10, qty=110.0, fees_usd=0.0, order_status="filled",
        intended_price=2.10, spread_bps_at_decision=10.0,
        entry_price=2.21, exit_reason="stop", realized_pnl_usd=-12.1,
    )
    lr._record_fill_outcome_safe(db, sess, **base)
    lr._record_fill_outcome_safe(db, sess, **base)
    db.commit()

    assert _count_rows(db, sess.id) == 2, "NULL broker_order_id keeps the leg_seq (non-dedup) path"


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ FIX B — recycle resets entry-order / position lifecycle state             ║
# ╚══════════════════════════════════════════════════════════════════════════╝
def test_fix_b_reset_helper_clears_all_adoption_keys_keeps_session_state():
    """Unit: _reset_entry_state_on_recycle clears EVERY entry-order / position
    lifecycle key (incl. the load-bearing five the late-fill sweep / pre-submit poll
    read) and RETAINS the cross-cycle session state."""
    le = {
        # ── the load-bearing adoption keys (must be cleared) ──
        "entry_order_id": "AREC-ENTRY-1",
        "entry_order_ids_all": ["AREC-ENTRY-1"],
        "entry_orders_resolved": {"AREC-ENTRY-1": "adopted"},
        "entry_submitted": True,
        "position": {"quantity": 221.0, "avg_entry_price": 2.21},
        # ── a sample of per-trade lifecycle keys (must be cleared) ──
        "entry_submit_utc": "2026-06-27T13:00:00",
        "entry_limit_price": 2.20,
        "entry_stop_atr_pct": 0.05,
        "exit_order_id": "AREC-EXIT-1",
        "pending_exit_reason": "stop",
        "scale_limit_order_id": "AREC-SCALE-1",
        "pyramid_order_id": "AREC-PYR-1",
        "anticipation_add_order_id": "AREC-ANT-1",
        "micropullback_reentry_order_id": "AREC-MPR-1",
        "max_loss_circuit_fired": True,
        "structural_stop_price": 2.05,
        "halt_entry_size_mult": 0.5,
        # ── cross-cycle session state (must be RETAINED) ──
        "trade_cycles": 3,
        "realized_pnl_usd": -42.0,
        "fees_usd_total": 1.5,
        "per_symbol_fatigue": {"mult": 0.8},
        "win_cycle_fatigue": {"mult": 0.9},
        "green_day_graduation": {"on": True},
        "prior_day_pnl_damper": {"mult": 0.7},
        "post_exit_excursion_pending": {"state": "pending"},
        "last_exit_reason": "stop",
        "last_exit_price": 2.10,
        "halt_chain_up_count": 2,
        "eod_flatten_done": True,
        "tick_count": 17,
    }
    cleared = lr._reset_entry_state_on_recycle(le)

    # the load-bearing five are gone
    for k in ("entry_order_id", "entry_order_ids_all", "entry_orders_resolved",
              "entry_submitted", "position"):
        assert k not in le, f"{k} must be cleared on recycle"
        assert k in cleared, f"{k} must be reported as reset"

    # sample lifecycle keys gone
    for k in ("entry_submit_utc", "entry_limit_price", "entry_stop_atr_pct",
              "exit_order_id", "pending_exit_reason", "scale_limit_order_id",
              "pyramid_order_id", "anticipation_add_order_id",
              "micropullback_reentry_order_id", "max_loss_circuit_fired",
              "structural_stop_price", "halt_entry_size_mult"):
        assert k not in le, f"{k} must be cleared on recycle"

    # cross-cycle state retained
    for k, want in (
        ("trade_cycles", 3),
        ("realized_pnl_usd", -42.0),
        ("fees_usd_total", 1.5),
        ("halt_chain_up_count", 2),
        ("eod_flatten_done", True),
        ("tick_count", 17),
    ):
        assert le.get(k) == want, f"{k} must persist across recycle"
    for k in ("per_symbol_fatigue", "win_cycle_fatigue", "green_day_graduation",
              "prior_day_pnl_damper", "post_exit_excursion_pending",
              "last_exit_reason", "last_exit_price"):
        assert k in le, f"{k} must persist across recycle"


def test_fix_b_reset_set_covers_every_adoption_gate_key():
    """Guard against a future edit dropping an adoption key from the reset set: the
    five keys the entry-poll / late-fill adoption path reads MUST all be present."""
    for k in ("entry_order_id", "entry_order_ids_all", "entry_orders_resolved",
              "entry_submitted", "position"):
        assert k in lr._RECYCLE_ENTRY_STATE_KEYS, f"{k} missing from _RECYCLE_ENTRY_STATE_KEYS"


def _seed_cooldown_session(db, *, symbol, name, cooldown_expired=True):
    """A live session sitting in COOLDOWN with the PRIOR trade's entry-order +
    position state still on `le` and (by default) the cooldown already elapsed."""
    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    uid = _uid(db, name)
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    until = datetime.utcnow() - timedelta(seconds=5) if cooldown_expired else (
        datetime.utcnow() + timedelta(seconds=3600)
    )
    sess = create_trading_automation_session(
        db, user_id=uid, symbol=symbol, variant_id=vid, mode="live",
        state=STATE_LIVE_COOLDOWN,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_live_execution": {
                "cooldown_until_utc": until.replace(tzinfo=timezone.utc).isoformat(),
                # PRIOR trade lifecycle state — the phantom fuel.
                "entry_order_id": "AREC-ENTRY-1",
                "entry_order_ids_all": ["AREC-ENTRY-1"],
                "entry_orders_resolved": {"AREC-ENTRY-1": "adopted"},
                "entry_submitted": True,
                "position": {"quantity": 221.0, "avg_entry_price": 2.21,
                             "product_id": symbol},
                "exit_order_id": "AREC-EXIT-1",
                "structural_stop_price": 2.05,
                # cross-cycle state that MUST survive the recycle.
                "trade_cycles": 1,
                "realized_pnl_usd": -42.0,
                "fees_usd_total": 1.5,
                "per_symbol_fatigue": {"mult": 0.8},
            },
        },
    )
    db.commit()
    return sess


def _le(sess):
    return (sess.risk_snapshot_json or {}).get("momentum_live_execution") or {}


def test_fix_b_tick_recycle_clears_entry_state_and_recycles(monkeypatch, db):
    """End-to-end: a COOLDOWN session whose cooldown has elapsed recycles to
    WATCHING_LIVE with the entry-order / position state CLEARED (no phantom to
    re-adopt) and trade_cycles incremented + cross-cycle state retained."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_recycle_entry_state_reset_enabled", True)
    sess = _seed_cooldown_session(db, symbol="AREC-USD", name="recyc_on")

    ad = _mk_adapter()
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
               return_value=False):
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)

    assert sess.state == STATE_WATCHING_LIVE
    le = _le(sess)
    # The adoption keys are GONE — the recycled watcher has no entry order to re-poll.
    for k in ("entry_order_id", "entry_order_ids_all", "entry_orders_resolved",
              "entry_submitted", "position", "exit_order_id", "structural_stop_price"):
        assert k not in le, f"{k} must be cleared after recycle"
    # trade_cycles incremented; cross-cycle accounting + discipline retained.
    assert le.get("trade_cycles") == 2
    assert le.get("realized_pnl_usd") == -42.0
    assert le.get("fees_usd_total") == 1.5
    assert le.get("per_symbol_fatigue") == {"mult": 0.8}


def test_fix_b_flag_off_retains_entry_state_byte_identical(monkeypatch, db):
    """Kill-switch OFF => the recycle is byte-identical to the legacy behavior: the
    session still recycles + increments trade_cycles, but the entry-order / position
    state is RETAINED (the pre-fix behavior)."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_recycle_entry_state_reset_enabled", False)
    sess = _seed_cooldown_session(db, symbol="ARECOFF-USD", name="recyc_off")

    ad = _mk_adapter()
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
               return_value=False):
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)

    assert sess.state == STATE_WATCHING_LIVE
    le = _le(sess)
    # Flag OFF: the prior-trade entry-order / position state is STILL THERE.
    assert le.get("entry_order_id") == "AREC-ENTRY-1"
    assert le.get("entry_submitted") is True
    assert le.get("entry_order_ids_all") == ["AREC-ENTRY-1"]
    assert le.get("entry_orders_resolved") == {"AREC-ENTRY-1": "adopted"}
    assert isinstance(le.get("position"), dict)
    # Recycle bookkeeping still runs (only the entry-state reset is gated).
    assert le.get("trade_cycles") == 2
