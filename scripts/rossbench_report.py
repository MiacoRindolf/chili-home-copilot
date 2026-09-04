#!/usr/bin/env python
"""Ross Parity Bench reporter — ``report.md`` + ``rpi.json`` (STEP 12).

Reads a finished bench run tree (``<out-dir>/<case>/<arm>/{run.json,timeline.jsonl}``)
and renders the two artefacts the operator actually reads:

  * ``report.md`` — the 2026-07-16 scorecard format
    (docs/STRATEGY/CC_REPORTS/2026-07-16_ross-5day-replay-scorecard.md) extended with the
    per-arm CHILI columns, the divergence stages and the emitting ``code_ref``.
  * ``rpi.json`` — schema ``chili.ross_parity_index.v1``.

WHAT THIS OUTPUT IS NOT
-----------------------
Every artefact this module writes carries ``causal_use_allowed=false`` and
``admission_claim=false``, and both lines are printed at the TOP of ``report.md`` — not in a
footnote.

The reason is mechanical, not editorial. The Tier-1 harness force-arms the FSM: it seeds a
``queued_live`` session and hands the runner ``risk_gate_allows=True`` so the run reaches the
FSM at all. Admission — "would the scanner have found this name, and would the risk gate have
let it through, at that instant?" — is therefore *supplied by the harness*, not measured by it.
Any latency, hit-rate or "CHILI would have caught it" claim derived from these numbers would be
a claim about the harness's own fixture. Hence ``admission_claim: false`` on every row and every
document. Stages ABOVE ``runner_never_started`` come only from the RECORDED side (live session
events / the ledger's ``xref_verdict``) and are labelled with their source per row, so a reader
can never mistake a harness-supplied stage for an observed one.

``evidence_grade`` is the third stamp and it is NOT one of those two. It used to be the
constant ``"DIAGNOSTIC_ONLY"``, which asserted a grade nothing had checked. It is now DERIVED:
the reporter feeds each run receipt to ``ross_replay_benchmark.diagnostic_coverage_from_replay_
receipt``, feeds the adapter's ``ValidatedPhaseWindow`` and that coverage record to
``grade_recap_phase_window``, and reads ``PhaseBenchmarkGrade.evidence_grade`` back. A run whose
windows fail their coverage checks stamps ``UNSCORABLE``; ``CERTIFIED`` is unreachable here by
construction and the document says why (``SEALED_COVERAGE_NOT_SUPPLIED``).

THE RECORDED SIDE
-----------------
The recorded column is measured when ``scripts/rossbench_export_recorded_events.py`` has
written ``<case>/recorded_events.jsonl``, and INFERRED from the ledger's ``xref_verdict``
otherwise. Two verdict tokens cannot be placed on the ladder from prose alone; the scorer
publishes that refusal as data (``ross_bench_scoring.XREF_VERDICT_PLACEMENT``) and
``render_recorded_side`` prints it, per run, with the counts and the measured reason. A bare
``unknown`` in a stage cell is a bug, not an outcome.

INPUTS AND THEIR SCHEMAS
------------------------
``run.json``    — the replay driver's receipt, schema ``chili.replay_v3_fsm_window_result.v1``,
                  written at scripts/replay_v3_fsm_window.py:1140-1183. Keys used here were read
                  from that emission site, not assumed: ``tree{head,tree,branch,dirty}``, ``env``
                  (the full knob echo built at :770-794), ``tape_sources``, ``sink_reset``
                  (``_reset_sim_sink`` returns ``{database, cleaned, suspended}``, :1544-1548),
                  ``mirrored{tick_rows,nbbo_rows,depth_rows}``, ``density``, ``grid_steps``,
                  ``mock``, ``pnl_usd``, ``final_state``, ``certification_failures``, ``events``.
``timeline.jsonl`` — one JSON object per line from the timeline writer (STEP 10); the reporter
                  uses ONLY the row flagged as the first divergence, for its ``code_ref``.
``manifest.json`` — ``chili.ross_ground_truth_manifest.v1`` (scripts/build_ross_manifest.py:51),
                  read for ``symbol/date/account/window_et/ross_net_usd/pnl_confidence`` plus the
                  master-ledger 4th-layer fields when present.
``pins.json``   — ``chili.ross_event_pins.v1`` (STEP 3), read for pin method + pin confidence.

NUMBERS THIS MODULE DOES NOT INVENT
-----------------------------------
The reporter computes no metric of its own. The Ross Parity Index is produced by the scorer
(STEP 11, ``ross_bench_scoring.ross_parity_index``), which is INJECTED — see ``build_report``.
The only arithmetic here is the A/B delta ``Δ = CHILI $ arm − CHILI $ base``, defined in the
table legend, and it is emitted only when both sides are present. There are no thresholds.

Every knob printed in the provenance block must arrive WITH a derivation string. The reporter
carries no derivation table of its own: a knob it cannot attribute is printed as
``derivation: UNATTRIBUTED`` and counted, because a silently unexplained default is exactly the
failure this project's doctrine rejects. ``--strict`` turns that count into a non-zero exit.

Usage:
  python scripts/rossbench_report.py --run-dir <out-dir> --manifest <manifest.json> \
      --pins <pins.json> [--base-arm base] [--out-dir <dir>] \
      [--ross-equity main=USD,small=USD] [--receipt-clock-tz utc] \
      [--allow-dirty-tree] [--strict]

``--ross-equity`` is Ross's OWN account size (the Capture %-of-equity denominator) and is
omitted unless the operator states the figure; omitted means "unknown" and the index reports
``equity_normalized: null``. It is not the driver's ``env.EQUITY``, which is the sim account
CHILI was sized against.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# SCHEMAS AND FROZEN LABELS
# ─────────────────────────────────────────────────────────────────────────────

RPI_SCHEMA = "chili.ross_parity_index.v1"

# Verified at scripts/replay_v3_fsm_window.py:199 (REPLAY_RESULT_SCHEMA).
DRIVER_RECEIPT_SCHEMA = "chili.replay_v3_fsm_window_result.v1"

# ── THE EVIDENCE GRADE IS DERIVED, NOT DECLARED ──────────────────────────────
# It used to be the constant ``EVIDENCE_GRADE = "DIAGNOSTIC_ONLY"``, which asserted a grade
# nothing had checked — worse than no stamp, because a reader takes a stamp as the output of
# a test. It is now the grader's own answer: the reporter builds a ``DiagnosticReplayCoverage``
# from each run receipt, hands it plus the adapter's ``ValidatedPhaseWindow`` to
# ``grade_recap_phase_window``, and reads ``PhaseBenchmarkGrade.evidence_grade`` back. A run
# whose windows fail their coverage checks now stamps UNSCORABLE, which the constant could
# never do.
#
# Uppercase in the document because the operator reads it as a stamp; the grader's own enum
# (``EvidenceGrade``, ross_replay_benchmark.py) is lowercase, and both spellings are printed
# so nobody has to guess they are the same thing.
EVIDENCE_GRADE_STAMPS = {
    "certified": "CERTIFIED",
    "diagnostic_only": "DIAGNOSTIC_ONLY",
    "unscorable": "UNSCORABLE",
}
# Worst -> best. The run-level grade is the BEST tier any row actually earned, with the
# per-tier counts printed beside it: a document's stamp answers "what is the strongest claim
# anything in here rests on?", and the counts stop that from being read as "every row".
EVIDENCE_GRADE_PRECEDENCE = ("unscorable", "diagnostic_only", "certified")
EVIDENCE_GRADE_WHEN_NOTHING_GRADED = "unscorable"

# The reporter never constructs a ``ValidatedReplayCoverage``, so ``certified`` is
# unreachable here BY CONSTRUCTION and the derivation can only return diagnostic_only or
# unscorable. That is not a policy choice: the sealed record binds a decision checkpoint to
# an exact sealed capture identity, and DiagnosticReplayCoverage's own docstring states that
# hydrated tape "cannot acquire such a binding after the fact". The bench replays hydrated
# tape. Stated in the document rather than left implicit, so a reader who never sees
# CERTIFIED knows why.
SEALED_COVERAGE_NOT_SUPPLIED = (
    "the reporter supplies no ValidatedReplayCoverage: this bench replays HYDRATED tape, "
    "which has no sealed-capture binding to acquire after the fact (see "
    "DiagnosticReplayCoverage's docstring in ross_replay_benchmark.py). 'certified' is "
    "therefore unreachable from this pipeline; the attainable grades are diagnostic_only "
    "and unscorable."
)

CAUSAL_USE_ALLOWED = False
ADMISSION_CLAIM = False

# ─────────────────────────────────────────────────────────────────────────────
# THE TWO LIMITATIONS THAT PRINT ON EVERY REPORT
# ─────────────────────────────────────────────────────────────────────────────
# Both are structural properties of the Tier-1 harness, not incidents, so they are not
# conditional on what a particular run found. They print on EVERY report, above the
# results, because a limitation discovered in a footnote after the number has been read
# has already failed to do its job.

LEADER_BOARD_MODE = "isolated_single_symbol"

LIMITATION_LEADER_BOARD = (
    "**Leader board is isolated to a single seeded symbol.** Each bench run seeds exactly one "
    "``momentum_symbol_viability`` row, so the replayed symbol is the #1 leader BY CONSTRUCTION — "
    "there is nothing else on the board to outrank it. Every leader-gated exemption (re-entry "
    "bypasses, leader-ignition paths, rank-conditional derates) is therefore OVER-GRANTED relative "
    "to a live board carrying the day's full universe. Reported as "
    f"``leader_board_mode={LEADER_BOARD_MODE!r}``. A stage that depends on a leader exemption is a "
    "Tier-1 artefact until it is re-measured against a populated board."
)


def limitation_depth(depth_rows_by_run: Mapping[str, int]) -> tuple[bool, str]:
    """The depth limitation, stated against the MEASURED mirrored-depth counts.

    ``depth_levers_unmeasurable`` is asserted only when every receipt in the run mirrored
    ZERO depth rows. That is the expected state — ``chili_hydrated.iqfeed_depth_snapshots``
    is empty — but the reporter reads ``mirrored.depth_rows`` from the receipts
    (scripts/replay_v3_fsm_window.py:1153) rather than declaring it, so a run that DOES carry
    depth is not mislabelled by a stale sentence.

    The point of the flag is that an unmeasurable lever must be REPORTED as unmeasurable and
    never rendered as a delta of 0.00: a depth-reading exit lever with no book to read is a
    silent no-op, and a no-op's A/B delta is exactly 0.00 — indistinguishable, on the page,
    from "we measured this lever and it did nothing".
    """
    counts = {k: int(v) for k, v in (depth_rows_by_run or {}).items()}
    total = sum(counts.values())
    unmeasurable = (total == 0)
    if not counts:
        # No receipt at all. "Unmeasurable" is still the right answer for the reader, but the
        # basis is the ABSENCE of evidence, and saying "measured 0" here would be a lie.
        return True, (
            "**No run receipt carried a depth count, so no depth-reading lever can be "
            "spoken to.** ``depth_levers_unmeasurable: true`` — asserted from the ABSENCE of "
            "receipts, not from a measured zero. Do not read any 0.00 delta in this report as "
            "evidence about a depth-reading lever."
        )
    if unmeasurable:
        body = (
            "**No L2 book was mirrored — every depth-reading lever is UNMEASURABLE.** Measured "
            f"across {len(counts)} run receipt(s): ``mirrored.depth_rows`` totals 0 "
            "(``chili_hydrated.iqfeed_depth_snapshots`` is empty for this corpus). A lever that "
            "reads the book therefore executes as a silent no-op, and a no-op's A/B delta is "
            "0.00 — which on a page is indistinguishable from a lever that was measured and did "
            "nothing. Reported as ``depth_levers_unmeasurable: true``. Do NOT read any 0.00 "
            "delta in this report as evidence about a depth-reading lever."
        )
    else:
        body = (
            f"**L2 book partially mirrored: {total} depth row(s) across {len(counts)} run "
            "receipt(s).** ``depth_levers_unmeasurable: false``. Per-run counts are in the "
            "provenance block; a run with 0 rows still cannot speak to a depth-reading lever, so "
            "check the per-run column before reading any delta as a lever result."
        )
    return unmeasurable, body


# ─────────────────────────────────────────────────────────────────────────────
# KEY ALIASES
# ─────────────────────────────────────────────────────────────────────────────
# The manifest 4th layer (STEP 2), the pin file (STEP 3), the bench runner (STEP 9), the
# timeline writer (STEP 10) and the scorer (STEP 11) are authored in parallel with this
# module. Only the DRIVER receipt schema above was read from its emission site; for the
# others the reporter accepts a short, explicit alias list per field and records which
# alias it used, so a naming mismatch surfaces as a named field in the output instead of a
# crash or a silently empty column. Collapse each of these to one name once the sibling
# schemas are frozen.

_CASE_ID_KEYS = ("case_id", "case", "ross_case")
_MANIFEST_ROWS_KEYS = ("windows", "rows", "cases")
_PIN_ROWS_KEYS = ("pins", "windows", "rows")
_PIN_METHOD_KEYS = ("pin_method", "method")
_PIN_CONFIDENCE_KEYS = ("pin_confidence", "confidence")
# The pin builder emits ONE ROW PER LEG (``leg`` is "entry" | "exit",
# scripts/rossbench_pin_ross_events.py:763,829), so a symbol-day has up to two rows and the
# ENTRY leg is the one that anchors the grading window. Selecting by leg rather than taking
# the first row matters: an exit-leg pin can be ``tape_confirmed`` while the entry leg is
# ``tape_ambiguous``, and reading the exit's confidence would mark the case scorable on
# evidence about the wrong instant.
_PIN_LEG_KEYS = ("leg",)
PIN_ENTRY_LEG = "entry"
_PIN_ENTRY_KEYS = ("pin_second_et", "entry_pin_et", "entry_pin", "pin_second_utc",
                   "entry_pin_utc", "entry_ts")
_MANIFEST_ENTRY_ET_KEYS = ("stated_entry_et", "window_et", "entry_et")
_MANIFEST_EXIT_ET_KEYS = ("stated_exit_et", "exit_et")
_ROSS_PNL_KEYS = ("ross_net_usd", "ross_pnl_usd", "pnl_usd")
_TIMELINE_DIVERGENCE_KEYS = ("first_divergence", "is_first_divergence", "divergence")
_TIMELINE_CODE_REF_KEYS = ("code_ref", "coderef", "emit_site")
_KNOB_BLOCK_KEYS = ("knobs", "knob_derivations", "derivations")
_NESTED_RECEIPT_KEYS = ("replay_result", "receipt", "driver_result", "run", "replay")
# Ross's own account equity, if the ground truth ever states it. Matches the scorer's
# ``_CASE_ROSS_EQUITY_KEYS`` (ross_bench_scoring.py:712) so a row that states one is used
# in preference to any operator-supplied map. NOT related to the driver's ``env.EQUITY``,
# which is CHILI's sim equity — see ``ROSS_EQUITY_IS_NOT_SIM_EQUITY`` below.
_MANIFEST_ROSS_EQUITY_KEYS = ("ross_equity_usd", "equity_usd", "account_equity_usd")
_MANIFEST_MECHANISM_KEYS = ("xref_mechanism", "mechanism")
_MANIFEST_RECORDED_PNL_KEYS = ("chili_outcome_usd", "chili_recorded_pnl_usd", "recorded_pnl_usd")
# Recorded (live-lane) events for a case. Case-level, not arm-level: the recorded side is
# the same whatever CHILI arm we replayed.
#
# THE PRODUCER IS scripts/rossbench_export_recorded_events.py, which writes
# ``recorded_events.jsonl`` (its ``EVENTS_FILENAME``) plus a ``recorded_events.meta.json``
# sidecar. Until that script existed nothing in the tree wrote any of these names, so
# ``recorded_stage`` ALWAYS fell back to the ledger's ``xref_verdict`` — see
# ``recorded_side_limitation`` below for what that costs and which verdicts it costs it on.
# The other two names are accepted because a hand-assembled tree may carry them; the
# exporter writes only the first ``.jsonl``.
_RECORDED_EVENT_FILES = ("recorded_events.json", "recorded_events.jsonl", "recorded.jsonl")
RECORDED_EVENTS_EXPORTER = "scripts/rossbench_export_recorded_events.py"

# Keys the reporter puts on each case dict it hands to the scorer. Verified against the
# scorer's own accessor tables (ross_bench_scoring.py:707-718): ``ross_pnl_usd`` is read
# through ``sentinel_zero_to_none``; ``replay`` is the driver receipt itself, from which
# the scorer reads ``pnl_usd`` (receipt line 1174) and ``entries`` (line 1179) rather than
# trusting a number the reporter re-derived.
CASE_FOR_SCORER_KEYS = (
    "case_id", "symbol", "date", "account", "ross_pnl_usd", "ross_equity_usd",
    "xref_verdict", "xref_mechanism", "chili_outcome_usd",
    "recorded_stage", "replay",
)

# The one thing that must never be conflated in this pipeline.
ROSS_EQUITY_IS_NOT_SIM_EQUITY = (
    "Ross's account equity (the Capture %-of-equity denominator) is NOT the driver's "
    "env.EQUITY, which is the sim account CHILI was sized against. The reporter never "
    "substitutes one for the other: when no ground-truth row states Ross's equity and the "
    "operator supplies none, Capture's equity_normalized block is null."
)

# ─────────────────────────────────────────────────────────────────────────────
# UNSCORABLE REASON VOCABULARY
# ─────────────────────────────────────────────────────────────────────────────
# A fixed vocabulary so "why was this case dropped" is greppable and countable rather than
# free prose. Every reason the reporter itself can raise is here; the scorer may add its own
# (they are passed through verbatim and marked with their source).

REASON_NO_RUN_OUTPUT = "no_run_output"                    # case dir exists, no readable receipt
REASON_ARM_MISSING = "arm_missing"                        # ran in some arms but not all
REASON_RECEIPT_UNREADABLE = "receipt_unreadable"          # run.json present but not parseable
REASON_RECEIPT_WRONG_SCHEMA = "receipt_wrong_schema"      # not chili.replay_v3_fsm_window_result.v1
REASON_ROSS_PNL_ABSENT = "ross_pnl_absent"                # ledger 0-sentinel -> None; NOT a zero
REASON_PIN_AMBIGUOUS = "pin_ambiguous"                    # >1 tape cluster matched the stated time
REASON_UNPINNED = "unpinned"                              # no tape print could carry the event
REASON_NO_MANIFEST_ROW = "no_manifest_row"                # ran, but no ground-truth row to grade
# The symbol-day has SEVERAL ground-truth rows and nothing named which one ran. Refusing is
# the point: 62 of 217 symbol-days are ambiguous, their rows carry different entries, exits
# and Ross dollars, and grading against the wrong one is a silent wrong answer.
REASON_MANIFEST_ROW_AMBIGUOUS = "manifest_row_ambiguous"
REASON_EQUITY_MISMATCH = "equity_mismatch"                # arms priced at different sizes
REASON_CASE_ID_MISMATCH = "case_id_mismatch"              # dir name != the runner's own case id
# The scorer keeps its OWN exclusion list (``ross_parity_index()["excluded_cases"]``, with a
# per-metric reason); those are rendered verbatim in the RPI section rather than merged into
# this vocabulary, because "the reporter could not grade it" and "the index excluded it from
# one metric" are different statements and merging them would lose which metric was affected.

# Pin confidences that make a case scorable. ``tape_ambiguous`` and ``unpinned`` are EXPECTED
# in volume and are reported, not treated as failures (STEP 3), but they cannot anchor a
# grading window, so they leave the scored population.
PIN_CONFIDENCE_SCORABLE = ("tape_confirmed",)


# ─────────────────────────────────────────────────────────────────────────────
# SMALL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _first(mapping: Any, names: Sequence[str], default: Any = None) -> Any:
    """First present, non-None value among ``names``. Returns ``default`` otherwise."""
    if not isinstance(mapping, Mapping):
        return default
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def _first_key(mapping: Any, names: Sequence[str]) -> Optional[str]:
    """Which alias actually carried the value — recorded so a mismatch is visible."""
    if not isinstance(mapping, Mapping):
        return None
    for name in names:
        if name in mapping and mapping[name] is not None:
            return name
    return None


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _md_cell(value: Any) -> str:
    """Markdown-table-safe cell. A pipe inside a stage/reason string would silently split
    the row into two columns and shift every value after it."""
    if value is None:
        return "—"
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip() or "—"


def _fmt_usd(value: Optional[float], *, absent: str = "—") -> str:
    """``None`` prints as ``absent`` and NEVER as ``$0.00``.

    Load-bearing: the master ledger uses ``0`` as a NULL SENTINEL (measured across all 187
    rows: entry_px 67 zeros, exit_px 103, shares 118, pnl_usd 30). A 0 rendered as a real
    zero would silently enter Avoidance ("CHILI $ >= 0") and Capture as a genuine flat.
    """
    if value is None:
        return absent
    return f"{value:+,.2f}"


def _fmt_ratio(value: Any) -> str:
    f = _as_float(value)
    if f is None:
        return "—"
    return f"{f:.4f}"


def _iso(value: Any) -> Optional[str]:
    """``datetime`` -> ISO string; anything else -> ``str`` or ``None``. Never invents a tz."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _phase_grade_json(grade: Any) -> Optional[dict]:
    """A ``PhaseBenchmarkGrade`` as JSON, or ``None``.

    Read by attribute rather than by ``dataclasses.asdict`` so the reporter does not fail on
    a grader that grew a field, and so ``coverage_reasons`` — which carries the DIAGNOSTIC
    caveats alongside credit on a passing row, and the disqualifying reasons on a failing
    one (ross_replay_benchmark.grade_recap_phase_window) — is never dropped.
    """
    if grade is None:
        return None
    inner = getattr(grade, "grade", None)
    return {
        "label_id": getattr(grade, "label_id", None),
        "symbol": getattr(grade, "symbol", None),
        "evidence_grade": getattr(grade, "evidence_grade", None),
        "matching_trade_count": getattr(grade, "matching_trade_count", None),
        "aggregate_pnl_usd": getattr(grade, "aggregate_pnl_usd", None),
        "coverage_reasons": list(getattr(grade, "coverage_reasons", ()) or ()),
        "diagnostic_provenance": (dict(getattr(grade, "diagnostic_provenance", None) or {})
                                  or None),
        "status": getattr(inner, "status", None),
        "credit": getattr(inner, "credit", None),
        "expected_action": getattr(inner, "expected_action", None),
        "actual_action": getattr(inner, "actual_action", None),
        "reason": getattr(inner, "reason", None),
    }


