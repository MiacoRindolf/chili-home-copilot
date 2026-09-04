"""Corpus selection and tape-density verification must REJECT the shapes that
made the ledger dangerous, not merely accept the clean ones.

Every test here names the measured property of ``chili.ross_master_ledger.v1``
(or of ``scripts/replay_v3_fsm_window.py``) that it is defending. The ledger is a
26-batch transcript merge, so the dangerous shapes are all real: ``0`` used as a
null sentinel, a narrative ``entry_time_et``, three account vocabularies, and 30
rows that are not trades at all sitting in ``ledger['trades']``.

DB-FREE BY CONSTRUCTION. ``scripts/rossbench_density_check`` takes injectable
connection factories and parses the driver's SQL out of its SOURCE, so nothing
below opens a socket. That is deliberate: a live trading lane and a shared
``_test`` database run on the same machine as this suite.

Runnable: pytest tests/test_rossbench_corpus.py -v
"""
from __future__ import annotations

import csv
import io
import json
import pathlib
import sys
import tempfile
from datetime import date, datetime, timezone

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import rossbench_corpus as rc  # noqa: E402
import rossbench_density_check as rd  # noqa: E402

DRIVER_SRC = (_SCRIPTS / "replay_v3_fsm_window.py").read_text(encoding="utf-8")
LEDGER_PATH = _ROOT / "project_ws" / "AgentOps" / "ross" / "ross_master_ledger.json"

TODAY = date(2026, 9, 4)  # fixed, so the 180-day cliff in these tests never moves


# ─────────────────────────────────────────────────────────────────────────────
# 1. THE NULL SENTINEL — the single most dangerous field convention in the ledger
# ─────────────────────────────────────────────────────────────────────────────

def test_zero_pnl_is_none_not_a_flat_trade():
    """⚠️ MEASURED: 30 of the 157 trade rows carry pnl_usd == 0 because the
    transcript never stated a number. Scoring one as a real flat credits CHILI
    with a scratch where Ross actually had an unrecoverable magnitude."""
    assert rc.sentinel_to_none(0) is None
    assert rc.sentinel_to_none(0.0) is None


@pytest.mark.parametrize("field_zeros", [("entry_px", 67), ("exit_px", 103),
                                         ("shares", 118), ("pnl_usd", 30)])
def test_every_sentinel_field_is_declared(field_zeros):
    """The zero counts that motivated the rule are recorded in the module, so a
    field cannot quietly leave the sentinel list."""
    field, _count = field_zeros
    assert field in rc.NULL_SENTINEL_FIELDS


def test_real_values_survive_and_junk_does_not():
    assert rc.sentinel_to_none(-25431.11) == -25431.11
    assert rc.sentinel_to_none(4.45) == 4.45
    assert rc.sentinel_to_none(None) is None
    assert rc.sentinel_to_none("not stated") is None
    # bool is an int subclass; True must never become a price of 1.0.
    assert rc.sentinel_to_none(True) is None


def test_outcome_of_none_is_unknown_not_flat():
    assert rc.outcome_of(None) == "unknown"
    assert rc.outcome_of(1.0) == "win"
    assert rc.outcome_of(-1.0) == "loss"


# ─────────────────────────────────────────────────────────────────────────────
# 2. entry_time_et IS A NARRATIVE FIELD
# ─────────────────────────────────────────────────────────────────────────────

def test_leading_clock_is_parsed_and_labelled():
    """The 132/157 shape."""
    assert rc.parse_entry_clock("~09:20 (break of 10.00 = VWAP 10.04)") == (
        "09:20", rc.CLOCK_BASIS_LEADING)
    assert rc.parse_entry_clock("08:11-08:13 (tape-pinned; 12:11-12:13Z)") == (
        "08:11", rc.CLOCK_BASIS_LEADING)


def test_mid_sentence_clock_is_found_but_flagged_weaker():
    """The further 14 rows. Returning None for these would throw away a real
    clock; returning LEADING for them would overstate the evidence."""
    clock, basis = rc.parse_entry_clock("not stated (he called it around 09:41)")
    assert clock == "09:41"
    assert basis == rc.CLOCK_BASIS_MID_SENTENCE


def test_clockless_narrative_is_absent():
    """The 11 rows with no clock at all — verbatim from the ledger."""
    assert rc.parse_entry_clock(
        "not stated ('ADBB hit the scanner just as VCIG started to pull back')"
    ) == (None, rc.CLOCK_BASIS_ABSENT)
    assert rc.parse_entry_clock(None) == (None, rc.CLOCK_BASIS_ABSENT)
    assert rc.parse_entry_clock("") == (None, rc.CLOCK_BASIS_ABSENT)


# ─────────────────────────────────────────────────────────────────────────────
# 3. THREE ACCOUNT VOCABULARIES
# ─────────────────────────────────────────────────────────────────────────────

