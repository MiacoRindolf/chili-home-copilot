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

So depth became evidence on the receipt and the positive-confirm became a PRINT PROOF
(last_print > bounce_high AND signed_tape_accel > 0) -- the [59] form. The structural
knives are untouched: the SHELF still refuses, and `_entry_flow_veto` stays as the NAMED
knife.

The live_runner assertions are source-level, and that limit is deliberate: the branch sits
~48,300 lines into `tick_live_session` behind a live position in STATE_LIVE_TRAILING, so a
unit test cannot reach it without simulating a whole session. What a source test CAN
guarantee is that the inverted floors do not quietly come back and that the proof reads the
tape.

Runnable: pytest tests/test_dip_gates_report_not_refuse.py -v
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pandas as pd
import pytest

from app.config import Settings
from app.services.trading.momentum_neural.entry_gates import (
    _MICROPULLBACK_DIP_ONSET_QUANTILES,
    _dip_onset_percentile,
    micro_pullback_reentry_detect,
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


def test_the_reload_proof_reads_the_tape_and_compares_it_to_the_break(lr_src: str):
    """The replacement: a PRINT above `bounce_high` with the signed push still running.
    `bounce_high` existed on this path all along and was never once compared to a price."""
    i = lr_src.find("live_micro_pullback_detected")
    assert i > 0
    blk = lr_src[i:i + 17000]
    assert "signed_tape_accel_features as _mpr_tape_fn" in blk
    assert "window_prints=_mpr_win_prints" in blk
    assert "_mpr_last_print > _mpr_bounce_high" in blk, (
        "the micro-break level must be compared to an actual print"
    )
    assert "_mpr_accel > 0.0" in blk


def test_the_reload_blocks_are_named(lr_src: str):
    """One name per mechanism, so a receipt can be read. `flow` named four different
    things; these name one each."""
    i = lr_src.find("live_micro_pullback_detected")
    blk = lr_src[i:i + 17000]
    for reason in ("flow_veto", "tape_unreadable", "reclaim_wait", "tape_not_confirming"):
        assert f'"{reason}"' in blk, f"block reason {reason} missing"
    assert "live_micro_pullback_reentry_proof" in blk, "a PASS must leave a receipt too"


def test_the_named_knife_survives(lr_src: str):
    """`_entry_flow_veto` (never buy into selling, the 06-24 fix) is the one thing in the
    old flow gate that was a knife. It stays, and now has its own name."""
    i = lr_src.find("live_micro_pullback_detected")
    blk = lr_src[i:i + 17000]
    assert "_entry_flow_veto(_mpr_ofi, _mpr_tf, settings)" in blk
    assert re.search(r"if _veto:\s*\n(?:\s*#.*\n)*\s*_mpr_block = \"flow_veto\"", blk), (
        "the veto must be the FIRST branch -- a knife outranks evidence"
    )


def test_ofi_and_trade_flow_are_still_reported(lr_src: str):
    """Demoted to evidence, not deleted: the value that used to decide still lands on the
    receipt so the decision stays auditable against the derivation."""
    i = lr_src.find("live_micro_pullback_detected")
    blk = lr_src[i:i + 17000]
    assert '"ofi": _mpr_ofi' in blk
    assert '"trade_flow": _mpr_tf' in blk
    assert '"ofi_policy": "reported_not_enforced"' in blk
    assert '"trade_flow_policy": "reported_not_enforced"' in blk


def test_unreadable_or_stale_tape_waits(lr_src: str):
    """Fail-CLOSED for an extra BUY, the [59] contract. And the AGE bound is load-bearing:
    37 names have no real-time NYSE entitlement -- TPET arrives p50 900.44 s behind."""
    i = lr_src.find("live_micro_pullback_detected")
    blk = lr_src[i:i + 17000]
    assert "chili_momentum_g4_reentry_max_print_age_seconds" in blk, (
        "a 15-minute-old print is not a reclaim proof"
    )
    assert "_mpr_stale is True" in blk
    assert '_mpr_block = "tape_unreadable"' in blk


def test_the_proof_reuses_derived_values_and_adds_no_new_knob(lr_src: str):
    """No magic numbers: both the window and the age bound are values already derived and
    documented for [58]/[59]. No new Settings field is introduced by this path."""
    i = lr_src.find("live_micro_pullback_detected")
    blk = lr_src[i:i + 17000]
    assert "chili_momentum_g4_reentry_tape_window_prints" in blk
    s = Settings()
    assert s.chili_momentum_g4_reentry_tape_window_prints == 255
    assert s.chili_momentum_g4_reentry_max_print_age_seconds == pytest.approx(14.69)


def test_the_receipt_carries_the_binding_block(lr_src: str):
    i = lr_src.find("live_micro_pullback_detected")
    blk = lr_src[i:i + 17000]
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


# ===================================================== G. THE LIVE PIN (SKYQ 21591)
_SKYQ_21591_INSTANTS = [
    # (utc, last_print in the 255-print window, signed_tape_accel, window_high)
    ("2026-09-10 13:52:17.602228", 3.67, 5133.0, 3.68),
    ("2026-09-10 13:52:22.876284", 3.6597, -4412.0, 3.6799),
    ("2026-09-10 13:52:30.705169", 3.6811, -1120.0, 3.71),
]
_SKYQ_21591_BOUNCE_HIGH = 3.715


def _proof_decision(last_print, accel, bounce_high, *, veto=False, stale=False):
    """The live block's decision ladder, transcribed. Kept beside the source pins above so
    the ORDER of the branches is executable, not just asserted to exist."""
    if veto:
        return "flow_veto"
    if last_print is None or last_print <= 0 or accel is None or stale:
        return "tape_unreadable"
    if bounce_high is None or not (last_print > bounce_high + 1e-9):
        return "reclaim_wait"
    if not (accel > 0.0):
        return "tape_not_confirming"
    return "proof"


@pytest.mark.parametrize("utc,last_print,accel,win_high", _SKYQ_21591_INSTANTS)
def test_skyq_21591_would_be_reclaim_wait(utc, last_print, accel, win_high):
    """EXECUTABLE PIN of the real instants. All three SKYQ 21591 micro-pullback detections
    (2026-09-10 13:52) reported bounce_high 3.715 while the tape's newest print in the
    255-print window was 3.67 / 3.6597 / 3.6811 and the window's HIGHEST print was
    3.68 / 3.6799 / 3.71 -- the micro-break level sat ABOVE every print in the window.

    Values measured read-only against live `chili` via
    signed_tape_accel_features('SKYQ', as_of=<utc>, window_prints=255).

    The old gate refused these as `reason=flow` with veto=false, i.e. on an anti-selective
    OFI floor, while never checking that the break had not been taken out. The new proof
    refuses them as `reclaim_wait` -- the same answer for the RIGHT reason, and the receipt
    now carries the two numbers that make it checkable."""
    assert win_high < _SKYQ_21591_BOUNCE_HIGH, (
        "the detector's quote-mid bounce_high sat above the highest PRINT of the window"
    )
    assert _proof_decision(last_print, accel, _SKYQ_21591_BOUNCE_HIGH) == "reclaim_wait"


def test_sune_20774_would_be_reclaim_wait():
    """The fourth and only other detection ever: SUNE 20774, 2026-09-09 09:31:14,
    bounce_high 3.01, last_print 3.00, accel +14,447, window high 3.02. The tape was
    genuinely buying (accel strongly positive, buy_share_delta +0.256) and trade_flow
    +0.655 CLEARED its floor -- it was the OFI floor (-0.0184 vs +0.30) that refused it.
    The print proof says the honest thing instead: one cent short of the break. WAIT."""
    assert _proof_decision(3.00, 14447.0, 3.01) == "reclaim_wait"


def test_the_ladder_lets_a_real_reclaim_through():
    """The negative control for the pins above: clear the break with the push running and
    the proof passes -- the ladder is not a disguised permanent refusal."""
    assert _proof_decision(3.72, 5133.0, _SKYQ_21591_BOUNCE_HIGH) == "proof"
    assert _proof_decision(3.72, -1.0, _SKYQ_21591_BOUNCE_HIGH) == "tape_not_confirming"
    assert _proof_decision(3.72, 5133.0, _SKYQ_21591_BOUNCE_HIGH, stale=True) == "tape_unreadable"
    assert _proof_decision(3.72, 5133.0, _SKYQ_21591_BOUNCE_HIGH, veto=True) == "flow_veto"


# ===================================================== H. THE SIDE FIXES ON THIS PATH
def test_the_pullback_add_receipt_names_its_basis(lr_src: str):
    """`live_pullback_add_vetoed` dropped front_side_basis / buy_share_delta /
    high_print_position, so a `weak_front_side` row could not be told apart from a tape
    that was unreadable at decision time. TPET (all 6 such rows on 2026-09-10) arrives
    `available_at - observed_at` p50 900.44 s -- exactly 15 minutes -- so at the decision
    instant its tape WAS empty while SKYQ/SUNE arrived in 0.27 s."""
    i = lr_src.find('_emit(db, sess, "live_pullback_add_vetoed", {')
    assert i > 0
    blk = lr_src[i:i + 4000]
    for k in ("front_side_basis", "buy_share_delta", "high_print_position",
              "tape_unreadable"):
        assert f'"{k}"' in blk, f"{k} still missing from the veto receipt"


def test_the_pullback_add_tape_window_is_print_indexed(lr_src: str):
    """Same doctrine as [58]/[59]: the window must not be a clock. Measured at the six TPET
    weak_front_side instants, buy_share_delta comes out with the OPPOSITE SIGN in the 15-s
    window vs the 255-print window at three of six -- and buy_share_delta > 0 IS the gate."""
    i = lr_src.find("signed_tape_accel_features as _pba_feat_fn")
    assert i > 0
    blk = lr_src[i:i + 2000]
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


def test_the_modules_still_parse(lr_src: str, eg_src: str):
    """Two hand-edited files, one of them 48k lines. Parse them, cheaply, every run."""
    ast.parse(lr_src)
    ast.parse(eg_src)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
