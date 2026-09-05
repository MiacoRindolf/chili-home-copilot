#!/usr/bin/env python
"""STEP 3 of the Ross Parity Bench — pin every stated Ross entry/exit to the TAPE.

WHAT THIS IS FOR
----------------
``project_ws/AgentOps/ross/ross_master_ledger.json`` (schema
``chili.ross_master_ledger.v1``) records what Ross SAID he did. Its
``entry_time_et`` / ``exit_time_et`` are NARRATIVE fields, not timestamps —
measured on all 187 rows of the current ledger: 132/157 trade-ish rows start
with a clock, 14 carry one mid-sentence, 11 carry none at all. A bench that
grades CHILI against "09:41" without ever asking the tape whether anything
happened at 09:41 is grading a transcript, not a market.

This tool takes each stated time, builds a search window FROM THE STATED TIME
ALONE, reads a bounded slice of the hydrated tape (``chili_hydrated``,
``iqfeed_trade_ticks``) and records the exact SECOND the stated event most
plausibly happened, together with how confident that pin is.

Output: ``project_ws/AgentOps/ross/pins.json``, schema ``chili.ross_event_pins.v1``.

PIN CONTRACT — the exact key names, and what each one means
-----------------------------------------------------------
ONE ROW PER MANIFEST WINDOW. Not one row per leg: the consumer
(``app/services/trading/momentum_neural/ross_manifest_adapter.py``) joins on
``manifest_id`` and reads BOTH sides off the same row, and its (symbol, date)
fallback refuses a symbol-day that carries more than one pin row. An earlier
per-leg layout produced two rows per window and was measured scoring 0 of 418
cases (243 ``pin_duplicate_rows``, 175 ``pin_missing``). Do not re-split it.

JOIN KEY
  ``manifest_id``            The ``chili.ross_ground_truth_manifest.v1`` window
                             this row grades. Read by ross_manifest_adapter's
                             ``_pin_id``. ``null`` when the join could not be
                             resolved — never a guess.
  ``manifest_id_basis``      HOW manifest_id was resolved; one of
                             ``MANIFEST_ID_BASES``. ``layer4_exact`` = the id
                             this tool derived is present in the manifest it was
                             handed; ``absorbed_into`` = the ledger row was
                             folded into an older layer's window and this is
                             THAT window's id; ``layer4_unverified`` = the
                             derived id could not be checked (stale/absent
                             manifest); ``no_symbol`` = unjoinable.
  ``ledger_manifest_id``     The layer-4 id derived from the ledger row itself,
                             kept even when ``manifest_id`` differs, so an
                             absorption is auditable.
  ``symbol`` / ``date``      The (symbol, date) fallback key. ``symbol`` is
                             spelled as the MANIFEST spells it (see
                             ``normalize_ledger_symbol``); ``symbol_verbatim``
                             keeps the ledger's own text.

PER-SIDE PIN (what the adapter turns into a grading window)
  ``entry_ts_utc_pinned``    Entry instant, ``...Z``, or null. Adapter:
                             ``_event_pin(row, "entry")``.
  ``exit_ts_utc_pinned``     Exit instant, ``...Z``, or null. Becomes the window
                             END when present; otherwise the adapter reads the
                             manifest's stated end.
  ``entry_pin_method`` /     Per-side method and confidence, from ``PIN_METHODS``
  ``entry_pin_confidence``   and ``PIN_CONFIDENCES``. The adapter certifies a
  ``exit_pin_method`` /      window only when the ENTRY side is
  ``exit_pin_confidence``    ``tape_confirmed`` by a method other than
                             ``stated_only``.

ROW-LEVEL INSTANT (what the bench runner and the timeline read)
  ``leg``                    Which side the row-level instant fields below
                             describe — "entry" whenever the ledger states an
                             entry time, else "exit", else null. The row still
                             carries BOTH sides above; this names the anchor.
  ``pin_id``                 Stable id of that anchor leg,
                             ``<video>::<symbol>::<date>::<leg>::r<row_index>``.
  ``pin_second_utc`` /       The anchor leg's pinned second (UTC / ET clock).
  ``pin_second_et``
  ``grading_anchor_utc``     The instant a grading window is built around: the
                             pin when there is one, else the stated time. Read
                             by scripts/ross_replay_bench.py:238-241.
  ``grading_anchor_source``  ``tape_pin`` | ``ross_stated``.
  ``lead_s``                 Explicitly null: the pre-event lead is the bench's
                             choice, not this tool's.
  ``pin_method`` /           The anchor leg's method/confidence, repeated at row
  ``pin_confidence``         level. The adapter prefers the per-side keys and
                             falls back to these.
  ``window_basis``           ``WINDOW_BASES`` — closed enum, no tape-derived
                             member.
  ``tape``                   ``{rows_in_window, first_utc, last_utc, sources,
                             error}`` unioned over the legs. ``tape.sources`` is
                             read by ross_replay_bench.check_pin_sources.
  ``legs``                   ``{"entry": {...}, "exit": {...}}`` — the FULL
                             per-leg pin record (search window, clusters,
                             per-leg notes). Deliberately NOT keyed ``entry`` /
                             ``exit`` at row level: the adapter treats a
                             row-level ``entry`` mapping as its nested layout
                             and would look for different key names inside it.

``assert_pin_row_contract`` checks the join key and the four per-side keys on
every emitted row, so a rename fails the run instead of silently emptying the
bench.

HINDSIGHT — THE WHOLE POINT
---------------------------
Pinning is the single easiest way to fake a good benchmark: nudge the window a
few seconds until CHILI's entry looks good, or anchor on the window's best
price and call it "Ross's fill". Three structural rules make that impossible
here, and ``tests/test_rossbench_pins.py`` binds all three:

  1. ``build_search_window`` TAKES NO TAPE. Its signature is checked at startup
     by ``assert_window_builder_is_tape_blind`` — a window cannot be a function
     of prices it has not been handed.
  2. Every pin is fenced back inside the window it was searched in
     (``assert_pin_in_window``). A pin outside the stated uncertainty is an
     AssertionError, not a silently better number.
  3. ``window_basis`` is a CLOSED enum of two values — "ross_pin_minus_lead"
     and "ross_stated_minus_lead". There is deliberately no "tape peak" basis
     to select, so a downstream consumer cannot be handed one.

And the pin is used for THREE things only: the grading window, window
placement, and the timeline overlay. It never becomes a price, a fill, or a
PnL. ``usage_constraints`` in the emitted document says so in-band, so a
consumer that reads only the JSON still gets the rule.

LEDGER REALITY THIS CODES FOR (measured on the current 187-row ledger)
---------------------------------------------------------------------
  * ``0`` is a NULL SENTINEL, not a price: entry_px 67 zeros, exit_px 103,
    shares 118, pnl_usd 30. ``_nz`` maps 0 -> None, so a sentinel can never
    become a price-match target (it would match every sub-penny print near
    zero, i.e. nothing, and would look like a clean "unpinned" for the wrong
    reason).
  * 30 of the 187 rows are NOT trades. They arrived through a ``walk_lists``
    heuristic from five different sub-schemas and are separated here by their
    ``_path`` (``no_trade_references``, ``misses_and_no_trades``,
    ``ross_no_trade_context`` are non-trade; ``trades`` and ``rows`` are
    trade-ish). They are counted and reported, never pinned.
  * ``account`` has three vocabularies; ``big`` collapses to ``main``.
  * Video timecodes (``[00:02:24.68-00:02:40.72]``) look exactly like clock
    ranges. They are stripped before any clock parse, and every surviving
    endpoint must fall inside the 04:00-20:00 ET session CHILI actually trades
    (the same bound ``scripts/hydration_coverage_report.py:60-66`` uses).

READ-ONLY. Touches ``chili_hydrated`` only; refuses ``chili`` / ``chili_test``
by database NAME. Every query is bounded by (symbol, observed_at) — the
``ix_iqfeed_trades_sym_at`` index at ``scripts/historical_tick_hydrator.py:520``
— and carries its own ``SET LOCAL statement_timeout``, because the tick table
is ~89.6M rows and an unbounded scan there is a lane outage, not a slow query.

Usage — REBUILD THE MANIFEST FIRST. ``--manifest`` supplies both the derived
halfwidth and the join key, so a stale manifest.json makes every row's
manifest_id ``layer4_unverified`` (the run says so, loudly, and continues):

  python scripts/build_ross_manifest.py                             # 1. manifest
  python scripts/rossbench_pin_ross_events.py                       # 2. pin the tape
  python scripts/rossbench_pin_ross_events.py --offline             # windows only, no DB
  python scripts/rossbench_pin_ross_events.py --symbol VRAX --limit 5
  # 3. check the join before benching anything:
  python -m app.services.trading.momentum_neural.ross_manifest_adapter \\
      --manifest project_ws/AgentOps/ross_video_evidence/manifest.json \\
      --pins project_ws/AgentOps/ross/pins.json

MEASURED end to end 2026-09-04 (fresh build -> real hydrated-tape pin run ->
adapter): 418 manifest windows, 157 pin rows, 157/157 joined on manifest_id,
11 cases scorable. The binding constraint is TAPE COVERAGE — 38 of the ledger's
72 traded symbol-days carry any hydrated ticks — not the join.
"""
from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import date as _date, datetime, time as _time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

SCHEMA = "chili.ross_event_pins.v1"

# Via zoneinfo, never a fixed offset: the ledger spans 2025-11 to 2026-08 and a
# hardcoded -4/-5 would shift a whole symbol-day by an hour across a DST edge.
# Same reasoning, same tz name as scripts/historical_tick_hydrator.py:158.
ET = ZoneInfo("America/New_York")

DEFAULT_LEDGER = _REPO / "project_ws" / "AgentOps" / "ross" / "ross_master_ledger.json"
DEFAULT_MANIFEST = _REPO / "project_ws" / "AgentOps" / "ross_video_evidence" / "manifest.json"
DEFAULT_OUT = _REPO / "project_ws" / "AgentOps" / "ross" / "pins.json"

# ─────────────────────────────────────────────────────────────────────────────
# LEDGER SHAPE
# ─────────────────────────────────────────────────────────────────────────────

