"""Chase-cap HALT RE-ANCHOR — XPON 2026-08-26 forensics.

Pagkatapos ng drive release, ang dominanteng harang sa post-halt rip ay ang
anti-chase cap: naka-angkla sa prior HWM 8.31 habang ang LULD auction ay
muling nagpresyo sa 8.67 (43,998-share cross) — BAWAT post-resume tick ay
nabasa bilang "chase". Ang halt na nag-resume PAGKATAPOS ng prior exit ay
nagpapawalang-bisa sa lumang resistensya: ang resumption open ang bagong
reference, at ang parehong cap*ATR ceiling ay humaharang pa rin sa parabolic
top na malayo sa itaas ng resume (SVRE/JEM fade-chase class ay protektado).

Runnable: pytest tests/test_chase_cap_halt_reanchor.py -v
"""
from __future__ import annotations

from app.services.trading.momentum_neural.live_runner import (
    chase_cap_halt_reanchor,
)


def _call(**over):
    kw = dict(
        anchor=8.31,
        resumption_open=8.67,
        resumed_at_utc="2026-08-26T13:55:29.795583",
        prior_exited_at_utc="2026-08-26T13:49:36.000000",
    )
    kw.update(over)
    return chase_cap_halt_reanchor(**kw)


def test_halt_up_resume_after_exit_reanchors_to_resumption_open():
    # Ang eksaktong XPON shape: exit 13:49:36, resume 13:55:29 @ 8.67 > 8.31.
    assert _call() == 8.67


def test_resume_before_prior_exit_keeps_old_anchor():
    # Halt na mas luma sa exit — ang exit na ang pinakabagong resistensya.
    assert _call(
        resumed_at_utc="2026-08-26T13:40:00",
        prior_exited_at_utc="2026-08-26T13:49:36",
    ) == 8.31


def test_halt_down_resume_keeps_old_anchor():
    # Resume sa IBABA ng anchor: walang halt-up repricing; mas mahigpit ang
    # lumang anchor at iyon ang mananatili.
    assert _call(resumption_open=8.10) == 8.31


def test_missing_resumption_open_keeps_old_anchor():
    assert _call(resumption_open=None) == 8.31


def test_missing_timestamps_keep_old_anchor():
    assert _call(resumed_at_utc=None) == 8.31
    assert _call(prior_exited_at_utc=None) == 8.31


def test_unparseable_timestamp_keeps_old_anchor():
    assert _call(resumed_at_utc="hindi-petsa") == 8.31


def test_none_anchor_stays_none():
    assert _call(anchor=None) is None


def test_z_suffix_timestamps_parse():
    assert _call(
        resumed_at_utc="2026-08-26T13:55:29Z",
        prior_exited_at_utc="2026-08-26T13:49:36Z",
    ) == 8.67
