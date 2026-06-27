"""Adversarial branch-coverage tests for the READ-ONLY momentum metrics surface.

Target: app/services/trading/momentum_neural/metrics_surface.py — two additive, flag-gated
operator-journaling surfaces with ZERO trading impact (they never gate / size / veto an
entry; they only READ already-closed outcome rows):

  * PROCESS-OVER-PROFITS SCORE  (process_over_profits_score, item 4)
  * CHALLENGE METRICS SURFACE   (accuracy% / pnl-ratio / streak / summary, item 6)

Coverage strategy — PURE LOGIC + mocks, NO DB truncate:
``_recent_real_outcomes`` is the single bounded read; we replace it (or the underlying
``db.query(...).filter(...).order_by(...).limit(...).all()`` chain) with a fake that returns
synthetic ``[(outcome_class, realized_pnl_usd), ...]`` rows. That exercises every branch of
the score math, the real-entry filter, the empty / all-win / all-loss / no-trade / div-by-zero
paths, the PnL-ratio cap + zero-denominator path, FLAG on/off parity, and the read-only
contract — all without touching Postgres.

Properties proven:
  S = process_over_profits_score, A = accuracy_pct, R = pnl_ratio, D = daily_streak, M = summary

  S1  flag OFF  => None, and the DB is NEVER touched (byte-identical to not existing)
  S2  score math = mean of per-class adherence weights over REAL ENTERED trades
  S3  real-entry filter: never-entered classes (cancelled_pre_entry/no_fill/...) are SKIPPED
  S4  pnl=None on a real-entered class is skipped (belt-and-suspenders)
  S5  no real trades => thin_history sentinel (no div-by-zero)
  S6  unknown entered class => 0.5 neutral weight
  S7  win_rate counts strictly pnl > 0.0 ; non-numeric pnl swallowed for the WIN tally
  S8  window resolution: bad/None/zero settings fall back to the default window
  S9  _recent_real_outcomes returns [] on db None / limit<=0 / query error (fail-neutral)
  A1  accuracy% = honored / real * 100 (honored = adherence >= 0.5), thin => None
  A2  all-loss(stop_loss) is HONORED process (planned stop) => 100% accuracy
  A3  bailout / governance_exit are demerits (< 0.5) => NOT honored
  R1  pnl_ratio = sum(win)/|sum(loss)|, CAPPED at _PNL_RATIO_CAP
  R2  all-win (no losses) => cap ; all-loss (no wins) => None ; no trades => None
  R3  near-zero loss denominator can't explode the ratio (cap bites)
  M1  summary {} when flag OFF (byte-identical) ; populated dict otherwise
  M2  summary fans the SAME window into accuracy + pnl_ratio

[[project_momentum_lane]] [[feedback_no_dark_flags]]
"""
from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.services.trading.momentum_neural import metrics_surface as ms
from app.services.trading.momentum_neural.metrics_surface import (
    _PNL_RATIO_CAP,
    _PROCESS_DEFAULT_WINDOW,
    _CHALLENGE_DEFAULT_WINDOW,
    challenge_metrics_accuracy_pct,
    challenge_metrics_daily_streak,
    challenge_metrics_pnl_ratio,
    challenge_metrics_summary,
    process_over_profits_score,
)
from app.services.trading.momentum_neural.outcome_labels import (
    OUTCOME_BAILOUT,
    OUTCOME_CANCELLED_PRE_ENTRY,
    OUTCOME_ERROR_EXIT,
    OUTCOME_GOVERNANCE_EXIT,
    OUTCOME_NO_FILL,
    OUTCOME_RISK_BLOCK,
    OUTCOME_SMALL_WIN,
    OUTCOME_STOP_LOSS,
    OUTCOME_SUCCESS,
    OUTCOME_TIMED_EXIT,
)

_EF = "coinbase_spot"

# A sentinel object that is NOT None, so the `db is None` short-circuit in
# _recent_real_outcomes is the only thing that can produce [] for a real db arg.
_DB = object()


# ── helpers ───────────────────────────────────────────────────────────────────


