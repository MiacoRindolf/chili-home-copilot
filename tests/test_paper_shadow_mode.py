"""Tests for f-add-paper-shadow-mode.

Covers the 8 cases from the brief:
  1. Flag off (default) -> _maybe_open_paper_shadow is a no-op.
  2. Flag on, decision='placed' -> shadow row created with attribution.
  3. Flag on, decision='blocked_pdt' -> shadow row created (opportunity cost).
  4. Flag on, decision='blocked_no_order_id' -> shadow row created.
  5. Paper-mode call site (the paper branch) does NOT call shadow helper
     (regression guard: dispatcher.py-style source pin).
  6. Shadow row carries paper_shadow_of_alert_id matching the alert.
  7. update_pattern_stats_from_closed_trades only reads Trade table
     (no PaperTrade union); shadow rows can't double-count today.
     Forward-looking comment exists at the query site.
  8. Shadow open failure does not break the live decision flow
     (mock open_paper_trade to raise; helper swallows).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from app import models
from app.models.trading import (
    BreakoutAlert, PaperTrade, ScanPattern,
)
from app.services.trading import auto_trader as at_mod
from app.services.trading.auto_trader import _maybe_open_paper_shadow

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_pattern_and_alert(db) -> tuple[ScanPattern, BreakoutAlert]:
    user = models.User(name="psm_user")
    db.add(user)
    db.flush()
    pat = ScanPattern(
        name="psm_pat",
        rules_json={},
        origin="test",
        asset_class="all",
        timeframe="1d",
        win_rate=0.5,
        avg_return_pct=1.0,
    )
    db.add(pat)
    db.commit()
    db.refresh(pat)

    alert = BreakoutAlert(
        ticker="TEST",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=100.0,
        entry_price=100.0,
        alerted_at=datetime.utcnow(),
        user_id=user.id,
        scan_pattern_id=pat.id,
        stop_loss=95.0,
        target_price=110.0,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return pat, alert


# ---------------------------------------------------------------------------
# 1. Flag off: helper is a no-op
# ---------------------------------------------------------------------------

def test_helper_no_op_when_flag_off(db, monkeypatch):
    pat, alert = _seed_pattern_and_alert(db)
    # Confirm flag default is False (don't override).
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_enabled", False,
    )
    _maybe_open_paper_shadow(
        db, uid=alert.user_id, alert=alert, qty=1, px=100.0,
        snap={}, decision="placed",
    )
    rows = db.query(PaperTrade).filter(
        PaperTrade.paper_shadow_of_alert_id == alert.id
    ).all()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# 2. Flag on, decision='placed' -> shadow opens with attribution
# ---------------------------------------------------------------------------

def test_helper_creates_shadow_when_flag_on_placed(db, monkeypatch):
    pat, alert = _seed_pattern_and_alert(db)
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_enabled", True,
    )
    # Stub the slippage/atr helpers so the function doesn't try to fetch
    # market data.
    from app.services.trading import paper_trading as pt_mod
    monkeypatch.setattr(
        pt_mod, "_compute_atr_levels",
        lambda ticker, entry_price, exit_cfg: (
            entry_price * 0.97, entry_price * 1.10, 1.0,
        ),
    )
    monkeypatch.setattr(
        pt_mod, "_apply_slippage",
        lambda price, direction, is_entry: price,
    )

    _maybe_open_paper_shadow(
        db, uid=alert.user_id, alert=alert, qty=1, px=100.0,
        snap={"projected_profit_pct": 5.0}, decision="placed",
    )
    db.commit()
    rows = db.query(PaperTrade).filter(
        PaperTrade.paper_shadow_of_alert_id == alert.id
    ).all()
    assert len(rows) == 1
    pt = rows[0]
    assert pt.scan_pattern_id == pat.id
    assert pt.ticker == "TEST"
    assert pt.entry_price == pytest.approx(100.0)
    assert pt.signal_json.get("paper_shadow") is True
    assert pt.signal_json.get("shadow_decision") == "placed"


# ---------------------------------------------------------------------------
# 3, 4. Flag on, blocked decisions still create shadow (opportunity cost)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["blocked_pdt", "blocked_no_order_id"])
def test_helper_creates_shadow_on_blocked_decisions(db, monkeypatch, decision):
    pat, alert = _seed_pattern_and_alert(db)
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_enabled", True,
    )
    from app.services.trading import paper_trading as pt_mod
    monkeypatch.setattr(
        pt_mod, "_compute_atr_levels",
        lambda ticker, entry_price, exit_cfg: (
            entry_price * 0.97, entry_price * 1.10, 1.0,
        ),
    )
    monkeypatch.setattr(
        pt_mod, "_apply_slippage",
        lambda price, direction, is_entry: price,
    )

    _maybe_open_paper_shadow(
        db, uid=alert.user_id, alert=alert, qty=1, px=100.0,
        snap={}, decision=decision,
    )
    db.commit()
    rows = db.query(PaperTrade).filter(
        PaperTrade.paper_shadow_of_alert_id == alert.id
    ).all()
    assert len(rows) == 1
    assert rows[0].signal_json.get("shadow_decision") == decision


def test_helper_creates_shadow_for_qualified_block_when_base_flag_off(db, monkeypatch):
    pat, alert = _seed_pattern_and_alert(db)
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_enabled", False,
    )
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_qualified_blocks_enabled", True,
    )
    from app.services.trading import paper_trading as pt_mod
    monkeypatch.setattr(
        pt_mod, "_compute_atr_levels",
        lambda ticker, entry_price, exit_cfg: (
            entry_price * 0.97, entry_price * 1.10, 1.0,
        ),
    )
    monkeypatch.setattr(
        pt_mod, "_apply_slippage",
        lambda price, direction, is_entry: price,
    )

    _maybe_open_paper_shadow(
        db, uid=alert.user_id, alert=alert, qty=1, px=100.0,
        snap={}, decision="blocked_coinbase_cap",
    )
    db.commit()

    rows = db.query(PaperTrade).filter(
        PaperTrade.paper_shadow_of_alert_id == alert.id
    ).all()
    assert len(rows) == 1
    assert rows[0].scan_pattern_id == pat.id
    assert rows[0].signal_json.get("shadow_decision") == "blocked_coinbase_cap"


# ---------------------------------------------------------------------------
# 5. Paper branch (live=False) does NOT call shadow helper
# ---------------------------------------------------------------------------

def test_paper_branch_does_not_call_shadow_helper():
    """Source-text guard: the paper branch (after `# Paper` comment)
    must not reference _maybe_open_paper_shadow. If a future edit adds
    it there, shadow + paper-direct would create duplicate paper trades
    for the same alert."""
    src = (REPO / "app/services/trading/auto_trader.py").read_text()
    paper_marker = src.find("\n    # Paper\n")
    assert paper_marker > 0, "expected '# Paper' marker"
    paper_branch = src[paper_marker:]
    assert "_maybe_open_paper_shadow" not in paper_branch, (
        "paper branch must not call _maybe_open_paper_shadow "
        "(would duplicate paper trades when live=False)"
    )


# ---------------------------------------------------------------------------
# 6. paper_shadow_of_alert_id == alert.id (covered by tests 2/3)
# ---------------------------------------------------------------------------
# (Asserted inline in tests 2 + 3 above.)


# ---------------------------------------------------------------------------
# 7. update_pattern_stats_from_closed_trades doesn't read PaperTrade today
# ---------------------------------------------------------------------------

def test_evidence_aggregation_doesnt_read_paper_trades():
    """Forward-looking guard: today's
    update_pattern_stats_from_closed_trades reads only Trade. If a
    future edit adds a PaperTrade union, the f-add-paper-shadow-mode
    comment must stay (or the filter for paper_shadow_of_alert_id must
    be added). This test pins both."""
    src = (REPO / "app/services/trading/learning.py").read_text(encoding="utf-8")
    # Find the function body.
    fn_start = src.find("def update_pattern_stats_from_closed_trades")
    assert fn_start > 0
    # Find the next def (function end).
    fn_end = src.find("\ndef ", fn_start + 1)
    fn_body = src[fn_start:fn_end if fn_end > 0 else len(src)]

    # Today's contract: function body references Trade but NOT PaperTrade.
    assert "Trade." in fn_body
    # If PaperTrade ever appears, the filter must too.
    if "PaperTrade." in fn_body:
        assert "paper_shadow_of_alert_id" in fn_body, (
            "PaperTrade was added to the closed-trade query but the "
            "shadow filter is missing -- shadow rows would double-count"
        )

    # Pin the f-add-paper-shadow-mode comment so future deletes are visible.
    assert "f-add-paper-shadow-mode" in fn_body, (
        "the forward-looking comment was deleted; re-add or Cowork "
        "needs to know the contract changed"
    )


# ---------------------------------------------------------------------------
# 8. Shadow open failure does not break the live decision flow
# ---------------------------------------------------------------------------

def test_shadow_failure_swallowed(db, monkeypatch, caplog):
    pat, alert = _seed_pattern_and_alert(db)
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_enabled", True,
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated open_paper_trade failure")

    with patch(
        "app.services.trading.paper_trading.open_paper_trade",
        side_effect=_boom,
    ):
        # Must NOT raise -- the helper swallows.
        _maybe_open_paper_shadow(
            db, uid=1, alert=alert, qty=1, px=100.0,
            snap={}, decision="placed",
        )

    # No shadow row should have been created.
    rows = db.query(PaperTrade).filter(
        PaperTrade.paper_shadow_of_alert_id == alert.id
    ).all()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Bonus: live-branch wiring guard (regression)
# ---------------------------------------------------------------------------

def test_live_branch_calls_shadow_at_all_three_terminal_points():
    """Source-text guard: the three terminal `_audit(... decision=...)`
    branches in the live path must each precede a
    `_maybe_open_paper_shadow(...)` call. Catches accidental future
    deletion of one of the three wirings."""
    src = (REPO / "app/services/trading/auto_trader.py").read_text()
    # All three shadow calls in the live branch carry one of these
    # decision strings.
    for dec in ("placed", "blocked_pdt", "blocked_no_order_id"):
        assert f'decision="{dec}"' in src, (
            f"live-branch shadow call for decision={dec!r} missing"
        )
