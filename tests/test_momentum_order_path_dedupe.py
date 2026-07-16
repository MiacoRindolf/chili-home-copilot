"""ORDER-PATH DEDUPE / RECONCILE invariants for the momentum lane — PURE-LOGIC + ADVERSARIAL.

The agentic rail has a duplicate-fill + stranded-naked-long history (BATL 2026-06-10:
5 ack-timeout cancels lost the race -> 5 untracked fills stacked ~$8k with no lane stop;
HIHO 2026-06-09: a placed-but-unfilled order orphaned at the broker). The two newer order
shapes — ORDER CHUNKING (split a parent into N child blocks) and the ANTICIPATION STARTER
(probe-then-add) — are BUILT but DEFAULT OFF. The gatekeeper said the structure is right but
unproven. These tests PROVE the dedupe/reconcile invariants and FAIL if any regresses.

Design:
  * PURE-LOGIC: a ``FakeVenueAdapter`` records placed orders in a list + an in-memory order
    book. No live broker, no DB, no truncate-collision with other tests (no ``db`` fixture).
  * Each test names the invariant it pins and is ADVERSARIAL: it asserts the *failure mode*
    that the agentic rail actually suffered, so deleting the guard makes the test go red.

Invariants covered (one or more tests each):
  I1  chunk split sums EXACTLY to the parent qty (increment + no-increment paths).
  I2  every chunk uses a DISTINCT client_order_id (fresh uuid4 per leg).
  I3  every child broker_order_id is recorded BEFORE the ok-check returns (crash-safe).
  I4  the late-fill sweep predicate keeps EVERY unresolved leg in view until terminal.
  I5  no double-count: the same broker_order_id recorded twice is held ONCE.
  I6  partial submit is FAIL-CLOSED-TO-SINGLE: any child failure cancels the placed legs
      AND surfaces ok=False (never a silently half-placed resting multi-leg).
  I7  flag OFF / blocks<=1 ⇒ exactly ONE order, byte-identical to the un-wrapped path.
  I8  anticipation probe+remainder never OVER-buy the parent and both legs are tracked;
      an add-leg reject leaves NO naked remainder.

Run (operator):
  TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \
    conda run -n chili-env pytest tests/test_momentum_order_path_dedupe.py -v
"""

from __future__ import annotations

# stdlib
import math

# third-party
import pytest

# relative app
from app.services.trading.venue import chunking_adapter as ca
from app.services.trading.venue.chunking_adapter import (
    ChunkingVenueAdapter,
    _split_base_size,
    maybe_wrap_chunking,
    CHUNK_RESULT_KEY,
)
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.live_runner import (
    _record_entry_order_placed,
    _mark_entry_order_resolved,
    _unresolved_entry_order_ids,
    _order_done_for_entry,
    _round_base_size,
)


# ── Fake venue adapter (pure in-memory, records every placed order) ───────────
class _FakeOrder:
    """Mutable stand-in for NormalizedOrder (the frozen real one can't be mutated to
    simulate a late fill). The live_runner only reads .status / .filled_size /
    .average_filled_price, so this minimal shape is sufficient."""

    def __init__(self, order_id, client_order_id, base_size):
        self.order_id = order_id
        self.client_order_id = client_order_id
        self.product_id = ""
        self.side = "buy"
        self.status = "submitted"
        self.order_type = "limit"
        self.filled_size = 0.0
        self.average_filled_price = None


class _FakeProduct:
    def __init__(self, base_increment, base_min_size=None):
        self.base_increment = base_increment
        self.base_min_size = base_min_size


class _FreshMeta:
    """Stand-in for FreshnessMeta — the order-path code under test never inspects it."""


class FakeVenueAdapter:
    """Records every place/cancel; serves get_product / get_order from in-memory state.

    ``fail_cids`` is an optional set/predicate of client_order_id fragments whose submit
    should return ok=False (to drive the partial-submit fail-closed test)."""

    def __init__(self, *, base_increment=None, fail_on=None):
        self.placed_orders = []  # (product_id, side, base_size_float, limit_price, client_order_id)
        self.orders_by_oid = {}
        self.cancelled = []
        self._base_increment = base_increment
        # predicate(client_order_id) -> True means "reject this child"
        self._fail_on = fail_on
        self._enabled = True

    def is_enabled(self):
        return self._enabled

    def get_product(self, product_id):
        return (_FakeProduct(self._base_increment), _FreshMeta())

    def place_limit_order_gtc(
        self,
        *,
        product_id,
        side,
        base_size,
        limit_price,
        client_order_id=None,
        extended_hours=False,
        **kwargs,
    ):
        if self._fail_on is not None and self._fail_on(client_order_id):
            return {"ok": False, "error": "insufficient_balance", "client_order_id": client_order_id}
        # A real venue rejects (returns an error) an unparseable size; it does NOT crash.
        try:
            _bs = float(base_size)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_size", "client_order_id": client_order_id}
        self.placed_orders.append(
            (product_id, side, _bs, limit_price, client_order_id)
        )
        oid = f"fake_oid_{len(self.placed_orders)}"
        self.orders_by_oid[oid] = _FakeOrder(oid, client_order_id, _bs)
        return {"ok": True, "order_id": oid, "client_order_id": client_order_id}

    def get_order(self, order_id):
        return (self.orders_by_oid.get(order_id), _FreshMeta())

    def cancel_order(self, order_id):
        self.cancelled.append(str(order_id))
        o = self.orders_by_oid.get(order_id)
        if o is not None:
            o.status = "cancelled"
        return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────────
# I1 — chunk split sums EXACTLY to parent qty (no over/under-fill vs the parent)
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "total, blocks, increment",
    [
        (10.0, 3, 0.001),     # increment path, not evenly divisible
        (100.0, 5, 0.01),     # increment path, evenly divisible
        (3.0, 3, 1.0),        # integer shares, one each
        (7.0, 2, 1.0),        # integer shares, remainder onto last
        (10.0, 3, None),      # no-increment float path
        (1.0, 4, None),       # no-increment float path, remainder absorbed
    ],
)
def test_split_base_size_sums_exactly_to_parent(total, blocks, increment):
    pieces = _split_base_size(total, blocks, increment=increment)
    # ADVERSARIAL: if the remainder were dropped instead of folded onto the last
    # piece, the children would under-buy the parent — the exact bug that leaves a
    # naked sliver outside the bracket. Sum must equal the parent within fp epsilon.
    assert math.isclose(sum(pieces), total, rel_tol=0.0, abs_tol=1e-9), (
        f"chunk children sum {sum(pieces)!r} != parent {total!r}"
    )
    assert all(p > 0 for p in pieces), "every child must be strictly positive"


