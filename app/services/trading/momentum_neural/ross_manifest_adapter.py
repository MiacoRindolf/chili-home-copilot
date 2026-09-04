"""Adapt the Ross ground-truth manifest + tape pins into grading windows.

READ-ONLY AFTER-FACT TOOLING.  Nothing in this module may be imported by
strategy, scanner, admission, sizing or execution code, and the objects it
returns are grading labels -- never event-time inputs.  The import direction is
one-way: this module imports ``ross_replay_benchmark`` (a pure dataclass /
grading module) and stdlib, and nothing else.  ``tests/test_ross_manifest_
adapter.py`` enforces both directions with an AST scan so the rule cannot rot
silently.

Why this file exists
--------------------
Two artifacts describe the same Ross trade from different angles:

* the manifest (``chili.ross_ground_truth_manifest.v1``, built by
  ``scripts/build_ross_manifest.py``) says WHAT happened -- symbol, ET trading
  day, expected action, narrated clock text, Ross's dollars; and
* the pins file (``chili.ross_event_pins.v1``, built by
  ``scripts/rossbench_pin_ross_events.py``) says WHEN it happened on the tape --
  an exact UTC instant plus the method and confidence with which it was found.

The grader in ``ross_replay_benchmark.py`` refuses anything softer.  Its
``ValidatedPhaseWindow.valid_for`` requires tz-aware bounds, ``evidence_role ==
"after_fact_grading_only"``, a non-empty ``evidence_source``, matching
label/symbol, ``start <= decision <= end``, and ``independently_verified is
True``.  A narrated "~09:00-10:00" alone can never satisfy that, and it should
not: an approximate clock stretched to fit a good price is precisely how
hindsight leaks into a benchmark.  This module is the only place where a pin is
allowed to become a window, and it refuses on every path where the pin is not
independent evidence.

Cross-module references in this file name SYMBOLS, not line numbers.  An
additive edit elsewhere shifts every line citation at once -- this file already
carried five stale ones after ``ross_replay_benchmark.py`` grew from 730 to
1263 lines -- and a comment that points at the wrong line is worse than one
that makes the reader grep.

THE PIN CONTRACT THIS MODULE CONSUMES
-------------------------------------
One pin row per manifest window, joined on ``manifest_id``, carrying BOTH sides:
``entry_ts_utc_pinned`` / ``exit_ts_utc_pinned`` plus per-side
``entry_pin_method`` / ``entry_pin_confidence`` and the exit equivalents.  The
authoritative statement of those key names is the "PIN CONTRACT" block at the
top of ``scripts/rossbench_pin_ross_events.py``, and that module's
``assert_pin_row_contract`` fails a pin run that stops emitting them.  A
previous per-LEG layout (one row for the entry, one for the exit, keyed
``pin_id``) shared no join key with this module and was measured scoring 0 of
418 cases with ``pin_confidence`` null on every one; the same chain re-run
against the window-keyed layout scores 11 (see below).

WHO CALLS THIS
--------------
``phase_windows_from_manifest`` -> ``validated_phase_windows`` /
``expected_actions_by_label`` feed ``grade_recap_phase_window``, and
``adaptation_summary`` / ``case_as_json_row`` feed the bench report (its
``cases`` list is a row container ``scripts/ross_replay_bench.py`` already reads
by name).  ``main()`` is the same join as a dry run, so the wiring can be
checked without a bench run:

    python -m app.services.trading.momentum_neural.ross_manifest_adapter \
        --manifest <fresh manifest.json> --pins <pins.json> --json-out adapted.json

The hindsight rules encoded here
--------------------------------
1. ``start_ts = decision_ts = entry pin``.  The window never starts before the
   pinned entry, so a replay cannot earn credit for a decision Ross had not yet
   made.
2. ``end_ts = exit pin`` when the exit was pinned, otherwise the *stated* end of
   the manifest window text.  A stated end is only read when it is the leading
   token of the window text -- a parenthetical uncertainty range such as
   ``"~07:45 (07:40-07:55)"`` is NOT an end boundary, and widening the window to
   its upper bound would be exactly the "extend until the price is good" error.
3. ``independently_verified = (pin_confidence == "tape_confirmed")``.
   ``tape_ambiguous`` (>1 candidate cluster) and ``unpinned`` are unscorable, by
   design and in volume.  A large unscorable count is the expected result, not a
   defect.  MEASURED 2026-09-04 against a fresh ``build_ross_manifest.build()``
   (418 windows, after the master-ledger layer landed) with NO pins file at all:
   233 rows have no end boundary this module will read (120
   ``expected_action="reject"``, 89 ``"trade"``, 24 undefined) and 185 do.
   Watchlist reject rows are built with only an approximate scanner time in
   ``window_et`` and nothing else -- they are minted as ``"~" + approx_time_et``
   in ``build_ross_manifest.load_trades_files`` (the ``...::watchlist`` reject
   branch) -- so an exit pin is the only thing that can make most of them
   scorable.

   And MEASURED end to end on the same day, ``build()`` -> a real
   ``rossbench_pin_ross_events`` run against ``chili_hydrated`` -> this module:
   418 cases, 11 scorable, 407 unscorable.  The 407 break down as 261 manifest
   windows with no ledger row behind them and therefore no pin at all (182
   ``pin_missing`` + 79 ``pin_symbol_day_ambiguous``), 97 ``pin_unpinned``, 45
   ``pin_ambiguous``, 48 ``expected_action_undefined``, 7
   ``exit_pin_not_confirmed`` and 3 ``window_end_before_start`` (reasons
   overlap, so they do not sum to 407).  The binding constraint is TAPE
   COVERAGE, not this module: 38 of the ledger's 72 traded symbol-days have any
   hydrated ticks at all.
4. ``stated_only`` is not tape evidence.  If a pin ever arrives claiming both
   ``method="stated_only"`` and ``confidence="tape_confirmed"`` the case is
   refused as a contradiction rather than silently trusted.
5. An unscorable case carries ``window=None``.  No caller can accidentally hand
   an unverified window to the grader, because no unverified window object is
   ever constructed.

No thresholds are defined in this module.  There is nothing to tune: every
decision is a vocabulary comparison or a clock comparison.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date as date_cls, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .ross_replay_benchmark import ValidatedPhaseWindow

logger = logging.getLogger(__name__)


# --- vocabularies -----------------------------------------------------------
#
# These mirror scripts/rossbench_pin_ross_events.py.  They are deliberately
# closed sets: an unrecognised method or confidence is refused rather than
# guessed at, because "unknown" cannot be shown to be independent evidence.

MANIFEST_SCHEMA = "chili.ross_ground_truth_manifest.v1"
PINS_SCHEMA = "chili.ross_event_pins.v1"
ADAPTATION_SUMMARY_SCHEMA = "chili.ross_manifest_adaptation_summary.v1"

PIN_CONFIDENCE_CONFIRMED = "tape_confirmed"
PIN_CONFIDENCE_AMBIGUOUS = "tape_ambiguous"
PIN_CONFIDENCE_UNPINNED = "unpinned"
PIN_CONFIDENCES = frozenset(
    {PIN_CONFIDENCE_CONFIRMED, PIN_CONFIDENCE_AMBIGUOUS, PIN_CONFIDENCE_UNPINNED}
)

# Pin methods in the precedence order the pinner tries them.  All but
# ``stated_only`` bind the event to something observed independently of the
# recap narration (an audited broker-panel frame, or a tape print).
PIN_METHODS = (
    "frame_audit_stated",
    "price_match",
    "level_cross",
    "stated_only",
)
# A method that carries no independent observation.  It may exist in the pins
# file (it is the pinner's terminal fallback) but it can never certify a window.
NON_VERIFYING_PIN_METHODS = frozenset({"stated_only"})

EVIDENCE_ROLE = "after_fact_grading_only"

# Ross's recaps state a wall clock in US Eastern time and the manifest's
# ``date`` is the ET trading day (every layer passes it through
# ``build_ross_manifest._window`` verbatim).  This is a named market timezone,
# not a tunable.
ET_ZONE_NAME = "America/New_York"

END_BASIS_EXIT_PIN = "exit_pin"
END_BASIS_STATED = "stated_end"

# A leading "HH:MM[:SS] - HH:MM[:SS]" range, optionally prefixed with "~".
# Anchored at the start on purpose: see hindsight rule 2 in the module
# docstring.  Any trailing prose (e.g. "(low confidence)") is ignored, but a
# range that is *not* the leading token is never read as an end boundary.
_LEADING_RANGE_RE = re.compile(
    r"^\s*~?\s*(?P<start>\d{1,2}:\d{2}(?::\d{2})?)"
    r"\s*[-‐‑‒–—]\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?)"
)
# A whole field that is nothing but a clock, e.g. the ``stated_exit_et`` the
# master-ledger manifest layer emits.  Full-match: a narrative sentence with a
# clock buried in it is not a boundary we will trust.
_BARE_CLOCK_RE = re.compile(r"^\s*~?\s*(?P<clock>\d{1,2}:\d{2}(?::\d{2})?)\s*$")


# --- unscorable reason vocabulary -------------------------------------------
#
# Stable strings; the reporter groups on them.  Extend deliberately, never
# reuse.

REASON_PIN_MISSING = "pin_missing"
REASON_PIN_DUPLICATE_ROWS = "pin_duplicate_rows"
# Distinct from the above ON PURPOSE.  ``pin_duplicate_rows`` means two pin rows
# claim the SAME manifest_id -- a producer bug.  This one means the manifest
# window has no pin of its own and the (symbol, date) fallback found more than
# one candidate, which is the ordinary shape of a symbol Ross traded twice.
# Measured 2026-09-04 on the 418-window manifest: 79 windows land here and 0
# land on a genuine id collision, so folding them together (as this module did)
# reported 79 producer bugs that do not exist.
REASON_PIN_SYMBOL_DAY_AMBIGUOUS = "pin_symbol_day_ambiguous"
REASON_PIN_AMBIGUOUS = "pin_ambiguous"
REASON_PIN_UNPINNED = "pin_unpinned"
REASON_PIN_CONFIDENCE_UNKNOWN = "pin_confidence_unknown"
REASON_PIN_CONFIDENCE_MISSING = "pin_confidence_missing"
REASON_PIN_METHOD_MISSING = "pin_method_missing"
REASON_PIN_METHOD_UNKNOWN = "pin_method_unknown"
REASON_PIN_METHOD_CONFIDENCE_CONTRADICTION = "pin_method_confidence_contradiction"
REASON_ENTRY_PIN_MISSING = "entry_pin_missing"
REASON_EXIT_PIN_NOT_CONFIRMED = "exit_pin_not_confirmed"
REASON_PIN_TIMESTAMP_NAIVE = "pin_timestamp_naive"
REASON_PIN_TIMESTAMP_UNPARSEABLE = "pin_timestamp_unparseable"
REASON_PIN_DATE_MISMATCH = "pin_date_mismatch"
REASON_PIN_SYMBOL_MISMATCH = "pin_symbol_mismatch"
REASON_WINDOW_END_MISSING = "window_end_missing"
REASON_WINDOW_END_BEFORE_START = "window_end_before_start"
REASON_EXPECTED_ACTION_UNDEFINED = "expected_action_undefined"
REASON_LABEL_ID_MISSING = "label_id_missing"
REASON_SYMBOL_MISSING = "symbol_missing"
REASON_TRADE_DATE_MISSING = "trade_date_missing"
REASON_TRADE_DATE_UNPARSEABLE = "trade_date_unparseable"
REASON_STATED_END_TZ_UNAVAILABLE = "stated_end_timezone_unavailable"
REASON_WINDOW_SELF_CHECK_FAILED = "window_self_check_failed"

# Diagnostics are NOT unscorable reasons.  They flag a suspected defect
# upstream of this adapter without changing the grade.
#
# DIAGNOSTIC_PNL_ZERO_SENTINEL is a TRIPWIRE, and it is expected never to fire.
# The manifest layer maps the ledger's 0 sentinel to null before this module
# sees it, so an exact 0.0 arriving from ``source.kind == "master_ledger"``
# would mean that mapping regressed.  MEASURED on the 418-window manifest of
# 2026-09-04: 0 rows carry ross_net_usd == 0.0 and 196 carry null, i.e. the
# layer is behaving and this flag correctly counts zero.
DIAGNOSTIC_PNL_ZERO_SENTINEL = "pnl_zero_sentinel_suspect"
DIAGNOSTIC_PIN_JOINED_BY_SYMBOL_DATE = "pin_joined_by_symbol_date_fallback"
DIAGNOSTIC_PIN_DATE_UNCHECKED = "pin_date_unchecked_no_timezone"

_ZONE_CACHE: dict[str, Any] = {}


@dataclass(frozen=True)
class AdaptedCase:
    """One manifest row after the pin join: either a window, or a refusal.

    ``window`` is non-None only when every check passed, so a caller can feed
    ``validated_phase_windows(cases)`` straight to the grader without
    re-deriving the rules.  The pin fields stay populated even on refusal so
    the reporter can show *why* a case was dropped without re-reading pins.json.
    """

    label_id: str
    symbol: str
    trade_date: str
    account: str | None
    expected_action: str | None
    window: ValidatedPhaseWindow | None
    pin_method: str | None
    pin_confidence: str | None
    entry_pin_ts: datetime | None
    exit_pin_ts: datetime | None
    end_ts_basis: str | None
    stated_window_et: str | None
    ross_net_usd: float | None
    pnl_confidence: str | None
    source_kind: str | None
    unscorable_reasons: tuple[str, ...]
    diagnostics: tuple[str, ...]

    @property
    def scorable(self) -> bool:
        return self.window is not None and not self.unscorable_reasons


# --- small parsing helpers --------------------------------------------------


def _text(value: Any) -> str | None:
    """Non-empty stripped string, else None.  Never coerces non-strings."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _et_zone() -> Any:
    """Return the ET tzinfo, cached.

    Raises rather than falling back to a fixed offset: a hard-coded -4h would
    be a silently wrong window on either side of a DST boundary, and a wrong
    grading window is worse than a refused one.
    """
    zone = _ZONE_CACHE.get(ET_ZONE_NAME)
    if zone is None:
        from zoneinfo import ZoneInfo  # local import: only needed for stated ends

        zone = ZoneInfo(ET_ZONE_NAME)
        _ZONE_CACHE[ET_ZONE_NAME] = zone
    return zone


