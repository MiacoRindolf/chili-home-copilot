"""Each replay-bench invariant must REJECT its known violation and ACCEPT the good case.

An invariant that only ever sees the good case is not an invariant, it is a comment. Every
test below names the run that was WRONG AND LOOKED RIGHT, and asserts the guard would have
stopped it.

DB-free by construction: ``scripts/replay_harness_invariants`` imports stdlib only, so this
file needs no database, no ``app.config``, and no DATABASE_URL.

Runnable: pytest tests/test_replay_harness_invariants.py -v
"""
from __future__ import annotations

import pathlib
import sys
import textwrap

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import replay_harness_invariants as inv  # noqa: E402


# ─── 1. DENSE STRIDE ─────────────────────────────────────────────────────────────────────

def test_stride_10_is_rejected_for_an_exit_question():
    """⚠️ MEASURED 2026-08-28: the SAME symbol/window replayed +$193.92 at stride 1 and
    -$4.66 at stride 10. The published "exit churn" finding was the downsampling."""
    with pytest.raises(AssertionError, match=r"193\.92"):
        inv.assert_dense_stride(10, "does the exit ladder churn?")


@pytest.mark.parametrize("question", ["exit ladder A/B", "order flow read", "rossbench scoring"])
def test_every_dense_question_kind_is_policed(question):
    with pytest.raises(AssertionError):
        inv.assert_dense_stride(8, question)


@pytest.mark.parametrize("stride", [1, 2])
def test_dense_strides_are_accepted(stride):
    inv.assert_dense_stride(stride, "exit ladder A/B")


def test_an_undeclared_question_asserts_nothing():
    """The pre-existing A/B callers declare no question and must stay byte-identical."""
    inv.assert_dense_stride(8, "")
    inv.assert_dense_stride(10, None)


def test_a_non_dense_question_may_downsample():
    inv.assert_dense_stride(8, "does the arm fire at all?")


def test_a_non_integer_stride_is_rejected_not_ignored():
    with pytest.raises(AssertionError):
        inv.assert_dense_stride("dense", "exit ladder A/B")


# ─── 2. CLEAN SINK ───────────────────────────────────────────────────────────────────────

def test_the_documented_contamination_shortcut_is_rejected():
    """⚠️ MEASURED 2026-08-29: a REUSED sink moved a MIMI baseline +60.60 -> +46.59 with
    no code change, and nearly rejected the correct 0.6R-rung experiment on a ghost kill."""
    with pytest.raises(AssertionError, match=r"60\.60"):
        inv.assert_clean_sink({"REPLAY_KEEP_SINK": "1"})


@pytest.mark.parametrize("env", [{}, {"REPLAY_KEEP_SINK": "0"}, {"REPLAY_KEEP_SINK": ""}])
def test_a_clean_sink_is_accepted(env):
    inv.assert_clean_sink(env)


def test_any_truthy_value_counts_not_just_one():
    with pytest.raises(AssertionError):
        inv.assert_clean_sink({"REPLAY_KEEP_SINK": "true"})


# ─── 3. INTERLEAVING ─────────────────────────────────────────────────────────────────────

def test_all_a_then_all_b_is_rejected():
    """⚠️ The live source DB drifts under the 120-hour frame warm-up, so an arm-blocked
    plan hands that drift to ONE arm and calls it a result."""
    bad = [("MIMI", "A"), ("GELS", "A"), ("MIMI", "B"), ("GELS", "B")]
    with pytest.raises(AssertionError, match="SPLIT"):
        inv.assert_interleaved(bad)


def test_interleave_builds_a_plan_that_passes():
    plan = inv.interleave(["MIMI", "GELS", "JLHL"], ["A", "B"])
    assert plan[:2] == [("MIMI", "A"), ("MIMI", "B")]
    inv.assert_interleaved(plan)


def test_a_case_missing_an_arm_is_rejected():
    with pytest.raises(AssertionError, match="every window must run every arm"):
        inv.assert_interleaved([("MIMI", "A"), ("MIMI", "B"), ("GELS", "A")])


