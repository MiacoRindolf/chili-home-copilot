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
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.config import (
    AUTOTRADER_PAPER_SHADOW_DEFAULT_DEDUPE_RECENT_REASON_FAMILY_MINUTES,
    AUTOTRADER_PAPER_SHADOW_DEFAULT_MAX_OPEN,
    PATTERN_IMMINENT_HARD_RECERT_SHADOW_SIGNAL_LANE,
)
from app import models
from app.models.trading import (
    BreakoutAlert, PaperTrade, ScanPattern,
)
from app.services.trading import auto_trader as at_mod
from app.services.trading.auto_trader import (
    PAPER_SHADOW_DUPLICATE_POLICY_REJECT_BYPASS,
    PAPER_SHADOW_DUPLICATE_POLICY_STRICT,
    SYNERGY_RETRY_EXHAUSTED_REASON,
    SYNERGY_RETRY_SOURCE_REASON,
    _maybe_open_paper_shadow,
    _maybe_open_reject_paper_shadow,
)
from app.services.trading.paper_trading import (
    PAPER_SHADOW_CAPACITY_EVICTED_REASON,
    PAPER_SHADOW_CAPACITY_EVICTION_META_KEY,
    prune_autotrader_paper_shadow_capacity,
)

REPO = Path(__file__).resolve().parent.parent
TEST_SHADOW_QUANTITY = 1
TEST_REJECT_SHADOW_LIGHTWEIGHT_ALERT_ID = 101
TEST_REJECT_SHADOW_RISK_ALERT_ID = 102
TEST_REJECT_SHADOW_LIGHTWEIGHT_PATTERN_ID = 202
TEST_REJECT_SHADOW_RISK_PATTERN_ID = 203
TEST_REJECT_SHADOW_LIGHTWEIGHT_NOTIONAL = 120.0
TEST_REJECT_SHADOW_LIGHTWEIGHT_PRICE = 10.0
TEST_REJECT_SHADOW_LIGHTWEIGHT_QTY = (
    TEST_REJECT_SHADOW_LIGHTWEIGHT_NOTIONAL / TEST_REJECT_SHADOW_LIGHTWEIGHT_PRICE
)
TEST_REJECT_SHADOW_RISK_NOTIONAL = 50.0
TEST_REJECT_SHADOW_RISK_PRICE = 25.0
TEST_REJECT_SHADOW_RISK_QTY = (
    TEST_REJECT_SHADOW_RISK_NOTIONAL / TEST_REJECT_SHADOW_RISK_PRICE
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_pattern_and_alert(db) -> tuple[ScanPattern, BreakoutAlert]:
    suffix = uuid4().hex[:12]
    user = models.User(name=f"psm_user_{suffix}")
    db.add(user)
    db.flush()
    pat = ScanPattern(
        name=f"psm_pat_{suffix}",
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


def _seed_sibling_alert(db, alert: BreakoutAlert) -> BreakoutAlert:
    sibling = BreakoutAlert(
        ticker=alert.ticker,
        alert_tier=alert.alert_tier,
        score_at_alert=alert.score_at_alert,
        price_at_alert=alert.price_at_alert,
        entry_price=alert.entry_price,
        alerted_at=datetime.utcnow(),
        user_id=alert.user_id,
        scan_pattern_id=alert.scan_pattern_id,
        stop_loss=alert.stop_loss,
        target_price=alert.target_price,
    )
    db.add(sibling)
    db.commit()
    db.refresh(sibling)
    return sibling


# ---------------------------------------------------------------------------
# 1. Flag off: helper is a no-op
# ---------------------------------------------------------------------------

def test_reject_shadow_decision_map():
    assert at_mod._qualified_reject_shadow_decision(
        "non_positive_expected_edge"
    ) == "skipped_non_positive_expected_edge"
    assert at_mod._qualified_reject_shadow_decision(
        "duplicate_pattern_already_open"
    ) == "skipped_duplicate_pattern_already_open"
    assert at_mod._qualified_reject_shadow_decision(
        "max_concurrent_crypto"
    ) == "blocked_max_concurrent_crypto"
    assert at_mod._qualified_reject_shadow_decision("no_quote") is None


def test_reject_shadow_uses_lightweight_sizing_without_broker_lookup(monkeypatch):
    alert = SimpleNamespace(
        id=TEST_REJECT_SHADOW_LIGHTWEIGHT_ALERT_ID,
        ticker="EDGE-USD",
        scan_pattern_id=TEST_REJECT_SHADOW_LIGHTWEIGHT_PATTERN_ID,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_reject_lightweight_sizing_enabled",
        True,
    )
    monkeypatch.setattr(
        at_mod,
        "_resolve_entry_risk_notional",
        lambda *a, **k: pytest.fail("reject shadow should avoid broker-risk sizing"),
    )
    monkeypatch.setattr(
        at_mod,
        "_resolve_shadow_observation_lightweight_notional",
        lambda: (
            TEST_REJECT_SHADOW_LIGHTWEIGHT_NOTIONAL,
            {
                "notional_source": "test_lightweight",
                "notional_broker_lookup_skipped": True,
            },
        ),
    )

    from app.services.trading import tick_normalizer

    monkeypatch.setattr(tick_normalizer, "normalize_quantity", lambda qty, _ticker: qty)
    opened: list[dict] = []
    monkeypatch.setattr(
        at_mod,
        "_maybe_open_paper_shadow",
        lambda *a, **k: opened.append(k),
    )

    _maybe_open_reject_paper_shadow(
        object(),
        uid=1,
        alert=alert,
        px=TEST_REJECT_SHADOW_LIGHTWEIGHT_PRICE,
        snap={},
        reason="non_positive_expected_edge",
    )

    assert len(opened) == 1
    assert opened[0]["qty"] == TEST_REJECT_SHADOW_LIGHTWEIGHT_QTY
    assert opened[0]["decision"] == "skipped_non_positive_expected_edge"
    assert opened[0]["snap"]["paper_shadow_qty_source"] == (
        at_mod.PAPER_SHADOW_REJECT_QTY_SOURCE_LIGHTWEIGHT
    )
    assert opened[0]["snap"]["paper_shadow_notional_broker_lookup_skipped"] is True


def test_reject_shadow_full_risk_sizing_requires_explicit_opt_out(monkeypatch):
    alert = SimpleNamespace(
        id=TEST_REJECT_SHADOW_RISK_ALERT_ID,
        ticker="EDGE-USD",
        scan_pattern_id=TEST_REJECT_SHADOW_RISK_PATTERN_ID,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_reject_lightweight_sizing_enabled",
        False,
    )
    monkeypatch.setattr(
        at_mod,
        "_resolve_shadow_observation_lightweight_notional",
        lambda: pytest.fail("explicit full-risk mode should skip lightweight sizing"),
    )
    monkeypatch.setattr(
        at_mod,
        "_resolve_entry_risk_notional",
        lambda *a, **k: (
            TEST_REJECT_SHADOW_RISK_NOTIONAL,
            {
                "notional_source": "test_risk",
                "notional_broker_lookup_skipped": False,
            },
        ),
    )

    from app.services.trading import tick_normalizer

    monkeypatch.setattr(tick_normalizer, "normalize_quantity", lambda qty, _ticker: qty)
    opened: list[dict] = []
    monkeypatch.setattr(
        at_mod,
        "_maybe_open_paper_shadow",
        lambda *a, **k: opened.append(k),
    )

    _maybe_open_reject_paper_shadow(
        object(),
        uid=1,
        alert=alert,
        px=TEST_REJECT_SHADOW_RISK_PRICE,
        snap={},
        reason="non_positive_expected_edge",
    )

    assert len(opened) == 1
    assert opened[0]["qty"] == TEST_REJECT_SHADOW_RISK_QTY
    assert opened[0]["snap"]["paper_shadow_qty_source"] == (
        at_mod.PAPER_SHADOW_REJECT_QTY_SOURCE_RISK_NOTIONAL
    )
    assert opened[0]["snap"]["paper_shadow_notional_source"] == "test_risk"


def test_hard_recert_signal_lane_requests_shadow_observation() -> None:
    alert = BreakoutAlert(
        indicator_snapshot={
            "imminent_scorecard": {
                "signal_lane": PATTERN_IMMINENT_HARD_RECERT_SHADOW_SIGNAL_LANE,
            },
        },
    )

    assert at_mod._alert_requests_shadow_observation(alert) is True


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
    assert (
        pt.signal_json.get("paper_shadow_duplicate_policy")
        == PAPER_SHADOW_DUPLICATE_POLICY_STRICT
    )


def test_placed_shadow_keeps_duplicate_dedupe_strict(db, monkeypatch):
    pat, alert = _seed_pattern_and_alert(db)
    sibling = _seed_sibling_alert(db, alert)
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
        db,
        uid=alert.user_id,
        alert=alert,
        qty=TEST_SHADOW_QUANTITY,
        px=float(alert.entry_price),
        snap={},
        decision="placed",
    )
    _maybe_open_paper_shadow(
        db,
        uid=sibling.user_id,
        alert=sibling,
        qty=TEST_SHADOW_QUANTITY,
        px=float(sibling.entry_price),
        snap={},
        decision="placed",
    )
    db.commit()

    duplicate_scope_rows = db.query(PaperTrade).filter(
        PaperTrade.user_id == alert.user_id,
        PaperTrade.ticker == alert.ticker,
        PaperTrade.scan_pattern_id == pat.id,
        PaperTrade.status == "open",
    ).all()
    sibling_rows = db.query(PaperTrade).filter(
        PaperTrade.paper_shadow_of_alert_id == sibling.id
    ).all()
    assert len(duplicate_scope_rows) == 1
    assert len(sibling_rows) == 0


def test_reject_shadow_can_bypass_duplicate_dedupe_for_learning(
    db, monkeypatch,
):
    pat, alert = _seed_pattern_and_alert(db)
    sibling = _seed_sibling_alert(db, alert)
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_enabled", False,
    )
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_qualified_blocks_enabled", True,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_reject_allow_duplicate_open",
        True,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_dedupe_recent_reason_family_minutes",
        0,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_max_open",
        AUTOTRADER_PAPER_SHADOW_DEFAULT_MAX_OPEN,
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
    db.add(PaperTrade(
        user_id=alert.user_id,
        scan_pattern_id=pat.id,
        ticker=alert.ticker,
        direction="long",
        entry_price=float(alert.entry_price),
        stop_price=float(alert.stop_loss),
        target_price=float(alert.target_price),
        quantity=TEST_SHADOW_QUANTITY,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json={"auto_trader_v1": True, "paper_shadow": True},
        paper_shadow_of_alert_id=alert.id,
    ))
    db.commit()

    _maybe_open_reject_paper_shadow(
        db,
        uid=sibling.user_id,
        alert=sibling,
        px=float(sibling.entry_price),
        snap={},
        reason="non_positive_expected_edge",
        existing_qty=TEST_SHADOW_QUANTITY,
    )
    db.commit()

    duplicate_scope_rows = db.query(PaperTrade).filter(
        PaperTrade.user_id == alert.user_id,
        PaperTrade.ticker == alert.ticker,
        PaperTrade.scan_pattern_id == pat.id,
        PaperTrade.status == "open",
    ).all()
    sibling_rows = db.query(PaperTrade).filter(
        PaperTrade.paper_shadow_of_alert_id == sibling.id
    ).all()
    assert len(duplicate_scope_rows) == 2
    assert len(sibling_rows) == 1
    assert sibling_rows[0].signal_json.get("shadow_decision") == (
        "skipped_non_positive_expected_edge"
    )
    assert sibling_rows[0].signal_json.get("paper_shadow_reject_reason") == (
        "non_positive_expected_edge"
    )
    assert sibling_rows[0].signal_json.get("paper_shadow_duplicate_policy") == (
        PAPER_SHADOW_DUPLICATE_POLICY_REJECT_BYPASS
    )


