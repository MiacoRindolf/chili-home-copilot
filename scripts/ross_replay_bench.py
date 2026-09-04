#!/usr/bin/env python
"""ROSS PARITY BENCH RUNNER — drive ``scripts/replay_v3_fsm_window.py`` once per
(case, arm) as a SUBPROCESS, under an env contract that is fixed in code, and refuse
to report a number the harness cannot stand behind.

WHY THIS EXISTS AS A SEPARATE PROCESS PER RUN. The driver is 100% env-configured and
resets its own sink at startup (replay_v3_fsm_window.py:1567-1572). One process per
(case, arm) is therefore the only shape in which every arm gets its own clean sink and
its own module-level constant binding — an in-process loop would re-use the driver's
module-level ``SYMBOL`` / ``WIN_START`` / ``TICK_STRIDE`` (bound at import,
replay_v3_fsm_window.py:153-174) and silently bench the FIRST case forever.

FOUR FENCES, each of which has a wrong-and-plausible result behind it:

  1. ARMS ARE INTERLEAVED PER WINDOW (``interleave`` / ``assert_interleaved`` from
     replay_harness_invariants.py:103-152). The source DB is live under a 120-hour frame
     warm-up (FRAME_WARMUP_MIN, replay_v3_fsm_window.py:157-167), so an
     all-A-then-all-B plan measures the drift between the first and last run and prints it
     with the treatment's name on it.

  2. NO ``ROSS_*`` KEY REACHES THE DRIVER. The Ross ledger is after-the-fact grading
     evidence — ``build_ross_manifest.py:319`` stamps every manifest
     ``evidence_role: after_fact_grading_only``. A run that can read the answer is not a
     measurement of the lane. The parent environment is SANITISED (not merely
     "not added to"), because whoever launches this bench may well have the ledger
     exported in their shell.

  3. ``REPLAY_KEEP_SINK`` NEVER REACHES THE DRIVER. It is the sink-contamination
     shortcut: measured 2026-08-29, a reused sink moved a MIMI baseline +60.60 -> +46.59
     with no code change (replay_harness_invariants.py:86-96). Stripped from the parent
     env and refused inside an arm file.

  4. AN ARM MAY ONLY FLIP BEHAVIOUR, NEVER IDENTITY. Every key in the env contract is
     PROTECTED: an "arm" that quietly moved WIN_END, TICK_STRIDE or EQUITY would produce a
     delta that reads as a treatment effect and is really a different experiment. Two
     different size canons already exist in this project and mixing them has produced a
     false regression — which is why ``--equity`` and ``--risk`` are REQUIRED here and are
     not defaulted anywhere in this file.

WHAT IT WRITES (per run, under ``--out-dir``)::

    <case-dir>/<arm>/run.json                    the driver's own receipt, verbatim
                                                 (schema chili.replay_v3_fsm_window_result.v1)
    <case-dir>/<arm>/events.jsonl                receipt["events"], one object per line
    <case-dir>/<arm>/events_timeline.jsonl       events + fills merged on the clock
    <case-dir>/<arm>/events_timeline.md          the same event log, for a human
    bench.json                                   the plan, the fences, and every run's verdict

``<case-dir>`` is ``<SYMBOL>_<YYYY-MM-DD>``, or ``<SYMBOL>@<selector>_<YYYY-MM-DD>`` when a
manifest row was named explicitly — see ``Case.dirname``.

⚠️ THE EVENT LOG IS NOT ``timeline.jsonl``. ``scripts/rossbench_timeline.py`` writes
``timeline.jsonl`` / ``timeline.md`` / ``timeline.meta.json`` into this SAME directory, and
that is a different document: a per-second ``chili.rossbench_timeline_row.v1`` analysis
carrying ``first_divergence`` and per-event ``code_ref``. What this file writes is the raw
merge of ``receipt["events"]`` and ``receipt["fills"]`` with neither. Both used to be called
``timeline.jsonl`` and whichever ran last silently won. The reporter reads
``timeline.meta.json`` first and falls back to ``timeline.jsonl``
(rossbench_report.py:616-644); with the runner's log under its own name that fallback can
only ever read the analysed document, so the reporter needs no change for this rename —
verified by reading rossbench_report.load_arm_dir, which names no other timeline file.

Nothing here reads a database. Every post-run check reads the driver's receipt, so the
bench cannot itself contend for the sink it just asked the driver to reset.

Usage (all of ``--equity``/``--risk``/``--grid-step-s``/``--exec-family``/``--timeout-s``
are required — see ``_build_parser`` for why each one refuses a default)::

    python scripts/ross_replay_bench.py \
      --manifest project_ws/AgentOps/ross/bench_manifest.json \
      --corpus   project_ws/AgentOps/ross/bench_corpus.json \
      --pins     project_ws/AgentOps/ross/bench_pins.json \
      --cases    TMCR:2026-08-24,ILLR:2026-06-25:A7Gnw1CMExI::ILLR::2026-06-25::ml1 \
      --arms     base,sticky_off=arms/sticky_off.json \
      --build E:/dev/wt-bench --ref 078487738 \
      --source postgresql://chili:chili@localhost:5433/chili \
      --sink   postgresql://chili:chili@localhost:5433/chili_bench_test \
      --out-dir E:/dev/wt-bench/project_ws/AgentOps/ross/bench_out \
      --equity 30000 --risk 900 --grid-step-s 1.0 --exec-family alpaca_spot \
      --lead-s 900 --lag-s 2700 --timeout-s 5400

A ``--cases`` entry is ``SYMBOL:DATE`` or ``SYMBOL:DATE:<manifest_id>``. The third field is
REQUIRED whenever the manifest holds more than one row for that symbol-day, which since the
master-ledger layer landed is the common case: measured 2026-09-04 against the 418-window
manifest built from the current evidence tree, 62 of 217 symbol-days carry more than one row
(ILLR 2026-06-25 carries five), and the eight lane-alive known-answer cases split 3 unique /
5 ambiguous. See ``find_manifest_row`` for the full resolution order.

Runnable contract test: pytest tests/test_ross_replay_bench_contract.py -v
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

# Sibling scripts/ modules. Same sys.path shim the driver itself uses
# (replay_v3_fsm_window.py:96-102) so this runs both as
# ``python scripts/ross_replay_bench.py`` and as an import from the repo root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from replay_harness_invariants import (  # noqa: E402
    DENSE_STRIDE_MAX,
    KEEP_SINK_ENV,
    additive_count_check,
    assert_clean_sink,
    assert_dense_stride,
    assert_interleaved,
    assert_mock_parity,
    interleave,
    verify_tree,
)

logger = logging.getLogger(__name__)

BENCH_SCHEMA = "chili.ross_replay_bench.v1"

# Must equal replay_v3_fsm_window.py:199. The contract test asserts the two agree, so a
# driver that starts emitting a v2 receipt fails the test instead of being silently
# mis-parsed here.
DRIVER_RESULT_SCHEMA = "chili.replay_v3_fsm_window_result.v1"

DRIVER_RELPATH = os.path.join("scripts", "replay_v3_fsm_window.py")

# ─────────────────────────────────────────────────────────────────────────────
# THE ENV CONTRACT
# ─────────────────────────────────────────────────────────────────────────────

# The four hydrated providers this bench replays from. Without a provenance predicate a
# symbol-day hydrated from two providers returns BOTH tapes CONCATENATED — measured on
# TMCR 2026-08-24: 16,933 iqfeed_lookup_hist rows + 16,933 polygon_v3_trades rows came
# back as 33,866 ticks, every print twice, nothing about the rows malformed
# (replay_v3_fsm_window.py:176-185). The driver additionally proves, after the load, that
# the predicate left exactly ONE source per table (:1577-1591), so this list being wider
# than one provider does not weaken that check.
SOURCE_FILTER_VALUE = "iqfeed_lookup_hist,iqfeed_lookup_bbo,polygon_v3_trades,polygon_v3_quotes"

# 5 days, in minutes. NOT a tuning constant: it is the period the LIVE runner requests
# from its OHLCV providers, which is what the frame warm-up exists to reproduce
# (replay_v3_fsm_window.py:157-167). With OHLCV_START == WIN_START every frame starts the
# window at ZERO bars and the whole run reads "insufficient_bars"; that artifact is what
# made 47 nightly reports worthless.
FRAME_WARMUP_MIN_DEFAULT = 5 * 24 * 60

# 1 = keep every print. Derived, not chosen: ``assert_dense_stride`` refuses anything
# coarser than DENSE_STRIDE_MAX (=2, replay_harness_invariants.py:42) for a question whose
# text contains "bench", and BENCH_QUESTION below contains it. Measured 2026-08-28: the
# same window replayed +$193.92 at stride 1 and -$4.66 at stride 10.
DEFAULT_TICK_STRIDE = 1

# The operator's declared question for this harness, passed to ``assert_dense_stride``.
# It is deliberately NOT exported as BENCH_QUESTION into the driver env: the env contract
# below is exact, and the density floor is enforced HERE, before any subprocess starts.
BENCH_QUESTION = "ross parity bench: exit and entry behaviour against a human ledger"

# EXACTLY the keys this bench sets on the driver. The contract test asserts set equality,
# so adding a key silently is not possible.
CONTRACT_ENV_KEYS: frozenset[str] = frozenset({
    "PYTHONPATH",           # the build tree under test (python itself reads this)
    "CHILI_PYTEST",         # skips app-startup migrations (app/main.py:55)
    "DATABASE_URL",         # READ-ONLY source tape (replay_v3_fsm_window.py:128)
    "TEST_DATABASE_URL",    # the throwaway sink; its NAME must end in _test (:147-151)
    "SOURCE_FILTER",        # tape provenance allow-list (:183-185)
    "SYMBOL",
    "WIN_START",
    "WIN_END",
    "OHLCV_START",          # == WIN_START; the frame depth comes from FRAME_WARMUP_MIN
    "FRAME_WARMUP_MIN",
    "TICK_STRIDE",
    "GRID_STEP_S",
    "FULL_MIRROR",          # 1 = full-density streaming mirror (:994)
    "ARM",                  # the DRIVER's G4 on/off knob (:1596) — see the note below
    "EQUITY",
    "RISK",
    "EXEC_FAMILY",          # a silent no-op until 2026-09-04 (:187-195)
    "REPLAY_JSON_OUT",      # the per-run receipt this bench reads back (:198)
    "DIAG",
    "ENTRY_DIAG",
})

# ⚠️ NAME COLLISION, ON PURPOSE. ``ARM`` in the DRIVER's vocabulary is the G4 exit A/B
# switch (on/off/both, replay_v3_fsm_window.py:1596-1610); ``arm`` in the BENCH's
# vocabulary is a treatment (a JSON dict of env overrides). They are different things and
# the driver's one is pinned to "on" for every bench run, because ARM=both writes TWO
# receipts under mangled filenames (:809-817) and the bench needs exactly one run.json per
# arm directory. ``ARM`` is protected, so a bench arm cannot flip it.
DRIVER_ARM_VALUE = "on"

# Keys an arm file may never set. Every contract key is protected: an arm exists to flip
# BEHAVIOUR, and anything that moves the window, the tape, the sizing or the rail makes a
# different experiment, not a different arm.
PROTECTED_ENV_KEYS: frozenset[str] = CONTRACT_ENV_KEYS

# ``ROSS_*`` is the ledger fence (see fence 2 above). ``ROSSBENCH_*`` is here because the
# fence as first written was NARROWER THAN IT READ: ROSSBENCH_PIN_HALFWIDTH_S is a real knob
# — rossbench_pin_ross_events.py:1695 takes it as the default for --halfwidth-s, which sets
# how wide a tape slice a pin is searched in — and a "ROSS_" prefix test does not match it,
# so it and every other bench-side ROSSBENCH_* key in the operator's shell survived
# sanitise_parent_env and reached the driver. The driver reads none of them today (checked
# against replay_v3_fsm_window.py, which has no ROSSBENCH_ reference), so nothing misbehaved;
# the point is that the bench's own knobs must not be able to become the driver's.
FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = ("ROSS_", "ROSSBENCH_")
FORBIDDEN_ENV_KEYS: frozenset[str] = frozenset({KEEP_SINK_ENV})  # "REPLAY_KEEP_SINK"

# Sentinel proving the BUILD TREE carries the change this bench depends on. The NBBO
# mirror is the one that matters: without it ``momentum_nbbo_spread_tape`` is EMPTY in the
# sink while the FSM reads it directly in three places, so the micro-pullback detector and
# the adaptive spread-cost veto measure silence and the bench scores a lane that never ran
# (replay_v3_fsm_window.py:37-46, function at :514).
DEFAULT_SENTINEL_FILE = DRIVER_RELPATH
DEFAULT_SENTINEL = "def mirror_nbbo_streaming("

# ─────────────────────────────────────────────────────────────────────────────
# ET CLOCK DISAMBIGUATION
# ─────────────────────────────────────────────────────────────────────────────

ET = ZoneInfo("America/New_York")

# The US equity extended session is 04:00-20:00 ET. A bare "1:15" in a trading recap is
# therefore 13:15 ET, never 01:15 — 01:00-03:59 ET is outside every session, 13:00-15:59
# is mid-session. These two numbers are session bounds, not tuning: they are the only
# reason a 12-hour narrative clock can be resolved at all, and any anchor that lands
# outside them is REFUSED rather than guessed.
ET_SESSION_FIRST_HOUR = 4
ET_SESSION_LAST_HOUR = 20

# 132/157 ledger rows start with a clock (optionally "~"-prefixed); 14 more carry one
# mid-sentence; 11 carry none at all and are not benchable from the narrative field.
_LEADING_CLOCK = re.compile(r"^\s*~?\s*(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?", re.I)
_EMBEDDED_CLOCK = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])\s*(am|pm|a\.m\.|p\.m\.)?", re.I)

# Row-container and field names this bench will read, in priority order, EACH ONE CHECKED
# against the file that emits it rather than guessed. The bench consumes manifest / corpus /
# pins documents built by four other tools, and a cosmetic container difference silently
# producing zero joins is the failure mode these lists exist to avoid — so an unrecognised
# shape is REFUSED by name (find_manifest_row) or reported as "unknown", never as a match.
# ⚠️ These citations name SYMBOLS, not lines, wherever the emitting file is still under
# active edit — the adapter's own cross-module line refs went stale inside one session when
# an additive change moved a file from 730 to 1263 lines.
_ROW_LIST_KEYS = (
    "cases",      # bench-report shape (ross_manifest_adapter.case_as_json_row)
    "windows",    # chili.ross_ground_truth_manifest.v1 (build_ross_manifest.build:901)
    "rows",       # chili.rossbench_corpus.v1 (rossbench_corpus.py:834)
    "pins",       # chili.ross_event_pins.v1 (rossbench_pin_ross_events.build_pins_doc)
    "events",
    "trades",     # chili.ross_master_ledger.v1, if the raw ledger is passed directly
)
_ROW_SYMBOL_KEYS = ("symbol", "ticker")
# The per-window identity, in the SAME priority order ross_manifest_adapter._pin_id:430-435
# uses, so the runner and the adapter select the same row from the same document.
# ``manifest_id`` is the ground-truth manifest's own key and is unique by construction —
# build_ross_manifest.build raises "duplicate manifest_ids" before writing (:891-894).
# ``label_id`` is the adapter's name for it in its ``cases`` container
# (ross_manifest_adapter.case_as_json_row:846).
_ROW_ID_KEYS = ("manifest_id", "label_id", "window_id")
# "trade_date" is the adapter's name for the ET trading day
# (ross_manifest_adapter.case_as_json_row).
_ROW_DATE_KEYS = ("date", "trade_date", "trading_day", "session_date")
# "start_ts"/"end_ts" are the adapter's VALIDATED grading window (same function) — when
# present they win outright, because that is the window the grader will score against and
# re-deriving it from an anchor here could disagree with the grader.
_ROW_WIN_START_KEYS = ("win_start", "win_start_utc", "window_start_utc", "start_ts")
_ROW_WIN_END_KEYS = ("win_end", "win_end_utc", "window_end_utc", "end_ts")
# "grading_anchor_utc" is the pinner's own answer to "what instant does a window get built
# around", and it deliberately leaves the LEAD null because that is not its choice to make
# (rossbench_pin_ross_events.pin_window emits ``"lead_s": None``) — which is exactly why
# --lead-s/--lag-s live here.
_ROW_ANCHOR_UTC_KEYS = (
    "grading_anchor_utc", "decision_ts", "entry_pin_ts",
    "ross_entry_time_utc", "entry_time_utc", "anchor_utc",
)
# Narrative ET clocks. "entry_clock_et" is already normalised to HH:MM by the corpus builder
# (rossbench_corpus.py:231-247); the rest are raw prose out of the ledger/manifest.
_ROW_ANCHOR_ET_KEYS = (
    "entry_clock_et", "window_et", "stated_window_et",
    "entry_time_et", "ross_entry_time_et", "entry_et",
)

# How much of a failed run's output to keep in bench.json. The driver's end-of-arm summary
# block is ~35 lines (replay_v3_fsm_window.py:1192-1226); 60 keeps that block plus the
# traceback or SystemExit line that displaced it.
STDOUT_TAIL_LINES = 60


# ─────────────────────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Case:
    """One benchable manifest window. Frozen and str-only so it can be a dict key in
    ``assert_interleaved`` (replay_harness_invariants.py:124).

    A case is NOT a symbol-day. 62 of the manifest's 217 symbol-days carry more than one
    window (measured 2026-09-04 on the 418-window manifest), so two cases of the same
    symbol-day can be benched in one run — which is why ``__str__`` and ``dirname`` carry the
    selector when one was named. Those two strings are identities: ``str(case)`` is the
    interleave key and the tape-reference key, and ``dirname`` is the on-disk directory.
    """

    symbol: str
    date: str            # ET trading day, YYYY-MM-DD
    win_start: str       # UTC-naive isoformat — what the driver parses (:154)
    win_end: str
    anchor_source: str   # which field/branch produced the window (recorded, never inferred later)
    anchor_detail: str
    # The manifest row this case came from. Recorded ALWAYS (it is provenance), but only
    # folded into the identity strings when the operator named it — an implicitly resolved
    # case is by definition the only row for its symbol-day, so it needs no suffix and keeps
    # the directory name the report and the docs already expect.
    manifest_id: str = ""
    selector_explicit: bool = False

    def __str__(self) -> str:
        return f"{self.symbol}:{self.date}:{self.manifest_id}" if self.selector_explicit \
            else f"{self.symbol}:{self.date}"

    @property
    def selector_slug(self) -> str:
        """Filename-safe short form of ``manifest_id``, with the parts already in the
        directory name (the symbol and the date) dropped.

        ``A7Gnw1CMExI::ILLR::2026-06-25::ml1`` -> ``A7Gnw1CMExI-ml1``. Uniqueness within a
        symbol-day survives the drop because the discarded segments are byte-identical
        across that group; ``main`` asserts dirname uniqueness across the whole plan anyway,
        because a collision would put two experiments in one directory.
        """
        parts = [p for p in str(self.manifest_id).split("::")
                 if p and p != self.symbol and p != self.date]
        raw = "-".join(parts) or str(self.manifest_id)
        return re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9._-]", "-", raw)).strip("-.")

    @property
    def dirname(self) -> str:
        # ":" is a legal separator in the --cases CLI spec and an ILLEGAL filename
        # character on Windows, which is the only platform this project runs on.
        #
        # The date stays LAST and "@" separates the selector because that is the shape
        # rossbench_report.case_identity parses: it takes case_name.replace("@","_")
        # .split("_")[-1] and accepts it as the ET trading day only if it is a YYYY-MM-DD.
        # A suffix after the date would push the reporter onto its WIN_START fallback, which
        # reports the window's UTC date — the wrong day for any window that crosses midnight
        # Z, and a silent one.
        #
        # VERIFIED by calling that function on the eight names this runner produced for the
        # lane-alive set: all eight return date_source="case_dir" with the right day, and the
        # SYMBOL comes back bare ("ILLR", not "ILLR@A7Gnw1CMExI-ml1") because its primary
        # source is the driver receipt's env.SYMBOL. Its LAST-RESORT branch —
        # case_name.rsplit("_", 1)[0], reached only when no arm of the case produced a
        # receipt at all — does return the decorated string; such a case has no receipt to
        # score either way, but that is a known rough edge in a file this agent does not own.
        if self.selector_explicit and self.selector_slug:
            return f"{self.symbol}@{self.selector_slug}_{self.date}"
        return f"{self.symbol}_{self.date}"


@dataclass
class Arm:
    """A treatment: a name plus a JSON dict of env overrides for the driver subprocess."""

    name: str
    overrides: dict[str, str] = field(default_factory=dict)
    source: Optional[str] = None

    def __str__(self) -> str:
        # ``interleave`` de-duplicates on str() (replay_harness_invariants.py:111), so the
        # NAME is the identity — two arm files with the same name must collide.
        return self.name


_ARM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# ─────────────────────────────────────────────────────────────────────────────
# INPUT PARSING
# ─────────────────────────────────────────────────────────────────────────────

def parse_case_spec(spec: str) -> list[tuple[str, str, Optional[str]]]:
    """``SYM:DATE[:MANIFEST_ID],...`` -> [(symbol, date, manifest_id_or_None)].

    The optional third field is the manifest row selector. It is split off with
    ``maxsplit=2`` and NOT validated further here, because a manifest_id contains colons of
    its own — layer 4 builds ``<video_id>::<symbol>::<date>::ml<n>``
    (build_ross_manifest.py:644) — so everything after the second colon is the id verbatim.
    ``,`` stays the case separator: no manifest_id in the 418-window manifest contains one.

    There is deliberately no "all rows in the manifest" mode: 30 of the ledger's 187 rows
    are no-trade/miss records merged in from five sub-schemas, and 11 more carry no clock
    at all, so a bench that swept the file would score records that are not trades.
    """
    out: list[tuple[str, str, Optional[str]]] = []
    for raw in str(spec or "").split(","):
        tok = raw.strip()
        if not tok:
            continue
        parts = [p.strip() for p in tok.split(":", 2)]
        if len(parts) < 2:
            raise SystemExit(f"--cases: {tok!r} is not SYMBOL:YYYY-MM-DD[:MANIFEST_ID]")
        sym, day = parts[0], parts[1]
        mid: Optional[str] = parts[2] if len(parts) == 3 and parts[2] else None
        if not sym or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
            raise SystemExit(f"--cases: {tok!r} is not SYMBOL:YYYY-MM-DD[:MANIFEST_ID]")
        if len(parts) == 3 and not parts[2]:
            raise SystemExit(
                f"--cases: {tok!r} ends in a colon but names no manifest_id. Drop the colon "
                "to select the symbol-day's only row, or name the row."
            )
        entry = (sym.upper(), day, mid)
        if entry in out:
            raise SystemExit(f"--cases: {tok!r} listed twice")
        out.append(entry)
    if not out:
        raise SystemExit("--cases: no cases given")
    return out


def parse_arm_spec(spec: str, *, base_dir: str = ".") -> list[Arm]:
    """``base,name=path.json`` -> [Arm]. ``base`` is the control arm (no overrides).

    Each override file must be a FLAT JSON object of string-able values; every key is
    validated against the forbidden and protected sets here, at load time, so a bad arm
    file fails before the sink is reset rather than after an hour of replay.
    """
    arms: list[Arm] = []
    for raw in str(spec or "").split(","):
        tok = raw.strip()
        if not tok:
            continue
        if "=" in tok:
            name, _, path = tok.partition("=")
            name, path = name.strip(), path.strip()
            if not path:
                raise SystemExit(f"--arms: {tok!r} names no file")
            full = path if os.path.isabs(path) else os.path.join(base_dir, path)
            overrides = _load_arm_file(full, name)
            arms.append(Arm(name=name, overrides=overrides, source=os.path.abspath(full)))
        else:
            arms.append(Arm(name=tok, overrides={}, source=None))
    if not arms:
        raise SystemExit("--arms: no arms given")
    for a in arms:
        if not _ARM_NAME_RE.fullmatch(a.name):
            raise SystemExit(
                f"--arms: arm name {a.name!r} is not a safe directory segment "
                "([A-Za-z0-9][A-Za-z0-9._-]*) — it becomes <case>/<arm>/ on disk"
            )
    names = [a.name for a in arms]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise SystemExit(f"--arms: duplicate arm name(s) {dupes}")
    return arms


def _load_arm_file(path: str, name: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as exc:
        raise SystemExit(f"--arms: cannot read arm {name!r} from {path!r}: {exc}") from None
    except ValueError as exc:
        raise SystemExit(f"--arms: arm {name!r} at {path!r} is not JSON: {exc}") from None
    if not isinstance(doc, dict):
        raise SystemExit(
            f"--arms: arm {name!r} at {path!r} must be a JSON OBJECT of env overrides, "
            f"got {type(doc).__name__}"
        )
    out: dict[str, str] = {}
    for key, value in doc.items():
        k = str(key)
        if isinstance(value, (dict, list)):
            raise SystemExit(
                f"--arms: arm {name!r} key {k!r} is {type(value).__name__}; an env override "
                "must be a scalar"
            )
        _refuse_forbidden_key(k, where=f"arm {name!r} ({path})")
        if k in PROTECTED_ENV_KEYS:
            raise SystemExit(
                f"--arms: arm {name!r} sets {k!r}, which is part of the run's IDENTITY "
                "(window, tape, sizing, rail, receipt path). An arm flips behaviour; "
                "changing identity makes a different experiment whose delta would read as "
                "a treatment effect. Make it a separate --cases entry or a separate bench run."
            )
        out[k] = "" if value is None else str(value)
    return out


def _refuse_forbidden_key(key: str, *, where: str) -> None:
    k = str(key)
    if k in FORBIDDEN_ENV_KEYS:
        raise SystemExit(
            f"{where}: {k!r} is refused. It keeps the previous run's sessions, lockouts and "
            "viability rows in the sink — measured 2026-08-29, a reused sink moved a baseline "
            "+60.60 -> +46.59 with no code change."
        )
    for prefix in FORBIDDEN_ENV_PREFIXES:
        if k.startswith(prefix):
            # Two different reasons behind one fence, and saying the wrong one would send
            # the operator looking in the wrong place.
            why = (
                "The Ross ledger is after-the-fact grading evidence "
                "(evidence_role=after_fact_grading_only); a run that can read the answer is "
                "not a measurement of the lane."
                if prefix == "ROSS_" else
                "ROSSBENCH_* keys configure the BENCH side (pin halfwidth, corpus paths). "
                "The driver's environment is the experiment's identity and carries only the "
                "keys in CONTRACT_ENV_KEYS."
            )
            raise SystemExit(f"{where}: {k!r} is refused. {why}")


def _sha256(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def load_json_input(path: Optional[str], label: str) -> tuple[Optional[Any], dict[str, Any]]:
    """Load an input document and describe it for the receipt.

    The description (path, sha256, declared schema, row count) is what makes a later
    schema mismatch DIAGNOSABLE rather than mysterious — this bench consumes manifest /
    pins / corpus files produced by other steps and cannot verify their internals.
    """
    if not path:
        return None, {"path": None, "present": False}
    doc: Any
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as exc:
        raise SystemExit(f"--{label}: cannot read {path!r}: {exc}") from None
    except ValueError as exc:
        raise SystemExit(f"--{label}: {path!r} is not JSON: {exc}") from None
    meta = {
        "path": os.path.abspath(path),
        "present": True,
        "sha256": _sha256(path),
        "schema": (doc.get("schema") if isinstance(doc, dict) else None),
        "top_level_keys": (sorted(doc)[:24] if isinstance(doc, dict) else None),
        "rows": (len(_row_list(doc)) if _row_list(doc) is not None else None),
    }
    return doc, meta


def _row_list(doc: Any) -> Optional[list[dict]]:
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict)]
    if isinstance(doc, dict):
        for key in _ROW_LIST_KEYS:
            val = doc.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
    return None


def _first(row: Mapping[str, Any], keys: Sequence[str]) -> tuple[Optional[str], Optional[Any]]:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return k, row[k]
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# WINDOW RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────

def parse_et_clock(text: str) -> tuple[Optional[int], Optional[int], str, str]:
    """Pull an ET wall clock out of a NARRATIVE field.

    Returns ``(hour24, minute, source, matched)`` where ``source`` is one of
    ``leading`` / ``embedded`` / ``none``. ``entry_time_et`` in the ledger is prose, e.g.
    ``"premarket (clock time not stated; ...)"`` — those resolve to ``none`` and the case
    is refused rather than defaulted onto the open.
    """
    s = str(text or "")
    m = _LEADING_CLOCK.match(s)
    source = "leading"
    if not m:
        m = _EMBEDDED_CLOCK.search(s)
        source = "embedded"
    if not m:
        return None, None, "none", ""
    hour, minute = int(m.group(1)), int(m.group(2))
    meridiem = (m.group(3) or "").lower().replace(".", "")
    if minute > 59:
        return None, None, "none", m.group(0)
    if meridiem.startswith("p") and hour < 12:
        hour += 12
    elif meridiem.startswith("a") and hour == 12:
        hour = 0
    elif not meridiem and 1 <= hour <= 3:
        # 12-hour narrative clock: 01:00-03:59 ET is outside the 04:00-20:00 extended
        # session, 13:00-15:59 is mid-session. This is the ONLY inference this parser makes
        # and it is recorded in the receipt as pm_inferred.
        hour += 12
        source = f"{source}+pm_inferred"
    if not (ET_SESSION_FIRST_HOUR <= hour <= ET_SESSION_LAST_HOUR):
        return None, None, "none", m.group(0)
    return hour, minute, source, m.group(0)


def et_clock_to_utc(day: str, hour: int, minute: int) -> datetime:
    """ET wall clock on an ET trading day -> UTC-naive, which is what the driver parses
    (``datetime.fromisoformat`` at replay_v3_fsm_window.py:154-156)."""
    y, mo, d = (int(p) for p in day.split("-"))
    aware = datetime(y, mo, d, hour, minute, tzinfo=ET)
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_utc_field(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def resolve_window(
    row: Mapping[str, Any],
    *,
    symbol: str,
    day: str,
    lead_s: Optional[float],
    lag_s: Optional[float],
    pin: Optional[Mapping[str, Any]] = None,
) -> Case:
    """Turn one manifest row into a Case, refusing (never guessing) when it cannot.

    Four branches, most trustworthy first, and the ORDER is the point — the manifest is the
    ground truth the grader scores against, so nothing outside it may override what it says:

      1. the row already carries an explicit UTC window (``win_start``/``win_end``);
      2. it carries an explicit UTC instant (``ross_entry_time_utc`` — 18 ledger xref rows
         do) and ``--lead-s``/``--lag-s`` say how far around it to replay;
      3. it carries a NARRATIVE ET clock, parsed by ``parse_et_clock`` above;
      4. LAST RESORT — the manifest row says nothing about when, and the PIN row for this
         same ``manifest_id`` carries a ``grading_anchor_utc``. That field is the pinner's
         own answer to "what instant does this window get built around"
         (rossbench_pin_ross_events.pin_window), derived from the ledger's stated time; it is
         the same evidence the manifest row is missing, read off the document that kept it.
         MEASURED 2026-09-04 over the 418-window manifest and the 157-row pins file it was
         built with: 196 windows carry no clock this function can read, and 13 of those have
         exactly one anchored pin row. It is recorded as ``pin_anchor_utc``, never as a
         manifest clock, because the grader must be able to tell the two apart.
    """
    k_start, v_start = _first(row, _ROW_WIN_START_KEYS)
    k_end, v_end = _first(row, _ROW_WIN_END_KEYS)
    if v_start and v_end:
        start, end = _parse_utc_field(v_start), _parse_utc_field(v_end)
        if start is None or end is None:
            raise SystemExit(
                f"case {symbol}:{day}: manifest {k_start}/{k_end} are not parseable "
                f"timestamps ({v_start!r}, {v_end!r})"
            )
        if end <= start:
            raise SystemExit(f"case {symbol}:{day}: {k_end} <= {k_start} ({v_start!r}..{v_end!r})")
        return Case(symbol, day, start.isoformat(), end.isoformat(),
                    "manifest_window_utc", f"{k_start}/{k_end}")

    k_anchor, v_anchor = _first(row, _ROW_ANCHOR_UTC_KEYS)
    anchor: Optional[datetime] = _parse_utc_field(v_anchor) if v_anchor else None
    detail = f"{k_anchor}={v_anchor!r}"
    anchor_source = "manifest_anchor_utc"

    if anchor is None:
        k_et, v_et = _first(row, _ROW_ANCHOR_ET_KEYS)
        hour = minute = None
        if v_et is not None:
            hour, minute, clock_source, matched = parse_et_clock(v_et)
        if hour is not None and minute is not None:
            anchor = et_clock_to_utc(day, hour, minute)
            anchor_source = f"manifest_et_clock:{clock_source}"
            detail = f"{k_et}={v_et!r} -> matched {matched!r} -> {hour:02d}:{minute:02d} ET"
        else:
            # Branch 4. Only reached when the manifest row itself offered nothing.
            k_pin, v_pin = _first(pin or {}, _ROW_ANCHOR_UTC_KEYS)
            pin_anchor = _parse_utc_field(v_pin) if v_pin else None
            if pin_anchor is not None:
                anchor = pin_anchor
                anchor_source = "pin_anchor_utc"
                detail = f"manifest {k_et or '(no clock field)'}={v_et!r}; pin {k_pin}={v_pin!r}"
            elif v_et is None:
                raise SystemExit(
                    f"case {symbol}:{day}: manifest row carries none of "
                    f"{list(_ROW_WIN_START_KEYS)}, {list(_ROW_ANCHOR_UTC_KEYS)} or "
                    f"{list(_ROW_ANCHOR_ET_KEYS)}, and no pin row supplies an anchor either — "
                    "nothing here says WHEN to replay. "
                    "Row keys: " + ", ".join(sorted(map(str, row)))
                )
            else:
                raise SystemExit(
                    f"case {symbol}:{day}: {k_et}={v_et!r} carries no usable ET clock "
                    "(11 of 187 ledger rows do not, and 30 more are no-trade records), and "
                    "no pin row for this window carries a grading anchor. Give this case an "
                    "explicit win_start/win_end in the manifest, or drop it."
                )

    if lead_s is None or lag_s is None:
        raise SystemExit(
            f"case {symbol}:{day}: the manifest gives an ANCHOR ({detail}) but no window, "
            "so --lead-s and --lag-s are required to say how much tape around it to replay. "
            "They have no default here on purpose: the window length decides what the FSM "
            "can ever see."
        )
    start = anchor - timedelta(seconds=float(lead_s))
    end = anchor + timedelta(seconds=float(lag_s))
    return Case(symbol, day, start.isoformat(), end.isoformat(), anchor_source, detail)


@dataclass(frozen=True)
class ManifestPick:
    """One manifest row and HOW it was chosen. The ``resolution`` string lands in bench.json
    so a reader can tell an operator's explicit choice from the harness's inference."""

    row: Mapping[str, Any]
    manifest_id: str
    resolution: str
    candidates: tuple[str, ...]


def _row_id(row: Mapping[str, Any]) -> str:
    _, value = _first(row, _ROW_ID_KEYS)
    return str(value).strip() if value not in (None, "") else ""


def _anchored_pins_for(pins: Any, symbol: str, day: str) -> list[Mapping[str, Any]]:
    """Pin rows for this symbol-day that actually carry a grading anchor.

    A pin with no ``grading_anchor_utc`` says nothing about WHICH window it belongs to, so
    it cannot disambiguate one; counting it would let an unanchored row veto a resolution
    that the one anchored row could have made.
    """
    rows = _row_list(pins) or []
    out = []
    for r in rows:
        _, sym = _first(r, _ROW_SYMBOL_KEYS)
        _, date = _first(r, _ROW_DATE_KEYS)
        if str(sym or "").strip().upper() != symbol or str(date or "").strip() != day:
            continue
        _, anchor = _first(r, _ROW_ANCHOR_UTC_KEYS)
        if anchor:
            out.append(r)
    return out


def find_manifest_row(
    doc: Any,
    symbol: str,
    day: str,
    *,
    manifest_id: Optional[str] = None,
    pins: Any = None,
) -> ManifestPick:
    """Pick the ONE manifest row this case benches, in three steps, most explicit first.

    WHY THIS IS NOT "the row for this symbol-day". The master-ledger layer emits one window
    per ledger LEG, so a symbol-day is routinely several windows. MEASURED 2026-09-04 against
    the 418-window manifest built from the operator's evidence tree: 217 symbol-days, of which
    62 carry more than one row. The eight lane-alive known-answer cases carry ILLR 5, SDOT 1,
    ZDAI 4, UPC 3, IPST 6, WETO(08-17) 1, PFSA 2, SLE 1 — so before the selector below existed
    five of the eight could not be benched at all. (Measured after: 7 of the 8 resolve. SLE
    2026-08-18 still does not, for an unrelated reason — its only clock field reads
    "premarket into the open" and no pin row exists for it, so ``resolve_window`` refuses
    rather than defaulting onto the open. That row needs an explicit win_start/win_end.)

      1. ``manifest_id`` given (``--cases SYMBOL:DATE:<id>``) — exact, and refused by name if
         it matches nothing or if it does not belong to this symbol-day. Nothing is inferred.
      2. exactly one row for the symbol-day — the pre-existing behaviour, unchanged.
      3. AMBIGUOUS, and the pins document holds exactly ONE anchored pin row whose
         ``manifest_id`` is among the candidates — that row wins, and the resolution says so.
         MEASURED on the same pair of documents (418 windows, 157 window-shaped pin rows):
         this rescues 3 of the 62 ambiguous symbol-days, and NONE of the eight lane-alive
         cases. It is a convenience, not the fix; step 1 is the fix.

    Otherwise: REFUSE, listing every candidate id so the operator can paste one back into
    ``--cases``. Two rows for one symbol-day are two different trades and picking one
    silently would bench an arbitrary window under the other window's name.
    """
    rows = _row_list(doc)
    if rows is None:
        raise SystemExit(
            "--manifest: no row list found. Looked for a top-level JSON array or one of "
            f"{list(_ROW_LIST_KEYS)}."
        )
    hits = []
    for r in rows:
        _, sym = _first(r, _ROW_SYMBOL_KEYS)
        _, date = _first(r, _ROW_DATE_KEYS)
        if str(sym or "").strip().upper() == symbol and str(date or "").strip() == day:
            hits.append(r)
    ids = tuple(_row_id(r) for r in hits)

    if manifest_id:
        exact = [r for r in hits if _row_id(r) == manifest_id]
        if len(exact) == 1:
            return ManifestPick(exact[0], manifest_id, "cases_manifest_id", ids)
        if not exact:
            # Distinguish "wrong symbol-day" from "no such id anywhere": the first is a typo
            # in the --cases line, the second is a stale manifest.
            elsewhere = [r for r in rows if _row_id(r) == manifest_id]
            if elsewhere:
                other = elsewhere[0]
                where = (f"it belongs to {_first(other, _ROW_SYMBOL_KEYS)[1]}:"
                         f"{_first(other, _ROW_DATE_KEYS)[1]}, not to {symbol}:{day}")
            else:
                where = "no row in the manifest carries it"
            raise SystemExit(
                f"--cases: {symbol}:{day}:{manifest_id} — {where}. Rows for {symbol}:{day}: "
                f"{list(ids)}"
            )
        raise SystemExit(
            f"--manifest: {len(exact)} rows carry manifest_id {manifest_id!r}. Ids are unique "
            "by construction (build_ross_manifest.py:891-894 raises on duplicates), so this "
            "manifest was assembled by hand or merged."
        )

    if not hits:
        raise SystemExit(f"--manifest: no row for {symbol}:{day}")
    if len(hits) == 1:
        return ManifestPick(hits[0], ids[0], "symbol_day_unique", ids)

    anchored = _anchored_pins_for(pins, symbol, day)
    by_id = {_row_id(r): r for r in hits if _row_id(r)}
    matched = [p for p in anchored if str(p.get("manifest_id") or "").strip() in by_id]
    if len(matched) == 1:
        pin = matched[0]
        pid = str(pin.get("manifest_id")).strip()
        k_anchor, v_anchor = _first(pin, _ROW_ANCHOR_UTC_KEYS)
        return ManifestPick(
            by_id[pid], pid,
            f"pin_anchor:{pid}:{k_anchor}={v_anchor}", ids,
        )

    raise SystemExit(
        f"--manifest: {len(hits)} rows for {symbol}:{day} — ambiguous. Name one with "
        f"--cases {symbol}:{day}:<manifest_id>. Candidates: {list(ids)}. "
        f"(The pins document offered {len(matched)} anchored row(s) for these ids, so it "
        "cannot decide either.)"
    )


def corpus_membership(corpus: Any, symbol: str, day: str) -> str:
    """``in`` / ``absent`` / ``unknown:<why>``. Never silently passes an unverified case."""
    if corpus is None:
        return "unknown:no_corpus_given"
    rows = _row_list(corpus)
    if rows is None:
        keys = sorted(corpus)[:12] if isinstance(corpus, dict) else type(corpus).__name__
        return f"unknown:unrecognised_corpus_shape:{keys}"
    for r in rows:
        _, sym = _first(r, _ROW_SYMBOL_KEYS)
        _, date = _first(r, _ROW_DATE_KEYS)
        if str(sym or "").strip().upper() == symbol and str(date or "").strip() == day:
            return "in"
    return "absent"


def pin_for_case(
    pins: Any,
    symbol: str,
    day: str,
    *,
    manifest_id: Optional[str] = None,
) -> tuple[Optional[Mapping[str, Any]], str]:
    """The pin row for THIS window -> ``(row_or_None, status)``.

    ``manifest_id`` is the join key and is tried first. It works because the pinner emits one
    row per manifest window carrying that id (rossbench_pin_ross_events.pin_window, row key
    ``manifest_id``, asserted present by ``assert_pin_row_contract``); verified against a real
    ``--offline`` run of it over the 418-window manifest — 157 window rows, 157 of them
    joined.

    The (symbol, date) fallback is kept for a pins document that predates that key, but it
    now REFUSES an ambiguous symbol-day instead of returning row 0. The old behaviour picked
    whichever row the file happened to list first, and what it fed —
    ``check_pin_sources`` — is a tape-provenance contradiction check, so a wrong row there
    means the "the pinned tape is the replayed tape" assertion is being made about a
    different window's tape. The caller refuses at plan time, before any subprocess.
    """
    rows = _row_list(pins)
    if rows is None:
        return None, "no_pins_document"
    if manifest_id:
        exact = [r for r in rows if str(r.get("manifest_id") or "").strip() == manifest_id]
        if len(exact) == 1:
            return exact[0], "manifest_id"
        if len(exact) > 1:
            return None, f"ambiguous:{len(exact)}_pin_rows_claim_manifest_id:{manifest_id}"
        # Fall through: a window with no pin row is normal — the pinner emits rows only for
        # the 157 ledger trade windows, not for all 418 manifest windows.
    same_day = []
    for r in rows:
        _, sym = _first(r, _ROW_SYMBOL_KEYS)
        _, date = _first(r, _ROW_DATE_KEYS)
        if str(sym or "").strip().upper() == symbol and str(date or "").strip() == day:
            same_day.append(r)
    if not same_day:
        return None, ("no_pin_row_for_manifest_id" if manifest_id else "no_pin_row")
    if len(same_day) > 1:
        seen = [str(r.get("manifest_id") or r.get("pin_id") or "?") for r in same_day]
        return None, f"ambiguous:{len(same_day)}_pin_rows_for_symbol_day:{seen}"
    return same_day[0], "symbol_day_unique"


# ─────────────────────────────────────────────────────────────────────────────
# ENV
# ─────────────────────────────────────────────────────────────────────────────

def contract_env(
    *,
    case: Case,
    build: str,
    source: str,
    sink: str,
    equity: float,
    risk: float,
    tick_stride: int,
    grid_step_s: float,
    exec_family: str,
    frame_warmup_min: float,
    json_out: str,
) -> dict[str, str]:
    """EXACTLY the contract keys, and nothing else. ``set(contract_env(...)) ==
    CONTRACT_ENV_KEYS`` is asserted by the contract test, so this cannot drift silently."""
    return {
        "PYTHONPATH": str(build),
        "CHILI_PYTEST": "1",
        "DATABASE_URL": str(source),
        "TEST_DATABASE_URL": str(sink),
        "SOURCE_FILTER": SOURCE_FILTER_VALUE,
        "SYMBOL": case.symbol,
        "WIN_START": case.win_start,
        "WIN_END": case.win_end,
        # The driver's frame depth comes from FRAME_WARMUP_MIN, not from an earlier
        # OHLCV_START: the tick mirror and the sim grid stay bound to the window while only
        # the OHLCV provider seam reaches back (replay_v3_fsm_window.py:157-167).
        "OHLCV_START": case.win_start,
        "FRAME_WARMUP_MIN": _num(frame_warmup_min),
        "TICK_STRIDE": str(int(tick_stride)),
        "GRID_STEP_S": _num(grid_step_s),
        "FULL_MIRROR": "1",
        "ARM": DRIVER_ARM_VALUE,
        "EQUITY": _num(equity),
        "RISK": _num(risk),
        "EXEC_FAMILY": str(exec_family),
        "REPLAY_JSON_OUT": str(json_out),
        "DIAG": "1",
        "ENTRY_DIAG": "1",
    }


def redact_db_url(url: Any) -> str:
    """A connection URL reduced to its DATABASE NAME, for anything written to disk.

    ``bench.json`` lands in an evidence directory that gets committed and shared, and the
    URLs this bench is given carry credentials (``postgresql://chili:chili@...``). The
    driver already made this call for its own receipt — it records ``_sim_db_name(PROD)``
    and ``_sim_db_name(SIM)`` rather than the URLs (replay_v3_fsm_window.py:792-793) — so
    this matches it. The real URL still reaches the subprocess env; only the artifact is
    redacted.

    ⚠️ Parse the URL; do NOT rsplit on "/". The replay lane's own socket URL is
    ``postgresql://chili:chili@/chili_test?host=/var/run/...``
    (docker-compose.replay-zero-egress.yml), whose QUERY STRING contains slashes — a naive
    tail-split returns "run". The driver hit the same trap from the other side and uses
    SQLAlchemy's parser for it (replay_v3_fsm_window.py:132-144); ``urlsplit`` is the
    stdlib equivalent and keeps this module dependency-free.
    """
    raw = str(url or "")
    if not raw:
        return ""
    path = urlsplit(raw).path if "//" in raw else raw
    name = path.rsplit("/", 1)[-1]
    return name or "(unnamed)"


def _redacted(mapping: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {k: (redact_db_url(v) if k in keys else v) for k, v in mapping.items()}


def _num(value: float) -> str:
    """Render a number without a trailing ``.0`` so the receipt echo compares cleanly."""
    f = float(value)
    return str(int(f)) if f.is_integer() else repr(f)


def sanitise_parent_env(parent: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Copy the parent env MINUS every hindsight/contamination key, and say what was dropped.

    Stripping rather than merely not-setting is the point: whoever runs this bench very
    plausibly has the ledger exported in their shell, and ``REPLAY_KEEP_SINK=1`` survives
    in a terminal from an earlier accumulation study. Ambient ``CHILI_*`` flags are NOT
    stripped — they are part of the deployed configuration the lane runs under, and removing
    them would bench a lane that does not exist — but every one of them is RECORDED in
    bench.json so a reader can see which non-default flags were in force.
    """
    env: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in parent.items():
        if key in FORBIDDEN_ENV_KEYS or any(key.startswith(p) for p in FORBIDDEN_ENV_PREFIXES):
            dropped.append(key)
            continue
        env[key] = value
    return env, sorted(dropped)


def build_env(
    *,
    case: Case,
    arm: Arm,
    parent: Mapping[str, str],
    **contract_kwargs: Any,
) -> tuple[dict[str, str], list[str]]:
    """Sanitised parent + the exact contract + the arm's overrides.

    Order matters and is asserted by the contract test: the contract is applied AFTER the
    parent (so an ambient ``SYMBOL`` from a previous manual run cannot leak in), and the
    arm's overrides are applied last but can never touch a contract key because
    ``_load_arm_file`` already refused every protected name.
    """
    env, dropped = sanitise_parent_env(parent)
    env.update(contract_env(case=case, **contract_kwargs))
    for key, value in (arm.overrides or {}).items():
        _refuse_forbidden_key(key, where=f"arm {arm.name!r}")
        if key in PROTECTED_ENV_KEYS:  # belt and braces; _load_arm_file already refused it
            raise SystemExit(f"arm {arm.name!r} may not set contract key {key!r}")
        env[key] = str(value)
    assert_clean_sink(env)  # invariant 2 — REPLAY_KEEP_SINK must not survive any of the above
    return env, dropped


def build_plan(cases: Sequence[Case], arms: Sequence[Arm]) -> list[tuple[Case, Arm]]:
    """CASE-MAJOR: every arm of one window runs before the next window's first arm."""
    plan = interleave(list(cases), list(arms))
    assert_interleaved(plan)   # proves it, rather than trusting the builder above
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# POST-RUN INVARIANTS (receipt-only — this bench never opens a database)
# ─────────────────────────────────────────────────────────────────────────────

def check_receipt_schema(receipt: Mapping[str, Any]) -> list[str]:
    got = str(receipt.get("schema") or "")
    if got != DRIVER_RESULT_SCHEMA:
        return [f"receipt schema {got!r} != {DRIVER_RESULT_SCHEMA!r} — the driver's receipt "
                "format changed under this bench; re-read the emission site before scoring"]
    return []


def check_env_bound(receipt: Mapping[str, Any], env: Mapping[str, str]) -> list[str]:
    """The driver echoes its whole env contract back (replay_v3_fsm_window.py:770-794).
    Comparing it to what we SENT is the only cheap proof that a knob actually bound.

    This is not hypothetical: ``EXEC_FAMILY`` was set by the nightly report for weeks while
    the seed site hard-coded ``robinhood_agentic_mcp``, so every "Alpaca" replay ran the
    Robinhood fill path (:187-192). An echoed value that does not match what we passed means
    the run measured something other than what was asked for.
    """
    echoed = receipt.get("env") or {}
    problems: list[str] = []
    for key in ("SYMBOL", "WIN_START", "WIN_END", "OHLCV_START", "EXEC_FAMILY"):
        want, got = env.get(key), echoed.get(key)
        if got is not None and str(got) != str(want):
            problems.append(f"env {key}: sent {want!r}, driver ran {got!r}")
    for key in ("EQUITY", "RISK", "GRID_STEP_S", "FRAME_WARMUP_MIN"):
        want, got = env.get(key), echoed.get(key)
        if got is None:
            continue
        try:
            if float(str(got)) != float(str(want)):
                problems.append(f"env {key}: sent {want!r}, driver ran {got!r}")
        except (TypeError, ValueError):
            problems.append(f"env {key}: driver echoed non-numeric {got!r}")
    if echoed.get("TICK_STRIDE") is not None and str(echoed["TICK_STRIDE"]) != str(env.get("TICK_STRIDE")):
        problems.append(f"env TICK_STRIDE: sent {env.get('TICK_STRIDE')!r}, driver ran {echoed['TICK_STRIDE']!r}")
    want_sources = [s for s in SOURCE_FILTER_VALUE.split(",") if s]
    got_sources = echoed.get("SOURCE_FILTER")
    if isinstance(got_sources, list) and got_sources != want_sources:
        problems.append(f"env SOURCE_FILTER: sent {want_sources}, driver ran {got_sources}")
    keep = echoed.get(KEEP_SINK_ENV)
    if keep not in (None, "", "0"):
        problems.append(f"{KEEP_SINK_ENV}={keep!r} was live inside the driver despite the fence")
    return problems


def check_nbbo_mirrored(receipt: Mapping[str, Any]) -> list[str]:
    """The NBBO tape must actually be in the sink.

    ⚠️ ``replay_harness_invariants`` exposes no ``assert_nbbo_mirrored`` in the version read
    while writing this (its ``__all__`` at :498-505 lists ten helpers and none is named
    that), so the check lives here, against the receipt's own ``mirrored.nbbo_rows``. If
    that module later grows one, this should move.

    Zero here is the tenth-layer defect: the FSM reads ``momentum_nbbo_spread_tape``
    DIRECTLY from the sink in three places — the 15 s micro-pullback frame
    (live_runner._build_micro_bar_df), the C1 phantom-loss cross-check, and the adaptive
    spread-cost veto's rolling percentiles — so an empty table means those three read
    silence and the bench scores a lane that never ran (replay_v3_fsm_window.py:37-46).
    """
    mirrored = receipt.get("mirrored") or {}
    try:
        rows = int(mirrored.get("nbbo_rows") or 0)
    except (TypeError, ValueError):
        rows = 0
    if rows <= 0:
        return ["mirrored.nbbo_rows == 0 — the NBBO mirror wrote nothing, so the "
                "micro-pullback detector and the spread-cost veto read an EMPTY table. "
                "This run measures silence; do not score it."]
    return []


def check_mock_parity(receipt: Mapping[str, Any]) -> list[str]:
    """Feed the receipt's recorded mock config back through invariant 9.

    ``assert_mock_parity`` reads ATTRIBUTES (replay_harness_invariants.py:472-476), so the
    receipt's ``mock`` dict is wrapped in a namespace. The values survive JSON round-trip
    intact because ``FillMode.CONSERVATIVE`` is the plain string ``"conservative"``
    (replay_mock_broker.py:129) rather than an enum member.
    """
    mock = receipt.get("mock")
    if not isinstance(mock, dict) or not mock:
        return ["receipt carries no mock config — invariant 9 (mock parity) cannot be checked"]
    try:
        assert_mock_parity(SimpleNamespace(**mock))
    except AssertionError as exc:
        return [str(exc)]
    return []


def check_tree_match(receipt: Mapping[str, Any], head: Optional[str]) -> list[str]:
    """The tree the SUBPROCESS ran must be the tree ``verify_tree`` blessed."""
    if not head:
        return []
    got = str(((receipt.get("tree") or {}).get("head")) or "")
    if got and not (got == head or got.startswith(head) or head.startswith(got)):
        return [f"receipt tree.head {got!r} != verified build HEAD {head!r} — the subprocess "
                "ran a different tree than the one this bench verified"]
    return []


def check_same_tape(receipt: Mapping[str, Any], reference: Mapping[str, Any]) -> list[str]:
    """Every arm of ONE case must see byte-for-byte the same tape.

    Arms may only flip behaviour flags (``PROTECTED_ENV_KEYS`` above), the mirror is
    tie-stable since 2026-09-04 (replay_v3_fsm_window.py:64-69), and each run resets its own
    sink — so the mirrored row counts and the grid length are expected to be IDENTICAL
    across a case's arms. A difference means the arms did not replay the same tape, which
    makes their PnL delta uncomparable regardless of how clean it looks.
    """
    problems: list[str] = []
    ref_m, got_m = (reference.get("mirrored") or {}), (receipt.get("mirrored") or {})
    for key in ("tick_rows", "nbbo_rows", "depth_rows"):
        if ref_m.get(key) != got_m.get(key):
            problems.append(f"mirrored.{key}: reference arm {ref_m.get(key)!r}, this arm {got_m.get(key)!r}")
    if reference.get("grid_steps") != receipt.get("grid_steps"):
        problems.append(f"grid_steps: reference arm {reference.get('grid_steps')!r}, "
                        f"this arm {receipt.get('grid_steps')!r}")
    if problems:
        return ["the arms of this case did NOT replay the same tape, so their delta is not a "
                "treatment effect: " + "; ".join(problems)]
    return []


_PIN_SOURCE_KEYS = ("sources", "source", "provider", "providers", "hydrate_provider")


def check_pin_sources(receipt: Mapping[str, Any], pin: Optional[Mapping[str, Any]]) -> str:
    """Compare the tape provenance the pin RECORDED against what the driver actually read.

    ``receipt["tape_sources"]`` is ``{table: {source: row_count}}`` — the driver's own
    per-table provenance survey (replay_v3_fsm_window.py:346-380). A pin naming a provider
    the run did not read (or the run reading one the pin does not name) means the pinned
    tape and the replayed tape are not the same tape.

    Returns a STATUS STRING rather than a problem list: the pins file is produced by
    another step and this bench cannot verify its schema, so "unverified" must stay
    distinguishable from "verified and matching". Only a real contradiction is reported as
    a mismatch.
    """
    if pin is None:
        return "unverified:no_pin_row"
    key, raw = _first(pin, _PIN_SOURCE_KEYS)
    if raw is None and isinstance(pin.get("tape"), Mapping):
        # chili.ross_event_pins.v1 nests the surveyed providers one level down, under
        # ``tape.sources`` (rossbench_pin_ross_events.pin_window; the key is in that
        # module's own PIN_ROW_REQUIRED_KEYS, so a rename fails its run rather than
        # silently emptying this check).
        key, raw = _first(pin["tape"], _PIN_SOURCE_KEYS)
        key = f"tape.{key}" if key else key
    if raw is None:
        return f"unverified:pin_row_names_no_source_field:{sorted(map(str, pin))[:12]}"
    if isinstance(raw, str):
        pinned = {s.strip() for s in raw.split(",") if s.strip()}
    elif isinstance(raw, (list, tuple, set)):
        pinned = {str(s).strip() for s in raw if str(s).strip()}
    elif isinstance(raw, dict):
        pinned = {str(s).strip() for s in raw}
    else:
        return f"unverified:pin.{key} is {type(raw).__name__}"
    observed: set[str] = set()
    for per_table in (receipt.get("tape_sources") or {}).values():
        if isinstance(per_table, dict):
            observed |= {str(s) for s, n in per_table.items() if n}
    if not observed:
        return "unverified:receipt_reports_no_tape_sources"
    if observed <= pinned:
        return f"match:{sorted(observed)}"
    return f"MISMATCH:pin.{key}={sorted(pinned)} but the run read {sorted(observed)}"


def sink_counts(receipt: Mapping[str, Any]) -> dict[str, int]:
    """The one contamination counter a receipt can supply.

    The driver's reset TRUNCATEs ``trading_automation_sessions`` (a sink seed,
    replay_v3_fsm_window.py:1246) with ``RESTART IDENTITY`` (:1469-1473), so a genuinely
    clean sink hands the next run the SAME low ``seed_session_id`` every time. A
    run-over-run increase is the additive signature ``additive_count_check`` exists to
    catch (replay_harness_invariants.py:433-455).
    """
    try:
        return {"seed_session_id": int(receipt.get("seed_session_id") or 0)}
    except (TypeError, ValueError):
        return {}


def post_run_invariants(
    receipt: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    head: Optional[str],
    reference: Optional[Mapping[str, Any]],
    previous_counts: Optional[Mapping[str, int]],
) -> list[str]:
    """Every check that reads only the receipt. Returns problems; the caller records them,
    marks the run unscoreable and keeps going — losing the remaining cases' evidence to an
    abort would cost more than it saves."""
    problems: list[str] = []
    problems += check_receipt_schema(receipt)
    problems += check_env_bound(receipt, env)
    problems += check_nbbo_mirrored(receipt)
    problems += check_mock_parity(receipt)
    problems += check_tree_match(receipt, head)
    if reference is not None:
        problems += check_same_tape(receipt, reference)
    if previous_counts:
        try:
            additive_count_check(previous_counts, sink_counts(receipt))
        except AssertionError as exc:
            problems.append(str(exc))
    return problems


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT RENDERING
# ─────────────────────────────────────────────────────────────────────────────

def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # newline="\n" — Windows text mode would otherwise rewrite every \n to \r\n and change
    # the bytes of an otherwise identical artifact.
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _ts_key(value: Any) -> str:
    """Sort key for a receipt timestamp. The driver stringifies both event ts
    (``str(e.ts)``, :1133) and fill ``trade_time`` (``default=str``, :827), so these are
    ISO-ish strings that sort correctly lexically; a missing ts sorts last, never first."""
    s = str(value or "")
    return s if s else "\uffff"


def timeline_rows(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Events and fills on ONE clock, in order. Receipt order is the tiebreak: the driver
    reads events ``ORDER BY id ASC`` (:1125-1126), which is insertion order."""
    rows: list[tuple[str, int, dict[str, Any]]] = []
    for idx, ev in enumerate(receipt.get("events") or []):
        payload = ev.get("payload") if isinstance(ev, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        rows.append((_ts_key(ev.get("ts") if isinstance(ev, dict) else None), idx, {
            "ts": (ev.get("ts") if isinstance(ev, dict) else None),
            "kind": "event",
            "what": (ev.get("event_type") if isinstance(ev, dict) else None),
            "why": payload.get("reason") or payload.get("trigger") or payload.get("blocked_trigger"),
            "payload": payload,
        }))
    offset = len(rows)
    for idx, fill in enumerate(receipt.get("fills") or []):
        if not isinstance(fill, dict):
            continue
        rows.append((_ts_key(fill.get("ts")), offset + idx, {
            "ts": fill.get("ts"),
            "kind": "fill",
            "what": str(fill.get("side") or "").upper() or "FILL",
            "why": None,
            "px": fill.get("px"),
            "qty": fill.get("qty"),
            "fee": fill.get("fee"),
            "order_id": fill.get("order_id"),
        }))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [r[2] for r in rows]


# The runner's own outputs. NOT "timeline.jsonl": scripts/rossbench_timeline.py writes that
# name into this same directory with a different schema (per-second
# chili.rossbench_timeline_row.v1, carrying first_divergence and per-event code_ref), and for
# as long as both used the name, whichever process ran last silently replaced the other's
# document. What this module writes is a raw merge of receipt["events"] and receipt["fills"]
# with neither field, so a reader that opened "timeline.jsonl" and got this one would look
# for a divergence flag that was never going to be there and conclude there was none.
EVENTS_FILE = "events.jsonl"
EVENT_TIMELINE_JSONL = "events_timeline.jsonl"
EVENT_TIMELINE_MD = "events_timeline.md"


def write_run_outputs(out_dir: str, receipt: Mapping[str, Any], case: Case, arm: Arm) -> None:
    """events.jsonl / events_timeline.jsonl / events_timeline.md beside the driver's run.json."""
    events = [e for e in (receipt.get("events") or []) if isinstance(e, dict)]
    _write_text(os.path.join(out_dir, EVENTS_FILE),
                "".join(json.dumps(e, default=str) + "\n" for e in events))
    rows = timeline_rows(receipt)
    _write_text(os.path.join(out_dir, EVENT_TIMELINE_JSONL),
                "".join(json.dumps(r, default=str) + "\n" for r in rows))

    hist = receipt.get("event_histogram") or {}
    top = sorted(hist.items(), key=lambda kv: (-int(kv[1] or 0), str(kv[0])))[:15]
    md = [
        f"# {case.symbol} {case.date} — arm `{arm.name}`",
        "",
        f"- manifest row: `{case.manifest_id or '(none recorded)'}`",
        f"- window (UTC): `{case.win_start}` .. `{case.win_end}`  ({case.anchor_source})",
        f"- final_state: `{receipt.get('final_state')}`  "
        f"entries={receipt.get('entries')} exits={receipt.get('exits')}",
        f"- pnl_usd: **{receipt.get('pnl_usd')}**  mtm_usd: {receipt.get('mtm_usd')}  "
        f"net_open_shares: {receipt.get('net_open_shares')}",
        f"- mirrored: {receipt.get('mirrored')}  grid_steps: {receipt.get('grid_steps')}",
        f"- execution_family: `{receipt.get('execution_family')}` venue: `{receipt.get('venue')}`",
        "",
        "PnL here is the driver's own arithmetic over its mock fills; it is NOT a broker",
        "result and it is not comparable to any run whose mock config or tape differs.",
        "",
        "## Event histogram (top 15)",
        "",
        "| event_type | n |",
        "| --- | ---: |",
    ]
    md += [f"| `{k}` | {v} |" for k, v in top]
    md += ["", "## Event log", "",
           "Receipt events and fills on one clock. This is NOT the analysed timeline — that "
           "is `timeline.md`, written by scripts/rossbench_timeline.py, and it is the one "
           "that carries the first-divergence call and the code refs.", "",
           "| ts | kind | what | why | px | qty |", "| --- | --- | --- | --- | ---: | ---: |"]
    for r in rows:
        md.append("| {ts} | {kind} | `{what}` | {why} | {px} | {qty} |".format(
            ts=str(r.get("ts") or ""),
            kind=r.get("kind"),
            what=str(r.get("what") or ""),
            why=str(r.get("why") or "")[:70].replace("|", "/"),
            px=("" if r.get("px") is None else r.get("px")),
            qty=("" if r.get("qty") is None else r.get("qty")),
        ))
    _write_text(os.path.join(out_dir, EVENT_TIMELINE_MD), "\n".join(md) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────────────

def run_one(
    *,
    case: Case,
    arm: Arm,
    env: Mapping[str, str],
    build: str,
    out_dir: str,
    timeout_s: float,
) -> dict[str, Any]:
    """One driver subprocess. Never raises for a driver failure — it records it."""
    driver = os.path.join(build, DRIVER_RELPATH)
    if not os.path.isfile(driver):
        return {"status": "no_driver", "error": f"driver not found: {driver}"}
    os.makedirs(out_dir, exist_ok=True)
    started = datetime.now(timezone.utc)
    try:
        proc = subprocess.run(
            [sys.executable, driver],
            env=dict(env), cwd=build, capture_output=True, text=True, timeout=float(timeout_s),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        rc: Optional[int] = proc.returncode
        status = "ok" if rc == 0 else "driver_failed"
    except subprocess.TimeoutExpired as exc:
        out = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") + \
              ((exc.stderr or "") if isinstance(exc.stderr, str) else "")
        rc, status = None, f"timeout_{int(float(timeout_s))}s"
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    return {
        "status": status,
        "returncode": rc,
        "duration_s": round(duration, 3),
        "stdout_tail": out.splitlines()[-STDOUT_TAIL_LINES:],
    }


def read_receipt(path: str) -> tuple[Optional[dict], Optional[str]]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as exc:
        return None, f"no receipt at {path}: {exc}"
    except ValueError as exc:
        return None, f"receipt at {path} is not JSON: {exc}"
    if not isinstance(doc, dict):
        return None, f"receipt at {path} is {type(doc).__name__}, expected an object"
    return doc, None


def _receipt_summary(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """The scoreable facts, lifted verbatim. No derived metric is invented here — grading
    is ``ross_replay_benchmark.py``'s job, and a bench that also graded would hide the
    boundary between what ran and what it is worth."""
    return {
        key: receipt.get(key)
        for key in (
            "pnl_usd", "mtm_usd", "net_open_shares", "cost_usd", "proceeds_usd",
            "entries", "exits", "final_state", "states_visited", "grid_steps",
            "mirrored", "density", "seed_session_id", "execution_family", "venue",
            "economic_seed_mode", "certification_eligible", "certification_failures",
            "tape_sources", "event_histogram",
        )
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ross_replay_bench",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Inputs.
    ap.add_argument("--manifest", required=True,
                    help="bench manifest JSON: one row per benched symbol-day window")
    ap.add_argument("--pins", default=None,
                    help="tape pin receipt JSON (chili.ross_event_pins.v1). Joined on "
                         "manifest_id; recorded, cross-checked against the driver's observed "
                         "tape_sources, used to disambiguate a symbol-day the manifest fans "
                         "out, and used as the LAST-RESORT window anchor for a manifest row "
                         "that states no clock")
    ap.add_argument("--corpus", default=None,
                    help="benchable-corpus JSON; every --cases entry must be a member")
    ap.add_argument("--cases", required=True, metavar="SYM:DATE[:MANIFEST_ID],...",
                    help="explicit case list. The third field names ONE manifest row and is "
                         "required whenever the symbol-day has several — measured on the "
                         "418-window manifest, 62 of 217 symbol-days do. There is no "
                         "'everything' mode: 30 of the ledger's 187 rows are no-trade records "
                         "and 11 more carry no clock")
    ap.add_argument("--arms", default="base", metavar="base[,name=arm.json...]",
                    help="'base' is the control arm (no overrides); 'name=file.json' is a "
                         "flat JSON object of env overrides for the driver subprocess")
    # Tree + databases.
    ap.add_argument("--build", required=True, help="build tree to run (becomes PYTHONPATH and cwd)")
    ap.add_argument("--ref", required=True, help="commit the build tree must be at (verify_tree)")
    ap.add_argument("--sentinel-file", default=DEFAULT_SENTINEL_FILE,
                    help="file verify_tree greps for --sentinel (default: the replay driver)")
    ap.add_argument("--sentinel", default=DEFAULT_SENTINEL,
                    help="string that must be present in --sentinel-file; the default is the "
                         "NBBO mirror, without which the spread veto and micro-pullback "
                         "detector read an empty table")
    ap.add_argument("--source", required=True,
                    help="READ-ONLY tape DB -> DATABASE_URL (no default: the driver would "
                         "otherwise fall back to the live chili database)")
    ap.add_argument("--sink", required=True,
                    help="throwaway sim DB -> TEST_DATABASE_URL; its NAME must end in _test")
    ap.add_argument("--out-dir", required=True, help="root of the <case>/<arm>/ output tree")
    # Measurement knobs. Every one of these is required or derived-with-a-citation; none
    # carries a silent default (see the module docstring, fence 4).
    ap.add_argument("--equity", required=True, type=float,
                    help="REQUIRED. Account equity the sizer works from. Two different size "
                         "canons exist in this project and mixing them has already produced a "
                         "false regression, so this is never defaulted")
    ap.add_argument("--risk", required=True, type=float,
                    help="REQUIRED. Per-trade risk budget, same reason as --equity")
    ap.add_argument("--tick-stride", type=int, default=DEFAULT_TICK_STRIDE,
                    help=f"default {DEFAULT_TICK_STRIDE} = every print. Anything above "
                         f"{DENSE_STRIDE_MAX} is refused by assert_dense_stride for a bench "
                         "question (measured: +$193.92 at stride 1, -$4.66 at stride 10)")
    ap.add_argument("--grid-step-s", required=True, type=float,
                    help="REQUIRED. Sim grid step seconds; it decides how often the FSM is "
                         "ticked at all, so a default here would be an invisible knob")
    ap.add_argument("--frame-warmup-min", type=float, default=FRAME_WARMUP_MIN_DEFAULT,
                    help=f"default {FRAME_WARMUP_MIN_DEFAULT} min = the 5d OHLCV period the "
                         "LIVE runner requests from its providers")
    ap.add_argument("--lead-s", type=float, default=None,
                    help="seconds of tape BEFORE the manifest anchor. Required only for cases "
                         "resolved from an anchor rather than an explicit window")
    ap.add_argument("--lag-s", type=float, default=None,
                    help="seconds of tape AFTER the manifest anchor; same condition as --lead-s")
    ap.add_argument("--exec-family", required=True,
                    help="REQUIRED. Execution rail -> EXEC_FAMILY. It was a silent no-op until "
                         "2026-09-04, so every 'Alpaca' replay actually ran the Robinhood "
                         "agentic fill path; naming it explicitly is the fix")
    ap.add_argument("--timeout-s", required=True, type=float,
                    help="REQUIRED. Per-run subprocess timeout. A too-short bound silently "
                         "truncates the bench into a shorter, cheaper, wrong answer")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve cases, build the interleaved plan and the exact env for "
                         "every run, write bench.json, and start no subprocess")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_parser().parse_args(argv)

    # Density floor FIRST, before any file is read or any sink is touched: a stride that
    # cannot answer the question makes everything after it pointless.
    assert_dense_stride(args.tick_stride, BENCH_QUESTION)

    manifest_doc, manifest_meta = load_json_input(args.manifest, "manifest")
    pins_doc, pins_meta = load_json_input(args.pins, "pins")
    corpus_doc, corpus_meta = load_json_input(args.corpus, "corpus")

    arms = parse_arm_spec(args.arms, base_dir=os.getcwd())
    cases: list[Case] = []
    case_records: list[dict[str, Any]] = []
    for symbol, day, wanted_id in parse_case_spec(args.cases):
        pick = find_manifest_row(manifest_doc, symbol, day,
                                 manifest_id=wanted_id, pins=pins_doc)
        # The pin is resolved BEFORE the window, because a manifest row that states no clock
        # can still be placed from the pin row's own grading anchor (resolve_window branch 4).
        pin, pin_resolution = pin_for_case(pins_doc, symbol, day,
                                           manifest_id=pick.manifest_id or None)
        if pin_resolution.startswith("ambiguous"):
            # Refused HERE, at plan time, and not inside pin_for_case: this is cheap (no
            # subprocess has started) and it is the same place find_manifest_row refuses, so
            # every "which row did you mean" question is answered before the sink is touched.
            raise SystemExit(
                f"--pins: {pin_resolution} for {symbol}:{day}. The pin row is what "
                "check_pin_sources compares the run's observed tape providers against, so the "
                "wrong one asserts provenance about a different window's tape. Name the "
                f"manifest row: --cases {symbol}:{day}:<manifest_id>."
            )
        window = resolve_window(pick.row, symbol=symbol, day=day,
                                lead_s=args.lead_s, lag_s=args.lag_s, pin=pin)
        # The window resolver knows nothing about which row it was handed, so the identity
        # is stamped here, where the pick was made.
        case = Case(
            symbol=window.symbol, date=window.date,
            win_start=window.win_start, win_end=window.win_end,
            anchor_source=window.anchor_source, anchor_detail=window.anchor_detail,
            manifest_id=pick.manifest_id,
            selector_explicit=bool(wanted_id),
        )
        membership = corpus_membership(corpus_doc, symbol, day)
        if membership == "absent":
            raise SystemExit(
                f"case {symbol}:{day} is NOT in --corpus {args.corpus!r}. The corpus is the "
                "list of symbol-days whose tape is actually present and pinned; benching "
                "outside it produces an empty or half-hydrated window that scores as a miss."
            )
        if membership.startswith("unknown"):
            logger.warning("[ross_replay_bench] corpus membership UNVERIFIED for %s:%s (%s)",
                           symbol, day, membership)
        cases.append(case)
        case_records.append({
            "case": str(case), "symbol": symbol, "date": day,
            "manifest_id": pick.manifest_id or None,
            "manifest_resolution": pick.resolution,
            "manifest_candidates": list(pick.candidates),
            "out_dirname": case.dirname,
            "win_start": case.win_start, "win_end": case.win_end,
            "anchor_source": case.anchor_source, "anchor_detail": case.anchor_detail,
            "corpus": membership,
            "pin_resolution": pin_resolution,
            "pin": pin,
        })

    # Two cases of one symbol-day are now legal, so their identities must be distinct or the
    # second would overwrite the first's output tree and ``interleave`` would de-duplicate
    # them (it keys on str(case), replay_harness_invariants.py:111).
    for label, seen in (("case id", [str(c) for c in cases]),
                        ("output directory", [c.dirname for c in cases])):
        dupes = sorted({v for v in seen if seen.count(v) > 1})
        if dupes:
            raise SystemExit(
                f"--cases: duplicate {label} {dupes}. Two benched windows would share one "
                "identity; name each one's manifest row explicitly."
            )

    plan = build_plan(cases, arms)

    parent = dict(os.environ)
    ambient_chili = {k: v for k, v in parent.items() if k.startswith("CHILI_")}
    out_root = os.path.abspath(args.out_dir)
    os.makedirs(out_root, exist_ok=True)

    head: Optional[str] = None
    if not args.dry_run:
        # Invariant 6, BEFORE the first run: a stale build tree once produced a confident
        # "the fix works" for code that was never checked out.
        head = verify_tree(args.build, args.ref, args.sentinel_file, args.sentinel)
        logger.info("[ross_replay_bench] build %s verified at %s", args.build, head)

    bench: dict[str, Any] = {
        "schema": BENCH_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "bench_python": sys.executable,
        "driver_result_schema": DRIVER_RESULT_SCHEMA,
        # --source/--sink carry credentials; bench.json is a shared evidence artifact.
        "args": _redacted({k: v for k, v in sorted(vars(args).items())}, ("source", "sink")),
        "inputs": {"manifest": manifest_meta, "pins": pins_meta, "corpus": corpus_meta},
        "tree": {"build": os.path.abspath(args.build), "ref": args.ref, "head": head,
                 "sentinel_file": args.sentinel_file, "sentinel": args.sentinel},
        "env_fence": {
            "forbidden_prefixes": list(FORBIDDEN_ENV_PREFIXES),
            "forbidden_keys": sorted(FORBIDDEN_ENV_KEYS),
            "stripped_from_parent": [],
            "ambient_chili_env": dict(sorted(ambient_chili.items())),
        },
        "arms": [{"name": a.name, "source": a.source, "overrides": a.overrides} for a in arms],
        "cases": case_records,
        "plan": [[str(c), str(a)] for (c, a) in plan],
        "runs": [],
    }

    reference: dict[str, dict] = {}     # case -> the first arm's receipt (tape reference)
    pins_by_case = {r["case"]: r.get("pin") for r in case_records}
    previous_counts: Optional[dict[str, int]] = None
    failures = 0

    # Loop-invariant: every run sanitises the SAME parent env, so record the drop list once.
    bench["env_fence"]["stripped_from_parent"] = sanitise_parent_env(parent)[1]

    for case, arm in plan:
        run_dir = os.path.join(out_root, case.dirname, arm.name)
        json_out = os.path.join(run_dir, "run.json")
        env, _dropped = build_env(
            case=case, arm=arm, parent=parent,
            build=os.path.abspath(args.build), source=args.source, sink=args.sink,
            equity=args.equity, risk=args.risk, tick_stride=args.tick_stride,
            grid_step_s=args.grid_step_s, exec_family=args.exec_family,
            frame_warmup_min=args.frame_warmup_min, json_out=json_out,
        )
        record: dict[str, Any] = {
            "case": str(case), "arm": arm.name, "out_dir": run_dir,
            # The window's own identity, repeated per run so a reader of one run record does
            # not have to join back to "cases" to learn which of a symbol-day's rows this was.
            "symbol": case.symbol, "date": case.date,
            "manifest_id": case.manifest_id or None,
            "contract_env": _redacted({k: env[k] for k in sorted(CONTRACT_ENV_KEYS)},
                                      ("DATABASE_URL", "TEST_DATABASE_URL")),
            "arm_overrides": arm.overrides,
        }

        if args.dry_run:
            record["status"] = "dry_run"
            bench["runs"].append(record)
            logger.info("[ross_replay_bench] DRY-RUN %s / %s -> %s", case, arm.name, run_dir)
            continue

        # A stale receipt from an earlier bench would be read back as this run's result if
        # the driver died before writing one.
        if os.path.exists(json_out):
            os.remove(json_out)

        logger.info("[ross_replay_bench] RUN %s / %s (%s..%s)",
                    case, arm.name, case.win_start, case.win_end)
        record.update(run_one(case=case, arm=arm, env=env, build=os.path.abspath(args.build),
                              out_dir=run_dir, timeout_s=args.timeout_s))

        receipt, err = read_receipt(json_out)
        problems: list[str] = []
        if receipt is None:
            problems.append(err or "no receipt")
        else:
            ref = reference.get(str(case))
            problems += post_run_invariants(
                receipt, env=env, head=head, reference=ref, previous_counts=previous_counts,
            )
            if ref is None:
                reference[str(case)] = dict(receipt)
            previous_counts = sink_counts(receipt) or previous_counts
            record["receipt"] = _receipt_summary(receipt)
            # Provenance, recorded either way: a pin whose provider set contradicts what the
            # run actually read means the pinned tape is not the replayed tape.
            pin_check = check_pin_sources(receipt, pins_by_case.get(str(case)))
            record["pin_check"] = pin_check
            if pin_check.startswith("MISMATCH"):
                problems.append(pin_check)
            elif pin_check.startswith("unverified"):
                logger.warning("[ross_replay_bench]   pin UNVERIFIED %s / %s: %s",
                               case, arm.name, pin_check)
            write_run_outputs(run_dir, receipt, case, arm)

        record["invariant_problems"] = problems
        record["scoreable"] = bool(record.get("status") == "ok" and receipt is not None and not problems)
        if not record["scoreable"]:
            failures += 1
            for p in problems:
                logger.error("[ross_replay_bench]   INVARIANT %s / %s: %s", case, arm.name, p)
        bench["runs"].append(record)

    bench["summary"] = {
        "runs": len(bench["runs"]),
        "scoreable": sum(1 for r in bench["runs"] if r.get("scoreable")),
        "failed": failures,
        "dry_run": bool(args.dry_run),
    }
    bench_path = os.path.join(out_root, "bench.json")
    _write_text(bench_path, json.dumps(bench, indent=2, default=str) + "\n")
    logger.info("[ross_replay_bench] wrote %s (%d run(s), %d unscoreable)",
                bench_path, len(bench["runs"]), failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
