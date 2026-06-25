"""Baseline-lock for the Ross ``score_universe`` COMPRESSION bug (pre-fix).

This file is the PARITY FIXTURE + baseline lock for the explosive-scoring fix
(flag ``chili_momentum_explosive_scoring_enabled``, not yet implemented). It does
NOT exercise the new scorer — it pins the CURRENT (flag-off) behaviour so the fix
can prove additivity (flag-off byte-identical) and efficacy (flag-on flips the
ranking) against a frozen reference.

THE BUG (researched + proven, see the project notes / 2026-06-24 batch):
``ross_momentum.score_universe`` COMPRESSES scores. Two compounding mechanisms:

  1. ``_percentile_rank`` maps the batch maximum (even a tied cluster) to ~1.0, so a
     15,000x RVOL and a 6x RVOL collapse into the same upper band — *magnitude is
     destroyed*.
  2. The score is a linear weighted-AVERAGE of percentiles (fully *compensatory*),
     so an extreme axis is averaged back toward the batch mean by the mid pillars.

Net effect on the live 2026-06-24 batch (471 names): a non-explosive MEGA-CAP
(MU: ~2.5x RVOL, +10%, ~1B float) scored ~0.665 and OUT-RANKED a +400% /
15,000x-RVOL / recent-IPO rocket (PLSM: ~0.651, rank #15 of 471) — exactly the
100-1000% mover the lane exists to catch. The math averages PLSM's explosiveness
away; the mega-cap's broad-field-relative momentum + fillable dollar-volume
(``tradeable_liquidity``, the live lane's weight-set) lift it ABOVE the rocket.

This fixture reproduces that compression in a *small synthetic batch* and asserts
it, locking the baseline. When the new 3-layer scorer lands behind the flag:
  * flag OFF  -> these assertions must STILL hold (byte-identical / additive), and
  * flag ON   -> PLSM (tier 3) must rank top-3 and MU (tier 0) must NOT out-rank it.

Pure functions, no DB — the fixture is plain dicts of scanner-shaped signals.
"""

from app.services.trading.momentum_neural.ross_momentum import (
    ROSS_PILLAR_WEIGHTS,
    ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
    score_universe,
)


# The explosive scorer is gated behind ``chili_momentum_explosive_scoring_enabled``
# (default True per the no-dark-flags policy). The baseline-lock tests below pass
# ``explosive=False`` EXPLICITLY so they pin the legacy (flag-OFF) path regardless of
# the live config default — that is the byte-identical additivity reference. The
# flag-ON tests at the bottom prove the fix flips the ranking.


# ── Fixture shape ─────────────────────────────────────────────────────────────
# 25-name synthetic batch mirroring the 471-name 2026-06-24 distribution at scale:
#   * PLSM  — the explosive recent IPO: RVOL ~15,000x, change ~+400%, vendor float
#             50M shares. Thin tape (low $-volume).
#   * MU    — the non-explosive mega-cap: RVOL ~2.5x, change ~+10%, float ~1B shares.
#             Deep tape (huge $-volume => top ``tradeable_liquidity``).
#   * R0..R4  (5 "rockets") — strictly ABOVE PLSM on RVOL *and* change with even
#             tinier floats, so PLSM's RVOL/momentum percentiles SATURATE below 1.0
#             (the magnitude-destruction) and its small-float ``liquidity`` pillar is
#             NOT top-of-batch (many names are smaller-float still).
#   * D0..D17 (18 "dull" names) — strictly BELOW MU on RVOL *and* change (so MU's
#             RVOL/momentum percentiles sit HIGH relative to the broad flat field),
#             with floats spanning small-cap..mega-cap and modest $-volume.
# The keys are exactly what ``scanner._score_ticker_intraday`` emits (``vol_ratio`` /
# ``daily_change_pct`` / ``gap_pct`` / ``float_shares``) plus ``price``/``volume`` so
# the ``tradeable_liquidity`` pillar (price*volume) is computed for the biased set.


