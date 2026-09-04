"""Ross Parity Bench — STEP 10: the per-case, second-by-second timeline.

WHY THIS EXISTS
---------------
The operator's standing rule for this bench: **no net/mean/rate sentence is allowed until a
per-case second-by-second timeline exists where you can point at ONE LINE and say "the money
was lost here, and this is the code"**. Every aggregate in this repo's history that was not
built on top of such a line has been wrong at least once — a 55% spread across three books,
a 67% win rate taught to a learner that had seen 3W/16L. So this module is deliberately the
*unit* of the bench: it produces a dense grid of seconds and, on each second, three things
side by side:

  1. THE TAPE      — what the market actually printed (last, bid/ask, cumulative size).
  2. ROSS          — what Ross did, from the pins, carrying the dollars AND the pin
                     confidence, so a reader never mistakes an inferred number for a
                     measured one.
  3. CHILI         — what the run actually emitted, mapped onto a divergence-stage ladder,
                     with ``code_ref = file:line`` resolved against the VERIFIED build tree
                     so a reader can jump straight to the branch that made the decision.

and marks the ``first_divergence`` row: the first second where the two sides are no longer in
the same stage.

WHAT IT CONSUMES (schemas owned by other components — see the header of each loader)
-----------------------------------------------------------------------------------
* ``run.json``  — schema ``chili.replay_v3_fsm_window_result.v1``, written by
  ``scripts/replay_v3_fsm_window.py`` (the writer is at replay_v3_fsm_window.py:1140-1183).
  This module reads ONLY: ``schema``, ``env.{SYMBOL,WIN_START,WIN_END}``, ``tree.{head,dirty}``,
  ``events[].{ts,event_type,payload}`` and ``fills[]``. The payload key set is NOT free-form:
  the receipt keeps only ``_load_bearing_payload`` (export_replay_v3_parity_fixtures.py:74-83
  -> avg / filled_size / fill_price / reason / pnl_usd / unrealized_pnl / bid / ask / stop /
  target / peak_r / order_id) plus ``_BENCH_PAYLOAD_KEYS`` (replay_v3_fsm_window.py:734-736
  -> reason / blocked_trigger / benched_at_hod / trigger / viability_score / errors). Anything
  else a caller hopes to render is simply not in the receipt.
* ``pins``      — schema ``chili.ross_event_pins.v1``, written by
  ``scripts/rossbench_pin_ross_events.py`` (the PIN CONTRACT block at its :21-95 names every
  key). That document is **one row per manifest window**, carrying BOTH legs; this module
  wants one pin per EVENT, so ``expand_pin_rows`` splits each window row into its entry and
  exit legs first and ``normalize_pin`` then reads a leg through the ``PIN_ALIASES`` table.
  Every alias that fires is recorded on the row (``_aliases``), the aggregate is written to
  ``meta.pin_alias_usage`` / ``meta.pin_fields_never_supplied``, and ``meta.ross_column_health``
  states outright whether the column came out empty — because it silently did once: the first
  alias table was written against the raw ``ross_master_ledger`` and produced
  ``kind=None, stage='unmapped', t_utc=None`` on all 157 real pin rows (measured 2026-09-04,
  reproduced below in ``ross_column_health``).
* ``tape``      — trade prints / NBBO quotes, from a JSONL file (primary) or, only behind an
  explicit non-production DSN guard, from a replay sink.

WHAT IT EMITS
-------------
* ``timeline.md``        — ``t ET | last | bid/ask | cum_vol | Ross | CHILI (stage) | code_ref``
* ``timeline.jsonl``     — one object per second, schema ``chili.rossbench_timeline_row.v1``
* ``timeline.meta.json`` — schema ``chili.rossbench_timeline_meta.v1``: the divergence rows,
  the code_ref index and its verification state, and EVERYTHING that could not be placed on
  the grid (unplaced pins, out-of-window events, unmapped event types). Nothing this module
  cannot place is dropped — an invisible drop is how "measuring silence" starts.

NO MAGIC NUMBERS
----------------
There is not one tunable numeric threshold in this file. The stage ladder is the repo's own
``CANONICAL_ENTRY_SPINE`` (replay_parity.py:59-66); the window comes from the run receipt or
the CLI; staleness is REPORTED as an age in seconds, never compared against a constant; and
the AM/PM reading of a bare Ross clock is resolved against the declared window, not against
an invented cutoff.

Runnable:
    python scripts/rossbench_timeline.py \
        --case-id 2026-06-30_CELZ --run-json run.g4_on.json --pins pins.json \
        --tape tape.jsonl --out-dir out/2026-06-30_CELZ
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

try:  # zoneinfo is stdlib on 3.11; the tzdata it needs is NOT guaranteed on Windows.
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - defensive; 3.11 always ships the module itself
    ZoneInfo = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ─── SCHEMAS ─────────────────────────────────────────────────────────────────────────────
SCHEMA_ROW = "chili.rossbench_timeline_row.v1"
SCHEMA_META = "chili.rossbench_timeline_meta.v1"

# The receipt this module consumes. Declared at replay_v3_fsm_window.py:199 and written into
# every run.json at :1141. A receipt that does not carry this string is a DIFFERENT document
# and is refused rather than best-effort parsed.
RUN_RESULT_SCHEMA = "chili.replay_v3_fsm_window_result.v1"

# Ross's tape is quoted in Eastern. The bench's clock is UTC. This is the only conversion in
# the file and it is done with a real tz database, never with a fixed offset: the ledger spans
# 2026-03 to 2026-08, i.e. both sides of a DST boundary, so a hardcoded "UTC = ET+4" (or +5,
# which several ledger note fields assert) is wrong for part of the corpus by construction.
ET_ZONE_NAME = "America/New_York"


# ─────────────────────────────────────────────────────────────────────────────────────────
# 1. THE DIVERGENCE-STAGE VOCABULARY
# ─────────────────────────────────────────────────────────────────────────────────────────
# DERIVATION, not invention. The order of the ADVANCING stages is the repo's own
# ``CANONICAL_ENTRY_SPINE`` — "the MINIMAL load-bearing skeleton every completed entry->exit
# session must exhibit" (app/services/trading/momentum_neural/replay_parity.py:59-66):
#
#     live_arm_confirmed -> live_watch_started -> live_entry_candidate_detected
#         -> live_entry_submitted -> live_entry_filled -> live_exit_filled
#
# with ``managed`` inserted between filled and exited for the in-position events
# (``live_partial_exit_filled``), because a partial is not an exit and collapsing the two
# would hide the exit-geometry lever the 2026-09-01 partial-exit study identified.
STAGE_ABSENT = "absent"
STAGE_ARMED = "armed"
STAGE_WATCHING = "watching"
STAGE_CANDIDATE = "candidate"
STAGE_SUBMITTED = "submitted"
STAGE_FILLED = "filled"
STAGE_MANAGED = "managed"
STAGE_EXITED = "exited"

ADVANCING_STAGES: tuple[str, ...] = (
    STAGE_ABSENT, STAGE_ARMED, STAGE_WATCHING, STAGE_CANDIDATE,
    STAGE_SUBMITTED, STAGE_FILLED, STAGE_MANAGED, STAGE_EXITED,
)
STAGE_RANK: dict[str, int] = {s: i for i, s in enumerate(ADVANCING_STAGES)}

# NON-ADVANCING annotations. They are displayed and counted but they must NOT raise a side's
# rank, because "CHILI cancelled before it ever filled" is not progress past "Ross filled" —
# ranking a cancel above a fill would invert the direction of the divergence and produce the
# exact false reading ("CHILI got further than Ross") this bench exists to prevent.
STAGE_BLOCKED = "blocked"      # a gate refused: this is usually where the money is lost
STAGE_RETIRED = "retired"      # cooldown / cancelled / recycled
STAGE_NO_TRADE = "no_trade"    # a Ross pin that records a deliberate pass
STAGE_UNMAPPED = "unmapped"    # the event type is not in the vocabulary — SURFACED, not dropped
NON_ADVANCING_STAGES: tuple[str, ...] = (
    STAGE_BLOCKED, STAGE_RETIRED, STAGE_NO_TRADE, STAGE_UNMAPPED,
)

DIVERGENCE_STAGES: tuple[str, ...] = ADVANCING_STAGES + NON_ADVANCING_STAGES

# The 13 load-bearing transitions, mapped exactly. Source of the vocabulary:
# replay_parity.py:41-55 (``LOAD_BEARING_TRANSITIONS``) and the identical tuple in
# scripts/export_replay_v3_parity_fixtures.py:43-57. These are the only event types the
# parity gate compares on, so they are the only ones whose stage must never be guessed.
EVENT_TYPE_STAGE: dict[str, str] = {
    "live_arm_requested": STAGE_ARMED,
    "live_arm_confirmed": STAGE_ARMED,
    "live_watch_started": STAGE_WATCHING,
    "live_entry_candidate_detected": STAGE_CANDIDATE,
    "live_entry_submitted": STAGE_SUBMITTED,
    "live_entry_filled": STAGE_FILLED,
    "live_partial_exit_filled": STAGE_MANAGED,
    "live_bailout": STAGE_EXITED,
    "live_tape_accel_reversal_exit": STAGE_EXITED,
    "live_exit_filled": STAGE_EXITED,
    "live_cooldown_started": STAGE_RETIRED,
    "live_cancelled": STAGE_RETIRED,
    "live_recycled": STAGE_RETIRED,
}

# Fallback for the HUNDREDS of other event types a real session emits (the receipt carries
# every event, not just the load-bearing 13 — replay_v3_fsm_window.py:1122-1139). Ordered:
# FIRST match wins, and the refusal markers are checked before everything else so that e.g.
# ``live_entry_void_while_paused_blocked`` (live_runner.py:5221) reads as ``blocked`` and not
# as ``filled``. ``blocked`` deliberately borrows the refusal markers from
# replay_harness_invariants.DECISIVE_EVENT_MARKERS so the two modules agree on what a
# commitment-refusing event looks like.
_STAGE_SUBSTRING_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("blocked", "veto", "benched", "rejected", "refused", "denied"), STAGE_BLOCKED),
    (("cooldown", "cancel", "recycl", "retire", "abandon"), STAGE_RETIRED),
    (("partial", "scale_out", "trim"), STAGE_MANAGED),
    (("exit_filled", "bailout", "reversal_exit", "stopped", "stop_filled",
      "liquidat", "flatten"), STAGE_EXITED),
    (("entry_filled", "fill_adopted", "entered"), STAGE_FILLED),
    (("submitted", "order_placed", "order_sent"), STAGE_SUBMITTED),
    (("candidate", "trigger_detected"), STAGE_CANDIDATE),
    (("watch",), STAGE_WATCHING),
    (("arm",), STAGE_ARMED),
)


def stage_for_event(event_type: str) -> str:
    """Map a CHILI ``event_type`` onto the divergence-stage vocabulary.

    Exact match on the 13 load-bearing transitions first (those must never be guessed), then
    the ordered substring rules, then ``unmapped`` — which is a VISIBLE outcome, counted in
    the meta document, not a silent drop."""
    et = str(event_type or "")
    exact = EVENT_TYPE_STAGE.get(et)
    if exact is not None:
        return exact
    low = et.lower()
    for markers, stage in _STAGE_SUBSTRING_RULES:
        if any(m in low for m in markers):
            return stage
    return STAGE_UNMAPPED


# Ross pin kinds -> the same ladder. NOTE THE ASYMMETRY, it is the whole point: a Ross pin is
# an EXECUTED trade recovered from video, so a Ross "entry" is a FILL (rank ``filled``), never
# a "submitted". CHILI can sit at ``submitted`` for a whole window; Ross cannot. Treating a
# Ross entry as ``submitted`` would make a CHILI run that submitted-and-never-filled look
# level with a Ross trade that actually printed.
_PIN_KIND_STAGE: tuple[tuple[tuple[str, ...], str], ...] = (
    (("no_trade", "notrade", "miss", "pass", "skip", "avoid", "watchlist_no_trade"),
     STAGE_NO_TRADE),
    (("partial", "trim", "scale_out", "sold_half", "half_out"), STAGE_MANAGED),
    (("exit", "sell", "sold", "stop", "cover", "close", "flat", "out"), STAGE_EXITED),
    (("entry", "buy", "bought", "long", "add", "added", "scale_in", "in"), STAGE_FILLED),
    (("watch", "scan", "alert", "scanner"), STAGE_WATCHING),
)


def stage_for_pin_kind(kind: str) -> str:
    """Map a Ross pin ``kind`` onto the divergence-stage vocabulary."""
    low = str(kind or "").strip().lower()
    if not low:
        return STAGE_UNMAPPED
    tokens = set(re.split(r"[^a-z0-9]+", low)) | {low}
    for markers, stage in _PIN_KIND_STAGE:
        if any(m in tokens for m in markers) or any(m in low for m in markers):
            return stage
    return STAGE_UNMAPPED


# TWO confidence vocabularies reach this column, and they answer DIFFERENT questions. They
# are named separately and never merged into one word, because "the ledger is sure Ross
# bought" and "the tape confirms the second he bought" are independent facts and collapsing
# them would let an unpinned trade render as a measured one.
#
#   LEDGER  — how sure the transcript/frame audit is about WHAT Ross did. Measured on all 187
#             rows of project_ws/AgentOps/ross/ross_master_ledger.json (schema
#             chili.ross_master_ledger.v1) on 2026-09-04: inferred 90, approx 59, exact 8,
#             absent 30. Reaches a pin row as ``ledger_confidence``.
#   TAPE PIN— whether the TAPE confirmed WHEN. The closed enum
#             rossbench_pin_ross_events.PIN_CONFIDENCES (:285), reaching a pin row as
#             ``pin_confidence`` / ``<leg>_pin_confidence``.
#
# A pin carrying anything outside both lists is kept VERBATIM and flagged — silently coercing
# an unknown confidence word into a known one is how an inferred number becomes a measured one.
LEDGER_CONFIDENCE_VOCABULARY: tuple[str, ...] = ("exact", "approx", "inferred")
TAPE_PIN_CONFIDENCE_VOCABULARY: tuple[str, ...] = (
    "tape_confirmed", "tape_ambiguous", "unpinned",
)
PIN_CONFIDENCE_VOCABULARY: tuple[str, ...] = (
    TAPE_PIN_CONFIDENCE_VOCABULARY + LEDGER_CONFIDENCE_VOCABULARY
)


def confidence_vocabulary_of(word: Any) -> Optional[str]:
    """Which vocabulary a confidence word belongs to — ``tape_pin`` | ``ledger`` | None.

    Named rather than inferred: the rendered cell says which question the word answers, so
    a reader never reads "unpinned" (the tape did not confirm the second) as "the ledger is
    unsure Ross traded"."""
    low = str(word or "").strip().lower()
    if not low:
        return None
    if low in TAPE_PIN_CONFIDENCE_VOCABULARY:
        return "tape_pin"
    if low in LEDGER_CONFIDENCE_VOCABULARY:
        return "ledger"
    return None


