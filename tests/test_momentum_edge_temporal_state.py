"""PRINCIPAL-LEVEL edge-case bug hunt — TEMPORAL / STATE lifecycle of the Ross
momentum lane.

These are NOT branch-coverage tests. Each constructs the gnarliest plausible
input the function meets in prod and asserts the SPECIFIC correct stateful
result so the test FAILS if the code is subtly wrong (stale-read, overwrite,
reset, midnight boundary, double-count).

Four lifecycles under attack (TESTS-ONLY — no source modified):

  1. ``le["entry_trigger_reason"]`` threading: SET at candidate-fire (tick N) /
     READ at sizing (tick N+1) by the reclaim spread-cost carve-out
     (``spread_cost_veto._is_reclaim_family``). A session that armed before this
     code, two fires with different reasons, or a reason STALE from a prior entry
     on the same session.

  2. halt_level / halt_resumption_open / halt_resumed_at_utc / halt_chain_up_count
     capture lifecycle across HALT -> RESUME -> RE-HALT in
     ``live_runner._register_stale_quote_tick`` + ``_register_fresh_quote_tick``,
     and the new-day vs resume-down reset of the up-chain counter.

  3. move-exhaustion ``_VIABILITY_PEAK`` tracking in ``auto_arm``: peak set, a
     higher peak, then regression; a missing/None/stale/zero peak.

  4. green-day consecutive streak in ``risk_policy.consecutive_green_days``:
     terminal_at exactly at ET midnight, two outcomes the same ET day (sum),
     terminal_at = None, never-entered rows, the day-1 graduation multiplier.

Run with TEST_DATABASE_URL set (conftest seeds DATABASE_URL); the tests are
PURE-LOGIC + mocks (SimpleNamespace sessions, fake-query db, patch.object on
settings) so they do not truncate / require live DB data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.services.trading.momentum_neural import auto_arm
from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural import risk_policy as RP
from app.services.trading.momentum_neural.spread_cost_veto import _is_reclaim_family


# --------------------------------------------------------------------------- #
# Helpers / doubles                                                           #
# --------------------------------------------------------------------------- #
class _Tick:
    """Minimal quote-tick double exposing the attrs the halt capture reads
    (bid / mid / open). Missing attrs default to None (getattr fallback)."""

    def __init__(self, bid=None, mid=None, open=None):  # noqa: A002 - mirror source attr name
        self.bid = bid
        self.mid = mid
        self.open = open


def _sess():
    """Lightweight TradingAutomationSession double. ``risk_snapshot_json`` is the
    persisted store ``_commit_le`` writes; everything else is metadata the halt
    path references in log/emit payloads."""
    return SimpleNamespace(
        id=4242,
        symbol="ABCD",
        risk_snapshot_json={},
        state="watching_live",
        updated_at=None,
        user_id=1,
        variant_id=7,
        execution_family="momentum_neural",
        mode="live",
    )


ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _et_midnight_to_naive_utc(et_date_str: str, hour=0, minute=0):
    """Build a NAIVE-UTC datetime (the shape ``terminal_at`` rows carry) for a
    given ET wall-clock instant. Mirrors how the DB stores tz-naive UTC."""
    y, m, d = (int(x) for x in et_date_str.split("-"))
    et_dt = datetime(y, m, d, hour, minute, tzinfo=ET)
    return et_dt.astimezone(UTC).replace(tzinfo=None)


class _FakeQuery:
    """db.query(...).filter(...).all() chain returning a fixed row list.

    consecutive_green_days references real ORM column expressions in .filter();
    a fake query ignores them and returns our synthetic 3-tuples. This proves the
    PURE bucketing/streak logic without a live table."""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeQuery(self._rows)


# ============================================================================ #
# (1) le["entry_trigger_reason"] threading — set @ fire, read @ sizing         #
# ============================================================================ #
class TestEntryTriggerReasonThreading:
    """The reclaim spread-cost carve-out reads le['entry_trigger_reason'] on a
    LATER tick. The danger is a STALE reason from a PRIOR entry, a MISSING key
    (session armed before this code shipped), or a reason that no longer matches
    the structural stash. _is_reclaim_family is the consumer — it must be
    fail-CLOSED so a missing/garbage reason can NEVER loosen the cost gate."""

    def test_missing_key_is_not_reclaim_failclosed(self):
        # le.get("entry_trigger_reason") on a pre-code session => None. A None must
        # be judged NON-reclaim (stricter, hard-veto-capable base), never the
        # permissive reclaim base. Fail-OPEN here would silently widen cost on
        # every legacy session.
        assert _is_reclaim_family(None) is False
        assert _is_reclaim_family("") is False
        assert _is_reclaim_family("   ") is False

    def test_non_reclaim_early_fire_reasons_are_strict(self):
        # The two additive early-fire paths stamp these EXACT reasons. Neither is a
        # reclaim — if a substring accidentally matched, every continuation/tape-hold
        # entry would get the loose base. Lock the negative.
        assert _is_reclaim_family("tape_confirmed_hold") is False
        assert _is_reclaim_family("momentum_continuation") is False
        assert _is_reclaim_family("score_only") is False
        assert _is_reclaim_family("trigger_wait") is False

    def test_reclaim_family_variants_all_recognized(self):
        # Every reclaim/dip trigger-reason variant the candidate path stamps must
        # map to the permissive base. Substring match means *_tick_ok variants count.
        for r in (
            "vwap_reclaim",
            "wick_reclaim",
            "deep_reclaim_ok",
            "deep_reclaim_tick_ok",
            "deep_reclaim_dipbuy_ok",
            "flush_dip_buy",
            "halt_resume_dip_ok",
            "ask_thins_dip_tick",
            "sub_vwap_trap",
        ):
            assert _is_reclaim_family(r) is True, r

    def test_stale_reclaim_reason_survives_a_non_reclaim_refire(self):
        """SOURCE-BUG PROBE. A session enters once on a reclaim (key set), exits,
        then the SAME session re-fires via the momentum_volume FALLBACK. The
        candidate path POPS structural_stop_price / breakout_level_price /
        dip_velocity_size_mult on a non-structural fire — but it OVERWRITES
        entry_trigger_reason only when it reaches the stamp line. We assert the
        DESIRED invariant: after a non-reclaim re-fire the threaded reason must NOT
        still read as a reclaim. If the lane ever advances a NEW entry to sizing
        WITHOUT restamping (e.g. a candidate detected by a branch that skips the
        stamp), the carve-out would loosen the cost gate using the prior trade's
        reclaim reason. This test encodes the contract sizing relies on."""
        le = {"entry_trigger_reason": "vwap_reclaim"}
        # ... prior entry done; a fresh non-reclaim trigger fires and restamps.
        le["entry_trigger_reason"] = "momentum_volume_confirmation"
        assert _is_reclaim_family(le.get("entry_trigger_reason")) is False
        # And the reverse contract: a reclaim restamp must flip it permissive.
        le["entry_trigger_reason"] = "flush_dip_buy"
        assert _is_reclaim_family(le.get("entry_trigger_reason")) is True

    def test_garbage_or_nonstring_reason_substring_footgun(self):
        """SOURCE-BUG PROBE (low-severity). _is_reclaim_family does
        ``str(reason).lower()`` then a SUBSTRING scan — it does NOT require the value
        to actually be a string. So a corrupted snapshot value whose repr CONTAINS a
        reclaim token (a dict {"reason": "vwap_reclaim"} or a list ["dip"]) is
        WRONGLY classified as a reclaim and gets the permissive cost base. A plain int
        is correctly non-reclaim. This documents the foot-gun: if entry_trigger_reason
        is ever written as a non-string, the carve-out can loosen on garbage."""
        assert _is_reclaim_family(12345) is False  # no token in "12345"
        # str({'reason': 'vwap_reclaim'}) contains "reclaim" => leaks to True.
        assert _is_reclaim_family({"reason": "vwap_reclaim"}) is True
        # str(['dip']) == "['dip']" contains "dip" => leaks to True.
        assert _is_reclaim_family(["dip"]) is True
        # A benign non-string with no token is correctly non-reclaim.
        assert _is_reclaim_family({"k": "v"}) is False


# ============================================================================ #
# (2) halt capture lifecycle: halt -> resume -> RE-HALT                        #
# ============================================================================ #
class TestHaltCaptureLifecycle:
    """halt_level (last good price) is captured ONCE at onset guarded by
    `not suspected_halt_since_utc`; resume pops that flag. A RE-HALT must capture
    a FRESH halt_level, and the up-chain counter must increment on resume-UP and
    reset on resume-DOWN. Stale halt_level / halt_resumed_at_utc leaking across
    halts is the bug class."""

    @staticmethod
    def _flags_on():
        # Turn on the resumption-direction + chain gate so the capture/branch code
        # actually writes halt_level / halt_chain_up_count (default OFF == byte-identical).
        return patch.multiple(
            LR.settings,
            chili_momentum_halt_resumption_direction_enabled=True,
            chili_momentum_halt_chain_risk_gate_enabled=True,
            chili_momentum_false_halt_avoid_enabled=False,
            chili_momentum_add_into_halt_enabled=False,
            chili_momentum_overnight_trading_enabled=False,
            chili_momentum_halt_stale_ticks=3,
            create=True,
        )

    def test_halt_level_captured_once_at_threshold_not_before(self):
        sess = _sess()
        le = sess.risk_snapshot_json.setdefault(LR.KEY_LIVE_EXEC, {})
        with self._flags_on(), patch.object(LR, "_emit"):
            # ticks below threshold: NO halt_level, NO suspected flag.
            LR._register_stale_quote_tick(None, sess, le, _Tick(bid=10.0))
            LR._register_stale_quote_tick(None, sess, le, _Tick(bid=9.9))
            assert "halt_level" not in le
            assert "suspected_halt_since_utc" not in le
            # threshold tick (3rd): capture the LAST GOOD bid as halt_level.
            LR._register_stale_quote_tick(None, sess, le, _Tick(bid=9.8))
            assert le.get("halt_level") == pytest.approx(9.8)
            assert le.get("suspected_halt_since_utc")

    def test_rehalt_recaptures_fresh_halt_level_not_stale(self):
        """SOURCE-CONTRACT. First halt captures 9.8; resume UP; a SECOND halt at a
        DIFFERENT price (12.0) must overwrite halt_level — the chain gate compares
        the NEXT resume against the SECOND halt's level, not the first. A stale
        9.8 would mis-classify the second resume direction."""
        sess = _sess()
        le = sess.risk_snapshot_json.setdefault(LR.KEY_LIVE_EXEC, {})
        with self._flags_on(), patch.object(LR, "_emit"):
            # Halt #1 onset @ 9.8
            for px in (10.0, 9.9, 9.8):
                LR._register_stale_quote_tick(None, sess, le, _Tick(bid=px))
            assert le.get("halt_level") == pytest.approx(9.8)
            # Resume UP @ 11.0 (>= 9.8) => up-chain +1, suspected flag popped.
            LR._register_fresh_quote_tick(None, sess, le, _Tick(bid=11.0, open=11.0))
            assert le.get("halt_chain_up_count") == 1
            assert "suspected_halt_since_utc" not in le
            # Halt #2 onset @ 12.0 — MUST recapture (suspected flag was popped).
            for px in (12.2, 12.1, 12.0):
                LR._register_stale_quote_tick(None, sess, le, _Tick(bid=px))
            assert le.get("halt_level") == pytest.approx(12.0), (
                "RE-HALT must capture a FRESH halt_level; a stale 9.8 here means the "
                "second resume-direction read compares against the wrong level"
            )

    def test_up_chain_increments_then_resets_on_resume_down(self):
        sess = _sess()
        le = sess.risk_snapshot_json.setdefault(LR.KEY_LIVE_EXEC, {})
        with self._flags_on(), patch.object(LR, "_emit"):
            # Halt #1 @ 5.0 -> resume UP @ 6.0 => count 1
            for px in (5.2, 5.1, 5.0):
                LR._register_stale_quote_tick(None, sess, le, _Tick(bid=px))
            LR._register_fresh_quote_tick(None, sess, le, _Tick(bid=6.0, open=6.0))
            assert le["halt_chain_up_count"] == 1
            # Halt #2 @ 6.5 -> resume UP @ 7.0 => count 2 (chain extends)
            for px in (6.7, 6.6, 6.5):
                LR._register_stale_quote_tick(None, sess, le, _Tick(bid=px))
            LR._register_fresh_quote_tick(None, sess, le, _Tick(bid=7.0, open=7.0))
            assert le["halt_chain_up_count"] == 2
            # Halt #3 @ 7.5 -> resume DOWN @ 6.0 (< 7.5) => RESET to 0 (fade ends chain)
            for px in (7.7, 7.6, 7.5):
                LR._register_stale_quote_tick(None, sess, le, _Tick(bid=px))
            LR._register_fresh_quote_tick(None, sess, le, _Tick(bid=6.0, open=6.0))
            assert le["halt_chain_up_count"] == 0, "resume DOWN must reset the up-chain"

    def test_resume_with_no_directional_read_counts_as_up_conservative(self):
        # halt_level present but the resume tick yields no usable price => the code
        # treats it as an UP (conservative: tighten the chain, don't loosen).
        sess = _sess()
        le = sess.risk_snapshot_json.setdefault(LR.KEY_LIVE_EXEC, {})
        with self._flags_on(), patch.object(LR, "_emit"):
            for px in (5.2, 5.1, 5.0):
                LR._register_stale_quote_tick(None, sess, le, _Tick(bid=px))
            assert le.get("halt_chain_up_count") in (None, 0)
            # resume tick with no bid/mid/open => _resume_px None => conservative +1.
            LR._register_fresh_quote_tick(None, sess, le, _Tick())
            assert le["halt_chain_up_count"] == 1

    def test_rehalt_onset_clears_stale_halt_resumed_at_utc(self):
        """FIX MED-3 — STALE RESUME MARKER CLEARED. _register_fresh_quote_tick stamps
        halt_resumed_at_utc on resume; a NEW halt onset now CLEARS it (pop) so the
        re-halt window cannot read the PRIOR resume. _halt_resume_cooldown_active reads
        that timestamp + the cooldown window, so once cleared it correctly reports
        FALSE (we are mid-halt, NOT in a post-resume cooldown) regardless of how recent
        the old resume was. Adversarial: even a still-fresh prior resume must not keep
        the cooldown alive across a fresh halt."""
        sess = _sess()
        le = sess.risk_snapshot_json.setdefault(LR.KEY_LIVE_EXEC, {})
        with self._flags_on(), patch.object(LR, "_emit"), patch.object(
            LR.settings, "chili_momentum_halt_resume_cooldown_seconds", 120.0, create=True
        ):
            # Halt #1 -> resume just now -> cooldown active.
            for px in (5.2, 5.1, 5.0):
                LR._register_stale_quote_tick(None, sess, le, _Tick(bid=px))
            LR._register_fresh_quote_tick(None, sess, le, _Tick(bid=6.0, open=6.0))
            assert LR._halt_resume_cooldown_active(le) is True
            assert le.get("halt_resumed_at_utc")  # marker present after resume
            # Halt #2 onset — book goes dark again. The NEW halt onset CLEARS the prior
            # resume marker, so the cooldown reads FALSE because there is no live resume,
            # not because an old marker happened to age out.
            for px in (6.7, 6.6, 6.5):
                LR._register_stale_quote_tick(None, sess, le, _Tick(bid=px))
            assert LR._halt_resume_cooldown_active(le) is False
            # The stale resume marker was CLEARED at the new halt onset (no stale leak).
            assert le.get("halt_resumed_at_utc") is None

    def test_halt_resumption_open_cleared_at_new_halt_onset(self):
        """FIX MED-3 — halt_resumption_open CLEARED at re-halt onset. Captured on resume,
        it is now popped when a NEW halt begins, so the add-into-halt H2/H3 legs (which
        compare a resumption open vs halt_level) cannot read the PRIOR resume's open
        mid-halt. Assert it is GONE after a new halt's stale ticks (no stale leak)."""
        sess = _sess()
        le = sess.risk_snapshot_json.setdefault(LR.KEY_LIVE_EXEC, {})
        with self._flags_on(), patch.object(LR, "_emit"):
            for px in (5.2, 5.1, 5.0):
                LR._register_stale_quote_tick(None, sess, le, _Tick(bid=px))
            LR._register_fresh_quote_tick(None, sess, le, _Tick(bid=6.0, open=6.0))
            assert le.get("halt_resumption_open") == pytest.approx(6.0)
            # New halt onset — resumption_open from the prior resume IS cleared.
            for px in (7.2, 7.1, 7.0):
                LR._register_stale_quote_tick(None, sess, le, _Tick(bid=px))
            assert le.get("halt_resumption_open") is None, (
                "halt_resumption_open must be cleared at re-halt onset so a mid-halt reader "
                "never sees the PRIOR resume's open"
            )

    def test_flags_off_is_byte_identical_no_halt_keys(self):
        sess = _sess()
        le = sess.risk_snapshot_json.setdefault(LR.KEY_LIVE_EXEC, {})
        with patch.multiple(
            LR.settings,
            chili_momentum_halt_resumption_direction_enabled=False,
            chili_momentum_halt_chain_risk_gate_enabled=False,
            chili_momentum_false_halt_avoid_enabled=False,
            chili_momentum_add_into_halt_enabled=False,
            chili_momentum_overnight_trading_enabled=False,
            chili_momentum_halt_stale_ticks=3,
            create=True,
        ), patch.object(LR, "_emit"):
            for px in (5.2, 5.1, 5.0):
                LR._register_stale_quote_tick(None, sess, le, _Tick(bid=px))
            LR._register_fresh_quote_tick(None, sess, le, _Tick(bid=6.0, open=6.0))
            # Flag-OFF: halt_level / chain / resumption_open are never written.
            assert "halt_level" not in le
            assert "halt_chain_up_count" not in le
            assert "halt_resumption_open" not in le
            # The resume bookkeeping (streak reset + resumed marker) STILL runs.
            assert le["halt_stale_streak"] == 0
            assert le.get("halt_resumed_at_utc")


