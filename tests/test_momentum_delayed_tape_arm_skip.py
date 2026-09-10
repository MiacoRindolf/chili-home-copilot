"""[38-B] DELAYED-TAPE ARM SKIP (2026-09-10): never arm a LIVE entry on a tape we cannot see.

NASUKAT: 37 NYSE / NYSE American / NYSE Arca na simbolo ang dumarating sa 15-MINUTONG delayed na
IQFeed entitlement -- bawat hilera available_at - observed_at >= 899.9 s (p50 900.6), 31.7% ng
pinakabagong 150k hilera; TPET 0 real-time / 37,937 delayed. Pinasok ang TPET nang dalawang beses
sa tape na iyon (13:07:56 at 13:22:35) dahil ang _tape_cold ay bumabasa ng 15-s window na
nagtatapos sa NGAYON -- walang laman sa 900-s na lumang tape -- at ang walang laman ay fail-open
bilang HOT.

Real-time cluster: p50 0.29 s, p99.9 1.28 s, pinakamasamang single-row bridge stall 334.9 s (CYCU).
Sa pagitan ng 334.9 at 899.9 ay WALANG hilera. Ang threshold ay nasa bandang iyon; ang probe ay
MEDIAN ng pinakabagong N hilera para walang bisa ang iisang stall.

Runnable: pytest tests/test_momentum_delayed_tape_arm_skip.py -v
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timezone

import pytest

import app.services.trading.momentum_neural.auto_arm as aa
from app.config import Settings, settings

AS_OF = datetime(2026, 9, 10, 13, 7, 56, tzinfo=timezone.utc)

# The measured numbers the derivation rests on.
WORST_SINGLE_ROW_STALL_S = 334.9      # CYCU, Nasdaq, bridge stall -- NOT an entitlement delay
DELAYED_FLOOR_S = 899.9               # 15 min exactly; every row of the NYSE-family cohort
TPET_MEDIAN_S = 900.293               # TPET newest 2,000 rows, p50


class _Result:
    def __init__(self, scalar):
        self._scalar = scalar

    def scalar(self):
        return self._scalar


class _Session:
    """Short-lived read session double: records the SQL + params, returns one scalar."""

    def __init__(self, scalar=None, raise_on_execute: Exception | None = None):
        self._scalar = scalar
        self._raise = raise_on_execute
        self.sql = None
        self.params = None
        self.closed = False
        self.executes = 0

    def execute(self, stmt, params=None):
        self.executes += 1
        self.sql = getattr(stmt, "text", str(stmt))
        self.params = params
        if self._raise is not None:
            raise self._raise
        return _Result(self._scalar)

    def close(self):
        self.closed = True


def _bind(monkeypatch, session: _Session):
    """Route the helper's SessionLocal() to our double."""
    import app.db as dbmod

    monkeypatch.setattr(dbmod, "SessionLocal", lambda: session, raising=True)
    return session


# ── the knobs are derived, and the threshold sits inside the measured empty band ─────


def test_the_three_knobs_exist_with_their_derivation_written_down():
    for name in (
        "chili_momentum_delayed_tape_arm_skip_enabled",
        "chili_momentum_delayed_tape_arm_skip_median_delay_s",
        "chili_momentum_delayed_tape_arm_skip_tail_rows",
    ):
        f = Settings.model_fields[name]
        assert f.description, name
    for name in (
        "chili_momentum_delayed_tape_arm_skip_median_delay_s",
        "chili_momentum_delayed_tape_arm_skip_tail_rows",
    ):
        assert "HINANGO" in Settings.model_fields[name].description, name


def test_the_threshold_is_inside_the_measured_empty_band():
    v = float(settings.chili_momentum_delayed_tape_arm_skip_median_delay_s)
    assert WORST_SINGLE_ROW_STALL_S < v < DELAYED_FLOOR_S, v
    # and a stall of the worst measured size cannot reach it even doubled by a burst
    assert v >= 1.5 * WORST_SINGLE_ROW_STALL_S
    # the field bounds must not admit a value at or beyond the delayed floor
    fld = Settings.model_fields["chili_momentum_delayed_tape_arm_skip_median_delay_s"]
    le = next(m.le for m in fld.metadata if hasattr(m, "le"))
    assert le < DELAYED_FLOOR_S


def test_the_tail_makes_a_single_stall_row_irrelevant():
    n = int(settings.chili_momentum_delayed_tape_arm_skip_tail_rows)
    assert n >= 5   # median of >= 5 ignores any one row


# ── the probe: symbol-scoped, LIMIT-bounded, as-of bounded, tie-stable ───────────────


def test_the_probe_sql_is_symbol_scoped_bounded_and_as_of_bounded():
    sql = " ".join(aa._delayed_tape_probe_sql().split())
    assert "WHERE symbol = :s" in sql, "walang symbol filter = ang 73 GB scan"
    assert "observed_at <= :as_of" in sql, "dapat as-of bounded (replay look-ahead)"
    assert "LIMIT :n" in sql, "dapat LIMIT-bounded"
    assert "available_at IS NOT NULL" in sql, "walang arrival clock ang hindi pa nare-release"
    assert "AT TIME ZONE 'UTC'" in sql, "TIMESTAMPTZ - naive UTC ay kailangan ng conversion"
    assert "ORDER BY observed_at DESC, id DESC" in sql, "tie-stable order (id tie-break)"
    assert "percentile_cont(0.5)" in sql, "MEDIAN, hindi max o newest-row"


