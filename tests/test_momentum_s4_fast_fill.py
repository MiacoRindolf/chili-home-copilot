"""CHUNK 3 (S4 fast executor + rail governor) — fast-fill + flood-safety tests.

The engine's admissions must turn into fills in a few RTTs (not the 2–15s tick-coupled
latency), AND multi-admission must never flood / 429 the broker rail. Three default-ON,
kill-switched levers, ALL existing bounds preserved:

  A. INLINE MICRO-REPEG — the bounded entry repegs run WITHIN one tick (re-reading the
     live ask each iter) instead of one repeg per external tick. EVERY bound preserved:
     the cumulative-spread ceiling (``_entry_repeg_price``), the risk-first re-size on
     each repeg, the max-repeg counter, the equity/fresh-quote gate. NEVER past ceiling.
  B. FAST ACK-POLL — confirm the fill by polling ``get_order`` at the measured RTT
     (geometric widen) WITHIN the tick, bounded by the existing rest-bars window + a hard
     iter cap, adopting immediately on confirm.
  C. THE ADAPTIVE RAIL GOVERNOR — a process-local token bucket SHARED by every lane rail
     call (places AND get_order polls — get_order is a LIST endpoint on the same budget).
     Conservative start; WIDEN-on-success, HALVE-on-429; empty bucket WAITS briefly then
     DEFERS (never a silent drop). This is what makes deleting the slot count safe.

  D. FLAG-OFF (all three) ⇒ byte-identical to the deployed order path.

The adapter is MOCKED — NO real broker calls. docs/DESIGN/MOMENTUM_ENGINE.md §3 / Phase 5.
"""

from __future__ import annotations

import inspect
import time
import types

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner, rail_governor
from app.services.trading.momentum_neural.live_runner import (
    _entry_repeg_price,
    _fast_ack_poll_entry,
    _governed_get_order,
    _governed_place,
    _measured_rail_rtt_s,
    _note_rail_rtt,
)
from app.services.trading.momentum_neural.risk_policy import compute_risk_first_quantity


# ─────────────────────────────────────────────────────────────────────────────
# Stubs (NO real broker). A minimal NormalizedOrder-ish object + a stub adapter.
# ─────────────────────────────────────────────────────────────────────────────


class _StubOrder:
    def __init__(self, status="confirmed", filled_size=0.0, order_id="oid-1",
                 average_filled_price=None):
        self.status = status
        self.filled_size = filled_size
        self.order_id = order_id
        self.average_filled_price = average_filled_price


class _StubSess:
    def __init__(self, user_id=42, symbol="ABCD"):
        self.user_id = user_id
        self.symbol = symbol


class _StubAdapter:
    """Records every rail call so tests can count places/polls and script outcomes."""

    def __init__(self, *, order_sequence=None, place_result=None, asks=None):
        # get_order returns successive orders from this sequence (last one repeats).
        self._order_seq = list(order_sequence or [])
        self._order_i = 0
        self._place_result = place_result or {"ok": True, "order_id": "oid-new"}
        self._asks = list(asks or [])
        self._ask_i = 0
        self.get_order_calls = 0
        self.place_calls = 0

    def get_order(self, oid):
        self.get_order_calls += 1
        if self._order_seq:
            o = self._order_seq[min(self._order_i, len(self._order_seq) - 1)]
            self._order_i += 1
        else:
            o = None
        return o, None  # (order, freshness)

    def place_limit_order_gtc(self, **kw):
        self.place_calls += 1
        if callable(self._place_result):
            return self._place_result(self.place_calls, kw)
        return dict(self._place_result)


@pytest.fixture(autouse=True)
def _isolate_governor_and_rtt(monkeypatch):
    """Each test starts with a fresh governor registry + a reset RTT EMA."""
    rail_governor.reset_for_tests()
    monkeypatch.setattr(live_runner, "_RAIL_RTT_EMA_S", None, raising=False)
    yield
    rail_governor.reset_for_tests()


