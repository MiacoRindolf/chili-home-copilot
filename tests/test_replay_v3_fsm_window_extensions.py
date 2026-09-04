"""The three defects that block the Ross bench, plus the structured result a scorer reads.

Follows tests/test_replay_mirrors_l2_depth.py: AST/source-level for "does the driver actually
DO it", plus direct calls into the driver's pure SQL builders for "and does it build the right
statement". No database is touched.

WHAT THIS PINS
--------------
1. SOURCE_FILTER. ``load_prod`` and ``mirror_ticks_streaming`` filtered on symbol and time
   ONLY. In ``chili_hydrated`` a symbol-day hydrated from two providers therefore returned
   BOTH tapes concatenated -- measured on TMCR 2026-08-24: 16,933 ``iqfeed_lookup_hist`` rows
   + 16,933 ``polygon_v3_trades`` rows came back as 33,866 ticks, every print twice, nothing
   about the rows malformed. UNSET must add NO predicate (every existing caller identical).

2. THE NBBO MIRROR. The driver mirrored the trade tape and the L2 book into the sink but
   NEVER ``momentum_nbbo_spread_tape`` -- while the FSM reads that table DIRECTLY FROM THE
   SINK in ``_build_micro_bar_df`` (live_runner:23426), the C1 IQFeed cross-check (:24335)
   and the adaptive spread-cost veto's rolling distribution (:38885). Every replay to date
   read an EMPTY table there: "measuring silence", exactly the defect the L2 depth mirror
   fixed on 2026-08-26.

3. EXEC_FAMILY. ``scripts/nightly_replay_report.py:164`` has set EXEC_FAMILY=alpaca_spot for
   weeks while the seed site hard-coded "robinhood_agentic_mcp" -- a silent no-op. The live
   rail is Alpaca and the fill path differs. The DEFAULT must not move.

4. TIE STABILITY. Every tape SELECT must end ``ORDER BY observed_at ASC, id ASC``; equal
   timestamps previously fell back to physical scan order.

Runnable: pytest tests/test_replay_v3_fsm_window_extensions.py -v
"""
from __future__ import annotations

import ast
import importlib
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "scripts" / "replay_v3_fsm_window.py"
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import replay_v3_fsm_window as drv  # noqa: E402


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"walang function na {name!r}")


def _body(name: str) -> str:
    fn = _fn(name)
    return "\n".join(_SRC.read_text(encoding="utf-8").splitlines()[fn.lineno - 1: fn.end_lineno])


# ─── A. SOURCE_FILTER ────────────────────────────────────────────────────────────────────

_BUILDERS = ("nbbo_tape_sql", "trade_tape_sql", "frame_tape_sql",
             "trade_mirror_sql", "nbbo_mirror_sql")


@pytest.mark.parametrize("builder", _BUILDERS)
def test_unset_source_filter_adds_no_predicate(builder):
    """ANG BYTE-IDENTICAL NA KASO. Every existing caller passes no SOURCE_FILTER; the SQL
    it gets must carry no source predicate and no bind parameter at all."""
    sql = getattr(drv, builder)(())
    assert " source " not in sql and "source =" not in sql, sql
    assert ":sources" not in sql and "ANY(" not in sql, sql


@pytest.mark.parametrize("builder", _BUILDERS)
def test_a_set_source_filter_adds_the_predicate(builder):
    sql = getattr(drv, builder)(("iqfeed_lookup_hist",))
    assert "AND source = ANY(" in sql, sql


@pytest.mark.parametrize("builder,placeholder", [
    ("nbbo_tape_sql", ":sources"),
    ("trade_tape_sql", ":sources"),
    ("frame_tape_sql", ":sources"),
    ("trade_mirror_sql", "%s"),
    ("nbbo_mirror_sql", "%s"),
])
def test_the_predicate_uses_each_halfs_own_paramstyle(builder, placeholder):
    """⚠️ The SQLAlchemy reads bind ``:sources``; the psycopg2 streaming mirrors bind
    ``%s``. Mixing them is a runtime error at mirror time, i.e. mid-run."""
    assert f"AND source = ANY({placeholder})" in getattr(drv, builder)(("x",))


def test_the_parameter_is_bound_only_when_the_predicate_is_present():
    """An unused bind parameter is an error on some drivers and a LIE in the run receipt
    on all of them."""
    assert "sources" not in drv.tape_params({"s": "TMCR"}, ())
    assert drv.tape_params({"s": "TMCR"}, ("a", "b"))["sources"] == ["a", "b"]


