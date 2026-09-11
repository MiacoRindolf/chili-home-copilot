"""THE TAPE WINDOW IS NOT A CLOCK — and the print branch may not hide one either.

[29], 2026-09-10. ``signed_tape_accel_features`` has had a print-indexed branch
(``window_prints``) since #1370. Until #1376 it had ZERO callers; by tonight it had
three (the re-entry ramp, the accel-reversal exit, replay_v2) while the whole ENTRY
side — ``_l2_entry_confirm``, ``tape_confirms_hold`` (12 pattern triggers plus the
momentum continuation), the explosive raw-break escape — and the ARM side
(``auto_arm._tape_cold``) still fell through to the SECONDS default,
``chili_momentum_l2_confirm_window_s`` = 15.0.

WHAT THAT COSTS, MEASURED (7 days to 2026-09-10, the real helper run at live
decision instants: 63 readable entry fills, 69 exit fills):

    15-s window vs last-255-print window
      signed_tape_accel SIGN flips ......... 26/63 entries, 29/69 exits
      buy_share_delta   SIGN flips ......... 22/63 entries, 32/69 exits
        -> _l2_entry_confirm verdict flips 22/63 (35%)
        -> tape_confirms_hold verdict flips 26/63 (41%)

and INSIDE the print branch two clocks were still running:

    time-split vs COUNT-split of the SAME 255 prints
      signed_tape_accel SIGN flips ......... 17/63 entries (27%)
    halt-gap trim ``window_s / 2`` = 7.5 SECONDS inside a print window
      fired on ........................... 7/63 print windows (n_ticks p10 178)

So the window length, not the tape, was deciding. These tests pin the repair:

  * the PRINT form is the DEFAULT; a seconds window survives only as a NAMED
    fallback a caller asks for explicitly, and says so on the receipt;
  * the print form splits its halves by COUNT (equal populations);
  * the print form trims discontinuities at a DERIVED bound
    ``max(chili_momentum_g4_reentry_max_print_age_seconds, the window's own
    pre-trim gap p99)`` — both measured numbers, no 7.5-s literal;
  * every ``signed_tape_accel_features`` call site in ``app/`` is print-indexed;
  * ``auto_arm._tape_cold`` carries the print-AGE bound, because 255 prints at a
    pre-market arm can span an hour and a dead tape must not read as a live cold one.

DB-free: a tiny fake session returns canned rows and records the SQL.
"""
from __future__ import annotations

import ast
import contextlib
from datetime import datetime
from pathlib import Path

import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural.entry_gates import _signed_tape_features

_REPO = Path(__file__).resolve().parents[1]
_APP = _REPO / "app"


# ── helpers ──────────────────────────────────────────────────────────────────────


class _FakeDB:
    """Records every statement + params and replays canned tape rows."""

    def __init__(self, rows):
        self.rows = rows
        self.statements: list[tuple[str, dict]] = []

    def execute(self, stmt, params=None):
        self.statements.append((str(stmt), dict(params or {})))
        rows = self.rows

        class _R:
            def fetchall(self_inner):
                return rows

            def fetchone(self_inner):
                return rows[0] if rows else None

        return _R()

    def begin_nested(self):
        return contextlib.nullcontext()


def _optional_passthrough(monkeypatch):
    from app.services.trading.momentum_neural import optional_db_read as ODR

    monkeypatch.setattr(
        ODR, "optional_fetchall",
        lambda db, stmt, params=None: db.execute(stmt, params).fetchall(),
    )


def _lift(px, ts, size=100.0):
    """One print that LIFTS the ask (aggressor buy) at ``ts`` epoch seconds."""
    return (px, size, px - 0.01, px, ts)


def _hit(px, ts, size=100.0):
    """One print that HITS the bid (aggressor sell)."""
    return (px, size, px, px + 0.01, ts)


# ── 1. every app caller is print-indexed ─────────────────────────────────────────


