"""Ang mahabang-interval na job ay dapat tumakbo pagkatapos ng restart, hindi lang
pagkaraan ng isang buong interval.

ANG MEKANISMO. Ang ``IntervalTrigger(hours=4)`` na walang ``start_date`` ay
kumukuha ng "ngayon" sa oras na GAWIN ang trigger, at ang unang pagputok ay
``start_date + interval``. Napatunayan laban sa tunay na APScheduler: eksaktong
4.00 oras. Kaya ang unang pagtakbo ay HINDI sa startup -- at **binubura ng bawat
restart ang orasan**. Sa isang araw ng rebuild na mas madalas kaysa sa interval,
ang job ay HINDI TUMATAKBO KAILANMAN.

ANG PANGYAYARI (2026-08-25). Ang PR #1148 ay nag-ayos ng triple-barrier labeler
na hindi kailanman nakapaglagay ng label. Tatlong beses na-restart ang scheduler
nang gabing iyon para maipasok ang ayos. Kinaumagahan ay **0 pa rin ang laman ng
``trading_triple_barrier_labels``** -- hindi dahil sira ang ayos, kundi dahil ang
job ay nakatakdang pumutok 4 na oras pagkatapos ng huling restart, paulit-ulit.

Ang ayos ay HINDI bago: **52 sa 61** na ``IntervalTrigger`` sa
``trading_scheduler.py`` ang nagtatakda na ng ``next_run_time`` -- iyon ang
kombensiyon dito. Dalawang mahabang-interval na job lamang ang naiwan.

⚠️ Ang tsekeng ito ay para sa mga job na >= 1 oras. Ang maiikli (5s-30m) ay
nakakabawi sa sarili sa loob ng isang restart cycle, kaya hindi sila hinihingian
-- pero kung magtakda sila ay mas mabuti pa rin.

⚠️ Bago dagdagan ng ``next_run_time`` ang isang job, tiyaking LIGTAS itong
tumakbo malapit sa startup. Ang dalawang idinagdag dito ay purong backfill at
labeling; walang order placement sa alinman.

Runnable: pytest tests/test_interval_jobs_run_after_restart.py -v
"""
from __future__ import annotations

import io
import pathlib
import re
from datetime import datetime

import pytest

_SCHED = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "services" / "trading_scheduler.py"
)

_ADD_JOB = re.compile(r"_scheduler\.add_job\((.*?)\n\s*\)\n", re.S)
_ID = re.compile(r'id="([^"]+)"')
_INTERVAL = re.compile(r"IntervalTrigger\(([^)]*)\)")


def _interval_jobs() -> list[tuple[str, float, bool]]:
    """(job id, interval sa minuto, may tahasang unang-takbo)."""
    src = io.open(_SCHED, encoding="utf-8-sig").read()
    out: list[tuple[str, float, bool]] = []
    for block in _ADD_JOB.findall(src):
        m = _INTERVAL.search(block)
        if not m:
            continue
        idm = _ID.search(block)
        args = m.group(1)
        hours = re.search(r"hours=(\d+)", args)
        minutes = re.search(r"minutes=(\d+)", args)
        seconds = re.search(r"seconds=(\d+)", args)
        total = (
            (int(hours.group(1)) * 60 if hours else 0)
            + (int(minutes.group(1)) if minutes else 0)
            + (int(seconds.group(1)) / 60.0 if seconds else 0.0)
        )
        explicit = ("next_run_time" in block) or ("start_date" in args)
        out.append((idm.group(1) if idm else "?", total, explicit))
    return out


def test_the_scan_sees_the_scheduler():
    """Bantay laban sa walang-lamang tseke na tahimik na pumapasa."""
    jobs = _interval_jobs()
    assert len(jobs) > 40, f"inaasahan ang dose-dosenang interval job, nakuha {len(jobs)}"


def test_apscheduler_really_does_wait_a_full_interval():
    """⚠️ ANG PREMISE, hindi alaala. Kung magbago ito sa itaas na bersyon ng
    APScheduler ay dapat malaman natin dito, hindi sa pamamagitan ng isang job
    na tahimik na hindi tumatakbo."""
    from apscheduler.triggers.interval import IntervalTrigger

    trigger = IntervalTrigger(hours=4)
    now = datetime.now(trigger.timezone)
    first = trigger.get_next_fire_time(None, now)
    waited_hours = (first - now).total_seconds() / 3600.0
    assert 3.9 < waited_hours < 4.1, (
        f"inaasahan ang unang pagputok pagkaraan ng buong interval, nakuha {waited_hours:.2f}h"
    )


def test_every_long_interval_job_runs_soon_after_startup():
    """⚠️ ANG PANGUNAHING BANTAY."""
    late = [
        (jid, mins) for jid, mins, explicit in _interval_jobs()
        if mins >= 60 and not explicit
    ]
    if late:
        detail = "; ".join(f"{jid} (kada {m/60:.1f}h)" for jid, m in late)
        pytest.fail(
            "May mahabang-interval na job na walang next_run_time. Ang unang "
            "pagtakbo nito ay isang buong interval pagkatapos ng startup, at "
            "binubura ng bawat restart ang orasan: " + detail
        )


def test_the_two_jobs_that_were_missing_it_now_have_it():
    """Pin sa dalawang partikular na naitama, para hindi tahimik na bumalik."""
    jobs = {jid: explicit for jid, _m, explicit in _interval_jobs()}
    for jid in ("triple_barrier_label_cycle", "monitor_decision_review"):
        assert jid in jobs, f"nawala ang job na {jid}"
        assert jobs[jid], f"{jid} ay walang next_run_time"


def test_the_convention_is_actually_the_majority():
    """Ang argumento ay 'sundin ang sariling kombensiyon ng codebase' -- pinipin
    ito rito para hindi ito maging alamat."""
    jobs = _interval_jobs()
    explicit = sum(1 for _j, _m, e in jobs if e)
    assert explicit / len(jobs) > 0.75, (
        f"{explicit}/{len(jobs)} lamang ang nagtatakda ng unang takbo -- "
        "kung bumaba ito ay hindi na kombensiyon ang tawag dito"
    )
