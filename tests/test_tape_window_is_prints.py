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
  * the new entry print contract uses count halves and an empirical p90 scale;
    ramp/exit explicitly retain legacy geometry pending their own calibration;
  * every ``signed_tape_accel_features`` call site in ``app/`` is print-indexed;
  * ``auto_arm._tape_cold`` carries the print-AGE bound, because 255 prints at a
    pre-market arm can span an hour and a dead tape must not read as a live cold one.

DB-free: a tiny fake session returns canned rows and records the SQL.
"""
from __future__ import annotations

import ast
import contextlib
from functools import lru_cache
from datetime import datetime
from pathlib import Path

import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural.entry_gates import _signed_tape_features

_REPO = Path(__file__).resolve().parents[1]
_APP = _REPO / "app"
_SCRIPTS = _REPO / "scripts"


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


@lru_cache(maxsize=1)
def _call_sites():
    """Every ``signed_tape_accel_features(...)`` call in ``app/`` and ``scripts/``.

    ALIASES ARE RESOLVED ([29] review fix, 2026-09-11). The first cut of this pin
    matched only ``ast.Name.id`` / ``ast.Attribute.attr == "signed_tape_accel_features"``
    — and FOUR of the live call sites import the helper under another name
    (``from .entry_gates import signed_tape_accel_features as _g4e_tape_fn`` at
    live_runner:31555 and three more), so the pin could not see the re-entry ramp's
    call at all while the PR body cited it as covered. Any future caller could have
    re-imported under an alias and handed the helper a seconds clock with the pin
    still green. The walk now tracks per-module import bindings, and covers
    ``scripts/`` as well as ``app/`` (both instruments [29] touches live there).

    Returns ``(relpath, lineno, kwarg-names, has_star_kwargs)``.
    """
    sites = []
    for root in (_APP, _SCRIPTS):
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            local: set[str] = {"signed_tape_accel_features"}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for a in node.names:
                        if a.name == "signed_tape_accel_features":
                            local.add(a.asname or a.name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if isinstance(fn, ast.Name):
                    name = fn.id if fn.id in local else None
                elif isinstance(fn, ast.Attribute):
                    name = (
                        fn.attr if fn.attr == "signed_tape_accel_features" else None
                    )
                else:
                    name = None
                if name is None:
                    continue
                kws = {k.arg for k in node.keywords if k.arg}
                star = any(k.arg is None for k in node.keywords)
                sites.append(
                    (path.relative_to(_REPO).as_posix(), node.lineno, kws, star)
                )
    return sites


# The ONLY callers permitted to hand the helper a SECONDS window. After [29] this
# list is EMPTY: a seconds window at a call site inside app/ or scripts/ is, by
# definition, a clock deciding a tape question. Adding a name here is a design
# decision that needs its own receipt.
_SECONDS_ALLOWLIST: set[str] = set()

# Call sites that build their kwargs dynamically (``**win``), so the AST cannot read
# the unit. These are INSTRUMENTS with an explicit ``--window-s`` flag for a
# side-by-side against the legacy unit; a lane module may not appear here.
_DYNAMIC_ALLOWLIST: set[str] = {"scripts/feature_outcome_correlation.py", "scripts/tape_verdict_probe.py"}


def test_every_app_caller_of_signed_tape_accel_features_is_print_indexed():
    sites = _call_sites()
    assert sites, "the AST walk found no call sites at all — the pin is not watching"
    seconds = [
        f"{f}:{ln}" for f, ln, kws, _ in sites
        if "window_s" in kws and f not in _SECONDS_ALLOWLIST
    ]
    assert not seconds, (
        "these call sites still hand the tape helper a CLOCK: "
        f"{seconds}. Fifteen seconds is ~900 prints on a fast name and four on a "
        "slow one; the verdict flipped on 22 of 63 live entries between the two forms."
    )
    dynamic = [
        f"{f}:{ln}" for f, ln, kws, star in sites
        if star and f not in _DYNAMIC_ALLOWLIST
    ]
    assert not dynamic, (
        f"these call sites hide the window behind ** unpacking ({dynamic}); the AST "
        "cannot tell whether a clock is being passed, so the pin cannot hold."
    )
    # WHAT THIS ENFORCES, EXACTLY: no call site of the WRAPPER passes ``window_s``.
    # Calls that pass no window argument at all are fine — the wrapper's default IS
    # the print form since [29], and it reports ``window_prints`` on the receipt.
    # NOT enforced here (and named so nobody reads more into a green test): calls to
    # the PURE ``_signed_tape_features``, which ``first_dip_tape_policy`` still makes
    # with ``window_s=policy.window_seconds`` from inside a sha256-sealed policy
    # schema — moving that field changes provenance hashes and needs its own slice.
    ambiguous = [
        f"{f}:{ln}" for f, ln, kws, star in sites
        if "window_prints" not in kws and "window_s" not in kws and not star
    ]
    for site in ambiguous:
        assert site  # they read the print default; nothing to assert beyond presence


def test_the_ast_pin_sees_an_aliased_import():
    """The regression this pin was blind to: a caller that renames the helper on
    import. live_runner does exactly that at the re-entry ramp."""
    sites = _call_sites()
    files = {f for f, _ln, _k, _s in sites}
    lr = "app/services/trading/momentum_neural/live_runner.py"
    assert lr in files, (
        "the aliased live_runner call sites are invisible to the pin again — "
        "an alias must not be a way around it"
    )
    aliased = [
        (f, ln) for f, ln, kws, _s in sites
        if f == lr and "window_prints" in kws
    ]
    assert aliased, "the re-entry ramp / accel-reversal print calls are not seen"


def test_the_ramp_window_follows_the_shared_n_in_the_settings_model():
    """ONE derived N in PRODUCTION, not merely in this process's environment.

    The earlier form of this test asserted equality on the runtime ``settings``
    singleton — which proves only that the TEST process sets neither env var. The
    two are independent aliases, so re-pinning ``CHILI_MOMENTUM_TAPE_WINDOW_PRINTS``
    alone (the PR's own open question invites it) would have left the ramp and the
    accel-reversal exit on 255 while the entry surface moved, with nothing in the
    running lane detecting it. Settings now derives one from the other."""
    from app.config import Settings

    assert int(settings.chili_momentum_tape_window_prints) == int(
        settings.chili_momentum_g4_reentry_tape_window_prints
    )

    s2 = Settings(chili_momentum_tape_window_prints=181)
    assert int(s2.chili_momentum_g4_reentry_tape_window_prints) == 181, (
        "re-pinning the shared tape window left the ramp reading a different one"
    )

    # …and an operator who NAMES the ramp's own variable still gets two windows.
    import os

    prev = os.environ.get("CHILI_MOMENTUM_G4_REENTRY_TAPE_WINDOW_PRINTS")
    os.environ["CHILI_MOMENTUM_G4_REENTRY_TAPE_WINDOW_PRINTS"] = "64"
    try:
        s3 = Settings(chili_momentum_tape_window_prints=181)
        assert int(s3.chili_momentum_g4_reentry_tape_window_prints) == 64
    finally:
        if prev is None:
            os.environ.pop("CHILI_MOMENTUM_G4_REENTRY_TAPE_WINDOW_PRINTS", None)
        else:
            os.environ["CHILI_MOMENTUM_G4_REENTRY_TAPE_WINDOW_PRINTS"] = prev


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

      time split  : the midpoint of the SPAN puts 227 prints in the front half and
                    28 in the back ⇒ accel = back_buy − front_buy is hugely
                    NEGATIVE, i.e. "the buying collapsed", purely from the clock cut.
      count split : 127 vs 128 prints ⇒ accel = +1 print of size ⇒ POSITIVE.

    Same tape, same instant, opposite sign. Measured live: 17 of 63 entry instants
    flip the accel sign between the two splits of the SAME 255 prints.
    """
    t0 = 1_800_000_000.0
    rows = _burst_then_slow()
    assert len(rows) == 255

    by_time = _signed_tape_features(
        rows, window_s=120.0, tick_rate_floor_pctile=0.0,
        split="time", gap_trim_s=14.69,
    )
    by_count = _signed_tape_features(
        rows, tick_rate_floor_pctile=0.0, split="count", gap_trim_s=14.69,
    )
    assert by_time is not None and by_count is not None
    assert by_time["n_ticks"] == by_count["n_ticks"] == 255, "neither may be trimmed"
    assert by_count["gap_restricted"] is False, "there is no discontinuity here"
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


def _tape_with_hole(hole_s: float, n: int = 200, cadence_s: float = 0.1):
    """``n`` prints on a ``cadence_s`` cadence with ONE internal hole of ``hole_s``."""
    t = 1_800_000_000.0
    rows = []
    for i in range(n):
        rows.append(_lift(1.0 + i * 0.001, t))
        t += hole_s if i == n // 2 else cadence_s
    return rows


def _burst_then_slow():
    """200 prints at 4 ms, then 55 at 1 s — a burst that went quiet, which is most of
    what our names do. No discontinuity: the window's own p90 gap is 1 s."""
    t = 1_800_000_000.0
    rows = []
    for i in range(200):
        rows.append(_lift(1.00 + i * 0.0001, t))
        t += 0.004
    for i in range(55):
        rows.append(_lift(1.03, t))
        t += 1.0
    return rows


def test_the_discontinuity_bound_is_scale_free_not_a_pooled_floor():
    """A TEN-SECOND hole is normal cadence on a slow name and a HALT on a fast one.

    Three forms of this bound have now been tried and only the third answers that:

      ``window_s / 2`` = 7.5 s     a clock hiding inside a print window; it fired on
                                   7 of 63 live 255-print windows.
      ``max(14.69 s, own p99)``    [29]'s first cut, REFUTED in review: 14.69 s was
                                   derived as the MAX-AGE of the deciding print
                                   (#1386), not as a halt threshold, and as a FLOOR it
                                   declares a 12-second feed outage on a name printing
                                   every 50 ms to be "inside the measured cadence" —
                                   240x its own cadence, the exact halt contamination
                                   the trim exists for.
      own gap p90 x 7.82           the bound now: the window's OWN cadence times the
                                   LARGEST routine p99/p90 ratio measured across 48
                                   symbol-hours of the names we traded on 2026-09-10.

    Both directions are pinned here."""
    # FAST name, 0.05-s cadence, one 12-second feed outage (the documented IQFeed
    # bridge silent-hang). Its own p90 gap is 0.05 s ⇒ bound 0.391 s ⇒ TRIMMED.
    fast = _signed_tape_features(
        _tape_with_hole(12.0, n=255, cadence_s=0.05),
        tick_rate_floor_pctile=0.0, split="count", gap_trim_s=14.69,
    )
    assert fast is not None
    assert fast["gap_restricted"] is True, (
        "a hole 240x the name's own cadence is a discontinuity, not cadence"
    )
    assert fast["n_ticks"] < 255
    # (epoch-second floats carry ~1e-7 s of resolution at 1.8e9, so the cadence
    # recovered from the timestamps is 0.05 s to within that, not to the bit.)
    assert fast["gap_trim_window_p90_s"] == pytest.approx(0.05, abs=1e-6)
    assert fast["gap_trim_mult"] == pytest.approx(7.82)
    assert fast["gap_trim_s"] == pytest.approx(0.05 * 7.82, abs=1e-5)
    assert fast["gap_trim_basis"] == "window_gap_p90 x measured_p99_over_p90"

    # SLOW name, 60-s cadence, the SAME 12-second hole: routine. Nothing trimmed.
    slow = _signed_tape_features(
        _tape_with_hole(12.0, n=60, cadence_s=60.0),
        tick_rate_floor_pctile=0.0, split="count", gap_trim_s=14.69,
    )
    assert slow is not None
    assert slow["gap_restricted"] is False, "12 s is nothing to a once-a-minute name"
    assert slow["n_ticks"] == 60
    assert slow["gap_trim_s"] == pytest.approx(60.0 * 7.82, abs=1e-3)

    # …and the SAME ten-second hole IS a halt under the old seconds literal on ANY
    # name, which is exactly the defect: the unit decided, not the tape.
    legacy = _signed_tape_features(
        _tape_with_hole(10.0, n=60, cadence_s=60.0), window_s=15.0,
        tick_rate_floor_pctile=0.0,
    )
    assert legacy is None, (
        "the 7.5-second literal trims a once-a-minute name at EVERY print, leaving "
        "fewer than three — the window is destroyed by the unit, not by the tape"
    )


def test_ordinary_jitter_on_a_slow_name_is_not_a_halt_at_the_production_n():
    """THE SELF-REFERENTIAL PERCENTILE, at the N that actually ships.

    The refuted bound took ``max(floor, the window's own gap p99)``. At N=255 there
    are 254 gaps and the nearest-rank p99 index is ``ceil(0.99*254)-1 = 251`` — the
    THIRD LARGEST gap of the window itself — so exactly two of its own gaps always
    exceed it. A slow name with ordinary cadence jitter and NO halt was therefore
    shredded (measured in review: 255 prints, ~23 s median cadence, n_ticks 255 ->
    47, decision by jitter). The one test that covered this used n=30, where the p99
    index IS the maximum and nothing can exceed it — the degenerate small-n regime
    hid the production behaviour. This runs the production N."""
    import random

    rng = random.Random(7)
    t = 1_800_000_000.0
    rows = []
    for i in range(255):
        rows.append(_lift(1.0 + i * 0.001, t))
        t += rng.lognormvariate(3.15, 0.9)   # median ~23 s, a real tail, no halt
    out = _signed_tape_features(
        rows, tick_rate_floor_pctile=0.0, split="count", gap_trim_s=14.69,
    )
    assert out is not None
    assert out["gap_restricted"] is False, (
        "cadence jitter was read as a halt: the bound is self-referential again"
    )
    assert out["n_ticks"] == 255


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
    # the window's own p90 non-zero gap (3 s) x the measured multiplier
    assert dbg["gap_trim_s"] == pytest.approx(3.0 * 7.82)
    assert dbg["gap_trim_basis"] == "window_gap_p90 x measured_p99_over_p90"
    # [29] review fix — the receipt now carries what a reader needs to SEE the defect:
    # whether the window was trimmed, how old the deciding print was, and what the
    # activity floor was ranked against.
    for k in ("gap_restricted", "print_age_s", "print_age_bound_s", "print_stale",
              "tick_rate_floor_n", "tick_rate_floor_pctile", "tick_rate_basis"):
        assert k in dbg, f"the receipt cannot expose the window without {k}"


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


# ── 5b. A PRINT WINDOW HAS NO LOWER TIME BOUND, SO EACH SURFACE MUST SAY SO ─────


def _dead_tape(as_of_epoch: float, age_s: float):
    """255-ish prints that all finished ``age_s`` ago, back half heavier on the BUY —
    i.e. a tape that would read 'confirmed' if nobody asked WHEN it printed."""
    t0 = as_of_epoch - age_s
    rows = [_hit(10.00 + i * 0.001, t0 - 12.0 + i * 0.05, 100) for i in range(120)]
    rows += [_lift(10.10 + i * 0.001, t0 - 6.0 + i * 0.05, 900) for i in range(120)]
    return rows


_AS_OF = datetime(2026, 9, 10, 14, 20, 0)
_AS_OF_EPOCH = (_AS_OF - datetime(1970, 1, 1)).total_seconds()


def test_tape_confirms_hold_fails_closed_on_a_tape_that_finished_hours_ago(monkeypatch):
    """THE BLOCKING DEFECT [29] SHIPPED AND THIS FIXES.

    The print branch is ``WHERE symbol=:s AND observed_at <= :as_of ORDER BY
    observed_at DESC LIMIT :n`` — NO lower bound — and ``iqfeed_trade_ticks`` retains
    14 days. The 15-s form was stale-proof by construction (a row inside the window was
    at most 15 s old); the print form is not. MEASURED with the real helper on the live
    book at as_of 2026-09-10 07:30:00Z (a premarket decision instant; median first tick
    of the day is 13:57Z, so decision instants routinely precede a name's first print):
    the age of the newest print was 8.61 h on TNON, 9.90 h on BJDX, 40.28 h on SKYQ —
    and all three FLIPPED ``tape_confirms_hold`` from its documented fail-CLOSED floor
    to ``tape_hold_confirmed``, arming the early fire for 12 pattern triggers plus the
    momentum continuation on YESTERDAY'S tape. The internal gap trim cannot see it: the
    hole is TRAILING, outside the window."""
    _optional_passthrough(monkeypatch)
    rows = _dead_tape(_AS_OF_EPOCH, age_s=6 * 3600.0)
    ok, dbg = EG.tape_confirms_hold(
        "HALTD", db=_FakeDB(rows), settings=settings, l2_as_of=_AS_OF
    )
    assert ok is False, "a six-hour-dead tape armed the early fire"
    assert dbg["reason"] == "tape_hold_source_stale"
    assert dbg["print_stale"] is True
    assert dbg["print_age_s"] > dbg["print_age_bound_s"]

    # the SAME tape shape, ending AT the decision instant, still confirms.
    fresh = _dead_tape(_AS_OF_EPOCH, age_s=0.0)
    ok2, dbg2 = EG.tape_confirms_hold(
        "HALTD", db=_FakeDB(fresh), settings=settings, l2_as_of=_AS_OF
    )
    assert ok2 is True and dbg2["reason"] == "tape_hold_confirmed"
    assert dbg2["print_stale"] is False


def test_l2_entry_confirm_never_defers_on_a_stale_tape(monkeypatch):
    """The OTHER direction, from the SAME missing bound. ``_l2_entry_confirm``'s own
    docstring promises "FAIL-OPEN ... Never defers on missing / thin / stale data",
    and on the live book two of the six measured names (MOBX 23.4 h, WYHG 17.1 h)
    manufactured a DEFER out of hour-old prints."""
    _optional_passthrough(monkeypatch)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(settings, "chili_momentum_l2_confirm_enabled", True)
    # back half SELLS -> buy_share_delta < 0 -> the defer leg, if it were allowed to run
    t0 = _AS_OF_EPOCH - 20 * 3600.0
    rows = [_lift(10.00 + i * 0.001, t0 + i * 0.05, 900) for i in range(120)]
    rows += [_hit(10.00 - i * 0.001, t0 + 6.0 + i * 0.05, 900) for i in range(120)]
    decision, dbg = EG._l2_entry_confirm(
        "STALE", db=_FakeDB(rows), settings=settings, l2_as_of=_AS_OF
    )
    assert decision == "confirm", "a 20-hour-old tape produced a refusal"
    assert dbg["reason"] == "l2_confirm_tape_stale"
    assert dbg["print_stale"] is True


def test_the_raw_break_escape_stays_fail_closed_on_a_stale_tape(monkeypatch):
    """``tape_required_fail_closed`` means RECENT tape: the escape rides the same
    two legs ``tape_confirms_hold`` does, so the same trailing hole bypassed it."""
    _optional_passthrough(monkeypatch)
    rows = _dead_tape(_AS_OF_EPOCH, age_s=6 * 3600.0)
    ok, dbg = EG._explosive_raw_break_escape(
        "HALTD", db=_FakeDB(rows), vol_ratio=99.0, explosive_rvol_floor=1.0,
        l2_as_of=_AS_OF,
    )
    assert ok is False
    assert dbg["raw_break_blocked"] == "tape_source_stale"
    assert dbg["raw_break_print_stale"] is True


# ── 5c. the activity floor must be ABLE to refuse at the shipped percentile ──────


def test_the_tick_rate_floor_can_refuse_at_the_shipped_percentile():
    """``chili_momentum_l2_confirm_tick_rate_floor_pctile`` ships at 0.0, so the floor
    is the MINIMUM of the sample. Once [29] made ``tick_rate`` an exact member of that
    sample, ``tick_rate >= tick_rate_floor`` became provably unable to fire — a member
    is always >= its own minimum. Confirmed on the live book: ``rate >= floor`` was
    True on all six symbols measured, including a 7-print window from 23 hours before.
    The fix ranks the back half against the OTHER rolling windows, so a back half that
    really has gone quiet is refused."""
    t = 1_800_000_000.0
    rows = []
    for i in range(12):          # brisk front: 0.1 s apart
        rows.append(_lift(1.0 + i * 0.001, t))
        t += 0.1
    for i in range(12):          # the back half stalls: 5 s apart
        rows.append(_lift(1.02 + i * 0.001, t))
        t += 5.0
    out = _signed_tape_features(
        rows, tick_rate_floor_pctile=0.0, split="count", gap_trim_s=14.69,
    )
    assert out is not None
    assert out["tick_rate_floor_excludes_self"] is True
    assert out["tick_rate_floor_n"] >= 2
    assert out["tick_rate"] < out["tick_rate_floor"], (
        "the activity leg still cannot refuse at the SHIPPED percentile 0.0"
    )
    # and the pctile that decided is on the receipt, not inferred.
    assert out["tick_rate_floor_pctile"] == pytest.approx(0.0)

    # A tape with an even cadence is NOT refused — the leg conditions, it does not veto.
    even = [_lift(1.0 + i * 0.001, 1_800_000_000.0 + i * 0.1) for i in range(24)]
    ok = _signed_tape_features(
        even, tick_rate_floor_pctile=0.0, split="count", gap_trim_s=14.69,
    )
    assert ok is not None and ok["tick_rate"] >= ok["tick_rate_floor"]


# ── 5d. no clock may supply the rate inside the print form ──────────────────────


def test_the_count_split_never_falls_back_to_window_s_over_two():
    """THE 15-SECOND KNOB WAS STILL LATENT INSIDE THE PRINT FORM.

    ``back_secs`` fell back to ``max(1e-6, window_s) / 2`` = 7.5 s whenever the back
    half had fewer than two distinct timestamps — common on fast names (TNON
    2026-09-10 14:30-14:31Z: 788 prints / 735 distinct ``observed_at``) and reachable
    whenever the gap trim shortens the window to three or four prints. The receipt
    said ``window_s: None`` while a 7.5-second literal decided ``tick_rate``. The rate
    now comes from the window's OWN median cadence, and the receipt names the basis."""
    t = 1_800_000_000.0
    rows = [_lift(1.000, t), _lift(1.001, t + 0.2), _lift(1.002, t + 0.4),
            _lift(1.003, t + 0.6),
            # the back half shares ONE timestamp — a burst inside one tick of
            # timestamp resolution
            _lift(1.004, t + 0.8), _lift(1.005, t + 0.8),
            _lift(1.006, t + 0.8), _lift(1.007, t + 0.8)]
    out = _signed_tape_features(
        rows, tick_rate_floor_pctile=0.0, split="count", gap_trim_s=14.69,
    )
    assert out is not None
    assert out["tick_rate_basis"] == "window_median_cadence"
    # median non-zero gap is 0.2 s over 4 back-half prints -> 3 * 0.2 = 0.6 s span
    assert out["tick_rate"] == pytest.approx(3.0 / 0.6)
    # …and the same rows read at two different (irrelevant) seconds windows are equal:
    # no clock is reachable from the count split at all.
    a = _signed_tape_features(
        rows, tick_rate_floor_pctile=0.0, split="count", gap_trim_s=14.69,
        window_s=15.0,
    )
    b = _signed_tape_features(
        rows, tick_rate_floor_pctile=0.0, split="count", gap_trim_s=14.69,
        window_s=600.0,
    )
    assert a is not None and b is not None
    assert a["tick_rate"] == b["tick_rate"] == pytest.approx(out["tick_rate"])


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
    assert cold is False and rc["reason"] == "tape_cold_observed"
    assert rc["cold_observed"] is True
    assert rc["binding"] == "observational_arm_population_not_calibrated"
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


# ── 6b. the two ALREADY-SHIPPED print callers changed unit, so nothing may compare
#       across that change, and the receipts must say which unit decided ──────────

_LIVE_RUNNER = (
    _APP / "services" / "trading" / "momentum_neural" / "live_runner.py"
)


def test_the_accel_reversal_exit_refuses_to_compare_accels_across_a_unit_change():
    """[29] changed the UNIT of ``signed_tape_accel`` for every ``window_prints`` read,
    including the two callers that were already print-indexed. For the accel-reversal
    exit that matters twice over:

      * gate 2 is a SIGN CROSSING (``prev > 0 -> accel <= 0``), so the split re-defines
        the event; the band conditioning gate 3 has been re-derived in the new unit
        (see test_constant_is_the_derived_band);
      * ``prev_signed_tape_accel`` is PERSISTED leg state and survives a restart, so the
        first tick after a deploy would compare a time-split ``prev`` against a
        count-split ``accel`` — a manufactured (or suppressed) climax exit on a real
        position, since a fired gate writes ``pos["stop_price"]``.

    The value is now stamped with its unit and the comparison is refused across a
    change. Source pin: this is runner-loop state, and the loop cannot be driven from a
    unit test without a live session."""
    src = _LIVE_RUNNER.read_text(encoding="utf-8")
    assert 'le["prev_signed_tape_accel_unit"] = _tape_unit' in src, (
        "the persisted accel no longer carries the unit it was measured in"
    )
    assert "_prev_unit_mismatch" in src and "_prev_accel = None" in src, (
        "a cross-unit prev is being fed to the genuine-TURN test again"
    )
    assert '"prev_unit_mismatch": _prev_unit_mismatch' in src, (
        "the receipt does not report that a comparison was refused"
    )
    # …and the stamp is cleared with the value it describes when a watcher recycles.
    i_val = src.index('    "prev_signed_tape_accel",')
    assert '"prev_signed_tape_accel_unit",' in src[i_val:i_val + 400], (
        "the recycle clear-list drops the accel but keeps its unit stamp"
    )
    # the same block must not read a rollover off a tape that stopped printing.
    assert "if bool(_tape_stale):" in src and "_accel = None" in src


def test_the_reentry_ramp_reports_the_unit_that_decided():
    """The ramp's level>=1 leg is ``signed_tape_accel > 0 AND buy_share_delta > 0``. It
    is a SIGN test, not a fitted band, so the count split needs no refit — but it is a
    silent change unless the receipt names it."""
    src = _LIVE_RUNNER.read_text(encoding="utf-8")
    i = src.index('_g4e_dbg["binding"] = {')
    block = src[i:i + 900]
    for k in ('"tape_split"', '"gap_trim_basis"', '"gap_restricted"'):
        assert k in block, f"the ramp receipt does not carry {k}"


def test_the_window_receipt_is_copied_from_ONE_place():
    """Each of the three entry surfaces used to hand-copy a different subset of the
    helper's fields, which is how a window trimmed from 255 prints to 47, or one whose
    newest print was hours old, booked as a healthy ``span_s``."""
    tape = EG.signed_tape_accel_features(
        "SKYQ",
        db=_FakeDB([_lift(1.0 + i * 0.01, 1_800_000_000.0 + i * 0.1) for i in range(20)]),
        as_of=datetime(2026, 9, 10, 18, 0, 0),
    ) or {}
    r = EG.tape_window_receipt(tape)
    for k in ("window_kind", "window_prints", "n_ticks", "span_s", "tape_split",
              "gap_restricted", "gap_trim_s", "gap_trim_basis",
              "gap_trim_window_p90_s", "gap_trim_mult",
              "print_age_s", "print_age_bound_s", "print_stale",
              "tick_rate_floor_n", "tick_rate_floor_pctile", "tick_rate_basis"):
        assert k in r, f"the shared receipt lost {k}"
    assert EG.tape_window_receipt(None) == {}
    pref = EG.tape_window_receipt(tape, prefix="raw_break_")
    assert "raw_break_print_stale" in pref and "print_stale" not in pref


# ── 6c. the INSTRUMENT must measure the window it names ─────────────────────────


def test_tape_verdict_probe_measures_the_window_it_reports(monkeypatch):
    from scripts.tape_verdict_probe import probe
    called = []
    def reader(symbol, **kwargs):
        called.append((symbol, kwargs))
        return {"selection_contract": "test", "window_prints": kwargs.get("window_prints")}
    monkeypatch.setattr(EG, "signed_tape_accel_features", reader)
    db = object()
    result = probe(db, "ABC", _AS_OF, prints=255)
    assert called == [("ABC", {"db": db, "as_of": _AS_OF, "window_prints": 255})]
    assert result["window_prints"] == 255
    probe(db, "ABC", _AS_OF, window_s=15)
    assert called[-1][1]["window_s"] == 15


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