def _call_sites():
    """Every ``signed_tape_accel_features(...)`` call in ``app/`` with its keywords."""
    sites = []
    for path in sorted(_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (
                fn.id if isinstance(fn, ast.Name)
                else fn.attr if isinstance(fn, ast.Attribute)
                else None
            )
            if name != "signed_tape_accel_features":
                continue
            kws = {k.arg for k in node.keywords if k.arg}
            sites.append((path.relative_to(_REPO).as_posix(), node.lineno, kws))
    return sites


# The ONLY callers permitted to hand the helper a SECONDS window. After [29] this
# list is EMPTY: a seconds window inside app/ is, by definition, a clock deciding a
# tape question. Adding a name here is a design decision that needs its own receipt.
_SECONDS_ALLOWLIST: set[str] = set()


def test_every_app_caller_of_signed_tape_accel_features_is_print_indexed():
    sites = _call_sites()
    assert sites, "the AST walk found no call sites at all — the pin is not watching"
    seconds = [
        f"{f}:{ln}" for f, ln, kws in sites
        if "window_s" in kws and f not in _SECONDS_ALLOWLIST
    ]
    assert not seconds, (
        "these call sites still hand the tape helper a CLOCK: "
        f"{seconds}. Fifteen seconds is ~900 prints on a fast name and four on a "
        "slow one; the verdict flipped on 22 of 63 live entries between the two forms."
    )
    silent = [f"{f}:{ln}" for f, ln, kws in sites if "window_prints" not in kws]
    assert not silent, (
        "these call sites do not NAME the window they read "
        f"({silent}); the print count is a binding value and belongs on the receipt."
    )


def test_the_two_window_prints_settings_are_pinned_equal():
    """ONE derived N. The re-entry ramp keeps its own override name (#1376 shipped
    it and its tests pin it), but the two must never silently disagree."""
    assert int(settings.chili_momentum_tape_window_prints) == int(
        settings.chili_momentum_g4_reentry_tape_window_prints
    ), (
        "the shared tape window and the re-entry ramp's override have diverged; "
        "the derivation (p50 of the print count inside the legacy 15-s window at "
        "live decision instants) produced ONE number for both."
    )


# ── 2. the print form is the default; seconds is a NAMED fallback ────────────────


def test_print_form_is_the_default(monkeypatch):
    """No window argument at all ⇒ the LIMIT-N read, N from the shared setting."""
    _optional_passthrough(monkeypatch)
    ts0 = 1_800_000_000.0
    rows = [_lift(1.0 + i * 0.01, ts0 + i * 0.1) for i in range(20)]
    db = _FakeDB(rows)
    out = EG.signed_tape_accel_features(
        "SKYQ", db=db, as_of=datetime(2026, 9, 10, 18, 0, 0)
    )
    assert out is not None
    sql, params = db.statements[0]
    assert "LIMIT :n" in sql, "the DEFAULT read is still a clock"
    assert "make_interval" not in sql, "a print window is not a clock"
    assert params["n"] == int(settings.chili_momentum_tape_window_prints)
    assert out["window_kind"] == "prints"
    assert out["window_prints"] == int(settings.chili_momentum_tape_window_prints)
    assert out["window_s"] is None
    assert out["split"] == "count"
    assert out["span_s"] == pytest.approx(1.9, abs=1e-6)


def test_seconds_form_is_a_named_fallback_with_a_receipt(monkeypatch):
    """A caller may still ask for seconds — but the receipt SAYS so, and the halves
    are split the old way. Nothing silent survives."""
    _optional_passthrough(monkeypatch)
    ts0 = 1_800_000_000.0
    rows = [_lift(1.0 + i * 0.01, ts0 + i * 0.1) for i in range(20)]
    db = _FakeDB(rows)
    out = EG.signed_tape_accel_features("SKYQ", db=db, window_s=15.0)
    assert out is not None
    sql, params = db.statements[0]
    assert "make_interval" in sql and params["w"] == 15.0
    assert out["window_kind"] == "seconds"
    assert out["window_s"] == 15.0
    assert out["window_prints"] is None
    assert out["split"] == "time"
    assert out["gap_trim_basis"] == "window_s_half"
    assert out["gap_trim_s"] == pytest.approx(7.5)


# ── 3. the halves hold equal populations in the print form ───────────────────────


def test_count_split_halves_hold_equal_populations():
    """THE HIDDEN CLOCK INSIDE THE PRINT BRANCH.

    250 prints inside one second, then 5 prints spread over the next minute — a
    burst that went quiet, which is most of what our names do. Every print is an
    ask-lift of the same size, so the tape is unambiguously one-sided buying.

      time split  : midpoint at ~30 s puts 252 prints in the front half and 3 in
                    the back ⇒ accel = back_buy − front_buy is hugely NEGATIVE,
                    i.e. "the buying collapsed", purely from where the clock cut.
      count split : 127 vs 128 prints ⇒ accel = +1 print of size ⇒ POSITIVE.

    Same tape, same instant, opposite sign. Measured live: 17 of 63 entry instants
    flip the accel sign between the two splits of the SAME 255 prints.
    """
    t0 = 1_800_000_000.0
    rows = [_lift(1.00 + i * 0.0001, t0 + i * 0.004) for i in range(250)]
    rows += [_lift(1.03, t0 + 1.0 + (i + 1) * 12.0) for i in range(5)]
    assert len(rows) == 255

    by_time = _signed_tape_features(
        rows, window_s=120.0, tick_rate_floor_pctile=0.0,
        split="time", gap_trim_s=14.69,
    )
    by_count = _signed_tape_features(
        rows, window_s=120.0, tick_rate_floor_pctile=0.0,
        split="count", gap_trim_s=14.69,
    )
    assert by_time is not None and by_count is not None
    assert by_time["n_ticks"] == by_count["n_ticks"] == 255, "neither may be trimmed"
    assert by_time["signed_tape_accel"] < 0.0, "the clock cut says the buying died"
    # 255 prints, all buys of 100 shares: count split is 127 front / 128 back.
    assert by_count["signed_tape_accel"] == pytest.approx(100.0)
    assert by_count["split"] == "count" and by_time["split"] == "time"


def test_time_split_stays_the_default_so_untouched_callers_are_unchanged():
    """The byte-identity pin: the legacy behaviour is what you get when you ask for
    nothing. ``first_dip_tape_policy`` (a sealed, sha256'd schema) still calls the
    pure helper this way and must read exactly what it read yesterday."""
    rows = [_lift(1.0 + i * 0.01, 1_800_000_000.0 + i) for i in range(10)]
    plain = _signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=0.0)
    explicit = _signed_tape_features(
        rows, window_s=15.0, tick_rate_floor_pctile=0.0, split="time", gap_trim_s=None
    )
    assert plain is not None and explicit is not None
    assert plain == explicit
    assert plain["split"] == "time"
    assert plain["gap_trim_basis"] == "window_s_half"


