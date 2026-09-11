"""[1] The two INVERTED dip gates now REPORT instead of refusing.

WHAT THIS PROTECTS. The micro-pullback re-load is the operator's buy-the-dip doctrine in
code ("ibenta ang spike, bumalik sa pullback"). It has filled ZERO times, ever. Two gates
held it shut, and both were measured inverted against our own tape:

    dip_pct > 0.04 -> dip_too_deep          entry_gates.micro_pullback_reentry_detect
      dip depth AT THE ONSET of a clean run is DEEPER than at random controls at every
      quantile: onset p50 0.0210 / p90 0.0425 vs ctrl p50 0.0118 / p90 0.0260, pooled
      AUC 0.733 / clustered AUC 0.710 (37 symbol-day clusters, 832 onset / 15,916 ctrl).
      The 0.04 cap refuses 12.3% of onsets vs 2.2% of controls -- 5.6x ANTI-selective,
      and NO cap level is selective (0.02: 52.9/20.2 ... 0.10: 0.4/0.0).

    ofi >= 0.30 AND trade_flow >= 0.20      live_runner re-load block
      OFI at onset is LOWER than at controls: onset p50 -0.2226 vs ctrl +0.0047, pooled
      AUC 0.370 / clustered AUC 0.400 (51 clusters, 956 onset / 16,524 ctrl). The +0.30
      floor refuses 82.0% of onsets vs 74.9% of controls; every floor tried is
      anti-selective. All-time `reason=flow` blocks = 18 with veto=true in ZERO of them
      -- the knife never fired, the positive-confirm was the entire blocker.

So depth became evidence on the receipt and the positive-confirm became a PRINT PROOF --
the [59] form. The structural knives are untouched: the SHELF still refuses, and
`_entry_flow_veto` stays as the NAMED knife.

2026-09-10 REVIEW FIXES, also covered here:
  * The proof compares PRINT to PRINT. `bounce_high` is a quote-MID level (the micro frame
    buckets NBBO midpoints), so the reference is now the break BAR's own high PRINT with the
    mid as a NAMED fallback on the receipt, and the evidence is the highest print SINCE that
    bar -- not the side of one tick, and not the whole window's high (which contains the
    break itself).
  * The ladder is the pure `entry_gates.micro_pullback_reload_proof`, so the branch ORDER is
    executed by these tests rather than transcribed into a copy beside them.
  * An UNKNOWN print age counts as stale (fail-closed), at both tape call sites.
  * The pullback-add tape read is age-bounded: the print-count query has no lower time bound,
    so on a 15-minute-delayed name it handed a falling-knife guard 900-second-old prints.
  * In print mode the halt-gap restriction is half the SPAN READ, not 7.5 wall-clock seconds.
  * `micro_pullback_primary*` joined STRUCTURAL_TRIGGER_REASONS, so the dip low survives as
    the placed stop instead of being popped in favour of a depth-blind ATR stop.

The remaining live_runner assertions are source-level, and that limit is deliberate: the
branch sits ~48,300 lines into `tick_live_session` behind a live position in
STATE_LIVE_TRAILING, so a unit test cannot reach it without simulating a whole session.
Everything that DECIDES is now a pure function and is executed directly.

Runnable: pytest tests/test_dip_gates_report_not_refuse.py -v
"""
from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

from app.config import Settings
from app.services.trading.momentum_neural.entry_gates import (
    _MICROPULLBACK_DIP_ONSET_QUANTILES,
    _dip_onset_percentile,
    _signed_tape_features,
    high_print_in_window,
    micro_pullback_reentry_detect,
    micro_pullback_reload_proof,
    tape_print_age_bound_s,
    tape_print_age_s,
)
from app.services.trading.momentum_neural.live_runner import (
    structural_trigger_reasons,
)

_ROOT = Path(__file__).resolve().parents[1]
_LR = _ROOT / "app/services/trading/momentum_neural/live_runner.py"
_EG = _ROOT / "app/services/trading/momentum_neural/entry_gates.py"


@pytest.fixture(scope="module")
def lr_src() -> str:
    return _LR.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def eg_src() -> str:
    return _EG.read_text(encoding="utf-8", errors="replace")


def _code_only(src: str) -> str:
    """Source with whole-line comments removed. Several assertions below are of the form
    "this refusal is no longer in the code", and the explanation of WHY it went quotes the
    refusal verbatim -- so the comment would keep the test red forever."""
    return "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )


@pytest.fixture(scope="module")
def lr_code(lr_src: str) -> str:
    return _code_only(lr_src)


def _reload_block(src: str) -> str:
    """The re-load decision block: from the detected-receipt emit to the admission gate.
    Anchored on both ends so it cannot silently stop covering the ladder as the block
    grows."""
    i = src.find('_emit(db, sess, "live_micro_pullback_detected"')
    assert i > 0, "the re-load block's detected receipt moved or vanished"
    j = src.find("runner_boundary_risk_ok", i)
    assert j > i, "the re-load block's admission gate moved or vanished"
    return src[i:j]


# --------------------------------------------------------------------------- helpers
def _df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def _squeeze_then_dip(*, base: float = 10.0, step: float = 0.50, dip: float) -> pd.DataFrame:
    """Rising squeeze, then a pullback of ``dip`` dollars off the local high, then a green
    curl that holds the dip low. >= 10 bars so the sparse-frame fail-safe does not trip.

    ``step`` is deliberately steep (0.50): a deep dip on a GENTLE squeeze is still refused,
    by ``ema_not_rising`` — the structure guards keep bounding depth indirectly, which is
    the point. Only the free-standing PERCENTAGE cap went away."""
    rows: list[tuple[float, float, float, float]] = []
    px = base
    for _ in range(8):
        o, c = px, px + step
        rows.append((o, c + 0.02, o - 0.01, c))
        px = c
    bounce_high = px
    dip_low = bounce_high - dip
    rows.append((bounce_high, bounce_high + 0.01, dip_low, dip_low + 0.03))
    curl_close = dip_low + 0.12
    rows.append((dip_low + 0.02, curl_close + 0.01, dip_low, curl_close))
    return _df(rows)


