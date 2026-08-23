"""Duration meter para sa equity viability refresh job (2026-08-23).

Ang job na ito ang may dalang PREMARKET GAP SCAN, at ang premarket lang ang
kumikitang sesyon ng lane. Ang body nito ay dalawang SERIAL per-ticker provider
loop sa universe na may ceiling na 1500, walang wall-clock budget, at
max_instances=1 — kaya ang body na lumalampas sa sariling interval ay TAHIMIK na
nagtutulak sa susunod na fire. Ang totoong premarket selection cadence ay ang
BODY DURATION, at hindi pa ito nasusukat kailanman.

Runnable: pytest tests/test_equity_refresh_duration_meter.py -v
"""
from __future__ import annotations

import inspect

from app.services import trading_scheduler as ts


def test_job_measures_its_own_duration():
    src = inspect.getsource(ts._run_equity_viability_refresh_job)
    assert "_evr_t0 = time.monotonic()" in src
    assert "_evr_dur = time.monotonic() - _evr_t0" in src


def test_log_carries_duration_scanned_and_interval():
    src = inspect.getsource(ts._run_equity_viability_refresh_job)
    assert "dur_s=%.1f" in src
    assert "scanned=%d" in src
    assert "interval_s=%d" in src


def test_overrun_is_called_out_explicitly():
    """Ang tahimik na overrun ang mismong depekto — dapat hindi ito tahimik."""
    src = inspect.getsource(ts._run_equity_viability_refresh_job)
    assert "OVERRUN" in src
    assert "_evr_dur > _evr_interval" in src


def test_timer_starts_before_the_universe_build():
    """Ang universe build ay bahagi ng gastos — dapat kasama sa sukat."""
    src = inspect.getsource(ts._run_equity_viability_refresh_job)
    assert src.index("_evr_t0 = time.monotonic()") < src.index(
        "build_equity_universe(EQUITY_ROSS_SMALLCAP)"
    )


def test_scan_list_is_alphabetical_so_a_budget_would_bias():
    """Ang bitag na nakadokumento sa job: kung may deadline na puputol sa loop,
    ang parehong dulo ng alpabeto ang mawawala sa BAWAT cycle."""
    merge_src = inspect.getsource(ts._merge_equity_refresh_universe)
    assert "sorted(" in merge_src
    job_src = inspect.getsource(ts._run_equity_viability_refresh_job)
    assert "ALPHABETICAL" in job_src
    assert "blind spot" in job_src


def test_meter_is_observability_only():
    """Walang binagong gawi — ang timer ay binabasa LANG sa log line.

    Ang `_evr_dur` ay dapat lumitaw nang eksaktong dalawang beses: kung saan ito
    kinukuwenta, at kung saan ito ini-log/inihahambing. Walang control flow na
    nakasandal dito maliban sa pagpili ng suffix ng log message.
    """
    src = inspect.getsource(ts._run_equity_viability_refresh_job)

    def _code_only(text: str) -> str:
        """Itapon ang mga komento — ang salitang 'break' ay lumilitaw sa prosa."""
        out = []
        for line in text.splitlines():
            stripped = line.split("#", 1)[0]
            if stripped.strip():
                out.append(stripped)
        return "\n".join(out)

    # walang deadline na pumuputol sa scan (ang bitag na alphabetical)
    scan_code = _code_only(
        src.split("_evr_t0")[1].split("write_db = SessionLocal()")[0]
    )
    assert "break" not in scan_code
    assert "continue" not in scan_code
    # ang duration ay ginagamit LANG sa log line: assign + log arg + overrun compare
    assert _code_only(src).count("_evr_dur") == 3
