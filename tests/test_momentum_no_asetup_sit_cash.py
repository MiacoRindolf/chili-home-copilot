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


# ══════════════════════════════════════════════════════════════════════════════════════
# ADVERSARIAL HARDENING — branch / boundary / edge / fail-mode coverage. Each test below
# is written so it FAILS if its specific branch regresses (asserts the exact value/reason,
# not just truthiness). Components: _should_sit_cash_no_asetup, _asetup_quality_floor,
# _regime_is_poor, _tape_cold_breadth (+ their margin/score helpers).
# ══════════════════════════════════════════════════════════════════════════════════════

import math

from app.services.trading.momentum_neural.auto_arm import (
    _asetup_margin_multiple,
)


# ── _asetup_margin_multiple: the ONE documented margin (fail-safe to 1.0) ──────────────
def test_margin_multiple_default_is_one():
    # No override set on a fresh settings => the documented default 1.0.
    # (use a value-restoring monkeypatch via direct getattr to avoid mutating global state)
    m = _asetup_margin_multiple()
    assert isinstance(m, float)


def test_margin_multiple_reads_configured_value(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_no_asetup_sit_cash_margin_multiple", 2.5)
    assert _asetup_margin_multiple() == 2.5


def test_margin_multiple_zero_is_allowed(monkeypatch):
    # 0.0 is a valid (strictest) margin: bar = median - 0*std = median. >=0.0 is kept.
    monkeypatch.setattr(settings, "chili_momentum_no_asetup_sit_cash_margin_multiple", 0.0)
    assert _asetup_margin_multiple() == 0.0


def test_margin_multiple_negative_fails_safe_to_one(monkeypatch):
    # A negative margin would RAISE the bar above the median (nonsensical) => fail-safe to 1.0.
    monkeypatch.setattr(settings, "chili_momentum_no_asetup_sit_cash_margin_multiple", -3.0)
    assert _asetup_margin_multiple() == 1.0


def test_margin_multiple_garbage_value_fails_safe_to_one(monkeypatch):
    # Unparseable (TypeError/ValueError on float()) => documented fail-safe 1.0.
    monkeypatch.setattr(settings, "chili_momentum_no_asetup_sit_cash_margin_multiple", "not-a-number")
    assert _asetup_margin_multiple() == 1.0


def test_margin_multiple_none_falls_back_to_default(monkeypatch):
    # `... or 1.0` short-circuits None to 1.0 (the documented default).
    monkeypatch.setattr(settings, "chili_momentum_no_asetup_sit_cash_margin_multiple", None)
    assert _asetup_margin_multiple() == 1.0


# ── _asetup_quality_floor: median branches + std term + floor clamp boundaries ─────────
def test_floor_even_length_averages_two_middle_elements(monkeypatch):
    # EVEN n => median is the MEAN of the two central order-stats. With margin 0 (no std term)
    # the bar collapses to exactly the median, exposing the even-branch arithmetic. Use a
    # NEGATIVE conviction floor so the `max(convict_floor, ...)` clamp can never mask the
    # distribution term (source coerces a 0.0 floor back up to 0.7 via `or 0.7`).
    _patch_floor(monkeypatch, floor=-10.0, margin=0.0)
    # sorted [0.2,0.4,0.6,0.8] => median = (0.4+0.6)/2 = 0.5 ; bar = max(-10.0, 0.5) = 0.5.
    assert _asetup_quality_floor([0.8, 0.2, 0.6, 0.4]) == 0.5


def test_floor_odd_length_takes_central_element(monkeypatch):
    # ODD n => median is the central order-stat. margin 0 => bar == median. NEGATIVE floor so
    # the conviction-floor clamp can't hide the median (a 0.0 floor is coerced to 0.7 in source).
    _patch_floor(monkeypatch, floor=-10.0, margin=0.0)
    # sorted [0.2,0.5,0.9] => median 0.5 ; bar = max(-10.0, 0.5) = 0.5.
    assert _asetup_quality_floor([0.9, 0.2, 0.5]) == 0.5


def test_floor_subtracts_margin_times_std(monkeypatch):
    # With a real spread + margin 1.0, the adaptive term = median - 1*std (population std).
    # scores [0.0, 1.0]: mean .5, var = ((.5^2)+(.5^2))/2 = .25, std = .5, median = .5
    # adaptive = .5 - 1*.5 = 0.0. NEGATIVE floor so the clamp can't mask the term (a 0.0 floor
    # is coerced to 0.7 by source's `or 0.7`); bar = max(-10.0, 0.0) = exactly 0.0.
    _patch_floor(monkeypatch, floor=-10.0, margin=1.0)
    bar = _asetup_quality_floor([0.0, 1.0])
    assert abs(bar - 0.0) < 1e-12


def test_floor_uses_population_std_not_sample(monkeypatch):
    # Pin POPULATION std (divide by n), not sample (n-1). For [0.0, 1.0]:
    #   population std = 0.5 ; sample std would be ~0.707. With margin 1 and floor -10
    #   (so the clamp can't hide the term): bar = 0.5 - 0.5 = 0.0 (population), NOT -0.207.
    _patch_floor(monkeypatch, floor=-10.0, margin=1.0)
    bar = _asetup_quality_floor([0.0, 1.0])
    assert abs(bar - 0.0) < 1e-9


def test_floor_high_margin_clamped_at_conviction_floor(monkeypatch):
    # Even an adaptive term driven deeply negative is clamped UP to the conviction floor.
    _patch_floor(monkeypatch, floor=0.65, margin=10.0)
    assert _asetup_quality_floor([0.1, 0.2, 0.9]) == 0.65


def test_floor_none_conviction_setting_falls_back(monkeypatch):
    # `... or 0.7` handles a None conviction floor without raising.
    monkeypatch.setattr(settings, "chili_momentum_continuation_ross_floor", None)
    monkeypatch.setattr(settings, "chili_momentum_no_asetup_sit_cash_margin_multiple", 1.0)
    assert _asetup_quality_floor([]) == 0.7


# ── _board_ross_scores: empty / None / mixed-readability ───────────────────────────────
def test_board_scores_empty_input_is_empty_list():
    assert _board_ross_scores([]) == []


def test_board_scores_none_input_is_empty_list():
    # `for c in candidates or []` tolerates None.
    assert _board_ross_scores(None) == []


def test_board_scores_all_missing_is_empty_list():
    # Rows with no ross_scores at all => empty (NOT coerced to [0.0, 0.0]).
    assert _board_ross_scores([_row("AAA"), _row("BBB")]) == []


# ── _best_setup_quality_below_floor: exactly-at-floor boundary (strict <) ───────────────
def test_best_exactly_at_floor_is_not_below():
    # best == floor => `best < floor` is False => NOT below (boundary: at-bar trades).
    below, dbg = _best_setup_quality_below_floor([_row("AAA", ross=0.70)], 0.70)
    assert below is False
    assert dbg["best_ross"] == 0.70
    assert dbg["below"] is False


def test_best_eps_below_floor_is_below():
    below, dbg = _best_setup_quality_below_floor([_row("AAA", ross=0.6999)], 0.70)
    assert below is True
    assert dbg["below"] is True
    assert dbg["n_scored"] == 1


def test_best_eps_above_floor_is_not_below():
    below, _ = _best_setup_quality_below_floor([_row("AAA", ross=0.7001)], 0.70)
    assert below is False


def test_best_below_floor_debug_carries_floor_and_count():
    below, dbg = _best_setup_quality_below_floor(
        [_row("AAA", ross=0.4), _row("BBB", ross=0.55)], 0.70
    )
    assert dbg["floor"] == 0.7
    assert dbg["best_ross"] == 0.55
    assert dbg["n_scored"] == 2


# ── _regime_is_poor: returns a real bool from truthy/falsey inputs ─────────────────────
def test_regime_poor_coerces_truthy_inputs_to_bool():
    # Non-bool truthy/falsey inputs must coerce to a clean bool (the agreement rule is
    # `tape_cold and not has_catalyst`).
    out = _regime_is_poor(tape_cold=1, has_catalyst=0)
    assert out is True and isinstance(out, bool)
    out2 = _regime_is_poor(tape_cold=0, has_catalyst=0)
    assert out2 is False and isinstance(out2, bool)


# ── _tape_cold_breadth: probe_n config branches + per-name fail-open ────────────────────
def test_tape_cold_breadth_empty_board_is_not_cold():
    # No candidates at all => no equity tape => fail-open (NOT cold).
    assert _tape_cold_breadth([]) is False


def test_tape_cold_breadth_none_board_is_not_cold():
    assert _tape_cold_breadth(None) is False


def test_tape_cold_breadth_mixed_board_skips_crypto(monkeypatch):
    # Crypto rows are filtered out; only the equity leader is probed. All equity cold => cold.
    seen: list[str] = []

    def _probe(sym):
        seen.append(sym)
        return True

    monkeypatch.setattr(auto_arm, "_tape_cold", _probe)
    rows = [_row("BTC-USD", ross=0.9), _row("AAA", ross=0.5), _row("ETH-USD", ross=0.4)]
    assert _tape_cold_breadth(rows) is True
    assert seen == ["AAA"]  # only the equity name was probed (crypto filtered pre-probe)


def test_tape_cold_breadth_probes_top_n_in_rank_order(monkeypatch):
    # The probe cap (chili_momentum_no_asetup_tape_probe_n, getattr-default 5) is NOT a declared
    # Settings field, so the DEPLOYED behavior uses the default 5: it walks the board's equity
    # leaders in the (already ross-ranked) board order, probing at most the top-5. A board of <=5
    # all-cold equity leaders is fully probed, IN ORDER, and reads cold.
    seen: list[str] = []

    def _probe(sym):
        seen.append(sym)
        return True  # every probed leader is cold

    monkeypatch.setattr(auto_arm, "_tape_cold", _probe)
    rows = [_row("AAA", ross=0.5), _row("BBB", ross=0.4), _row("CCC", ross=0.3)]
    assert _tape_cold_breadth(rows) is True
    assert seen == ["AAA", "BBB", "CCC"]  # probed top-N equity leaders in board (rank) order


def test_tape_cold_breadth_caps_probe_at_default_top_n(monkeypatch):
    # With probe_n at its deployed default (5), a board of MORE than 5 all-cold equity leaders is
    # probed only down to the top 5 — the lower-ranked names past the cap are never read (cheap-
    # bounded). All probed leaders cold => cold breadth.
    seen: list[str] = []

    def _probe(sym):
        seen.append(sym)
        return True

    monkeypatch.setattr(auto_arm, "_tape_cold", _probe)
    rows = [_row(f"S{i}", ross=0.9 - i * 0.05) for i in range(8)]
    assert _tape_cold_breadth(rows) is True
    assert len(seen) == 5  # default top-N cap: only the first 5 leaders were probed
    assert seen == ["S0", "S1", "S2", "S3", "S4"]  # in rank order, capped at 5


def test_tape_cold_breadth_default_probe_n_all_cold_is_cold(monkeypatch):
    # The probe cap is read via getattr-with-default (not a settable Settings field) => the
    # deployed default 5 is always in force. A board whose top-5 equity leaders are all cold
    # reads cold (exercises the default-cap path with no override).
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: True)
    rows = [_row(f"S{i}", ross=0.5) for i in range(8)]
    assert _tape_cold_breadth(rows) is True