# ===================================================== A. DEPTH IS REPORTED, NOT REFUSED
def test_a_deep_dip_that_holds_the_shelf_now_fires():
    """THE POINT. A ~10% dip -- 2.5x the old 0.04 cap, and above the 95th percentile of
    real onsets -- FIRES as long as the shelf holds. Under the old gate this was
    `dip_too_deep`, refusing the deepest 12.3% of genuine move onsets."""
    df = _squeeze_then_dip(dip=1.08)          # ~7.8% of the bounce-high
    out = micro_pullback_reentry_detect(df, shelf=9.0, max_dip_pct=0.04)
    assert out["fire"] is True, out
    assert out["reason"] == "micro_pullback_curl"
    assert out["dip_pct"] > 0.04, "the test frame must actually exceed the old cap"


def test_the_detector_no_longer_has_a_dip_too_deep_verdict(eg_src: str):
    """The verdict itself must be gone -- not merely unreachable. (The name still appears
    in the derivation comment that records WHY it went; the assignment may not.)"""
    assert 'out["reason"] = "dip_too_deep"' not in eg_src, (
        "the 5.6x anti-selective depth cap is back in the detector"
    )
    assert "dip_pct > float(max_dip_pct)" not in eg_src, (
        "the depth comparison is back -- max_dip_pct may only feed the receipt"
    )


def test_depth_is_reported_with_its_onset_percentile_and_the_old_bound():
    """Opening a gate without recording what it would have refused throws away the only
    evidence that could ever justify closing it again."""
    df = _squeeze_then_dip(dip=1.08)
    out = micro_pullback_reentry_detect(df, shelf=9.0, max_dip_pct=0.04)
    assert out["dip_pct"] is not None
    assert out["would_have_blocked_at"] == 0.04
    assert out["would_have_blocked"] is True          # the named fallback says so
    pctl = out["dip_pct_onset_pctl"]
    assert pctl is not None and 0.0 <= pctl <= 1.0
    assert pctl > 0.90, (
        "a ~7.8% dip is above the 95th percentile of measured onsets -- report it as such"
    )


def test_a_shallow_dip_reports_would_have_blocked_false():
    """The receipt must be readable in BOTH directions, otherwise it is an alibi."""
    df = _squeeze_then_dip(dip=0.20)
    out = micro_pullback_reentry_detect(df, shelf=9.0, max_dip_pct=0.04)
    assert out["fire"] is True
    assert out["would_have_blocked"] is False
    assert out["dip_pct"] < 0.04


def test_depth_is_reported_even_when_another_guard_refuses():
    """A shelf rejection still has to say how deep the dip was -- otherwise the only
    question you can answer about a refusal is 'was it outside', never 'by how much'."""
    df = _squeeze_then_dip(dip=0.20)
    dip_low = float(df["low"].iloc[-1])
    out = micro_pullback_reentry_detect(df, shelf=dip_low + 0.05, max_dip_pct=0.04)
    assert out["fire"] is False
    assert out["reason"] == "dip_below_shelf"
    assert out["dip_pct"] is not None
    assert out["dip_pct_onset_pctl"] is not None


# ===================================================== B. THE STRUCTURAL KNIVES SURVIVE
def test_the_shelf_is_still_the_knife_on_depth():
    """Depth stopped being a knife; the RATCHETING SHELF did not. A dip below the shelf is
    not a higher low, and that is structural, not a percentage."""
    df = _squeeze_then_dip(dip=1.08)
    dip_low = float(df["low"].iloc[-1])
    out = micro_pullback_reentry_detect(df, shelf=dip_low + 0.01, max_dip_pct=0.50)
    assert out["fire"] is False
    assert out["reason"] == "dip_below_shelf"


@pytest.mark.parametrize(
    "reason",
    ["ema_not_rising", "no_dip_after_high", "last_bar_undercut_dip", "frame_too_sparse"],
)
def test_the_other_non_depth_guards_are_still_in_the_detector(eg_src: str, reason: str):
    """Removing one gate must not have removed the others."""
    assert f'"{reason}"' in eg_src


def test_falling_stack_still_refuses():
    rows = [(20.0 - i * 0.2, 20.1 - i * 0.2, 19.8 - i * 0.2, 19.9 - i * 0.2) for i in range(12)]
    out = micro_pullback_reentry_detect(_df(rows), shelf=0.0, max_dip_pct=0.50)
    assert out["fire"] is False
    assert out["reason"] == "ema_not_rising"


def test_sparse_frame_still_fails_safe():
    assert micro_pullback_reentry_detect(None, shelf=9.0, max_dip_pct=0.5)["fire"] is False
    assert micro_pullback_reentry_detect(_df([]), shelf=9.0, max_dip_pct=0.5)["fire"] is False


# ===================================================== C. THE ONSET QUANTILE TABLE
def test_the_onset_table_is_the_measured_distribution():
    """The table IS the onset column of the derivation (832 onsets, 2026-09-09) -- pinned
    so a later 'tidy-up' cannot silently refit it into a threshold."""
    assert _MICROPULLBACK_DIP_ONSET_QUANTILES[0] == (0.05, 0.00609)
    assert (0.50, 0.02099) in _MICROPULLBACK_DIP_ONSET_QUANTILES
    assert (0.90, 0.04252) in _MICROPULLBACK_DIP_ONSET_QUANTILES
    assert _MICROPULLBACK_DIP_ONSET_QUANTILES[-1] == (0.99, 0.07867)
    xs = [x for _, x in _MICROPULLBACK_DIP_ONSET_QUANTILES]
    assert xs == sorted(xs), "quantiles must be monotone"