# ─────────────────────────────────────────────────────────────────────────────
# (C) THE ADAPTIVE RAIL GOVERNOR — the load-bearing flood-safety component.
# ─────────────────────────────────────────────────────────────────────────────


def _gov_settings(**over):
    """A settings-like object carrying ONLY the governor knobs (so a test never mutates
    the global settings singleton)."""
    base = dict(
        chili_momentum_entry_placement_governor_enabled=True,
        chili_momentum_rail_governor_start_rps=2.0,
        chili_momentum_rail_governor_min_rps=0.25,
        chili_momentum_rail_governor_max_rps=20.0,
        chili_momentum_rail_governor_burst=4.0,
        chili_momentum_rail_governor_max_wait_s=0.0,
        chili_momentum_rail_governor_widen_after_successes=3,
        chili_momentum_rail_governor_widen_factor=1.25,
        chili_momentum_rail_governor_halve_factor=0.5,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def test_governor_rate_limits_a_burst_to_the_bucket():
    """A burst of N acquisitions against a burst-capacity-K bucket grants AT MOST K in
    the window (the rest DEFER) — this is the flood ceiling that replaces the slot
    count. With max_wait_s=0 the window is instantaneous, so exactly `burst` are granted."""
    s = _gov_settings(chili_momentum_rail_governor_burst=4.0,
                      chili_momentum_rail_governor_max_wait_s=0.0)
    grants = defers = 0
    for _ in range(20):
        r = rail_governor.acquire_rail(s, lane_key="burst")
        if r.acquired:
            grants += 1
        else:
            defers += 1
            assert r.deferred is True
    assert grants == 4              # never more than the bucket capacity in the window
    assert defers == 16
    snap = rail_governor.get_bucket("burst").snapshot()
    assert snap["grants"] == 4
    assert snap["defers"] == 16


def test_governor_429_halves_rate_success_widens_rate():
    """The rate SELF-DISCOVERS: a 429 HALVES the refill rate (multiplicative decrease)
    and a run of clean calls WIDENS it (toward the max). No fixed RPS."""
    s = _gov_settings(chili_momentum_rail_governor_start_rps=4.0,
                      chili_momentum_rail_governor_widen_after_successes=3,
                      chili_momentum_rail_governor_widen_factor=2.0,
                      chili_momentum_rail_governor_halve_factor=0.5)
    b = rail_governor.get_bucket("adapt", rail_governor._config_from_settings(s))
    assert b.snapshot()["refill_rps"] == 4.0
    # A 429 halves: 4.0 -> 2.0, and drains the bucket (immediate backoff).
    rail_governor.note_rail_outcome(s, {"ok": False, "error": "HTTP 429 rate limit"},
                                    lane_key="adapt")
    assert b.snapshot()["refill_rps"] == 2.0
    assert b.snapshot()["rate_limit_events"] == 1
    # 3 clean successes widen once: 2.0 -> 4.0.
    for _ in range(3):
        rail_governor.note_rail_outcome(s, {"ok": True}, lane_key="adapt")
    assert b.snapshot()["refill_rps"] == 4.0
    assert b.snapshot()["widens"] == 1


def test_governor_rate_is_bounded_min_and_max():
    """Adaptation can never widen past max_rps nor halve below min_rps (a sane band, not
    a runaway)."""
    s = _gov_settings(chili_momentum_rail_governor_start_rps=1.0,
                      chili_momentum_rail_governor_min_rps=0.5,
                      chili_momentum_rail_governor_max_rps=2.0,
                      chili_momentum_rail_governor_widen_after_successes=1,
                      chili_momentum_rail_governor_widen_factor=10.0,
                      chili_momentum_rail_governor_halve_factor=0.01)
    b = rail_governor.get_bucket("bound", rail_governor._config_from_settings(s))
    for _ in range(10):
        rail_governor.note_rail_outcome(s, {"ok": True}, lane_key="bound")
    assert b.snapshot()["refill_rps"] == 2.0   # clamped at max
    for _ in range(10):
        rail_governor.note_rail_outcome(s, {"ok": False, "error": "429"}, lane_key="bound")
    assert b.snapshot()["refill_rps"] == 0.5   # clamped at min


def test_governor_registry_is_bounded_hard_cap():
    """The process-local registry has a HARD CAP (bounded memory, CLAUDE.md cache rule):
    creating more buckets than the cap evicts the oldest down to the cap."""
    rail_governor.reset_for_tests()
    cap = rail_governor._MAX_BUCKETS
    for i in range(cap + 25):
        rail_governor.get_bucket(f"lane-{i}")
    assert len(rail_governor._REGISTRY) <= cap


def test_governor_empty_bucket_waits_then_grants_within_window():
    """With a non-zero max_wait_s, an empty bucket BLOCKS briefly for a refill and then
    GRANTS (the call is delayed, not dropped) — the 'WAIT briefly' half of the contract."""
    s = _gov_settings(chili_momentum_rail_governor_start_rps=50.0,  # ~20ms/token
                      chili_momentum_rail_governor_burst=1.0,
                      chili_momentum_rail_governor_max_wait_s=1.0)
    # Drain the single token.
    r1 = rail_governor.acquire_rail(s, lane_key="wait")
    assert r1.acquired is True
    t0 = time.monotonic()
    r2 = rail_governor.acquire_rail(s, lane_key="wait")
    waited = time.monotonic() - t0
    assert r2.acquired is True       # granted after a short wait (not deferred)
    assert waited > 0.0
    assert rail_governor.get_bucket("wait").snapshot()["waits"] >= 1


def test_governor_flag_off_is_instant_passthrough():
    """Governor OFF ⇒ acquire returns acquired=True instantly and NEVER creates a bucket
    (byte-identical to the deployed path: no governor in the loop)."""
    s = _gov_settings(chili_momentum_entry_placement_governor_enabled=False)
    for _ in range(100):
        r = rail_governor.acquire_rail(s, lane_key="off")
        assert r.acquired is True and r.waited_s == 0.0
    assert "off" not in rail_governor._REGISTRY  # no bucket ever created


def test_is_rate_limit_outcome_recognizes_429_shapes_but_not_generic_errors():
    """Only explicit rate-limit signals halve the rate — a generic place error must NOT
    falsely halve (over-throttle) NOR widen."""
    rl = rail_governor.is_rate_limit_outcome
    assert rl({"ok": False, "error": "HTTP 429 Too Many Requests"}) is True
    assert rl({"ok": False, "status_code": 429}) is True
    assert rl({"ok": False, "error": "rate-limited, retry"}) is True
    assert rl(429) is True
    assert rl(_StubOrder(status="429")) is True
    # NOT rate limits:
    assert rl({"ok": True}) is False
    assert rl({"ok": False, "error": "EQUITY_SUITABILITY"}) is False
    assert rl(None) is False
    assert rl(_StubOrder(status="filled")) is False


# ─────────────────────────────────────────────────────────────────────────────
# Governor wiring into the place + poll wrappers (the SHARED budget).
# ─────────────────────────────────────────────────────────────────────────────


def test_governed_place_defers_when_bucket_empty_no_real_call(monkeypatch):
    """When the shared bucket is empty, _governed_place returns a DEFER result WITHOUT
    calling the adapter (no flood) — the caller's not-ok branch re-watches. Never a
    silent drop: the deferral is signalled (deferred=True)."""
    monkeypatch.setattr(settings, "chili_momentum_entry_placement_governor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_burst", 1.0)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_max_wait_s", 0.0)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_start_rps", 1.0)
    sess = _StubSess(user_id=7)
    ad = _StubAdapter(place_result={"ok": True, "order_id": "x"})
    # First place drains the only token.
    r1 = _governed_place(ad, ad.place_limit_order_gtc, sess=sess, client_order_id="c1")
    assert r1["ok"] is True and ad.place_calls == 1
    # Second place: bucket empty + max_wait 0 -> DEFER, adapter NOT called again.
    r2 = _governed_place(ad, ad.place_limit_order_gtc, sess=sess, client_order_id="c2")
    assert r2["ok"] is False and r2["deferred"] is True
    assert r2["error"] == "rail_governor_deferred"
    assert ad.place_calls == 1  # no real broker call on the deferred placement