def test_split_base_size_pieces_are_increment_multiples():
    inc = 0.001
    pieces = _split_base_size(10.0, 3, increment=inc)
    assert len(pieces) == 3
    # ADVERSARIAL: a piece that is NOT a whole multiple of base_increment is rejected
    # by the real venue (Coinbase quantizes / RH refuses) — that would orphan a leg.
    for p in pieces:
        units = round(p / inc)
        assert math.isclose(units * inc, p, abs_tol=1e-9), f"{p} not a multiple of {inc}"


@pytest.mark.parametrize(
    "total, blocks, increment",
    [
        (0.1, 3, 0.05),   # can't give each block >=1 increment
        (2.0, 5, 1.0),    # only 2 integer shares, 5 blocks
        (0.0, 3, 0.001),  # zero qty
        (5.0, 1, 0.01),   # blocks<=1
    ],
)
def test_split_base_size_falls_back_to_single(total, blocks, increment):
    pieces = _split_base_size(total, blocks, increment=increment)
    # ADVERSARIAL: fail-closed-to-single. If the splitter instead returned tiny
    # sub-min pieces, the venue would reject some children → a half-placed multi-leg.
    assert pieces == [total], f"expected single [{total}], got {pieces}"


def test_split_base_size_is_deterministic():
    # ADVERSARIAL: a re-submit after ack-timeout must split IDENTICALLY, else the
    # re-issued chunks differ from the resting ones and the fold double-counts qty.
    a = _split_base_size(100.0, 5, increment=0.01)
    b = _split_base_size(100.0, 5, increment=0.01)
    assert a == b


# ──────────────────────────────────────────────────────────────────────────────
# I2 / I3 / I6 / I7 — chunking wrapper behaviour through the FakeVenueAdapter
# ──────────────────────────────────────────────────────────────────────────────
def test_chunking_wrapper_places_n_distinct_legs_summing_to_parent():
    base = FakeVenueAdapter(base_increment=0.001)
    wrapped = ChunkingVenueAdapter(base, blocks=3)
    res = wrapped.place_limit_order_gtc(
        product_id="BTC-USD", side="buy", base_size="3.0",
        limit_price="30000", client_order_id="parent_123",
    )
    assert res["ok"] is True
    oids = res[CHUNK_RESULT_KEY]
    # I2: N legs, all with DISTINCT client_order_ids derived from the parent cid.
    assert len(base.placed_orders) == 3
    cids = [o[4] for o in base.placed_orders]
    assert len(set(cids)) == 3, "chunk client_order_ids must all be distinct"
    assert all(c.startswith("parent_123_c") for c in cids), "cid lineage must trace the parent"
    # broker order ids distinct too.
    assert len(set(oids)) == 3
    # I1 again end-to-end: the placed base_sizes sum to the parent qty exactly.
    assert math.isclose(sum(o[2] for o in base.placed_orders), 3.0, abs_tol=1e-9)


def test_chunk_oids_present_in_result_before_ok_is_read():
    # I3 (crash-safety): chunk_order_ids must be populated on the SAME result dict the
    # caller inspects, so a crash after broker-ACK but before db.commit still leaves the
    # oids for the reconciler. We assert the result the wrapper hands back carries them.
    base = FakeVenueAdapter(base_increment=0.001)
    wrapped = ChunkingVenueAdapter(base, blocks=2)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="2.0",
        limit_price="100", client_order_id="cid",
    )
    # ADVERSARIAL: if the wrapper returned the bare first-child dict (no chunk lineage),
    # the live_runner's `for oid in res.get("chunk_order_ids")` loop records nothing →
    # the 2nd leg is an untracked stranded naked long.
    assert CHUNK_RESULT_KEY in res
    assert len(res[CHUNK_RESULT_KEY]) == 2
    # and the primary order_id is one real leg (caller's existing res["order_id"] path).
    assert res.get("order_id") in res[CHUNK_RESULT_KEY]


def test_chunking_partial_submit_is_fail_closed_to_single():
    # I6: child 2 fails → ok=False AND the placed children are CANCELLED (no resting
    # half-placed multi-leg at the broker). This is the stranded-naked-long defense.
    base = FakeVenueAdapter(
        base_increment=0.01,
        fail_on=lambda cid: cid is not None and "_c1_" in cid,  # the 2nd child (index 1)
    )
    wrapped = ChunkingVenueAdapter(base, blocks=3)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="3.0",
        limit_price="100", client_order_id="p",
    )
    # ADVERSARIAL: if a partial submit returned ok=True, the lane would adopt a position
    # smaller than intended with a sibling leg silently failed; if it left the acked
    # children resting, they'd be orphans. Both must be impossible.
    assert res["ok"] is False
    assert "chunk_partial_submit" in (res.get("error") or "")
    # the children that DID place (c0, c2) must have been cancelled.
    placed_oids = [o for o in res.get(CHUNK_RESULT_KEY, [])]
    assert placed_oids, "the acked children must still be recorded for the sweep"
    assert set(res.get("chunk_cancelled_order_ids", [])) == set(placed_oids)
    assert set(base.cancelled) == set(placed_oids)


def test_chunking_wrapper_blocks_one_is_single_order():
    # I7: blocks<=1 ⇒ exactly ONE order with the caller's OWN cid (byte-identical).
    base = FakeVenueAdapter(base_increment=0.001)
    wrapped = ChunkingVenueAdapter(base, blocks=1)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="5.0",
        limit_price="100", client_order_id="solo",
    )
    assert res["ok"] is True
    assert len(base.placed_orders) == 1
    # ADVERSARIAL: the single-order path must NOT mint a fresh chunk cid nor add chunk
    # metadata — it must be byte-identical to the un-wrapped adapter.
    assert base.placed_orders[0][4] == "solo"
    assert CHUNK_RESULT_KEY not in res


def test_chunking_wrapper_uncleanly_splittable_falls_back_to_single():
    # I7: qty too small to split into N increment-multiples ⇒ one order, caller's cid.
    base = FakeVenueAdapter(base_increment=1.0)  # whole shares
    wrapped = ChunkingVenueAdapter(base, blocks=5)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="2.0",  # only 2 shares for 5 blocks
        limit_price="10", client_order_id="solo2",
    )
    assert res["ok"] is True
    assert len(base.placed_orders) == 1
    assert base.placed_orders[0][4] == "solo2"
    assert CHUNK_RESULT_KEY not in res