def test_the_old_cap_sits_just_under_the_onset_p90():
    """The refutation in one line: the bound that refused 'too deep' is BELOW the 90th
    percentile of dips at genuine move onsets, so it was cutting real onsets."""
    p90 = dict(_MICROPULLBACK_DIP_ONSET_QUANTILES)[0.90]
    assert 0.04 < p90, f"0.04 vs measured onset p90 {p90}"


def test_percentile_helper_is_monotone_and_clamped():
    assert _dip_onset_percentile(0.0) == 0.0
    assert _dip_onset_percentile(1.0) == 1.0
    assert _dip_onset_percentile(None) is None
    mid = _dip_onset_percentile(0.02099)
    assert mid == pytest.approx(0.50, abs=1e-6)
    seq = [_dip_onset_percentile(v) for v in (0.005, 0.01, 0.02, 0.03, 0.05, 0.09)]
    assert seq == sorted(seq)


# ===================================================== D. THE PRIMARY ENTRY CARRIES IT TOO
def test_the_primary_entry_shares_the_detector_and_reports_depth(eg_src: str):
    """`micro_pullback_primary_confirmation` reuses this detector, so the cap was refusing
    the PRIMARY entry too (an open path, flag default True). Its debug must now carry the
    depth fields -- otherwise the change is invisible on the path that can actually fire."""
    i = eg_src.find("def micro_pullback_primary_confirmation")
    assert i > 0
    body = eg_src[i:i + 12000]
    assert "micro_pullback_reentry_detect(" in body
    for field in ("dip_pct", "dip_pct_onset_pctl",
                  "dip_would_have_blocked_at", "dip_would_have_blocked"):
        assert f'debug["{field}"]' in body, f"{field} missing from the primary debug"
    assert 'debug["dip_depth_policy"] = "reported_not_enforced"' in body


# ===================================================== E. THE RE-LOAD PROOF IS THE TAPE
def test_the_positive_confirm_is_gone(lr_src: str):
    """18 all-time flow blocks, veto=true in zero of them: this expression was 100% of the
    blocker, standing on two anti-selective floors."""
    assert "_pos_confirm" not in lr_src, (
        "the ofi>=0.30 AND tf>=0.20 positive-confirm is back"
    )
    assert '"reason": "flow",' not in lr_src, (
        "the unnamed `reason=flow` refusal is back -- it conflated the knife with the floor"
    )


def test_the_reload_proof_reads_the_tape_and_compares_print_to_print(lr_code: str):
    """The replacement: the tape must have PRINTED above the micro-break since the dip,
    with the signed push still running. Both sides of the comparison are prints -- the
    reference is the break BAR's high print, not the quote-mid `bounce_high` the detector
    reports (a level nobody necessarily traded at)."""
    blk = _reload_block(lr_code)
    assert "signed_tape_accel_features as _mpr_tape_fn" in blk
    assert "window_prints=_mpr_win_prints" in blk
    assert "high_print_in_window as _mpr_hp_fn" in blk, (
        "the micro-break reference must be read from the TAPE, not taken from a mid"
    )
    assert "micro_pullback_reload_proof as _mpr_ladder_fn" in blk
    assert "reclaim_high_px=_mpr_reclaim_high" in blk
    assert "break_ref_px=_mpr_break_ref" in blk
    # the transcribed inline chain must not come back
    assert "_mpr_last_print > _mpr_bounce_high" not in blk


def test_the_reload_blocks_are_named(lr_code: str):
    """One name per mechanism, so a receipt can be read. `flow` named four different
    things; these name one each -- and the names live on the pure function, so the runner
    and the tests cannot drift apart."""
    from app.services.trading.momentum_neural.entry_gates import (
        MICRO_PULLBACK_RELOAD_VERDICTS,
    )

    blk = _reload_block(lr_code)
    assert "live_micro_pullback_reentry_proof" in blk, "a PASS must leave a receipt too"
    for reason in ("flow_veto", "tape_unreadable", "break_reference_unreadable",
                   "reclaim_wait", "tape_not_confirming", "proof"):
        assert reason in MICRO_PULLBACK_RELOAD_VERDICTS
    # every verdict but "proof" is emitted verbatim as the receipt's reason
    assert '"reason": _mpr_block' in blk


def test_the_named_knife_survives(lr_code: str):
    """`_entry_flow_veto` (never buy into selling, the 06-24 fix) is the one thing in the
    old flow gate that was a knife. It stays, it is the FIRST rung of the ladder, and it
    has its own name."""
    blk = _reload_block(lr_code)
    assert "_entry_flow_veto(_mpr_ofi, _mpr_tf, settings)" in blk
    assert "veto=bool(_veto)" in blk
    # executable: the knife outranks a complete, fresh, clearing proof
    assert micro_pullback_reload_proof(
        veto=True, last_print=10.5, signed_tape_accel=900.0, tape_stale=False,
        break_ref_px=10.0, reclaim_high_px=10.5,
    ) == "flow_veto"


def test_ofi_and_trade_flow_are_still_reported(lr_code: str):
    """Demoted to evidence, not deleted: the value that used to decide still lands on the
    receipt so the decision stays auditable against the derivation."""
    blk = _reload_block(lr_code)
    assert '"ofi": _mpr_ofi' in blk
    assert '"trade_flow": _mpr_tf' in blk
    assert '"ofi_policy": "reported_not_enforced"' in blk
    assert '"trade_flow_policy": "reported_not_enforced"' in blk