def test_big_collapses_to_main():
    """⚠️ MEASURED: main 49 / small 25 / big 16 / absent 67.
    build_ross_manifest._norm_account (:122) already treats big and main as one
    account; if these two builders
    disagreed, the same trade would land in two accounts."""
    assert rc.normalize_account("big") == "main"
    assert rc.normalize_account("main") == "main"
    assert rc.normalize_account("Big account") == "main"
    assert rc.normalize_account("small") == "small"
    assert rc.normalize_account(None) is None
    assert rc.normalize_account("brokerage") is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. RETENTION AND PROVIDER
# ─────────────────────────────────────────────────────────────────────────────

def test_expires_on_is_the_same_cliff_the_hydrator_enforces():
    """``day + 180 >= today`` and ``day >= today - 180`` must agree on every day
    around the boundary, or the corpus and the hydrator would disagree about what
    is still buyable."""
    from historical_tick_hydrator import iqfeed_retention_floor
    floor = iqfeed_retention_floor(TODAY)
    for delta in range(-3, 4):
        day = date.fromordinal(floor.toordinal() + delta)
        assert (rc.expires_on(day) >= TODAY) == (day >= floor), day


def test_inside_retention_routes_to_iqfeed():
    provider, reason = rc.provider_for(date(2026, 8, 17), today=TODAY)
    assert provider == rc.PROVIDER_IQFEED
    assert "retention" in reason


def test_past_the_cliff_routes_to_polygon():
    """⚠️ The 180-day cliff is a HARD ceiling measured in Phase 1, and the
    hydrator fails such a request loudly. Anything older must be routed, not
    retried against IQFeed."""
    provider, reason = rc.provider_for(date(2025, 11, 11), today=TODAY)
    assert provider == rc.PROVIDER_POLYGON
    assert "cliff" in reason


def test_unrecoverable_is_named_not_silent():
    assert rc.provider_for(None, today=TODAY)[0] == rc.PROVIDER_UNRECOVERABLE
    assert rc.provider_for(date(2027, 1, 1), today=TODAY)[0] == rc.PROVIDER_UNRECOVERABLE
    assert rc.provider_for(date(1999, 1, 1), today=TODAY)[0] == rc.PROVIDER_UNRECOVERABLE
    # The empirical case beats the date arithmetic: every provider we tried came
    # back with nothing.
    provider, reason = rc.provider_for(date(2026, 8, 17), today=TODAY, jobs_exhausted=True)
    assert provider == rc.PROVIDER_UNRECOVERABLE
    assert "hydration_jobs" in reason


# ─────────────────────────────────────────────────────────────────────────────
# 5. THE HYDRATION LEDGER — "done" IS NOT COVERAGE
# ─────────────────────────────────────────────────────────────────────────────

def test_db_unavailable_is_not_the_same_as_no_jobs():
    """A dead database must never look like an un-hydrated corpus."""
    assert rc.roll_up_hydration(None)["hydration_status"] == rc.HYDRATION_UNKNOWN
    assert rc.roll_up_hydration(None)["replayable"] is None
    assert rc.roll_up_hydration([])["hydration_status"] == rc.HYDRATION_ABSENT
    assert rc.roll_up_hydration([])["replayable"] is False


def test_done_with_zero_rows_is_a_hole_not_coverage():
    """⚠️ docs/HISTORICAL_TICK_HYDRATOR.md: 'a job marked done that produced zero
    rows is a hole, not coverage'."""
    out = rc.roll_up_hydration([
        {"dataset": "trades", "provider": "iqfeed", "status": "done", "rows_loaded": 0},
        {"dataset": "nbbo", "provider": "polygon", "status": "done", "rows_loaded": 0},
    ])
    assert out["replayable"] is False
    assert out["hydration_status"] == "jobs_present_zero_rows"


def test_trades_without_nbbo_is_not_replayable():
    """⚠️ _confidence returns no_tape the moment the NBBO tape is empty and
    bar-candidate generation is NBBO-guarded, so a trades-only symbol-day yields
    ZERO candidates — not few."""
    out = rc.roll_up_hydration([
        {"dataset": "trades", "provider": "iqfeed", "status": "done", "rows_loaded": 185_005},
    ])
    assert out["replayable"] is False
    assert out["hydration_status"] == "tape_only_not_replayable"
    assert out["hydrated_trade_rows"] == 185_005


def test_trades_and_nbbo_is_replayable():
    out = rc.roll_up_hydration([
        {"dataset": "trades", "provider": "iqfeed", "status": "done", "rows_loaded": 16_933},
        {"dataset": "nbbo", "provider": "polygon", "status": "done", "rows_loaded": 7_076},
    ])
    assert out["replayable"] is True
    assert out["hydration_status"] == "replayable_trades_and_nbbo"


def test_exhausted_only_when_nothing_is_still_pending():
    exhausted = rc.roll_up_hydration([
        {"dataset": "trades", "provider": "iqfeed", "status": "no_data", "rows_loaded": 0},
        {"dataset": "trades", "provider": "polygon", "status": "failed", "rows_loaded": 0},
    ])
    assert exhausted["jobs_exhausted"] is True
    still_going = rc.roll_up_hydration([
        {"dataset": "trades", "provider": "iqfeed", "status": "no_data", "rows_loaded": 0},
        {"dataset": "nbbo", "provider": "polygon", "status": "pending", "rows_loaded": 0},
    ])
    assert still_going["jobs_exhausted"] is False


