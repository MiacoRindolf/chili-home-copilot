"""Live OFI + micro-price viability tilt (the wired-LIVE L2 signal) + compute math.

OFI (Cont/Kukanov/Stoikov) is the research's top L2 short-horizon predictor; here
it is USED as a small agreement-guarded long-bias selection tilt on the viability
score (not log-only), validated by live A/B. These tests pin: the OFI/micro-price
math is directionally correct + bounded; the tilt fires only on OFI<->micro-price
AGREEMENT (spoof/flicker guard); it is a no-op when the signal is absent
(backward-compatible) or below threshold or weight-disabled (env rollback lever).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.config import settings
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viability import score_viability
from app.services.trading.momentum_neural.pipeline import _compute_ofi_micro


def _ctx():
    return build_momentum_regime_context(
        now=datetime(2026, 4, 7, 17, 0, tzinfo=timezone.utc),
        atr_pct=0.018,
        meta={"spread_regime": "tight"},
    )


def _feats(**kw):
    base = dict(spread_bps=4.0, slippage_estimate_bps=4.0, fee_to_target_ratio=0.08)
    base.update(kw)
    return ExecutionReadinessFeatures(**base)


# ── OFI / micro-price computation ─────────────────────────────────────

def test_compute_ofi_directional_and_bounded():
    bull = [(100.0, 5, 101.0, 5), (100.5, 6, 101.5, 4), (101.0, 7, 102.0, 3)]  # bid up, ask retreats
    bear = [(100.0, 5, 101.0, 5), (99.5, 4, 100.5, 6), (99.0, 3, 100.0, 7)]     # bid down, ask presses
    ob, mb = _compute_ofi_micro(bull)
    oe, me = _compute_ofi_micro(bear)
    assert ob is not None and ob > 0 and -1.0 <= ob <= 1.0
    assert mb is not None and mb > 0
    assert oe is not None and oe < 0 and -1.0 <= oe <= 1.0
    assert me is not None and me < 0


def test_compute_edge_cases():
    assert _compute_ofi_micro([]) == (None, None)
    o, m = _compute_ofi_micro([(100.0, 5, 101.0, 5)])  # one snap: no OFI, balanced micro
    assert o is None and m == 0.0


def test_compute_micro_edge_sign():
    _, m_bid = _compute_ofi_micro([(100.0, 10.0, 101.0, 1.0)])  # bid-heavy -> micro above mid
    _, m_ask = _compute_ofi_micro([(100.0, 1.0, 101.0, 10.0)])  # ask-heavy -> micro below mid
    assert m_bid > 0
    assert m_ask < 0


# ── viability tilt ────────────────────────────────────────────────────

def test_tilt_fires_on_bullish_agreement():
    fam = get_family("vwap_reclaim_continuation")
    ctx = _ctx()
    base = score_viability("ETH-USD", fam, ctx, _feats()).viability
    bull = score_viability("ETH-USD", fam, ctx, _feats(ofi=0.5, micro_price_edge=8.0)).viability
    assert bull > base


def test_tilt_penalizes_bearish_agreement():
    fam = get_family("vwap_reclaim_continuation")
    ctx = _ctx()
    base = score_viability("ETH-USD", fam, ctx, _feats()).viability
    bear = score_viability("ETH-USD", fam, ctx, _feats(ofi=-0.5, micro_price_edge=-8.0))
    assert bear.viability < base
    assert any("order-flow" in w.lower() for w in bear.warnings)


def test_tilt_noop_on_disagreement():
    # OFI bullish but micro-price bearish -> agreement guard suppresses the tilt
    fam = get_family("vwap_reclaim_continuation")
    ctx = _ctx()
    base = score_viability("ETH-USD", fam, ctx, _feats()).viability
    dis = score_viability("ETH-USD", fam, ctx, _feats(ofi=0.5, micro_price_edge=-8.0)).viability
    assert dis == base


def test_tilt_noop_when_signal_absent():
    # backward compatibility: no L2 (ofi/micro None) -> viability byte-identical
    fam = get_family("vwap_reclaim_continuation")
    ctx = _ctx()
    base = score_viability("ETH-USD", fam, ctx, _feats()).viability
    none = score_viability("ETH-USD", fam, ctx, _feats(ofi=None, micro_price_edge=None)).viability
    assert none == base


def test_tilt_noop_below_threshold():
    fam = get_family("vwap_reclaim_continuation")
    ctx = _ctx()
    base = score_viability("ETH-USD", fam, ctx, _feats()).viability
    weak = score_viability("ETH-USD", fam, ctx, _feats(ofi=0.1, micro_price_edge=2.0)).viability
    assert weak == base  # |OFI| 0.1 < threshold 0.25 -> no tilt


def test_tilt_disabled_when_weight_zero(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_ofi_tilt_weight", 0.0)
    fam = get_family("vwap_reclaim_continuation")
    ctx = _ctx()
    base = score_viability("ETH-USD", fam, ctx, _feats()).viability
    strong = score_viability("ETH-USD", fam, ctx, _feats(ofi=0.9, micro_price_edge=20.0)).viability
    assert strong == base  # weight 0 = env rollback lever, tilt off


# ── features round-trip ───────────────────────────────────────────────

def test_features_roundtrip_includes_ofi_microprice():
    f = ExecutionReadinessFeatures.from_meta({"ofi": 0.4, "micro_price_edge": 5.5})
    assert f.ofi == 0.4 and f.micro_price_edge == 5.5
    d = f.to_public_dict()
    assert d["ofi"] == 0.4 and d["micro_price_edge"] == 5.5