def test_governed_place_and_poll_share_the_same_budget(monkeypatch):
    """get_order POLLS and order PLACES draw from the SAME per-user bucket (get_order is
    a LIST endpoint on the same rail budget) — so a poll can exhaust the budget a place
    would need, exactly the shared-limiter contract that makes fast-poll safe."""
    monkeypatch.setattr(settings, "chili_momentum_entry_placement_governor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_burst", 2.0)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_max_wait_s", 0.0)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_start_rps", 1.0)
    sess = _StubSess(user_id=99)
    ad = _StubAdapter(order_sequence=[_StubOrder(status="confirmed")],
                      place_result={"ok": True, "order_id": "x"})
    # Two polls drain the 2-token bucket...
    _governed_get_order(ad, "oid", sess=sess)
    _governed_get_order(ad, "oid", sess=sess)
    # ...so a place by the SAME user now DEFERS (shared budget).
    r = _governed_place(ad, ad.place_limit_order_gtc, sess=sess, client_order_id="c")
    assert r.get("deferred") is True
    assert ad.place_calls == 0


class _RateLimitExc(Exception):
    """Mimics RhMcpError('MCP HTTP 429 ...', code='http_429') on the poll path."""

    def __init__(self, message="MCP HTTP 429 for get_order", code="http_429"):
        super().__init__(message)
        self.code = code