def test_fetch_hydration_jobs_never_raises_on_a_dead_database():
    """The corpus must still build when chili_hydrated is down — every row simply
    degrades to unknown, WITH the reason recorded."""
    def _boom(_dbname, _env):
        raise OSError("could not connect to server")
    jobs, err = rc.fetch_hydration_jobs([("SDOT", date(2026, 6, 26))], connect=_boom)
    assert jobs is None
    assert "could not connect" in err


# ─────────────────────────────────────────────────────────────────────────────
# 6. LEDGER SHAPE GUARDS
# ─────────────────────────────────────────────────────────────────────────────

def _ledger(trades=(), xref=(), worklist=()):
    return {"schema": rc.LEDGER_SCHEMA, "trades": list(trades),
            "xref": list(xref), "hydration_worklist": list(worklist)}


def test_a_foreign_schema_is_refused():
    with pytest.raises(AssertionError, match="ledger schema"):
        rc.assert_ledger_shape({"schema": "chili.ross_master_ledger.v2",
                                "trades": [], "xref": []})


def test_an_unknown_path_is_refused_rather_than_defaulted():
    """⚠️ _path is the ONLY thing separating 157 trades from 30 no-trade records.
    A new sub-schema silently falling to either side would corrupt both Capture
    and Avoidance."""
    with pytest.raises(AssertionError, match="unknown _path"):
        rc.assert_ledger_shape(_ledger(trades=[{"_path": "brand_new_list"}]))


def test_lane_alive_must_be_backed_by_the_ledgers_own_verdict():
    """The 8-case head is an operator input, so it cannot be derived — but it can
    be falsified. An unbacked pair would put an unproven row at the head."""
    led = _ledger(xref=[{"symbol": "SDOT", "date": "2026-06-26",
                         "chili_verdict": "not_in_universe"}])
    with pytest.raises(AssertionError, match="lane-alive"):
        rc.assert_lane_alive_supported(led, [("SDOT", "2026-06-26")])
    led["xref"][0]["chili_verdict"] = "armed_no_entry"
    rc.assert_lane_alive_supported(led, [("SDOT", "2026-06-26")])


def test_the_declared_eight_are_all_armed_in_the_real_ledger():
    """The cross-check that keeps the operator's list and the ledger honest."""
    if not LEDGER_PATH.exists():
        pytest.skip("ross_master_ledger.json not present in this tree")
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    rc.assert_lane_alive_supported(ledger)  # raises and names the row if it drifts
    assert len(rc.LANE_ALIVE_SYMBOL_DAYS) == 8


# ─────────────────────────────────────────────────────────────────────────────
# 7. TAPE CONFIRMATION vs REPLAYABILITY — deliberately two different predicates
# ─────────────────────────────────────────────────────────────────────────────

def test_recorded_nbbo_only_is_confirmed_but_not_replayable():
    """WETO/PFSA/SLE are ``recorded_nbbo_only`` in the ledger: a tape for that
    symbol-day is an established fact AND the FSM cannot yet be driven over it."""
    hyd = rc.roll_up_hydration([])
    confirmed, basis = rc.tape_confirmation(
        ("WETO", "2026-08-17"),
        coverage={("WETO", "2026-08-17"): "recorded_nbbo_only"},
        tape_pins=set(), hydration=hyd)
    assert confirmed is True
    assert "recorded_nbbo_only" in basis
    assert hyd["replayable"] is False


def test_a_tape_pin_confirms_even_without_coverage():
    confirmed, basis = rc.tape_confirmation(
        ("PFSA", "2026-08-18"), coverage={}, tape_pins={("PFSA", "2026-08-18")},
        hydration=rc.roll_up_hydration([]))
    assert confirmed is True
    assert "tape_pin" in basis


def test_none_must_hydrate_is_not_confirmed():
    confirmed, basis = rc.tape_confirmation(
        ("SDOT", "2026-06-26"),
        coverage={("SDOT", "2026-06-26"): "none_must_hydrate"},
        tape_pins=set(), hydration=rc.roll_up_hydration([]))
    assert confirmed is False
    assert "none_must_hydrate" in basis


# ─────────────────────────────────────────────────────────────────────────────
# 8. ROW BUILD AND ORDER
# ─────────────────────────────────────────────────────────────────────────────

def _trade(symbol, day, path="trades", **kw):
    row = {"symbol": symbol, "date": day, "_path": path, "_src": "t.json",
           "video_id": "vid", "side": "long", "entry_time_et": "~09:30 (x)",
           "entry_px": 5.0, "exit_px": 6.0, "shares": 100, "pnl_usd": 1000.0,
           "confidence": "approx", "account": "main"}
    row.update(kw)
    return row


def _xref(symbol, day, verdict="armed_no_entry", coverage="none_must_hydrate", pnl=None, **kw):
    row = {"symbol": symbol, "date": day, "_path": "rows", "_src": "x.json",
           "video_id": "vid", "chili_verdict": verdict, "tape_coverage": coverage,
           "ross_pnl_usd": pnl}
    row.update(kw)
    return row


