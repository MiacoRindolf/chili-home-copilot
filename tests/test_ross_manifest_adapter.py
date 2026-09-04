"""Tests for the Ross manifest -> grading-window adapter.

The adapter is the single gate where an approximate recap clock is allowed to
become an exact grading window.  These tests are therefore mostly *refusal*
tests: the interesting behaviour is what the adapter declines to certify.
"""

from __future__ import annotations

import ast
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services.trading.momentum_neural.ross_manifest_adapter import (
    ADAPTATION_SUMMARY_SCHEMA,
    END_BASIS_EXIT_PIN,
    END_BASIS_STATED,
    EVIDENCE_ROLE,
    MANIFEST_SCHEMA,
    PIN_CONFIDENCES,
    PIN_METHODS,
    PINS_SCHEMA,
    adaptation_summary,
    case_as_json_row,
    expected_actions_by_label,
    main,
    phase_windows_from_manifest,
    validated_phase_windows,
)
from app.services.trading.momentum_neural.ross_replay_benchmark import (
    ReplayTradeObservation,
    grade_recap_phase_window,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = (
    REPO_ROOT
    / "app"
    / "services"
    / "trading"
    / "momentum_neural"
    / "ross_manifest_adapter.py"
)

LABEL = "VID::ABCD::2026-06-25::t1"
# 2026-06-25 is EDT (UTC-4), so 09:31:07 ET == 13:31:07 UTC and the stated
# "10:00" end of the window text == 14:00:00 UTC.
ENTRY_UTC = datetime(2026, 6, 25, 13, 31, 7, tzinfo=timezone.utc)
STATED_END_UTC = datetime(2026, 6, 25, 14, 0, 0, tzinfo=timezone.utc)
EXIT_UTC = datetime(2026, 6, 25, 14, 5, 0, tzinfo=timezone.utc)


def _row(**overrides):
    """One manifest window in the shape scripts/build_ross_manifest.py:119-137 emits."""
    row = {
        "manifest_id": LABEL,
        "video_id": "VID",
        "date": "2026-06-25",
        "symbol": "ABCD",
        "account": "main",
        "ross_action": "trade",
        "expected_action": "trade",
        "side": "long",
        "window_et": "~09:00-10:00",
        "ross_entry_px": 1.95,
        "ross_exit_px": None,
        "ross_net_usd": 1234.5,
        "pnl_confidence": "frame_verified",
        "source": {"kind": "frame_audit", "refs": []},
        "catalyst": None,
        "notes": None,
        "tape": {"live_covered": None, "golden_pinned": None},
    }
    row.update(overrides)
    return row


def _manifest(*rows):
    return {"schema": MANIFEST_SCHEMA, "evidence_role": EVIDENCE_ROLE, "windows": list(rows)}


def _pin(**overrides):
    """One flat pin row (the layout scripts/rossbench_pin_ross_events.py describes)."""
    pin = {
        "manifest_id": LABEL,
        "symbol": "ABCD",
        "date": "2026-06-25",
        "entry_ts_utc_pinned": "2026-06-25T13:31:07Z",
        "pin_method": "price_match",
        "pin_confidence": "tape_confirmed",
    }
    pin.update(overrides)
    return pin


def _pins(*rows):
    return {"schema": PINS_SCHEMA, "pins": list(rows)}


def _one(manifest, pins):
    cases = phase_windows_from_manifest(manifest, pins)
    assert len(cases) == 1
    return cases[0]


# --- the certified path -----------------------------------------------------


def test_tape_confirmed_pin_produces_a_verified_grading_window():
    case = _one(_manifest(_row()), _pins(_pin()))

    assert case.scorable is True
    assert case.unscorable_reasons == ()
    window = case.window
    assert window is not None
    # start == decision == entry pin: a replay can never earn credit for a
    # decision made before the instant Ross's entry was pinned to.
    assert window.start_ts == ENTRY_UTC
    assert window.decision_ts == ENTRY_UTC
    assert window.end_ts == STATED_END_UTC
    assert window.evidence_source == "tape_pin:price_match"
    assert window.evidence_role == "after_fact_grading_only"
    assert window.independently_verified is True
    assert case.end_ts_basis == END_BASIS_STATED
    # The grader's own predicate must accept it.
    assert window.valid_for(label_id=LABEL, symbol="ABCD") is True


def test_exit_pin_wins_over_the_stated_end():
    case = _one(_manifest(_row()), _pins(_pin(exit_ts_utc_pinned="2026-06-25T14:05:00Z")))

    assert case.scorable is True
    assert case.window is not None
    assert case.window.end_ts == EXIT_UTC
    assert case.end_ts_basis == END_BASIS_EXIT_PIN
    assert case.exit_pin_ts == EXIT_UTC


def test_nested_pin_layout_matches_the_flat_layout():
    flat = _one(
        _manifest(_row()),
        _pins(_pin(exit_ts_utc_pinned="2026-06-25T14:05:00Z")),
    )
    nested = _one(
        _manifest(_row()),
        _pins(
            {
                "manifest_id": LABEL,
                "entry": {
                    "ts_utc_pinned": "2026-06-25T13:31:07Z",
                    "pin_method": "price_match",
                    "pin_confidence": "tape_confirmed",
                },
                "exit": {
                    "ts_utc_pinned": "2026-06-25T14:05:00Z",
                    "pin_method": "price_match",
                    "pin_confidence": "tape_confirmed",
                },
            }
        ),
    )

    assert nested.window == flat.window


def test_scorable_window_reaches_the_grader_as_a_valid_window():
    """A window this adapter certifies must not be rejected by the grader.

    ``grade_recap_phase_window`` reports ``phase_window_missing_or_unverified``
    when the window itself fails ``valid_for`` (ross_replay_benchmark.py:515-529).
    Getting ``replay_coverage_missing`` instead proves the window passed and the
    only thing still absent is the coverage proof, which a different component
    supplies.
    """
    case = _one(_manifest(_row()), _pins(_pin()))
    window = case.window
    assert window is not None

    grade = grade_recap_phase_window(
        label_id=window.label_id,
        symbol=window.symbol,
        expected_action="trade",
        trades=[ReplayTradeObservation("ABCD", window.start_ts, window.end_ts, 10.0)],
        phase_window=window,
        replay_coverage=None,
    )

    assert grade.coverage_reasons == ("replay_coverage_missing",)


# --- refusals ---------------------------------------------------------------


def test_tape_ambiguous_is_unscorable_and_leaks_no_window():
    case = _one(_manifest(_row()), _pins(_pin(pin_confidence="tape_ambiguous")))

    assert case.scorable is False
    assert case.window is None
    assert "pin_ambiguous" in case.unscorable_reasons


def test_unpinned_confidence_is_unscorable_without_a_duplicate_missing_reason():
    case = _one(
        _manifest(_row()),
        _pins({"manifest_id": LABEL, "pin_method": "stated_only", "pin_confidence": "unpinned"}),
    )

    assert case.unscorable_reasons == ("pin_unpinned",)
    assert case.window is None


def test_missing_pin_row_is_unscorable():
    case = _one(_manifest(_row()), _pins())

    assert case.window is None
    assert "pin_missing" in case.unscorable_reasons


def test_unknown_pin_confidence_is_refused_not_guessed():
    case = _one(_manifest(_row()), _pins(_pin(pin_confidence="probably_fine")))

    assert case.window is None
    assert "pin_confidence_unknown" in case.unscorable_reasons


def test_unknown_pin_method_is_refused():
    case = _one(_manifest(_row()), _pins(_pin(pin_method="vibes")))

    assert case.window is None
    assert "pin_method_unknown" in case.unscorable_reasons


def test_stated_only_method_cannot_claim_tape_confirmation():
    """A narrated clock cannot confirm itself.

    This is the failure that would quietly convert Ross's narrated entry times
    into 'verified' grading windows, so it is refused even though the pin file
    asserts ``tape_confirmed``.
    """
    case = _one(_manifest(_row()), _pins(_pin(pin_method="stated_only")))

    assert case.window is None
    assert "pin_method_confidence_contradiction" in case.unscorable_reasons


def test_exit_pin_weaker_than_entry_pin_is_refused():
    case = _one(
        _manifest(_row()),
        _pins(
            {
                "manifest_id": LABEL,
                "entry": {
                    "ts_utc_pinned": "2026-06-25T13:31:07Z",
                    "pin_method": "price_match",
                    "pin_confidence": "tape_confirmed",
                },
                "exit": {
                    "ts_utc_pinned": "2026-06-25T14:05:00Z",
                    "pin_method": "level_cross",
                    "pin_confidence": "tape_ambiguous",
                },
            }
        ),
    )

    assert case.window is None
    assert "exit_pin_not_confirmed" in case.unscorable_reasons


def test_duplicate_pin_rows_for_one_label_are_refused():
    case = _one(_manifest(_row()), _pins(_pin(), _pin()))

    assert case.window is None
    assert "pin_duplicate_rows" in case.unscorable_reasons


def test_symbol_day_ambiguity_is_not_reported_as_a_duplicate_id():
    """Two REASONS, two meanings.  ``pin_duplicate_rows`` means two pin rows
    claim the same manifest_id -- a producer bug.  A window with no pin of its
    own, on a symbol-day the pins file happens to cover twice, is the ordinary
    shape of a symbol Ross traded twice; calling that a duplicate id reported 79
    producer bugs that did not exist on the 418-window manifest.
    """
    unkeyed = {
        "symbol": "ABCD",
        "date": "2026-06-25",
        "entry_ts_utc_pinned": "2026-06-25T13:31:07Z",
        "pin_method": "price_match",
        "pin_confidence": "tape_confirmed",
    }
    case = _one(
        _manifest(_row(manifest_id="VID::ABCD::2026-06-25::unpinned")),
        _pins(unkeyed, dict(unkeyed)),
    )

    assert case.window is None
    assert "pin_symbol_day_ambiguous" in case.unscorable_reasons
    assert "pin_duplicate_rows" not in case.unscorable_reasons
    # and it is still reported as exactly one reason, not doubled with the
    # "no pin at all" reason
    assert "pin_missing" not in case.unscorable_reasons


def test_point_only_window_text_has_no_end_boundary():
    case = _one(_manifest(_row(window_et="~09:25")), _pins(_pin()))

    assert case.window is None
    assert "window_end_missing" in case.unscorable_reasons


def test_parenthetical_uncertainty_range_is_never_read_as_an_end():
    """``"~07:45 (07:40-07:55)"`` states a point, plus how unsure the point is.

    Reading 07:55 as the window end would widen the grading window using an
    uncertainty bound -- the exact 'stretch it until the price is good' move the
    bench exists to avoid.  The row must land in the unscorable bucket instead.
    """
    case = _one(_manifest(_row(window_et="~07:45 (07:40-07:55)")), _pins(_pin()))

    assert case.window is None
    assert "window_end_missing" in case.unscorable_reasons


def test_leading_range_with_trailing_prose_still_yields_the_stated_end():
    case = _one(
        _manifest(_row(window_et="~09:15-09:30 (coming into the open)")),
        _pins(_pin(entry_ts_utc_pinned="2026-06-25T13:20:00Z")),
    )

    assert case.scorable is True
    assert case.window is not None
    assert case.window.end_ts == datetime(2026, 6, 25, 13, 30, tzinfo=timezone.utc)


def test_stated_exit_et_field_wins_over_the_window_text():
    case = _one(
        _manifest(_row(stated_exit_et="09:45")),
        _pins(_pin()),
    )

    assert case.window is not None
    assert case.window.end_ts == datetime(2026, 6, 25, 13, 45, tzinfo=timezone.utc)


def test_narrative_stated_exit_et_is_ignored_and_falls_back_to_the_window_text():
    case = _one(
        _manifest(_row(stated_exit_et="not stated (last trade of the day)")),
        _pins(_pin()),
    )

    # The window text still supplies 10:00 ET; the narrative field supplies
    # nothing, and no clock is scraped out of the middle of the sentence.
    assert case.window is not None
    assert case.window.end_ts == STATED_END_UTC


def test_end_before_start_is_refused_rather_than_rolled_forward():
    case = _one(_manifest(_row(window_et="~08:00-08:30")), _pins(_pin()))

    assert case.window is None
    assert "window_end_before_start" in case.unscorable_reasons


def test_pin_naming_a_different_symbol_is_refused():
    case = _one(_manifest(_row()), _pins(_pin(symbol="WXYZ")))

    assert case.window is None
    assert "pin_symbol_mismatch" in case.unscorable_reasons


def test_pin_on_a_different_trading_day_is_refused():
    case = _one(_manifest(_row()), _pins(_pin(entry_ts_utc_pinned="2026-06-26T13:31:07Z")))

    assert case.window is None
    assert "pin_date_mismatch" in case.unscorable_reasons


def test_naive_timestamp_is_rejected_when_the_key_does_not_assert_utc():
    case = _one(
        _manifest(_row()),
        _pins(
            {
                "manifest_id": LABEL,
                "entry_ts": "2026-06-25T13:31:07",
                "pin_method": "price_match",
                "pin_confidence": "tape_confirmed",
            }
        ),
    )

    assert case.window is None
    assert "pin_timestamp_naive" in case.unscorable_reasons


def test_naive_timestamp_is_accepted_when_the_key_asserts_utc():
    case = _one(_manifest(_row()), _pins(_pin(entry_ts_utc_pinned="2026-06-25T13:31:07")))

    assert case.scorable is True
    assert case.entry_pin_ts == ENTRY_UTC


@pytest.mark.parametrize("value", ["not a timestamp", 1782394267, 13.5])
def test_unparseable_timestamps_are_refused(value):
    case = _one(_manifest(_row()), _pins(_pin(entry_ts_utc_pinned=value)))

    assert case.window is None
    assert "pin_timestamp_unparseable" in case.unscorable_reasons


def test_undefined_expected_action_is_unscorable():
    """The master ledger's genuine unknowns must not be graded.

    ``ExpectedAction`` is exactly {"trade","reject"} (ross_replay_benchmark.py:17);
    a ledger row whose outcome is a NULL sentinel maps to None upstream and has
    no defined benchmark target here.
    """
    case = _one(_manifest(_row(expected_action=None)), _pins(_pin()))

    assert case.window is None
    assert case.expected_action is None
    assert "expected_action_undefined" in case.unscorable_reasons


# --- joins ------------------------------------------------------------------


def test_symbol_day_fallback_join_is_used_only_when_both_sides_are_unique():
    pin_without_id = {
        "symbol": "ABCD",
        "date": "2026-06-25",
        "entry_ts_utc_pinned": "2026-06-25T13:31:07Z",
        "pin_method": "price_match",
        "pin_confidence": "tape_confirmed",
    }

    unique = _one(_manifest(_row()), _pins(pin_without_id))
    assert unique.scorable is True
    assert "pin_joined_by_symbol_date_fallback" in unique.diagnostics

    # Two manifest windows on the same symbol-day (the VEEE-style multi-attempt
    # shape) must not both collapse onto the single pin.
    ambiguous = phase_windows_from_manifest(
        _manifest(_row(), _row(manifest_id="VID::ABCD::2026-06-25::t2")),
        _pins(pin_without_id),
    )
    assert [case.scorable for case in ambiguous] == [False, False]
    assert all("pin_missing" in case.unscorable_reasons for case in ambiguous)


def test_pins_container_shapes_are_interchangeable():
    expected = _one(_manifest(_row()), _pins(_pin())).window

    as_list = _one(_manifest(_row()), [_pin()]).window
    as_map = _one(_manifest(_row()), {LABEL: _pin()}).window

    assert as_list == expected
    assert as_map == expected


def test_missing_pins_document_yields_all_unscorable_not_a_crash():
    cases = phase_windows_from_manifest(_manifest(_row(), _row(manifest_id="x")), None)

    assert [case.scorable for case in cases] == [False, False]


def test_manifest_without_a_windows_list_returns_no_cases(caplog):
    with caplog.at_level(logging.ERROR):
        cases = phase_windows_from_manifest({"schema": MANIFEST_SCHEMA}, _pins(_pin()))

    assert cases == []
    assert "windows" in caplog.text


def test_schema_mismatch_is_logged_not_swallowed(caplog):
    with caplog.at_level(logging.WARNING):
        phase_windows_from_manifest(
            {"schema": "chili.something_else.v9", "windows": [_row()]},
            {"schema": "chili.other_pins.v9", "pins": [_pin()]},
        )

    assert "chili.something_else.v9" in caplog.text
    assert "chili.other_pins.v9" in caplog.text


# --- passthrough and reporting ---------------------------------------------


def test_zero_pnl_from_the_master_ledger_is_flagged_but_not_rewritten():
    """0 is a NULL sentinel in the ledger; scoring it as a flat trade is wrong.

    Mapping the sentinel to None belongs to the manifest layer.  The adapter
    passes the value through verbatim and raises a diagnostic so a regression
    upstream is visible in the report rather than silently scored.
    """
    case = _one(
        _manifest(_row(ross_net_usd=0, source={"kind": "master_ledger", "refs": []})),
        _pins(_pin()),
    )

    assert case.ross_net_usd == 0.0
    assert "pnl_zero_sentinel_suspect" in case.diagnostics
    assert case.scorable is True  # the diagnostic does not change the grade


def test_non_numeric_pnl_becomes_none_rather_than_zero():
    case = _one(_manifest(_row(ross_net_usd="about two grand")), _pins(_pin()))

    assert case.ross_net_usd is None


def test_cases_preserve_manifest_order_and_report_every_row():
    manifest = _manifest(
        _row(),
        _row(manifest_id="VID::WXYZ::2026-06-25::t1", symbol="WXYZ"),
        _row(manifest_id="VID::EFGH::2026-06-25::watchlist", symbol="EFGH", expected_action="reject"),
    )

    cases = phase_windows_from_manifest(manifest, _pins(_pin()))

    assert [case.label_id for case in cases] == [
        LABEL,
        "VID::WXYZ::2026-06-25::t1",
        "VID::EFGH::2026-06-25::watchlist",
    ]


def test_summary_partitions_every_case_exactly_once():
    manifest = _manifest(
        _row(),
        _row(manifest_id="VID::WXYZ::2026-06-25::t1", symbol="WXYZ"),
        _row(manifest_id="VID::EFGH::2026-06-25::watchlist", symbol="EFGH", expected_action="reject"),
    )
    cases = phase_windows_from_manifest(manifest, _pins(_pin()))

    summary = adaptation_summary(cases)

    assert summary["schema"] == ADAPTATION_SUMMARY_SCHEMA
    assert summary["case_count"] == 3
    assert summary["scorable_count"] + summary["unscorable_count"] == 3
    assert summary["scorable_count"] == 1
    action_total = sum(
        bucket["scorable"] + bucket["unscorable"]
        for bucket in summary["by_expected_action"].values()
    )
    assert action_total == 3


def test_only_scorable_cases_are_handed_to_the_grader():
    manifest = _manifest(_row(), _row(manifest_id="VID::WXYZ::2026-06-25::t1", symbol="WXYZ"))
    cases = phase_windows_from_manifest(manifest, _pins(_pin()))

    windows = validated_phase_windows(cases)

    assert [window.label_id for window in windows] == [LABEL]
    assert all(window.independently_verified for window in windows)
    assert expected_actions_by_label(cases) == {LABEL: "trade"}


def test_case_json_row_is_serialisable():
    case = _one(_manifest(_row()), _pins(_pin()))

    payload = json.loads(json.dumps(case_as_json_row(case)))

    assert payload["evidence_source"] == "tape_pin:price_match"
    assert payload["start_ts"] == ENTRY_UTC.isoformat()
    assert payload["scorable"] is True


def test_cli_dry_run_writes_only_where_asked(tmp_path, capsys):
    manifest_path = tmp_path / "manifest.json"
    pins_path = tmp_path / "pins.json"
    out_path = tmp_path / "adapted.json"
    manifest_path.write_text(json.dumps(_manifest(_row())), encoding="utf-8")
    pins_path.write_text(json.dumps(_pins(_pin())), encoding="utf-8")

    exit_code = main(
        [
            "--manifest",
            str(manifest_path),
            "--pins",
            str(pins_path),
            "--json-out",
            str(out_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["summary"]["scorable_count"] == 1
    assert payload["cases"][0]["label_id"] == LABEL
    assert "scorable_count" in capsys.readouterr().out


# --- the producer/consumer contract -----------------------------------------
#
# Everything above hand-writes a pin row in the shape this module expects.  That
# is exactly how the two sides diverged: the pinner emitted one row per LEG
# keyed on ``pin_id`` with the instant at ``pin_second_utc``, this module joined
# on ``manifest_id`` and read ``entry_ts_utc_pinned``, and the whole bench
# scored 0 of 418 cases with ``pin_confidence`` null on every one -- silently,
# because a missing join is indistinguishable from missing evidence.
#
# The two tests below therefore drive the REAL producer.  They build a ledger,
# run ``iter_ross_windows`` + ``pin_event`` + ``pin_window`` against a synthetic
# in-memory tape (no database), and feed the result straight into
# ``phase_windows_from_manifest``.  If either side renames a key, these fail.


def _producer_chain(*, exit_price_on_tape: bool):
    """Ledger -> pins document, through the pinner's own functions.

    Synthetic tape only: a live trading lane and a shared _test database run on
    this machine, so nothing here opens a connection.
    """
    import sys as _sys

    _sys.path.insert(0, str(REPO_ROOT))
    from scripts import rossbench_pin_ross_events as producer

    # 2026-06-25 is EDT (UTC-4): 09:31 ET == 13:31 UTC, 10:05 ET == 14:05 UTC.
    ledger = {
        "schema": "chili.ross_master_ledger.v1",
        "trades": [
            {
                "_path": "trades",
                "video_id": "VID",
                "date": "2026-06-25",
                "symbol": "ABCD",
                "account": "big",
                "side": "long",
                "entry_time_et": "09:31:07",
                "exit_time_et": "10:05:00",
                "entry_px": 1.95,
                "exit_px": 2.60,
                "pnl_usd": 1234.5,
                "confidence": "exact",
            }
        ],
    }
    manifest_doc = {
        "schema": MANIFEST_SCHEMA,
        "windows": [{"manifest_id": "VID::ABCD::2026-06-25::ml1", "notes": None}],
    }
    windows, _non_trades = producer.iter_ross_windows(ledger, manifest=manifest_doc)
    assert len(windows) == 1

    def tape(ts, price):
        return [{"observed_at": ts, "price": price, "size": 100.0,
                 "bid": None, "ask": None, "source": "iqfeed_lookup_hist", "id": 1}]

    leg_pins = {}
    for event in windows[0].legs:
        search = producer.build_search_window(event.day, event.stated, 600.0)
        stated_utc = producer.et_to_utc(event.day, event.stated.point_s)
        if event.leg == "entry" or exit_price_on_tape:
            rows = tape(stated_utc, event.ross_px)
        else:
            rows = []
        leg_pins[event.leg] = producer.pin_event(event, rows, search)
    return producer.build_pins_doc(
        [producer.pin_window(windows[0], leg_pins)], [], provenance={}
    )


def test_the_pinners_own_output_scores_through_this_adapter():
    """End to end, producer to consumer, with no hand-written pin row."""
    pins = _producer_chain(exit_price_on_tape=True)
    manifest = _manifest(
        _row(
            manifest_id="VID::ABCD::2026-06-25::ml1",
            window_et="~09:31-10:05",
            stated_exit_et="10:05",
        )
    )

    case = _one(manifest, pins)

    assert case.unscorable_reasons == ()
    assert case.scorable is True
    assert case.pin_confidence == "tape_confirmed"
    assert case.pin_method == "price_match"
    assert case.window is not None
    assert case.window.start_ts == datetime(2026, 6, 25, 13, 31, 7, tzinfo=timezone.utc)
    # the exit leg pinned on the tape, so it -- not the narrated clock -- is the end
    assert case.end_ts_basis == END_BASIS_EXIT_PIN
    assert case.window.end_ts == datetime(2026, 6, 25, 14, 5, tzinfo=timezone.utc)
    assert case.window.valid_for(label_id=case.label_id, symbol="ABCD") is True


def test_an_unpinned_exit_falls_back_to_the_manifests_stated_end():
    """The common real shape: the entry price prints, the exit price does not.
    The window still closes -- on the NARRATED end, recorded as such."""
    pins = _producer_chain(exit_price_on_tape=False)
    manifest = _manifest(
        _row(manifest_id="VID::ABCD::2026-06-25::ml1", stated_exit_et="10:05")
    )

    case = _one(manifest, pins)

    assert case.scorable is True
    assert case.exit_pin_ts is None
    assert case.end_ts_basis == END_BASIS_STATED
    assert case.window.end_ts == datetime(2026, 6, 25, 14, 5, tzinfo=timezone.utc)


def test_every_key_this_adapter_reads_is_on_the_producers_contract_list():
    """The pinner asserts its own row shape on every emitted row.  This binds
    the two lists together so a key can only be dropped from both at once.
    """
    import sys as _sys

    _sys.path.insert(0, str(REPO_ROOT))
    from scripts import rossbench_pin_ross_events as producer

    required = set(producer.PIN_ROW_REQUIRED_KEYS)
    # what _pin_id joins on, and what _event_pin reads for each side
    assert "manifest_id" in required
    for side in ("entry", "exit"):
        assert {f"{side}_ts_utc_pinned", f"{side}_pin_method", f"{side}_pin_confidence"} <= required
    # what the (symbol, date) fallback indexes on
    assert {"symbol", "date"} <= required
    # and the pinner's vocabularies are the ones this module gates on
    assert set(producer.PIN_CONFIDENCES) == set(PIN_CONFIDENCES)
    assert tuple(producer.PIN_METHODS) == tuple(PIN_METHODS)


# --- the one-directional import rule ---------------------------------------


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add("." * node.level + (node.module or ""))
            elif node.module:
                names.add(node.module.split(".")[0])
    return names


def test_adapter_imports_only_stdlib_and_the_pure_grading_module():
    """No DB, no network, no strategy imports -- the adapter stays inert."""
    allowed = {
        "__future__",
        "argparse",
        "json",
        "logging",
        "re",
        "sys",
        "dataclasses",
        "datetime",
        "typing",
        "zoneinfo",
        ".ross_replay_benchmark",
    }

    assert _imported_modules(ADAPTER_PATH) <= allowed


def test_strategy_code_does_not_import_the_adapter():
    """Grading tooling must never become an event-time input.

    The allowlist is deliberately tiny: the adapter itself, and the pure bench
    scorer that consumes AdaptedCase.  Anything else under ``app/`` importing
    this module means a hindsight label has a path into live code.
    """
    allowed = {ADAPTER_PATH, ADAPTER_PATH.with_name("ross_bench_scoring.py")}
    offenders = []

    for path in (REPO_ROOT / "app").rglob("*.py"):
        if path in allowed:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "ross_manifest_adapter" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and "ross_manifest_adapter" in (
                node.module or ""
            ):
                offenders.append(f"{path}:{node.lineno}")
            elif isinstance(node, ast.Import) and any(
                "ross_manifest_adapter" in alias.name for alias in node.names
            ):
                offenders.append(f"{path}:{node.lineno}")

    assert offenders == [], (
        "after-fact grading tooling imported by app code: " + ", ".join(offenders)
    )
