from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.momentum_neural.setup_trace_audit import (
    audit_recent_setup_trace_events,
    audit_setup_trace_events,
    summarize_setup_trace_certification,
)


def test_setup_trace_audit_accepts_clean_canonical_trace() -> None:
    report = audit_setup_trace_events(
        [
            {
                "session_id": 10,
                "event_type": "live_entry_candidate",
                "payload_json": {
                    "setup_trace": {
                        "setup_alias": "abcd_break_tick_ok",
                        "structural_stop_covered": True,
                        "a_setup_floor_covered": True,
                        "source_wait_reason": "waiting_for_break",
                        "source_wait_tick_armed": True,
                        "source_wait_tape_hold_eligible": True,
                        "source_wait_has_pullback_levels": True,
                        "pullback_high": 4.25,
                        "pullback_low": 3.92,
                    }
                },
            }
        ]
    )

    assert report.ok is True
    assert report.events_seen == 1
    assert report.traces_seen == 1
    assert report.lifecycle_summary["stage_counts"]["setup_trace"] == 1
    assert report.lifecycle_summary["trace_alias_counts"]["abcd_break_tick_ok"] == 1
    assert report.lifecycle_summary["wait_reason_counts"]["waiting_for_break"] == 1
    assert report.lifecycle_summary["event_type_counts"]["live_entry_candidate"] == 1


def test_setup_trace_audit_flags_alias_floor_gap() -> None:
    report = audit_setup_trace_events(
        [
            SimpleNamespace(
                session_id=11,
                event_type="live_entry_candidate",
                payload_json={
                    "setup_trace": {
                        "setup_alias": "new_structural_alias",
                        "structural_stop_covered": True,
                        "a_setup_floor_covered": False,
                        "pullback_low": 2.10,
                    }
                },
            )
        ]
    )

    assert report.ok is False
    assert report.finding_reasons == ["setup_alias_missing_a_setup_floor"]
    assert report.findings[0].session_id == 11


def test_setup_trace_audit_flags_missing_alias_coverage_booleans() -> None:
    report = audit_setup_trace_events(
        [
            {
                "session_id": 111,
                "event_type": "live_entry_candidate",
                "payload_json": {
                    "setup_trace": {
                        "setup_alias": "abcd_break_tick_ok",
                        "source_wait_reason": "",
                        "pullback_low": 2.10,
                    }
                },
            }
        ]
    )

    assert report.ok is False
    assert report.finding_reasons == [
        "setup_alias_missing_structural_stop",
        "setup_alias_missing_a_setup_floor",
    ]
    assert report.findings[0].setup_alias == "abcd_break_tick_ok"


def test_setup_trace_audit_infers_structural_coverage_from_setup_coverage() -> None:
    report = audit_setup_trace_events(
        [
            {
                "session_id": 112,
                "event_type": "live_entry_pre_candidate_ross_shape_block",
                "payload_json": {
                    "trigger_reason": "abcd_break_tick_ok",
                    "setup_coverage": "structural_a_setup",
                    "reason": "ross_live_requires_tick_tape_revalidation",
                },
            }
        ]
    )

    assert report.ok is True
    assert report.traces_seen == 1


def test_setup_trace_audit_still_requires_stop_level_on_candidate() -> None:
    report = audit_setup_trace_events(
        [
            {
                "session_id": 113,
                "event_type": "live_entry_candidate",
                "payload_json": {
                    "trigger_reason": "abcd_break_tick_ok",
                    "setup_coverage": "structural_a_setup",
                },
            }
        ]
    )

    assert report.ok is False
    assert report.finding_reasons == ["structural_setup_missing_stop_level"]


def test_setup_trace_audit_accepts_non_structural_volume_fallback() -> None:
    report = audit_setup_trace_events(
        [
            {
                "session_id": 10374,
                "event_type": "live_entry_pending_place",
                "payload_json": {
                    "setup_trace": {
                        "setup_alias": "momentum_ok_rel_vol",
                        "trigger_reason": "momentum_ok_rel_vol",
                        "setup_coverage": "non_structural_volume_fallback",
                        "structural_stop_covered": False,
                        "a_setup_floor_covered": False,
                    }
                },
            }
        ]
    )

    assert report.ok is True
    assert report.traces_seen == 1


