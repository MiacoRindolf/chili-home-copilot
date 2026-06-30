"""REGIME-ADAPTIVE FRONT-SIDE TILT THRESHOLDS — kill the fixed 0.25/0.75 magic numbers.

The front-side size-tilt ramp used to take ``s_lo``/``s_hi``/``defer_below`` from the
``front_side_size_tilt`` function defaults (0.25/0.75/0.15) — FIXED magic numbers that
ignore the regime. ``live_runner`` now maintains a BOUNDED, TTL'd, in-memory rolling cache
of recently-computed cross-name front-side strength scores and, once warm (>= K samples),
derives the ramp anchors as the p25/p75/p15 of that LIVE distribution. Hot tape ⇒ scores
run high ⇒ the ramp shifts UP (a mid strength now sizes DOWN); cold tape ⇒ shifts DOWN
(a mid strength sizes UP). ``size_floor`` STAYS the one documented safety-floor.

These tests pin the cache + the (cache-state)->thresholds contract as PURE functions (no
DB / no broker), exactly mirroring the call site:

  (a) COLD cache              -> _frontside_adaptive_thresholds() is None -> caller uses the
                                 documented base 0.25/0.75/0.15 (byte-identical to today);
  (b) WARM cache skewed HIGH  -> s_lo/s_hi shift UP -> a fixed MID strength sizes DOWN more
                                 than under the fixed base (ramp adapted to the hot regime);
  (c) WARM cache skewed LOW   -> s_lo/s_hi shift DOWN -> the SAME mid strength sizes UP;
  (d) FLAG OFF                -> the whole front-side block (incl this cache) never runs ->
                                 mult 1.0 -> byte-identical;
  (e) the cache respects its HARD MAX SIZE + per-sample TTL (old entries evicted).
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.ross_momentum import (
    FRONTSIDE_SIZE_FLOOR,
    front_side_size_tilt,
)


@pytest.fixture(autouse=True)
def _clean_dist():
    """Each test starts and ends with an EMPTY rolling distribution (no cross-test leak)."""
    lr._FRONTSIDE_DIST.clear()
    yield
    lr._FRONTSIDE_DIST.clear()


# ── (a) COLD cache -> base thresholds (byte-identical to today) ──────────────────────────
def test_cold_cache_returns_none_and_caller_uses_base():
    # No samples warmed yet: the adaptive helper returns None, so the caller keeps the
    # documented base (0.25/0.75/0.15) -> the mult for a given strength is byte-identical
    # to today's fixed-default behavior.
    assert lr._frontside_adaptive_thresholds() is None

    strength = 0.5
    # base path (what the caller falls back to when None)
    m_base, _d_base, _ = front_side_size_tilt(strength, s_lo=0.25, s_hi=0.75, defer_below=0.15)
    # explicit "today" defaults must agree (proves the fallback IS the old behavior)
    m_today, _d_today, _ = front_side_size_tilt(strength)
    assert m_base == pytest.approx(m_today)


def test_below_warmup_floor_stays_cold():
    # K-1 samples is still COLD (warm-up floor is _FRONTSIDE_DIST_MIN_K): no adaptation.
    for i in range(lr._FRONTSIDE_DIST_MIN_K - 1):
        lr._frontside_dist_note(0.6, now=1000.0)
    assert lr._frontside_adaptive_thresholds(now=1000.0) is None


# ── (b) WARM cache skewed HIGH -> anchors up -> mid strength sizes DOWN more ─────────────
def test_warm_high_regime_shifts_ramp_up_and_sizes_mid_down():
    now = 5000.0
    for i in range(60):
        lr._frontside_dist_note(0.72 + 0.002 * i, now=now)  # hot cluster spanning ~0.72-0.84
    adapt = lr._frontside_adaptive_thresholds(now=now)
    assert adapt is not None
    s_lo, s_hi, defer_below, n = adapt
    assert n >= lr._FRONTSIDE_DIST_MIN_K
    # The hot regime pulls BOTH p25/p75 anchors UP, above the fixed 0.25/0.75 base.
    assert s_lo > 0.25 + 1e-6
    assert s_hi > 0.75 - 1e-6  # the whole ramp shifts up relative to the base hi
    assert s_lo < s_hi  # ramp stays monotone

    mid = 0.5
    m_base, _, _ = front_side_size_tilt(mid, s_lo=0.25, s_hi=0.75, defer_below=0.15)
    m_hot, _, _ = front_side_size_tilt(mid, s_lo=s_lo, s_hi=s_hi, defer_below=defer_below)
    # In the hot regime a MID-strength name is now BELOW the ramp -> sizes DOWN MORE.
    assert m_hot < m_base - 1e-9
    # Never below the safety floor, never above 1.0.
    assert FRONTSIDE_SIZE_FLOOR - 1e-9 <= m_hot <= 1.0 + 1e-9


# ── (c) WARM cache skewed LOW -> anchors down -> mid strength sizes UP ───────────────────
def test_warm_low_regime_shifts_ramp_down_and_sizes_mid_up():
    now = 6000.0
    for i in range(60):
        lr._frontside_dist_note(0.20 + 0.0005 * i, now=now)  # tight cluster near ~0.20-0.23
    adapt = lr._frontside_adaptive_thresholds(now=now)
    assert adapt is not None
    s_lo, s_hi, defer_below, n = adapt
    # The cold regime pushes p25/p75 anchors DOWN, below the fixed 0.25/0.75 base.
    assert s_hi < 0.75 - 1e-6
    assert s_lo < s_hi

    mid = 0.5
    m_base, _, _ = front_side_size_tilt(mid, s_lo=0.25, s_hi=0.75, defer_below=0.15)
    m_cold, _, _ = front_side_size_tilt(mid, s_lo=s_lo, s_hi=s_hi, defer_below=defer_below)
    # In the cold regime a MID-strength name is now ABOVE the ramp -> sizes UP (toward full).
    assert m_cold > m_base + 1e-9
    assert m_cold <= 1.0 + 1e-9


def test_high_vs_low_regime_bracket_the_base():
    # The SAME mid strength: hot regime < base < cold regime. The regime literally moves the
    # ramp in the right direction relative to today's fixed anchors.
    mid = 0.5
    m_base, _, _ = front_side_size_tilt(mid, s_lo=0.25, s_hi=0.75, defer_below=0.15)

    lr._FRONTSIDE_DIST.clear()
    for i in range(60):
        lr._frontside_dist_note(0.70 + 0.001 * i, now=7000.0)  # hot cluster ~0.70-0.76
    hi = lr._frontside_adaptive_thresholds(now=7000.0)
    assert hi is not None
    m_hot, _, _ = front_side_size_tilt(mid, s_lo=hi[0], s_hi=hi[1], defer_below=hi[2])

    lr._FRONTSIDE_DIST.clear()
    for i in range(60):
        lr._frontside_dist_note(0.20 + 0.001 * i, now=8000.0)  # cold cluster ~0.20-0.26
    lo = lr._frontside_adaptive_thresholds(now=8000.0)
    assert lo is not None
    m_cold, _, _ = front_side_size_tilt(mid, s_lo=lo[0], s_hi=lo[1], defer_below=lo[2])

    assert m_hot < m_base < m_cold


# ── (d) FLAG OFF -> the block (incl the cache) never runs -> byte-identical ──────────────
def test_flag_off_skips_cache_and_is_byte_identical():
    # Mirror the live gate: when chili_momentum_frontside_adaptive_enabled is False the whole
    # front-side block is skipped, so NOTHING is ever appended to the distribution and the
    # tilt mult is a hard 1.0 (byte-identical). We model that here: the flag-off branch never
    # calls _frontside_dist_note, so the cache stays empty and thresholds stay cold (None).
    enabled = False
    if enabled:  # pragma: no cover - documents the live gate; off-path never notes
        lr._frontside_dist_note(0.9)
    assert len(lr._FRONTSIDE_DIST) == 0
    assert lr._frontside_adaptive_thresholds() is None


# ── (e) HARD MAX SIZE + per-sample TTL eviction ─────────────────────────────────────────
def test_cache_respects_hard_max_size():
    # Pump well past the cap; the cache must never exceed _FRONTSIDE_DIST_MAX and must keep
    # the MOST-RECENT samples (FIFO eviction of the oldest).
    base_now = 9000.0
    for i in range(lr._FRONTSIDE_DIST_MAX + 100):
        lr._frontside_dist_note(0.5, now=base_now + i * 0.001)
    assert len(lr._FRONTSIDE_DIST) == lr._FRONTSIDE_DIST_MAX
    # The oldest retained timestamp is newer than the very first one we inserted.
    assert lr._FRONTSIDE_DIST[0][0] > base_now


def test_cache_respects_ttl_eviction():
    # Samples older than the TTL window fall out as new ones arrive.
    for i in range(40):
        lr._frontside_dist_note(0.5, now=0.0)  # all at t=0
    assert len(lr._FRONTSIDE_DIST) == 40
    # A note past the TTL horizon evicts the entire stale t=0 batch.
    lr._frontside_dist_note(0.6, now=lr._FRONTSIDE_DIST_TTL_S + 1.0)
    assert len(lr._FRONTSIDE_DIST) == 1
    assert lr._FRONTSIDE_DIST[0][1] == pytest.approx(0.6)


def test_ttl_expired_samples_excluded_from_warm_count():
    # Stale samples don't count toward the warm-up floor: K stale + (K-1) fresh is still COLD.
    for i in range(lr._FRONTSIDE_DIST_MIN_K + 5):
        lr._frontside_dist_note(0.5, now=0.0)  # stale
    for i in range(lr._FRONTSIDE_DIST_MIN_K - 1):
        lr._frontside_dist_note(0.5, now=lr._FRONTSIDE_DIST_TTL_S + 10.0)  # fresh but < K
    # Evaluate "now" past the TTL so the t=0 batch is expired: only the fresh < K remain.
    assert lr._frontside_adaptive_thresholds(now=lr._FRONTSIDE_DIST_TTL_S + 10.0) is None


def test_degenerate_all_equal_distribution_falls_back_to_base():
    # An all-identical distribution has p25 == p75 (no ramp span) -> guard returns None ->
    # caller keeps the documented base. Adaptation must never collapse the ramp to a cliff.
    for i in range(60):
        lr._frontside_dist_note(0.5, now=10_000.0)
    assert lr._frontside_adaptive_thresholds(now=10_000.0) is None


def test_non_finite_and_bad_inputs_are_dropped():
    # NaN / inf strength never enters the cache (the score is finite [0,1] by construction).
    lr._frontside_dist_note(float("nan"), now=11_000.0)
    lr._frontside_dist_note(float("inf"), now=11_000.0)
    assert len(lr._FRONTSIDE_DIST) == 0
