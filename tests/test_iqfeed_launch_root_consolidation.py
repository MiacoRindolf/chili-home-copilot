"""Ang buong IQFeed launch chain ay dapat nasa ILALIM NG IISANG ROOT.

BAKIT. Ang ``scripts/collect_captured_paper_host_snapshot.py`` ay nagpapatunay
ng TATLONG path laban sa isang ``--legacy-root``::

    argv[0] ng task              -> ang VBS wrapper
    -File na argumento ng task   -> ang PowerShell starter
    cmdline[1] ng proseso        -> ang bridge .py

(Ang ``wscript.exe`` / ``powershell.exe`` / ``python.exe`` ay hindi -- sila ay
``_stable_hash_unrooted``.)

Hati sa TATLONG DRIVE ang host noong 2026-08-24 -- wscript sa C:, ang .vbs/.ps1
sa D:, ang bridge .py sa E: -- kaya walang isang root ang makakasapat. Ang
collector ay tumatanggi nang ``PATH_OUTSIDE_ROOT``, ang cutover ay walang
rollback authority, at ang sealed capture rail ay nananatiling hindi nakabuklod:
**5,302,433 na row ang tinanggihan sa isang session**.

Ang root ngayon ay ``E:\dev\wt-window2`` -- kung saan na tumatakbo ang bridges.

⚠️ Ang mga tseke dito ay laban sa REPO, hindi sa live na host. Ang
depinisyon ng scheduled task ay hiwalay na operational na estado.

Runnable: pytest tests/test_iqfeed_launch_root_consolidation.py -v
"""
from __future__ import annotations

import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_IQ = _ROOT / "project_ws" / "AgentOps" / "iqfeed"

_STARTERS = ("start-iqfeed-depth-bridge-main.ps1", "start-iqfeed-trade-bridge-main.ps1")
_RUNNERS = ("run-depth-bridge.cmd", "run-trade-bridge.cmd")


def _text(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _code_lines(p: pathlib.Path) -> list[str]:
    """Mga linyang HINDI komento -- ang komento ay nagtatala ng kasaysayan."""
    out = []
    for ln in _text(p).splitlines():
        s = ln.strip()
        if s and not s.startswith("#") and not s.lower().startswith("rem "):
            out.append(ln)
    return out


@pytest.mark.parametrize("name", _STARTERS + _RUNNERS)
def test_the_chain_is_in_the_repo(name):
    """Ang launcher ay dapat naka-bersyon, hindi untracked sa ibang tree."""
    assert (_IQ / name).is_file(), f"{name} ay wala sa repo"


def test_run_hidden_vbs_is_in_the_repo():
    """Ang VBS wrapper ay `argv[0]` ng task at ROOT-CHECKED."""
    assert (_ROOT / "scripts" / "run-hidden.vbs").is_file()


@pytest.mark.parametrize("name", _STARTERS + _RUNNERS)
def test_no_executable_line_points_at_the_old_D_root(name):
    """⚠️ ANG PANGUNAHING BANTAY. Ang isang natirang D: na reference ay
    magbabalik ng tatlong-drive na hati at muling magre-reject ng collector."""
    bad = [ln for ln in _code_lines(_IQ / name) if "D:\dev\chili-home-copilot" in ln]
    assert not bad, f"{name} ay tumuturo pa rin sa lumang root: {bad}"


@pytest.mark.parametrize("name", _STARTERS)
def test_the_starter_launches_its_runner_from_the_same_root(name):
    """Sinusuri kada BAHAGI para hindi masira ng backslash escaping ang bantay."""
    launch = [
        ln for ln in _code_lines(_IQ / name)
        if "Start-Process" in ln and "-bridge.cmd" in ln
    ]
    assert len(launch) == 1, f"{name}: inaasahan ang isang runner launch, nakuha {launch}"
    line = launch[0]
    drive, _, rest = line.partition(":")
    assert drive.strip().endswith("'E"), f"ang runner ay dapat nasa E: drive: {line}"
    for part in ("wt-window2", "project_ws", "AgentOps", "iqfeed"):
        assert part in rest, f"nawawala ang {part!r} sa runner path: {line}"


@pytest.mark.parametrize("name", _RUNNERS)
def test_the_runner_keeps_its_repo_cd_and_supervisor_loop(name):
    """⚠️ HUWAG BAWASAN ANG .CMD. Umiiral ito para sa dalawang nasukat na dahilan:
    (1) ang `Start-Process -Redirect*` sa isang task ay pipe na walang pump ->
    bumabara ang stderr -> 28/28 handshake failure; (2) kung walang `cd` sa repo
    root ay hindi nakikita ang `.env` -> WALA ang market-data API key -> WALANG
    LAMAN ang ROSS band. May supervisor restart loop din ito."""
    src = _text(_IQ / name)
    assert 'set "REPO=E:\dev\wt-window2"' in src
    assert "cd /d " in src
    assert ":bridge_loop" in src and "goto bridge_loop" in src


@pytest.mark.parametrize("name", _RUNNERS)
def test_the_runner_still_runs_the_bridge_from_the_same_root(name):
    src = _text(_IQ / name)
    assert "%REPO%\scripts\iqfeed_" in src and "_bridge.py" in src


def test_the_starter_is_valid_powershell():
    """Ang syntax error dito ay tahimik na pumapatay sa bridges sa boot."""
    import subprocess

    for name in _STARTERS:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "$e=$null;[System.Management.Automation.Language.Parser]::ParseFile("
             f"'{_IQ / name}',[ref]$null,[ref]$e)|Out-Null;exit $e.Count"],
            capture_output=True,
        )
        assert r.returncode == 0, f"{name} ay may {r.returncode} parse error"
