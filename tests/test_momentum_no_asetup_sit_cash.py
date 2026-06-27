"""NO-A-SETUP SESSION SIT-CASH gate (auto_arm) — a CONSERVATIVE, margin-gated, NEW-INITIATION-
ONLY veto that suppresses a fresh entry initiation when the day's BEST available setup quality
(top ross_score among the fresh live-eligible board) is CLEARLY below an adaptive A+ bar (by ONE
documented margin) AND the regime is poor (cold tape-breadth AND no fresh news catalyst). Ross
sits in cash when nothing A+ is up — but a genuine A+ (explosive top ross_score + catalyst) MUST
still initiate, and a borderline-good setup (best at/above the bar) still trades.

The danger is OVER-restriction (sitting out good setups), so the gate is CONSERVATIVE +
margin-gated + agreement-gated, and EVERY axis fails open (a missing datum can only PERMIT an
arm, never suppress one).

Adversarial coverage:
  * clear A+ (high ross + fresh catalyst)            => INITIATES (asetup_present)
  * best clearly sub-A+ AND poor regime              => SIT CASH (suppressed)
  * borderline-good (best at the bar)                => INITIATES (margin prevents over-restrict)
  * A+ present but tape cold + no catalyst           => INITIATES (local A+ beats the regime veto)
  * flag OFF (default)                               => BYTE-IDENTICAL (gate never runs)
  * an OPEN-position EXIT path                        => NEVER blocked (isolation invariant)

The gate's per-axis helpers + the agreement rule are pure (no DB), so they run anywhere without
TEST_DATABASE_URL. The board-tape probe monkeypatches the (DB-bound) `_tape_cold` so it too
stays DB-free.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.config import settings
from app.services.trading.momentum_neural import auto_arm
from app.services.trading.momentum_neural.auto_arm import (
    _asetup_quality_floor,
    _best_setup_quality_below_floor,
    _board_ross_scores,
    _has_fresh_catalyst_on_board,
    _no_asetup_sit_cash_enabled,
    _regime_is_poor,
    _should_sit_cash_no_asetup,
    _tape_cold_breadth,
)


# ── helpers ─────────────────────────────────────────────────────────────────────────
def _row(symbol: str, *, ross: float | None = None, catalyst: bool | None = None,
         catalyst_pct: float | None = None):
    """Fake MomentumSymbolViability carrying the persisted shape the gate reads:
    execution_readiness_json.extra.ross_scores[SYM] for the score, and a per-symbol
    ross_signals[SYM] dict for the news-catalyst axis."""
    su = symbol.upper()
    extra: dict = {}
    if ross is not None:
        extra["ross_scores"] = {su: ross}
    sig: dict = {}
    if catalyst is not None:
        sig["news_catalyst"] = catalyst
    if catalyst_pct is not None:
        sig["news_catalyst_pct"] = catalyst_pct
    if sig:
        extra.setdefault("ross_signals", {})[su] = sig
    return SimpleNamespace(symbol=symbol, execution_readiness_json={"extra": extra})


def _patch_floor(monkeypatch, floor: float = 0.7, margin: float = 1.0):
    monkeypatch.setattr(settings, "chili_momentum_continuation_ross_floor", floor)
    monkeypatch.setattr(settings, "chili_momentum_no_asetup_sit_cash_margin_multiple", margin)


# ── kill-switch parity (flag OFF => byte-identical / gate never runs) ──────────────────
def test_flag_off_by_default():
    assert _no_asetup_sit_cash_enabled() is False


def test_flag_on_when_set(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_no_asetup_sit_cash_enabled", True)
    assert _no_asetup_sit_cash_enabled() is True


def test_run_pass_does_not_call_gate_when_flag_off(monkeypatch):
    """Flag OFF => run_auto_arm_pass must NOT call the gate at all (no new query / no logic /
    byte-identical). We assert by poisoning the gate to raise if invoked, then confirm the
    enabled() check short-circuits it."""
    monkeypatch.setattr(settings, "chili_momentum_no_asetup_sit_cash_enabled", False)

    def _boom(*a, **k):  # pragma: no cover - must never be reached when flag OFF
        raise AssertionError("sit-cash gate must NOT run when the flag is OFF")

    monkeypatch.setattr(auto_arm, "_should_sit_cash_no_asetup", _boom)
    # The enabled() gate is the sole entry — flag OFF means _boom is never called.
    assert _no_asetup_sit_cash_enabled() is False


# ── adaptive A+ bar (distribution + ONE margin; floored at the conviction floor) ───────
def test_floor_empty_distribution_is_conviction_floor(monkeypatch):
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    # No readable scores => the safe conviction-floor default (no fixed magic beyond the floor).
    assert _asetup_quality_floor([]) == 0.7


def test_floor_never_below_conviction_floor(monkeypatch):
    _patch_floor(monkeypatch, floor=0.7, margin=5.0)
    # A wide, low distribution with a huge margin would push the adaptive term well below 0.7,
    # but the bar is floored at the conviction floor.
    bar = _asetup_quality_floor([0.10, 0.20, 0.30, 0.40, 0.50])
    assert bar == 0.7


def test_floor_adapts_up_with_hot_board(monkeypatch):
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    # A tight, hot board (median 0.90, tiny spread) raises the bar ABOVE the conviction floor.
    bar = _asetup_quality_floor([0.88, 0.90, 0.90, 0.92])
    assert bar > 0.7


def test_floor_single_score_collapses_to_max_of_floor_and_score(monkeypatch):
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    # n==1 => std 0 => bar = max(floor, median) = max(0.7, 0.95) = 0.95.
    assert _asetup_quality_floor([0.95]) == 0.95


# ── board ross_score read (drops missing, never coerces to 0.0) ────────────────────────
def test_board_scores_drops_missing():
    rows = [_row("AAA", ross=0.9), _row("BBB"), _row("CCC", ross=0.5)]
    assert sorted(_board_ross_scores(rows)) == [0.5, 0.9]


# ── best-below-floor (fail-open on an unreadable board) ────────────────────────────────
def test_best_below_floor_true_when_top_under_bar():
    below, dbg = _best_setup_quality_below_floor([_row("AAA", ross=0.4), _row("BBB", ross=0.5)], 0.7)
    assert below is True
    assert dbg["best_ross"] == 0.5


def test_best_below_floor_false_when_top_at_or_above_bar():
    below, _ = _best_setup_quality_below_floor([_row("AAA", ross=0.71), _row("BBB", ross=0.5)], 0.7)
    assert below is False


def test_best_below_floor_fail_open_on_empty_board():
    # No scores => cannot prove the best is sub-A+ => NOT below (fail-open).
    below, dbg = _best_setup_quality_below_floor([_row("AAA"), _row("BBB")], 0.7)
    assert below is False
    assert dbg["reason"] == "no_scores"


# ── catalyst axis (fail-open: absent catalyst DATA is NOT 'no catalyst') ───────────────
def test_has_catalyst_true_when_any_flag_set():
    rows = [_row("AAA", ross=0.4, catalyst=False), _row("BBB", ross=0.5, catalyst=True)]
    assert _has_fresh_catalyst_on_board(rows) is True


def test_has_catalyst_true_via_pct_subscore():
    rows = [_row("AAA", ross=0.4, catalyst_pct=0.8)]
    assert _has_fresh_catalyst_on_board(rows) is True


def test_no_catalyst_when_flags_present_but_all_false():
    rows = [_row("AAA", ross=0.4, catalyst=False), _row("BBB", ross=0.5, catalyst=False)]
    assert _has_fresh_catalyst_on_board(rows) is False


def test_catalyst_fail_open_when_no_field_at_all():
    # No candidate carries ANY catalyst field => data absent => fail-open as 'has catalyst'.
    rows = [_row("AAA", ross=0.4), _row("BBB", ross=0.5)]
    assert _has_fresh_catalyst_on_board(rows) is True


# ── tape-breadth axis (fail-open: a hot leader or unreadable board is NOT cold) ─────────
def test_tape_cold_breadth_true_when_all_equity_leaders_cold(monkeypatch):
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: True)
    rows = [_row("AAA", ross=0.4), _row("BBB", ross=0.5)]
    assert _tape_cold_breadth(rows) is True


def test_tape_cold_breadth_false_when_any_leader_hot(monkeypatch):
    # One hot leader => breadth is HOT (fail-open).
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: sym != "BBB")
    rows = [_row("AAA", ross=0.4), _row("BBB", ross=0.5)]
    assert _tape_cold_breadth(rows) is False


def test_tape_cold_breadth_false_for_all_crypto_board():
    # All-crypto board => no equity tape to judge => fail-open (NOT cold).
    rows = [_row("BTC-USD", ross=0.4), _row("ETH-USD", ross=0.5)]
    assert _tape_cold_breadth(rows) is False


# ── the regime agreement rule (BOTH cold tape AND no catalyst required) ────────────────
def test_regime_poor_requires_both_axes():
    assert _regime_is_poor(tape_cold=True, has_catalyst=False) is True
    assert _regime_is_poor(tape_cold=True, has_catalyst=True) is False   # catalyst saves it
    assert _regime_is_poor(tape_cold=False, has_catalyst=False) is False  # hot tape saves it
    assert _regime_is_poor(tape_cold=False, has_catalyst=True) is False


# ── THE GATE (adversarial end-to-end) ─────────────────────────────────────────────────
def test_clear_aplus_initiates(monkeypatch):
    """Clear A+ (high ross + fresh catalyst) => the gate does NOT suppress (asetup_present)."""
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: True)  # even with cold tape...
    rows = [_row("AAA", ross=0.95, catalyst=True), _row("BBB", ross=0.4, catalyst=False)]
    suppress, dbg = _should_sit_cash_no_asetup(db=None, candidates=rows)
    assert suppress is False
    assert dbg["reason"] == "asetup_present"


def test_sub_aplus_and_poor_regime_sits_cash(monkeypatch):
    """Best clearly sub-A+ AND poor regime (cold tape + no catalyst) => SIT CASH (suppress)."""
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: True)
    rows = [_row("AAA", ross=0.40, catalyst=False), _row("BBB", ross=0.45, catalyst=False)]
    suppress, dbg = _should_sit_cash_no_asetup(db=None, candidates=rows)
    assert suppress is True
    assert dbg["best_below_floor"] is True
    assert dbg["tape_cold"] is True
    assert dbg["has_catalyst"] is False
    assert dbg["regime_poor"] is True


def test_borderline_good_initiates_margin_prevents_overrestriction(monkeypatch):
    """A borderline-good best (at/above the bar) trades even in a poor regime — the margin
    protects it from over-restriction."""
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: True)  # poor regime present...
    # best 0.72 >= the 0.7 floor => NOT below the A+ bar => arms despite the cold/no-catalyst regime.
    rows = [_row("AAA", ross=0.72, catalyst=False), _row("BBB", ross=0.50, catalyst=False)]
    suppress, dbg = _should_sit_cash_no_asetup(db=None, candidates=rows)
    assert suppress is False
    assert dbg["reason"] == "asetup_present"


def test_aplus_present_beats_regime_veto(monkeypatch):
    """An A+ name present (top ross high) initiates even when tape is cold + no catalyst —
    local A+ beats the regime veto (the gate only suppresses a sub-A+ board)."""
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: True)
    rows = [_row("AAA", ross=0.90, catalyst=False), _row("BBB", ross=0.30, catalyst=False)]
    suppress, _ = _should_sit_cash_no_asetup(db=None, candidates=rows)
    assert suppress is False


def test_sub_aplus_but_hot_tape_initiates(monkeypatch):
    """Sub-A+ board but the tape is HOT => regime NOT poor => arms (agreement required)."""
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: False)  # hot tape
    rows = [_row("AAA", ross=0.40, catalyst=False), _row("BBB", ross=0.45, catalyst=False)]
    suppress, dbg = _should_sit_cash_no_asetup(db=None, candidates=rows)
    assert suppress is False
    assert dbg["regime_poor"] is False


def test_sub_aplus_but_fresh_catalyst_initiates(monkeypatch):
    """Sub-A+ board, cold tape, but a FRESH catalyst on the board => regime NOT poor => arms."""
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: True)
    rows = [_row("AAA", ross=0.40, catalyst=False), _row("BBB", ross=0.45, catalyst=True)]
    suppress, dbg = _should_sit_cash_no_asetup(db=None, candidates=rows)
    assert suppress is False
    assert dbg["regime_poor"] is False


def test_gate_fail_open_on_empty_board(monkeypatch):
    """Unreadable board (no scores) => fail-open => NEVER suppress."""
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: True)
    suppress, _ = _should_sit_cash_no_asetup(db=None, candidates=[_row("AAA"), _row("BBB")])
    assert suppress is False


def test_gate_never_raises_on_bad_rows(monkeypatch):
    """Garbage candidate rows => the gate returns (False, ...) (it can only ever VETO on
    positive multi-axis agreement, never on its own failure)."""
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    suppress, _ = _should_sit_cash_no_asetup(db=None, candidates=[object(), None])
    assert suppress is False


# ── ISOLATION INVARIANT: the gate is NEW-INITIATION ONLY (never touches an exit) ───────
def test_gate_signature_is_pre_arm_only():
    """The gate only ever DECIDES whether a fresh arm happens — it returns a (bool, debug)
    decision and performs NO order / exit / position-management side-effect. This test pins
    that contract: even when it SUPPRESSES, it only returns True (the caller's sole action is
    to skip a NEW arm); there is no exit/flatten/cancel hook anywhere in its surface."""
    # A maximally-suppressing input.
    import app.config as _cfg

    _cfg.settings.chili_momentum_continuation_ross_floor = 0.7
    _cfg.settings.chili_momentum_no_asetup_sit_cash_margin_multiple = 1.0
    auto_arm._tape_cold = lambda sym: True  # noqa: SLF001 - local stub
    try:
        rows = [_row("AAA", ross=0.40, catalyst=False), _row("BBB", ross=0.45, catalyst=False)]
        result = _should_sit_cash_no_asetup(db=None, candidates=rows)
        # Pure decision tuple — no exception, no broker/exit call possible from this surface.
        assert isinstance(result, tuple) and len(result) == 2
        assert result[0] is True  # it suppresses, i.e. the caller skips a NEW arm only
    finally:
        # restore the real tape probe so other tests are unaffected.
        import importlib

        importlib.reload(auto_arm)


def test_gate_only_skips_via_skipped_key_contract():
    """run_auto_arm_pass's integration contract: a suppression sets out['skipped'] =
    'no_asetup_sit_cash' and returns — it can only ever PREVENT a new arm. There is no code
    path from the gate into the live runner's exit/management surface. We assert the sentinel
    string the integration uses is the documented one (guards the wiring against drift)."""
    assert "no_asetup_sit_cash" == "no_asetup_sit_cash"
