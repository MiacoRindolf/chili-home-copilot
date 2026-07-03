"""Ross-batch2 QUCY-vs-ILLR lesson (AUDIT_REPORT_BATCH2.md row #9): grade a STRONG catalyst
higher/lower by HEADLINE VERB QUALITY + DOLLAR AMOUNT.

  * COMPLETED-ACTION verbs (acquires / signed / definitive agreement / awarded) => BOOST.
  * TENTATIVE / pursuit verbs (approves pursuit / explores / letter of intent)  => DE-BOOST
    (the QUCY +24%-pop-and-fade class).
  * DOLLAR-AMOUNT in the headline ($400M class)                                 => added BOOST,
    scaled adaptively vs market cap when known, else base tiers (>=$100M strong / >=$10M mod).

FAIL-CLOSED: a plain PR with no verb/dollar signal grades 0 (byte-identical to flag-off). ONE
flag chili_momentum_catalyst_action_grading_enabled (default True).
"""
from __future__ import annotations

from app.config import settings
from app.services.trading.momentum_neural.catalyst import (
    _action_completeness_grade,
    _catalyst_tilt,
    _dollar_boost_frac,
    _parse_dollar_amount,
    action_dollar_grade_delta,
    catalyst_action_class,
    catalyst_class_reliability_multiplier,
    catalyst_grade_selection_delta,
    CATALYST_ACTION_CLASSES,
    _ACTION_COMPLETENESS_TILT_FRAC,
    _DOLLAR_BOOST_STRONG_FRAC,
    _DOLLAR_BOOST_MODERATE_FRAC,
)

# The two real headlines from the audit (row #9).
ILLR_HEADLINE = "Triller Group to acquire a $400 million SpaceX treasury stake"
QUCY_HEADLINE = "Quantum Cyber board approves pursuit of strategic alternatives including a SpaceX stake"
PLAIN_PR = "Acme Corp announces positive Phase 3 trial results"  # strong TYPE, no action/dollar verb


# ── completeness classifier ──────────────────────────────────────────────────

def test_completed_action_detected():
    assert _action_completeness_grade("BigCo to acquire Acme in a definitive agreement") == 1
    assert _action_completeness_grade("Acme signed a merger agreement") == 1
    assert _action_completeness_grade("Acme awarded a government contract") == 1
    assert _action_completeness_grade("Acme receives FDA approval") == 1
    assert _action_completeness_grade(ILLR_HEADLINE) == 1


def test_tentative_action_detected():
    assert _action_completeness_grade("Board approves pursuit of a stake") == -1
    assert _action_completeness_grade("Acme explores strategic alternatives") == -1
    assert _action_completeness_grade("Acme signs a letter of intent to acquire") == -1  # LOI dominates
    assert _action_completeness_grade("Acme intends to acquire a rival") == -1
    assert _action_completeness_grade(QUCY_HEADLINE) == -1


def test_neither_verb_class_is_zero():
    assert _action_completeness_grade(PLAIN_PR) == 0
    assert _action_completeness_grade("Acme reports Q2 earnings") == 0
    assert _action_completeness_grade("") == 0
    assert _action_completeness_grade(None) == 0


def test_tentative_dominates_a_cooccurring_completed_word():
    # "letter of intent" + "acquire" -> intent dominates (the QUCY trap).
    assert _action_completeness_grade("Acme signs a letter of intent, plans to acquire target") == -1


# ── dollar extraction ────────────────────────────────────────────────────────

def test_dollar_amount_parsing():
    assert _parse_dollar_amount("a $400 million stake") == 400_000_000.0
    assert _parse_dollar_amount("$400M deal") == 400_000_000.0
    assert _parse_dollar_amount("$1.2 billion acquisition") == 1_200_000_000.0
    assert _parse_dollar_amount("$400,000,000 contract") == 400_000_000.0
    assert _parse_dollar_amount("$50M offering") == 50_000_000.0
    assert _parse_dollar_amount("no dollars here") is None
    assert _parse_dollar_amount("") is None


def test_dollar_amount_picks_largest():
    assert _parse_dollar_amount("raises $10M in a $400 million round") == 400_000_000.0


def test_dollar_boost_tiers():
    # absolute tiers when market cap unknown
    assert _dollar_boost_frac(400_000_000.0, None) == _DOLLAR_BOOST_STRONG_FRAC   # >= $100M
    assert _dollar_boost_frac(50_000_000.0, None) == _DOLLAR_BOOST_MODERATE_FRAC  # >= $10M
    assert _dollar_boost_frac(5_000_000.0, None) == 0.0                           # < $10M
    assert _dollar_boost_frac(None, None) == 0.0