def test_setup_trace_audit_infers_historical_non_structural_volume_alias() -> None:
    report = audit_setup_trace_events(
        [
            {
                "session_id": 10374,
                "event_type": "live_entry_pending_place",
                "payload_json": {
                    "setup_trace": {
                        "setup_alias": "momentum_ok_rel_vol",
                        "trigger_reason": "momentum_ok_rel_vol",
                        "structural_stop_covered": False,
                        "a_setup_floor_covered": False,
                    }
                },
            }
        ]
    )

    assert report.ok is True
    assert report.traces_seen == 1


def test_setup_trace_audit_flags_wait_without_structural_levels() -> None:
    report = audit_setup_trace_events(
        [
            {
                "session_id": 12,
                "event_type": "live_entry_wait",
                "payload_json": {
                    "setup_trace": {
                        "setup_alias": "vwap_reclaim",
                        "structural_stop_covered": True,
                        "a_setup_floor_covered": True,
                        "source_wait_reason": "waiting_for_vwap_reclaim",
                        "source_wait_tick_armed": True,
                        "source_wait_tape_hold_eligible": True,
                        "source_wait_has_pullback_levels": False,
                        "pullback_high": 3.91,
                    }
                },
            }
        ]
    )

    assert report.ok is False
    assert report.finding_reasons == [
        "structural_setup_missing_stop_level",
        "wait_reason_missing_pullback_levels",
    ]
    assert report.findings[1].detail["pullback_low_present"] is False


def test_setup_trace_audit_flags_wait_event_missing_setup_trace() -> None:
    report = audit_setup_trace_events(
        [
            {
                "session_id": 122,
                "event_type": "live_entry_tick_scalp_wait",
                "payload_json": {
                    "reason": "waiting_for_first_pullback_break",
                    "pullback_low": 3.70,
                    "high": 3.91,
                },
            }
        ]
    )

    assert report.ok is False
    assert report.finding_reasons == ["wait_event_missing_setup_trace"]
    assert report.traces_seen == 0
    assert report.lifecycle_summary["event_type_counts"]["live_entry_tick_scalp_wait"] == 1


def test_setup_trace_audit_allows_pre_structure_tick_wait_without_stop_level() -> None:
    report = audit_setup_trace_events(
        [
            {
                "session_id": 123,
                "event_type": "live_entry_tick_scalp_wait",
                "payload_json": {
                    "reason": "ross_pillars_not_explosive",
                    "setup_trace": {
                        "setup_alias": "tick_first_pullback_scalp",
                        "setup_coverage": "structural_a_setup",
                        "structural_stop_covered": True,
                        "a_setup_floor_covered": True,
                        "source_wait_reason": "ross_pillars_not_explosive",
                        "source_wait_tick_armed": False,
                        "source_wait_tape_hold_eligible": False,
                        "source_wait_has_pullback_levels": False,
                    },
                },
            }
        ]
    )

    assert report.ok is True
    assert report.finding_reasons == []


def test_setup_trace_audit_flags_missing_wait_tick_and_tape_proof() -> None:
    report = audit_setup_trace_events(
        [
            {
                "session_id": 121,
                "ts": "2026-07-02T15:30:00Z",
                "event_type": "live_entry_wait",
                "payload_json": {
                    "setup_trace": {
                        "setup_alias": "vwap_reclaim",
                        "structural_stop_covered": True,
                        "a_setup_floor_covered": True,
                        "source_wait_reason": "waiting_for_vwap_reclaim",
                        "source_wait_has_pullback_levels": True,
                        "pullback_high": 3.91,
                        "pullback_low": 3.70,
                    }
                },
            }
        ]
    )

    assert report.ok is False
    assert report.finding_reasons == [
        "wait_reason_not_tick_armed",
        "wait_reason_missing_tape_hold_eligibility",
    ]
    assert report.findings[0].ts == "2026-07-02T15:30:00Z"


