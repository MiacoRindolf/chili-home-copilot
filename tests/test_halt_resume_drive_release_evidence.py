"""Ang halt-resume cooldown na harang ay nagdadala na ng ebidensya (#1270).

BAKIT ITO MAHALAGA — NASUKAT 2026-09-01:

Sa 181 sesyong may natukoy na halt sa 7 araw, 147 (81%) ang nakakita ng
resume. Sa 147 na iyon: **0 nagsumite ng entry, 0 na-fill.**

Dalawa lang ang sanctioned na landas papasok pagkatapos ng resume:

1. ``halt_resume_dip_trigger`` — tumatanggi nang ``resume_dip_forming`` sa
   **1,387 sa 1,549 (89.5%)** na ebalwasyon sa 102 sesyon. ISTRUKTURAL ito:
   ``entry_gates.py:12101`` ay nagbabalik ng "forming" habang ang post-resume
   na HIGH ay siya pa ring HULING bar, kaya sa isang STRAIGHT-UP na resume ay
   hindi ito kailanman puputok. Itinala mismo ng #1245: "ang tanging escape
   (halt_resume_dip_ok) ay imposible sa straight-up resume".

2. Ang DRIVE RELEASE — kaya ito ang **nag-iisang** landas sa isang
   one-directional na resume: ang hugis ng XPON (+17% sa 69s) at ng GPRO
   reopening (10,598,632 na share). Nailunsad lamang ito noong 2026-08-29
   (d54e3603c), kaya **TATLONG pagkakataon** pa lang ito nabigyan.

Sa tatlong sample na iyon ay hindi masasabi ng payload kung bakit ito humawak.
Ang pagkakaiba ng "nabasa ang tape at puro seller" (tama ang hawak) at "walang
nabasang tape" (sirang feed) ang buong tanong — at ``back_buy_share=None`` vs
``0.0`` ang eksaktong nagsasabi niyon.

Runnable: pytest tests/test_halt_resume_drive_release_evidence.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.services.trading.momentum_neural.live_runner import (
    halt_resume_drive_release_decision as _decide,
)

_SRC = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "services" / "trading" / "momentum_neural" / "live_runner.py"
)

# Ang mga field na dapat dalhin ng harang para masagot ang "bakit ito humawak".
REQUIRED_EVIDENCE = {
    "drive_release_enabled",
    "drive_release_verdict",
    "back_buy_share",
    "signed_tape_accel",
    "tape_read",
    "trigger_reason",
}


@pytest.fixture(scope="module")
def cooldown_payload_keys() -> set[str]:
    """Hanguin ang mga susi ng cooldown-block payload sa pamamagitan ng AST.

    AST, hindi regex: ang negatibong assertion sa isang lumilipat na fixed
    window ay tahimik na pumapasa kapag gumalaw ang code.
    """
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_cd_payload"
        ):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        return {
            k.value for k in node.value.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
    pytest.fail("hindi mahanap ang _cd_payload — gumalaw ang cooldown emit")


def test_cooldown_block_carries_the_drive_release_evidence(cooldown_payload_keys):
    """ANG PANGUNAHIN. Walang ebidensya ⇒ hindi masasagot ang 3 sample."""
    missing = REQUIRED_EVIDENCE - cooldown_payload_keys
    assert not missing, (
        f"wala sa halt_resume_cooldown na harang: {sorted(missing)} — nang wala "
        f"ang mga ito ay hindi masasabi kung ang drive-release ay humawak dahil "
        f"seller-dominado ang tape o dahil walang nabasang tape"
    )


def test_the_original_fields_are_kept(cooldown_payload_keys):
    """Walang nasisirang kasaysayan — nananatili ang lumang hugis."""
    for k in ("reason", "halt_resumed_at_utc", "cooldown_seconds"):
        assert k in cooldown_payload_keys, k


def test_tape_read_distinguishes_none_from_zero():
    """Ang None (walang tape) at 0.0 (puro seller) ay MAGKAIBANG kuwento."""
    assert (None is not None) is False           # walang nabasa  -> tape_read False
    assert (0.0 is not None) is True             # nabasa, seller -> tape_read True
    # at ang parehong dalawa ay HUMAHAWAK, kaya ang verdict lamang ay
    # hindi kayang paghiwalayin sila — kaya kailangan ang tape_read.
    assert _decide(enabled=True, already_released=False, back_buy_share=None) == "hold"
    assert _decide(enabled=True, already_released=False, back_buy_share=0.0) == "hold"


def test_release_needs_a_buyer_dominated_tape():
    """0.5 ang natural na midpoint — mahigpit na higit, hindi pantay."""
    assert _decide(enabled=True, already_released=False, back_buy_share=0.51) == "release"
    assert _decide(enabled=True, already_released=False, back_buy_share=0.50) == "hold"
    assert _decide(enabled=True, already_released=False, back_buy_share=1.0) == "release"


def test_release_is_sticky_and_flag_gated():
    assert _decide(enabled=True, already_released=True, back_buy_share=None) == "already"
    assert _decide(enabled=False, already_released=False, back_buy_share=1.0) == "hold"
    # naka-off + naka-release na: ang flag ang nananaig (walang tahimik na resume)
    assert _decide(enabled=False, already_released=True, back_buy_share=1.0) == "hold"


def test_garbage_share_holds_rather_than_releases():
    """Fail-CLOSED: hindi kailanman nagre-release sa hindi mabasang input."""
    for bad in (float("nan"), float("inf"), "kalokohan", object()):
        assert _decide(
            enabled=True, already_released=False, back_buy_share=bad,
        ) == "hold", bad