def test_unreadable_or_stale_tape_waits(lr_code: str):
    """Fail-CLOSED for an extra BUY, the [59] contract. The AGE bound is load-bearing:
    37 names have no real-time NYSE entitlement -- TPET arrives p50 900.23 s behind
    (n=21,560, 2026-09-10 13:20-14:00Z) against SKYQ p50 0.068 s."""
    blk = _reload_block(lr_code)
    assert "chili_momentum_g4_reentry_max_print_age_seconds" in blk, (
        "a 15-minute-old print is not a reclaim proof"
    )
    assert "tape_print_age_s as _tape_age_fn" in blk
    assert "tape_print_age_bound_s as _tape_age_bound_fn" in blk
    assert "_mpr_stale is True" not in blk, (
        "an UNKNOWN print age must not walk through as if it were fresh"
    )
    # executable: every unreadable shape WAITS, including an un-aged print
    for kw in ({"last_print": None}, {"signed_tape_accel": None},
               {"tape_stale": True}, {"tape_stale": None}, {"last_print": 0.0}):
        base = dict(veto=False, last_print=10.5, signed_tape_accel=900.0,
                    tape_stale=False, break_ref_px=10.0, reclaim_high_px=10.5)
        base.update(kw)
        assert micro_pullback_reload_proof(**base) == "tape_unreadable", kw


def test_the_proof_reuses_derived_values_and_adds_no_new_knob(lr_code: str):
    """No magic numbers: both the window and the age bound are values already derived and
    documented for [58]/[59]. No new Settings field is introduced by this path."""
    blk = _reload_block(lr_code)
    assert "chili_momentum_g4_reentry_tape_window_prints" in blk
    s = Settings()
    assert s.chili_momentum_g4_reentry_tape_window_prints == 255
    assert s.chili_momentum_g4_reentry_max_print_age_seconds == pytest.approx(14.69)


def test_the_receipt_carries_the_binding_block(lr_code: str):
    blk = _reload_block(lr_code)
    assert '"binding": _mpr_binding' in blk
    for k in ("tape_window_prints", "print_age_s", "print_age_bound_s",
              "dip_pct", "dip_pct_onset_pctl", "dip_would_have_blocked_at"):
        assert f'"{k}"' in blk, f"binding value {k} missing from the receipt"


# ===================================================== F. CONFIG SAYS WHAT IT DOES
@pytest.mark.parametrize(
    "field",
    [
        "chili_momentum_micropullback_reentry_ofi_thr",
        "chili_momentum_micropullback_reentry_trade_flow_thr",
        "chili_momentum_micropullback_reentry_max_dip_pct",
    ],
)
def test_the_config_descriptions_say_reported_not_enforced(field: str):
    """A knob whose description still claims it gates is a lie that outlives the change."""
    desc = Settings.model_fields[field].description or ""
    assert "REPORTED" in desc, f"{field} description does not say it is reported"
    assert "NOT enforced" in desc or "not enforced" in desc
    assert "2026-09-10" in desc, "the description must date the measurement"


def test_defaults_are_untouched_no_dark_flag():
    """The change is in behaviour, not in a new off-by-default switch. The old values stay
    exactly where they were so they remain readable as the named fallback."""
    s = Settings()
    assert s.chili_momentum_micropullback_reentry_ofi_thr == 0.30
    assert s.chili_momentum_micropullback_reentry_trade_flow_thr == 0.20
    assert s.chili_momentum_micropullback_reentry_max_dip_pct == 0.04
    assert s.chili_momentum_micropullback_reentry_enabled is True
    assert s.chili_momentum_micro_pullback_primary_enabled is True


# ===================================================== G. THE LIVE PIN (4 of 18 instants)
#
# ⚠️ COVERAGE, STATED HONESTLY ([1] review fix). The live book holds EIGHTEEN
# `live_micro_pullback_detected` rows, not four: RKTO 12050 x8 (2026-07-09 13:42-13:43),
# JZXN 12663 x6 (2026-07-10 13:46-14:19), SUNE 20774 x1, SKYQ 21591 x3. Only 4 of 18
# (22 %, two symbol-days) can be replayed at all -- `iqfeed_trade_ticks` holds 0 rows for
# RKTO 2026-07-09 13:30-13:50 and 0 for JZXN 2026-07-10 13:40-14:25, so there is no tape
# to run any print proof against. The 14 omitted rows all carry dip_pct 2.2-3.3 % (from
# their bounce_high/dip_low payloads), i.e. UNDER the old 0.04 cap, so depth is not what
# refused them either.
_ALL_TIME_DETECTIONS = 18
_REPLAYABLE_DETECTIONS = 4

# (utc, break_ref_px = the break BAR's high PRINT, reclaim_high_px = the highest print
#  SINCE that bar ended, signed_tape_accel, last_print). Measured read-only on live
#  `chili`; the break bar is the 10 s bucket whose max NBBO mid equals the recorded
#  bounce_high, which reproduces the payload exactly in all four cases.
_SKYQ_21591_INSTANTS = [
    ("2026-09-10 13:52:17.602228", 3.72, 3.69, 5133.0, 3.67),
    ("2026-09-10 13:52:22.876284", 3.72, 3.69, -4412.0, 3.6597),
    ("2026-09-10 13:52:30.705169", 3.72, 3.71, -1120.0, 3.6811),
]
_SKYQ_21591_BOUNCE_HIGH = 3.715          # the QUOTE-MID level the detector reported