class _Rate429Adapter:
    """get_order RAISES a 429-shaped exception (the adapter now SURFACES a poll-path
    rate-limit instead of masking it to (None, fresh))."""

    def get_order(self, oid):
        raise _RateLimitExc()


def test_get_order_429_halves_the_bucket_rate(monkeypatch):
    """POLL-PATH 429 UNMASKING: a 429 RhMcpError surfaced from get_order must HALVE the
    shared bucket rate (not read as a success that widens INTO the limit). The governed
    wrapper re-raises after feeding the outcome, so the caller still sees the exception."""
    monkeypatch.setattr(settings, "chili_momentum_entry_placement_governor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_start_rps", 4.0)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_min_rps", 0.25)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_halve_factor", 0.5)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_max_wait_s", 0.0)
    sess = _StubSess(user_id=1234)
    lane = live_runner._rail_lane_key(sess)
    b = rail_governor.get_bucket(lane, rail_governor._config_from_settings(settings))
    rps_before = b.snapshot()["refill_rps"]
    with pytest.raises(_RateLimitExc):
        _governed_get_order(_Rate429Adapter(), "oid", sess=sess)
    snap = b.snapshot()
    assert snap["rate_limit_events"] == 1
    assert snap["refill_rps"] == pytest.approx(rps_before * 0.5)  # HALVED, not widened


def test_is_rate_limit_outcome_recognizes_exception_429():
    """The governor recognizes a 429 surfaced as an EXCEPTION (str text and/or typed
    .code) — so a re-raised poll-path RhMcpError halves the rate."""
    rl = rail_governor.is_rate_limit_outcome
    assert rl(_RateLimitExc("MCP HTTP 429 for get_order", code="http_429")) is True
    assert rl(_RateLimitExc("opaque", code="http_429")) is True       # typed code alone
    assert rl(_RateLimitExc("rate limit exceeded", code="")) is True  # text alone
    assert rl(Exception("EQUITY_SUITABILITY rejected")) is False      # generic -> neutral