# ``_path`` is the ONLY reliable trade/no-trade discriminator in the ledger: the
# builder's walk_lists heuristic merged five sub-schemas into one ``trades``
# array, and 30 of the 187 rows are miss / no-trade records that carry no
# side, no price and no clock. Keying off "has an entry_px" instead would
# silently promote a no-trade note into a graded trade.
TRADE_PATHS: tuple[str, ...] = ("trades", "rows")
NON_TRADE_PATHS: tuple[str, ...] = (
    "no_trade_references", "misses_and_no_trades", "ross_no_trade_context",
)

# The three account vocabularies observed in the ledger (main 49 / small 25 /
# big 16 / absent 97). "big" and "main" are the same account in Ross's own
# narration; "small" is the challenge account and is NEVER summed with it.
_ACCOUNT_COLLAPSE = {"big": "main", "main": "main", "small": "small"}

# ─────────────────────────────────────────────────────────────────────────────
# CLOCK PARSING
# ─────────────────────────────────────────────────────────────────────────────

# Video timecodes are the corpus's other bracketed clock: "[00:02:24.68-
# 00:02:40.72]" is a position in a YouTube recording, not a market time. They
# are stripped WHOLESALE before any parse — a partial guard (only the hour
# check below) already rejects them, but stripping first keeps a stray
# "[... 09:41 ...]" transcript citation from being read as the stated time.
_BRACKETED = re.compile(r"\[[^\]]*\]")

# HH:MM or HH:MM:SS, refusing to start or end inside a longer numeric token so
# "00:05:29.32" cannot yield "05:29".
_CLOCK_SRC = r"(?<![\d:.])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d.])"
_CLOCK_RE = re.compile(_CLOCK_SRC)
_LEADING_CLOCK_RE = re.compile(r"^\s*~?\s*" + _CLOCK_SRC)
_RANGE_RE = re.compile(
    _CLOCK_SRC + r"\s*(?:-|–|—|to |until |thru |through )\s*" + _CLOCK_SRC
)

# 04:00-20:00 ET is the session CHILI actually trades — the same bound
# scripts/hydration_coverage_report.py:60-66 uses to size a hydration request.
# Any "clock" outside it in this corpus is a video timecode, a duration, or a
# price, so this doubles as the timecode fence.
SESSION_OPEN_HOUR_ET = 4
SESSION_CLOSE_HOUR_ET = 20

# A UTC restatement of the SAME range ("~09:06-09:40 ET = 13:06-13:40Z") is
# common in the ledger. Counting it as a second range double-weights that row
# in the halfwidth distribution, and reading it as the ET window shifts the
# search 4-5 hours. Detected by the marker that follows the endpoint.
_UTC_MARKER_RE = re.compile(r"^\s*(?:z\b|utc\b)", re.IGNORECASE)

# Markers that a row's stated time came from a FRAME AUDIT of the broker panel
# / chart rather than from narration. These rows are the only ones whose stated
# clock is trustworthy to the second, which is why frame_audit_stated is tried
# first. Terms are taken from the ledger's own prose ("per frame audit",
# "arrow #1 f0470", "f0378 crop").
_FRAME_AUDIT_MARKERS: tuple[str, ...] = ("frame audit", "frame-audit", "arrow #", "crop")
_FRAME_CROP_RE = re.compile(r"\bf\d{3,5}\b")

# ─────────────────────────────────────────────────────────────────────────────
# PIN RESOLUTION AND TOLERANCE
# ─────────────────────────────────────────────────────────────────────────────

# The pin's stated output granularity is ONE SECOND ("record the exact second").
# Two matching prints in the same or adjacent seconds therefore cannot be
# distinguished as separate events by anything downstream, so they are one
# cluster. This is derived from the output contract, not chosen: widening it
# would merge genuinely distinct clusters and UNDER-report ambiguity, which is
# the direction that flatters the bench.
PIN_SECOND_RESOLUTION_S = 1.0

# Price-match floor, fixed by the step contract: max($0.01, spread_at_t).
# $0.01 is also the minimum quotable increment for NMS stocks priced >= $1.00
# (SEC Rule 612), so no printed price can sit closer to a stated price than
# this without being the same tick. CAVEAT, recorded per-pin rather than
# silently absorbed: for sub-$1.00 names the quoting increment is $0.0001, so
# this floor is LOOSER than the tape's own resolution and such a pin is tagged
# ``sub_dollar_tolerance_is_loose`` in its notes.
MIN_PRICE_TOLERANCE_USD = 0.01

# Liveness fence, NOT a measurement threshold: its value cannot change any pin,
# only turn a slow query into a recorded pin_error. Precedent for a per-query
# SET LOCAL fence on a bounded read is app/routers/trading_sub/paper_observer.py:31
# (2,000 ms for a single row); this read returns a multi-minute slice of a
# ~89.6M-row table, so the fence is larger. Operator-overridable.
DEFAULT_STATEMENT_TIMEOUT_MS = 15_000

PIN_METHODS: tuple[str, ...] = (
    "frame_audit_stated", "price_match", "level_cross", "stated_only",
)
PIN_CONFIDENCES: tuple[str, ...] = ("tape_confirmed", "tape_ambiguous", "unpinned")

# CLOSED enum. There is deliberately no tape-derived basis to select: a window
# placed on a tape PEAK is hindsight, and the way to make that structurally
# impossible is to never mint the value in the first place.
WINDOW_BASES: tuple[str, ...] = ("ross_pin_minus_lead", "ross_stated_minus_lead")

USAGE_CONSTRAINTS: tuple[str, ...] = (
    "A pin is used for the grading window, window placement and the timeline "
    "overlay ONLY. It is never a price, a fill, or a PnL.",
    "A pin is CONFIRMATION of a stated time, never a replacement for it: the "
    "search window is built from the stated time alone, before any tape read.",
    "A window is never widened to capture a better price. pin_confidence "
    "'unpinned' means the tape did not confirm the stated time — it does not "
    "license a wider search.",
    "window_basis is one of "
    + " | ".join(WINDOW_BASES)
    + "; a tape peak is not a legal window anchor.",
    "pin_confidence 'tape_ambiguous' lists every candidate cluster and pins the "
    "EARLIEST. Picking a later cluster because it grades better is hindsight.",
    "lead_s is null here on purpose: this tool does not choose the bench's "
    "pre-event lead. Subtract the lead from grading_anchor_utc.",
    "ONE ROW PER MANIFEST WINDOW, carrying both sides. Join on manifest_id; "
    "read entry_ts_utc_pinned / exit_ts_utc_pinned and the per-side "
    "<leg>_pin_method / <leg>_pin_confidence. See the PIN CONTRACT block in "
    "scripts/rossbench_pin_ross_events.py.",
)

# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST JOIN — how a ledger row finds its manifest window
# ─────────────────────────────────────────────────────────────────────────────

# HOW manifest_id was resolved, recorded per row. A closed enum for the same
# reason WINDOW_BASES is one: a consumer must be able to tell a verified join
# from an unverified guess without re-reading the manifest.
MANIFEST_ID_BASES: tuple[str, ...] = (
    "layer4_exact",       # the derived id is present in the manifest handed in
    "absorbed_into",      # the ledger row was folded into an older layer's row
    "layer4_unverified",  # derived, but no manifest was available to check it
    "no_symbol",          # the ledger row names no ticker; nothing to key on
)

# build_ross_manifest's layer 4 mints "<video_id>::<symbol>::<date>::ml<n>",
# where n counts the ledger rows sharing that triple, in ledger order, over
# EVERY row that has a symbol (no-trade rows included) —
# scripts/build_ross_manifest.py:642-644. The ordinal is therefore not a
# trade-row ordinal, which is why iter_ross_windows advances the counter for
# rows it does not pin.
_LAYER4_ORDINAL_FMT = "ml%d"

# _absorb_master (scripts/build_ross_manifest.py:778-781) folds a ledger row
# into an existing layer-1/2 window and records WHICH row in that window's
# notes; the absorbing row keeps its own manifest_id. That sentence is the only
# in-band record of the absorption, so it is parsed rather than guessed.
# MEASURED on a fresh build() of this tree (2026-09-04): 418 windows,
# 140 standalone master_ledger rows + 47 absorbed = the ledger's 187 rows, with
# 0 ledger rows unaccounted for.
_ABSORB_NOTE_RE = re.compile(r"Merged with master-ledger row (\S+) on ")

# The keys of a chili.ross_ground_truth_manifest.v1 row this module reads. Named
# here so the join code never spells them inline.
_MANIFEST_WINDOWS_KEY = "windows"
_MANIFEST_ID_KEY = "manifest_id"


# ─────────────────────────────────────────────────────────────────────────────
# SMALL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _nz(value: Any) -> Optional[float]:
    """0 is a NULL SENTINEL in this ledger — 67 entry_px, 103 exit_px, 118
    shares and 30 pnl_usd zeros are "absent", not "zero". Treating a 0 pnl as a
    real zero breaks Avoidance and Capture; treating a 0 price as a real price
    makes every price-match target unmatchable in a way that LOOKS like a clean
    'the tape disagrees'. Both failures are silent, so the None rule is
    load-bearing."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return None if v == 0.0 else v


def _iso_z(dt: Optional[datetime]) -> Optional[str]:
    """Naive-UTC datetime -> '...Z'. ``iqfeed_trade_ticks.observed_at`` is
    TIMESTAMP (naive UTC) — confirmed against information_schema by
    scripts/replay_v3_fsm_window.py:213 — so every instant in this module is
    carried naive-UTC and only stamped with a Z at the edge."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _et_clock_str(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    aware = dt.replace(tzinfo=timezone.utc).astimezone(ET)
    return aware.strftime("%H:%M:%S")


def et_to_utc(day: _date, seconds_of_day: int) -> datetime:
    """(ET calendar day, seconds past ET midnight) -> naive UTC instant."""
    base = datetime.combine(day, _time(0, 0), tzinfo=ET) + timedelta(seconds=int(seconds_of_day))
    return base.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_day(value: Any) -> Optional[_date]:
    try:
        return _date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _clock_seconds(match: re.Match) -> Optional[int]:
    """Seconds past midnight for a _CLOCK_SRC match group triple, or None when
    the hour falls outside the 04:00-20:00 ET session (a video timecode, a
    duration like '10:00 of run-up', or a price)."""
    hh, mm, ss = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
    if not (SESSION_OPEN_HOUR_ET <= hh <= SESSION_CLOSE_HOUR_ET):
        return None
    if mm > 59 or ss > 59:
        return None
    return hh * 3600 + mm * 60 + ss