def test_a_case_repeating_an_arm_is_rejected():
    with pytest.raises(AssertionError, match="repeats an arm"):
        inv.assert_interleaved([("MIMI", "A"), ("MIMI", "A")])


def test_a_single_case_is_fine():
    inv.assert_interleaved(inv.interleave(["MIMI"], ["A", "B"]))


def test_interleave_refuses_duplicate_arms():
    with pytest.raises(AssertionError, match="duplicate arms"):
        inv.interleave(["MIMI"], ["A", "A"])


# ─── 4. AS-OF READS ──────────────────────────────────────────────────────────────────────

_GOOD_ASOF = textwrap.dedent(
    """
    class AsOfProvider:
        def __call__(self, ticker, *, interval="1d", period="6mo"):
            now = _naive(lr._utcnow())
            sl = self._t[self._t.index <= now]
            return sl

    def run_arm():
        _mp.schedule_window_now = lambda now=None, _o=_o: _o(now or lr._utcnow())
        def _staf_simclock(symbol, *, as_of=None, _o=_orig):
            return _o(symbol, as_of=(as_of if as_of is not None else lr._utcnow()))
        _eg.signed_tape_accel_features = _staf_simclock
        _eg._utcnow_for_bars = lambda sample: lr._utcnow()
        def _nsp_simclock(db, symbol, *, now_utc=None, _o=_orig_nsp, **k):
            return _o(db, symbol, now_utc=(now_utc if now_utc is not None else lr._utcnow()), **k)
        _scv.name_spread_percentiles = _nsp_simclock
    """
)


def test_the_good_driver_shape_is_accepted():
    inv.assert_as_of_reads(_GOOD_ASOF)


def test_a_wall_clock_as_of_slice_is_rejected():
    """⚠️ With a trailing real-now() read the MIRRORED tape looks EMPTY, so every gate
    that reads it fails closed and the replay measures silence."""
    bad = _GOOD_ASOF.replace("now = _naive(lr._utcnow())", "now = datetime.now()")
    with pytest.raises(AssertionError):
        inv.assert_as_of_reads(bad)


def test_an_unbounded_slice_is_rejected():
    """No `<= now` bound means the replay LOOKS AHEAD past the replayed instant."""
    bad = _GOOD_ASOF.replace("self._t[self._t.index <= now]", "self._t")
    with pytest.raises(AssertionError, match="LOOKS AHEAD"):
        inv.assert_as_of_reads(bad)


@pytest.mark.parametrize("anchor", inv.REQUIRED_SIM_CLOCK_ANCHORS)
def test_every_sim_clock_anchor_is_required(anchor):
    """Losing ONE re-point silently degrades a whole gate family to 'reads empty'."""
    lines = [ln for ln in _GOOD_ASOF.splitlines() if f".{anchor} =" not in ln]
    with pytest.raises(AssertionError, match=anchor):
        inv.assert_as_of_reads("\n".join(lines))


def test_the_real_driver_passes():
    src = (_SCRIPTS / "replay_v3_fsm_window.py").read_text(encoding="utf-8")
    inv.assert_as_of_reads(src)


# ─── 5. TIE-STABLE TAPE SQL ──────────────────────────────────────────────────────────────

_TIE_BAD = 'SQL = "SELECT observed_at FROM iqfeed_trade_ticks WHERE symbol=:s ORDER BY observed_at ASC"'
_TIE_GOOD = ('SQL = "SELECT observed_at, id FROM iqfeed_trade_ticks WHERE symbol=:s '
             'ORDER BY observed_at ASC, id ASC"')


def test_an_unstable_tape_select_is_rejected():
    """⚠️ Rows sharing an observed_at came back in PHYSICAL SCAN ORDER, so the same window
    mirrored differently and filled differently — a heap-layout delta read as an A/B delta."""
    with pytest.raises(AssertionError, match="tie-stable"):
        inv.assert_tie_stable_sql(_TIE_BAD)