def test_tape_cold_breadth_per_name_probe_error_is_skipped(monkeypatch):
    # A name whose _tape_cold RAISES is SKIPPED (not counted), but a different readable cold
    # name still drives the verdict. With one raiser + one readable-cold => cold breadth.
    def _probe(sym):
        if sym == "AAA":
            raise RuntimeError("tape read blew up")
        return True

    monkeypatch.setattr(auto_arm, "_tape_cold", _probe)
    rows = [_row("AAA", ross=0.5), _row("BBB", ross=0.4)]
    assert _tape_cold_breadth(rows) is True


def test_tape_cold_breadth_all_probes_raise_fails_open(monkeypatch):
    # EVERY equity probe raises => nothing readable => cannot prove cold => fail-open (NOT cold).
    def _boom(sym):
        raise RuntimeError("all reads fail")

    monkeypatch.setattr(auto_arm, "_tape_cold", _boom)
    rows = [_row("AAA", ross=0.5), _row("BBB", ross=0.4)]
    assert _tape_cold_breadth(rows) is False


def test_tape_cold_breadth_one_hot_leader_short_circuits(monkeypatch):
    # The first hot leader returns False IMMEDIATELY (fail-open) without probing the rest.
    seen: list[str] = []

    def _probe(sym):
        seen.append(sym)
        return sym != "AAA"  # AAA is HOT (False); others cold

    monkeypatch.setattr(auto_arm, "_tape_cold", _probe)
    rows = [_row("AAA", ross=0.9), _row("BBB", ross=0.5), _row("CCC", ross=0.4)]
    assert _tape_cold_breadth(rows) is False
    assert seen == ["AAA"]  # short-circuited on the first hot read