def _has_seconds(match: re.Match) -> bool:
    return match.group(3) is not None


# ─────────────────────────────────────────────────────────────────────────────
# STATED TIME
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StatedTime:
    """What the ledger row actually says about WHEN, and nothing more."""
    raw: str
    kind: str                       # "range" | "point" | "none"
    point_s: Optional[int] = None   # seconds past ET midnight
    point_has_seconds: bool = False
    point_position: Optional[str] = None   # "leading" | "midsentence"
    range_lo_s: Optional[int] = None
    range_hi_s: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "raw": self.raw,
            "kind": self.kind,
            "clock_et": _hhmmss(self.point_s),
            "clock_has_seconds": self.point_has_seconds,
            "clock_position": self.point_position,
            "range_lo_et": _hhmmss(self.range_lo_s),
            "range_hi_et": _hhmmss(self.range_hi_s),
        }


def _hhmmss(secs: Optional[int]) -> Optional[str]:
    if secs is None:
        return None
    return "%02d:%02d:%02d" % (secs // 3600, (secs % 3600) // 60, secs % 60)


def _iter_ranges(text: str, *, skip_utc: bool = True) -> list[tuple[int, int, int]]:
    """Every EXPLICITLY STATED clock range in ``text`` as (lo_s, hi_s, start_pos).

    Bracketed video timecodes are stripped first; endpoints outside the session
    are dropped; a range immediately followed by a Z/UTC marker is skipped when
    ``skip_utc`` because it is a restatement of the ET range beside it, not a
    second observation.
    """
    cleaned = _BRACKETED.sub(" ", str(text or ""))
    out: list[tuple[int, int, int]] = []
    for m in _RANGE_RE.finditer(cleaned):
        # groups 1-3 are the LO endpoint (_clock_seconds reads those and applies
        # the session-hour fence); groups 4-6 are the HI endpoint.
        lo = _clock_seconds(m)
        if lo is None:
            continue
        hh, mm, ss = int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
        if not (SESSION_OPEN_HOUR_ET <= hh <= SESSION_CLOSE_HOUR_ET) or mm > 59 or ss > 59:
            continue
        hi = hh * 3600 + mm * 60 + ss
        if hi <= lo:
            continue  # crosses midnight or is malformed; not an interpretable range
        if skip_utc and _UTC_MARKER_RE.match(cleaned[m.end():m.end() + 6]):
            continue
        out.append((lo, hi, m.start()))
    return out


def parse_stated_time(raw: Any) -> StatedTime:
    """Parse one narrative ledger time field.

    Precedence, and why:
      * an explicitly stated RANGE is the operator's own uncertainty statement,
        so it wins as the search window even when a point is also given
        ("~07:45 (07:40-07:55)" searches 07:40-07:55);
      * otherwise a LEADING clock ("09:41 (approx; ...)") is the stated time;
      * otherwise the first in-session clock anywhere in the sentence
        ("not stated ('ADBB hit the scanner just as ...')" has none; 14 rows do);
      * otherwise nothing, and the event is reported unpinned rather than
        dropped.
    """
    text = str(raw or "")
    cleaned = _BRACKETED.sub(" ", text)
    ranges = _iter_ranges(text)

    point_s = None
    point_secs = False
    position = None
    lead = _LEADING_CLOCK_RE.match(cleaned)
    if lead is not None and _clock_seconds(lead) is not None:
        point_s, point_secs, position = _clock_seconds(lead), _has_seconds(lead), "leading"
    else:
        for m in _CLOCK_RE.finditer(cleaned):
            s = _clock_seconds(m)
            if s is None:
                continue
            # A mid-sentence clock inside a UTC restatement is not the ET time.
            if _UTC_MARKER_RE.match(cleaned[m.end():m.end() + 6]):
                continue
            point_s, point_secs, position = s, _has_seconds(m), "midsentence"
            break

    if ranges:
        lo, hi, _pos = ranges[0]
        return StatedTime(raw=text, kind="range", point_s=point_s,
                          point_has_seconds=point_secs, point_position=position,
                          range_lo_s=lo, range_hi_s=hi)
    if point_s is not None:
        return StatedTime(raw=text, kind="point", point_s=point_s,
                          point_has_seconds=point_secs, point_position=position)
    return StatedTime(raw=text, kind="none")


# ─────────────────────────────────────────────────────────────────────────────
# HALFWIDTH — DERIVED, NEVER A LITERAL
# ─────────────────────────────────────────────────────────────────────────────

# Fields whose VALUE is a time statement. Scanned by name so this survives
# whichever manifest the operator points at (the ground-truth manifest calls it
# window_et; the ledger calls it entry_time_et / exit_time_et).
_TIME_FIELD_MARKERS: tuple[str, ...] = ("time", "window")


def stated_range_widths_s(doc: Any) -> list[int]:
    """Every explicitly stated range width, in seconds, anywhere in ``doc``.

    Walks the whole JSON so the caller may hand this the ground-truth manifest,
    the master ledger, or a future bench manifest without this tool needing to
    know their shapes. Only string values under a key naming a time/window are
    considered — prose fields legitimately contain prices, percentages and
    video timecodes shaped like clocks.
    """
    widths: list[int] = []

    def walk(node: Any, key: Optional[str]) -> None:
        if isinstance(node, Mapping):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v, key)
        elif isinstance(node, str) and key and any(m in key.lower() for m in _TIME_FIELD_MARKERS):
            for lo, hi, _pos in _iter_ranges(node):
                widths.append(hi - lo)

    walk(doc, None)
    return widths


def derive_halfwidth_s(widths: Sequence[int]) -> float:
    """MEDIAN stated range width. Fails closed on an empty distribution.

    This is the whole reason there is no literal halfwidth in this file: the
    corpus itself states how precise Ross's times are, so the search tolerance
    is read off that distribution at run time rather than guessed. NOTE the
    consequence, which is deliberate and not a bug: a stated POINT is searched
    over ``2 x median`` seconds (point +/- halfwidth), i.e. twice the median
    stated range, because a point carries no stated bound of its own.
    """
    vals = [int(w) for w in widths if int(w) > 0]
    if not vals:
        raise ValueError(
            "derive_halfwidth_s: no explicitly stated ranges in the manifest — the "
            "search halfwidth is derived from that distribution and has NO default. "
            "Point --manifest at a document that states ranges, or pass an explicit "
            "--halfwidth-s / ROSSBENCH_PIN_HALFWIDTH_S."
        )
    return float(statistics.median(vals))