def _parse_timestamp(value: Any, *, key: str) -> tuple[datetime | None, str | None]:
    """Parse a pin timestamp.  Returns ``(dt, reason)``; both None means absent.

    A naive value is accepted as UTC ONLY when the key it came from asserts UTC
    (``entry_ts_utc_pinned`` and friends).  The rest of the harness carries
    naive-UTC tick clocks by the same convention, but the assertion has to come
    from the producer's own field name -- guessing a timezone would move a
    grading window by hours.
    """
    if value is None or value == "":
        return None, None

    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        # datetime.fromisoformat gained 'Z' support in 3.11; normalise anyway so
        # behaviour does not depend on the interpreter's patch level.
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None, REASON_PIN_TIMESTAMP_UNPARSEABLE
    else:
        # Numbers are refused: epoch seconds vs milliseconds vs a bare HHMMSS
        # cannot be told apart, and picking one would be an invented unit.
        return None, REASON_PIN_TIMESTAMP_UNPARSEABLE

    if parsed.tzinfo is None:
        if "utc" not in key.lower():
            return None, REASON_PIN_TIMESTAMP_NAIVE
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed, None


def _stated_end_clock(row: Mapping[str, Any]) -> str | None:
    """The stated end-of-window clock text, or None.

    Precedence: the master-ledger layer's strict ``stated_exit_et`` field, then
    the end of a *leading* range in ``window_et``.  Both are full/anchored
    matches; see hindsight rule 2.
    """
    explicit = _text(row.get("stated_exit_et"))
    if explicit is not None:
        match = _BARE_CLOCK_RE.match(explicit)
        if match:
            return match.group("clock")
        # A non-clock ``stated_exit_et`` is a producer bug, not an end boundary.
        logger.warning(
            "[ross_manifest_adapter] ignoring non-clock stated_exit_et=%r on %s",
            explicit,
            row.get("manifest_id"),
        )

    window_et = _text(row.get("window_et"))
    if window_et is None:
        return None
    match = _LEADING_RANGE_RE.match(window_et)
    return match.group("end") if match else None