@pytest.mark.parametrize("utc,break_ref,reclaim_high,accel,last_print", _SKYQ_21591_INSTANTS)
def test_skyq_21591_would_be_reclaim_wait(utc, break_ref, reclaim_high, accel, last_print):
    """EXECUTABLE PIN of the real instants, against the PRODUCTION ladder.

    All three SKYQ 21591 detections (2026-09-10 13:52) reported a quote-mid bounce_high of
    3.715. The break BAR [13:52:00, 13:52:10) printed a high of 3.72 (1,028 prints) -- the
    price someone actually paid at the micro-break -- and the highest print SINCE that bar
    was 3.69 / 3.69 / 3.71. So the tape had not taken the break out: `reclaim_wait`, with
    both numbers on the receipt.

    ⚠️ This replaces the first version's claim that the window's own `window_high_px`
    (3.68 / 3.6799 / 3.71) showed the break was never reached. `window_high_px` spans the
    whole 255-print window, which CONTAINS the break itself, so it is neither evidence for
    nor against a reclaim -- measured on SUNE it IS the break (see below)."""
    assert micro_pullback_reload_proof(
        veto=False, last_print=last_print, signed_tape_accel=accel, tape_stale=False,
        break_ref_px=break_ref, reclaim_high_px=reclaim_high,
    ) == "reclaim_wait"
    assert break_ref > _SKYQ_21591_BOUNCE_HIGH, (
        "the break bar's high PRINT sits above the quote-mid bounce_high (half a spread) "
        "-- which is exactly why the two bases must not be compared to each other"
    )


def test_sune_20774_window_high_is_the_break_itself_not_a_reclaim():
    """The fourth and only other replayable detection: SUNE 20774, 2026-09-09 09:31:14,
    bounce_high (mid) 3.01, newest tick 3.00, accel +14,447, window high print 3.02.

    ⚠️ THE CORRECTION. It is tempting to read `window_high 3.02 > bounce_high 3.01` as "the
    tape DID pay up through the micro-break, and only the side of one bid-side tick refused
    it". Measured, it is the opposite: the 3.02 print is inside the BREAK BAR
    [09:31:00, 09:31:10) (119 prints) -- it is the break, made BEFORE the dip. The highest
    print since that bar ended is 3.00. So the honest verdict is WAIT, and it is decided by
    the window's high print since the break, not by one tick."""
    assert micro_pullback_reload_proof(
        veto=False, last_print=3.00, signed_tape_accel=14447.0, tape_stale=False,
        break_ref_px=3.02, reclaim_high_px=3.00,
    ) == "reclaim_wait"


def test_the_ladder_lets_a_real_reclaim_through():
    """The negative control for the pins above: clear the break with the push running and
    the proof passes -- the ladder is not a disguised permanent refusal.

    MEASURED, not asserted: at every one of the four replayable instants the tape printed
    above `break_ref_px` within seconds of the detection -- SUNE 3.0205 at 09:31:39
    (+25.0 s), SKYQ 3.7277 at 13:52:33 (+15.5 / +10.2 / +2.4 s). This is the shape the
    verdict produces: WAIT, then buy the break."""
    def _d(**kw):
        base = dict(veto=False, last_print=3.7277, signed_tape_accel=5133.0,
                    tape_stale=False, break_ref_px=3.72, reclaim_high_px=3.7277)
        base.update(kw)
        return micro_pullback_reload_proof(**base)

    assert _d() == "proof"
    assert _d(signed_tape_accel=-1.0) == "tape_not_confirming"
    assert _d(tape_stale=True) == "tape_unreadable"
    assert _d(tape_stale=None) == "tape_unreadable"
    assert _d(veto=True) == "flow_veto"
    assert _d(break_ref_px=None) == "break_reference_unreadable"


def test_the_replayable_coverage_is_stated_not_implied():
    """[1] review fix. The first version of the PR body and doc SS14.3 presented the four
    replayed instants as "every live_micro_pullback_detected instant that has ever
    existed". It is 4 of 18. The doc must now say so -- a reader who believes the
    validation covers 100 % of history cannot weigh it correctly."""
    doc = (_ROOT / "docs/DESIGN/MOMENTUM_LANE.md").read_text(
        encoding="utf-8", errors="replace")
    assert str(_ALL_TIME_DETECTIONS) in doc and "RKTO" in doc and "JZXN" in doc, (
        "the doc must name the 18 all-time detections and the two unreplayable sessions"
    )
    assert "0 rows" in doc, "the doc must say WHY the other 14 cannot be replayed"
    assert str(_REPLAYABLE_DETECTIONS) in doc


# ===================================================== H. THE SIDE FIXES ON THIS PATH
def test_the_pullback_add_receipt_names_its_basis(lr_code: str):
    """`live_pullback_add_vetoed` dropped front_side_basis / buy_share_delta /
    high_print_position, so a `weak_front_side` row could not be told apart from a tape
    that was unreadable at decision time. TPET (all 6 such rows on 2026-09-10) arrives
    `available_at - observed_at` p50 900.44 s -- exactly 15 minutes -- so at the decision
    instant its tape WAS empty while SKYQ/SUNE arrived in 0.27 s."""
    i = lr_code.find('_emit(db, sess, "live_pullback_add_vetoed", {')
    assert i > 0
    blk = lr_code[i:i + 6000]
    for k in ("front_side_basis", "buy_share_delta", "high_print_position",
              "tape_unreadable"):
        assert f'"{k}"' in blk, f"{k} still missing from the veto receipt"


def test_the_pullback_add_tape_window_is_print_indexed(lr_code: str):
    """Same doctrine as [58]/[59]: the window must not be a clock. Measured at the six TPET
    weak_front_side instants, buy_share_delta comes out with the OPPOSITE SIGN in the 15-s
    window vs the 255-print window at three of six -- and buy_share_delta > 0 IS the gate."""
    i = lr_code.find("signed_tape_accel_features as _pba_feat_fn")
    assert i > 0
    blk = lr_code[i:i + 2000]
    assert "window_prints=_pba_win_prints" in blk