def _patch_rows(monkeypatch, rows: list[tuple[str | None, float | None]]) -> None:
    """Replace the single bounded read with a fake returning ``rows`` (newest first).

    Pure-logic: no DB. The fake honours ``limit`` (slices to it, mirroring .limit(N).all())
    so window-truncation behaviour is exercised too.
    """

    def _fake(db: Any, *, execution_family: str | None, limit: int):
        if db is None or limit <= 0:
            return []
        return list(rows)[: int(limit)]

    monkeypatch.setattr(ms, "_recent_real_outcomes", _fake)


class _FakeQuery:
    """Minimal stand-in for a SQLAlchemy query chain that yields preset rows."""

    def __init__(self, rows, *, boom: bool = False):
        self._rows = rows
        self._boom = boom

    def filter(self, *a, **k):  # noqa: D401 - chainable no-op
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, n):
        self._rows = self._rows[: int(n)]
        return self

    def all(self):
        if self._boom:
            raise RuntimeError("synthetic query failure")
        return list(self._rows)


class _FakeDB:
    """A db whose .query(...) returns a _FakeQuery of preset (class, pnl) rows."""

    def __init__(self, rows, *, boom: bool = False):
        # rows stored as 2-tuples; the real code indexes r[0], r[1]
        self._rows = list(rows)
        self._boom = boom

    def query(self, *cols):
        return _FakeQuery(self._rows, boom=self._boom)


def _enable_process(monkeypatch, *, window: Any = None) -> None:
    monkeypatch.setattr(settings, "chili_momentum_process_score_enabled", True, raising=False)
    if window is not None:
        monkeypatch.setattr(settings, "chili_momentum_process_score_window", window, raising=False)


def _enable_challenge(monkeypatch, *, window: Any = None) -> None:
    monkeypatch.setattr(settings, "chili_momentum_challenge_metrics_enabled", True, raising=False)
    if window is not None:
        monkeypatch.setattr(settings, "chili_momentum_challenge_metrics_window", window, raising=False)


# ══════════════════════════════════════════════════════════════════════════════
# S1 — flag OFF => None and DB never touched
# ══════════════════════════════════════════════════════════════════════════════


def test_s1_process_flag_off_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_process_score_enabled", False, raising=False)
    assert process_over_profits_score(_DB, execution_family=_EF) is None


def test_s1_process_flag_off_does_not_touch_db(monkeypatch) -> None:
    # byte-identical to the function not existing: the read must NOT run when OFF.
    monkeypatch.setattr(settings, "chili_momentum_process_score_enabled", False, raising=False)

    def _must_not_run(*a, **k):
        raise AssertionError("recent-outcomes read must not run when the flag is OFF")

    monkeypatch.setattr(ms, "_recent_real_outcomes", _must_not_run)
    assert process_over_profits_score(object(), execution_family=_EF) is None


# ══════════════════════════════════════════════════════════════════════════════
# S2 — score math = mean adherence over REAL ENTERED trades
# ══════════════════════════════════════════════════════════════════════════════


def test_s2_all_success_score_is_one(monkeypatch) -> None:
    _enable_process(monkeypatch)
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, 10.0)] * 4)
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out is not None
    assert out["process_score"] == 1.0
    assert out["n"] == 4
    assert out["real_trades"] == 4
    assert out["wins"] == 4
    assert out["win_rate"] == 1.0


def test_s2_mixed_class_mean_is_exact(monkeypatch) -> None:
    _enable_process(monkeypatch)
    # success(1.0) + stop_loss(0.5) + bailout(0.0) => mean 0.5
    _patch_rows(
        monkeypatch,
        [(OUTCOME_SUCCESS, 12.0), (OUTCOME_STOP_LOSS, -5.0), (OUTCOME_BAILOUT, -2.0)],
    )
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["process_score"] == pytest.approx(0.5)
    assert out["real_trades"] == 3
    assert out["wins"] == 1  # only the +12 success is pnl > 0


def test_s2_score_is_rounded_to_four_places(monkeypatch) -> None:
    _enable_process(monkeypatch)
    # success + success + stop_loss => (1+1+0.5)/3 = 0.8333... -> 0.8333
    _patch_rows(
        monkeypatch,
        [(OUTCOME_SUCCESS, 1.0), (OUTCOME_SUCCESS, 1.0), (OUTCOME_STOP_LOSS, -1.0)],
    )
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["process_score"] == 0.8333


def test_s2_small_win_and_timed_exit_full_credit(monkeypatch) -> None:
    _enable_process(monkeypatch)
    _patch_rows(monkeypatch, [(OUTCOME_SMALL_WIN, 3.0), (OUTCOME_TIMED_EXIT, 1.0)])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["process_score"] == 1.0  # both weight 1.0