def test_governed_get_order_none_result_is_neutral_not_a_widen(monkeypatch):
    """A bare None get_order result is AMBIGUOUS (not-found vs swallowed transient) so it
    is NEUTRAL — it must NOT count as a success that widens the rate toward the limit."""
    monkeypatch.setattr(settings, "chili_momentum_entry_placement_governor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_start_rps", 2.0)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_widen_after_successes", 1)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_widen_factor", 2.0)
    monkeypatch.setattr(settings, "chili_momentum_rail_governor_max_wait_s", 0.0)
    sess = _StubSess(user_id=555)
    lane = live_runner._rail_lane_key(sess)
    b = rail_governor.get_bucket(lane, rail_governor._config_from_settings(settings))
    rps_before = b.snapshot()["refill_rps"]
    ad = _StubAdapter(order_sequence=[None, None, None])  # get_order returns (None, None)
    for _ in range(5):
        _governed_get_order(ad, "oid", sess=sess)
    assert b.snapshot()["refill_rps"] == rps_before   # never widened on bare-None polls
    assert b.snapshot()["widens"] == 0


def test_fast_poll_worst_case_blocking_is_single_digit_seconds_at_prod_defaults(monkeypatch):
    """IDLE-IN-TRANSACTION GUARD: tick_live_session holds a row-lock for the whole call,
    so the fast-poll must NOT sleep for minutes. At PRODUCTION defaults (seed=0.25,
    widen=1.6, max_iters=12) with a large interval window, the TOTAL in-tick wall-clock
    is capped to single-digit seconds (the new max_wall_s ceiling), NOT the ~117s the
    geometric widen would reach."""
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_entry_placement_governor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_seed_interval_s", 0.25)
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_widen_factor", 1.6)
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_max_iters", 12)
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_max_wall_s", 5.0)
    sess = _StubSess()
    ad = _StubAdapter(order_sequence=[_StubOrder(status="confirmed", filled_size=0.0)])
    # interval_window_s mirrors rest_bars*interval at a 5m bar (~600s) — the OLD binding
    # window. The new ceiling must bound the actual blocking regardless.
    t0 = time.monotonic()
    no, _ = _fast_ack_poll_entry(ad, "oid", sess=sess, interval_window_s=600.0)
    elapsed = time.monotonic() - t0
    assert elapsed <= 6.0          # single-digit seconds (ceiling 5s + slack), NOT ~117s
    assert no is not None and no.status == "confirmed"


def test_governed_get_order_flag_off_is_single_plain_call(monkeypatch):
    """Governor OFF ⇒ _governed_get_order calls adapter.get_order exactly once and
    returns its tuple unchanged (byte-identical)."""
    monkeypatch.setattr(settings, "chili_momentum_entry_placement_governor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_enabled", False)
    sess = _StubSess()
    ord_obj = _StubOrder(status="filled", filled_size=10.0)
    ad = _StubAdapter(order_sequence=[ord_obj])
    out = _governed_get_order(ad, "oid", sess=sess)
    assert out[0] is ord_obj and ad.get_order_calls == 1


# ─────────────────────────────────────────────────────────────────────────────
# (B) FAST ACK-POLL — confirm a fill WITHOUT an external tick.
# ─────────────────────────────────────────────────────────────────────────────


def test_fast_poll_confirms_fill_without_external_tick(monkeypatch):
    """A mock get_order that returns 'open' a few times then 'filled' is confirmed by the
    in-tick fast-poll (multiple polls in ONE call), with NO external tick. The first
    confirmed order is returned for the existing adopt path."""
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_entry_placement_governor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_seed_interval_s", 0.01)
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_widen_factor", 1.2)
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_max_iters", 12)
    sess = _StubSess()
    seq = [
        _StubOrder(status="confirmed", filled_size=0.0),
        _StubOrder(status="confirmed", filled_size=0.0),
        _StubOrder(status="filled", filled_size=25.0, average_filled_price=10.0),
        _StubOrder(status="filled", filled_size=25.0, average_filled_price=10.0),
    ]
    ad = _StubAdapter(order_sequence=seq)
    no, _fr = _fast_ack_poll_entry(ad, "oid", sess=sess, interval_window_s=2.0)
    assert no is not None and no.status == "filled" and no.filled_size == 25.0
    assert ad.get_order_calls >= 3  # polled multiple times within the single call