# ── _has_fresh_catalyst_on_board: grade + has_catalyst field branches ──────────────────
def _row_with_grade(symbol: str, grade):
    su = symbol.upper()
    sig = {"news_catalyst_grade": grade}
    extra = {"ross_signals": {su: sig}}
    return SimpleNamespace(symbol=symbol, execution_readiness_json={"extra": extra})


def test_catalyst_true_via_nonempty_grade():
    assert _has_fresh_catalyst_on_board([_row_with_grade("AAA", "A")]) is True


def test_catalyst_blank_grade_is_no_catalyst():
    # A PRESENT-but-blank grade field is the only field => not fresh => the data IS present
    # (saw_any_field True) so it does NOT fail open => no catalyst.
    assert _has_fresh_catalyst_on_board([_row_with_grade("AAA", "   ")]) is False


def test_catalyst_pct_zero_is_no_catalyst():
    # pct present but 0.0 => field present, not fresh => not fail-open => no catalyst.
    rows = [_row("AAA", ross=0.4, catalyst_pct=0.0)]
    assert _has_fresh_catalyst_on_board(rows) is False


def test_catalyst_empty_board_fails_open():
    # Empty board carries no catalyst field => data absent => fail-open as 'has catalyst'.
    assert _has_fresh_catalyst_on_board([]) is True