def test_s2_stop_loss_is_neutral_half_not_zero(monkeypatch) -> None:
    # A planned stop is GOOD process (0.5), NOT a process failure (0.0).
    _enable_process(monkeypatch)
    _patch_rows(monkeypatch, [(OUTCOME_STOP_LOSS, -5.0), (OUTCOME_STOP_LOSS, -7.0)])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["process_score"] == 0.5
    assert out["wins"] == 0


def test_s2_governance_exit_is_demerit_zero(monkeypatch) -> None:
    _enable_process(monkeypatch)
    _patch_rows(monkeypatch, [(OUTCOME_GOVERNANCE_EXIT, -5.0), (OUTCOME_GOVERNANCE_EXIT, -3.0)])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["process_score"] == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# S3 — real-entry filter: never-entered classes are SKIPPED
# ══════════════════════════════════════════════════════════════════════════════


def test_s3_never_entered_rows_skipped(monkeypatch) -> None:
    _enable_process(monkeypatch)
    # 2 real successes + a pile of never-entered $0 rows that MUST NOT pollute the score.
    _patch_rows(
        monkeypatch,
        [
            (OUTCOME_SUCCESS, 10.0),
            (OUTCOME_CANCELLED_PRE_ENTRY, 0.0),
            (OUTCOME_NO_FILL, 0.0),
            (OUTCOME_RISK_BLOCK, 0.0),
            (OUTCOME_ERROR_EXIT, None),
            (OUTCOME_SUCCESS, 8.0),
        ],
    )
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["real_trades"] == 2  # only the 2 successes
    assert out["n"] == 2
    assert out["process_score"] == 1.0
    assert out["wins"] == 2


def test_s3_only_never_entered_is_thin_history(monkeypatch) -> None:
    _enable_process(monkeypatch)
    _patch_rows(
        monkeypatch,
        [(OUTCOME_CANCELLED_PRE_ENTRY, 0.0), (OUTCOME_NO_FILL, 0.0)],
    )
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out == {"process_score": None, "n": 0, "real_trades": 0, "reason": "thin_history"}


# ══════════════════════════════════════════════════════════════════════════════
# S4 — pnl=None on a real-entered class is skipped (belt-and-suspenders)
# ══════════════════════════════════════════════════════════════════════════════


def test_s4_real_class_with_none_pnl_skipped(monkeypatch) -> None:
    _enable_process(monkeypatch)
    # success carries None pnl (anomaly) -> skipped; only the real +10 success counts.
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, None), (OUTCOME_SUCCESS, 10.0)])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["real_trades"] == 1
    assert out["process_score"] == 1.0


def test_s4_all_none_pnl_is_thin_history(monkeypatch) -> None:
    _enable_process(monkeypatch)
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, None), (OUTCOME_STOP_LOSS, None)])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["reason"] == "thin_history"
    assert out["real_trades"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# S5 — empty history => thin_history (NO div-by-zero)
# ══════════════════════════════════════════════════════════════════════════════


def test_s5_empty_history_thin(monkeypatch) -> None:
    _enable_process(monkeypatch)
    _patch_rows(monkeypatch, [])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out == {"process_score": None, "n": 0, "real_trades": 0, "reason": "thin_history"}


def test_s5_no_div_by_zero_when_zero_real(monkeypatch) -> None:
    # adher is empty -> the `sum(adher)/len(adher)` line is never reached. No ZeroDivisionError.
    _enable_process(monkeypatch)
    _patch_rows(monkeypatch, [(OUTCOME_NO_FILL, 0.0)])
    out = process_over_profits_score(_DB, execution_family=_EF)  # must not raise
    assert out["process_score"] is None


# ══════════════════════════════════════════════════════════════════════════════
# S6 — unknown entered class => 0.5 neutral
# ══════════════════════════════════════════════════════════════════════════════


def test_s6_unknown_entered_class_neutral_half(monkeypatch) -> None:
    _enable_process(monkeypatch)
    # "flat_unknown" is a real-entry class (not in _NEVER_ENTERED) but not in _PROCESS_ADHERENCE.
    _patch_rows(monkeypatch, [("flat_unknown", 1.0), ("flat_unknown", -1.0)])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["real_trades"] == 2
    assert out["process_score"] == 0.5  # default neutral weight


def test_s6_class_case_and_whitespace_normalised(monkeypatch) -> None:
    # adherence lookup lowercases + strips: "  SUCCESS  " must still map to 1.0.
    _enable_process(monkeypatch)
    _patch_rows(monkeypatch, [("  SUCCESS  ", 5.0)])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["process_score"] == 1.0


# ══════════════════════════════════════════════════════════════════════════════
# S7 — win tally is strict pnl > 0.0 ; non-numeric pnl swallowed for the WIN count
# ══════════════════════════════════════════════════════════════════════════════


def test_s7_zero_pnl_is_not_a_win(monkeypatch) -> None:
    _enable_process(monkeypatch)
    # an entered success that closed at exactly $0.00 is a real trade but NOT a win (> 0 strict).
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, 0.0), (OUTCOME_SUCCESS, 5.0)])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["real_trades"] == 2
    assert out["wins"] == 1
    assert out["win_rate"] == 0.5