def _corpus(ledger, **kw):
    """build_corpus over a SYNTHETIC ledger, which by construction carries none of
    the eight declared lane-alive symbol-days.

    ``lane_alive=()`` is not a convenience: ``assert_lane_alive_supported`` refuses
    to build when a declared pair is not backed by the ledger's own verdict, and
    that guard firing here is correct — a synthetic three-row ledger genuinely
    cannot prove CHILI's lane was armed on 2026-06-26.
    """
    kw.setdefault("today", TODAY)
    kw.setdefault("jobs_by_pair", None)
    kw.setdefault("lane_alive", ())
    return rc.build_corpus(ledger, **kw)


def test_the_thirty_non_trades_are_separated_and_never_scoreable():
    """⚠️ MEASURED: 30 of 187 rows are no-trade/miss records merged in from five
    sub-schemas. Scoring one as a trade credits CHILI for an entry Ross refused."""
    led = _ledger(trades=[_trade("AAA", "2026-08-17"),
                          _trade("BBB", "2026-08-17", path="no_trade_references"),
                          _trade("CCC", "2026-08-17", path="misses_and_no_trades"),
                          _trade("DDD", "2026-08-17", path="ross_no_trade_context")])
    rows = rc.build_rows(led, today=TODAY, jobs_by_pair=None, lane_alive=())
    kinds = {r["symbol"]: r["record_kind"] for r in rows}
    assert kinds["AAA"] == rc.RECORD_KIND_TRADE
    assert kinds["BBB"] == kinds["CCC"] == kinds["DDD"] == rc.RECORD_KIND_NO_TRADE
    for r in rows:
        if r["record_kind"] == rc.RECORD_KIND_NO_TRADE:
            assert r["scoreable"] is False
            assert "not a Ross trade" in r["scoreable_reason"]


def test_a_zero_entry_px_row_is_not_scoreable():
    led = _ledger(trades=[_trade("IPST", "2026-08-17", entry_px=0)])
    row = rc.build_rows(led, today=TODAY, jobs_by_pair=None, lane_alive=())[0]
    assert row["entry_px"] is None
    assert row["scoreable"] is False
    assert "sentinel" in row["scoreable_reason"]


def test_a_row_past_the_cliff_is_not_scoreable_but_is_still_carried():
    led = _ledger(trades=[_trade("QNRX", "2025-11-11")])
    row = rc.build_rows(led, today=TODAY, jobs_by_pair=None, lane_alive=())[0]
    assert row["scoreable"] is False
    assert row["iqfeed_retention_ok"] is False
    assert row["provider"] == rc.PROVIDER_POLYGON  # carried, routed, not dropped


def test_xref_only_symbol_days_are_emitted():
    """⚠️ FOUR of the eight declared lane-alive cases (UPC 06-29, WETO 08-17,
    PFSA/SLE 08-18) have NO row in ledger['trades'] at all. A corpus built from
    the trades list alone silently omits half the bench's head."""
    led = _ledger(trades=[_trade("AAA", "2026-08-17")],
                  xref=[_xref("UPC", "2026-06-29", pnl=39000.0)])
    rows = rc.build_rows(led, today=TODAY, jobs_by_pair=None,
                         lane_alive=[("UPC", "2026-06-29")])
    upc = [r for r in rows if r["symbol"] == "UPC"][0]
    assert upc["record_kind"] == rc.RECORD_KIND_XREF
    assert upc["lane_alive"] is True
    assert upc["pnl_usd"] == 39000.0


def test_an_xref_row_does_not_duplicate_a_symbol_day_that_has_trades():
    led = _ledger(trades=[_trade("SDOT", "2026-06-26")],
                  xref=[_xref("SDOT", "2026-06-26", pnl=5885.15)])
    rows = rc.build_rows(led, today=TODAY, jobs_by_pair=None, lane_alive=())
    assert [r["record_kind"] for r in rows] == [rc.RECORD_KIND_TRADE]


def test_order_puts_tape_confirmed_wins_first_then_lane_alive():
    led = _ledger(
        trades=[
            _trade("LOSS", "2026-08-17", pnl_usd=-9000.0),
            _trade("PLAIN", "2026-08-17", pnl_usd=500.0),
            _trade("CONF", "2026-08-17", pnl_usd=100.0),
            _trade("ALIVE", "2026-08-17", pnl_usd=50.0),
        ],
        xref=[_xref("CONF", "2026-08-17", coverage="recorded_ticks"),
              _xref("ALIVE", "2026-08-17", coverage="recorded_ticks")],
    )
    rows = rc.build_rows(led, today=TODAY, jobs_by_pair=None,
                         lane_alive=[("ALIVE", "2026-08-17")])
    rows.sort(key=rc.order_key)
    order = [r["symbol"] for r in rows]
    # tier 0 = tape-confirmed wins; ALIVE leads it because it is lane-alive even
    # though CONF carries more dollars.
    assert order[:2] == ["ALIVE", "CONF"]
    # PLAIN is a win but has no tape confirmation, so it drops to tier 1.
    assert order[2] == "PLAIN"
    # The loss is the negative control: kept, and last.
    assert order[-1] == "LOSS"