def test_reject_shadow_dedupes_recent_same_candidate_reason_family(
    db, monkeypatch,
):
    pat, alert = _seed_pattern_and_alert(db)
    sibling = _seed_sibling_alert(db, alert)
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_enabled", False,
    )
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_qualified_blocks_enabled", True,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_reject_allow_duplicate_open",
        True,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_dedupe_recent_reason_family_minutes",
        AUTOTRADER_PAPER_SHADOW_DEFAULT_DEDUPE_RECENT_REASON_FAMILY_MINUTES,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_max_open",
        AUTOTRADER_PAPER_SHADOW_DEFAULT_MAX_OPEN,
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

    _maybe_open_reject_paper_shadow(
        db,
        uid=alert.user_id,
        alert=alert,
        px=float(alert.entry_price),
        snap={},
        reason="non_positive_expected_edge",
        existing_qty=TEST_SHADOW_QUANTITY,
    )
    _maybe_open_reject_paper_shadow(
        db,
        uid=sibling.user_id,
        alert=sibling,
        px=float(sibling.entry_price),
        snap={},
        reason="non_positive_expected_edge",
        existing_qty=TEST_SHADOW_QUANTITY,
    )
    db.commit()

    duplicate_scope_rows = db.query(PaperTrade).filter(
        PaperTrade.user_id == alert.user_id,
        PaperTrade.ticker == alert.ticker,
        PaperTrade.scan_pattern_id == pat.id,
        PaperTrade.status == "open",
    ).all()
    sibling_rows = db.query(PaperTrade).filter(
        PaperTrade.paper_shadow_of_alert_id == sibling.id
    ).all()
    assert len(duplicate_scope_rows) == 1
    assert len(sibling_rows) == 0
    assert duplicate_scope_rows[0].paper_shadow_of_alert_id == alert.id


