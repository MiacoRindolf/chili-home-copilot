"""Unit tests for P4 — Ortex squeeze-score DEEPENING into ENTRY + EXIT.

The squeeze_fuel sub-score (si_pct_free_float + cost_to_borrow) already feeds the
SELECTION tilt. P4 extends the SAME score to two downstream uses driven SOLELY by the
name's OWN within-batch squeeze PERCENTILE (squeeze_fuel_rank_pct):

  (1) ENTRY size-up — a bounded UPWARD risk-budget multiplier when the name is in the
      TOP squeeze percentile AND live OFI > 0 AND news (strong-catalyst) agrees.
  (2) EXIT squeeze-aware-hold — a bounded RIDE-band WIDEN factor in the extreme tail.

These are pure-function tests (no DB / no IO): the adaptive math (no magic), the
triple-gate, the bounds (ONE documented cap each), and the byte-identical (1.0) off-path.
"""

import pytest

from app.services.trading.momentum_neural.ross_momentum import (
    ROSS_SQUEEZE_ENTRY_MAX_MULT,
    ROSS_SQUEEZE_ENTRY_TOP_PCTL,
    ROSS_SQUEEZE_EXIT_MAX_WIDEN,
    ROSS_SQUEEZE_EXIT_TAIL_PCTL,
    _percentile_rank,
    squeeze_entry_size_multiplier,
    squeeze_exit_band_widen,
)


# ── ENTRY size-up: the triple-gate (percentile AND ofi>0 AND news) ──────────────


def test_entry_sizeup_arms_only_when_all_three_agree():
    # top percentile + OFI>0 + news ⇒ armed, mult > 1.0
    mult, meta = squeeze_entry_size_multiplier(0.95, ofi=0.4, news_agrees=True)
    assert meta["armed"] is True
    assert mult > 1.0


def test_entry_sizeup_neutral_when_ofi_not_positive():
    # tape DISAGREES (ofi <= 0) ⇒ neutral, byte-identical 1.0
    for ofi in (0.0, -0.2, None):
        mult, meta = squeeze_entry_size_multiplier(0.99, ofi=ofi, news_agrees=True)
        assert mult == 1.0
        assert meta["armed"] is False


def test_entry_sizeup_neutral_when_news_disagrees():
    mult, meta = squeeze_entry_size_multiplier(0.99, ofi=0.5, news_agrees=False)
    assert mult == 1.0
    assert meta["armed"] is False


def test_entry_sizeup_neutral_below_top_percentile():
    # rank below the documented floor ⇒ not in the top cohort ⇒ neutral
    mult, _ = squeeze_entry_size_multiplier(
        ROSS_SQUEEZE_ENTRY_TOP_PCTL - 0.05, ofi=0.5, news_agrees=True
    )
    assert mult == 1.0


def test_entry_sizeup_neutral_when_rank_missing():
    # no squeeze data (e.g. crypto / Ortex absent) ⇒ rank None ⇒ neutral 1.0
    mult, meta = squeeze_entry_size_multiplier(None, ofi=0.5, news_agrees=True)
    assert mult == 1.0
    assert meta["armed"] is False


def test_entry_sizeup_is_percentile_driven_and_monotone():
    # a HIGHER squeeze rank ⇒ a strictly LARGER up-size (percentile-driven, no magic step)
    m_lo, _ = squeeze_entry_size_multiplier(0.85, ofi=0.3, news_agrees=True)
    m_hi, _ = squeeze_entry_size_multiplier(0.99, ofi=0.3, news_agrees=True)
    assert 1.0 < m_lo < m_hi


def test_entry_sizeup_capped_at_documented_max():
    # at rank == 1.0 the multiplier hits EXACTLY the ONE documented cap, never above
    mult, _ = squeeze_entry_size_multiplier(1.0, ofi=1.0, news_agrees=True)
    assert mult == pytest.approx(ROSS_SQUEEZE_ENTRY_MAX_MULT)
    assert mult <= ROSS_SQUEEZE_ENTRY_MAX_MULT


def test_entry_sizeup_at_floor_is_neutral_edge():
    # exactly AT the floor ⇒ frac 0 ⇒ neutral 1.0 (the ramp starts above the floor)
    mult, _ = squeeze_entry_size_multiplier(
        ROSS_SQUEEZE_ENTRY_TOP_PCTL, ofi=0.3, news_agrees=True
    )
    assert mult == 1.0


# ── EXIT band-widen: extreme-tail only, bounded, INVARIANT-A-safe widening ───────


def test_exit_widen_arms_only_in_extreme_tail():
    factor, meta = squeeze_exit_band_widen(0.95)
    assert factor > 1.0
    assert meta["armed"] is True


def test_exit_widen_neutral_below_tail():
    factor, meta = squeeze_exit_band_widen(ROSS_SQUEEZE_EXIT_TAIL_PCTL - 0.05)
    assert factor == 1.0
    assert meta["armed"] is False


def test_exit_widen_neutral_when_rank_missing():
    factor, _ = squeeze_exit_band_widen(None)
    assert factor == 1.0


def test_exit_widen_monotone_and_capped():
    f_lo, _ = squeeze_exit_band_widen(0.92)
    f_hi, _ = squeeze_exit_band_widen(1.0)
    assert 1.0 < f_lo < f_hi
    assert f_hi == pytest.approx(ROSS_SQUEEZE_EXIT_MAX_WIDEN)
    assert f_hi <= ROSS_SQUEEZE_EXIT_MAX_WIDEN


def test_exit_widen_only_widens_never_tightens():
    # the factor is a WIDEN (>= 1.0) — it can never return below 1.0 (which would TIGHTEN
    # the band and risk an INVARIANT-A violation at the call site). Sweep the whole range.
    for rp in (0.0, 0.5, ROSS_SQUEEZE_EXIT_TAIL_PCTL, 0.95, 1.0):
        factor, _ = squeeze_exit_band_widen(rp)
        assert factor >= 1.0


# ── the within-batch percentile axis (the no-magic adaptive bar) ─────────────────


def test_rank_pct_is_within_batch_adaptive():
    # the SAME raw squeeze score ranks DIFFERENTLY depending on the live batch — proving the
    # bar floats with the batch (no fixed absolute SI/CTB cutoff drives the levers).
    batch_strong = sorted([0.55, 0.6, 0.62, 0.65])  # 0.62 is mid-pack here
    batch_weak = sorted([0.2, 0.3, 0.4, 0.62])       # 0.62 tops this batch
    rp_mid = _percentile_rank(0.62, batch_strong)
    rp_top = _percentile_rank(0.62, batch_weak)
    assert rp_top > rp_mid
    assert rp_top == pytest.approx(1.0)
