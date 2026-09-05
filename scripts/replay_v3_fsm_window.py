"""ACCURATE FSM WINDOW REPLAY — drive the REAL live FSM (``tick_live_session``) across a
symbol's recorded tape for a chosen window, so entry triggers, exits, recycle and re-entry
run FRESH (not re-priced). Highest-fidelity momentum backtest we have: reuses the ``replay_v3``
engine (``ReplayV3Driver`` / ``seed_replay_session`` / ``MockBrokerAdapter``) with the validated
$0.05-fidelity mock config, feeds the REAL per-tick printed volume so resting limit orders fill
against actual traded volume, and neutralizes the network/venue guards + re-points the schedule
and tape clocks at the SIM clock (see the monkeypatch block).

READ-ONLY on the source DB (``chili``); the throwaway sim DB is ``chili_test`` (a dedicated
seeded session + ``source='replay_v3'`` ticks, cleaned each run — never point this at prod).

100% env-config so it is a clean A/B harness — flip any live flag between two runs of the SAME
window and diff PnL / entries / escalation count:

    PYTHONPATH=. DATABASE_URL=postgresql://chili:chili@localhost:5433/chili \
    TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test CHILI_PYTEST=1 \
    DIAG=1 FULL_MIRROR=1 ARM=on SYMBOL=CELZ TICK_STRIDE=8 GRID_STEP_S=1.0 \
    WIN_START=2026-06-30T12:35:00 WIN_END=2026-06-30T14:30:00 OHLCV_START=2026-06-30T12:35:00 \
    conda run -n chili-env python scripts/replay_v3_fsm_window.py

Env knobs: SYMBOL, WIN_START/WIN_END (replayed window, UTC-naive), OHLCV_START (as-of OHLCV
warm-up), FRAME_WARMUP_MIN (minutes of pre-WIN_START tape resampled into the OHLCV frames;
default 5d = the period the runner requests live; tape-bounded), TICK_STRIDE (downsample tape
1/N), GRID_STEP_S (sim grid), FULL_MIRROR (1=full-density streaming mirror — needed for
cadence + 5m higher-low), ARM (on/off/both), DIAG/ENTRY_DIAG/GRIND_DIAG (diagnostics),
SOURCE_FILTER (comma-separated tape provenance allow-list), EXEC_FAMILY (execution rail),
REPLAY_JSON_OUT (structured per-run result), BENCH_QUESTION (arms the density invariant).

⚠️ NINE GOTCHA-LAYERS cracked for parity (do NOT remove without re-checking): schedule_window_now
real-clock -> sim clock; signed_tape_accel_features as_of -> sim clock (else the mirrored tape
reads empty vs trailing now()); RH-401 pricebook -> None; stale_bbo -> freshness_mode='wall';
OOM on tick load -> streaming server-side-cursor mirror; SQL %% -> mod(); execution_family
DEFAULTS to robinhood_agentic_mcp (not _spot) and is now EXEC_FAMILY-selectable;
NormalizedFill .price/.size (not the ORDER's average_filled_price); MTM the open position at
window-end. See project_fsm_replay_instrument.

⚠️ THE TENTH LAYER — THE NBBO MIRROR (2026-09-04). This driver mirrored the trade tape and
(since 08-26) the L2 book into the sim sink, but NEVER ``momentum_nbbo_spread_tape`` — while
the FSM reads that table DIRECTLY FROM THE SINK in at least three places:
``live_runner._build_micro_bar_df`` (:23426 — the 15 s micro-pullback frame, also the exit
block at :45492), the C1 IQFeed phantom-loss cross-check (:24335 via
``nbbo_tape.recent_bid_spread_tape``), and the adaptive spread-cost veto's rolling p50/p75/p90
spread distribution (:38885 via ``spread_cost_veto.name_spread_percentiles``). So in EVERY
replay run to date the micro-pullback detector and the spread-distribution veto read an EMPTY
table — "measuring silence", the same defect the L2 depth mirror fixed on 2026-08-26.
``mirror_nbbo_streaming`` closes it.

⚠️ AND WHAT THE MIRROR SWITCHED ON. Turning a silent read into a live one is not free: the
mirror pre-loads the WHOLE window into the sink at t=0, so every reader must be as-of bounded
or it reads the FUTURE. Two of the three were already:  ``_build_micro_bar_df`` and
``recent_bid_spread_tape`` both bind ``observed_at <= :now``. The third did NOT —
``name_spread_percentiles`` bound only ``>= :since``, and ``live_runner:38905`` reaches it
WITHOUT ``now_utc``, i.e. against the real WALL clock. MEASURED on chili_test (60 min of tape,
30 min @20 bps then 30 min @400 bps, sim-now = +35 min): p50=210.0 n=60 where the as-of answer
is p50=20.0 n=36, and end-to-end through the real ``adaptive_spread_cost_veto_derate`` the size
multiplier moved 0.70 -> 0.50 on identical input — the look-ahead was making the gate too
PERMISSIVE. Worse, on the wall clock ``since`` walks off the replayed day, so a tape older than
``chili_momentum_spread_norm_lookback_days`` (20.0) returns None and the gate fails open: the
verdict would then depend on the CALENDAR DATE the bench runs. Fixed in two places — the SQL
upper bound in ``spread_cost_veto.py`` (a no-op live, where no future rows exist) and the
sim-clock re-point in ``run_arm``, which is now a REQUIRED_SIM_CLOCK_ANCHOR so losing it fails
invariant 4 at startup.

⚠️ TIE STABILITY (2026-09-04). Every tape SELECT here now ends
``ORDER BY observed_at ASC, id ASC``, and the stride's ``row_number()`` window is ordered the
same way. Rows sharing an ``observed_at`` (routine inside a burst) previously came back in
PHYSICAL SCAN ORDER, so the same window could mirror in a different order and fill
differently — an "A/B delta" that was really a heap-layout delta. This is the ONE change here
that is not gated behind a new env var, because a nondeterministic baseline cannot be A/B'd.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

import app.services.trading.momentum_neural.replay_v3 as rv3
import app.services.trading.momentum_neural.live_runner as lr
from app.services.trading.momentum_neural import market_profile as _mp
from app.services.trading.momentum_neural import risk_evaluator as _re
from app.services.trading.momentum_neural.replay_mock_broker import FillMode
from app.services.trading.execution_family_registry import (
    normalize_execution_family,
    venue_for_execution_family,
)
from app.config import settings
from app.models.trading import TradingAutomationSession, TradingAutomationEvent

# Sibling scripts/ modules. Same sys.path shim scripts/hydration_preflight.py uses
# (~:58-61) so this runs both as `python scripts/replay_v3_fsm_window.py` and as an
# import from the repo root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from export_replay_v3_parity_fixtures import _load_bearing_payload  # noqa: E402
from hydration_canonicalize import TABLES as _CANON_TABLES, plan as _canon_plan  # noqa: E402
from replay_harness_invariants import (  # noqa: E402
    assert_as_of_reads,
    assert_clean_sink,
    assert_dense_stride,
    assert_mock_parity,
    assert_tie_stable_sql,
)

# VALIDATED parity-fixture mock config ($0.05 fidelity, replay_parity.py:219): resting limit
# orders (fill only when the recorded NBBO crosses), conservative adverse-side fills, volume-
# capped partials, wall freshness (age~0 vs the sim clock). This is the accurate setup —
# resting_limit_fills=False caused the exit-ladder submit spam. Module-level so invariant 9
# fences at STARTUP and run_arm's real mock cannot drift from what was checked.
_PARITY_MOCK_KWARGS = dict(
    resting_limit_fills=True,
    volume_cap_enabled=True,
    fill_mode=FillMode.CONSERVATIVE,
    freshness_mode="wall",
)

# READ-ONLY source DB (defaults to the local chili). SIM is the throwaway seeded DB (chili_test)
# — its name MUST end in _test as a guard against ever pointing the seeded/cleaned run at prod.
# TAPE SOURCE vs APP ENGINE (2026-09-05). ``PROD`` is the READ-ONLY tape source the mirrors
# read from. It used to be DATABASE_URL -- but DATABASE_URL is ALSO what app/db.py binds the
# process-wide engine to at import, and the Alpaca claim helpers open ``SessionLocal()`` on
# that engine (alpaca_orphan_claims.py:6923). With DATABASE_URL pointing at the tape DB, every
# committed claim ran against the hydrated research DB: ``relation
# "broker_symbol_action_claims" does not exist`` -> ``risk_ledger_unreadable`` on 19/26
# entry attempts (SDOT 2026-06-26, 2026-09-05) -- and against the LIVE ``chili`` when the
# nightly ran the same driver. ``TAPE_SOURCE_URL`` names the tape; DATABASE_URL can then be
# the sink so any app-engine session lands where the replay lives. Absent, the legacy
# reading (DATABASE_URL is the tape) is byte-identical for every existing caller.
PROD = (os.environ.get("TAPE_SOURCE_URL") or "").strip() or os.environ.get(
    "DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili"
)
SIM = os.environ.get("TEST_DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili_test")


def _sim_db_name(_url: str) -> str:
    """The DATABASE NAME a URL resolves to — never a suffix match on the whole URL.

    A raw ``endswith('_test')`` on the URL string is not a fence: it passes
    ``.../chili?application_name=chili_test`` (which connects to PROD) and it REJECTS
    the replay lane's own socket URL ``postgresql://chili:chili@/chili_test?host=...``
    (docker-compose.replay-zero-egress.yml). ``tests/conftest.py`` parses the name for
    the same reason; this uses SQLAlchemy's own parser so the two agree.
    """
    try:
        return (make_url(_url).database or "").strip()
    except Exception:
        return ""


if not _sim_db_name(SIM).endswith("_test"):
    raise SystemExit(
        "refusing to run: the TEST_DATABASE_URL database NAME must end in _test "
        f"(got {_sim_db_name(SIM)!r} from {SIM!r})"
    )

SYMBOL = os.environ.get("SYMBOL", "CELZ")
WIN_START = datetime.fromisoformat(os.environ.get("WIN_START", "2026-06-30T12:35:00"))
WIN_END = datetime.fromisoformat(os.environ.get("WIN_END", "2026-06-30T14:30:00"))
OHLCV_START = datetime.fromisoformat(os.environ.get("OHLCV_START", "2026-06-30T12:35:00"))
# P1 FRAME WARM-UP (2026-08-21 HUIZ shelf diagnostics): the OHLCV frames used to be
# resampled from ticks loaded at OHLCV_START (usually == WIN_START), so every frame started
# the window at ZERO bars — pullback_break_confirmation (10 bars) and the 25-bar
# momentum_volume ladder sat in "insufficient_bars" for most/all of the window and only the
# tick-stream fallback ever ran, while LIVE gets period="5d" frames from the providers with
# a full session (plus prior days) of depth. Frames now resample from a WIDER tick load that
# reaches back FRAME_WARMUP_MIN minutes before WIN_START (default = the 5d period the runner
# actually requests; tape-bounded — a symbol subscribed at ignition simply yields what
# exists). Printed volume, the SIM tick mirror and the driver grid are UNTOUCHED (still
# OHLCV_START/WIN_START-bounded); only the OHLCV provider seam gains depth.
FRAME_WARMUP_MIN = float(os.environ.get("FRAME_WARMUP_MIN", str(5 * 24 * 60)))
FRAME_START = min(OHLCV_START, WIN_START - timedelta(minutes=FRAME_WARMUP_MIN))
GRID_STEP_S = float(os.environ.get("GRID_STEP_S", "1.5"))
DIAG = os.environ.get("DIAG", "0") == "1"
ENTRY_DIAG = os.environ.get("ENTRY_DIAG", "0") == "1"
EQUITY = float(os.environ.get("EQUITY", "13000"))
RISK = float(os.environ.get("RISK", "130"))
TICK_STRIDE = int(os.environ.get("TICK_STRIDE", "8"))

# SOURCE_FILTER (2026-09-04) — TAPE PROVENANCE. ``counterfactual_replay.load_trade_tape`` /
# ``load_nbbo_tape`` and, until now, every read in THIS driver filtered on symbol and time
# ONLY. In ``chili_hydrated`` a symbol-day hydrated from two providers therefore returns BOTH
# tapes concatenated: measured on TMCR 2026-08-24, 16,933 ``iqfeed_lookup_hist`` rows +
# 16,933 ``polygon_v3_trades`` rows came back as 33,866 ticks — double the prints, double the
# volume, every price repeated back to back, and nothing about the rows looks malformed.
# Comma-separated; UNSET ⇒ no predicate at all ⇒ byte-identical SQL for every existing caller.
SOURCE_FILTER: tuple[str, ...] = tuple(
    s.strip() for s in os.environ.get("SOURCE_FILTER", "").split(",") if s.strip()
)

# EXEC_FAMILY (2026-09-04) — was a SILENT NO-OP. ``scripts/nightly_replay_report.py:164`` has
# been setting EXEC_FAMILY=alpaca_spot for weeks while the seed site hard-coded
# "robinhood_agentic_mcp", so every "Alpaca" replay actually ran the Robinhood agentic fill
# path. The live rail is Alpaca and the fill path differs. Normalised through the repo's own
# ``normalize_execution_family`` so an unknown/typo'd value cannot silently become a
# different rail; the DEFAULT is unchanged, so every existing caller stays byte-identical.
EXEC_FAMILY = normalize_execution_family(
    os.environ.get("EXEC_FAMILY") or "robinhood_agentic_mcp"
)

# REPLAY_JSON_OUT — structured per-run result a scorer can read. stdout is UNCHANGED.
REPLAY_JSON_OUT = (os.environ.get("REPLAY_JSON_OUT") or "").strip()
REPLAY_RESULT_SCHEMA = "chili.replay_v3_fsm_window_result.v1"

# BENCH_QUESTION — the operator's declared question for this run. Empty (the default, and
# what every existing caller passes) asserts nothing; declaring an exit/flow/bench question
# arms the density floor in replay_harness_invariants.assert_dense_stride.
BENCH_QUESTION = (os.environ.get("BENCH_QUESTION") or "").strip()


def _naive(t):
    return t.replace(tzinfo=None) if getattr(t, "tzinfo", None) else t


def _utc(t):
    """Naive-UTC -> tz-AWARE UTC. ``momentum_nbbo_spread_tape.observed_at`` is TIMESTAMPTZ
    while ``iqfeed_trade_ticks.observed_at`` is TIMESTAMP (naive UTC) — confirmed against
    information_schema. Comparing a naive bound to a timestamptz column makes PostgreSQL
    coerce it through the SESSION TimeZone, which is only harmless while that is UTC. The
    NBBO reads this driver OWNS bind aware bounds so they are correct under any session TZ."""
    return t.replace(tzinfo=timezone.utc) if getattr(t, "tzinfo", None) is None else t


# ─── TAPE SQL ────────────────────────────────────────────────────────────────────────────
# Each statement is ONE string literal that carries its FROM and its ORDER BY together, with
# the optional provenance predicate injected at ``{source}``. That shape is what lets
# replay_harness_invariants.assert_tie_stable_sql prove, from the AST alone, that every tape
# SELECT here ends ``ORDER BY observed_at ASC, id ASC``.

def source_predicate(sources: tuple[str, ...] | None = None, *, placeholder: str = ":sources") -> str:
    """``AND source = ANY(<placeholder>)`` when a filter is configured, else ``''``.

    ``placeholder`` is ``:sources`` for the SQLAlchemy reads and ``%s`` for the psycopg2
    streaming mirrors — the two halves of this driver speak different paramstyles."""
    srcs = SOURCE_FILTER if sources is None else tuple(sources)
    return f" AND source = ANY({placeholder})" if srcs else ""


_NBBO_TAPE_SQL = (
    "SELECT observed_at, bid, ask, mid FROM momentum_nbbo_spread_tape "
    "WHERE symbol=:s AND observed_at>=:a AND observed_at<:b AND bid>0 AND ask>=bid"
    "{source} "
    "ORDER BY observed_at ASC, id ASC"
)

_TRADE_TAPE_SQL = (
    "SELECT observed_at, price, size*:st AS size, bid, ask, id FROM ("
    "  SELECT id, observed_at, price, size, bid, ask, "
    "         row_number() OVER (ORDER BY observed_at ASC, id ASC) AS rn "
    "  FROM iqfeed_trade_ticks "
    "  WHERE symbol=:s AND observed_at>=:a AND observed_at<:b AND price>0"
    "{source} "
    ") q WHERE mod(rn, :st) = 0 "
    "ORDER BY observed_at ASC, id ASC"
)

_FRAME_TAPE_SQL = (
    "SELECT observed_at, price, size*:st AS size, id FROM ("
    "  SELECT id, observed_at, price, size, "
    "         row_number() OVER (ORDER BY observed_at ASC, id ASC) AS rn "
    "  FROM iqfeed_trade_ticks "
    "  WHERE symbol=:s AND observed_at>=:a AND observed_at<:b AND price>0"
    "{source} "
    ") q WHERE mod(rn, :st) = 0 "
    "ORDER BY observed_at ASC, id ASC"
)


_TRADE_MIRROR_SQL = (
    "SELECT observed_at, price, size, bid, ask, id FROM iqfeed_trade_ticks "
    "WHERE symbol=%s AND observed_at>=%s AND observed_at<%s AND price>0"
    "{source} "
    "ORDER BY observed_at ASC, id ASC"
)

# The NBBO mirror carries every column a live NBBO READER touches, not just bid/ask —
# ``spread_bps`` is what the C1 phantom-loss cross-check and the adaptive spread-cost veto's
# p50/p75/p90 distribution read, ``mid`` is what the run-up/ignition reads use, and the
# provider clocks are the freshness basis. Dropping any of them re-creates the same
# "measuring silence" failure in a narrower place (cf. ``provider_at`` on the depth mirror).
_NBBO_MIRROR_SQL = (
    "SELECT observed_at, bid, ask, mid, spread_bps, day_volume, "
    "       provider_event_at, received_at, timestamp_basis, bridge_version, "
    "       provider_trade_reference_at, message_type, bridge_run_id, "
    "       connection_generation, id "
    "FROM momentum_nbbo_spread_tape "
    "WHERE symbol=%s AND observed_at>=%s AND observed_at<%s AND bid>0 AND ask>=bid"
    "{source} "
    "ORDER BY observed_at ASC, id ASC"
)


def nbbo_tape_sql(sources: tuple[str, ...] | None = None) -> str:
    return _NBBO_TAPE_SQL.format(source=source_predicate(sources))


def trade_mirror_sql(sources: tuple[str, ...] | None = None) -> str:
    return _TRADE_MIRROR_SQL.format(source=source_predicate(sources, placeholder="%s"))


def nbbo_mirror_sql(sources: tuple[str, ...] | None = None) -> str:
    return _NBBO_MIRROR_SQL.format(source=source_predicate(sources, placeholder="%s"))


def trade_tape_sql(sources: tuple[str, ...] | None = None) -> str:
    return _TRADE_TAPE_SQL.format(source=source_predicate(sources))


def frame_tape_sql(sources: tuple[str, ...] | None = None) -> str:
    return _FRAME_TAPE_SQL.format(source=source_predicate(sources))


def tape_params(base: dict, sources: tuple[str, ...] | None = None) -> dict:
    """Bind ``:sources`` only when the predicate is actually present — an unused bind
    parameter is an error on some drivers and a lie in the run receipt on all of them."""
    srcs = SOURCE_FILTER if sources is None else tuple(sources)
    return {**base, "sources": list(srcs)} if srcs else dict(base)


# ─── PROVENANCE GUARD ────────────────────────────────────────────────────────────────────
# Reuses hydration_canonicalize's OWN day expressions, preference order and ``plan()``
# decision — the rule lives in one place. Only the SURVEY is re-scoped: that module's
# survey() groups the WHOLE table, and momentum_nbbo_spread_tape is a 26 GB / 41.8M row
# relation, so an unscoped GROUP BY here would be a full sort before a single tick loads.
#
# The predicate is ``source = ANY(<the four hydration providers>)``, exactly as
# hydration_canonicalize.survey does. That is deliberate and load-bearing: a LIVE ``chili``
# symbol-day legitimately carries several sources at once (iqfeed_l1 per-tick +
# massive_snapshot once a minute + massive_ws_universe), and those are different
# granularities, not duplicates — ``_build_micro_bar_df`` even prefers the non-snapshot rows
# explicitly. Only a DOUBLE-HYDRATED symbol-day is a duplicate, and only hydration providers
# can produce one.

def _survey_symbol_day_sources(conn, table: str, symbol: str, lo, hi) -> list[tuple]:
    """(symbol, ET day, source, rows) for ONE symbol over ONE window — the scoped form of
    hydration_canonicalize.survey, same columns, same predicate, index-friendly bounds."""
    day_expr, prefs = _CANON_TABLES[table]
    naive = table == "iqfeed_trade_ticks"  # trade tape is naive-UTC; NBBO tape is tz-aware
    _lo, _hi = (_naive(lo), _naive(hi)) if naive else (_utc(lo), _utc(hi))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT symbol, {day_expr} AS d, source, count(*) FROM {table} "
            "WHERE symbol = %s AND observed_at >= %s AND observed_at < %s "
            "AND source = ANY(%s) GROUP BY 1, 2, 3",
            (symbol, _lo, _hi, list(prefs)),
        )
        return [(s, d, src, int(n)) for s, d, src, n in cur.fetchall()]


def assert_single_hydrated_source() -> dict:
    """SystemExit if the (symbol, ET day) carries more than one hydration source in either
    tape table. Measured TMCR 2026-08-24: 33,866 ticks = 2 x 16,933, silently."""
    import psycopg2

    windows = {
        "iqfeed_trade_ticks": (FRAME_START, WIN_END),
        "momentum_nbbo_spread_tape": (WIN_START, WIN_END),
    }
    found: dict[str, dict[str, int]] = {}
    conn = psycopg2.connect(PROD)
    conn.set_session(readonly=True)
    try:
        for table, (lo, hi) in windows.items():
            rows = _survey_symbol_day_sources(conn, table, SYMBOL, lo, hi)
            found[table] = {}
            for _sym, _day, src, n in rows:
                found[table][src] = found[table].get(src, 0) + n
            drops = _canon_plan(rows, _CANON_TABLES[table][1])
            if drops:
                raise SystemExit(
                    f"  [tape] ABORT: {SYMBOL} is hydrated from MORE THAN ONE provider in "
                    f"{table} — the replay would read both tapes CONCATENATED (measured "
                    "TMCR 2026-08-24: 16,933 + 16,933 = 33,866 ticks, every print twice):\n"
                    + "\n".join(
                        f"  [tape]   {d['day']}: keep {d['keep']} ({d['kept_rows']} rows), "
                        f"drop {d['drop']} ({d['rows']} rows)" for d in drops
                    )
                    + "\n  [tape]   fix it structurally: "
                      "python scripts/hydration_canonicalize.py --apply"
                )
    finally:
        conn.close()
    return found


def load_prod():
    eng = create_engine(PROD)
    with eng.connect() as c:
        nbbo = pd.read_sql(
            text(nbbo_tape_sql()), c,
            params=tape_params({"s": SYMBOL, "a": _utc(WIN_START), "b": _utc(WIN_END)}))
        # downsample ticks at the SQL level (keep every TICK_STRIDE-th) — the full CLRO run
        # window is 200k+ ticks (OOM risk); every 8th keeps ~30k, plenty for the 1m/5m resample
        # + forward-momentum slope direction. Volume is scaled back up by the stride so the
        # micro-frame volume magnitude stays approximately right.
        # ⚠️ The row_number() window is ordered (observed_at, id): equal timestamps are routine
        # inside a burst, and an unstable window made WHICH rows survive the stride depend on
        # physical scan order.
        stride = TICK_STRIDE
        ticks = pd.read_sql(
            text(trade_tape_sql()), c,
            params=tape_params({"s": SYMBOL, "a": OHLCV_START, "b": WIN_END, "st": stride}))
        # P1 FRAME WARM-UP: the wider (stride-downsampled) load that feeds ONLY the
        # OHLCV provider, so frames carry real pre-window depth like live's 5d fetch.
        if FRAME_START < OHLCV_START:
            frame_ticks = pd.read_sql(
                text(frame_tape_sql()), c,
                params=tape_params({"s": SYMBOL, "a": FRAME_START, "b": WIN_END, "st": stride}))
        else:
            frame_ticks = ticks
    return nbbo, ticks, frame_ticks


def build_grid(nbbo):
    """Downsample the recorded NBBO to ~GRID_STEP_S spacing -> the driver grid."""
    grid, last_t = [], None
    for _, r in nbbo.iterrows():
        t = _naive(pd.Timestamp(r["observed_at"]).to_pydatetime())
        if last_t is not None and (t - last_t).total_seconds() < GRID_STEP_S:
            continue
        last_t = t
        grid.append(rv3.RecordedNbboTick(ts=t, bid=float(r["bid"]), ask=float(r["ask"]),
                                         last=float(r["mid"]) if pd.notna(r["mid"]) else None))
    return grid


def build_printed_volume(grid, ticks):
    """Per-grid-tick printed volume = sum of trade-tick sizes in (prev_tick, this_tick].
    Feeds the mock's volume-cap fill model so resting limit orders fill against the REAL
    printed volume that traded in each window (the validated parity-fixture approach)."""
    if ticks.empty:
        return {}
    tv = ticks.copy()
    tv["observed_at"] = pd.to_datetime(tv["observed_at"]).map(_naive)
    tv = tv.sort_values("observed_at")
    vol = {}
    prev = None
    for gt in grid:
        if prev is None:
            lo = gt.ts - timedelta(seconds=GRID_STEP_S)
        else:
            lo = prev
        m = (tv["observed_at"] > lo) & (tv["observed_at"] <= gt.ts)
        vol[gt.ts] = float(tv.loc[m, "size"].sum()) if m.any() else 0.0
        prev = gt.ts
    return vol


def mirror_ticks(db, ticks):
    """Legacy in-memory mirror (downsampled ticks). Kept for the fallback path."""
    if ticks.empty:
        return 0
    ins = text("INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask, source) "
               "VALUES (:sym,:at,:px,:sz,:bid,:ask,'replay_v3')")
    rows = [{"sym": SYMBOL, "at": _naive(pd.Timestamp(r["observed_at"]).to_pydatetime()),
             "px": float(r["price"]), "sz": float(r["size"]) if pd.notna(r["size"]) else 0.0,
             "bid": float(r["bid"]) if pd.notna(r["bid"]) else None,
             "ask": float(r["ask"]) if pd.notna(r["ask"]) else None} for _, r in ticks.iterrows()]
    for i in range(0, len(rows), 5000):
        db.execute(ins, rows[i:i+5000])
    db.flush()
    return len(rows)


def mirror_ticks_streaming(sim_engine):
    """FULL-DENSITY mirror WITHOUT loading all ticks into memory.

    GOTCHA 11 (2026-08-20): the original single server-side cursor held ONE
    read-only transaction on the SOURCE for the whole minutes-long mirror, and
    its ``query_start`` never advanced — so the window app's db_watchdog
    (kills idle-in-transaction > 10 min measured from query_start) TERMINATED
    the mirror mid-run twice ("administrator command"). The mirror now walks
    the window in 5-minute time slices, each in its OWN short transaction on
    both ends (commit after every slice), so no connection ever shows an old
    idle transaction. Same rows, same order (slices are contiguous and each is
    ORDER BY observed_at ASC), bounded memory per slice.
    """
    import psycopg2
    from psycopg2.extras import execute_values as _ev
    from datetime import timedelta as _td
    src = psycopg2.connect(PROD)
    src.set_session(readonly=True)
    dst = sim_engine.raw_connection()
    dcur = dst.cursor()
    # GOTCHA 11b (2026-08-20): row-by-row executemany took 15+ min for 164k
    # rows — long enough for an as-yet-unidentified backend terminator to kill
    # the mirror four times ("administrator command", no janitor logged it).
    # execute_values batches the VALUES lists (~10-50x faster), so the whole
    # mirror finishes in well under a minute and outruns whatever kills
    # long-lived replay connections.
    ins = ("INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask, source) "
           "VALUES %s")
    total = 0
    slice_start = OHLCV_START
    while slice_start < WIN_END:
        slice_end = min(slice_start + _td(minutes=5), WIN_END)
        scur = src.cursor()
        _args = [SYMBOL, slice_start, slice_end]
        if SOURCE_FILTER:
            _args.append(list(SOURCE_FILTER))
        scur.execute(trade_mirror_sql(), tuple(_args))
        batch = scur.fetchall()
        scur.close()
        src.commit()  # isara ang read tx — sariwa ang query_start sa susunod
        if batch:
            rows = [(SYMBOL, r[0], float(r[1]), float(r[2] or 0), r[3], r[4], 'replay_v3') for r in batch]
            _ev(dcur, ins, rows, page_size=5000)
            dst.commit()  # maiksi ang bawat SIM tx din
            total += len(rows)
            del rows
        del batch
        slice_start = slice_end
    dcur.close(); dst.close()
    src.close()
    return total


def mirror_nbbo_streaming(sim_engine):
    """FULL-DENSITY mirror ng NBBO TAPE — ang IKATLONG nawawalang kalahati ng harness.

    ⚠️ BAKIT ITO UMIIRAL (2026-09-04). Ang driver na ito ay nagmi-mirror ng TRADE tape
    (:513) at, mula 08-26, ng L2 DEPTH (:516) papasok sa sink — pero KAILANMAN ay hindi
    ng ``momentum_nbbo_spread_tape``. Ang NBBO ay ginagamit lang bilang GRID (in-memory,
    mula sa ``load_prod``). Samantala ang FSM ay bumabasa ng table na iyon NANG DIREKTA
    MULA SA SINK sa hindi bababa sa TATLONG lugar::

        live_runner._build_micro_bar_df        :23426  (15 s micro-pullback frame;
                                                        ginagamit din ng exit block :45492)
        live_runner._c1_iqfeed_phantom_loss    :24335  (via nbbo_tape.recent_bid_spread_tape)
        spread_cost_veto.name_spread_percentiles :38885 (rolling p50/p75/p90 spread)

    Kaya sa BAWAT replay hanggang ngayon, ang micro-pullback detector at ang
    spread-distribution veto ay bumabasa ng WALANG LAMAN na table — "sumusukat ng
    katahimikan", ang EKSAKTONG depektong inayos ng L2 depth mirror noong 2026-08-26.

    ⚠️ ASIMETRIYA NG ORASAN. ``iqfeed_trade_ticks.observed_at`` ay TIMESTAMP (naive UTC)
    habang ``momentum_nbbo_spread_tape.observed_at`` ay TIMESTAMPTZ (nakumpirma sa
    information_schema). Ang mga hangganan ng slice dito ay ginagawang tz-AWARE UTC bago
    i-bind; ang mga hilerang binabasa ay tz-aware na at isinusulat nang BERBATIM, kaya ang
    parehong instant ang nakikita ng FSM na nakikita ng live.

    ⚠️ KAPAREHONG DALAWANG GOTCHA ng tick mirror, at sinasadya iyon:
      * GOTCHA 11  — 5-minutong slice, bawat isa sa SARILING maikling transaction sa
        magkabilang dulo (db_watchdog pumapatay ng >10 min mula query_start).
      * GOTCHA 11b — ``execute_values`` na batched, hindi row-by-row executemany.

    ⚠️ DALA ANG BUONG HANAY NA BINABASA: ``spread_bps`` (C1 + ang p50/p75/p90 veto),
    ``mid`` (run-up/ignition), ``day_volume``, at ang provider clocks. Kung may nawawala,
    tahimik na magiging None ang gate at muli tayong susukat ng katahimikan.
    """
    import psycopg2
    from psycopg2.extras import execute_values as _ev
    from datetime import timedelta as _td
    src = psycopg2.connect(PROD)
    src.set_session(readonly=True)
    dst = sim_engine.raw_connection()
    dcur = dst.cursor()
    ins = (
        "INSERT INTO momentum_nbbo_spread_tape "
        "(symbol, observed_at, bid, ask, mid, spread_bps, day_volume, source, "
        " provider_event_at, received_at, timestamp_basis, bridge_version, "
        " provider_trade_reference_at, message_type, bridge_run_id, connection_generation) "
        "VALUES %s"
    )
    total = 0
    slice_start = OHLCV_START
    while slice_start < WIN_END:
        slice_end = min(slice_start + _td(minutes=5), WIN_END)
        scur = src.cursor()
        _args = [SYMBOL, _utc(slice_start), _utc(slice_end)]
        if SOURCE_FILTER:
            _args.append(list(SOURCE_FILTER))
        scur.execute(nbbo_mirror_sql(), tuple(_args))
        batch = scur.fetchall()
        scur.close()
        src.commit()  # isara ang read tx — sariwa ang query_start sa susunod
        if batch:
            rows = [
                (SYMBOL, r[0], r[1], r[2], r[3], r[4], r[5], 'replay_v3',
                 r[6], r[7], r[8], r[9], r[10], r[11], r[12], r[13])
                for r in batch
            ]
            _ev(dcur, ins, rows, page_size=5000)
            dst.commit()  # maiksi ang bawat SIM tx din
            total += len(rows)
            del rows
        del batch
        slice_start = slice_end
    dcur.close(); dst.close()
    src.close()
    return total


def mirror_depth_streaming(sim_engine):
    """FULL-DENSITY mirror ng L2 DEPTH -- ang nawawalang kalahati ng harness na ito.

    ⚠️ BAKIT ITO UMIIRAL (2026-08-26). Ang replay ay nagmi-mirror ng TRADE tape at
    NBBO, pero KAILANMAN ay hindi ng libro. Kaya ang buong pamilya ng exit lever na
    nagbabasa ng depth ay HINDI MASUSUKAT dito -- at LAHAT ng natitirang naka-OFF na
    exit lever ay nasa pamilyang iyon::

        exit_ladder_live             (ang "Ross ladder read")
        exit_ofi_hidden_seller_enabled
        exit_ofi_lock_partial_enabled
        exit_candle_confirm_live

    Napatunayan: ang isang A/B ng `exit_ladder_live` 0 laban sa 1 sa XPON 08-24 ay
    nagbigay ng EKSAKTONG parehong fill at parehong -67.74 -- ang flag ay walang
    kayang basahin, kaya wala itong magagawa.

    ANG DEADLOCK NA BINABASAG NITO: ang exit ladder ay naka-OFF sa live dahil
    "naghihintay ng A/B proof", at ang A/B harness ay hindi makapagbigay ng proof
    dahil walang depth. Nananatili itong naka-off nang hindi dahil pumalpak kundi
    dahil walang paraan para subukan -- habang ang nasukat na capture ratio ay
    18.5% (+108.35R na naabot, +20.02R lang ang nakuha sa 5 araw).

    ⚠️ KAPAREHONG DALAWANG GOTCHA ng tick mirror, at sinasadya iyon:
      * GOTCHA 11  -- 5-minutong slice, bawat isa sa SARILING maikling transaction
        sa magkabilang dulo, kaya walang koneksyon na nagpapakita ng lumang
        idle transaction sa db_watchdog (na pumapatay ng >10 min mula query_start).
      * GOTCHA 11b -- `execute_values` na batched, hindi row-by-row executemany.

    ⚠️ DALA ANG `provider_at` (migration 371) -- ang quote-event clock. Kung wala
    ito, ang anumang gate na sumusuri ng freshness ng libro ay makakakita ng NULL
    at fail-closed, at muli tayong susukat ng katahimikan sa halip na gawi.
    """
    import psycopg2
    from psycopg2.extras import execute_values as _ev, Json as _Json
    from datetime import timedelta as _td
    src = psycopg2.connect(PROD)
    src.set_session(readonly=True)
    dst = sim_engine.raw_connection()
    dcur = dst.cursor()
    ins = (
        "INSERT INTO iqfeed_depth_snapshots "
        "(symbol, observed_at, bid_top, ask_top, bid_top_size, ask_top_size, "
        " bid5_size, ask5_size, imbalance5, venues, source, bids_json, asks_json, provider_at) "
        "VALUES %s"
    )
    total = 0
    slice_start = OHLCV_START
    while slice_start < WIN_END:
        slice_end = min(slice_start + _td(minutes=5), WIN_END)
        scur = src.cursor()
        # ⚠️ INLINE ON PURPOSE: tests/test_replay_mirrors_l2_depth.py reads THIS FUNCTION'S
        # BODY to prove every column the depth-reading exit levers need is carried
        # (provider_at, imbalance5, bid5/ask5...). Hoisting it to a module constant would
        # pass the tests vacuously.
        scur.execute(
            "SELECT observed_at, bid_top, ask_top, bid_top_size, ask_top_size, "
            "       bid5_size, ask5_size, imbalance5, venues, bids_json, asks_json, "
            "       provider_at, id "
            "FROM iqfeed_depth_snapshots "
            "WHERE symbol=%s AND observed_at>=%s AND observed_at<%s "
            "  AND bid_top>0 AND ask_top>0 "
            "ORDER BY observed_at ASC, id ASC",
            (SYMBOL, slice_start, slice_end))
        batch = scur.fetchall()
        scur.close()
        src.commit()  # isara ang read tx -- sariwa ang query_start sa susunod
        if batch:
            rows = [
                # ⚠️ Ang bids_json/asks_json ay JSONB. Ibinabalik sila ng psycopg2
                # bilang Python list, at kung walang balot ay iniaadapt niya sila
                # bilang PG ARRAY -> DatatypeMismatch. Ang Json() ang nagsasabi
                # sa driver na jsonb ang patutunguhan.
                (SYMBOL, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                 'replay_v3',
                 _Json(r[9]) if r[9] is not None else None,
                 _Json(r[10]) if r[10] is not None else None,
                 r[11])
                for r in batch
            ]
            _ev(dcur, ins, rows, page_size=5000)
            dst.commit()
            total += len(rows)
            del rows
        del batch
        slice_start = slice_end
    dcur.close(); dst.close()
    src.close()
    return total


class AsOfProvider:
    """As-of-t OHLCV from real ticks (no lookahead) — reads the sim clock via lr._utcnow().

    P1 PRODUCTION-PARITY FRAMES (2026-08-21 HUIZ shelf diagnostics), two fixes:
      * DEPTH — construct with the FRAME-warm-up tick load (see FRAME_WARMUP_MIN) so the
        1m/5m/15m frames carry the pre-window session depth live gets from period="5d",
        instead of starting the window at zero bars (insufficient_bars everywhere).
      * INDEX — keep the naive-UTC DatetimeIndex on the served frame. Live provider frames
        are datetime-indexed; reset_index(drop=True) silently disabled every timestamp read
        downstream — the forming-bar elapsed fraction (the 08-19 YJ volume-rate fix), the
        _today_session_frame session slice, bar-width detection — all fell back to their
        degraded paths ONLY in replay. As-of safety is unchanged (index <= sim-now)."""
    def __init__(self, ticks):
        t = ticks.copy()
        if not t.empty:
            t["observed_at"] = pd.to_datetime(t["observed_at"])
            t = t.set_index("observed_at").sort_index()
        self._t = t
        self._rule = {"15m": "15min", "5m": "5min", "1m": "1min", "1d": "1D"}
        self._cache = {}

    def __call__(self, ticker, *, interval="1d", period="6mo"):
        now = _naive(lr._utcnow())
        rule = self._rule.get(str(interval), "5min")
        ck = (str(interval), int(now.timestamp() // 60))
        if ck in self._cache:
            return self._cache[ck].copy()
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        if self._t.empty:
            return empty
        sl = self._t[self._t.index <= now]
        if sl.empty:
            return empty
        o = sl["price"].resample(rule).ohlc()
        v = sl["size"].resample(rule).sum()
        bars = o.join(v.rename("Volume")).dropna()
        if bars.empty:
            return empty
        bars = bars.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
        bars = bars[["Open", "High", "Low", "Close", "Volume"]]
        self._cache[ck] = bars
        return bars.copy()


# ─── STRUCTURED RESULT (REPLAY_JSON_OUT) ─────────────────────────────────────────────────
# The driver used to emit stdout ONLY, so nothing downstream could score a run without
# re-parsing prose. This writes one machine-readable receipt per arm — the env contract it
# actually ran under, the measured tape density, the mock config, and the ordered decisions —
# BEFORE the end-of-arm cleanup deletes the evidence. stdout is UNCHANGED.

# Payload facts the bench scorer needs on top of the parity fixture's load-bearing set
# (``_load_bearing_payload``): WHY a decision went the way it did, and what it was measured
# against. Everything else in a payload stays out, so the receipt is stable across releases.
_BENCH_PAYLOAD_KEYS = (
    "reason", "blocked_trigger", "benched_at_hod", "trigger", "viability_score", "errors",
    # 2026-09-04: the SDOT alpaca receipt showed ``live_entry_blocked_by_breaker`` x26 with
    # payload ``{}`` -- the runner had written breaker=daily_loss_cap_broker, family,
    # daily_pnl_usd, max_daily_loss_usd and this filter dropped every one of them; the
    # sink row had to be read by hand to name the gate. Breaker / blocker attribution is
    # exactly the "WHY" this receipt exists to carry.
    "breaker", "family", "dd_reason", "daily_pnl_usd", "max_daily_loss_usd", "transient",
    "source", "error_type", "detail", "skipped",
    # 2026-09-05: the alpaca sweep receipts carried 1,775 x ``live_blocked_by_risk
    # wide_bbo_spread`` per case as ``{reason, bid, ask}`` -- the cap it was measured
    # against (max_spread_bps / expected_move_bps / spread_bps) and the deadman's pending
    # state (client_order_id / broker_error) were dropped, so WHICH cap bound (the 12-bps
    # floor with no expected move on a held tick) had to be reconstructed from source.
    "spread_bps", "max_spread_bps", "expected_move_bps", "median_spread_bps", "samples",
    "effective_spread_bps", "bid", "ask", "mid", "rescued_from", "failed_check",
    "client_order_id", "broker_error", "owner_transport_advanced", "phase", "session_state",
    "target_price", "position_quantity",
    # 2026-09-05 gate #11: ``live_deadman_stop_release_blocked`` carries WHY in ``error``
    # (deadman_cancel_unsupported / _pre_cancel_truth_unknown / ...); ``errors`` (plural) was
    # whitelisted, ``error`` was not, and the block repeated 2,590 times per case unnamed.
    "error", "deadman_order_id", "deadman_client_order_id", "frozen_order_type",
    "superseding_order_type", "handoff_token",
)


def _bench_payload(event_type: str, payload: dict) -> dict:
    p = payload or {}
    keep = dict(_load_bearing_payload(str(event_type), p))
    for k in _BENCH_PAYLOAD_KEYS:
        if k in p:
            keep[k] = p[k]
    return keep


def _tree_sha() -> dict:
    """The tree that RAN. A stale build tree once produced a fake 'fix works' result, so the
    receipt carries HEAD + the tree object, not a branch name."""
    out = {}
    for key, args in (("head", ["rev-parse", "HEAD"]),
                      ("tree", ["rev-parse", "HEAD^{tree}"]),
                      ("branch", ["rev-parse", "--abbrev-ref", "HEAD"])):
        try:
            proc = subprocess.run(["git", "-C", _REPO] + args,
                                  capture_output=True, text=True, check=False)
            out[key] = proc.stdout.strip() if proc.returncode == 0 else None
        except Exception:
            out[key] = None
    try:
        proc = subprocess.run(["git", "-C", _REPO, "status", "--porcelain"],
                              capture_output=True, text=True, check=False)
        out["dirty"] = bool(proc.stdout.strip()) if proc.returncode == 0 else None
    except Exception:
        out["dirty"] = None
    return out


def _env_contract() -> dict:
    """Echo back EVERY knob that shapes the run. A result whose inputs are not recorded
    cannot be re-run, and an A/B whose arms differ in an unrecorded knob is not an A/B."""
    return {
        "SYMBOL": SYMBOL,
        "WIN_START": WIN_START.isoformat(),
        "WIN_END": WIN_END.isoformat(),
        "OHLCV_START": OHLCV_START.isoformat(),
        "FRAME_WARMUP_MIN": FRAME_WARMUP_MIN,
        "FRAME_START": FRAME_START.isoformat(),
        "TICK_STRIDE": TICK_STRIDE,
        "GRID_STEP_S": GRID_STEP_S,
        "EQUITY": EQUITY,
        "RISK": RISK,
        "SOURCE_FILTER": list(SOURCE_FILTER),
        "EXEC_FAMILY": EXEC_FAMILY,
        "BENCH_QUESTION": BENCH_QUESTION,
        "FULL_MIRROR": os.environ.get("FULL_MIRROR", "1"),
        "ARM": os.environ.get("ARM", "both"),
        "MAXLOSS_USD": os.environ.get("MAXLOSS_USD"),
        "GRIND_FIX": os.environ.get("GRIND_FIX"),
        "REPLAY_KEEP_SINK": os.environ.get("REPLAY_KEEP_SINK"),
        "PROD_DB": _sim_db_name(PROD),
        "SIM_DB": _sim_db_name(SIM),
    }


def _mock_config(mock) -> dict:
    out = {}
    for key in ("resting_limit_fills", "volume_cap_enabled", "fill_mode", "freshness_mode",
                "volume_participation_frac", "max_age_seconds", "slippage_bps",
                "ack_delay_ticks", "partial_first_fill"):
        for cand in (key, f"_{key}"):
            if hasattr(mock, cand):
                out[key] = getattr(mock, cand)
                break
    return out


def _json_out_path(g4_on: bool):
    """One receipt per ARM. ARM=both runs the same window twice, so the two receipts must
    not overwrite each other — a single file would silently keep only the second arm."""
    if not REPLAY_JSON_OUT:
        return None
    if os.environ.get("ARM", "both") in ("on", "off"):
        return REPLAY_JSON_OUT
    base, ext = os.path.splitext(REPLAY_JSON_OUT)
    return f"{base}.g4_{'on' if g4_on else 'off'}{ext or '.json'}"


def _write_run_json(path, doc) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    # newline="" — Windows text mode would otherwise rewrite every \n to \r\n and change
    # the bytes of an otherwise identical receipt (reference_python_write_text_crlf_windows).
    with open(path, "w", newline="", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, default=str)


def run_arm(label, grid, ticks, frame_ticks, g4_on, *, sink_reset=None, tape_sources=None):
    """Seed a fresh queued_live CLRO session + real ticks in SIM, run the REAL FSM over the
    grid with G4 flags on/off, mine the fills -> PnL + the grind/escalation event evidence."""
    settings.chili_momentum_g4_grind_exit_enabled = g4_on
    settings.chili_momentum_g4_reentry_escalation_enabled = g4_on
    settings.chili_momentum_live_runner_enabled = True
    # neutralize env-coupled gates orthogonal to the exit A/B (kill switch / broker-connectivity
    # / tradeable-now wall-clock) so the run isn't blocked by ops state — these do NOT touch the
    # entry-trigger geometry or the G4 exit machinery under test (same neutralization the
    # bespoke replay_v3_upc driver uses).
    lr._venue_broker_connected = lambda ef: True
    lr.is_kill_switch_active = lambda: False
    _re.is_kill_switch_active = lambda: False
    _re.get_kill_switch_status = lambda: {"active": False, "reason": None}
    _mp.is_tradeable_now = lambda symbol, **k: True
    # network guards: kill the real-venue marketdata reads the pending-entry / quote paths make
    # (the RH pricebook 401 that stalled the fill), and force all bars/quotes through the mock +
    # the replay OHLCV provider. Same set the bespoke replay_v3_upc driver installs.
    import app.services.trading.market_data as _md
    import app.services.trading.momentum_neural.universe as _uni
    import app.services.trading.momentum_neural.entry_features as _ef
    def _boom_fetch(*a, **k):
        raise AssertionError("NETWORK GUARD: real fetch_ohlcv_df during replay")
    def _boom_adapter(*a, **k):
        raise AssertionError("NETWORK GUARD: real live-spot adapter during replay")
    _md.fetch_ohlcv_df = _boom_fetch
    lr.resolve_live_spot_adapter_factory = _boom_adapter
    lr._entry_pricebook_snapshot = lambda symbol: None          # the RH-401 source
    lr._refetch_bbo_secondary = lambda symbol: None
    _uni.snapshot_dollar_volumes = lambda syms: {}
    _ef.macro_regime_features = lambda *a, **k: {}
    # GOTCHA 10 (2026-08-19): the shelf-registration damper primes its cache from
    # SEC EDGAR over HTTP. In a live lane that is a watch-start daemon thread, but
    # inside ReplayV3 the network guard blocks it, the module swallows the raised
    # ReplayNetworkAccessError, and the driver then aborts the whole run on the
    # bumped attempt_count — which is what broke this harness entirely:
    #   prime_shelf_cache -> _fetch_state -> _load_ticker_map -> _http_get_json
    # Neutralise the PRIMER only. cached_shelf_state stays untouched: it is
    # cache-only by construction, so it correctly returns "unknown" here and the
    # damper fails open exactly as it does live on a cold cache.
    try:
        from app.services.trading.momentum_neural import shelf_registration as _shelf

        _shelf.prime_shelf_cache = lambda *a, **k: None
    except Exception:
        pass
    # THE placement blocker: schedule_window_now() defaults to datetime.now() (REAL wall-clock),
    # so during replay it returns "afterhours/closed" => sched_mult 0.0 => entry placement is
    # SKIPPED (live_entry_wait_late_window). Re-point it at the SIM clock (lr._utcnow(), frozen
    # to the replay instant by the driver's replay_clock) so it returns the window that was
    # ACTUALLY in effect at the recorded tick (CLRO 16:16Z = 12:16 ET = midday).
    _orig_swn = _mp.schedule_window_now
    _mp.schedule_window_now = lambda now=None, _o=_orig_swn: _o(now if now is not None else lr._utcnow())
    # TAPE AS-OF FIX (buyers-confirm validation): the entry-gate tape reads
    # (signed_tape_accel_features -> tape_confirms_hold / buyers_confirmed) use as_of=None in
    # tick_live_session, which in LIVE means the trailing real now(). In replay the mirrored ticks
    # live at the RECORDED instant, so a trailing-now() read finds NOTHING (empty -> fail-closed,
    # which would just DISABLE the gated touch triggers rather than test real buyer presence).
    # Re-point the tape read at the SIM clock (lr._utcnow()) when as_of is None so the buyers gate
    # reads the ACTUAL executed tape at the replayed instant — accurate buyers-confirmation.
    import app.services.trading.momentum_neural.entry_gates as _eg
    _orig_staf = _eg.signed_tape_accel_features
    def _staf_simclock(symbol, *, db=None, window_s=None, as_of=None, _o=_orig_staf):
        return _o(symbol, db=db, window_s=window_s,
                  as_of=(as_of if as_of is not None else lr._utcnow()))
    _eg.signed_tape_accel_features = _staf_simclock
    # P1 FORMING-BAR CLOCK (2026-08-21): _forming_bar_elapsed_fraction measures how much of
    # the LAST bar has elapsed via entry_gates._utcnow_for_bars = the REAL wall clock. In
    # replay the frame's last bar sits at the RECORDED instant, so real-now reads every bar
    # as long complete and the volume-rate normalization (the 08-19 YJ measurement fix)
    # silently disables — replay then re-runs the exact bug live was cured of. Re-point it
    # at the SIM clock (naive UTC, the same awareness as the recorded frame index).
    _eg._utcnow_for_bars = lambda sample: lr._utcnow()
    # SPREAD-DISTRIBUTION AS-OF (2026-09-04): the NBBO mirror above pre-loads the WHOLE
    # window into the sink at t=0, which switches ON a read that was previously dormant
    # against an empty table — spread_cost_veto.name_spread_percentiles, reached from
    # live_runner:38905 WITHOUT now_utc, i.e. against the REAL wall clock. Two failures
    # follow: (a) `since` walks off the replayed day, so a day older than
    # chili_momentum_spread_norm_lookback_days (20.0) reads NOTHING and the gate fails
    # open — the gate's answer would depend on how many days after the tape the bench
    # happens to run, which defeats the double-run premise this harness asserts
    # elsewhere; (b) even WITH now_utc the SQL had no upper bound (fixed in
    # spread_cost_veto.py) so it read the future. Re-point at the SIM clock with the
    # same idiom as the two anchors above; this also sets the function's own _as_of
    # flag, which correctly disables its WALL-CLOCK-indexed percentile cache for a
    # historical read (its docstring warns that serving a replay from that cache feeds
    # live-time distribution into an as-of decision).
    import app.services.trading.momentum_neural.spread_cost_veto as _scv
    _orig_nsp = _scv.name_spread_percentiles
    def _nsp_simclock(db, symbol, *, now_utc=None, _o=_orig_nsp, **k):
        return _o(db, symbol, now_utc=(now_utc if now_utc is not None else lr._utcnow()), **k)
    _scv.name_spread_percentiles = _nsp_simclock
    # PROPOSED G4 FIX validation (GRIND_FIX=1): align grind ACTIVATION with MAINTENANCE + the
    # classifier's own semantics — accept UNCERTAIN cadence (which the classifier defaults to
    # "FAST/normal, no modulation" and which maintenance keeps as "NOT SLOW_CHOPPER"). Only
    # SLOW_CHOPPER / None still block. Implemented by promoting UNCERTAIN->FAST for the grind
    # decision only. All the OTHER strict gates (leader/1R/higher-low/EMA/floor/VWAP) unchanged.
    if os.environ.get("GRIND_FIX") == "1":
        import app.services.trading.momentum_neural.live_runner as _lr3
        _gmd0 = _lr3.grind_mode_decision
        def _gmd_fixed(*a, _o=_gmd0, **k):
            if str(k.get("cadence_cls") or "") == "UNCERTAIN":
                k = {**k, "cadence_cls": "FAST"}
            return _o(*a, **k)
        _lr3.grind_mode_decision = _gmd_fixed
    # GRIND DIAGNOSTIC: wrap grind_mode_decision to record WHY it (doesn't) activate on the
    # trailing ticks — the histogram of reasons + the max peak_r seen tells us if P1 should
    # have engaged (a real grind that grind-mode missed) or correctly stayed off.
    if os.environ.get("GRIND_DIAG") == "1":
        import app.services.trading.momentum_neural.live_runner as _lr2
        _grind_reasons = run_arm.__dict__.setdefault("_grind_reasons", {})
        _orig_gmd = _lr2.grind_mode_decision
        def _gmd_spy(*a, _o=_orig_gmd, **k):
            r = _o(*a, **k)
            try:
                key = f"{bool(r.get('active'))}:{r.get('reason')}"
                _grind_reasons[key] = _grind_reasons.get(key, 0) + 1
                pr = r.get("peak_r")
                if pr is not None:
                    _grind_reasons["_max_peak_r"] = max(_grind_reasons.get("_max_peak_r", -9), float(pr))
                _grind_reasons["_is_leader_true"] = _grind_reasons.get("_is_leader_true", 0) + (1 if k.get("is_day_leader") else 0)
                _cc = f"cadence={k.get('cadence_cls')!r}"
                _grind_reasons[_cc] = _grind_reasons.get(_cc, 0) + 1
            except Exception:
                pass
            return r
        _lr2.grind_mode_decision = _gmd_spy

    eng = create_engine(SIM)
    Sess = sessionmaker(bind=eng)
    db = Sess()
    # clean any prior replay_v3 ticks + stale seeded CLRO sessions
    db.execute(text("DELETE FROM iqfeed_trade_ticks WHERE source='replay_v3' AND symbol=:s"), {"s": SYMBOL})
    db.execute(text("DELETE FROM momentum_nbbo_spread_tape WHERE source='replay_v3' AND symbol=:s"), {"s": SYMBOL})
    db.commit()

    arm = rv3.RecordedArm(symbol=SYMBOL, live_eligible_at_utc=WIN_START.isoformat(),
                          viability_score=0.9, atr_pct=0.05)
    # EXEC_FAMILY (2026-09-04): was hard-coded here, so nightly_replay_report.py's
    # EXEC_FAMILY=alpaca_spot has been silently ignored for weeks while the live rail IS
    # Alpaca and the fill path differs. seed_replay_session derives the venue itself
    # (venue_for_execution_family) and requires an account identity for the NON-Alpaca
    # families only — RecordedArm already carries REPLAY_MOCK_ACCOUNT_IDENTITY, which the
    # seeder writes under NON_ALPACA_ACCOUNT_IDENTITY_KEY for RH/Coinbase and correctly
    # omits for alpaca_spot/alpaca_short. Default unchanged ⇒ existing callers identical.
    seed = rv3.seed_replay_session(db, arm=arm, execution_family=EXEC_FAMILY)
    # MAXLOSS_USD: experiment knob — the diagnostic seed freezes LEGACY_DIAGNOSTIC_POLICY_CAPS
    # ($50/trade) into the session snapshot; no setting reaches it. Rewriting the frozen cap
    # post-seed is the only lever that scales per-trade size without touching equity. Results
    # stay non-certifying (legacy_config_diagnostic) — dollar-scale exploration only.
    _maxloss_env = os.environ.get("MAXLOSS_USD")
    if _maxloss_env:
        _sess = db.get(TradingAutomationSession, seed.session_id)
        _rs = dict(_sess.risk_snapshot_json or {})
        _caps = dict(_rs.get("momentum_policy_caps") or {})
        _caps["max_loss_per_trade_usd"] = float(_maxloss_env)
        _rs["momentum_policy_caps"] = _caps
        _sess.risk_snapshot_json = _rs
        db.commit()
        print(f"[harness] MAXLOSS_USD override: frozen max_loss_per_trade_usd -> {float(_maxloss_env)}")
    # FULL-density streaming mirror (cadence + 5m higher-low need real tick density); falls back
    # to the in-memory downsampled mirror only if FULL_MIRROR=0.
    mirrored_depth = 0
    mirrored_nbbo = 0
    if os.environ.get("FULL_MIRROR", "1") == "1":
        db.commit()  # commit the seed first (streaming mirror uses its own raw connection)
        mirrored = mirror_ticks_streaming(eng)
        # ANG NBBO TAPE (2026-09-04). Walang ito, ang micro-pullback frame
        # (_build_micro_bar_df) at ang spread-distribution veto ay bumabasa ng WALANG
        # LAMAN na table — sumusukat ng katahimikan, gaya ng libro bago ang 08-26.
        mirrored_nbbo = mirror_nbbo_streaming(eng)
        print("  mirrored_nbbo_rows=%s" % mirrored_nbbo)
        # ANG LIBRO (2026-08-26). Walang ito, ang bawat depth-reading na exit lever
        # ay tahimik na no-op at ang A/B ay sumusukat ng katahimikan.
        mirrored_depth = mirror_depth_streaming(eng)
        print("  mirrored_depth_rows=%s" % mirrored_depth)
    else:
        mirrored = mirror_ticks(db, ticks)
        # ⚠️ The NBBO mirror runs here TOO. The silence defect is not conditional on tick
        # density: FULL_MIRROR=0 downsamples the TRADE tape, it does not mean the FSM's
        # micro-pullback frame and spread-distribution veto should read an empty table.
        db.commit()  # the streaming mirror uses its own raw connection
        mirrored_nbbo = mirror_nbbo_streaming(eng)
        print("  mirrored_nbbo_rows=%s" % mirrored_nbbo)
    db.commit()

    # The VALIDATED parity-fixture config, defined once at _PARITY_MOCK_KWARGS.
    mock = rv3.MockBrokerAdapter(**_PARITY_MOCK_KWARGS)
    # The account identity the seeded session froze (Alpaca families only), applied
    # through the mock's own identity seam so the runner's bind_account_id at the broker
    # boundary sees the same string on both sides. NOT a constructor kwarg: the parity
    # string above is pinned verbatim by test_the_startup_mock_and_the_run_mock_cannot_drift
    # and invariant 9 below reads the FILL config off the instance, not identity.
    rv3.apply_replay_mock_identity(mock, EXEC_FAMILY)
    # The sim account's start-of-day equity: the paper daily-loss gate reads
    # equity - last_equity from the broker, and the mock answers from its own book.
    mock.set_account_equity(EQUITY)
    # INVARIANT 9, FAIL CLOSED: prove the mock the run will actually fill against IS the
    # validated config, read back off the instance — a constructor argument that a later
    # edit stops honouring produces a PnL nobody can compare to a baseline.
    assert_mock_parity(mock)
    # Feed the REAL per-tick printed volume so resting orders fill against actual traded volume
    # (the ReplayV3Driver sets clock+quote but NOT printed_volume — parity mode_i does; without
    # it, resting limits never fill). Wrap set_quote: after each quote, feed the bucket volume.
    _vol_by_ts = build_printed_volume(grid, ticks)
    _orig_set_quote = mock.set_quote
    def _set_quote_and_vol(pid, q, _o=_orig_set_quote):
        _o(pid, q)
        try:
            _t = mock._clock
            _v = _vol_by_ts.get(_t, 0.0)
            mock.set_printed_volume(pid, max(_v, 1.0))  # floor 1 so a marketable order can cross
        except Exception:
            pass
    mock.set_quote = _set_quote_and_vol
    provider = AsOfProvider(frame_ticks)
    driver = rv3.ReplayV3Driver(
        db, seed, mock=mock, ohlcv_provider=provider, grid=grid,
        risk_gate_allows=True,                 # short-circuit ONLY the pre-entry risk gate
        equity_provider=lambda *a, **k: EQUITY,
    )
    # GATE #6 (2026-09-04): governance's paper daily-loss observation reads the Alpaca
    # account through AlpacaSpotAdapter().get_account_snapshot(); in a replay that read
    # failed every tick and blocked 26/26 entry attempts as a $0 "breach". For the run the
    # mock's own snapshot answers it. Production never enters this manager.
    from app.services.trading import governance as _gov
    # GATE #12 (2026-09-05): the claim helpers' "committed short session" opens its own
    # connection; this driver holds one transaction for the whole window, so those reads
    # saw the SEED session row and the deadman lineage could never retire after a re-arm
    # (exit never re-derived). Under the seam the short session is THIS session under a
    # SAVEPOINT -- the visibility a live per-tick commit gives. Production never installs it.
    from app.services.trading.momentum_neural import alpaca_orphan_claims as _oc

    with _gov.alpaca_account_snapshot_provider(mock.get_account_snapshot), \
            _oc.replay_short_session_provider(db):
        res = driver.run()

    # mine fills -> realized PnL (buys are cost, sells are proceeds; net of the mock's fees).
    # NormalizedFill (venue/protocol.py) fields: .side / .size / .price / .fee.
    fills, _ = mock.get_fills(limit=5000)
    def _pxsz(f):
        return float(getattr(f, "price", 0) or 0), float(getattr(f, "size", 0) or 0)
    buys = [_pxsz(f) for f in fills if str(f.side).lower() in ("buy", "bid", "long") and getattr(f, "price", None)]
    sells = [_pxsz(f) for f in fills if str(f.side).lower() in ("sell", "ask", "short") and getattr(f, "price", None)]
    cost = sum(p * q for p, q in buys)
    proceeds = sum(p * q for p, q in sells)
    # MARK-TO-MARKET any position still OPEN at window end (final_state trailing/entered/etc):
    # value the un-sold shares at the last grid bid (the honest liquidation value) so an
    # unclosed position is NOT counted as pure cost. net_open = bought - sold.
    net_open = sum(q for _, q in buys) - sum(q for _, q in sells)
    mtm = 0.0
    if net_open > 0.0001 and grid:
        last_bid = float(grid[-1].bid)
        mtm = net_open * last_bid
        proceeds += mtm
    pnl = proceeds - cost
    evs = [str(e.event_type) for e in db.query(TradingAutomationEvent)
           .filter(TradingAutomationEvent.session_id == seed.session_id)
           .order_by(TradingAutomationEvent.id.asc()).all()]
    grind_evts = [e for e in evs if "grind" in e.lower()]
    esc_evts = [e for e in evs if "escal" in e.lower()]

    # capture the session's entry-submit state BEFORE close (DIAG)
    _diag_rs = {}
    _entry_trace = []
    try:
        import json as _j
        _s = db.query(TradingAutomationSession).filter(
            TradingAutomationSession.id == seed.session_id).one_or_none()
        _diag_rs = _j.loads(getattr(_s, "risk_snapshot_json", None) or "{}")
        # ENTRY-DECISION TRACE: the entry/fill/exit events with their trigger reason + ts,
        # so we see WHICH indicator fired for each entry, price-vs-VWAP, and HOLD duration
        # (Ross holds 1-2 min — is CHILI entering too early / on the wrong signal?).
        if ENTRY_DIAG:
            _evs = db.query(TradingAutomationEvent).filter(
                TradingAutomationEvent.session_id == seed.session_id).order_by(
                TradingAutomationEvent.id.asc()).all()
            for e in _evs:
                et = str(e.event_type)
                if any(k in et for k in ("entry_filled", "entry_candidate", "entry_submitted",
                                          "exit_filled", "partial_exit", "trail_ratchet",
                                          "ofi_exhaustion", "tape_accel_reversal", "sell_into_strength",
                                          "bailout", "stopped", "backside")):
                    pj = {}
                    try:
                        pj = _j.loads(e.payload_json) if isinstance(e.payload_json, str) else (e.payload_json or {})
                    except Exception:
                        pj = {}
                    _entry_trace.append((str(e.ts), et, pj.get("reason") or pj.get("trigger") or "",
                                         pj.get("price") or pj.get("fill_price") or pj.get("entry_price")))
    except Exception:
        _diag_rs = {}

    # ── STRUCTURED RESULT — written BEFORE the cleanup below deletes the evidence ────────
    _json_path = _json_out_path(g4_on)
    if _json_path:
        from collections import Counter as _JC

        _span_s = max((WIN_END - OHLCV_START).total_seconds(), 1e-9)
        _win_s = max((WIN_END - WIN_START).total_seconds(), 1e-9)
        # NormalizedFill (venue/protocol.py:121): fill_id / order_id / product_id / side /
        # size / price / fee / trade_time. The instant is ``trade_time`` — NOT ``ts``; a
        # scorer that cannot place a fill on the clock cannot score it against a ledger.
        _fill_rows = []
        for f in fills:
            _fill_rows.append({
                "ts": getattr(f, "trade_time", None),
                "side": str(getattr(f, "side", "")),
                "px": (float(getattr(f, "price", 0) or 0) if getattr(f, "price", None) is not None else None),
                "qty": float(getattr(f, "size", 0) or 0),
                "fee": (float(getattr(f, "fee", 0) or 0) if getattr(f, "fee", None) is not None else None),
                "order_id": getattr(f, "order_id", None),
                "fill_id": getattr(f, "fill_id", None),
                "product_id": getattr(f, "product_id", None),
            })
        _full_events = []
        try:
            for e in db.query(TradingAutomationEvent).filter(
                    TradingAutomationEvent.session_id == seed.session_id).order_by(
                    TradingAutomationEvent.id.asc()).all():
                try:
                    _pj = (json.loads(e.payload_json)
                           if isinstance(e.payload_json, str) else (e.payload_json or {}))
                except Exception:
                    _pj = {}
                _full_events.append({
                    "ts": str(e.ts),
                    "event_type": str(e.event_type),
                    "payload": _bench_payload(str(e.event_type), _pj if isinstance(_pj, dict) else {}),
                })
        except Exception as _exc:  # a receipt that omits WHY it is short is worse than none
            _full_events = [{"ts": None, "event_type": "_receipt_event_read_failed",
                             "payload": {"errors": str(_exc)}}]
        _write_run_json(_json_path, {
            "schema": REPLAY_RESULT_SCHEMA,
            "label": str(label),
            "arm": ("g4_on" if g4_on else "g4_off"),
            "g4_on": bool(g4_on),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "tree": _tree_sha(),
            "env": _env_contract(),
            "tape_sources": tape_sources or {},
            "sink_reset": sink_reset,
            "mirrored": {
                "tick_rows": int(mirrored),
                "nbbo_rows": int(mirrored_nbbo),
                "depth_rows": int(mirrored_depth),
            },
            "density": {
                "mirror_span_seconds": round(_span_s, 3),
                "window_seconds": round(_win_s, 3),
                "ticks_per_second": round(float(mirrored) / _span_s, 6),
                "nbbo_rows_per_second": round(float(mirrored_nbbo) / _span_s, 6),
                "depth_rows_per_second": round(float(mirrored_depth) / _span_s, 6),
                "grid_steps_per_second": round(float(len(grid)) / _win_s, 6),
            },
            "grid_steps": len(grid),
            "mock": _mock_config(mock),
            "seed_session_id": int(seed.session_id),
            "execution_family": EXEC_FAMILY,
            "venue": venue_for_execution_family(EXEC_FAMILY),
            "economic_seed_mode": getattr(res, "economic_seed_mode", None),
            "certification_eligible": bool(getattr(res, "certification_eligible", False)),
            "certification_failures": list(getattr(res, "certification_failures", []) or []),
            "final_state": str(res.final_state),
            "states_visited": list(res.states_visited or []),
            "fills": _fill_rows,
            "pnl_usd": round(pnl, 6),
            "mtm_usd": round(mtm, 6),
            "net_open_shares": round(net_open, 6),
            "cost_usd": round(cost, 6),
            "proceeds_usd": round(proceeds, 6),
            "entries": len(buys),
            "exits": len(sells),
            "event_histogram": dict(_JC(evs)),
            "events": _full_events,
        })
        print(f"  replay_json_out={_json_path} events={len(_full_events)} fills={len(_fill_rows)}")

    # cleanup this arm's rows
    db.execute(text("DELETE FROM iqfeed_trade_ticks WHERE source='replay_v3' AND symbol=:s"), {"s": SYMBOL})
    db.execute(text("DELETE FROM momentum_nbbo_spread_tape WHERE source='replay_v3' AND symbol=:s"), {"s": SYMBOL})
    db.commit()
    db.close()

    print(f"\n===== {label} (G4 {'ON' if g4_on else 'OFF'}) =====")
    print(f"  grid_steps={len(grid)}  mirrored_ticks={mirrored}  final_state={res.final_state}")
    if DIAG:
        from collections import Counter
        print(f"  states_visited={res.states_visited}")
        reasons = Counter()
        for tk in res.ticks:
            r = tk.result or {}
            reasons[str(r.get("reason") or r.get("state") or r.get("blocked") or "ok")] += 1
        print(f"  top result reasons: {reasons.most_common(12)}")
        from collections import Counter as _C
        print(f"  event histogram: {_C(evs).most_common(15)}")
        # last 3 tick results (to see the pending-entry stall reason)
        for r in [tk.result for tk in res.ticks[-3:]]:
            print(f"  last_result: {dict(r)}")
        if os.environ.get("GRIND_DIAG") == "1":
            print(f"  GRIND decision histogram: {run_arm.__dict__.get('_grind_reasons', {})}")
        print(f"  le.entry_submitted={_diag_rs.get('entry_submitted')} "
              f"entry_order_id={_diag_rs.get('entry_order_id')} "
              f"entry_orders_resolved={_diag_rs.get('entry_orders_resolved')} "
              f"last_entry_block={_diag_rs.get('last_entry_block')} "
              f"pending_entry_submitted_at={_diag_rs.get('pending_entry_submitted_at_utc')}")
    print(f"  entries(buys)={len(buys)}  exits(sells)={len(sells)}  "
          f"net_open_shares={net_open:.0f}  mtm_value={mtm:+.2f} (@ last_bid {float(grid[-1].bid) if grid else 0:.2f})")
    if ENTRY_DIAG and _entry_trace:
        print(f"  --- ENTRY-DECISION TRACE (ts | event | reason | px) ---")
        for (ts, et, reason, px) in _entry_trace:
            print(f"    {ts[11:19]} | {et:32s} | {str(reason)[:34]:34s} | {px}")
    for i, (p, q) in enumerate(buys):
        print(f"    BUY  {q:.0f} @ {p:.4f}")
    for i, (p, q) in enumerate(sells):
        print(f"    SELL {q:.0f} @ {p:.4f}")
    print(f"  grind events: {len(grind_evts)}  {grind_evts[:3]}")
    print(f"  escalation events: {len(esc_evts)}  {esc_evts[:3]}")
    print(f"  >>> {label} PnL = {pnl:+.2f} USD")
    return pnl, len(buys), len(sells), len(grind_evts), len(esc_evts)


# Relations the replay itself writes. TRUNCATE ... CASCADE reaches far beyond them
# (33 tables as of migration 354), so this is a SEED list, not the clean list.
#
# The first six are the tables the run writes directly. The rest are FK PARENTS of those:
# CASCADE only descends to REFERENCING tables, so a parent is structurally invisible to
# both the truncate and the verification below — it would survive the reset and be
# counted as clean. That is not theoretical: ``adaptive_risk_reservations`` has a NOT NULL
# FK to ``adaptive_risk_decision_packets``, so every reservation the replay writes implies
# a packet, and a surviving packet makes the NEXT run take the idempotent-retry branch in
# adaptive_risk_reservation.py (or raise AdaptiveReservationIdempotencyConflict outright) —
# the 2026-08-29 contamination failure mode exactly. Every parent added here is one the
# project's own per-test clean list already names (tests/conftest.py
# ``_append_only_targeted_delete_tables``). ``_SINK_UNCOVERED_PARENTS_OK`` below keeps this
# list honest.
_SINK_SEED_TABLES = (
    # written directly by the replay
    "trading_automation_events", "trading_automation_sessions",
    "trading_automation_simulated_fills", "momentum_symbol_viability",
    "adaptive_risk_reservations", "adaptive_risk_opportunity_claims",
    # written by the Alpaca claim helpers through the APP engine once DATABASE_URL is the
    # sink (2026-09-05); a claim left by the previous run would block the next one with
    # account_entry_claim_present
    "broker_symbol_action_claims",
    # FK parents of the above that CASCADE cannot reach
    "adaptive_risk_decision_packets",
    "alpaca_paper_account_settlement_heads",
    "alpaca_paper_bp_reflection_receipts",
    "alpaca_paper_fill_page_objects",
    "captured_paper_post_commit_outbox",
    "captured_paper_completed_fill_watch_events",
)

# Parents that are deliberately NOT cleaned, with the reason they are safe to keep.
# ``momentum_strategy_variants`` is a shared fixture seeded with a RANDOM key per call —
# replay_v3._ensure_variant uses ``variant_key=f"replay_v3_{uuid4}"`` explicitly "so
# repeated seeds (and a non-truncated DB) never collide" — so rows accumulating across
# runs cannot influence a later run, and truncating it would take out the variant the
# seeded session still points at. (``users`` is likewise kept, and reaches this reset
# only through NULLABLE FKs, so it never appears in the check below.) Anything NOT on
# this list that a covered table has a NOT NULL FK to is a split clean and aborts the
# reset (see ``_SINK_UNCOVERED_PARENT_SQL``).
_SINK_UNCOVERED_PARENTS_OK = frozenset({"momentum_strategy_variants"})

# Every relation TRUNCATE ... CASCADE will actually reach from the seeds, walked from
# pg_constraint instead of hardcoded: PostgreSQL truncates every table holding an FK to
# a truncated one, transitively, regardless of the FK's ON DELETE action. The FK graph
# grows with every migration, so discovering it is the only way this stays correct.
# SCOPE: this walk is FK-only. TRUNCATE also descends partition/inheritance children
# (pg_inherits), which pg_constraint does not model. No relation in the closure is a
# partitioned or inherited parent today (the 7 partitioned tables in public — fast_* /
# trading_microstructure_log / trading_tenbeat_candle_log — are all outside it), so a
# guard on a child cannot currently be missed; widen this walk if that ever changes.
_SINK_CLOSURE_SQL = """
WITH RECURSIVE reached(oid) AS (
        SELECT c.oid
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
          AND c.relname = ANY(:seeds)
    UNION
        SELECT con.conrelid
        FROM pg_constraint con
        JOIN reached r ON con.confrelid = r.oid
        WHERE con.contype = 'f'
)
SELECT c.oid, n.nspname, c.relname
FROM reached r
JOIN pg_class c ON c.oid = r.oid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
ORDER BY n.nspname, c.relname
"""

# BEFORE TRUNCATE statement triggers on those relations (pg_trigger.tgtype bit 5 = 32).
# Discovered from the catalog, never matched on SQLSTATE or message text: the guards in
# this schema raise under at least two different SQLSTATEs (55000 and 23514) from seven
# different trigger functions, so any error-shape heuristic is wrong for most of them.
_SINK_TRUNCATE_GUARD_SQL = """
SELECT t.tgrelid, t.tgname, t.tgenabled
FROM pg_trigger t
WHERE NOT t.tgisinternal
  AND (t.tgtype & 32) <> 0
  AND t.tgrelid = ANY(:oids)
