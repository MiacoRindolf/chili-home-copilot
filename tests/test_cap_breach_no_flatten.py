"""Aggregate-cap breach must never liquidate working positions (2026-06-12).

The at-risk cap breach path called the flatten helper UNGATED — when VSME+
ALOY+ASTN pushed open at-risk $160 past the $122 cap, the runner market-
dumped ALOY (a +$15 winner mid-trail) and ASTN simultaneously. A cap breach
blocks NEW risk; only the kill switch force-exits."""

import re


def test_held_position_boundary_fail_flatten_is_kill_switch_gated():
    src = open(
        "app/services/trading/momentum_neural/live_runner.py", encoding="utf-8"
    ).read()
    # the held-position boundary-fail branch must gate the flatten on the
    # kill switch, not call it unconditionally
    block = src.split("_held_position_keeps_exit_on_boundary_fail(sess.state")[1][:900]
    assert "_kill_switch_blocks_live() and _handle_kill_switch_mid_run()" in block
    assert not re.search(r"\n\s+if _handle_kill_switch_mid_run\(\):", block)