# ── 4. the discontinuity bound is derived, not 7.5 seconds ───────────────────────


def _tape_with_hole(hole_s: float, n: int = 200):
    """``n`` prints on a 0.1-s cadence with ONE internal hole of ``hole_s``."""
    t = 1_800_000_000.0
    rows = []
    for i in range(n):
        rows.append(_lift(1.0 + i * 0.001, t))
        t += hole_s if i == n // 2 else 0.1
    return rows


def test_print_window_gap_trim_is_the_derived_age_bound_not_7_5_s():
    """A TEN-SECOND hole is normal cadence on a slow name and a halt on a fast one.
    The old literal (``window_s / 2`` = 7.5 s at the shipped 15-s window) called it a
    halt unconditionally and threw away the front half; it fired on 7 of 63 live
    255-print windows. The bound is now the measured print-age floor (14.69 s = p99
    of 96,360 inter-print gaps over the 8 names we traded on 2026-09-10) raised to
    the window's OWN pre-trim gap p99 when that is larger."""
    ten = _signed_tape_features(
        _tape_with_hole(10.0), window_s=15.0, tick_rate_floor_pctile=0.0,
        split="count", gap_trim_s=14.69,
    )
    assert ten is not None
    assert ten["gap_restricted"] is False, "10 s is inside the measured cadence"
    assert ten["n_ticks"] == 200
    assert ten["gap_trim_s"] == pytest.approx(14.69)
    assert ten["gap_trim_basis"] == "max(print_age_floor,window_gap_p99)"

    twenty = _signed_tape_features(
        _tape_with_hole(20.0), window_s=15.0, tick_rate_floor_pctile=0.0,
        split="count", gap_trim_s=14.69,
    )
    assert twenty is not None
    assert twenty["gap_restricted"] is True, "20 s is past the measured bound"
    assert twenty["n_ticks"] < 200

    # …and the SAME ten-second hole IS a halt under the old seconds literal, which
    # is exactly the defect: the unit decided, not the tape.
    legacy = _signed_tape_features(
        _tape_with_hole(10.0), window_s=15.0, tick_rate_floor_pctile=0.0
    )
    assert legacy is not None and legacy["gap_restricted"] is True