def _et_clock_to_utc(
    trade_date: str, clock: str
) -> tuple[datetime | None, str | None]:
    """Combine the ET trading day with an ET wall clock into a UTC instant."""
    try:
        day = date_cls.fromisoformat(trade_date)
    except (TypeError, ValueError):
        return None, REASON_TRADE_DATE_UNPARSEABLE

    parts = clock.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) > 2 else 0
    except (IndexError, ValueError):
        return None, REASON_PIN_TIMESTAMP_UNPARSEABLE
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None, REASON_PIN_TIMESTAMP_UNPARSEABLE

    try:
        zone = _et_zone()
    except Exception:  # noqa: BLE001 - missing tzdata must not kill the run
        logger.error(
            "[ross_manifest_adapter] %s unavailable; stated-end windows refused",
            ET_ZONE_NAME,
        )
        return None, REASON_STATED_END_TZ_UNAVAILABLE

    # fold=0 is passed explicitly for determinism.  US DST transitions happen at
    # 02:00 local, outside every premarket/RTH window this bench grades, so the
    # ambiguous-hour case does not arise for these rows.
    local = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=zone, fold=0)
    return local.astimezone(timezone.utc), None


# --- pins index -------------------------------------------------------------


def _pin_rows(pins: Any) -> list[Mapping[str, Any]]:
    """Normalise the pins document into a flat list of pin rows.

    ASSUMPTION (see the report): ``pins.json`` is either a list of rows, a
    mapping with the rows under ``pins``/``rows``/``events``, or a mapping of
    ``manifest_id -> row``.  All three are accepted so a cosmetic change in the
    pinner's container does not silently produce zero joins.
    """
    if pins is None:
        return []
    if isinstance(pins, (list, tuple)):
        return [row for row in pins if isinstance(row, Mapping)]
    if isinstance(pins, Mapping):
        schema = _text(pins.get("schema"))
        if schema is not None and schema != PINS_SCHEMA:
            logger.warning(
                "[ross_manifest_adapter] pins schema=%r, expected %r",
                schema,
                PINS_SCHEMA,
            )
        for key in ("pins", "rows", "events", "windows"):
            candidate = pins.get(key)
            if isinstance(candidate, (list, tuple)):
                return [row for row in candidate if isinstance(row, Mapping)]
        # mapping of id -> row; inject the key so the join below still works
        rows: list[Mapping[str, Any]] = []
        for key, value in pins.items():
            if isinstance(value, Mapping) and isinstance(key, str):
                merged = dict(value)
                merged.setdefault("manifest_id", key)
                rows.append(merged)
        return rows
    logger.warning("[ross_manifest_adapter] unsupported pins container %s", type(pins))
    return []