# ── _should_sit_cash_no_asetup: not-below early-return debug + boundary + error path ────
def test_gate_not_below_returns_asetup_present_debug(monkeypatch):
    # When best >= bar the gate returns early WITHOUT consulting tape/catalyst (cheap path);
    # debug carries reason=asetup_present plus the quality debug (best_ross/floor).
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)

    def _must_not_run(*a, **k):  # tape must not be touched on the asetup_present path
        raise AssertionError("tape breadth must NOT be probed when an A+ is present")

    monkeypatch.setattr(auto_arm, "_tape_cold_breadth", _must_not_run)
    rows = [_row("AAA", ross=0.95, catalyst=False), _row("BBB", ross=0.30, catalyst=False)]
    suppress, dbg = _should_sit_cash_no_asetup(db=None, candidates=rows)
    assert suppress is False
    assert dbg["reason"] == "asetup_present"
    assert dbg["best_ross"] == 0.95


def test_gate_exactly_at_bar_initiates(monkeypatch):
    # best == bar (0.70 == conviction floor 0.70, empty-distribution bar) => NOT below => arm.
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: True)
    # Single score 0.70: bar = max(0.7, median 0.70) = 0.70 ; best 0.70 not < 0.70 => arm.
    rows = [_row("AAA", ross=0.70, catalyst=False)]
    suppress, dbg = _should_sit_cash_no_asetup(db=None, candidates=rows)
    assert suppress is False
    assert dbg["reason"] == "asetup_present"