def test_the_predicate_is_never_string_interpolated():
    """Provenance is enforced by a BOUND parameter, never by formatting a value in."""
    assert drv.source_predicate(("iqfeed_lookup_hist",)) == " AND source = ANY(:sources)"
    assert "iqfeed_lookup_hist" not in drv.nbbo_tape_sql(("iqfeed_lookup_hist",))


def test_the_streaming_tick_mirror_binds_the_filter():
    body = _body("mirror_ticks_streaming")
    assert "trade_mirror_sql()" in body
    assert "SOURCE_FILTER" in body, "kailangang i-append ang listahan sa mga argumento"


def test_load_prod_uses_the_builders_not_inline_sql():
    body = _body("load_prod")
    for builder in ("nbbo_tape_sql()", "trade_tape_sql()", "frame_tape_sql()"):
        assert builder in body, body
    assert "tape_params(" in body


def test_the_double_hydration_guard_exists_and_reuses_the_canonicalizer():
    """⚠️ MEASURED TMCR 2026-08-24: 33,866 ticks = 2 x 16,933. Silent -- the rows are
    individually valid and only the COUNTS are wrong."""
    body = _body("assert_single_hydrated_source")
    assert "_canon_plan(" in body, "dapat gamitin ang hydration_canonicalize.plan"
    assert "SystemExit" in body
    assert "33,866" in body, "dapat nakasulat ang nasukat na numero sa mensahe"


def test_the_guard_is_called_before_the_load():
    main = _body("main")
    assert main.index("assert_single_hydrated_source()") < main.index("load_prod()")


# ─── B. THE NBBO MIRROR ──────────────────────────────────────────────────────────────────

def test_the_nbbo_mirror_exists():
    """ANG PANGUNAHING KASO."""
    assert _fn("mirror_nbbo_streaming") is not None


def test_it_is_actually_called_from_run_arm():
    """⚠️ Ang isang mirror na hindi tinatawag ay walang naimimirror."""
    assert "mirror_nbbo_streaming(eng)" in _body("run_arm")


def test_it_reports_its_count():
    """Kung walang bilang, hindi makikita ang katahimikan."""
    assert "mirrored_nbbo_rows" in _body("run_arm")


def test_it_filters_by_source():
    body = _body("mirror_nbbo_streaming")
    assert "nbbo_mirror_sql()" in body and "SOURCE_FILTER" in body


def test_it_writes_and_cleans_its_own_source_tag():
    """⚠️ Gaya EKSAKTO ng tick mirror: burahin sa simula AT sa dulo ng arm, kung hindi ang
    susunod na run ay magbabasa ng tape ng nakaraan."""
    assert "'replay_v3'" in _body("mirror_nbbo_streaming")
    run = _body("run_arm")
    deletes = [ln for ln in run.splitlines()
               if "DELETE FROM momentum_nbbo_spread_tape" in ln and "replay_v3" in ln]
    assert len(deletes) >= 2, f"kailangan ng start AT end cleanup, may {len(deletes)}"


def test_it_reuses_gotcha_11_the_five_minute_slices():
    """⚠️ Isang mahabang read transaction ang pinapatay ng db_watchdog (>10 min mula
    query_start) -- pinatay nito ang tick mirror nang DALAWANG beses."""
    body = _body("mirror_nbbo_streaming")
    assert "minutes=5" in body
    assert body.count("commit()") >= 2, "dapat mag-commit sa source AT sa sim kada slice"


def test_it_reuses_gotcha_11b_batched_inserts():
    body = _body("mirror_nbbo_streaming")
    assert "_ev(" in body and "page_size" in body


def test_the_source_connection_is_read_only():
    """⚠️ Ang source ay PRODUKSYON."""
    assert "readonly=True" in _body("mirror_nbbo_streaming")


def test_it_handles_the_timestamp_asymmetry():
    """⚠️ ``iqfeed_trade_ticks.observed_at`` ay TIMESTAMP (naive UTC) habang
    ``momentum_nbbo_spread_tape.observed_at`` ay TIMESTAMPTZ. Ang naive na hangganan ay
    ipinapasa sa SESSION TimeZone -- tama lang habang UTC iyon."""
    body = _body("mirror_nbbo_streaming")
    assert "_utc(slice_start)" in body and "_utc(slice_end)" in body