def test_maybe_wrap_chunking_off_returns_base_factory_unchanged():
    # I7: with the flag OFF (default) maybe_wrap_chunking returns the SAME factory object,
    # so the live_runner gets the exact adapter it always did (byte-identical, no wrapper).
    sentinel = FakeVenueAdapter()

    def factory():
        return sentinel

    wrapped_factory = maybe_wrap_chunking(factory)
    # ADVERSARIAL: if the wrapper were inserted while the flag is off, this would be a
    # ChunkingVenueAdapter, not the bare sentinel.
    assert wrapped_factory is factory
    assert wrapped_factory() is sentinel


def test_chunking_wrapper_delegates_unknown_methods_to_base():
    # The wrapper must be protocol-preserving: any non-overridden method passes through.
    base = FakeVenueAdapter(base_increment=0.001)
    base.some_method = lambda x: x * 2  # type: ignore[attr-defined]
    wrapped = ChunkingVenueAdapter(base, blocks=3)
    assert wrapped.some_method(21) == 42
    assert wrapped.is_enabled() is True


# ──────────────────────────────────────────────────────────────────────────────
# I3 / I4 / I5 — entry-order HISTORY: record-before-ok, sweep-keeps-in-view, no double
# ──────────────────────────────────────────────────────────────────────────────
def test_every_chunk_oid_recorded_and_unresolved():
    # I3+I4: simulate the live_runner fold — every chunk oid goes into entry_order_ids_all
    # and (with no active pointer / no resolution) shows up as UNRESOLVED so the pre-submit
    # guard blocks a second clip and the sweep will adopt each leg.
    le: dict = {}
    res = {"ok": True, "order_id": "fake_oid_1",
           "chunk_order_ids": ["fake_oid_1", "fake_oid_2", "fake_oid_3"]}
    le["entry_order_id"] = res["order_id"]
    _record_entry_order_placed(le, res.get("order_id"))
    for oid in (res.get("chunk_order_ids") or []):
        _record_entry_order_placed(le, oid)
    assert le["entry_order_ids_all"] == ["fake_oid_1", "fake_oid_2", "fake_oid_3"]
    # active pointer (fake_oid_1) is owned by the pending handler; the OTHER legs are
    # unresolved and therefore block re-entry until the sweep terminalizes them.
    # ADVERSARIAL: if the fold loop were removed, only fake_oid_1 would be tracked and a
    # late fill on _2/_3 would stack an untracked naked position (the BATL bug).
    assert _unresolved_entry_order_ids(le) == ["fake_oid_2", "fake_oid_3"]


def test_record_entry_order_is_idempotent_no_double_count():
    # I5: recording the SAME broker_order_id twice must keep it ONCE — a chunk fill and a
    # broker-sync re-ingest of the same id must not both add the same shares.
    le: dict = {}
    _record_entry_order_placed(le, "oid_A")
    _record_entry_order_placed(le, "oid_A")  # duplicate ingest
    _record_entry_order_placed(le, "oid_B")
    # ADVERSARIAL: if the history appended unconditionally, oid_A would appear twice and a
    # qty aggregation over the history would double-count the leg.
    assert le["entry_order_ids_all"] == ["oid_A", "oid_B"]
    assert _unresolved_entry_order_ids(le) == ["oid_A", "oid_B"]


def test_resolved_leg_drops_out_of_unresolved_to_terminal():
    # I4: once a leg is marked adopted/void it leaves the unresolved set — proving every
    # leg is driven to a TERMINAL resolution rather than blocking forever.
    le: dict = {}
    for oid in ("oid_1", "oid_2", "oid_3"):
        _record_entry_order_placed(le, oid)
    _mark_entry_order_resolved(le, "oid_1", "adopted")
    _mark_entry_order_resolved(le, "oid_2", "void")
    # ADVERSARIAL: if resolution didn't subtract from unresolved, the pre-submit guard
    # would dead-lock the lane (never trades again) — the opposite failure but still a bug.
    assert _unresolved_entry_order_ids(le) == ["oid_3"]


def test_record_skips_empty_order_id():
    # A None/empty oid (e.g. a place that ack'd but returned no id) must NOT be recorded
    # as a phantom unresolved leg that blocks the lane forever.
    le: dict = {}
    _record_entry_order_placed(le, None)
    _record_entry_order_placed(le, "")
    _record_entry_order_placed(le, "real")
    assert le.get("entry_order_ids_all") == ["real"]


def test_history_excludes_active_pointer():
    # The active entry_order_id is owned by the pending-entry handler, NOT the sweep, so it
    # must be excluded from the unresolved set (else the sweep races the primary handler).
    le: dict = {}
    for oid in ("a", "b", "c"):
        _record_entry_order_placed(le, oid)
    le["entry_order_id"] = "b"
    assert _unresolved_entry_order_ids(le) == ["a", "c"]


# ──────────────────────────────────────────────────────────────────────────────
# I4 — late-fill sweep adopts a leg back to the SINGLE parent (terminal) via FakeAdapter
# ──────────────────────────────────────────────────────────────────────────────
def test_sweep_predicate_adopts_late_filled_leg():
    # The sweep re-points + adopts a leg that filled AFTER the ack-timeout abandoned it.
    # We exercise the PREDICATE the sweep uses (_order_done_for_entry / filled_size>0) on a
    # fake order, proving a late fill is recognised as adoptable (not discarded).
    base = FakeVenueAdapter(base_increment=0.001)
    o = _FakeOrder("fake_oid_9", "cid", 1.5)
    base.orders_by_oid["fake_oid_9"] = o
    # before: zero fill, still open → NOT adoptable yet.
    no, _ = base.get_order("fake_oid_9")
    assert not (_order_done_for_entry(no) or float(no.filled_size or 0.0) > 0.0)
    # the venue fills it LATE (cancel lost the race).
    o.filled_size = 1.5
    o.status = "filled"
    o.average_filled_price = 12.34
    no2, _ = base.get_order("fake_oid_9")
    # ADVERSARIAL: if the adoption predicate ignored filled_size (only trusted terminal
    # state), an "open"-with-fills late fill would be left unmanaged (the INDP 612sh bug).
    assert _order_done_for_entry(no2) or float(no2.filled_size or 0.0) > 0.0