def test_june_july_outranks_august_within_a_tier():
    """Coarse era, because June rows hit IQFeed's 180-day cliff first."""
    led = _ledger(trades=[_trade("AUG", "2026-08-17", pnl_usd=42100.0),
                          _trade("JUN", "2026-06-26", pnl_usd=1095.08)])
    rows = sorted(rc.build_rows(led, today=TODAY, jobs_by_pair=None, lane_alive=()),
                  key=rc.order_key)
    assert [r["symbol"] for r in rows] == ["JUN", "AUG"]


def test_dollars_still_order_within_one_era():
    """If the era key were fine-grained (per-date) the dollar key would be dead."""
    led = _ledger(trades=[_trade("SMALL", "2026-08-06", pnl_usd=100.0),
                          _trade("BIG", "2026-08-17", pnl_usd=42100.0)])
    rows = sorted(rc.build_rows(led, today=TODAY, jobs_by_pair=None, lane_alive=()),
                  key=rc.order_key)
    assert [r["symbol"] for r in rows] == ["BIG", "SMALL"]


def test_the_order_is_total_so_two_builds_are_identical():
    led = _ledger(trades=[_trade("SAME", "2026-08-17", pnl_usd=1.0),
                          _trade("SAME", "2026-08-17", pnl_usd=1.0)])
    a = _corpus(led, generated_at="fixed")
    b = _corpus(led, generated_at="fixed")
    assert rc.render_json(a) == rc.render_json(b)
    assert rc.render_csv(a) == rc.render_csv(b)


# ─────────────────────────────────────────────────────────────────────────────
# 9. OUTPUT CONTRACTS
# ─────────────────────────────────────────────────────────────────────────────

def test_csv_leads_with_symbol_and_date_so_the_hydrator_can_eat_it():
    """read_pairs_csv (historical_tick_hydrator.py:1472) needs a symbol column and
    one of date/trading_day/session_date/day, and preserves first-seen order."""
    assert rc.CSV_COLUMNS[:2] == ("symbol", "date")
    led = _ledger(trades=[_trade("AAA", "2026-08-17")])
    text = rc.render_csv(_corpus(led))
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = {f.lower() for f in reader.fieldnames}
    assert "symbol" in fields and "date" in fields
    row = next(reader)
    assert row["symbol"] == "AAA" and row["date"] == "2026-08-17"


def test_csv_uses_lf_not_crlf():
    """⚠️ Windows text mode plus csv's default \\r\\n would change the bytes of an
    otherwise identical corpus and make every --check look like drift."""
    led = _ledger(trades=[_trade("AAA", "2026-08-17")])
    text = rc.render_csv(_corpus(led))
    assert "\r\n" not in text


def test_json_hides_the_internal_sort_fields():
    led = _ledger(trades=[_trade("AAA", "2026-08-17")])
    doc = json.loads(rc.render_json(_corpus(led)))
    assert doc["schema"] == rc.SCHEMA
    assert not any(k.startswith("_") for k in doc["rows"][0])
    assert doc["rows"][0]["rank"] == 1


def test_unrecoverable_rows_are_reported_and_kept():
    """An unrecoverable row is REPORTED, never silently dropped — a corpus that
    quietly shrinks is how a bench comes to measure a subset and call it whole."""
    led = _ledger(trades=[_trade("OLD", "1999-01-04")])
    doc = _corpus(led)
    assert doc["counts"]["unrecoverable"] == 1
    assert doc["unrecoverable"][0]["symbol"] == "OLD"
    assert len(doc["rows"]) == 1  # still in the corpus


def test_a_missing_hydration_ledger_is_announced_not_assumed():
    doc = _corpus(_ledger(trades=[_trade("AAA", "2026-08-17")]),
                  hydration_error="skipped (--no-db)")
    assert doc["hydration_lookup"]["read"] is False
    assert doc["hydration_lookup"]["error"] == "skipped (--no-db)"
    assert doc["rows"][0]["hydration_status"] == rc.HYDRATION_UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# 10. AGAINST THE REAL LEDGER
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def real_corpus():
    if not LEDGER_PATH.exists():
        pytest.skip("ross_master_ledger.json not present in this tree")
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    return rc.build_corpus(ledger, today=TODAY, jobs_by_pair=None,
                           hydration_error="skipped (test)", generated_at="fixed")


def test_real_ledger_splits_157_trades_from_30_non_trades(real_corpus):
    """The measured partition of the 187-row ledger."""
    by_kind = real_corpus["counts"]["by_record_kind"]
    assert by_kind[rc.RECORD_KIND_TRADE] == 157
    assert by_kind[rc.RECORD_KIND_NO_TRADE] == 30