def test_setup_trace_audit_normalizes_older_top_level_payload() -> None:
    report = audit_setup_trace_events(
        [
            {
                "session_id": 13,
                "event_type": "live_entry_candidate",
                "payload_json": {
                    "setup_reason": "pullback_break_tick_ok",
                    "structural_stop_covered": True,
                    "a_setup_floor_covered": True,
                    "source_wait_reason": "waiting_for_break",
                    "source_wait_tick_armed": False,
                    "source_wait_tape_hold_eligible": True,
                    "pullback_high": 5.10,
                    "pullback_low": 4.90,
                },
            }
        ]
    )

    assert report.ok is False
    assert report.finding_reasons == ["wait_reason_not_tick_armed"]
    assert report.findings[0].setup_alias == "pullback_break_tick_ok"


def test_setup_trace_audit_summarizes_entry_add_exit_lifecycle() -> None:
    report = audit_setup_trace_events(
        [
            {
                "session_id": 31,
                "event_type": "live_entry_filled",
                "payload_json": {"price": 4.0},
            },
            {
                "session_id": 31,
                "event_type": "live_trailing_armed",
                "payload_json": {"bid": 4.18},
            },
            {
                "session_id": 31,
                "event_type": "live_pullback_add_fill",
                "payload_json": {"add_qty": 10, "add_price": 4.22},
            },
            {
                "session_id": 31,
                "event_type": "live_exit_filled",
                "payload_json": {"fill_price": 4.4, "pnl_usd": 20.0},
            },
        ]
    )

    assert report.ok is True
    assert report.lifecycle_summary["stage_counts"] == {
        "add_fill": 1,
        "entry_fill": 1,
        "exit_fill": 1,
        "trailing_armed": 1,
    }
    assert report.lifecycle_summary["sessions_with_entry_fill"] == 1
    assert report.lifecycle_summary["sessions_with_trailing_armed"] == 1
    assert report.lifecycle_summary["sessions_with_add_fill"] == 1
    assert report.lifecycle_summary["sessions_with_exit_fill"] == 1
    assert report.lifecycle_summary["sessions_with_entry_and_trailing"] == 1
    assert report.lifecycle_summary["sessions_with_entry_and_add"] == 1
    assert report.lifecycle_summary["sessions_with_entry_and_exit"] == 1


def test_setup_trace_audit_certifies_ordered_anticipation_remainder_lifecycle() -> None:
    report = audit_setup_trace_events(
        [
            {"session_id": 41, "event_type": "live_entry_filled", "payload_json": {"price": 10.0}},
            {
                "session_id": 41,
                "event_type": "live_anticipation_remainder_submitted",
                "payload_json": {"remainder_qty": 134},
            },
            {
                "session_id": 41,
                "event_type": "live_anticipation_remainder_filled",
                "payload_json": {"fill_qty": 134, "fill_price": 10.2},
            },
            {
                "session_id": 41,
                "event_type": "live_partial_exit_filled",
                "payload_json": {"qty": 89, "fill_price": 10.5},
            },
            {"session_id": 41, "event_type": "live_trailing_armed", "payload_json": {"bid": 10.55}},
            {"session_id": 41, "event_type": "live_exit_filled", "payload_json": {"fill_price": 10.8}},
        ]
    )

    assert report.ok is True
    summary = report.lifecycle_summary
    assert summary["stage_counts"] == {
        "add_fill": 1,
        "add_submit": 1,
        "entry_fill": 1,
        "exit_fill": 1,
        "partial_exit": 1,
        "trailing_armed": 1,
    }
    assert summary["sessions_with_ordered_entry_add_exit"] == 1
    assert summary["sessions_with_complete_anticipation_remainder_lifecycle"] == 1
    assert summary["sessions_with_complete_runner_exit_lifecycle"] == 1
    assert summary["issue_counts"] == {}
    row = summary["sessions"][0]
    assert row["add_families"] == ["anticipation_remainder"]
    assert row["complete_anticipation_remainder_lifecycle"] is True
    assert row["issues"] == []


