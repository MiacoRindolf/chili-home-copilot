"""Ang tanong ay hindi "naka-register ba ang job" kundi "buhay ba ang proseso".

ANG PANGYAYARI (2026-08-25). Ang ``chili-clean-recovery-broker-sync`` at
``chili-clean-recovery-autotrader`` ay ``Exited (137)`` nang **pitong linggo**.
Walang alarma.

Ang delikadong bahagi ay ito: ang mga role gate sa code ay TAMA. Ang
``include_broker_sync`` ay nakalista na ang ``broker_sync_only``; ang
``include_autotrader`` ay ang ``autotrader_only``. Walang masisisi sa source, at
walang code review ang makakahuli nito. Ang mga proseso lamang na dapat magdala
ng mga role na iyon ay patay.

⚠️ Kaya nga bulag dito ang umiiral nang canonical-job assertion: binibilang nito
ang mga job SA LOOB ng isang tumatakbong scheduler, at hindi makakakita ng
scheduler na hindi tumatakbo. Ang kawalan ay hindi kayang sukatin mula sa loob.

Ang nawala habang patay sila: broker-DB position sync kada 2 min,
stuck-order canceller, disconnect alarm, bracket repair sweep, at ang TANGING
nag-e-evaluate ng software stop/target ng crypto -- habang may tatlong buhay na
Coinbase position na nagkakahalaga ng ~$1.8k.

Runnable: pytest tests/test_expected_containers_guard.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_expected_containers.py"
)


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("_check_expected_containers", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_check_expected_containers"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_two_that_died_for_seven_weeks_are_expected(guard):
    """⚠️ REGRESSION PIN. Kung matanggal ang alinman sa dalawang ito sa mapa ay
    tahimik na babalik ang pitong linggong katahimikan."""
    assert "chili-clean-recovery-broker-sync" in guard.EXPECTED
    assert "chili-clean-recovery-autotrader" in guard.EXPECTED


def test_every_entry_says_what_is_lost_not_just_that_it_is_down(guard):
    """Ang alarma ay binabasa sa gitna ng gabi. Ang 'X ay patay' ay hindi sapat --
    kailangang sagutin nito ang 'ano ang nawawala sa akin' nang hindi nagbubukas
    ng code."""
    for name, why in guard.EXPECTED.items():
        assert why and len(why) > 20, f"{name} ay walang makabuluhang dahilan"


def test_a_dead_container_is_reported_and_exits_nonzero(guard, monkeypatch):
    def fake(args):
        if "ps" in args and "-a" not in args:
            return "chili-clean-recovery-web\nchili-home-copilot-postgres-1\n"
        return "Exited (137) 7 weeks ago\n"

    monkeypatch.setattr(guard, "_run", fake)
    assert guard.main(["--json"]) == 1


def test_all_up_exits_zero(guard, monkeypatch):
    """Kontrol: hindi ito dapat laging nag-aalarma."""
    everything = "\n".join(guard.EXPECTED) + "\n"
    monkeypatch.setattr(guard, "_run", lambda a: everything if "-a" not in a else "Up 2 hours\n")
    assert guard.main(["--json"]) == 0


def test_a_dead_docker_does_not_read_as_all_healthy(guard, monkeypatch):
    """⚠️ FAIL-LOUD. Kapag patay ang docker ay walang ibinabalik ang `docker ps`.
    Ang basahin iyon bilang 'walang nawawala' ang eksaktong hugis ng bug na
    inaayos ng tsekeng ito."""
    monkeypatch.setattr(guard, "_run", lambda _a: "")
    assert guard.main(["--json"]) == 1, "walang laman na docker ps ⇒ LAHAT ay nawawala"


def test_the_name_filter_is_anchored(guard, monkeypatch):
    """Ang `--filter name=x` ng docker ay substring match, kaya ang
    `...-broker-sync-pre571` ay tutugma sa `...-broker-sync`. Nakaangkla ang
    pattern (^...$) para ang isang lumang backup na container ay hindi
    magmukhang buhay ang tunay."""
    seen: list[list[str]] = []

    def fake(args):
        seen.append(args)
        return ""

    monkeypatch.setattr(guard, "_run", fake)
    guard.status_of("chili-clean-recovery-broker-sync")
    joined = " ".join(seen[-1])
    assert "name=^chili-clean-recovery-broker-sync$" in joined