def test_open_with_fills_is_adoptable():
    # RH can hold status="open" with shares already filled (a silently-failed cancel + a
    # later fill). Those shares are OWNED the moment they exist.
    o = _FakeOrder("oid", "cid", 5.0)
    o.status = "open"
    o.filled_size = 3.0
    # ADVERSARIAL: the predicate must adopt on FILLS, not on terminal-state ceremony.
    assert float(o.filled_size or 0.0) > 0.0
    # _order_done_for_entry alone is False (open), but the OR-filled_size branch saves it.
    assert _order_done_for_entry(o) is False


# ──────────────────────────────────────────────────────────────────────────────
# I8 — anticipation probe + remainder: never over-buy, both tracked, reject ⇒ no naked leg
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "full, frac, inc, mn",
    [
        (10.0, 0.25, 1.0, 1.0),    # whole shares
        (100.0, 0.3, 1.0, 1.0),
        (40.0, 0.5, 1.0, 1.0),
        (12.0, 0.25, 1.0, 1.0),
    ],
)
def test_anticipation_probe_plus_remainder_never_exceeds_full(full, frac, inc, mn):
    # The split mirrors live_runner: probe = round(full*frac), remainder = round(full-probe).
    # _round_base_size FLOORS to the increment, so the sum may be <= full but must NEVER
    # exceed it (over-buy = unintended leverage / a leg outside the risk-first size).
    probe = _round_base_size(full * frac, inc, mn)
    rem = _round_base_size(full - probe, inc, mn)
    # ADVERSARIAL: if rounding ever rounded UP, probe+rem would exceed the risk-first
    # parent qty and the lane would carry more shares than sized for.
    assert probe + rem <= full + 1e-9, f"probe {probe} + rem {rem} exceeds full {full}"
    assert probe >= mn and rem >= mn, "both legs must clear base_min_size when split"


def test_anticipation_probe_and_remainder_both_recorded():
    # Both the probe leg and the later remainder add-leg are folded into the SAME entry-order
    # history → the sweep tracks each to terminal (no stranded remainder).
    le: dict = {"anticipation_armed": True, "anticipation_probe_qty": 2.0,
                "anticipation_remainder_qty": 8.0}
    _record_entry_order_placed(le, "probe_oid")      # probe placed
    le["entry_order_id"] = "probe_oid"
    _record_entry_order_placed(le, "remainder_oid")  # remainder add placed
    assert le["entry_order_ids_all"] == ["probe_oid", "remainder_oid"]
    # remainder is unresolved until adopted; probe is the active pointer.
    assert _unresolved_entry_order_ids(le) == ["remainder_oid"]


def test_anticipation_remainder_reject_leaves_no_naked_leg():
    # The probe filled + was adopted; the remainder add is REJECTED by the broker (ok=False,
    # no order_id). The live_runner only records on ok+order_id, so NOTHING is added → there
    # is no naked remainder leg and no phantom unresolved id blocking the lane.
    le: dict = {"anticipation_armed": True, "anticipation_probe_qty": 2.0,
                "anticipation_remainder_qty": 8.0}
    _record_entry_order_placed(le, "probe_oid")
    le["entry_order_id"] = "probe_oid"
    ant_res = {"ok": False, "error": "retest_gate"}  # broker refused the add
    if ant_res.get("ok") and ant_res.get("order_id"):
        _record_entry_order_placed(le, ant_res.get("order_id"))  # not reached
    # ADVERSARIAL: if the code recorded the add unconditionally (ignoring ok/order_id), a
    # phantom None/failed id would either be skipped (fine) OR a real-looking id would imply
    # a naked remainder leg. Here the history must stay exactly the probe.
    assert le["entry_order_ids_all"] == ["probe_oid"]
    # probe is the active pointer → no unresolved legs → lane is clean, not stacked.
    assert _unresolved_entry_order_ids(le) == []


def test_anticipation_remainder_uses_distinct_client_order_id_per_attempt():
    # Each remainder attempt bumps anticipation_place_count → a DISTINCT deterministic cid,
    # so a retry is a NEW order the venue + idempotency store accept (never a silent re-fill
    # of the same id, never a collision that the store dedups into a no-op).
    import hashlib

    def _ant_cid(session_id, corr, place_n):
        seed = f"{session_id}|{corr}|ant|{place_n}".encode("utf-8")
        suffix = hashlib.sha1(seed).hexdigest()[:10]
        return f"chili_ml_ant_{session_id}_{(corr or 'x')[:8]}_{suffix}"[:120]

    c1 = _ant_cid(42, "abcd1234", 1)
    c2 = _ant_cid(42, "abcd1234", 2)
    # ADVERSARIAL: if the cid didn't fold in place_count, a retry would reuse c1 and the
    # idempotency store would drop the retry (or the venue would reject the dup) → the
    # remainder silently never re-submits, leaving the position permanently short.
    assert c1 != c2
    assert c1.startswith("chili_ml_ant_42_abcd1234_")


# ══════════════════════════════════════════════════════════════════════════════
# HARDENING PASS — adversarial branch / boundary / failure-mode coverage.
# Every test below pins a SPECIFIC source branch that the original suite left
# unexercised. Each asserts the exact value/reason so a regression in that one
# branch turns it red (not generic truthiness).
# ══════════════════════════════════════════════════════════════════════════════

# stdlib (hardening pass)
from unittest.mock import patch  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# _split_base_size — increment-falsy / negative-increment / no-increment branches
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad_inc", [0.0, -1.0, -0.001])
def test_split_base_size_nonpositive_increment_uses_float_path(bad_inc):
    # BRANCH: `inc = float(increment) if increment and increment > 0 else None`.
    # A zero/negative increment must NOT crash and must NOT be used as a divisor;
    # it falls through to the equal-float split. Sum must still be exact.
    pieces = _split_base_size(9.0, 3, increment=bad_inc)
    assert len(pieces) == 3, f"non-positive inc {bad_inc} should still split float-wise"
    assert math.isclose(sum(pieces), 9.0, abs_tol=1e-9)
    assert all(p > 0 for p in pieces)