def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _read_jsonl(path: str) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # a partially-flushed final line is not a reason to lose the file
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _fallback_normalize_account(raw: Any) -> str:
    """FALLBACK ONLY — used when the scorer module could not be imported.

    The bucket vocabulary belongs to ONE place: ``ross_bench_scoring.normalize_account``
    (ross_bench_scoring.py:436-450, aliases at :429-434 — ``big`` collapses into ``main``,
    a compound value such as "small+main" gets its own ``mixed`` bucket, absent is
    ``unknown``). The reporter prefers that function and falls back here only so the report
    still renders without it; when this path runs it is recorded as a load warning, because
    a second copy of a vocabulary is exactly how two definitions drift apart.
    """
    if raw is None:
        return "unknown"
    tokens = [t.strip().lower()
              for t in str(raw).replace("/", "+").replace(",", "+").replace("&", "+")
              .replace(" and ", "+").split("+") if t.strip()]
    mapped = {("main" if t in ("main", "big") else "small" if t == "small" else "unknown")
              for t in tokens}
    mapped.discard("unknown")
    if not mapped:
        return "unknown"
    return mapped.pop() if len(mapped) == 1 else "mixed"


# ─────────────────────────────────────────────────────────────────────────────
# DATA CARRIERS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ArmRun:
    """One (case, arm) directory's worth of evidence."""
    arm: str
    run_dir: str
    receipt: Optional[dict] = None
    receipt_error: Optional[str] = None
    code_ref: Optional[str] = None
    code_ref_source: Optional[str] = None
    divergence_row: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return self.receipt is not None

    @property
    def pnl_usd(self) -> Optional[float]:
        if not self.ok:
            return None
        return _as_float(self.receipt.get("pnl_usd"))

    @property
    def env(self) -> dict:
        return dict((self.receipt or {}).get("env") or {})

    @property
    def depth_rows(self) -> int:
        return int(((self.receipt or {}).get("mirrored") or {}).get("depth_rows") or 0)


@dataclass
class CaseRow:
    """One symbol-day, across every arm."""
    case_id: str
    symbol: Optional[str] = None
    date: Optional[str] = None
    account: str = "unknown"                              # scorer bucket vocabulary
    account_raw: Any = None                               # exactly what the manifest said
    expected_action: Optional[str] = None
    ross_pnl_usd: Optional[float] = None
    ross_pnl_confidence: Optional[str] = None
    ross_equity_usd: Optional[float] = None
    ross_window_et: Optional[str] = None
    pin_method: Optional[str] = None
    pin_confidence: Optional[str] = None
    xref_verdict: Optional[str] = None
    xref_mechanism: Optional[str] = None
    chili_outcome_usd: Optional[float] = None
    recorded_stage: Any = None                            # scorer Stage (a str subclass)
    recorded_stage_source: Optional[str] = None
    recorded_events: list = field(default_factory=list)
    recorded_events_source: Optional[str] = None          # which file carried them
    replay_stage: dict = field(default_factory=dict)      # arm -> scorer Stage
    arms: dict = field(default_factory=dict)              # arm -> ArmRun
    unscorable_reasons: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    # ── the grader's tier, per arm (filled by ``grade_evidence``) ───────────────────
    # ``manifest_id`` is the join key into the adapter's ``AdaptedCase.label_id``
    # (ross_manifest_adapter._adapt_row reads it from the manifest row's ``manifest_id``).
    # It is the id of the SAME manifest row this case's ground truth came from — the report
    # never grades one row and reports another.
    manifest_id: Optional[str] = None
    phase_window: Any = None                              # grader ValidatedPhaseWindow|None
    phase_window_reasons: list = field(default_factory=list)   # adapter's refusal reasons
    phase_grade: dict = field(default_factory=dict)       # arm -> PhaseBenchmarkGrade
    evidence_grade_by_arm: dict = field(default_factory=dict)  # arm -> grader enum str
    coverage_problems: list = field(default_factory=list)      # per-arm coverage failures

    @property
    def scorable(self) -> bool:
        return not self.unscorable_reasons

    def pnl(self, arm: str) -> Optional[float]:
        run = self.arms.get(arm)
        return run.pnl_usd if run is not None else None

    def delta(self, base_arm: str, arm: str) -> Optional[float]:
        """Δ = CHILI $ arm − CHILI $ base. Only when BOTH sides actually ran."""
        a, b = self.pnl(arm), self.pnl(base_arm)
        if a is None or b is None:
            return None
        return a - b

    def scorer_payload(self, arm: str) -> dict:
        """One case, as the scorer reads it, for ONE arm.

        ``replay`` is the driver receipt VERBATIM rather than a reporter-derived P&L: the
        scorer reads ``pnl_usd`` and ``entries`` straight off it (ross_bench_scoring.py:733
        and :745, citing receipt lines 1174 and 1179), so there is exactly one place those
        numbers are interpreted. When the arm has no receipt the key is absent and the
        scorer excludes the row with its own reason — it is NOT scored as a $0 capture.

        ``account`` is the RAW manifest value so the scorer's own ``normalize_account``
        decides the bucket; ``self.account`` is only the display form.
        """
        run = self.arms.get(arm)
        payload: dict = {
            "case_id": self.case_id,
            "symbol": self.symbol,
            "date": self.date,
            "account": self.account_raw,
            "ross_pnl_usd": self.ross_pnl_usd,
            "ross_equity_usd": self.ross_equity_usd,
            "xref_verdict": self.xref_verdict,
            "xref_mechanism": self.xref_mechanism,
            "chili_outcome_usd": self.chili_outcome_usd,
        }
        if self.recorded_stage is not None:
            payload["recorded_stage"] = self.recorded_stage
        if run is not None and run.ok:
            payload["replay"] = run.receipt
        return payload

    def to_public_case(self) -> dict:
        """The per-case record written into ``rpi.json`` (report-side, not scorer-side)."""
        return {
            "case_id": self.case_id,
            "symbol": self.symbol,
            "date": self.date,
            "account": self.account,
            "account_raw": self.account_raw,
            "expected_action": self.expected_action,
            "ross_pnl_usd": self.ross_pnl_usd,
            "ross_pnl_confidence": self.ross_pnl_confidence,
            "ross_equity_usd": self.ross_equity_usd,
            "ross_window_et": self.ross_window_et,
            "pin_method": self.pin_method,
            "pin_confidence": self.pin_confidence,
            "xref_verdict": self.xref_verdict,
            "recorded_stage": (str(self.recorded_stage) if self.recorded_stage is not None else None),
            "recorded_stage_source": self.recorded_stage_source,
            "recorded_stage_detail": dict(getattr(self.recorded_stage, "detail", {}) or {}),
            "recorded_events_loaded": len(self.recorded_events),
            "recorded_events_source": self.recorded_events_source,
            "manifest_id": self.manifest_id,
            "phase_window": (None if self.phase_window is None else {
                "label_id": getattr(self.phase_window, "label_id", None),
                "start_ts": _iso(getattr(self.phase_window, "start_ts", None)),
                "end_ts": _iso(getattr(self.phase_window, "end_ts", None)),
                "decision_ts": _iso(getattr(self.phase_window, "decision_ts", None)),
                "evidence_source": getattr(self.phase_window, "evidence_source", None),
                "independently_verified": getattr(
                    self.phase_window, "independently_verified", None),
            }),
            "phase_window_reasons": list(self.phase_window_reasons),
            "arms": {
                arm: {
                    "pnl_usd": run.pnl_usd,
                    "replay_stage": (str(self.replay_stage[arm])
                                     if self.replay_stage.get(arm) is not None else None),
                    "replay_stage_source": getattr(self.replay_stage.get(arm), "source", None),
                    "final_state": (run.receipt or {}).get("final_state"),
                    "certification_failures": list((run.receipt or {}).get("certification_failures") or []),
                    "code_ref": run.code_ref,
                    "code_ref_source": run.code_ref_source,
                    "receipt_error": run.receipt_error,
                    "evidence_grade": self.evidence_grade_by_arm.get(arm),
                    "phase_grade": _phase_grade_json(self.phase_grade.get(arm)),
                }
                for arm, run in self.arms.items()
            },
            "scorable": self.scorable,
            "unscorable_reasons": list(self.unscorable_reasons),
            "coverage_problems": list(self.coverage_problems),
            "notes": list(self.notes),
        }


@dataclass
class Bundle:
    """Everything the renderers need, already gathered. Pure from here on."""
    run_dir: str
    cases: list = field(default_factory=list)             # list[CaseRow]
    arms: list = field(default_factory=list)              # ordered arm names
    base_arm: str = "base"
    manifest_meta: dict = field(default_factory=dict)
    pins_meta: dict = field(default_factory=dict)
    bench_plan: Optional[dict] = None                     # <run-dir>/bench.json, if written
    alias_hits: dict = field(default_factory=dict)        # field -> alias actually used
    load_warnings: list = field(default_factory=list)
    # The adapter's join of manifest x pins (ross_manifest_adapter.phase_windows_from_manifest).
    # Kept whole, not just the matched rows, because ``adaptation_summary`` is the denominator
    # a reader needs to see: "n of m windows carried a usable grading window".
    adapted_cases: list = field(default_factory=list)     # list[AdaptedCase]
    adaptation_summary: dict = field(default_factory=dict)
    adapter_available: bool = False
    adapter_problem: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# LOADING
# ─────────────────────────────────────────────────────────────────────────────

