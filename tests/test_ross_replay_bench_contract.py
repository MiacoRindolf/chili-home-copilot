"""The bench's env contract is the experiment. These tests are the contract.

WHY A CONTRACT TEST AND NOT A RUN TEST. ``scripts/ross_replay_bench.py`` spends 30-45
minutes per window in a subprocess against a live tape, so nothing here starts one. What
IS cheap — and what has actually gone wrong in this project — is the handful of strings
the bench hands the driver:

  * ``EXEC_FAMILY`` was set by the nightly report for weeks while the driver's seed site
    hard-coded ``robinhood_agentic_mcp``, so every "Alpaca" replay ran the Robinhood
    agentic fill path (replay_v3_fsm_window.py:187-192). A knob that does not bind is
    worse than a missing knob: the report names a rail that never ran.
  * ``REPLAY_KEEP_SINK`` left over in a shell moved a MIMI baseline +60.60 -> +46.59 with
    no code change (2026-08-29 sink contamination).
  * an all-A-then-all-B plan measures the source DB's drift under the 120-hour frame
    warm-up and prints it under the treatment's name.
  * and the Ross ledger is stamped ``evidence_role: after_fact_grading_only``
    (build_ross_manifest.py:319) — a run that can read the answer is not a measurement.

So: the exact key set, the exact values, the interleaved order, and the two fences.

Runnable: pytest tests/test_ross_replay_bench_contract.py -v
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "ross_replay_bench.py"
_DRIVER = _REPO / "scripts" / "replay_v3_fsm_window.py"


def _load():
    """Import the bench module by path (same shape as
    tests/test_nightly_replay_loop_actually_runs.py:59-67)."""
    spec = importlib.util.spec_from_file_location("_ross_bench_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ross_bench_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


B = _load()


# ── fixtures ─────────────────────────────────────────────────────────────────

CASE = B.Case(
    symbol="TMCR", date="2026-08-24",
    win_start="2026-08-24T13:26:00", win_end="2026-08-24T14:26:00",
    anchor_source="manifest_et_clock:leading", anchor_detail="entry_time_et='9:41 ...'",
)
CASE2 = B.Case(
    symbol="DAIC", date="2026-08-26",
    win_start="2026-08-26T13:00:00", win_end="2026-08-26T14:30:00",
    anchor_source="manifest_window_utc", anchor_detail="win_start/win_end",
)

_CONTRACT_KWARGS = dict(
    build="E:/dev/wt-bench",
    source="postgresql://chili:chili@localhost:5433/chili",
    sink="postgresql://chili:chili@localhost:5433/chili_bench_test",
    equity=30000.0, risk=900.0, tick_stride=1, grid_step_s=1.0,
    exec_family="alpaca_spot", frame_warmup_min=7200.0,
    json_out="E:/out/TMCR_2026-08-24/base/run.json",
)


# The certified paper account every Alpaca run must carry (build_env refuses without it,
# see test_an_alpaca_run_refuses_without_the_certified_account_id). Merged into the parent
# by default so the rest of this file keeps exercising alpaca_spot under a VALID config;
# a test that wants to see the refusal opts out with certified=False.
CERTIFIED_PAPER_ACCOUNT = "c7d421e0-4fae-4219-9503-5ce051d4d923"
# The lane's launch env for the Alpaca family (timeshare_supervisor.py:88): the legacy
# sizing escape. Required by build_env for the same reason the account id is.
LANE_ALPACA_ENV = {
    "CHILI_ALPACA_EXPECTED_ACCOUNT_ID": CERTIFIED_PAPER_ACCOUNT,
    "CHILI_MOMENTUM_LEGACY_ALPACA_DISPATCH_ENABLED": "true",
}


def _env(parent=None, arm=None, *, certified=True):
    merged = dict(parent) if parent is not None else {}
    if certified:
        for key, value in LANE_ALPACA_ENV.items():
            merged.setdefault(key, value)
    env, _dropped = B.build_env(
        case=CASE, arm=(arm or B.Arm("base")), parent=merged, **_CONTRACT_KWARGS
    )
    return env


def _arm_file(tmp_path, name, payload):
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


# ── the exact env contract ───────────────────────────────────────────────────

def test_the_contract_env_is_exactly_the_declared_key_set():
    """No key may appear on the driver's env by accident, and none may go missing.

    ``contract_env`` is the seam that carries ONLY the contract, so set equality here is a
    real statement — ``build_env`` layers it over an inherited parent env where set
    equality would be meaningless."""
    assert set(B.contract_env(case=CASE, **_CONTRACT_KWARGS)) == B.CONTRACT_ENV_KEYS


def test_every_contract_key_is_actually_read_by_the_driver():
    """A key nothing reads is decoration; a key that does not bind is a false report.

    PYTHONPATH is read by the interpreter itself and CHILI_PYTEST by the app
    (app/main.py:55), so neither appears in the driver source — everything else must."""
    src = _DRIVER.read_text(encoding="utf-8")
    exempt = {"PYTHONPATH", "CHILI_PYTEST"}
    missing = [k for k in sorted(B.CONTRACT_ENV_KEYS - exempt) if k not in src]
    assert not missing, f"the driver never reads: {missing}"


def test_the_ohlcv_start_equals_the_window_start():
    """The frame depth comes from FRAME_WARMUP_MIN, which deepens the OHLCV seam ONLY
    while the tick mirror and sim grid stay window-bound (replay_v3_fsm_window.py:157-167).
    Reaching back with OHLCV_START instead would move the mirror too."""
    env = _env()
    assert env["OHLCV_START"] == env["WIN_START"] == CASE.win_start


def test_the_driver_arm_is_pinned_to_a_single_run():
    """ARM=both writes TWO receipts under mangled filenames (replay_v3_fsm_window.py:809-817),
    so the bench would find no run.json where it put one."""
    assert B.DRIVER_ARM_VALUE in ("on", "off")
    assert _env()["ARM"] == B.DRIVER_ARM_VALUE
    src = _DRIVER.read_text(encoding="utf-8")
    assert 'in ("on", "off")' in src and "return REPLAY_JSON_OUT" in src, (
        "the driver no longer writes REPLAY_JSON_OUT verbatim for a single-arm run; "
        "re-read _json_out_path before trusting <case>/<arm>/run.json"
    )


def test_the_receipt_path_is_the_run_json_the_bench_reads_back():
    assert _env()["REPLAY_JSON_OUT"].endswith("run.json")


def test_the_tape_provenance_filter_is_the_four_hydrated_providers():
    """Without a provenance predicate a symbol-day hydrated from two providers returns BOTH
    tapes concatenated — measured TMCR 2026-08-24: 16,933 + 16,933 = 33,866 ticks, every
    print twice, nothing about the rows malformed (replay_v3_fsm_window.py:176-185)."""
    assert _env()["SOURCE_FILTER"].split(",") == [
        "iqfeed_lookup_hist", "iqfeed_lookup_bbo", "polygon_v3_trades", "polygon_v3_quotes",
    ]


def test_the_diagnostics_and_the_full_mirror_are_on():
    """FULL_MIRROR=0 downsamples the trade tape the FSM's cadence and 5m higher-low read
    (replay_v3_fsm_window.py:24-25, :994); the diag flags are what make the receipt's
    entry-decision evidence non-empty."""
    env = _env()
    assert env["FULL_MIRROR"] == "1"
    assert env["DIAG"] == "1" and env["ENTRY_DIAG"] == "1"
    assert env["CHILI_PYTEST"] == "1"


def test_the_bench_question_is_not_exported_into_the_driver_env():
    """The density floor is enforced HERE, before any subprocess starts. Exporting
    BENCH_QUESTION would widen the contract without widening what is checked."""
    assert "BENCH_QUESTION" not in B.CONTRACT_ENV_KEYS
    assert "BENCH_QUESTION" not in _env()


# ── fence: no hindsight, no kept sink ────────────────────────────────────────

def test_no_ROSS_key_from_the_parent_shell_ever_reaches_the_driver():
    """The ledger is after-the-fact grading evidence. Whoever runs this bench plausibly has
    it exported; the parent env is SANITISED rather than merely not-added-to."""
    parent = {"PATH": "/usr/bin", "ROSS_MASTER_LEDGER": "…/ross_master_ledger.json",
              "ROSS_PNL_USD": "27000"}
    env = _env(parent=parent)
    assert [k for k in env if k.startswith("ROSS_")] == []
    assert env["PATH"] == "/usr/bin", "the sanitiser must not eat the ordinary environment"


def test_no_ROSSBENCH_key_from_the_parent_shell_reaches_the_driver_either():
    """The fence as first written was narrower than it read. ``ROSSBENCH_PIN_HALFWIDTH_S`` is
    a real bench-side knob — rossbench_pin_ross_events.py:1695 takes it as the default for
    --halfwidth-s, which sets how wide a tape slice a pin is searched in — and a "ROSS_"
    prefix test does not match it, so it survived into the driver's environment. The driver
    reads no ROSSBENCH_* key today (grep of replay_v3_fsm_window.py: 0 hits), so nothing
    misbehaved; the point is that the bench's own knobs must not be able to become the
    driver's."""
    env = _env(parent={"ROSSBENCH_PIN_HALFWIDTH_S": "600", "PATH": "/usr/bin"})
    assert [k for k in env if k.startswith("ROSSBENCH_")] == []
    assert env["PATH"] == "/usr/bin"
    assert "ROSSBENCH_" in B.FORBIDDEN_ENV_PREFIXES


