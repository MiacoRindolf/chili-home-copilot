"""Ang lock-busy na tick ay isang linya, hindi buong traceback (2026-08-27).

ANG SINUKAT: sa lock storm ngayong hapon (straggler tick na may hawak na row
lock nang minuto), ~2,000 traceback lines ng CapturedPaperRuntimeUnavailableError
("busy, susunod na tick" semantics) ang nagbaon sa mga TUNAY na failure sa log.

Runnable: pytest tests/test_lock_busy_log_downgrade.py -v
"""
from __future__ import annotations

import ast
import pathlib

from app.services import trading_scheduler as TS

_SRC = pathlib.Path(TS.__file__)


def test_busy_is_one_line_and_real_failures_keep_the_stack():
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("live runner tick busy session=")
    region = src[max(0, i - 1200):i + 600]
    assert "CapturedPaperRuntimeUnavailableError" in region, (
        "ang busy class LANG ang dine-downgrade"
    )
    assert "live runner tick failed session=" in src, (
        "ang tunay na failure ay may buong traceback pa rin"
    )
    i_failed = src.index("live runner tick failed session=")
    region_failed = src[i_failed:i_failed + 200]
    assert "exc_info=True" in region_failed


def test_the_busy_line_has_no_stack():
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("live runner tick busy session=")
    region = src[i:i + 200]
    assert "exc_info" not in region, "ang busy ay walang stack trace"


def test_a_missing_import_fails_open_to_the_full_traceback():
    """⚠️ Kung sakaling mabigo ang import ng busy class, ang lahat ay dapat
    bumagsak sa LUMANG behavior (buong traceback) — hindi tahimik na lunok."""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("live runner tick busy session=")
    region = src[max(0, i - 1200):i]
    assert "except Exception" in region and "_CPBusy = ()" in region