def test_dollar_boost_adaptive_vs_market_cap():
    # a $30M deal on a $200M-cap micro-float is >= 10% of cap => material => strong boost,
    # even though $30M is only the MODERATE absolute tier.
    assert _dollar_boost_frac(30_000_000.0, 200_000_000.0) == _DOLLAR_BOOST_STRONG_FRAC
    # a $30M deal on a $10B-cap large-cap is immaterial => falls back to moderate absolute tier.
    assert _dollar_boost_frac(30_000_000.0, 10_000_000_000.0) == _DOLLAR_BOOST_MODERATE_FRAC


# ── the combined delta: ILLR-high / QUCY-neutral-or-de-boost / plain parity ────

def test_illr_headline_grades_high_boost():
    tilt = _catalyst_tilt()
    d = action_dollar_grade_delta(ILLR_HEADLINE)  # completed + $400M
    # completed (+0.5) + $400M strong (+0.5) = +1.0 * tilt
    assert d > 0
    assert d == round(tilt * (_ACTION_COMPLETENESS_TILT_FRAC + _DOLLAR_BOOST_STRONG_FRAC), 6)


def test_qucy_headline_grades_neutral_or_deboost():
    d = action_dollar_grade_delta(QUCY_HEADLINE)  # tentative, no dollar in this variant
    # pursuit language => negative completeness, no dollar => net negative (de-boost).
    assert d < 0


def test_qucy_with_a_dollar_still_not_a_strong_boost():
    # even a pursuit headline that names a big number nets <= 0 unless the number is material
    # (intent + a big number is Ross's exact pop-and-fade trap).
    d = action_dollar_grade_delta("Board approves pursuit of a potential $200 million SpaceX stake")
    tilt = _catalyst_tilt()
    # -0.5 (tentative) + 0.5 ($200M strong tier) = 0.0 * tilt -> nets to zero, NOT a positive boost.
    assert d <= 0.0


def test_plain_pr_unchanged_vs_flag_off_byte_identical():
    # a strong-TYPE PR with no completed/tentative verb and no dollar => 0 (fail-closed),
    # so it is byte-identical whether the flag is on or off.
    d_on = action_dollar_grade_delta(PLAIN_PR)
    assert d_on == 0.0


def test_flag_off_makes_delta_zero():
    orig = settings.chili_momentum_catalyst_action_grading_enabled
    try:
        settings.chili_momentum_catalyst_action_grading_enabled = False
        assert action_dollar_grade_delta(ILLR_HEADLINE) == 0.0   # flag off -> no delta
        assert action_dollar_grade_delta(QUCY_HEADLINE) == 0.0
    finally:
        settings.chili_momentum_catalyst_action_grading_enabled = orig


# ── selection-delta integration (only STRONG names get the action refinement) ──

def test_selection_delta_adds_action_boost_for_strong_illr():
    tilt = _catalyst_tilt()
    base = tilt * 0.5  # the pre-batch2 strong boost
    illr_delta = action_dollar_grade_delta(ILLR_HEADLINE)
    d = catalyst_grade_selection_delta(
        "ILLR",
        strong_symbols={"ILLR"},
        action_deltas={"ILLR": illr_delta},
    )
    assert d == round(base + illr_delta, 6)
    assert d > base  # ILLR is boosted ABOVE the plain strong tilt


def test_selection_delta_deboosts_strong_qucy():
    tilt = _catalyst_tilt()
    base = tilt * 0.5
    qucy_delta = action_dollar_grade_delta(QUCY_HEADLINE)  # negative
    d = catalyst_grade_selection_delta(
        "QUCY",
        strong_symbols={"QUCY"},          # QUCY matched a strong keyword ("stake"/"alternatives")
        action_deltas={"QUCY": qucy_delta},
    )
    assert d == round(base + qucy_delta, 6)
    assert d < base  # QUCY is DE-boosted below the plain strong tilt (the pop-and-fade class)


def test_selection_delta_byte_identical_without_action_deltas():
    # absent action map -> byte-identical to the prior strong-boost path.
    a = catalyst_grade_selection_delta("ABCD", strong_symbols={"ABCD"})
    b = catalyst_grade_selection_delta("ABCD", strong_symbols={"ABCD"}, action_deltas=None)
    c = catalyst_grade_selection_delta("ABCD", strong_symbols={"ABCD"}, action_deltas={})
    assert a == b == c == _catalyst_tilt() * 0.5