def test_a_bench_side_knob_in_an_arm_file_is_refused_with_its_own_reason(tmp_path):
    """Two different reasons behind one fence: a ROSS_* key is hindsight, a ROSSBENCH_* key
    is the bench configuring the experiment it is measuring. Naming the wrong one sends the
    operator looking in the wrong place."""
    path = _arm_file(tmp_path, "bad", {"ROSSBENCH_PIN_HALFWIDTH_S": "600"})
    with pytest.raises(SystemExit) as exc:
        B.parse_arm_spec(f"base,bad={path}", base_dir=str(tmp_path))
    assert "configure the BENCH side" in str(exc.value)
    assert "after-the-fact" not in str(exc.value)


def test_REPLAY_KEEP_SINK_is_stripped_from_the_parent_env():
    """Measured 2026-08-29: a reused sink moved a baseline +60.60 -> +46.59 with no code
    change, and nearly rejected a correct experiment on a ghost kill inherited from a
    prior run."""
    env = _env(parent={"REPLAY_KEEP_SINK": "1"})
    assert B.KEEP_SINK_ENV not in env
    # and the invariant itself agrees — build_env calls assert_clean_sink on the result
    from replay_harness_invariants import assert_clean_sink
    assert_clean_sink(env)


def test_the_sanitiser_reports_exactly_what_it_dropped():
    """A silent strip is a dark flag. bench.json records the names."""
    _e, dropped = B.build_env(
        case=CASE, arm=B.Arm("base"),
        # The certified id rides along (an Alpaca run refuses without it) and, being an
        # ambient CHILI_* key, must NOT appear in `dropped` — which is the point of the test.
        parent={"ROSS_X": "1", "REPLAY_KEEP_SINK": "1", "PATH": "/usr/bin", **LANE_ALPACA_ENV},
        **_CONTRACT_KWARGS,
    )
    assert dropped == ["REPLAY_KEEP_SINK", "ROSS_X"]


def test_a_stale_ambient_SYMBOL_cannot_survive_into_the_run():
    """Order matters: the contract is applied AFTER the parent env."""
    assert _env(parent={"SYMBOL": "STALE", "WIN_END": "2026-01-01T00:00:00"})["SYMBOL"] == "TMCR"
    assert _env(parent={"WIN_END": "2026-01-01T00:00:00"})["WIN_END"] == CASE.win_end


def test_ambient_CHILI_flags_are_kept_but_visible():
    """Deliberate asymmetry. Ambient CHILI_* flags are part of the deployed configuration
    the lane runs under, so stripping them would bench a lane that does not exist — but an
    unrecorded one is a dark flag, so main() records every one of them in bench.json."""
    env = _env(parent={"CHILI_MOMENTUM_SOMETHING": "1"})
    assert env["CHILI_MOMENTUM_SOMETHING"] == "1"
    src = _SCRIPT.read_text(encoding="utf-8")
    assert 'ambient_chili_env' in src and 'startswith("CHILI_")' in src


# ── fence: an arm flips behaviour, never identity ────────────────────────────

def test_an_arm_override_reaches_the_driver(tmp_path):
    path = _arm_file(tmp_path, "sticky_off",
                     {"CHILI_MOMENTUM_STICKY_BACKSIDE_BENCH_ENABLED": "0"})
    arms = B.parse_arm_spec(f"base,sticky_off={path}", base_dir=str(tmp_path))
    assert [a.name for a in arms] == ["base", "sticky_off"]
    assert arms[0].overrides == {}
    env = _env(arm=arms[1])
    assert env["CHILI_MOMENTUM_STICKY_BACKSIDE_BENCH_ENABLED"] == "0"


@pytest.mark.parametrize("key", ["WIN_END", "TICK_STRIDE", "EQUITY", "SOURCE_FILTER", "ARM"])
def test_an_arm_may_not_move_the_experiment_itself(tmp_path, key):
    """An arm that quietly moved the window, the tape, the sizing or the rail would produce
    a delta that reads as a treatment effect and is really a different experiment."""
    path = _arm_file(tmp_path, "sneaky", {key: "1"})
    with pytest.raises(SystemExit) as exc:
        B.parse_arm_spec(f"base,sneaky={path}", base_dir=str(tmp_path))
    assert key in str(exc.value)


@pytest.mark.parametrize("payload", [{"ROSS_PNL_USD": "27000"}, {"REPLAY_KEEP_SINK": "1"}])
def test_an_arm_file_carrying_a_forbidden_key_is_refused_at_load(tmp_path, payload):
    """Refused at LOAD time, before the sink is reset — not after an hour of replay."""
    path = _arm_file(tmp_path, "bad", payload)
    with pytest.raises(SystemExit):
        B.parse_arm_spec(f"base,bad={path}", base_dir=str(tmp_path))


def test_an_arm_file_must_be_a_flat_object(tmp_path):
    with pytest.raises(SystemExit):
        B.parse_arm_spec(f"base,x={_arm_file(tmp_path, 'a', ['not', 'an', 'object'])}",
                         base_dir=str(tmp_path))
    with pytest.raises(SystemExit):
        B.parse_arm_spec(f"base,x={_arm_file(tmp_path, 'b', {'K': {'nested': 1}})}",
                         base_dir=str(tmp_path))


def test_duplicate_arm_names_are_refused(tmp_path):
    path = _arm_file(tmp_path, "one", {"CHILI_X": "1"})
    with pytest.raises(SystemExit):
        B.parse_arm_spec(f"a={path},a={path}", base_dir=str(tmp_path))


def test_an_arm_name_must_be_a_safe_directory_segment(tmp_path):
    path = _arm_file(tmp_path, "one", {"CHILI_X": "1"})
    with pytest.raises(SystemExit):
        B.parse_arm_spec(f"base,../escape={path}", base_dir=str(tmp_path))


# ── fence: interleaving ──────────────────────────────────────────────────────

def test_the_plan_is_case_major_every_arm_before_the_next_window():
    """The source DB drifts under the 120-hour frame warm-up, so an all-A-then-all-B plan
    hands that drift to one arm only."""
    arms = [B.Arm("base"), B.Arm("treat", {"CHILI_X": "1"})]
    plan = B.build_plan([CASE, CASE2], arms)
    assert [(str(c), str(a)) for (c, a) in plan] == [
        ("TMCR:2026-08-24", "base"), ("TMCR:2026-08-24", "treat"),
        ("DAIC:2026-08-26", "base"), ("DAIC:2026-08-26", "treat"),
    ]


def test_the_interleave_assertion_has_teeth():
    """Proves the previous test is testing something: the arm-blocked shape must FAIL."""
    from replay_harness_invariants import assert_interleaved
    base, treat = B.Arm("base"), B.Arm("treat", {"CHILI_X": "1"})
    blocked = [(CASE, base), (CASE2, base), (CASE, treat), (CASE2, treat)]
    with pytest.raises(AssertionError):
        assert_interleaved(blocked)


# ── fence: density ───────────────────────────────────────────────────────────

def test_the_default_stride_keeps_every_print():
    """Measured 2026-08-28: the same window replayed +$193.92 at stride 1 and -$4.66 at
    stride 10, so a published 'exit churn' finding was the stride, not the strategy."""
    from replay_harness_invariants import DENSE_STRIDE_MAX, assert_dense_stride
    assert B.DEFAULT_TICK_STRIDE == 1
    assert B.DEFAULT_TICK_STRIDE <= DENSE_STRIDE_MAX
    assert_dense_stride(B.DEFAULT_TICK_STRIDE, B.BENCH_QUESTION)  # must not raise


def test_the_bench_question_arms_the_density_floor():
    """``assert_dense_stride`` only bites on an exit/flow/bench question — this bench's
    question must be one, or the floor is decorative."""
    from replay_harness_invariants import assert_dense_stride
    with pytest.raises(AssertionError):
        assert_dense_stride(10, B.BENCH_QUESTION)


# ── the required flags ───────────────────────────────────────────────────────

