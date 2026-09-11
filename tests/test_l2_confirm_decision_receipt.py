"""The entry L2 confirmer must record what it DECIDED, not only when it refused.

Until f023d38a6 only the "defer" path wrote an event, so the confirm rate was never
measurable: the entire live book held exactly ONE l2_confirm event, a defer on
2026-06-29. The receipt (`live_l2_confirm_decision`, on change of reason) now carries
`reason`, and these tests pin that reason against a KNOWN vocabulary so it can be read
back.

WHAT CHANGED 2026-09-11 ([2] [c]). The previous version of this file pinned the
vocabulary by re-implementing the OLD predicate (`accel<=0 AND ofi<0`, reason
`l2_confirm_defer_no_tape`) in a local helper and testing the helper — it never called
`_l2_entry_confirm`, so it stayed green while the real predicate was replaced by
`buy_share_delta` (c92bf49ca) and `l2_confirm_defer_no_tape` stopped existing. It now
drives the REAL function through every path, and an AST pin fails the moment a reason
string appears in the source that is not in the vocabulary below.

The vocabulary also splits the old catch-all `l2_confirm_no_data` three ways: an empty
tape (`l2_confirm_no_tape`), a failed tape read (`l2_confirm_tape_error`), and any other
exception (`l2_confirm_error`). `l2_confirm_no_data` means only "db None / blank symbol".

DB-free.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime

import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural.pipeline import LadderRead

# reason -> (decision, is_named_fail_open_fallback)
VOCABULARY: dict[str, tuple[str, bool]] = {
    "l2_confirm_disabled": ("confirm", False),       # kill switch, before any I/O
    "l2_confirm_no_data": ("confirm", True),         # db None / blank symbol
    "l2_confirm_no_tape": ("confirm", True),         # read ok, too little tape
    "l2_confirm_tape_error": ("confirm", True),      # tape read raised (why/error/where)
    "l2_confirm_tape_stale": ("confirm", True),      # newest print past its age bound
    "l2_confirm_pass_mixed": ("confirm", True),      # share unreadable (can't halve)
    "l2_confirm_error": ("confirm", True),           # anything else raised
    "l2_confirm_tape_thrust": ("confirm", False),    # buy_share_delta > 0
    "l2_confirm_secondary_override": ("confirm", False),  # bsd <= 0, readable book agrees
    "l2_confirm_buying_not_carrying": ("defer", False),   # bsd <= 0, no book agrees
}


# ── fakes ───────────────────────────────────────────────────────────────────────


def _now_epoch() -> float:
    return (datetime.utcnow() - datetime(1970, 1, 1)).total_seconds()


class _TapeDB:
    """Replays canned (price, size, bid, ask, ts) rows; the newest lands at
    ``end_epoch`` (default: 1 s before now), prints 3 s apart."""

    def __init__(self, sides, end_epoch: float | None = None):
        t_last = (_now_epoch() - 1.0) if end_epoch is None else float(end_epoch)
        n = len(sides)
        self.rows = []
        for i, (px, sz, lift) in enumerate(sides):
            bid, ask = (px - 0.01, px) if lift else (px, px + 0.01)
            self.rows.append((px, sz, bid, ask, t_last - (n - 1 - i) * 3.0))

    def execute(self, *_a, **_k):
        rows = self.rows

        class _R:
            def fetchall(self_inner):
                return rows

        return _R()


class _RaisingDB:
    def execute(self, *_a, **_k):
        raise RuntimeError("read failed")


_CARRYING = [(10.00, 400, False), (10.01, 400, False), (10.02, 600, True), (10.03, 600, True)]
_FADING = [(10.00, 600, True), (10.01, 600, True), (10.02, 400, False), (10.01, 400, False)]
_TOO_SHORT_TO_HALVE = [(10.00, 400, True), (10.01, 400, True), (10.02, 400, True)]


def _book(*, ofi=0.9, micro=2.0, pctile=0.9, age=1.0, n_snaps=6):
    return LadderRead(None, pctile, ofi, micro, None, None, None, age, n_snaps)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_l2_confirm_enabled", True)


def _no_book(monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: None,
    )


def _with_book(monkeypatch, lr):
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: lr,
    )


# ── 1. the vocabulary in the SOURCE is the vocabulary here ───────────────────────


def _reason_literals_in_source() -> set[str]:
    src = textwrap.dedent(inspect.getsource(eg._l2_entry_confirm))
    found: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if (isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Name) and tgt.value.id == "dbg"
                    and isinstance(tgt.slice, ast.Constant) and tgt.slice.value == "reason"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str) and node.value.value):
                found.add(node.value.value)
    return found


def test_every_reason_in_the_source_is_in_the_pinned_vocabulary():
    found = _reason_literals_in_source()
    assert found, "the AST walk found no reason assignments — the pin is not watching"
    assert found == set(VOCABULARY), (
        f"unpinned: {sorted(found - set(VOCABULARY))}; "
        f"pinned but gone: {sorted(set(VOCABULARY) - found)}"
    )


def test_the_removed_predicates_reason_is_gone():
    """`l2_confirm_defer_no_tape` was the old `accel<=0 AND ofi<0` defer."""
    assert "l2_confirm_defer_no_tape" not in _reason_literals_in_source()


# ── 2. every path, through the REAL function ─────────────────────────────────────


def _drive(monkeypatch, path):
    """Return (decision, dbg) from the real `_l2_entry_confirm` for one named path."""
    if path == "l2_confirm_disabled":
        monkeypatch.setattr(settings, "chili_momentum_l2_confirm_enabled", False)
        return eg._l2_entry_confirm("ABCD", db=None, settings=settings)
    if path == "l2_confirm_no_data":
        return eg._l2_entry_confirm("ABCD", db=None, settings=settings)
    if path == "l2_confirm_no_tape":
        _no_book(monkeypatch)
        return eg._l2_entry_confirm("ABCD", db=_TapeDB([]), settings=settings)
    if path == "l2_confirm_tape_error":
        _no_book(monkeypatch)
        return eg._l2_entry_confirm("ABCD", db=_RaisingDB(), settings=settings)
    if path == "l2_confirm_tape_stale":
        # A FADING tape (it would defer) whose newest print is 20 h before the decision.
        _no_book(monkeypatch)
        as_of = datetime(2026, 9, 10, 14, 20, 0)
        end = (as_of - datetime(1970, 1, 1)).total_seconds() - 20 * 3600.0
        return eg._l2_entry_confirm(
            "ABCD", db=_TapeDB(_FADING, end_epoch=end), settings=settings, l2_as_of=as_of,
        )
    if path == "l2_confirm_pass_mixed":
        # Three prints: enough to be a tape (>= 3), too few to halve (< 4).
        _no_book(monkeypatch)
        return eg._l2_entry_confirm(
            "ABCD", db=_TapeDB(_TOO_SHORT_TO_HALVE), settings=settings)
    if path == "l2_confirm_error":
        monkeypatch.setattr(eg, "signed_tape_accel_features",
                            lambda *a, **k: {"signed_tape_accel": object()})
        return eg._l2_entry_confirm("ABCD", db=_TapeDB([]), settings=settings)
    if path == "l2_confirm_tape_thrust":
        _no_book(monkeypatch)
        return eg._l2_entry_confirm("ABCD", db=_TapeDB(_CARRYING), settings=settings)
    if path == "l2_confirm_secondary_override":
        _with_book(monkeypatch, _book(ofi=0.9, micro=2.0, pctile=0.9))
        return eg._l2_entry_confirm("ABCD", db=_TapeDB(_FADING), settings=settings)
    if path == "l2_confirm_buying_not_carrying":
        _no_book(monkeypatch)
        return eg._l2_entry_confirm("ABCD", db=_TapeDB(_FADING), settings=settings)
    raise AssertionError(path)


@pytest.mark.parametrize("reason", sorted(VOCABULARY))
def test_each_path_reports_its_reason_decision_and_fallback(on, monkeypatch, reason):
    decision, dbg = _drive(monkeypatch, reason)
    want_decision, is_fallback = VOCABULARY[reason]
    assert dbg["reason"] == reason, dbg
    assert decision == want_decision
    if is_fallback:
        assert dbg.get("fallback") == "fail_open_confirm", (
            f"{reason} is a fail-open and must be NAMED as one on the receipt"
        )
    else:
        assert "fallback" not in dbg, f"{reason} is a decision, not a fallback"


def test_only_one_reason_refuses():
    """Every path but a not-carrying tape with no agreeing book says yes — and the
    parametrized test above proves each against the real function."""
    assert [d for d, _ in VOCABULARY.values()].count("defer") == 1


def test_a_zero_depth_corpus_removes_the_override(on, monkeypatch):
    """With no depth rows the book can never be the thing that overrides — recorded
    because the bench corpus has zero depth rows, so an A/B there exercises a
    DIFFERENT gate than production does."""
    _no_book(monkeypatch)
    d0, dbg0 = eg._l2_entry_confirm("ABCD", db=_TapeDB(_FADING), settings=settings)
    assert dbg0["depth_rising"] is False and d0 == "defer"
    _with_book(monkeypatch, _book(ofi=-0.5, micro=-1.0, pctile=0.8))
    d1, dbg1 = eg._l2_entry_confirm("ABCD", db=_TapeDB(_FADING), settings=settings)
    assert dbg1["depth_rising"] is True and d1 == "confirm"
    assert dbg1["reason"] == "l2_confirm_secondary_override"


# ── 3. the confirm that placed an ORDER is on the order's own receipt ────────────


def test_the_submitted_order_carries_the_confirmer_reason():
    """The on-change `live_l2_confirm_decision` can be suppressed by an `le` whose last
    reason already equals the pass's own (PSIG 21640 2026-09-10: a defer at
    17:25:13.177, `live_entry_submitted` at 17:25:20.696, no decision receipt between).
    So each ORDER carries the reason that let it through."""
    from app.services.trading.momentum_neural import live_runner as lr

    src = inspect.getsource(lr.tick_live_session)
    i = src.find('_emit(db, sess, "live_entry_submitted", {')
    assert i > 0
    j = src.find("})", i)
    block = src[i:j]
    assert '"l2_confirm_reason": _l2c_reason' in block
    assert '"l2_confirm_fallback": _l2c_dbg.get("fallback")' in block
    # …and the name it reads is bound by the confirmer seam BEFORE the submit.
    seam = src.find("_l2c_decision, _l2c_dbg = _l2_entry_confirm(")
    bind = src.find('_l2c_reason = str(_l2c_dbg.get("reason") or "").strip()')
    assert 0 < seam < bind < i


def test_the_confirmer_still_exists_where_the_runner_calls_it():
    """Guards the seam the receipt is attached to."""
    assert callable(getattr(eg, "_l2_entry_confirm", None))