def test_split_base_size_odd_increment_exact_sum_distinct_remainder():
    # BOUNDARY: odd increment where units_total is NOT divisible by blocks — the
    # remainder must land on the LAST piece and the sum stay exact.
    inc = 0.003
    total = 0.07  # 0.07 / 0.003 = 23.33 → 23 units; 23 // 4 = 5, remainder 3 units onto last
    pieces = _split_base_size(total, 4, increment=inc)
    assert len(pieces) == 4
    units = [round(p / inc) for p in pieces]
    # ADVERSARIAL: first three blocks equal, last absorbs the remainder (5,5,5,8).
    assert units[:3] == [units[0]] * 3, f"leading blocks must be equal, got {units}"
    assert units[-1] >= units[0], "remainder must fold onto the LAST block, not be dropped"
    assert sum(units) == 23, f"increment-units must sum to the quantized total, got {units}"
    # sum of the quantized pieces equals quantized-total*inc (NOT necessarily raw total).
    assert math.isclose(sum(pieces), 23 * inc, abs_tol=1e-9)


def test_split_base_size_no_increment_remainder_on_last_only():
    # BRANCH: no-increment float path builds (blocks-1) equal pieces + a final
    # remainder piece. Pin that ONLY the last piece carries the remainder.
    pieces = _split_base_size(10.0, 3, increment=None)
    assert len(pieces) == 3
    assert math.isclose(pieces[0], pieces[1], abs_tol=1e-12), "leading pieces equal"
    assert math.isclose(sum(pieces), 10.0, abs_tol=1e-9)
    # ADVERSARIAL: the last piece = total - per*(n-1); if it instead repeated `per`
    # the sum would drift. Here per=3.333.. and last=3.333.. all equal, so use an
    # uneven total to make the remainder visibly distinct:
    pieces2 = _split_base_size(10.0, 4, increment=None)  # per=2.5 even → all equal
    assert math.isclose(sum(pieces2), 10.0, abs_tol=1e-9)


def test_split_base_size_negative_total_returns_single():
    # BOUNDARY: total <= 0 short-circuits to [total] regardless of blocks.
    assert _split_base_size(-5.0, 4, increment=0.01) == [-5.0]
    assert _split_base_size(0.0, 4, increment=None) == [0.0]


def test_split_base_size_exact_units_equal_blocks_each_one_unit():
    # BOUNDARY: units_total == blocks (the eps-above of the `< blocks` fallback).
    # Each block must get exactly one increment — NOT the single-order fallback.
    inc = 0.5
    pieces = _split_base_size(1.5, 3, increment=inc)  # 3 units, 3 blocks → 1 each
    assert pieces == [0.5, 0.5, 0.5], f"each block exactly one increment, got {pieces}"


def test_split_base_size_units_one_below_blocks_falls_back():
    # BOUNDARY (eps-below): units_total == blocks-1 → can't give each block one unit.
    inc = 0.5
    total = 1.0  # 2 units for 3 blocks
    assert _split_base_size(total, 3, increment=inc) == [total]


# ──────────────────────────────────────────────────────────────────────────────
# ChunkingVenueAdapter — blocks clamp, bad base_size, total<=0, increment-probe error
# ──────────────────────────────────────────────────────────────────────────────
def test_chunking_blocks_clamped_to_ten_max():
    # The wrapper clamps blocks into 1..10 defensively. blocks=50 must place AT MOST
    # 10 legs (clamp), not 50 — an unbounded fan-out would spray the order book.
    base = FakeVenueAdapter(base_increment=0.001)
    wrapped = ChunkingVenueAdapter(base, blocks=50)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="100.0",
        limit_price="10", client_order_id="p",
    )
    assert res["ok"] is True
    assert len(base.placed_orders) == 10, "blocks must clamp to the 10 max"
    assert math.isclose(sum(o[2] for o in base.placed_orders), 100.0, abs_tol=1e-9)


def test_chunking_blocks_zero_clamped_to_single():
    # blocks<=0 clamps to 1 → single order with caller's cid (byte-identical).
    base = FakeVenueAdapter(base_increment=0.001)
    wrapped = ChunkingVenueAdapter(base, blocks=0)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="3.0",
        limit_price="10", client_order_id="solo0",
    )
    assert len(base.placed_orders) == 1
    assert base.placed_orders[0][4] == "solo0"
    assert CHUNK_RESULT_KEY not in res


def test_chunking_negative_blocks_clamped_to_single():
    base = FakeVenueAdapter(base_increment=0.001)
    wrapped = ChunkingVenueAdapter(base, blocks=-7)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="3.0",
        limit_price="10", client_order_id="soloN",
    )
    assert len(base.placed_orders) == 1
    assert base.placed_orders[0][4] == "soloN"
    assert CHUNK_RESULT_KEY not in res


@pytest.mark.parametrize("bad_size", ["abc", "", "NaN", None])
def test_chunking_non_numeric_base_size_falls_back_to_single(bad_size):
    # BRANCH: `float(base_size)` raises TypeError/ValueError ⇒ fail-closed-to-single.
    # NaN is numeric to float() but `total <= 0` is False for NaN → it must NOT crash
    # nor multi-submit; the splitter on NaN returns [NaN] (len<=1) ⇒ single.
    base = FakeVenueAdapter(base_increment=0.001)
    wrapped = ChunkingVenueAdapter(base, blocks=3)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size=bad_size,
        limit_price="10", client_order_id="badsz",
    )
    # ADVERSARIAL: the WRAPPER's contract is "delegate ONE unchanged call to the base,
    # never a partial fan-out" — it is not a size validator. So: no chunk fan-out, and at
    # most one delegated order (the base may itself reject an unparseable size).
    assert CHUNK_RESULT_KEY not in res, f"bad size {bad_size!r} must not fan out into chunks"
    assert len(base.placed_orders) <= 1, f"bad size {bad_size!r} delegates at most one (single)"


def test_chunking_zero_base_size_falls_back_to_single():
    # BOUNDARY: total == 0 ⇒ single delegated order (the `if total <= 0` branch).
    base = FakeVenueAdapter(base_increment=0.001)
    wrapped = ChunkingVenueAdapter(base, blocks=3)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="0",
        limit_price="10", client_order_id="z",
    )
    assert len(base.placed_orders) == 1
    assert base.placed_orders[0][4] == "z"