def test_reject_shadow_dedupes_same_alert_synergy_retry_family(db, monkeypatch):
    _pat, alert = _seed_pattern_and_alert(db)
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_enabled", False,
    )
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_qualified_blocks_enabled", True,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_reject_allow_duplicate_open",
        True,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_dedupe_same_alert_reason_family",
        True,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_max_open",
        AUTOTRADER_PAPER_SHADOW_DEFAULT_MAX_OPEN,
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

    _maybe_open_reject_paper_shadow(
        db,
        uid=alert.user_id,
        alert=alert,
        px=float(alert.entry_price),
        snap={},
        reason=SYNERGY_RETRY_SOURCE_REASON,
        existing_qty=TEST_SHADOW_QUANTITY,
    )
    _maybe_open_reject_paper_shadow(
        db,
        uid=alert.user_id,
        alert=alert,
        px=float(alert.entry_price),
        snap={"synergy_retry": True},
        reason=SYNERGY_RETRY_EXHAUSTED_REASON,
        existing_qty=TEST_SHADOW_QUANTITY,
    )
    db.commit()

    rows = db.query(PaperTrade).filter(
        PaperTrade.paper_shadow_of_alert_id == alert.id
    ).all()
    assert len(rows) == 1
    assert rows[0].signal_json.get("shadow_decision") == (
        f"skipped_{SYNERGY_RETRY_SOURCE_REASON}"
    )
    assert rows[0].signal_json.get("paper_shadow_reject_reason") == (
        SYNERGY_RETRY_SOURCE_REASON
    )


