"""Verdict + market-session derivation for the live observer status board.

These are pure-function tests: no DB, no fixtures. They exist because the page's
whole value rests on two invariants that are easy to regress by accident.

  1. An unrecognised lane status must never render as healthy. The status vocabulary
     is OPEN (``_captured_runtime_identity`` returns ``str(reason)`` for arbitrary
     upstream reasons), so an exhaustive mapper would show a novel failure as
     all-clear -- reintroducing the exact bug this surface exists to kill.
  2. A beating control loop must never, on its own, produce a healthy verdict while
     the tape is dead. That is the "heartbeat != healthy" failure.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.trading.momentum_neural.live_status import (
    TAPE_BLIND_SECONDS,
    classify_watch_status,
    describe_watch_status,
    market_session_snapshot,
    resolve_lane_verdict,
)


def _verdict(**over):
    base = dict(
        watch_status="ok",
        market_open=True,
        market_label="Regular hours",
        control_age_s=4.0,
        tape_age_s=19.0,
        stale_after_s=75.0,
        lane_health={"enabled": True, "frozen": False},
        symbol_count=3,
    )
    base.update(over)
    return resolve_lane_verdict(**base)


# --------------------------------------------------------------------------- #
# 1. Open vocabulary: unknown must never be all-clear
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "status,bucket",
    [
        ("ok", "ok"),
        ("OK", "ok"),
        ("runtime_stale", "degraded"),
        ("live_runner_loop_heartbeat_stale", "degraded"),
        ("captured_watch_heartbeat_missing", "blind"),
        ("captured_watch_heartbeat_not_today", "blind"),
        ("captured_watch_generation_ambiguous", "blind"),
        ("captured_watch_inventory_unreadable", "blind"),
        # lane_health has its own vocabulary that flows through verbatim.
        ("captured_paper_heartbeat_account_mismatch", "blind"),
        ("live_runner_loop_owner_overlap", "blind"),
        # A status nobody has written yet must still read as a failure.
        ("captured_watch_some_future_failure_mode", "blind"),
        ("totally_unknown_string", "unknown"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_classify_watch_status_never_defaults_to_ok(status, bucket):
    assert classify_watch_status(status) == bucket


def test_novel_status_resolves_to_warn_not_ok():
    v = _verdict(watch_status="a_status_that_did_not_exist_when_this_was_written")
    assert v["verdict"] == "unknown"
    assert v["severity"] == "warn"
    assert "unrecognized status" in v["detail"]


def test_describe_watch_status_always_returns_a_sentence():
    assert describe_watch_status("captured_watch_heartbeat_missing").startswith(
        "No captured-PAPER heartbeat row"
    )
    # Unknown status still gets actionable copy rather than an empty string.
    assert "Treat the lane as blind" in describe_watch_status("brand_new")


# --------------------------------------------------------------------------- #
# 2. heartbeat != healthy
# --------------------------------------------------------------------------- #

def test_beating_loop_with_dead_tape_is_not_healthy():
    """The regression that motivated the whole surface."""
    v = _verdict(watch_status="ok", control_age_s=2.0, tape_age_s=None, symbol_count=0)
    assert v["verdict"] == "tape_blind"
    assert v["severity"] == "warn"
    assert v["severity"] != "ok"


def test_stale_tape_past_threshold_is_flagged():
    assert _verdict(tape_age_s=TAPE_BLIND_SECONDS + 1)["verdict"] == "tape_blind"
    assert _verdict(tape_age_s=TAPE_BLIND_SECONDS - 1)["verdict"] == "ok"


def test_dead_tape_while_market_closed_is_idle_not_alarming():
    """A silent tape on a Saturday is correct, not a fault."""
    v = _verdict(
        tape_age_s=None, market_open=False, market_label="Weekend - market closed",
        symbol_count=0,
    )
    assert v["verdict"] == "idle_closed"
    assert v["severity"] == "info"


# --------------------------------------------------------------------------- #
# 3. Resolution order
# --------------------------------------------------------------------------- #

def test_halted_outranks_every_other_signal():
    v = _verdict(
        watch_status="captured_watch_heartbeat_missing",
        lane_health={
            "enabled": True, "frozen": True,
            "headline": "Kill switch active.", "detail": "Manual reset required.",
        },
    )
    assert v["verdict"] == "halted"
    assert "Kill switch active." in v["detail"]


def test_disabled_alerting_does_not_claim_halted():
    """`evaluate_lane_health` short-circuits to frozen=False when alerting is off,
    so `frozen` is meaningless there. The UI surfaces the disabled probe separately
    from `lane_health.enabled`; the verdict must not invent a halt."""
    v = _verdict(lane_health={"enabled": False, "frozen": True, "headline": "x"})
    assert v["verdict"] != "halted"


def test_blind_outranks_stale_and_tape():
    v = _verdict(
        watch_status="captured_watch_heartbeat_missing",
        control_age_s=999.0, tape_age_s=None,
    )
    assert v["verdict"] == "blind"
    assert v["severity"] == "critical"


def test_runtime_stale_is_warn_not_critical():
    """A late loop still manages open positions -- degradation, not blackout."""
    v = _verdict(watch_status="runtime_stale", control_age_s=94.0)
    assert v["verdict"] == "degraded"
    assert v["severity"] == "warn"
    assert "94s" in v["detail"] and "75s" in v["detail"]


def test_healthy_open_market_distinguishes_trading_from_watching():
    assert "watching" in _verdict(symbol_count=0)["headline"].lower()
    assert "trading" in _verdict(symbol_count=3)["headline"].lower()


def test_every_input_combination_yields_a_verdict():
    """Total function: no combination may fall through to None/empty."""
    for status in ("ok", "runtime_stale", "captured_watch_heartbeat_missing", "junk", None):
        for market_open in (True, False):
            for tape in (None, 5.0, 9999.0):
                for frozen in (True, False):
                    v = _verdict(
                        watch_status=status, market_open=market_open, tape_age_s=tape,
                        lane_health={"enabled": True, "frozen": frozen},
                    )
                    assert v["verdict"], (status, market_open, tape, frozen)
                    assert v["severity"] in ("ok", "info", "warn", "critical")
                    assert v["headline"]


# --------------------------------------------------------------------------- #
# 4. Market session -- the crypto trap and the three flavours of "closed"
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "when,phase,is_open",
    [
        # 2026-08-08 is a Saturday.
        (datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc), "closed_weekend", False),
        # 2026-08-12 is a Wednesday. 14:00Z = 10:00 ET.
        (datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc), "regular", True),
        (datetime(2026, 8, 12, 11, 0, tzinfo=timezone.utc), "premarket", True),
        (datetime(2026, 8, 12, 21, 0, tzinfo=timezone.utc), "afterhours", True),
        (datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc), "closed_overnight", False),
        # 2026-07-04 falls on a Saturday, so the market closes Friday the 3rd.
        (datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc), "closed_holiday", False),
    ],
)
def test_market_session_expands_closed_into_three_phases(when, phase, is_open):
    snap = market_session_snapshot(when)
    assert snap["phase"] == phase
    assert snap["is_open"] is is_open


def test_market_session_is_not_fooled_by_crypto():
    """`market_session_now` returns "regular" unconditionally for crypto symbols.
    If the snapshot ever starts passing a symbol through, a Saturday reads as open
    and every empty-state branch inverts."""
    saturday_3am = datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc)
    assert market_session_snapshot(saturday_3am)["phase"] == "closed_weekend"


def test_market_session_names_the_holiday():
    snap = market_session_snapshot(datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc))
    assert snap["holiday_name"] and "Independence Day" in snap["holiday_name"]
    assert "Independence Day" in snap["label"]


def test_next_open_skips_weekends_and_holidays():
    snap = market_session_snapshot(datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc))
    assert snap["next_open_label"].startswith("Monday")
    assert snap["next_open_in"]


def test_naive_datetime_is_treated_as_utc():
    aware = market_session_snapshot(datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc))
    naive = market_session_snapshot(datetime(2026, 8, 8, 18, 0))
    assert aware["phase"] == naive["phase"]
