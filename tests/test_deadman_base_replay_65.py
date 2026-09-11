"""[65] review of #1419 -- the replay of record's clock and stop inversion (DB-free).

  minor  the RTH window was a hard-coded EDT clock literal (13:30-20:00 UTC) and the tape read
         started at a fixed 08:00Z -- after the 2026-11-01 DST change both are wrong by 1 h with
         no warning. Both now come from zoneinfo America/New_York, the way the live runner
         derives its session key.
  minor  the broker-buffer inversion copied the runner's 0.0025 / 0.25 / 0.01 -- it now inverts
         the runner's OWN `deadman_stop_buffer` (the named `DEADMAN_STOP_BUFFER_*`).
  major  the replay's shipped variant is the SHIPPED base: the resting stop at the fill; the
         ledger's median is context (the #1419 draft rule is kept only as a labelled variant).

Runnable: pytest tests/test_deadman_base_replay_65.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.trading.momentum_neural import live_runner as lr
from scripts import deadman_base_replay_65 as R


def _ts(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("ts,want", [
    # EDT (2026-09-11): 09:30-16:00 ET = 13:30-20:00 UTC
    (_ts(2026, 9, 11, 13, 29), False), (_ts(2026, 9, 11, 13, 30), True),
    (_ts(2026, 9, 11, 19, 59), True), (_ts(2026, 9, 11, 20, 0), False),
    # EST (2026-11-02, the first session after the 2026-11-01 change): 14:30-21:00 UTC
    (_ts(2026, 11, 2, 13, 30), False),     # 08:30 EST -- the old literal called this RTH
    (_ts(2026, 11, 2, 14, 29), False), (_ts(2026, 11, 2, 14, 30), True),
    (_ts(2026, 11, 2, 20, 30), True),      # 15:30 EST -- the old literal called this closed
    (_ts(2026, 11, 2, 20, 59), True), (_ts(2026, 11, 2, 21, 0), False),
])
def test_the_broker_stop_rth_window_is_new_york_wall_time_across_dst(ts, want):
    assert R.rth(ts) is want


def test_the_tape_read_starts_at_0400_new_york_across_dst():
    assert R.session_start_utc("2026-09-11") == datetime(2026, 9, 11, 8, 0)     # EDT
    assert R.session_start_utc("2026-11-02") == datetime(2026, 11, 2, 9, 0)     # EST
    # the live runner's own session key agrees on both sides of the change
    assert lr._tape_cycle_day_key_at(datetime(2026, 11, 2, 9, 0)) == "2026-11-02"
    assert lr._tape_cycle_day_key_at(datetime(2026, 11, 2, 8, 59)) == "2026-11-01"


@pytest.mark.parametrize("avg", [0.85, 1.0, 2.79, 6.9, 7.13, 10.0, 25.0])
@pytest.mark.parametrize("risk_frac", [0.003, 0.01, 0.03, 0.052, 0.08, 0.15])
def test_the_inversion_recovers_the_software_stop_from_the_runners_own_buffer(avg, risk_frac):
    sw = avg * (1.0 - risk_frac)
    broker = sw - lr.deadman_stop_buffer(avg, sw)
    assert R.software_stop(broker, avg) == pytest.approx(sw, abs=1e-9)


def test_the_replay_names_its_constants_from_the_runner():
    assert (R.DEADMAN_STOP_BUFFER_AVG_FRAC, R.DEADMAN_STOP_BUFFER_RISK_FRAC, R.DEADMAN_STOP_BUFFER_MIN_USD) == (
        lr.DEADMAN_STOP_BUFFER_AVG_FRAC, lr.DEADMAN_STOP_BUFFER_RISK_FRAC, lr.DEADMAN_STOP_BUFFER_MIN_USD)


def test_the_shipped_variant_is_the_resting_stop_and_the_draft_rule_is_only_a_labelled_variant():
    rows = []
    t0 = _ts(2026, 9, 11, 9, 0)
    prices = [6.27, 6.29, 6.27] + [p for k in range(13) for p in (round(6.30 + 0.01 * k, 2), round(6.28 + 0.01 * k, 2))]
    prices += [6.60, 6.90, 6.62, 7.00]
    for i, px in enumerate(prices):
        rows.append((t0 + i, 1000 + i, px, 100.0, px - 0.01, px + 0.01))
    b = R.ledger_base(rows, len(rows), 7.13, 6.762)
    assert b["level"] == 6.762 and b["binding"] == "resting_stop_at_fill"
    assert b["resting_stop_source"] == "replay_inverted_broker_deadman"
    cand = b["cont_context"]["cont_candidate"]
    assert cand == pytest.approx(7.11)                       # the cold-start median (0.02)
    assert R.max_floor(6.762, cand, 7.13) == pytest.approx(7.11)     # what the draft would bind
    assert R.max_floor(6.762, 7.20, 7.13) == 6.762          # outside (0, entry): the resting stop
    assert R.max_floor(6.762, None, 7.13) == 6.762
    assert R.max_floor(None, 7.0, 7.13) == 7.0
    assert ("N1_c4", "S0_live") == R.PAIRS[0]                # the headline pair is the shipped one