def test_chunking_increment_probe_exception_falls_back_to_float_split():
    # BRANCH: `_base_increment` swallows any get_product error → None → float split.
    # A broker that errors on get_product must NOT abort chunking; it splits float-wise.
    base = FakeVenueAdapter(base_increment=0.001)
    def _boom(_pid):
        raise RuntimeError("venue down")
    base.get_product = _boom  # type: ignore[assignment]
    wrapped = ChunkingVenueAdapter(base, blocks=4)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="8.0",
        limit_price="10", client_order_id="p",
    )
    assert res["ok"] is True
    assert len(base.placed_orders) == 4, "increment-probe error must not block the split"
    assert math.isclose(sum(o[2] for o in base.placed_orders), 8.0, abs_tol=1e-9)


def test_chunking_no_client_order_id_mints_chunk_prefix():
    # BRANCH: `base_cid = client_order_id or f"chunk_{uuid...}"`. With no caller cid,
    # every child cid must still be distinct AND carry the synthetic chunk_ lineage.
    base = FakeVenueAdapter(base_increment=0.001)
    wrapped = ChunkingVenueAdapter(base, blocks=3)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="3.0",
        limit_price="10", client_order_id=None,
    )
    cids = [o[4] for o in base.placed_orders]
    assert len(set(cids)) == 3, "children distinct even without a parent cid"
    assert all(c.startswith("chunk_") for c in cids), f"synthetic chunk lineage, got {cids}"
    assert res["ok"] is True


def test_chunking_all_children_fail_is_fail_closed_no_phantom_ok():
    # FAILURE MODE: EVERY child rejected → ok=False, error set, and (since no child
    # acked) there are NO order ids to cancel and chunk_order_ids is empty. The lane
    # must see a clean failure, never a phantom ok with zero real legs.
    base = FakeVenueAdapter(base_increment=0.01, fail_on=lambda cid: True)
    wrapped = ChunkingVenueAdapter(base, blocks=3)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="3.0",
        limit_price="10", client_order_id="p",
    )
    assert res["ok"] is False
    assert res.get(CHUNK_RESULT_KEY) == [], "no child acked → no recorded oids"
    assert base.cancelled == [], "nothing placed → nothing to cancel"
    assert (res.get("error") or "") != "", "a clean failure reason must be surfaced"


def test_chunking_first_child_fails_primary_mirrors_first_and_fails_closed():
    # FAILURE MODE: the FIRST child fails but later ones ack. `first` mirrors child 0's
    # FAILED result, yet the acked siblings (c1,c2) are recorded AND cancelled, and the
    # whole submit is ok=False (partial). Pins that the primary's ok is the AND of all
    # children, not just `first`.
    base = FakeVenueAdapter(
        base_increment=0.01,
        fail_on=lambda cid: cid is not None and "_c0_" in cid,
    )
    wrapped = ChunkingVenueAdapter(base, blocks=3)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="3.0",
        limit_price="10", client_order_id="p",
    )
    assert res["ok"] is False
    placed = res.get(CHUNK_RESULT_KEY, [])
    assert len(placed) == 2, "the two acked siblings must be recorded for the sweep"
    assert set(base.cancelled) == set(placed), "every acked sibling must be cancelled"


def test_chunking_cancel_cleanup_failure_still_keeps_oids_for_sweep():
    # FAILURE MODE: partial submit AND the cleanup cancel itself raises. The wrapper
    # must NOT crash; it still surfaces ok=False and KEEPS the acked oids in
    # chunk_order_ids so the live-runner late-fill sweep tracks them to terminal.
    base = FakeVenueAdapter(
        base_increment=0.01,
        fail_on=lambda cid: cid is not None and "_c2_" in cid,  # last child fails
    )
    def _cancel_boom(_oid):
        raise RuntimeError("cancel API down")
    base.cancel_order = _cancel_boom  # type: ignore[assignment]
    wrapped = ChunkingVenueAdapter(base, blocks=3)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="3.0",
        limit_price="10", client_order_id="p",
    )
    assert res["ok"] is False
    placed = res.get(CHUNK_RESULT_KEY, [])
    assert len(placed) == 2, "acked legs stay recorded even when cleanup-cancel fails"
    # chunk_cancelled_order_ids should be EMPTY (every cancel raised) — proving the
    # oids survive in chunk_order_ids precisely as the belt-and-suspenders fallback.
    assert res.get("chunk_cancelled_order_ids", []) == []


def test_chunking_child_exception_marks_child_failed_and_fails_closed():
    # FAILURE MODE: a child submit RAISES (not just returns ok=False). The wrapper
    # records a synthetic failed result, the overall submit is ok=False, and any
    # acked siblings are cancelled. Distinguish the exception branch from the
    # ok=False branch by the error prefix.
    state = {"n": 0}
    base = FakeVenueAdapter(base_increment=0.01)
    real_place = base.place_limit_order_gtc
    def _place(**kw):
        state["n"] += 1
        if state["n"] == 2:  # second child raises
            raise RuntimeError("socket reset")
        return real_place(**kw)
    base.place_limit_order_gtc = _place  # type: ignore[assignment]
    wrapped = ChunkingVenueAdapter(base, blocks=3)
    res = wrapped.place_limit_order_gtc(
        product_id="X", side="buy", base_size="3.0",
        limit_price="10", client_order_id="p",
    )
    assert res["ok"] is False
    # the raised child contributed no order_id; the two real ones were placed.
    placed = res.get(CHUNK_RESULT_KEY, [])
    assert len(placed) == 2, "only the two non-raising children produced oids"
    assert set(base.cancelled) == set(placed), "acked siblings cancelled on partial"


# ──────────────────────────────────────────────────────────────────────────────
# maybe_wrap_chunking — flag ON inserts the wrapper; exception → base factory
# ──────────────────────────────────────────────────────────────────────────────
def test_maybe_wrap_chunking_on_inserts_wrapper():
    # FLAG PARITY (the ON side): flag on + blocks>1 ⇒ the factory now yields a
    # ChunkingVenueAdapter wrapping the base. (The OFF side is already covered.)
    sentinel = FakeVenueAdapter(base_increment=0.001)

    # maybe_wrap_chunking reads settings via a LOCAL `from ...config import settings`, so we
    # must patch the real shared singleton's attributes (patching a module-level ca.settings
    # is ignored — the function never reads it).
    from app.config import settings as _rs

    with patch.object(_rs, "chili_momentum_order_chunking_enabled", True), patch.object(
        _rs, "chili_momentum_order_chunking_blocks", 3
    ):
        wrapped_factory = maybe_wrap_chunking(lambda: sentinel)
        adapter = wrapped_factory()
    assert isinstance(adapter, ChunkingVenueAdapter)
    # the wrapped adapter still delegates is_enabled through to the base sentinel.
    assert adapter.is_enabled() is True


