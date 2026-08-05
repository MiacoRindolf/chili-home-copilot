"""Unit tests for the Ross momentum-quality scorer (pure functions, no DB).

Validates the M2 selection core: explosive instruments (high RVOL + already
moving + low float) out-rank generic ones, and the bar is ADAPTIVE (percentile
within the batch), per docs/DESIGN/MOMENTUM_LANE.md §7.
"""

from app.services.trading.momentum_neural.ross_momentum import (
    ROSS_PILLAR_WEIGHTS,
    score_universe,
)


def _sig(vol_ratio=None, daily=None, gap=None, float_shares=None, market_cap=None):
    s = {}
    if vol_ratio is not None:
        s["vol_ratio"] = vol_ratio
    if daily is not None:
        s["daily_change_pct"] = daily
    if gap is not None:
        s["gap_pct"] = gap
    if float_shares is not None:
        s["float_shares"] = float_shares
    if market_cap is not None:
        s["market_cap"] = market_cap
    return s


def test_empty_universe_returns_empty():
    assert score_universe({}) == {}


def test_weights_sum_to_one():
    assert abs(sum(ROSS_PILLAR_WEIGHTS.values()) - 1.0) < 1e-9


def test_explosive_outranks_generic():
    sigs = {
        "ROCKET": _sig(vol_ratio=12.0, daily=45.0),
        "DULL": _sig(vol_ratio=1.1, daily=0.3),
        "MID": _sig(vol_ratio=3.0, daily=8.0),
    }
    res = score_universe(sigs)
    assert res["ROCKET"].rank == 1
    assert res["DULL"].rank == 3
    assert res["ROCKET"].score > res["MID"].score > res["DULL"].score


def test_percentile_is_adaptive_to_the_batch():
    # Same raw RVOL=4x is "top" in a quiet batch but "bottom" in a hot batch.
    quiet = score_universe(
        {"A": _sig(4.0, 5), "B": _sig(1.0, 1), "C": _sig(1.5, 2)}
    )
    hot = score_universe(
        {"A": _sig(4.0, 5), "B": _sig(20.0, 40), "C": _sig(15.0, 30)}
    )
    assert quiet["A"].rvol_pct > hot["A"].rvol_pct


def test_high_volume_dump_ranks_below_pump():
    # Both high RVOL; PUMP is up, DUMP is down -> PUMP must out-rank (long bias).
    res = score_universe(
        {"PUMP": _sig(10.0, 30.0), "DUMP": _sig(10.0, -25.0)}
    )
    assert res["PUMP"].rank == 1
    assert res["DUMP"].rank == 2
    assert res["PUMP"].momentum_pct > res["DUMP"].momentum_pct


def test_liquidity_absent_degrades_gracefully():
    res = score_universe({"X": _sig(5.0, 10.0), "Y": _sig(2.0, 3.0)})
    assert res["X"].liquidity_pct is None
    assert "liquidity" not in res["X"].breakdown["pillars_present"]
    assert res["X"].rank == 1


def test_low_float_is_more_explosive():
    # Equal RVOL + momentum; smaller float ranks higher (Ross: low float = fuel).
    res = score_universe(
        {
            "MICRO": _sig(8.0, 20.0, float_shares=3_000_000),
            "BIG": _sig(8.0, 20.0, float_shares=500_000_000),
        }
    )
    assert res["MICRO"].liquidity_pct > res["BIG"].liquidity_pct
    assert res["MICRO"].rank == 1


def test_crypto_symbol_handled():
    res = score_universe(
        {"BTC-USD": _sig(1.2, 1.0), "PEPE-USD": _sig(9.0, 35.0)}
    )
    assert res["PEPE-USD"].rank == 1


def test_in_top_fraction_is_adaptive_cutoff():
    sigs = {f"S{i}": _sig(float(i), float(i)) for i in range(1, 11)}
    res = score_universe(sigs)
    top = [s for s in res.values() if s.in_top_fraction(0.2)]
    assert len(top) == 2  # top 20% of 10
    assert all(s.rank <= 2 for s in top)


def _tilt_triplet():
    from app.services.trading.momentum_neural.context import (
        build_momentum_regime_context,
    )
    from app.services.trading.momentum_neural.features import (
        ExecutionReadinessFeatures,
    )
    from app.services.trading.momentum_neural.variants import iter_momentum_families
    from app.services.trading.momentum_neural.viability import score_viability

    ctx = build_momentum_regime_context(
        realized_vol_rank=None,
        atr_pct=None,
        meta={"ross_scores": {"HOT": 0.95, "COLD": 0.05}},
    )
    feats = ExecutionReadinessFeatures.from_meta({})
    fam = next(iter(iter_momentum_families()))
    return (
        score_viability("HOT", fam, ctx, feats, db=None),
        score_viability("COLD", fam, ctx, feats, db=None),
        score_viability("NOSCORE", fam, ctx, feats, db=None),
    )


