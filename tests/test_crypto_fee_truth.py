"""Crypto fee truth (2026-06-13): venue-bps paper fees + live commission booking.

The crypto forensics found the paper sim booked ~1/7th of real Coinbase fees
and the live ledger hardcoded fee=0.0 — the system was blind to the cost that
caused 81 % of the crypto lane's losses. These tests pin the fix.
"""

import math
from types import SimpleNamespace

import app.services.trading.momentum_neural.paper_execution as pe
from app.services.trading.momentum_neural import live_runner
from app.services.trading.momentum_neural.live_runner import _order_total_fees_usd
from app.services.trading.momentum_neural.paper_execution import (
    crypto_paper_roundtrip_bps,
    roundtrip_fee_usd,
)


# ── paper: venue-bps truth path ──────────────────────────────────────────────

def test_venue_bps_overrides_the_ratio_model():
    # $1,000 notional at Coinbase taker 153 bps round trip = $15.30 — NOT the
    # ratio model's ~8 % of target PnL (which on a 2 % target is ~$1.60).
    fee = roundtrip_fee_usd(1_000.0, 0.08, entry=100.0, target=102.0, venue_rt_bps=153.0)
    assert abs(fee - 15.30) < 1e-9


def test_ratio_model_unchanged_when_no_venue_bps():
    fee = roundtrip_fee_usd(1_000.0, 0.08, entry=100.0, target=102.0)
    assert abs(fee - (20.0 * 0.08)) < 1e-9  # 8 % of the $20 target PnL


def test_garbage_venue_bps_falls_back_to_ratio_model():
    base = roundtrip_fee_usd(1_000.0, 0.08, entry=100.0, target=102.0)
    assert roundtrip_fee_usd(1_000.0, 0.08, entry=100.0, target=102.0, venue_rt_bps=float("nan")) == base
    assert roundtrip_fee_usd(1_000.0, 0.08, entry=100.0, target=102.0, venue_rt_bps=-5.0) == base


# ── paper crypto fee mode: maker-only charges maker, else taker ──────────────

class _FeeSettings:
    chili_coinbase_taker_fee_bps_round_trip = 153
    chili_coinbase_maker_fee_bps_round_trip = 50
    chili_coinbase_maker_only_enabled = False


def test_crypto_paper_bps_taker_by_default(monkeypatch):
    monkeypatch.setattr(pe, "settings", _FeeSettings())
    assert crypto_paper_roundtrip_bps() == 153.0


def test_crypto_paper_bps_maker_when_maker_only(monkeypatch):
    s = _FeeSettings()
    s.chili_coinbase_maker_only_enabled = True
    monkeypatch.setattr(pe, "settings", s)
    assert crypto_paper_roundtrip_bps() == 50.0


# ── live: broker-reported commission extraction ──────────────────────────────

def _no(raw):
    return SimpleNamespace(raw=raw)


def test_order_total_fees_reads_coinbase_raw():
    assert _order_total_fees_usd(_no({"total_fees": "1.23"})) == 1.23
    assert _order_total_fees_usd(_no({"totalFees": 0.5})) == 0.5


def test_order_total_fees_absent_or_garbage_is_none():
    assert _order_total_fees_usd(_no({})) is None                       # Robinhood shape
    assert _order_total_fees_usd(_no({"total_fees": "abc"})) is None
    assert _order_total_fees_usd(_no({"total_fees": -1.0})) is None
    assert _order_total_fees_usd(SimpleNamespace()) is None


# ── live: completion nets fees out of realized PnL + books them ──────────────

class _Captures:
    def __init__(self):
        self.ledger_fee = None
        self.ledger_pnl = None


def _patch_completion_deps(monkeypatch, cap):
    monkeypatch.setattr(live_runner, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(live_runner, "_commit_le", lambda *a, **k: None)
    monkeypatch.setattr(live_runner, "_safe_transition", lambda *a, **k: None)
    monkeypatch.setattr(live_runner, "_finalize_live_decision_after_exit", lambda *a, **k: None)

    def _cap_exit(db, sess, *, le, quantity, entry_price, fill_price,
                  realized_pnl_usd, reason, fee=0.0):
        cap.ledger_fee = fee
        cap.ledger_pnl = realized_pnl_usd

    monkeypatch.setattr(live_runner, "_record_live_exit_ledger_safe", _cap_exit)

    def _cap_partial(db, sess, *, le, quantity, entry_price, fill_price,
                     realized_pnl_usd, reason, fee=0.0):
        cap.ledger_fee = fee
        cap.ledger_pnl = realized_pnl_usd

    monkeypatch.setattr(live_runner, "_record_live_partial_exit_ledger_safe", _cap_partial)


def _sess():
    return SimpleNamespace(id=1, symbol="ORCA-USD", user_id=1, venue="coinbase",
                           mode="live", state="live_trailing", variant_id=1)


def test_full_exit_nets_exit_and_entry_fees(monkeypatch):
    cap = _Captures()
    _patch_completion_deps(monkeypatch, cap)
    le = {
        "position": {"stop_price": 99.0, "target_price": 103.0},
        "last_exit_fee_usd": 0.5,
        "entry_fee_usd_unbooked": 0.7,
    }
    pnl = live_runner._complete_confirmed_live_exit(
        None, _sess(), le=le, quantity=2.0, entry_price=100.0, fill_price=101.0,
        reason="target", slip_bps=5.0,
    )
    assert abs(pnl - (2.0 - 1.2)) < 1e-9          # gross 2.00 − fees 1.20
    assert abs(le["realized_pnl_usd"] - 0.8) < 1e-9
    assert abs(le["fees_usd_total"] - 1.2) < 1e-9
    assert abs(cap.ledger_fee - 1.2) < 1e-9       # ledger sees the real fee
    assert "entry_fee_usd_unbooked" not in le      # consumed exactly once
    assert "last_exit_fee_usd" not in le


def test_full_exit_without_fee_data_books_zero(monkeypatch):
    cap = _Captures()
    _patch_completion_deps(monkeypatch, cap)
    le = {"position": {}}
    pnl = live_runner._complete_confirmed_live_exit(
        None, _sess(), le=le, quantity=2.0, entry_price=100.0, fill_price=101.0,
        reason="target", slip_bps=5.0,
    )
    assert abs(pnl - 2.0) < 1e-9                   # old behavior preserved
    assert cap.ledger_fee == 0.0


def test_partial_exit_nets_only_its_own_fee(monkeypatch):
    cap = _Captures()
    _patch_completion_deps(monkeypatch, cap)
    le = {
        "position": {"quantity": 3.0, "avg_entry_price": 100.0},
        "last_exit_fee_usd": 0.4,
        "entry_fee_usd_unbooked": 0.7,
    }
    pnl = live_runner._apply_confirmed_live_partial_exit(
        None, _sess(), le=le, filled_quantity=1.0, entry_price=100.0,
        fill_price=102.0, reason="scale_out",
    )
    assert abs(pnl - (2.0 - 0.4)) < 1e-9
    assert abs(le["fees_usd_total"] - 0.4) < 1e-9
    assert le["entry_fee_usd_unbooked"] == 0.7     # waits for the FULL exit
    assert abs(cap.ledger_fee - 0.4) < 1e-9
    assert math.isclose(le["position"]["quantity"], 2.0)