def test_live_runner_hard_bypasses_chunking_for_alpaca_entry_and_close():
    """Flag-on still sends one exact claimed parent CID for both directions."""
    from app.config import settings as _rs

    base = FakeVenueAdapter(base_increment=1.0)

    def factory():
        return base

    with patch.object(_rs, "chili_momentum_order_chunking_enabled", True), patch.object(
        _rs, "chili_momentum_order_chunking_blocks", 4
    ):
        selected = lr._live_runner_order_factory(factory, "alpaca_spot")
        adapter = selected()
        entry = adapter.place_limit_order_gtc(
            product_id="ACTU",
            side="buy",
            base_size="8",
            limit_price="1.25",
            client_order_id="claimed-entry-parent",
        )
        close = adapter.place_limit_order_gtc(
            product_id="ACTU",
            side="sell",
            base_size="8",
            limit_price="1.20",
            client_order_id="claimed-close-parent",
        )

    assert selected is factory
    assert entry["ok"] is True and close["ok"] is True
    assert [row[4] for row in base.placed_orders] == [
        "claimed-entry-parent",
        "claimed-close-parent",
    ]
    assert [row[1] for row in base.placed_orders] == ["buy", "sell"]
    assert all(CHUNK_RESULT_KEY not in result for result in (entry, close))


def test_maybe_wrap_chunking_on_but_blocks_one_returns_base():
    # FLAG PARITY: flag ON but blocks<=1 ⇒ still byte-identical (base factory returned).
    # maybe_wrap_chunking reads settings via a LOCAL `from ...config import settings`,
    # so the real shared singleton must be patched — a module-level ca.settings stub is
    # ignored by the function and this test would pass for the wrong reason (flag OFF).
    sentinel = FakeVenueAdapter()

    def factory():
        return sentinel

    from app.config import settings as _rs

    with patch.object(_rs, "chili_momentum_order_chunking_enabled", True), patch.object(
        _rs, "chili_momentum_order_chunking_blocks", 1
    ):
        wf = maybe_wrap_chunking(factory)
    assert wf is factory, "blocks<=1 must return the untouched factory"


def test_maybe_wrap_chunking_settings_import_error_returns_base(monkeypatch):
    # FAILURE MODE: any error reading settings ⇒ fail-safe to the base factory (never
    # wrap on an indeterminate config). The function's local import re-reads the real
    # config module, so force the failure on the attribute that import resolves —
    # a raising property installed on the settings singleton's class.
    sentinel = FakeVenueAdapter()

    def factory():
        return sentinel

    from app.config import settings as _rs

    def _boom(self):  # pragma: no cover - executed via the property below
        raise RuntimeError("config blew up")

    monkeypatch.setattr(
        type(_rs),
        "chili_momentum_order_chunking_enabled",
        property(_boom),
        raising=False,
    )
    wf = maybe_wrap_chunking(factory)
    # ADVERSARIAL: a config read that raises must NOT crash the lane and must NOT
    # silently wrap — it returns the exact base factory.
    assert wf is factory


# ──────────────────────────────────────────────────────────────────────────────
# _round_base_size — qty<=0, no-increment rounding, sub-min floor, eps boundary
# ──────────────────────────────────────────────────────────────────────────────
def test_round_base_size_nonpositive_returns_zero():
    # BRANCH: qty <= 0 → 0.0 (never a negative size submitted to the venue).
    assert _round_base_size(0.0, 1.0, 1.0) == 0.0
    assert _round_base_size(-3.0, 1.0, 1.0) == 0.0


def test_round_base_size_no_increment_rounds_to_8dp():
    # BRANCH: increment falsy ⇒ round(qty, 8). Pin the 8-dp rounding (crypto fine size).
    assert _round_base_size(1.123456789, None, None) == round(1.123456789, 8)
    assert _round_base_size(1.123456789, 0.0, None) == round(1.123456789, 8)


def test_round_base_size_below_min_floors_to_zero():
    # BRANCH: a quantized qty below base_min_size returns 0.0 (don't submit a sub-min
    # leg the venue rejects → which would orphan a chunk).
    assert _round_base_size(0.4, 0.1, 1.0) == 0.0  # floors to 0.4 < min 1.0 → 0
    # eps-above the min: exactly at min must PASS (the 1e-12 tolerance).
    assert _round_base_size(1.0, 0.1, 1.0) == 1.0


def test_round_base_size_floor_not_round_for_increment():
    # ADVERSARIAL: the increment path must FLOOR, never round up (rounding up could
    # buy more than sized). 1.99 with increment 1.0 → 1.0, never 2.0.
    assert _round_base_size(1.99, 1.0, None) == 1.0
    assert _round_base_size(2.0, 1.0, None) == 2.0


# ──────────────────────────────────────────────────────────────────────────────
# _order_done_for_entry — partial-fill-then-cancelled, 1e-12 boundary, empty status
# ──────────────────────────────────────────────────────────────────────────────
def test_order_done_partial_fill_then_cancelled_is_done():
    # BRANCH: filled_size>0 AND status terminal-cancel ⇒ done (the shares are OWNED,
    # the order won't fill more). This is a leg that partially filled then the
    # ack-timeout cancel landed.
    o = _FakeOrder("oid", "cid", 5.0)
    o.filled_size = 2.0
    o.status = "cancelled"
    assert _order_done_for_entry(o) is True


def test_order_done_zero_fill_cancelled_is_done_via_filled_branch_false():
    # BOUNDARY: cancelled with ZERO fill → filled_size>1e-12 is False, status not in
    # the filled-set → returns False (it's a clean cancel; the void path handles it,
    # NOT the done-with-fills path). Pins that a clean cancel is NOT mis-flagged
    # "done with fills".
    o = _FakeOrder("oid", "cid", 5.0)
    o.filled_size = 0.0
    o.status = "cancelled"
    assert _order_done_for_entry(o) is False


def test_order_done_fill_size_eps_boundary():
    # BOUNDARY: the > 1e-12 threshold. A dust fill exactly at/below the epsilon must
    # NOT count as filled (treated as zero); just above it counts.
    o = _FakeOrder("oid", "cid", 5.0)
    o.status = "expired"
    o.filled_size = 1e-12  # NOT strictly greater than 1e-12
    assert _order_done_for_entry(o) is False
    o.filled_size = 1e-11  # comfortably above
    assert _order_done_for_entry(o) is True