def test_a_slow_name_carries_its_own_scale():
    """A name that prints once a minute is not halted. Its own pre-trim gap p99
    raises the bound above the floor, so the window is measured, not shredded."""
    t = 1_800_000_000.0
    rows = [_lift(1.0 + i * 0.01, t + i * 60.0) for i in range(30)]
    out = _signed_tape_features(
        rows, window_s=15.0, tick_rate_floor_pctile=0.0,
        split="count", gap_trim_s=14.69,
    )
    assert out is not None
    assert out["gap_trim_s"] == pytest.approx(60.0), "the bound came from the tape"
    assert out["gap_restricted"] is False
    assert out["n_ticks"] == 30


# ── 5. the confirmers read prints, and say so ────────────────────────────────────


def test_l2_entry_confirm_reads_prints_and_names_the_window(monkeypatch):
    _optional_passthrough(monkeypatch)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(settings, "chili_momentum_l2_confirm_enabled", True)
    t0 = 1_800_000_000.0
    rows = [_hit(10.00, t0), _hit(10.01, t0 + 3),
            _lift(10.02, t0 + 6, 600), _lift(10.03, t0 + 9, 600)]
    db = _FakeDB(rows)
    decision, dbg = EG._l2_entry_confirm("ABCD", db=db, settings=settings)
    assert decision == "confirm" and dbg["reason"] == "l2_confirm_tape_thrust"
    sql, params = db.statements[0]
    assert "LIMIT :n" in sql and "make_interval" not in sql
    assert params["n"] == int(settings.chili_momentum_tape_window_prints)
    assert dbg["window_kind"] == "prints"
    assert dbg["window_prints"] == int(settings.chili_momentum_tape_window_prints)
    assert dbg["tape_split"] == "count"
    assert dbg["n_ticks"] == 4
    assert dbg["span_s"] == pytest.approx(9.0)
    assert dbg["gap_trim_s"] == pytest.approx(14.69)


def test_tape_confirms_hold_reads_prints(monkeypatch):
    """The 12 pattern triggers + momentum continuation. The fail-CLOSED contract on
    a missing tape is untouched — only the unit of the window changed."""
    _optional_passthrough(monkeypatch)
    t0 = 1_800_000_000.0
    rows = [_hit(10.00, t0), _hit(10.01, t0 + 1),
            _lift(10.02, t0 + 2, 900), _lift(10.03, t0 + 3, 900)]
    db = _FakeDB(rows)
    ok, dbg = EG.tape_confirms_hold("ABCD", db=db, settings=settings)
    assert ok is True and dbg["reason"] == "tape_hold_confirmed"
    sql, params = db.statements[0]
    assert "LIMIT :n" in sql
    assert params["n"] == int(settings.chili_momentum_tape_window_prints)
    assert dbg["window_kind"] == "prints" and dbg["tape_split"] == "count"
    assert dbg["window_prints"] == int(settings.chili_momentum_tape_window_prints)

    empty = _FakeDB([])
    ok2, dbg2 = EG.tape_confirms_hold("ABCD", db=empty, settings=settings)
    assert ok2 is False and dbg2["reason"] == "tape_hold_no_data"