def test_s7_nonnumeric_pnl_swallowed_for_win_count(monkeypatch) -> None:
    # A non-numeric pnl on a real class: it is NOT None (so it is a real trade & scored), but
    # float() raises in the win tally -> swallowed -> not a win, no crash.
    _enable_process(monkeypatch)
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, "not-a-number"), (OUTCOME_SUCCESS, 5.0)])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["real_trades"] == 2  # both are non-None real classes
    assert out["wins"] == 1  # only the numeric +5 counts; the string is swallowed
    assert out["process_score"] == 1.0


# ══════════════════════════════════════════════════════════════════════════════
# S8 — window resolution falls back to default on bad settings
# ══════════════════════════════════════════════════════════════════════════════


def test_s8_window_default_when_unset(monkeypatch) -> None:
    _enable_process(monkeypatch)
    # ensure the attr is absent/None -> default window reported back
    monkeypatch.setattr(settings, "chili_momentum_process_score_window", None, raising=False)
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, 1.0)])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["window"] == _PROCESS_DEFAULT_WINDOW


def test_s8_window_zero_falls_back_to_default(monkeypatch) -> None:
    # `int(0) or default` -> 0 is falsy -> default. (A 0 window would otherwise read nothing.)
    _enable_process(monkeypatch, window=0)
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, 1.0)])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["window"] == _PROCESS_DEFAULT_WINDOW


def test_s8_window_garbage_falls_back_to_default(monkeypatch) -> None:
    _enable_process(monkeypatch, window="garbage")
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, 1.0)])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["window"] == _PROCESS_DEFAULT_WINDOW


def test_s8_explicit_window_passthrough(monkeypatch) -> None:
    _enable_process(monkeypatch, window=7)
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, 1.0)])
    out = process_over_profits_score(_DB, execution_family=_EF)
    assert out["window"] == 7


def test_s8_execution_family_echoed(monkeypatch) -> None:
    _enable_process(monkeypatch)
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, 1.0)])
    out = process_over_profits_score(_DB, execution_family="robinhood_agentic")
    assert out["execution_family"] == "robinhood_agentic"


# ══════════════════════════════════════════════════════════════════════════════
# S9 — _recent_real_outcomes is fail-neutral ([]) — the real read, exercised directly
# ══════════════════════════════════════════════════════════════════════════════


def test_s9_recent_outcomes_none_db_empty() -> None:
    assert ms._recent_real_outcomes(None, execution_family=_EF, limit=10) == []


def test_s9_recent_outcomes_nonpositive_limit_empty() -> None:
    assert ms._recent_real_outcomes(_FakeDB([]), execution_family=_EF, limit=0) == []
    assert ms._recent_real_outcomes(_FakeDB([]), execution_family=_EF, limit=-3) == []


def test_s9_recent_outcomes_query_error_fail_neutral(monkeypatch) -> None:
    # The model import succeeds but .all() blows up -> the try/except returns [] (fail-neutral),
    # never propagates. We force the chain to raise via the fake.
    db = _FakeDB([(OUTCOME_SUCCESS, 1.0)], boom=True)
    # Patch the model import target so the function body reaches our fake chain.
    import app.models.trading as trading_models  # noqa: F401

    rows = ms._recent_real_outcomes(db, execution_family=_EF, limit=10)
    assert rows == []


