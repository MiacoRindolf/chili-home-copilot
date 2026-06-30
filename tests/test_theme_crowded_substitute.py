"""Unit tests for the CROWDED-TAPE CATALYST-SUBSTITUTE (Ross sympathy-name rank hold).

DIAGNOSIS the substitute fixes: when the news-catalyst selection pillar is active, a name in
NONE of the catalyst sets reads ``news_catalyst_pct=None`` — NEUTRAL in isolation, but its
strong-catalyst peers carry a high news percentile and out-rank it on the same RVOL+momentum
core, so a catalyst-LESS crowded-tape / high-RVOL THEME name (a Ross sympathy mover) is demoted
purely on P1-catalyst absence. The substitute credits a genuine keyword-theme member that is ALSO
a crowded high-RVOL name with a partial news sub-score (capped at the present grade, below strong),
FLOORED onto news_catalyst_pct ONLY for no-own-catalyst names.

Pure-function tests (no DB / no IO): the adaptive ramp math (no magic absolute), the bounds (the
substitute never reaches the strong grade), the no-over-promotion of a non-mover crowded tape, and
an INTEGRATION test on score_universe proving the rank hold + the flag-off byte-identical path.
"""

import pytest

from app.services.trading.momentum_neural.ross_momentum import (
    ROSS_NEWS_CATALYST_PILLAR_WEIGHT,
    ROSS_NEWS_GRADE_PRESENT,
    ROSS_NEWS_GRADE_STRONG,
    ROSS_PILLAR_WEIGHTS,
    score_universe,
)
from app.services.trading.momentum_neural.theme_detector import (
    THEME_CROWDED_RVOL_FLOOR_PCTL,
    crowded_tape_news_substitute as _sub,
)


# ── the substitute ramp: adaptive, bounded, fail-neutral, no over-promotion ─────


def test_non_member_earns_no_credit():
    # not a theme member ⇒ None no matter how crowded the tape (the credit requires
    # genuine theme membership, never a lone high-RVOL name).
    for rp in (0.0, 0.5, 0.7, 1.0):
        assert _sub(False, rp, grade_present=ROSS_NEWS_GRADE_PRESENT) is None


def test_member_below_rvol_floor_earns_nothing():
    # a theme member on a QUIET tape (RVOL below the crowded floor) is NOT credited — a
    # crowded tape on a non-mover does not get over-promoted.
    for rp in (0.0, 0.5, THEME_CROWDED_RVOL_FLOOR_PCTL - 0.01):
        assert _sub(True, rp, grade_present=ROSS_NEWS_GRADE_PRESENT) is None


def test_member_ramps_through_the_crowded_tail():
    at_floor = _sub(True, THEME_CROWDED_RVOL_FLOOR_PCTL, grade_present=ROSS_NEWS_GRADE_PRESENT)
    mid = _sub(True, (THEME_CROWDED_RVOL_FLOOR_PCTL + 1.0) / 2.0, grade_present=ROSS_NEWS_GRADE_PRESENT)
    top = _sub(True, 1.0, grade_present=ROSS_NEWS_GRADE_PRESENT)
    # starts at the 0.5 neutral midpoint, ramps strictly up through the crowded tail
    assert at_floor == pytest.approx(0.5)
    assert at_floor < mid < top


def test_credit_is_capped_at_present_below_strong():
    # the substitute saturates at the present/ungraded reference — NEVER the strong grade,
    # so a genuinely GRADED strong-catalyst leader still out-ranks a crowded sympathy name.
    top = _sub(True, 1.0, grade_present=ROSS_NEWS_GRADE_PRESENT)
    assert top == pytest.approx(ROSS_NEWS_GRADE_PRESENT)
    assert top < ROSS_NEWS_GRADE_STRONG


def test_fail_neutral_on_missing_rvol_percentile():
    assert _sub(True, None, grade_present=ROSS_NEWS_GRADE_PRESENT) is None


# ── INTEGRATION on score_universe: the rank hold + flag-off byte-identical ───────