def test_a_tie_stable_tape_select_is_accepted():
    inv.assert_tie_stable_sql(_TIE_GOOD)


@pytest.mark.parametrize("table", inv.TAPE_TABLES)
def test_every_tape_relation_is_policed(table):
    bad = f'SQL = "SELECT observed_at FROM {table} WHERE symbol=%s ORDER BY observed_at ASC"'
    with pytest.raises(AssertionError):
        inv.assert_tie_stable_sql(bad)


def test_a_delete_is_not_a_select():
    """The sink cleanup DELETEs by source; it has no ordering to be unstable about."""
    inv.assert_tie_stable_sql(
        'SQL = "DELETE FROM iqfeed_trade_ticks WHERE source=\'replay_v3\' AND symbol=:s"')


def test_a_non_tape_select_is_left_alone():
    inv.assert_tie_stable_sql('SQL = "SELECT count(*) FROM trading_automation_events"')


def test_the_real_driver_is_tie_stable():
    src = (_SCRIPTS / "replay_v3_fsm_window.py").read_text(encoding="utf-8")
    inv.assert_tie_stable_sql(src)


# ─── 6. TREE VERIFICATION ────────────────────────────────────────────────────────────────

@pytest.fixture()
def build_tree(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "driver.py").write_text(
        "def mirror_nbbo_streaming():\n    pass\n", encoding="utf-8", newline="")
    return tmp_path


def test_a_stale_build_tree_is_rejected(build_tree):
    """⚠️ A stale local branch once produced a confident 'the fix works' result for code
    that was never in the run."""
    with pytest.raises(AssertionError, match="stale build tree"):
        inv.verify_tree(str(build_tree), "deadbeef" * 5, "scripts/driver.py",
                        "mirror_nbbo_streaming", head_reader=lambda _d: "cafe" * 10)


def test_a_matching_tree_with_the_sentinel_is_accepted(build_tree):
    head = "a" * 40
    assert inv.verify_tree(str(build_tree), head, "scripts/driver.py",
                           "mirror_nbbo_streaming", head_reader=lambda _d: head) == head


def test_a_short_ref_matches_the_full_head(build_tree):
    head = "0784877386795d243cb1b0290da975cf72c1b3f6"
    inv.verify_tree(str(build_tree), head[:9], "scripts/driver.py",
                    "mirror_nbbo_streaming", head_reader=lambda _d: head)


def test_the_right_head_with_a_missing_sentinel_is_rejected(build_tree):
    """HEAD can match while the working tree does not carry the change under test."""
    head = "b" * 40
    with pytest.raises(AssertionError, match="ABSENT"):
        inv.verify_tree(str(build_tree), head, "scripts/driver.py",
                        "mirror_depth_streaming_v2", head_reader=lambda _d: head)


def test_a_missing_sentinel_file_is_rejected(build_tree):
    head = "c" * 40
    with pytest.raises(AssertionError, match="does not exist"):
        inv.verify_tree(str(build_tree), head, "scripts/gone.py", "x",
                        head_reader=lambda _d: head)


# ─── 7. COLD-START TAGS ──────────────────────────────────────────────────────────────────

_EVENTS = [
    {"ts": "2026-09-02T13:30:01", "event_type": "live_entry_candidate_detected", "ticks_seen": 1},
    {"ts": "2026-09-02T13:30:02", "event_type": "live_entry_blocked", "ticks_seen": 2},
    {"ts": "2026-09-02T13:45:00", "event_type": "live_entry_filled", "ticks_seen": 900},
    {"ts": "2026-09-02T13:30:03", "event_type": "live_watch_started", "ticks_seen": 1},
]


