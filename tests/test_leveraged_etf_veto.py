"""Fix A: hard-veto leveraged/inverse ETFs at the viability eligibility gate.

SOXS (3x-inverse semis) and the Tradr/Defiance/T-REX "2X Short XXX" single-stock wave
cleared every numeric gate and armed in a lane meant for low-float COMMON stock
(11 of 18 eligible names were these on 2026-06-23). The veto must force BOTH
eligibility flags False for an ETF, while real companies (VTAK/ICCM/ORIS) stay
eligible. The adaptive name classifier (leveraged_etf.symbol_is_leveraged_etf, which
resolves the security name via cached fundamentals) is monkeypatched here so the test
is deterministic and offline.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.trading.momentum_neural import viability as V
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viability import score_viability

# Names the (monkeypatched) classifier treats as leveraged/inverse ETFs.
_ETFS = {"SOXS", "SSG", "IREZ", "SNDQ", "NBIZ", "INFH", "MUZ", "LITZ", "DAMD", "SMCZ"}


def _ctx():
    return build_momentum_regime_context(
        now=datetime(2026, 4, 7, 17, 0, tzinfo=timezone.utc),
        atr_pct=0.018,
        meta={"spread_regime": "tight"},
    )


def _feats():
    # Clean execution: absent the veto, the symbol would be live-eligible.
    return ExecutionReadinessFeatures(spread_bps=4.0, slippage_estimate_bps=4.0, fee_to_target_ratio=0.08)


@pytest.fixture(autouse=True)
def _patch_classifier(monkeypatch):
    # Deterministic + offline — no yfinance fundamentals lookup in the test.
    monkeypatch.setattr(
        V, "symbol_is_leveraged_etf",
        lambda s, *a, **k: str(s or "").upper() in _ETFS,
    )


@pytest.mark.parametrize("sym", ["SOXS", "IREZ", "LITZ", "INFH", "SNDQ"])
def test_veto_blocks_leveraged_etf(sym):
    fam = get_family("vwap_reclaim_continuation")
    res = score_viability(sym, fam, _ctx(), _feats())
    assert res.live_eligible is False
    assert res.paper_eligible is False
    assert res.viability == 0.0
    assert res.regime_fit == "leveraged_inverse_etf_vetoed"


@pytest.mark.parametrize("sym", ["VTAK", "ICCM", "ORIS", "NXTS", "BOLD", "GITS"])
def test_parity_real_companies_eligible(sym):
    # REQUIRED parity: a legit low-float common must NEVER be vetoed.
    fam = get_family("vwap_reclaim_continuation")
    res = score_viability(sym, fam, _ctx(), _feats())
    assert res.paper_eligible is True
    assert res.regime_fit != "leveraged_inverse_etf_vetoed"


def test_crypto_never_vetoed():
    # The classifier short-circuits -USD; crypto must be untouched (parity).
    fam = get_family("vwap_reclaim_continuation")
    res = score_viability("BTC-USD", fam, _ctx(), _feats())
    assert res.regime_fit != "leveraged_inverse_etf_vetoed"


def test_kill_switch_reverts(monkeypatch):
    # CHILI_MOMENTUM_EXCLUDE_LEVERAGED_ETFS=0 disables the veto -> ETF eligible again.
    monkeypatch.setattr(V.settings, "chili_momentum_exclude_leveraged_etfs", False, raising=False)
    fam = get_family("vwap_reclaim_continuation")
    res = score_viability("SOXS", fam, _ctx(), _feats())
    assert res.regime_fit != "leveraged_inverse_etf_vetoed"