def test_selection_delta_action_only_applies_to_strong_names():
    # a NON-strong name (not in strong_symbols) never gets an action boost, even if the map
    # carries a delta for it (weak/fake/medium dominance is unchanged).
    d = catalyst_grade_selection_delta("ABCD", strong_symbols={"OTHER"}, action_deltas={"ABCD": 1.0})
    assert d == 0.0


def test_weak_still_dominates_action_boost():
    # a name that is BOTH weak and carries a positive action delta stays a full de-boost
    # (dilution dominates — Ross distrusts a diluting "acquisition").
    d = catalyst_grade_selection_delta(
        "ABCD",
        weak_symbols={"ABCD"},
        strong_symbols={"ABCD"},
        action_deltas={"ABCD": 1.0},
    )
    assert d == -_catalyst_tilt()


def test_crypto_always_zero():
    # action_dollar_grade_delta is a pure TITLE-level fn (crypto has no headline concept), so it
    # returns a number for any string; the crypto exclusion lives in the SYMBOL-level selection
    # delta, which returns 0 for any -USD pair regardless of the action map.
    assert catalyst_grade_selection_delta(
        "BTC-USD", strong_symbols={"BTC"}, action_deltas={"BTC": 1.0}
    ) == 0.0


# ── A9: MERGER-class reliability notch (Ross CLRO-lesson: "merger agreements ... don't always") ──


def test_merger_headline_still_grades_completed():
    # A merger / definitive agreement is STILL a completed action (+1) — byte-identical grade.
    assert _action_completeness_grade("Acme signed a merger agreement") == 1
    assert _action_completeness_grade("BigCo enters definitive agreement to combine") == 1
    assert _action_completeness_grade("Target completes merger with Acquirer") == 1


def test_merger_class_label_emitted():
    assert catalyst_action_class("Acme signed a merger agreement") == "merger"
    assert catalyst_action_class("BigCo enters definitive agreement") == "merger"
    assert catalyst_action_class("Target completes merger with Acquirer") == "merger"


def test_other_action_class_labels():
    assert catalyst_action_class("Acme to acquire Beta Corp") == "acquisition"
    assert catalyst_action_class("Acme receives FDA approval") == "approval"
    assert catalyst_action_class("Acme awarded a government contract") == "contract"
    assert catalyst_action_class("Board approves pursuit of a stake") == "tentative"
    assert catalyst_action_class("Acme reports Q2 earnings") == "none"
    assert catalyst_action_class("") == "none"
    assert catalyst_action_class(None) == "none"
    # every emitted label is a member of the declared class set
    for title in (
        "Acme signed a merger agreement", "Acme to acquire Beta", "Acme receives FDA approval",
        "Acme awarded a contract", "Board explores alternatives", "Acme reports earnings",
    ):
        assert catalyst_action_class(title) in CATALYST_ACTION_CLASSES


def test_merger_reliability_multiplier_is_exactly_one_without_samples():
    # Ross's n=1 merger: NO labeled history => the class multiplier is EXACTLY 1.0
    # (byte-identical grade until the class is trained). FAIL direction: no history => 1.0.
    assert catalyst_class_reliability_multiplier("merger") == 1.0
    assert catalyst_class_reliability_multiplier("acquisition") == 1.0
    # non-named classes are never reliability-scaled
    assert catalyst_class_reliability_multiplier("none") == 1.0
    assert catalyst_class_reliability_multiplier("other") == 1.0


def test_merger_delta_byte_identical_until_trained():
    # A merger headline's grade is byte-identical to the raw completed-action grade because
    # the reliability multiplier is exactly 1.0 (no samples yet).
    tilt = _catalyst_tilt()
    d = action_dollar_grade_delta("Acme signed a merger agreement")  # completed, no dollar
    assert d == round(tilt * _ACTION_COMPLETENESS_TILT_FRAC, 6)
    assert d > 0


def test_class_reliability_flag_off_is_one():
    orig = settings.chili_momentum_catalyst_class_reliability_enabled
    try:
        settings.chili_momentum_catalyst_class_reliability_enabled = False
        assert catalyst_class_reliability_multiplier("merger") == 1.0
    finally:
        settings.chili_momentum_catalyst_class_reliability_enabled = orig