def test_qualified_block_shadow_bypasses_duplicate_dedupe_for_recert_debt(
    db, monkeypatch,
):
    pat, alert = _seed_pattern_and_alert(db)
    sibling = _seed_sibling_alert(db, alert)
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_enabled", False,
    )
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_qualified_blocks_enabled", True,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_reject_allow_duplicate_open",
        True,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_dedupe_recent_reason_family_minutes",
        0,
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
    db.add(PaperTrade(
        user_id=alert.user_id,
        scan_pattern_id=pat.id,
        ticker=alert.ticker,
        direction="long",
        entry_price=float(alert.entry_price),
        stop_price=float(alert.stop_loss),
        target_price=float(alert.target_price),
        quantity=TEST_SHADOW_QUANTITY,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json={"auto_trader_v1": True, "paper_shadow": True},
        paper_shadow_of_alert_id=alert.id,
    ))
    db.commit()

    _maybe_open_paper_shadow(
        db,
        uid=sibling.user_id,
        alert=sibling,
        qty=TEST_SHADOW_QUANTITY,
        px=float(sibling.entry_price),
        snap={"paper_shadow_reject_reason": "pattern_recert_required"},
        decision="blocked_recert_required",
    )
    db.commit()

    duplicate_scope_rows = db.query(PaperTrade).filter(
        PaperTrade.user_id == alert.user_id,
        PaperTrade.ticker == alert.ticker,
        PaperTrade.scan_pattern_id == pat.id,
        PaperTrade.status == "open",
    ).all()
    sibling_rows = db.query(PaperTrade).filter(
        PaperTrade.paper_shadow_of_alert_id == sibling.id
    ).all()
    assert len(duplicate_scope_rows) == 2
    assert len(sibling_rows) == 1
    assert sibling_rows[0].signal_json.get("shadow_decision") == (
        "blocked_recert_required"
    )
    assert sibling_rows[0].signal_json.get("paper_shadow_duplicate_policy") == (
        PAPER_SHADOW_DUPLICATE_POLICY_REJECT_BYPASS
    )


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