ORDER BY t.tgrelid, t.tgname
"""

# SELF-POLICING COVERAGE. A NOT NULL FK from a covered table to an UNCOVERED parent is a
# provable split clean: every covered row implies a parent row that the reset cannot reach,
# because CASCADE descends to children only. Rather than trust the seed list to stay
# complete as migrations land, derive the violation from the catalog and abort naming the
# parent. Only parents on _SINK_UNCOVERED_PARENTS_OK are exempt.
_SINK_UNCOVERED_PARENT_SQL = """
SELECT parent.relname AS parent, child.relname AS child, con.conname
FROM pg_constraint con
JOIN pg_class child ON child.oid = con.conrelid
JOIN pg_class parent ON parent.oid = con.confrelid
WHERE con.contype = 'f'
  AND con.conrelid = ANY(:oids)
  AND NOT (con.confrelid = ANY(:oids))
  AND NOT EXISTS (
      SELECT 1
      FROM unnest(con.conkey) AS k(attnum)
      JOIN pg_attribute att
        ON att.attrelid = con.conrelid AND att.attnum = k.attnum
      WHERE NOT att.attnotnull
  )
ORDER BY 1, 2
"""

# tgenabled -> the ALTER verb that restores exactly that state ('D' is never suspended).
_SINK_GUARD_RESTORE_VERB = {"O": "ENABLE", "R": "ENABLE REPLICA", "A": "ENABLE ALWAYS"}


def _qi(_name: str) -> str:
    """Quote a catalog-sourced identifier for interpolation into DDL."""
    return '"' + _name.replace('"', '""') + '"'


def _sink_truncate_targets(_closure):
    """Relations the reset will TRUNCATE — the full discovered closure.

    Seam: the regression test overrides this to simulate a PARTIAL clean and assert the
    verification below still fails loudly. Production behaviour is the identity function.
    """
    return list(_closure)


def _reset_sim_sink() -> dict | None:
    """CLEAN-SINK PROTOCOL (2026-08-29, sink-contamination incident).

    Ang naipong sessions/lockouts/viability ng mga naunang replay sa PAREHONG
    sink ay bumabago sa mga sumunod na run — SINUKAT: ang MIMI "baseline" ay
    gumalaw +60.60→+46.59 nang walang code change, at isang tamang eksperimento
    (0.6R rung, #1240) ay halos tuluyang natanggihan dahil sa multong "MIMI
    kill". Bawat invocation ay nagsisimula na ngayon sa malinis na sink — ang
    determinism ay napatunayan ng byte-identical na double-run. Ang _test
    suffix guard sa itaas ang pumipigil na matamaan ang prod. Opt-out (para sa
    sadyang multi-run accumulation studies): REPLAY_KEEP_SINK=1.

    TRUNCATE GUARDS (2026-09-03). The CASCADE above reaches append-only evidence
    ledgers that carry BEFORE TRUNCATE statement triggers, so on a migrated database
    this function used to abort the whole harness before a single tick was loaded —
    ``adaptive_risk_reservation_events is append-only; TRUNCATE is forbidden``
    (ERRCODE 55000, installed by migration 354). Thirteen such guards sit inside the
    cascade closure today.

    Those guards are load-bearing and are NOT weakened: the ledgers are a hash chain
    (``sequence`` / ``previous_event_sha256`` / ``event_sha256``, walked backwards by
    ``adaptive_risk_reservation.py`` to prove exit-owner ancestry), and nothing in
    ``app/migrations.py`` changes here. Instead this reset does what
    ``tests/conftest.py::_truncate_app_tables`` already does for pytest isolation:
    inside ONE transaction, against a database whose name the guard above has already
    proven ends in ``_test``, it suspends exactly the discovered TRUNCATE triggers,
    truncates, and restores them. DDL is transactional in PostgreSQL, so a crash or an
    error between the two ALTERs rolls the suspension back — there is no window in
    which the guard stays off. Unlike conftest's hand-maintained 18-name list, the
    guards here are discovered from ``pg_trigger``, so the next migration to add one
    does not break the harness again.

    A partial clean is worse than a hard failure — contaminated results look like
    results — so every relation in the closure is counted after commit and every
    suspended guard is re-read; anything non-zero or still disabled raises SystemExit.
    Coverage itself is policed the same way: a NOT NULL FK out of the covered set to a
    parent CASCADE cannot reach is a split clean, and aborts.

    ⚠️ DO NOT WIDEN THE _test FENCE. Two independent checks stand between
    ``ALTER TABLE ... DISABLE TRIGGER`` and a real-money hash chain: the URL-parsed name
    at import, and ``current_database()`` read from the server on the very connection
    that will issue the DDL. The second exists because a URL check validates a string,
    not a connection.
    """
    if os.environ.get("REPLAY_KEEP_SINK", "").strip() == "1":
        print("  [sink] REPLAY_KEEP_SINK=1 — hindi nireset ang sink")
        return None
    import sqlalchemy as _sa

    _eng = _sa.create_engine(SIM)
    try:
        with _eng.connect() as _c:
            # FENCE, before anything else touches this connection: the database's OWN
            # identity, from the server. The import-time check validates a URL string —
            # `.../chili?application_name=chili_test` would satisfy a naive suffix match
            # while connecting to prod, where this function would DISABLE the append-only
            # guards and TRUNCATE the reservation-event hash chain.
            _dbname = _c.execute(_sa.text("SELECT current_database()")).scalar_one()
            if not str(_dbname).endswith("_test"):
                raise SystemExit(
                    "  [sink] ABORT: refusing to suspend append-only TRUNCATE guards on "
                    f"database {str(_dbname)!r} — the clean-sink reset runs only against a "
                    f"*_test database (TEST_DATABASE_URL={SIM!r} resolves here)."
                )
            _closure = [
                (int(_oid), _ns, _rel) for (_oid, _ns, _rel) in _c.execute(
                    _sa.text(_SINK_CLOSURE_SQL), {"seeds": list(_SINK_SEED_TABLES)}
                )
            ]
            _found = {_rel for (_o, _ns, _rel) in _closure}
            _missing = [_t for _t in _SINK_SEED_TABLES if _t not in _found]
            if _missing:
                # Never degrade to "skipped": an unmigrated or wrong sink DB must stop the
                # run, not replay on top of whatever is in there.
                raise SystemExit(
                    f"  [sink] ABORT: sink table(s) missing from {str(_dbname)!r}: "
                    + ", ".join(_missing)
                    + "\n  [sink]   point TEST_DATABASE_URL at a MIGRATED *_test database."
                )
            _by_oid = {_oid: (_ns, _rel) for (_oid, _ns, _rel) in _closure}
            _uncovered = [
                (_p, _ch, _cn) for (_p, _ch, _cn) in _c.execute(
                    _sa.text(_SINK_UNCOVERED_PARENT_SQL), {"oids": list(_by_oid)}
                ) if _p not in _SINK_UNCOVERED_PARENTS_OK
            ]
            if _uncovered:
                raise SystemExit(
                    "  [sink] ABORT: the reset would be a SPLIT CLEAN — these covered "
                    "tables have a NOT NULL FK to a parent CASCADE cannot reach, so every "
                    "row they write leaves an orphaned parent behind:\n"
                    + "\n".join(f"  [sink]   {_ch} -> {_p}  ({_cn})"
                                for (_p, _ch, _cn) in _uncovered)
                    + "\n  [sink]   add the parent to _SINK_SEED_TABLES, or to "
                      "_SINK_UNCOVERED_PARENTS_OK with the reason it is safe to keep."
                )
            _guards = [
                (int(_oid), _tg, _en) for (_oid, _tg, _en) in _c.execute(
                    _sa.text(_SINK_TRUNCATE_GUARD_SQL), {"oids": list(_by_oid)}
                )
            ]
        # 'D' guards are already disabled — leave them exactly as found.
        _suspend = [(o, g, e) for (o, g, e) in _guards if e != "D"]
        _unknown = [(o, g, e) for (o, g, e) in _suspend if e not in _SINK_GUARD_RESTORE_VERB]
        if _unknown:
            raise SystemExit(
                "  [sink] ABORT: cannot restore unknown pg_trigger.tgenabled state(s): "
                + ", ".join(f"{_by_oid[o][1]}.{g}={e!r}" for (o, g, e) in _unknown)
            )

        _targets = _sink_truncate_targets(_closure)
        with _eng.begin() as _c:
            for _oid, _tg, _en in _suspend:
                _ns, _rel = _by_oid[_oid]
                _c.execute(_sa.text(
                    f"ALTER TABLE {_qi(_ns)}.{_qi(_rel)} DISABLE TRIGGER {_qi(_tg)}"
                ))
            _c.execute(_sa.text(
                "TRUNCATE "
                + ", ".join(f"{_qi(_ns)}.{_qi(_rel)}" for (_o, _ns, _rel) in _targets)
                + " RESTART IDENTITY CASCADE"
            ))
            for _oid, _tg, _en in _suspend:
                _ns, _rel = _by_oid[_oid]
                _c.execute(_sa.text(
                    f"ALTER TABLE {_qi(_ns)}.{_qi(_rel)} "
                    f"{_SINK_GUARD_RESTORE_VERB[_en]} TRIGGER {_qi(_tg)}"
                ))

        # Post-commit verification. Counts prove the sink is genuinely empty; the guard
        # re-read proves the integrity triggers came back on.
        with _eng.connect() as _c:
            _residue = []
            for _oid, _ns, _rel in _closure:
                _n = _c.execute(_sa.text(
                    f"SELECT count(*) FROM {_qi(_ns)}.{_qi(_rel)}"
                )).scalar_one()
                if _n:
                    _residue.append((_rel, int(_n)))
            _now = {
                (int(_oid), _tg): _en for (_oid, _tg, _en) in _c.execute(
                    _sa.text(_SINK_TRUNCATE_GUARD_SQL), {"oids": list(_by_oid)}
                )
            }
            _broken = [
                (_by_oid[o][1], g, e, _now.get((o, g)))
                for (o, g, e) in _suspend if _now.get((o, g)) != e
            ]
    finally:
        _eng.dispose()

    if _residue or _broken:
        _lines = ["  [sink] ABORT: clean-sink reset did not hold — refusing to replay."]
        if _residue:
            _lines.append(
                "  [sink]   NOT EMPTY after reset: "
                + ", ".join(f"{_t}={_n}" for (_t, _n) in _residue)
            )
        if _broken:
            _lines.append(
                "  [sink]   TRUNCATE guard NOT restored: "
                + ", ".join(f"{_t}.{_g} want={_w!r} got={_gt!r}"
                            for (_t, _g, _w, _gt) in _broken)
            )
        _lines.append(
            "  [sink]   A partial clean contaminates results (see the 2026-08-29 "
            "incident); fix the sink rather than setting REPLAY_KEEP_SINK=1."
        )
        raise SystemExit("\n".join(_lines))

    _names = [_rel for (_o, _ns, _rel) in _closure]
    _seeded = [_n for _n in _names if _n in _SINK_SEED_TABLES]
    print(f"  [sink] clean-sink reset on {str(_dbname)!r}: {len(_names)} tables, "
          f"ALL VERIFIED EMPTY ({len(_seeded)} sink seeds + "
          f"{len(_names) - len(_seeded)} reached via FK cascade; no NOT NULL FK escapes "
          f"the covered set except {', '.join(sorted(_SINK_UNCOVERED_PARENTS_OK))})")
    _line = "  [sink]   cleaned:"
    for _n in _names:
        if len(_line) + len(_n) + 1 > 96:
            print(_line)
            _line = "  [sink]           "
        _line += " " + _n
    if _line.strip():
        print(_line)
    if _suspend:
        print(f"  [sink]   truncate guards suspended for this one transaction and "
              f"restored ({len(_suspend)}, all re-verified enabled):")
        for _oid, _tg, _en in _suspend:
            print(f"  [sink]           {_by_oid[_oid][1]}.{_tg}")
    # Returned so a caller (and the regression test) can assert what was actually
    # covered and which guards were actually suspended — "no guard was ever disabled"
    # must not be able to pass silently.
    return {
        "database": str(_dbname),
        "cleaned": _names,
        "suspended": [(_by_oid[_o][1], _tg) for (_o, _tg, _en) in _suspend],
    }


def _startup_invariants() -> None:
    """FAIL CLOSED before a single row is read. Each of these has a run behind it whose
    number was wrong and looked right; see scripts/replay_harness_invariants.py."""
    with open(os.path.abspath(__file__), encoding="utf-8") as _fh:
        _src = _fh.read()
    assert_dense_stride(TICK_STRIDE, BENCH_QUESTION)   # 1 — stride-10 flipped +193.92 -> -4.66
    assert_clean_sink(os.environ)                      # 2 — reused sink moved +60.60 -> +46.59
    assert_as_of_reads(_src)                           # 4 — wall clock reads the tape as empty
    assert_tie_stable_sql(_src)                        # 5 — equal ts fell back to scan order
    # 9 — at STARTUP, on a throwaway mock built from the SAME kwargs run_arm uses, so a
    # constructor that stops honouring a knob aborts BEFORE the sink reset and the three
    # multi-minute mirrors rather than after them. run_arm re-asserts on the instance that
    # actually fills (a read-back of derived private state, not an echo of these kwargs).
    assert_mock_parity(rv3.MockBrokerAdapter(**_PARITY_MOCK_KWARGS))


def main():
    _startup_invariants()
    _sink = _reset_sim_sink()
    # TAPE PROVENANCE, before the load: a symbol-day hydrated from two providers returns
    # BOTH tapes concatenated with no visible defect (TMCR 2026-08-24: 33,866 = 2 x 16,933).
    _tape_sources = assert_single_hydrated_source()
    print(f"Loading {SYMBOL} tape ({WIN_START}..{WIN_END})...")
    nbbo, ticks, frame_ticks = load_prod()
    print(f"  nbbo_rows={len(nbbo)}  tick_rows={len(ticks)}  "
          f"frame_tick_rows={len(frame_ticks)} (warmup from {FRAME_START})")
    if SOURCE_FILTER:
        # AFTER the load, prove the predicate actually bound: exactly ONE source per table.
        # Only meaningful under an explicit filter — a live `chili` window legitimately
        # carries iqfeed_l1 + massive_snapshot + massive_ws_universe at once, which are
        # different granularities, not duplicates (_build_micro_bar_df prefers the
        # non-snapshot rows explicitly), so asserting one-source unconditionally would
        # break every live-tape replay.
        for _t, _srcs in _tape_sources.items():
            _present = {s: n for s, n in _srcs.items() if n}
            if len(_present) > 1:
                raise SystemExit(
                    f"  [tape] ABORT: SOURCE_FILTER={list(SOURCE_FILTER)} still leaves "
                    f"{len(_present)} sources in {_t}: {_present}"
                )
        print(f"  source_filter={list(SOURCE_FILTER)}  tape_sources={_tape_sources}")
    grid = build_grid(nbbo)
    print(f"  grid_steps(after {GRID_STEP_S}s downsample)={len(grid)}")
    if not grid:
        print("NO GRID — tape missing. Abort."); return
    arm = os.environ.get("ARM", "both")
    if arm == "on":
        on = run_arm(SYMBOL, grid, ticks, frame_ticks, g4_on=True,
                     sink_reset=_sink, tape_sources=_tape_sources)
        print(f"\n[ARM=on] G4 ON PnL {on[0]:+.2f} entries={on[1]} exits={on[2]} grind={on[3]} esc={on[4]}")
        return
    if arm == "off":
        off = run_arm(SYMBOL, grid, ticks, frame_ticks, g4_on=False,
                      sink_reset=_sink, tape_sources=_tape_sources)
        print(f"\n[ARM=off] G4 OFF PnL {off[0]:+.2f} entries={off[1]} exits={off[2]} grind={off[3]} esc={off[4]}")
        return
    on = run_arm(SYMBOL, grid, ticks, frame_ticks, g4_on=True,
                 sink_reset=_sink, tape_sources=_tape_sources)
    off = run_arm(SYMBOL, grid, ticks, frame_ticks, g4_on=False,
                  sink_reset=_sink, tape_sources=_tape_sources)
    print(f"\n================ FSM A/B RESULT ({SYMBOL}) ================")
    print(f"  G4 ON : PnL {on[0]:+.2f}  entries={on[1]} exits={on[2]} grind_evts={on[3]} esc_evts={on[4]}")
    print(f"  G4 OFF: PnL {off[0]:+.2f}  entries={off[1]} exits={off[2]} grind_evts={off[3]} esc_evts={off[4]}")
    print(f"  DELTA (ON - OFF) = {on[0]-off[0]:+.2f} USD")


if __name__ == "__main__":
    main()