def test_s9_recent_outcomes_happy_path_maps_tuples(monkeypatch) -> None:
    # Confirm the (r[0], r[1]) projection + execution_family/no-family branches both work.
    db = _FakeDB([(OUTCOME_SUCCESS, 10.0), (OUTCOME_STOP_LOSS, -4.0)])
    with_family = ms._recent_real_outcomes(db, execution_family=_EF, limit=10)
    assert with_family == [(OUTCOME_SUCCESS, 10.0), (OUTCOME_STOP_LOSS, -4.0)]
    no_family = ms._recent_real_outcomes(_FakeDB([(OUTCOME_SMALL_WIN, 2.0)]),
                                         execution_family=None, limit=10)
    assert no_family == [(OUTCOME_SMALL_WIN, 2.0)]


def test_s9_recent_outcomes_honours_limit(monkeypatch) -> None:
    db = _FakeDB([(OUTCOME_SUCCESS, 1.0)] * 10)
    rows = ms._recent_real_outcomes(db, execution_family=_EF, limit=3)
    assert len(rows) == 3


# ══════════════════════════════════════════════════════════════════════════════
# A — challenge_metrics_accuracy_pct
# ══════════════════════════════════════════════════════════════════════════════


def test_a1_accuracy_basic(monkeypatch) -> None:
    # 3 honored (success/small_win/stop_loss all >= 0.5) + 1 demerit (bailout) of 4 real => 75%.
    _patch_rows(
        monkeypatch,
        [
            (OUTCOME_SUCCESS, 5.0),
            (OUTCOME_SMALL_WIN, 1.0),
            (OUTCOME_STOP_LOSS, -3.0),
            (OUTCOME_BAILOUT, -1.0),
        ],
    )
    assert challenge_metrics_accuracy_pct(_DB, execution_family=_EF) == 75.0


def test_a1_accuracy_thin_history_none(monkeypatch) -> None:
    _patch_rows(monkeypatch, [(OUTCOME_NO_FILL, 0.0), (OUTCOME_CANCELLED_PRE_ENTRY, 0.0)])
    assert challenge_metrics_accuracy_pct(_DB, execution_family=_EF) is None


def test_a1_accuracy_empty_none(monkeypatch) -> None:
    _patch_rows(monkeypatch, [])
    assert challenge_metrics_accuracy_pct(_DB, execution_family=_EF) is None


def test_a2_all_stop_loss_is_fully_honored(monkeypatch) -> None:
    # A planned stop is HONORED process: all-loss-by-stop => 100% accuracy (NOT 0%).
    _patch_rows(monkeypatch, [(OUTCOME_STOP_LOSS, -2.0)] * 5)
    assert challenge_metrics_accuracy_pct(_DB, execution_family=_EF) == 100.0


def test_a3_bailout_and_governance_not_honored(monkeypatch) -> None:
    # bailout(0.0) + governance_exit(0.0) are demerits (< 0.5) => 0% honored.
    _patch_rows(monkeypatch, [(OUTCOME_BAILOUT, -1.0), (OUTCOME_GOVERNANCE_EXIT, -2.0)])
    assert challenge_metrics_accuracy_pct(_DB, execution_family=_EF) == 0.0


def test_a3_unknown_class_counts_as_honored(monkeypatch) -> None:
    # unknown entered class defaults to 0.5 (>= 0.5) => honored.
    _patch_rows(monkeypatch, [("flat_unknown", 1.0)])
    assert challenge_metrics_accuracy_pct(_DB, execution_family=_EF) == 100.0


def test_a_accuracy_none_pnl_still_counts(monkeypatch) -> None:
    # accuracy ignores pnl entirely (uses only outcome_class) — a None-pnl real class still
    # counts as a real trade (unlike the score, which drops None-pnl). Locks that divergence.
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, None), (OUTCOME_STOP_LOSS, None)])
    assert challenge_metrics_accuracy_pct(_DB, execution_family=_EF) == 100.0


def test_a_accuracy_rounds_two_places(monkeypatch) -> None:
    # 2 honored of 3 => 66.6666 -> 66.67
    _patch_rows(
        monkeypatch,
        [(OUTCOME_SUCCESS, 1.0), (OUTCOME_SMALL_WIN, 1.0), (OUTCOME_BAILOUT, -1.0)],
    )
    assert challenge_metrics_accuracy_pct(_DB, execution_family=_EF) == 66.67