def test_fast_poll_flag_off_is_single_poll(monkeypatch):
    """Fast-poll OFF ⇒ exactly ONE get_order (the deployed tick-coupled confirm) —
    byte-identical. Even if the order is still open, it does NOT loop."""
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_placement_governor_enabled", False)
    sess = _StubSess()
    ad = _StubAdapter(order_sequence=[_StubOrder(status="confirmed", filled_size=0.0)])
    no, _ = _fast_ack_poll_entry(ad, "oid", sess=sess, interval_window_s=5.0)
    assert ad.get_order_calls == 1   # never polled again
    assert no is not None and no.status == "confirmed"


def test_fast_poll_window_is_bounded_no_infinite_poll(monkeypatch):
    """The fast-poll TOTAL window is bounded (rest-bars window) AND a hard iter cap — an
    order that NEVER fills returns after a bounded number of polls (never busy-spins the
    rail)."""
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_entry_placement_governor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_seed_interval_s", 0.01)
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_widen_factor", 1.5)
    monkeypatch.setattr(settings, "chili_momentum_entry_fast_poll_max_iters", 5)
    sess = _StubSess()
    ad = _StubAdapter(order_sequence=[_StubOrder(status="confirmed", filled_size=0.0)])
    no, _ = _fast_ack_poll_entry(ad, "oid", sess=sess, interval_window_s=2.0)
    # 1 initial poll + at most max_iters(5) loop polls = 6 hard ceiling.
    assert ad.get_order_calls <= 6
    assert no is not None and no.status == "confirmed"  # last poll, still open


# ─────────────────────────────────────────────────────────────────────────────
# (A) INLINE MICRO-REPEG — bounded, risk-re-sized, NEVER past the ceiling.
# ─────────────────────────────────────────────────────────────────────────────


def test_repeg_price_never_exceeds_cumulative_ceiling_as_ask_runs_up(monkeypatch):
    """The cumulative-spread ceiling is the load-bearing R:R guard: as the live ask runs
    UP across repegs, _entry_repeg_price tracks it UNTIL the ask crosses the ceiling
    (original_limit * (1 + adaptive_max_spread)), after which it returns None (stop
    chasing). No repeg price ever exceeds that ceiling — the inline loop cannot drift
    past the budget no matter how many iterations run."""
    # Pin the adaptive max spread so the ceiling is deterministic for the assert.
    monkeypatch.setattr(live_runner, "_adaptive_live_max_spread_bps", lambda *_a, **_k: 200.0)
    monkeypatch.setattr(live_runner, "_adaptive_notional_guard_multiplier",
                        lambda *, expected_move_bps=None: 1.001)
    original = 10.0
    ceiling = original * (1.0 + 200.0 / 10_000.0)  # 10.20
    last_px = original
    repegs = 0
    # Simulate the ask running up tick by tick through and PAST the ceiling.
    for ask in [10.05, 10.10, 10.15, 10.19, 10.21, 10.30]:
        px = _entry_repeg_price(original_limit_px=original, live_ask=ask,
                                expected_move_bps=None)
        if px is None:
            break  # ask ran past the cumulative budget -> stop (the loop would exit)
        assert px <= ceiling + 1e-9            # NEVER past the ceiling
        assert px >= last_px - 1e-9            # monotonic up as the ask climbs
        last_px = px
        repegs += 1
    # It chased while the ask stayed under the ceiling, then stopped at the first
    # over-ceiling ask (10.21 > 10.20) — bounded by construction.
    assert repegs == 4


