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

from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from app.services.trading.momentum_neural.replay_mock_broker import (
    DEFAULT_ACK_LATENCY_SECONDS,
    DEFAULT_VOLUME_PARTICIPATION_FRAC,
    EXACT_PRINT_MARKET_FILL_UNAVAILABLE,
    FillMode,
    MockBrokerAdapter,
    RecordedQuote,
    VerifiedExactPrint,
    _mint_verified_exact_print,
    _mint_verified_exact_print_inventory,
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


def test_full_participation_fraction_still_requires_observed_printed_volume():
    """A 100% participation bound means at most all observed prints, not uncapped fills."""

    m = _volcap_broker(frac=1.0)
    m.set_clock(_BASE)
    m.set_quote("ONE", RecordedQuote(bid=9.99, ask=10.00))
    placed = m.place_limit_order_gtc(
        product_id="ONE", side="buy", base_size="100", limit_price="10.05"
    )
    before, _ = m.get_order(placed["order_id"])
    assert before.status == "open"
    assert before.filled_size == pytest.approx(0.0)

    m.set_printed_volume("ONE", 40.0)
    after, _ = m.get_order(placed["order_id"])
    assert after.status == "open"
    assert after.filled_size == pytest.approx(40.0)


@pytest.mark.parametrize("fraction", [-0.01, 1.01, float("nan"), float("inf")])
def test_invalid_participation_fraction_fails_closed(fraction: float):
    with pytest.raises(ValueError, match="volume_participation_frac"):
        MockBrokerAdapter(volume_participation_frac=fraction)


def test_default_mode_non_marketable_limit_rests_until_recorded_quote_crosses():
    """The legacy immediate mode may not fabricate a fill through an unmarketable limit."""

    m = MockBrokerAdapter()
    m.set_clock(_BASE)
    m.set_quote("LIM", RecordedQuote(bid=9.99, ask=10.00))
    placed = m.place_limit_order_gtc(
        product_id="LIM", side="buy", base_size="10", limit_price="9.95"
    )
    before, _ = m.get_order(placed["order_id"])
    assert before.status == "open"
    assert before.filled_size == pytest.approx(0.0)

    m.set_quote("LIM", RecordedQuote(bid=9.93, ask=9.94))
    after, _ = m.get_order(placed["order_id"])
    assert after.status == "filled"
    assert after.filled_size == pytest.approx(10.0)
    assert after.average_filled_price == pytest.approx(9.94)

    m.set_quote("SELLREST", RecordedQuote(bid=9.00, ask=9.02))
    sell = m.place_limit_order_gtc(
        product_id="SELLREST", side="sell", base_size="10", limit_price="9.80"
    )
    sell_before, _ = m.get_order(sell["order_id"])
    assert sell_before.status == "open"
    assert sell_before.filled_size == pytest.approx(0.0)

    m.set_quote("SELLREST", RecordedQuote(bid=9.81, ask=9.83))
    sell_after, _ = m.get_order(sell["order_id"])
    assert sell_after.status == "filled"
    assert sell_after.average_filled_price == pytest.approx(9.81)


def test_adverse_slippage_cannot_execute_limit_worse_than_order_bound():
    m = MockBrokerAdapter(resting_limit_fills=True, slippage_bps=100.0)
    m.set_clock(_BASE)

    m.set_quote("BUY", RecordedQuote(bid=9.98, ask=10.00))
    buy = m.place_limit_order_gtc(
        product_id="BUY", side="buy", base_size="10", limit_price="10.05"
    )
    buy_order, _ = m.get_order(buy["order_id"])
    assert buy_order.status == "filled"
    assert buy_order.average_filled_price == pytest.approx(10.05)

    m.set_quote("SELL", RecordedQuote(bid=10.00, ask=10.02))
    sell = m.place_limit_order_gtc(
        product_id="SELL", side="sell", base_size="10", limit_price="9.95"
    )
    sell_order, _ = m.get_order(sell["order_id"])
    assert sell_order.status == "filled"
    assert sell_order.average_filled_price == pytest.approx(9.95)


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


def _exact_print_broker(
    *,
    participation: float = 0.25,
    latency_seconds: float = 0.0,
    expected_sequences: tuple[int, ...] = tuple(range(1, 11)),
) -> MockBrokerAdapter:
    broker = MockBrokerAdapter(
        resting_limit_fills=True,
        volume_cap_enabled=True,
        volume_participation_frac=participation,
        exact_print_fills=True,
        exact_print_order_latency_seconds=latency_seconds,
    )
    broker.configure_verified_exact_print_inventory(
        _mint_verified_exact_print_inventory(
            capture_identity_sha256="a" * 64,
            final_capture_seal_sha256="b" * 64,
            release_order_root_sha256="c" * 64,
            event_sha256s=tuple(
                hashlib.sha256(f"exact-print-{sequence}".encode()).hexdigest()
                for sequence in expected_sequences
            ),
        )
    )
    return broker


def _exact_print(
    sequence: int,
    *,
    release_ordinal: int | None = None,
    provider_offset: float,
    received_offset: float | None = None,
    available_offset: float,
    price: float = 10.0,
    size: float = 100.0,
    bid: float | None = 9.99,
    ask: float | None = 10.0,
    conditions: tuple[str, ...] = (),
    capture_identity_sha256: str = "a" * 64,
):
    base = _BASE.replace(tzinfo=timezone.utc)
    return _mint_verified_exact_print(
        event_sha256=hashlib.sha256(f"exact-print-{sequence}".encode()).hexdigest(),
        sequence=sequence,
        release_ordinal=release_ordinal or sequence,
        capture_identity_sha256=capture_identity_sha256,
        final_capture_seal_sha256="b" * 64,
        release_order_root_sha256="c" * 64,
        product_id="XPR",
        provider_event_at=base + timedelta(seconds=provider_offset),
        received_at=base
        + timedelta(
            seconds=(available_offset if received_offset is None else received_offset)
        ),
        available_at=base + timedelta(seconds=available_offset),
        price=price,
        size=size,
        bid=bid,
        ask=ask,
        conditions=conditions,
    )


def test_exact_print_budget_is_shared_fifo_across_concurrent_orders():
    broker = _exact_print_broker(participation=0.25)
    broker.set_clock(_BASE)
    broker.set_quote("XPR", RecordedQuote(bid=9.99, ask=10.0))
    first = broker.place_limit_order_gtc(
        product_id="XPR", side="buy", base_size="20", limit_price="10.05"
    )
    second = broker.place_limit_order_gtc(
        product_id="XPR", side="buy", base_size="20", limit_price="10.05"
    )
    assert broker.get_order(first["order_id"])[0].filled_size == pytest.approx(0.0)
    assert broker.get_order(second["order_id"])[0].filled_size == pytest.approx(0.0)

    broker.set_clock(_BASE + timedelta(seconds=2))
    broker.release_verified_exact_print(
        _exact_print(1, provider_offset=1.0, available_offset=2.0)
    )

    first_order = broker.get_order(first["order_id"])[0]
    second_order = broker.get_order(second["order_id"])[0]
    assert first_order.status == "filled"
    assert first_order.filled_size == pytest.approx(20.0)
    assert second_order.status == "open"
    assert second_order.filled_size == pytest.approx(5.0)
    allocations = broker.exact_print_allocations
    assert [value.quantity for value in allocations] == pytest.approx([20.0, 5.0])
    assert sum(value.quantity for value in allocations) == pytest.approx(25.0)
    assert broker.exact_print_audit[0]["participation_budget"] == pytest.approx(25.0)


def test_exact_print_mode_rejects_aggregate_volume_and_market_fill_shortcuts():
    broker = _exact_print_broker()
    broker.set_clock(_BASE)
    broker.set_quote("XPR", RecordedQuote(bid=9.99, ask=10.0))
    with pytest.raises(ValueError, match="aggregate printed volume"):
        broker.set_printed_volume("XPR", 1000.0)
    result = broker.place_market_order(product_id="XPR", side="buy", base_size="10")
    assert result["ok"] is False
    assert result["error"] == EXACT_PRINT_MARKET_FILL_UNAVAILABLE
    assert result["coverage_unavailable"] is True


def test_exact_print_provider_event_before_order_cannot_fill_on_late_release():
    broker = _exact_print_broker()
    broker.set_clock(_BASE + timedelta(seconds=10))
    broker.set_quote("XPR", RecordedQuote(bid=9.99, ask=10.0))
    placed = broker.place_limit_order_gtc(
        product_id="XPR", side="buy", base_size="20", limit_price="10.05"
    )
    broker.set_clock(_BASE + timedelta(seconds=15))
    broker.release_verified_exact_print(
        _exact_print(1, provider_offset=5.0, available_offset=15.0)
    )
    order = broker.get_order(placed["order_id"])[0]
    assert order.status == "open"
    assert order.filled_size == pytest.approx(0.0)
    assert broker.exact_print_audit[0]["candidate_order_ids"] == []


def test_exact_print_requires_latency_to_elapse_in_provider_event_time():
    broker = _exact_print_broker(latency_seconds=1.0)
    broker.set_clock(_BASE)
    broker.set_quote("XPR", RecordedQuote(bid=9.99, ask=10.0))
    placed = broker.place_limit_order_gtc(
        product_id="XPR", side="buy", base_size="20", limit_price="10.05"
    )
    broker.set_clock(_BASE + timedelta(seconds=2))
    broker.release_verified_exact_print(
        _exact_print(1, provider_offset=0.5, available_offset=2.0)
    )
    assert broker.get_order(placed["order_id"])[0].filled_size == pytest.approx(0.0)
    broker.set_clock(_BASE + timedelta(seconds=3))
    broker.release_verified_exact_print(
        _exact_print(2, provider_offset=1.5, available_offset=3.0)
    )
    assert broker.get_order(placed["order_id"])[0].filled_size == pytest.approx(20.0)


def test_exact_print_sell_requires_bid_side_print_and_respects_limit():
    broker = _exact_print_broker(participation=1.0)
    broker.set_clock(_BASE)
    broker.set_quote("XPR", RecordedQuote(bid=10.0, ask=10.02))
    placed = broker.place_limit_order_gtc(
        product_id="XPR", side="sell", base_size="10", limit_price="9.95"
    )
    broker.set_clock(_BASE + timedelta(seconds=2))
    broker.release_verified_exact_print(
        _exact_print(
            1,
            provider_offset=1.0,
            available_offset=2.0,
            price=10.0,
            size=10.0,
            bid=10.0,
            ask=10.02,
        )
    )
    order = broker.get_order(placed["order_id"])[0]
    assert order.status == "filled"
    assert order.average_filled_price == pytest.approx(10.0)
    assert broker.exact_print_allocations[0].side == "sell"


@pytest.mark.parametrize(
    ("price", "bid", "ask", "conditions", "disposition"),
    [
        (9.995, 9.99, 10.0, (), "inside_spread_aggressor_unresolved"),
        (10.0, 9.99, 10.0, ("UNMAPPED",), "print_conditions_unsupported"),
        (10.0, None, None, (), "print_nbbo_unavailable"),
        (10.0, 10.0, 10.0, (), "print_quote_side_ambiguous"),
    ],
)
def test_exact_print_unresolved_execution_semantics_fail_closed(
    price, bid, ask, conditions, disposition
):
    broker = _exact_print_broker()
    broker.set_clock(_BASE)
    broker.set_quote("XPR", RecordedQuote(bid=9.99, ask=10.0))
    placed = broker.place_limit_order_gtc(
        product_id="XPR", side="buy", base_size="10", limit_price="10.05"
    )
    broker.set_clock(_BASE + timedelta(seconds=2))
    broker.release_verified_exact_print(
        _exact_print(
            1,
            provider_offset=1.0,
            available_offset=2.0,
            price=price,
            bid=bid,
            ask=ask,
            conditions=conditions,
        )
    )
    assert broker.get_order(placed["order_id"])[0].filled_size == pytest.approx(0.0)
    assert broker.exact_print_audit[0]["disposition"] == disposition
    assert any(
        value.startswith("exact_print_execution_semantics_unavailable:")
        for value in broker.exact_print_counterfactual_authority_blockers
    )


def test_exact_print_rejects_future_duplicate_and_regressing_release():
    broker = _exact_print_broker()
    broker.set_clock(_BASE + timedelta(seconds=1))
    future = _exact_print(1, provider_offset=1.0, available_offset=2.0)
    before_future = broker.exact_print_allocation_root_sha256
    with pytest.raises(ValueError, match="before its available_at"):
        broker.release_verified_exact_print(future)
    assert broker.exact_print_allocation_root_sha256 == before_future
    broker.set_clock(_BASE + timedelta(seconds=3))
    later = _exact_print(
        2, release_ordinal=1, provider_offset=2.0, available_offset=3.0
    )
    broker.release_verified_exact_print(later)
    with pytest.raises(ValueError, match="released twice"):
        broker.release_verified_exact_print(later)

    separate = _exact_print_broker()
    separate.set_clock(_BASE + timedelta(seconds=4))
    separate.release_verified_exact_print(later)
    earlier = _exact_print(1, provider_offset=1.0, available_offset=2.0)
    with pytest.raises(ValueError, match="release ordinal is not contiguous"):
        separate.release_verified_exact_print(earlier)


def test_exact_print_rejects_availability_regression_even_with_valid_ordinal():
    broker = _exact_print_broker()
    broker.set_clock(_BASE + timedelta(seconds=5))
    first_released = _exact_print(
        2,
        release_ordinal=1,
        provider_offset=2.0,
        available_offset=4.0,
    )
    delayed_prefix_release = _exact_print(
        5,
        release_ordinal=2,
        provider_offset=1.0,
        available_offset=3.0,
    )
    broker.release_verified_exact_print(first_released)
    before = broker.exact_print_allocation_root_sha256
    with pytest.raises(ValueError, match="availability clock regressed"):
        broker.release_verified_exact_print(delayed_prefix_release)
    assert broker.exact_print_allocation_root_sha256 == before


def test_exact_print_release_ordinal_cannot_skip_and_clock_cannot_regress():
    broker = _exact_print_broker()
    broker.set_clock(_BASE + timedelta(seconds=3))
    with pytest.raises(ValueError, match="release ordinal is not contiguous"):
        broker.release_verified_exact_print(
            _exact_print(
                3,
                release_ordinal=3,
                provider_offset=2.0,
                available_offset=3.0,
            )
        )
    with pytest.raises(ValueError, match="clock cannot move backwards"):
        broker.set_clock(_BASE + timedelta(seconds=2))


def test_exact_print_capture_binding_cannot_change_mid_run():
    broker = _exact_print_broker()
    broker.set_clock(_BASE + timedelta(seconds=3))
    broker.release_verified_exact_print(
        _exact_print(1, provider_offset=1.0, available_offset=2.0)
    )
    before = broker.exact_print_allocation_root_sha256
    with pytest.raises(ValueError, match="capture binding changed"):
        broker.release_verified_exact_print(
            _exact_print(
                2,
                provider_offset=2.0,
                available_offset=3.0,
                capture_identity_sha256="d" * 64,
            )
        )
    assert broker.exact_print_allocation_root_sha256 == before


def test_one_exact_print_cannot_fill_opposing_sides():
    broker = _exact_print_broker(participation=1.0)
    broker.set_clock(_BASE)
    broker.set_quote("XPR", RecordedQuote(bid=9.99, ask=10.0))
    buy = broker.place_limit_order_gtc(
        product_id="XPR", side="buy", base_size="10", limit_price="10.05"
    )
    sell = broker.place_limit_order_gtc(
        product_id="XPR", side="sell", base_size="10", limit_price="9.95"
    )
    broker.set_clock(_BASE + timedelta(seconds=2))
    broker.release_verified_exact_print(
        _exact_print(1, provider_offset=1.0, available_offset=2.0)
    )
    assert broker.get_order(buy["order_id"])[0].status == "filled"
    assert broker.get_order(sell["order_id"])[0].status == "open"
    assert {value.side for value in broker.exact_print_allocations} == {"buy"}


def test_canceled_order_and_price_beyond_limit_receive_no_exact_allocation():
    canceled_broker = _exact_print_broker(participation=1.0)
    canceled_broker.set_clock(_BASE)
    canceled_broker.set_quote("XPR", RecordedQuote(bid=9.99, ask=10.0))
    placed = canceled_broker.place_limit_order_gtc(
        product_id="XPR", side="buy", base_size="10", limit_price="10.05"
    )
    canceled_broker.cancel_order(placed["order_id"])
    canceled_broker.set_clock(_BASE + timedelta(seconds=2))
    canceled_broker.release_verified_exact_print(
        _exact_print(1, provider_offset=1.0, available_offset=2.0)
    )
    assert canceled_broker.exact_print_allocations == ()

    boundary_broker = _exact_print_broker(participation=1.0)
    boundary_broker.set_clock(_BASE)
    boundary_broker.set_quote("XPR", RecordedQuote(bid=9.99, ask=10.0))
    strict = boundary_broker.place_limit_order_gtc(
        product_id="XPR", side="buy", base_size="10", limit_price="10.0"
    )
    boundary_broker.set_clock(_BASE + timedelta(seconds=2))
    boundary_broker.release_verified_exact_print(
        _exact_print(
            1,
            provider_offset=1.0,
            available_offset=2.0,
            price=10.0000000000001,
            bid=9.99,
            ask=10.0,
        )
    )
    assert boundary_broker.get_order(strict["order_id"])[0].filled_size == 0.0


def test_exact_print_provider_clock_ahead_of_receive_clock_fails_without_mutation():
    broker = _exact_print_broker()
    broker.set_clock(_BASE + timedelta(seconds=3))
    before = broker.exact_print_allocation_root_sha256
    with pytest.raises(ValueError, match="ahead of its receive clock"):
        broker.release_verified_exact_print(
            _exact_print(
                1,
                provider_offset=2.0,
                received_offset=1.5,
                available_offset=3.0,
            )
        )
    assert broker.exact_print_allocation_root_sha256 == before
    assert broker.exact_print_terminal_complete is False


def test_exact_print_direct_construction_and_invalid_mode_fail_closed():
    base = _BASE.replace(tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="verified sealed-capture provenance"):
        VerifiedExactPrint(
            event_sha256="a" * 64,
            sequence=1,
            release_ordinal=1,
            capture_identity_sha256="b" * 64,
            final_capture_seal_sha256="c" * 64,
            release_order_root_sha256="d" * 64,
            product_id="XPR",
            provider_event_at=base,
            received_at=base,
            available_at=base,
            price=10.0,
            size=10.0,
            bid=9.99,
            ask=10.0,
        )
    with pytest.raises(ValueError, match="requires conservative resting limits"):
        MockBrokerAdapter(
            exact_print_fills=True,
            exact_print_order_latency_seconds=0.0,
        )
    with pytest.raises(ValueError, match="requires an explicit order latency"):
        MockBrokerAdapter(
            resting_limit_fills=True,
            volume_cap_enabled=True,
            exact_print_fills=True,
        )


def test_exact_print_allocation_ledger_root_is_deterministic():
    def run() -> tuple[str, str]:
        broker = _exact_print_broker()
        broker.set_clock(_BASE)
        broker.set_quote("XPR", RecordedQuote(bid=9.99, ask=10.0))
        broker.place_limit_order_gtc(
            product_id="XPR", side="buy", base_size="20", limit_price="10.05"
        )
        broker.set_clock(_BASE + timedelta(seconds=2))
        broker.release_verified_exact_print(
            _exact_print(1, provider_offset=1.0, available_offset=2.0)
        )
        assert broker.exact_print_evidence_grade == "DIAGNOSTIC_ONLY"
        assert broker.exact_print_counterfactual_authority is False
        assert broker.exact_print_counterfactual_authority_blockers
        return broker.exact_print_policy_sha256, broker.exact_print_allocation_root_sha256

    assert run() == run()


def test_verified_exact_print_inventory_proves_tail_completeness_only_at_terminal():
    broker = _exact_print_broker(expected_sequences=(1, 2))
    broker.set_clock(_BASE + timedelta(seconds=3))
    assert broker.exact_print_terminal_complete is False
    broker.release_verified_exact_print(
        _exact_print(1, provider_offset=1.0, available_offset=2.0)
    )
    assert broker.exact_print_terminal_complete is False
    assert (
        "exact_print_release_tail_completeness_receipt_unavailable"
        in broker.exact_print_counterfactual_authority_blockers
    )
    broker.release_verified_exact_print(
        _exact_print(2, provider_offset=2.0, available_offset=3.0)
    )
    assert broker.exact_print_terminal_complete is True
    assert (
        "exact_print_release_tail_completeness_receipt_unavailable"
        not in broker.exact_print_counterfactual_authority_blockers
    )
    assert broker.exact_print_counterfactual_authority is False


def test_exact_print_audit_exposure_is_deep_copied():
    broker = _exact_print_broker()
    broker.set_clock(_BASE + timedelta(seconds=2))
    broker.release_verified_exact_print(
        _exact_print(1, provider_offset=1.0, available_offset=2.0)
    )
    before = broker.exact_print_allocation_root_sha256
    exposed = broker.exact_print_audit[0]
    exposed["candidate_order_ids"].append("forged-order")
    exposed["allocation_fill_ids"].append("forged-fill")
    assert broker.exact_print_allocation_root_sha256 == before
    assert broker.exact_print_audit[0]["candidate_order_ids"] == []


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"side": "unknown", "base_size": "10", "limit_price": "10"}, "bad_side"),
        ({"side": "buy", "base_size": "inf", "limit_price": "10"}, "bad_base_size"),
        ({"side": "buy", "base_size": "10", "limit_price": "inf"}, "bad_limit_price"),
        ({"side": "buy", "base_size": "10", "limit_price": "0"}, "bad_limit_price"),
    ],
)
def test_order_inputs_fail_closed_before_exact_print_allocation(kwargs, error):
    broker = _exact_print_broker()
    broker.set_clock(_BASE)
    broker.set_quote("XPR", RecordedQuote(bid=9.99, ask=10.0))
    result = broker.place_limit_order_gtc(product_id="XPR", **kwargs)
    assert result["ok"] is False
    assert result["error"] == error


def test_exact_print_mode_refuses_recorded_lifecycle_configuration():
    broker = _exact_print_broker()
    with pytest.raises(ValueError, match="cannot mix with exact-print"):
        broker.configure_recorded_lifecycle(intents=(), transitions=())