def test_real_ledger_scoreable_core_is_49_wins_and_21_losses(real_corpus):
    """⚠️ The honest scoreable core: clock AND entry_px > 0 AND inside IQFeed's
    180-day retention. The 16 remaining rows carry the 0 pnl sentinel, so they are
    scoreable for entry GEOMETRY and carry no dollar truth — which is exactly why
    ``outcome`` is a separate field and not folded into ``scoreable``."""
    c = real_corpus["counts"]
    assert c["scoreable_wins"] == 49
    assert c["scoreable_losses"] == 21
    assert c["scoreable_pnl_unknown"] == 16
    assert c["scoreable"] == 86


def test_all_eight_lane_alive_symbol_days_appear_in_the_real_corpus(real_corpus):
    present = {(r["symbol"], r["date"]) for r in real_corpus["rows"] if r["lane_alive"]}
    for pair in rc.LANE_ALIVE_SYMBOL_DAYS:
        assert pair in present, pair


def test_real_corpus_head_is_a_lane_alive_tape_confirmed_win(real_corpus):
    head = real_corpus["rows"][0]
    assert head["lane_alive"] is True
    assert head["tape_confirmed"] is True
    assert head["outcome"] == "win"


# ═════════════════════════════════════════════════════════════════════════════
# DENSITY CHECK
# ═════════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# 11. THE PREDICATE COMES OUT OF THE DRIVER — the anti-drift test
# ─────────────────────────────────────────────────────────────────────────────

def test_the_drivers_real_predicates_are_what_gets_counted():
    """⚠️ A density check that used any other predicate would measure a different
    population than the driver's mirrors and report a number the driver will never
    see. If this test starts failing, the DRIVER changed and this module is
    already following it — update the expectation, never the extraction."""
    trades_sql, trades_table, trades_pred = rd.derive_count_sql(
        rd.read_driver_sql(rd.MIRROR_CONSTANTS["trades"], DRIVER_SRC),
        with_source_filter=False)
    nbbo_sql, nbbo_table, nbbo_pred = rd.derive_count_sql(
        rd.read_driver_sql(rd.MIRROR_CONSTANTS["nbbo"], DRIVER_SRC),
        with_source_filter=False)
    assert trades_table == "iqfeed_trade_ticks"
    assert nbbo_table == "momentum_nbbo_spread_tape"
    assert trades_pred == "price>0"
    # Logically the same population as the doc's "bid > 0 AND ask > 0 AND
    # ask >= bid"; this is the driver's spelling, which is the one that runs.
    assert nbbo_pred == "bid>0 AND ask>=bid"
    assert trades_sql.startswith("SELECT count(*) FROM iqfeed_trade_ticks WHERE")
    assert "ORDER BY" not in trades_sql and "ORDER BY" not in nbbo_sql
    assert "{source}" not in trades_sql and "{source}" not in nbbo_sql


def test_the_source_filter_slot_is_filled_with_the_drivers_own_clause():
    sql, _t, _p = rd.derive_count_sql(
        rd.read_driver_sql(rd.MIRROR_CONSTANTS["trades"], DRIVER_SRC),
        with_source_filter=True)
    assert sql.endswith("AND source = ANY(%s)")
    rd.assert_source_predicate_shape(DRIVER_SRC)  # the driver still builds it this way


def test_a_renamed_mirror_constant_fails_loudly():
    with pytest.raises(AssertionError, match="ABSENT"):
        rd.read_driver_sql("_NO_SUCH_SQL", DRIVER_SRC)


def test_a_computed_sql_constant_is_refused_rather_than_guessed():
    src = '_TRADE_MIRROR_SQL = "SELECT a FROM t WHERE " + WHATEVER\n'
    with pytest.raises(AssertionError, match="plain string constant"):
        rd.read_driver_sql("_TRADE_MIRROR_SQL", src)


def test_a_subquery_shape_is_refused_rather_than_counted_wrongly():
    """The stride-applying ``_TRADE_TAPE_SQL`` has this shape. Counting it with the
    flat rewrite would count the inner relation and silently report a different
    number."""
    with pytest.raises(AssertionError, match="subquery"):
        rd.derive_count_sql(
            "SELECT observed_at FROM ( SELECT id FROM iqfeed_trade_ticks "
            "WHERE symbol=%s AND observed_at>=%s AND observed_at<%s AND price>0"
            "{source} ) q ORDER BY observed_at ASC, id ASC",
            with_source_filter=False)


def test_a_predicate_that_vanished_is_refused():
    with pytest.raises(AssertionError, match="validity predicate"):
        rd.derive_count_sql(
            "SELECT observed_at FROM iqfeed_trade_ticks WHERE symbol=%s "
            "AND observed_at>=%s AND observed_at<%s ORDER BY observed_at ASC",
            with_source_filter=False)


def test_a_driver_that_changed_its_source_clause_is_refused():
    with pytest.raises(AssertionError, match="provenance predicate"):
        rd.assert_source_predicate_shape("nothing resembling a source filter here")


# ─────────────────────────────────────────────────────────────────────────────
# 12. THE CLOCK ASYMMETRY
# ─────────────────────────────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, calls, rows):
        self._calls, self._rows = calls, rows
        self._n = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._calls.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        out = self._rows[self._n] if self._n < len(self._rows) else (0,)
        self._n += 1
        return out