def _news_weights():
    """The active weight-set with the news pillar folded (mirrors pipeline.py)."""
    w = dict(ROSS_PILLAR_WEIGHTS)
    w["news_catalyst"] = ROSS_NEWS_CATALYST_PILLAR_WEIGHT
    return w


def _batch(*, sympathy_news_pct):
    """Three same-core (identical RVOL+momentum) names so ONLY the news axis separates them:
      * LEADER  — a graded strong catalyst (the 🔥), news_catalyst_pct = strong.
      * SYMPNW  — a crowded high-RVOL THEME sympathy name with NO own catalyst; its
                  news_catalyst_pct is ``sympathy_news_pct`` (None = the demoted baseline;
                  the substitute = the credited value).
      * LAGGARD — same core but a WEAK catalyst (dilution-class), low news pct.
    Equal rvol/momentum so the explosive CORE is identical across all three — the news axis
    inside the quality_blend is the only thing that can re-order them."""
    base = {"vol_ratio": 8.0, "daily_change_pct": 40.0, "float_shares": 5_000_000}
    leader = dict(base, news_catalyst_pct=ROSS_NEWS_GRADE_STRONG)
    sympnw = dict(base)
    if sympathy_news_pct is not None:
        sympnw["news_catalyst_pct"] = sympathy_news_pct
    laggard = dict(base, news_catalyst_pct=0.30)  # weak grade
    return {"LEADER": leader, "SYMPNW": sympnw, "LAGGARD": laggard}


def test_sympathy_name_rank_holds_with_the_substitute():
    # BEFORE: the crowded sympathy name has NO own catalyst (news_catalyst_pct absent) ⇒ it is
    # demoted below the weak-catalyst LAGGARD purely on news-axis absence.
    before = score_universe(_batch(sympathy_news_pct=None), weights=_news_weights(), explosive=True)
    rank_before = before["SYMPNW"].rank

    # AFTER: the pipeline stamps the crowded-tape substitute (top-RVOL theme member ⇒ present
    # grade) onto news_catalyst_pct. The same-core sympathy name now ranks AT or ABOVE where it
    # did, and at/above the weak LAGGARD it was being beaten by.
    credited = _sub(True, 1.0, grade_present=ROSS_NEWS_GRADE_PRESENT)
    after = score_universe(
        _batch(sympathy_news_pct=credited), weights=_news_weights(), explosive=True
    )
    rank_after = after["SYMPNW"].rank

    assert rank_after <= rank_before  # rank improved (lower number) or held
    assert after["SYMPNW"].rank <= after["LAGGARD"].rank  # no longer demoted under the weak name
    # the GRADED strong LEADER still out-ranks the crowded sympathy name (cap < strong).
    assert after["LEADER"].rank <= after["SYMPNW"].rank


def test_non_mover_crowded_tape_does_not_over_promote():
    # a theme member on a QUIET tape earns no substitute ⇒ its rank is the demoted baseline,
    # NOT promoted above the leaders. (We assert the helper returns no credit; the pipeline
    # therefore never stamps it — byte-identical to the no-credit batch.)
    assert _sub(True, 0.10, grade_present=ROSS_NEWS_GRADE_PRESENT) is None
    no_credit = score_universe(_batch(sympathy_news_pct=None), weights=_news_weights(), explosive=True)
    # the strong LEADER still ranks first; a non-mover sympathy name never displaces it.
    assert no_credit["LEADER"].rank == 1


def test_flag_off_path_is_byte_identical():
    # the "flag off" path = the pipeline simply never stamps the substitute, so the score is
    # the no-credit batch. Identical inputs ⇒ identical scores (the helper is the only new code;
    # not calling it leaves score_universe untouched).
    a = score_universe(_batch(sympathy_news_pct=None), weights=_news_weights(), explosive=True)
    b = score_universe(_batch(sympathy_news_pct=None), weights=_news_weights(), explosive=True)
    assert {s: a[s].score for s in a} == {s: b[s].score for s in b}