def test_viability_tilt_prefers_explosive_setup(monkeypatch):
    """End-to-end wiring (M2): ang ross_scores na dumadaan sa ctx.meta ay
    nagti-tilt sa explosive at palayo sa dull.

    Dating iginigiit nito ang ``hot > neutral > cold`` — na ang walang ross_score
    ay strict no-op. Napalitan ang bahaging iyon ng `no_signal_derank_enabled`
    (config default TRUE): ang pangalang WALANG Ross signal ay sinasadyang
    ibinababa sa ILALIM ng mga scored mover, gaya mismo ng sinasabi ng babala
    nito ("de-ranked below scored movers"). Dahil hindi na-update ang test, pula
    ito mula nang mag-default ON ang derank.

    Ngayon ay parehong kontrata ang naka-pin — hindi lang ang bagong ugali — kaya
    ang epekto ng flag ay tahasang nababantayan sa halip na maipahiwatig.
    """
    from app.services.trading.momentum_neural import viability as v

    monkeypatch.setattr(v.settings, "chili_momentum_no_signal_derank_enabled", True, raising=False)
    hot, cold, neutral = _tilt_triplet()
    # Ang tilt mismo: buhay pa rin ang HOT vs COLD.
    assert hot.viability > cold.viability
    # At ang no-signal ay nasa ILALIM ng scored mover kahit mababa ang score nito.
    assert cold.viability > neutral.viability
    assert any("no ross momentum signal" in w.lower() for w in neutral.warnings)


def test_viability_tilt_no_signal_ay_no_op_kapag_naka_off_ang_derank(monkeypatch):
    """Kapag naka-OFF ang derank, bumabalik ang orihinal na kontrata: ang
    walang-signal ay hindi ginagalaw, kaya nasa PAGITAN ito ng hot at cold."""
    from app.services.trading.momentum_neural import viability as v

    monkeypatch.setattr(v.settings, "chili_momentum_no_signal_derank_enabled", False, raising=False)
    hot, cold, neutral = _tilt_triplet()
    assert hot.viability > neutral.viability > cold.viability
    assert not any("de-ranked" in w.lower() for w in neutral.warnings)


def test_crypto_breakout_schema_keys_work():
    """The crypto-breakout cache (the live crypto source) uses 'rvol'/'change_24h',
    NOT 'vol_ratio'/'daily_change_pct'. The scorer must read those equivalent
    keys — the M2 deploy found crypto viability flat at base because of this exact
    key mismatch (no signal -> no tilt)."""
    res = score_universe(
        {
            "HOT-USD": {"rvol": 9.0, "change_24h": 30.0},
            "COLD-USD": {"rvol": 0.5, "change_24h": -2.0},
        }
    )
    assert res["HOT-USD"].rank == 1
    assert res["COLD-USD"].rank == 2
    assert res["HOT-USD"].rvol_pct > res["COLD-USD"].rvol_pct
    # must NOT be flat — the bug was both scoring identically at base
    assert res["HOT-USD"].score != res["COLD-USD"].score


def test_liquidity_biased_weights_lift_fillable_names():
    """ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED adds the tradeable_liquidity pillar
    (dollar turnover -> tighter spread -> FILLABLE): a liquid mover gains rank
    pressure vs the baseline; the default weights stay byte-identical (no
    tradeable_liquidity term -> baseline unchanged)."""
    from app.services.trading.momentum_neural.ross_momentum import (
        ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
        score_universe,
    )

    sig = {
        # explosive but illiquid: tiny float, low $-vol (wide spread, never fills)
        "EXPLO": {"rvol": 12.0, "daily_change_pct": 40.0, "float_shares": 2_000_000,
                  "price": 3.0, "volume": 1_500_000},
        # less explosive but liquid: high $-vol (tight spread, fillable)
        "LIQ": {"rvol": 6.0, "daily_change_pct": 18.0, "float_shares": 40_000_000,
                "price": 12.0, "volume": 30_000_000},
    }
    base = score_universe(sig)
    biased = score_universe(sig, weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED)

    # default weights ignore the new pillar entirely (weight absent -> not blended)
    assert base["EXPLO"].score == 1.0
    # ANG PAGSASARA NG AGWAT ANG SINUSUKAT, HINDI ANG ABSOLUTONG SCORE.
    # Dating iginigiit nito ang `biased["LIQ"].score > base["LIQ"].score`. Naging
    # imposible iyon nang dumating ang 3-layer explosive scorer (9b52d30, ang
    # PLSM #15 compression fix), na nag-normalize ng huling score sa loob ng
    # batch: sa dalawang pangalan, ang mas mababa ay LAGING eksaktong 0.0
    # anuman ang timbang, kaya 0.0 > 0.0 ang naging assertion. Ang INTENSYON —
    # inaangat ng liquidity bias ang fillable na pangalan patungo sa explosive —
    # ay buhay pa rin at nasusukat sa AGWAT.
    assert biased["LIQ"].tradeable_liquidity_pct == 1.0
    base_gap = base["EXPLO"].score - base["LIQ"].score
    biased_gap = biased["EXPLO"].score - biased["LIQ"].score
    assert biased_gap < base_gap, (base_gap, biased_gap)
    assert biased["EXPLO"].score < base["EXPLO"].score
    # explosiveness still leads (rvol+momentum dominate the blend) — bias, not flip
    assert biased["EXPLO"].rank == 1