class _FakeConn:
    def __init__(self, rows=((0,),)):
        self.calls = []
        self._cur = _FakeCursor(self.calls, list(rows))

    def cursor(self):
        return self._cur

    def close(self):
        pass

    def rollback(self):
        pass


def test_trade_bounds_are_naive_and_nbbo_bounds_are_aware():
    """⚠️ iqfeed_trade_ticks.observed_at is TIMESTAMP (naive UTC);
    momentum_nbbo_spread_tape.observed_at is TIMESTAMPTZ. Binding a naive bound to
    the timestamptz column makes PostgreSQL coerce it through the SESSION
    TimeZone, which is harmless only while that happens to be UTC."""
    lo = datetime(2026, 6, 26, 13, 0, 0)
    hi = datetime(2026, 6, 26, 15, 0, 0)
    conn = _FakeConn(rows=[(11,), (22,)])
    rd.count_dataset(conn, "trades", "SDOT", lo, hi, driver_src=DRIVER_SRC)
    rd.count_dataset(conn, "nbbo", "SDOT", lo, hi, driver_src=DRIVER_SRC)
    trade_params = conn.calls[0][1]
    nbbo_params = conn.calls[1][1]
    assert trade_params[1].tzinfo is None and trade_params[2].tzinfo is None
    assert nbbo_params[1].tzinfo is timezone.utc
    assert nbbo_params[2].tzinfo is timezone.utc


def test_the_source_list_is_bound_only_when_a_filter_is_configured():
    lo, hi = datetime(2026, 6, 26, 13), datetime(2026, 6, 26, 15)
    plain = _FakeConn()
    rd.count_dataset(plain, "trades", "SDOT", lo, hi, driver_src=DRIVER_SRC)
    assert len(plain.calls[0][1]) == 3
    filtered = _FakeConn()
    rd.count_dataset(filtered, "trades", "SDOT", lo, hi, driver_src=DRIVER_SRC,
                     sources=("iqfeed_lookup_hist",))
    assert filtered.calls[0][1][3] == ["iqfeed_lookup_hist"]
    assert "source = ANY(%s)" in filtered.calls[0][0]


def test_the_live_side_sets_a_statement_timeout_before_it_counts():
    """⚠️ A VACUUM FULL just ran on chili and left the 89.6M-row tape with zero
    stats, so an unbounded count there competes with the live lane for I/O."""
    lo, hi = datetime(2026, 6, 26, 13), datetime(2026, 6, 26, 15)
    conn = _FakeConn(rows=[(1,), (1,)])
    rd.measure_side(conn, "SDOT", lo, hi, driver_src=DRIVER_SRC, statement_timeout_s=20)
    timeouts = [c for c in conn.calls if "statement_timeout" in c[0]]
    assert timeouts and timeouts[0][1] == ("20s",)
    # SET LOCAL, so the budget dies with the transaction rather than leaking onto
    # a pooled connection.
    assert timeouts[0][0].upper().startswith("SET LOCAL")


# ─────────────────────────────────────────────────────────────────────────────
# 13. GRACEFUL LIVE DEGRADATION
# ─────────────────────────────────────────────────────────────────────────────

def test_a_live_timeout_degrades_instead_of_raising():
    """A bench that dies because the live lane is busy is a bench nobody runs."""
    def _timeout():
        raise RuntimeError("canceling statement due to statement timeout")
    live, err = rd.measure_live(_timeout, "SDOT", datetime(2026, 6, 26, 13),
                                datetime(2026, 6, 26, 15), driver_src=DRIVER_SRC)
    assert live is None
    assert "statement timeout" in err


def test_a_missing_live_database_degrades_too():
    def _gone():
        raise OSError('database "chili" does not exist')
    live, err = rd.measure_live(_gone, "SDOT", datetime(2026, 6, 26, 13),
                                datetime(2026, 6, 26, 15), driver_src=DRIVER_SRC)
    assert live is None and "does not exist" in err


def test_live_unavailable_does_not_turn_a_good_tape_into_a_failure():
    hydrated = {"trade_rows": 31_286, "nbbo_rows": 7_076}
    v = rd.verdict_for(hydrated, None, min_ratio=1.0)
    assert v["verdict"] == rd.VERDICT_OK
    assert v["ratios"] == {"trades": None, "nbbo": None}


# ─────────────────────────────────────────────────────────────────────────────
# 14. VERDICTS
# ─────────────────────────────────────────────────────────────────────────────

def test_an_empty_hydrated_tape_is_no_tape():
    v = rd.verdict_for({"trade_rows": 0, "nbbo_rows": 0}, None, min_ratio=1.0)
    assert v["verdict"] == rd.VERDICT_NO_TAPE


def test_trades_without_nbbo_is_flagged_before_any_ratio():
    """⚠️ Zero candidates, not few — the run is not a thin replay, it is no
    replay. This must not depend on the live side being reachable."""
    v = rd.verdict_for({"trade_rows": 185_005, "nbbo_rows": 0}, None, min_ratio=1.0)
    assert v["verdict"] == rd.VERDICT_NBBO_MISSING
    assert any("zero candidates" in r for r in v["reasons"])