def test_inline_repeg_is_bounded_by_max_repegs_and_risk_resizes(monkeypatch):
    """Drive the inline loop's EXACT bounding arithmetic with a mock ascending ask: it
    repegs AT MOST max_repegs times within one tick, each repeg RISK-FIRST re-sized at
    the chased price (qty falls as the entry climbs for a fixed max-loss/stop), and never
    past the ceiling. This reproduces the loop body using the SAME real helpers the
    runner calls, with a MOCK adapter (no broker)."""
    monkeypatch.setattr(live_runner, "_adaptive_live_max_spread_bps", lambda *_a, **_k: 500.0)
    monkeypatch.setattr(live_runner, "_adaptive_notional_guard_multiplier",
                        lambda *, expected_move_bps=None: 1.0005)
    max_repegs = 3
    original = 10.0
    max_loss = 50.0
    stop_atr_mult = 0.6
    atr_pct = 0.02  # stop distance ~ entry*atr*mult
    asks = [10.05, 10.10, 10.15, 10.20, 10.25]  # keeps running up (under the 5% ceiling)
    ad = _StubAdapter(place_result={"ok": True, "order_id": "rp"})

    n = 0
    lim_px = original
    qtys = []
    ask_i = 0
    while n < max_repegs and ask_i < len(asks):
        ask = asks[ask_i]
        new_px = _entry_repeg_price(original_limit_px=original, live_ask=ask,
                                    expected_move_bps=None)
        if not (new_px and new_px > lim_px):
            break
        qty, _meta = compute_risk_first_quantity(
            entry_price=new_px, atr_pct=atr_pct, max_loss_usd=max_loss,
            max_notional_ceiling_usd=100_000.0, base_increment=1.0, base_min_size=1.0,
            stop_atr_mult=stop_atr_mult,
        )
        if not (qty and qty >= 1.0):
            break
        res = ad.place_limit_order_gtc(limit_price=str(new_px), base_size=str(qty))
        assert res["ok"]
        qtys.append(qty)
        lim_px = new_px
        n += 1
        ask_i += 1

    # Bounded by max_repegs (the move kept running, but the counter stopped it).
    assert n == max_repegs
    assert ad.place_calls == max_repegs
    # RISK-FIRST: as the entry price climbed each repeg, the risk-sized qty is
    # non-increasing (a higher entry with the same max-loss/stop buys fewer shares).
    assert all(qtys[i] >= qtys[i + 1] for i in range(len(qtys) - 1))
    # The final resting limit never exceeded the cumulative ceiling.
    assert lim_px <= original * (1.0 + 500.0 / 10_000.0) + 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# (D) STRUCTURAL: the deployed bounds + flag gates are wired in source (so a future
# edit can't silently remove a bound or run a lever when its flag is off).
# ─────────────────────────────────────────────────────────────────────────────


def test_inline_repeg_source_preserves_every_bound_and_flag_gate():
    """The inline-repeg loop in the live runner MUST: be gated by the inline flag, be
    bounded by the max-repeg counter, re-size risk-first each iter, cap at the cumulative
    ceiling (_entry_repeg_price), re-read the live ask, and fall back to the SINGLE-repeg
    behaviour when the flag is off (`if not _inline: break`)."""
    src = inspect.getsource(live_runner)
    assert "chili_momentum_entry_inline_repeg_enabled" in src
    assert "if not _inline:" in src and "break  # one-repeg-per-tick" in src
    # Every existing bound is still referenced in/around the loop.
    assert "_rp_n < _rp_max" in src                       # max-repeg counter
    assert "chili_momentum_entry_max_repegs" in src
    assert "compute_risk_first_quantity(" in src          # risk-first re-size each iter
    assert "_entry_repeg_price(" in src                   # cumulative ceiling
    assert "is_fresh_enough(_rp_fr)" in src               # fresh-quote gate
    assert "_rp_is_equity" in src                         # equity-only gate
    assert "adapter.get_best_bid_ask(product_id)" in src  # re-read the live ask each iter


def test_place_and_polls_are_rail_governed_in_source():
    """The entry PLACE and the entry get_order POLLS go through the governor wrappers
    (the shared bucket) — so multi-admission cannot bypass the rate limiter."""
    src = inspect.getsource(live_runner)
    # The main entry place is governed.
    assert "_governed_place(\n            adapter, adapter.place_limit_order_gtc, sess=sess, **_entry_kwargs" in src
    # The confirm poll routes through the fast-poll (which governs every poll).
    assert "_fast_ack_poll_entry(" in src
    # The race-guard get_order calls are governed too.
    assert "_governed_get_order(adapter, le[\"entry_order_id\"], sess=sess)" in src
    # The repeg place is governed.
    assert "_rp_res = _governed_place(" in src


