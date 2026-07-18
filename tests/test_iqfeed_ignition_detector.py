"""IgnitionDetector unit tests — pure, event-time driven, no DB/socket.

The PLSM-shaped burst mirrors the PIT-measured 2026-07-13 ignition ramp
(~$500k/60s with +8-12% and hundreds of prints/10s); the flat tape mirrors a
quiet small-cap (<$50k/min, <1% wiggle).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.iqfeed_ignition_detector import IgnitionConfig, IgnitionDetector

T0 = datetime(2026, 7, 13, 11, 50, 0, tzinfo=timezone.utc)


def _feed_uniform(
    det: IgnitionDetector,
    symbol: str,
    *,
    start: datetime,
    seconds: float,
    prints_per_second: float,
    price_fn,
    size: float = 100.0,
):
    """Feed evenly spaced prints; returns every fire in order."""
    fires = []
    n = int(seconds * prints_per_second)
    for i in range(n):
        at = start + timedelta(seconds=i / prints_per_second)
        elapsed = (at - start).total_seconds()
        fire = det.on_print(symbol, at, price_fn(elapsed), size)
        if fire is not None:
            fires.append(fire)
    return fires


def _plsm_burst_price(elapsed: float) -> float:
    """4.30 -> ~5.20 over 60s (+21%), PLSM 11:57-11:59-shaped."""
    return 4.30 * (1.0 + 0.0035 * elapsed)


def test_fires_on_plsm_shaped_burst_within_seconds():
    det = IgnitionDetector(IgnitionConfig())
    # Quiet warmup: 1 print/s @ $430/print (~$26k/min) for 5 minutes.
    _feed_uniform(
        det,
        "PLSM",
        start=T0,
        seconds=300,
        prints_per_second=1.0,
        price_fn=lambda _e: 4.30,
    )
    # Burst: 40 prints/s, 500 shares each (~$900k/60s), +21%/60s ramp.
    burst_start = T0 + timedelta(seconds=300)
    fires = _feed_uniform(
        det,
        "PLSM",
        start=burst_start,
        seconds=60,
        prints_per_second=40.0,
        price_fn=_plsm_burst_price,
        size=500.0,
    )
    assert fires, "PLSM-shaped burst must nominate"
    latency = (fires[0].fired_at - burst_start).total_seconds()
    assert latency < 20.0, f"nomination too slow: {latency}s"
    assert fires[0].symbol == "PLSM"
    assert fires[0].pct_change_60s >= det.config.pct_base
    assert fires[0].dollar_vol_60s >= det.config.dollar_vol_base


def test_silent_on_flat_tape():
    det = IgnitionDetector(IgnitionConfig())
    # 10 minutes of flat trade: 2 prints/s, $8.00 +/- 0.5% wiggle, $50k/min.
    fires = _feed_uniform(
        det,
        "FLAT",
        start=T0,
        seconds=600,
        prints_per_second=2.0,
        price_fn=lambda e: 8.00 * (1.0 + 0.005 * ((int(e) % 2) * 2 - 1)),
        size=50.0,
    )
    assert fires == []


def test_first_print_of_day_explosion_fires_fast():
    """ERNA-shaped: NO history, first prints of the day explode (11:50 07-15)."""
    det = IgnitionDetector(IgnitionConfig())
    fires = _feed_uniform(
        det,
        "ERNA",
        start=T0,
        seconds=60,
        prints_per_second=30.0,
        price_fn=lambda e: 6.35 * (1.0 + 0.008 * e),
        size=400.0,
    )
    assert fires, "cold-start explosion must nominate"
    latency = (fires[0].fired_at - T0).total_seconds()
    assert latency < 30.0, f"cold-start nomination too slow: {latency}s"


def test_per_symbol_dedup_ttl():
    cfg = IgnitionConfig(dedup_ttl_s=300.0)
    det = IgnitionDetector(cfg)
    first = _feed_uniform(
        det,
        "DUPX",
        start=T0,
        seconds=60,
        prints_per_second=40.0,
        price_fn=_plsm_burst_price,
        size=500.0,
    )
    assert len(first) == 1, "one nomination per burst"
    # A second, still-qualifying burst inside the TTL stays suppressed.
    again = _feed_uniform(
        det,
        "DUPX",
        start=T0 + timedelta(seconds=90),
        seconds=60,
        prints_per_second=40.0,
        price_fn=lambda e: 5.30 * (1.0 + 0.0035 * e),
        size=500.0,
    )
    assert again == []
    assert det.suppressed_dedup > 0
    # After the TTL the symbol may nominate again (fresh surge over new baseline).
    later = _feed_uniform(
        det,
        "DUPX",
        start=T0 + timedelta(seconds=800),
        seconds=60,
        prints_per_second=80.0,
        price_fn=lambda e: 6.00 * (1.0 + 0.006 * e),
        size=1000.0,
    )
    assert len(later) == 1


def test_global_fires_per_minute_cap():
    cfg = IgnitionConfig(max_fires_per_minute=3)
    det = IgnitionDetector(cfg)
    fired = []
    for idx in range(6):
        sym = f"CAP{idx}"
        fires = _feed_uniform(
            det,
            sym,
            start=T0,
            seconds=60,
            prints_per_second=40.0,
            price_fn=_plsm_burst_price,
            size=500.0,
        )
        fired.extend(fires)
    assert len(fired) == 3
    assert det.suppressed_cap > 0


def test_adaptive_baseline_blocks_always_active_name():
    """A name that ALWAYS runs heavy $-volume must show a genuine surge, not
    merely cross the static floor."""
    det = IgnitionDetector(IgnitionConfig())
    # 10 minutes of steady heavy tape: 20 prints/s * $2000 = $2.4M/60s baseline,
    # then a slow +6%/60s drift with the SAME volume profile (no surge).
    _feed_uniform(
        det,
        "HVY",
        start=T0,
        seconds=600,
        prints_per_second=20.0,
        price_fn=lambda _e: 20.00,
        size=100.0,
    )
    fires = _feed_uniform(
        det,
        "HVY",
        start=T0 + timedelta(seconds=600),
        seconds=60,
        prints_per_second=20.0,
        price_fn=lambda e: 20.00 * (1.0 + 0.001 * e),
        size=100.0,
    )
    assert fires == [], "no-surge drift on an always-heavy name must not nominate"


def test_price_bounds_block_out_of_universe_prints():
    det = IgnitionDetector(IgnitionConfig())
    # Sub-floor penny prints, burst-shaped.
    penny = _feed_uniform(
        det,
        "PNY",
        start=T0,
        seconds=60,
        prints_per_second=40.0,
        price_fn=lambda e: 0.05 * (1.0 + 0.01 * e),
        size=500_000.0,
    )
    assert penny == []
    # Above-ceiling large-cap prints, burst-shaped.
    big = _feed_uniform(
        det,
        "BIGC",
        start=T0,
        seconds=60,
        prints_per_second=40.0,
        price_fn=lambda e: 400.0 * (1.0 + 0.002 * e),
        size=500.0,
    )
    assert big == []


def test_single_print_never_fires():
    det = IgnitionDetector(IgnitionConfig())
    fire = det.on_print("ONE", T0, 5.0, 1_000_000.0)
    assert fire is None


def test_invalid_prints_are_ignored():
    det = IgnitionDetector(IgnitionConfig())
    assert det.on_print("", T0, 5.0, 100.0) is None
    assert det.on_print("BAD", T0, 0.0, 100.0) is None
    assert det.on_print("BAD", T0, 5.0, 0.0) is None
    assert det.on_print("BAD", T0, -5.0, 100.0) is None


def test_symbol_table_hard_cap_is_bounded():
    cfg = IgnitionConfig(max_symbols=16)
    det = IgnitionDetector(cfg)
    for idx in range(64):
        det.on_print(f"S{idx:03d}", T0 + timedelta(seconds=idx), 5.0, 100.0)
    assert len(det._symbols) <= 16
