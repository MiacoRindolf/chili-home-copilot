"""Sinong PROSESO ang pinapayagang mag-armado ng FSM wake (2026-08-24).

ANG PROBLEMA. Tatlong waker ang nagre-redispatch ng buong live FSM tick mula sa
isang in-process na daemon thread::

    ignition_loop.wake_armed_sessions  -> _spawn_arm_wake       -> arm-wake-<sid>
    live_runner._schedule_dispatch_wake -> stop-confirm timer
    live_runner._schedule_dispatch_wake -> exit-continuation timer

Wala ni isa sa kanila ang may tsek sa ROLE. Ang tanging bantay ay
``CHILI_PYTEST`` at ``CHILI_DIAGNOSTIC_REPLAY_ISOLATED``, at ang tatlong flag ay
default na ``True``.

BAKIT ITO MAHALAGA. Ang tape-delta ignite job na nagdadala sa
``wake_armed_sessions`` ay naka-register sa ilalim ng::

    include_heavy = role in ("all", "worker", "cron_only", "rnd_only")

Kasama ang ``rnd_only`` -- at ``rnd_only`` ang eksaktong role ng scheduler
container. Ang naitalang layunin ng ``rnd_only`` (``trading_scheduler.py``) ay
*"cron_only MINUS this set"*, ginawa **para hindi kailanman i-restart ng R&D
deploy ang prosesong may hawak na buhay na posisyon**.

Kaya kung walang gate na ito, ang isang R&D container ay makakapagpatakbo ng
live FSM sa loob mismo ng proseso nito, makakapagpasa ng entry, at pagkatapos ay
paulit-ulit na mag-a-arm ng sarili nitong stop-confirm / exit-continuation timer
-- ang eksaktong sitwasyong ipinagbabawal ng role na iyon.

At may pangalawang bunga: ang time-share ACCEPT census ay binibilang ang mga
order-capable na container ayon sa PANGALAN (regex ``exec|paper|runner``). Ang
``chili-clean-recovery-scheduler`` ay hindi tumutugma -- kaya iuulat nitong
malinis ang census habang may buhay na order-capable na surface.

ANG ISINASAAD. Isang bagay lang: ang wake ay maaari lamang mag-armado sa isang
prosesong ang scheduler role ay NAG-AARI ng momentum execution. Ito ang parehong
tuple na ginagamit ng ``include_momentum_exec``. Kapag hindi kabilang ang role,
ang wake ay tahimik na hindi nag-a-arm -- ang batch/loop driver ng may-ari na
lane ang tumutugon pa rin sa session sa normal nitong cadence, kaya ito ay
pagkawala ng LATENCY, hindi ng saklaw.

⚠️ Ang isang WALANG-LAMAN o hindi nakatakdang role ay ituturing na MAY-ARI.
Ang default ng ``trading_scheduler`` kapag hindi nakatakda ang env ay ``all``,
at ang lane mismo ay tahasang nagtatakda ng ``momentum_exec_only``. Ang
fail-closed dito ay tahimik na sisirain ang wake sa mga prosesong dapat may
hawak nito -- kabilang ang bawat pagpapatakbo ng test at bawat lokal na uvicorn.
"""
from __future__ import annotations

import os

# Kapareho ng `include_momentum_exec` sa `app/services/trading_scheduler.py`.
# Kung magbago iyon, dapat magbago rin ito -- may test na nagbabantay.
MOMENTUM_EXEC_ROLES: frozenset[str] = frozenset(
    {"all", "web", "worker", "cron_only", "momentum_exec_only"}
)


def current_scheduler_role() -> str:
    """Ang role ng prosesong ito, na-normalize. Walang laman = hindi nakatakda."""
    return str(os.environ.get("CHILI_SCHEDULER_ROLE") or "").strip().lower()


def process_owns_momentum_execution() -> bool:
    """True kapag ang prosesong ito ay pinapayagang magpatakbo ng live FSM tick.

    Ang hindi nakatakdang role ay nag-de-default sa MAY-ARI: iyon ang parehong
    default na ginagawa ng scheduler (``all``), at ito ang nagpapanatiling
    hindi nagbabago ang gawi para sa mga test, lokal na uvicorn, at sa lane.
    """
    role = current_scheduler_role()
    if not role:
        return True
    return role in MOMENTUM_EXEC_ROLES