def test_setup_trace_audit_certifies_runner_add_only_after_trailing_arm() -> None:
    report = audit_setup_trace_events(
        [
            {"session_id": 42, "event_type": "live_entry_filled", "payload_json": {"price": 4.0}},
            {"session_id": 42, "event_type": "live_trailing_armed", "payload_json": {"bid": 4.12}},
            {
                "session_id": 42,
                "event_type": "live_pullback_add_fired",
                "payload_json": {"add_qty": 20, "limit_price": 4.14},
            },
            {
                "session_id": 42,
                "event_type": "live_pullback_add_fill",
                "payload_json": {"add_qty": 20, "add_price": 4.14},
            },
            {"session_id": 42, "event_type": "live_exit_filled", "payload_json": {"fill_price": 4.35}},
        ]
    )

    assert report.ok is True
    summary = report.lifecycle_summary
    assert summary["sessions_with_complete_runner_add_lifecycle"] == 1
    assert summary["issue_counts"] == {}
    row = summary["sessions"][0]
    assert row["add_families"] == ["pullback_add"]
    assert row["stages"] == ["entry_fill", "trailing_armed", "add_submit", "add_fill", "exit_fill"]
    assert row["complete_runner_add_lifecycle"] is True
    cert = summarize_setup_trace_certification(report)
    assert cert["trace_coverage_ok"] is False
    assert cert["trace_coverage_blocker"] == "no_setup_trace_events"
    assert cert["lifecycle_order_ok"] is True
    assert cert["lifecycle_claim_ready"] is False
    assert cert["complete_lifecycle_counts"]["runner_add"] == 1


def test_setup_trace_certification_requires_trace_and_complete_lifecycle() -> None:
    report = audit_setup_trace_events(
        [
            {
                "session_id": 421,
                "event_type": "live_entry_candidate",
                "payload_json": {
                    "setup_trace": {
                        "setup_alias": "abcd_break_tick_ok",
                        "structural_stop_covered": True,
                        "a_setup_floor_covered": True,
                        "source_wait_reason": "",
                        "pullback_low": 3.90,
                    }
                },
            },
            {"session_id": 421, "event_type": "live_entry_filled", "payload_json": {"price": 4.0}},
            {"session_id": 421, "event_type": "live_trailing_armed", "payload_json": {"bid": 4.12}},
            {"session_id": 421, "event_type": "live_pullback_add_fired", "payload_json": {"add_qty": 20}},
            {"session_id": 421, "event_type": "live_pullback_add_fill", "payload_json": {"add_qty": 20}},
            {"session_id": 421, "event_type": "live_exit_filled", "payload_json": {"fill_price": 4.35}},
        ]
    )

    cert = summarize_setup_trace_certification(report)

    assert report.ok is True
    assert report.traces_seen == 1
    assert cert["trace_coverage_ok"] is True
    assert cert["lifecycle_order_ok"] is True
    assert cert["lifecycle_claim_ready"] is True
    assert cert["complete_lifecycle_counts"]["runner_add"] == 1


def test_setup_trace_audit_reports_runner_add_before_trailing_without_failing_trace_ok() -> None:
    report = audit_setup_trace_events(
        [
            {"session_id": 43, "event_type": "live_entry_filled", "payload_json": {"price": 4.0}},
            {
                "session_id": 43,
                "event_type": "live_pullback_add_fired",
                "payload_json": {"add_qty": 20, "limit_price": 4.14},
            },
            {
                "session_id": 43,
                "event_type": "live_pullback_add_fill",
                "payload_json": {"add_qty": 20, "add_price": 4.14},
            },
            {"session_id": 43, "event_type": "live_exit_filled", "payload_json": {"fill_price": 4.35}},
        ]
    )

    assert report.ok is True
    assert report.lifecycle_summary["sessions_with_complete_runner_add_lifecycle"] == 0
    assert report.lifecycle_summary["issue_counts"] == {"runner_add_without_trailing_arm": 1}
    assert report.lifecycle_summary["sessions"][0]["issues"] == ["runner_add_without_trailing_arm"]
    cert = summarize_setup_trace_certification(report)
    assert cert["trace_coverage_ok"] is False
    assert cert["trace_coverage_blocker"] == "no_setup_trace_events"
    assert cert["lifecycle_order_ok"] is False
    assert cert["lifecycle_claim_ready"] is False
    assert cert["lifecycle_issue_counts"] == {"runner_add_without_trailing_arm": 1}