def test_a_delayed_tape_is_skipped_with_its_measured_median(monkeypatch):
    s = _bind(monkeypatch, _Session(scalar=TPET_MEDIAN_S))
    delayed, med = aa._tape_delayed("TPET", as_of=AS_OF)
    assert delayed is True
    assert abs(med - TPET_MEDIAN_S) < 1e-6
    assert s.closed, "ang short-lived session ay dapat laging isinasara"
    assert s.params["s"] == "TPET"
    assert s.params["n"] == int(settings.chili_momentum_delayed_tape_arm_skip_tail_rows)
    # as_of is threaded through NAIVE (the column is naive UTC)
    assert s.params["as_of"].tzinfo is None
    assert s.params["as_of"] == AS_OF.replace(tzinfo=None)


def test_a_real_time_tape_is_not_skipped(monkeypatch):
    _bind(monkeypatch, _Session(scalar=0.289))
    delayed, med = aa._tape_delayed("TNON", as_of=AS_OF)
    assert delayed is False and abs(med - 0.289) < 1e-9


def test_a_worst_case_bridge_stall_is_not_skipped(monkeypatch):
    """Ang 334.9 s (CYCU) ay stall ng bridge, hindi entitlement -- hindi dapat tumanggi."""
    _bind(monkeypatch, _Session(scalar=WORST_SINGLE_ROW_STALL_S))
    delayed, _ = aa._tape_delayed("CYCU", as_of=AS_OF)
    assert delayed is False


# ── FAIL-OPEN in every direction ──────────────────────────────────────────────────


def test_no_rows_fails_open(monkeypatch):
    s = _bind(monkeypatch, _Session(scalar=None))
    assert aa._tape_delayed("NEWNAME", as_of=AS_OF) == (False, None)
    assert s.closed


def test_a_probe_error_fails_open_and_still_closes(monkeypatch):
    s = _bind(monkeypatch, _Session(raise_on_execute=RuntimeError("relation does not exist")))
    assert aa._tape_delayed("TPET", as_of=AS_OF) == (False, None)
    assert s.closed


def test_crypto_and_empty_symbols_never_touch_the_db(monkeypatch):
    s = _bind(monkeypatch, _Session(scalar=TPET_MEDIAN_S))
    assert aa._tape_delayed("BTC-USD", as_of=AS_OF) == (False, None)
    assert aa._tape_delayed("", as_of=AS_OF) == (False, None)
    assert aa._tape_delayed(None, as_of=AS_OF) == (False, None)
    assert s.executes == 0


def test_the_flag_off_never_touches_the_db(monkeypatch):
    s = _bind(monkeypatch, _Session(scalar=TPET_MEDIAN_S))
    monkeypatch.setattr(settings, "chili_momentum_delayed_tape_arm_skip_enabled", False, raising=False)
    assert aa._tape_delayed("TPET", as_of=AS_OF) == (False, None)
    assert s.executes == 0


def test_as_of_has_no_default():
    """A wall-clock default here would be a replay look-ahead (HARNESS GATE 15 class)."""
    import inspect

    p = inspect.signature(aa._tape_delayed).parameters["as_of"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        aa._tape_delayed("TPET")  # type: ignore[call-arg]


# ── the seam: wired at the arm loop, after the asset-type skip, before market-open ───


def _run_auto_arm_pass_source() -> str:
    src = pathlib.Path(aa.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run_auto_arm_pass":
            return ast.get_source_segment(src, node)
    raise AssertionError("run_auto_arm_pass not found")


def test_the_guard_is_wired_between_asset_type_skip_and_market_open():
    body = _run_auto_arm_pass_source()
    i_asset = body.index("_asset_type_blocks_arm(c.symbol)")
    i_guard = body.index("_tape_delayed(c.symbol, as_of=pass_as_of)")
    i_open = body.index("_symbol_market_open(c.symbol)")
    assert i_asset < i_guard < i_open, "order: asset-type skip -> delayed-tape skip -> market-open"
    assert 'out["delayed_tape_skipped"] = out.get("delayed_tape_skipped", 0) + 1' in body


def test_the_counter_is_initialised_so_the_receipt_shows_the_guard_exists():
    body = _run_auto_arm_pass_source()
    assert 'out["delayed_tape_skipped"] = 0' in body, (
        "0 sa resibo = umiiral ang guard at hindi pumutok; ang nawawalang key = walang guard")


def test_the_guard_uses_the_pass_decision_clock_not_a_wall_clock():
    body = _run_auto_arm_pass_source()
    i_guard = body.index("_tape_delayed(c.symbol, as_of=pass_as_of)")
    window = body[max(0, i_guard - 400): i_guard + 200]
    assert "datetime.now(" not in window and "utcnow(" not in window