_MIN_ARGV = [
    "--manifest", "m.json", "--cases", "TMCR:2026-08-24",
    "--build", "E:/dev/wt-bench", "--ref", "078487738",
    "--source", "postgresql://u@h/chili", "--sink", "postgresql://u@h/b_test",
    "--out-dir", "E:/out",
    "--equity", "30000", "--risk", "900",
    "--grid-step-s", "1.0", "--exec-family", "alpaca_spot", "--timeout-s", "5400",
]


def test_the_minimal_argv_parses():
    args = B._build_parser().parse_args(_MIN_ARGV)
    assert args.equity == 30000.0 and args.risk == 900.0
    assert args.tick_stride == B.DEFAULT_TICK_STRIDE
    assert args.lead_s is None and args.lag_s is None


@pytest.mark.parametrize("flag", ["--equity", "--risk", "--grid-step-s",
                                  "--exec-family", "--timeout-s",
                                  "--build", "--ref", "--source", "--sink",
                                  "--out-dir", "--manifest", "--cases"])
def test_the_measurement_knobs_have_no_silent_default(flag):
    """⚠️ ``--equity``/``--risk`` above all: two different size canons exist in this project
    and mixing them has already produced a false regression. A default would make the
    mix-up invisible."""
    argv = list(_MIN_ARGV)
    i = argv.index(flag)
    del argv[i:i + 2]
    with pytest.raises(SystemExit):
        B._build_parser().parse_args(argv)


def test_there_is_no_env_var_configuration_of_the_bench_itself(monkeypatch):
    """argparse only. An env-configured bench inherits whatever the last manual run left in
    the shell, which is the same class of defect as the kept sink."""
    for name in ("EQUITY", "RISK", "SYMBOL", "TICK_STRIDE", "CHILI_REPLAY_BUILD"):
        monkeypatch.setenv(name, "999")
    args = B._build_parser().parse_args(_MIN_ARGV)
    assert args.equity == 30000.0 and args.tick_stride == B.DEFAULT_TICK_STRIDE


# ── cases, windows, and the narrative clock ──────────────────────────────────

def test_the_case_spec_refuses_anything_that_is_not_symbol_colon_date():
    assert B.parse_case_spec("TMCR:2026-08-24, DAIC:2026-08-26") == [
        ("TMCR", "2026-08-24", None), ("DAIC", "2026-08-26", None)]
    for bad in ("TMCR", "TMCR:24-08-2026", "", "TMCR:2026-08-24,TMCR:2026-08-24",
                "TMCR:2026-08-24:"):
        with pytest.raises(SystemExit):
            B.parse_case_spec(bad)


def test_the_case_spec_takes_a_manifest_id_whose_own_separator_is_a_colon():
    """``--cases SYMBOL:DATE:<manifest_id>`` is the answer to a symbol-day with several
    windows, and layer 4 builds ids as ``<video>::<symbol>::<date>::ml<n>``
    (build_ross_manifest.py:644) — so the id itself is full of colons and the split must be
    ``maxsplit=2``, not a count check."""
    assert B.parse_case_spec("ILLR:2026-06-25:A7Gnw1CMExI::ILLR::2026-06-25::ml1") == [
        ("ILLR", "2026-06-25", "A7Gnw1CMExI::ILLR::2026-06-25::ml1")]
    # two waves of the same symbol-day are two distinct cases, not a duplicate
    assert len(B.parse_case_spec(
        "ILLR:2026-06-25:A::ml1,ILLR:2026-06-25:A::ml3")) == 2


def test_the_case_directory_name_carries_no_colon():
    """Windows is the only platform this project runs on and ':' is illegal in a filename."""
    assert ":" not in CASE.dirname
    assert CASE.dirname == "TMCR_2026-08-24"


def test_an_implicitly_resolved_case_keeps_its_plain_directory_name():
    """A symbol-day with ONE manifest row needs no selector, and decorating its directory
    anyway would churn every existing output tree and every doc that names one."""
    case = B.Case(symbol="TMCR", date="2026-08-24", win_start="x", win_end="y",
                  anchor_source="s", anchor_detail="d",
                  manifest_id="vid::TMCR::2026-08-24::ml1", selector_explicit=False)
    assert case.dirname == "TMCR_2026-08-24"
    assert str(case) == "TMCR:2026-08-24"


def test_two_windows_of_one_symbol_day_get_distinct_identities_and_directories():
    """ILLR 2026-06-25 is five manifest rows. Two of them benched in one run must not share
    ``str(case)`` — ``interleave`` de-duplicates on it
    (replay_harness_invariants.py:111) — nor a directory, which would have the second run's
    receipt overwrite the first's."""
    def mk(mid):
        return B.Case(symbol="ILLR", date="2026-06-25", win_start="x", win_end="y",
                      anchor_source="s", anchor_detail="d",
                      manifest_id=mid, selector_explicit=True)
    a = mk("A7Gnw1CMExI::ILLR::2026-06-25::ml1")
    b = mk("A7Gnw1CMExI::ILLR::2026-06-25::ml3")
    assert str(a) != str(b)
    assert a.dirname != b.dirname
    assert a.dirname == "ILLR@A7Gnw1CMExI-ml1_2026-06-25"
    assert ":" not in a.dirname


def test_the_case_directory_still_ends_in_the_ET_day_the_reporter_parses():
    """rossbench_report.case_identity reads the ET trading day off the DIRECTORY NAME —
    ``case_name.replace("@","_").split("_")[-1]``, accepted only if it is a YYYY-MM-DD — and
    falls back to the receipt's naive-UTC WIN_START date when it is not. That fallback is
    the wrong day for any window crossing midnight Z, so the selector goes BEFORE the date
    and uses the '@' the reporter already normalises away."""
    case = B.Case(symbol="ILLR", date="2026-06-25", win_start="x", win_end="y",
                  anchor_source="s", anchor_detail="d",
                  manifest_id="A7Gnw1CMExI::ILLR::2026-06-25::ml1", selector_explicit=True)
    tail = case.dirname.replace("@", "_").split("_")[-1]
    assert tail == "2026-06-25"


@pytest.mark.parametrize("text,hour,minute,source", [
    ("9:41 HOD break", 9, 41, "leading"),
    ("~10:07 the flush", 10, 7, "leading"),
    ("he took it at 10:07 after the flush", 10, 7, "embedded"),
    ("1:15 afternoon push", 13, 15, "leading+pm_inferred"),
    ("7:45 pm", 19, 45, "leading"),
    ("12:30", 12, 30, "leading"),
])
def test_the_narrative_clock_parses_what_it_can(text, hour, minute, source):
    """``entry_time_et`` is PROSE: 132/157 rows start with a clock, 14 carry one
    mid-sentence. A bare 1:15 is 13:15 ET because 01:00-03:59 ET is outside the
    04:00-20:00 extended session — and the inference is recorded in the source tag, never
    hidden."""
    got_h, got_m, got_src, _matched = B.parse_et_clock(text)
    assert (got_h, got_m, got_src) == (hour, minute, source)


@pytest.mark.parametrize("text", [
    "premarket (clock time not stated; the squeeze began right after the two scalps)",
    "not stated (last trade of the day; he stopped after this give-back)",
    "",
    "25:00",
])
def test_a_row_with_no_usable_clock_is_refused_not_defaulted(text):
    """11 of 187 ledger rows carry no clock. Defaulting them onto the open would bench a
    window Ross was not in and score the miss against the lane."""
    assert B.parse_et_clock(text)[0] is None


def test_the_ET_day_converts_across_the_DST_boundary():
    """2026-03-04 is EST (UTC-5) and 2026-08-24 is EDT (UTC-4); a fixed offset would put
    every March window an hour wrong, which is a whole ignition."""
    assert B.et_clock_to_utc("2026-08-24", 9, 41).isoformat() == "2026-08-24T13:41:00"
    assert B.et_clock_to_utc("2026-03-04", 9, 41).isoformat() == "2026-03-04T14:41:00"


def test_an_explicit_manifest_window_wins_over_any_anchor():
    row = {"symbol": "DAIC", "date": "2026-08-26", "entry_time_et": "9:41",
           "win_start": "2026-08-26T13:00:00Z", "win_end": "2026-08-26T14:30:00"}
    case = B.resolve_window(row, symbol="DAIC", day="2026-08-26", lead_s=None, lag_s=None)
    assert (case.win_start, case.win_end) == ("2026-08-26T13:00:00", "2026-08-26T14:30:00")
    assert case.anchor_source == "manifest_window_utc"


