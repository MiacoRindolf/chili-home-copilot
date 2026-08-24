"""Ang FSM wake ay dapat mag-armado LAMANG sa prosesong nagmamay-ari ng execution.

ANG NAKAWALA (2026-08-24, natuklasan sa audit ng 53 commit bago ang image rebuild).
Tatlong waker ang nagre-redispatch ng buong live FSM tick mula sa in-process na
daemon thread, at wala ni isa sa kanila ang may tsek sa role::

    ignition_loop.wake_armed_sessions   -> _spawn_arm_wake  -> arm-wake-<sid>
    live_runner._schedule_dispatch_wake -> stop-confirm timer
    live_runner._schedule_dispatch_wake -> exit-continuation timer

Ang job na nagdadala sa una ay naka-register sa ilalim ng::

    include_heavy = role in ("all", "worker", "cron_only", "rnd_only")

Kasama ang ``rnd_only`` -- ang eksaktong role ng scheduler container. At ang
naitalang layunin ng ``rnd_only`` ay *"cron_only MINUS this set"*, ginawa **para
hindi kailanman i-restart ng R&D deploy ang prosesong may hawak na buhay na
posisyon**.

Napatunayang BAGO ito: ``git show 2e1eb77:...ignition_loop.py`` ay may **0** na
paglitaw ng ``wake_armed_sessions``; ang kasalukuyang main ay may 2. Ang lumang
image ay NAG-AARM ng session mula sa `rnd_only` ngunit hindi kailanman
NAGTI-TICK ng isa.

⚠️ Pangalawang bunga: ang time-share ACCEPT census ay binibilang ang
order-capable na container ayon sa PANGALAN (`exec|paper|runner`). Ang
`chili-clean-recovery-scheduler` ay hindi tumutugma -- kaya iuulat nitong
malinis ang census habang may buhay na order-capable na surface.

Runnable: pytest tests/test_wake_role_ownership.py -v
"""
from __future__ import annotations

import inspect

import pytest

from app.services.trading.momentum_neural import wake_ownership as wo


def test_rnd_only_does_not_own_execution(monkeypatch):
    """ANG EKSAKTONG KASO NA NAKAWALA."""
    monkeypatch.setenv("CHILI_SCHEDULER_ROLE", "rnd_only")
    assert wo.process_owns_momentum_execution() is False


@pytest.mark.parametrize("role", ["all", "web", "worker", "cron_only", "momentum_exec_only"])
def test_the_exec_roles_still_own_it(monkeypatch, role):
    """Walang nawawalang saklaw -- ang lane ay tumatakbo sa momentum_exec_only."""
    monkeypatch.setenv("CHILI_SCHEDULER_ROLE", role)
    assert wo.process_owns_momentum_execution() is True


@pytest.mark.parametrize("role", ["", "   "])
def test_an_unset_role_defaults_to_OWNER(monkeypatch, role):
    """⚠️ FAIL-OPEN NANG SADYA. Ang default ng scheduler kapag hindi nakatakda
    ang env ay 'all'. Ang fail-closed dito ay tahimik na sisirain ang wake sa
    bawat test run at bawat lokal na uvicorn."""
    monkeypatch.setenv("CHILI_SCHEDULER_ROLE", role)
    assert wo.process_owns_momentum_execution() is True


def test_a_missing_env_var_defaults_to_owner(monkeypatch):
    monkeypatch.delenv("CHILI_SCHEDULER_ROLE", raising=False)
    assert wo.process_owns_momentum_execution() is True


def test_role_matching_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("CHILI_SCHEDULER_ROLE", "  RND_ONLY  ")
    assert wo.process_owns_momentum_execution() is False
    monkeypatch.setenv("CHILI_SCHEDULER_ROLE", "  Momentum_Exec_Only ")
    assert wo.process_owns_momentum_execution() is True


def test_role_none_does_not_own_execution(monkeypatch):
    """Ang `none` ay ang WEB container: tahasang walang APScheduler
    (`docker-entrypoint-chili.sh`: "web: none = no APScheduler in Uvicorn").
    Ang isang prosesong sadyang walang scheduler ay hindi dapat magpatakbo ng
    background FSM tick. Ito rin ang default ng `tests/conftest.py:87`, kaya ang
    anumang suite na sumusubok sa MEKANISMO ng wake ay dapat tahasang magtakda
    ng may-ari na role."""
    monkeypatch.setenv("CHILI_SCHEDULER_ROLE", "none")
    assert wo.process_owns_momentum_execution() is False


def test_an_unknown_role_does_not_own_execution(monkeypatch):
    """Ang hindi kilalang role ay hindi dapat basta nagmamana ng awtoridad."""
    monkeypatch.setenv("CHILI_SCHEDULER_ROLE", "broker_sync_only")
    assert wo.process_owns_momentum_execution() is False


def test_the_role_tuple_matches_the_scheduler(monkeypatch):
    """⚠️ ANG BANTAY SA DRIFT. Kung magbago ang `include_momentum_exec` at
    hindi ito, ang wake ay tahimik na maling ipagkakait o ipagkakaloob."""
    from app.services import trading_scheduler as ts

    src = inspect.getsource(ts)
    line_idx = src.index("include_momentum_exec = role in (")
    block = src[line_idx:line_idx + 200]
    for role in wo.MOMENTUM_EXEC_ROLES:
        assert f'"{role}"' in block, f"{role} ay nawawala sa include_momentum_exec"


def test_the_arm_wake_path_is_gated():
    """Bantayan ang tunay na call site -- hindi lang ang helper."""
    from app.services.trading.momentum_neural import ignition_loop

    src = inspect.getsource(ignition_loop._spawn_arm_wake)
    assert "process_owns_momentum_execution" in src
    # ang gate ay dapat NAUUNA sa pag-spawn ng thread
    assert src.index("process_owns_momentum_execution") < src.index("threading.Thread")


def test_the_dispatch_wake_path_is_gated():
    from app.services.trading.momentum_neural import live_runner

    src = inspect.getsource(live_runner._schedule_dispatch_wake)
    assert "process_owns_momentum_execution" in src
    assert src.index("process_owns_momentum_execution") < src.index("threading.Timer")


def test_the_gate_runs_before_the_loop_timer_is_consulted():
    """Kahit ang loop timer ay hindi dapat tawagin mula sa maling proseso."""
    from app.services.trading.momentum_neural import live_runner

    src = inspect.getsource(live_runner._schedule_dispatch_wake)
    assert src.index("process_owns_momentum_execution") < src.index(
        "schedule_live_runner_stop_confirmation"
    )
