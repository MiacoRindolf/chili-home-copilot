"""Time-share runner escape (2026-08-17, operator decision Option C).

Ang alpaca_spot adaptive-risk sizing ay nangangailangan ng capture provider na
tanging ang sealed captured-paper service ang nakakapag-install — kaya ang
ordinaryong time-share lane ay istrukturang hindi makapag-post ng entry
(`builder_missing_capture_binding` sa bawat attempt, buong umaga ng 08-17).

Ang escape: kapag ang `chili_momentum_legacy_alpaca_dispatch_enabled` ay ON at
WALANG installed capture provider, ang lane ay tumatakbo sa LEGACY sizing —
(a) walang raise sa adaptive builder, (b) tinatanggap ng claim/place seams ang
kawalan ng adaptive triple, at (c) ang `alpaca_paper_hard_loss_cap_usd` ay
nagbabalik ng TUNAY na equity-relative na per-trade cap. Kapag MAY provider
(sealed/replay/tests), ang adaptive path ang laging mananaig.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
import app.services.trading.momentum_neural.live_runner as lr
import app.services.trading.momentum_neural.risk_policy as rp
from app.services.trading.momentum_neural.adaptive_risk_request_builder import (
    AdaptiveRiskBuilderError,
    adaptive_risk_capture_provider_installed,
    adaptive_risk_source_provider,
)


def test_escape_inactive_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", False)
    assert lr._legacy_alpaca_timeshare_sizing_active() is False


def test_escape_active_when_flag_on_and_no_provider(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    assert adaptive_risk_capture_provider_installed() is False
    assert lr._legacy_alpaca_timeshare_sizing_active() is True


def test_escape_yields_to_installed_provider(monkeypatch):
    """Kapag may provider (sealed/replay), ang adaptive path ang mananaig."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    with adaptive_risk_source_provider(lambda **kw: None):
        assert adaptive_risk_capture_provider_installed() is True
        assert lr._legacy_alpaca_timeshare_sizing_active() is False
    assert lr._legacy_alpaca_timeshare_sizing_active() is True


def _le_long():
    return {"position_side": "long", "structural_stop_price": 7.5}


def test_builder_returns_none_triple_under_escape(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    sess = SimpleNamespace(id=1, correlation_id="c", symbol="IPST",
                           execution_family="alpaca_spot")
    built, place_n, cid = lr._build_adaptive_alpaca_primary_before_legacy_sizing(
        sess, _le_long(), execution_family="alpaca_spot", bid=7.9, ask=8.0,
    )
    assert built is None and place_n is None and cid is None


def test_builder_still_raises_without_escape(monkeypatch):
    """Flag OFF → ang orihinal na hard requirement ay buo (walang provider → raise)."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", False)
    sess = SimpleNamespace(id=1, correlation_id="c", symbol="IPST",
                           execution_family="alpaca_spot")
    try:
        lr._build_adaptive_alpaca_primary_before_legacy_sizing(
            sess, _le_long(), execution_family="alpaca_spot", bid=7.9, ask=8.0,
        )
    except AdaptiveRiskBuilderError as exc:
        assert getattr(exc, "reason", str(exc.args[0])) == "builder_missing_capture_binding"
    else:
        raise AssertionError("dapat nag-raise nang walang escape")


def test_builder_short_still_raises_under_escape(monkeypatch):
    """Ang escape ay long-alpaca_spot lang — ang short ay bawal pa rin."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    sess = SimpleNamespace(id=1, correlation_id="c", symbol="IPST",
                           execution_family="alpaca_short")
    try:
        lr._build_adaptive_alpaca_primary_before_legacy_sizing(
            sess, {"position_side": "short"}, execution_family="alpaca_short",
            bid=7.9, ask=8.0,
        )
    except AdaptiveRiskBuilderError:
        pass
    else:
        raise AssertionError("dapat nag-raise para sa short")


def test_hard_loss_cap_none_on_adaptive_path(monkeypatch):
    """Walang escape → None pa rin (adaptive parity ang may-ari ng sizing)."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", False)
    assert rp.alpaca_paper_hard_loss_cap_usd("alpaca_spot") is None


def test_hard_loss_cap_real_under_escape(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    with patch.object(rp, "equity_relative_loss_cap", return_value=125.0):
        cap = rp.alpaca_paper_hard_loss_cap_usd("alpaca_spot")
    assert cap == 125.0


def test_hard_loss_cap_fail_open_to_none_on_zero(monkeypatch):
    """Kapag hindi mabasa ang equity (cap 0/None) → None (walang pekeng ceiling)."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    with patch.object(rp, "equity_relative_loss_cap", return_value=0.0):
        assert rp.alpaca_paper_hard_loss_cap_usd("alpaca_spot") is None


def test_hard_loss_cap_non_alpaca_none(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    assert rp.alpaca_paper_hard_loss_cap_usd("coinbase_spot") is None