def test_governor_defer_on_entry_place_re_watches_not_errors_in_source():
    """A governor DEFER on the entry place must NOT transition to LIVE_ERROR nor write an
    entry-reject cooldown (the name is fine — the rail was busy): it stays WATCHING and
    retries. Assert the deferred branch precedes (and short-circuits before) the not-ok
    reject/error path."""
    src = inspect.getsource(live_runner)
    i_defer = src.index("live_entry_governor_deferred")
    i_skip = src.index('"skipped": "rail_governor_deferred"')
    i_reject = src.index("_write_entry_reject_cooldown")
    assert i_defer < i_skip < i_reject  # the defer branch returns before the reject path


def test_inline_repeg_cancels_prior_order_before_next_place_in_source():
    """DUPLICATE-FILL GUARD: before placing each SUBSEQUENT inline repeg, the loop must
    CANCEL the current entry_order_id at the broker and CONFIRM it terminal — never leave
    a prior repeg resting while a new one is placed (phantom 2x long). On a cancel race it
    must ADOPT a fill, and on an unconfirmed/indeterminate cancel it must STOP (not place a
    second live order). The first iteration is exempt (O0 already cancelled pre-loop)."""
    src = inspect.getsource(live_runner)
    i_loop = src.index("while (\n                                _cancel_why == \"entry_limit_left_behind\"")
    i_place = src.index("_rp_res = _governed_place(", i_loop)
    seg = src[i_loop:i_place]
    # The pre-place cancel+confirm runs only AFTER the first repeg (_rp_did True).
    assert "if _rp_did:" in seg
    assert "adapter.cancel_order(str(_rp_old_eid))" in seg
    assert "_governed_get_order(\n                                        adapter, _rp_old_eid, sess=sess" in seg
    # Adopt a fill that raced the cancel; never place on top of a real position.
    assert "inline_repeg_cancel_raced_fill_adopt" in seg
    # Unconfirmed / indeterminate cancel -> STOP (do not place a second live order).
    assert "inline_repeg_cancel_unconfirmed" in seg
    assert "_rp_post is None or _order_open(_rp_post)" in seg


def test_governor_defer_on_confirm_poll_stays_pending_not_live_error_in_source():
    """A governor DEFER (or transient None) on the ENTRY-CONFIRM poll must NOT push a
    healthy resting order to LIVE_ERROR and orphan it: when `no is None` the runner emits
    entry_confirm_deferred and returns ok=True / pending with the entry pointer intact —
    BEFORE the live_error/STATE_LIVE_ERROR fall-through."""
    src = inspect.getsource(live_runner)
    i_guard = src.index('"pending": "entry_confirm_deferred"')
    i_liveerr = src.index('_emit(db, sess, "live_error", {"reason": "entry_order_state"')
    assert i_guard < i_liveerr            # the None-guard short-circuits before LIVE_ERROR
    # The guard keeps the session alive (no STATE_LIVE_ERROR / no bad_entry_order in it).
    seg = src[src.index("if no is None:", i_guard - 400):i_liveerr]
    assert "entry_confirm_deferred" in seg
    assert "STATE_LIVE_ERROR" not in seg
    assert '"ok": False' not in seg


def test_rtt_ema_is_bounded_and_rejects_absurd_measurements():
    """The measured-RTT EMA is a bounded scalar that ignores NaN / absurd values (a
    one-off stall or clock skew can't poison the adaptive inter-repeg / poll delays)."""
    live_runner._RAIL_RTT_EMA_S = None
    _note_rail_rtt(0.05)
    assert abs(_measured_rail_rtt_s() - 0.05) < 1e-9
    _note_rail_rtt(float("nan"))     # ignored
    _note_rail_rtt(9999.0)           # absurd -> ignored
    _note_rail_rtt(-1.0)             # negative -> ignored
    # EMA still reflects only the valid measurement(s).
    assert 0.0 <= _measured_rail_rtt_s() <= 1.0