def test_the_pullback_add_tape_vars_are_bound_before_the_try(lr_src: str):
    """Latent NameError fixed in passing: _pba_bsd / _pba_hpp were bound INSIDE the try,
    after the ross_momentum import and the OFI read, while the `except` reset only the
    other three names -- so an early failure left them unbound and the next statement
    passed them to pullback_add_decision."""
    init = lr_src.find("_pba_bsd = None\n                        _pba_hpp = None")
    assert init > 0, "the tape vars must be initialised at the pre-try indent level"
    use = lr_src.find("buy_share_delta=_pba_bsd,")
    assert use > init


def test_the_pullback_add_tape_read_is_age_bounded(lr_code: str):
    """⚠️ BLOCKING, [1] review fix. The print-count query has NO lower time bound at all
    (`WHERE symbol = :s AND observed_at <= :as_of ORDER BY observed_at DESC LIMIT :n`),
    while the seconds query has `observed_at > :as_of - make_interval(...)`. So switching
    this falling-knife guard to a count window did NOT preserve its fail-closed behaviour
    on a delayed name -- it reached back PAST the 15-minute delay and handed the guard a
    900-second-old tape.

    MEASURED on live `chili` at the six TPET 21589 `weak_front_side` instants this change
    cites, counting only rows VISIBLE at the decision instant (`received_at <= T`):

        15-s window       0 prints at all six  -> features None -> basis "score" -> refuse
        255-print window  255 prints at all six, newest age 900.1 / 900.2 / 900.2 /
                          901.0 / 903.6 / 900.5 s
        high_print_position on that stale tape: 0.906 / 1.000 / 0.972 / 0.996 / 0.992 /
                          0.972 -- every one at or above the 0.75 `spent_position`
                          quartile, so the window change ALONE would have converted a
                          fail-closed `weak_front_side` into `dip_into_a_spent_move`,
                          decided on 15-minute-old prints.

    (TPET `received_at - observed_at` 2026-09-10 13:20-14:00Z: n=21,560, p50 900.23 s,
    min 899.95, max 900.73; SKYQ on the same tape p50 0.068 s.)"""
    i = lr_code.find("signed_tape_accel_features as _pba_feat_fn")
    assert i > 0
    blk = lr_code[i:i + 6000]
    assert "window_prints=_pba_win_prints" in blk
    assert "tape_print_age_s as _pba_age_fn" in blk, (
        "the pullback-add tape read must measure the newest print's AGE"
    )
    assert "tape_print_age_bound_s as _pba_age_bound_fn" in blk
    assert "_pba_stale is not False" in blk, (
        "stale OR unknown-age tape must drop the tape proof (fail-closed), the same rule "
        "the re-load ladder applies"
    )
    assert "_pba_bsd = _pba_hpp = None" in blk


def test_the_pullback_add_receipt_derives_unreadable_from_the_age(lr_code: str):
    """[1] review fix. `tape_unreadable` was `front_side_basis == "score"` -- a proxy that
    the print-window switch in the SAME change invalidated: with a count-bounded window a
    delayed name's read is no longer empty, so the basis becomes "tape" and the flag would
    have emitted FALSE at exactly the six instants offered as proof that the tape was
    unreadable. The receipt must carry the value that DECIDED (the print age)."""
    i = lr_code.find('_emit(db, sess, "live_pullback_add_vetoed", {')
    assert i > 0
    blk = lr_code[i:i + 6000]
    assert '"tape_unreadable": bool(_pba_stale is not False)' in blk, (
        "tape_unreadable must be derived from the measured print age, not from the basis"
    )
    assert '_decn_p.get("front_side_basis") == "score"' not in blk
    for k in ("tape_print_age_s", "tape_print_age_bound_s", "tape_stale",
              "tape_n_ticks_effective", "tape_gap_restricted", "midday_lull"):
        assert f'"{k}"' in blk, f"{k} missing from the pullback-add receipt"


def test_the_reload_receipt_binds_the_window_that_decided(lr_code: str):
    """[1] review fix. `binding.tape_window_prints` is the window REQUESTED. The halt-gap
    restriction can cut it to a handful of prints and `gap_restricted` was dropped from
    both receipts, so a 3-print accel was reportable as 255. Receipts carry the inputs AND
    the value that decided."""
    i = lr_code.find("_mpr_binding = {")
    assert i > 0
    blk = lr_code[i:i + 4000]
    for k in ("tape_n_ticks_effective", "tape_gap_restricted", "tape_gap_split_s",
              "break_ref_kind", "break_ref_px", "print_age_floor_s",
              "print_age_gap_p99_s"):
        assert f'"{k}"' in blk, f"{k} missing from the re-load binding block"


def test_the_reload_ladder_is_a_function_not_an_inline_chain(lr_code: str):
    """⚠️ THE COVERAGE FIX. Every assertion about the live ladder used to be a substring
    match on a 17,000-character source slice plus a hand-written COPY of the branches in
    the test file -- so reordering `reclaim_wait` and `tape_not_confirming`, or dropping
    the staleness term, or flipping `>` to `>=`, left every test green. The ladder is now
    `entry_gates.micro_pullback_reload_proof`, called from the runner and executed
    directly by the tests (see section G and test_momentum_micro_pullback_reentry.py)."""
    assert "micro_pullback_reload_proof as _mpr_ladder_fn" in lr_code
    assert "_mpr_verdict = _mpr_ladder_fn(" in lr_code
    # and no transcribed copy of the branch chain survives in the runner
    assert '_mpr_block = "reclaim_wait"' not in lr_code
    assert '_mpr_block = "tape_unreadable"' not in lr_code