def _build_batch() -> dict[str, dict]:
    s: dict[str, dict] = {}
    s["PLSM"] = {
        "vol_ratio": 15000.0, "daily_change_pct": 400.0, "gap_pct": 380.0,
        "float_shares": 50_000_000, "price": 5.0, "volume": 10_000_000,
    }
    s["MU"] = {
        "vol_ratio": 2.5, "daily_change_pct": 10.0, "gap_pct": 8.0,
        "float_shares": 1_000_000_000, "price": 120.0, "volume": 50_000_000,
    }
    # 5 rockets strictly above PLSM on rvol+change, floats SMALLER than PLSM's 50M,
    # thin $-volume (so PLSM's small-float liquidity pillar is mid, not top).
    for i in range(5):
        s[f"R{i}"] = {
            "vol_ratio": 15500.0 + i * 200,
            "daily_change_pct": 405.0 + i * 5,
            "gap_pct": 385.0 + i * 5,
            "float_shares": 5_000_000 + i * 3_000_000,
            "price": 2.0 + i * 0.2,
            "volume": 2_000_000 + i * 200_000,
        }
    # 18 dull names strictly below MU on rvol+change; floats span small..mega-cap so
    # PLSM's 50M is NOT a top liquidity percentile; $-volume modest (below MU).
    dull_float = [
        8_000_000, 12_000_000, 15_000_000, 20_000_000, 25_000_000, 30_000_000,
        35_000_000, 40_000_000, 45_000_000, 60_000_000, 800_000_000,
        1_500_000_000, 2_500_000_000, 3_000_000_000, 18_000_000, 22_000_000,
        28_000_000, 33_000_000,
    ]
    for i in range(18):
        s[f"D{i}"] = {
            "vol_ratio": round(0.5 + 1.8 * i / 18, 3),
            "daily_change_pct": round(-1.0 + 10.0 * i / 18, 3),
            "gap_pct": round(-2.0 + 9.0 * i / 18, 3),
            "float_shares": dull_float[i],
            "price": 15.0 + i,
            "volume": 3_000_000 + i * 400_000,
        }
    return s


# Frozen reference: the baseline numbers observed on the current (flag-off) scorer.
# These are the lock — the fix must keep flag-off output byte-identical to these.
BASELINE_DEFAULT = {
    "PLSM": {"rank": 6, "score": 0.696},
    "MU":   {"rank": 9, "score": 0.640},
}
BASELINE_LIQUIDITY_BIASED = {
    "PLSM": {"rank": 11, "score": 0.644},
    "MU":   {"rank": 7,  "score": 0.706},
}


def test_fixture_shape():
    """The synthetic batch is the documented 25-name shape (PLSM + MU + 5 rockets +
    18 dull) and carries the scanner-emitted pillar keys."""
    batch = _build_batch()
    assert len(batch) == 25
    assert "PLSM" in batch and "MU" in batch
    assert sum(1 for k in batch if k.startswith("R")) == 5
    assert sum(1 for k in batch if k.startswith("D")) == 18
    for sym in ("PLSM", "MU"):
        for key in ("vol_ratio", "daily_change_pct", "gap_pct", "float_shares"):
            assert key in batch[sym]
    # PLSM is the explosive IPO; MU the non-explosive mega-cap.
    assert batch["PLSM"]["vol_ratio"] >= 10000.0
    assert batch["PLSM"]["daily_change_pct"] >= 100.0
    assert batch["MU"]["vol_ratio"] <= 5.0
    assert batch["MU"]["float_shares"] >= 500_000_000