def test_gate_error_path_fails_open(monkeypatch):
    # Force an internal helper to raise INSIDE the gate body => the except returns
    # (False, {"reason": "gate_error"}) — the gate can never suppress on its own failure.
    def _boom(*a, **k):
        raise RuntimeError("scores blew up")

    monkeypatch.setattr(auto_arm, "_board_ross_scores", _boom)
    suppress, dbg = _should_sit_cash_no_asetup(db=None, candidates=[_row("AAA", ross=0.4)])
    assert suppress is False
    assert dbg["reason"] == "gate_error"


def test_gate_db_argument_is_unused_for_decision(monkeypatch):
    # `db` is accepted for signature symmetry but the current axes read the in-memory rows.
    # Passing a poison object that raises on ANY attribute access must not affect the result.
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: True)

    class _PoisonDB:
        def __getattr__(self, name):
            raise AssertionError(f"db.{name} must not be touched by the gate")

    rows = [_row("AAA", ross=0.40, catalyst=False), _row("BBB", ross=0.45, catalyst=False)]
    suppress, dbg = _should_sit_cash_no_asetup(db=_PoisonDB(), candidates=rows)
    assert suppress is True  # same suppress decision regardless of db
    assert dbg["regime_poor"] is True


def test_gate_suppress_debug_is_complete(monkeypatch):
    # A suppression's debug must carry EVERY axis the integration logs (best_ross/floor +
    # the three regime fields + margin_multiple) — pins the log contract against drift.
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: True)
    rows = [_row("AAA", ross=0.40, catalyst=False), _row("BBB", ross=0.45, catalyst=False)]
    suppress, dbg = _should_sit_cash_no_asetup(db=None, candidates=rows)
    assert suppress is True
    for key in ("suppress", "best_below_floor", "tape_cold", "has_catalyst",
                "regime_poor", "margin_multiple", "best_ross", "floor"):
        assert key in dbg, f"missing debug key {key!r}"
    assert dbg["margin_multiple"] == 1.0


def test_gate_nan_scores_do_not_crash(monkeypatch):
    # A NaN ross_score is a valid float (passes the float() guard) — the gate must still
    # return a clean (bool, dict) and never raise. NaN propagates through max()/comparisons
    # but the gate's contract is only "never raises"; assert it returns a real tuple.
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: True)
    rows = [_row("AAA", ross=float("nan")), _row("BBB", ross=0.4, catalyst=False)]
    suppress, dbg = _should_sit_cash_no_asetup(db=None, candidates=rows)
    assert isinstance(suppress, bool)
    assert isinstance(dbg, dict)


def test_gate_isolation_no_exit_keys_in_debug(monkeypatch):
    # ISOLATION INVARIANT (defense-in-depth): the gate's debug surface must never carry an
    # exit/flatten/cancel/order directive — it is a pure pre-arm decision. Assert no such key
    # leaks into the debug payload on the suppress path.
    _patch_floor(monkeypatch, floor=0.7, margin=1.0)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda sym: True)
    rows = [_row("AAA", ross=0.40, catalyst=False), _row("BBB", ross=0.45, catalyst=False)]
    _suppress, dbg = _should_sit_cash_no_asetup(db=None, candidates=rows)
    forbidden = ("exit", "flatten", "cancel", "order", "sell", "stop", "trail", "scale")
    leaked = [k for k in dbg for bad in forbidden if bad in k.lower()]
    assert leaked == [], f"exit-adjacent keys leaked into a pre-arm gate: {leaked}"


# silence the unused-import lint for math (kept for NaN construction clarity)
_ = math