def test_the_reload_break_reference_is_a_print_with_a_named_fallback(lr_code: str):
    """[1] review fix. `bounce_high` is a QUOTE-MID level (`_build_micro_bar_df` buckets
    NBBO midpoints via `_row_ts_mid`), so `last_print > bounce_high` compared two different
    price bases -- the exact defect [59] was written to remove. The reference is now the
    break BAR's own high PRINT, and when that cannot be read the quote-mid is a NAMED
    fallback on the receipt, never a silent one."""
    assert "high_print_in_window as _mpr_hp_fn" in lr_code
    assert '_mpr_break_ref_kind = "break_bar_high_print"' in lr_code
    assert '_mpr_break_ref_kind = "quote_mid_micro_bar"' in lr_code
    assert "bounce_high_pos" in lr_code, (
        "the runner must map the break BAR back to its bucket to read its prints"
    )


def test_the_detector_reports_where_the_break_bar_is():
    """The positional index is what lets the caller turn a quote-mid level back into a
    print. Pure, additive; the fire path is unchanged."""
    df = _squeeze_then_dip(dip=0.30)
    out = micro_pullback_reentry_detect(df, shelf=9.0, max_dip_pct=0.04)
    assert out["fire"] is True, out
    assert out["n_bars"] == len(df)
    assert isinstance(out["bounce_high_pos"], int)
    assert isinstance(out["dip_low_pos"], int)
    assert out["dip_low_pos"] > out["bounce_high_pos"], "the dip follows the high"
    cols = {c.lower(): c for c in df.columns}
    assert df[cols["high"]].iloc[out["bounce_high_pos"]] == pytest.approx(
        out["bounce_high"])
    assert df[cols["low"]].iloc[out["dip_low_pos"]] == pytest.approx(out["dip_low"])


def test_the_reload_midday_lull_reports_instead_of_refusing(lr_code: str):
    """[1] review fix. `in_midday_lull` is a pure 10:30-14:30 ET wall-clock band and it
    refused this block BEFORE the tape was read, with `{"reason": "midday_lull"}` and no
    value at all -- 10 of 579 all-time re-load blocks. A clock in front of a print proof is
    the same defect as the floors this change replaces."""
    i = lr_code.find('"live_micro_pullback_detected"')
    assert i > 0
    pre = lr_code[max(0, i - 4000):i]
    assert '"reason": "midday_lull"' not in pre, (
        "the wall-clock lull still refuses the re-load ahead of the print proof"
    )
    blk = lr_code[i:i + 3000]
    assert '"midday_lull"' in blk and '"midday_lull_band"' in blk
    assert '"midday_lull_policy": "reported_not_enforced"' in blk


def test_the_micro_pullback_primary_entry_keeps_its_structural_stop():
    """⚠️ [1] review fix. `micro_pullback_primary_confirmation` sets
    `debug["pullback_low"] = dip_low` -- "entry = the micro-break, stop = the
    micro-pullback low" -- but neither fire reason was in STRUCTURAL_TRIGGER_REASONS, so
    the runner ran `le.pop("structural_stop_price")` on every fire and the placed stop fell
    back to the vol-floored ATR stop, depth-blind. That is load-bearing here: with the
    free-standing 0.04 cap gone, depth MUST reach the machinery that prices it -- a deeper
    dip widens the stop and therefore shrinks the size."""
    got = structural_trigger_reasons()
    assert "micro_pullback_primary" in got
    assert "micro_pullback_primary_tick_ok" in got


# ============================================ I. THE WINDOW MUST NOT BE A CLOCK ANYWHERE
def _ticks(n: int, cadence_s: float, *, t0: float = 1.0e9):
    """n synthetic prints at a fixed cadence, in the (price, size, bid, ask, ts) shape
    `_signed_tape_features` parses."""
    return [
        (10.0 + i * 0.001, 100.0, 9.99 + i * 0.001, 10.01 + i * 0.001, t0 + i * cadence_s)
        for i in range(n)
    ]


@pytest.mark.parametrize("cadence", [7.6, 10.0, 30.0])
def test_a_print_window_is_not_truncated_by_a_seconds_clock(cadence):
    """⚠️ [1] review fix. In `window_prints` mode `window_s` was STILL
    chili_momentum_l2_confirm_window_s (15.0), and the halt-gap restriction dropped
    everything before the last inter-print gap above `window_s / 2` = 7.5 s. So a perfectly
    healthy slow tape returned None -> `tape_unreadable` -> permanent WAIT on the re-load,
    and `front_side_basis="score"` -> the 0.50 strength floor (whose live median AND
    maximum are both 0.4611) on the pullback add. The headline claim "the window must not
    be a clock" was false in the decision-relevant direction.

    Not hypothetical: measured on `chili` 2026-09-10 11:00-13:30Z, gaps > 7.5 s were SUNE
    86 of 4,418 (max 635 s), TPET 18 of 42,196, SKYQ 6 of 16,771."""
    rows = _ticks(255, cadence)
    assert _signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=0.0) is None, (
        "the pre-fix behaviour this test exists to prevent"
    )
    out = _signed_tape_features(
        rows, window_s=15.0, tick_rate_floor_pctile=0.0, window_mode="prints")
    assert out is not None
    assert out["n_ticks"] == 255
    assert out["gap_restricted"] is False
    assert out["window_mode"] == "prints"
    # the threshold is HALF THE SPAN ACTUALLY READ -- the tape's own clock
    assert out["gap_split_s"] == pytest.approx(254 * cadence / 2.0, rel=1e-6)