def test_an_anchor_without_lead_and_lag_is_refused():
    """The window length decides what the FSM can ever see, so it is never defaulted."""
    row = {"symbol": "TMCR", "date": "2026-08-24", "entry_time_et": "9:41 HOD break"}
    with pytest.raises(SystemExit) as exc:
        B.resolve_window(row, symbol="TMCR", day="2026-08-24", lead_s=None, lag_s=None)
    assert "--lead-s" in str(exc.value)


def test_the_anchor_window_is_lead_plus_lag_around_the_entry():
    row = {"symbol": "TMCR", "date": "2026-08-24", "entry_time_et": "9:41 HOD break"}
    case = B.resolve_window(row, symbol="TMCR", day="2026-08-24", lead_s=900, lag_s=2700)
    assert case.win_start == "2026-08-24T13:26:00"   # 13:41Z - 15 min
    assert case.win_end == "2026-08-24T14:26:00"     # 13:41Z + 45 min


# ── which manifest row is this case? ─────────────────────────────────────────
#
# Since the master-ledger layer landed, one window per ledger LEG, a symbol-day is routinely
# several manifest rows: measured 2026-09-04 on the 418-window manifest built from the
# operator's evidence tree, 62 of 217 symbol-days carry more than one, and five of the eight
# lane-alive known-answer cases (ILLR 5, ZDAI 4, UPC 3, IPST 6, PFSA 2) could not be benched
# at all while symbol-day uniqueness was the only way in.

_AMBIGUOUS_DOC = {"windows": [
    {"manifest_id": "vid::VCIG::2026-03-04::ml1", "symbol": "VCIG", "date": "2026-03-04",
     "entry_time_et": "9:41"},
    {"manifest_id": "vid::VCIG::2026-03-04::ml2", "symbol": "VCIG", "date": "2026-03-04",
     "entry_time_et": "10:20"},
]}


def test_two_manifest_rows_for_one_symbol_day_are_ambiguous_not_arbitrary():
    """The ledger routinely holds several trades per name per day; picking one silently
    would bench an arbitrary window. The refusal must NAME the candidates — a refusal the
    operator cannot act on just moves the dead end."""
    with pytest.raises(SystemExit) as exc:
        B.find_manifest_row(_AMBIGUOUS_DOC, "VCIG", "2026-03-04")
    assert "ambiguous" in str(exc.value)
    assert "vid::VCIG::2026-03-04::ml1" in str(exc.value)
    assert "vid::VCIG::2026-03-04::ml2" in str(exc.value)


def test_a_named_manifest_id_selects_exactly_that_row():
    pick = B.find_manifest_row(_AMBIGUOUS_DOC, "VCIG", "2026-03-04",
                               manifest_id="vid::VCIG::2026-03-04::ml2")
    assert pick.manifest_id == "vid::VCIG::2026-03-04::ml2"
    assert pick.resolution == "cases_manifest_id"
    assert pick.row["entry_time_et"] == "10:20"


def test_a_manifest_id_that_names_another_symbol_day_is_refused_by_name():
    """A typo in --cases and a stale manifest are different problems and get different
    messages; both refuse rather than falling back to 'the symbol-day's rows'."""
    doc = {"windows": _AMBIGUOUS_DOC["windows"] + [
        {"manifest_id": "vid::OTHR::2026-03-05::ml1", "symbol": "OTHR", "date": "2026-03-05"}]}
    with pytest.raises(SystemExit) as exc:
        B.find_manifest_row(doc, "VCIG", "2026-03-04", manifest_id="vid::OTHR::2026-03-05::ml1")
    assert "it belongs to OTHR:2026-03-05" in str(exc.value)
    with pytest.raises(SystemExit) as exc2:
        B.find_manifest_row(doc, "VCIG", "2026-03-04", manifest_id="nope")
    assert "no row in the manifest carries it" in str(exc2.value)


def test_one_anchored_pin_row_disambiguates_a_symbol_day():
    """Second-best resolution, kept because it costs nothing: if exactly ONE of the
    candidate rows has a pin carrying a grading anchor, that pin has already answered which
    window the evidence is about. MEASURED over the real 418-window manifest and its
    157-row pins file: this resolves 3 of the 62 ambiguous symbol-days (FCUV 2026-08-03,
    PN 2026-07-30, ZCMD 2026-07-23) and NONE of the eight lane-alive cases — the explicit
    selector is the fix, this is a convenience."""
    pins = {"pins": [
        {"manifest_id": "vid::VCIG::2026-03-04::ml2", "symbol": "VCIG", "date": "2026-03-04",
         "grading_anchor_utc": "2026-03-04T15:20:00Z"},
    ]}
    pick = B.find_manifest_row(_AMBIGUOUS_DOC, "VCIG", "2026-03-04", pins=pins)
    assert pick.manifest_id == "vid::VCIG::2026-03-04::ml2"
    assert pick.resolution.startswith("pin_anchor:")


def test_several_anchored_pins_leave_the_symbol_day_ambiguous():
    """A pin per window is the pinner's NORMAL output, so 'there is a pins file' must never
    be read as 'the pins file decided'. Silently picking one would bench an arbitrary
    window under another window's name — the failure this whole selector exists to stop."""
    pins = {"pins": [
        {"manifest_id": "vid::VCIG::2026-03-04::ml1", "symbol": "VCIG", "date": "2026-03-04",
         "grading_anchor_utc": "2026-03-04T14:41:00Z"},
        {"manifest_id": "vid::VCIG::2026-03-04::ml2", "symbol": "VCIG", "date": "2026-03-04",
         "grading_anchor_utc": "2026-03-04T15:20:00Z"},
    ]}
    with pytest.raises(SystemExit) as exc:
        B.find_manifest_row(_AMBIGUOUS_DOC, "VCIG", "2026-03-04", pins=pins)
    assert "ambiguous" in str(exc.value)
    assert "offered 2 anchored row(s)" in str(exc.value)


def test_an_unanchored_pin_row_cannot_decide_anything():
    """A pin with no grading anchor says nothing about WHICH window it belongs to. Counting
    it would let it veto the resolution the one anchored row could have made."""
    pins = {"pins": [
        {"manifest_id": "vid::VCIG::2026-03-04::ml1", "symbol": "VCIG", "date": "2026-03-04"},
        {"manifest_id": "vid::VCIG::2026-03-04::ml2", "symbol": "VCIG", "date": "2026-03-04",
         "grading_anchor_utc": "2026-03-04T15:20:00Z"},
    ]}
    assert B.find_manifest_row(_AMBIGUOUS_DOC, "VCIG", "2026-03-04",
                               pins=pins).manifest_id == "vid::VCIG::2026-03-04::ml2"


def test_a_unique_symbol_day_still_needs_no_selector():
    """The pre-existing path, unchanged: the 155 unique symbol-days keep working with a bare
    ``SYMBOL:DATE``, and the resolution says which branch answered."""
    doc = {"windows": [{"manifest_id": "vid::TMCR::2026-08-24::ml1", "symbol": "TMCR",
                        "date": "2026-08-24", "entry_time_et": "9:41"}]}
    pick = B.find_manifest_row(doc, "TMCR", "2026-08-24")
    assert pick.resolution == "symbol_day_unique"
    assert pick.manifest_id == "vid::TMCR::2026-08-24::ml1"


def test_corpus_membership_never_silently_passes_an_unverified_case():
    """'unknown' is a distinct answer from 'in'. main() warns on unknown and REFUSES on
    absent — a case outside the corpus replays a half-hydrated window that scores as a
    miss the lane did not make."""
    corpus = {"cases": [{"symbol": "TMCR", "date": "2026-08-24"}]}
    assert B.corpus_membership(corpus, "TMCR", "2026-08-24") == "in"
    assert B.corpus_membership(corpus, "DAIC", "2026-08-26") == "absent"
    assert B.corpus_membership(None, "TMCR", "2026-08-24").startswith("unknown")
    assert B.corpus_membership({"unexpected": 1}, "TMCR", "2026-08-24").startswith("unknown")


# ── the receipt the bench reads back ─────────────────────────────────────────

def test_the_expected_receipt_schema_matches_the_driver():
    """If the driver starts emitting a different schema this fails here, instead of the
    bench silently scoring keys that moved."""
    src = _DRIVER.read_text(encoding="utf-8")
    assert f'REPLAY_RESULT_SCHEMA = "{B.DRIVER_RESULT_SCHEMA}"' in src