def extract_receipt(doc: Any) -> tuple[Optional[dict], Optional[str]]:
    """Return ``(receipt, error)``.

    ``run.json`` is expected to BE the driver receipt: the bench runner points the driver's
    ``REPLAY_JSON_OUT`` at it and pins ``ARM=on``, and the driver only suffixes the path
    (``.g4_on``/``.g4_off``) when ``ARM`` is neither ``on`` nor ``off``
    (scripts/replay_v3_fsm_window.py:809-817). A runner that instead WRAPS the receipt is
    tolerated by looking one level down, because losing the whole provenance block to a
    wrapper key would be a silent, total loss.
    """
    if not isinstance(doc, Mapping):
        return None, "run.json is not a JSON object"
    if doc.get("schema") == DRIVER_RECEIPT_SCHEMA:
        return dict(doc), None
    for key in _NESTED_RECEIPT_KEYS:
        inner = doc.get(key)
        if isinstance(inner, Mapping) and inner.get("schema") == DRIVER_RECEIPT_SCHEMA:
            nested = dict(inner)
            # Knob derivations, if the runner emits them, live on the WRAPPER.
            for kk in _KNOB_BLOCK_KEYS:
                if kk in doc and kk not in nested:
                    nested[kk] = doc[kk]
            for kk in _CASE_ID_KEYS:
                if kk in doc and kk not in nested:
                    nested[kk] = doc[kk]
            return nested, None
    return None, (
        f"schema is {doc.get('schema')!r}, expected {DRIVER_RECEIPT_SCHEMA!r} "
        "(directly or under one of " + "/".join(_NESTED_RECEIPT_KEYS) + ")"
    )


def first_divergence_code_ref(timeline_rows: Sequence[Mapping[str, Any]]) -> tuple[Optional[str], Optional[dict]]:
    """The ``file:line`` of the emit site on the FIRST-DIVERGENCE row of the timeline.

    The timeline writer (STEP 10) marks that row; this module does not decide what diverged.
    If no row is flagged, the reporter reports no code_ref rather than picking the first row
    that happens to have one — an unflagged code_ref would be a guess presented as a citation.

    The ``code_ref`` lives on the row's CHILI EVENTS, not on the row itself
    (scripts/rossbench_timeline.py:1077-1085 attaches it per event; the row document at
    :1374-1393 has no top-level ``code_ref``), so the per-event refs are read and de-duplicated
    in order. A top-level key is still accepted first, for a timeline writer that hoists one.
    A divergence second can carry more than one emit site; all of them are reported, because
    picking one would assert a primary cause the timeline did not name.
    """
    for row in timeline_rows or []:
        flag = _first(row, _TIMELINE_DIVERGENCE_KEYS)
        if not flag:
            continue
        ref = _first(row, _TIMELINE_CODE_REF_KEYS)
        if ref is not None:
            return str(ref), dict(row)
        refs: list = []
        for event in (row.get("chili") or []):
            if not isinstance(event, Mapping):
                continue
            value = _first(event, _TIMELINE_CODE_REF_KEYS)
            if value is not None and str(value) not in refs:
                refs.append(str(value))
        return ("; ".join(refs) if refs else None), dict(row)
    return None, None


def load_arm_dir(arm: str, path: str) -> ArmRun:
    run = ArmRun(arm=arm, run_dir=path)
    run_json = os.path.join(path, "run.json")
    if not os.path.exists(run_json):
        run.receipt_error = "run.json missing"
        return run
    try:
        doc = _read_json(run_json)
    except (OSError, ValueError) as exc:
        run.receipt_error = f"run.json unreadable: {exc}"
        return run
    receipt, err = extract_receipt(doc)
    run.receipt, run.receipt_error = receipt, err

    # ── code_ref: prefer the timeline writer's own meta document ────────────────────────
    # TWO producers write ``timeline.jsonl`` into this same directory and they are NOT the
    # same document. The bench runner writes a flat event log there (rows of
    # {ts, kind, what, why, px, qty} — scripts/ross_replay_bench.py:1043-1050) which carries
    # NEITHER a divergence flag nor a code_ref. The timeline writer writes the per-second
    # document with ``first_divergence`` and per-event ``code_ref``
    # (scripts/rossbench_timeline.py:1374-1393) plus ``timeline.meta.json``, whose
    # ``first_divergence`` block already resolves ``code_refs`` (:1134-1146).
    #
    # So the meta file is read FIRST — it is the writer's own answer to "where did they
    # diverge", not a re-derivation of it — and the JSONL scan is the fallback for a tree
    # written before the meta file existed. Whichever answered is recorded in
    # ``code_ref_source`` so the report never implies a citation it inferred.
    meta_path = os.path.join(path, "timeline.meta.json")
    if os.path.exists(meta_path):
        try:
            meta = _read_json(meta_path)
        except (OSError, ValueError) as exc:
            logger.warning("[rossbench_report] timeline meta unreadable %s: %s", meta_path, exc)
            meta = None
        div = (meta or {}).get("first_divergence") if isinstance(meta, Mapping) else None
        if isinstance(div, Mapping):
            refs = [str(r) for r in (div.get("code_refs") or []) if r]
            run.code_ref = "; ".join(dict.fromkeys(refs)) or None
            run.divergence_row = dict(div)
            run.code_ref_source = "timeline.meta.json:first_divergence"
            return run
        if isinstance(meta, Mapping) and "first_divergence" in meta:
            # Explicit null: the writer looked and found no divergence. That is an ANSWER,
            # and re-scanning the JSONL for one would overturn it with a guess.
            run.code_ref_source = "timeline.meta.json:no_divergence"
            return run

    timeline = os.path.join(path, "timeline.jsonl")
    if os.path.exists(timeline):
        try:
            rows = _read_jsonl(timeline)
        except OSError as exc:
            logger.warning("[rossbench_report] timeline unreadable %s: %s", timeline, exc)
            rows = []
        run.code_ref, run.divergence_row = first_divergence_code_ref(rows)
        run.code_ref_source = "timeline.jsonl" if run.code_ref else "timeline.jsonl:unflagged"
    return run


def discover_runs(run_dir: str) -> dict:
    """``{case_dir_name: {arm_name: ArmRun}}`` from ``<run-dir>/<case>/<arm>/``."""
    out: dict = {}
    if not os.path.isdir(run_dir):
        raise SystemExit(f"[rossbench_report] --run-dir {run_dir!r} is not a directory")
    for case_name in sorted(os.listdir(run_dir)):
        case_path = os.path.join(run_dir, case_name)
        if not os.path.isdir(case_path):
            continue
        arms: dict = {}
        for arm_name in sorted(os.listdir(case_path)):
            arm_path = os.path.join(case_path, arm_name)
            if not os.path.isdir(arm_path):
                continue
            arms[arm_name] = load_arm_dir(arm_name, arm_path)
        if arms:
            out[case_name] = arms
    return out


def index_manifest(manifest: Mapping[str, Any]) -> dict:
    """``{(symbol, date): [row, ...]}`` from the ground-truth manifest.

    Keyed on (symbol, date) and NOT on account: the ledger carries separate small- and
    main-account rows for the same trade (``t1`` and ``t1-big-acct``,
    scripts/build_ross_manifest.py:203-214) and those dollars must never be summed. When a
    (symbol, date) resolves to more than one row the reporter says so per case instead of
    picking one.
    """
    index: dict = {}
    rows = _first(manifest, _MANIFEST_ROWS_KEYS, []) or []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = (str(row.get("symbol") or "").upper(), str(row.get("date") or ""))
        index.setdefault(key, []).append(dict(row))
    return index


def index_pins(pins: Mapping[str, Any]) -> dict:
    """``{(symbol, date): row}`` from the pin file (STEP 3), keeping the ENTRY leg.

    The pin builder writes one row per leg (``leg`` = "entry" | "exit",
    scripts/rossbench_pin_ross_events.py:763). The grading window is anchored on the ENTRY
    pin, so that is the row whose method and confidence belong in the report; an exit-leg
    row is used only when there is no entry-leg row at all, and never in preference to one.
    """
    index: dict = {}
    rows = _first(pins, _PIN_ROWS_KEYS, []) or []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = (str(row.get("symbol") or "").upper(), str(row.get("date") or ""))
        leg = str(_first(row, _PIN_LEG_KEYS) or "").strip().lower()
        existing = index.get(key)
        if existing is None:
            index[key] = row
            continue
        existing_leg = str(_first(existing, _PIN_LEG_KEYS) or "").strip().lower()
        if leg == PIN_ENTRY_LEG and existing_leg != PIN_ENTRY_LEG:
            index[key] = row
    return index


def case_identity(case_name: str, arms: Mapping[str, ArmRun]) -> tuple[Optional[str], Optional[str], str]:
    """``(symbol, date, date_source)``.

    Symbol comes from the driver receipt's own ``env.SYMBOL``
    (scripts/replay_v3_fsm_window.py:775) — the authoritative record of what actually ran.

    The DATE is trickier and is not derived silently. The ledger's dates are ET trading days;
    the receipt's ``env.WIN_START`` is the driver's naive-UTC window bound. For a regular
    session those agree, but that is an argument, not a proof, so the reporter prefers a
    ``YYYY-MM-DD`` suffix on the case directory (which the runner names from the case id) and
    records ``date_source`` when it had to fall back to the receipt's UTC date.
    """
    symbol = None
    for run in arms.values():
        if run.ok:
            symbol = str(run.env.get("SYMBOL") or "") or None
            if symbol:
                break

    tail = case_name.replace("@", "_").split("_")[-1]
    if len(tail) == 10 and tail[4] == "-" and tail[7] == "-" and tail.replace("-", "").isdigit():
        return (symbol or case_name.rsplit("_", 1)[0]).upper(), tail, "case_dir"

    for run in arms.values():
        if not run.ok:
            continue
        raw = run.env.get("WIN_START")
        if raw:
            return (symbol or case_name).upper(), str(raw)[:10], "receipt_env.WIN_START_utc_date"
    return (symbol or case_name).upper(), None, "unresolved"


BENCH_PLAN_FILE = "bench.json"
BENCH_PLAN_SCHEMA = "chili.ross_replay_bench.v1"


def load_bench_plan(run_dir: str) -> tuple[Optional[dict], list]:
    """The bench runner's own plan document, written at the root of the out-dir.

    Verified against scripts/ross_replay_bench.py:1282-1300 and :1384-1385: it carries
    ``args`` (with ``--source``/``--sink`` redacted, because they hold credentials), the
    resolved ``tree`` (build dir, ref, verified HEAD, sentinel), ``inputs`` (manifest / pins /
    corpus metadata), the ``env_fence``, and ``arms`` with each arm's env ``overrides``.

    The overrides are the load-bearing part for this report: without them the A/B columns say
    an arm moved the number but not WHAT the arm changed, which is not a finding anyone can
    act on. Absent plan -> the report says so; it does not reconstruct the plan from the runs.
    """
    path = os.path.join(run_dir, BENCH_PLAN_FILE)
    if not os.path.exists(path):
        return None, [f"{BENCH_PLAN_FILE} is absent from the run dir — the report cannot show "
                      "what each arm actually overrode, nor the verified build ref"]
    try:
        doc = _read_json(path)
    except (OSError, ValueError) as exc:
        return None, [f"{BENCH_PLAN_FILE} unreadable: {exc}"]
    if not isinstance(doc, Mapping):
        return None, [f"{BENCH_PLAN_FILE} is not a JSON object"]
    warnings: list = []
    if doc.get("schema") != BENCH_PLAN_SCHEMA:
        warnings.append(f"{BENCH_PLAN_FILE} schema is {doc.get('schema')!r}, expected "
                        f"{BENCH_PLAN_SCHEMA!r}")
    return dict(doc), warnings


def load_recorded_events(case_dir: str) -> tuple[list, Optional[str]]:
    """``(events, filename)`` — live-lane events for this case, and which file carried them.

    Case-level, not arm-level: the recorded side is the same whichever CHILI arm ran.
    Written by ``scripts/rossbench_export_recorded_events.py``; ``filename`` is returned so
    the report can distinguish "exported, and the day was genuinely empty" from "nobody ever
    exported anything", which look identical in an empty list.

    When nothing is exported the list is empty and the scorer falls back to the ledger's
    ``xref_verdict``, tagging the resulting Stage ``source="xref_verdict"``
    (``ross_bench_scoring.classify_first_divergence``) so a human judgement is never
    presented as an observed lifecycle. The reporter prints that source, so the fallback is
    visible, not silent — and ``render_recorded_side`` names the verdicts that fallback
    cannot place at all.
    """
    for name in _RECORDED_EVENT_FILES:
        path = os.path.join(case_dir, name)
        if not os.path.exists(path):
            continue
        try:
            if name.endswith(".jsonl"):
                return _read_jsonl(path), name
            doc = _read_json(path)
        except (OSError, ValueError) as exc:
            logger.warning("[rossbench_report] recorded events unreadable %s: %s", path, exc)
            return [], f"{name} (unreadable: {exc})"
        if isinstance(doc, list):
            return [d for d in doc if isinstance(d, Mapping)], name
        if isinstance(doc, Mapping):
            for key in ("events", "recorded_events", "rows"):
                if isinstance(doc.get(key), list):
                    return [d for d in doc[key] if isinstance(d, Mapping)], name
    return [], None