# ══════════════════════════════════════════════════════════════════════════════
# R — challenge_metrics_pnl_ratio
# ══════════════════════════════════════════════════════════════════════════════


def test_r1_basic_ratio(monkeypatch) -> None:
    # wins 30 / losses 15 => 2.0
    _patch_rows(
        monkeypatch,
        [(OUTCOME_SUCCESS, 20.0), (OUTCOME_SMALL_WIN, 10.0), (OUTCOME_STOP_LOSS, -15.0)],
    )
    assert challenge_metrics_pnl_ratio(_DB, execution_family=_EF) == 2.0


def test_r1_ratio_rounded_four_places(monkeypatch) -> None:
    # 10 / 3 = 3.3333...
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, 10.0), (OUTCOME_STOP_LOSS, -3.0)])
    assert challenge_metrics_pnl_ratio(_DB, execution_family=_EF) == 3.3333


def test_r1_ratio_capped(monkeypatch) -> None:
    # wins 1000 / losses 1 => 1000 -> capped at _PNL_RATIO_CAP
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, 1000.0), (OUTCOME_STOP_LOSS, -1.0)])
    assert challenge_metrics_pnl_ratio(_DB, execution_family=_EF) == _PNL_RATIO_CAP


def test_r2_all_win_no_loss_reports_cap(monkeypatch) -> None:
    # No losses, real wins present => report the cap (bounded "very high").
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, 10.0), (OUTCOME_SMALL_WIN, 5.0)])
    assert challenge_metrics_pnl_ratio(_DB, execution_family=_EF) == _PNL_RATIO_CAP


def test_r2_all_loss_no_win_is_zero(monkeypatch) -> None:
    # losses but zero wins: loss_usd>0 path, wins_usd=0 => min(cap, 0/loss)=0.0 ratio (NOT None).
    _patch_rows(monkeypatch, [(OUTCOME_STOP_LOSS, -5.0), (OUTCOME_STOP_LOSS, -7.0)])
    assert challenge_metrics_pnl_ratio(_DB, execution_family=_EF) == 0.0


def test_r2_no_trades_is_none(monkeypatch) -> None:
    _patch_rows(monkeypatch, [])
    assert challenge_metrics_pnl_ratio(_DB, execution_family=_EF) is None


def test_r2_only_never_entered_is_none(monkeypatch) -> None:
    _patch_rows(monkeypatch, [(OUTCOME_NO_FILL, 0.0), (OUTCOME_CANCELLED_PRE_ENTRY, 0.0)])
    assert challenge_metrics_pnl_ratio(_DB, execution_family=_EF) is None


def test_r2_all_zero_pnl_seen_but_no_win_no_loss_is_none(monkeypatch) -> None:
    # Real entered trades that all closed exactly $0.00: seen>0, loss_usd==0, wins_usd==0
    # => the `loss_usd <= 0` branch with wins_usd not > 0 => None.
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, 0.0), (OUTCOME_TIMED_EXIT, 0.0)])
    assert challenge_metrics_pnl_ratio(_DB, execution_family=_EF) is None


def test_r3_near_zero_loss_denominator_cannot_explode(monkeypatch) -> None:
    # wins 50 / losses 0.0001 => 500000 -> cap bites, not an exploded ratio.
    _patch_rows(monkeypatch, [(OUTCOME_SUCCESS, 50.0), (OUTCOME_STOP_LOSS, -0.0001)])
    assert challenge_metrics_pnl_ratio(_DB, execution_family=_EF) == _PNL_RATIO_CAP


def test_r_nonnumeric_pnl_skipped(monkeypatch) -> None:
    # A non-numeric pnl on a real class is skipped (continue) and does not count toward `seen`.
    _patch_rows(
        monkeypatch,
        [(OUTCOME_SUCCESS, "bad"), (OUTCOME_SUCCESS, 10.0), (OUTCOME_STOP_LOSS, -5.0)],
    )
    assert challenge_metrics_pnl_ratio(_DB, execution_family=_EF) == 2.0


def test_r_none_pnl_skipped(monkeypatch) -> None:
    _patch_rows(
        monkeypatch,
        [(OUTCOME_SUCCESS, None), (OUTCOME_SUCCESS, 8.0), (OUTCOME_STOP_LOSS, -4.0)],
    )
    assert challenge_metrics_pnl_ratio(_DB, execution_family=_EF) == 2.0