def _pin_id(row: Mapping[str, Any]) -> str | None:
    for key in ("manifest_id", "label_id", "window_id"):
        value = _text(row.get(key))
        if value is not None:
            return value
    return None


def _index_pins(pins: Any) -> tuple[dict[str, list[Mapping[str, Any]]], dict[tuple[str, str], list[Mapping[str, Any]]]]:
    """Index pin rows by manifest_id and, as a fallback, by (symbol, date).

    ``manifest_id`` is unique by construction -- ``build_ross_manifest.build``
    raises ``duplicate manifest_ids`` before writing -- so it is the primary
    key.  The (symbol, date) fallback exists only for a pins file that predates
    the id, and it is used below ONLY when both sides of the join are unique for
    that symbol-day.
    """
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    by_symbol_day: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in _pin_rows(pins):
        pin_id = _pin_id(row)
        if pin_id is not None:
            by_id.setdefault(pin_id, []).append(row)
        symbol = _text(row.get("symbol"))
        day = _text(row.get("date")) or _text(row.get("trade_date"))
        if symbol and day:
            by_symbol_day.setdefault((symbol.upper(), day), []).append(row)
    return by_id, by_symbol_day


def _event_pin(
    row: Mapping[str, Any], kind: str
) -> tuple[datetime | None, str | None, str | None, str | None]:
    """Read one side (``entry``/``exit``) of a pin row.

    Returns ``(ts, method, confidence, reason)``.  Two layouts are accepted:

    * nested -- ``{"entry": {"ts_utc_pinned": ..., "pin_method": ...}}``
    * flat   -- ``{"entry_ts_utc_pinned": ..., "pin_method": ...}``

    The flat layout is what the plan's prose describes (``entry_ts_utc_pinned``
    + ``pin_method`` + ``pin_confidence``); the nested one is accepted because a
    row that pins both entry and exit needs per-side method/confidence.

    Method and confidence are resolved even when no timestamp is present: a row
    that says ``pin_confidence="unpinned"`` and carries no clock is the pinner's
    normal "not found" output, and it must be reported as ``pin_unpinned``, not
    as three separate "missing field" reasons.
    """
    nested = row.get(kind)
    if isinstance(nested, Mapping):
        source: Mapping[str, Any] = nested
        ts_keys = ("ts_utc_pinned", "ts_utc", "pinned_ts_utc", "ts_pinned", "ts")
        # Per-side values win; the row-level value is the documented default.
        method = (
            _text(nested.get("pin_method"))
            or _text(nested.get("method"))
            or _text(row.get("pin_method"))
        )
        confidence = (
            _text(nested.get("pin_confidence"))
            or _text(nested.get("confidence"))
            or _text(row.get("pin_confidence"))
        )
    else:
        source = row
        ts_keys = (
            f"{kind}_ts_utc_pinned",
            f"{kind}_ts_utc",
            f"{kind}_pinned_ts_utc",
            f"{kind}_ts",
        )
        method = _text(row.get(f"{kind}_pin_method")) or _text(row.get("pin_method"))
        confidence = _text(row.get(f"{kind}_pin_confidence")) or _text(
            row.get("pin_confidence")
        )

    for key in ts_keys:
        if key in source:
            ts, reason = _parse_timestamp(source.get(key), key=key)
            return ts, method, confidence, reason
    return None, method, confidence, None


