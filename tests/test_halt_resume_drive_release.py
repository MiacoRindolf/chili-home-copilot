"""Halt-resume DRIVE RELEASE — XPON 2026-08-26 forensics.

Ang 120s wall-clock cooldown pagkatapos ng halt resume ay panangga sa whipsaw
(KMRK 06-10), pero sa XPON ang resume ay ISANG DIREKSYON (43,998-share cross,
8.67→10.13 sa 69s sa at-ask na pagbili): apat na ganap na armadong entry ang
hinarang at ang buong leg ay lumipas sa loob ng cooldown; ang tanging escape
(halt_resume_dip_ok) ay structurally imposible sa straight-up resume.

Ang fix: kapag ang post-resume tape ay nababasa at buyer-dominado
(back_buy_share > 0.5), sticky na i-release ang cooldown.

Runnable: pytest tests/test_halt_resume_drive_release.py -v
"""
from __future__ import annotations

from app.services.trading.momentum_neural.live_runner import (
    halt_resume_drive_release_decision,
)


def test_majority_buy_tape_releases():
    assert halt_resume_drive_release_decision(
        enabled=True, already_released=False, back_buy_share=0.92,
    ) == "release"


def test_exact_half_share_holds():
    # 0.5 = walang nagdodomina — hindi pa napatunayang mali ang whipsaw premise.
    assert halt_resume_drive_release_decision(
        enabled=True, already_released=False, back_buy_share=0.5,
    ) == "hold"


def test_seller_dominated_tape_holds():
    assert halt_resume_drive_release_decision(
        enabled=True, already_released=False, back_buy_share=0.30,
    ) == "hold"


def test_unreadable_tape_holds_kmrk_behavior():
    assert halt_resume_drive_release_decision(
        enabled=True, already_released=False, back_buy_share=None,
    ) == "hold"


def test_sticky_release_short_circuits():
    # Kapag na-stamp na, hindi na kailangan ng panibagong tape read.
    assert halt_resume_drive_release_decision(
        enabled=True, already_released=True, back_buy_share=None,
    ) == "already"


def test_disabled_flag_holds_even_on_strong_tape():
    assert halt_resume_drive_release_decision(
        enabled=False, already_released=False, back_buy_share=0.99,
    ) == "hold"


def test_nan_share_holds():
    assert halt_resume_drive_release_decision(
        enabled=True, already_released=False, back_buy_share=float("nan"),
    ) == "hold"