@pytest.mark.parametrize("column", [
    "bid", "ask", "mid", "spread_bps", "day_volume",
    "provider_event_at", "received_at", "timestamp_basis", "bridge_version",
])
def test_every_column_a_live_nbbo_reader_touches_is_carried(column):
    """``spread_bps`` ang binabasa ng C1 cross-check AT ng p50/p75/p90 spread veto; ``mid``
    ang binabasa ng run-up/ignition. Kung may nawawala, tahimik na magiging None ang gate."""
    assert column in drv.nbbo_mirror_sql()
    assert column in drv._NBBO_MIRROR_SQL


def test_the_insert_column_list_matches_the_select():
    """Isang hindi tugmang haligi ay isang tahimik na maling halaga sa maling patlang."""
    body = _body("mirror_nbbo_streaming")
    ins = body[body.index("INSERT INTO momentum_nbbo_spread_tape"):body.index("VALUES %s")]
    cols = [c.strip() for c in ins[ins.index("("): ins.rindex(")")].strip("() \n\"").split(",")]
    cols = [c.strip('" \n') for c in cols if c.strip('" \n')]
    assert cols[0] == "symbol" and "source" in cols
    assert len(cols) == 16, cols


# ─── C. EXEC_FAMILY ──────────────────────────────────────────────────────────────────────

def test_the_default_execution_family_is_unchanged(monkeypatch):
    """⚠️ ANG BYTE-IDENTICAL NA KASO: every existing caller must still seed the Robinhood
    agentic rail."""
    monkeypatch.delenv("EXEC_FAMILY", raising=False)
    reloaded = importlib.reload(drv)
    try:
        assert reloaded.EXEC_FAMILY == "robinhood_agentic_mcp"
    finally:
        importlib.reload(drv)


def test_exec_family_is_honoured(monkeypatch):
    """⚠️ nightly_replay_report.py:164 has set this to alpaca_spot for WEEKS and been
    ignored. The live rail is Alpaca and the fill path differs."""
    monkeypatch.setenv("EXEC_FAMILY", "alpaca_spot")
    reloaded = importlib.reload(drv)
    try:
        assert reloaded.EXEC_FAMILY == "alpaca_spot"
    finally:
        monkeypatch.delenv("EXEC_FAMILY", raising=False)
        importlib.reload(drv)


def test_it_goes_through_the_repo_normalizer(monkeypatch):
    """A typo'd rail must not silently become a DIFFERENT one."""
    monkeypatch.setenv("EXEC_FAMILY", "  ALPACA_SPOT  ")
    reloaded = importlib.reload(drv)
    try:
        assert reloaded.EXEC_FAMILY == "alpaca_spot"
    finally:
        monkeypatch.delenv("EXEC_FAMILY", raising=False)
        importlib.reload(drv)


def test_the_seed_site_no_longer_hardcodes_the_family():
    body = _body("run_arm")
    assert "execution_family=EXEC_FAMILY" in body
    assert 'execution_family="robinhood_agentic_mcp"' not in body


def test_the_venue_is_derived_not_guessed():
    """seed_replay_session derives the venue and requires an account identity for the
    NON-Alpaca families only; the receipt records what was actually used."""
    from app.services.trading.execution_family_registry import venue_for_execution_family
    assert venue_for_execution_family("alpaca_spot") == "alpaca"
    assert venue_for_execution_family("robinhood_agentic_mcp") == "robinhood"
    assert "venue_for_execution_family(EXEC_FAMILY)" in _body("run_arm")


# ─── D. THE STRUCTURED RESULT ────────────────────────────────────────────────────────────

_REQUIRED_JSON_KEYS = (
    "schema", "tree", "env", "tape_sources", "sink_reset", "mirrored", "density",
    "grid_steps", "mock", "seed_session_id", "execution_family", "final_state",
    "states_visited", "certification_failures", "fills", "pnl_usd", "mtm_usd",
    "event_histogram", "events",
)


def _receipt_keys() -> set[str]:
    """The literal dict handed to _write_run_json inside run_arm."""
    for node in ast.walk(_fn("run_arm")):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_write_run_json"):
            for arg in node.args:
                if isinstance(arg, ast.Dict):
                    return {k.value for k in arg.keys if isinstance(k, ast.Constant)}
    raise AssertionError("walang _write_run_json(...) na may dict literal sa run_arm")


def test_the_schema_key_is_pinned():
    assert drv.REPLAY_RESULT_SCHEMA == "chili.replay_v3_fsm_window_result.v1"