def _receipt(**over):
    doc = {
        "schema": B.DRIVER_RESULT_SCHEMA,
        "env": {"SYMBOL": "TMCR", "WIN_START": CASE.win_start, "WIN_END": CASE.win_end,
                "OHLCV_START": CASE.win_start, "EXEC_FAMILY": "alpaca_spot",
                "EQUITY": 30000.0, "RISK": 900.0, "GRID_STEP_S": 1.0,
                "FRAME_WARMUP_MIN": 7200.0, "TICK_STRIDE": 1,
                "SOURCE_FILTER": B.SOURCE_FILTER_VALUE.split(","),
                "REPLAY_KEEP_SINK": None},
        "tree": {"head": "078487738aaaaaaa"},
        "mirrored": {"tick_rows": 12000, "nbbo_rows": 4000, "depth_rows": 900},
        "grid_steps": 3600, "seed_session_id": 1,
        # the receipt's own mock echo — FillMode.CONSERVATIVE is the plain string
        # "conservative" (replay_mock_broker.py:129), so it survives JSON intact
        "mock": {"resting_limit_fills": True, "volume_cap_enabled": True,
                 "fill_mode": "conservative", "freshness_mode": "wall"},
        "fills": [{"ts": "2026-08-24 13:31:00", "side": "BUY", "px": 1.2, "qty": 100}],
        "events": [{"ts": "2026-08-24 13:30:00", "event_type": "entry_candidate",
                    "payload": {"reason": "hod_break"}}],
        "event_histogram": {"entry_candidate": 1},
        "pnl_usd": 12.0, "final_state": "flat", "entries": 1, "exits": 0,
    }
    doc.update(over)
    return doc


def test_a_clean_receipt_raises_no_invariant_problem():
    assert B.post_run_invariants(_receipt(), env=_env(), head="078487738",
                                 reference=None, previous_counts=None) == []


def test_an_empty_NBBO_mirror_is_flagged_as_measuring_silence():
    """The FSM reads momentum_nbbo_spread_tape DIRECTLY from the sink in three places, so a
    zero here means the micro-pullback detector and the adaptive spread-cost veto both read
    an empty table (replay_v3_fsm_window.py:37-46)."""
    r = _receipt(mirrored={"tick_rows": 12000, "nbbo_rows": 0, "depth_rows": 900})
    problems = B.check_nbbo_mirrored(r)
    assert problems and "nbbo_rows" in problems[0]


def test_a_rail_that_did_not_bind_is_flagged():
    """THE EXEC_FAMILY INCIDENT. The bench asks for alpaca_spot; if the driver echoes back
    robinhood_agentic_mcp the run measured a different fill path than the one reported."""
    r = _receipt()
    r["env"] = dict(r["env"], EXEC_FAMILY="robinhood_agentic_mcp")
    problems = B.check_env_bound(r, _env())
    assert problems and "EXEC_FAMILY" in problems[0]


def test_a_window_that_did_not_bind_is_flagged():
    r = _receipt()
    r["env"] = dict(r["env"], WIN_END="2026-08-24T20:00:00")
    assert any("WIN_END" in p for p in B.check_env_bound(r, _env()))


def test_a_kept_sink_inside_the_driver_is_flagged_even_though_it_was_fenced():
    """Belt and braces: the fence strips it, and the receipt echo proves it stayed stripped
    (the driver records REPLAY_KEEP_SINK in its own env contract, :791)."""
    r = _receipt()
    r["env"] = dict(r["env"], REPLAY_KEEP_SINK="1")
    assert any("REPLAY_KEEP_SINK" in p for p in B.check_env_bound(r, _env()))


def test_a_mock_that_is_not_the_parity_config_is_flagged():
    """resting_limit_fills=False caused exit-ladder submit spam and a non-conservative mock
    over-credits fills the recorded tape could not have supplied — either way the PnL is not
    comparable to any baseline (replay_harness_invariants.py:479-495)."""
    r = _receipt(mock={"resting_limit_fills": False, "volume_cap_enabled": True,
                       "fill_mode": "conservative", "freshness_mode": "wall"})
    assert B.check_mock_parity(r)
    assert B.check_mock_parity(_receipt()) == []


def test_a_subprocess_that_ran_a_different_tree_is_flagged():
    """A stale build tree once produced a confident 'the fix works' for code that never ran."""
    r = _receipt(tree={"head": "deadbeefdeadbeef"})
    assert B.check_tree_match(r, "078487738")


def test_arms_of_one_case_must_have_replayed_the_same_tape():
    """Arms may only flip behaviour and the mirror is tie-stable since 2026-09-04
    (replay_v3_fsm_window.py:64-69), so a difference in mirrored rows or grid length means
    the two arms did not see the same tape and their delta is not a treatment effect."""
    ref = _receipt()
    drifted = _receipt(mirrored={"tick_rows": 11999, "nbbo_rows": 4000, "depth_rows": 900})
    assert B.check_same_tape(drifted, ref)
    assert B.check_same_tape(_receipt(), ref) == []


def test_a_growing_seed_session_id_is_read_as_sink_contamination():
    """The reset TRUNCATEs trading_automation_sessions with RESTART IDENTITY
    (replay_v3_fsm_window.py:1469-1473), so a clean sink hands every run the same low id.
    Growth is the additive signature additive_count_check exists to catch."""
    assert B.sink_counts(_receipt()) == {"seed_session_id": 1}
    problems = B.post_run_invariants(_receipt(seed_session_id=4), env=_env(),
                                     head="078487738", reference=None,
                                     previous_counts={"seed_session_id": 1})
    assert any("additive" in p for p in problems)


def test_a_receipt_from_a_different_schema_is_refused():
    assert B.check_receipt_schema(_receipt(schema="chili.replay_v3_fsm_window_result.v2"))


def test_the_pin_check_separates_unverified_from_verified_and_matching():
    """The pins file comes from another step, so 'I could not tell' must never render as
    'they match'. Only a real contradiction is escalated to an invariant problem."""
    r = _receipt(tape_sources={"iqfeed_trade_ticks": {"iqfeed_lookup_hist": 16933},
                               "momentum_nbbo_spread_tape": {"iqfeed_lookup_bbo": 4000}})
    assert B.check_pin_sources(r, None).startswith("unverified")
    assert B.check_pin_sources(r, {"symbol": "TMCR"}).startswith("unverified")
    assert B.check_pin_sources(r, {"sources": ["iqfeed_lookup_hist", "iqfeed_lookup_bbo"]}
                               ).startswith("match")
    assert B.check_pin_sources(r, {"hydrate_provider": "polygon"}).startswith("MISMATCH")


def test_the_pin_check_reads_the_nested_tape_sources_the_pinner_actually_emits():
    """``chili.ross_event_pins.v1`` nests the surveyed providers under ``tape.sources``
    (rossbench_pin_ross_events.py:980-985), not at the top level. Reading only the top level
    would have made every pin 'unverified' forever — a check that never fires."""
    r = _receipt(tape_sources={"iqfeed_trade_ticks": {"iqfeed_lookup_hist": 16933}})
    pin = {"symbol": "TMCR", "date": "2026-08-24",
           "tape": {"rows_in_window": 16933, "sources": ["iqfeed_lookup_hist"]}}
    assert B.check_pin_sources(r, pin).startswith("match")
    pin_wrong = {"symbol": "TMCR", "tape": {"sources": ["polygon_v3_trades"]}}
    assert B.check_pin_sources(r, pin_wrong).startswith("MISMATCH")


def test_the_row_readers_name_the_containers_the_sibling_builders_emit():
    """These lists are a cross-module contract with four other tools. A cosmetic container
    rename that silently produced zero joins is the exact failure this pins down."""
    assert "windows" in B._ROW_LIST_KEYS      # build_ross_manifest.py:321
    assert "rows" in B._ROW_LIST_KEYS         # chili.rossbench_corpus.v1
    assert "pins" in B._ROW_LIST_KEYS         # chili.ross_event_pins.v1
    assert "cases" in B._ROW_LIST_KEYS        # ross_manifest_adapter.case_as_json_row
    assert "trade_date" in B._ROW_DATE_KEYS   # ross_manifest_adapter.py:782
    assert "start_ts" in B._ROW_WIN_START_KEYS and "end_ts" in B._ROW_WIN_END_KEYS
    assert "grading_anchor_utc" in B._ROW_ANCHOR_UTC_KEYS
    assert "entry_clock_et" in B._ROW_ANCHOR_ET_KEYS
    # The window IDENTITY, in the same priority order ross_manifest_adapter._pin_id uses, so
    # the runner and the adapter select the same row out of the same document.
    assert B._ROW_ID_KEYS == ("manifest_id", "label_id", "window_id")
    adapter = (_REPO / "app" / "services" / "trading" / "momentum_neural"
               / "ross_manifest_adapter.py")
    if adapter.exists():
        assert '("manifest_id", "label_id", "window_id")' in adapter.read_text(encoding="utf-8")