def test_a_decision_on_the_first_grid_step_is_tagged():
    """⚠️ #1287 A/B (2026-09-02): the ONLY delta across four windows was a cold-start seed
    that armed on the FIRST grid step against a frame HOD inherited from the warm-up, and
    blocked a +1.515R entry."""
    tags = cold = inv.cold_start_tags(_EVENTS, "2026-09-02T13:30:00", 50, 60.0)
    assert {t["event_type"] for t in cold} == {
        "live_entry_candidate_detected", "live_entry_blocked"}
    assert all("uptime_" in r for t in tags for r in t["reasons"] if r.startswith("uptime"))


def test_a_warm_decision_is_not_tagged():
    cold = inv.cold_start_tags(_EVENTS, "2026-09-02T13:30:00", 50, 60.0)
    assert "live_entry_filled" not in {t["event_type"] for t in cold}


def test_non_decisive_events_are_never_tagged():
    cold = inv.cold_start_tags(_EVENTS, "2026-09-02T13:30:00", 50, 60.0)
    assert "live_watch_started" not in {t["event_type"] for t in cold}


def test_the_tick_floor_fires_independently_of_uptime():
    evs = [{"ts": "2026-09-02T14:00:00", "event_type": "live_entry_submitted", "ticks_seen": 3}]
    tags = inv.cold_start_tags(evs, "2026-09-02T13:00:00", 50, 60.0)
    assert len(tags) == 1 and tags[0]["reasons"] == ["ticks_3_lt_50"]


def test_it_never_raises_on_junk():
    assert inv.cold_start_tags([{"event_type": "live_entry_filled"}], None, 5, 5.0) == []
    assert inv.cold_start_tags([], "2026-09-02T13:30:00", 5, 5.0) == []


# ─── 8. ADDITIVE COUNTS ──────────────────────────────────────────────────────────────────

def test_growing_counts_are_rejected():
    """⚠️ 2026-08-29: post-run counts that grew run-over-run were the visible surface of
    the sink contamination."""
    with pytest.raises(AssertionError, match=r"trading_automation_events: 412 -> 824"):
        inv.additive_count_check({"trading_automation_events": 412},
                                 {"trading_automation_events": 824})


def test_identical_counts_are_accepted():
    inv.additive_count_check({"trading_automation_events": 412, "adaptive_risk_reservations": 3},
                             {"trading_automation_events": 412, "adaptive_risk_reservations": 3})


def test_a_smaller_second_run_is_not_additive():
    inv.additive_count_check({"trading_automation_events": 412},
                             {"trading_automation_events": 400})


def test_a_key_absent_from_the_previous_run_is_ignored():
    inv.additive_count_check({}, {"trading_automation_events": 412})


# ─── 9. MOCK PARITY ──────────────────────────────────────────────────────────────────────

class _Mock:
    def __init__(self, **kw):
        cfg = dict(inv.REQUIRED_MOCK_CONFIG)
        cfg.update(kw)
        for k, v in cfg.items():
            setattr(self, f"_{k}", v)


def test_the_validated_parity_config_is_accepted():
    inv.assert_mock_parity(_Mock())


def test_resting_limit_fills_off_is_rejected():
    """⚠️ resting_limit_fills=False caused the exit-ladder submit SPAM."""
    with pytest.raises(AssertionError, match="resting_limit_fills"):
        inv.assert_mock_parity(_Mock(resting_limit_fills=False))


@pytest.mark.parametrize("key,bad", [
    ("volume_cap_enabled", False),
    ("fill_mode", "optimistic"),
    ("freshness_mode", "sim"),
])
def test_every_parity_knob_is_policed(key, bad):
    with pytest.raises(AssertionError, match=key):
        inv.assert_mock_parity(_Mock(**{key: bad}))


def test_fill_mode_is_compared_case_insensitively():
    inv.assert_mock_parity(_Mock(fill_mode="CONSERVATIVE"))


def test_a_mock_missing_a_knob_is_rejected_not_skipped():
    class _Bare:
        pass
    with pytest.raises(AssertionError, match="exposes no"):
        inv.assert_mock_parity(_Bare())