def test_baseline_compression_default_weights():
    """DEFAULT weights (rvol/momentum/liquidity): PLSM's 15,000x / +400% magnitude is
    COMPRESSED into the [0.6, 0.75] band rather than separating to ~1.0, and the
    non-explosive mega-cap MU lands in the SAME compressed band — the magnitude
    destruction + compensatory averaging the fix targets."""
    res = score_universe(_build_batch(), explosive=False)
    plsm, mu = res["PLSM"], res["MU"]

    # The rocket is squashed into the compression band (NOT a runaway top score).
    assert 0.6 <= plsm.score <= 0.75, plsm.score
    # The non-explosive mega-cap lands in the SAME band (compression: no separation).
    assert 0.6 <= mu.score <= 0.75, mu.score

    # Exact frozen baseline (the byte-identical lock for flag-off additivity).
    assert plsm.rank == BASELINE_DEFAULT["PLSM"]["rank"]
    assert plsm.score == BASELINE_DEFAULT["PLSM"]["score"]
    assert mu.rank == BASELINE_DEFAULT["MU"]["rank"]
    assert mu.score == BASELINE_DEFAULT["MU"]["score"]

    # Compression evidence: the two are within a hair despite a ~6000x raw-RVOL gap
    # and a ~40x change gap — the scorer cannot tell the rocket from the mega-cap.
    assert abs(plsm.score - mu.score) <= 0.10


def test_baseline_megacap_outranks_rocket_liquidity_biased():
    """LIQUIDITY-BIASED weights (the LIVE lane's weight-set): the non-explosive
    mega-cap MU STRICTLY OUT-RANKS the +400% / 15,000x rocket PLSM — the exact
    documented bug. MU's deep dollar-volume tops the ``tradeable_liquidity`` pillar
    while PLSM's explosiveness is averaged away; both stay in the [0.6, 0.75]
    compression band."""
    res = score_universe(
        _build_batch(), weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED, explosive=False
    )
    plsm, mu = res["PLSM"], res["MU"]

    # THE BUG: a non-explosive mega-cap out-ranks the explosive rocket.
    assert mu.rank < plsm.rank, (mu.rank, plsm.rank)
    assert mu.score > plsm.score, (mu.score, plsm.score)

    # Both squashed into the compression band.
    assert 0.6 <= plsm.score <= 0.75, plsm.score
    assert 0.6 <= mu.score <= 0.75, mu.score

    # Exact frozen baseline (the byte-identical lock for flag-off additivity).
    assert plsm.rank == BASELINE_LIQUIDITY_BIASED["PLSM"]["rank"]
    assert plsm.score == BASELINE_LIQUIDITY_BIASED["PLSM"]["score"]
    assert mu.rank == BASELINE_LIQUIDITY_BIASED["MU"]["rank"]
    assert mu.score == BASELINE_LIQUIDITY_BIASED["MU"]["score"]

    # PLSM is buried deep (mirrors live "rank #15 of 471"): the rocket is NOT top-3.
    assert plsm.rank > 3


def test_baseline_weights_sum_to_one():
    """Sanity: both weight-sets normalise (the blend is a weighted average)."""
    assert abs(sum(ROSS_PILLAR_WEIGHTS.values()) - 1.0) < 1e-9
    assert abs(sum(ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED.values()) - 1.0) < 1e-9


# ── Flag-ON: the 3-layer explosive scorer FLIPS the ranking ───────────────────
# The live-shaped batch matching the real 2026-06-24 scenario: PLSM (the lone
# +400% / 15,000x-RVOL rocket) + MU (the non-explosive mega-cap) + the 18 mediocre
# names. The 5 synthetic ``R*`` rockets in ``_build_batch`` were ARTIFACTS that forced
# the OLD scorer's percentiles to saturate below 1.0 (so PLSM and a 6x name looked the
# same); they are STRICTLY more explosive than PLSM, so under the new scorer they
# legitimately out-rank it — which is why the "PLSM top-3" guarantee is asserted on the
# live-shaped batch (PLSM the lone rocket), exactly the production claim.


def _live_batch() -> dict[str, dict]:
    """The 2026-06-24 live shape: PLSM (lone rocket) + MU + 18 mediocre (no R* artifacts)."""
    return {k: v for k, v in _build_batch().items() if not k.startswith("R")}


