"""Ang halt na nakita ng PRINT clock ay kinakansela lamang ng PRINT clock (2026-08-24).

ANG BUG, SINUKAT LIVE (DAIC, 2026-08-24). Dalawang magkasunod na linya sa
green-tick path ng ``live_runner`` ay nanonood ng MAGKAIBANG orasan::

    _register_fresh_quote_tick(...)          <- RESUME sa sariwang QUOTE
    _register_print_recency_halt_check(...)  <- HALT sa tahimik na PRINT

Sa tunay na LULD halt ay humihinto ang mga PRINT habang **patuloy** ang mga QUOTE
-- iyon mismo ang dahilan kung bakit idinagdag ang R6 print-recency path. Pero ang
RESUME side ay naiwan sa quote clock, kaya ang unang linya ay naglilinis ng halt
marker na ibinabalik agad ng pangalawa::

    10:59:41  TUNAY NA PRINT      <- huling print bago ang halt
    11:00:49  suspected_halt      <- tama
    11:00:58  halt_resumed        <- MALI, zero print
    11:01:48  halt_resumed        <- MALI
    11:02:58  halt_resumed        <- MALI
    11:05:25  TUNAY NA PRINT      <- ang tunay na resume: WALANG event

Sinukat: 939-1021 na halt event kada araw, at ang ``halt_resume_dip`` -- ang
sanctioned na post-resume entry, ginawa mula sa audited na trades ni Ross
(AUDIT_REPORT_BATCH2: *"halt_resume_dip's stabilization requirement is validated
live (ILLR)"*) -- ay **hindi kailanman naging primary trigger sa 521 candidate /
30 araw**.

Runnable: pytest tests/test_halt_print_resume_symmetry.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import nbbo_tape


SILENT = {"last_print_age_s": 340.0, "recent_print_count": 4300, "median_gap_s": 0.01}
PRINTING = {"last_print_age_s": 0.4, "recent_print_count": 4300, "median_gap_s": 0.01}


@pytest.fixture
def stub_tape(monkeypatch):
    """Palitan ang tape reader; ibinabalik ang huling itinakdang estado."""
    box: dict = {"state": None}

    def _fake(db, symbol, **kwargs):
        return box["state"]

    monkeypatch.setattr(nbbo_tape, "print_recency_state", _fake)
    return box


def _sess(symbol: str = "ZHLT"):
    return SimpleNamespace(id=1, symbol=symbol, state="watching_live")


def _le(source: str = "print_recency"):
    return {
        "suspected_halt_since_utc": lr._utcnow().isoformat(),
        "suspected_halt_source": source,
    }


# ── ang pangunahing invariant ───────────────────────────────────────────────

def test_silent_tape_holds_the_halt_against_a_fresh_quote(stub_tape):
    """ANG BUG: ang sariwang quote ay dating naglilinis nito. Hindi na."""
    stub_tape["state"] = SILENT
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), _le()) is True


def test_returned_tape_releases_the_halt(stub_tape):
    """Kapag bumalik ang mga PRINT, tunay na iyon ang resume."""
    stub_tape["state"] = PRINTING
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), _le()) is False


def test_the_window_matches_the_detect_side_exactly(stub_tape):
    """Simetriko: halt kapag age >= max(floor, median*mult); resume kapag mas mababa.

    Sa default (floor 30s, mult 8.0) at median_gap 10s ang window ay 80s.
    """
    monkey_floor = float(settings.chili_momentum_halt_print_gap_floor_seconds)
    monkey_mult = float(settings.chili_momentum_halt_print_gap_multiple)
    window = max(monkey_floor, 10.0 * monkey_mult)

    stub_tape["state"] = {"last_print_age_s": window - 0.5, "recent_print_count": 99, "median_gap_s": 10.0}
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), _le()) is False

    stub_tape["state"] = {"last_print_age_s": window + 0.5, "recent_print_count": 99, "median_gap_s": 10.0}
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), _le()) is True


def test_no_median_gap_falls_back_to_the_floor(stub_tape):
    floor = float(settings.chili_momentum_halt_print_gap_floor_seconds)
    stub_tape["state"] = {"last_print_age_s": floor + 1.0, "recent_print_count": 9, "median_gap_s": None}
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), _le()) is True
    stub_tape["state"] = {"last_print_age_s": floor - 1.0, "recent_print_count": 9, "median_gap_s": None}
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), _le()) is False


# ── ang quote-detected na halt ay pag-aari pa rin ng quote clock ────────────

def test_a_quote_detected_halt_is_untouched(stub_tape):
    """Ang lumang path ay hindi dapat magbago -- byte-identical."""
    stub_tape["state"] = SILENT
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), _le("stale_quote")) is False


def test_unknown_source_is_untouched(stub_tape):
    stub_tape["state"] = SILENT
    le = _le()
    le.pop("suspected_halt_source")
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), le) is False


# ── FAIL-OPEN sa bawat sulok ───────────────────────────────────────────────

def test_no_tape_state_fails_open(stub_tape):
    stub_tape["state"] = None
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), _le()) is False


def test_missing_last_print_age_fails_open(stub_tape):
    stub_tape["state"] = {"last_print_age_s": None, "recent_print_count": 9, "median_gap_s": 1.0}
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), _le()) is False


def test_an_exploding_tape_reader_fails_open(monkeypatch):
    def _boom(db, symbol, **kwargs):
        raise RuntimeError("tape reader nasira")

    monkeypatch.setattr(nbbo_tape, "print_recency_state", _boom)
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), _le()) is False


def test_crypto_is_untouched(stub_tape):
    """Walang iqfeed_trade_ticks ang crypto -- walang print clock doon."""
    stub_tape["state"] = SILENT
    assert lr._print_detected_halt_tape_still_silent(None, _sess("BTC-USD"), _le()) is False


def test_flag_off_is_byte_identical(stub_tape, monkeypatch):
    stub_tape["state"] = SILENT
    monkeypatch.setattr(
        settings, "chili_momentum_halt_print_resume_symmetry_enabled", False, raising=False
    )
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), _le()) is False


# ── ANG SAFETY CEILING: hindi kailanman ma-trap ang lane ───────────────────

def test_the_hard_ceiling_releases_a_stuck_halt(stub_tape, monkeypatch):
    """Ang sirang tape ay hinding-hindi dapat mag-trap ng lane nang habambuhay."""
    from datetime import timedelta

    stub_tape["state"] = SILENT
    monkeypatch.setattr(
        settings, "chili_momentum_halt_print_resume_max_hold_seconds", 60.0, raising=False
    )
    le = _le()
    le["suspected_halt_since_utc"] = (lr._utcnow() - timedelta(seconds=120)).isoformat()
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), le) is False, (
        "lampas sa ceiling ay dapat bumalik ang quote clock"
    )
    # ...pero sa loob pa ng ceiling ay hawak pa rin.
    le["suspected_halt_since_utc"] = (lr._utcnow() - timedelta(seconds=5)).isoformat()
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), le) is True


def test_an_unparseable_timestamp_does_not_release(stub_tape):
    """Ang basurang timestamp ay hindi dapat maging tahimik na bypass ng gate."""
    stub_tape["state"] = SILENT
    le = _le()
    le["suspected_halt_since_utc"] = "hindi-ito-petsa"
    assert lr._print_detected_halt_tape_still_silent(None, _sess(), le) is True


# ── ang source ay naitatala sa halt onset ──────────────────────────────────

def test_the_detection_source_is_persisted(db, monkeypatch):
    """Kung walang naitalang source ay hindi masasabi ng resume ang kaibahan."""
    from tests.test_momentum_emergency_exit_recovery import _seed_session

    sess = _seed_session(db, symbol="ZSRC", quantity=10.0)
    monkeypatch.setattr(lr, "_emit", lambda *a, **k: None)

    le: dict = {}
    lr._mark_suspected_halt(db, sess, le, None, detail={"source": "print_recency"})
    assert le["suspected_halt_source"] == "print_recency"

    le2: dict = {}
    lr._mark_suspected_halt(db, sess, le2, None, detail={"stale_tick_streak": 3})
    assert le2["suspected_halt_source"] == "stale_quote", (
        "ang quote path ay walang 'source' key — dapat itong ma-default"
    )


def _drive_fresh_quote(monkeypatch, le: dict, symbol: str = "ZHLT"):
    """Patakbuhin ang TOTOONG _register_fresh_quote_tick sa isang stub na sess."""
    emitted: list = []
    monkeypatch.setattr(lr, "_commit_le", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_emit", lambda db, sess, kind, payload=None: emitted.append(kind))
    tick = SimpleNamespace(bid=1.62, mid=1.625, open=1.60)
    lr._register_fresh_quote_tick(None, _sess(symbol), le, tick)
    return emitted


def test_behaviour_a_fresh_quote_does_not_resume_a_silent_print_halt(stub_tape, monkeypatch):
    """ANG TUNAY NA GUARD: patakbuhin ang buong function, hindi lang ang helper.

    Bago ang lunas, ang isang sariwang quote sa panahon ng LULD halt ay
    nagbubura ng marker at nag-e-emit ng halt_resumed. Iyon ang flip-flop.
    """
    stub_tape["state"] = SILENT
    le = _le()
    marker = le["suspected_halt_since_utc"]
    emitted = _drive_fresh_quote(monkeypatch, le)

    assert le.get("suspected_halt_since_utc") == marker, (
        "ang halt marker ay dapat NANATILI — tahimik pa rin ang tape"
    )
    assert "halt_resumed" not in emitted, "walang phantom na halt_resumed"
    assert "halt_resumed_at_utc" not in le, (
        "ang resume timestamp ay hindi dapat na-stamp — ito ang sumusunog sa "
        "600s na halt_resume_dip window habang halted pa"
    )


def test_behaviour_the_returned_tape_does_resume(stub_tape, monkeypatch):
    """Kapag bumalik ang mga PRINT, dapat KUMPLETO ang resume lifecycle."""
    stub_tape["state"] = PRINTING
    le = _le()
    emitted = _drive_fresh_quote(monkeypatch, le)

    assert "suspected_halt_since_utc" not in le, "dapat nalinis na ang marker"
    assert "halt_resumed" in emitted
    assert le.get("halt_resumed_at_utc"), "dapat naka-stamp ang resume window"


def test_behaviour_a_quote_detected_halt_still_resumes_on_a_quote(stub_tape, monkeypatch):
    """Ang lumang quote-clocked na lifecycle ay hindi dapat magbago."""
    stub_tape["state"] = SILENT
    le = _le("stale_quote")
    emitted = _drive_fresh_quote(monkeypatch, le)

    assert "suspected_halt_since_utc" not in le
    assert "halt_resumed" in emitted


def test_the_resume_gate_is_wired_into_the_quote_path():
    """Bantayan ang call site mismo — huwag payagang mawala nang tahimik."""
    import inspect

    src = inspect.getsource(lr._register_fresh_quote_tick)
    assert "_print_detected_halt_tape_still_silent(" in src
    # Ang gate ay dapat NASA LOOB ng suspected-halt branch at BAGO ang chain counter.
    gate = src.index("_print_detected_halt_tape_still_silent(")
    chain = src.index("halt_chain_up_count")
    assert gate < chain, "ang gate ay dapat mauna sa anumang resume side effect"
