"""Ross gap #16: SEC EDGAR dilution-risk penalty (videos 06/36). A recent S-1/424B*
(registration / offering) filing means a low-float will ISSUE SHARES and fade despite
good news (CTNT vs SNTI). Such names get a viability penalty. Pure-logic + monkeypatched
fetch tests (no live SEC call), plus the viability integration.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.trading.momentum_neural import edgar
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.edgar import _recent_dilution_in, dilution_risk_symbols
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viability import score_viability


# ── pure _recent_dilution_in ─────────────────────────────────────────────────

def test_recent_dilution_detected():
    forms = ["8-K", "424B5", "4"]
    dates = ["2026-06-01", "2026-06-10", "2026-06-12"]
    assert _recent_dilution_in(forms, dates, "2026-06-05") is True   # 424B5 on 06-10 >= cutoff


def test_s1_detected():
    assert _recent_dilution_in(["S-1"], ["2026-06-10"], "2026-06-05") is True


def test_old_dilution_ignored():
    assert _recent_dilution_in(["424B5"], ["2026-01-01"], "2026-06-05") is False


def test_non_dilution_forms_ignored():
    assert _recent_dilution_in(["8-K", "10-Q", "4", "144"], ["2026-06-10"] * 4, "2026-06-05") is False


# ── dilution_risk_symbols (monkeypatched fetch) ──────────────────────────────

def test_dilution_set_skips_crypto_and_unknown(monkeypatch):
    monkeypatch.setattr(edgar, "_cik_for", lambda t: "0000000001" if t == "DILU" else None)
    monkeypatch.setattr(edgar, "_has_recent_dilution", lambda cik, **k: True)
    edgar._dilution_cache.clear()
    out = dilution_risk_symbols(["DILU", "CLEAN", "BTC-USD"])
    assert out == {"DILU"}   # CLEAN -> no CIK; BTC-USD -> crypto, skipped before lookup


def test_dilution_lookups_are_bounded(monkeypatch):
    monkeypatch.setattr(edgar, "_cik_for", lambda t: "0000000001")
    monkeypatch.setattr(edgar, "_has_recent_dilution", lambda cik, **k: True)
    edgar._dilution_cache.clear()
    out = dilution_risk_symbols([f"T{i}" for i in range(60)], max_lookups=10)
    assert len(out) == 10   # only 10 NEW lookups this pass; the rest warm next pass


# ── viability penalty ────────────────────────────────────────────────────────

def _ctx(dil=None):
    meta = {"spread_regime": "tight"}
    if dil is not None:
        meta["dilution_symbols"] = dil
    return build_momentum_regime_context(
        now=datetime(2026, 4, 7, 17, 0, tzinfo=timezone.utc), atr_pct=0.018, meta=meta
    )


def _feats():
    return ExecutionReadinessFeatures(spread_bps=4.0, slippage_estimate_bps=4.0, fee_to_target_ratio=0.08)


def test_dilution_filer_is_penalized():
    fam = get_family("vwap_reclaim_continuation")
    base = score_viability("ABCD", fam, _ctx(), _feats()).viability
    pen = score_viability("ABCD", fam, _ctx(dil=["ABCD"]), _feats()).viability
    assert pen < base
    assert abs((base - pen) - 0.10) < 1e-9


def test_non_filer_and_crypto_unaffected():
    fam = get_family("vwap_reclaim_continuation")
    base = score_viability("ABCD", fam, _ctx(), _feats()).viability
    assert score_viability("ABCD", fam, _ctx(dil=["WXYZ"]), _feats()).viability == base
    cbase = score_viability("BTC-USD", fam, _ctx(), _feats()).viability
    assert score_viability("BTC-USD", fam, _ctx(dil=["BTC-USD"]), _feats()).viability == cbase