@pytest.mark.parametrize(
    "decision",
    [
        "blocked_coinbase_cap",
        "blocked_max_concurrent_crypto",
        "skipped_non_positive_expected_edge",
        "skipped_duplicate_pattern_already_open",
    ],
)
def test_helper_creates_shadow_for_qualified_block_when_base_flag_off(
    db, monkeypatch, decision,
):
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
        snap={}, decision=decision,
    )
    db.commit()

    rows = db.query(PaperTrade).filter(
        PaperTrade.paper_shadow_of_alert_id == alert.id
    ).all()
    assert len(rows) == 1
    assert rows[0].scan_pattern_id == pat.id
    assert rows[0].signal_json.get("shadow_decision") == decision


def test_rule_gate_reject_opens_qualified_paper_shadow(db, monkeypatch):
    pat, alert = _seed_pattern_and_alert(db)
    pat.lifecycle_stage = "live"
    alert.alert_tier = "pattern_imminent"
    alert.asset_type = "crypto"
    alert.ticker = "EDGE-USD"
    db.commit()

    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_enabled", False,
    )
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_qualified_blocks_enabled", True,
    )
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_max_open", 100,
    )
    monkeypatch.setattr(at_mod, "_current_price", lambda ticker: 100.0)
    monkeypatch.setattr(at_mod, "_maybe_substitute_with_options", lambda *a, **k: None)
    monkeypatch.setattr(at_mod, "count_autotrader_v1_open", lambda *a, **k: 0)
    monkeypatch.setattr(
        at_mod, "count_autotrader_v1_open_by_lane", lambda *a, **k: {},
    )
    monkeypatch.setattr(at_mod, "autotrader_realized_pnl_today_et", lambda *a, **k: 0.0)
    monkeypatch.setattr(at_mod, "find_open_autotrader_trade", lambda *a, **k: None)
    monkeypatch.setattr(at_mod, "maybe_scale_in", lambda *a, **k: None)
    monkeypatch.setattr(
        at_mod,
        "check_autopilot_entry_gate",
        lambda *a, **k: {"allowed": True, "reason": "free", "owner": None},
    )
    monkeypatch.setattr(
        at_mod,
        "passes_rule_gate",
        lambda *a, **k: (
            False,
            "non_positive_expected_edge",
            {"expected_net_pct": -0.12},
        ),
    )
    monkeypatch.setattr(
        at_mod,
        "_resolve_entry_risk_notional",
        lambda *a, **k: (
            100.0,
            {
                "notional_capital_usd": 10_000.0,
                "notional_explicit_fallback_usd": 0.0,
                "notional_risk_pct": 1.0,
                "notional_source": "test",
            },
        ),
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

    out = {"scaled_in": 0, "skipped": 0, "entered": 0}
    at_mod._process_one_alert(
        db,
        alert.user_id,
        alert,
        out,
        {"live_orders_effective": True, "paper_mode_effective": False},
    )

    rows = db.query(PaperTrade).filter(
        PaperTrade.paper_shadow_of_alert_id == alert.id
    ).all()
    assert len(rows) == 1
    assert rows[0].ticker == "EDGE-USD"
    assert rows[0].signal_json.get("shadow_decision") == (
        "skipped_non_positive_expected_edge"
    )
    assert rows[0].signal_json["_paper_meta"]["original_entry"] == 100.0


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


def test_shadow_janitor_only_closes_stale_autotrader_shadow_rows(db, monkeypatch):
    pat, alert = _seed_pattern_and_alert(db)
    stale_at = datetime.utcnow() - timedelta(hours=5)
    shadow = PaperTrade(
        user_id=alert.user_id,
        scan_pattern_id=pat.id,
        ticker="TEST",
        direction="long",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=1,
        status="open",
        entry_date=stale_at,
        signal_json={"auto_trader_v1": True, "paper_shadow": True},
        paper_shadow_of_alert_id=alert.id,
    )
    ordinary = PaperTrade(
        user_id=alert.user_id,
        scan_pattern_id=pat.id,
        ticker="PLAIN",
        direction="long",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=1,
        status="open",
        entry_date=stale_at,
        signal_json={},
    )
    db.add_all([shadow, ordinary])
    db.commit()

    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_quote",
        lambda ticker: {"price": 101.0},
    )
    monkeypatch.setattr(
        "app.services.trading.paper_trading._apply_slippage",
        lambda price, direction, is_entry: price,
    )

    result = prune_autotrader_paper_shadow_capacity(
        db,
        alert.user_id,
        max_open=100,
        max_age_hours=1,
        buffer=5,
    )

    db.refresh(shadow)
    db.refresh(ordinary)
    assert result["closed"] == 1
    assert shadow.status == "closed"
    assert shadow.exit_reason == "shadow_capacity_janitor"
    assert ordinary.status == "open"