# --- the adaptation ---------------------------------------------------------


def _manifest_windows(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if not isinstance(manifest, Mapping):
        raise TypeError("manifest must be a mapping")
    schema = _text(manifest.get("schema"))
    if schema is not None and schema != MANIFEST_SCHEMA:
        logger.warning(
            "[ross_manifest_adapter] manifest schema=%r, expected %r",
            schema,
            MANIFEST_SCHEMA,
        )
    windows = manifest.get("windows")
    if not isinstance(windows, (list, tuple)):
        logger.error(
            "[ross_manifest_adapter] manifest has no 'windows' list (keys=%s)",
            sorted(str(key) for key in manifest.keys()),
        )
        return []
    return [row for row in windows if isinstance(row, Mapping)]


def _adapt_row(
    row: Mapping[str, Any],
    by_id: Mapping[str, list[Mapping[str, Any]]],
    by_symbol_day: Mapping[tuple[str, str], list[Mapping[str, Any]]],
    symbol_day_window_counts: Mapping[tuple[str, str], int],
) -> AdaptedCase:
    reasons: list[str] = []
    diagnostics: list[str] = []

    label_id = _text(row.get("manifest_id"))
    symbol_raw = _text(row.get("symbol"))
    symbol = symbol_raw.upper() if symbol_raw else ""
    trade_date = _text(row.get("date")) or ""
    if label_id is None:
        reasons.append(REASON_LABEL_ID_MISSING)
    if symbol_raw is None:
        reasons.append(REASON_SYMBOL_MISSING)
    if not trade_date:
        reasons.append(REASON_TRADE_DATE_MISSING)

    expected_action = _text(row.get("expected_action"))
    # The grader's ``ExpectedAction`` literal is exactly {"trade","reject"}.
    # Anything else -- including the master ledger's genuinely-unknown outcome,
    # which the manifest layer maps to None rather than to a fabricated zero --
    # is unscorable here, matching ``grade_manifest_phase_labels``' own
    # treatment of a target outside that pair (it emits an "unscorable" row with
    # credit=None rather than grading it).
    if expected_action not in {"trade", "reject"}:
        reasons.append(REASON_EXPECTED_ACTION_UNDEFINED)
        expected_action = None

    # --- join to a pin row
    pin_row: Mapping[str, Any] | None = None
    candidates = list(by_id.get(label_id, ())) if label_id else []
    if len(candidates) > 1:
        reasons.append(REASON_PIN_DUPLICATE_ROWS)
    elif len(candidates) == 1:
        pin_row = candidates[0]
    elif symbol and trade_date:
        key = (symbol, trade_date)
        fallback = list(by_symbol_day.get(key, ()))
        # Only safe when the symbol-day is unambiguous on BOTH sides: one pin
        # row and one manifest window.  A symbol Ross traded twice in a day (the
        # VEEE-style multi-attempt sequence ``grade_manifest_phase_labels``
        # calls out in its aggregation note) must not collapse onto one pin.
        # With a window-keyed pins file this fallback is a legacy path: measured
        # 2026-09-04, all 157 pinnable rows joined on manifest_id and none
        # reached it.
        if len(fallback) == 1 and symbol_day_window_counts.get(key, 0) == 1:
            pin_row = fallback[0]
            diagnostics.append(DIAGNOSTIC_PIN_JOINED_BY_SYMBOL_DATE)
        elif len(fallback) > 1:
            reasons.append(REASON_PIN_SYMBOL_DAY_AMBIGUOUS)

    entry_ts = exit_ts = None
    pin_method = pin_confidence = None
    if pin_row is None:
        if not {REASON_PIN_DUPLICATE_ROWS, REASON_PIN_SYMBOL_DAY_AMBIGUOUS} & set(reasons):
            reasons.append(REASON_PIN_MISSING)
    else:
        # A pin row that joined on manifest_id but names a different symbol is a
        # producer bug, and grading one symbol's replay against another symbol's
        # instant would be silently meaningless.
        pin_symbol = _text(pin_row.get("symbol"))
        if symbol and pin_symbol is not None and pin_symbol.upper() != symbol:
            reasons.append(REASON_PIN_SYMBOL_MISMATCH)

        entry_ts, entry_method, entry_confidence, entry_reason = _event_pin(pin_row, "entry")
        exit_ts, _exit_method, exit_confidence, exit_reason = _event_pin(pin_row, "exit")
        pin_method = entry_method
        pin_confidence = entry_confidence
        if entry_reason:
            reasons.append(entry_reason)
        if exit_reason:
            # A malformed exit clock does not silently fall back to the stated
            # end -- that would substitute narration for a value the producer
            # believed it had pinned.
            reasons.append(exit_reason)

        if (
            entry_ts is None
            and entry_reason is None
            and entry_confidence != PIN_CONFIDENCE_UNPINNED
        ):
            # ``unpinned`` already explains the absent clock; do not report the
            # same fact twice.
            reasons.append(REASON_ENTRY_PIN_MISSING)

        if (
            exit_ts is not None
            and exit_confidence is not None
            and exit_confidence != entry_confidence
            and exit_confidence != PIN_CONFIDENCE_CONFIRMED
        ):
            # A per-side exit confidence that is weaker than the entry's means
            # the end boundary was chosen from more than one candidate cluster.
            # Refuse rather than grade against a boundary the pinner itself is
            # unsure of.  When both sides share one row-level confidence the
            # entry gate below already covers it, so this never double-reports.
            reasons.append(REASON_EXIT_PIN_NOT_CONFIRMED)

        # --- confidence gate: the whole point of the module
        if pin_confidence is None:
            reasons.append(REASON_PIN_CONFIDENCE_MISSING)
        elif pin_confidence == PIN_CONFIDENCE_AMBIGUOUS:
            reasons.append(REASON_PIN_AMBIGUOUS)
        elif pin_confidence == PIN_CONFIDENCE_UNPINNED:
            reasons.append(REASON_PIN_UNPINNED)
        elif pin_confidence != PIN_CONFIDENCE_CONFIRMED:
            reasons.append(REASON_PIN_CONFIDENCE_UNKNOWN)

        # --- method gate
        if pin_method is None:
            reasons.append(REASON_PIN_METHOD_MISSING)
        elif pin_method not in PIN_METHODS:
            reasons.append(REASON_PIN_METHOD_UNKNOWN)
        elif (
            pin_method in NON_VERIFYING_PIN_METHODS
            and pin_confidence == PIN_CONFIDENCE_CONFIRMED
        ):
            # A narrated time cannot confirm itself.  Refuse instead of trusting
            # the confidence field: this is the failure mode that would quietly
            # convert 132 narrated clocks into "verified" grading windows.
            reasons.append(REASON_PIN_METHOD_CONFIDENCE_CONTRADICTION)

    # --- end boundary
    stated_window_et = _text(row.get("window_et"))
    end_ts: datetime | None = None
    end_basis: str | None = None
    if exit_ts is not None:
        end_ts, end_basis = exit_ts, END_BASIS_EXIT_PIN
    else:
        clock = _stated_end_clock(row)
        if clock is None:
            reasons.append(REASON_WINDOW_END_MISSING)
        elif not trade_date:
            pass  # REASON_TRADE_DATE_MISSING is already recorded above
        else:
            end_ts, end_reason = _et_clock_to_utc(trade_date, clock)
            if end_reason:
                reasons.append(end_reason)
            else:
                end_basis = END_BASIS_STATED

    if entry_ts is not None and end_ts is not None and end_ts < entry_ts:
        # Never roll a stated end onto the next day to "fix" this: an end before
        # the pinned entry means the two sources disagree, and the honest answer
        # is a refusal, not a longer window.
        reasons.append(REASON_WINDOW_END_BEFORE_START)

    # --- the pin must land on the manifest's own trading day
    if entry_ts is not None and trade_date:
        try:
            zone = _et_zone()
        except Exception:  # noqa: BLE001
            diagnostics.append(DIAGNOSTIC_PIN_DATE_UNCHECKED)
        else:
            if entry_ts.astimezone(zone).date().isoformat() != trade_date:
                reasons.append(REASON_PIN_DATE_MISMATCH)

    # --- P/L passthrough, verbatim
    # The master ledger uses 0 as a NULL sentinel: counted over
    # project_ws/AgentOps/ross/ross_master_ledger.json in this tree, 30 of its
    # 187 trade rows carry ``pnl_usd == 0``.  Collapsing that to a real zero
    # breaks both Capture and Avoidance.  Mapping it to None is the manifest
    # layer's job; this adapter never rewrites the value, but it does flag an
    # exact zero arriving from that layer so a regression there shows up in the
    # report instead of scoring as a flat trade.
    ross_net_usd = row.get("ross_net_usd")
    if not isinstance(ross_net_usd, (int, float)) or isinstance(ross_net_usd, bool):
        ross_net_usd = None
    else:
        ross_net_usd = float(ross_net_usd)
    source = row.get("source")
    source_kind = _text(source.get("kind")) if isinstance(source, Mapping) else None
    if ross_net_usd == 0.0 and source_kind == "master_ledger":
        diagnostics.append(DIAGNOSTIC_PNL_ZERO_SENTINEL)

    # --- build the window only when nothing objected
    window: ValidatedPhaseWindow | None = None
    if not reasons and label_id and entry_ts is not None and end_ts is not None:
        window = ValidatedPhaseWindow(
            label_id=label_id,
            symbol=symbol,
            start_ts=entry_ts,
            end_ts=end_ts,
            decision_ts=entry_ts,
            evidence_source="tape_pin:%s" % pin_method,
            evidence_role=EVIDENCE_ROLE,
            # Computed from the pin rather than hard-coded True: if the reason
            # logic above is ever loosened, this still refuses to certify.
            independently_verified=(pin_confidence == PIN_CONFIDENCE_CONFIRMED),
        )
        # Re-validate with the grader's OWN predicate.  If a future change to
        # valid_for tightens the contract, the case degrades to unscorable here
        # instead of being silently rejected inside the grader.
        if not window.valid_for(label_id=label_id, symbol=symbol):
            reasons.append(REASON_WINDOW_SELF_CHECK_FAILED)
            window = None

    return AdaptedCase(
        label_id=label_id or "",
        symbol=symbol,
        trade_date=trade_date,
        account=_text(row.get("account")),
        expected_action=expected_action,
        window=window,
        pin_method=pin_method,
        pin_confidence=pin_confidence,
        entry_pin_ts=entry_ts,
        exit_pin_ts=exit_ts,
        end_ts_basis=end_basis,
        stated_window_et=stated_window_et,
        ross_net_usd=ross_net_usd,
        pnl_confidence=_text(row.get("pnl_confidence")),
        source_kind=source_kind,
        unscorable_reasons=tuple(dict.fromkeys(reasons)),
        diagnostics=tuple(dict.fromkeys(diagnostics)),
    )


def phase_windows_from_manifest(
    manifest: Mapping[str, Any],
    pins: Any = None,
) -> list[AdaptedCase]:
    """Join manifest rows to tape pins and emit grading cases.

    One ``AdaptedCase`` per manifest window, in manifest order, whether or not
    it produced a usable window.  Refusals are returned rather than dropped:
    the bench reports "n of m scorable" and a silently shortened list would
    make the denominator a lie.
    """
    rows = _manifest_windows(manifest)
    by_id, by_symbol_day = _index_pins(pins)

    symbol_day_window_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        symbol = _text(row.get("symbol"))
        day = _text(row.get("date"))
        if symbol and day:
            key = (symbol.upper(), day)
            symbol_day_window_counts[key] = symbol_day_window_counts.get(key, 0) + 1

    cases = [
        _adapt_row(row, by_id, by_symbol_day, symbol_day_window_counts) for row in rows
    ]
    scorable = sum(1 for case in cases if case.scorable)
    logger.info(
        "[ross_manifest_adapter] adapted %d manifest windows: %d scorable, %d unscorable",
        len(cases),
        scorable,
        len(cases) - scorable,
    )
    return cases


def validated_phase_windows(cases: Iterable[AdaptedCase]) -> list[ValidatedPhaseWindow]:
    """The windows the grader may consume -- scorable cases only."""
    return [case.window for case in cases if case.scorable and case.window is not None]


def expected_actions_by_label(cases: Iterable[AdaptedCase]) -> dict[str, str]:
    """``label_id -> "trade"|"reject"`` for scorable cases, for the grader call."""
    return {
        case.label_id: case.expected_action
        for case in cases
        if case.scorable and case.expected_action in {"trade", "reject"}
    }


def _counts(values: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = "null" if value is None else str(value)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def adaptation_summary(cases: Sequence[AdaptedCase]) -> dict[str, Any]:
    """Reportable counts.  Every case is in exactly one of scorable/unscorable."""
    scorable = [case for case in cases if case.scorable]
    unscorable = [case for case in cases if not case.scorable]
    by_action: dict[str, dict[str, int]] = {}
    for case in cases:
        key = case.expected_action or "undefined"
        bucket = by_action.setdefault(key, {"scorable": 0, "unscorable": 0})
        bucket["scorable" if case.scorable else "unscorable"] += 1
    return {
        "schema": ADAPTATION_SUMMARY_SCHEMA,
        "evidence_role": EVIDENCE_ROLE,
        "case_count": len(cases),
        "scorable_count": len(scorable),
        "unscorable_count": len(unscorable),
        "by_expected_action": dict(sorted(by_action.items())),
        "unscorable_reason_counts": _counts(
            reason for case in unscorable for reason in case.unscorable_reasons
        ),
        "diagnostic_counts": _counts(
            flag for case in cases for flag in case.diagnostics
        ),
        "pin_confidence_counts": _counts(case.pin_confidence for case in cases),
        "pin_method_counts": _counts(case.pin_method for case in cases),
        "end_ts_basis_counts": _counts(case.end_ts_basis for case in cases),
    }


def case_as_json_row(case: AdaptedCase) -> dict[str, Any]:
    """JSON-safe view of a case for the bench report."""
    return {
        "label_id": case.label_id,
        "symbol": case.symbol,
        "trade_date": case.trade_date,
        "account": case.account,
        "expected_action": case.expected_action,
        "scorable": case.scorable,
        "start_ts": case.window.start_ts.isoformat() if case.window else None,
        "end_ts": case.window.end_ts.isoformat() if case.window else None,
        "decision_ts": case.window.decision_ts.isoformat() if case.window else None,
        "evidence_source": case.window.evidence_source if case.window else None,
        "entry_pin_ts": case.entry_pin_ts.isoformat() if case.entry_pin_ts else None,
        "exit_pin_ts": case.exit_pin_ts.isoformat() if case.exit_pin_ts else None,
        "end_ts_basis": case.end_ts_basis,
        "pin_method": case.pin_method,
        "pin_confidence": case.pin_confidence,
        "stated_window_et": case.stated_window_et,
        "ross_net_usd": case.ross_net_usd,
        "pnl_confidence": case.pnl_confidence,
        "source_kind": case.source_kind,
        "unscorable_reasons": list(case.unscorable_reasons),
        "diagnostics": list(case.diagnostics),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Dry-run the join and print the summary.  Reads two files, writes nothing
    unless ``--json-out`` is given.  No DB, no network, no defaults invented:
    both inputs are required.

    Run from the repo root as a module so the package import resolves::

        python -m app.services.trading.momentum_neural.ross_manifest_adapter \\
            --manifest project_ws/AgentOps/ross_video_evidence/manifest.json \\
            --pins project_ws/AgentOps/ross/pins.json
    """
    parser = argparse.ArgumentParser(
        description="Adapt the Ross manifest + tape pins into grading windows (read-only)."
    )
    parser.add_argument("--manifest", required=True, help="path to manifest.json")
    parser.add_argument("--pins", required=True, help="path to pins.json")
    parser.add_argument("--json-out", help="write the adapted cases + summary here")
    args = parser.parse_args(argv)

    with open(args.manifest, encoding="utf-8") as handle:
        manifest = json.load(handle)
    with open(args.pins, encoding="utf-8") as handle:
        pins = json.load(handle)

    cases = phase_windows_from_manifest(manifest, pins)
    summary = adaptation_summary(cases)
    print(json.dumps(summary, indent=2, sort_keys=True))

    if args.json_out:
        payload = {
            "schema": ADAPTATION_SUMMARY_SCHEMA,
            "summary": summary,
            "cases": [case_as_json_row(case) for case in cases],
        }
        with open(args.json_out, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        print("wrote %s" % args.json_out)
    return 0


__all__ = [
    "ADAPTATION_SUMMARY_SCHEMA",
    "AdaptedCase",
    "EVIDENCE_ROLE",
    "END_BASIS_EXIT_PIN",
    "END_BASIS_STATED",
    "MANIFEST_SCHEMA",
    "NON_VERIFYING_PIN_METHODS",
    "PINS_SCHEMA",
    "PIN_CONFIDENCES",
    "PIN_CONFIDENCE_AMBIGUOUS",
    "PIN_CONFIDENCE_CONFIRMED",
    "PIN_CONFIDENCE_UNPINNED",
    "PIN_METHODS",
    "REASON_PIN_DUPLICATE_ROWS",
    "REASON_PIN_MISSING",
    "REASON_PIN_SYMBOL_DAY_AMBIGUOUS",
    "adaptation_summary",
    "case_as_json_row",
    "expected_actions_by_label",
    "main",
    "phase_windows_from_manifest",
    "validated_phase_windows",
]


if __name__ == "__main__":
    sys.exit(main())