# ── 6. the arm-time read: prints AND a freshness bound ───────────────────────────


def test_auto_arm_tape_cold_reads_prints_and_respects_print_age(monkeypatch):
    """MEASURED: over 7 days, 731 of 1,549 live equity arms had FEWER THAN THREE
    prints inside the 15-s window (p50 = 3), so the helper returned None and this
    gate fail-opened without ever reading a tape — decision-inert. The print form
    always has 255 prints to read, which is why it needs the age bound: at a
    pre-market arm those 255 prints can span an hour, and an hour-old tape must not
    be scored as a live cold one in EITHER direction."""
    from app.services.trading.momentum_neural import auto_arm as AA

    _optional_passthrough(monkeypatch)
    now = datetime(2026, 9, 10, 18, 0, 0)
    monkeypatch.setattr(AA, "_utcnow", lambda: now)
    now_epoch = (now - datetime(1970, 1, 1)).total_seconds()

    # A fading tape (sells into the back half) that is FRESH -> genuinely cold.
    fresh = [_lift(10.00, now_epoch - 6, 900), _lift(10.01, now_epoch - 5, 900),
             _hit(10.00, now_epoch - 2, 100), _hit(9.99, now_epoch - 1, 100)]
    cold, rc = AA._tape_cold_probe("ABCD", db=_FakeDB(fresh))
    assert cold is True and rc["reason"] == "tape_cold"
    assert rc["window_kind"] == "prints"
    assert rc["window_prints"] == int(settings.chili_momentum_tape_window_prints)

    # The SAME tape shape, ten minutes old -> unreadable as "now", so fail-OPEN.
    stale = [(px, sz, b, a, ts - 600.0) for (px, sz, b, a, ts) in fresh]
    cold2, rc2 = AA._tape_cold_probe("ABCD", db=_FakeDB(stale))
    assert cold2 is False, "a ten-minute-dead tape may not be scored cold"
    assert rc2["reason"] == "tape_source_stale"
    assert rc2["print_age_s"] > rc2["print_age_bound_s"]
    assert rc2["print_age_bound_s"] == pytest.approx(
        float(settings.chili_momentum_g4_reentry_max_print_age_seconds)
    )

    # Crypto and empty tape keep their fail-open contract.
    assert AA._tape_cold_probe("BTC-USD", db=_FakeDB(fresh))[0] is False
    assert AA._tape_cold_probe("ABCD", db=_FakeDB([]))[0] is False

    # …and the bool wrapper the three call sites bind to still delegates.
    monkeypatch.setattr(AA, "_tape_cold_probe",
                        lambda sym, db=None: (True, {"reason": "tape_cold"}))
    assert AA._tape_cold("ABCD") is True


# ── 7. the activity floor now ranks the value it is compared against ─────────────


def test_the_print_form_ranks_tick_rate_against_its_own_population():
    """The floor's own comment claims ``tick_rate`` IS the last rolling m-print
    window, so the comparison is like-for-like. It was not: ``tick_rate`` was
    ``m / dt`` while every sample in the distribution was ``(m-1) / dt`` — the
    compared value sat ~m/(m-1) ABOVE its own sample, so the leg leaned open by
    construction. In the count split it is now exactly a member: on an even-cadence
    tape at percentile 1.0 (the strictest reading available) the two are equal."""
    rows = [_lift(1.0 + i * 0.01, 1_800_000_000.0 + i) for i in range(11)]
    by_count = _signed_tape_features(
        rows, window_s=15.0, tick_rate_floor_pctile=1.0,
        split="count", gap_trim_s=14.69,
    )
    assert by_count is not None
    assert by_count["tick_rate_floor_n"] > 2
    assert by_count["tick_rate"] == pytest.approx(by_count["tick_rate_floor"])
    assert by_count["tick_rate"] >= by_count["tick_rate_floor"]

    by_time = _signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=1.0)
    assert by_time is not None
    assert by_time["tick_rate"] > by_time["tick_rate_floor"], (
        "the legacy seconds form keeps its documented (and permissive) behaviour"
    )