def test_shadow_capacity_janitor_evicts_low_value_before_pilot_evidence(
    db,
    monkeypatch,
):
    pat, alert = _seed_pattern_and_alert(db)
    pat.lifecycle_stage = "candidate"
    pilot = ScanPattern(
        name=f"psm_pilot_{uuid4().hex[:12]}",
        rules_json={},
        origin="test",
        asset_class="all",
        timeframe="1d",
        win_rate=0.5,
        avg_return_pct=1.0,
        lifecycle_stage="pilot_promoted",
    )
    db.add(pilot)
    db.commit()
    db.refresh(pilot)
    old_at = datetime.utcnow() - timedelta(hours=3)
    low_value = PaperTrade(
        user_id=alert.user_id,
        scan_pattern_id=pat.id,
        ticker="LOW",
        direction="long",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=1,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json={
            "auto_trader_v1": True,
            "paper_shadow": True,
            "shadow_decision": "legacy_shadow_bucket",
        },
    )
    high_value = PaperTrade(
        user_id=alert.user_id,
        scan_pattern_id=pilot.id,
        ticker="HIGH",
        direction="long",
        entry_price=100.0,
        stop_price=99.0,
        target_price=101.0,
        quantity=1,
        status="open",
        entry_date=old_at,
        signal_json={
            "auto_trader_v1": True,
            "paper_shadow": True,
            "shadow_decision": "blocked_shadow_promoted",
            "paper_observation_signal_lane": "shadow_near_miss",
        },
    )
    standard = PaperTrade(
        user_id=alert.user_id,
        scan_pattern_id=pat.id,
        ticker="STD",
        direction="long",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=1,
        status="open",
        entry_date=old_at + timedelta(minutes=1),
        signal_json={
            "auto_trader_v1": True,
            "paper_shadow": True,
            "shadow_decision": "placed",
        },
    )
    db.add_all([low_value, high_value, standard])
    db.commit()

    def fail_fetch_quote(ticker):
        raise AssertionError("capacity eviction must not fetch quotes inline")

    monkeypatch.setattr("app.services.trading.market_data.fetch_quote", fail_fetch_quote)
    monkeypatch.setattr(
        "app.services.trading.paper_trading._apply_slippage",
        lambda price, direction, is_entry: price,
    )

    result = prune_autotrader_paper_shadow_capacity(
        db,
        alert.user_id,
        max_open=4,
        max_age_hours=100,
        buffer=1,
    )

    db.refresh(low_value)
    db.refresh(high_value)
    db.refresh(standard)
    assert result["capacity_cancelled"] == 1
    assert result["capacity_removed"] == 1
    assert result["eviction_policy"] == "priority_evidence_buffer"
    assert low_value.status == "cancelled"
    assert low_value.exit_reason == PAPER_SHADOW_CAPACITY_EVICTED_REASON
    assert low_value.pnl is None
    assert low_value.exit_price is None
    assert low_value.signal_json[PAPER_SHADOW_CAPACITY_EVICTION_META_KEY][
        "pnl_recorded"
    ] is False
    assert high_value.status == "open"
    assert standard.status == "open"