@pytest.mark.parametrize("key", _REQUIRED_JSON_KEYS)
def test_the_receipt_carries_every_required_field(key):
    assert key in _receipt_keys()


def test_the_receipt_is_written_before_the_cleanup_deletes_the_evidence():
    body = _body("run_arm")
    assert body.index("_write_run_json(") < body.index("# cleanup this arm's rows")


def test_no_json_out_means_no_file(monkeypatch):
    """Default OFF: unset REPLAY_JSON_OUT writes nothing and changes nothing."""
    monkeypatch.setattr(drv, "REPLAY_JSON_OUT", "")
    assert drv._json_out_path(True) is None


def test_both_arms_get_their_own_receipt(monkeypatch, tmp_path):
    """⚠️ ARM=both replays the SAME window twice; one path would silently keep only the
    second arm."""
    monkeypatch.setattr(drv, "REPLAY_JSON_OUT", str(tmp_path / "run.json"))
    monkeypatch.setenv("ARM", "both")
    on, off = drv._json_out_path(True), drv._json_out_path(False)
    assert on != off and on.endswith("run.g4_on.json") and off.endswith("run.g4_off.json")
    monkeypatch.setenv("ARM", "on")
    assert drv._json_out_path(True) == str(tmp_path / "run.json")


def test_the_writer_round_trips_and_creates_its_directory(tmp_path):
    out = tmp_path / "nested" / "dir" / "run.json"
    drv._write_run_json(str(out), {"schema": drv.REPLAY_RESULT_SCHEMA, "pnl_usd": 1.5})
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema"] == drv.REPLAY_RESULT_SCHEMA
    # newline="" — Windows text mode would rewrite every \n and change the bytes of an
    # otherwise identical receipt (reference_python_write_text_crlf_windows).
    assert b"\r\n" not in out.read_bytes()


def test_the_payload_whitelist_extends_the_parity_fixture_set():
    """Reuses ``_load_bearing_payload`` from the parity-fixture exporter, plus the facts a
    bench scorer needs: WHY a decision went the way it did."""
    payload = {"fill_price": 4.21, "reason": "double_bottom_break", "trigger": "tick_ok",
               "blocked_trigger": "benched_at_hod", "benched_at_hod": True,
               "viability_score": 0.91, "errors": ["x"], "not_load_bearing": "drop me"}
    keep = drv._bench_payload("live_entry_filled", payload)
    assert keep["fill_price"] == 4.21          # from the parity fixture set
    for k in ("reason", "trigger", "blocked_trigger", "benched_at_hod",
              "viability_score", "errors"):
        assert k in keep, k
    assert "not_load_bearing" not in keep


def test_the_env_contract_echoes_the_new_knobs():
    env = drv._env_contract()
    for key in ("SOURCE_FILTER", "EXEC_FAMILY", "TICK_STRIDE", "GRID_STEP_S",
                "WIN_START", "WIN_END", "BENCH_QUESTION"):
        assert key in env, key


# ─── E. STARTUP INVARIANTS ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("call", [
    "assert_dense_stride(TICK_STRIDE, BENCH_QUESTION)",
    "assert_clean_sink(os.environ)",
    "assert_as_of_reads(_src)",
    "assert_tie_stable_sql(_src)",
])
def test_the_driver_fails_closed_at_startup(call):
    assert call in _body("_startup_invariants")


def test_the_startup_invariants_run_before_anything_touches_a_database():
    main = _body("main")
    assert main.index("_startup_invariants()") < main.index("_reset_sim_sink()")


def test_the_mock_parity_invariant_runs_on_the_constructed_mock():
    body = _body("run_arm")
    assert body.index("MockBrokerAdapter(") < body.index("assert_mock_parity(mock)")
    assert body.index("assert_mock_parity(mock)") < body.index("driver.run()")