def manifest_ids_from_plan(bench_plan: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """``{case-dir basename: manifest_id}`` from the runner's own plan document.

    THE RUNNER ALREADY KNOWS. ``ross_replay_bench`` accepts ``SYMBOL:DATE:<manifest_id>``
    precisely because 62 of 217 symbol-days carry more than one manifest row, and it stamps
    the resolved ``manifest_id`` on every record in ``bench.json`` (:1663-1668). Reading it
    back here is what stops this report from re-guessing an answer the runner was given.

    Without it the join below falls to ``mrows[0]``: ILLR 2026-06-25 has five rows
    (``ml1`` trade, ``ml2`` null, ``ml3`` trade, ``ml4`` null, ``multiwave-spacex`` trade)
    with different entries, exits and ``ross_net_usd``, so a run of ``::ml3`` was graded
    against ``::ml1`` — a different Ross trade — while printing only a note.

    Keyed by the CASE directory (the parent of each run's ``out_dir``), because that is the
    key ``build_cases`` iterates.
    """
    out: dict[str, str] = {}
    for rec in ((bench_plan or {}).get("runs") or []):
        if not isinstance(rec, Mapping):
            continue
        mid = str(rec.get("manifest_id") or "").strip()
        od = str(rec.get("out_dir") or "").strip()
        if not mid or not od:
            continue
        case_dir = os.path.basename(os.path.dirname(od.replace("\\", "/").rstrip("/")))
        if case_dir:
            out.setdefault(case_dir, mid)
    return out


def _manifest_id_from_case_dir(case_name: str, mrows: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """Recover the selector from a hand-assembled tree's directory name.

    The runner names a disambiguated case directory ``SYMBOL@<slug>_<date>``, where the slug
    is the manifest_id with the symbol and date segments dropped (``Case.selector_slug``).
    Re-joining the slug's parts against each candidate resolves it. Returns None when the
    name carries no selector or when it matches more than one row — an ambiguous match must
    refuse, never pick.
    """
    if "@" not in case_name:
        return None
    slug = case_name.split("@", 1)[1].rsplit("_", 1)[0]
    parts = [p for p in slug.replace("-", "::").split("::") if p]
    if not parts:
        return None
    hits = [
        str(m.get("manifest_id"))
        for m in mrows
        if all(p in str(m.get("manifest_id") or "") for p in parts)
    ]
    return hits[0] if len(hits) == 1 else None


def build_cases(
    runs: Mapping[str, Mapping[str, ArmRun]],
    manifest_index: Mapping[tuple, list],
    pin_index: Mapping[tuple, Mapping[str, Any]],
    *,
    all_arms: Sequence[str],
    run_dir: str = "",
    account_normalizer: Optional[Callable[[Any], str]] = None,
    plan_manifest_ids: Optional[Mapping[str, str]] = None,
) -> tuple[list, dict]:
    """Join run tree x manifest x pins into ``CaseRow``s. Pure; no I/O.

    Returns ``(cases, alias_hits)`` where ``alias_hits`` records which alias actually carried
    each cross-component field, so a naming mismatch between this module and its parallel
    siblings is visible in the report rather than showing up as an empty column.
    """
    cases: list = []
    alias_hits: dict = {}
    normalize = account_normalizer or _fallback_normalize_account

    def note_alias(field_name: str, source: Any, names: Sequence[str]) -> None:
        hit = _first_key(source, names)
        if hit is not None:
            alias_hits.setdefault(field_name, hit)

    for case_name in sorted(runs):
        arms = dict(runs[case_name])
        symbol, date, date_source = case_identity(case_name, arms)
        row = CaseRow(case_id=case_name, symbol=symbol, date=date, arms=arms)
        if date_source != "case_dir":
            row.notes.append(f"date_source={date_source}")
        if run_dir:
            row.recorded_events, row.recorded_events_source = load_recorded_events(
                os.path.join(run_dir, case_name))

        # The runner may stamp its own case id on the run wrapper. A disagreement with the
        # directory name means the tree was assembled by hand or renamed, so the row is
        # flagged rather than quietly trusting whichever name the filesystem happened to
        # carry — a mislabelled case is graded against the wrong symbol-day's ground truth.
        declared = {str(_first(r.receipt, _CASE_ID_KEYS))
                    for r in arms.values() if r.ok and _first(r.receipt, _CASE_ID_KEYS)}
        if declared and declared != {case_name}:
            row.unscorable_reasons.append(
                f"{REASON_CASE_ID_MISMATCH}:dir={case_name} declared={sorted(declared)}"
            )

        # ── ground truth ────────────────────────────────────────────────────────────
        mrows = list(manifest_index.get((str(symbol or "").upper(), str(date or "")), []))
        if not mrows:
            row.unscorable_reasons.append(REASON_NO_MANIFEST_ROW)
        else:
            m = mrows[0]
            if len(mrows) > 1:
                # AMBIGUOUS SYMBOL-DAY. 62 of 217 carry more than one manifest row, with
                # different entries, exits and Ross dollars, so picking one silently grades
                # the run against a trade it did not replay. Resolve it from the runner's own
                # selector; refuse if that is not available.
                want = (plan_manifest_ids or {}).get(case_name) \
                    or _manifest_id_from_case_dir(case_name, mrows)
                picked = next(
                    (x for x in mrows if str(x.get("manifest_id") or "") == str(want or "")),
                    None,
                )
                if picked is not None:
                    m = picked
                    row.notes.append(
                        "manifest has %d rows for this symbol-day; selector resolved to %s"
                        % (len(mrows), want)
                    )
                else:
                    row.unscorable_reasons.append(
                        "%s:%d_candidates=%s" % (
                            REASON_MANIFEST_ROW_AMBIGUOUS, len(mrows),
                            ",".join(str(x.get("manifest_id")) for x in mrows))
                    )
            # The join key into the adapter's ``AdaptedCase.label_id``. Recorded from the
            # SAME row the ground truth above came from, so the graded window and the
            # reported Ross dollars can never come from two different manifest rows.
            row.manifest_id = m.get("manifest_id")
            row.account_raw = m.get("account")
            row.account = normalize(row.account_raw)
            row.expected_action = m.get("expected_action")
            # The 0-sentinel: a ledger 0 is "not stated", never a real flat. Applied to the
            # ROSS number only — CHILI's replay 0.0 is a measurement
            # (ross_bench_scoring.sentinel_zero_to_none, :397-413).
            raw_pnl = _as_float(_first(m, _ROSS_PNL_KEYS))
            row.ross_pnl_usd = None if (raw_pnl is None or raw_pnl == 0.0) else raw_pnl
            row.ross_pnl_confidence = m.get("pnl_confidence")
            row.ross_equity_usd = _as_float(_first(m, _MANIFEST_ROSS_EQUITY_KEYS))
            row.ross_window_et = _first(m, _MANIFEST_ENTRY_ET_KEYS)
            exit_et = _first(m, _MANIFEST_EXIT_ET_KEYS)
            if exit_et:
                row.ross_window_et = f"{row.ross_window_et}–{exit_et}"
            row.xref_verdict = m.get("xref_verdict")
            row.xref_mechanism = _first(m, _MANIFEST_MECHANISM_KEYS)
            row.chili_outcome_usd = _as_float(_first(m, _MANIFEST_RECORDED_PNL_KEYS))
            note_alias("ross_pnl_usd", m, _ROSS_PNL_KEYS)
            note_alias("ross_window_et", m, _MANIFEST_ENTRY_ET_KEYS)
            note_alias("xref_mechanism", m, _MANIFEST_MECHANISM_KEYS)
            note_alias("ross_equity_usd", m, _MANIFEST_ROSS_EQUITY_KEYS)
            if row.ross_pnl_usd is None:
                # Absent P&L leaves Capture/Avoidance; it is not scored as a flat.
                row.unscorable_reasons.append(REASON_ROSS_PNL_ABSENT)

        # ── pins ────────────────────────────────────────────────────────────────────
        pin = pin_index.get((str(symbol or "").upper(), str(date or "")))
        if pin:
            row.pin_method = _first(pin, _PIN_METHOD_KEYS)
            row.pin_confidence = _first(pin, _PIN_CONFIDENCE_KEYS)
            note_alias("pin_method", pin, _PIN_METHOD_KEYS)
            note_alias("pin_confidence", pin, _PIN_CONFIDENCE_KEYS)
            entry_pin = _first(pin, _PIN_ENTRY_KEYS)
            if entry_pin and not row.ross_window_et:
                row.ross_window_et = str(entry_pin)
        conf = str(row.pin_confidence or "").strip().lower()
        if conf == "tape_ambiguous":
            row.unscorable_reasons.append(REASON_PIN_AMBIGUOUS)
        elif conf == "unpinned" or (pin is None and row.ross_window_et is None):
            row.unscorable_reasons.append(REASON_UNPINNED)
        elif conf and conf not in PIN_CONFIDENCE_SCORABLE:
            row.unscorable_reasons.append(f"pin_confidence_{conf}")

        # ── run health ──────────────────────────────────────────────────────────────
        for arm_name in all_arms:
            run = arms.get(arm_name)
            if run is None:
                row.unscorable_reasons.append(f"{REASON_ARM_MISSING}:{arm_name}")
                continue
            if run.receipt is None:
                bad = (REASON_RECEIPT_WRONG_SCHEMA
                       if (run.receipt_error or "").startswith("schema is")
                       else REASON_RECEIPT_UNREADABLE if "unreadable" in (run.receipt_error or "")
                       else REASON_NO_RUN_OUTPUT)
                row.unscorable_reasons.append(f"{bad}:{arm_name}({run.receipt_error})")

        # ── arms must be priced identically or their dollars are not comparable ─────
        sizes = {}
        for arm_name, run in arms.items():
            if run.ok:
                sizes[arm_name] = (run.env.get("EQUITY"), run.env.get("RISK"))
        if len(set(sizes.values())) > 1:
            row.unscorable_reasons.append(
                f"{REASON_EQUITY_MISMATCH}:{sizes}"
            )

        cases.append(row)
    return cases, alias_hits


# ─────────────────────────────────────────────────────────────────────────────
# STAGE CLASSIFICATION + RPI (BOTH DELEGATED TO THE SCORER)
# ─────────────────────────────────────────────────────────────────────────────

def arm_window(run: ArmRun) -> dict:
    """The grading window for one arm's run, in the shape the scorer accepts.

    ``win_start`` / ``win_end`` are read straight off the receipt's env echo
    (scripts/replay_v3_fsm_window.py:776-777) and match the scorer's ``_WINDOW_START_KEYS`` /
    ``_WINDOW_END_KEYS`` (ross_bench_scoring.py:332-333). The reporter does not widen or
    shift the window: a report that grades a wider window than the run executed would credit
    or blame decisions the run never made.
    """
    return {
        "win_start": run.env.get("WIN_START"),
        "win_end": run.env.get("WIN_END"),
    }


def apply_scorer_stages(cases: Sequence[CaseRow], scorer: Any) -> list:
    """Fill ``recorded_stage`` / ``replay_stage[arm]`` via ``classify_first_divergence``.

    The reporter classifies NOTHING itself. STEP 11 owns the stage vocabulary and its rules;
    duplicating any of them here would create a second, drifting definition of the same word.
    The scorer returns ``Stage`` objects (``str`` subclasses carrying ``.source``/``.detail``/
    ``.rung``, ross_bench_scoring.py:114) and they are stored AS RETURNED, so the recorded
    stage keeps its ``source`` — ``events`` when a live session was replayed into the window,
    ``xref_verdict`` when the stage came from the ledger's human judgement instead.

    Every failure is captured per case and returned, never swallowed: a report that quietly
    shows blank stages reads as "no divergence", which is the opposite of the truth.
    """
    problems: list = []
    fn = getattr(scorer, "classify_first_divergence", None)
    if fn is None:
        problems.append(
            _scorer_why(scorer) + "scorer exposes no classify_first_divergence(case, recorded_events, replay_events, "
            "window) — every stage column is reported as unavailable rather than guessed"
        )
        return problems

    for case in cases:
        for arm_name, run in case.arms.items():
            if not run.ok:
                continue
            replay_events = list((run.receipt or {}).get("events") or [])
            # A Tier-1 receipt carries ``seed_session_id``: admission was force-fed by the
            # harness, so the replay ladder must start at runner_never_started. The flag is
            # passed only when the scorer declares the kwarg; a scorer that cannot be told is
            # reported, not worked around (2026-09-04: SDOT's 7-fill run graded
            # ``no_arm_attempt`` because the seeded session never emits arm events).
            supplied = bool((run.receipt or {}).get("seed_session_id"))
            kwargs = {}
            if supplied:
                try:
                    accepts = "replay_admission_supplied" in inspect.signature(fn).parameters
                except (TypeError, ValueError):
                    accepts = False
                if accepts:
                    kwargs["replay_admission_supplied"] = True
                else:
                    problems.append(
                        f"{case.case_id}/{arm_name}: receipt is harness-seeded but the scorer "
                        "does not accept replay_admission_supplied — its arm rungs are fixtures"
                    )
            try:
                verdict = fn(case.scorer_payload(arm_name), case.recorded_events,
                             replay_events, arm_window(run), **kwargs)
            except Exception as exc:  # noqa: BLE001 — the message IS the deliverable
                problems.append(f"{case.case_id}/{arm_name}: classify_first_divergence raised {exc!r}")
                continue
            recorded, replay = _unpack_stages(verdict)
            if recorded is not None and case.recorded_stage is None:
                # The recorded side does not depend on which CHILI arm ran, so the first
                # arm's answer is kept and the rest are checked against it rather than
                # silently overwriting — a disagreement would mean the window moved.
                case.recorded_stage = recorded
                case.recorded_stage_source = getattr(recorded, "source", None) or "unknown"
            elif recorded is not None and str(recorded) != str(case.recorded_stage):
                problems.append(
                    f"{case.case_id}: recorded stage differs by arm "
                    f"({case.recorded_stage!s} vs {recorded!s}) — the arms graded different "
                    "windows, so their recorded sides are not one observation"
                )
            case.replay_stage[arm_name] = replay
    return problems


def _unpack_stages(verdict: Any) -> tuple[Any, Any]:
    """Unpack the documented 2-tuple ``(recorded_stage, replay_stage)``, or a mapping.

    Values are returned AS RECEIVED (not str()-ed) so a ``Stage``'s ``.source``/``.base``
    survive into the report and into the scorer's own ``recorded_stage`` input. Anything
    that is neither shape returns ``(None, None)``, so the column reads as unavailable
    rather than rendering a repr.
    """
    if isinstance(verdict, Mapping):
        return verdict.get("recorded_stage"), verdict.get("replay_stage")
    if isinstance(verdict, (tuple, list)) and len(verdict) == 2:
        return verdict[0], verdict[1]
    return None, None


def parse_ross_equity(spec: Optional[str]) -> dict:
    """``--ross-equity main=60000,small=2000`` -> ``{"main": 60000.0, "small": 2000.0}``.

    An EMPTY spec yields ``{}``, which the scorer reads as "unknown" and answers with
    ``capture[...].equity_normalized = null`` (ross_bench_scoring.py:1008-1018). That is the
    intended default: the reporter has no way to know Ross's account size, and the one thing
    it must never do is reach for the driver's ``env.EQUITY`` — see
    ``ROSS_EQUITY_IS_NOT_SIM_EQUITY``. The buckets are the scorer's own account vocabulary
    (``main`` / ``small`` / ``mixed`` / ``unknown``).
    """
    out: dict = {}
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise SystemExit(
                f"[rossbench_report] --ross-equity expects account=USD pairs, got {chunk!r}"
            )
        account, _, raw = chunk.partition("=")
        value = _as_float(raw)
        if value is None or value <= 0:
            raise SystemExit(
                f"[rossbench_report] --ross-equity {chunk!r}: {raw!r} is not a positive number"
            )
        out[account.strip().lower()] = value
    return out


def compute_rpi(
    cases: Sequence[CaseRow],
    scorer: Any,
    *,
    arms: Sequence[str],
    ross_equity: Mapping[str, float],
) -> tuple[dict, list]:
    """``{arm: {"index": <ross_parity_index result>, ...}}`` plus any problems.

    The RPI numbers are the scorer's, not the reporter's — the reporter computes no metric of
    its own. One call PER ARM, over case payloads carrying that arm's receipt, because a
    single call could only ever describe one replay.

    ``ross_equity`` is the operator-supplied per-account map for the Capture %-of-equity
    denominator; ``{}`` means "unknown" and the scorer answers ``equity_normalized: null``.
    It is deliberately NOT derived from the receipts.
    """
    problems: list = []
    fn = getattr(scorer, "ross_parity_index", None)
    if fn is None:
        problems.append(
            _scorer_why(scorer) + "scorer exposes no ross_parity_index(cases, equity) — rpi is reported as "
            "unavailable; the reporter does not compute parity metrics of its own"
        )
        return {}, problems

    out: dict = {}
    for arm_name in arms:
        payload = [c.scorer_payload(arm_name) for c in cases]
        try:
            index = fn(payload, dict(ross_equity))
        except TypeError as exc:
            problems.append(
                f"arm {arm_name!r}: ross_parity_index rejected the reporter's arguments "
                f"({exc!r}). The reporter passes case dicts with keys "
                f"{list(CASE_FOR_SCORER_KEYS)} and equity as a Mapping; reconcile this "
                "contract with STEP 11 rather than reshaping it here"
            )
            continue
        except Exception as exc:  # noqa: BLE001
            problems.append(f"arm {arm_name!r}: ross_parity_index raised {exc!r}")
            continue
        out[arm_name] = {
            "index": index,
            "cases_submitted": len(payload),
            "cases_with_receipt": sum(1 for p in payload if "replay" in p),
            "ross_equity_supplied": dict(ross_equity),
        }
    return out, problems


# ─────────────────────────────────────────────────────────────────────────────
# EVIDENCE GRADE (DELEGATED TO THE ADAPTER + THE GRADER)
# ─────────────────────────────────────────────────────────────────────────────
# This is the wiring the STEP-4 adapter and the STEP-5 diagnostic tier were built for and
# which nothing previously called. The reporter's job here is join and hand-off ONLY: it
# decides no window, no coverage rule and no tier.
#
#   ross_manifest_adapter.phase_windows_from_manifest(manifest, pins)
#       -> AdaptedCase per manifest window, ``.window`` = ValidatedPhaseWindow | None
#   ross_replay_benchmark.diagnostic_coverage_from_replay_receipt(receipt, label_id=...)
#       -> DiagnosticReplayCoverage, built from the receipt's own env/tape/tree blocks
#   ross_replay_benchmark.grade_recap_phase_window(...)
#       -> PhaseBenchmarkGrade, whose ``.evidence_grade`` IS the document's stamp

DEFAULT_ADAPTER_MODULE = "app.services.trading.momentum_neural.ross_manifest_adapter"
DEFAULT_GRADER_MODULE = "app.services.trading.momentum_neural.ross_replay_benchmark"

# The driver's window clocks are NAIVE — WIN_START/WIN_END/OHLCV_START come from a bare
# ``datetime.fromisoformat`` over an env string (scripts/replay_v3_fsm_window.py:154-156)
# and FRAME_START is derived from them — so the grader refuses to build a coverage record
# without being told which zone they are in (``_receipt_clock``: "this function will NOT
# assume it").
#
# UTC is the DERIVED answer for a run this bench produced, not a convenience: the bench
# runner converts every window bound to UTC-naive before handing it to the driver.
# ``ross_replay_bench.et_clock_to_utc`` returns
# ``aware.astimezone(timezone.utc).replace(tzinfo=None)`` and ``_parse_utc_field`` does the
# same for a row that already states one. (Cited by SYMBOL, not line: that file is under
# active edit and its line numbers have already moved once.) It is still a CLI knob, because
# a receipt produced by hand-running the driver carries whatever the operator's env said,
# and the answer is then theirs to state.
DEFAULT_RECEIPT_CLOCK_TZ = "utc"


def parse_clock_tz(name: Optional[str]):
    """``"utc"`` or an IANA zone name -> tzinfo. Never guesses; raises on an unknown name."""
    text = (name or DEFAULT_RECEIPT_CLOCK_TZ).strip()
    if text.lower() in ("utc", "z", "+00:00"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        return ZoneInfo(text)
    except Exception as exc:  # noqa: BLE001 — the message IS the deliverable
        raise SystemExit(
            f"[rossbench_report] --receipt-clock-tz {text!r} is not a zone this system "
            f"knows ({exc!r}). Pass 'utc' or an IANA name."
        )


def resolve_module(module_name: str, *, what: str) -> tuple[Any, Optional[str]]:
    """``(module, problem)``. An unavailable module is REPORTED, never silently skipped."""
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    try:
        return importlib.import_module(module_name), None
    except Exception as exc:  # noqa: BLE001
        problem = (
            f"{what} module {module_name!r} unavailable ({exc!r}); the evidence grade is "
            "reported as unscorable because nothing checked it — it is NOT assumed"
        )
        logger.warning("[rossbench_report] %s", problem)
        return None, problem


def adapt_manifest(manifest: Any, pins: Any, adapter: Any) -> tuple[list, dict, Optional[str]]:
    """``(adapted_cases, adaptation_summary, problem)`` from the STEP-4 adapter.

    The adapter owns every rule about what makes a grading window usable — pin confidence,
    pin method, end-boundary basis, timezone-awareness. Re-deriving any of that here would
    create a second definition of "scorable" that could disagree with the one the grader
    actually enforces.
    """
    if adapter is None:
        return [], {}, None  # the caller already recorded why
    fn = getattr(adapter, "phase_windows_from_manifest", None)
    if fn is None:
        return [], {}, (
            "adapter exposes no phase_windows_from_manifest(manifest, pins) — no grading "
            "window can be built and every case is reported unscorable"
        )
    try:
        cases = list(fn(manifest, pins))
    except Exception as exc:  # noqa: BLE001
        return [], {}, f"phase_windows_from_manifest raised {exc!r}"
    summary_fn = getattr(adapter, "adaptation_summary", None)
    summary: dict = {}
    if summary_fn is not None:
        try:
            summary = dict(summary_fn(cases))
        except Exception as exc:  # noqa: BLE001
            return cases, {}, f"adaptation_summary raised {exc!r}"
    return cases, summary, None


def attach_phase_windows(cases: Sequence[CaseRow], adapted: Sequence[Any]) -> list:
    """Join each ``CaseRow`` to its ``AdaptedCase`` by ``manifest_id`` == ``label_id``.

    An EXACT id join, never a symbol-day one: the manifest's 4th layer emits one window per
    ledger leg, so a symbol-day carries several windows with different clocks, and picking
    one by symbol-day would grade a run against another leg's window. When the matched row
    is not scorable but a SIBLING window for the same symbol-day is, that is recorded as a
    note — it is the runner's row-selection ambiguity surfacing, and it is actionable — but
    the reporter still grades the row it reported, and does not go shopping for a window
    that produces a better grade.
    """
    problems: list = []
    by_label: dict = {}
    by_symbol_day: dict = {}
    for ac in adapted:
        label = getattr(ac, "label_id", None)
        if label:
            by_label.setdefault(str(label), []).append(ac)
        key = (str(getattr(ac, "symbol", "") or "").upper(),
               str(getattr(ac, "trade_date", "") or ""))
        by_symbol_day.setdefault(key, []).append(ac)

    for case in cases:
        if not case.manifest_id:
            if adapted:
                problems.append(
                    f"{case.case_id}: no manifest_id on the ground-truth row, so it cannot "
                    "be joined to a grading window"
                )
            continue
        matches = by_label.get(str(case.manifest_id), [])
        if len(matches) > 1:
            problems.append(
                f"{case.case_id}: manifest_id {case.manifest_id!r} resolves to "
                f"{len(matches)} adapted cases — the manifest_id is not unique, which it is "
                "by construction supposed to be"
            )
            continue
        if not matches:
            problems.append(
                f"{case.case_id}: manifest_id {case.manifest_id!r} has no adapted case"
            )
            continue
        ac = matches[0]
        case.phase_window = getattr(ac, "window", None)
        case.phase_window_reasons = list(getattr(ac, "unscorable_reasons", ()) or ())
        siblings = by_symbol_day.get(
            (str(case.symbol or "").upper(), str(case.date or "")), [])
        scorable_siblings = [s for s in siblings
                             if getattr(s, "scorable", False) and s is not ac]
        if case.phase_window is None and scorable_siblings:
            case.notes.append(
                "this manifest row has no grading window, but %d other window(s) for the "
                "same symbol-day do (%s) — the run was executed against ONE of them and the "
                "reporter grades the row it reported, not the one that would grade best" % (
                    len(scorable_siblings),
                    ", ".join(str(getattr(s, "label_id", "?")) for s in scorable_siblings),
                )
            )
    return problems


def replay_trade_observations(receipt: Mapping[str, Any], grader: Any, *,
                              naive_clock_tz) -> tuple[list, Optional[str]]:
    """``(observations, note)`` — the receipt's fills as ONE grader trade observation.

    The reporter does NOT reconstruct round-trips by pairing buys to sells. That pairing is
    the driver's own arithmetic (it is what produces ``receipt["pnl_usd"]``, computed from
    the mined fills at scripts/replay_v3_fsm_window.py:1174), and re-deriving it here would
    create a second, drifting definition of "a trade" — the exact failure this bench exists
    to catch elsewhere.

    So: one observation spanning the FIRST fill to the LAST, carrying the receipt's own
    ``pnl_usd``. The grader uses ``entry_ts`` to decide whether the trade falls inside the
    labelled phase window and sums ``pnl_usd`` over the matches; with one observation that
    sum IS the driver's number. A receipt with several round-trips therefore collapses to
    its earliest entry, which is the conservative reading of "did CHILI act inside the
    window" — and the note says so on every such row.

    ``naive_clock_tz`` is applied to naive fill clocks for the same reason the coverage
    record needs it: the grader refuses a naive datetime rather than assuming a zone.
    """
    cls = getattr(grader, "ReplayTradeObservation", None)
    if cls is None:
        return [], "grader exposes no ReplayTradeObservation"
    fills = [f for f in (receipt.get("fills") or []) if isinstance(f, Mapping)]
    stamps: list[datetime] = []
    unparsed = 0
    for f in fills:
        parsed = _parse_receipt_clock(f.get("ts"), naive_clock_tz)
        if parsed is None:
            unparsed += 1
            continue
        stamps.append(parsed)
    if not stamps:
        note = (f"receipt carries {len(fills)} fill(s), none with a parseable ts"
                if fills else None)
        return [], note
    stamps.sort()
    symbol = str(((receipt.get("env") or {}).get("SYMBOL")) or "").strip().upper()
    obs = cls(symbol=symbol, entry_ts=stamps[0], exit_ts=stamps[-1],
              pnl_usd=_as_float(receipt.get("pnl_usd")))
    note = f"{len(fills)} fill(s) folded into one observation (first fill -> last fill)"
    if unparsed:
        note += f"; {unparsed} fill(s) had no parseable ts and were dropped from the span"
    return ([obs] if obs.valid() else []), note


def _parse_receipt_clock(raw: Any, naive_clock_tz) -> Optional[datetime]:
    """Parse one receipt clock, stamping ``naive_clock_tz`` on a naive value."""
    if isinstance(raw, datetime):
        parsed = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=naive_clock_tz)


GRADEABLE_EXPECTED_ACTIONS = ("trade", "reject")


def grade_evidence(
    cases: Sequence[CaseRow],
    grader: Any,
    *,
    naive_clock_tz,
    allow_dirty_tree: bool,
) -> list:
    """Fill ``phase_grade`` / ``evidence_grade_by_arm`` per (case, arm). Returns problems.

    Every failure path here ends in a row whose evidence grade is ``unscorable`` WITH a
    reason attached — never a row that quietly keeps a tier it did not earn.
    """
    problems: list = []
    cov_fn = getattr(grader, "diagnostic_coverage_from_replay_receipt", None)
    grade_fn = getattr(grader, "grade_recap_phase_window", None)
    if grade_fn is None or cov_fn is None:
        return [
            "grader exposes no grade_recap_phase_window/diagnostic_coverage_from_replay_"
            "receipt — the evidence grade is reported as unscorable because nothing "
            "checked it"
        ]

    for case in cases:
        action = str(case.expected_action or "").strip().lower()
        for arm_name, run in case.arms.items():
            if not run.ok:
                continue
            if action not in GRADEABLE_EXPECTED_ACTIONS:
                # The grader's ExpectedAction literal is exactly {"trade","reject"} and
                # ``grade_recap_decision`` RAISES on anything else, so this is a refusal,
                # not a fallback. It matches the adapter's own REASON_EXPECTED_ACTION_
                # UNDEFINED, which will already be in phase_window_reasons for this row.
                case.evidence_grade_by_arm[arm_name] = "unscorable"
                case.coverage_problems.append(
                    f"{arm_name}: expected_action={case.expected_action!r} is outside "
                    f"{list(GRADEABLE_EXPECTED_ACTIONS)}; not graded"
                )
                continue
            label_id = str(case.manifest_id or "")
            if not label_id:
                case.evidence_grade_by_arm[arm_name] = "unscorable"
                case.coverage_problems.append(f"{arm_name}: no manifest_id to grade against")
                continue

            coverage = None
            try:
                coverage = cov_fn(run.receipt, label_id=label_id,
                                  naive_clock_tz=naive_clock_tz,
                                  allow_dirty_tree=allow_dirty_tree)
            except Exception as exc:  # noqa: BLE001 — ValueError is the documented refusal
                case.coverage_problems.append(f"{arm_name}: coverage refused — {exc}")

            trades, trade_note = replay_trade_observations(
                run.receipt, grader, naive_clock_tz=naive_clock_tz)
            if trade_note:
                case.notes.append(f"{arm_name}: {trade_note}")

            try:
                grade = grade_fn(
                    label_id=label_id,
                    symbol=str(case.symbol or ""),
                    expected_action=action,
                    trades=trades,
                    phase_window=case.phase_window,
                    # Deliberately absent: see SEALED_COVERAGE_NOT_SUPPLIED. Passing a
                    # sealed record we cannot substantiate would launder a diagnostic run
                    # into a certified one.
                    replay_coverage=None,
                    diagnostic_coverage=coverage,
                )
            except Exception as exc:  # noqa: BLE001
                problems.append(
                    f"{case.case_id}/{arm_name}: grade_recap_phase_window raised {exc!r}")
                case.evidence_grade_by_arm[arm_name] = "unscorable"
                continue
            case.phase_grade[arm_name] = grade
            case.evidence_grade_by_arm[arm_name] = str(
                getattr(grade, "evidence_grade", "unscorable"))
    return problems


def derive_evidence_grade(cases: Sequence[CaseRow], *, grader_problem: Optional[str] = None,
                          adapter_problem: Optional[str] = None) -> dict:
    """The run-level stamp, DERIVED from the per-row grades the grader returned.

    Rule, stated once and applied without exception: the run's grade is the BEST tier any
    (case, arm) row actually earned, by ``EVIDENCE_GRADE_PRECEDENCE``; when no row was
    graded at all the answer is ``unscorable``. The per-tier counts travel with the stamp so
    "one row earned diagnostic_only out of forty" cannot read as "this run is diagnostic".

    Why best-of rather than worst-of: most rows in this corpus are unscorable for pin
    reasons that say nothing about the tape a graded row ran on, so a worst-of rule would
    make the stamp a function of the corpus's coverage rather than of the evidence behind
    the numbers a reader is looking at. Best-of + counts keeps the stamp about the strongest
    claim in the document and shows how thin it is.
    """
    counts: dict = {}
    for case in cases:
        for arm_name, tier in case.evidence_grade_by_arm.items():
            key = str(tier or "unscorable")
            counts[key] = counts.get(key, 0) + 1
    graded_rows = sum(counts.values())
    best = EVIDENCE_GRADE_WHEN_NOTHING_GRADED
    for tier in EVIDENCE_GRADE_PRECEDENCE:
        if counts.get(tier):
            best = tier
    reasons: list = []
    if grader_problem:
        reasons.append(grader_problem)
    if adapter_problem:
        reasons.append(adapter_problem)
    if not graded_rows and not reasons:
        reasons.append("no (case, arm) row reached the grader")
    if best == "diagnostic_only":
        reasons.append(SEALED_COVERAGE_NOT_SUPPLIED)
    return {
        "grader_enum": best,
        "stamp": EVIDENCE_GRADE_STAMPS.get(best, best.upper()),
        "graded_rows": graded_rows,
        "counts": dict(sorted(counts.items())),
        "precedence": list(EVIDENCE_GRADE_PRECEDENCE),
        "rule": ("best tier any (case, arm) row earned from "
                 "ross_replay_benchmark.grade_recap_phase_window; unscorable when nothing "
                 "was graded"),
        "sealed_coverage_supplied": False,
        "reasons": reasons,
    }


def coverage_reason_counts(cases: Sequence[CaseRow]) -> dict:
    """``{reason: n}`` over every graded row's ``coverage_reasons``.

    On a FAILING row these are the disqualifying reasons; on a PASSING diagnostic row they
    are the advisory caveats the tier carries alongside its credit
    (``grade_recap_phase_window``'s coverage-precedence note). Counted together on purpose:
    a reader needs to see ``diagnostic:sealed_decision_coverage_not_bound`` on every graded
    row, because that caveat is the whole reason the tier is called diagnostic.
    """
    counts: dict = {}
    for case in cases:
        for grade in case.phase_grade.values():
            for reason in getattr(grade, "coverage_reasons", ()) or ():
                counts[str(reason)] = counts.get(str(reason), 0) + 1
    return dict(sorted(counts.items()))


def recorded_side_limitation(cases: Sequence[CaseRow], scorer: Any) -> dict:
    """What the recorded side of this run actually rests on, per case, counted.

    This block exists because the recorded column used to print a bare ``unknown`` for the
    corpus's second-most-common verdict and say nothing about why. The vocabulary and the
    refusal texts are the SCORER's (``XREF_VERDICT_PLACEMENT``); the reporter counts rows
    against them and prints them. It defines no verdict of its own.
    """
    placement = dict(getattr(scorer, "XREF_VERDICT_PLACEMENT", {}) or {})
    by_source: dict = {}
    unplaceable: dict = {}
    exported = 0
    for case in cases:
        source = str(case.recorded_stage_source or "none")
        by_source[source] = by_source.get(source, 0) + 1
        if case.recorded_events_source:
            exported += 1
        detail = getattr(case.recorded_stage, "detail", {}) or {}
        reason = detail.get("unplaceable_reason")
        if reason and str(getattr(case.recorded_stage, "source", "")) == "xref_verdict":
            token = str(detail.get("xref_verdict") or case.xref_verdict or "unknown")
            entry = unplaceable.setdefault(
                token, {"count": 0, "cases": [], "reason": str(reason),
                        "rung_bounds": detail.get("rung_bounds")})
            entry["count"] += 1
            entry["cases"].append(case.case_id)
    return {
        "cases": len(cases),
        "cases_with_exported_events": exported,
        "exporter": RECORDED_EVENTS_EXPORTER,
        "recorded_stage_sources": dict(sorted(by_source.items())),
        "verdicts_placeable": list(getattr(scorer, "XREF_VERDICTS_PLACEABLE", ()) or ()),
        "verdicts_unplaceable": list(getattr(scorer, "XREF_VERDICTS_UNPLACEABLE", ()) or ()),
        "unplaceable_in_this_run": unplaceable,
        "placement_table_available": bool(placement),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PROVENANCE
# ─────────────────────────────────────────────────────────────────────────────

def nbbo_vendor(tape_sources: Any) -> Optional[str]:
    """Derived, exactly as the grader documents it.

    The receipt has NO scalar ``nbbo_vendor`` field — verified at the emission site
    (scripts/replay_v3_fsm_window.py:1140-1183). It is the single key of
    ``tape_sources["momentum_nbbo_spread_tape"]``, which is also how the grader defines it
    (app/services/trading/momentum_neural/ross_replay_benchmark.py:336-338). More than one
    key means the mirror concatenated two vendors' tapes and the vendor is reported as
    ambiguous rather than as whichever key happened to sort first.
    """
    table = (tape_sources or {}).get("momentum_nbbo_spread_tape")
    if not isinstance(table, Mapping):
        return None
    present = [k for k, n in table.items() if n]
    if len(present) == 1:
        return str(present[0])
    if not present:
        return None
    return "AMBIGUOUS:" + ",".join(sorted(str(p) for p in present))


def collect_knobs(
    cases: Sequence[CaseRow],
    *,
    supplied_derivations: Optional[Mapping[str, Any]] = None,
) -> tuple[list, int]:
    """Every knob that shaped the run, with its derivation string — or ``UNATTRIBUTED``.

    The reporter deliberately ships NO derivation table of its own. A derivation must arrive
    as DATA from whoever chose the knob: a ``knobs`` block on the run wrapper, or an operator
    file passed to ``--knob-derivations``. A table maintained inside this module would drift
    from the runner's real defaults and would let the report state a provenance it never
    received — which is worse than a blank, because it reads as verified.

    As of this writing the bench runner writes the driver receipt VERBATIM as ``run.json``
    (scripts/ross_replay_bench.py:42, :1315) and its ``bench.json`` records ``args`` without
    derivation strings (:1282-1300), so an unmodified pipeline reports every knob as
    ``UNATTRIBUTED``. That is the honest state of the pipeline, not a reporter defect;
    ``--knob-derivations`` is the seam for fixing it without hardcoding prose here.
    """
    values: dict = {}
    derivations: dict = {}
    for key, spec in (supplied_derivations or {}).items():
        text = spec.get("derivation") if isinstance(spec, Mapping) else spec
        if text:
            derivations[str(key)] = str(text)
    for case in cases:
        for run in case.arms.values():
            if not run.ok:
                continue
            for key, value in (run.receipt.get("env") or {}).items():
                values.setdefault(key, set()).add(json.dumps(value, sort_keys=True, default=str))
            for block_key in _KNOB_BLOCK_KEYS:
                block = run.receipt.get(block_key)
                if not isinstance(block, Mapping):
                    continue
                for key, spec in block.items():
                    if isinstance(spec, Mapping):
                        d = spec.get("derivation") or spec.get("why")
                    else:
                        d = spec
                    if d:
                        derivations.setdefault(str(key), str(d))

    rows: list = []
    unattributed = 0
    for key in sorted(values):
        seen = sorted(values[key])
        value = seen[0] if len(seen) == 1 else "VARIES ACROSS ARMS: " + " | ".join(seen)
        derivation = derivations.get(key)
        if not derivation:
            unattributed += 1
            derivation = "UNATTRIBUTED"
        rows.append({"knob": key, "value": value, "derivation": derivation})
    return rows, unattributed


def collect_provenance(
    cases: Sequence[CaseRow],
    *,
    arms: Sequence[str],
    bench_plan: Optional[Mapping[str, Any]] = None,
    knob_derivations: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Tree sha, sink, tape sources, nbbo vendor, depth rows, stride, grid, equity/risk, mock.

    Everything here is read back from the receipts of the runs that actually happened. Where
    runs disagree the disagreement is printed — an A/B whose arms ran on different trees, sinks
    or fill models is not an A/B, and collapsing that to one value would hide it.
    """
    trees: dict = {}
    sinks: dict = {}
    tape: dict = {}
    mocks: dict = {}
    depth_by_run: dict = {}
    density_rows: list = []

    for case in cases:
        for arm_name, run in case.arms.items():
            if not run.ok:
                continue
            r = run.receipt
            key = f"{case.case_id}/{arm_name}"
            tree = r.get("tree") or {}
            trees.setdefault(
                json.dumps({"head": tree.get("head"), "tree": tree.get("tree"),
                            "branch": tree.get("branch"), "dirty": tree.get("dirty")},
                           sort_keys=True), []).append(key)
            sink = r.get("sink_reset")
            sink_db = (sink or {}).get("database") if isinstance(sink, Mapping) else None
            sinks.setdefault(json.dumps(
                {"database": sink_db,
                 "tables_cleaned": len((sink or {}).get("cleaned") or []) if isinstance(sink, Mapping) else None,
                 "guards_suspended": len((sink or {}).get("suspended") or []) if isinstance(sink, Mapping) else None,
                 "reset_performed": sink is not None},
                sort_keys=True), []).append(key)
            tape.setdefault(json.dumps(r.get("tape_sources") or {}, sort_keys=True), []).append(key)
            mocks.setdefault(json.dumps(r.get("mock") or {}, sort_keys=True, default=str), []).append(key)
            depth_by_run[key] = int((r.get("mirrored") or {}).get("depth_rows") or 0)
            dens = r.get("density") or {}
            density_rows.append({
                "run": key,
                "tick_rows": (r.get("mirrored") or {}).get("tick_rows"),
                "nbbo_rows": (r.get("mirrored") or {}).get("nbbo_rows"),
                "depth_rows": (r.get("mirrored") or {}).get("depth_rows"),
                "ticks_per_second": dens.get("ticks_per_second"),
                "nbbo_rows_per_second": dens.get("nbbo_rows_per_second"),
                "grid_steps": r.get("grid_steps"),
                "nbbo_vendor": nbbo_vendor(r.get("tape_sources")),
                "execution_family": r.get("execution_family"),
                "venue": r.get("venue"),
                "certification_failures": list(r.get("certification_failures") or []),
            })

    knobs, unattributed = collect_knobs(cases, supplied_derivations=knob_derivations)
    depth_unmeasurable, depth_text = limitation_depth(depth_by_run)

    def _fold(bucket: dict) -> list:
        return [{"value": json.loads(k), "runs": v} for k, v in sorted(bucket.items())]

    plan = dict(bench_plan or {})
    return {
        "arms": list(arms),
        "bench_plan": {
            "present": bool(bench_plan),
            "schema": plan.get("schema"),
            "generated_at_utc": plan.get("generated_at_utc"),
            # ``args`` already has --source/--sink redacted by the runner
            # (ross_replay_bench.py:1288); it is passed through, not re-derived.
            "args": plan.get("args"),
            "tree": plan.get("tree"),
            "inputs": plan.get("inputs"),
            "env_fence": plan.get("env_fence"),
            "arm_overrides": {str(a.get("name")): (a.get("overrides") or {})
                              for a in (plan.get("arms") or [])
                              if isinstance(a, Mapping)},
        },
        "tree": _fold(trees),
        "sink": _fold(sinks),
        "tape_sources": _fold(tape),
        "mock": _fold(mocks),
        "per_run": density_rows,
        "depth_rows_by_run": depth_by_run,
        "depth_levers_unmeasurable": depth_unmeasurable,
        "depth_limitation_text": depth_text,
        "leader_board_mode": LEADER_BOARD_MODE,
        "knobs": knobs,
        "knobs_unattributed": unattributed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# RENDERING
# ─────────────────────────────────────────────────────────────────────────────

def render_header(doc: Mapping[str, Any]) -> str:
    ev = dict(doc.get("evidence_grade_derivation") or {})
    counts = ev.get("counts") or {}
    counts_text = ", ".join(f"`{k}` × {v}" for k, v in counts.items()) or "no row graded"
    lines = [
        "# Ross Parity Bench — diagnostic report",
        "",
        f"- `evidence_grade`: **{doc['evidence_grade']}** "
        f"(grader enum: `{ev.get('grader_enum')}`) — DERIVED, not declared: "
        f"{counts_text} over {ev.get('graded_rows', 0)} graded (case, arm) row(s)",
        f"- `causal_use_allowed`: **{str(doc['causal_use_allowed']).lower()}**",
        f"- `admission_claim`: **{str(doc['admission_claim']).lower()}**",
        "",
        "> The Tier-1 harness **supplies** admission: it seeds a `queued_live` session and hands "
        "the runner `risk_gate_allows=True`, so the scanner and risk-gate decisions that would "
        "decide whether CHILI ever saw this name are fixtures, not measurements. **No number "
        "below may be read as an admission or admission-latency claim**, and none of it "
        "establishes cause. Stages above `runner_never_started` come from the RECORDED side "
        "only and carry their source per row.",
        "",
        f"- run dir: `{doc.get('run_dir')}`",
        f"- generated: `{doc.get('generated_at_utc')}`",
        f"- arms: {', '.join('`%s`' % a for a in doc.get('arms') or []) or '—'} "
        f"(base = `{doc.get('base_arm')}`)",
        "",
    ]
    return "\n".join(lines)


def render_limitations(provenance: Mapping[str, Any]) -> str:
    return "\n".join([
        "## Limitations that apply to every line below",
        "",
        f"1. {LIMITATION_LEADER_BOARD}",
        "",
        f"2. {provenance.get('depth_limitation_text')}",
        "",
    ])


def render_case_table(cases: Sequence[CaseRow], *, base_arm: str, arm: str) -> str:
    """The 2026-07-16 scorecard format, extended.

    ``Δ`` is defined once, here: **Δ = CHILI $ arm − CHILI $ base**, emitted only when both
    arms produced a receipt. It is an A/B delta between two CHILI runs, NOT a gap to Ross.
    """
    head = (
        f"| symbol | date | acct | Ross window ET (pin) | Ross $ | CHILI $ `{base_arm}` | "
        f"CHILI $ `{arm}` | Δ | recorded stage | replay stage `{base_arm}` | "
        f"replay stage `{arm}` | code_ref |"
    )
    sep = "|" + "---|" * 12
    lines = [head, sep]
    for case in cases:
        pin = "/".join(x for x in (case.pin_method, case.pin_confidence) if x) or "no pin"
        window = f"{case.ross_window_et or '—'} ({pin})"
        base_ref = (case.arms.get(base_arm).code_ref if case.arms.get(base_arm) else None)
        arm_ref = (case.arms.get(arm).code_ref if case.arms.get(arm) else None)
        if base_ref == arm_ref:
            code_ref = base_ref
        else:
            code_ref = f"{base_arm}={base_ref or '—'} {arm}={arm_ref or '—'}"
        lines.append("| " + " | ".join(_md_cell(v) for v in (
            case.symbol,
            case.date,
            case.account,
            window,
            _fmt_usd(case.ross_pnl_usd),
            _fmt_usd(case.pnl(base_arm)),
            _fmt_usd(case.pnl(arm)),
            _fmt_usd(case.delta(base_arm, arm)),
            _stage_cell(case.recorded_stage),
            _stage_cell(case.replay_stage.get(base_arm)),
            _stage_cell(case.replay_stage.get(arm)),
            code_ref,
        )) + " |")
    lines += [
        "",
        f"`Δ` = CHILI $ `{arm}` − CHILI $ `{base_arm}` (an A/B delta between two CHILI runs, not a "
        "gap to Ross); blank when either arm has no receipt. A `—` in **Ross $** means the ledger "
        "carries NO P&L for that row — the ledger writes `0` as a null sentinel and it is never "
        "read here as a real zero.",
        "",
        "Every stage cell carries its SOURCE in brackets. `[events]` means a lifecycle actually "
        "observed in that side's event stream. `[xref_verdict]` means the recorded side had no "
        "session in the window and the stage is the ledger's HUMAN judgement, not a measurement. "
        "`[no_replay_events]` means the replay emitted nothing inside the window. Replay stages "
        "come from the Tier-1 harness, which was force-armed — see the admission note above.",
        "",
    ]
    return "\n".join(lines)


def _stage_cell(stage: Any) -> str:
    """``stage [source]`` — the source is never dropped.

    A stage and the evidence class it rests on are one fact. Printing ``not_alive`` without
    ``[xref_verdict]`` would let a hand-written ledger verdict read as an observed lifecycle,
    which is the specific confusion the scorer's ``Stage.source`` exists to prevent.
    """
    if stage is None:
        return "unavailable"
    source = getattr(stage, "source", None)
    return f"{stage} [{source}]" if source else str(stage)


# Metrics whose scorer block is the flat {numerator, denominator, ratio, rule} shape.
# ``capture`` is NOT one of them: it is bucketed by account and denominated in dollars,
# because pooling main and small equity bases would make the ratio a rate of nothing
# (ross_bench_scoring.py:1030-1035). ``liveness`` is null by construction in Tier 1.
_FLAT_RPI_METRICS = ("avoidance", "precision", "recorded_liveness")


def _case_ids(rows: Any) -> str:
    if not rows:
        return "—"
    out = []
    for r in rows:
        out.append(str(r.get("case_id")) if isinstance(r, Mapping) else str(r))
    return ", ".join(out)


def render_rpi(rpi_by_arm: Mapping[str, Any], problems: Sequence[str]) -> str:
    """Render the scorer's index. Every rate is printed WITH its numerator and denominator.

    A bare rate hides its own sample size: "Capture 0.33" over three cases and over three
    hundred are different claims, and this bench routinely runs on a handful of symbol-days.
    """
    lines = ["## Ross Parity Index", ""]
    if not rpi_by_arm:
        lines += [
            "**Unavailable.** The reporter computes no parity metric of its own; the RPI "
            "numbers come from the scorer (STEP 11, "
            "`app/services/trading/momentum_neural/ross_bench_scoring.py`). Reasons:",
            "",
        ]
        lines += [f"- {p}" for p in (problems or ["no reason recorded"])]
        lines.append("")
        return "\n".join(lines)

    for arm_name in sorted(rpi_by_arm):
        block = rpi_by_arm[arm_name] or {}
        index = block.get("index")
        lines += [f"### arm `{arm_name}`", ""]
        if not isinstance(index, Mapping):
            # Never reshape an unexpected payload into a number — show it verbatim.
            lines += ["```json", json.dumps(index, indent=2, default=str), "```", ""]
            continue

        lines += [
            f"- cases submitted: **{block.get('cases_submitted')}**, of which "
            f"**{block.get('cases_with_receipt')}** carried a replay receipt for this arm",
            f"- tier: **{index.get('tier')}** · nested schema: `{index.get('schema')}`",
            f"- `blended_score`: **null** — {index.get('blended_score_note') or 'not combined'}",
            f"- Ross equity supplied by the operator: "
            f"`{json.dumps(block.get('ross_equity_supplied') or {}, sort_keys=True)}` "
            f"(empty = unknown; {ROSS_EQUITY_IS_NOT_SIM_EQUITY})",
            "",
        ]

        capture = index.get("capture") or {}
        by_account = capture.get("by_account") or {}
        lines += ["**Capture** (Ross's winning symbol-days, in dollars, bucketed by account "
                  "— never pooled):", "",
                  "| account | numerator (CHILI $) | denominator (Ross $) | ratio | "
                  "%-of-equity | cases |", "|---|---|---|---|---|---|"]
        if not by_account:
            lines.append("| — | — | — | — | — | no symbol-day in this run had a stated Ross win |")
        for acct in sorted(by_account):
            b = by_account[acct] or {}
            eqn = b.get("equity_normalized")
            if isinstance(eqn, Mapping):
                pct = (f"{eqn.get('numerator_pct_equity')} / {eqn.get('denominator_pct_equity')} "
                       f"(over {len(eqn.get('cases_with_equity') or [])} of "
                       f"{len(eqn.get('cases_with_equity') or []) + len(eqn.get('cases_without_equity') or [])} cases)")
            else:
                pct = "null (no row stated Ross's equity)"
            lines.append("| " + " | ".join(_md_cell(v) for v in (
                acct, _fmt_usd(_as_float(b.get("numerator_usd"))),
                _fmt_usd(_as_float(b.get("denominator_usd"))),
                _fmt_ratio(b.get("ratio")), pct, _case_ids(b.get("cases")),
            )) + " |")
        if capture.get("rule"):
            lines += ["", f"> rule: {capture['rule']}"]
        lines.append("")

        lines += ["| metric | ratio | numerator | denominator | cases |",
                  "|---|---|---|---|---|"]
        for metric in _FLAT_RPI_METRICS:
            entry = index.get(metric)
            if not isinstance(entry, Mapping):
                continue
            lines.append("| " + " | ".join(_md_cell(v) for v in (
                metric,
                _fmt_ratio(entry.get("ratio")) if entry.get("ratio") is not None else "null",
                entry.get("numerator"), entry.get("denominator"),
                _case_ids(entry.get("cases")),
            )) + " |")
        liveness = index.get("liveness") or {}
        lines.append("| " + " | ".join(_md_cell(v) for v in (
            "liveness", "null", liveness.get("numerator"), liveness.get("denominator"),
            f"reason: {liveness.get('reason')}",
        )) + " |")
        lines.append("")
        for metric in _FLAT_RPI_METRICS + ("liveness",):
            entry = index.get(metric)
            if isinstance(entry, Mapping) and entry.get("rule"):
                lines.append(f"> **{metric}** rule: {entry['rule']}")
        lines.append("")

        excluded = index.get("excluded_cases") or []
        if excluded:
            lines += [f"**Excluded by the scorer ({len(excluded)}):**", "",
                      "| case | metric | reason |", "|---|---|---|"]
            for e in excluded:
                lines.append("| " + " | ".join(_md_cell(v) for v in (
                    (e or {}).get("case_id"), (e or {}).get("metric"), (e or {}).get("reason"),
                )) + " |")
            lines.append("")

        # Anything the scorer added that this renderer does not know about is listed rather
        # than dropped: a metric that appears in a later scorer version must not vanish here.
        known = {"schema", "tier", "case_count", "blended_score", "blended_score_note",
                 "capture", "liveness", "excluded_cases"} | set(_FLAT_RPI_METRICS)
        extra = sorted(set(index) - known)
        if extra:
            lines += [f"**Additional scorer keys not rendered above:** {', '.join(extra)}",
                      "", "```json",
                      json.dumps({k: index[k] for k in extra}, indent=2, default=str),
                      "```", ""]

    if problems:
        lines += ["**Scorer problems:**", ""] + [f"- {p}" for p in problems] + [""]
    return "\n".join(lines)


def render_unscorable(cases: Sequence[CaseRow]) -> str:
    bad = [c for c in cases if not c.scorable]
    lines = ["## Unscorable cases", ""]
    if not bad:
        lines += ["None — every case in the run tree carried ground truth, a confirmed pin and a "
                  "receipt in every arm.", ""]
        return "\n".join(lines)
    lines += [
        f"{len(bad)} of {len(cases)} cases left the scored population. Each reason is listed; "
        "a case is dropped, never silently defaulted.",
        "",
        "| case | symbol | date | reasons |",
        "|---|---|---|---|",
    ]
    for case in bad:
        lines.append("| " + " | ".join(_md_cell(v) for v in (
            case.case_id, case.symbol, case.date, "; ".join(case.unscorable_reasons),
        )) + " |")
    lines.append("")
    return "\n".join(lines)


def render_evidence_grade(doc: Mapping[str, Any], bundle: "Bundle") -> str:
    """Show the grade's WORKING, not just its answer.

    Per-row tiers, the coverage reasons behind them, and the adapter's own scorable/
    unscorable split — because "DIAGNOSTIC_ONLY" on its own is a stamp again, and a stamp
    without its arithmetic is what this section replaced.
    """
    ev = dict(doc.get("evidence_grade_derivation") or {})
    adaptation = dict(doc.get("adaptation") or {})
    summary = dict(adaptation.get("summary") or {})
    lines = [
        "## Evidence grade — how it was derived",
        "",
        f"`{doc.get('evidence_grade')}` (`{ev.get('grader_enum')}`). "
        f"Rule: {ev.get('rule')}.",
        "",
        "| tier | (case, arm) rows |",
        "|---|---:|",
    ]
    counts = ev.get("counts") or {}
    if counts:
        lines += [f"| `{k}` | {v} |" for k, v in counts.items()]
    else:
        lines.append("| — | 0 |")
    lines.append("")

    for reason in ev.get("reasons") or []:
        lines += [f"- {reason}", ""]

    lines += [
        f"- `sealed_coverage_supplied`: **{str(ev.get('sealed_coverage_supplied')).lower()}**",
        f"- `receipt_clock_tz`: `{doc.get('receipt_clock_tz')}` — {doc.get('receipt_clock_tz_basis')}",
        f"- `allow_dirty_tree`: **{str(doc.get('allow_dirty_tree')).lower()}** — "
        f"{doc.get('allow_dirty_tree_note')}",
        "",
    ]
    if not adaptation.get("available"):
        lines += [
            f"**The manifest adapter was not available**: {adaptation.get('problem')}",
            "",
        ]
    elif summary:
        lines += [
            f"Grading windows (`ross_manifest_adapter.phase_windows_from_manifest`): "
            f"**{summary.get('scorable_count')} of {summary.get('case_count')}** manifest "
            f"windows produced one; {summary.get('unscorable_count')} did not.",
            "",
        ]
        reasons = summary.get("unscorable_reason_counts") or {}
        if reasons:
            lines += ["| adapter refusal | n |", "|---|---:|"]
            lines += [f"| `{k}` | {v} |" for k, v in reasons.items()]
            lines.append("")

    cov = doc.get("coverage_reason_counts") or {}
    if cov:
        lines += [
            "Coverage reasons on the graded rows. On a row that FAILED these are "
            "disqualifying; on a row that passed at the diagnostic tier they are the "
            "caveats carried alongside the credit — `diagnostic:sealed_decision_coverage_"
            "not_bound` appears on every diagnostic row by construction.",
            "",
            "| coverage reason | rows |", "|---|---:|",
        ]
        lines += [f"| `{k}` | {v} |" for k, v in cov.items()]
        lines.append("")
    return "\n".join(lines)


def render_recorded_side(doc: Mapping[str, Any]) -> str:
    """What the recorded column rests on, and which verdicts it cannot place.

    The bench's recorded side is either measured (exported live-lane events) or inferred
    (the ledger's hand-written ``xref_verdict``). This section says which, per run, and
    names the verdicts the inference cannot place on the ladder — with the scorer's own
    measured reason, not a shrug. A bare ``unknown`` in the stage column with no explanation
    is exactly what this replaced.
    """
    rec = dict(doc.get("recorded_side") or {})
    lines = ["## The recorded side: measured, or inferred?", ""]
    total = int(rec.get("cases") or 0)
    exported = int(rec.get("cases_with_exported_events") or 0)
    lines += [
        f"**{exported} of {total}** case(s) carried exported live-lane events. The rest fall "
        f"back to the ledger's hand-written `xref_verdict`, which every stage cell labels "
        f"`[xref_verdict]`. Export the events with `{rec.get('exporter')}` to replace the "
        "inference with a measurement — with events in the window the scorer walks the "
        "ladder and never consults the verdict.",
        "",
    ]
    sources = rec.get("recorded_stage_sources") or {}
    if sources:
        lines += ["| recorded stage source | cases |", "|---|---:|"]
        lines += [f"| `{k}` | {v} |" for k, v in sources.items()]
        lines.append("")

    unplaceable = rec.get("unplaceable_in_this_run") or {}
    if not rec.get("placement_table_available"):
        lines += ["The scorer's verdict-placement table was unavailable, so no verdict can "
                  "be reported as placeable or not.", ""]
    elif not unplaceable:
        lines += [
            "Every `xref_verdict` in this run placed on the ladder. Placeable tokens: "
            + (", ".join(f"`{v}`" for v in rec.get("verdicts_placeable") or []) or "—")
            + ".",
            "",
        ]
    else:
        lines += [
            "**Verdicts this run could not place on the ladder.** These rows print as "
            "`unknown(<verdict>)`, never a bare `unknown`, and they are excluded from "
            "`recorded_liveness` rather than assigned a rung:",
            "",
        ]
        for token, entry in sorted(unplaceable.items()):
            bounds = entry.get("rung_bounds")
            bound_text = (f" Constrained to rungs `{bounds[0]}` .. `{bounds[1]}` inclusive."
                          if bounds else "")
            lines += [
                f"- **`{token}`** — {entry.get('count')} case(s): "
                f"{', '.join(str(c) for c in entry.get('cases') or [])}.{bound_text}",
                f"  {entry.get('reason')}",
                "",
            ]
    return "\n".join(lines)


def render_provenance(provenance: Mapping[str, Any], bundle: "Bundle") -> str:
    lines = ["## Provenance", ""]

    def block(title: str, folded: Sequence[Mapping[str, Any]], *, warn_split: str) -> None:
        # ``lines.extend`` and not ``lines += ...``: an augmented assignment inside a nested
        # function rebinds the name locally and shadows the enclosing list.
        lines.extend([f"### {title}", ""])
        if not folded:
            lines.extend(["(no receipt carried this)", ""])
            return
        if len(folded) > 1:
            lines.extend(
                [f"WARNING — **{warn_split}**: {len(folded)} distinct values across the run tree:", ""]
            )
        for item in folded:
            lines.extend(["```json", json.dumps(item["value"], indent=2, default=str), "```",
                          f"runs: {', '.join('`%s`' % r for r in item['runs'])}", ""])

    plan = provenance.get("bench_plan") or {}
    lines += ["### What each arm actually changed", ""]
    if not plan.get("present"):
        lines += [
            f"**`{BENCH_PLAN_FILE}` was not found in the run dir.** Without it this report can "
            "show that an arm moved the number but not WHAT it overrode, and cannot show the "
            "verified build ref or the env fence. A delta whose treatment is unnamed is not a "
            "finding.", "",
        ]
    else:
        overrides = plan.get("arm_overrides") or {}
        lines += ["| arm | env overrides |", "|---|---|"]
        for name in sorted(overrides):
            body = json.dumps(overrides[name], sort_keys=True) if overrides[name] else "(none — this is the base)"
            lines.append(f"| `{_md_cell(name)}` | {_md_cell(body)} |")
        tree = plan.get("tree") or {}
        lines += ["",
                  f"- build tree: `{tree.get('build')}` at ref `{tree.get('ref')}`, "
                  f"verified HEAD `{tree.get('head')}`",
                  f"- sentinel: `{tree.get('sentinel')}` in `{tree.get('sentinel_file')}`",
                  ""]
        fence = plan.get("env_fence") or {}
        if fence:
            lines += ["```json", json.dumps(fence, indent=2, default=str), "```", ""]

    lines += ["### Ground-truth inputs", "",
              "| input | path | schema | generated_at |", "|---|---|---|---|",
              "| manifest | " + " | ".join(_md_cell(bundle.manifest_meta.get(k))
                                           for k in ("path", "schema", "generated_at")) + " |",
              "| pins | " + " | ".join(_md_cell(bundle.pins_meta.get(k))
                                       for k in ("path", "schema", "generated_at")) + " |",
              ""]

    block("Tree that ran", provenance.get("tree") or [],
          warn_split="the arms did not run on one tree, so their dollars are not an A/B")
    block("Sink", provenance.get("sink") or [],
          warn_split="the arms did not share one sink reset")
    block("Tape sources", provenance.get("tape_sources") or [],
          warn_split="the arms read different tapes")
    block("Mock fill model", provenance.get("mock") or [],
          warn_split="the arms filled against different mock configs; their PnL is not comparable")

    lines += ["### Per-run tape / density / venue", "",
              "| run | tick rows | nbbo rows | depth rows | ticks/s | nbbo/s | grid steps | "
              "nbbo_vendor | exec family | venue | certification failures |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in provenance.get("per_run") or []:
        lines.append("| " + " | ".join(_md_cell(v) for v in (
            r.get("run"), r.get("tick_rows"), r.get("nbbo_rows"), r.get("depth_rows"),
            r.get("ticks_per_second"), r.get("nbbo_rows_per_second"), r.get("grid_steps"),
            r.get("nbbo_vendor"), r.get("execution_family"), r.get("venue"),
            ", ".join(r.get("certification_failures") or []) or "none",
        )) + " |")
    lines += ["",
              "`nbbo_vendor` is DERIVED: the receipt has no scalar field for it; it is the single "
              "key of `tape_sources[\"momentum_nbbo_spread_tape\"]` "
              "(app/services/trading/momentum_neural/ross_replay_benchmark.py:336-338). More than "
              "one key prints as `AMBIGUOUS:` — a concatenated two-vendor tape, not a vendor.",
              ""]

    lines += ["### Knobs and their derivations", "",
              "| knob | value | derivation |", "|---|---|---|"]
    for k in provenance.get("knobs") or []:
        lines.append("| " + " | ".join(_md_cell(v) for v in (
            k["knob"], k["value"], k["derivation"])) + " |")
    unattributed = int(provenance.get("knobs_unattributed") or 0)
    total = len(provenance.get("knobs") or [])
    lines += ["",
              f"**{unattributed} of {total} knobs are `UNATTRIBUTED`.** The reporter carries no "
              "derivation table of its own on purpose: a derivation must travel with the knob "
              "from whoever chose it (the bench runner's `knobs` block on the run wrapper). An "
              "unattributed knob is an unexplained default, and this report will not invent an "
              "explanation for one.",
              ""]

    if bundle.alias_hits:
        lines += ["### Cross-component field names actually used", "",
                  "| reporter field | key found in the sibling document |", "|---|---|"]
        for k in sorted(bundle.alias_hits):
            lines.append(f"| {_md_cell(k)} | `{_md_cell(bundle.alias_hits[k])}` |")
        lines.append("")
    if bundle.load_warnings:
        lines += ["### Load warnings", ""] + [f"- {w}" for w in bundle.load_warnings] + [""]
    return "\n".join(lines)


def render_report(doc: Mapping[str, Any], bundle: "Bundle") -> str:
    cases = bundle.cases
    provenance = doc["provenance"]
    parts = [render_header(doc), render_limitations(provenance)]
    others = [a for a in bundle.arms if a != bundle.base_arm]
    parts.append("## Per-case results\n")
    if not others:
        # One arm only: still print the table, with the arm column equal to base, so the
        # shape of the report never changes with the shape of the run.
        parts.append(render_case_table(cases, base_arm=bundle.base_arm, arm=bundle.base_arm))
    for arm_name in others:
        parts.append(f"### `{bundle.base_arm}` vs `{arm_name}`\n")
        parts.append(render_case_table(cases, base_arm=bundle.base_arm, arm=arm_name))
    parts.append(render_rpi(doc.get("rpi_by_arm") or {}, doc.get("scorer_problems") or []))
    # Both of these sit ABOVE the unscorable list and the provenance dump for the same
    # reason the limitations sit above the results: a reader who has to scroll past the
    # numbers to learn what the numbers rest on has already read them.
    parts.append(render_evidence_grade(doc, bundle))
    parts.append(render_recorded_side(doc))
    parts.append(render_unscorable(cases))
    parts.append(render_provenance(provenance, bundle))
    return "\n".join(parts).rstrip() + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# DOCUMENT
# ─────────────────────────────────────────────────────────────────────────────

def build_rpi_document(
    bundle: "Bundle",
    *,
    rpi_by_arm: Mapping[str, Any],
    scorer_problems: Sequence[str],
    provenance: Mapping[str, Any],
    generated_at_utc: Optional[str] = None,
    evidence: Optional[Mapping[str, Any]] = None,
    recorded_side: Optional[Mapping[str, Any]] = None,
    naive_clock_tz=None,
    allow_dirty_tree: bool = False,
) -> dict:
    """The ``chili.ross_parity_index.v1`` document.

    ``causal_use_allowed`` and ``admission_claim`` are top-level and unconditional; nothing
    in the pipeline can set them to anything else, because they are properties of the Tier-1
    harness rather than of a particular run's result. ``evidence_grade`` is NOT one of them —
    it is the grader's derived answer (see ``derive_evidence_grade``), and a run that earned
    nothing stamps UNSCORABLE.
    """
    ev = dict(evidence or derive_evidence_grade(bundle.cases))
    return {
        "schema": RPI_SCHEMA,
        # SCHEMA-ID COLLISION, resolved 2026-09-04 rather than merely reported: the scorer's
        # ``ross_parity_index`` result used to declare ``chili.ross_parity_index.v1`` too,
        # with a DIFFERENT shape (one arm's four metrics vs this run-level envelope), so one
        # file carried two objects under one id. The scorer's block is now
        # ``chili.ross_parity_index_metrics.v1`` (ross_bench_scoring.PARITY_INDEX_SCHEMA) and
        # this envelope kept the original id. Each arm's scorer result is still nested
        # verbatim at ``rpi_by_arm[<arm>].index`` with its own ``schema`` key.
        "schema_note": (
            "this document is the run-level envelope; the per-arm metric blocks nested at "
            "rpi_by_arm[<arm>].index declare chili.ross_parity_index_metrics.v1, a "
            "different schema id for a different shape"
        ),
        "generated_at_utc": generated_at_utc or datetime.now(timezone.utc).isoformat(),
        "evidence_grade": ev.get("stamp"),
        "evidence_grade_grader_enum": ev.get("grader_enum"),
        "evidence_grade_derivation": ev,
        "evidence_grade_note": (
            "DERIVED from ross_replay_benchmark.grade_recap_phase_window's per-row "
            "PhaseBenchmarkGrade.evidence_grade — not a constant. A run whose windows fail "
            "their coverage checks stamps UNSCORABLE."
        ),
        "recorded_side": dict(recorded_side or {}),
        "adaptation": {
            "available": bundle.adapter_available,
            "problem": bundle.adapter_problem,
            "summary": dict(bundle.adaptation_summary),
        },
        "coverage_reason_counts": coverage_reason_counts(bundle.cases),
        "receipt_clock_tz": str(getattr(naive_clock_tz or timezone.utc, "key", None)
                                or naive_clock_tz or timezone.utc),
        "receipt_clock_tz_basis": (
            "the driver's env window clocks are naive (replay_v3_fsm_window.py:154-156); "
            "the bench runner writes them as UTC-naive "
            "(ross_replay_bench.et_clock_to_utc / _parse_utc_field). Overridable with "
            "--receipt-clock-tz for a hand-run receipt."
        ),
        "allow_dirty_tree": bool(allow_dirty_tree),
        "allow_dirty_tree_note": (
            "false means a receipt whose tree.dirty is true is REFUSED coverage, so its rows "
            "grade unscorable — the grader's own default, because a dirty tree means tree.tree "
            "does not describe the code that ran. This repository routinely carries untracked "
            "files, so an operator who accepts that has to say so with --allow-dirty-tree."
        ),
        "causal_use_allowed": CAUSAL_USE_ALLOWED,
        "admission_claim": ADMISSION_CLAIM,
        "admission_claim_note": (
            "The Tier-1 harness force-arms the FSM (seeded queued_live session, "
            "risk_gate_allows=True), so admission is supplied by the fixture. No output of this "
            "document may be read as an admission or admission-latency claim."
        ),
        "run_dir": bundle.run_dir,
        "manifest": dict(bundle.manifest_meta),
        "pins": dict(bundle.pins_meta),
        "arms": list(bundle.arms),
        "base_arm": bundle.base_arm,
        "leader_board_mode": LEADER_BOARD_MODE,
        "leader_board_limitation": LIMITATION_LEADER_BOARD,
        "depth_levers_unmeasurable": bool(provenance.get("depth_levers_unmeasurable")),
        "depth_limitation": provenance.get("depth_limitation_text"),
        "ross_equity_note": ROSS_EQUITY_IS_NOT_SIM_EQUITY,
        "cases": [c.to_public_case() for c in bundle.cases],
        "unscorable": [
            {"case_id": c.case_id, "symbol": c.symbol, "date": c.date,
             "reasons": list(c.unscorable_reasons)}
            for c in bundle.cases if not c.scorable
        ],
        "rpi_by_arm": dict(rpi_by_arm),
        "scorer_problems": list(scorer_problems),
        "provenance": dict(provenance),
        "load_warnings": list(bundle.load_warnings),
        "field_aliases_used": dict(bundle.alias_hits),
    }


def build_report(
    bundle: "Bundle",
    *,
    scorer: Any,
    grader: Any = None,
    ross_equity: Optional[Mapping[str, float]] = None,
    knob_derivations: Optional[Mapping[str, Any]] = None,
    generated_at_utc: Optional[str] = None,
    naive_clock_tz=None,
    allow_dirty_tree: bool = False,
    grader_problem: Optional[str] = None,
) -> tuple[dict, str]:
    """Assemble ``(rpi_document, report_markdown)``.

    ``scorer`` and ``grader`` are INJECTED rather than imported here so the reporter can be
    tested without either module and so a swap is a call-site decision, not an import-time
    one. The scorer must expose ``classify_first_divergence`` and ``ross_parity_index``; the
    grader must expose ``grade_recap_phase_window`` and
    ``diagnostic_coverage_from_replay_receipt``. A module missing any of those degrades that
    section to an explicit "unavailable" with the reason printed — and, for the grader, to
    an ``unscorable`` evidence grade, because a grade nothing checked is not a grade.

    Order matters twice:

    * stages are classified BEFORE the index, so each case carries its ``recorded_stage``
      into ``ross_parity_index`` and ``recorded_liveness`` is measured from the graded stage
      rather than re-derived from the ledger verdict a second time;
    * the evidence grade is derived BEFORE the document is built, because the document's
      top-level stamp IS that derivation's answer.
    """
    problems = list(apply_scorer_stages(bundle.cases, scorer))
    rpi_by_arm, rpi_problems = compute_rpi(
        bundle.cases, scorer, arms=bundle.arms, ross_equity=dict(ross_equity or {}),
    )
    problems += rpi_problems

    grade_problems: list = []
    if grader is None:
        grader_problem = grader_problem or (
            "no grader was injected; the evidence grade is unscorable because nothing "
            "checked it"
        )
    else:
        grade_problems = grade_evidence(
            bundle.cases, grader,
            naive_clock_tz=naive_clock_tz or timezone.utc,
            allow_dirty_tree=allow_dirty_tree,
        )
    problems += grade_problems
    evidence = derive_evidence_grade(
        bundle.cases, grader_problem=grader_problem,
        adapter_problem=bundle.adapter_problem,
    )

    provenance = collect_provenance(
        bundle.cases, arms=bundle.arms, bench_plan=bundle.bench_plan,
        knob_derivations=knob_derivations,
    )
    doc = build_rpi_document(
        bundle, rpi_by_arm=rpi_by_arm, scorer_problems=problems,
        provenance=provenance, generated_at_utc=generated_at_utc,
        evidence=evidence, recorded_side=recorded_side_limitation(bundle.cases, scorer),
        naive_clock_tz=naive_clock_tz or timezone.utc,
        allow_dirty_tree=allow_dirty_tree,
    )
    return doc, render_report(doc, bundle)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SCORER_MODULE = "app.services.trading.momentum_neural.ross_bench_scoring"

# The bench runner's first arm is named ``base`` (its ``--arms base[,name=arm.json...]``
# contract). This is a NAME, not a tuned value; when it is absent from the run tree the
# reporter aborts and lists the arm directories it actually found rather than silently
# electing one.
DEFAULT_BASE_ARM = "base"


def load_bundle(
    run_dir: str,
    manifest_path: str,
    pins_path: str,
    *,
    base_arm: str = DEFAULT_BASE_ARM,
    account_normalizer: Optional[Callable[[Any], str]] = None,
    adapter: Any = None,
    adapter_problem: Optional[str] = None,
) -> Bundle:
    runs = discover_runs(run_dir)
    if not runs:
        raise SystemExit(f"[rossbench_report] no <case>/<arm>/ directories under {run_dir!r}")

    arm_names: list = []
    for arms in runs.values():
        for name in arms:
            if name not in arm_names:
                arm_names.append(name)
    arm_names.sort(key=lambda n: (n != base_arm, n))  # base first, then alphabetical
    if base_arm not in arm_names:
        raise SystemExit(
            f"[rossbench_report] --base-arm {base_arm!r} is not in the run tree; found "
            f"{arm_names!r}. Name the base explicitly rather than letting the report elect one."
        )

    manifest = _read_json(manifest_path)
    pins = _read_json(pins_path)
    warnings: list = []
    if isinstance(manifest, Mapping) and manifest.get("schema") != "chili.ross_ground_truth_manifest.v1":
        warnings.append(f"manifest schema is {manifest.get('schema')!r}, expected "
                        "'chili.ross_ground_truth_manifest.v1'")
    if isinstance(pins, Mapping) and pins.get("schema") != "chili.ross_event_pins.v1":
        warnings.append(f"pins schema is {pins.get('schema')!r}, expected 'chili.ross_event_pins.v1'")

    bench_plan, plan_warnings = load_bench_plan(run_dir)
    warnings.extend(plan_warnings)
    if account_normalizer is None:
        warnings.append(
            "account buckets were computed by the reporter's FALLBACK normalizer because the "
            "scorer module was unavailable; ross_bench_scoring.normalize_account is the single "
            "source of that vocabulary"
        )
    cases, aliases = build_cases(
        runs, index_manifest(manifest), index_pins(pins), all_arms=arm_names,
        run_dir=run_dir, account_normalizer=account_normalizer,
        plan_manifest_ids=manifest_ids_from_plan(bench_plan),
    )

    # The adapter join. Runs over the WHOLE manifest, not just the benched cases, so the
    # report can state the corpus-level denominator ("n of m windows carry a grading
    # window") rather than only the slice this run happened to touch.
    adapted, adaptation, adapt_problem = adapt_manifest(manifest, pins, adapter)
    if adapt_problem:
        warnings.append(adapt_problem)
    warnings.extend(attach_phase_windows(cases, adapted))

    return Bundle(
        run_dir=os.path.abspath(run_dir),
        cases=cases,
        arms=arm_names,
        base_arm=base_arm,
        adapted_cases=list(adapted),
        adaptation_summary=dict(adaptation),
        adapter_available=(adapter is not None and not adapt_problem),
        adapter_problem=(adapter_problem or adapt_problem),
        manifest_meta={"path": os.path.abspath(manifest_path),
                       "schema": (manifest or {}).get("schema") if isinstance(manifest, Mapping) else None,
                       "generated_at": (manifest or {}).get("generated_at") if isinstance(manifest, Mapping) else None},
        pins_meta={"path": os.path.abspath(pins_path),
                   "schema": (pins or {}).get("schema") if isinstance(pins, Mapping) else None,
                   "generated_at": (pins or {}).get("generated_at") if isinstance(pins, Mapping) else None},
        bench_plan=bench_plan,
        alias_hits=aliases,
        load_warnings=warnings,
    )


def _scorer_why(scorer: Any) -> str:
    """The import failure behind a missing scorer, as a prefix for the problem line.

    MEASURED 2026-09-04: run from a tree without a ``.env``, importing the scorer pulls the
    app Settings and fails on ``database_url Field required``; the report then said only
    "scorer exposes no classify_first_divergence" and every stage read ``unavailable`` --
    which looks like a scorer regression when it is an environment gap. The reason lives on
    the stub ``resolve_scorer`` returns; this puts it in front of the reader.
    """
    why = getattr(scorer, "unavailable_reason", None)
    return f"{why}; " if why else ""


def resolve_scorer(module_name: str) -> Any:
    """Import the scorer, or return a stub whose absence is reported, never papered over."""
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[rossbench_report] scorer %s unavailable: %r", module_name, exc)

        class _MissingScorer:
            unavailable_reason = f"import {module_name} failed: {exc!r}"
        return _MissingScorer()


def write_text(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    # newline="\n": Windows text mode rewrites every \n to \r\n, which changes the bytes of an
    # otherwise identical report and breaks byte-comparison of a no-op A/B
    # (reference_python_write_text_crlf_windows).
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--run-dir", required=True,
                    help="bench out-dir containing <case>/<arm>/{run.json,timeline.jsonl}")
    ap.add_argument("--manifest", required=True, help="chili.ross_ground_truth_manifest.v1")
    ap.add_argument("--pins", required=True, help="chili.ross_event_pins.v1")
    ap.add_argument("--base-arm", default=DEFAULT_BASE_ARM,
                    help="arm every other arm is differenced against (default: %(default)s)")
    ap.add_argument("--out-dir", default=None,
                    help="where report.md + rpi.json are written (default: --run-dir)")
    ap.add_argument("--scorer-module", default=DEFAULT_SCORER_MODULE,
                    help="module exposing classify_first_divergence / ross_parity_index")
    ap.add_argument("--adapter-module", default=DEFAULT_ADAPTER_MODULE,
                    help="module exposing phase_windows_from_manifest (the grading windows)")
    ap.add_argument("--grader-module", default=DEFAULT_GRADER_MODULE,
                    help="module exposing grade_recap_phase_window / "
                         "diagnostic_coverage_from_replay_receipt (the evidence grade)")
    ap.add_argument("--receipt-clock-tz", default=DEFAULT_RECEIPT_CLOCK_TZ,
                    help="timezone the driver's NAIVE env clocks are in. Default "
                         "%(default)s, which is what the bench runner writes "
                         "(ross_replay_bench.et_clock_to_utc). State it explicitly for a "
                         "hand-run receipt; the grader refuses to guess.")
    ap.add_argument("--allow-dirty-tree", action="store_true",
                    help="accept a receipt whose tree.dirty is true. OFF by default (the "
                         "grader's own default): a dirty tree means tree.tree does not "
                         "describe the code that ran, and those rows grade unscorable.")
    ap.add_argument("--ross-equity", default=None, metavar="main=USD,small=USD",
                    help="Ross's per-account equity for Capture's %%-of-equity denominator. "
                         "OMIT IT unless the operator states the figure: the default is "
                         "'unknown', which reports equity_normalized as null. This is NOT the "
                         "driver's env.EQUITY (that is CHILI's sim account).")
    ap.add_argument("--knob-derivations", default=None, metavar="PATH",
                    help="JSON mapping knob -> derivation string (or {value, derivation}). "
                         "The reporter ships NO derivation table; a knob with no derivation "
                         "from any source prints as UNATTRIBUTED.")
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 when the report had to record an unattributed knob, an "
                         "unreadable receipt, or a scorer problem")
    args = ap.parse_args(argv)

    knob_derivations = _read_json(args.knob_derivations) if args.knob_derivations else None
    if knob_derivations is not None and not isinstance(knob_derivations, Mapping):
        raise SystemExit("[rossbench_report] --knob-derivations must be a JSON object")
    ross_equity = parse_ross_equity(args.ross_equity)
    naive_clock_tz = parse_clock_tz(args.receipt_clock_tz)
    scorer = resolve_scorer(args.scorer_module)
    adapter, adapter_problem = resolve_module(args.adapter_module, what="manifest adapter")
    grader, grader_problem = resolve_module(args.grader_module, what="grader")
    bundle = load_bundle(
        args.run_dir, args.manifest, args.pins, base_arm=args.base_arm,
        account_normalizer=getattr(scorer, "normalize_account", None),
        adapter=adapter, adapter_problem=adapter_problem,
    )
    doc, markdown = build_report(bundle, scorer=scorer, grader=grader,
                                 ross_equity=ross_equity,
                                 knob_derivations=knob_derivations,
                                 naive_clock_tz=naive_clock_tz,
                                 allow_dirty_tree=args.allow_dirty_tree,
                                 grader_problem=grader_problem)

    out_dir = args.out_dir or args.run_dir
    report_path = os.path.join(out_dir, "report.md")
    rpi_path = os.path.join(out_dir, "rpi.json")
    write_text(report_path, markdown)
    write_text(rpi_path, json.dumps(doc, indent=2, default=str) + "\n")

    unscorable = len(doc.get("unscorable") or [])
    unattributed = int((doc.get("provenance") or {}).get("knobs_unattributed") or 0)
    problems = list(doc.get("scorer_problems") or []) + list(doc.get("load_warnings") or [])
    logger.info("[rossbench_report] wrote %s", report_path)
    logger.info("[rossbench_report] wrote %s", rpi_path)
    logger.info("[rossbench_report] cases=%d unscorable=%d arms=%s unattributed_knobs=%d "
                "depth_levers_unmeasurable=%s",
                len(bundle.cases), unscorable, bundle.arms, unattributed,
                doc.get("depth_levers_unmeasurable"))
    for p in problems:
        logger.info("[rossbench_report]   problem: %s", p)

    if args.strict and (unattributed or problems):
        logger.error("[rossbench_report] --strict: %d unattributed knob(s), %d problem(s)",
                     unattributed, len(problems))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