def test_setup_trace_certification_marks_possible_truncated_lifecycle_window() -> None:
    report = audit_setup_trace_events(
        [
            {"session_id": 44, "event_type": "live_pullback_add_fill", "payload_json": {"add_qty": 20}},
            {"session_id": 44, "event_type": "live_exit_filled", "payload_json": {"fill_price": 4.35}},
        ]
    )

    cert = summarize_setup_trace_certification(report)

    assert report.ok is True
    assert cert["lifecycle_order_ok"] is False
    assert cert["window_completeness_ok"] is False
    assert cert["lifecycle_claim_ready"] is False
    assert cert["possible_truncated_window_issue_counts"] == {
        "add_fill_without_prior_submit": 1,
        "exit_without_entry_fill": 1,
    }


class _FakeQuery:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.limit_value = None
        self.filter_calls = 0

    def filter(self, *_args, **_kwargs):
        self.filter_calls += 1
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def all(self):
        return self.rows[: self.limit_value]


class _FakeDb:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.query_obj = _FakeQuery(rows)

    def query(self, *_args, **_kwargs):
        return self.query_obj


def test_recent_setup_trace_audit_db_wrapper_is_read_only_shape_compatible() -> None:
    db = _FakeDb(
        [
            SimpleNamespace(
                session_id=21,
                event_type="live_entry_candidate",
                payload_json={
                    "setup_trace": {
                        "setup_alias": "pullback_break_tick_ok",
                        "structural_stop_covered": True,
                        "a_setup_floor_covered": True,
                        "source_wait_reason": "waiting_for_break",
                        "source_wait_tick_armed": True,
                        "source_wait_tape_hold_eligible": True,
                        "source_wait_has_pullback_levels": True,
                        "pullback_high": 5.2,
                        "pullback_low": 4.95,
                    }
                },
            )
        ]
    )

    report = audit_recent_setup_trace_events(
        db,
        session_id=21,
        event_types=("live_entry_candidate",),
        limit=5000,
    )

    assert report.ok is True
    assert report.events_seen == 1
    assert db.query_obj.filter_calls == 2
    assert db.query_obj.limit_value == 1000


def test_recent_setup_trace_audit_reorders_recent_desc_rows_for_lifecycle() -> None:
    db = _FakeDb(
        [
            SimpleNamespace(session_id=51, event_type="live_exit_filled", payload_json={"fill_price": 4.35}),
            SimpleNamespace(session_id=51, event_type="live_pullback_add_fill", payload_json={"add_qty": 20}),
            SimpleNamespace(session_id=51, event_type="live_pullback_add_fired", payload_json={"add_qty": 20}),
            SimpleNamespace(session_id=51, event_type="live_trailing_armed", payload_json={"bid": 4.12}),
            SimpleNamespace(session_id=51, event_type="live_entry_filled", payload_json={"price": 4.0}),
        ]
    )

    report = audit_recent_setup_trace_events(db, session_id=51, limit=10)
    cert = summarize_setup_trace_certification(report)

    assert report.lifecycle_summary["issue_counts"] == {}
    assert cert["trace_coverage_ok"] is False
    assert cert["trace_coverage_blocker"] == "no_setup_trace_events"
    assert cert["lifecycle_order_ok"] is True
    assert cert["lifecycle_claim_ready"] is False
    assert report.lifecycle_summary["sessions"][0]["stages"] == [
        "entry_fill",
        "trailing_armed",
        "add_submit",
        "add_fill",
        "exit_fill",
    ]