# ─── F. THE NBBO MIRROR'S SIDE EFFECT: A DORMANT LOOK-AHEAD SWITCHED ON ──────────────────
#
# The mirror pre-loads the ENTIRE window into the sink at t=0, so every read the FSM makes
# against momentum_nbbo_spread_tape must be as-of bounded or it reads the FUTURE. Two of the
# three target readers already were: _build_micro_bar_df (live_runner:23426) and
# recent_bid_spread_tape (nbbo_tape:1228) both bind `observed_at <= :now`. The third,
# spread_cost_veto.name_spread_percentiles, bound only `>= :since` and is reached from
# live_runner:38905 WITHOUT now_utc — i.e. against the real WALL clock.
#
# MEASURED (chili_test, 60-minute tape, first 30 min @20 bps, last 30 min @400 bps,
# source='replay_v3' exactly as the mirror writes, sim-now = window + 35 min):
#     before: p50=210.0 p75=400.0 p90=400.0 n=60      <- 24 rows that had not happened yet
#     after:  p50=20.0                        n=36
# a 10.5x error in the name's OWN "typical spread" baseline. The gate is default-ON
# (config.py:6936 Field(default=True), with an explicit note that the "DEFAULT FALSE" prose
# is wrong) and composes into _eff_max_loss at live_runner:38925 — it scales position size.
# Worse, with the wall clock the `since` bound walks off the replayed day entirely, so a tape
# older than chili_momentum_spread_norm_lookback_days (20.0) returns None and the gate fails
# open: the verdict then depends on the CALENDAR DATE the bench happens to run, which defeats
# the byte-identical-double-run premise this harness asserts everywhere else.

_SCV = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "trading" / "momentum_neural" / "spread_cost_veto.py"


def test_the_spread_percentile_read_is_as_of_bounded():
    """Without an upper bound the mirrored window is read in FULL at every sim instant."""
    src = _SCV.read_text(encoding="utf-8")
    assert "observed_at <= :now" in src, (
        "name_spread_percentiles has no as-of upper bound — with the NBBO mirror in place "
        "it reads rows that had not happened yet at the moment of the decision."
    )


def test_the_spread_percentile_read_binds_the_upper_bound():
    """A predicate with no bound parameter raises rather than silently filtering nothing."""
    src = _SCV.read_text(encoding="utf-8")
    assert '"now": now_utc.replace(tzinfo=None)' in src


def test_the_driver_repoints_the_spread_distribution_at_the_sim_clock():
    """live_runner:38905 passes no now_utc, so unpatched this read uses the WALL clock."""
    body = _body("run_arm")
    assert "_scv.name_spread_percentiles = _nsp_simclock" in body
    assert "lr._utcnow()" in body.split("_nsp_simclock")[1][:400]


def test_the_repoint_is_a_required_sim_clock_anchor():
    """So losing it fails invariant 4 at startup instead of quietly moving the sizing."""
    inv = importlib.import_module("replay_harness_invariants")
    assert "name_spread_percentiles" in inv.REQUIRED_SIM_CLOCK_ANCHORS


def test_the_mock_parity_invariant_also_fences_at_startup():
    """Invariant 9 must abort BEFORE the sink reset and the three multi-minute mirrors."""
    body = _body("_startup_invariants")
    assert "assert_mock_parity(rv3.MockBrokerAdapter(**_PARITY_MOCK_KWARGS))" in body


def test_the_startup_mock_and_the_run_mock_cannot_drift():
    """One definition, two construction sites — a knob checked but not used is no check."""
    body = _body("run_arm")
    assert "rv3.MockBrokerAdapter(**_PARITY_MOCK_KWARGS)" in body


# ═══════════════════════════════════════════════════════════════════════════════════
# The receipt keeps breaker / blocker attribution (2026-09-04)
# ═══════════════════════════════════════════════════════════════════════════════════

def test_receipt_keeps_breaker_attribution():
    """SDOT alpaca: 26 breaker blocks arrived as ``{}`` -- the runner had named the breaker
    and this filter dropped it. The receipt exists to carry WHY."""
    payload = {"breaker": "daily_loss_cap_broker", "family": "alpaca_spot", "daily_pnl_usd": None,
               "max_daily_loss_usd": 5000.0, "transient": True, "sticky": False,
               "reason": "alpaca_account_daily_change_unavailable", "source": "settings",
               "some_debug_blob": {"x": 1}}
    keep = drv._bench_payload("live_entry_blocked_by_breaker", payload)
    for k in ("breaker", "family", "daily_pnl_usd", "max_daily_loss_usd", "transient", "reason", "source"):
        assert k in keep, k
    assert "some_debug_blob" not in keep


def test_receipt_keeps_adaptive_risk_blocker_detail():
    keep = drv._bench_payload("live_entry_adaptive_risk_blocked",
                              {"reason": "adaptive_risk_builder_source_invalid",
                               "error_type": "TypeError", "detail": "float() argument"})
    assert keep == {"reason": "adaptive_risk_builder_source_invalid",
                    "error_type": "TypeError", "detail": "float() argument"}