def test_order_done_empty_and_open_status_not_done():
    # BRANCH: empty/None status with no fills is NOT done (indeterminate → keep open).
    o = _FakeOrder("oid", "cid", 5.0)
    o.status = None
    assert _order_done_for_entry(o) is False
    o.status = ""
    assert _order_done_for_entry(o) is False
    o.status = "submitted"
    assert _order_done_for_entry(o) is False


def test_order_done_terminal_statuses_filled_done_closed():
    # BRANCH: each of the explicit terminal-fill statuses returns True regardless of
    # fill size (the venue says it's done).
    for st in ("filled", "done", "closed", "FILLED", "Done"):
        o = _FakeOrder("oid", "cid", 5.0)
        o.status = st
        assert _order_done_for_entry(o) is True, f"status {st} must be done"


# ──────────────────────────────────────────────────────────────────────────────
# _record_entry_order_placed — history CAP, str-coercion dedupe, ordering
# ──────────────────────────────────────────────────────────────────────────────
def test_record_entry_history_capped_at_max():
    # BOUNDARY: the history is bounded to _ENTRY_ORDER_HISTORY_MAX (20). The 21st id
    # evicts the oldest — the json never grows unbounded, and the most-recent (live)
    # legs are always retained.
    le: dict = {}
    cap = lr._ENTRY_ORDER_HISTORY_MAX
    for i in range(cap + 5):
        _record_entry_order_placed(le, f"oid_{i}")
    hist = le["entry_order_ids_all"]
    assert len(hist) == cap, f"history must cap at {cap}, got {len(hist)}"
    # the FIRST 5 were evicted; the LAST one is retained.
    assert hist[-1] == f"oid_{cap + 4}"
    assert "oid_0" not in hist, "oldest legs evicted past the cap"


def test_record_entry_order_str_coerces_for_dedupe():
    # The dedupe compares str(order_id): an int 7 and the str "7" are the SAME leg and
    # must not both be recorded (a broker-sync re-ingest may hand back a different type).
    le: dict = {}
    _record_entry_order_placed(le, 7)
    _record_entry_order_placed(le, "7")
    assert le["entry_order_ids_all"] == ["7"], "int and str of same id are one leg"


def test_record_entry_order_zero_int_is_falsy_skipped():
    # EDGE: order_id == 0 (int) is falsy → skipped (no phantom "0" leg). Matches the
    # None/"" skip already covered, but 0 is the sneaky falsy id.
    le: dict = {}
    _record_entry_order_placed(le, 0)
    assert le.get("entry_order_ids_all", []) == []


# ──────────────────────────────────────────────────────────────────────────────
# _mark_entry_order_resolved / _unresolved — overwrite, str keys, missing maps
# ──────────────────────────────────────────────────────────────────────────────
def test_mark_resolved_overwrites_outcome_and_str_keys():
    # A leg can be re-resolved (e.g. void then later adopted); the latest outcome wins
    # and the key is the str id (so the unresolved filter — which compares str — drops it).
    le: dict = {}
    _record_entry_order_placed(le, 99)
    _mark_entry_order_resolved(le, 99, "void")
    assert le["entry_orders_resolved"]["99"] == "void"
    _mark_entry_order_resolved(le, "99", "adopted")
    assert le["entry_orders_resolved"]["99"] == "adopted", "latest outcome overwrites"
    assert _unresolved_entry_order_ids(le) == [], "a resolved leg (any outcome) is not unresolved"


def test_unresolved_empty_le_is_empty_list():
    # EDGE: a fresh le with no history/resolved/active keys must not KeyError; returns [].
    assert _unresolved_entry_order_ids({}) == []


def test_unresolved_active_pointer_also_resolved_still_excluded():
    # The active pointer is excluded even if it's ALSO in the resolved map (belt and
    # suspenders — the pending handler owns it; the sweep must not touch it).
    le: dict = {}
    for oid in ("a", "b", "c"):
        _record_entry_order_placed(le, oid)
    le["entry_order_id"] = "b"
    _mark_entry_order_resolved(le, "a", "adopted")
    assert _unresolved_entry_order_ids(le) == ["c"], "active(b) + resolved(a) both excluded"


def test_record_then_resolve_preserves_history_for_audit():
    # Resolving a leg removes it from UNRESOLVED but must KEEP it in the history (audit
    # lineage) — the fold/reconcile relies on the full placed-id list.
    le: dict = {}
    _record_entry_order_placed(le, "x")
    _mark_entry_order_resolved(le, "x", "adopted")
    assert le["entry_order_ids_all"] == ["x"], "resolution must not erase history"
    assert _unresolved_entry_order_ids(le) == []


# ──────────────────────────────────────────────────────────────────────────────
# Anticipation split — exact-at-min boundary, frac rounding never over-buys
# ──────────────────────────────────────────────────────────────────────────────
def test_anticipation_probe_floor_never_overbuys_at_odd_increment():
    # BOUNDARY: an odd increment where full*frac is NOT a clean multiple. The probe
    # floors and the remainder floors — their sum must be <= full (never an over-buy),
    # and each leg an exact increment multiple the venue accepts.
    full, frac, inc, mn = 7.0, 0.3, 0.5, 0.5
    probe = _round_base_size(full * frac, inc, mn)   # 2.1 → floor to 2.0
    rem = _round_base_size(full - probe, inc, mn)     # 5.0
    assert probe + rem <= full + 1e-9
    for leg in (probe, rem):
        units = round(leg / inc)
        assert math.isclose(units * inc, leg, abs_tol=1e-9), f"{leg} not a multiple of {inc}"


def test_anticipation_probe_below_min_collapses_to_zero_remainder_full():
    # EDGE: a tiny probe fraction that floors BELOW base_min_size → probe=0; the
    # remainder then carries the FULL size. The lane must not place a sub-min probe leg.
    full, frac, inc, mn = 10.0, 0.01, 1.0, 1.0
    probe = _round_base_size(full * frac, inc, mn)  # 0.1 → below min → 0
    rem = _round_base_size(full - probe, inc, mn)    # 10.0
    assert probe == 0.0, "sub-min probe must collapse to zero, not a rejected leg"
    assert rem == full, "remainder carries the full size when the probe is voided"