def resolve_halfwidth_s(manifest_doc: Any, *, override: Any = None) -> dict:
    """The halfwidth actually used, plus the provenance that proves it was derived."""
    widths = stated_range_widths_s(manifest_doc)
    if override is not None and str(override).strip() != "":
        value = float(override)
        if value <= 0:
            raise ValueError(f"resolve_halfwidth_s: halfwidth must be > 0, got {value!r}")
        return {
            "value_s": value,
            "basis": "operator_override",
            "n_stated_ranges": len(widths),
            "median_stated_range_s": (float(statistics.median(widths)) if widths else None),
            "min_stated_range_s": (min(widths) if widths else None),
            "max_stated_range_s": (max(widths) if widths else None),
        }
    return {
        "value_s": derive_halfwidth_s(widths),
        "basis": "median_stated_range_width",
        "n_stated_ranges": len(widths),
        "median_stated_range_s": float(statistics.median(widths)),
        "min_stated_range_s": min(widths),
        "max_stated_range_s": max(widths),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH WINDOW — TAPE-BLIND BY CONSTRUCTION
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SearchWindow:
    lo_utc: datetime
    hi_utc: datetime
    basis: str          # "stated_range" | "stated_point_pm_halfwidth"
    halfwidth_s: Optional[float]

    def as_dict(self) -> dict:
        return {
            "lo_utc": _iso_z(self.lo_utc),
            "hi_utc": _iso_z(self.hi_utc),
            "basis": self.basis,
            "halfwidth_s": self.halfwidth_s,
            "width_s": (self.hi_utc - self.lo_utc).total_seconds(),
        }


def build_search_window(day: _date, stated: StatedTime, halfwidth_s: float) -> Optional[SearchWindow]:
    """Search bounds from the STATED time and nothing else.

    ⚠️ This function must never gain a tape/price/fill parameter. A window that
    can see prices can be nudged toward a better one, and that is the exact
    hindsight this bench exists to rule out. ``assert_window_builder_is_tape_blind``
    enforces the signature at startup and in the test suite.

    Returns None when the row states no usable clock at all (11 of 157 trade-ish
    rows) — such an event is reported unpinned, never dropped.
    """
    if day is None or stated is None:
        return None
    if stated.kind == "range" and stated.range_lo_s is not None:
        return SearchWindow(
            lo_utc=et_to_utc(day, stated.range_lo_s),
            hi_utc=et_to_utc(day, stated.range_hi_s),
            basis="stated_range",
            halfwidth_s=None,
        )
    if stated.point_s is not None:
        hw = float(halfwidth_s)
        if hw <= 0:
            raise ValueError("build_search_window: halfwidth must be > 0")
        return SearchWindow(
            lo_utc=et_to_utc(day, stated.point_s) - timedelta(seconds=hw),
            hi_utc=et_to_utc(day, stated.point_s) + timedelta(seconds=hw),
            basis="stated_point_pm_halfwidth",
            halfwidth_s=hw,
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HINDSIGHT FENCES
# ─────────────────────────────────────────────────────────────────────────────

# Any parameter whose name contains one of these would let the window see the
# market it is supposed to be independent of.
_TAPE_ISH_PARAM_MARKERS: tuple[str, ...] = (
    "tape", "tick", "print", "price", "px", "quote", "bar", "fill", "row", "slice", "conn",
)


def assert_window_builder_is_tape_blind(fn: Callable = build_search_window) -> None:
    """The window builder may not accept anything derived from the market."""
    params = list(inspect.signature(fn).parameters)
    bad = [p for p in params if any(m in p.lower() for m in _TAPE_ISH_PARAM_MARKERS)]
    if bad:
        raise AssertionError(
            f"assert_window_builder_is_tape_blind: {getattr(fn, '__name__', fn)!r} takes "
            f"{bad!r} — a search window that can see the tape can be nudged toward a "
            "better price. The window is a function of the STATED time only."
        )


def assert_window_basis(value: str) -> str:
    """window_basis is a closed enum; a tape peak is not a legal anchor."""
    if value not in WINDOW_BASES:
        raise AssertionError(
            f"assert_window_basis: {value!r} is not one of {list(WINDOW_BASES)}. A window "
            "anchored on anything the tape revealed (a peak, a best fill, a favourable "
            "second) is hindsight, not a pin."
        )
    return value


def assert_pin_in_window(pin_ts: Optional[datetime], window: Optional[SearchWindow]) -> None:
    """A pin must lie inside the window that was searched — the stated time plus
    its stated uncertainty. This is what makes "widen the window until the price
    is better" impossible after the fact rather than merely discouraged."""
    if pin_ts is None or window is None:
        return
    if not (window.lo_utc <= pin_ts <= window.hi_utc):
        raise AssertionError(
            f"assert_pin_in_window: pin {_iso_z(pin_ts)} falls outside the stated search "
            f"window [{_iso_z(window.lo_utc)}, {_iso_z(window.hi_utc)}]. A pin outside the "
            "stated uncertainty means the window was widened to reach it."
        )


# ─────────────────────────────────────────────────────────────────────────────
# TAPE MATCHING (PURE — the tests hand these a synthetic in-memory tape)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Cluster:
    """A contiguous run of matching prints, at the pin's own 1-second resolution."""
    first_utc: datetime
    last_utc: datetime
    n_prints: int
    first_px: float
    detail: str

    def as_dict(self) -> dict:
        return {
            "first_utc": _iso_z(self.first_utc),
            "last_utc": _iso_z(self.last_utc),
            "n_prints": self.n_prints,
            "first_px": self.first_px,
            "detail": self.detail,
        }


def _row_ts(row: Mapping[str, Any]) -> datetime:
    ts = row["observed_at"]
    return ts.replace(tzinfo=None) if getattr(ts, "tzinfo", None) else ts


def spread_at(row: Mapping[str, Any]) -> Optional[float]:
    """The at-trade spread carried on the tick row, or None when the quote is
    absent/crossed. IQFeed 6.2 L1 ships the last trade and top-of-book in one
    record, so this is a native measurement for ``iqfeed_lookup_hist`` rows and
    an as-of merge for ``polygon_v3_trades`` rows — see the derivation table at
    scripts/hydration_coverage_report.py (TRADE_QUOTE_DERIVATION)."""
    bid, ask = row.get("bid"), row.get("ask")
    if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return float(ask) - float(bid)


def price_tolerance(row: Mapping[str, Any], *, min_tol: float = MIN_PRICE_TOLERANCE_USD) -> float:
    """max($0.01, spread_at_t) — the step contract's tolerance, per print."""
    s = spread_at(row)
    return max(float(min_tol), float(s)) if s is not None else float(min_tol)


def cluster_hits(hits: Sequence[tuple[datetime, float, str]],
                 *, gap_s: float = PIN_SECOND_RESOLUTION_S) -> list[Cluster]:
    """Group matching prints into clusters at the pin's output resolution.

    Prints in the same or adjacent seconds are one event as far as anything
    downstream can tell, so they are one cluster. More than one cluster is
    reported as ``tape_ambiguous`` — an OUTCOME, not a failure. Many of these
    are expected: a stated 13-minute window around a $6.30 entry on a low-float
    mover legitimately contains several $6.30 prints.
    """
    clusters: list[Cluster] = []
    for ts, px, detail in sorted(hits, key=lambda h: (h[0], h[1])):
        if clusters and (ts - clusters[-1].last_utc).total_seconds() <= float(gap_s):
            prev = clusters[-1]
            clusters[-1] = Cluster(
                first_utc=prev.first_utc, last_utc=ts, n_prints=prev.n_prints + 1,
                first_px=prev.first_px, detail=prev.detail,
            )
        else:
            clusters.append(Cluster(first_utc=ts, last_utc=ts, n_prints=1,
                                    first_px=float(px), detail=detail))
    return clusters


def pin_frame_audit_stated(rows: Sequence[Mapping[str, Any]], stated_utc: datetime,
                           *, resolution_s: float = PIN_SECOND_RESOLUTION_S) -> list[Cluster]:
    """The stated second itself, CONFIRMED by a print inside it.

    Frame-audited rows are the only ones whose narration is trustworthy to the
    second (the broker panel / chart was read frame by frame), so their stated
    time is tried before any price reasoning. If the tape has no print in that
    second the method yields nothing and the chain falls through — a frame audit
    is evidence about Ross's screen, not proof that our tape covers it.
    """
    lo = stated_utc - timedelta(seconds=float(resolution_s))
    hi = stated_utc + timedelta(seconds=float(resolution_s))
    hits = [
        (_row_ts(r), float(r["price"]), "frame_audit_second")
        for r in rows if lo <= _row_ts(r) <= hi
    ]
    return cluster_hits(hits)


def pin_price_match(rows: Sequence[Mapping[str, Any]], target_px: float,
                    *, min_tol: float = MIN_PRICE_TOLERANCE_USD) -> list[Cluster]:
    """Prints within max($0.01, spread_at_t) of the STATED price.

    The tolerance band is centred on the price Ross stated and is NEVER
    re-centred on something the window revealed. A print 10% better than the
    stated fill is not a match; it is a different print.
    """
    hits: list[tuple[datetime, float, str]] = []
    for r in rows:
        px = r.get("price")
        if not isinstance(px, (int, float)) or px <= 0:
            continue
        tol = price_tolerance(r, min_tol=min_tol)
        if abs(float(px) - float(target_px)) <= tol:
            hits.append((_row_ts(r), float(px),
                         "price_match|target=%.4f|tol=%.4f" % (float(target_px), tol)))
    return cluster_hits(hits)


# Numeric tokens in the ``setup`` prose that could be a price level. The real
# guard is the slice's own price band (below): "500,000-share float", "109%" and
# "5.6M float" are all rejected by it without this regex needing to know what a
# float or a percentage is.
_LEVEL_TOKEN_RE = re.compile(r"(?<![\d.,:])\$?(\d{1,5}(?:\.\d{1,4})?)(?![\d.,:])")
_LEVEL_REJECT_SUFFIX_RE = re.compile(r"^\s*(?:%|percent\b|[MmKk]\b|million\b|shares?\b|x\b)")


def extract_price_levels(setup: Any, *, price_lo: float, price_hi: float) -> list[float]:
    """Price levels named in ``setup``, bounded by the slice's own price range.

    The plausibility band is DERIVED from the bounded slice under examination —
    a "level" outside the prices the window actually printed cannot be crossed
    inside that window, so it is not a level here. That is what keeps float
    sizes, percentages and share counts out without a hand-tuned price ceiling.
    """
    text = _BRACKETED.sub(" ", str(setup or ""))
    out: list[float] = []
    for m in _LEVEL_TOKEN_RE.finditer(text):
        if _LEVEL_REJECT_SUFFIX_RE.match(text[m.end():m.end() + 10]):
            continue
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if v <= 0 or not (float(price_lo) <= v <= float(price_hi)):
            continue
        if v not in out:
            out.append(v)
    return out


def pin_level_cross(rows: Sequence[Mapping[str, Any]], levels: Sequence[float]) -> list[Cluster]:
    """The first print at which the tape TRAVERSES a named level.

    A traversal is a consecutive pair of prints that brackets the level (in
    either direction), plus the degenerate case of the window's first print
    landing exactly on it. Direction is not assumed: the ledger names levels
    both as breakouts ("break of 25") and as failures ("the 9.96 rejection").
    """
    hits: list[tuple[datetime, float, str]] = []
    ordered = sorted(rows, key=_row_ts)
    for level in levels:
        lv = float(level)
        prev: Optional[Mapping[str, Any]] = None
        for r in ordered:
            px = r.get("price")
            if not isinstance(px, (int, float)) or px <= 0:
                continue
            px = float(px)
            if prev is None:
                if px == lv:
                    hits.append((_row_ts(r), px, "level_cross|level=%.4f|touch" % lv))
            else:
                p = float(prev["price"])
                if min(p, px) <= lv <= max(p, px):
                    hits.append((_row_ts(r), px, "level_cross|level=%.4f" % lv))
            prev = r
    return cluster_hits(hits)


# ─────────────────────────────────────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RossEvent:
    """One stated Ross entry or exit, before any tape has been read."""
    pin_id: str
    row_index: int
    ledger_path: str
    ledger_src: Optional[str]
    video_id: Optional[str]
    day: Optional[_date]
    date_raw: Any
    symbol: Optional[str]
    account: Optional[str]
    side: Optional[str]
    leg: str                       # "entry" | "exit"
    stated: StatedTime
    ross_px: Optional[float]       # the LEG's own stated price (0 -> None)
    setup: Optional[str]
    confidence: Optional[str]      # ledger's own: inferred / approx / exact
    frame_audited: bool


@dataclass(frozen=True)
class RossWindow:
    """ONE ledger row = ONE manifest window = ONE emitted pin row.

    ``legs`` holds the RossEvents the row actually states (entry, exit, one of
    them, or neither). The window is the unit the pins document is keyed on;
    the leg is the unit the tape is searched on.
    """
    manifest_id: Optional[str]
    manifest_id_basis: str
    ledger_manifest_id: Optional[str]
    row_index: int
    ledger_path: str
    ledger_src: Optional[str]
    video_id: Optional[str]
    day: Optional[_date]
    date_raw: Any
    symbol: Optional[str]
    symbol_verbatim: Any
    symbol_note: Optional[str]
    account: Optional[str]
    side: Optional[str]
    setup: Optional[str]
    confidence: Optional[str]
    entry_px: Optional[float]
    exit_px: Optional[float]
    legs: tuple[RossEvent, ...]

    def leg(self, name: str) -> Optional[RossEvent]:
        for ev in self.legs:
            if ev.leg == name:
                return ev
        return None

    @property
    def window_pin_id(self) -> str:
        """Id for a window whose ledger row states no leg at all (so there is no
        leg pin_id to borrow). Same shape, with "window" where a leg would be."""
        return "::".join(str(x) for x in (self.video_id, self.symbol, self.date_raw,
                                          "window", "r%d" % self.row_index))


def _is_frame_audited(*texts: Any) -> bool:
    blob = " ".join(str(t or "") for t in texts).lower()
    if any(m in blob for m in _FRAME_AUDIT_MARKERS):
        return True
    return bool(_FRAME_CROP_RE.search(blob))


def normalize_account(value: Any) -> Optional[str]:
    """main / small / big -> main / small. ``big`` and ``main`` are the same
    account; ``small`` is the challenge account and is never merged with it."""
    if value is None:
        return None
    return _ACCOUNT_COLLAPSE.get(str(value).strip().lower())


# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST JOIN
# ─────────────────────────────────────────────────────────────────────────────

def _manifest_builder():
    """Import the manifest builder lazily.

    Its symbol normaliser is IMPORTED rather than re-spelled here for the same
    reason ``_hydrator`` imports the hydrator's table name: ``manifest_id`` is
    built out of that value, so a private copy that drifted by one character
    would mint a join key matching nothing and the bench would report it as a
    clean ``pin_missing`` — a false negative that looks like missing evidence.
    Lazy so a caller that only wants the pure pin functions need not resolve the
    builder's evidence tree.
    """
    from scripts import build_ross_manifest as bld  # noqa: PLC0415
    return bld


def normalize_ledger_symbol(raw: Any) -> tuple[Optional[str], Optional[str]]:
    """(symbol as the MANIFEST spells it, the builder's own note) — never invents.

    MEASURED on the current 187-row ledger: 2 rows change spelling
    ('NUWE (09:30 pivot 5.34)' -> 'NUWE',
    'FCUV (4.80/break of 5; ...)' -> 'FCUV'). Both changes matter twice over:
    they make the manifest_id match, and they make the tape query ask for a
    ticker the tape actually carries. A slash-joined symbol ('EDBL/LGHL/BIYA')
    is deliberately NOT split by the builder and is returned verbatim here, so
    it stays un-joinable to a tape — the honest outcome.
    """
    return _manifest_builder()._master_symbol(raw)


def layer4_manifest_id(video_id: Any, symbol: Any, date: Any, ordinal: int) -> str:
    """The manifest_id build_ross_manifest layer 4 mints for one ledger row.

    Spelling and ordinal rule copied from scripts/build_ross_manifest.py:642-644
    and re-verified against a live ``build()`` (see ``_ABSORB_NOTE_RE`` above):
    all 187 derived ids resolve, 0 unaccounted for.
    """
    return "::".join((str(video_id), str(symbol), str(date),
                      _LAYER4_ORDINAL_FMT % int(ordinal)))


def _manifest_rows(manifest: Any) -> list[Mapping[str, Any]]:
    if not isinstance(manifest, Mapping):
        return []
    rows = manifest.get(_MANIFEST_WINDOWS_KEY)
    return [r for r in rows if isinstance(r, Mapping)] if isinstance(rows, (list, tuple)) else []


def manifest_window_ids(manifest: Any) -> set[str]:
    """Every manifest_id in the handed-in manifest document."""
    out = set()
    for row in _manifest_rows(manifest):
        mid = row.get(_MANIFEST_ID_KEY)
        if isinstance(mid, str) and mid:
            out.add(mid)
    return out


def manifest_absorption_map(manifest: Any) -> dict[str, str]:
    """``layer-4 manifest_id -> the manifest_id of the window that absorbed it``.

    Parsed out of the absorbing row's own notes, which is where
    ``_absorb_master`` writes it. An absorbed ledger row has no window of its
    own, so without this map its pin would name an id no consumer can find.
    """
    out: dict[str, str] = {}
    for row in _manifest_rows(manifest):
        target = row.get(_MANIFEST_ID_KEY)
        if not isinstance(target, str) or not target:
            continue
        for m in _ABSORB_NOTE_RE.finditer(str(row.get("notes") or "")):
            out[m.group(1)] = target
    return out


def resolve_manifest_id(ledger_manifest_id: Optional[str], *,
                        known_ids: Optional[set] = None,
                        absorbed: Optional[Mapping[str, str]] = None,
                        manifest_available: bool = False) -> tuple[Optional[str], str]:
    """(manifest_id, basis). Fails to ``None`` rather than to a plausible guess."""
    if not ledger_manifest_id:
        return None, "no_symbol"
    if manifest_available:
        if known_ids and ledger_manifest_id in known_ids:
            return ledger_manifest_id, "layer4_exact"
        target = (absorbed or {}).get(ledger_manifest_id)
        if target:
            return target, "absorbed_into"
    # No manifest to check against (or a manifest that predates layer 4). The
    # derived id is still the id a freshly built manifest WILL carry for a
    # standalone ledger row, so it is emitted — labelled unverified, never
    # dressed up as a confirmed join.
    return ledger_manifest_id, "layer4_unverified"


def iter_ross_windows(ledger: Mapping[str, Any],
                      *, manifest: Any = None) -> tuple[list[RossWindow], list[dict]]:
    """(one RossWindow per pinnable ledger row, non-trade records).

    Split by ``_path``, never by "does it have a price": 30 of the 187 rows are
    miss / no-trade records merged in from five sub-schemas by the builder's
    walk_lists heuristic, and promoting one of those into a graded trade is a
    fabricated Ross trade.

    ``manifest`` is the ground-truth manifest this pin run will be joined
    against. It is used ONLY to resolve ``manifest_id`` (exact id, or the window
    that absorbed the ledger row); it never touches a window, a price or a pin.
    Passing None is legal and yields ``manifest_id_basis="layer4_unverified"``.

    The (video_id, symbol, date) ordinal advances for EVERY ledger row that
    names a symbol, including the no-trade rows this function does not pin,
    because build_ross_manifest.py:642-644 counts them too. Skipping them here
    would shift every subsequent ml<n> on that symbol-day by one.
    """
    rows = list(ledger.get("trades") or [])
    known_ids = manifest_window_ids(manifest)
    absorbed = manifest_absorption_map(manifest)
    manifest_available = bool(known_ids)

    windows: list[RossWindow] = []
    non_trades: list[dict] = []
    seen: dict[tuple, int] = {}
    for idx, row in enumerate(rows):
        path = str(row.get("_path") or "")
        video_id = row.get("video_id")
        date_raw = row.get("date")
        symbol, symbol_note = normalize_ledger_symbol(row.get("symbol"))

        ledger_manifest_id: Optional[str] = None
        if symbol and video_id and date_raw:
            key = (video_id, symbol, date_raw)
            ordinal = seen.get(key, 0) + 1
            seen[key] = ordinal
            ledger_manifest_id = layer4_manifest_id(video_id, symbol, date_raw, ordinal)

        if path in NON_TRADE_PATHS or path not in TRADE_PATHS:
            non_trades.append({
                "row_index": idx,
                "ledger_path": path,
                "video_id": video_id,
                "date": date_raw,
                "symbol": symbol,
                "ledger_manifest_id": ledger_manifest_id,
                "kind": row.get("kind"),
                "reason": ("non-trade record; not pinned" if path in NON_TRADE_PATHS
                           else "unrecognised _path; not pinned (fail closed)"),
            })
            continue

        day = _parse_day(date_raw)
        account = normalize_account(row.get("account"))
        side = row.get("side")
        setup = row.get("setup")
        confidence = row.get("confidence")
        legs: list[RossEvent] = []
        for leg, time_field, px_field in (("entry", "entry_time_et", "entry_px"),
                                          ("exit", "exit_time_et", "exit_px")):
            raw_time = row.get(time_field)
            if raw_time is None:
                continue
            legs.append(RossEvent(
                pin_id="::".join(str(x) for x in (video_id, symbol, date_raw, leg, "r%d" % idx)),
                row_index=idx,
                ledger_path=path,
                ledger_src=row.get("_src"),
                video_id=video_id,
                day=day,
                date_raw=date_raw,
                symbol=symbol,
                account=account,
                side=side,
                leg=leg,
                stated=parse_stated_time(raw_time),
                ross_px=_nz(row.get(px_field)),
                setup=setup,
                confidence=confidence,
                frame_audited=_is_frame_audited(raw_time, row.get("notes")),
            ))

        manifest_id, basis = resolve_manifest_id(
            ledger_manifest_id, known_ids=known_ids, absorbed=absorbed,
            manifest_available=manifest_available,
        )
        windows.append(RossWindow(
            manifest_id=manifest_id,
            manifest_id_basis=basis,
            ledger_manifest_id=ledger_manifest_id,
            row_index=idx,
            ledger_path=path,
            ledger_src=row.get("_src"),
            video_id=video_id,
            day=day,
            date_raw=date_raw,
            symbol=symbol,
            symbol_verbatim=row.get("symbol"),
            symbol_note=symbol_note,
            account=account,
            side=side,
            setup=setup,
            confidence=confidence,
            entry_px=_nz(row.get("entry_px")),
            exit_px=_nz(row.get("exit_px")),
            legs=tuple(legs),
        ))
    return windows, non_trades


def iter_ross_events(ledger: Mapping[str, Any]) -> tuple[list[RossEvent], list[dict]]:
    """Flat per-leg view of ``iter_ross_windows``.

    The LEG is still the unit the tape is searched on — one search window and
    one method chain per stated time — so the pin functions keep taking a
    RossEvent. Only the emitted DOCUMENT is keyed by window.
    """
    windows, non_trades = iter_ross_windows(ledger)
    return [ev for w in windows for ev in w.legs], non_trades


# ─────────────────────────────────────────────────────────────────────────────
# PINNING ONE EVENT
# ─────────────────────────────────────────────────────────────────────────────

def pin_event(event: RossEvent, rows: Optional[Sequence[Mapping[str, Any]]],
              window: Optional[SearchWindow], *,
              min_tol: float = MIN_PRICE_TOLERANCE_USD,
              tape_error: Optional[str] = None) -> dict:
    """Run the method chain over ONE bounded slice and emit the pin record.

    Chain order (fixed by the step contract):
      frame_audit_stated -> price_match -> level_cross -> stated_only

    ``rows`` must already be restricted to ``window``; ``assert_pin_in_window``
    re-checks the result so a caller that hands in a wider slice fails loudly
    instead of quietly producing a better-looking pin.

    This record is NOT a row of the pins document. ``pin_window`` folds one or
    two of these into the single window row the document emits, and keeps the
    full record here under ``legs.<entry|exit>`` — see the PIN CONTRACT block at
    the top of this module.
    """
    notes: list[str] = []
    rows = list(rows or [])
    clusters: list[Cluster] = []
    method = "stated_only"

    stated_point_utc = (
        et_to_utc(event.day, event.stated.point_s)
        if (event.day is not None and event.stated.point_s is not None) else None
    )

    if window is None:
        notes.append("no_stated_clock" if event.stated.kind == "none" else "no_search_window")
    if tape_error:
        notes.append("tape_error:" + tape_error)

    if rows:
        # 1. frame_audit_stated — only for rows whose stated time came from a
        #    frame audit AND is stated to the second.
        if (event.frame_audited and event.stated.point_has_seconds
                and stated_point_utc is not None):
            clusters = pin_frame_audit_stated(rows, stated_point_utc)
            if clusters:
                method = "frame_audit_stated"

        # 2. price_match — needs a real stated price for THIS leg (0 is a NULL
        #    sentinel, so a sentinel leg skips straight to level_cross).
        if not clusters and event.ross_px is not None:
            clusters = pin_price_match(rows, event.ross_px, min_tol=min_tol)
            if clusters:
                method = "price_match"
                if event.ross_px < 1.0:
                    notes.append("sub_dollar_tolerance_is_loose")

        # 3. level_cross — a level named in ``setup``, bounded by the slice's
        #    own price range.
        if not clusters and event.setup:
            prices = [float(r["price"]) for r in rows
                      if isinstance(r.get("price"), (int, float)) and r["price"] > 0]
            if prices:
                levels = extract_price_levels(event.setup, price_lo=min(prices), price_hi=max(prices))
                if levels:
                    clusters = pin_level_cross(rows, levels)
                    if clusters:
                        method = "level_cross"
                        notes.append("levels:" + ",".join("%g" % v for v in levels))

    if clusters:
        # EARLIEST cluster, always. Picking a later one because it grades better
        # is the hindsight this file exists to prevent.
        chosen = min(clusters, key=lambda c: c.first_utc)
        pin_ts = chosen.first_utc.replace(microsecond=0)
        confidence = "tape_confirmed" if len(clusters) == 1 else "tape_ambiguous"
        window_basis = "ross_pin_minus_lead"
        if len(clusters) > 1:
            notes.append("clusters:%d" % len(clusters))
    else:
        chosen = None
        pin_ts = None
        confidence = "unpinned"
        window_basis = "ross_stated_minus_lead"
        if rows and method == "stated_only":
            notes.append("no_method_matched")
        elif not rows and window is not None and not tape_error:
            notes.append("no_tape_rows_in_window")

    assert_pin_in_window(pin_ts, window)
    assert_window_basis(window_basis)

    sources = sorted({str(r.get("source")) for r in rows if r.get("source")})
    if len(sources) > 1:
        # Measured TMCR 2026-08-24 (scripts/replay_v3_fsm_window.py:176-182): a
        # double-hydrated symbol-day returns both providers' tapes concatenated —
        # every price printed twice, nothing about the rows looking malformed.
        # It cannot move a pin EARLIER, but it inflates cluster counts.
        notes.append("multi_source_slice")

    return {
        "pin_id": event.pin_id,
        "row_index": event.row_index,
        "ledger_path": event.ledger_path,
        "ledger_src": event.ledger_src,
        "video_id": event.video_id,
        "date": event.date_raw,
        "symbol": event.symbol,
        "account": event.account,
        "side": event.side,
        "leg": event.leg,
        "ross_px": event.ross_px,
        "ledger_confidence": event.confidence,
        "frame_audited": event.frame_audited,
        "stated": event.stated.as_dict(),
        "stated_utc": _iso_z(stated_point_utc),
        "search_window_utc": (window.as_dict() if window is not None else None),
        "pin_method": method,
        "pin_confidence": confidence,
        "pin_second_utc": _iso_z(pin_ts),
        "pin_second_et": _et_clock_str(pin_ts),
        "pin_detail": (chosen.detail if chosen is not None else None),
        "clusters": [c.as_dict() for c in clusters],
        "n_clusters": len(clusters),
        "window_basis": window_basis,
        # What a downstream window must be built from. The LEAD is not this
        # tool's to choose (see usage_constraints), so it is explicitly null
        # rather than absent — a missing key reads as zero to a careless consumer.
        "grading_anchor_utc": _iso_z(pin_ts if pin_ts is not None else stated_point_utc),
        "grading_anchor_source": ("tape_pin" if pin_ts is not None else "ross_stated"),
        "lead_s": None,
        "tape": {
            "rows_in_window": len(rows),
            "first_utc": (_iso_z(_row_ts(rows[0])) if rows else None),
            "last_utc": (_iso_z(_row_ts(rows[-1])) if rows else None),
            "sources": sources,
            "error": tape_error,
        },
        "notes": notes,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ONE ROW PER MANIFEST WINDOW
# ─────────────────────────────────────────────────────────────────────────────

# Every key the consumer joins or grades on. Checked on each emitted row by
# ``assert_pin_row_contract`` — a rename then fails the run loudly instead of
# emptying the bench silently, which is exactly how the previous per-leg layout
# scored 0 of 418 cases without anything raising.
PIN_ROW_REQUIRED_KEYS: tuple[str, ...] = (
    "manifest_id", "manifest_id_basis", "symbol", "date",
    "entry_ts_utc_pinned", "exit_ts_utc_pinned",
    "entry_pin_method", "entry_pin_confidence",
    "exit_pin_method", "exit_pin_confidence",
    # kept for the bench runner and the timeline overlay
    "pin_id", "leg", "pin_second_utc", "grading_anchor_utc", "tape",
)


def assert_pin_row_contract(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """Every emitted row carries the whole PIN CONTRACT, or the run stops."""
    missing = [k for k in PIN_ROW_REQUIRED_KEYS if k not in row]
    if missing:
        raise AssertionError(
            "assert_pin_row_contract: emitted pin row is missing %r. These are the "
            "keys ross_manifest_adapter and ross_replay_bench read; a row without "
            "them joins to nothing and the bench reports it as missing evidence "
            "rather than as a broken contract." % (missing,)
        )
    # A nested {"entry": {...}} at row level is the adapter's OTHER accepted
    # layout and reads different key names inside; emitting both shapes at once
    # would make which one wins depend on the adapter's branch order.
    for reserved in ("entry", "exit"):
        if isinstance(row.get(reserved), Mapping):
            raise AssertionError(
                "assert_pin_row_contract: row-level %r is a mapping. The per-leg "
                "records belong under 'legs'; a row-level 'entry' mapping selects "
                "ross_manifest_adapter._event_pin's nested layout, which looks for "
                "ts_utc_pinned rather than entry_ts_utc_pinned." % (reserved,)
            )
    return row


def _leg_field(leg_pin: Optional[Mapping[str, Any]], key: str) -> Any:
    return None if leg_pin is None else leg_pin.get(key)


def pin_window(window: RossWindow,
               leg_pins: Mapping[str, Mapping[str, Any]]) -> dict:
    """Compose the ONE pins-document row for ONE manifest window.

    ``leg_pins`` maps "entry"/"exit" to that leg's ``pin_event`` record. Both
    sides land on the row (``entry_ts_utc_pinned`` / ``exit_ts_utc_pinned`` and
    per-side method/confidence) because the adapter reads both off a single row
    and its (symbol, date) fallback refuses a symbol-day carrying two.

    The ROW-LEVEL instant fields (``pin_second_utc``, ``grading_anchor_utc``,
    ``pin_id``, ``leg``) describe the ANCHOR leg — the entry when the ledger
    states one, else the exit. They are kept because the bench runner reads
    ``grading_anchor_utc`` (scripts/ross_replay_bench.py:238-241) and the
    timeline overlay reads the pinned second; ``leg`` says which side they mean
    so neither reader has to assume.
    """
    entry = leg_pins.get("entry")
    exit_ = leg_pins.get("exit")
    # An exit that does not come strictly AFTER the entry is not an exit pin. MEASURED
    # 2026-09-05 on the first winners sweep: PPCB 2026-08-27 t2 (and two more rows) carried
    # exit_ts_utc_pinned == entry_ts_utc_pinned -- the exit leg's level_cross search
    # ("~09:15-09:28 (not stated)") landed on the same print as the entry -- so the
    # timeline read Ross as `exited` at his entry second and the first divergence became
    # "exited/blocked". The exit is demoted to unpinned (Ross stays `filled`), the pinned
    # second is kept in pin_detail as provenance, and the row says why.
    _entry_s = _leg_field(entry, "pin_second_utc")
    _exit_s = _leg_field(exit_, "pin_second_utc")
    if _entry_s and _exit_s and str(_exit_s) <= str(_entry_s):
        demoted = dict(exit_)
        demoted.update({"pin_second_utc": None, "pin_second_et": None,
                        "pin_confidence": "unpinned",
                        "demotion": {"reason": "exit_not_after_entry",
                                     "pinned_second_utc_was": _exit_s,
                                     "entry_second_utc": _entry_s},
                        "notes": list(demoted.get("notes") or ()) + ["not_after_entry"]})
        exit_ = demoted
    anchor_leg = "entry" if entry is not None else ("exit" if exit_ is not None else None)
    anchor = entry if anchor_leg == "entry" else exit_

    notes: list[str] = []
    if window.symbol_note:
        notes.append("symbol:" + window.symbol_note)
    if window.manifest_id is None:
        notes.append("manifest_join:none")
    elif window.manifest_id_basis != "layer4_exact":
        notes.append("manifest_join:" + window.manifest_id_basis)
    if anchor is None:
        # The ledger row states neither an entry nor an exit time. It is still
        # emitted: the manifest has a window for it, and a silently absent pin
        # row is indistinguishable from a join that failed.
        notes.append("no_stated_leg")
    for name, leg_pin in (("entry", entry), ("exit", exit_)):
        for note in (_leg_field(leg_pin, "notes") or ()):
            notes.append("%s:%s" % (name, note))

    tapes = [p.get("tape") or {} for p in (entry, exit_) if p is not None]
    sources = sorted({s for t in tapes for s in (t.get("sources") or [])})
    errors = [t.get("error") for t in tapes if t.get("error")]
    firsts = [t.get("first_utc") for t in tapes if t.get("first_utc")]
    lasts = [t.get("last_utc") for t in tapes if t.get("last_utc")]

    row = {
        # --- join key
        "manifest_id": window.manifest_id,
        "manifest_id_basis": window.manifest_id_basis,
        "ledger_manifest_id": window.ledger_manifest_id,
        "row_index": window.row_index,
        "ledger_path": window.ledger_path,
        "ledger_src": window.ledger_src,
        "video_id": window.video_id,
        "date": window.date_raw,
        "symbol": window.symbol,
        "symbol_verbatim": window.symbol_verbatim,
        "account": window.account,
        "side": window.side,

        # --- per-side pin: what the adapter turns into a grading window
        "entry_ts_utc_pinned": _leg_field(entry, "pin_second_utc"),
        "exit_ts_utc_pinned": _leg_field(exit_, "pin_second_utc"),
        "entry_pin_method": _leg_field(entry, "pin_method"),
        "entry_pin_confidence": _leg_field(entry, "pin_confidence"),
        "exit_pin_method": _leg_field(exit_, "pin_method"),
        "exit_pin_confidence": _leg_field(exit_, "pin_confidence"),
        "legs_stated": [ev.leg for ev in window.legs],

        # --- Ross's own numbers, verbatim (never tape-derived)
        "ross_entry_px": window.entry_px,
        "ross_exit_px": window.exit_px,
        "ross_px": _leg_field(anchor, "ross_px"),
        "ledger_confidence": window.confidence,
        "frame_audited": bool(_leg_field(anchor, "frame_audited")),

        # --- row-level anchor (bench runner + timeline overlay)
        "pin_id": _leg_field(anchor, "pin_id") or window.window_pin_id,
        "leg": anchor_leg,
        "stated": _leg_field(anchor, "stated"),
        "stated_utc": _leg_field(anchor, "stated_utc"),
        "search_window_utc": _leg_field(anchor, "search_window_utc"),
        "pin_method": _leg_field(anchor, "pin_method"),
        "pin_confidence": _leg_field(anchor, "pin_confidence"),
        "pin_second_utc": _leg_field(anchor, "pin_second_utc"),
        "pin_second_et": _leg_field(anchor, "pin_second_et"),
        "pin_detail": _leg_field(anchor, "pin_detail"),
        "clusters": _leg_field(anchor, "clusters") or [],
        "n_clusters": _leg_field(anchor, "n_clusters") or 0,
        # An absent window_basis would read as "no rule applied"; a row with no
        # stated leg was never searched, so it takes the stated-time basis.
        "window_basis": _leg_field(anchor, "window_basis") or "ross_stated_minus_lead",
        "grading_anchor_utc": _leg_field(anchor, "grading_anchor_utc"),
        "grading_anchor_source": _leg_field(anchor, "grading_anchor_source"),
        "lead_s": None,

        "tape": {
            "rows_in_window": sum(int(t.get("rows_in_window") or 0) for t in tapes),
            "first_utc": (min(firsts) if firsts else None),
            "last_utc": (max(lasts) if lasts else None),
            "sources": sources,
            "error": (errors[0] if errors else None),
        },
        "notes": notes,
        # The full per-leg records, under 'legs' and NOT under 'entry'/'exit' —
        # see assert_pin_row_contract for why that distinction is load-bearing.
        # the (possibly demoted) leg records, not the raw leg_pins
        "legs": {name: lp for name, lp in (("entry", entry), ("exit", exit_)) if lp is not None},
    }
    return dict(assert_pin_row_contract(row))


# ─────────────────────────────────────────────────────────────────────────────
# DOCUMENT
# ─────────────────────────────────────────────────────────────────────────────

def _tally(pins: Sequence[Mapping[str, Any]], key: str) -> dict:
    out: dict[str, int] = {}
    for p in pins:
        out[str(p.get(key))] = out.get(str(p.get(key)), 0) + 1
    return dict(sorted(out.items()))


def _tally_present(pins: Sequence[Mapping[str, Any]], key: str) -> dict:
    """Like ``_tally`` but ignores rows that do not carry the key at all.

    Used for the window-level counts so that a caller handing ``build_pins_doc``
    a bare per-leg record (the tests do) does not get a spurious "None" bucket
    for a field only the window row has."""
    out: dict[str, int] = {}
    for p in pins:
        if key not in p:
            continue
        out[str(p.get(key))] = out.get(str(p.get(key)), 0) + 1
    return dict(sorted(out.items()))


def build_pins_doc(pins: Sequence[Mapping[str, Any]], non_trades: Sequence[Mapping[str, Any]],
                   *, provenance: Mapping[str, Any], generated_at: Optional[str] = None) -> dict:
    return {
        "schema": SCHEMA,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "built_by": "scripts/rossbench_pin_ross_events.py",
        "evidence_role": "window_placement_and_overlay_only",
        "usage_constraints": list(USAGE_CONSTRAINTS),
        "provenance": dict(provenance),
        "counts": {
            "pins": len(pins),
            "non_trade_records_skipped": len(non_trades),
            "by_pin_method": _tally(pins, "pin_method"),
            "by_pin_confidence": _tally(pins, "pin_confidence"),
            "by_window_basis": _tally(pins, "window_basis"),
            "by_leg": _tally(pins, "leg"),
            "with_search_window": sum(1 for p in pins if p.get("search_window_utc")),
            "tape_errors": sum(1 for p in pins if (p.get("tape") or {}).get("error")),
            # Window-level: how many rows the adapter can actually join, and how
            # each side landed. A high joined_to_manifest with an all-"unpinned"
            # entry histogram means the contract is fine and the TAPE is thin —
            # two very different failures that used to look identical.
            "joined_to_manifest": sum(1 for p in pins if p.get("manifest_id")),
            "by_manifest_id_basis": _tally_present(pins, "manifest_id_basis"),
            "by_entry_pin_confidence": _tally_present(pins, "entry_pin_confidence"),
            "by_exit_pin_confidence": _tally_present(pins, "exit_pin_confidence"),
            "by_entry_pin_method": _tally_present(pins, "entry_pin_method"),
        },
        "pins": list(pins),
        "non_trade_records": list(non_trades),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE (read-only, bounded, hydrated corpus only)
# ─────────────────────────────────────────────────────────────────────────────

# ONE string constant carrying its FROM and its ORDER BY together, in the shape
# replay_harness_invariants.assert_tie_stable_sql can prove tie-stable from the
# AST alone. Equal ``observed_at`` values are COMMON (a burst prints many rows
# inside one millisecond) and without the ``id`` tiebreak they come back in
# PHYSICAL SCAN ORDER — which would make the "earliest cluster" pin depend on
# heap layout. tests/test_rossbench_pins.py runs that invariant over this file.
_TAPE_SLICE_SQL = (
    "SELECT observed_at, price, size, bid, ask, source, id FROM iqfeed_trade_ticks "
    "WHERE symbol = %s AND observed_at >= %s AND observed_at < %s AND price > 0"
    "{source} "
    "ORDER BY observed_at ASC, id ASC"
)


def _hydrator():
    """Import the hydrator lazily.

    Its constants (table name, source tags, DSN resolution) are IMPORTED rather
    than re-spelled here — a drifted copy would query a tag nothing was written
    under and report a clean "0 rows", a false negative that looks like a
    coverage hole. Lazy because it pulls psycopg2, and the pure pin functions
    (and their tests) must import this module without a database driver.
    """
    from scripts import historical_tick_hydrator as hyd  # noqa: PLC0415
    return hyd


def resolve_hydrated_dsn(cli_dsn: Optional[str] = None) -> str:
    """DSN for the hydrated corpus, refusing the live and test databases.

    Hard rule: this tool is read-only against ``chili_hydrated``. The name check
    is on the parsed DATABASE NAME, never a substring of the URL — a suffix
    match on the whole URL passes ``.../chili?application_name=chili_hydrated``,
    which connects to PROD (the same trap scripts/replay_v3_fsm_window.py:132
    documents for the sim DB).
    """
    hyd = _hydrator()
    dsn = (cli_dsn or "").strip() or hyd.resolve_dsn()
    name = dsn.rpartition("/")[2].partition("?")[0]
    if name in ("chili", "chili_test"):
        raise SystemExit(
            f"refusing to read database {name!r}: the pin tool reads the HYDRATED corpus "
            "only (--dsn / HYDRATED_DATABASE_URL)."
        )
    if "hydrated" not in name:
        raise SystemExit(
            f"refusing to read database {name!r}: expected the hydrated corpus "
            f"(e.g. {hyd.DEFAULT_HYDRATED_DB!r}). Pass --dsn explicitly if this is wrong."
        )
    return dsn


def open_hydrated_conn(dsn: str):
    """Read-only connection with the session pinned to UTC.

    The UTC pin is the same correctness lock the hydrator applies
    (scripts/historical_tick_hydrator.py:762-774): naive text against a
    timestamptz column resolves through the SESSION time zone, and the same
    naive text was measured landing 7 h off under America/Los_Angeles.
    """
    import psycopg2  # noqa: PLC0415

    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True)
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
    conn.commit()
    return conn


def fetch_tape_slice(conn, symbol: str, lo_utc: datetime, hi_utc: datetime, *,
                     statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
                     sources: Sequence[str] = (),
                     max_rows: Optional[int] = None) -> list[dict]:
    """One bounded, index-friendly slice of the hydrated trade tape.

    Bounded by (symbol, observed_at) so the read rides ``ix_iqfeed_trades_sym_at``
    (scripts/historical_tick_hydrator.py:520) instead of scanning a ~89.6M-row
    table. Its own ``SET LOCAL statement_timeout`` is a liveness fence, and the
    transaction is rolled back immediately afterwards so ``query_start`` stays
    fresh — the db_watchdog kills backends >10 min from query_start (GOTCHA 11,
    scripts/replay_v3_fsm_window.py:539).

    ``observed_at`` is TIMESTAMP (naive UTC) in this table, so naive bounds are
    bound directly; a tz-aware bound would be coerced through the session zone.
    """
    hyd = _hydrator()
    if hyd.TRADES_TABLE != "iqfeed_trade_ticks":
        # Fail loud rather than query a table nothing is written to.
        raise RuntimeError(
            "fetch_tape_slice: the hydrator now writes %r but this module's SQL literal "
            "still says 'iqfeed_trade_ticks'." % (hyd.TRADES_TABLE,)
        )
    srcs = [s for s in (sources or []) if s]
    sql = _TAPE_SLICE_SQL.format(source=(" AND source = ANY(%s)" if srcs else ""))
    args: list[Any] = [symbol, lo_utc.replace(tzinfo=None), hi_utc.replace(tzinfo=None)]
    if srcs:
        args.append(srcs)
    out: list[dict] = []
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = %s", (f"{int(statement_timeout_ms)}ms",))
            cur.execute(sql, tuple(args))
            for observed_at, price, size, bid, ask, source, row_id in cur.fetchall():
                out.append({
                    "observed_at": observed_at,
                    "price": float(price) if price is not None else None,
                    "size": float(size) if size is not None else None,
                    "bid": float(bid) if bid is not None else None,
                    "ask": float(ask) if ask is not None else None,
                    "source": source,
                    "id": row_id,
                })
    finally:
        conn.rollback()
    if max_rows is not None and len(out) > int(max_rows):
        # Fail closed rather than truncate: a truncated slice hides later
        # clusters and would silently turn a tape_ambiguous into a
        # tape_confirmed — the direction that flatters the bench.
        raise RuntimeError(
            "fetch_tape_slice: %d rows exceeds --max-rows=%d for %s [%s, %s); widen the cap "
            "rather than accept a truncated slice." % (
                len(out), int(max_rows), symbol, _iso_z(lo_utc), _iso_z(hi_utc))
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _load_json(path: os.PathLike | str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: os.PathLike | str, doc: Mapping[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # newline="" — Windows text mode rewrites \n to \r\n and changes the bytes of
    # an otherwise identical receipt (reference_python_write_text_crlf_windows).
    with open(p, "w", newline="", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                    help="chili.ross_master_ledger.v1 document (the events to pin)")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST),
                    help="chili.ross_ground_truth_manifest.v1. TWO uses: its EXPLICITLY "
                         "STATED ranges derive the search halfwidth, and its manifest_ids "
                         "resolve the join key every emitted row carries. Rebuild it first "
                         "(python scripts/build_ross_manifest.py) or every join is "
                         "reported unverified.")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output pins.json")
    ap.add_argument("--dsn", default=None,
                    help="hydrated-corpus DSN (default: HYDRATED_DATABASE_URL, else "
                         "DATABASE_URL with the db name swapped to chili_hydrated)")
    ap.add_argument("--halfwidth-s", default=os.environ.get("ROSSBENCH_PIN_HALFWIDTH_S"),
                    help="OVERRIDE the derived halfwidth (recorded as such in provenance). "
                         "Unset = derive the median stated range width from --manifest.")
    ap.add_argument("--statement-timeout-ms", type=int, default=DEFAULT_STATEMENT_TIMEOUT_MS,
                    help="per-query liveness fence (cannot change a pin, only record an error)")
    ap.add_argument("--source", action="append", default=[],
                    help="restrict the tape to this hydration source (repeatable); "
                         "unset = no predicate, and a multi-source slice is tagged")
    ap.add_argument("--max-rows", type=int, default=None,
                    help="fail (never truncate) when a slice exceeds this many rows")
    ap.add_argument("--symbol", default=None, help="pin only this symbol")
    ap.add_argument("--date", default=None, help="pin only this ET date (YYYY-MM-DD)")
    ap.add_argument("--limit", type=int, default=None,
                    help="pin at most N manifest windows (a window is a ledger row and "
                         "carries up to two legs)")
    ap.add_argument("--offline", action="store_true",
                    help="build windows only, never touch the database (every pin is "
                         "stated_only / unpinned)")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)

    # Startup fence, in the fail-closed spirit of replay_harness_invariants: a
    # window builder that can see the tape invalidates every pin below it.
    assert_window_builder_is_tape_blind()

    ledger = _load_json(args.ledger)
    if str(ledger.get("schema") or "") != "chili.ross_master_ledger.v1":
        logger.warning("[rossbench_pin] ledger schema is %r, expected "
                       "chili.ross_master_ledger.v1", ledger.get("schema"))
    manifest = _load_json(args.manifest)
    halfwidth = resolve_halfwidth_s(manifest, override=args.halfwidth_s)
    logger.info("[rossbench_pin] halfwidth=%.1fs basis=%s (n_stated_ranges=%d, median=%s)",
                halfwidth["value_s"], halfwidth["basis"], halfwidth["n_stated_ranges"],
                halfwidth["median_stated_range_s"])

    windows, non_trades = iter_ross_windows(ledger, manifest=manifest)
    if args.symbol:
        windows = [w for w in windows if (w.symbol or "").upper() == args.symbol.upper()]
    if args.date:
        windows = [w for w in windows if str(w.date_raw) == args.date]
    if args.limit is not None:
        windows = windows[: int(args.limit)]
    unjoined = [w for w in windows if w.manifest_id is None]
    unverified = [w for w in windows if w.manifest_id_basis == "layer4_unverified"]
    logger.info("[rossbench_pin] %d manifest windows to pin, %d legs "
                "(%d non-trade ledger records skipped)",
                len(windows), sum(len(w.legs) for w in windows), len(non_trades))
    if unverified:
        # Not fatal, but it is the difference between a join and a hope: a
        # manifest that predates layer 4 carries no id for any ledger row.
        logger.warning("[rossbench_pin] %d/%d windows carry an UNVERIFIED manifest_id — "
                       "%s has no matching window. Rebuild it (python scripts/"
                       "build_ross_manifest.py) before the adapter runs.",
                       len(unverified), len(windows), args.manifest)
    if unjoined:
        logger.warning("[rossbench_pin] %d windows have NO manifest_id (no ticker in the "
                       "ledger row); they can only be joined by (symbol, date).",
                       len(unjoined))

    conn = None
    dsn_name = None
    if not args.offline:
        dsn = resolve_hydrated_dsn(args.dsn)
        dsn_name = dsn.rpartition("/")[2].partition("?")[0]
        conn = open_hydrated_conn(dsn)
        logger.info("[rossbench_pin] reading %s (read-only, statement_timeout=%dms)",
                    dsn_name, args.statement_timeout_ms)

    pins: list[dict] = []
    try:
        for win in windows:
            leg_pins: dict[str, dict] = {}
            for event in win.legs:
                window = build_search_window(event.day, event.stated, halfwidth["value_s"])
                rows: list[dict] = []
                tape_error = None
                if conn is not None and window is not None and event.symbol:
                    try:
                        rows = fetch_tape_slice(
                            conn, event.symbol, window.lo_utc, window.hi_utc,
                            statement_timeout_ms=args.statement_timeout_ms,
                            sources=args.source, max_rows=args.max_rows,
                        )
                    except Exception as exc:  # recorded, never swallowed
                        tape_error = f"{type(exc).__name__}: {exc}"
                        logger.warning("[rossbench_pin] %s: %s", event.pin_id, tape_error)
                leg_pins[event.leg] = pin_event(
                    event, rows, window,
                    min_tol=MIN_PRICE_TOLERANCE_USD, tape_error=tape_error)
            pins.append(pin_window(win, leg_pins))
    finally:
        if conn is not None:
            conn.close()

    provenance = {
        "ledger_path": str(args.ledger),
        "ledger_schema": ledger.get("schema"),
        "ledger_trade_rows": len(ledger.get("trades") or []),
        "manifest_path": str(args.manifest),
        "manifest_schema": (manifest.get("schema") if isinstance(manifest, Mapping) else None),
        "manifest_join": {
            "manifest_windows": len(_manifest_rows(manifest)),
            "absorption_notes": len(manifest_absorption_map(manifest)),
            "windows_emitted": len(windows),
            "unjoined": len(unjoined),
            "unverified": len(unverified),
        },
        "halfwidth": halfwidth,
        "pin_second_resolution_s": PIN_SECOND_RESOLUTION_S,
        "min_price_tolerance_usd": MIN_PRICE_TOLERANCE_USD,
        "tape": {
            "mode": "offline" if args.offline else "hydrated_read_only",
            "database": dsn_name,
            "table": "iqfeed_trade_ticks",
            "source_filter": list(args.source),
            "statement_timeout_ms": args.statement_timeout_ms,
            "max_rows": args.max_rows,
        },
        "filters": {"symbol": args.symbol, "date": args.date, "limit": args.limit},
    }
    doc = build_pins_doc(pins, non_trades, provenance=provenance)
    _write_json(args.out, doc)
    logger.info("[rossbench_pin] wrote %s: %d window rows  joined=%d  "
                "entry_confidence=%s  exit_confidence=%s",
                args.out, len(pins), doc["counts"]["joined_to_manifest"],
                doc["counts"]["by_entry_pin_confidence"],
                doc["counts"]["by_exit_pin_confidence"])
    return 0


__all__ = [
    "SCHEMA", "PIN_METHODS", "PIN_CONFIDENCES", "WINDOW_BASES", "USAGE_CONSTRAINTS",
    "TRADE_PATHS", "NON_TRADE_PATHS", "PIN_SECOND_RESOLUTION_S", "MIN_PRICE_TOLERANCE_USD",
    "DEFAULT_STATEMENT_TIMEOUT_MS", "SESSION_OPEN_HOUR_ET", "SESSION_CLOSE_HOUR_ET",
    "MANIFEST_ID_BASES", "PIN_ROW_REQUIRED_KEYS",
    "StatedTime", "SearchWindow", "Cluster", "RossEvent", "RossWindow",
    "parse_stated_time", "stated_range_widths_s", "derive_halfwidth_s", "resolve_halfwidth_s",
    "build_search_window", "et_to_utc", "normalize_account", "normalize_ledger_symbol",
    "layer4_manifest_id", "manifest_window_ids", "manifest_absorption_map",
    "resolve_manifest_id", "iter_ross_windows", "iter_ross_events",
    "spread_at", "price_tolerance", "cluster_hits", "extract_price_levels",
    "pin_frame_audit_stated", "pin_price_match", "pin_level_cross", "pin_event",
    "pin_window", "assert_pin_row_contract",
    "build_pins_doc", "assert_window_builder_is_tape_blind", "assert_window_basis",
    "assert_pin_in_window", "resolve_hydrated_dsn", "open_hydrated_conn", "fetch_tape_slice",
    "build_parser", "main",
]


if __name__ == "__main__":
    sys.exit(main())