# ══════════════════════════════════════════════════════════════════════════════
# D — challenge_metrics_daily_streak (delegates to risk_policy.consecutive_green_days)
# ══════════════════════════════════════════════════════════════════════════════


def test_d_streak_delegates_and_shapes(monkeypatch) -> None:
    import app.services.trading.momentum_neural.risk_policy as rp

    monkeypatch.setattr(
        rp, "consecutive_green_days",
        lambda db, *, execution_family, lookback_days: (3, {"green_usd": 90.0, "days_seen": 5}),
        raising=False,
    )
    out = challenge_metrics_daily_streak(_DB, execution_family=_EF)
    assert out == {"consecutive_green": 3, "green_usd": 90.0, "days_seen": 5}


def test_d_streak_error_fail_neutral(monkeypatch) -> None:
    import app.services.trading.momentum_neural.risk_policy as rp

    def _boom(*a, **k):
        raise RuntimeError("synthetic streak failure")

    monkeypatch.setattr(rp, "consecutive_green_days", _boom, raising=False)
    out = challenge_metrics_daily_streak(_DB, execution_family=_EF)
    assert out == {"consecutive_green": 0, "reason": "error"}


def test_d_streak_lookback_setting_passed(monkeypatch) -> None:
    import app.services.trading.momentum_neural.risk_policy as rp

    seen = {}

    def _capture(db, *, execution_family, lookback_days):
        seen["lookback"] = lookback_days
        return (0, {})

    monkeypatch.setattr(rp, "consecutive_green_days", _capture, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_lookback_days", 14, raising=False)
    challenge_metrics_daily_streak(_DB, execution_family=_EF)
    assert seen["lookback"] == 14


def test_d_streak_int_coerced(monkeypatch) -> None:
    # consecutive_green returns int() of the streak even if the helper hands back a float.
    import app.services.trading.momentum_neural.risk_policy as rp

    monkeypatch.setattr(
        rp, "consecutive_green_days",
        lambda db, *, execution_family, lookback_days: (4.0, {}),
        raising=False,
    )
    out = challenge_metrics_daily_streak(_DB, execution_family=_EF)
    assert out["consecutive_green"] == 4
    assert isinstance(out["consecutive_green"], int)


# ══════════════════════════════════════════════════════════════════════════════
# M — challenge_metrics_summary (flag gate + window fan-out)
# ══════════════════════════════════════════════════════════════════════════════


def test_m1_summary_flag_off_empty_dict(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_challenge_metrics_enabled", False, raising=False)
    assert challenge_metrics_summary(_DB, execution_family=_EF) == {}


def test_m1_summary_flag_off_does_not_touch_db(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_challenge_metrics_enabled", False, raising=False)

    def _must_not_run(*a, **k):
        raise AssertionError("read must not run when challenge flag is OFF")

    monkeypatch.setattr(ms, "_recent_real_outcomes", _must_not_run)
    monkeypatch.setattr(ms, "challenge_metrics_daily_streak", _must_not_run)
    assert challenge_metrics_summary(object(), execution_family=_EF) == {}


def test_m1_summary_populated_shape(monkeypatch) -> None:
    _enable_challenge(monkeypatch)
    _patch_rows(
        monkeypatch,
        [(OUTCOME_SUCCESS, 20.0), (OUTCOME_STOP_LOSS, -10.0)],
    )
    monkeypatch.setattr(ms, "challenge_metrics_daily_streak",
                        lambda db, *, execution_family: {"consecutive_green": 2})
    out = challenge_metrics_summary(_DB, execution_family=_EF)
    assert out["accuracy_pct"] == 100.0  # success + stop_loss both honored
    assert out["pnl_ratio"] == 2.0  # 20 / 10
    assert out["streak"] == {"consecutive_green": 2}
    assert out["window"] == _CHALLENGE_DEFAULT_WINDOW
    assert out["execution_family"] == _EF


def test_m2_summary_fans_same_window(monkeypatch) -> None:
    _enable_challenge(monkeypatch, window=11)
    captured = {"acc": None, "pnl": None}

    def _acc(db, *, execution_family, window):
        captured["acc"] = window
        return None

    def _pnl(db, *, execution_family, window):
        captured["pnl"] = window
        return None

    monkeypatch.setattr(ms, "challenge_metrics_accuracy_pct", _acc)
    monkeypatch.setattr(ms, "challenge_metrics_pnl_ratio", _pnl)
    monkeypatch.setattr(ms, "challenge_metrics_daily_streak",
                        lambda db, *, execution_family: {"consecutive_green": 0})
    out = challenge_metrics_summary(_DB, execution_family=_EF)
    assert captured["acc"] == 11
    assert captured["pnl"] == 11
    assert out["window"] == 11


def test_m2_summary_window_garbage_falls_back(monkeypatch) -> None:
    _enable_challenge(monkeypatch, window="nope")
    monkeypatch.setattr(ms, "challenge_metrics_accuracy_pct",
                        lambda db, *, execution_family, window: None)
    monkeypatch.setattr(ms, "challenge_metrics_pnl_ratio",
                        lambda db, *, execution_family, window: None)
    monkeypatch.setattr(ms, "challenge_metrics_daily_streak",
                        lambda db, *, execution_family: {})
    out = challenge_metrics_summary(_DB, execution_family=_EF)
    assert out["window"] == _CHALLENGE_DEFAULT_WINDOW


def test_m2_summary_window_zero_falls_back(monkeypatch) -> None:
    _enable_challenge(monkeypatch, window=0)
    monkeypatch.setattr(ms, "challenge_metrics_accuracy_pct",
                        lambda db, *, execution_family, window: None)
    monkeypatch.setattr(ms, "challenge_metrics_pnl_ratio",
                        lambda db, *, execution_family, window: None)
    monkeypatch.setattr(ms, "challenge_metrics_daily_streak",
                        lambda db, *, execution_family: {})
    out = challenge_metrics_summary(_DB, execution_family=_EF)
    assert out["window"] == _CHALLENGE_DEFAULT_WINDOW


# ══════════════════════════════════════════════════════════════════════════════
# Read-only contract: no function mutates the db / has a trading side-effect
# ══════════════════════════════════════════════════════════════════════════════


class _SpyDB:
    """A db that records any method call so we can assert ONLY .query was used (read-only).

    Any write-shaped call (add/commit/delete/flush/merge/execute) flips a flag.
    """

    def __init__(self, rows):
        self._rows = list(rows)
        self.wrote = False

    def query(self, *cols):
        return _FakeQuery(self._rows)

    def __getattr__(self, name):  # any other attr access is a potential write
        if name in {"add", "commit", "delete", "flush", "merge", "execute", "add_all"}:
            def _w(*a, **k):
                object.__setattr__(self, "wrote", True)
                return None

            return _w
        raise AttributeError(name)


def test_readonly_process_score_no_write(monkeypatch) -> None:
    _enable_process(monkeypatch)
    db = _SpyDB([(OUTCOME_SUCCESS, 10.0), (OUTCOME_STOP_LOSS, -4.0)])
    process_over_profits_score(db, execution_family=_EF)
    assert db.wrote is False


def test_readonly_accuracy_no_write(monkeypatch) -> None:
    db = _SpyDB([(OUTCOME_SUCCESS, 10.0)])
    challenge_metrics_accuracy_pct(db, execution_family=_EF)
    assert db.wrote is False


def test_readonly_pnl_ratio_no_write(monkeypatch) -> None:
    db = _SpyDB([(OUTCOME_SUCCESS, 10.0), (OUTCOME_STOP_LOSS, -2.0)])
    challenge_metrics_pnl_ratio(db, execution_family=_EF)
    assert db.wrote is False


# ══════════════════════════════════════════════════════════════════════════════
# Adherence weight table — lock the EXACT rule-adherence semantics
# ══════════════════════════════════════════════════════════════════════════════


def test_adherence_table_exact_weights() -> None:
    t = ms._PROCESS_ADHERENCE
    assert t[OUTCOME_SUCCESS] == 1.0
    assert t[OUTCOME_SMALL_WIN] == 1.0
    assert t[OUTCOME_TIMED_EXIT] == 1.0
    assert t[OUTCOME_STOP_LOSS] == 0.5
    assert t[OUTCOME_BAILOUT] == 0.0
    assert t[OUTCOME_GOVERNANCE_EXIT] == 0.0
