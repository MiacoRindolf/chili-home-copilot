"""Unit tests for the L2-aware anti-shake-out stop breach classifier.

`classify_stop_breach` is a PURE helper: given a LadderRead-shaped L2 distribution
read at the moment `bid <= stop`, it returns BREAKDOWN (sell now) / CHOP (hold one
bounded beat) / INCONCLUSIVE (today's path). The load-bearing safety property is
BREAKDOWN-FIRST: any decisive-sell signal — and any stale / missing / too-few L2 —
returns BREAKDOWN before a CHOP hold can ever be granted, so a real breakdown's
loss-side latency is never worse than the existing time-only confirm. It NEVER reads
or moves the stop, so INVARIANT A (ratchet-only stop) cannot be violated here.
"""

from dataclasses import dataclass

from app.services.trading.momentum_neural.paper_execution import classify_stop_breach


@dataclass
class _Ladder:
    """Minimal LadderRead stand-in (only the fields the classifier reads)."""

    depth_imbal: float | None = None
    depth_imbal_pctile: float | None = None
    ofi: float | None = None
    micro_edge: float | None = None
    bid_refill: float | None = None
    ask_build: float | None = None
    spread_bps: float | None = None
    snapshot_age_s: float | None = None
    n_snaps: int = 0


THR = 0.25  # mirrors chili_momentum_ofi_threshold default


def _chop(**kw):
    """A FRESH, clearly-CHOP read: bids refilling, OFI ~flat, micro flat, book bid-rich."""
    base = dict(
        depth_imbal_pctile=0.7, ofi=0.0, micro_edge=0.0, bid_refill=0.20,
        ask_build=-0.05, spread_bps=8.0, snapshot_age_s=0.5, n_snaps=6,
    )
    base.update(kw)
    return _Ladder(**base)


def _classify(ladder, **kw):
    return classify_stop_breach(ladder=ladder, ofi_threshold=THR, **kw)


# ── BREAKDOWN: must sell now ────────────────────────────────────────────────

def test_real_breakdown_still_exits_ofi():
    """Decisive negative OFI ⇒ BREAKDOWN even if every other field looks like chop."""
    r = _chop(ofi=-3.0 * THR)  # < -2T
    out = _classify(r)
    assert out["cls"] == "BREAKDOWN"
    assert out["reason"] == "ofi_decisive"


def test_real_breakdown_depth_ask_heaviest():
    """Newest book ask-heaviest in its own window ⇒ BREAKDOWN."""
    out = _classify(_chop(depth_imbal_pctile=0.1))
    assert out["cls"] == "BREAKDOWN"
    assert out["reason"] == "depth_ask_heaviest"


def test_real_breakdown_ask_wall_building():
    """Ask side stacking faster than the bid refills + negative micro ⇒ BREAKDOWN."""
    out = _classify(_chop(ask_build=0.40, bid_refill=0.05, micro_edge=-2.0))
    assert out["cls"] == "BREAKDOWN"
    assert out["reason"] == "ask_wall_building"


def test_stale_l2_is_breakdown_not_hold():
    """L2 older than max_age_s ⇒ BREAKDOWN (never hold on stale data)."""
    out = _classify(_chop(snapshot_age_s=9.9), max_age_s=2.5)
    assert out["cls"] == "BREAKDOWN"
    assert out["reason"] == "stale_or_missing_l2"


def test_too_few_snaps_is_breakdown():
    """Fewer than min_snaps ⇒ BREAKDOWN (never hold on thin evidence)."""
    out = _classify(_chop(n_snaps=2), min_snaps=3)
    assert out["cls"] == "BREAKDOWN"


def test_missing_core_field_is_breakdown():
    """Any None in a required slot ⇒ BREAKDOWN."""
    assert _classify(_chop(ofi=None))["cls"] == "BREAKDOWN"
    assert _classify(_chop(micro_edge=None))["cls"] == "BREAKDOWN"
    assert _classify(_chop(depth_imbal_pctile=None))["cls"] == "BREAKDOWN"


def test_crossed_l2_book_is_breakdown_not_hold():
    """A negative spread is invalid book evidence and can never earn a hold."""
    out = _classify(_chop(spread_bps=-72.0))
    assert out["cls"] == "BREAKDOWN"
    assert out["reason"] == "crossed_l2_book"


def test_none_ladder_is_breakdown():
    out = classify_stop_breach(ladder=None, ofi_threshold=THR)
    assert out["cls"] == "BREAKDOWN"
    assert out["reason"] == "stale_or_missing_l2"


# ── CHOP: hold one bounded beat ─────────────────────────────────────────────

def test_chop_dip_held():
    """Bids absorbing, OFI flat, micro flat, book bid-rich, fresh ⇒ CHOP."""
    out = _classify(_chop())
    assert out["cls"] == "CHOP"
    assert out["reason"] == "bids_absorbing"


def test_chop_needs_bid_refill_positive():
    """No bid refill ⇒ not CHOP (no absorption to ride out)."""
    assert _classify(_chop(bid_refill=0.0))["cls"] != "CHOP"
    assert _classify(_chop(bid_refill=-0.1))["cls"] != "CHOP"


def test_chop_needs_book_not_ask_heavy():
    """pctile < 0.5 (book leaning ask-heavy) ⇒ not CHOP."""
    # 0.3 is above the 0.2 breakdown floor but below the 0.5 chop floor → INCONCLUSIVE.
    out = _classify(_chop(depth_imbal_pctile=0.3))
    assert out["cls"] == "INCONCLUSIVE"


def test_chop_micro_floor_is_spread_relative():
    """micro just inside -0.5*spread holds; clearly past it does not."""
    # spread 8bps → micro_floor = 4bps. micro=-3 (>= -4) is tolerable chop.
    assert _classify(_chop(spread_bps=8.0, micro_edge=-3.0))["cls"] == "CHOP"
    # micro=-6 (< -4) rolls past the floor → not CHOP.
    assert _classify(_chop(spread_bps=8.0, micro_edge=-6.0))["cls"] != "CHOP"


def test_chop_cannot_override_breakdown():
    """Even a perfect chop profile yields BREAKDOWN when a breakdown signal co-fires."""
    # decisive OFI present AND chop-looking depth/refill → breakdown wins (checked first).
    out = _classify(_chop(ofi=-2.5 * THR))
    assert out["cls"] == "BREAKDOWN"


# ── INCONCLUSIVE: caller falls back to today's >=1s sell path ───────────────

def test_mixed_is_inconclusive():
    """Fresh, no breakdown, but not full chop-confluence ⇒ INCONCLUSIVE (sell)."""
    # OFI in the dead zone: past the chop band (< -T) but not decisive (> -2T),
    # book not ask-heavy. Neither a breakdown signal nor full chop-confluence.
    out = _classify(_chop(ofi=-1.5 * THR, depth_imbal_pctile=0.55))
    assert out["cls"] == "INCONCLUSIVE"


# ── Invariant: the helper never returns a stop value (it cannot touch the stop) ─

def test_helper_returns_only_classification_no_stop():
    """The verdict dict carries cls/reason/signals — never a stop price/floor.

    This is the structural proof that classify_stop_breach is INVARIANT-A-safe:
    it has no channel to move the stop; it only tells the caller WHEN to sell.
    """
    out = _classify(_chop())
    assert set(out.keys()) == {"cls", "reason", "signals"}
    for k in out["signals"]:
        assert "stop" not in k.lower()
