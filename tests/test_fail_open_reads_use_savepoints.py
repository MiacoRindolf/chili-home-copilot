"""Ang "fail-open" na DB read ay dapat TUNAY na fail-open (2026-08-23).

Ang isang read na tumatakbo sa session ng CALLER at nakabalot sa try/except ay
HINDI fail-open: kapag nabigo ang SQL, inaabort ng PostgreSQL ang BUONG
transaction. Masaya namang nagbabalik ng neutral ang except — pero ang bawat
sumunod na statement sa transaction na iyon ay mamamatay sa
InFailedSqlTransaction. Fail-CLOSED iyon na may pagkalason sa lahat ng
downstream.

Ito ang tahimik na pumatay sa buong golden replay run: ang sink ay walang
momentum_squeeze_regime_daily (hindi tumatakbo doon ang migrations), kaya ang
"fail-open" na squeeze-regime probe ay nag-abort ng transaction ng FSM tick, at
ang run ay bumagsak sa final_state=queued_live na may ZERO events at zero
paliwanag.

Runnable: pytest tests/test_fail_open_reads_use_savepoints.py -v
"""
from __future__ import annotations

import inspect

import pytest

from app.services.trading.momentum_neural import (
    ftd_ingest,
    repeg_wall,
    squeeze_regime,
)


# ── ang pattern ay nasa code ────────────────────────────────────────────────

def _code_only(text: str) -> str:
    """Itapon ang mga komento — binabanggit din nila ang pangalan ng table."""
    out = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0]
        if stripped.strip():
            out.append(stripped)
    return "\n".join(out)


def test_squeeze_regime_read_goes_through_the_helper():
    src = _code_only(inspect.getsource(squeeze_regime.read_squeeze_regime))
    assert "optional_fetchall(" in src
    assert "db.execute(" not in src, "walang hubad na execute sa session ng caller"


def test_ftd_delta_read_goes_through_the_helper():
    src = _code_only(inspect.getsource(ftd_ingest.ftd_squeeze_viability_delta))
    assert "optional_fetchone(" in src
    assert "db.execute(" not in src


def test_repeg_wall_read_goes_through_the_helper():
    """Ang #1096 ay nagbabasa ng iqfeed_depth_snapshots — WALA sa replay sink —
    mula sa loob ng _l2_entry_veto, isang LIVE ENTRY GATE."""
    src = _code_only(inspect.getsource(repeg_wall.read_repeg_wall_state))
    assert src.count("optional_fetchall(") == 2, "parehong read ay dapat dumaan"
    assert "db.execute(" not in src


def test_no_bare_execute_left_in_these_fail_open_readers():
    """Ang buong punto: walang raw db.execute sa session ng caller sa loob ng
    isang 'fail-open' na except. Isang idiom lang: optional_db_read."""
    for fn in (
        squeeze_regime.read_squeeze_regime,
        ftd_ingest.ftd_squeeze_viability_delta,
        repeg_wall.read_repeg_wall_state,
    ):
        src = _code_only(inspect.getsource(fn))
        assert "db.execute(" not in src, fn.__name__
        assert "optional_" in src, fn.__name__


def test_both_still_fail_open_to_a_neutral_value():
    """Ang savepoint ay hindi dapat magpabago ng ibinabalik kapag nabigo."""
    for fn in (squeeze_regime.read_squeeze_regime, ftd_ingest.ftd_squeeze_viability_delta):
        src = inspect.getsource(fn)
        assert "except Exception" in src


# ── ang gawi, laban sa isang pekeng session na sumasabog ────────────────────

class _Nested:
    def __init__(self, log):
        self._log = log

    def __enter__(self):
        self._log.append("savepoint_open")
        return self

    def __exit__(self, exc_type, exc, tb):
        self._log.append("savepoint_rollback" if exc_type else "savepoint_release")
        return False  # huwag lunukin — ang except ng caller ang bahala


class _ExplodingSession:
    """Ginagaya ang sink na walang table: sumasabog ang execute."""

    def __init__(self):
        self.log: list[str] = []

    def begin_nested(self):
        return _Nested(self.log)

    def execute(self, *a, **k):
        self.log.append("execute")
        raise RuntimeError('relation "..." does not exist')


def test_squeeze_regime_opens_a_savepoint_before_the_failing_read():
    db = _ExplodingSession()
    out = squeeze_regime.read_squeeze_regime(db)
    assert out.label == "neutral"
    assert db.log[0] == "savepoint_open"
    assert "execute" in db.log
    assert db.log[-1] == "savepoint_rollback", db.log


def test_ftd_delta_opens_a_savepoint_before_the_failing_read():
    db = _ExplodingSession()
    out = ftd_ingest.ftd_squeeze_viability_delta(
        "HUIZ", float_shares=1_000_000.0, db=db
    )
    assert out == 0.0
    assert db.log[0] == "savepoint_open"
    assert db.log[-1] == "savepoint_rollback", db.log


def test_ftd_delta_short_circuits_before_touching_the_db():
    """Ang mga maagang guard ay hindi dapat magbukas ng savepoint."""
    db = _ExplodingSession()
    assert ftd_ingest.ftd_squeeze_viability_delta("BTC-USD", float_shares=1.0, db=db) == 0.0
    assert ftd_ingest.ftd_squeeze_viability_delta("HUIZ", float_shares=None, db=db) == 0.0
    assert ftd_ingest.ftd_squeeze_viability_delta("HUIZ", float_shares=0.0, db=db) == 0.0
    assert db.log == [], "walang DB na dapat hawakan sa mga guard na ito"


def test_squeeze_regime_returns_neutral_when_db_is_none():
    out = squeeze_regime.read_squeeze_regime(None)
    assert out.label == "neutral"


# ── ang tunay na invariant, laban sa TOTOONG Postgres ──────────────────────

@pytest.mark.usefixtures("db")
def test_caller_transaction_survives_a_missing_table_read(db):
    """Ang buong punto: pagkatapos ng nabigong probe, GAMIT PA RIN ang session.

    Ito ang mismong sukat na naiiba sa lumang code — doon, ang susunod na
    SELECT 1 ay bumabagsak sa InFailedSqlTransaction.
    """
    from sqlalchemy import text

    db.execute(text("SELECT 1")).fetchone()

    # Isang table na tiyak na wala — ginagaya ang sink na walang migrations.
    try:
        with db.begin_nested():
            db.execute(text("SELECT 1 FROM __table_na_wala_talaga__")).fetchall()
    except Exception:
        pass

    assert db.execute(text("SELECT 1")).fetchone()[0] == 1