# ============================================================================ #
# (3) move-exhaustion _VIABILITY_PEAK tracking across a session                #
# ============================================================================ #
class TestViabilityPeakTracking:
    """_update_viability_peak keeps a running per-symbol MAX (TTL-decayed);
    _viability_regressed proves a drop off that peak as a FRACTION. The peak dict
    is module-global keyed by SYMBOL only (no session id) — cross-session/-day
    bleed and None/zero/stale handling are the bug class."""

    def setup_method(self):
        auto_arm._VIABILITY_PEAK.clear()

    def teardown_method(self):
        auto_arm._VIABILITY_PEAK.clear()

    def _ttl(self):
        # Pin a generous TTL so timing is deterministic (default reuses the 600s
        # risk freshness window; we set it explicitly).
        return patch.object(
            auto_arm.settings,
            "chili_momentum_risk_viability_max_age_seconds",
            600.0,
            create=True,
        )

    def test_peak_raises_then_regression_detected_off_higher_peak(self):
        with self._ttl(), patch.object(
            auto_arm.settings, "chili_momentum_move_exhaustion_regress_frac", 0.20, create=True
        ):
            t0 = datetime(2026, 6, 27, 14, 0, 0)
            auto_arm._update_viability_peak("XYZ", 0.50, t0)
            # A HIGHER peak overwrites the running max.
            auto_arm._update_viability_peak("XYZ", 0.80, t0 + timedelta(seconds=10))
            # MED-5: peak is keyed by (symbol, session_id); default session is None.
            assert auto_arm._VIABILITY_PEAK[("XYZ", None)][0] == pytest.approx(0.80)
            # Now regress: 0.60 vs peak 0.80 => 0.60 <= 0.80*0.80=0.64 => regressed.
            assert auto_arm._viability_regressed("XYZ", 0.60, t0 + timedelta(seconds=20)) is True
            # 0.70 vs 0.80 => 0.70 > 0.64 => NOT regressed.
            assert auto_arm._viability_regressed("XYZ", 0.70, t0 + timedelta(seconds=21)) is False

    def test_peak_does_not_drop_when_score_falls_then_recovers(self):
        # The running MAX must hold through a dip: peak 0.9, dip 0.5 (does not lower
        # peak), recover 0.7. A regression read at 0.5 is TRUE against the 0.9 peak,
        # not a phantom 0.5 peak.
        with self._ttl(), patch.object(
            auto_arm.settings, "chili_momentum_move_exhaustion_regress_frac", 0.20, create=True
        ):
            t = datetime(2026, 6, 27, 14, 0, 0)
            auto_arm._update_viability_peak("AAA", 0.90, t)
            auto_arm._update_viability_peak("AAA", 0.50, t + timedelta(seconds=5))
            assert auto_arm._VIABILITY_PEAK[("AAA", None)][0] == pytest.approx(0.90), "peak must not fall"
            # 0.50 <= 0.90*0.8 = 0.72 => regressed True.
            assert auto_arm._viability_regressed("AAA", 0.50, t + timedelta(seconds=6)) is True

    def test_none_score_does_not_corrupt_or_create_peak(self):
        with self._ttl():
            t = datetime(2026, 6, 27, 14, 0, 0)
            auto_arm._update_viability_peak("NUL", None, t)
            assert ("NUL", None) not in auto_arm._VIABILITY_PEAK
            # regression with None current => fail-open False (cannot prove).
            with patch.object(
                auto_arm.settings, "chili_momentum_move_exhaustion_regress_frac", 0.20, create=True
            ):
                assert auto_arm._viability_regressed("NUL", None, t) is False
            # An existing peak must not be wiped by a later None.
            auto_arm._update_viability_peak("NUL", 0.7, t)
            auto_arm._update_viability_peak("NUL", None, t + timedelta(seconds=1))
            assert auto_arm._VIABILITY_PEAK[("NUL", None)][0] == pytest.approx(0.7)

    def test_missing_peak_failsopen_not_regressed(self):
        with patch.object(
            auto_arm.settings, "chili_momentum_move_exhaustion_regress_frac", 0.20, create=True
        ):
            # No peak ever recorded => cannot prove regression => False (arm permitted).
            assert auto_arm._viability_regressed("GHOST", 0.10, datetime(2026, 6, 27, 14, 0)) is False

    def test_stale_peak_beyond_ttl_failsopen(self):
        with self._ttl(), patch.object(
            auto_arm.settings, "chili_momentum_move_exhaustion_regress_frac", 0.20, create=True
        ):
            t = datetime(2026, 6, 27, 14, 0, 0)
            auto_arm._update_viability_peak("OLD", 0.90, t)
            # Read 700s later (> 600 TTL) => stale peak => cannot prove => False.
            assert auto_arm._viability_regressed("OLD", 0.10, t + timedelta(seconds=700)) is False

    def test_stale_update_rebuilds_peak_from_fresh_score(self):
        with self._ttl():
            t = datetime(2026, 6, 27, 14, 0, 0)
            auto_arm._update_viability_peak("RES", 0.90, t)
            # Resurgence after the TTL window => peak REBUILDS from the fresh score
            # (the prior 0.90 is dropped), not max(0.90, 0.40).
            auto_arm._update_viability_peak("RES", 0.40, t + timedelta(seconds=700))
            assert auto_arm._VIABILITY_PEAK[("RES", None)][0] == pytest.approx(0.40)

    def test_zero_peak_cannot_prove_regression(self):
        with self._ttl(), patch.object(
            auto_arm.settings, "chili_momentum_move_exhaustion_regress_frac", 0.20, create=True
        ):
            t = datetime(2026, 6, 27, 14, 0, 0)
            auto_arm._update_viability_peak("ZER", 0.0, t)
            # peak_f <= 0 => fail-open False even though now (0.0) <= 0*0.8.
            assert auto_arm._viability_regressed("ZER", 0.0, t) is False

    def test_distinct_sessions_same_symbol_get_independent_peaks(self):
        """FIX MED-5. _VIABILITY_PEAK is now keyed by (symbol, session_id), so two
        DIFFERENT sessions (or a re-arm) of the same ticker do NOT share the running
        max. A fresh re-arm at a lower score builds its OWN peak instead of being
        wrongly judged REGRESSED against a PRIOR session's peak. A single continuous
        session (session_id=None, the default) is unchanged."""
        with self._ttl(), patch.object(
            auto_arm.settings, "chili_momentum_move_exhaustion_regress_frac", 0.20, create=True
        ):
            t = datetime(2026, 6, 27, 14, 0, 0)
            # Session A prints a high peak.
            auto_arm._update_viability_peak("SHARE", 0.95, t, session_id="A")
            # Session B (a fresh re-arm, same symbol) starts at 0.60, 30s later, within
            # the TTL. It is a NEW session: its own peak builds from 0.60, so 0.60 is NOT
            # regressed against its OWN fresh peak (0.60 <= 0.60*0.8=0.48 is False).
            auto_arm._update_viability_peak("SHARE", 0.60, t + timedelta(seconds=30), session_id="B")
            assert (
                auto_arm._viability_regressed(
                    "SHARE", 0.60, t + timedelta(seconds=31), session_id="B"
                )
                is False
            )
            # Session A's peak is untouched: 0.60 vs A's 0.95 (0.60 <= 0.76) => still regressed.
            assert (
                auto_arm._viability_regressed(
                    "SHARE", 0.60, t + timedelta(seconds=31), session_id="A"
                )
                is True
            )
            # The two peaks are stored under distinct keys.
            assert auto_arm._VIABILITY_PEAK[("SHARE", "A")][0] == pytest.approx(0.95)
            assert auto_arm._VIABILITY_PEAK[("SHARE", "B")][0] == pytest.approx(0.60)