def test_a_pins_document_and_a_corpus_document_both_join_on_symbol_day():
    """Both sibling documents key their rows on plain ``symbol``/``date``
    (rossbench_pin_ross_events.pin_window, rossbench_corpus.py:697-698). The pins document
    ALSO carries ``manifest_id``, which is the real join — the symbol-day path is the
    fallback for a pins file written before that key existed."""
    pins = {"schema": "chili.ross_event_pins.v1",
            "pins": [{"symbol": "TMCR", "date": "2026-08-24", "pin_method": "tape_pin"}]}
    row, how = B.pin_for_case(pins, "TMCR", "2026-08-24")
    assert row["pin_method"] == "tape_pin" and how == "symbol_day_unique"
    corpus = {"schema": "chili.rossbench_corpus.v1",
              "rows": [{"symbol": "TMCR", "date": "2026-08-24"}]}
    assert B.corpus_membership(corpus, "TMCR", "2026-08-24") == "in"


def test_the_pin_row_is_joined_on_manifest_id_not_on_the_symbol_day():
    """The pinner emits one row per manifest WINDOW carrying ``manifest_id``
    (rossbench_pin_ross_events.pin_window; ``assert_pin_row_contract`` refuses a row
    without it). Verified against a real --offline run over the 418-window manifest: 157
    window rows, all 157 joined. Joining on the symbol-day instead would hand a
    four-wave day's first row to all four of its windows."""
    pins = {"pins": [
        {"manifest_id": "A::ILLR::D::ml1", "symbol": "ILLR", "date": "2026-06-25",
         "pin_method": "tape_pin"},
        {"manifest_id": "A::ILLR::D::ml3", "symbol": "ILLR", "date": "2026-06-25",
         "pin_method": "unpinned"},
    ]}
    row, how = B.pin_for_case(pins, "ILLR", "2026-06-25", manifest_id="A::ILLR::D::ml3")
    assert row["pin_method"] == "unpinned" and how == "manifest_id"


def test_an_ambiguous_pin_row_is_refused_rather_than_taken_first():
    """What the pin row feeds is ``check_pin_sources`` — a tape-provenance contradiction
    check — so the wrong row asserts 'the pinned tape is the replayed tape' about a
    DIFFERENT window's tape. Returning row 0 made that assertion silently; the status now
    names the problem and main() refuses at plan time, before any subprocess starts."""
    pins = {"pins": [
        {"manifest_id": "A::ILLR::D::ml1", "symbol": "ILLR", "date": "2026-06-25"},
        {"manifest_id": "A::ILLR::D::ml3", "symbol": "ILLR", "date": "2026-06-25"},
    ]}
    row, how = B.pin_for_case(pins, "ILLR", "2026-06-25")
    assert row is None and how.startswith("ambiguous:2_pin_rows_for_symbol_day")
    src = _SCRIPT.read_text(encoding="utf-8")
    assert 'if pin_resolution.startswith("ambiguous")' in src, (
        "main() must refuse an ambiguous pin at plan time, not carry it into a 45-minute run"
    )


def test_a_window_with_no_pin_row_is_normal_and_not_an_error():
    """The pinner writes rows only for the 157 ledger TRADE windows, not for all 418
    manifest windows, so 'no pin' is the ordinary state of a layer-1/2/3 row."""
    pins = {"pins": [{"manifest_id": "A::ILLR::D::ml1", "symbol": "ILLR",
                      "date": "2026-06-25"}]}
    # Measured on the real pair: 3 of the eight lane-alive cases (UPC, WETO 08-17, PFSA) have
    # no pin row at all. The status distinguishes "asked by id and found none" from "asked by
    # symbol-day and found none" so a stale pins file is not mistaken for a stale manifest.
    row, how = B.pin_for_case(pins, "WETO", "2026-08-17", manifest_id="Oz::WETO::D::t3")
    assert row is None and how == "no_pin_row_for_manifest_id"
    row2, how2 = B.pin_for_case(pins, "WETO", "2026-08-17")
    assert row2 is None and how2 == "no_pin_row"


def test_an_adapter_shaped_row_resolves_without_lead_or_lag():
    """When the adapter already produced a VALIDATED grading window, that window wins
    outright — re-deriving one from an anchor could disagree with the grader that will
    score the run (ross_manifest_adapter.py:786-787)."""
    row = {"symbol": "TMCR", "trade_date": "2026-08-24",
           "start_ts": "2026-08-24T13:26:00+00:00", "end_ts": "2026-08-24T14:26:00+00:00",
           "decision_ts": "2026-08-24T13:41:00+00:00"}
    case = B.resolve_window(row, symbol="TMCR", day="2026-08-24", lead_s=None, lag_s=None)
    assert (case.win_start, case.win_end) == ("2026-08-24T13:26:00", "2026-08-24T14:26:00")
    assert case.anchor_source == "manifest_window_utc"


def test_a_pin_anchor_still_needs_the_bench_to_supply_the_lead():
    """The pinner sets ``lead_s: None`` explicitly because choosing it is not its job
    (rossbench_pin_ross_events.pin_window). If nobody supplies one, the bench refuses
    rather than inventing a window length."""
    row = {"symbol": "TMCR", "date": "2026-08-24",
           "grading_anchor_utc": "2026-08-24T13:41:00Z"}
    case = B.resolve_window(row, symbol="TMCR", day="2026-08-24", lead_s=900, lag_s=2700)
    assert (case.win_start, case.win_end) == ("2026-08-24T13:26:00", "2026-08-24T14:26:00")
    assert case.anchor_source == "manifest_anchor_utc"
    with pytest.raises(SystemExit):
        B.resolve_window(row, symbol="TMCR", day="2026-08-24", lead_s=None, lag_s=None)


def test_the_pin_places_a_window_the_manifest_row_cannot():
    """LAST RESORT, and only when the manifest row itself said nothing. MEASURED over the
    418-window manifest and the 157-row pins file it was built with: 196 windows carry no
    clock ``resolve_window`` can read, and 13 of those have exactly one anchored pin row.
    The source tag is ``pin_anchor_utc`` and NOT a manifest clock, because the grader must
    be able to tell an anchor the manifest stated from one the pinner supplied."""
    row = {"symbol": "PN", "date": "2026-07-30", "window_et": None}
    pin = {"manifest_id": "x", "grading_anchor_utc": "2026-07-30T11:00:00Z"}
    case = B.resolve_window(row, symbol="PN", day="2026-07-30",
                            lead_s=900, lag_s=2700, pin=pin)
    assert case.anchor_source == "pin_anchor_utc"
    assert (case.win_start, case.win_end) == ("2026-07-30T10:45:00", "2026-07-30T11:45:00")


def test_the_manifest_clock_still_wins_over_the_pin_anchor():
    """Precedence, not preference: the manifest is the ground truth the grader scores
    against, so a document outside it may fill a hole but may never overwrite a statement.
    Verified end to end — re-resolving the eight lane-alive windows with the pin branch in
    place produced byte-identical win_start/win_end for every case that already resolved."""
    row = {"symbol": "TMCR", "date": "2026-08-24", "entry_time_et": "9:41 HOD break"}
    pin = {"grading_anchor_utc": "2026-08-24T19:00:00Z"}
    case = B.resolve_window(row, symbol="TMCR", day="2026-08-24",
                            lead_s=900, lag_s=2700, pin=pin)
    assert case.win_start == "2026-08-24T13:26:00"
    assert case.anchor_source.startswith("manifest_et_clock")


def test_a_row_with_no_clock_and_no_pin_anchor_is_still_refused():
    """SLE 2026-08-18 is the live example: its only clock field reads 'premarket into the
    open' and no pin row exists for it, so it stays unbenchable until someone puts an
    explicit win_start/win_end in the manifest. A default onto the open would bench a
    window Ross was not in."""
    row = {"symbol": "SLE", "date": "2026-08-18", "window_et": "premarket into the open"}
    with pytest.raises(SystemExit) as exc:
        B.resolve_window(row, symbol="SLE", day="2026-08-18",
                         lead_s=900, lag_s=2700, pin={"pin_second_utc": None})
    assert "no usable ET clock" in str(exc.value)


def test_a_pin_mismatch_means_the_pinned_tape_is_not_the_replayed_tape():
    """Measured TMCR 2026-08-24: a symbol-day hydrated from two providers returns both tapes
    concatenated, 16,933 + 16,933 = 33,866 ticks, with nothing visibly wrong with the rows."""
    r = _receipt(tape_sources={"iqfeed_trade_ticks": {"polygon_v3_trades": 16933}})
    assert B.check_pin_sources(r, {"sources": "iqfeed_lookup_hist"}).startswith("MISMATCH")


# ── the per-run output tree ──────────────────────────────────────────────────

