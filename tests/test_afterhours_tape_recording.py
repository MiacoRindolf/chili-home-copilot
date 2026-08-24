"""Ang pagtatala ng tape ay HINDI execution -- huwag i-gate na parang ganoon.

SINUKAT (2026-08-24, 20:0x UTC = 16:0x ET, after-hours)::

    iqfeed_depth_snapshots     0.4s ang pinakabagong row   <- BUHAY
    iqfeed_trade_ticks         2.5s                        <- BUHAY
    momentum_nbbo_spread_tape  342s                        <- PATAY

DALAWA ang manunulat ng tape, at PAREHO silang naka-tali sa lane:

1. ``tape_ws_recorder`` -- nasa loob ng proseso ng lane (``source='iqfeed_l1'``)
2. ang 1-min sampler (``source='massive_snapshot'``) -- naka-gate sa
   ``include_momentum_exec``, at ang lane ay tumatakbo sa ``momentum_exec_only``

Kapag nagsara ang lane, PAREHO silang namamatay -- at ang tanging natitirang
scheduler container ay tumatakbo sa ``rnd_only``, na hindi nagre-register ng
alinman. Samantala ang sariling gate ng sampler (``is_sample_session_now``) ay
sumasaklaw na sa BUONG 04:00-20:00 ET na data session: *premarket hanggang
afterhours*.

⚠️ PAGWAWASTO. Inangkin ko noong "hindi kailanman naka-register" ang sampler,
batay sa huling 20,000 na row na 100% ``iqfeed_l1``. Mali iyon: sa 174 row/s,
ang 20,000 na row ay mga dalawang minuto lang -- masyadong makitid para makita
ang 1 row/min/symbol. May ``massive_snapshot`` na row ngayong araw.

⚠️ Hindi ito kayang ayusin sa pamamagitan ng pagpapatakbo ng ``momentum_exec_only``
na container: sinubukan iyon noong 2026-08-24 at ang dobleng viability refresh
ay nagpabagal sa trading loop mula 3,984ms tungong **44,858ms**.

Runnable: pytest tests/test_afterhours_tape_recording.py -v
"""
from __future__ import annotations

import inspect

from app.services import trading_scheduler as ts


def _src() -> str:
    return inspect.getsource(ts)


def test_data_recording_is_its_own_gate():
    """Dapat may hiwalay na konsepto -- hindi muling ginagamit ang exec gate."""
    src = _src()
    assert "include_data_recording" in src


def test_rnd_only_records_data():
    """ANG BUONG PUNTO: ang tanging scheduler container ay `rnd_only`."""
    src = _src()
    line = next(
        ln for ln in src.splitlines() if "include_data_recording =" in ln
    )
    assert "rnd_only" in line, f"kailangang kasama ang rnd_only: {line!r}"


def test_data_recording_is_a_SUPERSET_of_exec():
    """Ang bawat role na dating nagtatala ay dapat magtala pa rin -- walang
    naibabawas na saklaw."""
    src = _src()
    line = next(
        ln for ln in src.splitlines() if "include_data_recording =" in ln
    )
    assert "include_momentum_exec" in line, (
        "dapat itong itayo MULA sa exec gate para hindi mawalan ng saklaw ang "
        f"anumang umiiral na role: {line!r}"
    )


def test_the_nbbo_sampler_uses_the_new_gate():
    """Ang mismong job na tahimik na nawawala."""
    src = _src()
    # Hanapin ang REGISTRATION site, hindi ang unang pagbanggit -- lumilitaw din
    # ang setting sa loob mismo ng job function, na mas maaga sa file.
    reg = [
        ln for ln in src.splitlines()
        if "chili_momentum_nbbo_tape_enabled" in ln and "include_" in ln
    ]
    assert len(reg) == 1, f"inaasahan ang isang registration gate, nakuha {reg}"
    assert "include_data_recording" in reg[0], (
        f"ang nbbo sampler ay dapat naka-gate sa data-recording: {reg[0]!r}"
    )


def test_the_sampler_gate_still_honours_its_kill_switch():
    """Huwag alisin ang `chili_momentum_nbbo_tape_enabled` -- iyon ang off switch."""
    src = _src()
    assert (
        'if include_data_recording and getattr(settings, "chili_momentum_nbbo_tape_enabled", True):'
        in src
    )


def test_execution_jobs_did_NOT_move_to_the_data_gate():
    """⚠️ ANG BANTAY SA KALIGTASAN. Ang `rnd_only` ay tahasang ginawa bilang
    "cron_only MINUS" ang exec set para hindi kailanman i-restart ng R&D deploy
    ang prosesong may hawak na buhay na posisyon. Ang pagpapadaan ng anumang
    ORDER-CAPABLE na job sa bagong gate ay magpapawalang-bisa niyon at
    magdadagdag ng producer na hindi nakikita ng time-share census."""
    src = _src()
    # Ang RH agentic keep-warm ay nagpapatuloy sa isang broker auth cache --
    # execution-adjacent, kaya dapat manatili sa exec gate.
    lines = src.splitlines()
    idx = [
        n for n, ln in enumerate(lines)
        if "chili_robinhood_agentic_probe_keepwarm_enabled" in ln
    ]
    assert idx, "hindi nahanap ang keep-warm job"
    # ang gate ay ang pinakamalapit na `if ...` sa itaas ng bawat pagbanggit
    for n in idx:
        gate = next(
            (lines[m] for m in range(n, max(-1, n - 6), -1)
             if lines[m].lstrip().startswith("if ")),
            "",
        )
        if "include_" in gate:
            assert "include_momentum_exec" in gate, (
                f"ang keep-warm ng broker auth ay dapat manatili sa exec gate: {gate!r}"
            )
            break
    else:
        raise AssertionError("walang nahanap na include_ gate para sa keep-warm")


def test_the_measured_evidence_is_recorded_in_the_source():
    """Ang susunod na tao ay dapat makita KUNG BAKIT, hindi lang ANO."""
    src = _src()
    i = src.index("include_data_recording =")
    block = src[max(0, i - 2600):i]
    assert "momentum_nbbo_spread_tape" in block
    assert "after-hours" in block or "afterhours" in block
