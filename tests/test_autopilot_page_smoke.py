"""P1 smoke — Autopilot page + endpoints + pattern tagging + no-op safety.

Covers the whole `/trading/autopilot` surface before we go live:

1. Page renders 200 for paired and guest (no template explosion).
2. Each endpoint called by `autopilot.js` / `autopilot-sessions.js` /
   `autopilot-pattern-desk.js` returns a non-500 status when hit as paired.
3. Pattern tagging: running the orchestrator on a ``pattern_imminent``
   ``BreakoutAlert`` writes a PaperTrade with ``scan_pattern_id`` set **and**
   ``signal_json.auto_trader_v1=True`` + ``signal_json.breakout_alert_id``.
4. Orchestrator and monitor tick functions no-op when
   ``chili_autotrader_enabled`` is off (no DB writes, no broker calls).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models.trading import AutoTraderRun, BreakoutAlert, PaperTrade, ScanPattern
from app.services.trading.auto_trader import run_auto_trader_tick
from app.services.trading.auto_trader_monitor import tick_auto_trader_monitor


# ────────────────────────────── helpers ──────────────────────────────


def _ok_status(status: int) -> bool:
    """Acceptable: 2xx, 4xx (guards/no data/guest). Not acceptable: 5xx."""
    return 200 <= status < 500


# ──────────────────────── 1. Page renders ────────────────────────────


def test_autopilot_page_paired_renders(paired_client) -> None:
    c, _user = paired_client
    r = c.get("/trading/autopilot")
    assert r.status_code == 200, r.text[:400]
    body = r.text
    assert "Trading Autopilot" in body
    assert "ap-pattern-desk-section" in body  # pattern desk include wired
    assert "ap-eligible-section" in body
    assert "autopilot-pattern-desk.js" in body


def test_autopilot_page_guest_renders(client) -> None:
    r = client.get("/trading/autopilot")
    assert r.status_code == 200, r.text[:400]
    assert "Trading Autopilot" in r.text


def test_autopilot_legacy_alias_renders(paired_client) -> None:
    c, _user = paired_client
    r = c.get("/trading/automation")
    assert r.status_code == 200, r.text[:400]
    assert "Trading Autopilot" in r.text
    assert "compatibility path" in r.text.lower()


# ──────────────────────── 2. Endpoint smoke ───────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        # Desk
        "/api/trading/autotrader/desk",
        # Momentum — opportunities + summary strip + decision ledger
        "/api/trading/momentum/opportunities?mode=paper&asset_class=all&limit=5",
        "/api/trading/momentum/automation/summary",
        "/api/trading/momentum/automation/sessions?limit=5",
        "/api/trading/momentum/automation/decisions/recent?limit=3",
        "/api/trading/momentum/automation/decisions/abstentions/recent?limit=3",
        "/api/trading/momentum/automation/deployment/summary",
    ],
)
def test_autopilot_endpoints_paired_non_500(paired_client, path: str) -> None:
    c, _user = paired_client
    r = c.get(path)
    assert _ok_status(r.status_code), (
        f"{path} returned {r.status_code}: {r.text[:400]}"
    )


# ────────────────────── 3. Pattern tagging ────────────────────────────


def test_orchestrator_paper_tags_scan_pattern_and_alert(
    paired_client, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BreakoutAlert -> paper_trade carries scan_pattern_id + auto_trader_v1 tag."""
    _c, user = paired_client

    sp = ScanPattern(
        name="smoke_pattern",
        rules_json={"conditions": []},
        origin="user",
        asset_class="stock",
        timeframe="1d",
    )
    db.add(sp)
    db.flush()

    ba = BreakoutAlert(
        ticker="SMOKE",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.5,
        target_price=11.5,
        user_id=user.id,
        scan_pattern_id=sp.id,
    )
    db.add(ba)
    db.commit()

    from app.config import settings as _s

    monkeypatch.setattr(_s, "chili_autotrader_enabled", True)
    monkeypatch.setattr(_s, "chili_autotrader_live_enabled", False)
    monkeypatch.setattr(_s, "chili_autotrader_llm_revalidation_enabled", False)
    monkeypatch.setattr(_s, "chili_autotrader_rth_only", False)
    monkeypatch.setattr(_s, "chili_autotrader_user_id", user.id)

    with patch(
        "app.services.trading.auto_trader._current_price", return_value=10.05
    ), patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, "ok"),
    ):
        result = run_auto_trader_tick(db)

    assert result.get("ok") is True, result

    pt = db.query(PaperTrade).filter(PaperTrade.ticker == "SMOKE").first()
    assert pt is not None, "paper trade not created"
    assert pt.scan_pattern_id == sp.id, "scan_pattern_id not tagged"
    sj = pt.signal_json or {}
    assert sj.get("auto_trader_v1") is True
    assert sj.get("breakout_alert_id") == ba.id

    # audit row present
    audit = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == ba.id)
        .first()
    )
    assert audit is not None
    assert audit.decision == "placed"
    assert audit.scan_pattern_id == sp.id


# ────────────────────── 4. No-op when disabled ─────────────────────────


def test_orchestrator_noop_when_disabled(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings as _s

    monkeypatch.setattr(_s, "chili_autotrader_enabled", False)

    before = db.query(AutoTraderRun).count()
    result = run_auto_trader_tick(db)
    after = db.query(AutoTraderRun).count()

    assert result == {"ok": True, "skipped": True, "reason": "disabled"}
    assert before == after, "disabled orchestrator tick wrote audit rows"


def test_monitor_noop_when_disabled(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings as _s

    monkeypatch.setattr(_s, "chili_autotrader_enabled", False)

    result = tick_auto_trader_monitor(db)
    assert result == {"ok": True, "skipped": "autotrader_disabled"}