# ─────────────────────────────────────────────────────────────────────────────────────────
# 2. TIME
# ─────────────────────────────────────────────────────────────────────────────────────────

def _et_zone():
    if ZoneInfo is None:  # pragma: no cover - defensive
        raise RuntimeError(
            "[rossbench_timeline] zoneinfo is unavailable; an ET column cannot be rendered "
            "honestly without a tz database (the corpus spans a DST boundary)."
        )
    try:
        return ZoneInfo(ET_ZONE_NAME)
    except Exception as exc:  # tzdata missing on a bare Windows interpreter
        raise RuntimeError(
            f"[rossbench_timeline] cannot load {ET_ZONE_NAME!r}: {exc}. Install `tzdata` in "
            "the env — a fixed UTC offset would be wrong on one side of every DST boundary "
            "in the ledger's 2026-03..2026-08 span."
        ) from exc


def parse_utc(value: Any) -> Optional[datetime]:
    """Anything the receipts hold -> NAIVE UTC. Mirrors replay_harness_invariants._as_dt
    (:379-390) so the two modules place the same event on the same instant.

    Naive input is treated as UTC because that is what the driver writes: WIN_START/WIN_END
    are parsed from UTC-naive env strings (replay_v3_fsm_window.py:154-155) and the fixture
    exporter normalises event ts through ``_naive_utc`` (export_replay_v3_parity_fixtures.py
    :68-71)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def to_et(dt_naive_utc: datetime) -> datetime:
    """Naive-UTC -> tz-aware ET (the column Ross's tape is quoted in)."""
    return dt_naive_utc.replace(tzinfo=timezone.utc).astimezone(_et_zone())


def _floor_second(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


# ─────────────────────────────────────────────────────────────────────────────────────────
# 3. ROSS PINS
# ─────────────────────────────────────────────────────────────────────────────────────────
# ``0`` IS A NULL SENTINEL IN THE LEDGER, NOT A NUMBER. Measured on all 187 rows on
# 2026-09-04: entry_px 67 zeros, exit_px 103, shares 118, pnl_usd 30. A 0 pnl rendered as a
# real "$0.00" would silently claim a flat trade and corrupt both the Avoidance and the
# Capture score, so every dollar/size field goes through ``_nz``. The cost of this rule is
# that a GENUINE $0.00 outcome is indistinguishable from a missing one — that is the ledger's
# limitation, and reporting None is the honest side of it.
def _nz(value: Any) -> Optional[float]:
    """Ledger numeric -> float, with 0 read as the NULL SENTINEL it is."""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num == 0.0:
        return None
    return num


# Every field is resolved through an explicit alias list and the alias that actually fired is
# recorded on the normalised pin (``_aliases``), so a schema mismatch shows up as a named
# missing field in the meta document instead of as a mysteriously empty Ross column.
#
# THE FIRST NAMES IN EACH TUPLE ARE THE PRODUCER'S OWN, READ OUT OF ITS SOURCE, not guessed:
# scripts/rossbench_pin_ross_events.py builds each leg record in ``pin_event`` (:1276-1315)
# and folds them into the emitted window row in ``pin_window`` (:1407-1470). The later names
# in each tuple are the ledger's own column names, kept so a hand-written or ledger-shaped
# pin still renders. Ordering inside a tuple is a PRECEDENCE statement:
#
#   t_utc  — ``pin_second_utc`` (the second the TAPE confirmed) outranks ``grading_anchor_utc``
#            (which is the pin when there is one and Ross's STATED time when there is not,
#            pin_event:1304) which outranks ``stated_utc``. ``_pick`` skips a null, so an
#            unpinned leg falls through to the stated instant and ``_aliases['t_utc']`` names
#            which one it was — the difference between "measured" and "asserted", in-band.
#   kind   — ``leg`` is the producer's word for which side this is ("entry"/"exit"), and
#            ``stage_for_pin_kind`` maps those onto filled/exited.
#   price  — ONLY the leg's own ``ross_px``. ``ross_entry_px`` / ``ross_exit_px`` are
#            deliberately absent: they live on the WINDOW row and an exit leg reading
#            ``ross_entry_px`` would render Ross's entry price on his exit line.
#            ``expand_pin_rows`` resolves the per-side value into ``ross_px`` instead.
PIN_ALIASES: dict[str, tuple[str, ...]] = {
    "t_utc": ("pin_second_utc", "grading_anchor_utc", "stated_utc",
              "t_utc", "ts_utc", "utc", "entry_time_utc", "time_utc"),
    "t_et": ("pin_second_et", "stated_clock_et",
             "t_et", "ts_et", "time_et", "et", "entry_time_et", "clock_et"),
    "kind": ("leg", "kind", "event", "type", "action", "pin_kind"),
    "price": ("ross_px", "price", "px", "usd", "entry_px", "exit_px", "fill_price"),
    "pnl_usd": ("pnl_usd", "pnl", "net_usd", "ross_net_usd"),
    "shares": ("shares", "qty", "size", "quantity"),
    "pin_confidence": ("pin_confidence", "confidence", "conf", "pnl_confidence"),
    "ledger_confidence": ("ledger_confidence",),
    "text": ("stated_raw", "text", "note", "notes", "detail", "why", "ross_reason", "setup"),
}

# Two of those fields are absent from a chili.ross_event_pins.v1 document BY DESIGN, not by
# drift: the producer's own usage_constraints (rossbench_pin_ross_events.py:292-294) say a pin
# "is never a price, a fill, or a PnL", and it carries no size or dollar outcome at all.
# MEASURED over the full 157-row / 314-leg corpus document on 2026-09-04: pnl_usd and shares
# fired zero aliases, every other field fired one. Splitting them out is what keeps
# ``pin_fields_never_supplied`` an ALARM instead of a line that is always on.
PIN_FIELDS_ABSENT_BY_DESIGN: tuple[str, ...] = ("pnl_usd", "shares")

# The document ``expand_pin_rows`` is built for. Checked by name where the envelope carries
# one; a document that does not declare it is still expanded (a hand-written per-case pins
# file is legitimate) but the shape check below is what actually decides.
PINS_SCHEMA = "chili.ross_event_pins.v1"

# A chili.ross_event_pins.v1 row is ONE MANIFEST WINDOW carrying BOTH legs — one row per leg
# was measured scoring 0 of 418 cases and was deliberately collapsed (the producer's PIN
# CONTRACT block, rossbench_pin_ross_events.py:23-28). These three keys are how such a row
# announces itself; ``assert_pin_row_contract`` (:1336-1357) guarantees the first two are
# present on every emitted row, so the test is reliable rather than heuristic.
_WINDOW_ROW_MARKERS: tuple[str, ...] = ("entry_ts_utc_pinned", "exit_ts_utc_pinned", "legs")

# Window-level context copied onto each leg. ``manifest_id`` is the important one: it exists
# ONLY at row level (pin_window:1409) and is the key every other consumer of the pins file
# joins on, so a leg that lost it could not be traced back to its window.
_WINDOW_CARRIED_KEYS: tuple[str, ...] = (
    "manifest_id", "manifest_id_basis", "ledger_manifest_id", "symbol", "symbol_verbatim",
    "date", "video_id", "account", "side", "ledger_confidence", "row_index",
    "ledger_path", "ledger_src",
)


def is_window_pin_row(raw: Mapping[str, Any]) -> bool:
    """True for a ``chili.ross_event_pins.v1`` window row (both legs on one row)."""
    return isinstance(raw, Mapping) and any(k in raw for k in _WINDOW_ROW_MARKERS)


def _expand_one_window(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One window row -> one flat pin per LEG the row actually states.

    A window row with no stated leg at all still yields ONE record (the producer emits such
    rows on purpose — its ``no_stated_leg`` note) so the row stays visible in
    ``unplaced_pins`` rather than disappearing between the two components."""
    base = {k: raw[k] for k in _WINDOW_CARRIED_KEYS if k in raw}
    legs_doc = raw.get("legs")
    legs_map: Mapping[str, Any] = legs_doc if isinstance(legs_doc, Mapping) else {}
    stated = {str(s) for s in (raw.get("legs_stated") or []) if s}

    out: list[dict[str, Any]] = []
    for side in ("entry", "exit"):
        leg_rec = legs_map.get(side)
        pinned_ts = raw.get(f"{side}_ts_utc_pinned")
        side_conf = raw.get(f"{side}_pin_confidence")
        if not (isinstance(leg_rec, Mapping) or pinned_ts is not None
                or side_conf is not None or side in stated):
            continue
        pin = dict(base)
        if isinstance(leg_rec, Mapping):
            pin.update({k: v for k, v in leg_rec.items() if k != "legs"})
        pin["leg"] = side
        # The row-level per-side instant IS this leg's pin_second_utc by construction
        # (pin_window:1423-1424); it is preferred so a row emitted WITHOUT a ``legs`` block
        # still places its pin.
        if pinned_ts is not None:
            pin["pin_second_utc"] = pinned_ts
        for key, window_key in (("pin_method", f"{side}_pin_method"),
                                ("pin_confidence", f"{side}_pin_confidence")):
            if pin.get(key) is None and raw.get(window_key) is not None:
                pin[key] = raw[window_key]
        if pin.get("ross_px") is None:
            pin["ross_px"] = raw.get(f"ross_{side}_px")
        out.append(pin)

    if not out:
        pin = dict(base)
        pin.update({k: v for k, v in raw.items() if k not in ("legs",)})
        out.append(pin)
    return [_hoist_nested_pin_fields(p) for p in out]


def _hoist_nested_pin_fields(pin: dict[str, Any]) -> dict[str, Any]:
    """Flatten the two nested blocks ``_pick`` cannot see into (it reads top level only).

    ``stated`` is the producer's parse of the ledger's prose (StatedTime.as_dict,
    rossbench_pin_ross_events.py:428-437) and ``search_window_utc`` is the bounded slice the
    pin was searched in (:606-620). Both are kept nested as well — nothing is moved, only
    copied, so the leg record still round-trips."""
    stated = pin.get("stated")
    if isinstance(stated, Mapping):
        if stated.get("raw") is not None:
            pin.setdefault("stated_raw", stated.get("raw"))
        if stated.get("clock_et") is not None:
            pin.setdefault("stated_clock_et", stated.get("clock_et"))
    win = pin.get("search_window_utc")
    if isinstance(win, Mapping) and win.get("halfwidth_s") is not None:
        pin.setdefault("search_halfwidth_s", win.get("halfwidth_s"))
    return pin


def expand_pin_rows(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``chili.ross_event_pins.v1`` window rows -> one flat pin per Ross EVENT.

    Returns ``(legs, diagnostics)``. A row that is already flat (a bare ``pin_event`` record,
    or a hand-written per-case pin) passes through untouched, so this is safe to run over any
    pins document.

    WHY THIS EXISTS rather than just more aliases: the alias table alone can only read ONE
    instant off a row, so a window row would render Ross's ENTRY and silently drop his EXIT —
    and the exit is where the 2026-09-01 partial-exit study located the lever. Half a Ross
    column is a worse failure than none, because it looks populated."""
    legs: list[dict[str, Any]] = []
    window_rows = 0
    flat_rows = 0
    no_leg_rows = 0
    by_leg: dict[str, int] = {}
    for raw in rows or ():
        if not isinstance(raw, Mapping):
            continue
        if is_window_pin_row(raw):
            window_rows += 1
            produced = _expand_one_window(raw)
            if len(produced) == 1 and produced[0].get("leg") not in ("entry", "exit"):
                no_leg_rows += 1
            legs.extend(produced)
        else:
            flat_rows += 1
            legs.append(_hoist_nested_pin_fields(dict(raw)))
    for pin in legs:
        key = str(pin.get("leg") or pin.get("kind") or "unstated")
        by_leg[key] = by_leg.get(key, 0) + 1
    return legs, {
        "rows_in": window_rows + flat_rows,
        "window_rows_expanded": window_rows,
        "flat_rows_passed_through": flat_rows,
        "window_rows_with_no_stated_leg": no_leg_rows,
        "legs_out": len(legs),
        "by_leg": dict(sorted(by_leg.items())),
    }


def select_case_pins(
    rows: Iterable[Mapping[str, Any]],
    *,
    symbol: Optional[str] = None,
    manifest_id: Optional[str] = None,
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    """Keep only the pins that belong to THIS case. Returns ``(kept, dropped)``.

    THE PINS FILE IS CORPUS-WIDE. The producer writes one document for the whole ledger (157
    window rows / 314 legs, measured 2026-09-04), so handing it straight to ``build_timeline``
    would let a pin for a DIFFERENT ticker that happens to fall inside this case's window land
    on this case's grid and be read as Ross acting on this symbol. That is not a cosmetic
    error — it would move ``first_divergence``.

    The window itself is left to do the time filtering (a pin outside it is already surfaced
    as ``outside_window``); this only removes rows that are about something else. A row that
    carries no ``symbol`` / ``manifest_id`` at all is KEPT — a hand-written per-case pins file
    is a legitimate input and has nothing to filter on."""
    want_symbol = str(symbol or "").strip().upper() or None
    want_mid = str(manifest_id or "").strip() or None
    kept: list[Mapping[str, Any]] = []
    dropped: list[dict[str, Any]] = []

    def _drop(pin: Mapping[str, Any], reason: str) -> None:
        # A trimmed record, not the whole row: the corpus-wide file would otherwise put 300+
        # full pin rows into every case's meta document.
        dropped.append({
            "reason": reason,
            "symbol": pin.get("symbol"),
            "date": pin.get("date"),
            "leg": pin.get("leg"),
            "manifest_id": pin.get("manifest_id"),
            "pin_id": pin.get("pin_id"),
        })

    for pin in rows or ():
        if not isinstance(pin, Mapping):
            continue
        row_mid = pin.get("manifest_id") or pin.get("ledger_manifest_id")
        if want_mid and row_mid and str(row_mid) != want_mid:
            _drop(pin, "other_manifest_id")
            continue
        row_symbol = pin.get("symbol")
        if want_symbol and row_symbol and str(row_symbol).strip().upper() != want_symbol:
            _drop(pin, "other_symbol")
            continue
        kept.append(pin)
    return kept, dropped

# A narrative clock that is explicitly a BOUND rather than a stated time. Measured on the
# ledger 2026-09-04: of the 14 rows whose only clock appears mid-sentence, 12 carry one of
# these markers and the other 2 say "UNRESOLVED" — i.e. a mid-sentence clock is essentially
# never a pin. 20 of the 132 leading-clock rows also carry a marker, but there the leading
# clock IS the stated pin (e.g. "~06:00-07:15 (headline 06:00; ... not stated)"), so those
# are tagged, not suppressed.
_BOUND_MARKERS = re.compile(
    r"not stated|not recoverable|unknown|unresolved|unclear|absent|bounded", re.IGNORECASE
)
# 132/157 ledger rows put the clock first; measured 2026-09-04. The optional ``~`` is Ross's
# own approximation marker and is preserved as ``approx_marker`` rather than thrown away.
_LEADING_CLOCK = re.compile(r"^\s*(?P<approx>~)?\s*(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?")
_ANY_CLOCK = re.compile(r"(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?")


def _pick(raw: Mapping[str, Any], field_name: str) -> tuple[Any, Optional[str]]:
    for alias in PIN_ALIASES[field_name]:
        if alias in raw and raw[alias] is not None:
            return raw[alias], alias
    return None, None


@dataclass
class ClockParse:
    """Result of reading a narrative ET clock. Every rejection carries its reason so the
    meta document can say WHY a pin has no line, instead of the pin just vanishing."""
    et_time: Optional[tuple[int, int, int]] = None
    approx_marker: bool = False
    clock_position: Optional[str] = None      # "leading" | None
    narrative_bound: bool = False
    bound_hint: Optional[str] = None
    reason: Optional[str] = None


def parse_narrative_clock(text: Any) -> ClockParse:
    """``entry_time_et`` is a NARRATIVE field, not a timestamp — read it as such.

    LEADING clock  -> a pin (tagged ``narrative_bound`` when the same string also says the
                      time is bounded/not stated).
    MID-SENTENCE   -> NOT a pin: measured 12/14 such rows explicitly say "not stated" /
                      "unknown" / "bounded"; the clock there is the bound, not the fill. The
                      number is still surfaced as ``bound_hint`` so nothing is hidden.
    """
    if not isinstance(text, str) or not text.strip():
        return ClockParse(reason="no_clock_text")
    bound = bool(_BOUND_MARKERS.search(text))
    lead = _LEADING_CLOCK.match(text)
    if lead:
        hour = int(lead.group("h"))
        minute = int(lead.group("m"))
        second = int(lead.group("s") or 0)
        if hour > 23 or minute > 59 or second > 59:
            return ClockParse(reason=f"clock_out_of_range:{lead.group(0).strip()!r}")
        return ClockParse(
            et_time=(hour, minute, second),
            approx_marker=bool(lead.group("approx")),
            clock_position="leading",
            narrative_bound=bound,
        )
    mid = _ANY_CLOCK.search(text)
    if mid:
        return ClockParse(
            narrative_bound=True,
            bound_hint=mid.group(0),
            reason="mid_sentence_clock_is_a_bound_not_a_pin",
        )
    return ClockParse(reason="no_clock_in_text")


def resolve_pin_instant(
    parsed: ClockParse,
    *,
    et_date,
    win_start_utc: datetime,
    win_end_utc: datetime,
) -> tuple[Optional[datetime], bool]:
    """(naive-UTC instant, meridiem_inferred) for a parsed narrative clock.

    A bare ``9:41`` carries no meridiem. Rather than invent a cutoff hour, the reading is
    resolved AGAINST THE DECLARED WINDOW — a named input, not a constant: take the literal
    reading; if it falls outside the window but the +12h reading falls inside, use that and
    say so. If neither lands inside, the literal reading is kept and the caller records the
    pin as out-of-window. This cannot silently move a pin that already fits."""
    if parsed.et_time is None:
        return None, False
    zone = _et_zone()
    hour, minute, second = parsed.et_time
    literal = datetime(et_date.year, et_date.month, et_date.day, hour % 24, minute, second,
                       tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    if win_start_utc <= literal < win_end_utc:
        return literal, False
    if hour <= 12:
        shifted_h = (hour % 12) + 12
        shifted = datetime(et_date.year, et_date.month, et_date.day, shifted_h, minute,
                           second, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
        if win_start_utc <= shifted < win_end_utc:
            return shifted, True
    return literal, False


def normalize_pin(
    raw: Mapping[str, Any],
    *,
    et_date,
    win_start_utc: datetime,
    win_end_utc: datetime,
) -> dict[str, Any]:
    """One producer-shaped pin -> the normalised shape this module renders.

    Never raises on a malformed pin: an unusable pin becomes a pin with ``t_utc=None`` and a
    populated ``placement_reason``, which the meta document lists. Dropping it would make the
    Ross column silently short."""
    raw = dict(raw or {})
    aliases: dict[str, str] = {}

    def take(name: str) -> Any:
        value, alias = _pick(raw, name)
        if alias is not None:
            aliases[name] = alias
        return value

    kind_raw = take("kind")
    price = _nz(take("price"))
    pnl = _nz(take("pnl_usd"))
    shares = _nz(take("shares"))
    conf_raw = take("pin_confidence")
    ledger_conf_raw = take("ledger_confidence")
    text = take("text")
    t_utc_raw = take("t_utc")
    t_et_raw = take("t_et")

    confidence = str(conf_raw).strip().lower() if conf_raw is not None else None
    conf_vocab = confidence_vocabulary_of(confidence)
    known_conf = conf_vocab is not None
    ledger_confidence = (
        str(ledger_conf_raw).strip().lower() if ledger_conf_raw is not None else None
    )

    instant = parse_utc(t_utc_raw)
    parsed = ClockParse()
    meridiem_inferred = False
    placement_reason: Optional[str] = None
    if instant is not None:
        source = "t_utc"
    else:
        parsed = parse_narrative_clock(t_et_raw)
        instant, meridiem_inferred = resolve_pin_instant(
            parsed, et_date=et_date, win_start_utc=win_start_utc, win_end_utc=win_end_utc,
        )
        source = "t_et" if instant is not None else None
        if instant is None:
            placement_reason = parsed.reason or "unparseable_clock"

    # WAS THIS SECOND MEASURED OR ASSERTED? ``pin_second_utc`` is a second the TAPE confirmed;
    # ``grading_anchor_utc`` is that same second only when the producer says
    # grading_anchor_source == "tape_pin" (rossbench_pin_ross_events.py:1304-1305), otherwise
    # it is Ross's STATED time carried forward. A stated placement can be off by the whole
    # search halfwidth, so the distinction is carried on the row and rendered in the cell
    # rather than left for a reader to infer from the confidence word.
    t_alias = aliases.get("t_utc")
    anchor_source = raw.get("grading_anchor_source")
    if instant is None:
        tape_pinned: Optional[bool] = None
    elif t_alias == "pin_second_utc":
        tape_pinned = True
    elif t_alias == "grading_anchor_utc":
        tape_pinned = (str(anchor_source) == "tape_pin")
    elif t_alias == "stated_utc":
        tape_pinned = False
    else:
        tape_pinned = None                       # a non-producer key: unknowable from here
    halfwidth = raw.get("search_halfwidth_s")
    try:
        halfwidth = float(halfwidth) if halfwidth is not None else None
    except (TypeError, ValueError):
        halfwidth = None

    stage = stage_for_pin_kind(kind_raw if kind_raw is not None else "")
    return {
        "kind": (str(kind_raw) if kind_raw is not None else None),
        "stage": stage,
        "t_utc": instant.isoformat() if instant is not None else None,
        "_instant": instant,                      # internal; stripped before serialisation
        "t_source": source,
        "t_field": t_alias,
        "instant_is_tape_pinned": tape_pinned,
        "search_halfwidth_s": halfwidth,
        "price_usd": price,
        "pnl_usd": pnl,
        "shares": shares,
        "pin_confidence": confidence,
        "pin_confidence_known_vocabulary": known_conf,
        "pin_confidence_vocabulary": conf_vocab,
        "ledger_confidence": ledger_confidence,
        # Provenance back to the pins document, so one timeline line can be traced to one
        # window row without re-deriving the join. Never aliased: these are the producer's
        # own key names (rossbench_pin_ross_events.py:1409 / :1439).
        "manifest_id": raw.get("manifest_id"),
        "pin_id": raw.get("pin_id"),
        "pin_method": raw.get("pin_method"),
        "approx_marker": parsed.approx_marker,
        "clock_position": parsed.clock_position,
        "narrative_bound": parsed.narrative_bound,
        "bound_hint": parsed.bound_hint,
        "meridiem_inferred": meridiem_inferred,
        "placement_reason": placement_reason,
        "text": (str(text) if text is not None else None),
        "_aliases": aliases,
    }


def render_pin(pin: Mapping[str, Any]) -> str:
    """One-line Ross cell: WHAT, for HOW MUCH, and HOW SURE. The confidence is never
    dropped — an inferred $7,000 and a measured $7,000 are different facts."""
    bits = [str(pin.get("kind") or pin.get("stage") or "?").upper()]
    price = pin.get("price_usd")
    if price is not None:
        bits.append(f"${price:,.2f}")
    shares = pin.get("shares")
    if shares is not None:
        bits.append(f"x{shares:,.0f}")
    pnl = pin.get("pnl_usd")
    if pnl is not None:
        bits.append(f"pnl ${pnl:+,.2f}")
    tags = []
    conf = pin.get("pin_confidence")
    tags.append(str(conf) if conf else "confidence?")
    ledger_conf = pin.get("ledger_confidence")
    if ledger_conf and ledger_conf != conf:
        tags.append(f"ledger:{ledger_conf}")
    # A STATED second is not a MEASURED one. Rendered as the producer's own halfwidth when it
    # supplied one, so the reader sees how far this line could actually be from the truth.
    if pin.get("instant_is_tape_pinned") is False:
        half = pin.get("search_halfwidth_s")
        tags.append(f"stated-time +/-{half:,.0f}s" if isinstance(half, (int, float))
                    else "stated-time")
    if pin.get("approx_marker"):
        tags.append("~")
    if pin.get("narrative_bound"):
        tags.append("bounded")
    if pin.get("meridiem_inferred"):
        tags.append("meridiem-inferred")
    if not pin.get("pin_confidence_known_vocabulary") and conf:
        tags.append("conf-vocab?")
    return " ".join(bits) + " (" + ", ".join(tags) + ")"


# ─────────────────────────────────────────────────────────────────────────────────────────
# 3b. IS THE ROSS COLUMN ACTUALLY POPULATED?
# ─────────────────────────────────────────────────────────────────────────────────────────
# THIS IS THE REGRESSION GUARD FOR A FAILURE THAT ALREADY HAPPENED. The first version of
# PIN_ALIASES was written against the raw ross_master_ledger instead of against the pinner's
# chili.ross_event_pins.v1, and MEASURED on the 157 real window rows of a full offline pin run
# (2026-09-04) it produced: kind=None 157/157, stage='unmapped' 157/157, t_utc 0/157,
# price_usd 0/157, known confidence vocabulary 0/157 — the entire Ross column blank, with
# nothing raising. Only ``meta.pin_fields_never_supplied`` knew, and nothing read it.
#
# So the diagnostic is now a computed VERDICT that the markdown states out loud and
# tests/test_rossbench_timeline.py asserts on against a document built by the real producer.
_PIN_TIME_FIELDS: tuple[str, ...] = ("t_utc", "t_et")


def ross_column_health(pins: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Would this timeline's Ross column contain anything? A VERDICT, not a hint.

    ``empty`` is true when pins were supplied and NONE of them can render a line — i.e. the
    bench would print a Ross column of blanks while claiming to compare against Ross. The
    reason is derived from which alias family failed, because that is the difference between
    "this case genuinely has no pins" and "the producer renamed a key"."""
    total = len(pins)
    with_kind = sum(1 for p in pins if p.get("kind"))
    with_instant = sum(1 for p in pins if p.get("t_utc"))
    on_ladder = sum(1 for p in pins if p.get("stage") != STAGE_UNMAPPED)
    renderable = sum(1 for p in pins if p.get("t_utc") and p.get("stage") != STAGE_UNMAPPED)
    tape_pinned = sum(1 for p in pins if p.get("instant_is_tape_pinned") is True)
    stated_only = sum(1 for p in pins if p.get("instant_is_tape_pinned") is False)

    fired: set[str] = set()
    for pin in pins:
        fired.update((pin.get("_aliases") or {}).keys())

    reason: Optional[str] = None
    if total == 0:
        reason = "no_pins_supplied"
    elif renderable == 0:
        if not fired & set(_PIN_TIME_FIELDS):
            reason = "no_time_alias_fired"          # PIN_ALIASES t_utc/t_et match nothing
        elif with_kind == 0:
            reason = "no_kind_alias_fired"          # every pin's stage would be 'unmapped'
        elif with_instant == 0:
            reason = "no_pin_could_be_placed"       # aliases fired, clocks unreadable
        else:
            reason = "placed_but_unmapped_kind"
    return {
        "pins": total,
        "renderable": renderable,
        "with_kind": with_kind,
        "with_instant": with_instant,
        "with_stage_on_ladder": on_ladder,
        "with_price": sum(1 for p in pins if p.get("price_usd") is not None),
        "with_known_confidence_vocabulary":
            sum(1 for p in pins if p.get("pin_confidence_known_vocabulary")),
        "instants_tape_pinned": tape_pinned,
        "instants_from_stated_time": stated_only,
        "alias_families_that_fired": sorted(fired),
        "empty": bool(total and renderable == 0),
        "reason": reason,
    }


# ─────────────────────────────────────────────────────────────────────────────────────────
# 4. CODE REFS — resolved by AST against the VERIFIED build tree
# ─────────────────────────────────────────────────────────────────────────────────────────
# A plain text search is WRONG here and this was verified, not assumed: grepping
# ``"live_arm_requested"`` in the build tree hits live_replay_audit.py:258 first, which only
# COUNTS the event. The reader must land on the branch that MADE the decision, so the search
# is AST-shaped: a string constant only counts when it is an argument to an event-emitting
# call. (Same doctrine as reference_source_guard_windows_rot: AST, not regex.)
#
# The three emitter shapes actually present in the tree, each verified by reading the site:
#   _emit(db, sess, "live_entry_filled", {...})                 live_runner.py:35802
#   append_trading_automation_event(db, sid, "...", payload)     persistence.py:807 (the def)
#   TradingAutomationEvent(..., event_type="live_exit_filled")   alpaca_reconcile.py:4116-4119
_EMITTER_FUNCS: frozenset[str] = frozenset({
    "_emit", "emit", "emit_event", "append_trading_automation_event",
    "TradingAutomationEvent",
})
_EVENT_TYPE_KEYWORDS: frozenset[str] = frozenset({"event_type", "etype", "kind"})

# Where events are emitted. ``app`` is the whole backend; the default is a PACKAGE name, not
# a tuned depth or file count, so it cannot rot into a magic number.
DEFAULT_CODE_SCAN_ROOTS: tuple[str, ...] = ("app",)


@dataclass
class CodeRef:
    file: str
    line: int
    literal_line: int
    func: str
    ambiguous: bool = False
    other_sites: tuple[str, ...] = ()
    verified: bool = True
    verification_note: Optional[str] = None

    def render(self) -> str:
        base = f"{self.file}:{self.line}"
        if self.ambiguous:
            base += f" (+{len(self.other_sites)} more)"
        if not self.verified:
            base += " [UNVERIFIED]"
        return base


def _call_func_name(node: ast.Call) -> Optional[str]:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _emit_sites_in_source(source: str, wanted: set[str]) -> dict[str, list[tuple[int, int, str]]]:
    """{event_type: [(call_line, literal_line, func_name), ...]} for one module's source."""
    out: dict[str, list[tuple[int, int, str]]] = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # A build tree containing a file this interpreter cannot parse is a real condition
        # (vendored py2, generated stubs). Skipping the file is correct; skipping SILENTLY
        # is not, so this is re-raised for the caller to log by name.
        raise
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = _call_func_name(node)
        if fname not in _EMITTER_FUNCS:
            continue
        candidates: list[ast.Constant] = [
            a for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
        ]
        candidates += [
            kw.value for kw in node.keywords
            if kw.arg in _EVENT_TYPE_KEYWORDS
            and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
        ]
        for const in candidates:
            value = const.value
            if value in wanted:
                out.setdefault(value, []).append((node.lineno, const.lineno, fname or "?"))
    return out


def resolve_code_refs(
    event_types: Iterable[str],
    build_dir: str | os.PathLike[str],
    *,
    scan_roots: Sequence[str] = DEFAULT_CODE_SCAN_ROOTS,
    verified: bool = True,
    verification_note: Optional[str] = None,
) -> dict[str, Optional[CodeRef]]:
    """event_type -> the ``_emit`` site that produced it, as ``file:line``.

    ``verified`` is threaded through from the caller's tree check rather than assumed: a
    line number resolved against a tree that is not the tree that RAN points at the wrong
    branch, which is exactly the stale-build-tree incident ``verify_tree`` exists for
    (replay_harness_invariants.py:334-363)."""
    wanted = {str(e) for e in event_types if str(e or "").strip()}
    found: dict[str, list[tuple[str, int, int, str]]] = {}
    if not wanted:
        return {}
    root = Path(build_dir)
    for scan_root in scan_roots:
        base = root / scan_root
        if not base.exists():
            logger.warning("[rossbench_timeline] code scan root missing: %s", base)
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Cheap pre-filter: parsing 871 modules to find 20 literals is pointless work.
            if not any(w in source for w in wanted):
                continue
            try:
                sites = _emit_sites_in_source(source, wanted)
            except SyntaxError:
                logger.warning("[rossbench_timeline] unparseable module skipped: %s", path)
                continue
            rel = path.relative_to(root).as_posix()
            for event_type, hits in sites.items():
                for (call_line, literal_line, fname) in hits:
                    found.setdefault(event_type, []).append((rel, call_line, literal_line, fname))

    refs: dict[str, Optional[CodeRef]] = {}
    for event_type in sorted(wanted):
        hits = sorted(found.get(event_type, []))
        if not hits:
            refs[event_type] = None
            continue
        rel, call_line, literal_line, fname = hits[0]
        others = tuple(f"{r}:{c}" for (r, c, _l, _f) in hits[1:])
        refs[event_type] = CodeRef(
            file=rel, line=call_line, literal_line=literal_line, func=fname,
            ambiguous=bool(others), other_sites=others,
            verified=verified, verification_note=verification_note,
        )
    return refs


def git_head(build_dir: str | os.PathLike[str]) -> Optional[str]:
    """HEAD of the build tree. Read-only; same call the driver's own receipt makes
    (replay_v3_fsm_window.py:748-767)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(build_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


@dataclass
class TreeVerification:
    ok: bool
    build_head: Optional[str]
    run_head: Optional[str]
    run_dirty: Optional[bool]
    note: Optional[str]


def verify_build_tree(
    build_dir: str | os.PathLike[str],
    run_doc: Mapping[str, Any],
    *,
    head_reader=git_head,
) -> TreeVerification:
    """The tree we resolve line numbers in must be the tree that RAN.

    The receipt records what ran (``tree.head`` / ``tree.dirty``, written by
    ``_tree_sha`` at replay_v3_fsm_window.py:748). A mismatch means every ``file:line`` in
    the timeline points into a different revision, so it is reported as NOT ok and the caller
    decides (refuse, or emit with every ref tagged ``[UNVERIFIED]``). ``dirty`` is reported
    too: a dirty tree can carry the right HEAD and still have moved every line."""
    tree = dict((run_doc or {}).get("tree") or {})
    run_head = tree.get("head")
    run_dirty = tree.get("dirty")
    build_head = head_reader(build_dir)
    if not run_head:
        return TreeVerification(False, build_head, None, run_dirty,
                                "run.json carries no tree.head — the running revision is unknown")
    if not build_head:
        return TreeVerification(False, None, str(run_head), run_dirty,
                                f"cannot read HEAD of build tree {str(build_dir)!r}")
    if build_head != str(run_head):
        return TreeVerification(False, build_head, str(run_head), run_dirty,
                                f"build tree is at {build_head} but the run was made at {run_head}")
    if run_dirty:
        return TreeVerification(False, build_head, str(run_head), True,
                                "HEAD matches but the run recorded a DIRTY tree — line numbers "
                                "may not be the lines that ran")
    return TreeVerification(True, build_head, str(run_head), run_dirty, None)


# ─────────────────────────────────────────────────────────────────────────────────────────
# 5. TAPE
# ─────────────────────────────────────────────────────────────────────────────────────────
# Column aliases for the tape file. The names on the left are the driver's own column names
# (``observed_at``/``price``/``size``/``bid``/``ask`` — replay_v3_fsm_window.py:242-270); the
# rest are the shapes a hand-exported tape tends to arrive in.
_TAPE_TS_KEYS = ("observed_at", "ts", "t", "time", "timestamp")
_TAPE_PRICE_KEYS = ("price", "last", "last_px", "px", "trade_price")
_TAPE_SIZE_KEYS = ("size", "last_size", "qty", "quantity", "volume")
_TAPE_BID_KEYS = ("bid", "bid_px", "best_bid")
_TAPE_ASK_KEYS = ("ask", "ask_px", "best_ask")


@dataclass
class TapeTick:
    ts: datetime                      # naive UTC
    price: Optional[float] = None
    size: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None


def _first(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _f(value: Any) -> Optional[float]:
    """Tape float. NOTE: unlike ``_nz`` this keeps 0 — a 0-size print or a 0 bid is a real
    tape condition, and the ledger's NULL-sentinel rule is a LEDGER rule, not a tape rule."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def tape_row_to_tick(row: Mapping[str, Any]) -> Optional[TapeTick]:
    ts = parse_utc(_first(row, _TAPE_TS_KEYS))
    if ts is None:
        return None
    return TapeTick(
        ts=ts,
        price=_f(_first(row, _TAPE_PRICE_KEYS)),
        size=_f(_first(row, _TAPE_SIZE_KEYS)),
        bid=_f(_first(row, _TAPE_BID_KEYS)),
        ask=_f(_first(row, _TAPE_ASK_KEYS)),
    )


def load_tape_jsonl(path: str | os.PathLike[str]) -> list[TapeTick]:
    """One JSON object per line. Rows without a readable timestamp are counted by the caller
    via the returned length delta, never silently discarded mid-stream without a log."""
    ticks: list[TapeTick] = []
    skipped = 0
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                logger.warning("[rossbench_timeline] tape line %d is not JSON", lineno)
                continue
            tick = tape_row_to_tick(row) if isinstance(row, dict) else None
            if tick is None:
                skipped += 1
                continue
            ticks.append(tick)
    if skipped:
        logger.warning("[rossbench_timeline] tape: %d unusable row(s) in %s", skipped, path)
    ticks.sort(key=lambda t: t.ts)
    return ticks


def load_tape_csv(path: str | os.PathLike[str]) -> list[TapeTick]:
    ticks: list[TapeTick] = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            tick = tape_row_to_tick(row)
            if tick is not None:
                ticks.append(tick)
    ticks.sort(key=lambda t: t.ts)
    return ticks


# The bench must never read the live trading database. This guard is the same doctrine as
# tests/conftest.py's ``_test``-suffix hard-fail (CLAUDE.md Hard Rule 4): the DB NAME has to
# announce that it is a sink, or the read does not happen. There is no override flag on
# purpose.
_NON_PRODUCTION_DB_MARKERS: tuple[str, ...] = ("_test", "_sim", "sim_", "replay", "staging")


def assert_non_production_dsn(url: str) -> str:
    """Return the database name, or raise if it does not announce itself as a sink."""
    name = str(url or "").rsplit("/", 1)[-1].split("?", 1)[0].strip()
    if not name:
        raise AssertionError(
            "[rossbench_timeline] --tape-db-url carries no database name; refusing to read."
        )
    low = name.lower()
    if not any(m in low for m in _NON_PRODUCTION_DB_MARKERS):
        raise AssertionError(
            f"[rossbench_timeline] refusing to read tape from database {name!r}: the bench "
            "reads replay sinks only. A DB name must contain one of "
            f"{list(_NON_PRODUCTION_DB_MARKERS)} (same doctrine as the _test-suffix guard in "
            "tests/conftest.py — a bench that can point at the live lane eventually does)."
        )
    return name


# Tie-stable tail, byte-identical to the driver's tape SELECTs and to
# replay_harness_invariants.TIE_STABLE_TAIL: equal timestamps must not fall back to physical
# scan order or two runs of this tool over the same sink can disagree.
_TAPE_DB_TRADE_SQL = (
    "SELECT observed_at, price, size, bid, ask FROM iqfeed_trade_ticks "
    "WHERE symbol=:s AND observed_at>=:a AND observed_at<:b AND price>0 "
    "ORDER BY observed_at ASC, id ASC"
)
_TAPE_DB_NBBO_SQL = (
    "SELECT observed_at, bid, ask FROM momentum_nbbo_spread_tape "
    "WHERE symbol=:s AND observed_at>=:a AND observed_at<:b AND bid>0 AND ask>=bid "
    "ORDER BY observed_at ASC, id ASC"
)


def load_tape_from_sink(url: str, symbol: str, win_start: datetime, win_end: datetime) -> list[TapeTick]:
    """Read the trade tape + NBBO from a REPLAY SINK. Guarded by ``assert_non_production_dsn``.

    sqlalchemy is imported lazily so that importing this module (and its tests) stays
    stdlib-only and cannot touch a database by accident."""
    assert_non_production_dsn(url)
    from sqlalchemy import create_engine, text  # lazy: keeps import-time stdlib-only

    engine = create_engine(url)
    ticks: list[TapeTick] = []
    params = {"s": symbol, "a": win_start, "b": win_end}
    with engine.connect() as conn:
        for row in conn.execute(text(_TAPE_DB_TRADE_SQL), params):
            m = row._mapping
            ticks.append(TapeTick(ts=parse_utc(m["observed_at"]), price=_f(m["price"]),
                                  size=_f(m["size"]), bid=_f(m["bid"]), ask=_f(m["ask"])))
        for row in conn.execute(text(_TAPE_DB_NBBO_SQL), params):
            m = row._mapping
            ticks.append(TapeTick(ts=parse_utc(m["observed_at"]), bid=_f(m["bid"]),
                                  ask=_f(m["ask"])))
    ticks = [t for t in ticks if t.ts is not None]
    ticks.sort(key=lambda t: t.ts)
    return ticks


# ─────────────────────────────────────────────────────────────────────────────────────────
# 6. THE TIMELINE
# ─────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class TimelineRow:
    t_utc: datetime
    t_et: str
    last: Optional[float] = None
    last_age_s: Optional[int] = None
    last_is_carried: bool = False
    bid: Optional[float] = None
    ask: Optional[float] = None
    quote_age_s: Optional[int] = None
    quote_is_carried: bool = False
    second_volume: float = 0.0
    cum_vol: float = 0.0
    prints: int = 0
    ross: list[dict[str, Any]] = field(default_factory=list)
    chili: list[dict[str, Any]] = field(default_factory=list)
    ross_stage: str = STAGE_ABSENT
    chili_stage: str = STAGE_ABSENT
    ross_rank: int = 0
    chili_rank: int = 0
    first_divergence: bool = False
    first_money_divergence: bool = False


@dataclass
class Timeline:
    case_id: str
    symbol: str
    win_start: datetime
    win_end: datetime
    rows: list[TimelineRow]
    meta: dict[str, Any]


def _event_dollars(payload: Mapping[str, Any]) -> Optional[float]:
    """The $ a CHILI event carries. Only keys the receipt actually preserves are consulted:
    ``fill_price`` / ``avg`` survive ``_load_bearing_payload``
    (export_replay_v3_parity_fixtures.py:79-80); an invented key would render blank forever."""
    for key in ("fill_price", "avg"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def render_chili_event(ev: Mapping[str, Any]) -> str:
    payload = dict(ev.get("payload") or {})
    bits = [f"{ev.get('event_type')} [{ev.get('stage')}]"]
    price = _event_dollars(payload)
    if price is not None:
        bits.append(f"${price:,.4f}")
    for key, fmt in (("filled_size", "x{:,.0f}"), ("pnl_usd", "pnl ${:+,.2f}"),
                     ("peak_r", "peakR {:.2f}")):
        value = payload.get(key)
        if value is None:
            continue
        try:
            bits.append(fmt.format(float(value)))
        except (TypeError, ValueError):
            pass
    for key in ("reason", "blocked_trigger", "trigger", "benched_at_hod"):
        value = payload.get(key)
        if value not in (None, ""):
            bits.append(f"{key}={value}")
            break
    return " ".join(bits)


def build_timeline(
    *,
    case_id: str,
    symbol: str,
    win_start: datetime,
    win_end: datetime,
    ticks: Sequence[TapeTick],
    pins: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    code_refs: Mapping[str, Optional[CodeRef]],
    extra_meta: Optional[Mapping[str, Any]] = None,
) -> Timeline:
    """The dense per-second grid. ``win_start``/``win_end`` are naive UTC, half-open.

    DENSE ON PURPOSE: every second in the window gets a row even when nothing happened. A
    grid that only has eventful seconds cannot show the reader how long CHILI sat in a stage
    while Ross was already in the trade — which is the measurement this bench was built to
    make (the 2026-09-03 master ledger attributed 71% of the gap to uptime, not to gates)."""
    if win_end <= win_start:
        raise ValueError(
            f"[rossbench_timeline] empty window: {win_start.isoformat()} .. {win_end.isoformat()}"
        )
    win_start = _floor_second(win_start)
    win_end = _floor_second(win_end)
    total_seconds = int((win_end - win_start).total_seconds())

    rows: list[TimelineRow] = []
    for i in range(total_seconds):
        t = win_start + timedelta(seconds=i)
        rows.append(TimelineRow(t_utc=t, t_et=to_et(t).strftime("%H:%M:%S")))
    index = {r.t_utc: idx for idx, r in enumerate(rows)}

    def slot(instant: Optional[datetime]) -> Optional[int]:
        if instant is None:
            return None
        return index.get(_floor_second(instant))

    # --- tape --------------------------------------------------------------------------
    tape_out_of_window = 0
    for tick in ticks:
        idx = slot(tick.ts)
        if idx is None:
            tape_out_of_window += 1
            continue
        row = rows[idx]
        if tick.price is not None:
            row.last = tick.price          # LAST print in the second wins (ticks are sorted)
            row.prints += 1
        if tick.size is not None:
            row.second_volume += float(tick.size)
        if tick.bid is not None or tick.ask is not None:
            if tick.bid is not None:
                row.bid = tick.bid
            if tick.ask is not None:
                row.ask = tick.ask

    # Carry-forward, EXPLICITLY AGED. A blank cell reads as "no data"; a silently carried
    # price reads as a live one. Neither is honest on its own, so the row carries the value
    # AND how old it is. (BBO staleness has been a live-lane defect twice in this repo — see
    # project_retirement_and_bbo_staleness_0901 — so the age is data, not decoration.)
    last_price: Optional[float] = None
    last_price_age = 0
    quote: tuple[Optional[float], Optional[float]] = (None, None)
    quote_age = 0
    running_volume = 0.0
    for row in rows:
        if row.last is not None:
            last_price, last_price_age = row.last, 0
            row.last_is_carried = False
        else:
            last_price_age += 1
            row.last = last_price
            row.last_is_carried = last_price is not None
        row.last_age_s = last_price_age if last_price is not None else None

        if row.bid is not None or row.ask is not None:
            quote = (row.bid if row.bid is not None else quote[0],
                     row.ask if row.ask is not None else quote[1])
            quote_age = 0
            row.quote_is_carried = False
        else:
            quote_age += 1
            row.bid, row.ask = quote
            row.quote_is_carried = any(q is not None for q in quote)
        row.quote_age_s = quote_age if any(q is not None for q in quote) else None

        running_volume += row.second_volume
        row.cum_vol = running_volume

    # --- Ross pins ----------------------------------------------------------------------
    # WHICH producer key SUPPLIED each field (i.e. was present and non-null), across every
    # pin. This is the schema mismatch detector: if the pin producer renames
    # ``pin_confidence`` tomorrow, this map goes empty for that field and the meta document
    # says so, instead of the Ross column quietly losing its confidence tags.
    # NOTE it records what was FOUND, not what was USED — a pin whose instant came from
    # ``pin_second_utc`` still records its ``t_et`` alias if one was present, because an
    # unused-but-present key is the earliest warning that the two sides are drifting. The
    # key the instant actually came from is on the pin itself, as ``t_field``.
    alias_usage: dict[str, dict[str, int]] = {}
    for pin in pins:
        for field_name, alias in dict((pin or {}).get("_aliases") or {}).items():
            alias_usage.setdefault(field_name, {})
            alias_usage[field_name][alias] = alias_usage[field_name].get(alias, 0) + 1

    unplaced_pins: list[dict[str, Any]] = []
    for pin in pins:
        pin = dict(pin)
        instant = pin.pop("_instant", None) or parse_utc(pin.get("t_utc"))
        idx = slot(instant)
        if idx is None:
            pin["placement_reason"] = pin.get("placement_reason") or (
                "outside_window" if instant is not None else "no_instant"
            )
            unplaced_pins.append(pin)
            continue
        rows[idx].ross.append(pin)

    # --- CHILI events ---------------------------------------------------------------------
    out_of_window_events: list[dict[str, Any]] = []
    unmapped_types: dict[str, int] = {}
    for ev in events:
        stage = stage_for_event(ev.get("event_type"))
        if stage == STAGE_UNMAPPED:
            key = str(ev.get("event_type"))
            unmapped_types[key] = unmapped_types.get(key, 0) + 1
        ref = code_refs.get(str(ev.get("event_type")))
        record = {
            "ts": ev.get("ts"),
            "event_type": str(ev.get("event_type")),
            "stage": stage,
            "payload": dict(ev.get("payload") or {}),
            "code_ref": ref.render() if ref is not None else None,
            "code_ref_file": ref.file if ref is not None else None,
            "code_ref_line": ref.line if ref is not None else None,
        }
        instant = parse_utc(ev.get("ts"))
        idx = slot(instant)
        if idx is None:
            record["placement_reason"] = (
                "outside_window" if instant is not None else "unparseable_ts"
            )
            out_of_window_events.append(record)
            continue
        rows[idx].chili.append(record)

    # --- stages + divergence --------------------------------------------------------------
    ross_rank = 0
    ross_stage = STAGE_ABSENT
    chili_rank = 0
    chili_stage = STAGE_ABSENT
    for row in rows:
        for pin in row.ross:
            stage = str(pin.get("stage") or STAGE_UNMAPPED)
            rank = STAGE_RANK.get(stage)
            if rank is not None and rank > ross_rank:
                ross_rank, ross_stage = rank, stage
            elif rank is None:
                ross_stage = stage if ross_rank == 0 else ross_stage
        for record in row.chili:
            stage = str(record.get("stage"))
            rank = STAGE_RANK.get(stage)
            if rank is not None and rank > chili_rank:
                chili_rank, chili_stage = rank, stage
            elif rank is None:
                chili_stage = stage if chili_rank == 0 else chili_stage
        row.ross_rank, row.ross_stage = ross_rank, ross_stage
        row.chili_rank, row.chili_stage = chili_rank, chili_stage

    first_div = None
    first_money = None
    money_rank = STAGE_RANK[STAGE_FILLED]   # "the money" starts when someone is filled
    # ANCHOR (2026-09-04): before Ross's first pin he is ``absent`` on every row, so a
    # CHILI that is merely ``watching`` from the first second "diverges" at row 0 — the
    # first real SDOT timeline reported ``first_divergence 09:05:01 absent/watching``,
    # which says nothing. The comparison starts at the first second that carries a Ross
    # pin; with no pin placed there is no first_divergence, and the meta says why.
    # With NO Ross pin in the window the anchor is CHILI's first fill instead: "CHILI
    # traded and Ross did not" is a real divergence (an Avoidance case reads exactly like
    # this), while "CHILI watched and Ross was absent" is not.
    first_ross_idx = next((i for i, r in enumerate(rows) if r.ross), None)
    for i, row in enumerate(rows):
        if first_div is None and row.ross_rank != row.chili_rank:
            if first_ross_idx is not None:
                anchored = i >= first_ross_idx
            else:
                anchored = row.chili_rank >= money_rank
            if anchored:
                first_div = row
                row.first_divergence = True
        if (first_money is None and row.ross_rank != row.chili_rank
                and max(row.ross_rank, row.chili_rank) >= money_rank):
            first_money = row
            row.first_money_divergence = True
        if first_div is not None and first_money is not None:
            break

    def _div_doc(row: Optional[TimelineRow]) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        return {
            "t_utc": row.t_utc.isoformat(),
            "t_et": row.t_et,
            "ross_stage": row.ross_stage,
            "chili_stage": row.chili_stage,
            "direction": ("ross_ahead" if row.ross_rank > row.chili_rank else "chili_ahead"),
            "ross": [render_pin(p) for p in row.ross],
            "chili": [render_chili_event(c) for c in row.chili],
            "code_refs": [c.get("code_ref") for c in row.chili if c.get("code_ref")],
        }

    meta: dict[str, Any] = {
        "schema": SCHEMA_META,
        "case_id": case_id,
        "symbol": symbol,
        "window": {
            "start_utc": win_start.isoformat(),
            "end_utc": win_end.isoformat(),
            "start_et": to_et(win_start).isoformat(),
            "end_et": to_et(win_end).isoformat(),
            "seconds": total_seconds,
        },
        "stage_vocabulary": {
            "advancing": list(ADVANCING_STAGES),
            "non_advancing": list(NON_ADVANCING_STAGES),
            "derived_from": "app/services/trading/momentum_neural/replay_parity.py:59-66 "
                            "(CANONICAL_ENTRY_SPINE)",
        },
        "counts": {
            "rows": len(rows),
            "tape_ticks_placed": sum(r.prints for r in rows),
            "tape_ticks_out_of_window": tape_out_of_window,
            "pins_placed": sum(len(r.ross) for r in rows),
            "pins_unplaced": len(unplaced_pins),
            "events_placed": sum(len(r.chili) for r in rows),
            "events_out_of_window": len(out_of_window_events),
            "seconds_with_any_event": sum(1 for r in rows if r.ross or r.chili),
        },
        "first_divergence": _div_doc(first_div),
        "first_divergence_anchor": {
            "rule": "first_ross_pin" if first_ross_idx is not None else "chili_first_fill",
            "t_utc": (rows[first_ross_idx].t_utc.isoformat()
                      if first_ross_idx is not None else None),
            "note": ("comparison starts at the first second carrying a Ross pin; "
                     "before it Ross is absent on every row"
                     if first_ross_idx is not None
                     else "no Ross pin placed in the window; a divergence is reported only "
                          "once CHILI reaches a fill (CHILI traded, Ross did not)"),
        },
        "first_money_divergence": _div_doc(first_money),
        "pin_alias_usage": {k: dict(sorted(v.items())) for k, v in sorted(alias_usage.items())},
        "pin_fields_never_supplied": sorted(set(PIN_ALIASES) - set(alias_usage)),
        "pin_fields_never_supplied_unexpected": sorted(
            set(PIN_ALIASES) - set(alias_usage) - set(PIN_FIELDS_ABSENT_BY_DESIGN)),
        # The verdict, not just the evidence: ``pin_fields_never_supplied`` above listed every
        # symptom of the 2026-09-04 empty-column defect and nothing acted on it.
        "ross_column_health": ross_column_health(pins),
        "unplaced_pins": unplaced_pins,
        "out_of_window_events": out_of_window_events,
        "unmapped_event_types": dict(sorted(unmapped_types.items())),
        "code_refs": {
            k: (None if v is None else {
                "file": v.file, "line": v.line, "literal_line": v.literal_line,
                "emitter": v.func, "ambiguous": v.ambiguous,
                "other_sites": list(v.other_sites), "verified": v.verified,
                "verification_note": v.verification_note,
            })
            for k, v in sorted(code_refs.items())
        },
        "code_refs_unresolved": sorted(k for k, v in code_refs.items() if v is None),
    }
    if extra_meta:
        meta.update(dict(extra_meta))
    return Timeline(case_id=case_id, symbol=symbol, win_start=win_start, win_end=win_end,
                    rows=rows, meta=meta)


# ─────────────────────────────────────────────────────────────────────────────────────────
# 7. RENDERING
# ─────────────────────────────────────────────────────────────────────────────────────────

MD_COLUMNS: tuple[str, ...] = (
    "t ET", "last", "bid/ask", "cum_vol", "Ross", "CHILI (stage)", "code_ref",
)


def _cell(text: Any) -> str:
    """Markdown table cells cannot contain a raw pipe or a newline."""
    if text is None:
        return ""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _px(value: Optional[float], *, carried: bool, age: Optional[int]) -> str:
    if value is None:
        return ""
    text = f"{value:,.4f}".rstrip("0").rstrip(".")
    if carried:
        text += f" (stale {age}s)" if age is not None else " (stale)"
    return text


def _eventful(rows: Sequence[TimelineRow]) -> list[int]:
    return [i for i, r in enumerate(rows) if r.ross or r.chili or r.first_divergence]


def render_markdown(tl: Timeline, *, context_seconds: Optional[int] = None) -> str:
    """``t ET | last | bid/ask | cum_vol | Ross | CHILI (stage) | code_ref``.

    ``context_seconds=None`` (the default) renders EVERY second — the dense grid the bench's
    unit rule asks for. Passing an integer narrows the table to that many seconds either side
    of an eventful second; the JSONL stays dense either way, and the header states which was
    used so no reader mistakes a narrowed table for the whole window."""
    rows = tl.rows
    keep: Optional[set[int]] = None
    if context_seconds is not None:
        keep = set()
        for idx in _eventful(rows):
            lo = max(0, idx - int(context_seconds))
            hi = min(len(rows), idx + int(context_seconds) + 1)
            keep.update(range(lo, hi))

    div = tl.meta.get("first_divergence")
    money = tl.meta.get("first_money_divergence")
    out: list[str] = []
    out.append(f"# Ross Parity Bench — timeline — {tl.case_id} ({tl.symbol})")
    out.append("")
    out.append(f"* window: `{tl.win_start.isoformat()}Z` .. `{tl.win_end.isoformat()}Z` "
               f"({tl.meta['window']['seconds']} s) — ET `{tl.meta['window']['start_et']}` .. "
               f"`{tl.meta['window']['end_et']}`")
    out.append(f"* rows rendered: {'ALL seconds (dense)' if keep is None else f'±{context_seconds}s around each eventful second'}")
    if div:
        out.append(f"* **first_divergence** `{div['t_et']}` ET — Ross `{div['ross_stage']}` "
                   f"vs CHILI `{div['chili_stage']}` ({div['direction']}) — marked `>>` below")
    else:
        anchor = tl.meta.get("first_divergence_anchor") or {}
        if anchor.get("t_utc") is None:
            out.append("* **first_divergence**: NONE — no Ross pin was placed in the window "
                       "and CHILI never reached a fill, so there is nothing to diverge from")
        else:
            out.append("* **first_divergence**: NONE — from Ross's first pin onward the two "
                       "sides held the same stage for every second of the window")
    if money:
        out.append(f"* **first_money_divergence** `{money['t_et']}` ET — the first mismatch at "
                   f"or past `{STAGE_FILLED}` — marked `$$` below")
    cr = tl.meta.get("code_ref_verification") or {}
    if cr:
        state = "VERIFIED" if cr.get("ok") else "NOT VERIFIED"
        out.append(f"* code_ref tree: **{state}** — {cr.get('note') or 'build HEAD == run tree.head, clean'}")
    health = tl.meta.get("ross_column_health") or {}
    if health.get("empty"):
        # LOUD, because a blank Ross column and a Ross who did nothing look identical in a
        # table, and only one of them means the bench is measuring anything.
        out.append(
            f"* **THE ROSS COLUMN IS EMPTY** — {health.get('pins')} pin(s) were supplied and "
            f"NONE produced a line (`{health.get('reason')}`). Every comparison below is "
            "CHILI against nothing. Check that the pins document is "
            f"`{PINS_SCHEMA}` and that `PIN_ALIASES` still names its keys "
            "(`pin_alias_usage` in the meta says which fired)."
        )
    elif health.get("pins"):
        out.append(
            f"* Ross column: {health.get('renderable')}/{health.get('pins')} pin(s) rendered — "
            f"{health.get('instants_tape_pinned')} placed on a TAPE-CONFIRMED second, "
            f"{health.get('instants_from_stated_time')} on Ross's STATED time (those can be "
            "off by the pin search halfwidth; the cell says so)"
        )
    never = tl.meta.get("pin_fields_never_supplied") or []
    unexpected = tl.meta.get("pin_fields_never_supplied_unexpected")
    if unexpected is None:                       # meta from an older writer
        unexpected = [f for f in never if f not in PIN_FIELDS_ABSENT_BY_DESIGN]
    if unexpected:
        # Not necessarily a defect — a case with no partials supplies no shares — but it is
        # also exactly what a renamed producer key looks like, so it is stated, not hidden.
        out.append(f"* pin fields NO pin supplied: `{'`, `'.join(unexpected)}` — either absent "
                   "for this case or the producer renamed the key (see `pin_alias_usage` in "
                   "the meta)")
    if never and not unexpected:
        out.append(f"* pin fields absent by design: `{'`, `'.join(never)}` — a "
                   f"{PINS_SCHEMA} pin carries no size and no PnL "
                   "(its own usage_constraints say so); nothing is missing")
    out.append("")
    out.append("| " + " | ".join(MD_COLUMNS) + " |")
    out.append("|" + "|".join(["---"] * len(MD_COLUMNS)) + "|")

    for idx, row in enumerate(rows):
        if keep is not None and idx not in keep:
            continue
        marker = ""
        if row.first_divergence:
            marker += ">> "
        if row.first_money_divergence:
            marker += "$$ "
        quote = ""
        if row.bid is not None or row.ask is not None:
            bid = f"{row.bid:,.4f}".rstrip("0").rstrip(".") if row.bid is not None else "?"
            ask = f"{row.ask:,.4f}".rstrip("0").rstrip(".") if row.ask is not None else "?"
            quote = f"{bid}/{ask}"
            if row.quote_is_carried:
                quote += f" (stale {row.quote_age_s}s)" if row.quote_age_s is not None else " (stale)"
        ross = " ; ".join(render_pin(p) for p in row.ross)
        chili = " ; ".join(render_chili_event(c) for c in row.chili)
        refs = " ; ".join(sorted({c["code_ref"] for c in row.chili if c.get("code_ref")}))
        out.append("| " + " | ".join(_cell(x) for x in (
            f"{marker}{row.t_et}",
            _px(row.last, carried=row.last_is_carried, age=row.last_age_s),
            quote,
            f"{row.cum_vol:,.0f}",
            ross,
            chili,
            refs,
        )) + " |")

    out.append("")
    out.append("## Nothing placed on the grid is dropped")
    out.append("")
    unplaced = tl.meta.get("unplaced_pins") or []
    out.append(f"### Ross pins with no line ({len(unplaced)})")
    if not unplaced:
        out.append("")
        out.append("_none — every pin landed on a second._")
    else:
        out.append("")
        out.append("| kind | reason | $ | confidence | text |")
        out.append("|---|---|---|---|---|")
        for pin in unplaced:
            price = pin.get("price_usd")
            pnl = pin.get("pnl_usd")
            money_txt = " ".join(x for x in (
                (f"${price:,.2f}" if price is not None else ""),
                (f"pnl ${pnl:+,.2f}" if pnl is not None else ""),
            ) if x)
            out.append("| " + " | ".join(_cell(x) for x in (
                pin.get("kind"), pin.get("placement_reason"), money_txt,
                pin.get("pin_confidence"), (pin.get("text") or "")[:160],
            )) + " |")
    out.append("")
    oow = tl.meta.get("out_of_window_events") or []
    out.append(f"### CHILI events outside the window ({len(oow)})")
    if not oow:
        out.append("")
        out.append("_none._")
    else:
        out.append("")
        for record in oow:
            out.append(f"* `{record.get('ts')}` `{record.get('event_type')}` "
                       f"[{record.get('stage')}] — {record.get('placement_reason')}")
    unmapped = tl.meta.get("unmapped_event_types") or {}
    out.append("")
    out.append(f"### Event types not in the stage vocabulary ({len(unmapped)})")
    if not unmapped:
        out.append("")
        out.append("_none — every emitted event mapped._")
    else:
        out.append("")
        out.append("These were rendered with stage `unmapped`; they neither advanced nor "
                   "blocked a side's rank. Extend `EVENT_TYPE_STAGE` if one of them is "
                   "load-bearing for a case.")
        out.append("")
        for event_type, count in unmapped.items():
            out.append(f"* `{event_type}` x{count}")
    unresolved = tl.meta.get("code_refs_unresolved") or []
    out.append("")
    out.append(f"### Event types with no resolvable `_emit` site ({len(unresolved)})")
    if not unresolved:
        out.append("")
        out.append("_none._")
    else:
        out.append("")
        out.append("No emitter call in the scanned tree passes these as a literal — they are "
                   "emitted from a computed/f-string event type, or from outside the scan "
                   "roots.")
        out.append("")
        for event_type in unresolved:
            out.append(f"* `{event_type}`")
    out.append("")
    return "\n".join(out)


def row_to_json(row: TimelineRow) -> dict[str, Any]:
    return {
        "schema": SCHEMA_ROW,
        "t_utc": row.t_utc.isoformat(),
        "t_et": row.t_et,
        "last": row.last,
        "last_age_s": row.last_age_s,
        "last_is_carried": row.last_is_carried,
        "bid": row.bid,
        "ask": row.ask,
        "quote_age_s": row.quote_age_s,
        "quote_is_carried": row.quote_is_carried,
        "second_volume": row.second_volume,
        "cum_vol": row.cum_vol,
        "prints": row.prints,
        "ross": [{k: v for k, v in p.items() if not k.startswith("_")} for p in row.ross],
        "chili": row.chili,
        "ross_stage": row.ross_stage,
        "chili_stage": row.chili_stage,
        "ross_rank": row.ross_rank,
        "chili_rank": row.chili_rank,
        "first_divergence": row.first_divergence,
        "first_money_divergence": row.first_money_divergence,
    }


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" — Windows text mode rewrites every \n to \r\n and changes the bytes of an
    # otherwise identical artifact (reference_python_write_text_crlf_windows; same guard the
    # driver uses at replay_v3_fsm_window.py:826).
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.write(text)


def write_timeline(tl: Timeline, out_dir: str | os.PathLike[str],
                   *, context_seconds: Optional[int] = None) -> dict[str, str]:
    out = Path(out_dir)
    md_path = out / "timeline.md"
    jsonl_path = out / "timeline.jsonl"
    meta_path = out / "timeline.meta.json"
    _write_text(md_path, render_markdown(tl, context_seconds=context_seconds))
    _write_text(jsonl_path,
                "".join(json.dumps(row_to_json(r), default=str) + "\n" for r in tl.rows))
    _write_text(meta_path, json.dumps(tl.meta, indent=2, default=str) + "\n")
    return {"md": str(md_path), "jsonl": str(jsonl_path), "meta": str(meta_path)}


# ─────────────────────────────────────────────────────────────────────────────────────────
# 8. INPUT LOADING + CLI
# ─────────────────────────────────────────────────────────────────────────────────────────

def load_run_json(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a ``chili.replay_v3_fsm_window_result.v1`` receipt, refusing anything else.

    Best-effort parsing a document with a different schema is how a scorer ends up reading
    keys that mean something else; the receipt declares its schema at
    replay_v3_fsm_window.py:1141 precisely so this check is possible."""
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        raise ValueError(f"[rossbench_timeline] {path}: not a JSON object")
    schema = doc.get("schema")
    if schema != RUN_RESULT_SCHEMA:
        raise ValueError(
            f"[rossbench_timeline] {path}: schema is {schema!r}, expected "
            f"{RUN_RESULT_SCHEMA!r} (scripts/replay_v3_fsm_window.py:199)"
        )
    return doc


def load_pins_document(path: str | os.PathLike[str]) -> list[Mapping[str, Any]]:
    """Accepts a bare list, ``{"pins": [...]}`` or ``{"ross": {"pins": [...]}}``.

    The producer's own envelope is the middle one: ``build_pins_doc``
    (scripts/rossbench_pin_ross_events.py:1498-1528) writes ``{"schema": PINS_SCHEMA, ...,
    "pins": [...]}``. The other two are kept for a hand-written per-case file. A foreign
    schema is LOGGED rather than refused — the rows are shape-checked downstream by
    ``expand_pin_rows`` — but an envelope with no pin list at all raises, because yielding
    nothing here is indistinguishable from a case in which Ross did nothing."""
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if isinstance(doc, list):
        logger.info("[rossbench_timeline] pins envelope: bare list")
        return list(doc)
    if isinstance(doc, dict):
        if isinstance(doc.get("pins"), list):
            schema = doc.get("schema")
            if schema and str(schema) != PINS_SCHEMA:
                logger.warning(
                    "[rossbench_timeline] pins schema is %r, expected %r — reading it anyway, "
                    "but check meta.ross_column_health before trusting the Ross column",
                    schema, PINS_SCHEMA,
                )
            logger.info("[rossbench_timeline] pins envelope: {'pins': [...]} schema=%s", schema)
            return list(doc["pins"])
        ross = doc.get("ross")
        if isinstance(ross, dict) and isinstance(ross.get("pins"), list):
            logger.info("[rossbench_timeline] pins envelope: {'ross': {'pins': [...]}}")
            return list(ross["pins"])
    raise ValueError(
        f"[rossbench_timeline] {path}: no pin list found (looked for a bare list, "
        "{'pins': [...]}, and {'ross': {'pins': [...]}})"
    )


def window_from_run(doc: Mapping[str, Any]) -> tuple[str, datetime, datetime]:
    """(symbol, win_start, win_end) from the receipt's env contract.

    ``_env_contract`` writes SYMBOL / WIN_START / WIN_END as ISO strings from UTC-naive
    datetimes (replay_v3_fsm_window.py:770-794 and :154-155)."""
    env = dict((doc or {}).get("env") or {})
    symbol = str(env.get("SYMBOL") or "").strip()
    start = parse_utc(env.get("WIN_START"))
    end = parse_utc(env.get("WIN_END"))
    missing = [k for k, v in (("SYMBOL", symbol), ("WIN_START", start), ("WIN_END", end)) if not v]
    if missing:
        raise ValueError(
            f"[rossbench_timeline] run.json env is missing {missing} — the window cannot be "
            "derived; pass --window-start/--window-end/--symbol explicitly."
        )
    return symbol, start, end


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rossbench_timeline",
        description="Ross Parity Bench step 10 — the per-case second-by-second timeline.",
    )
    parser.add_argument("--case-id", required=True,
                        help="Names the case in the artifacts (e.g. 2026-06-30_CELZ).")
    parser.add_argument("--run-json", required=True,
                        help=f"A {RUN_RESULT_SCHEMA} receipt from replay_v3_fsm_window.py.")
    parser.add_argument("--pins", required=True,
                        help=f"Ross pins JSON ({PINS_SCHEMA}). The producer writes ONE "
                             "corpus-wide document, so it is filtered to this case by symbol "
                             "(and by --manifest-id when given) before anything is placed.")
    parser.add_argument("--manifest-id", default=None,
                        help="Keep only the pin row for this manifest window. A symbol-day "
                             "with several Ross waves has several rows; without this they are "
                             "ALL placed on the grid, which is right for a whole-day window "
                             "and wrong for a single-wave one.")
    parser.add_argument("--no-symbol-pin-filter", action="store_true",
                        help="Do NOT drop pins whose symbol differs from the case symbol. For "
                             "a hand-built pins file that deliberately mixes tickers.")
    parser.add_argument("--tape", help="Tape file (.jsonl or .csv).")
    parser.add_argument("--tape-db-url",
                        help="Replay-SINK DSN to read the tape from. Refused unless the DB "
                             "name announces itself as a sink (see assert_non_production_dsn).")
    parser.add_argument("--out-dir", required=True,
                        help="Directory for timeline.md / timeline.jsonl / timeline.meta.json.")
    parser.add_argument("--build-dir", default=None,
                        help="Tree to resolve code_refs in. Default: the repo this script "
                             "lives in. Cross-checked against run.json tree.head.")
    parser.add_argument("--code-scan-root", action="append", default=None,
                        help=f"Package to scan for _emit sites (repeatable). "
                             f"Default: {list(DEFAULT_CODE_SCAN_ROOTS)}.")
    parser.add_argument("--symbol", default=None, help="Override run.json env.SYMBOL.")
    parser.add_argument("--window-start", default=None,
                        help="Override run.json env.WIN_START (ISO, UTC).")
    parser.add_argument("--window-end", default=None,
                        help="Override run.json env.WIN_END (ISO, UTC).")
    parser.add_argument("--allow-tree-mismatch", action="store_true",
                        help="Emit anyway when the build tree is not the tree that ran; every "
                             "code_ref is then tagged [UNVERIFIED].")
    parser.add_argument("--md-context-seconds", type=int, default=None,
                        help="Narrow the MARKDOWN table to N seconds around each eventful "
                             "second. Omitted = every second (dense). JSONL is always dense.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)

    run_doc = load_run_json(args.run_json)
    env_symbol, env_start, env_end = (None, None, None)
    try:
        env_symbol, env_start, env_end = window_from_run(run_doc)
    except ValueError:
        if not (args.symbol and args.window_start and args.window_end):
            raise
    symbol = args.symbol or env_symbol
    win_start = parse_utc(args.window_start) if args.window_start else env_start
    win_end = parse_utc(args.window_end) if args.window_end else env_end
    if win_start is None or win_end is None or not symbol:
        raise SystemExit("[rossbench_timeline] symbol/window unresolved")

    build_dir = Path(args.build_dir) if args.build_dir else Path(__file__).resolve().parents[1]
    verification = verify_build_tree(build_dir, run_doc)
    if not verification.ok and not args.allow_tree_mismatch:
        raise SystemExit(
            f"[rossbench_timeline] REFUSING to resolve code_refs: {verification.note}. "
            "Check out the tree the run was made on, or pass --allow-tree-mismatch to emit "
            "with every code_ref tagged [UNVERIFIED]."
        )
    if not verification.ok:
        logger.warning("[rossbench_timeline] code_refs are UNVERIFIED: %s", verification.note)

    events = list(run_doc.get("events") or [])
    event_types = {str(e.get("event_type")) for e in events if e.get("event_type")}
    refs = resolve_code_refs(
        event_types, build_dir,
        scan_roots=tuple(args.code_scan_root or DEFAULT_CODE_SCAN_ROOTS),
        verified=verification.ok, verification_note=verification.note,
    )

    # The ET calendar day the narrative clocks belong to — taken from the declared window,
    # never from "today", so re-running the bench months later reproduces the same pins.
    et_date = to_et(win_start).date()
    raw_pins = load_pins_document(args.pins)
    # ORDER MATTERS: expand the window rows into legs FIRST (the leg is what carries the
    # instant), then filter to this case, then normalise. Filtering first would compare a
    # window row's symbol before the leg that inherits it exists.
    leg_pins, expansion = expand_pin_rows(raw_pins)
    kept, dropped = select_case_pins(
        leg_pins,
        symbol=(None if args.no_symbol_pin_filter else symbol),
        manifest_id=args.manifest_id,
    )
    logger.info("[rossbench_timeline] pins: %d row(s) -> %d leg(s) -> %d for this case "
                "(%d dropped as another symbol/window)",
                expansion["rows_in"], expansion["legs_out"], len(kept), len(dropped))
    pins = [normalize_pin(p, et_date=et_date, win_start_utc=win_start, win_end_utc=win_end)
            for p in kept]

    if args.tape and args.tape_db_url:
        raise SystemExit("[rossbench_timeline] pass --tape OR --tape-db-url, not both")
    if args.tape:
        suffix = Path(args.tape).suffix.lower()
        ticks = load_tape_csv(args.tape) if suffix == ".csv" else load_tape_jsonl(args.tape)
    elif args.tape_db_url:
        ticks = load_tape_from_sink(args.tape_db_url, symbol, win_start, win_end)
    else:
        # An empty tape is a legitimate case (the bench also grades windows with no
        # coverage), but it must be announced: a blank price column otherwise reads as a
        # halt.
        logger.warning("[rossbench_timeline] no tape source given — price columns will be EMPTY")
        ticks = []

    timeline = build_timeline(
        case_id=args.case_id, symbol=symbol, win_start=win_start, win_end=win_end,
        ticks=ticks, pins=pins, events=events, code_refs=refs,
        extra_meta={
            "run_json": os.path.abspath(args.run_json),
            "pins_file": os.path.abspath(args.pins),
            "pins_selection": {
                "expansion": expansion,
                "symbol_filter": (None if args.no_symbol_pin_filter else symbol),
                "manifest_id_filter": args.manifest_id,
                "kept": len(kept),
                "dropped": dropped,
            },
            "tape_source": (os.path.abspath(args.tape) if args.tape
                            else ("sink:" + str(args.tape_db_url) if args.tape_db_url else None)),
            "tape_ticks_read": len(ticks),
            "code_ref_verification": {
                "ok": verification.ok, "build_head": verification.build_head,
                "run_head": verification.run_head, "run_dirty": verification.run_dirty,
                "note": verification.note, "build_dir": str(build_dir),
                "scan_roots": list(args.code_scan_root or DEFAULT_CODE_SCAN_ROOTS),
            },
            "run_summary": {
                "arm": run_doc.get("arm"),
                "final_state": run_doc.get("final_state"),
                "pnl_usd": run_doc.get("pnl_usd"),
                "entries": run_doc.get("entries"),
                "exits": run_doc.get("exits"),
                "fills": len(run_doc.get("fills") or []),
            },
        },
    )
    paths = write_timeline(timeline, args.out_dir, context_seconds=args.md_context_seconds)
    div = timeline.meta.get("first_divergence")
    logger.info("[rossbench_timeline] %s rows=%d first_divergence=%s",
                args.case_id, len(timeline.rows),
                (f"{div['t_et']} ET {div['ross_stage']}/{div['chili_stage']}" if div else "none"))
    for key in ("md", "jsonl", "meta"):
        logger.info("[rossbench_timeline] wrote %s", paths[key])
    return 0


__all__ = [
    "SCHEMA_ROW", "SCHEMA_META", "RUN_RESULT_SCHEMA", "PINS_SCHEMA", "ET_ZONE_NAME",
    "ADVANCING_STAGES", "NON_ADVANCING_STAGES", "DIVERGENCE_STAGES", "STAGE_RANK",
    "EVENT_TYPE_STAGE", "PIN_CONFIDENCE_VOCABULARY", "LEDGER_CONFIDENCE_VOCABULARY",
    "TAPE_PIN_CONFIDENCE_VOCABULARY", "confidence_vocabulary_of", "PIN_ALIASES",
    "PIN_FIELDS_ABSENT_BY_DESIGN", "MD_COLUMNS",
    "DEFAULT_CODE_SCAN_ROOTS",
    "STAGE_ABSENT", "STAGE_ARMED", "STAGE_WATCHING", "STAGE_CANDIDATE", "STAGE_SUBMITTED",
    "STAGE_FILLED", "STAGE_MANAGED", "STAGE_EXITED", "STAGE_BLOCKED", "STAGE_RETIRED",
    "STAGE_NO_TRADE", "STAGE_UNMAPPED",
    "ClockParse", "CodeRef", "TapeTick", "TimelineRow", "Timeline", "TreeVerification",
    "stage_for_event", "stage_for_pin_kind", "parse_utc", "to_et",
    "parse_narrative_clock", "resolve_pin_instant", "normalize_pin", "render_pin",
    "is_window_pin_row", "expand_pin_rows", "select_case_pins", "ross_column_health",
    "render_chili_event", "resolve_code_refs", "git_head", "verify_build_tree",
    "tape_row_to_tick", "load_tape_jsonl", "load_tape_csv", "load_tape_from_sink",
    "assert_non_production_dsn", "build_timeline", "render_markdown", "row_to_json",
    "write_timeline", "load_run_json", "load_pins_document", "window_from_run",
    "build_parser", "main",
]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