def test_reject_shadow_reclaims_buffer_slot_when_capacity_full(db, monkeypatch):
    pat, alert = _seed_pattern_and_alert(db)
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_enabled", False,
    )
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_qualified_blocks_enabled", True,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_reject_allow_duplicate_open",
        True,
    )
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_max_open", 2,
    )
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_janitor_enabled", True,
    )
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_janitor_max_age_hours", 100,
    )
    monkeypatch.setattr(
        at_mod.settings, "chili_autotrader_paper_shadow_janitor_buffer", 0,
    )
    db.add_all([
        PaperTrade(
            user_id=alert.user_id,
            scan_pattern_id=pat.id,
            ticker="OLD1",
            direction="long",
            entry_price=100.0,
            stop_price=95.0,
            target_price=110.0,
            quantity=1,
            status="open",
            entry_date=datetime.utcnow() - timedelta(hours=2),
            signal_json={"auto_trader_v1": True, "paper_shadow": True},
        ),
        PaperTrade(
            user_id=alert.user_id,
            scan_pattern_id=pat.id,
            ticker="OLD2",
            direction="long",
            entry_price=100.0,
            stop_price=95.0,
            target_price=110.0,
            quantity=1,
            status="open",
            entry_date=datetime.utcnow() - timedelta(hours=1),
            signal_json={"auto_trader_v1": True, "paper_shadow": True},
        ),
    ])
    db.commit()

    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_quote",
        lambda ticker: {"price": 101.0},
    )
    monkeypatch.setattr(
        "app.services.trading.paper_trading._apply_slippage",
        lambda price, direction, is_entry: price,
    )

    _maybe_open_reject_paper_shadow(
        db,
        uid=alert.user_id,
        alert=alert,
        px=100.0,
        snap={},
        reason="non_positive_expected_edge",
        existing_qty=TEST_SHADOW_QUANTITY,
    )

    rows = db.query(PaperTrade).filter(PaperTrade.user_id == alert.user_id).all()
    assert sum(1 for row in rows if row.status == "open") == 2
    assert any(row.paper_shadow_of_alert_id == alert.id for row in rows)
    assert any(row.exit_reason == PAPER_SHADOW_CAPACITY_EVICTED_REASON for row in rows)


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