def test_a_single_pause_no_longer_silently_shrinks_the_window():
    """The other half of the same defect: one 8 s pause with three prints after it left
    `n_ticks = 3` and `gap_restricted = True` while the binding block still said 255 --
    an accel computed from three prints, reported as the window that was requested."""
    rows = _ticks(250, 0.05) + [
        (10.5, 100.0, 10.49, 10.51, 1.0e9 + 250 * 0.05 + 8.0 + j * 0.05) for j in range(3)
    ]
    secs = _signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=0.0)
    assert secs is not None and secs["n_ticks"] == 3 and secs["gap_restricted"] is True
    prints = _signed_tape_features(
        rows, window_s=15.0, tick_rate_floor_pctile=0.0, window_mode="prints")
    assert prints is not None
    assert prints["n_ticks"] == 253
    assert prints["gap_restricted"] is False


def test_a_real_discontinuity_still_restricts_the_window():
    """The restriction is not disabled -- it is re-based. A genuine halt (a gap larger than
    half the span actually read) still truncates, which is what it was written for
    (XPON 2026-08-26 LULD halt)."""
    rows = _ticks(20, 0.05) + [
        (11.0, 100.0, 10.99, 11.01, 1.0e9 + 20 * 0.05 + 600.0 + j * 0.05) for j in range(20)
    ]
    out = _signed_tape_features(
        rows, window_s=15.0, tick_rate_floor_pctile=0.0, window_mode="prints")
    assert out is not None
    assert out["gap_restricted"] is True
    assert out["n_ticks"] == 20, "only the continuous post-halt segment is measured"


def test_the_seconds_mode_is_byte_identical():
    """Callers that have not moved must be unaffected: `window_mode` defaults to
    "seconds" and reproduces the old threshold exactly."""
    rows = _ticks(255, 0.05)
    a = _signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=0.0)
    b = _signed_tape_features(
        rows, window_s=15.0, tick_rate_floor_pctile=0.0, window_mode="seconds")
    assert a is not None and b is not None
    assert a["gap_split_s"] == pytest.approx(7.5) == b["gap_split_s"]
    assert a["window_mode"] == "seconds"


# ================================================ J. THE AGE BOUND, AND ITS DERIVATION
def test_the_age_bound_is_no_longer_inert_by_construction():
    """[1] review fix (derivation honesty). Under the old 7.5 s halt rule `gap_p99_s` was
    computed on the segment that SURVIVED the restriction, so every surviving gap was
    <= 7.5 s and `max(14.69, gap_p99)` was ALWAYS exactly the floor -- a constant presented
    as an adaptive, tape-derived bound. With the half-span rule a slow tape can genuinely
    carry a p99 above the floor."""
    assert tape_print_age_bound_s(age_floor_s=14.69, gap_p99_s=None) == pytest.approx(14.69)
    assert tape_print_age_bound_s(age_floor_s=14.69, gap_p99_s=4.62) == pytest.approx(14.69)
    assert tape_print_age_bound_s(age_floor_s=14.69, gap_p99_s=46.01) == pytest.approx(46.01)
    # measured at the four replayable detections the FLOOR binds (p99 0.08-4.62 s) --
    # recorded so the claim in the doc is checkable, not merely asserted.
    for p99 in (0.08, 0.08, 0.09, 4.62):
        assert tape_print_age_bound_s(age_floor_s=14.69, gap_p99_s=p99) == pytest.approx(14.69)


def test_an_unmeasurable_age_is_none_not_zero():
    """`tape_print_age_s` must not invent a fresh age: None in, None out -- and the ladder
    treats None as STALE, so the fail-closed contract holds end to end."""
    assert tape_print_age_s(None) is None
    assert tape_print_age_s("not-a-timestamp") is None
    assert micro_pullback_reload_proof(
        veto=False, last_print=10.0, signed_tape_accel=5.0,
        tape_stale=(None if tape_print_age_s(None) is None else False),
        break_ref_px=9.0, reclaim_high_px=10.0,
    ) == "tape_unreadable"


def test_the_age_is_measured_against_the_supplied_clock():
    """Replay parity: the age is computed against the as-of instant the caller threads, not
    wall time."""
    import datetime as _dt

    t = _dt.datetime(2026, 9, 10, 13, 27, 13, 851498)
    last_ts = (t - _dt.datetime(1970, 1, 1)).total_seconds() - 900.1
    assert tape_print_age_s(last_ts, now=t) == pytest.approx(900.1, abs=1e-3)
    # tz-aware clocks are normalised, not rejected
    assert tape_print_age_s(
        last_ts, now=t.replace(tzinfo=_dt.timezone.utc)
    ) == pytest.approx(900.1, abs=1e-3)


def test_high_print_in_window_fails_closed_on_every_unreadable_shape():
    """The reference read is the new load-bearing query; an unreadable answer must be
    `(None, 0)` so the ladder falls to `break_reference_unreadable` / `reclaim_wait`
    instead of admitting on a missing number."""
    assert high_print_in_window(None, db=object()) == (None, 0)
    assert high_print_in_window("SUNE", db=None) == (None, 0)
    assert high_print_in_window("BTC-USD", db=object()) == (None, 0)   # crypto: no tape
    assert high_print_in_window("SUNE", db=object(), start_at=None, end_at=None) == (None, 0)
    # end <= start is not a window
    assert high_print_in_window(
        "SUNE", db=object(),
        start_at="2026-09-09T09:31:10", end_at="2026-09-09T09:31:00",
    ) == (None, 0)


def test_the_modules_still_parse(lr_src: str, eg_src: str):
    """Two hand-edited files, one of them 48k lines. Parse them, cheaply, every run."""
    ast.parse(lr_src)
    ast.parse(eg_src)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