def test_nbbo_without_trades_is_flagged():
    v = rd.verdict_for({"trade_rows": 0, "nbbo_rows": 7_076}, None, min_ratio=1.0)
    assert v["verdict"] == rd.VERDICT_TRADES_MISSING


def test_short_of_our_own_recording_is_a_density_regression():
    """⚠️ Phase 3 measured the hydrated tape as a SUPERSET of our recording
    (66-100 % complete, duplicating up to 46 % of its rows). Below 1.0 the
    hydration is incomplete — it is not that live over-recorded."""
    v = rd.verdict_for({"trade_rows": 900, "nbbo_rows": 900},
                       {"trade_rows": 1000, "nbbo_rows": 900}, min_ratio=1.0)
    assert v["verdict"] == rd.VERDICT_REGRESSION
    assert v["ratios"]["trades"] == 0.9
    assert any("superset" in r.lower() or "SUPERSET" in r for r in v["reasons"])


def test_exactly_the_floor_passes():
    v = rd.verdict_for({"trade_rows": 1000, "nbbo_rows": 900},
                       {"trade_rows": 1000, "nbbo_rows": 900}, min_ratio=1.0)
    assert v["verdict"] == rd.VERDICT_OK


def test_a_live_side_with_no_rows_is_not_a_regression():
    """hydrated ⊇ ∅ holds trivially; reporting infinity would be a lie about a
    measurement nobody made."""
    v = rd.verdict_for({"trade_rows": 500, "nbbo_rows": 500},
                       {"trade_rows": 0, "nbbo_rows": 0}, min_ratio=1.0)
    assert v["verdict"] == rd.VERDICT_OK
    assert v["ratios"] == {"trades": None, "nbbo": None}


# ─────────────────────────────────────────────────────────────────────────────
# 15. THE FLOOR IS DECLARED, NEVER SILENT
# ─────────────────────────────────────────────────────────────────────────────

def test_min_ratio_precedence_is_cli_then_env_then_documented_default():
    assert rd.resolve_min_ratio("0.95", {}) == (0.95, "--min-ratio")
    value, origin = rd.resolve_min_ratio(None, {rd.DENSITY_MIN_RATIO_ENV: "0.8"})
    assert value == 0.8 and origin == rd.DENSITY_MIN_RATIO_ENV
    value, origin = rd.resolve_min_ratio(None, {})
    assert value == rd.DENSITY_MIN_RATIO_DEFAULT == 1.0
    assert "Phase-3" in origin  # the default carries its derivation, not a taste


# ─────────────────────────────────────────────────────────────────────────────
# 16. END TO END, STILL WITHOUT A DATABASE
# ─────────────────────────────────────────────────────────────────────────────

def test_check_symbol_day_receipt_names_both_windows_and_the_verdict():
    hydrated = _FakeConn(rows=[(31_286,), (7_076,)])
    live = _FakeConn(rows=[(31_286,), (7_076,)])
    out = rd.check_symbol_day(
        "SDOT", datetime(2026, 6, 26, 13), datetime(2026, 6, 26, 15),
        driver_src=DRIVER_SRC,
        hydrated_conn_factory=lambda: hydrated,
        live_conn_factory=lambda: live,
        min_ratio=1.0)
    assert out["verdict"] == rd.VERDICT_OK
    assert out["hydrated"]["trade_rows"] == 31_286
    assert out["hydrated"]["ticks_per_second"] == pytest.approx(31_286 / 7200.0, rel=1e-6)
    # ⚠️ The driver mirrors from OHLCV_START, not WIN_START; every row says so, so
    # this rate is never compared against the driver receipt's by accident.
    assert "OHLCV_START" in out["window_note"]
    assert out["live_status"] == "measured"


def test_check_symbol_day_survives_a_dead_live_database():
    hydrated = _FakeConn(rows=[(10,), (10,)])

    def _dead():
        raise OSError("connection refused")

    out = rd.check_symbol_day(
        "SDOT", datetime(2026, 6, 26, 13), datetime(2026, 6, 26, 15),
        driver_src=DRIVER_SRC, hydrated_conn_factory=lambda: hydrated,
        live_conn_factory=_dead, min_ratio=1.0)
    assert out["live_status"] == rd.LIVE_UNAVAILABLE
    assert out["verdict"] == rd.VERDICT_OK
    assert "connection refused" in out["live_error"]


def test_read_corpus_pairs_matches_the_hydrators_column_contract():
    led = _ledger(trades=[_trade("AAA", "2026-08-17"), _trade("AAA", "2026-08-17"),
                          _trade("BBB", "2026-06-26")])
    text = rc.render_csv(_corpus(led))
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "corpus.csv"
        path.write_text(text, encoding="utf-8", newline="")
        pairs = rd.read_corpus_pairs(str(path))
    # De-duplicated, order preserved: BBB (June) leads by the era key.
    assert pairs == [("BBB", date(2026, 6, 26)), ("AAA", date(2026, 8, 17))]