def test_explosive_megacap_no_longer_outranks_rocket_liquidity_biased():
    """LIQUIDITY-BIASED weights (the LIVE lane weight-set), flag ON: the documented bug is
    GONE. PLSM (tier 3) ranks TOP-3 and the non-explosive mega-cap MU (tier 0) does NOT
    out-rank it — the exact inversion of ``test_baseline_megacap_outranks_rocket_*``."""
    res = score_universe(
        _live_batch(), weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED, explosive=True
    )
    plsm, mu = res["PLSM"], res["MU"]

    # LAYER 1: PLSM is the extreme-explosiveness tier; MU is not explosive at all.
    assert plsm.tier == 3, plsm.tier
    assert mu.tier == 0, mu.tier
    # The fix: the rocket ranks TOP-3 and the mega-cap no longer out-ranks it.
    assert plsm.rank <= 3, plsm.rank
    assert plsm.rank < mu.rank, (plsm.rank, mu.rank)
    # LAYER 1 is non-compensatory: EVERY tier-0 name (incl. MU + all dull) ranks below the
    # single tier-3 name — no mid pillar can average the rocket back down.
    assert all(
        s.rank > plsm.rank for s in res.values() if s.symbol != "PLSM"
    )


def test_explosive_decompresses_magnitude_default_weights():
    """DEFAULT weights, flag ON: PLSM's 15,000x / +400% magnitude is RESTORED — the rocket
    separates well ABOVE the mega-cap instead of both collapsing into the [0.6,0.75]
    compression band the baseline test locks."""
    res = score_universe(_live_batch(), explosive=True)
    plsm, mu = res["PLSM"], res["MU"]
    assert plsm.tier == 3 and mu.tier == 0
    assert plsm.rank == 1, plsm.rank  # the lone rocket tops the live-shaped field
    # Magnitude restored: a wide separation, NOT the <=0.10 compression the baseline asserts.
    assert plsm.score - mu.score > 0.30, (plsm.score, mu.score)


def test_explosive_tier_cohort_fills_top_slots_full_fixture():
    """On the FULL 25-name fixture (with the 5 R* artifacts), flag ON: the explosive cohort
    (the 5 rockets + PLSM, all tier 3) occupies ranks 1-6 and MU + every dull name (tier 0)
    sit strictly below — the slot-starvation fix. PLSM ranks below the R* names only because
    they are, by fixture construction, strictly MORE explosive (the honest ordering)."""
    res = score_universe(_build_batch(), explosive=True)
    tier3 = sorted((s for s in res.values() if s.tier == 3), key=lambda s: s.rank)
    # PLSM + the 5 rockets are the tier-3 cohort, holding the top 6 ranks contiguously.
    assert {s.symbol for s in tier3} == {"PLSM", "R0", "R1", "R2", "R3", "R4"}
    assert [s.rank for s in tier3] == [1, 2, 3, 4, 5, 6]
    # MU (tier 0) is below the entire explosive cohort and no longer out-ranks PLSM.
    assert res["MU"].tier == 0
    assert res["MU"].rank > res["PLSM"].rank


def test_explosive_flag_off_is_byte_identical():
    """Additivity guard: flag OFF reproduces the frozen baseline EXACTLY (the new path is
    fully gated). This is the same numbers the baseline-lock tests pin, asserted here as a
    single explicit flag-OFF==baseline check."""
    off = score_universe(_build_batch(), explosive=False)
    assert off["PLSM"].rank == BASELINE_DEFAULT["PLSM"]["rank"]
    assert off["PLSM"].score == BASELINE_DEFAULT["PLSM"]["score"]
    assert off["MU"].rank == BASELINE_DEFAULT["MU"]["rank"]
    assert off["MU"].score == BASELINE_DEFAULT["MU"]["score"]
    # tier is 0 for every name on the flag-OFF path (the explosive layer is inert).
    assert all(s.tier == 0 for s in off.values())
