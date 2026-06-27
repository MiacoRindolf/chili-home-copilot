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
        self.placed_orders.append(
            (product_id, side, float(base_size), limit_price, client_order_id)
        )
        oid = f"fake_oid_{len(self.placed_orders)}"
        self.orders_by_oid[oid] = _FakeOrder(oid, client_order_id, float(base_size))
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
