"""Rank-displacement arm fix (2026-06-17): when arm slots are FULL, evict the worst-ranked
INERT pre-entry watcher so a higher-ranked NEWCOMER can arm (UTSI #7 @0.7275 sat un-armed
all session while 7/9 slots held 0.55-0.69 names; Ross made +$52k on UTSI).

Safety invariants from the design+red-team (wf wvtzafsfs):
  PARITY      — flag OFF -> no row touched, byte-identical skip-on-full.
  ORPHAN-SAFE — never reap watching_live (one tick from firing); never reap a session with a
                pending/unresolved entry order; PER-SYMBOL in-flight veto (the CRVO twin case);
                guarded reap re-verifies under a row lock.
  ANTI-THRASH — strict score margin; min-dwell off updated_at; reap-cooldown.
  DUPE-SAFE   — .all() not .one_or_none() (no MultipleResultsFound crash on dup-symbol rows).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models.trading import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import auto_arm as aa

_seq = 0


def _variant(db):
    global _seq
    _seq += 1
    v = MomentumStrategyVariant(
        family="test_rd", variant_key=f"rd_{_seq}", label="t", params_json={}
    )
    db.add(v)
    db.flush()
    return v


def _viab(db, symbol, score, variant_id):
    r = MomentumSymbolViability(
        symbol=symbol,
        variant_id=variant_id,
        viability_score=score,
        paper_eligible=True,
        live_eligible=True,
        freshness_ts=datetime.utcnow(),
    )
    db.add(r)
    db.flush()
    return r


def _sess(db, symbol, state, variant_id, *, le=None, dwell_sec=600, family="robinhood_spot"):
    now = datetime.utcnow()
    s = TradingAutomationSession(
        user_id=None,
        venue="test",
        execution_family=family,
        mode="live",
        symbol=symbol,
        variant_id=variant_id,
        state=state,
        risk_snapshot_json=({"momentum_live_execution": le} if le is not None else {}),
    )
    db.add(s)
    db.flush()
    s.started_at = now - timedelta(seconds=dwell_sec)
    s.updated_at = now - timedelta(seconds=dwell_sec)
    db.flush()
    return s


# ── the core win: displace a low-ranked inert watcher for a better newcomer ──
def test_displaces_worst_inert_for_better_newcomer(db):
    v = _variant(db)
    _viab(db, "LOWA", 0.55, v.id)
    sa = _sess(db, "LOWA", "queued_live", v.id)
    _viab(db, "LOWB", 0.60, v.id)
    sb = _sess(db, "LOWB", "queued_live", v.id)
    nc = _viab(db, "HIGH", 0.73, v.id)  # the starved top mover
    db.commit()
    displaced, info = aa._maybe_rank_displace(db, user_id=None, newcomer=nc, busy_symbols=set())
    assert displaced is True
    assert info["reaped_symbol"] == "LOWA"  # worst score reaped
    db.refresh(sa)
    db.refresh(sb)
    assert sa.state == "live_cancelled"
    assert sb.state == "queued_live"  # only ONE reaped (budget=1)
    assert "LOWA" in aa._REAP_COOLDOWN  # cooled down so it can't instantly re-arm


# ── ORPHAN-SAFE ──
def test_never_reaps_watching_live(db):
    v = _variant(db)
    _viab(db, "WATCH", 0.55, v.id)
    s = _sess(db, "WATCH", "watching_live", v.id)  # one tick from live_entry_candidate
    nc = _viab(db, "HIGH", 0.73, v.id)
    db.commit()
    displaced, _ = aa._maybe_rank_displace(db, user_id=None, newcomer=nc, busy_symbols=set())
    assert displaced is False
    db.refresh(s)
    assert s.state == "watching_live"


def test_never_reaps_with_entry_order_id(db):
    v = _variant(db)
    _viab(db, "PEND", 0.55, v.id)
    s = _sess(db, "PEND", "queued_live", v.id, le={"entry_order_id": "abc-123"})
    nc = _viab(db, "HIGH", 0.73, v.id)
    db.commit()
    displaced, _ = aa._maybe_rank_displace(db, user_id=None, newcomer=nc, busy_symbols=set())
    assert displaced is False
    db.refresh(s)
    assert s.state == "queued_live"


def test_never_reaps_with_unresolved_history(db):
    """The ack-timeout pushback orphan vector: entry_order_id cleared to None, but an id
    remains in entry_order_ids_all with an empty resolved-map -> can still late-fill."""
    v = _variant(db)
    _viab(db, "UNRES", 0.55, v.id)
    s = _sess(
        db, "UNRES", "queued_live", v.id,
        le={"entry_order_id": None, "entry_order_ids_all": ["old-1"], "entry_orders_resolved": {}},
    )
    nc = _viab(db, "HIGH", 0.73, v.id)
    db.commit()
    displaced, _ = aa._maybe_rank_displace(db, user_id=None, newcomer=nc, busy_symbols=set())
    assert displaced is False
    db.refresh(s)
    assert s.state == "queued_live"


def test_per_symbol_inflight_veto(db):
    """Twin case (CRVO): an inert queued twin must NOT be reaped while a SIBLING session
    for the same symbol holds an in-flight order."""
    v = _variant(db)
    _viab(db, "TWIN", 0.55, v.id)
    s_inert = _sess(db, "TWIN", "queued_live", v.id)
    _sess(db, "TWIN", "watching_live", v.id)  # in-flight sibling (past the inert stage)
    _viab(db, "OTHER", 0.60, v.id)
    _sess(db, "OTHER", "queued_live", v.id)
    nc = _viab(db, "HIGH", 0.73, v.id)
    db.commit()
    displaced, info = aa._maybe_rank_displace(db, user_id=None, newcomer=nc, busy_symbols=set())
    assert displaced is True
    assert info["reaped_symbol"] == "OTHER"  # TWIN vetoed; next-worst reaped
    db.refresh(s_inert)
    assert s_inert.state == "queued_live"  # the inert twin survived


# ── ANTI-THRASH ──
def test_margin_insufficient_no_op(db):
    v = _variant(db)
    _viab(db, "CLOSE", 0.72, v.id)
    s = _sess(db, "CLOSE", "queued_live", v.id)
    nc = _viab(db, "HIGH", 0.73, v.id)  # margin 0.01 < 0.02 default
    db.commit()
    displaced, _ = aa._maybe_rank_displace(db, user_id=None, newcomer=nc, busy_symbols=set())
    assert displaced is False
    db.refresh(s)
    assert s.state == "queued_live"


def test_min_dwell_uses_updated_at(db):
    v = _variant(db)
    _viab(db, "FRESH", 0.55, v.id)
    s = _sess(db, "FRESH", "queued_live", v.id, dwell_sec=10)  # updated_at 10s ago < 45s
    nc = _viab(db, "HIGH", 0.73, v.id)
    db.commit()
    displaced, _ = aa._maybe_rank_displace(db, user_id=None, newcomer=nc, busy_symbols=set())
    assert displaced is False
    db.refresh(s)
    assert s.state == "queued_live"


# ── DUPE-SAFE ──
def test_dupe_symbols_no_crash(db):
    """The current 9-dup-symbol reality must not raise MultipleResultsFound."""
    v = _variant(db)
    _viab(db, "DUP", 0.55, v.id)
    _sess(db, "DUP", "queued_live", v.id)
    _sess(db, "DUP", "queued_live", v.id)  # duplicate inert sessions, same symbol
    nc = _viab(db, "HIGH", 0.73, v.id)
    db.commit()
    displaced, info = aa._maybe_rank_displace(db, user_id=None, newcomer=nc, busy_symbols=set())
    assert displaced is True  # no crash; one inert DUP reaped (neither in-flight -> safe)
    assert info["reaped_symbol"] == "DUP"


# ── PARITY ──
def test_flag_off_parity(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_rank_displacement_enabled", False)
    out: dict = {}
    res = aa._try_displacement_for_full_slots(db, uid=None, out=out)
    assert res is False
    assert out == {}  # nothing fetched, nothing touched