def test_the_output_tree_is_case_slash_arm_with_four_files(tmp_path):
    out = tmp_path / CASE.dirname / "base"
    B.write_run_outputs(str(out), _receipt(), CASE, B.Arm("base"))
    # run.json is written by the DRIVER itself (REPLAY_JSON_OUT points into this directory)
    assert sorted(p.name for p in out.iterdir()) == [
        "events.jsonl", "events_timeline.jsonl", "events_timeline.md"]
    assert _env()["REPLAY_JSON_OUT"].replace("\\", "/").endswith(
        f"{CASE.dirname}/base/run.json")


def test_the_runners_event_log_does_not_squat_on_the_timeline_writers_name(tmp_path):
    """TWO producers wrote ``timeline.jsonl`` into this same directory with incompatible
    schemas and whichever ran last silently won. scripts/rossbench_timeline.py writes the
    per-second ``chili.rossbench_timeline_row.v1`` document carrying ``first_divergence``
    and per-event ``code_ref``; this module writes a raw merge of receipt events and fills
    that has neither. A reader who opened the runner's file expecting the analysed one
    would find no divergence flag and conclude there was no divergence."""
    out = tmp_path / CASE.dirname / "base"
    B.write_run_outputs(str(out), _receipt(), CASE, B.Arm("base"))
    names = {p.name for p in out.iterdir()}
    assert "timeline.jsonl" not in names and "timeline.md" not in names
    assert B.EVENT_TIMELINE_JSONL == "events_timeline.jsonl"
    # And the reporter's fallback still names only the analysed document, so it needs no
    # change for this rename — it reads timeline.meta.json first, then timeline.jsonl
    # (rossbench_report.load_arm_dir), both of which only the timeline writer produces.
    reporter = (_REPO / "scripts" / "rossbench_report.py").read_text(encoding="utf-8")
    assert 'os.path.join(path, "timeline.jsonl")' in reporter
    assert "events_timeline" not in reporter, (
        "if the reporter starts reading the runner's raw event log, it must do so knowing "
        "that log carries no divergence flag and no code_ref"
    )


def test_the_timeline_merges_events_and_fills_on_one_clock():
    rows = B.timeline_rows(_receipt())
    assert [r["kind"] for r in rows] == ["event", "fill"]
    assert rows[0]["why"] == "hod_break"
    assert rows[1]["what"] == "BUY" and rows[1]["px"] == 1.2


def test_an_event_without_a_timestamp_sorts_last_not_first():
    """A null ts must never be read as 'the first thing that happened'."""
    r = _receipt(events=[{"ts": None, "event_type": "_receipt_event_read_failed", "payload": {}},
                         {"ts": "2026-08-24 13:30:00", "event_type": "entry_candidate",
                          "payload": {}}])
    assert [x["what"] for x in B.timeline_rows(r)][-1] == "_receipt_event_read_failed"


def test_the_written_receipt_carries_no_database_credentials():
    """bench.json lands in a shared evidence directory, and the URLs handed to this bench
    carry credentials. The driver already made this call for its own receipt — it records
    the database NAME, not the URL (replay_v3_fsm_window.py:792-793)."""
    assert B.redact_db_url("postgresql://chili:chili@localhost:5433/chili_bench_test") == \
        "chili_bench_test"
    assert B.redact_db_url("postgresql://chili:chili@/chili_test?host=/var/run") == "chili_test"
    assert "chili:chili" not in B.redact_db_url("postgresql://chili:chili@h:5433/chili")
    src = _SCRIPT.read_text(encoding="utf-8")
    assert '_redacted({k: env[k] for k in sorted(CONTRACT_ENV_KEYS)}' in src, (
        "the per-run contract_env is written to bench.json verbatim; it must be redacted"
    )
    # and the real URL must still reach the subprocess
    assert _env()["TEST_DATABASE_URL"].startswith("postgresql://")


def test_the_bench_does_not_grade():
    """Grading is ross_replay_benchmark.py's job. A bench that also graded would blur the
    line between what ran and what it is worth, and the receipt summary is deliberately a
    verbatim lift with no derived metric."""
    summary = B._receipt_summary(_receipt())
    assert summary["pnl_usd"] == 12.0
    assert not any(k for k in summary if "score" in k or "grade" in k or "parity" in k)



# ── fence: an Alpaca run cannot silently seed the mock identity ──────────────


def test_an_alpaca_run_refuses_without_the_certified_account_id():
    """MEASURED 2026-09-04: without this key the seeder froze the replay mock identity, the
    driver's quarantine gate returned `alpaca_account_generation_mismatch` on all 2,670
    ticks, and the run finished rc=0 with zero events — indistinguishable from "the
    strategy never fired". The bench must refuse, not produce that."""
    with pytest.raises(SystemExit) as exc:
        _env(parent={}, certified=False)
    msg = str(exc.value)
    assert B.ALPACA_ACCOUNT_ID_ENV in msg
    assert "alpaca_spot" in msg


def test_an_alpaca_run_passes_the_certified_account_id_through():
    env = _env(parent={B.ALPACA_ACCOUNT_ID_ENV: CERTIFIED_PAPER_ACCOUNT})
    assert env[B.ALPACA_ACCOUNT_ID_ENV] == CERTIFIED_PAPER_ACCOUNT
    # It rides the ambient CHILI_* pass-through, so it is NOT a contract key — the contract
    # set stays exactly what test_the_contract_env_is_exactly_the_declared_key_set pins.
    assert B.ALPACA_ACCOUNT_ID_ENV not in B.CONTRACT_ENV_KEYS


def test_a_non_alpaca_run_does_not_need_the_account_id():
    kwargs = dict(_CONTRACT_KWARGS, exec_family="robinhood_agentic_mcp")
    env, _dropped = B.build_env(case=CASE, arm=B.Arm("base"), parent={}, **kwargs)
    assert B.ALPACA_ACCOUNT_ID_ENV not in env


def test_the_alpaca_families_match_the_app_constant():
    """The bench spells the families locally (it must import without app); pin them to the
    app's own set so a renamed family cannot let an Alpaca run skip the refusal."""
    from app.services.trading.momentum_neural.alpaca_orphan_claims import ALPACA_EXECUTION_FAMILIES
    assert set(B.BENCH_ALPACA_FAMILIES) == set(ALPACA_EXECUTION_FAMILIES)


# ── fence: an Alpaca run must carry the lane's dispatch mode ────────────────

def test_an_alpaca_run_refuses_without_the_lane_dispatch_mode():
    """Account id present, dispatch mode absent: the runner would require the adaptive-risk
    builder and block every attempt (x26, 0 fills, SDOT 2026-06-26 on 2026-09-04)."""
    with pytest.raises(SystemExit) as exc:
        _env(parent={B.ALPACA_ACCOUNT_ID_ENV: CERTIFIED_PAPER_ACCOUNT}, certified=False)
    msg = str(exc.value)
    assert B.ALPACA_DISPATCH_MODE_ENV in msg
    assert "timeshare_supervisor.py:88" in msg
    assert "live_entry_adaptive_risk_blocked" in msg


def test_an_alpaca_run_passes_the_lane_dispatch_mode_through():
    env = _env()
    assert env[B.ALPACA_DISPATCH_MODE_ENV] == "true"
    assert env[B.ALPACA_ACCOUNT_ID_ENV] == CERTIFIED_PAPER_ACCOUNT


def test_a_non_alpaca_run_does_not_need_the_dispatch_mode():
    kwargs = dict(_CONTRACT_KWARGS, exec_family="robinhood_agentic_mcp")
    env, _dropped = B.build_env(case=CASE, arm=B.Arm("base"), parent={}, **kwargs)
    assert B.ALPACA_DISPATCH_MODE_ENV not in env


def test_the_dispatch_mode_is_ambient_not_a_contract_key():
    assert B.ALPACA_DISPATCH_MODE_ENV not in B.CONTRACT_ENV_KEYS
    assert dict(B.ALPACA_REQUIRED_AMBIENT_ENV).keys() == {
        B.ALPACA_ACCOUNT_ID_ENV, B.ALPACA_DISPATCH_MODE_ENV
    }


# ── fence: the app engine is bound to the SINK; the tape has its own DSN ────────

def test_the_app_engine_is_bound_to_the_sink_not_the_tape():
    """MEASURED 2026-09-05 (SDOT 2026-06-26, alpaca_spot, eight gates answered): 19/26
    entry attempts deferred ``risk_ledger_unreadable`` because the Alpaca claim helpers open
    ``SessionLocal()`` -- app/db.py's engine, bound to DATABASE_URL -- and DATABASE_URL was
    the hydrated tape DB, which has no ``broker_symbol_action_claims``. The tape is read
    through TAPE_SOURCE_URL; DATABASE_URL is the sink."""
    env = B.contract_env(case=CASE, **_CONTRACT_KWARGS)
    assert env["TAPE_SOURCE_URL"] == _CONTRACT_KWARGS["source"]
    assert env["DATABASE_URL"] == _CONTRACT_KWARGS["sink"]
    assert env["TEST_DATABASE_URL"] == _CONTRACT_KWARGS["sink"]
    assert env["DATABASE_URL"] != env["TAPE_SOURCE_URL"]


