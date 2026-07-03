"""Replay v3 STEP-2 — REALISTIC FILL MODEL unit tests (pure, no DB).

Proves each property of the volume-aware / latency-aware / mode-aware fill layer added to
``MockBrokerAdapter`` on top of the P0/P1 recorded-NBBO fill engine:

  (a) marketable-limit BUY fills at the recorded NBBO ask path within the limit, walking the
      sim clock forward (already P1; re-asserted here under the STEP-2 knobs).
  (b) FILL-VOLUME REALISM — cumulative fill ≤ ``volume_participation_frac`` (base 0.25) of the
      recorded printed volume at-or-through the limit during the order's live window; PARTIAL
      fills result when the tape is thin; NO fill through an EMPTY tape (0 printed volume).
  (c) ack/latency derived from the observed real place→fill distribution (median ≈ 10 s,
      documented fallback base) → an integer ack-delay for the grid cadence.
  (d) SELLS symmetric on the bid side.
  (e) explicit conservative/optimistic MODE flag (conservative default; optimistic crosses the
      favorable mid + bypasses the volume cap, but is STILL bounded by the recorded quote).

All deterministic (no RNG / no wall clock / no network). One assertion-block per property.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services.trading.momentum_neural.replay_mock_broker import (
    DEFAULT_ACK_LATENCY_SECONDS,
    DEFAULT_VOLUME_PARTICIPATION_FRAC,
    FillMode,
    MockBrokerAdapter,
    RecordedQuote,
)

_BASE = datetime(2026, 6, 30, 14, 30, 0)  # naive-UTC (the _utcnow shape)


# ── (e) MODE FLAG — conservative default; explicit optimistic ────────────────────────
def test_conservative_is_the_default_mode():
    m = MockBrokerAdapter()
    assert m._fill_mode == FillMode.CONSERVATIVE


def test_fill_mode_normalize_only_recognizes_optimistic():
    assert FillMode.normalize("optimistic") == FillMode.OPTIMISTIC
    assert FillMode.normalize("OPTIMISTIC") == FillMode.OPTIMISTIC
    assert FillMode.normalize("conservative") == FillMode.CONSERVATIVE
    assert FillMode.normalize("garbage") == FillMode.CONSERVATIVE
    assert FillMode.normalize(None) == FillMode.CONSERVATIVE


def test_conservative_buy_crosses_the_ask_optimistic_crosses_the_mid():
    """(a)+(e): a conservative BUY fills at the recorded ASK (adverse); an optimistic BUY
    fills at the recorded MID (favorable) — but NEVER outside the recorded book."""
    q = RecordedQuote(bid=10.00, ask=10.10)  # mid 10.05
    cons = MockBrokerAdapter(fill_mode=FillMode.CONSERVATIVE)
    cons.set_clock(_BASE)
    cons.set_quote("AAA", q)
    rc = cons.place_market_order(product_id="AAA", side="buy", base_size="100")
    assert rc["ok"] and rc["raw"]["fill_price"] == pytest.approx(10.10)  # ask

    opt = MockBrokerAdapter(fill_mode=FillMode.OPTIMISTIC)
    opt.set_clock(_BASE)
    opt.set_quote("AAA", q)
    ro = opt.place_market_order(product_id="AAA", side="buy", base_size="100")
    assert ro["ok"] and ro["raw"]["fill_price"] == pytest.approx(10.05)  # mid
    # optimistic is strictly better than (or equal to) conservative for a buyer, and bounded
    assert q.bid <= ro["raw"]["fill_price"] <= q.ask


# ── (d) SELL symmetric on the bid side ───────────────────────────────────────────────
def test_conservative_sell_crosses_the_bid_optimistic_the_mid():
    q = RecordedQuote(bid=10.00, ask=10.10)  # mid 10.05
    cons = MockBrokerAdapter(fill_mode=FillMode.CONSERVATIVE)
    cons.set_clock(_BASE)
    cons.set_quote("SEL", q)
    rc = cons.place_market_order(product_id="SEL", side="sell", base_size="50")
    assert rc["ok"] and rc["raw"]["fill_price"] == pytest.approx(10.00)  # bid

    opt = MockBrokerAdapter(fill_mode=FillMode.OPTIMISTIC)
    opt.set_clock(_BASE)
    opt.set_quote("SEL", q)
    ro = opt.place_market_order(product_id="SEL", side="sell", base_size="50")
    assert ro["ok"] and ro["raw"]["fill_price"] == pytest.approx(10.05)  # mid
    # a seller is never worse-off optimistic, and never priced outside the book
    assert q.bid <= ro["raw"]["fill_price"] <= q.ask


# ── (b) FILL-VOLUME REALISM — cap, partial, no-fill-through-empty-tape ────────────────
def _volcap_broker(frac: float = 0.25) -> MockBrokerAdapter:
    return MockBrokerAdapter(
        resting_limit_fills=True,
        volume_cap_enabled=True,
        volume_participation_frac=frac,
        freshness_mode="wall",
    )


def test_default_volume_participation_frac_is_the_documented_base():
    assert DEFAULT_VOLUME_PARTICIPATION_FRAC == 0.25


def test_no_fill_through_empty_tape():
    """(b): with the limit crossable but ZERO printed volume fed, the order rests OPEN and
    fills NOTHING — the no-fill-through-empty-tape guarantee."""
    m = _volcap_broker()
    m.set_clock(_BASE)
    m.set_quote("EMP", RecordedQuote(bid=9.99, ask=10.00))
    r = m.place_limit_order_gtc(product_id="EMP", side="buy", base_size="1000", limit_price="10.05")
    o, _ = m.get_order(r["order_id"])
    # crossable (ask 10.00 <= 10.05) but no printed volume ⇒ NO fill, stays open
    assert o.status == "open"
    assert (o.filled_size or 0.0) == 0.0


def test_volume_cap_produces_a_partial_when_tape_is_thin():
    """(b): 400 shares print through the limit, frac=0.25 ⇒ at most 100 fill. A 1000-share
    order PARTIALS to 100 and stays open for more volume."""
    m = _volcap_broker(frac=0.25)
    m.set_clock(_BASE)
    m.set_quote("THN", RecordedQuote(bid=9.99, ask=10.00))
    r = m.place_limit_order_gtc(product_id="THN", side="buy", base_size="1000", limit_price="10.05")
    # feed 400 printed shares at/through the limit
    m.set_printed_volume("THN", 400.0)
    o, _ = m.get_order(r["order_id"])
    assert o.filled_size == pytest.approx(100.0)  # 0.25 * 400
    assert o.status == "open"  # remainder still wants more volume
    # feed another 400 ⇒ +100 more (cumulative cap 0.25*800 = 200)
    m.set_printed_volume("THN", 400.0)
    o2, _ = m.get_order(r["order_id"])
    assert o2.filled_size == pytest.approx(200.0)
    assert o2.status == "open"


def test_volume_cap_never_exceeds_frac_times_printed_volume():
    """(b): even with abundant printed volume, cumulative fill is capped at frac × printed —
    a small order fully fills, a large one is throttled to the participation cap."""
    m = _volcap_broker(frac=0.25)
    m.set_clock(_BASE)
    m.set_quote("CAP", RecordedQuote(bid=9.99, ask=10.00))
    # small order (10 shares) vs 1000 printed ⇒ cap is 250, so it fully fills
    r_small = m.place_limit_order_gtc(product_id="CAP", side="buy", base_size="10", limit_price="10.05")
    m.set_printed_volume("CAP", 1000.0)
    o_small, _ = m.get_order(r_small["order_id"])
    assert o_small.status == "filled"
    assert o_small.filled_size == pytest.approx(10.0)


def test_volume_cap_completes_full_size_once_enough_volume_prints():
    """(b): once cumulative printed volume × frac ≥ order size, the order fills FULLY and
    terminalizes."""
    m = _volcap_broker(frac=0.25)
    m.set_clock(_BASE)
    m.set_quote("FUL", RecordedQuote(bid=9.99, ask=10.00))
    r = m.place_limit_order_gtc(product_id="FUL", side="buy", base_size="100", limit_price="10.05")
    m.set_printed_volume("FUL", 4000.0)  # 0.25 * 4000 = 1000 >= 100
    o, _ = m.get_order(r["order_id"])
    assert o.status == "filled"
    assert o.filled_size == pytest.approx(100.0)
    assert o.average_filled_price == pytest.approx(10.00)  # crossed the recorded ask


def test_volume_cap_off_by_default_preserves_p1_full_fill():
    """Backward-compat: with volume-capping OFF (the P0/P1 default), a resting crossable limit
    fills FULLY on the first cross regardless of printed volume."""
    m = MockBrokerAdapter(resting_limit_fills=True, freshness_mode="wall")  # no volume cap
    m.set_clock(_BASE)
    m.set_quote("NOC", RecordedQuote(bid=9.99, ask=10.00))
    r = m.place_limit_order_gtc(product_id="NOC", side="buy", base_size="1000", limit_price="10.05")
    o, _ = m.get_order(r["order_id"])
    assert o.status == "filled"
    assert o.filled_size == pytest.approx(1000.0)


def test_optimistic_mode_bypasses_the_volume_cap():
    """(e): optimistic mode assumes full size available — the volume cap is inert even when
    requested, so a crossable limit fills fully immediately (upper-bound of the PnL band)."""
    m = MockBrokerAdapter(
        resting_limit_fills=True,
        volume_cap_enabled=True,  # requested…
        volume_participation_frac=0.25,
        fill_mode=FillMode.OPTIMISTIC,  # …but optimistic disables it
        freshness_mode="wall",
    )
    assert m._volume_cap_enabled is False
    m.set_clock(_BASE)
    m.set_quote("OPT", RecordedQuote(bid=9.99, ask=10.01))
    r = m.place_limit_order_gtc(product_id="OPT", side="buy", base_size="1000", limit_price="10.05")
    o, _ = m.get_order(r["order_id"])
    assert o.status == "filled"
    assert o.filled_size == pytest.approx(1000.0)


def test_volume_cap_still_never_fills_through_an_empty_or_uncrossed_book():
    """(b)+(a): the volume cap NEVER overrides the price gate — a limit BELOW the ask does
    not cross at all, no matter how much volume prints."""
    m = _volcap_broker(frac=0.25)
    m.set_clock(_BASE)
    m.set_quote("PRC", RecordedQuote(bid=9.90, ask=10.00))
    # BUY limit 9.95 is below the ask 10.00 ⇒ never crosses
    r = m.place_limit_order_gtc(product_id="PRC", side="buy", base_size="100", limit_price="9.95")
    m.set_printed_volume("PRC", 10_000.0)  # tons of volume, but the price gate blocks
    o, _ = m.get_order(r["order_id"])
    assert o.status == "open"
    assert (o.filled_size or 0.0) == 0.0


# ── (c) ACK / LATENCY from the observed distribution ─────────────────────────────────
def test_default_ack_latency_base_is_documented_median():
    assert DEFAULT_ACK_LATENCY_SECONDS == pytest.approx(10.0)


def test_ack_delay_ticks_derives_from_latency_and_grid_cadence():
    """(c): the median place→fill latency (10 s) over a 5 s grid ⇒ 2 ack-delay ticks;
    over a 1 s grid ⇒ 10; a 0/negative cadence ⇒ 0 (guard)."""
    m = MockBrokerAdapter(fill_mode=FillMode.CONSERVATIVE)  # base latency 10 s
    assert m.ack_delay_ticks_for(5.0) == 2   # round(10/5)
    assert m.ack_delay_ticks_for(1.0) == 10
    assert m.ack_delay_ticks_for(10.0) == 1
    assert m.ack_delay_ticks_for(0.0) == 0


def test_set_latency_distribution_overrides_the_base():
    """(c): the driver's measured percentiles override the fallback base — conservative uses
    the median, optimistic uses p25 (fast fills)."""
    cons = MockBrokerAdapter(fill_mode=FillMode.CONSERVATIVE)
    cons.set_latency_distribution(median_seconds=20.0, p25_seconds=6.0, p75_seconds=40.0)
    assert cons.ack_delay_ticks_for(5.0) == 4  # round(20/5)

    opt = MockBrokerAdapter(fill_mode=FillMode.OPTIMISTIC)
    opt.set_latency_distribution(median_seconds=20.0, p25_seconds=6.0, p75_seconds=40.0)
    assert opt.ack_delay_ticks_for(5.0) == 1  # round(6/5)


def test_ack_delay_holds_the_order_open_before_it_can_fill():
    """(c)+(a): an ack-delayed volume-capped BUY does NOT fill until the delay drains, THEN
    fills against printed volume — proving latency precedes the fill."""
    m = MockBrokerAdapter(
        resting_limit_fills=True,
        ack_delay_ticks=2,
        volume_cap_enabled=True,
        volume_participation_frac=0.25,
        freshness_mode="wall",
    )
    m.set_clock(_BASE)
    q = RecordedQuote(bid=9.99, ask=10.00)
    m.set_quote("LAT", q)  # placement quote consumes ack tick 1 (delay 2 -> 1)
    r = m.place_limit_order_gtc(product_id="LAT", side="buy", base_size="100", limit_price="10.05")
    assert m.get_order(r["order_id"])[0].filled_size == pytest.approx(0.0)  # ack not yet drained
    # each printed-volume advance also ticks the ack window down before it can fill
    m.set_printed_volume("LAT", 1000.0)  # advance (delay 1 -> 0), still no fill this tick
    assert m.get_order(r["order_id"])[0].filled_size == pytest.approx(0.0)
    m.set_printed_volume("LAT", 1000.0)  # delay drained ⇒ fills (cap 0.25*2000 >= 100)
    o, _ = m.get_order(r["order_id"])
    assert o.status == "filled"
    assert o.filled_size == pytest.approx(100.0)


# ── determinism ──────────────────────────────────────────────────────────────────────
def test_fill_model_is_deterministic_across_two_identical_runs():
    """No RNG / no wall clock: identical injected inputs ⇒ identical fills."""
    def _run():
        m = _volcap_broker(frac=0.25)
        m.set_clock(_BASE)
        m.set_quote("DET", RecordedQuote(bid=9.99, ask=10.00))
        r = m.place_limit_order_gtc(product_id="DET", side="buy", base_size="500", limit_price="10.05")
        m.set_printed_volume("DET", 800.0)
        m.set_printed_volume("DET", 800.0)
        o, _ = m.get_order(r["order_id"])
        return (o.status, round(o.filled_size, 6), round(o.average_filled_price or 0.0, 6))

    assert _run() == _run()
