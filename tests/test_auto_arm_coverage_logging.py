"""Drive the scheduler callback: quiet passes must expose selection coverage."""
import copy
import json
import logging
from unittest.mock import MagicMock

import pytest

from app import db as app_db
from app.config import settings
from app.services import trading_scheduler as scheduler
from app.services.trading.momentum_neural import auto_arm, ignition_loop, lane_health


@pytest.fixture
def run_pass(monkeypatch, caplog):
    for name in ("chili_momentum_auto_arm_live_enabled",
                 "chili_momentum_auto_arm_live_scheduler_enabled",
                 "chili_momentum_live_runner_enabled"):
        monkeypatch.setattr(settings, name, True)
    database = MagicMock()
    monkeypatch.setattr(app_db, "SessionLocal", lambda: database)
    monkeypatch.setattr(scheduler, "run_scheduler_job_guarded", lambda _, work: work())
    monkeypatch.setattr(scheduler, "_live_auto_arm_owner_health", lambda _: (True, None))
    monkeypatch.setattr(scheduler, "_auto_arm_last_coverage_sig", None)
    monkeypatch.setattr(scheduler, "_auto_arm_last_skip_sig", None)
    monkeypatch.setattr(lane_health, "record_auto_arm_run", lambda: None)
    wake = MagicMock(return_value=0)
    monkeypatch.setattr(ignition_loop, "wake_armed_sessions", wake)
    caplog.set_level(logging.INFO, logger=scheduler.logger.name)

    def run(summary):
        caplog.clear()
        original = copy.deepcopy(summary)
        monkeypatch.setattr(auto_arm, "run_auto_arm_pass", lambda _: summary)
        scheduler._run_momentum_auto_arm_live_job()
        wake.assert_called_with(summary.get("armed_session_ids"))
        assert summary == original  # logging cannot mutate execution output
        assert not any("pass failed" in record.getMessage() for record in caplog.records)
        return [json.loads(record.getMessage().split("coverage=", 1)[1])
                for record in caplog.records if "auto_arm coverage=" in record.getMessage()]
    return run


def summary(returned="AAA", unobserved="ZZZ"):
    return {"armed": False, "skipped": "no_active_trigger", "scanned": 2,
            "candidate_intake": {"source": "legacy_live_eligible_viability_rows",
                                 "full_broker_universe_observed": False},
            "probe_coverage": {"eligible_symbols": ["AAA", "ZZZ"],
                               "returned_symbols": [returned],
                               "unobserved_symbols": [unobserved],
                               "scope": "process_lifetime; not a durable tick-context observation"}}


def test_quiet_pass_exposes_membership_changes_with_identical_counts(run_pass):
    first = run_pass(summary())
    assert first[0]["probe_coverage"]["unobserved_symbols"] == ["ZZZ"]
    assert first[0]["candidate_intake"]["full_broker_universe_observed"] is False
    assert run_pass(summary()) == []
    changed = run_pass(summary("ZZZ", "AAA"))
    assert changed[0]["probe_coverage"]["unobserved_symbols"] == ["AAA"]


def test_membership_order_does_not_generate_false_transition(run_pass):
    run_pass(summary())
    reordered = summary()
    reordered["probe_coverage"]["eligible_symbols"].reverse()
    assert run_pass(reordered) == []


def test_missing_probe_result_is_not_empty_or_previous_coverage(run_pass):
    run_pass(summary())
    guarded = {"skipped": "daily_loss_cap"}
    assert run_pass(guarded) == [{"candidate_intake": None, "probe_coverage": None}]
    assert run_pass(guarded) == []
    assert len(run_pass(summary())) == 1


def test_coverage_present_without_probes_and_on_armed_passes(run_pass):
    empty = summary()
    empty.pop("probe_coverage")
    empty["skipped"] = "no_fresh_live_eligible"
    assert run_pass(empty)[0]["probe_coverage"] is None
    armed = summary()
    armed.update(armed=True, armed_session_ids=[42])
    assert len(run_pass(armed)) == 1


def test_changed_intake_limitations_are_visible_without_membership_changes(run_pass):
    run_pass(summary())
    changed = summary()
    changed["candidate_intake"]["source"] = "different_source"
    assert run_pass(changed)[0]["candidate_intake"]["source"] == "different_source"


def test_malformed_diagnostic_cannot_prevent_waking_committed_arm(run_pass, caplog):
    malformed = summary()
    malformed.update(armed=True, armed_session_ids=[43])
    malformed["probe_coverage"]["eligible_symbols"] = ["AAA", None]
    assert run_pass(malformed) == []
    assert any("coverage unavailable" in record.getMessage() for record in caplog.records)