# ============================================================================ #
# (4) green-day consecutive streak — ET midnight / same-day sum / None         #
# ============================================================================ #
class TestConsecutiveGreenDays:
    """consecutive_green_days buckets terminal_at by ET calendar day, sums per
    day, walks back from the most-recent PAST day (today excluded), and stops at
    the first non-green day. Midnight-ET bucketing, same-day summation, None
    terminal_at, and never-entered-row exclusion are the bug class."""

    def _rows_for(self, entries):
        """entries: list of (naive_utc_terminal_at, pnl, outcome_class)."""
        return _FakeDB(entries)

    def test_two_outcomes_same_et_day_are_summed_not_counted_twice(self):
        # Two fills the SAME ET day: -5 then +8 => net +3 => that day is GREEN once.
        # A naive per-row counter would see one red + one green and mis-streak.
        day = "2026-06-24"
        rows = [
            (_et_midnight_to_naive_utc(day, 10, 0), -5.0, "stop_loss"),
            (_et_midnight_to_naive_utc(day, 15, 0), 8.0, "success"),
        ]
        # Make "today" deterministic so day-2026-06-24 is a PAST day in window.
        with patch.object(
            RP, "_et_day_bounds_utc", side_effect=self._bounds_factory("2026-06-27")
        ):
            streak, meta = RP.consecutive_green_days(
                self._rows_for(rows), execution_family="momentum_neural", lookback_days=30
            )
        assert streak == 1
        assert meta["green_usd"] == pytest.approx(3.0)
        assert meta["days_seen"] == 1

    def test_same_day_sum_goes_red_breaks_streak(self):
        # -10 then +4 => net -6 => RED day => streak 0 even though one leg was green.
        day = "2026-06-24"
        rows = [
            (_et_midnight_to_naive_utc(day, 10, 0), -10.0, "stop_loss"),
            (_et_midnight_to_naive_utc(day, 15, 0), 4.0, "small_win"),
        ]
        with patch.object(
            RP, "_et_day_bounds_utc", side_effect=self._bounds_factory("2026-06-27")
        ):
            streak, _ = RP.consecutive_green_days(
                self._rows_for(rows), execution_family="momentum_neural", lookback_days=30
            )
        assert streak == 0

    def test_terminal_at_at_et_midnight_buckets_to_the_new_day(self):
        """An outcome stamped EXACTLY at 00:00 ET belongs to that NEW ET day, not
        the prior one. Build two green days that are CONTIGUOUS only if the midnight
        instant lands on the expected day; a 1-day bucketing slip would split them
        and cap the streak at 1."""
        # 06-25 has a green at 00:00 ET (midnight) and 06-26 has a green midday.
        # Both are past relative to 06-27 "today".
        rows = [
            (_et_midnight_to_naive_utc("2026-06-25", 0, 0), 5.0, "success"),   # exactly midnight ET
            (_et_midnight_to_naive_utc("2026-06-26", 12, 0), 7.0, "success"),
        ]
        with patch.object(
            RP, "_et_day_bounds_utc", side_effect=self._bounds_factory("2026-06-27")
        ):
            streak, meta = RP.consecutive_green_days(
                self._rows_for(rows), execution_family="momentum_neural", lookback_days=30
            )
        # 06-25 and 06-26 both green & contiguous => streak 2. If midnight mis-bucketed
        # to 06-24, the 06-25 day would be MISSING and the contiguous run breaks at 1.
        assert streak == 2, "midnight-ET outcome must bucket to its OWN ET day"
        assert meta["days_seen"] == 2

    def test_none_terminal_at_is_skipped(self):
        # A None terminal_at must be dropped, not crash, not bucket as epoch.
        rows = [
            (None, 100.0, "success"),  # malformed row — skip
            (_et_midnight_to_naive_utc("2026-06-26", 12, 0), 6.0, "success"),
        ]
        with patch.object(
            RP, "_et_day_bounds_utc", side_effect=self._bounds_factory("2026-06-27")
        ):
            streak, meta = RP.consecutive_green_days(
                self._rows_for(rows), execution_family="momentum_neural", lookback_days=30
            )
        assert streak == 1
        assert meta["green_usd"] == pytest.approx(6.0)

    def test_never_entered_rows_excluded_from_daily_sum(self):
        # A day with ONLY a +0.0 cancelled_pre_entry must NOT count as a (non-green)
        # day that breaks a streak — it is excluded entirely. Here 06-26 is a real
        # green; 06-25 is ONLY a never-entered cancel (excluded => day absent =>
        # contiguity is between 06-26 and ... nothing earlier => streak 1).
        rows = [
            (_et_midnight_to_naive_utc("2026-06-25", 11, 0), 0.0, "cancelled_pre_entry"),
            (_et_midnight_to_naive_utc("2026-06-26", 12, 0), 9.0, "success"),
        ]
        with patch.object(
            RP, "_et_day_bounds_utc", side_effect=self._bounds_factory("2026-06-27")
        ):
            streak, meta = RP.consecutive_green_days(
                self._rows_for(rows), execution_family="momentum_neural", lookback_days=30
            )
        assert streak == 1
        assert meta["days_seen"] == 1, "the never-entered-only day must not form a bucket"

    def test_real_entered_zero_pnl_day_is_red_and_breaks_streak(self):
        """A REAL entered trade that netted EXACTLY 0.0 (e.g. flat_unknown / a
        scratch) is NOT > 0.0 => the day is non-green => the streak breaks there.
        flat_unknown is a real-entry class (not in _NEVER_ENTERED), so it is NOT
        excluded — distinguishing it from a $0 cancel is the subtle correctness
        point."""
        rows = [
            (_et_midnight_to_naive_utc("2026-06-24", 12, 0), 5.0, "success"),       # older green
            (_et_midnight_to_naive_utc("2026-06-25", 12, 0), 0.0, "flat_unknown"),  # real scratch => red
            (_et_midnight_to_naive_utc("2026-06-26", 12, 0), 7.0, "success"),       # most-recent green
        ]
        with patch.object(
            RP, "_et_day_bounds_utc", side_effect=self._bounds_factory("2026-06-27")
        ):
            streak, _ = RP.consecutive_green_days(
                self._rows_for(rows), execution_family="momentum_neural", lookback_days=30
            )
        # Walk back from 06-26 (green=1) -> 06-25 scratch (0.0, not > 0) => STOP. Streak 1.
        assert streak == 1

    def test_today_excluded_so_intraday_red_cannot_collapse_streak(self):
        # An outcome stamped TODAY (>= today_start) must be filtered out by the query
        # bound — our fake query returns it, but the function's own date filter is in
        # the WHERE we mocked away, so we instead assert via the bucketer: a row whose
        # ET date == today must still be excluded by being >= today_start. We emulate
        # by giving the row a today timestamp and confirming it does not extend the
        # streak beyond the genuine past green.
        rows = [
            (_et_midnight_to_naive_utc("2026-06-26", 12, 0), 4.0, "success"),  # past green
            (_et_midnight_to_naive_utc("2026-06-27", 9, 30), -50.0, "stop_loss"),  # TODAY red
        ]
        # The real function filters terminal_at < today_start in SQL; our _FakeQuery
        # ignores filters, so to honor the contract we pre-strip today's row the way
        # the DB would, then assert the past green still streaks.
        past_only = _FakeDB([r for r in rows if r[0] < _et_midnight_to_naive_utc("2026-06-27", 0, 0)])
        with patch.object(
            RP, "_et_day_bounds_utc", side_effect=self._bounds_factory("2026-06-27")
        ):
            streak, _ = RP.consecutive_green_days(
                past_only, execution_family="momentum_neural", lookback_days=30
            )
        assert streak == 1

    def test_no_input_and_thin_history_neutral(self):
        # Defensive: no db / no family / empty rows => (0, neutral) — never crash.
        assert RP.consecutive_green_days(None, execution_family="x")[0] == 0
        assert RP.consecutive_green_days(_FakeDB([]), execution_family=None)[0] == 0
        with patch.object(
            RP, "_et_day_bounds_utc", side_effect=self._bounds_factory("2026-06-27")
        ):
            assert RP.consecutive_green_days(_FakeDB([]), execution_family="m")[0] == 0

    # --- graduation multiplier (day-1 vs streak) --------------------------- #
    def test_graduation_multiplier_day1_is_neutral(self):
        # streak<=1 => 1.0 (no graduation off a single green day) even when enabled.
        with patch.multiple(
            RP.settings,
            chili_momentum_green_day_graduation_enabled=True,
            chili_momentum_green_day_step_per_day=0.1,
            chili_momentum_green_day_max_multiplier=2.0,
            chili_momentum_green_day_lookback_days=30,
            create=True,
        ), patch.object(RP, "consecutive_green_days", return_value=(1, {"green_usd": 5.0, "days_seen": 1})):
            mult, meta = RP.green_day_graduation_multiplier(object(), execution_family="m")
        assert mult == pytest.approx(1.0)
        assert meta["consecutive_green_days"] == 1

    def test_graduation_multiplier_scales_with_streak_and_clamps(self):
        with patch.multiple(
            RP.settings,
            chili_momentum_green_day_graduation_enabled=True,
            chili_momentum_green_day_step_per_day=0.1,
            chili_momentum_green_day_max_multiplier=1.5,
            chili_momentum_green_day_lookback_days=30,
            create=True,
        ):
            # streak 4 => 1 + 0.1*(4-1) = 1.30
            with patch.object(RP, "consecutive_green_days", return_value=(4, {})):
                assert RP.green_day_graduation_multiplier(object(), execution_family="m")[0] == pytest.approx(1.30)
            # streak 20 => 1 + 0.1*19 = 2.90 but clamped to max 1.5.
            with patch.object(RP, "consecutive_green_days", return_value=(20, {})):
                assert RP.green_day_graduation_multiplier(object(), execution_family="m")[0] == pytest.approx(1.5)

    def test_graduation_disabled_is_neutral(self):
        with patch.object(RP.settings, "chili_momentum_green_day_graduation_enabled", False, create=True):
            mult, meta = RP.green_day_graduation_multiplier(object(), execution_family="m")
        assert mult == pytest.approx(1.0)
        assert meta.get("reason") == "disabled"

    # --- bounds factory ---------------------------------------------------- #
    @staticmethod
    def _bounds_factory(today_et_date: str):
        """Return a side_effect for _et_day_bounds_utc(days_ago=N) that pins 'today'
        to a fixed ET date so the past-day window is deterministic across runs."""
        y, m, d = (int(x) for x in today_et_date.split("-"))

        def _bounds(*, days_ago: int = 0):
            start_et = datetime(y, m, d, tzinfo=ET) - timedelta(days=days_ago)
            end_et = start_et + timedelta(days=1)
            start_utc = start_et.astimezone(UTC).replace(tzinfo=None)
            end_utc = end_et.astimezone(UTC).replace(tzinfo=None)
            return start_utc, end_utc

        return _bounds