def test_the_driver_reads_the_tape_from_tape_source_url_first():
    src = _DRIVER.read_text(encoding="utf-8")
    assert 'os.environ.get("TAPE_SOURCE_URL")' in src
    assert '"broker_symbol_action_claims"' in src   # cleaned per run once the app engine is the sink


# ── --lane-env: the strategy runs AS DEPLOYED, recorded by name and hash ──────────

def _lane_file(tmp_path, text):
    p = tmp_path / "lane.env"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_parse_lane_env_keeps_chili_keys_only_and_unwraps_quotes(tmp_path):
    p = _lane_file(tmp_path, """
# comment
export CHILI_MOMENTUM_LEGACY_ALPACA_DISPATCH_ENABLED=true
CHILI_ALPACA_EXPECTED_ACCOUNT_ID="c7d421e0-4fae-4219-9503-5ce051d4d923"
CHILI_MOMENTUM_RISK_MAX_SPREAD_BPS_LIVE='40'
DATABASE_URL=postgresql://chili:chili@localhost:5433/chili
ALPACA_API_KEY=secret
not a line
""")
    lane = B.parse_lane_env(p)
    assert lane == {
        "CHILI_MOMENTUM_LEGACY_ALPACA_DISPATCH_ENABLED": "true",
        "CHILI_ALPACA_EXPECTED_ACCOUNT_ID": "c7d421e0-4fae-4219-9503-5ce051d4d923",
        "CHILI_MOMENTUM_RISK_MAX_SPREAD_BPS_LIVE": "40",
    }


def test_lane_env_satisfies_the_alpaca_requirements_and_reaches_the_driver(tmp_path):
    p = _lane_file(tmp_path, "CHILI_MOMENTUM_LEGACY_ALPACA_DISPATCH_ENABLED=true\n"
                             f"CHILI_ALPACA_EXPECTED_ACCOUNT_ID={CERTIFIED_PAPER_ACCOUNT}\n"
                             "CHILI_MOMENTUM_RISK_MAX_SPREAD_BPS_LIVE=40\n")
    env, _dropped = B.build_env(case=CASE, arm=B.Arm("base"), parent={}, lane_env=B.parse_lane_env(p),
                                **_CONTRACT_KWARGS)
    assert env["CHILI_MOMENTUM_RISK_MAX_SPREAD_BPS_LIVE"] == "40"
    assert env[B.ALPACA_ACCOUNT_ID_ENV] == CERTIFIED_PAPER_ACCOUNT


def test_lane_env_never_overrides_the_contract_or_protected_keys(tmp_path):
    lane = {"CHILI_PYTEST": "0", "SYMBOL": "XXXX", "DATABASE_URL": "postgresql://x/y", **LANE_ALPACA_ENV}
    env, _dropped = B.build_env(case=CASE, arm=B.Arm("base"), parent={}, lane_env=lane, **_CONTRACT_KWARGS)
    assert env["CHILI_PYTEST"] == "1"
    assert env["SYMBOL"] == CASE.symbol
    assert env["DATABASE_URL"] == _CONTRACT_KWARGS["sink"]


def test_an_arm_still_overrides_a_lane_env_key(tmp_path):
    lane = {"CHILI_MOMENTUM_RISK_MAX_SPREAD_BPS_LIVE": "40", **LANE_ALPACA_ENV}
    arm = B.Arm("wide", overrides={"CHILI_MOMENTUM_RISK_MAX_SPREAD_BPS_LIVE": "80"})
    env, _dropped = B.build_env(case=CASE, arm=arm, parent={}, lane_env=lane, **_CONTRACT_KWARGS)
    assert env["CHILI_MOMENTUM_RISK_MAX_SPREAD_BPS_LIVE"] == "80"


def test_the_record_carries_names_and_a_hash_never_values():
    lane = {"CHILI_ALPACA_EXPECTED_ACCOUNT_ID": "secret-ish", "CHILI_X": "1"}
    rec = B.lane_env_record(lane, path="lane.env")
    assert rec["keys"] == 2 and rec["names"] == ["CHILI_ALPACA_EXPECTED_ACCOUNT_ID", "CHILI_X"]
    assert len(rec["sha256"]) == 64
    assert "secret-ish" not in str(rec)
    assert B.lane_env_record({"CHILI_X": "1", "CHILI_ALPACA_EXPECTED_ACCOUNT_ID": "secret-ish"})["sha256"] == rec["sha256"]


# ── invariant 7: cold start is applied to every receipt ───────────────────────────
# The verdict is tick-denominated (the FSM's own insufficient_bars guard is); wall uptime
# is recorded and warned about. Grid 1 s unless the receipt env says otherwise.

def _cs_receipt(events, grid="1.0"):
    return {"events": events, "env": {"GRID_STEP_S": grid}}


def _cs_ev(ts, et):
    return {"ts": ts, "event_type": et, "payload": {}}


def test_an_entry_before_min_ticks_is_a_cold_start_problem():
    r = _cs_receipt([_cs_ev("2026-07-23 13:00:00", "live_runner_started"),
                     _cs_ev("2026-07-23 13:00:01", "live_entry_backside_bench_veto"),
                     _cs_ev("2026-07-23 13:00:03", "live_entry_candidate_detected"),
                     _cs_ev("2026-07-23 13:00:04", "live_entry_filled")])
    problems, rec = B.cold_start_problems(r, min_uptime_s=60.0, min_ticks=6)
    assert len(problems) == 1 and problems[0].startswith("cold_start:live_entry_candidate_detected at runner tick 3")
    assert rec["tick_cold_entry"]["ticks_seen"] == 3 and rec["tick_cold_entry"]["uptime_s"] == 3.0
    assert rec["tag_count"] == 3   # the veto is tagged too, but only the entry makes a problem


def test_an_entry_cold_by_uptime_only_is_recorded_and_scoreable():
    # NCRA 07-29 shape: candidate on grid tick 8, fill at 13 s -- 13 runner ticks, 13 s uptime
    r = _cs_receipt([_cs_ev("2026-07-29 12:15:00", "live_runner_started"),
                     _cs_ev("2026-07-29 12:15:08", "live_entry_candidate_detected"),
                     _cs_ev("2026-07-29 12:15:13", "live_entry_filled")])
    problems, rec = B.cold_start_problems(r, min_uptime_s=60.0, min_ticks=6)
    assert problems == []
    assert rec["tick_cold_entry"] is None
    assert [t["ticks_seen"] for t in rec["uptime_only_entries"]] == [8, 13]


def test_the_grid_step_from_the_receipt_env_sets_the_tick_count():
    # live-like cadence: 10 s per tick -> 40 s of uptime is only 4 ticks
    r = _cs_receipt([_cs_ev("2026-07-23 13:00:00", "live_runner_started"),
                     _cs_ev("2026-07-23 13:00:40", "live_entry_submitted")], grid="10")
    problems, rec = B.cold_start_problems(r, min_uptime_s=60.0, min_ticks=6)
    assert rec["grid_step_s"] == 10.0 and len(problems) == 1 and "runner tick 4" in problems[0]


def test_early_vetoes_alone_are_not_a_problem():
    r = _cs_receipt([_cs_ev("2026-07-23 13:00:00", "live_runner_started"),
                     _cs_ev("2026-07-23 13:00:03", "live_entry_backside_bench_veto"),
                     _cs_ev("2026-07-23 13:20:00", "live_entry_candidate_detected"),
                     _cs_ev("2026-07-23 13:20:02", "live_entry_filled")])
    problems, rec = B.cold_start_problems(r, min_uptime_s=60.0, min_ticks=6)
    assert problems == []
    assert rec["first_entry_decisive_cold"] is None and rec["tag_count"] == 1


def test_without_runner_started_the_first_event_is_the_clock_origin():
    r = _cs_receipt([_cs_ev("2026-07-23 13:00:00", "live_watch_started"),
                     _cs_ev("2026-07-23 13:00:02", "live_entry_submitted")])
    problems, rec = B.cold_start_problems(r, min_uptime_s=60.0, min_ticks=6)
    assert rec["runner_started_ts"] == "2026-07-23 13:00:00"
    assert len(problems) == 1


def test_the_knobs_are_the_plan_defaults_and_not_arm_settable():
    assert B.COLD_START_MIN_UPTIME_S == 60.0 and B.COLD_START_MIN_TICKS == 6
    assert any(p.startswith("ROSSBENCH_") for p in B.FORBIDDEN_ENV_PREFIXES)
