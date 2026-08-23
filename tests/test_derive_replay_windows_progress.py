"""Progress output para sa manifest derive (2026-08-23).

Ang census loop ay nagpapatakbo ng dalawang per-(symbol, day) query sa buong
golden archive — ~15 minuto — at WALANG naililimbag hanggang sa dulo. Ang
tahimik na labinlimang minuto ay hindi makilala mula sa hang: napatay na ito ng
operator timeout na naka-set sa halos kaparehong haba ng runtime nito, at
napagkamalang patay nang higit sa isang beses.

Runnable: pytest tests/test_derive_replay_windows_progress.py -v
"""
from __future__ import annotations

import pathlib
import re


def _src() -> str:
    p = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "derive_replay_windows.py"
    return p.read_text(encoding="utf-8", errors="replace")


def test_announces_the_work_before_starting():
    src = _src()
    assert "census:" in src
    assert "pairs to walk" in src
    # sinasabi ang inaasahang haba para hindi ito mapagkamalang hang
    assert re.search(r"10-20 minutes|10-20 minuto", src)


def test_reports_progress_on_a_time_interval():
    src = _src()
    seg = src[src.index("_t0 = time.monotonic()"):]
    seg = seg[:seg.index("if ticks == 0:")]
    assert "_last_report" in seg
    assert ">= 15.0" in seg, "dapat may cadence, hindi kada row (spam)"
    assert "eta=" in seg and "elapsed=" in seg


def test_progress_goes_to_stderr_only():
    """Ang stdout ay ang BASELINE lines na binabasa ng caller — huwag dungisan."""
    src = _src()
    seg = src[src.index("_t0 = time.monotonic()"):]
    seg = seg[:seg.index("if ticks == 0:")]
    prints = re.findall(r"print\(", seg)
    stderrs = re.findall(r"file=sys\.stderr", seg)
    assert len(prints) == len(stderrs) == 2, (
        "bawat progress print ay dapat sa stderr"
    )


def test_final_row_always_reports():
    """Ang huling pares ay dapat laging mag-ulat para malinaw ang pagtatapos."""
    src = _src()
    assert "_done == _total" in src


def test_time_is_imported():
    src = _src()
    assert re.search(r"^import time$", src, re.M)


def test_progress_does_not_change_the_filter_logic():
    """Ang counter ay dapat mauna sa continue-guards para tumpak ang kabuuan."""
    src = _src()
    loop = src[src.index("for sym, day, ticks, nbbo in census:"):]
    loop = loop[:loop.index("cur.execute(HIST_SQL")]
    assert loop.index("_done += 1") < loop.index("if ticks == 0:")
